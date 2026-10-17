"""信息中心 · 机器治理线（Governance Line）。

宪法第三条线（机器规则与安全兜底）：
1. 写锁与租约（Locks & Leases）：单一写者互斥，续期检测盘上写入（st_mtime >= acquired_at），--stay 封顶。
2. 提交闸门（Guard Gate）：Git hook 检查暂存区改动是否全部被未过期写锁覆盖。
3. 任务队列（Tasks）：严格状态机（todo/doing/blocked/done/stopped），终态不可逆，supersedes 单向成链，停任务强制交接。
4. 意图排队（Queue）：拿不到锁时不争抢、不发信，提议上链并写进自己的 TODO。
5. 留痕与双重外锚审计（Audit）：所有治理变动同一事务写入 task_events 只增哈希链，重放一致性校验，盘上锚与 Git HEAD 双锚见证。
6. 死流程回收（Sweep）：过期锁自动清扫、离场/崩溃会话释放写锁并留痕。
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infocenter.common import (
    BEIJING,
    CASE_ACCEPTANCE_MAX,
    CLAIM_TTL,
    HUMAN,
    HUMAN_NAMES,
    LOCK_TTL,
    LOCK_TTL_MAX,
    MailError,
    STALL_HOURS,
    SWEEP_INTERVAL,
    TASK_ACCEPTANCE_MAX,
    TASK_HANDOFF_MAX,
    TASK_TEXT_MAX,
    _holder_names,
    _is_wire_name,
    _limit,
    _parse_utc,
    _same_agent,
    _subject,
    _workspace,
    _workspace_scope,
    beijing_time,
    is_person,
    new_id,
    normalize_case_id,
    normalize_name,
    normalize_window,
    utcnow,
)


# ── 常量定义 ─────────────────────────────────────────────────────────────

TASK_STATUSES = ("todo", "doing", "blocked", "stopped", "done")
TASK_TERMINAL = ("stopped", "done")

# 状态机合法流转规则
# owner 含本人与 coordinator；coord 仅 coordinator。不允许暂停/回退/换规格。
_TASK_OWNER_MOVES = {("todo", "doing"), ("doing", "blocked"), ("blocked", "doing")}
_TASK_COORD_MOVES = {
    ("todo", "stopped"),   # 没开工就放弃（现实里常见）
    ("doing", "done"),
    ("blocked", "done"),
    ("doing", "stopped"),
    ("blocked", "stopped"),
}

PROBE_MACHINERY_ACTIONS = {
    "lock", "unlock", "lock_expire", "claim", "claim_release", "offer", "offer_expired",
}

TASK_EVENT_FIELDS = (
    "workspace", "case_id", "owner", "title", "status",
    "blocked_reason", "deps", "acceptance", "handoff", "supersedes", "priority",
)

_CASE_FOLD_CACHE: dict[str, bool] = {}


# ── 辅助函数：Task 校验与展示 ──────────────────────────────────────────

def _task_status(value) -> str:
    text = str(value or "").strip().lower()
    if text not in TASK_STATUSES:
        raise MailError(
            "invalid_task_status", f"任务状态只能是 {'/'.join(TASK_STATUSES)}", 400
        )
    return text


def _task_text(value, limit: int = TASK_TEXT_MAX) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise MailError(
            "task_text_too_long", f"内容超过 {limit} 字；请写进文件，这里只留引用", 400
        )
    return text


def _task_priority(value) -> int:
    """优先级：整数（信只能 bump 它；越大越靠前）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_priority", "priority 必须是整数", 400) from None
    return max(-99, min(n, 99))


def _check_handoff(value) -> str:
    """停任务的交接段：一列自由文本，命令层只要求非空。"""
    text = _task_text(value, TASK_HANDOFF_MAX)
    if not text:
        raise MailError(
            "handoff_required",
            "停任务必须写交接段（做到哪 / 产物在哪 / 别踩什么坑）；不写不许停",
            400,
        )
    return text


def _find_task(conn: sqlite3.Connection, task_id) -> sqlite3.Row:
    tid = normalize_case_id(task_id) if task_id else ""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise MailError("not_found", f"没有找到任务：{task_id}", 404)
    return row


def _task_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "workspace": row["workspace"] or "",
        "case_id": row["case_id"] or "",
        "owner": row["owner"],
        "title": row["title"],
        "status": row["status"],
        "blocked_reason": row["blocked_reason"] or "",
        "deps": row["deps"] or "",
        "acceptance": row["acceptance"] or "",
        "handoff": row["handoff"] or "",
        "supersedes": row["supersedes"] or "",
        "priority": int(row["priority"] or 0),
        "created_at": row["created_at"],
        "created_at_beijing": beijing_time(row["created_at"]),
        "updated_at": row["updated_at"],
        "updated_at_beijing": beijing_time(row["updated_at"]),
        "updated_by": row["updated_by"] or "",
    }


def _replay_task_state(conn: sqlite3.Connection, task_id: str) -> dict:
    """把某个任务的痕迹从头重放，重建它「应该是什么样」。"""
    state: dict = {}
    for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY seq", (task_id,)
    ):
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            detail = {}
        for key, value in detail.items():
            if key in TASK_EVENT_FIELDS:
                state[key] = value
        if row["to_status"]:
            state["status"] = row["to_status"]
    return state


# ── 辅助函数：权限与角色校验 ──────────────────────────────────────────

def _must_human(agent: str) -> None:
    """只有人能指定项目的 coordinator、移除别人。现名 manager，旧名 human 同样算。"""
    if not is_person(agent):
        raise MailError(
            "human_only",
            f"{agent} 不是 manager；这件事只有 manager 能做（旧名 human 仍认）",
            403,
        )


def _is_manager(conn: sqlite3.Connection, agent: str) -> bool:
    """全局管理者（旧称 coordinator）：名册里 role=manager 的；人（manager / human）天然算。"""
    agent = normalize_name(agent)
    if is_person(agent):
        return True
    row = conn.execute("SELECT role FROM agents WHERE name = ?", (agent,)).fetchone()
    return bool(row and str(row["role"] or "") in {"manager", "coordinator"})


def _project_for(conn: sqlite3.Connection, workspace: str | None) -> sqlite3.Row | None:
    """按 workspace 找项目行。"""
    if not (workspace or "").strip():
        return None
    for row in conn.execute("SELECT * FROM projects").fetchall():
        if _workspace_scope(workspace, row["workspace"]):
            return row
    return None


def _project_for_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """按文件路径找它属于哪个项目（最长匹配）。"""
    if not (path or "").strip():
        return None
    real = os.path.realpath(path)
    best: sqlite3.Row | None = None
    best_len = -1
    for row in conn.execute("SELECT * FROM projects").fetchall():
        ws = (row["workspace"] or "").strip()
        if not ws:
            continue
        root = os.path.realpath(ws)
        if real == root or real.startswith(root.rstrip("/") + "/"):
            if len(root) > best_len:
                best, best_len = row, len(root)
    return best


def _is_coordinator(conn: sqlite3.Connection, agent: str, workspace: str | None) -> bool:
    """这个项目的协调者。"""
    agent = normalize_name(agent)
    if is_person(agent):
        return True
    row = _project_for(conn, workspace)
    if row is None:
        return _is_manager(conn, agent)
    return (row["coordinator"] or "").strip() == agent


def _may_force(conn: sqlite3.Connection, agent: str, paths) -> bool:
    """抢锁 / 替成员落地等不可逆操作权限判定。"""
    if paths is None:
        return _is_manager(conn, agent)
    for path in [p for p in (paths or []) if p]:
        row = _project_for_path(conn, path)
        if row is None:
            if not _is_manager(conn, agent):
                return False
        elif not _is_coordinator(conn, agent, row["workspace"]):
            return False
    return True


def _must_coordinator(conn: sqlite3.Connection, agent: str, workspace: str | None) -> None:
    if _is_coordinator(conn, agent, workspace):
        return
    row = _project_for(conn, workspace)
    if row is not None and not (row["coordinator"] or "").strip():
        raise MailError(
            "no_coordinator",
            f"项目 {row['workspace']} 还没指定 coordinator。请 human 指定：`ic --as human projects --project <路径> --coordinator <身份>`",
            409,
        )
    raise MailError(
        "not_coordinator",
        f"{agent} 不是这个项目的 coordinator"
        + (f"（{row['workspace']}：coordinator = {row['coordinator']}）" if row is not None else "（这卷没归属项目）")
        + "；建任务 / 改规格只有它或 human 能做",
        403,
    )


def _human_voices(conn: sqlite3.Connection) -> set[str]:
    """谁有资格代表人话。"""
    voices = set(HUMAN_NAMES)
    voices.update(
        row["name"]
        for row in conn.execute("SELECT name FROM agents WHERE role IN ('manager', 'coordinator')")
    )
    return voices


# ── 辅助函数：写锁与路径安全 ──────────────────────────────────────────

def _relative_lock_hint(paths, client_cwd: str | None = None) -> str:
    """相对路径提示。"""
    if isinstance(paths, str):
        items = [part for part in re.split(r"[\s,]+", paths) if part.strip()]
    elif isinstance(paths, (list, tuple, set)):
        items = [str(part).strip() for part in paths if str(part).strip()]
    else:
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
    if isinstance(paths, str):
        items = [part for part in re.split(r"[\s,]+", paths) if part.strip()]
    elif isinstance(paths, (list, tuple, set)):
        items = [str(part).strip() for part in paths if str(part).strip()]
    else:
        items = []
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


# ── 辅助函数：留痕与哈希链 ──────────────────────────────────────────

def _task_event_hash(prev_hash: str, fields: dict) -> str:
    """一条痕迹的哈希 = sha256(上一条哈希 + 本条全部关键字段)。"""
    blob = "\x1f".join([prev_hash] + [str(fields.get(key, "")) for key in
                                       ("task_id", "at", "agent", "action", "from_status", "to_status", "detail")])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _emit_case_event(
    conn, agent, action, from_status, to_status, case_id, window_id, note, now: str,
) -> None:
    """案卷状态机进既有哈希链。"""
    name = action if str(action).startswith("case_") else f"case_{action}"
    _emit_task_event(
        conn, "", agent, name, from_status or "", to_status or "",
        {"case_id": case_id, "window_id": window_id or "", "note": note or ""},
        now,
    )


def _emit_task_event(conn, task_id, agent, action, from_status, to_status, detail: dict, now: str) -> None:
    """写一行痕迹（必须与业务改动同一个事务）。"""
    payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
    row = conn.execute("SELECT hash FROM task_events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else ""
    fields = {
        "task_id": task_id, "at": now, "agent": agent, "action": action,
        "from_status": from_status or "", "to_status": to_status or "", "detail": payload,
    }
    conn.execute(
        """
        INSERT INTO task_events(task_id, at, agent, action, from_status, to_status, detail, prev_hash, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, now, agent, action, from_status or "", to_status or "",
            payload, prev_hash, _task_event_hash(prev_hash, fields),
        ),
    )


# ── GovernanceMixin 混入类 ──────────────────────────────────────────────

class GovernanceMixin:
    """治理线核心方法混入类，赋予 Store 类任务、写锁、提交闸、排队和审计能力。"""

    # ── 任务队列（Tasks） ────────────────────────────────────────────

    def task_add(
        self,
        agent: str,
        title: str,
        owner: str | None = None,
        workspace: str | None = None,
        deps: str | None = None,
        acceptance: str | None = None,
        case_id: str | None = None,
        supersedes: str | None = None,
        priority: int = 0,
    ) -> dict:
        """建任务：只有 coordinator，且必须带 workspace。"""
        from infocenter.store import _find_case, _upsert

        agent = normalize_name(agent)
        title_text = _subject(title if title is not None else "")
        workspace = _workspace(workspace)
        if not workspace:
            raise MailError("workspace_required", "任务必须带 workspace（否则 --mine 会串项目）", 400)
        owner_name = normalize_name(owner or agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _must_coordinator(conn, agent, workspace)
            _upsert(conn, owner_name, None)
            sup = normalize_case_id(supersedes) if supersedes else None
            if sup:
                old = _find_task(conn, sup)
                if old["status"] not in TASK_TERMINAL:
                    raise MailError(
                        "task_not_terminal",
                        f"只能取代已终结（done/stopped）的任务；{sup} 仍是 {old['status']}",
                        409,
                    )
                branch = conn.execute("SELECT id FROM tasks WHERE supersedes = ?", (sup,)).fetchone()
                if branch is not None:
                    raise MailError(
                        "supersedes_branch",
                        f"{sup} 已被 {branch['id']} 取代（supersedes 成链不成树）",
                        409,
                    )
                old_ws = _workspace(old["workspace"])
                if old_ws != workspace:
                    raise MailError(
                        "supersedes_workspace",
                        f"只能在同一 workspace 里接续：{sup} 属于 {old_ws}，这次是 {workspace}",
                        409,
                    )
            cid = normalize_case_id(case_id) if case_id else None
            if cid is not None and _find_case(conn, cid) is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            tid = new_id()
            conn.execute(
                """
                INSERT INTO tasks(
                    id, workspace, case_id, owner, title, status, blocked_reason,
                    deps, acceptance, handoff, supersedes, priority,
                    created_at, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?, 'todo', '', ?, ?, '', ?, ?, ?, ?, ?)
                """,
                (
                    tid, workspace, cid, owner_name, title_text,
                    _task_text(deps), _task_text(acceptance, TASK_ACCEPTANCE_MAX),
                    sup, _task_priority(priority), now, now, agent,
                ),
            )
            _emit_task_event(
                conn, tid, agent, "create", "", "todo",
                {
                    "workspace": workspace, "case_id": cid or "", "owner": owner_name,
                    "title": title_text, "deps": _task_text(deps),
                    "acceptance": _task_text(acceptance, TASK_ACCEPTANCE_MAX),
                    "supersedes": sup or "", "priority": _task_priority(priority),
                },
                now,
            )
            res = _task_view(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        self._signal()
        return res

    def task_list(
        self,
        agent: str,
        owner: str | None = None,
        workspace: str | None = None,
        status: str | None = None,
        include_superseded: bool = False,
        limit: int = 50,
    ) -> dict:
        """列表：默认只列活线；被取代的折叠进 superseded。"""
        normalize_name(agent)
        workspace = _workspace(workspace) if workspace else None
        owner_name = normalize_name(owner) if owner else None
        want_status = _task_status(status) if status else None
        limit = _limit(limit)
        with self._session(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY priority DESC, rowid DESC").fetchall()
        if workspace:
            rows = [r for r in rows if _workspace_scope(r["workspace"], workspace)]
        if owner_name:
            rows = [r for r in rows if r["owner"] == owner_name]
        if want_status:
            rows = [r for r in rows if r["status"] == want_status]
        superseded_ids = {r["supersedes"] for r in rows if r["supersedes"]}
        live = rows if include_superseded else [r for r in rows if r["id"] not in superseded_ids]
        folded = [] if include_superseded else [r for r in rows if r["id"] in superseded_ids]
        return {
            "count": len(live[:limit]),
            "total": len(live),
            "tasks": [_task_view(r) for r in live[:limit]],
            "superseded": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "owner": r["owner"],
                    "superseded_by": next(
                        (x["id"] for x in rows if x["supersedes"] == r["id"]), ""
                    ),
                    "handoff": (r["handoff"] or "")[:160],
                }
                for r in folded
            ],
        }

    def task_get(self, agent: str, task_id: str) -> dict:
        """详情：含完整 supersedes 链。"""
        normalize_name(agent)
        with self._session(readonly=True) as conn:
            row = _find_task(conn, task_id)
            view = _task_view(row)
            chain: list[dict] = []
            seen = {row["id"]}
            cursor = row["supersedes"]
            while cursor and cursor not in seen:
                seen.add(cursor)
                prev = _find_task(conn, cursor)
                chain.append(_task_view(prev))
                cursor = prev["supersedes"]
            nxt = conn.execute("SELECT id FROM tasks WHERE supersedes = ?", (row["id"],)).fetchone()
        view["supersedes_chain"] = chain
        view["superseded_by"] = nxt["id"] if nxt else ""
        view["attempt"] = len(chain) + 1
        return view

    def task_start(self, agent: str, task_id: str) -> dict:
        """开工：todo → doing。"""
        agent = normalize_name(agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            if row["status"] != "todo":
                raise MailError("invalid_transition", f"只有 todo 能开工（当前 {row['status']}）", 409)
            if not _same_agent(agent, row["owner"]) and not _is_coordinator(conn, agent, row["workspace"]):
                raise MailError("not_yours", "只有该任务的 owner 或 coordinator 能开工", 403)
            conn.execute(
                "UPDATE tasks SET status = 'doing', updated_at = ?, updated_by = ? WHERE id = ?",
                (now, agent, row["id"]),
            )
            _emit_task_event(
                conn, row["id"], agent, "start", row["status"], "doing",
                {"status": "doing", "spec_incomplete": [
                    k for k in ("acceptance", "deps") if not str(row[k] or "").strip()
                ]},
                now,
            )
            res = _task_view(conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone())
        self._signal()
        return res

    def task_set_status(
        self,
        agent: str,
        task_id: str,
        status: str,
        reason: str | None = None,
        handoff: str | None = None,
    ) -> dict:
        """改状态：owner 可 doing↔blocked；done/stopped 仅 coordinator；stopped 必带交接段。"""
        agent = normalize_name(agent)
        st = _task_status(status)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            cur = row["status"]
            if cur in TASK_TERMINAL:
                raise MailError(
                    "task_closed",
                    f"任务已终结（{cur}），不可再改；继续请**新建任务线**并 supersedes 它",
                    409,
                )
            if st == cur:
                raise MailError("invalid_transition", f"已经是 {st}", 409)
            is_coord = _is_coordinator(conn, agent, row["workspace"])
            mine = _same_agent(agent, row["owner"])
            if (cur, st) not in (_TASK_OWNER_MOVES | _TASK_COORD_MOVES):
                raise MailError(
                    "invalid_transition",
                    f"不允许 {cur} → {st}（合法转移只有：todo→doing、doing↔blocked、"
                    f"doing/blocked→done、todo/doing/blocked→stopped）",
                    409,
                )
            if st in ("done", "stopped"):
                if not is_coord:
                    raise MailError(
                        "not_coordinator", f"只有本项目的 coordinator（或 human）能把任务置为 {st}", 403
                    )
            elif not (mine or is_coord):
                raise MailError("not_yours", "只有该任务的 owner 或 coordinator 能改 doing/blocked", 403)
            if st == "blocked" and not (reason or "").strip():
                raise MailError("reason_required", "blocked 必带一行原因（--reason）", 400)
            hand = _check_handoff(handoff) if st == "stopped" else (row["handoff"] or "")
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, blocked_reason = ?, handoff = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                (
                    st,
                    (reason or "").strip() if st == "blocked" else "",
                    hand,
                    now,
                    agent,
                    row["id"],
                ),
            )
            after = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            _emit_task_event(
                conn, row["id"], agent, "status", cur, after["status"],
                {
                    "status": after["status"],
                    "blocked_reason": after["blocked_reason"] or "",
                    "handoff": after["handoff"] or "",
                },
                now,
            )
            res = _task_view(after)
        self._signal()
        return res

    def task_update(self, agent: str, task_id: str, **fields) -> dict:
        """改规格：仅 coordinator，且只在 todo。"""
        from infocenter.store import _find_case, _upsert

        agent = normalize_name(agent)
        allowed = {"title", "owner", "deps", "acceptance", "case_id", "priority"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not patch:
            raise MailError("nothing_to_update", "没有要改的字段", 400)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            _must_coordinator(conn, agent, row["workspace"])
            if row["status"] != "todo":
                raise MailError(
                    "spec_frozen",
                    f"任务已开工（{row['status']}）⇒ 规格已冻结；要改就 stopped 后**新建任务线**",
                    409,
                )
            sets, params = [], []
            for key, value in patch.items():
                if key == "title":
                    value = _subject(value)
                elif key == "owner":
                    value = normalize_name(value)
                    _upsert(conn, value, None)
                elif key == "acceptance":
                    value = _task_text(value, TASK_ACCEPTANCE_MAX)
                elif key == "deps":
                    value = _task_text(value)
                elif key == "case_id":
                    value = normalize_case_id(value)
                    if _find_case(conn, value) is None:
                        raise MailError("not_found", f"没有找到案卷：{value}", 404)
                elif key == "priority":
                    value = _task_priority(value)
                sets.append(f"{key} = ?")
                params.append(value)
            sets += ["updated_at = ?", "updated_by = ?"]
            params += [now, agent, row["id"]]
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
            after = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            _emit_task_event(
                conn, row["id"], agent, "update", after["status"], after["status"],
                {key: ("" if after[key] is None else after[key]) for key in patch},
                now,
            )
            res = _task_view(after)
        self._signal()
        return res

    def task_bump(self, agent: str, task_id: str, by: int = 1) -> dict:
        """调优先级。仅 coordinator。"""
        agent = normalize_name(agent)
        try:
            delta = int(by)
        except (TypeError, ValueError):
            raise MailError("invalid_by", "by 必须是整数") from None
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            _must_coordinator(conn, agent, row["workspace"])
            if row["status"] in TASK_TERMINAL:
                raise MailError(
                    "task_closed",
                    f"任务已终结（{row['status']}），别再调优先级了；要继续请**新建任务线**",
                    409,
                )
            new_priority = _task_priority(int(row["priority"]) + delta)
            conn.execute(
                "UPDATE tasks SET priority = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (new_priority, now, agent, row["id"]),
            )
            after = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            _emit_task_event(
                conn, row["id"], agent, "bump", after["status"], after["status"],
                {"priority": after["priority"], "by": delta},
                now,
            )
            res = _task_view(after)
        self._signal()
        return res

    # ── 写锁与钥匙（Locks） ──────────────────────────────────────────

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
            if force:
                if not _may_force(conn, agent, wanted):
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

    # ── 提交闸与探针（Guard & Probe） ───────────────────────────────

    def probe_counts(self, since: str) -> dict:
        """窗口内每家的产出与机械动作计数。"""
        with self._session(readonly=True) as conn:
            rows: dict[str, dict] = {}

            def row_for(name) -> dict:
                key = str(name or "")
                return rows.setdefault(
                    key,
                    {
                        "agent": key,
                        "traces": 0,
                        "machinery": 0,
                        "messages": 0,
                        "case_nodes": 0,
                        "activity": "",
                        "activity_at": "",
                    },
                )

            for row in conn.execute(
                "SELECT agent, action, COUNT(*) AS c FROM task_events WHERE at >= ?"
                " GROUP BY agent, action",
                (since,),
            ):
                item = row_for(row["agent"])
                if row["action"] in PROBE_MACHINERY_ACTIONS:
                    item["machinery"] += int(row["c"] or 0)
                else:
                    item["traces"] += int(row["c"] or 0)
            for row in conn.execute(
                "SELECT from_agent AS name, COUNT(*) AS c FROM messages WHERE created_at >= ?"
                " GROUP BY from_agent",
                (since,),
            ):
                row_for(row["name"])["messages"] += int(row["c"] or 0)
            for row in conn.execute(
                "SELECT agent, COUNT(*) AS c FROM case_nodes WHERE created_at >= ? GROUP BY agent",
                (since,),
            ):
                row_for(row["agent"])["case_nodes"] += int(row["c"] or 0)
            for row in conn.execute("SELECT name, activity, activity_at FROM agents"):
                item = row_for(row["name"])
                item["activity"] = row["activity"] or ""
                item["activity_at"] = row["activity_at"] or ""
        for item in rows.values():
            item["produced"] = item["traces"] + item["messages"] + item["case_nodes"]
        return rows

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

    # ── 回收与审计（Sweep & Audit） ─────────────────────────────────

    def sweep(self, now: str | None = None) -> dict:
        """死流程回收：过期锁、死进程窗口、僵尸认领。全部留痕。"""
        from infocenter.store import _mark_dead_windows, _release_stale_claims

        now = now or utcnow()
        with self._session(immediate=True) as conn:
            expired = _locks_sweep(conn, now)
            dead = _mark_dead_windows(conn, now)
            claims = _release_stale_claims(conn, now)
        if expired or dead or claims:
            self._signal()
        return {
            "expired_locks": expired,
            "dead_windows": dead,
            "released_claims": claims,
        }

    def audit_events(self, task_id: str | None = None, anchor: dict | None = None) -> dict:
        """机器验证留痕：① 哈希链完整；② 每个任务重放痕迹 == 当前行；③ 外锚核验。"""
        broken: list[dict] = []
        mismatches: list[dict] = []
        with self._session(readonly=True) as conn:
            prev = ""
            count = 0
            tip_seq = 0
            tip_hash = ""
            for row in conn.execute("SELECT * FROM task_events ORDER BY seq"):
                count += 1
                tip_seq, tip_hash = int(row["seq"]), row["hash"]
                if row["prev_hash"] != prev:
                    broken.append({"seq": row["seq"], "task_id": row["task_id"], "why": "链断（前一条对不上）"})
                expect = _task_event_hash(
                    row["prev_hash"],
                    {
                        "task_id": row["task_id"], "at": row["at"], "agent": row["agent"],
                        "action": row["action"], "from_status": row["from_status"],
                        "to_status": row["to_status"], "detail": row["detail"],
                    },
                )
                if row["hash"] != expect:
                    broken.append({"seq": row["seq"], "task_id": row["task_id"], "why": "内容被改（哈希对不上）"})
                prev = row["hash"]

            tasks = conn.execute(
                "SELECT * FROM tasks WHERE id = ?" if task_id else "SELECT * FROM tasks",
                (task_id,) if task_id else (),
            ).fetchall()
            for row in tasks:
                state = _replay_task_state(conn, row["id"])
                if not state:
                    mismatches.append({"task_id": row["id"], "why": "一条痕迹都没有"})
                    continue
                for key in TASK_EVENT_FIELDS:
                    want = state.get(key)
                    got = row[key]
                    if key == "priority":
                        want = int(want) if want not in (None, "") else 0
                        got = int(got or 0)
                    else:
                        want = "" if want is None else str(want)
                        got = "" if got is None else str(got)
                    if want != got:
                        mismatches.append(
                            {"task_id": row["id"], "field": key, "replay": want, "row": got}
                        )
            anchor_result: dict | None = None
            if anchor:
                want_seq = int(anchor.get("seq") or 0)
                want_hash = str(anchor.get("hash") or "")
                row = conn.execute("SELECT * FROM task_events WHERE seq = ?", (want_seq,)).fetchone()
                if row is None:
                    why = f"锚点那一行（seq {want_seq}）在库里找不到了：链被重做或被截短"
                elif row["hash"] != want_hash:
                    why = f"锚点那一行（seq {want_seq}）哈希对不上：链被重做或被改过"
                else:
                    why = ""
                after = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM task_events WHERE seq > ?", (want_seq,)
                    ).fetchone()[0]
                )
                anchor_result = {
                    "ok": not why,
                    "seq": want_seq,
                    "hash": want_hash,
                    "why": why,
                    "at": str(anchor.get("at") or ""),
                    "unwitnessed": after,
                }
        ok = not broken and not mismatches and (anchor_result is None or anchor_result["ok"])
        return {
            "ok": ok,
            "events": count,
            "tasks": len(tasks),
            "tip_seq": tip_seq,
            "tip_hash": tip_hash,
            "chain_broken": broken,
            "mismatches": mismatches,
            "anchor": anchor_result,
        }
