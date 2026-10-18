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
    _lock_stay_cap,
    _lock_ttl,
    _locks_sweep,
    _same_file,
)
from infocenter.cases import (
    CasesMixin,
    SEALED_STATUSES,
    _find_case,
    _insert_node,
)
from infocenter.windows import (
    WindowsMixin,
    _activity,
    _auto_claim_allowed,
    _claim_thread_on_touch,
    _effective_thread_claim,
    _is_thread_paused_or_sealed,
    _is_thread_sealed,
    _lazy_reconcile_all,
    _mark_own_delivery_read,
    _propagate_thread_claim,
    _release_stale_claims,
    _upsert_window,
    _window_summary,
    _window_workspace,
)


class Store(GovernanceMixin, CasesMixin, WindowsMixin):
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

    def events(
        self,
        agent: str,
        since=0,
        wait=0.0,
        limit: int = 50,
        workspace: str | None = None,
        window_id: str | None = None,
        pid: int | None = None,
    ) -> dict:
        """等新信。since 是上次返回的 deliveries rowid 游标；wait 秒内没有新信就返回现状。
        workspace 给定时只返回属于该项目范围的新信（多项目守护隔离用）。
        window_id 给定时过滤掉已被其他窗口认领或已封卷的信，并对首次触达的未认领新信做软认领。
        pid 给定时补齐窗口的进程号（M2 惰性判死）。"""
        agent = normalize_name(agent)
        window_id = normalize_window(window_id)
        limit = _limit(limit)
        try:
            since = max(0, int(since))
        except (TypeError, ValueError):
            raise MailError("invalid_since", "since 必须是整数游标") from None
        try:
            wait = float(wait)
        except (TypeError, ValueError):
            raise MailError("invalid_wait", "wait 必须是秒数") from None
        wait = max(0.0, min(wait, 120.0))
        deadline = time.monotonic() + wait
        first_pass = True
        unread_before: int | None = None
        with self._cond:
            while True:
                seen = self._seq
                snapshot = self._events_snapshot(
                    agent, since, limit, workspace, window_id, pid, touch_window=first_pass
                )
                first_pass = False
                if snapshot["new"] or wait <= 0:
                    return snapshot
                # 回执不新增 deliveries，new 仍为空。未读数相对这次等待变了就返回，
                # 当前这次长轮询才能交回新的 unread_total。无关的叫醒继续睡。
                if unread_before is not None and snapshot["unread_total"] != unread_before:
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
                unread_before = snapshot["unread_total"]
                self._cond.wait(remaining)
                if not self.path.exists():
                    return snapshot
                if deadline - time.monotonic() <= 0 and self._seq == seen:
                    return snapshot

    def _events_snapshot(
        self,
        agent: str,
        since: int,
        limit: int,
        workspace: str | None = None,
        window_id: str | None = None,
        pid: int | None = None,
        touch_window: bool = True,
    ) -> dict:
        """M1：只取最旧的 limit 条，cursor = 实际返回的最大 delivery_seq（无则 = since）。
        旧实现取全表 MAX(rowid) 却只回最新 limit 条，超出的旧信会永久跳过。
        人的两个名字共用 `_recipient_names`；同一封信两行都未读时折成一份。"""
        names = _recipient_names(agent)
        name_sql = ",".join("?" for _ in names)
        if window_id is None:
            with self._session(readonly=True) as conn:
                rows = conn.execute(
                    f"""
                    SELECT m.*, d.to_agent AS delivered_to, d.rowid AS delivery_seq,
                           d.status AS delivery_status,
                           d.read_at AS delivery_read_at, d.claimed_by AS delivery_claimed_by,
                           d.claimed_at AS delivery_claimed_at
                    FROM deliveries d
                    JOIN messages m ON m.id = d.message_id
                    WHERE d.to_agent IN ({name_sql}) AND d.status = 'unread'
                    ORDER BY d.rowid ASC
                    """,
                    names,
                ).fetchall()
                rows = _collapse_alias_deliveries(rows, agent)
                if workspace is not None:
                    rows = [row for row in rows if _workspace_scope(row["workspace"], workspace)]
                fresh = [row for row in rows if row["delivery_seq"] > since][:limit]
                cursor = fresh[-1]["delivery_seq"] if fresh else since
                messages = [
                    _view(
                        conn,
                        row,
                        agent,
                        False,
                        row["delivery_status"],
                        row["delivery_read_at"],
                        row["delivery_claimed_by"],
                        row["delivery_claimed_at"],
                    )
                    for row in fresh
                ]
                # N3：无窗口路径的 unread_total 与 _unread_counts(None) / 有窗口路径同口径——
                # 排除线程已暂停/封卷、以及已被某窗口认领的信，不再用裸行数虚高。
                unread_total = 0
                for row in rows:
                    if _is_thread_paused_or_sealed(conn, row["thread_id"]):
                        continue
                    eff_claim = _effective_thread_claim(
                        conn, row["thread_id"], agent, row["delivery_claimed_by"]
                    )
                    if eff_claim is None:
                        unread_total += 1
            return {"agent": agent, "cursor": cursor, "unread_total": unread_total, "new": messages}

        now = utcnow()
        with self._session(immediate=True) as conn:
            if touch_window:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=agent,
                    workspace=_workspace(workspace) if workspace else None,
                    activity=None,
                    pid=pid,
                    now=now,
                )
            else:
                win_row = conn.execute(
                    "SELECT state FROM windows WHERE window_id = ?", (window_id,)
                ).fetchone()
                if win_row is not None and win_row["state"] in {"left", "offline", "paused"}:
                    return {
                        "agent": agent,
                        "window_id": window_id,
                        "cursor": since,
                        "unread_total": 0,
                        "new": [],
                    }
            _lazy_reconcile_all(conn, now)
            rows = conn.execute(
                f"""
                SELECT m.*, d.to_agent AS delivered_to, d.rowid AS delivery_seq,
                       d.status AS delivery_status,
                       d.read_at AS delivery_read_at, d.claimed_by AS delivery_claimed_by,
                       d.claimed_at AS delivery_claimed_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent IN ({name_sql}) AND d.status = 'unread'
                ORDER BY d.rowid ASC
                """,
                names,
            ).fetchall()
            rows = _collapse_alias_deliveries(rows, agent)
            if workspace is not None:
                rows = [row for row in rows if _workspace_scope(row["workspace"], workspace)]

            visible_rows = []
            for row in rows:
                if _is_thread_paused_or_sealed(conn, row["thread_id"]):
                    continue
                eff_claim = _effective_thread_claim(conn, row["thread_id"], agent, row["delivery_claimed_by"])
                if eff_claim is not None and eff_claim != window_id:
                    continue
                visible_rows.append(row)

            candidates = [row for row in visible_rows if row["delivery_seq"] > since]
            messages = []
            last_seq = since
            for row in candidates:
                if len(messages) >= limit:
                    break
                claimed_by = row["delivery_claimed_by"]
                claimed_at = row["delivery_claimed_at"]
                if claimed_by is None:
                    # M6-0 / M6-10：只认领属于本窗 workspace 的信。他项目窗口照常看见但不认领；
                    # 空 workspace 也不自动认领。认领留给该项目自己的窗口，或留给显式签收。
                    if _auto_claim_allowed(conn, window_id, row["workspace"]):
                        target = row["delivered_to"]
                        cur = conn.execute(
                            """
                            UPDATE deliveries
                            SET claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
                            WHERE message_id = ? AND to_agent = ?
                              AND (claimed_by IS NULL OR claimed_by = ?)
                            """,
                            (window_id, now, row["id"], target, window_id),
                        )
                        if cur.rowcount == 0:
                            continue
                        _propagate_thread_claim(conn, row["thread_id"], agent, window_id, now)
                        claimed_by = window_id
                        claimed_at = claimed_at or now
                last_seq = row["delivery_seq"]
                messages.append(
                    _view(
                        conn,
                        row,
                        agent,
                        False,
                        row["delivery_status"],
                        row["delivery_read_at"],
                        claimed_by,
                        claimed_at,
                    )
                )
            cursor = last_seq if messages else since
        return {
            "agent": agent,
            "window_id": window_id,
            "cursor": cursor,
            "unread_total": len(visible_rows),
            "new": messages,
        }

    def _signal(self) -> None:
        with self._cond:
            self._seq += 1
            self._cond.notify_all()

    def send(
        self,
        sender: str,
        to,
        subject: str,
        body: str,
        priority: str = "normal",
        workspace: str | None = None,
        reply_to: str | None = None,
        subject_override: str | None = None,
        local_node: str | None = None,
        window_id: str | None = None,
        pid: int | None = None,
        client_cwd: str | None = None,
    ) -> dict:
        sender = normalize_name(sender)
        window_id = normalize_window(window_id)
        subject = _subject(subject if subject_override is None else subject_override)
        body = _body(body)
        priority = _priority(priority)
        explicit_workspace = _workspace(workspace)
        client_cwd = _workspace(client_cwd)
        reply_to = _reply_to(reply_to)
        recipients_in = parse_recipients(to)
        node_text = str(local_node).strip().lower() if isinstance(local_node, str) and local_node.strip() else None
        replay_id = None
        with self._session(immediate=True) as conn:
            _upsert(conn, sender, None)
            now = utcnow()
            # 只把调用方显式给的 workspace 写到窗口上。推断出来的路径不改窗口，
            # 避免长寿命窗口被父信/案卷/cwd 悄悄改到另一个项目。
            if window_id is not None:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=sender,
                    workspace=explicit_workspace,
                    activity=None,
                    pid=pid,
                    now=now,
                )
            thread_id = None
            parent_id = None
            parent = None
            if reply_to:
                parent = _must_visible(conn, reply_to, sender)
                parent_id = parent["id"]
                thread_id = parent["thread_id"]
            workspace, workspace_source = _resolve_outgoing_workspace(
                conn,
                explicit=explicit_workspace,
                parent=parent,
                thread_id=thread_id,
                window_id=window_id,
                client_cwd=client_cwd,
            )
            # M6-3：同一发件人 + 同一 reply_to + 同一主题 + 正文逐字节相同 ⇒ 不新建。
            # 不看收件人、优先级、workspace。命中就静默返回最早那条，不报错、不加开关。
            replay_id = _exact_send_id(conn, sender, parent_id, subject, body)
            if replay_id is not None:
                message_id = replay_id
            else:
                # 只有显式传入才登记项目。父信/案卷/窗口/cwd 的推断不得 _upsert_project，
                # 否则 cwd 兜底会继续造出垃圾项目。
                if workspace_source == "explicit" and workspace:
                    _upsert_project(conn, workspace, None, now)
                if recipients_in == ["*"]:
                    rows = conn.execute(
                        "SELECT name FROM agents WHERE name != ? ORDER BY name",
                        (sender,),
                    ).fetchall()
                    recipients = [row["name"] for row in rows]
                    if node_text:
                        recipients = [name for name in recipients if not _is_wire_name(name)]
                    if not recipients:
                        raise MailError(
                            "delivers_to_nobody",
                            "除了你自己，还没有其他 agent，广播没有人可收",
                            422,
                        )
                else:
                    recipients = recipients_in
                    # 第 5 道兜底（human 2026-10-16 定；kimi 2026-10-15 提的「死信拒收」）：
                    # 发给**不存在**（打错名字 / 已退休）的身份，程序会当作成功——而那封信谁也收不到，
                    # 话说了没人听是不可逆的损失。所以在这里直接拒（422）。
                    # 在册 = 库里注册过 ∪ 名册（config.json）里列着（kickoff 按名册群发，
                    # 新同事可能一次都还没开口，只看库会误拒）。
                    # 带点的「跨节点名」（`robin.kimi`）不归本地名册管，交给同步层导出。
                    known = _known_names(conn, self.path.parent)
                    unknown = [
                        name for name in recipients
                        if not _is_wire_name(name) and name not in known
                    ]
                    if unknown:
                        raise MailError(
                            "no_such_recipient",
                            "没有这些身份：" + "、".join(unknown)
                            + "（先核对名字；新同事让他 `ic register --as <名字>` 报个到，"
                            + "或把人加进 config.json 的名册）",
                            422,
                        )
                    for name in recipients:
                        _upsert(conn, name, None)
                message_id = new_id()
                created = now
                final_thread_id = thread_id or message_id
                conn.execute(
                    """
                    INSERT INTO messages(
                        id, thread_id, from_agent, subject, body, priority,
                        workspace, reply_to, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        final_thread_id,
                        sender,
                        subject,
                        body,
                        priority,
                        workspace,
                        parent_id,
                        created,
                    ),
                )
                delivery_rows = []
                for name in recipients:
                    inherited_claim = None
                    inherited_at = None
                    if thread_id and not _is_thread_sealed(conn, final_thread_id):
                        inherited_claim = _effective_thread_claim(conn, final_thread_id, name, None)
                        if inherited_claim:
                            inherited_at = created
                    delivery_rows.append((message_id, name, inherited_claim, inherited_at))
                conn.executemany(
                    """
                    INSERT INTO deliveries(message_id, to_agent, status, read_at, claimed_by, claimed_at)
                    VALUES (?, ?, 'unread', NULL, ?, ?)
                    """,
                    delivery_rows,
                )
                if thread_id:
                    case_row = _find_case(conn, final_thread_id)
                    if case_row is not None and not case_row["sealed"] and case_row["status"] not in SEALED_STATUSES:
                        owner_name = case_row["owner_agent"] or ""
                        owner_addressed = any(_same_agent(owner_name, name) for name in recipients)
                        if case_row["status"] == "waiting" and owner_addressed and not _same_agent(sender, owner_name):
                            conn.execute(
                                "UPDATE cases SET status = 'open', waiting_for = NULL, updated_at = ? WHERE id = ?",
                                (created, case_row["id"]),
                            )
                            _insert_node(
                                conn,
                                case_id=case_row["id"],
                                agent=sender,
                                window_id=window_id,
                                action="reply",
                                note=f"收到 {sender} 回复 → 在办",
                                status="open",
                                auto=False,
                                now=created,
                            )
                        elif case_row["status"] == "open" and _same_agent(sender, owner_name):
                            wait_for = next((r for r in recipients if not _same_agent(r, sender)), None)
                            conn.execute(
                                "UPDATE cases SET status = 'waiting', waiting_for = ?, updated_at = ? WHERE id = ?",
                                (wait_for, created, case_row["id"]),
                            )
                            _insert_node(
                                conn,
                                case_id=case_row["id"],
                                agent=sender,
                                window_id=window_id or case_row["owner_window"],
                                action="reply",
                                note=f"回复 {wait_for or ''} → 待谁".strip(),
                                status="waiting",
                                auto=False,
                                now=created,
                            )
        # 新投递提交之后，趁这次写信把死窗口的认领放掉。
        # 不改 events 游标，也不把已经越过的未读塞回 new。
        # 正文全同的重放不新建：不对账，也不叫醒。
        if replay_id is None:
            with self._session(immediate=True) as conn:
                _release_stale_claims(conn)
            self._signal()
        result = self.get_message(sender, message_id, full=True)
        # 留空时给调用方一个可提示的信号。CLI 文案不在这里拼。
        result["workspace_source"] = workspace_source
        result["workspace_unscoped"] = workspace_source == "empty"
        return result

    def reply(
        self,
        sender: str,
        message_id: str,
        body: str,
        subject: str | None = None,
        to=None,
        local_node: str | None = None,
        window_id: str | None = None,
        pid: int | None = None,
        client_cwd: str | None = None,
    ) -> dict:
        sender = normalize_name(sender)
        window_id = normalize_window(window_id)
        with self._session(immediate=True) as conn:
            parent = _must_visible(conn, message_id, sender)
            parent_view = _view(conn, parent, sender, full=False)
            if window_id is not None:
                now = utcnow()
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=sender,
                    workspace=parent_view.get("workspace"),
                    activity=None,
                    pid=pid,
                    now=now,
                )
                _claim_thread_on_touch(
                    conn,
                    agent=sender,
                    window_id=window_id,
                    message_row=parent,
                    force=False,
                    mark_read=False,
                    now=now,
                )
        if to is None:
            if not _same_agent(parent_view["from"], sender):
                targets: list[str] | str = [parent_view["from"]]
            else:
                others = [name for name in parent_view["to"] if not _same_agent(name, sender)]
                targets = others or [sender]
        else:
            targets = to
        final_subject = subject if subject else _re_subject(parent_view["subject"])
        # workspace 不在这里传。send(reply_to=) 与 reply() 共用同一套解析：
        # 显式 > 父信 > 案卷 > 窗口已登记 workspace > 客户端 cwd 命中已登记 project > 留空。
        return self.send(
            sender,
            targets,
            final_subject,
            body,
            priority="normal",
            reply_to=parent_view["id"],
            local_node=local_node,
            window_id=window_id,
            pid=pid,
            client_cwd=client_cwd,
        )

    def import_remote(self, envelope: dict, local_bare_names: list[str]) -> dict:
        """B3 §10：从网盘 JSON 导入他机消息。按 id + 不可变字段幂等，不 upsert 发信方。

        只有新插入（status=imported）才在事务提交之后 _signal。
        duplicate / conflict / skipped 不叫。父信不在库里仍插入，返回 parent=missing；
        父信 thread 不一致、reply_to 指向自己、或已有子信 thread 不一致，则 conflict、不插入。

        U-3 规格降级（记录）：`federation-design.md` §7 说「两份信封之间仍比排序后的完整
        `to`」，但 `messages` 表没有 `to` 列，且按设计「他机收件人不产生投递、仅作记录」，
        远程裸名完全没有落库。因此这里能比较的只是「本机投递收件人」：
        `sorted(_recipients(conn, id)) == sorted(local_bare_names)`。若同 id 的两份信封
        只差他机收件人，本实现会判成 `duplicate` 而非 `conflict`（先到者胜）。要真正比较
        完整 `to`，需要给 `messages` 增列存 wire `to`；本轮按既有表结构降级，未增列。
        """
        if not isinstance(envelope, dict):
            raise MailError("invalid_envelope", "envelope 必须是字典")
        bare_names = sorted(
            {normalize_name(name) for name in (local_bare_names or []) if isinstance(name, str) and name.strip()}
        )
        if not bare_names:
            return {"status": "skipped"}
        msg_id = str(envelope.get("id") or "").strip()
        thread_id = str(envelope.get("thread_id") or "").strip()
        reply_to = _reply_to(envelope.get("reply_to"))
        sender = normalize_name(str(envelope.get("from") or ""))
        subject = _subject(envelope.get("subject"))
        body = _body(envelope.get("body"))
        priority = _priority(envelope.get("priority"))
        workspace = _workspace(envelope.get("workspace"))
        created_at = str(envelope.get("created_at") or "").strip()
        if not msg_id or not thread_id or not created_at:
            raise MailError("invalid_envelope", "缺少必填字段 id/thread_id/created_at")

        def _blank(value: str | None) -> str | None:
            # 只用于幂等比较。空串当 None，不把库里的空串改掉。
            if value is None or (isinstance(value, str) and value.strip() == ""):
                return None
            return value

        inserted_parent: str | None = None
        with self._session(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if existing is not None:
                same = (
                    existing["thread_id"] == thread_id
                    and existing["reply_to"] == reply_to
                    and existing["from_agent"] == sender
                    and existing["subject"] == subject
                    and existing["body"] == body
                    and existing["priority"] == priority
                    and existing["created_at"] == created_at
                    and _blank(existing["workspace"]) == _blank(workspace)
                    and sorted(_recipients(conn, msg_id)) == bare_names
                )
                return {"status": "duplicate" if same else "conflict"}

            if reply_to == msg_id:
                return {"status": "conflict"}
            if reply_to is None:
                parent_flag = "none"
            else:
                parent = conn.execute(
                    "SELECT thread_id FROM messages WHERE id = ?",
                    (reply_to,),
                ).fetchone()
                if parent is None:
                    parent_flag = "missing"
                elif parent["thread_id"] != thread_id:
                    return {"status": "conflict"}
                else:
                    parent_flag = "present"
            # 已有行把这封当父信时，thread 必须一致。不等则这封不入库，子信保持原样。
            mismatch = conn.execute(
                "SELECT 1 FROM messages WHERE reply_to = ? AND thread_id != ? LIMIT 1",
                (msg_id, thread_id),
            ).fetchone()
            if mismatch is not None:
                return {"status": "conflict"}

            conn.execute(
                """
                INSERT INTO messages(
                    id, thread_id, from_agent, subject, body, priority,
                    workspace, reply_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    thread_id,
                    sender,
                    subject,
                    body,
                    priority,
                    workspace,
                    reply_to,
                    created_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO deliveries(message_id, to_agent, status, read_at)
                VALUES (?, ?, 'unread', NULL)
                """,
                [(msg_id, name) for name in bare_names],
            )
            inserted_parent = parent_flag
        self._signal()
        return {"status": "imported", "parent": inserted_parent}

    def apply_receipt(self, message_id: str, by: str, read_at: str) -> dict:
        """B3 §10：应用他机已读回执。仅当 to_agent==by 且当前为 unread 时标成 read。

        applied 在事务提交之后 _signal。duplicate / absent / archived 不叫。
        """
        msg_id = str(message_id or "").strip()
        by_name = normalize_name(str(by or ""))
        read_time = str(read_at or "").strip()
        if not msg_id or not read_time:
            raise MailError("invalid_receipt", "回执缺少 message_id 或 read_at")
        applied = False
        with self._session(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (msg_id, by_name),
            ).fetchone()
            if row is None:
                return {"status": "absent"}
            if row["status"] == "unread":
                conn.execute(
                    "UPDATE deliveries SET status = 'read', read_at = ? WHERE message_id = ? AND to_agent = ?",
                    (read_time, msg_id, by_name),
                )
                applied = True
            elif row["status"] == "read":
                return {"status": "duplicate"}
            else:
                return {"status": "archived"}
        if applied:
            self._signal()
            return {"status": "applied"}
        return {"status": "archived"}

    def inbox(
        self,
        agent: str,
        status: str = "unread",
        limit: int = 20,
        full: bool = False,
        workspace: str | None = None,
        window_id: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent)
        window_id = normalize_window(window_id)
        if not isinstance(status, str) or status not in {"unread", "read", "archived", "all"}:
            raise MailError("invalid_status", "status 只能是 unread、read、archived 或 all")
        limit = _limit(limit)
        workspace = _workspace(workspace) if workspace is not None else None
        pid = _pid_value(pid)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _upsert(conn, agent, None)
            if window_id is not None:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=agent,
                    workspace=workspace,
                    activity=None,
                    pid=pid,
                    now=now,
                )
            if window_id is not None and status == "unread":
                _lazy_reconcile_all(conn, now)
            # D-1：将窗口/封卷/暂停过滤条件直接下推到 SQL（NOT EXISTS），并在 LIMIT 前执行过滤，
            # 避免被过滤行先吃掉 LIMIT 配额导致本窗口未读被截断。
            # 人的收件箱把两个名字的 delivery 都算进来（视图）。d.to_agent 仍是落库的那一个名字。
            names = _recipient_names(agent)
            name_sql = ",".join("?" for _ in names)
            rows = conn.execute(
                f"""
                SELECT m.*, d.to_agent AS delivered_to, d.status AS delivery_status,
                       d.read_at AS delivery_read_at,
                       d.claimed_by AS delivery_claimed_by, d.claimed_at AS delivery_claimed_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent IN ({name_sql})
                  AND (? = 'all' OR d.status = ?)
                  AND (
                    ? IS NULL
                    OR ? != 'unread'
                    OR (
                      NOT EXISTS (
                        SELECT 1 FROM cases c
                        WHERE (c.id = m.thread_id OR c.thread_id = m.thread_id)
                          AND (c.sealed = 1 OR c.status IN ('aborted', 'done', 'paused'))
                      )
                      AND (d.claimed_by IS NULL OR d.claimed_by = ?)
                      AND COALESCE(
                        d.claimed_by,
                        (
                          SELECT d2.claimed_by
                          FROM deliveries d2
                          JOIN messages m2 ON m2.id = d2.message_id
                          WHERE m2.thread_id = m.thread_id
                            AND d2.to_agent = d.to_agent
                            AND d2.claimed_by IS NOT NULL
                          ORDER BY d2.rowid DESC
                          LIMIT 1
                        ),
                        (
                          SELECT c2.owner_window
                          FROM cases c2
                          WHERE (c2.id = m.thread_id OR c2.thread_id = m.thread_id)
                            AND c2.sealed = 0
                            AND c2.owner_agent = d.to_agent
                            AND c2.owner_window IS NOT NULL
                          LIMIT 1
                        ),
                        ?
                      ) = ?
                    )
                  )
                ORDER BY m.created_at DESC, m.id DESC
                """,
                (
                    *names,
                    status,
                    status,
                    window_id,
                    status,
                    window_id,
                    window_id,
                    window_id,
                ),
            ).fetchall()
            # 与 events 同一套 _workspace_scope（裸名 / 全路径 / 解析后路径），
            # 不再用 m.workspace = ? 的等值，避免同一项目在收件箱和长轮询里是两个集合。
            # 过滤放在 LIMIT 之前，被滤掉的行不占配额，也不计入 unread_total。
            if workspace is not None:
                rows = [row for row in rows if _workspace_scope(row["workspace"], workspace)]
            # 双投同一封信时两行都在。折掉之后列表和 unread_total 都只算一份。
            rows = _collapse_alias_deliveries(rows, agent)
            messages = []
            for row in rows:
                holder = row["delivered_to"]
                if window_id is not None and status == "unread":
                    if _is_thread_paused_or_sealed(conn, row["thread_id"]):
                        continue
                    eff_claim = _effective_thread_claim(conn, row["thread_id"], holder, row["delivery_claimed_by"])
                    if eff_claim is not None and eff_claim != window_id:
                        continue
                item = _view(
                    conn,
                    row,
                    holder,
                    full,
                    row["delivery_status"],
                    row["delivery_read_at"],
                    row["delivery_claimed_by"],
                    row["delivery_claimed_at"],
                )
                item["delivered_to"] = holder
                messages.append(item)
                if len(messages) >= limit:
                    break
            # M1-data：unread_total 与 `_unread_counts` 同口径——
            # 只算「未被（其他）窗口认领」且线程未暂停/未封卷的信（不受 limit 截断）。
            unread_total = 0
            if status == "unread":
                for row in rows:
                    if _is_thread_paused_or_sealed(conn, row["thread_id"]):
                        continue
                    eff_claim = _effective_thread_claim(
                        conn, row["thread_id"], row["delivered_to"], row["delivery_claimed_by"]
                    )
                    if window_id is None:
                        if eff_claim is not None:
                            continue
                    elif eff_claim is not None and eff_claim != window_id:
                        continue
                    unread_total += 1
            else:
                unread_total = len(rows)
        return {
            "agent": agent,
            "status": status,
            "count": len(messages),
            "unread_total": unread_total,
            "messages": messages,
        }

    def get_message(self, agent: str, message_id: str, full: bool = True) -> dict:
        agent = normalize_name(agent)
        with self._session(readonly=True) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            delivery = _resolve_own_delivery(conn, message_id, agent) if row is not None else None
            if row is None or (delivery is None and not _is_party(conn, row, agent)):
                raise MailError("not_found", "没有这封信，或者你不在其中", 404)
            status = delivery["status"] if delivery else None
            read_at = delivery["read_at"] if delivery else None
            claimed_by = delivery["claimed_by"] if delivery else None
            claimed_at = delivery["claimed_at"] if delivery else None
            holder = delivery["to_agent"] if delivery else agent
            view = _view(conn, row, holder, full, status, read_at, claimed_by, claimed_at)
            if delivery is not None:
                view["delivered_to"] = delivery["to_agent"]
            return view

    def mark(
        self,
        agent: str,
        message_id: str,
        status: str,
        window_id: str | None = None,
        force: bool = False,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent)
        window_id = normalize_window(window_id)
        if status not in {"read", "archived"}:
            raise MailError("invalid_status", "只能标成 read 或 archived")
        with self._session(immediate=True) as conn:
            msg_row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            delivery = _resolve_own_delivery(conn, message_id, agent) if msg_row is not None else None
            if msg_row is None or (delivery is None and not _is_party(conn, msg_row, agent)):
                raise MailError("not_found", "没有这封信，或者你不在其中", 404)
            if delivery is None:
                raise MailError("not_a_recipient", "这封信不是留给你的，不能替别人签收", 409)
            # 落在已经存在的那一行上。先本名，没有才是另一个别名。不改名、不新建。
            target = delivery["to_agent"]
            now = utcnow()
            read_at = delivery["read_at"] or now
            if window_id is None:
                if status == "read":
                    _mark_own_delivery_read(conn, message_id, agent, now)
                else:
                    # 归档仍只动先找到的那一行（本名在就本名）。不连带清另一个名字。
                    conn.execute(
                        "UPDATE deliveries SET status = ?, read_at = ? WHERE message_id = ? AND to_agent = ?",
                        (status, read_at, message_id, target),
                    )
            else:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=agent,
                    workspace=msg_row["workspace"],
                    activity=None,
                    pid=pid,
                    now=now,
                )
                if status == "read":
                    # 已封卷的线程在这里只标已读就返回：不认领、不动案卷。
                    _claim_thread_on_touch(
                        conn,
                        agent=agent,
                        window_id=window_id,
                        message_row=msg_row,
                        force=bool(force),
                        mark_read=True,
                        now=now,
                    )
                else:
                    conn.execute(
                        "UPDATE deliveries SET status = ?, read_at = ? WHERE message_id = ? AND to_agent = ?",
                        (status, read_at, message_id, target),
                    )
        self._signal()
        return self.get_message(agent, message_id, full=True)

    def thread(self, agent: str, thread_id: str) -> dict:
        agent = normalize_name(agent)
        thread_id = normalize_case_id(thread_id)
        names = _recipient_names(agent)
        name_sql = ",".join("?" for _ in names)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT m.* FROM messages m
                WHERE m.thread_id = ?
                  AND (
                    m.from_agent IN ({name_sql})
                    OR EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.message_id = m.id AND d.to_agent IN ({name_sql})
                    )
                  )
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (thread_id, *names, *names),
            ).fetchall()
            if not rows:
                raise MailError("not_found", "没有这封信，或者你不在其中", 404)
            # 树结构派生（不改存储、不嵌套）。agent 定位用：扁平数组 + 每封自带
            # parent_id/depth/children/root_id，仍按时间序排列。
            # 只在可见集内构树：父信不在可见集（未参与/跨线程导入）时该封按根处理，
            # 但 parent_id 仍返回真实 reply_to，不掩盖事实。
            ids = {row["id"] for row in rows}
            children_map: dict[str, list[str]] = {row["id"]: [] for row in rows}
            parent_of: dict[str, str | None] = {}
            for row in rows:
                parent = row["reply_to"]
                in_thread = parent if parent in ids else None
                parent_of[row["id"]] = in_thread
                if in_thread:
                    children_map[in_thread].append(row["id"])
            depth_map: dict[str, int] = {}
            root_map: dict[str, str] = {}
            for row in rows:
                if row["id"] in depth_map:
                    continue
                chain: list[str] = []
                cur: str | None = row["id"]
                # 逐级上溯；cycle 或不可见父信即停（chain 内的节点防御性防环）。
                while cur is not None and cur not in depth_map and cur not in chain:
                    chain.append(cur)
                    cur = parent_of.get(cur)
                if cur is not None and cur in depth_map:
                    base, base_root = depth_map[cur] + 1, root_map[cur]
                else:
                    base, base_root = 0, chain[-1]
                for node in reversed(chain):
                    depth_map[node] = base
                    root_map[node] = base_root
                    base += 1
            messages = []
            for row in rows:
                delivery = _resolve_own_delivery(conn, row["id"], agent)
                view = _view(
                    conn,
                    row,
                    agent,
                    True,
                    delivery["status"] if delivery else None,
                    delivery["read_at"] if delivery else None,
                    delivery["claimed_by"] if delivery else None,
                    delivery["claimed_at"] if delivery else None,
                )
                # 树结构派生字段：agent 靠它们定位分支，无需拿 id 两两对照。
                view["parent_id"] = row["reply_to"]
                view["depth"] = depth_map[row["id"]]
                view["root_id"] = root_map[row["id"]]
                view["children"] = children_map[row["id"]]
                # 只给发件人补收件人回执。delivery_status/read_at 仍是查看者自己的那份，语义不变。
                if _same_agent(row["from_agent"], agent):
                    view["recipient_receipts"] = _delivery_receipts(conn, row["id"])
                messages.append(view)
        return {"thread_id": thread_id, "count": len(messages), "messages": messages}

    def sent(self, agent: str, limit: int = 20) -> dict:
        """只读。我发出的信，以及每个收件人的未读/已读时间和是否已在本线程回过。

        不改 deliveries，不标已读。供 CLI `ic sent` 使用（路由归调用方）。
        """
        name = normalize_name(agent)
        n = _limit(limit)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE from_agent = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (name, n),
            ).fetchall()
            messages = []
            for row in rows:
                recipients = []
                for receipt in _delivery_receipts(conn, row["id"]):
                    recipients.append(
                        {
                            **receipt,
                            "replied": _recipient_replied(conn, row, receipt["name"]),
                        }
                    )
                messages.append(
                    {
                        "id": row["id"],
                        "thread_id": row["thread_id"],
                        "from": row["from_agent"],
                        "subject": row["subject"],
                        "workspace": row["workspace"],
                        "created_at": row["created_at"],
                        "created_at_beijing": beijing_time(row["created_at"]),
                        "recipients": recipients,
                    }
                )
        return {"agent": name, "count": len(messages), "messages": messages}


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
        # 空串＝**显式清空**（清完这个项目就"没指定"，coordinator 类操作一律拒）；None＝别动它。
        if isinstance(coordinator, str) and not coordinator.strip():
            wait_coord = ""
        else:
            wait_coord = normalize_name(coordinator) if isinstance(coordinator, str) else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            if wait_coord is not None:
                # human 2026-10-16 拍：**只有 human** 能指定/更换项目的 coordinator（真护栏，不是文档级）
                _must_human(actor or "")
                if wait_coord:   # 空串＝清空，不用查名册；给了名字就要求它在名册里（打错的名字会静默变成"没指定"）
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
                # **留痕**：指定/更换项目的 coordinator 是**治理**动作——必须进只增痕迹（哈希链覆盖它，
                # 外锚也把它钉住）。空 task_id 与案卷的 `acceptance` 事件同例，属正常用法。
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
            _lazy_reconcile_all(conn, now)
            return _collect_projects(conn)

    def agent_messages(self, agent: str, limit: int = 100) -> list[dict]:
        name = normalize_name(agent)
        names = _recipient_names(name)
        n = _limit(limit)
        name_sql = ",".join("?" for _ in names)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.thread_id, m.from_agent, m.subject, m.created_at, m.workspace, m.body,
                       (SELECT c.title FROM cases c WHERE (c.id = m.thread_id OR c.thread_id = m.thread_id) LIMIT 1) AS case_title,
                       (SELECT c.id FROM cases c WHERE (c.id = m.thread_id OR c.thread_id = m.thread_id) LIMIT 1) AS case_id
                FROM messages m
                WHERE m.from_agent IN ({name_sql})
                   OR EXISTS (
                        SELECT 1 FROM deliveries d2
                        WHERE d2.message_id = m.id AND d2.to_agent IN ({name_sql})
                   )
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT ?
                """,
                (*names, *names, n),
            ).fetchall()
            out = []
            for row in rows:
                recips = [
                    r["to_agent"]
                    for r in conn.execute(
                        "SELECT to_agent FROM deliveries WHERE message_id = ? ORDER BY to_agent",
                        (row["id"],),
                    )
                ]
                own = _resolve_own_delivery(conn, row["id"], name)
                sent = row["from_agent"] == name or (
                    is_person(name) and is_person(row["from_agent"])
                )
                out.append(
                    {
                        "id": row["id"],
                        "thread_id": row["thread_id"],
                        "from": row["from_agent"],
                        "to": recips,
                        "subject": row["subject"],
                        "body": row["body"] or "",
                        "workspace": row["workspace"] or "",
                        "direction": "out" if sent else "in",
                        "other": (", ".join(recips) if sent else row["from_agent"]),
                        "claimed_by": (own["claimed_by"] if own else None) or "",
                        "case_id": row["case_id"] or "",
                        "case_title": row["case_title"] or "",
                        "my_status": (own["status"] if own else None) if not sent else None,
                        "delivered_to": own["to_agent"] if own is not None else "",
                        "created_at": row["created_at"],
                        "created_at_beijing": beijing_time(row["created_at"]),
                    }
                )
        return out

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


def _exact_send_id(
    conn: sqlite3.Connection,
    sender: str,
    reply_to: str | None,
    subject: str,
    body: str,
) -> str | None:
    """同一发件人、同一 reply_to、同一主题、正文逐字节相同的最早一封。

    键不含收件人、优先级、workspace。正文按存储后的字节比，不模糊、不哈希落列。
    没有就返回 None。已有多条全同的，返回最早那条，不合并旧行。
    """
    row = conn.execute(
        """
        SELECT id FROM messages
        WHERE from_agent = ?
          AND subject = ?
          AND body = ?
          AND (
            (? IS NULL AND reply_to IS NULL)
            OR reply_to = ?
          )
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (sender, subject, body, reply_to, reply_to),
    ).fetchone()
    return row["id"] if row else None






def _resolve_outgoing_workspace(
    conn: sqlite3.Connection,
    explicit: str | None,
    parent: sqlite3.Row | None,
    thread_id: str | None,
    window_id: str | None,
    client_cwd: str | None,
) -> tuple[str | None, str]:
    """发出去的 workspace。不读进程 cwd，不登记新项目。

    顺序：显式传入 > 父信 > 该线程案卷 > 发送窗口已登记的 workspace
    > 客户端 cwd 命中已登记的未归档 project > 留空。
    窗口有登记值就用窗口原文，不再要求与 cwd 一致；窗口没有时才看 cwd。
    cwd 命中多条绝对路径、只命中已归档、或根本不命中：不猜。
    返回 (workspace, source)，source 为 explicit/parent/case/window/project/empty。
    """
    if explicit:
        return explicit, "explicit"
    if parent is not None:
        parent_ws = _workspace(parent["workspace"])
        if parent_ws:
            return parent_ws, "parent"
    if thread_id:
        case_row = _find_case(conn, thread_id)
        if case_row is not None:
            case_ws = _workspace(case_row["workspace"])
            if case_ws:
                return case_ws, "case"
    window_ws = _workspace(_window_workspace(conn, window_id))
    if window_ws:
        return window_ws, "window"
    hit = _cwd_project_workspace(conn, client_cwd)
    if hit:
        return hit, "project"
    return None, "empty"


def _cwd_project_workspace(conn: sqlite3.Connection, client_cwd: str | None) -> str | None:
    """客户端 cwd 命中的未归档 project。不读进程 cwd。

    路径正好对上（字符串或 resolve 后）且只对上一条时，用那条。
    否则按范围命中：恰好一条，或只有一条绝对路径，才采用。
    多条绝对路径都算命中、只命中已归档、或没有命中：不猜，返回 None。
    """
    cwd = _workspace(client_cwd)
    if not cwd:
        return None
    rows = [
        row["workspace"]
        for row in conn.execute("SELECT workspace FROM projects WHERE archived = 0").fetchall()
        if row["workspace"]
    ]
    exact: list[str] = []
    cwd_path = Path(cwd).expanduser()
    for ws in rows:
        if ws == cwd:
            exact.append(ws)
            continue
        try:
            if Path(ws).expanduser().resolve() == cwd_path.resolve():
                exact.append(ws)
        except OSError:
            pass
    unique_exact = list(dict.fromkeys(exact))
    if len(unique_exact) == 1:
        return unique_exact[0]
    if len(unique_exact) > 1:
        return None
    hits = [ws for ws in rows if _workspace_scope(cwd, ws)]
    if len(hits) == 1:
        return hits[0]
    absolutes = [ws for ws in hits if Path(ws).is_absolute()]
    if len(absolutes) == 1 and len(hits) == len(absolutes):
        return absolutes[0]
    return None


def _delivery_receipts(conn: sqlite3.Connection, message_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT to_agent, status, read_at
        FROM deliveries
        WHERE message_id = ?
        ORDER BY to_agent
        """,
        (message_id,),
    ).fetchall()
    return [
        {"name": row["to_agent"], "status": row["status"], "read_at": row["read_at"]}
        for row in rows
    ]


def _recipient_replied(conn: sqlite3.Connection, message_row: sqlite3.Row, recipient: str) -> bool:
    """收件人是否已在本线程回过这封信：直接 reply_to，或本线程里更晚的一封出自该收件人。"""
    found = conn.execute(
        """
        SELECT 1 FROM messages
        WHERE thread_id = ?
          AND from_agent = ?
          AND id != ?
          AND (
            reply_to = ?
            OR created_at > ?
            OR (created_at = ? AND id > ?)
          )
        LIMIT 1
        """,
        (
            message_row["thread_id"],
            recipient,
            message_row["id"],
            message_row["id"],
            message_row["created_at"],
            message_row["created_at"],
            message_row["id"],
        ),
    ).fetchone()
    return found is not None


def _collect_projects(conn: sqlite3.Connection) -> list[dict]:
    now = utcnow()
    # 汇总所有出现过的 workspace（projects 表 + cases/windows/messages）
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

    # 同一项目可能以「绝对路径」和「目录名」两种写法出现（messages 里常见裸目录名）。
    # 以绝对路径为锚，把同名裸目录名归并到同一项，避免 /ui 项目列表出现重复。
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
        # 优先用「锚点」（绝对路径）那一行的元数据，避免裸目录名那行把 name/archived 覆盖掉。
        # 旧写法 raw_rows.get(ws) 先取，集合遍历顺序不定 → archived/name 会随 PYTHONHASHSEED 翻转。
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






# ── M8/M9/M10 治理线常量与底层函数已抽取至 infocenter.governance ──









def _known_names(conn: sqlite3.Connection, home=None) -> set[str]:
    """在册的身份 = 库里注册过的 ∪ 名册（config.json）里列着的。

    为什么不只看库：`ic crew kickoff` 是**按名册群发**开工信，名册里的新同事可能一次都还没
    开口（`agents` 表里还没有他）——只看库会把这些人当成「不存在」而把整封 kickoff 拒掉。
    读不到配置就不较真：宁可漏拒，不可误拒。
    """
    names = {row[0] for row in conn.execute("SELECT name FROM agents")}
    if home:
        try:
            from infocenter import config as _config

            for item in _config.load(home).get("agents") or []:
                name = str((item or {}).get("name") or "").strip().lower()
                if name:
                    names.add(name)
        except Exception:
            pass
    return names


_upsert = _upsert_agent


def _unread_counts(conn: sqlite3.Connection, window_id: str | None = None) -> dict[str, int]:
    """M1-data：`ic agents` 未读角标与 `events.unread_total` 同口径——
    无 window_id 时只算「未被任何窗口认领」且线程未暂停/未封卷的信；
    有 window_id 时排除被同 agent 其他窗口认领或已暂停/已封卷的信（计划 §4.6）。"""
    rows = conn.execute(
        """
        SELECT d.message_id AS message_id, d.to_agent AS to_agent,
               d.claimed_by AS claimed_by, m.thread_id AS thread_id
        FROM deliveries d
        JOIN messages m ON m.id = d.message_id
        WHERE d.status = 'unread'
        """
    ).fetchall()
    # 人的两个名字合成一个未读数，同一封信两行都未读只算一次。名字只问 _recipient_names。
    folded = set(_recipient_names(HUMAN))
    fold = len(folded) > 1
    counts: dict[str, int] = {}
    person_ids: set[str] = set()
    for row in rows:
        if _is_thread_paused_or_sealed(conn, row["thread_id"]):
            continue
        eff_claim = _effective_thread_claim(conn, row["thread_id"], row["to_agent"], row["claimed_by"])
        if window_id is None:
            if eff_claim is not None:
                continue
        else:
            if eff_claim is not None and eff_claim != window_id:
                continue
        if fold and row["to_agent"] in folded:
            person_ids.add(row["message_id"])
        else:
            counts[row["to_agent"]] = counts.get(row["to_agent"], 0) + 1
    if fold:
        merged = len(person_ids)
        for name in folded:
            counts[name] = merged
    return counts


def _must_visible(conn: sqlite3.Connection, message_id: str, agent: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None or not _is_party(conn, row, agent):
        raise MailError("not_found", "没有这封信，或者你不在其中", 404)
    return row




def _recipients(conn: sqlite3.Connection, message_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT to_agent FROM deliveries WHERE message_id = ? ORDER BY to_agent",
        (message_id,),
    ).fetchall()
    return [row["to_agent"] for row in rows]


def _view(
    conn,
    row,
    agent: str,
    full: bool,
    delivery_status=None,
    read_at=None,
    claimed_by=None,
    claimed_at=None,
) -> dict:
    body = row["body"]
    eff_claim = _effective_thread_claim(conn, row["thread_id"], agent, claimed_by)
    view = {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "from": row["from_agent"],
        "to": _recipients(conn, row["id"]),
        "subject": row["subject"],
        "priority": row["priority"],
        "workspace": row["workspace"],
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
        "created_at_beijing": beijing_time(row["created_at"]),
        "body_length": len(body),
        "delivery_status": delivery_status,
        "read_at": read_at,
        "claimed_by": eff_claim,
        "claimed_at": claimed_at,
    }
    if full:
        view["body"] = body
    else:
        view["preview"] = preview(body)
    return view


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



