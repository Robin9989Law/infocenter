"""信息中心 · 写锁、排队意图与提交闸门模块（File Locks, Change Queue & Guard Gate）。

负责：
1. 写锁与租约（Locks & Leases）：单一写者互斥，续期检测盘上写入（st_mtime >= acquired_at），--stay 封顶。
2. 意图排队（Change Queue）：拿不到锁时不争抢、不发信，提议上链并写进自己的 TODO（queue_change / list_queue / merge_queue）。
3. 提交闸门（Guard Gate）：Git hook 检查暂存区改动是否全部被未过期写锁覆盖（guard_changes）。
从 governance.py 解耦为独立业务结构 LocksMixin。
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infocenter.common import (
    LOCK_TTL,
    LOCK_TTL_MAX,
    MailError,
    _emit_task_event,
    _holder_names,
    _may_force,
    _parse_utc,
    _same_agent,
    _task_text,
    beijing_time,
    normalize_case_id,
    normalize_name,
    normalize_window,
    utcnow,
)

_CASE_FOLD_CACHE: dict[str, bool] = {}


def _split_raw_paths(paths) -> list[str]:
    if isinstance(paths, str):
        return [part for part in re.split(r"[\s,]+", paths) if part.strip()]
    if isinstance(paths, (list, tuple, set)):
        return [str(part).strip() for part in paths if str(part).strip()]
    return []


def _relative_lock_hint(paths, client_cwd: str | None = None) -> str:
    """相对路径提示。"""
    items = _split_raw_paths(paths)
    if not items:
        return ""
    shown: list[str] = []
    for item in items:
        expanded = Path(item).expanduser()
        if expanded.is_absolute():
            continue
        try:
            landed = str(expanded.resolve())
        except OSError:
            landed = str(expanded)
        shown.append(f"{item} → {landed}")
    if not shown:
        return ""
    head = "；".join(shown[:4])
    more = f"（另有 {len(shown) - 4} 个）" if len(shown) > 4 else ""
    text = (
        f"收到相对路径，按服务端所在目录解析（{head}{more}）。"
        "这可能不是你敲命令的那个文件。没有拒绝。请先转成绝对路径再发。"
    )
    cwd = str(client_cwd or "").strip()
    if cwd:
        text += f" 客户端目录是 {cwd}。"
    return text


def _lock_paths(paths) -> list[str]:
    """把路径规整成绝对路径列表（去重）。"""
    items = _split_raw_paths(paths)
    if not items:
        raise MailError("path_required", "至少要给一个路径", 400)
    out: list[str] = []
    for item in items:
        try:
            path = str(Path(item).expanduser().resolve())
        except OSError:
            path = str(Path(item).expanduser())
        if path not in out:
            out.append(path)
    return out


def _chmod_locked(path: str) -> str | None:
    """锁＝只读：没人拿钥匙时，文件只能读、不能改。"""
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"读权限位失败：{exc.strerror or exc}"
    readonly = mode & ~0o222
    if readonly == mode:
        return None
    try:
        os.chmod(path, readonly)
    except OSError as exc:
        return f"改回只读失败：{exc.strerror or exc}"
    try:
        after = os.stat(path).st_mode & 0o7777
    except OSError as exc:
        return f"复核权限位失败：{exc.strerror or exc}"
    if after & 0o222:
        return "权限位没落地（改完仍是可写）"
    return None


def _chmod_writable(path: str) -> str | None:
    """拿钥匙期间可写：只给 owner 加写位。"""
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"读权限位失败：{exc.strerror or exc}"
    writable = mode | 0o200
    if writable != mode:
        try:
            os.chmod(path, writable)
        except OSError as exc:
            return f"改成可写失败：{exc.strerror or exc}"
    try:
        if os.stat(path).st_mode & 0o200:
            return None
    except OSError as exc:
        return f"复核不了：{exc.strerror or exc}"
    return "权限位没落地（复核仍是只读）"


def _fingerprint(path: str) -> tuple[int, int] | None:
    """文件的 (设备号, inode)。"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _parent_folds_case(path: str) -> bool:
    """父目录会不会把大小写不同的名字当成同一个文件。"""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    hit = _CASE_FOLD_CACHE.get(parent)
    if hit is not None:
        return hit
    folded = False
    if os.path.isdir(parent):
        nonce = f".ic-case-{os.getpid()}-{threading.get_ident()}"
        probe = os.path.join(parent, nonce)
        other = os.path.join(parent, nonce.upper())
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                folded = probe != other and os.path.exists(other)
            finally:
                try:
                    os.unlink(probe)
                except OSError:
                    pass
        except OSError:
            folded = False
    _CASE_FOLD_CACHE[parent] = folded
    return folded


def _same_file(a: str, b: str) -> bool:
    """两个路径是不是同一个文件。"""
    if a == b:
        return True
    left, right = _fingerprint(a), _fingerprint(b)
    if left is not None and left == right:
        return True
    if left is None and right is None and _parent_folds_case(a) and _parent_folds_case(b):
        return os.path.abspath(a).casefold() == os.path.abspath(b).casefold()
    return False


def _rows_covering(rows, paths: list[str]) -> list:
    """锁行里，和 paths 中任一条是同一个文件的那些。"""
    out = []
    seen: set[str] = set()
    for row in rows:
        stored = row["path"]
        if stored in seen:
            continue
        if any(_same_file(stored, path) for path in paths):
            out.append(row)
            seen.add(stored)
    return out


def _lock_ttl(value) -> int:
    try:
        seconds = int(value if value not in (None, "") else LOCK_TTL)
    except (TypeError, ValueError):
        seconds = LOCK_TTL
    return max(30, min(seconds, LOCK_TTL_MAX))


def _lock_expiry(now_iso: str, ttl: int) -> str:
    base = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock_stay_cap(acquired_at: str) -> str:
    """无进度续期的天花板：拿锁时刻 + 2×LOCK_TTL（默认一共 1 小时）。"""
    return _lock_expiry(acquired_at, 2 * LOCK_TTL)


def _lock_has_progress(path: str, acquired_at: str) -> bool:
    """自这把锁的 acquired_at 起，盘上有没有写入痕迹。"""
    try:
        mtime = int(os.stat(path).st_mtime)
    except OSError:
        return False
    moment = _parse_utc(acquired_at)
    if moment is None:
        return True
    return mtime >= int(moment.timestamp())


def _matching_lock(rows, path: str):
    """锁表里和 path 是同一个文件的那一行。"""
    for row in rows:
        if row["path"] == path or _same_file(row["path"], path):
            return row
    return None


def _restore_readonly(conn: sqlite3.Connection, rows) -> dict[str, str]:
    """放钥匙 ⇒ 文件变回只读。返回未成功的 {路径: 原因}。"""
    remaining = conn.execute("SELECT path FROM locks").fetchall()
    stuck: dict[str, str] = {}
    for row in rows:
        path = row["path"]
        if any(_same_file(other["path"], path) for other in remaining):
            _chmod_writable(path)
            continue
        why = _chmod_locked(path)
        if why:
            stuck[path] = why
    return stuck


def _release_window_locks(conn: sqlite3.Connection, window_id: str, now: str, why: str) -> int:
    """窗口离场 / 被判死：放掉它名下的写锁并逐条留痕。"""
    rows = conn.execute("SELECT * FROM locks WHERE window_id = ?", (window_id,)).fetchall()
    if not rows:
        return 0
    conn.execute("DELETE FROM locks WHERE window_id = ?", (window_id,))
    _restore_readonly(conn, rows)
    for row in rows:
        _emit_task_event(
            conn, row["task_id"] or "", row["agent"], "unlock", "", "",
            {
                "path": row["path"], "why": why, "auto": True, "window_id": window_id,
                "held_since": row["acquired_at"],
            },
            now,
        )
    return len(rows)


def _lock_remaining(expires_iso: str) -> int:
    """锁还剩多少秒。"""
    moment = _parse_utc(expires_iso)
    if moment is None:
        return 0
    return int((moment - datetime.now(timezone.utc)).total_seconds())


def _locks_sweep(conn: sqlite3.Connection, now_iso: str) -> int:
    """过期锁自动释放并留痕。"""
    rows = conn.execute("SELECT * FROM locks WHERE expires_at <= ?", (now_iso,)).fetchall()
    if not rows:
        return 0
    conn.execute("DELETE FROM locks WHERE expires_at <= ?", (now_iso,))
    _restore_readonly(conn, rows)
    for row in rows:
        _emit_task_event(
            conn, row["task_id"] or "", row["agent"], "lock_expire", "", "",
            {"path": row["path"], "expired_at": row["expires_at"], "auto": True,
             "window_id": row["window_id"] or ""},
            now_iso,
        )
    return len(rows)


def _lock_view(row: sqlite3.Row) -> dict:
    return {
        "path": row["path"],
        "agent": row["agent"],
        "window_id": row["window_id"] or "",
        "task_id": row["task_id"] or "",
        "note": row["note"] or "",
        "dirty_at_lock": row["dirty_at_lock"] or "",
        "acquired_at": row["acquired_at"],
        "acquired_at_beijing": beijing_time(row["acquired_at"]),
        "expires_at": row["expires_at"],
        "expires_at_beijing": beijing_time(row["expires_at"]),
        "expires_in_seconds": _lock_remaining(row["expires_at"]),
    }


class LocksMixin:
    """写锁、排队意图与提交闸门业务混入类。"""

    def lock_acquire(
        self,
        agent: str,
        paths,
        window_id: str | None = None,
        task_id: str | None = None,
        note: str | None = None,
        ttl: int = LOCK_TTL,
        force: bool = False,
        skip_locked: bool = False,
        dirty: dict | None = None,
        stay: bool = False,
        client_cwd: str | None = None,
    ) -> dict:
        """拿锁（可一次拿多个路径）。"""
        agent = normalize_name(agent)
        window_id = normalize_window(window_id)
        wanted = _lock_paths(paths)
        ttl_seconds = _lock_ttl(ttl)
        now = utcnow()
        expires = _lock_expiry(now, ttl_seconds)
        note_text = _task_text(note, 400)
        task = normalize_case_id(task_id) if task_id else None
        dirty_text = {
            str(key): _task_text(str(val), 200)
            for key, val in (dirty or {}).items()
            if str(val).strip()
        }
        with self._session(immediate=True) as conn:
            if force and not _may_force(conn, agent, wanted):
                raise MailError(
                    "not_coordinator",
                    f"{agent} 不能抢锁：项目内的路径要那个项目的 coordinator，项目外要 manager（human 天然算）",
                    403,
                )
            _locks_sweep(conn, now)
            held = []
            skipped: list[dict] = []
            existing = conn.execute("SELECT * FROM locks").fetchall()
            for path in wanted:
                row = conn.execute("SELECT * FROM locks WHERE path = ?", (path,)).fetchone()
                if row is None:
                    row = next(
                        (r for r in existing if _same_file(r["path"], path)),
                        None,
                    )
                if row is None or _same_agent(row["agent"], agent) or force:
                    continue
                item = {
                    "path": path,
                    "holds": row["path"],
                    "agent": row["agent"],
                    "since": row["acquired_at"],
                }
                if skip_locked:
                    skipped.append(item)
                else:
                    held.append(item)
            if skipped:
                wanted = [
                    path for path in wanted if path not in {item["path"] for item in skipped}
                ]
            if held:
                raise MailError(
                    "locked",
                    "这些路径正被别人锁着："
                    + "、".join(f"{h['path']}（{h['agent']}，自 {h['since']}）" for h in held)
                    + "；别硬写。把要改的事记下来（`ic queue <路径> --note \"我要改什么\"`，会写进你自己的 TODO）。"
                    "先做别的；轮到这条时 `ic lock`，能拿到就做，拿不到就跳过。不要发信、不要干等",
                    409,
                )
            renewals: dict[str, sqlite3.Row] = {}
            lacking: list[str] = []
            for path in wanted:
                row = _matching_lock(existing, path)
                if row is None or not _same_agent(row["agent"], agent):
                    continue
                renewals[path] = row
                if not _lock_has_progress(path, row["acquired_at"]):
                    lacking.append(path)
            if lacking and not stay:
                raise MailError(
                    "no_progress",
                    "这些路径自拿锁后盘上没有写入痕迹："
                    + "、".join(lacking)
                    + "。不给第二把租约。别硬写。把要改的事记下来"
                    "（`ic queue <路径> --note \"我要改什么\"`，会写进你自己的 TODO）。"
                    "先做别的；这把锁到期后自然回到可拿。不要发信、不要干等",
                    409,
                )
            if lacking and stay:
                for path in lacking:
                    row = renewals[path]
                    cap = _lock_stay_cap(row["acquired_at"])
                    if now >= cap or (row["expires_at"] or "") >= cap:
                        raise MailError(
                            "stay_exhausted",
                            "延过一次了，要再拿请先 `ic unlock` 放一次再拿",
                            409,
                        )
            plans: dict[str, tuple[str, str, bool]] = {}
            for path in wanted:
                row = renewals.get(path)
                if row is not None and path in lacking:
                    cap = _lock_stay_cap(row["acquired_at"])
                    path_expires = _lock_expiry(now, ttl_seconds)
                    if path_expires > cap:
                        path_expires = cap
                    plans[path] = (row["acquired_at"], path_expires, True)
                else:
                    plans[path] = (now, expires, False)
            if wanted:
                for path in wanted:
                    for stale in [
                        r["path"] for r in conn.execute("SELECT path FROM locks").fetchall()
                        if r["path"] != path and _same_file(r["path"], path)
                    ]:
                        conn.execute("DELETE FROM locks WHERE path = ?", (stale,))
                    dirt = dirty_text.get(path, "")
                    if not dirt and dirty_text:
                        dirt = next(
                            (val for key, val in dirty_text.items() if _same_file(key, path)), ""
                        )
                    acquired_at, path_expires, _stay_mark = plans[path]
                    prior = renewals.get(path)
                    stored_agent = prior["agent"] if prior is not None else agent
                    conn.execute(
                        """
                        INSERT INTO locks(path, agent, window_id, task_id, note, dirty_at_lock, acquired_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            agent = excluded.agent, window_id = excluded.window_id,
                            task_id = excluded.task_id, note = excluded.note,
                            dirty_at_lock = CASE
                                WHEN excluded.dirty_at_lock = '' THEN locks.dirty_at_lock
                                ELSE excluded.dirty_at_lock END,
                            acquired_at = excluded.acquired_at, expires_at = excluded.expires_at
                        """,
                        (path, stored_agent, window_id, task, note_text, dirt, acquired_at, path_expires),
                    )
                for path in wanted:
                    _acquired_at, path_expires, stay_mark = plans[path]
                    detail = {
                        "path": path, "ttl": ttl_seconds, "force": bool(force),
                        "window_id": window_id or "", "expires_at": path_expires,
                    }
                    if stay_mark:
                        detail["no_progress_stay"] = True
                    _emit_task_event(
                        conn, task or "", agent, "lock", "", "",
                        detail,
                        now,
                    )
                rows = conn.execute(
                    "SELECT * FROM locks WHERE path IN (%s)" % ",".join("?" * len(wanted)), wanted
                ).fetchall()
                unwritable = [
                    (path, why)
                    for path in wanted
                    if (why := _chmod_writable(path))
                ]
                if unwritable:
                    conn.rollback()
                    live = [row["path"] for row in conn.execute("SELECT path FROM locks").fetchall()]
                    for path in wanted:
                        if any(_same_file(held_path, path) for held_path in live):
                            continue
                        _chmod_locked(path)
                    raise MailError(
                        "lock_not_writable",
                        "拿到钥匙了，但这些文件没能变成可写："
                        + "；".join(f"{path}（{why}）" for path, why in unwritable)
                        + "。先看权限位/ACL（`ls -le`），别硬写（`sudo`/`chmod` 都不许）；"
                        "盘上有写入后再 `ic lock` 一次通常就能成；还没写入就带 stay"
                        "（`ic lock --stay`），普通再拿会被 no_progress 拒绝。",
                        500,
                    )
            else:
                rows = []
            queued = [
                {
                    "path": path,
                    "count": self._queue_head(path)[0],
                    "first": self._queue_head(path)[1],
                }
                for path in wanted
            ]
            response_expires = max((item[1] for item in plans.values()), default=expires)
            res = {
                "agent": agent,
                "ttl": ttl_seconds,
                "queued": [row for row in queued if row["count"]],
                "expires_at": response_expires,
                "expires_at_beijing": beijing_time(response_expires),
                "locks": [_lock_view(r) for r in rows],
                "skipped": skipped,
                "dirty_at_lock": {r["path"]: r["dirty_at_lock"] for r in rows if r["dirty_at_lock"]},
            }
            hint = _relative_lock_hint(paths, client_cwd)
            if hint:
                res["path_hint"] = hint
        self._signal()
        return res

    def lock_release(self, agent: str, paths=None, force: bool = False) -> dict:
        """放锁：只放自己的；force 仅 coordinator 可用。放锁留痕。"""
        agent = normalize_name(agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _locks_sweep(conn, now)
            if force:
                if not _may_force(conn, agent, None if paths is None else _lock_paths(paths)):
                    raise MailError(
                        "not_coordinator",
                        f"{agent} 不能强制放锁：项目内的路径要那个项目的 coordinator，项目外要 manager（human 天然算）",
                        403,
                    )
                if paths is None:
                    rows = conn.execute("SELECT * FROM locks").fetchall()
                    conn.execute("DELETE FROM locks")
                else:
                    rows = _rows_covering(
                        conn.execute("SELECT * FROM locks").fetchall(), _lock_paths(paths)
                    )
                    for row in rows:
                        conn.execute("DELETE FROM locks WHERE path = ?", (row["path"],))
            else:
                names = _holder_names(agent)
                marks = ",".join("?" * len(names))
                if paths is None:
                    rows = conn.execute(
                        f"SELECT * FROM locks WHERE agent IN ({marks})", names
                    ).fetchall()
                    conn.execute(f"DELETE FROM locks WHERE agent IN ({marks})", names)
                else:
                    rows = _rows_covering(
                        conn.execute(
                            f"SELECT * FROM locks WHERE agent IN ({marks})", names
                        ).fetchall(),
                        _lock_paths(paths),
                    )
                    for row in rows:
                        conn.execute(
                            "DELETE FROM locks WHERE path = ? AND agent = ?",
                            (row["path"], row["agent"]),
                        )
            targets = [row["path"] for row in rows]
            released = len(rows)
            stuck = _restore_readonly(conn, rows)
            for row in rows:
                _emit_task_event(
                    conn, row["task_id"] or "", row["agent"], "unlock", "", "",
                    {"path": row["path"], "force": bool(force),
                     "held_since": row["acquired_at"]},
                    now,
                )
        pending_left = []
        for path in targets:
            queue = self.list_queue(path)
            if queue["count"]:
                pending_left.append({"path": path, "count": queue["count"]})
        self._signal()
        return {
            "agent": agent,
            "released": targets,
            "released_count": released,
            "force": bool(force),
            "stuck": stuck,
            "pending_left": pending_left,
        }

    # ── 排队意图（Queue） ──────────────────────────────────────────

    def _queue_head(self, path: str) -> tuple[int, str]:
        """这个文件被几个人记了笔，以及现在谁正拿着钥匙。"""
        queue = self.list_queue(path)
        with self._session(readonly=True) as conn:
            row = conn.execute("SELECT agent FROM locks WHERE path = ?", (path,)).fetchone()
        return queue["count"], (row["agent"] if row else "")

    def queue_change(
        self,
        agent: str,
        path: str,
        note: str | None = None,
        patch: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """提议一处改动（拿不到钥匙时用这个）。"""
        agent = normalize_name(agent)
        paths = _lock_paths([path])
        target = paths[0]
        text = _task_text(note, 2000)
        patch_text = _task_text(patch, 20000)
        if not text and not patch_text:
            raise MailError("empty_change", "提议要写清「要改什么」（--note 或 --patch-file）", 400)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _emit_task_event(
                conn, normalize_case_id(task_id) if task_id else "", agent, "propose", "", "",
                {"path": target, "note": text, "patch": patch_text}, now,
            )
            seq = conn.execute("SELECT MAX(seq) FROM task_events").fetchone()[0]
            holder = conn.execute("SELECT agent FROM locks WHERE path = ?", (target,)).fetchone()
        self._signal()
        return {
            "id": f"q{seq}",
            "seq": seq,
            "path": target,
            "agent": agent,
            "note": text,
            "patch": patch_text,
            "held_by": (holder["agent"] if holder else ""),
        }

    def list_queue(self, path: str | None = None, include_done: bool = False) -> dict:
        """看排队：默认只列还没合并的提议。"""
        target = _lock_paths([path])[0] if path else None
        with self._session(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM task_events ORDER BY seq").fetchall()
            holders = {row["path"]: row["agent"] for row in conn.execute("SELECT * FROM locks")}
        merged: set[int] = set()
        for row in rows:
            if row["action"] != "propose_merged":
                continue
            try:
                payload = json.loads(row["detail"] or "{}")
            except ValueError:
                continue
            merged.update(int(item) for item in payload.get("ids") or [])
        pending, done = [], []
        for row in rows:
            if row["action"] != "propose":
                continue
            try:
                payload = json.loads(row["detail"] or "{}")
            except ValueError:
                continue
            if target and payload.get("path") != target:
                continue
            item = {
                "id": f"q{row['seq']}",
                "seq": int(row["seq"]),
                "path": payload.get("path", ""),
                "agent": row["agent"],
                "note": payload.get("note", ""),
                "patch": payload.get("patch", ""),
                "at": row["at"],
                "at_beijing": beijing_time(row["at"]),
                "held_by": holders.get(payload.get("path", ""), ""),
                "merged": int(row["seq"]) in merged,
            }
            (done if item["merged"] else pending).append(item)
        return {
            "pending": pending,
            "merged": done if include_done else [],
            "count": len(pending),
        }

    def merge_queue(
        self,
        agent: str,
        path: str,
        ids=None,
        note: str | None = None,
    ) -> dict:
        """合并排队内容（可选加速，不是义务）。"""
        agent = normalize_name(agent)
        target = _lock_paths([path])[0]
        now = utcnow()
        with self._session(immediate=True) as conn:
            holder = conn.execute("SELECT agent FROM locks WHERE path = ?", (target,)).fetchone()
            is_coord = _may_force(conn, agent, [target])
            if holder is not None and not _same_agent(holder["agent"], agent) and not is_coord:
                raise MailError(
                    "not_yours",
                    f"{target} 的钥匙在 {holder['agent']} 手里：合并只能由**拿钥匙的人**做"
                    "（规矩：不许替别人收口；要收口请 coordinator 来）",
                    403,
                )
            if holder is None and not is_coord:
                raise MailError(
                    "lock_required",
                    "你没拿这个文件的钥匙：要么先 lock 再改并合并，要么让 coordinator 收口",
                    409,
                )
            current = self.list_queue(target)["pending"]
            wanted = [int(item["seq"]) for item in current]
            if ids:
                wanted = []
                for item in ids:
                    token = str(item).strip().lstrip("qQ")
                    if not token.isdigit():
                        raise MailError(
                            "invalid_id",
                            f"`--id` 只认 `q<编号>`（如 q271）；多条可以重复给或用逗号分隔：收到 {item!r}",
                            400,
                        )
                    wanted.append(int(token))
            if not wanted:
                raise MailError("empty_queue", "这个文件没有排队中的改动", 400)
            text = _task_text(note, 2000)
            _emit_task_event(
                conn, "", agent, "propose_merged", "", "",
                {"path": target, "ids": wanted, "note": text}, now,
            )
        self._signal()
        return {"path": target, "merged": wanted, "by": agent, "note": text, "remaining": self.list_queue(target)["count"]}

    def lock_list(self, agent: str | None = None) -> dict:
        normalize_name(agent) if agent else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            _locks_sweep(conn, now)
            rows = conn.execute("SELECT * FROM locks ORDER BY acquired_at").fetchall()
        views = [_lock_view(r) for r in rows]
        pending = self.list_queue()["pending"]
        waiting: dict[str, int] = {}
        for item in pending:
            waiting[item["path"]] = waiting.get(item["path"], 0) + 1
        parsed_now = _parse_utc(now)
        for view, row in zip(views, rows):
            view["queued"] = waiting.get(view["path"], 0)
            acquired = row["acquired_at"]
            parsed_acquired = _parse_utc(acquired)
            held_long = bool(
                parsed_now
                and parsed_acquired
                and (parsed_now - parsed_acquired).total_seconds() >= LOCK_TTL
            )
            view["no_progress"] = bool(held_long and not _lock_has_progress(row["path"], acquired))
        live = [row["path"] for row in rows]
        orphans: dict[str, dict] = {}
        for item in pending:
            path = item.get("path") or ""
            if not path or any(_same_file(path, held) for held in live):
                continue
            slot = orphans.get(path)
            seq = int(item["seq"])
            if slot is None or seq < slot["_seq"]:
                if slot is None:
                    orphans[path] = {
                        "path": path,
                        "count": 0,
                        "agent": item["agent"],
                        "at": item["at"],
                        "at_beijing": item.get("at_beijing") or beijing_time(item["at"]),
                        "_seq": seq,
                    }
                else:
                    slot["agent"] = item["agent"]
                    slot["at"] = item["at"]
                    slot["at_beijing"] = item.get("at_beijing") or beijing_time(item["at"])
                    slot["_seq"] = seq
            orphans[path]["count"] += 1
        orphan_queue = []
        for slot in orphans.values():
            slot.pop("_seq", None)
            orphan_queue.append(slot)
        orphan_queue.sort(key=lambda item: (item["at"], item["path"]))
        return {
            "count": len(views),
            "locks": views,
            "now": now,
            "queued_total": len(pending),
            "orphan_queue": orphan_queue,
        }

    def guard_changes(self, agent: str, paths, require_mine: bool = True) -> dict:
        """提交闸：工作区里每个被改的文件都必须被一个未过期锁覆盖。"""
        agent = normalize_name(agent)
        wanted = _lock_paths(paths) if paths else []
        now = utcnow()
        with self._session(immediate=True) as conn:
            _locks_sweep(conn, now)
            is_coord = _may_force(conn, agent, wanted)
            held_rows = conn.execute("SELECT * FROM locks").fetchall()
        unlocked: list[str] = []
        mine: list[dict] = []
        foreign: list[dict] = []
        inherited: list[dict] = []
        for path in wanted:
            covering = _rows_covering(held_rows, [path])
            row = covering[0] if covering else None
            if row is None:
                unlocked.append(path)
            elif (not require_mine) or _same_agent(row["agent"], agent) or is_coord:
                mine.append({"path": path, "holder": row["agent"]})
                if row["dirty_at_lock"]:
                    inherited.append(
                        {"path": path, "holder": row["agent"], "dirty": row["dirty_at_lock"]}
                    )
            else:
                foreign.append({"path": path, "holder": row["agent"]})
        return {
            "ok": not unlocked and not foreign,
            "agent": agent,
            "require_mine": bool(require_mine),
            "checked": len(wanted),
            "covered": mine,
            "unlocked": unlocked,
            "locked_by_others": foreign,
            "inherited": inherited,
        }
