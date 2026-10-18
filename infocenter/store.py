import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infocenter.common import (
    BEIJING,
    CASE_ACCEPTANCE_MAX,
    CLAIM_TTL,
    HUMAN,
    HUMAN_NAMES,
    LOCK_TTL,
    STALL_HOURS,
    SWEEP_INTERVAL,
    MailError,
    _SEGMENT_RE,
    _body,
    _collapse_alias_deliveries,
    _emit_task_event,
    _is_party,
    _is_wire_name,
    _limit,
    _must_human,
    _own_delivery_fallback_names,
    _parse_utc,
    _pid_value,
    _priority,
    _re_subject,
    _recipient_names,
    _reply_to,
    _resolve_own_delivery,
    _role,
    _same_agent,
    _subject,
    _task_event_hash,
    _upsert_agent,
    _upsert_project,
    _workspace,
    _workspace_scope,
    atomic_replace,
    beijing_hhmm,
    beijing_stamp,
    beijing_time,
    is_person,
    iso_ago,
    new_id,
    new_window_id,
    normalize_case_id,
    normalize_name,
    normalize_window,
    parse_recipients,
    pid_alive,
    preview,
    utcnow,
)
from infocenter.governance import (
    GovernanceMixin,
)
from infocenter.locks import (
    LocksMixin,
    _lock_stay_cap,
    _lock_ttl,
    _locks_sweep,
    _same_file,
)
from infocenter.cases import (
    CasesMixin,
    SEALED_STATUSES,
    _block_unread_replies,
)
from infocenter.messages import (
    MessagesMixin,
    _unread_counts,
)
from infocenter.windows import (
    WindowsMixin,
    _activity,
    _lazy_reconcile_all,
    _upsert_window,
    _window_summary,
)


class Store(MessagesMixin, GovernanceMixin, LocksMixin, CasesMixin, WindowsMixin):
    def __init__(self, path: Path, seed: bool = True, seeds=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seeds = [
            (str(entry[0]), str(entry[1] or ""), str(entry[2] or "") if len(entry) > 2 else "")
            for entry in seeds
        ]
        self._cond = threading.Condition()
        self._seq = 0
        self._init()
        if seed:
            self.seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _session(self, immediate: bool = False, readonly: bool = False):
        conn = self._connect()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            elif readonly:
                conn.execute("BEGIN DEFERRED")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._session() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    activity TEXT NOT NULL DEFAULT '',
                    activity_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    from_agent TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    workspace TEXT,
                    reply_to TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    to_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    read_at TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    PRIMARY KEY (message_id, to_agent)
                );
                CREATE TABLE IF NOT EXISTS projects (
                    workspace TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS windows (
                    window_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    workspace TEXT,
                    state TEXT NOT NULL DEFAULT 'active',
                    activity TEXT NOT NULL DEFAULT '',
                    activity_at TEXT NOT NULL DEFAULT '',
                    pid INTEGER,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    left_at TEXT
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    workspace TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    owner_agent TEXT,
                    owner_window TEXT,
                    waiting_for TEXT,
                    sealed INTEGER NOT NULL DEFAULT 0,
                    wbs_path TEXT NOT NULL DEFAULT '',
                    milestones TEXT NOT NULL DEFAULT '[]',
                    acceptance TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS case_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    agent TEXT NOT NULL,
                    window_id TEXT,
                    action TEXT NOT NULL,
                    note TEXT NOT NULL,
                    status TEXT NOT NULL,
                    auto INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_deliveries_inbox ON deliveries(to_agent, status);
                CREATE INDEX IF NOT EXISTS idx_windows_agent ON windows(agent, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_cases_workspace ON cases(workspace, updated_at);
                CREATE INDEX IF NOT EXISTS idx_cases_owner ON cases(owner_window, status);
                CREATE INDEX IF NOT EXISTS idx_case_nodes_case ON case_nodes(case_id, id ASC);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    case_id TEXT,
                    owner TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'todo',
                    blocked_reason TEXT NOT NULL DEFAULT '',
                    deps TEXT NOT NULL DEFAULT '',
                    acceptance TEXT NOT NULL DEFAULT '',
                    handoff TEXT NOT NULL DEFAULT '',
                    supersedes TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner, status);
                CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace, status);
                CREATE TABLE IF NOT EXISTS task_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL DEFAULT '',
                    hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, seq);
                CREATE TABLE IF NOT EXISTS locks (
                    path TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    window_id TEXT,
                    task_id TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    dirty_at_lock TEXT NOT NULL DEFAULT '',
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            agent_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
            if "role" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            if "activity" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN activity TEXT NOT NULL DEFAULT ''")
            if "activity_at" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN activity_at TEXT NOT NULL DEFAULT ''")

            lock_cols = {row[1] for row in conn.execute("PRAGMA table_info(locks)")}
            if "dirty_at_lock" not in lock_cols:
                # 拿钥匙那一刻，盘上是否**本来就有**没提交的改动（有 ⇒ 那不是这次写的）。
                # 只读现场用，不参与任何判定。
                conn.execute("ALTER TABLE locks ADD COLUMN dirty_at_lock TEXT NOT NULL DEFAULT ''")

            delivery_cols = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
            if "claimed_by" not in delivery_cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN claimed_by TEXT")
            if "claimed_at" not in delivery_cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN claimed_at TEXT")

            window_cols = {row[1] for row in conn.execute("PRAGMA table_info(windows)")}
            if "pid" not in window_cols:
                conn.execute("ALTER TABLE windows ADD COLUMN pid INTEGER")

            case_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
            if "wbs_path" not in case_cols:
                conn.execute("ALTER TABLE cases ADD COLUMN wbs_path TEXT NOT NULL DEFAULT ''")
            if "milestones" not in case_cols:
                conn.execute("ALTER TABLE cases ADD COLUMN milestones TEXT NOT NULL DEFAULT '[]'")
            if "acceptance" not in case_cols:
                # 「怎样算完」：一行验收，写清这件事做成什么算做成。空 = 没说清（看板标出来，但不拦）。
                conn.execute("ALTER TABLE cases ADD COLUMN acceptance TEXT NOT NULL DEFAULT ''")

            project_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            if "archived" not in project_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            if "coordinator" not in project_cols:
                # human 2026-10-16：「每个项目 coordinator 不固定，由用户指定」+「一次性填成现在的那个」。
                # 这里只加列；「填谁」从库里 `agents.role` 取（`_is_coordinator` 一直看它，口径一致）——
                # 不去读 config.json，免得 store 去懂配置的格式。
                conn.execute("ALTER TABLE projects ADD COLUMN coordinator TEXT NOT NULL DEFAULT ''")
                legacy = conn.execute(
                    "SELECT name FROM agents WHERE role IN ('manager', 'coordinator') "
                    "ORDER BY created_at, name LIMIT 1"
                ).fetchone()
                if legacy is not None:
                    conn.execute(
                        "UPDATE projects SET coordinator = ? WHERE coordinator = ''", (legacy["name"],)
                    )
            # 旧值 `coordinator` 一律记成 `manager`：同一个词不许有两个意思（那是我们一直在打的病）。
            conn.execute("UPDATE agents SET role = 'manager' WHERE role = 'coordinator'")


    def seed(self) -> None:
        now = utcnow()
        with self._session(immediate=True) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO agents(name, description, role, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                # role 也走归一化：不然**测试库与真实库口径不一样**（迁移把真实库改成 manager，
                # 这里裸插旧值 ⇒ 假绿。2026-10-16 的 `_human_voices` 阻断就是这么被遮住的）。
                [(name, description, _role(role), now, now) for name, description, role in self._seeds],
            )

    def touch(
        self,
        name: str,
        description: str | None = None,
        role: str | None = None,
        activity: str | None = None,
        window_id: str | None = None,
        workspace: str | None = None,
        pid: int | None = None,
    ) -> dict:
        name = normalize_name(name)
        if description is not None and not isinstance(description, str):
            raise MailError("invalid_description", "description 必须是字符串")
        role = _role(role) if role is not None else None
        activity = _activity(activity) if activity is not None else None
        window_id = normalize_window(window_id)
        workspace = _workspace(workspace) if workspace is not None else None
        pid = _pid_value(pid)
        now = utcnow()
        with self._session(immediate=True) as conn:
            exists = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO agents(name, description, role, activity, activity_at, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, description or "", role or "", activity or "", now if activity else "", now, now),
                )
            else:
                if description is not None:
                    conn.execute("UPDATE agents SET description = ? WHERE name = ?", (description, name))
                if role is not None:
                    conn.execute("UPDATE agents SET role = ? WHERE name = ?", (role, name))
                if activity is not None:
                    conn.execute(
                        "UPDATE agents SET activity = ?, activity_at = ? WHERE name = ?",
                        (activity, now, name),
                    )
                conn.execute("UPDATE agents SET last_seen_at = ? WHERE name = ?", (now, name))
            if window_id is not None:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=name,
                    workspace=workspace,
                    activity=activity,
                    pid=pid,
                    now=now,
                    revive=True,
                )
            if workspace:
                _upsert_project(conn, workspace, None, now)
            row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
            unread = _unread_counts(conn, window_id=window_id).get(name, 0)
            win_rows = conn.execute(
                "SELECT * FROM windows WHERE agent = ? ORDER BY last_seen_at DESC, window_id ASC",
                (name,),
            ).fetchall()
            agent_dict = _agent(row, unread, [_window_summary(w) for w in win_rows])
            if window_id is not None:
                agent_dict["window_id"] = window_id
        return agent_dict

    def list_agents(self, window_id: str | None = None) -> list[dict]:
        window_id = normalize_window(window_id)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _lazy_reconcile_all(conn, now)
            rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
            unread = _unread_counts(conn, window_id=window_id)
            win_rows = conn.execute(
                "SELECT * FROM windows ORDER BY last_seen_at DESC, window_id ASC"
            ).fetchall()
            by_agent: dict[str, list[dict]] = {}
            for w in win_rows:
                by_agent.setdefault(w["agent"], []).append(_window_summary(w))
        return [_agent(row, unread.get(row["name"], 0), by_agent.get(row["name"], [])) for row in rows]

    def remove_agent(self, agent: str, name: str) -> dict:
        """从名册移除：**自己退自己**可以；移除**别人**只有 manager（含旧名 human）能（README 一直这么写，现在是机器护栏）。"""
        name = normalize_name(name)
        if normalize_name(agent) != name:
            _must_human(agent)
        with self._session(immediate=True) as conn:
            removed = conn.execute("DELETE FROM agents WHERE name = ?", (name,)).rowcount > 0
        return {"name": name, "removed": removed}

    def _signal(self) -> None:
        with self._cond:
            self._seq += 1
            self._cond.notify_all()

    def add_project(
        self, workspace: str, name: str | None = None, archived: bool | None = None,
        coordinator: str | None = None, actor: str | None = None,
    ) -> dict:
        ws = _workspace(workspace)
        if not ws:
            raise MailError("invalid_workspace", "项目目录 workspace 不能为空")
        if name is not None and not isinstance(name, str):
            raise MailError("invalid_project_name", "项目名称必须是字符串")
        if archived is not None and not isinstance(archived, bool):
            raise MailError("invalid_archived", "archived 必须是布尔值", 400)
        proj_name = (name or "").strip() or Path(ws).name or ws
        if len(proj_name) > 200 or "\n" in proj_name or "\r" in proj_name:
            raise MailError("invalid_project_name", "项目名称不能换行，最长 200 字")
        if coordinator is not None and not isinstance(coordinator, str):
            raise MailError("invalid_coordinator", "coordinator 必须是身份名", 400)
        if isinstance(coordinator, str) and not coordinator.strip():
            wait_coord = ""
        else:
            wait_coord = normalize_name(coordinator) if isinstance(coordinator, str) else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            if wait_coord is not None:
                _must_human(actor or "")
                if wait_coord:
                    known = conn.execute("SELECT 1 FROM agents WHERE name = ?", (wait_coord,)).fetchone()
                    if known is None:
                        raise MailError(
                            "unknown_agent", f"名册里没有 {wait_coord}（先 `ic agents add {wait_coord}`）", 400
                        )
            prev_coord = ""
            if wait_coord is not None:
                row = conn.execute("SELECT coordinator FROM projects WHERE workspace = ?", (ws,)).fetchone()
                prev_coord = (row["coordinator"] if row else "") or ""
            _upsert_project(conn, ws, proj_name, now, archived=archived, coordinator=wait_coord)
            if wait_coord is not None:
                _emit_task_event(
                    conn, "", normalize_name(actor or HUMAN), "project_coordinator",
                    prev_coord, wait_coord, {"workspace": ws, "coordinator": wait_coord}, now,
                )
            projects = _collect_projects(conn)
        ws_bare = Path(ws).name == ws
        for item in projects:
            anchor = item["workspace"]
            if anchor == ws or (
                ws_bare and Path(anchor).is_absolute() and (Path(anchor).name or anchor) == ws
            ):
                return item
        return {
            "workspace": ws,
            "name": proj_name,
            "archived": bool(archived),
            "case_count": 0,
            "total_cases": 0,
            "active_cases": 0,
            "paused_cases": 0,
            "closed_cases": 0,
            "active_windows": 0,
            "counts": {
                "open": 0, "waiting": 0, "paused": 0,
                "aborted": 0, "done": 0, "total": 0,
            },
            "created_at": now,
            "updated_at": now,
        }

    def list_projects(self) -> list[dict]:
        now = utcnow()
        with self._session(immediate=True) as conn:
            self._lazy_reconcile_all(conn, now)
            return _collect_projects(conn)

    def recent_activity(self, limit: int = 50) -> list[dict]:
        n = _limit(limit)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT n.id, n.case_id, n.agent, n.window_id, n.action, n.note, n.auto, n.created_at,
                       c.title AS case_title, c.workspace AS workspace
                FROM case_nodes n
                LEFT JOIN cases c ON c.id = n.case_id
                ORDER BY n.id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "case_id": row["case_id"],
                "case_title": row["case_title"] or "",
                "agent": row["agent"],
                "window_id": row["window_id"] or "",
                "action": row["action"],
                "note": row["note"],
                "auto": bool(row["auto"]),
                "workspace": row["workspace"] or "",
                "created_at": row["created_at"],
                "created_at_beijing": beijing_time(row["created_at"]),
            }
            for row in rows
        ]


# ── 内部辅助函数 ──────────────────────────────────────────────────────────


def _collect_projects(conn: sqlite3.Connection) -> list[dict]:
    now = utcnow()
    raw_rows = {
        row["workspace"]: dict(row)
        for row in conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, workspace ASC").fetchall()
    }
    mentioned = set(raw_rows)
    for table in ("cases", "windows", "messages"):
        for r in conn.execute(
            f"SELECT DISTINCT workspace FROM {table} WHERE workspace IS NOT NULL AND workspace != ''"
        ):
            mentioned.add(r["workspace"])

    absolutes = sorted(w for w in mentioned if Path(w).is_absolute())

    def _canonical(ws: str) -> str:
        if Path(ws).is_absolute():
            return ws
        for abs_ws in absolutes:
            if (Path(abs_ws).name or abs_ws) == ws:
                return abs_ws
        return ws

    proj_rows: dict[str, dict] = {}
    for ws in mentioned:
        key = _canonical(ws)
        if key in proj_rows:
            continue
        meta = raw_rows.get(key) or raw_rows.get(ws)
        proj_rows[key] = meta or {
            "workspace": key,
            "name": Path(key).name or key,
            "created_at": now,
            "updated_at": now,
        }
        proj_rows[key].setdefault("workspace", key)

    cases = conn.execute("SELECT workspace, status, sealed FROM cases WHERE workspace IS NOT NULL").fetchall()
    wins = conn.execute("SELECT workspace, state FROM windows WHERE workspace IS NOT NULL").fetchall()

    out = []
    for ws, meta in proj_rows.items():
        ws_cases = [c for c in cases if _workspace_scope(c["workspace"], ws)]
        total = len(ws_cases)
        open_cnt = sum(1 for c in ws_cases if not c["sealed"] and c["status"] in {"pending", "open", "doing"})
        waiting_cnt = sum(1 for c in ws_cases if not c["sealed"] and c["status"] == "waiting")
        doing = open_cnt + waiting_cnt
        paused = sum(1 for c in ws_cases if not c["sealed"] and c["status"] == "paused")
        aborted_cnt = sum(1 for c in ws_cases if c["status"] == "aborted")
        done_cnt = sum(1 for c in ws_cases if c["status"] == "done")
        closed = sum(1 for c in ws_cases if c["sealed"] or c["status"] in SEALED_STATUSES)
        active_wins = sum(1 for w in wins if w["state"] != "left" and _workspace_scope(w["workspace"], ws))
        out.append({
            "workspace": ws,
            "name": meta["name"] or Path(ws).name or ws,
            "archived": bool(meta.get("archived")),
            "coordinator": str(meta.get("coordinator") or ""),
            "case_count": total,
            "total_cases": total,
            "active_cases": doing,
            "paused_cases": paused,
            "closed_cases": closed,
            "active_windows": active_wins,
            "counts": {
                "open": open_cnt,
                "waiting": waiting_cnt,
                "paused": paused,
                "aborted": aborted_cnt,
                "done": done_cnt,
                "total": total,
            },
            "created_at": meta["created_at"],
            "updated_at": meta["updated_at"],
        })
    out.sort(key=lambda item: (item["active_cases"], item["total_cases"], item["workspace"]), reverse=True)
    return out


def _agent(row: sqlite3.Row, unread: int = 0, windows: list[dict] | None = None) -> dict:
    active_wins = [w for w in (windows or []) if w.get("state") == "active"]
    active_with_act = [w for w in active_wins if w.get("activity")]
    if len(active_with_act) > 1:
        activity_text = "；".join(
            f"[{w['window_id']}] {w['activity']}" for w in sorted(active_with_act, key=lambda x: x["window_id"])
        )
    else:
        activity_text = row["activity"]
    out = {
        "name": row["name"],
        "description": row["description"],
        "role": row["role"],
        "unread": unread,
        "activity": activity_text,
        "activity_at": row["activity_at"],
        "activity_at_beijing": beijing_time(row["activity_at"]) if row["activity_at"] else "",
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "last_seen_beijing": beijing_time(row["last_seen_at"]),
        "active_window_count": len(active_wins),
    }
    if windows is not None:
        out["windows"] = windows
    return out



