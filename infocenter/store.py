import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BROADCAST = {"*", "all", "everyone"}
NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$", re.UNICODE)
WINDOW_RE = re.compile(r"^[\w][\w.:-]{0,63}$", re.UNICODE)
# L2：案卷编号 / thread_id 的安全字符集与长度（与窗口号同风格，首字符须为字母/数字/下划线）。
# 服务端源头堵住 XSS：id 会进 URL / 前端属性，禁止换行、引号、斜杠、前导 . - : 等。
CASE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[\w][\w-]{0,31}$", re.UNICODE)

CASE_STATUS_LABELS = {
    "pending": "待办",
    "open": "在办",
    "doing": "在办",
    "waiting": "待谁",
    "paused": "暂停",
    "aborted": "中止",
    "done": "办结",
}
_LABEL_TO_CASE_STATUS = {
    "待办": "pending",
    "在办": "open",
    "待谁": "waiting",
    "暂停": "paused",
    "中止": "aborted",
    "办结": "done",
}
SEALED_STATUSES = {"aborted", "done"}

WINDOW_STATE_LABELS = {
    "active": "在办",
    "paused": "暂停",
    "left": "离场",
    "offline": "离场",
}


class MailError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time(utc_iso: str) -> str:
    """UTC ISO → 本机本地时区。存储统一 UTC，展示统一本地。"""
    try:
        moment = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(utc_iso)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def local_hhmm(utc_iso: str) -> str:
    try:
        moment = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(utc_iso)[:5]
    return moment.astimezone().strftime("%H:%M")


def new_id() -> str:
    return f"m{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def new_window_id() -> str:
    return f"w-{secrets.token_hex(3)}"


def normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise MailError("invalid_name", "agent 名字必须是字符串")
    name = (value or "").strip().lower()
    if name in BROADCAST or not NAME_RE.fullmatch(name) or ".." in name:
        raise MailError("invalid_name", "agent 名字需为 1–64 位字母、数字、_、- 或 .，且不能是 *、all、everyone")
    return name


def normalize_case_id(value: str) -> str:
    """L2：案卷编号 / thread_id 统一安全字符集与长度，非法一律 400。"""
    if not isinstance(value, str):
        raise MailError("invalid_case_id", "案卷编号必须是字符串")
    text = value.strip()
    if not CASE_ID_RE.fullmatch(text) or ".." in text or text[0] in {".", "-", ":"}:
        raise MailError(
            "invalid_case_id",
            "案卷编号需为 1–64 位字母、数字、_、-、. 或 :",
        )
    return text


def normalize_window(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError("invalid_window", "窗口号必须是字符串")
    text = value.strip()
    if not text:
        return None
    if not WINDOW_RE.fullmatch(text) or ".." in text:
        raise MailError("invalid_window", "窗口号需为 1–64 位字母、数字、_、-、. 或 :")
    return text


def _is_wire_name(value: str) -> bool:
    """恰好一个点、左右都是合法节点段，且整串合法。"""
    if not isinstance(value, str):
        return False
    name = value.strip().lower()
    left, dot, right = name.partition(".")
    if not dot or "." in right:
        return False
    if not _SEGMENT_RE.fullmatch(left) or not _SEGMENT_RE.fullmatch(right):
        return False
    return bool(NAME_RE.fullmatch(name)) and ".." not in name


def parse_recipients(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        parts = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, list):
        parts = value
    else:
        raise MailError("invalid_recipient", "to 必须是名字、逗号分隔的名字，或 *")
    if not parts:
        raise MailError("invalid_recipient", "至少要有一个收件人")
    tokens: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise MailError("invalid_recipient", "收件人必须是字符串")
        token = part.strip().lower()
        tokens.append(token if token in BROADCAST else normalize_name(token))
    if any(token in BROADCAST for token in tokens):
        if len(tokens) != 1:
            raise MailError("invalid_recipient", "广播不能和其他收件人混在一起")
        return ["*"]
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return seen


def preview(body: str, limit: int = 180) -> str:
    flat = " ".join(body.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


class Store:
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
                """
            )
            agent_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
            if "role" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            if "activity" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN activity TEXT NOT NULL DEFAULT ''")
            if "activity_at" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN activity_at TEXT NOT NULL DEFAULT ''")

            delivery_cols = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
            if "claimed_by" not in delivery_cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN claimed_by TEXT")
            if "claimed_at" not in delivery_cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN claimed_at TEXT")

            window_cols = {row[1] for row in conn.execute("PRAGMA table_info(windows)")}
            if "pid" not in window_cols:
                conn.execute("ALTER TABLE windows ADD COLUMN pid INTEGER")

            project_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            if "archived" not in project_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    def seed(self) -> None:
        now = utcnow()
        with self._session(immediate=True) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO agents(name, description, role, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(name, description, role, now, now) for name, description, role in self._seeds],
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

    def remove_agent(self, name: str) -> dict:
        name = normalize_name(name)
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
        with self._cond:
            while True:
                seen = self._seq
                snapshot = self._events_snapshot(
                    agent, since, limit, workspace, window_id, pid, touch_window=first_pass
                )
                first_pass = False
                if snapshot["new"] or wait <= 0:
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
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
        旧实现取全表 MAX(rowid) 却只回最新 limit 条，超出的旧信会永久跳过。"""
        if window_id is None:
            with self._session(readonly=True) as conn:
                rows = conn.execute(
                    """
                    SELECT m.*, d.rowid AS delivery_seq, d.status AS delivery_status,
                           d.read_at AS delivery_read_at, d.claimed_by AS delivery_claimed_by,
                           d.claimed_at AS delivery_claimed_at
                    FROM deliveries d
                    JOIN messages m ON m.id = d.message_id
                    WHERE d.to_agent = ? AND d.status = 'unread'
                    ORDER BY d.rowid ASC
                    """,
                    (agent,),
                ).fetchall()
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
                """
                SELECT m.*, d.rowid AS delivery_seq, d.status AS delivery_status,
                       d.read_at AS delivery_read_at, d.claimed_by AS delivery_claimed_by,
                       d.claimed_at AS delivery_claimed_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent = ? AND d.status = 'unread'
                ORDER BY d.rowid ASC
                """,
                (agent,),
            ).fetchall()
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
                    cur = conn.execute(
                        """
                        UPDATE deliveries
                        SET claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
                        WHERE message_id = ? AND to_agent = ?
                          AND (claimed_by IS NULL OR claimed_by = ?)
                        """,
                        (window_id, now, row["id"], agent, window_id),
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
    ) -> dict:
        sender = normalize_name(sender)
        window_id = normalize_window(window_id)
        subject = _subject(subject if subject_override is None else subject_override)
        body = _body(body)
        priority = _priority(priority)
        workspace = _workspace(workspace)
        reply_to = _reply_to(reply_to)
        recipients_in = parse_recipients(to)
        node_text = str(local_node).strip().lower() if isinstance(local_node, str) and local_node.strip() else None
        with self._session(immediate=True) as conn:
            _upsert(conn, sender, None)
            now = utcnow()
            if window_id is not None:
                _upsert_window(conn, window_id=window_id, agent=sender, workspace=workspace, activity=None, pid=pid, now=now)
            if workspace:
                _upsert_project(conn, workspace, None, now)
            thread_id = None
            parent_id = None
            if reply_to:
                parent = _must_visible(conn, reply_to, sender)
                parent_id = parent["id"]
                thread_id = parent["thread_id"]
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
                    if case_row["status"] == "waiting" and case_row["owner_agent"] in recipients and sender != case_row["owner_agent"]:
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
                    elif case_row["status"] == "open" and sender == case_row["owner_agent"]:
                        wait_for = next((r for r in recipients if r != sender), None)
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
        self._signal()
        return self.get_message(sender, message_id, full=True)

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
            if parent_view["from"] != sender:
                targets: list[str] | str = [parent_view["from"]]
            else:
                others = [name for name in parent_view["to"] if name != sender]
                targets = others or [sender]
        else:
            targets = to
        final_subject = subject if subject else _re_subject(parent_view["subject"])
        return self.send(
            sender,
            targets,
            final_subject,
            body,
            priority="normal",
            workspace=parent_view.get("workspace"),
            reply_to=parent_view["id"],
            local_node=local_node,
            window_id=window_id,
            pid=pid,
        )

    def import_remote(self, envelope: dict, local_bare_names: list[str]) -> dict:
        """B3 §10：从网盘 JSON 导入他机消息。按 id + 不可变字段幂等，不 upsert 发信方，不 _signal。

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
                    and sorted(_recipients(conn, msg_id)) == bare_names
                )
                return {"status": "duplicate" if same else "conflict"}

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
        return {"status": "imported"}

    def apply_receipt(self, message_id: str, by: str, read_at: str) -> dict:
        """B3 §10：应用他机已读回执。仅当 to_agent==by 且当前为 unread 时标成 read，不 _signal。"""
        msg_id = str(message_id or "").strip()
        by_name = normalize_name(str(by or ""))
        read_time = str(read_at or "").strip()
        if not msg_id or not read_time:
            raise MailError("invalid_receipt", "回执缺少 message_id 或 read_at")
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
                return {"status": "applied"}
            if row["status"] == "read":
                return {"status": "duplicate"}
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
            rows = conn.execute(
                """
                SELECT m.*, d.status AS delivery_status, d.read_at AS delivery_read_at,
                       d.claimed_by AS delivery_claimed_by, d.claimed_at AS delivery_claimed_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent = ?
                  AND (? = 'all' OR d.status = ?)
                  AND (? IS NULL OR m.workspace = ?)
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
                    agent,
                    status,
                    status,
                    workspace,
                    workspace,
                    window_id,
                    status,
                    window_id,
                    window_id,
                    window_id,
                ),
            ).fetchall()
            messages = []
            for row in rows:
                if window_id is not None and status == "unread":
                    if _is_thread_paused_or_sealed(conn, row["thread_id"]):
                        continue
                    eff_claim = _effective_thread_claim(conn, row["thread_id"], agent, row["delivery_claimed_by"])
                    if eff_claim is not None and eff_claim != window_id:
                        continue
                messages.append(
                    _view(
                        conn,
                        row,
                        agent,
                        full,
                        row["delivery_status"],
                        row["delivery_read_at"],
                        row["delivery_claimed_by"],
                        row["delivery_claimed_at"],
                    )
                )
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
                        conn, row["thread_id"], agent, row["delivery_claimed_by"]
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
            row = _must_visible(conn, message_id, agent)
            delivery = conn.execute(
                "SELECT status, read_at, claimed_by, claimed_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (row["id"], agent),
            ).fetchone()
            status = delivery["status"] if delivery else None
            read_at = delivery["read_at"] if delivery else None
            claimed_by = delivery["claimed_by"] if delivery else None
            claimed_at = delivery["claimed_at"] if delivery else None
            return _view(conn, row, agent, full, status, read_at, claimed_by, claimed_at)

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
            msg_row = _must_visible(conn, message_id, agent)
            delivery = conn.execute(
                "SELECT status, read_at, claimed_by, claimed_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
            if delivery is None:
                raise MailError("not_a_recipient", "这封信不是留给你的，不能替别人签收", 409)
            now = utcnow()
            read_at = delivery["read_at"] or now
            if window_id is None:
                conn.execute(
                    "UPDATE deliveries SET status = ?, read_at = ? WHERE message_id = ? AND to_agent = ?",
                    (status, read_at, message_id, agent),
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
                        (status, read_at, message_id, agent),
                    )
        self._signal()
        return self.get_message(agent, message_id, full=True)

    def thread(self, agent: str, thread_id: str) -> dict:
        agent = normalize_name(agent)
        thread_id = normalize_case_id(thread_id)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT m.* FROM messages m
                WHERE m.thread_id = ?
                  AND (
                    m.from_agent = ?
                    OR EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.message_id = m.id AND d.to_agent = ?
                    )
                  )
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (thread_id, agent, agent),
            ).fetchall()
            if not rows:
                raise MailError("not_found", "没有这封信，或者你不在其中", 404)
            messages = []
            for row in rows:
                delivery = conn.execute(
                    "SELECT status, read_at, claimed_by, claimed_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                    (row["id"], agent),
                ).fetchone()
                messages.append(
                    _view(
                        conn,
                        row,
                        agent,
                        True,
                        delivery["status"] if delivery else None,
                        delivery["read_at"] if delivery else None,
                        delivery["claimed_by"] if delivery else None,
                        delivery["claimed_at"] if delivery else None,
                    )
                )
        return {"thread_id": thread_id, "count": len(messages), "messages": messages}

    # ── D1 案卷（cases & case_nodes）─────────────────────────────────────

    def create_case(
        self,
        agent: str = "human",
        title: str | None = None,
        workspace: str | None = None,
        owner_agent: str | None = None,
        owner_window: str | None = None,
        status: str | None = None,
        note: str | None = None,
        thread_id: str | None = None,
        to=None,
        body: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent or "human")
        owner_window = normalize_window(owner_window)
        owner_agent = normalize_name(owner_agent) if owner_agent else (agent if owner_window else None)
        workspace = _workspace(workspace)
        title_text = _subject(title or "未命名案卷")

        # D-6：`to`/`body` 是「立案并同时发始信」的一对字段，必须成对给或不给。
        # 只给 to（缺 body）没有正文、只给 body（缺 to）没有收件人，都无法送达 → 一律 400，
        # 不再对任何调用方静默忽略或视情形降级为「开篇节点」。
        # 只想要案卷开篇节点正文时，用 `note` 字段（走 init_note），不要用 `body`。
        if (to is None) != (body is None):
            raise MailError(
                "invalid_request",
                "to 与 body 必须同时提供：这一对用于「立案并同时发始信」；只写开篇节点请用 note",
                400,
            )
        # L2：显式指定的 thread_id / case id 必须落在安全字符集与长度内。
        requested = normalize_case_id(thread_id) if thread_id is not None else None

        if to is not None and body is not None:
            sent = self.send(
                sender=agent,
                to=to,
                subject=title_text,
                body=body,
                workspace=workspace,
                window_id=owner_window if owner_agent == agent else None,
                pid=pid,
            )
            thread_id = sent["thread_id"]

        cid = requested if requested is not None else ((thread_id or "").strip() or new_id())
        init_status = _case_status(status or ("open" if owner_window else "pending"))
        if init_status in SEALED_STATUSES:
            raise MailError("invalid_status", "新建案卷状态不能直接为已封卷状态")
        init_note = _node_note(note or ("认领 → 在办" if owner_window else "立案"))
        now = utcnow()
        with self._session(immediate=True) as conn:
            existing = _find_case(conn, cid)
            if existing is not None:
                raise MailError("case_exists", f"案卷 {cid} 已存在", 409)
            # D-3：显式指定已存在的线程立案时，调用者必须是该线程当事人，否则 404。
            if requested is not None and cid == requested:
                if _thread_has_messages(conn, cid) and not _is_thread_party(conn, cid, agent):
                    raise MailError("not_found", f"没有找到案卷或线程：{cid}", 404)
            if owner_window and owner_agent:
                _upsert_window(
                    conn,
                    window_id=owner_window,
                    agent=owner_agent,
                    workspace=workspace,
                    activity=None,
                    pid=pid,
                    now=now,
                )
            if workspace:
                _upsert_project(conn, workspace, None, now)
            conn.execute(
                """
                INSERT INTO cases(
                    id, thread_id, title, workspace, status, owner_agent, owner_window,
                    waiting_for, sealed, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL)
                """,
                (cid, cid, title_text, workspace, init_status, owner_agent, owner_window, now, now),
            )
            _insert_node(
                conn,
                case_id=cid,
                agent=owner_agent or agent,
                window_id=owner_window,
                action="create",
                note=init_note,
                status=init_status,
                auto=False,
                now=now,
            )
            row = _find_case(conn, cid)
            return _case_view(conn, row)

    def get_case(self, case_id: str) -> dict:
        if not isinstance(case_id, str) or not case_id.strip():
            raise MailError("not_found", "缺少案卷编号", 404)
        cid = normalize_case_id(case_id)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            row = _lazy_reconcile_case(conn, row, now)
            return _case_view(conn, row)

    def list_cases(
        self,
        workspace: str | None = None,
        status: str | None = None,
        owner_window: str | None = None,
        owner_agent: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        limit = _limit(limit)
        status_filter = _case_status(status) if status else None
        window_filter = normalize_window(owner_window) if owner_window else None
        agent_filter = normalize_name(owner_agent) if owner_agent else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            _lazy_reconcile_all(conn, now)
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            matched = []
            for row in rows:
                if workspace is not None and not _workspace_scope(row["workspace"], workspace):
                    continue
                if status_filter is not None and row["status"] != status_filter:
                    continue
                if window_filter is not None and row["owner_window"] != window_filter:
                    continue
                if agent_filter is not None and row["owner_agent"] != agent_filter:
                    continue
                matched.append(_case_view(conn, row))
                if len(matched) >= limit:
                    break
        return matched

    def append_case_node(
        self,
        case_id: str,
        agent: str,
        note: str,
        status: str | None = None,
        window_id: str | None = None,
        action: str = "note",
        waiting_for: str | None = None,
        auto: bool = False,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        note_text = _node_note(note)
        action_text = _node_action(action or "note")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _must_case(conn, case_id, now)
            _must_unsealed(row)
            next_status = _case_status(status) if status else row["status"]
            if next_status == "aborted":
                return self._transition_case_in_tx(
                    conn, row, agent, window_id, "abort", note_text, "aborted", seal=True, auto=auto, now=now
                )
            if next_status == "done":
                return self._transition_case_in_tx(
                    conn, row, agent, window_id, "close", note_text, "done", seal=True, auto=auto, now=now
                )
            if window_id is not None:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=agent,
                    workspace=row["workspace"],
                    activity=None,
                    pid=pid,
                    now=now,
                )
            wait_target = normalize_name(waiting_for) if waiting_for else (row["waiting_for"] if next_status == "waiting" else None)
            conn.execute(
                """
                UPDATE cases
                SET status = ?, waiting_for = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, wait_target, now, row["id"]),
            )
            _insert_node(
                conn,
                case_id=row["id"],
                agent=agent,
                window_id=window_id or row["owner_window"],
                action=action_text,
                note=note_text,
                status=next_status,
                auto=auto,
                now=now,
            )
            updated = _find_case(conn, row["id"])
            return _case_view(conn, updated)

    def claim_case(
        self,
        case_id: str,
        agent: str,
        window_id: str,
        force: bool = False,
        note: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent)
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        if not window_id:
            raise MailError("window_required", "认领案卷需要提供窗口号（X-Window）")
        now = utcnow()
        with self._session(immediate=True) as conn:
            _upsert_window(conn, window_id=window_id, agent=agent, workspace=None, activity=None, pid=pid, now=now)
            row = _find_case(conn, case_id)
            if row is not None:
                has_msg = conn.execute(
                    "SELECT 1 FROM messages WHERE thread_id = ? LIMIT 1",
                    (row["thread_id"],),
                ).fetchone()
                if (
                    has_msg is not None
                    and agent != "human"
                    and row["owner_agent"] != agent
                    and not _is_thread_party(conn, row["thread_id"], agent)
                ):
                    raise MailError("not_found", f"没有找到案卷：{case_id}", 404)
                row = _lazy_reconcile_case(conn, row, now)
                _must_unsealed(row)
                cur_owner = row["owner_window"]
                if cur_owner and cur_owner != window_id and not force:
                    raise MailError("already_claimed", f"这条线已被 {cur_owner} 主办", 409)
                if cur_owner != window_id:
                    action = "takeover" if (cur_owner and force) else "claim"
                    default_note = f"强制接手（原主办 {cur_owner}） → 在办" if (cur_owner and force) else "认领 → 在办"
                    note_text = _node_note(note or default_note)
                    conn.execute(
                        """
                        UPDATE cases
                        SET owner_agent = ?, owner_window = ?, status = 'open',
                            waiting_for = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (agent, window_id, now, row["id"]),
                    )
                    _propagate_thread_claim(conn, row["thread_id"], agent, window_id, now, overwrite=force)
                    _insert_node(
                        conn,
                        case_id=row["id"],
                        agent=agent,
                        window_id=window_id,
                        action=action,
                        note=note_text,
                        status="open",
                        auto=False,
                        now=now,
                    )
                updated = _find_case(conn, row["id"])
                return _case_view(conn, updated)

            msg = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? OR id = ? ORDER BY created_at ASC LIMIT 1",
                (case_id, case_id),
            ).fetchone()
            if msg is None:
                raise MailError("not_found", f"没有找到案卷或线程：{case_id}", 404)
            # D-3：立案前要求调用者是当事人（发信人或某投递收件人），否则 404，防止非参与者抢先立案。
            if not _is_thread_party(conn, msg["thread_id"], agent):
                raise MailError("not_found", f"没有找到案卷或线程：{case_id}", 404)
            _claim_thread_on_touch(
                conn,
                agent=agent,
                window_id=window_id,
                message_row=msg,
                force=bool(force),
                mark_read=False,
                now=now,
            )
            created = _find_case(conn, msg["thread_id"])
            return _case_view(conn, created)

    def transfer_case(
        self,
        case_id: str,
        agent: str,
        to_window: str,
        to_agent: str | None = None,
        window_id: str | None = None,
        note: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        target_window = normalize_window(to_window)
        if not target_window:
            raise MailError("invalid_window", "转办目标窗口号不能为空")
        source_window = normalize_window(window_id)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _must_case(conn, case_id, now)
            _must_unsealed(row)
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (target_window,)).fetchone()
            target_agent = normalize_name(to_agent) if to_agent else (win_row["agent"] if win_row else (row["owner_agent"] or agent))
            _upsert_window(
                conn,
                window_id=target_window,
                agent=target_agent,
                workspace=row["workspace"],
                activity=None,
                pid=pid if target_agent == agent else None,
                now=now,
            )
            note_text = _node_note(note or f"转办 → 交接给 {target_window}")
            # D-2：跨 agent 转办时，先释放源 owner 名下这条线程的投递认领，
            # 避免旧窗口仍被当成主办、新信继续继承僵尸认领（双归属）。
            source_owner_agent = row["owner_agent"]
            if source_owner_agent and source_owner_agent != target_agent:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET claimed_by = NULL
                    WHERE to_agent = ?
                      AND message_id IN (SELECT id FROM messages WHERE thread_id = ?)
                    """,
                    (source_owner_agent, row["thread_id"]),
                )
            conn.execute(
                """
                UPDATE cases
                SET owner_agent = ?, owner_window = ?, status = 'open',
                    waiting_for = NULL, updated_at = ?
                WHERE id = ?
                """,
                (target_agent, target_window, now, row["id"]),
            )
            _propagate_thread_claim(conn, row["thread_id"], target_agent, target_window, now, overwrite=True)
            _insert_node(
                conn,
                case_id=row["id"],
                agent=agent,
                window_id=source_window or row["owner_window"] or target_window,
                action="transfer",
                note=note_text,
                status="open",
                auto=False,
                now=now,
            )
            updated = _find_case(conn, row["id"])
            res = _case_view(conn, updated)
        self._signal()
        return res

    def pause_case(
        self,
        case_id: str,
        agent: str = "human",
        window_id: str | None = None,
        note: str | None = None,
        interrupted: bool = False,
        actor: str | None = None,
    ) -> dict:
        agent = normalize_name(actor or agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        default_note = "暂停（当前轮被中断）" if interrupted else "暂停"
        note_text = _node_note(note or default_note)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, case_id)
            if row is None:
                # 允许直接对一条既有线程「立案并暂停」；否则 404。
                row = _ensure_case_from_thread(conn, case_id, agent, now)
            else:
                row = _lazy_reconcile_case(conn, row, now)
            _must_unsealed(row)
            res = self._transition_case_in_tx(
                conn, row, agent, window_id, "pause", note_text, "paused", seal=False, auto=False, now=now
            )
        self._signal()
        return res

    def resume_case(
        self,
        case_id: str,
        agent: str = "human",
        window_id: str | None = None,
        note: str | None = None,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        note_text = _node_note(note or "恢复在办")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _must_case(conn, case_id, now)
            _must_unsealed(row)
            res = self._transition_case_in_tx(
                conn, row, agent, window_id, "resume", note_text, "open", seal=False, auto=False, now=now
            )
        self._signal()
        return res

    def abort_case(
        self,
        case_id: str,
        agent: str = "human",
        window_id: str | None = None,
        note: str | None = None,
        auto: bool = False,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        note_text = _node_note(note or "中止（封卷）")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _must_case(conn, case_id, now)
            _must_unsealed(row)
            res = self._transition_case_in_tx(
                conn, row, agent, window_id, "abort", note_text, "aborted", seal=True, auto=auto, now=now
            )
        self._signal()
        return res

    def close_case(
        self,
        case_id: str,
        agent: str = "human",
        window_id: str | None = None,
        note: str | None = None,
        auto: bool = False,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        note_text = _node_note(note or "办结（结案封档）")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _must_case(conn, case_id, now)
            _must_unsealed(row)
            res = self._transition_case_in_tx(
                conn, row, agent, window_id, "close", note_text, "done", seal=True, auto=auto, now=now
            )
        self._signal()
        return res

    def reopen_case(
        self,
        case_id: str,
        agent: str = "human",
        window_id: str | None = None,
        note: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent or "human")
        case_id = normalize_case_id(case_id)
        window_id = normalize_window(window_id)
        note_text = _node_note(note or "重开案卷")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, case_id)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{case_id}", 404)
            new_status = "open"
            win_row = (
                conn.execute("SELECT agent FROM windows WHERE window_id = ?", (window_id,)).fetchone()
                if window_id
                else None
            )
            new_owner_agent = (win_row["agent"] if win_row else agent) if window_id else (row["owner_agent"] or agent)
            if window_id:
                _upsert_window(
                    conn,
                    window_id=window_id,
                    agent=new_owner_agent,
                    workspace=row["workspace"],
                    activity=None,
                    pid=pid if new_owner_agent == agent else None,
                    now=now,
                )
            conn.execute(
                """
                UPDATE cases
                SET status = ?, owner_agent = ?, owner_window = ?, sealed = 0,
                    waiting_for = NULL, updated_at = ?, closed_at = NULL
                WHERE id = ?
                """,
                (new_status, new_owner_agent, window_id, now, row["id"]),
            )
            if window_id:
                _propagate_thread_claim(conn, row["thread_id"], new_owner_agent, window_id, now, overwrite=True)
            _insert_node(
                conn,
                case_id=row["id"],
                agent=agent,
                window_id=window_id,
                action="reopen",
                note=note_text,
                status=new_status,
                auto=False,
                now=now,
            )
            updated = _find_case(conn, row["id"])
            res = _case_view(conn, updated)
        self._signal()
        return res

    def _transition_case_in_tx(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        agent: str,
        window_id: str | None,
        action: str,
        note: str,
        new_status: str,
        seal: bool,
        auto: bool,
        now: str,
    ) -> dict:
        node_window = window_id or row["owner_window"]
        if seal:
            # 封卷（中止 / 办结）：释放主办窗口占用，标记只读
            conn.execute(
                """
                UPDATE cases
                SET status = ?, sealed = 1, owner_window = NULL,
                    updated_at = ?, closed_at = ?
                WHERE id = ?
                """,
                (new_status, now, now, row["id"]),
            )
            conn.execute(
                """
                UPDATE deliveries
                SET claimed_by = NULL
                WHERE message_id IN (SELECT id FROM messages WHERE thread_id = ?)
                """,
                (row["thread_id"],),
            )
        else:
            owner_w = window_id or row["owner_window"]
            owner_a = (agent if window_id else None) or row["owner_agent"]
            conn.execute(
                """
                UPDATE cases
                SET status = ?, owner_agent = ?, owner_window = ?,
                    sealed = 0, waiting_for = NULL, updated_at = ?
                WHERE id = ?
                """,
                (new_status, owner_a, owner_w, now, row["id"]),
            )
        _insert_node(
            conn,
            case_id=row["id"],
            agent=agent,
            window_id=node_window,
            action=action,
            note=note,
            status=new_status,
            auto=auto,
            now=now,
        )
        updated = _find_case(conn, row["id"])
        return _case_view(conn, updated)

    # ── D1 办公室（windows）与项目（projects）──────────────────────────────

    def touch_window(
        self,
        window_id: str,
        agent: str,
        workspace: str | None = None,
        activity: str | None = None,
        state: str | None = None,
        pid: int | None = None,
    ) -> dict:
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("window_required", "需要提供窗口号 window_id")
        agent_name = normalize_name(agent)
        if state is not None and not isinstance(state, str):
            raise MailError("invalid_state", "窗口 state 必须是字符串")
        if state in {"left", "offline", "aborted", "stopped", "exited"}:
            return self.leave_window(wid, agent=agent_name)
        if state == "paused":
            return self.pause_window(wid, agent=agent_name)
        if state == "active":
            now = utcnow()
            with self._session(immediate=True) as conn:
                _upsert(conn, agent_name, None)
                _upsert_window(
                    conn,
                    window_id=wid,
                    agent=agent_name,
                    workspace=_workspace(workspace) if workspace is not None else None,
                    activity=_activity(activity) if activity is not None else None,
                    pid=pid,
                    now=now,
                    state="active",
                    revive=True,
                )
                row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
                return _window_view(conn, row)
        if state is not None:
            raise MailError("invalid_state", "窗口 state 只能是 active、paused 或 left")
        self.touch(agent_name, activity=activity, window_id=wid, workspace=workspace, pid=pid)
        return self.get_window(wid)

    def get_window(self, window_id: str) -> dict:
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("not_found", "缺少窗口号", 404)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _lazy_reconcile_all(conn, now)
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            if row is None:
                raise MailError("not_found", f"没有找到窗口：{wid}", 404)
            return _window_view(conn, row)

    def list_windows(
        self,
        agent: str | None = None,
        workspace: str | None = None,
        state: str | None = None,
    ) -> list[dict]:
        agent_filter = normalize_name(agent) if agent else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            _lazy_reconcile_all(conn, now)
            rows = conn.execute(
                "SELECT * FROM windows ORDER BY last_seen_at DESC, window_id ASC"
            ).fetchall()
            out = []
            for row in rows:
                if agent_filter and row["agent"] != agent_filter:
                    continue
                if workspace is not None and not _workspace_scope(row["workspace"], workspace):
                    continue
                if state and row["state"] != state:
                    continue
                out.append(_window_view(conn, row))
        return out

    def leave_window(
        self,
        window_id: str,
        agent: str | None = None,
        note: str | None = None,
        auto: bool = True,
        actor: str | None = None,
    ) -> dict:
        """办公室正常退出上报：窗口标为 left，名下所有未封卷案卷自动中止并封卷（标 auto）。"""
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("window_required", "需要提供窗口号 window_id")
        acting = actor or agent
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            agent_name = normalize_name(acting) if acting else (row["agent"] if row else "human")
            if row is None:
                conn.execute(
                    """
                    INSERT INTO windows(
                        window_id, agent, workspace, state, activity, activity_at,
                        pid, first_seen_at, last_seen_at, left_at
                    ) VALUES (?, ?, NULL, 'left', '', '', NULL, ?, ?, ?)
                    """,
                    (wid, agent_name, now, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE windows
                    SET state = 'left', last_seen_at = ?, left_at = COALESCE(left_at, ?)
                    WHERE window_id = ?
                    """,
                    (now, now, wid),
                )
            aborted_ids = _abort_window_cases(
                conn,
                window_id=wid,
                agent=agent_name,
                note=_node_note(note or "办公室离场 → 自动中止（封卷）"),
                auto=auto,
                now=now,
            )
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            view = _window_view(conn, win_row)
            view["aborted_cases"] = aborted_ids
        self._signal()
        return view

    def pause_window(
        self,
        window_id: str,
        agent: str | None = None,
        note: str | None = None,
    ) -> dict:
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("window_required", "需要提供窗口号 window_id")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            if row is None:
                raise MailError("not_found", f"没有找到窗口：{wid}", 404)
            actor = normalize_name(agent) if agent else row["agent"]
            conn.execute(
                "UPDATE windows SET state = 'paused', last_seen_at = ? WHERE window_id = ?",
                (now, wid),
            )
            open_cases = conn.execute(
                "SELECT * FROM cases WHERE owner_window = ? AND sealed = 0 AND status != 'paused'",
                (wid,),
            ).fetchall()
            note_text = _node_note(note or "暂停（当前轮被中断）")
            for c_row in open_cases:
                self._transition_case_in_tx(
                    conn, c_row, actor, wid, "pause", note_text, "paused", seal=False, auto=False, now=now
                )
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            view = _window_view(conn, win_row)
        self._signal()
        return view

    def resume_window(
        self,
        window_id: str,
        agent: str | None = None,
        note: str | None = None,
    ) -> dict:
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("window_required", "需要提供窗口号 window_id")
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            if row is None:
                raise MailError("not_found", f"没有找到窗口：{wid}", 404)
            actor = normalize_name(agent) if agent else row["agent"]
            next_pid = (
                None
                if (row["state"] in {"left", "offline"} or (row["pid"] is not None and _pid_dead(row["pid"])))
                else row["pid"]
            )
            conn.execute(
                "UPDATE windows SET state = 'active', left_at = NULL, pid = ?, last_seen_at = ? WHERE window_id = ?",
                (next_pid, now, wid),
            )
            paused_cases = conn.execute(
                "SELECT * FROM cases WHERE owner_window = ? AND sealed = 0 AND status = 'paused'",
                (wid,),
            ).fetchall()
            note_text = _node_note(note or "恢复在办")
            for c_row in paused_cases:
                self._transition_case_in_tx(
                    conn, c_row, actor, wid, "resume", note_text, "open", seal=False, auto=False, now=now
                )
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            view = _window_view(conn, win_row)
        self._signal()
        return view

    def add_project(self, workspace: str, name: str | None = None, archived: bool | None = None) -> dict:
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
        now = utcnow()
        with self._session(immediate=True) as conn:
            _upsert_project(conn, ws, proj_name, now, archived=archived)
            projects = _collect_projects(conn)
        for item in projects:
            if item["workspace"] == ws:
                return item
        return {
            "workspace": ws,
            "name": proj_name,
            "case_count": 0,
            "total_cases": 0,
            "active_cases": 0,
            "doing_cases": 0,
            "paused_cases": 0,
            "closed_cases": 0,
            "active_windows": 0,
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
        n = _limit(limit)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.thread_id, m.from_agent, m.subject, m.created_at, m.workspace,
                       d.status AS my_status
                FROM messages m
                LEFT JOIN deliveries d ON d.message_id = m.id AND d.to_agent = ?
                WHERE m.from_agent = ?
                   OR EXISTS (SELECT 1 FROM deliveries d2 WHERE d2.message_id = m.id AND d2.to_agent = ?)
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT ?
                """,
                (name, name, name, n),
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
                sent = row["from_agent"] == name
                out.append(
                    {
                        "id": row["id"],
                        "thread_id": row["thread_id"],
                        "from": row["from_agent"],
                        "to": recips,
                        "subject": row["subject"],
                        "workspace": row["workspace"] or "",
                        "direction": "out" if sent else "in",
                        "other": (", ".join(recips) if sent else row["from_agent"]),
                        "my_status": row["my_status"] if not sent else None,
                        "created_at": row["created_at"],
                        "created_at_local": local_time(row["created_at"]),
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
                "created_at_local": local_time(row["created_at"]),
            }
            for row in rows
        ]


# ── 内部辅助函数 ──────────────────────────────────────────────────────────


def _pid_dead(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return True
    try:
        os.kill(pid_int, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True


def _pid_value(pid) -> int | None:
    """pid 归一化：None 透传，其余必须是正整数（或可转正整数的字符串），否则 400。"""
    if pid is None:
        return None
    if isinstance(pid, bool) or not isinstance(pid, (int, str)):
        raise MailError("invalid_pid", "pid 必须是整数")
    try:
        val = int(pid)
    except (TypeError, ValueError):
        raise MailError("invalid_pid", "pid 必须是整数") from None
    if val <= 0:
        raise MailError("invalid_pid", "pid 必须是正整数")
    return val


def _is_window_gone(win_row: sqlite3.Row | None) -> bool:
    if win_row is None:
        return True
    if win_row["state"] in {"left", "offline", "crashed", "dead", "exited", "aborted", "stopped"}:
        return True
    if win_row["left_at"]:
        return True
    if win_row["pid"] is not None and _pid_dead(win_row["pid"]):
        return True
    return False


def _lazy_reconcile_case(conn: sqlite3.Connection, case_row: sqlite3.Row, now: str | None = None) -> sqlite3.Row:
    """崩溃惰性补齐：当未封卷案卷的主办窗口已不在（进程死/窗口被删/状态已离场）时，
    自动补写 `[自动]` 节点并中止封卷。"""
    if case_row["sealed"] or case_row["status"] in SEALED_STATUSES:
        return case_row
    owner_window = case_row["owner_window"]
    if not owner_window:
        return case_row
    win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (owner_window,)).fetchone()
    if not _is_window_gone(win_row):
        return case_row
    now = now or utcnow()
    if win_row is not None and win_row["state"] != "left":
        conn.execute(
            "UPDATE windows SET state = 'left', left_at = COALESCE(left_at, ?), last_seen_at = ? WHERE window_id = ?",
            (now, now, owner_window),
        )
    actor = case_row["owner_agent"] or (win_row["agent"] if win_row else "system")
    conn.execute(
        """
        UPDATE cases
        SET status = 'aborted', sealed = 1, owner_window = NULL,
            updated_at = ?, closed_at = COALESCE(closed_at, ?)
        WHERE id = ?
        """,
        (now, now, case_row["id"]),
    )
    # D-2：封卷时释放这条线程上**所有**窗口的认领（不再只按 owner_window 匹配），
    # 以便跨 agent 转办后遗留的源侧僵尸认领也能被清掉。
    conn.execute(
        """
        UPDATE deliveries
        SET claimed_by = NULL
        WHERE message_id IN (SELECT id FROM messages WHERE thread_id = ?)
        """,
        (case_row["thread_id"],),
    )
    _insert_node(
        conn,
        case_id=case_row["id"],
        agent=actor,
        window_id=owner_window,
        action="abort",
        note="主办离场（惰性补齐） → 中止（封卷）",
        status="aborted",
        auto=True,
        now=now,
    )
    return conn.execute("SELECT * FROM cases WHERE id = ?", (case_row["id"],)).fetchone()


def _lazy_reconcile_all(conn: sqlite3.Connection, now: str | None = None) -> None:
    now = now or utcnow()
    open_cases = conn.execute(
        "SELECT * FROM cases WHERE sealed = 0 AND owner_window IS NOT NULL"
    ).fetchall()
    for c_row in open_cases:
        _lazy_reconcile_case(conn, c_row, now)
    active_wins = conn.execute(
        "SELECT * FROM windows WHERE state != 'left' AND pid IS NOT NULL"
    ).fetchall()
    for w_row in active_wins:
        if _pid_dead(w_row["pid"]):
            conn.execute(
                "UPDATE windows SET state = 'left', left_at = COALESCE(left_at, ?) WHERE window_id = ?",
                (now, w_row["window_id"]),
            )


def _abort_window_cases(
    conn: sqlite3.Connection,
    window_id: str,
    agent: str,
    note: str,
    auto: bool,
    now: str,
) -> list[str]:
    open_cases = conn.execute(
        "SELECT * FROM cases WHERE owner_window = ? AND sealed = 0",
        (window_id,),
    ).fetchall()
    aborted: list[str] = []
    for c_row in open_cases:
        conn.execute(
            """
            UPDATE cases
            SET status = 'aborted', sealed = 1, owner_window = NULL,
                updated_at = ?, closed_at = COALESCE(closed_at, ?)
            WHERE id = ?
            """,
            (now, now, c_row["id"]),
        )
        conn.execute(
            """
            UPDATE deliveries
            SET claimed_by = NULL
            WHERE message_id IN (SELECT id FROM messages WHERE thread_id = ?)
              AND claimed_by = ?
            """,
            (c_row["thread_id"], window_id),
        )
        _insert_node(
            conn,
            case_id=c_row["id"],
            agent=c_row["owner_agent"] or agent,
            window_id=window_id,
            action="abort",
            note=note,
            status="aborted",
            auto=auto,
            now=now,
        )
        aborted.append(c_row["id"])
    return aborted


def _find_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cases WHERE id = ? OR thread_id = ?",
        (case_id, case_id),
    ).fetchone()


def _must_case(conn: sqlite3.Connection, case_id: str, now: str | None = None) -> sqlite3.Row:
    row = _find_case(conn, case_id)
    if row is None:
        raise MailError("not_found", f"没有找到案卷：{case_id}", 404)
    return _lazy_reconcile_case(conn, row, now)


def _ensure_case_from_thread(
    conn: sqlite3.Connection,
    case_id: str,
    agent: str,
    now: str,
) -> sqlite3.Row:
    """案卷不存在、但 case_id 指向一条既有线程时，按线程补建一份 pending 案卷。

    供 pause/resume 这类「直接作用于线程」的管理动作使用；线程不存在则 404。"""
    msg = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? OR id = ? ORDER BY created_at ASC, id ASC LIMIT 1",
        (case_id, case_id),
    ).fetchone()
    if msg is None:
        raise MailError("not_found", f"没有找到案卷或线程：{case_id}", 404)
    thread_id = msg["thread_id"]
    root = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC, id ASC LIMIT 1",
        (thread_id,),
    ).fetchone()
    source = root or msg
    workspace = source["workspace"]
    if workspace:
        _upsert_project(conn, workspace, None, now)
    conn.execute(
        """
        INSERT INTO cases(
            id, thread_id, title, workspace, status, owner_agent, owner_window,
            waiting_for, sealed, created_at, updated_at, closed_at
        ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL, 0, ?, ?, NULL)
        """,
        (thread_id, thread_id, source["subject"], workspace, now, now),
    )
    _insert_node(
        conn,
        case_id=thread_id,
        agent=agent,
        window_id=None,
        action="create",
        note="立案",
        status="pending",
        auto=False,
        now=now,
    )
    return conn.execute("SELECT * FROM cases WHERE id = ?", (thread_id,)).fetchone()


def _must_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"] or row["status"] in SEALED_STATUSES:
        raise MailError("case_sealed", f"案卷 {row['id']} 已封卷（{row['status']}），只读不可修改", 409)


def _is_thread_sealed(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = _find_case(conn, thread_id)
    if row is None:
        return False
    row = _lazy_reconcile_case(conn, row)
    return bool(row["sealed"]) or row["status"] in SEALED_STATUSES


def _is_thread_paused_or_sealed(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = _find_case(conn, thread_id)
    if row is None:
        return False
    row = _lazy_reconcile_case(conn, row)
    return bool(row["sealed"]) or row["status"] in (SEALED_STATUSES | {"paused"})


def _effective_thread_claim(
    conn: sqlite3.Connection,
    thread_id: str,
    agent: str,
    direct_claim: str | None = None,
) -> str | None:
    if direct_claim:
        return direct_claim
    row = conn.execute(
        """
        SELECT d.claimed_by
        FROM deliveries d
        JOIN messages m ON m.id = d.message_id
        WHERE m.thread_id = ? AND d.to_agent = ? AND d.claimed_by IS NOT NULL
        ORDER BY d.rowid DESC
        LIMIT 1
        """,
        (thread_id, agent),
    ).fetchone()
    if row and row["claimed_by"]:
        return row["claimed_by"]
    case_row = _find_case(conn, thread_id)
    if case_row and not case_row["sealed"] and case_row["owner_window"] and case_row["owner_agent"] == agent:
        return case_row["owner_window"]
    return None


def _propagate_thread_claim(
    conn: sqlite3.Connection,
    thread_id: str,
    agent: str,
    window_id: str,
    now: str,
    overwrite: bool = False,
) -> None:
    if overwrite:
        conn.execute(
            """
            UPDATE deliveries
            SET claimed_by = ?, claimed_at = ?
            WHERE to_agent = ?
              AND message_id IN (SELECT id FROM messages WHERE thread_id = ?)
            """,
            (window_id, now, agent, thread_id),
        )
    else:
        conn.execute(
            """
            UPDATE deliveries
            SET claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
            WHERE to_agent = ?
              AND (claimed_by IS NULL OR claimed_by = ?)
              AND message_id IN (SELECT id FROM messages WHERE thread_id = ?)
            """,
            (window_id, now, agent, window_id, thread_id),
        )


def _claim_thread_on_touch(
    conn: sqlite3.Connection,
    agent: str,
    window_id: str,
    message_row: sqlite3.Row,
    force: bool,
    mark_read: bool,
    now: str,
) -> None:
    thread_id = message_row["thread_id"]
    msg_id = message_row["id"]
    if not _is_thread_party(conn, thread_id, agent):
        raise MailError("not_found", f"没有找到案卷或线程：{thread_id}", 404)
    case_row = _find_case(conn, thread_id)
    if case_row is not None:
        case_row = _lazy_reconcile_case(conn, case_row, now)
        _must_unsealed(case_row)

    delivery = conn.execute(
        "SELECT status, read_at, claimed_by, claimed_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
        (msg_id, agent),
    ).fetchone()
    direct_claim = delivery["claimed_by"] if delivery else None
    existing_claim = _effective_thread_claim(conn, thread_id, agent, direct_claim)
    if existing_claim is None and case_row is not None and case_row["owner_window"] and case_row["owner_agent"] == agent:
        existing_claim = case_row["owner_window"]

    if existing_claim is not None and existing_claim != window_id and not force:
        raise MailError("already_claimed", f"这条线已被 {existing_claim} 主办，如需接手请使用 force=True", 409)

    if delivery is not None:
        read_at = (delivery["read_at"] or now) if mark_read else delivery["read_at"]
        new_status = "read" if mark_read else delivery["status"]
        if force and existing_claim is not None and existing_claim != window_id:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, read_at = ?, claimed_by = ?, claimed_at = ?
                WHERE message_id = ? AND to_agent = ?
                """,
                (new_status, read_at, window_id, now, msg_id, agent),
            )
        else:
            cur = conn.execute(
                """
                UPDATE deliveries
                SET status = ?, read_at = ?, claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
                WHERE message_id = ? AND to_agent = ?
                  AND (claimed_by IS NULL OR claimed_by = ?)
                """,
                (new_status, read_at, window_id, now, msg_id, agent, window_id),
            )
            if cur.rowcount == 0:
                raise MailError("already_claimed", "并发认领冲突：这封信已被其他窗口抢先认领", 409)

    _propagate_thread_claim(
        conn,
        thread_id,
        agent,
        window_id,
        now,
        overwrite=bool(force and existing_claim and existing_claim != window_id),
    )

    root_msg = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC, id ASC LIMIT 1",
        (thread_id,),
    ).fetchone() or message_row
    title = root_msg["subject"]
    workspace = root_msg["workspace"] or message_row["workspace"]
    if workspace:
        _upsert_project(conn, workspace, None, now)

    if case_row is None:
        conn.execute(
            """
            INSERT INTO cases(
                id, thread_id, title, workspace, status, owner_agent, owner_window,
                waiting_for, sealed, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, 0, ?, ?, NULL)
            """,
            (thread_id, thread_id, title, workspace, agent, window_id, now, now),
        )
        note_text = f"强制接手（原主办 {existing_claim}） → 在办" if (force and existing_claim and existing_claim != window_id) else "认领 → 在办"
        action_text = "takeover" if (force and existing_claim and existing_claim != window_id) else "claim"
        _insert_node(
            conn,
            case_id=thread_id,
            agent=agent,
            window_id=window_id,
            action=action_text,
            note=note_text,
            status="open",
            auto=False,
            now=now,
        )
    else:
        prev_owner = case_row["owner_window"]
        if force and prev_owner and prev_owner != window_id:
            conn.execute(
                """
                UPDATE cases
                SET owner_agent = ?, owner_window = ?, status = 'open',
                    waiting_for = NULL, updated_at = ?
                WHERE id = ?
                """,
                (agent, window_id, now, case_row["id"]),
            )
            _insert_node(
                conn,
                case_id=case_row["id"],
                agent=agent,
                window_id=window_id,
                action="takeover",
                note=f"强制接手（原主办 {prev_owner}） → 在办",
                status="open",
                auto=False,
                now=now,
            )
        elif prev_owner is None:
            conn.execute(
                """
                UPDATE cases
                SET owner_agent = ?, owner_window = ?, status = 'open',
                    waiting_for = NULL, updated_at = ?
                WHERE id = ?
                """,
                (agent, window_id, now, case_row["id"]),
            )
            _insert_node(
                conn,
                case_id=case_row["id"],
                agent=agent,
                window_id=window_id,
                action="claim",
                note="认领 → 在办",
                status="open",
                auto=False,
                now=now,
            )


def _upsert_window(
    conn: sqlite3.Connection,
    window_id: str,
    agent: str,
    workspace: str | None,
    activity: str | None,
    pid: int | None,
    now: str,
    state: str | None = None,
    revive: bool = False,
) -> None:
    row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO windows(
                window_id, agent, workspace, state, activity, activity_at,
                pid, first_seen_at, last_seen_at, left_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                window_id,
                agent,
                workspace,
                state or "active",
                activity or "",
                now if activity else "",
                _pid_value(pid),
                now,
                now,
            ),
        )
        return
    # D-8：窗口与 agent 首次绑定后不再改写归属。这里保留既有 agent，
    # 从源头堵住「凭 X-Window 冒用他人窗口号改写 windows.agent」；
    # HTTP 边界另用 _guard_window_agent 对异 agent 的 X-Window 直接 409。
    # 窗口状态规则（D-4 + 离场不可被软触达复活）：
    # - 显式 state 一律照办；
    # - 只有「本人到场」的 touch / touch_window（revive=True）可把 left 复活为 active；
    #   events/inbox/mark/reply 等软触达（revive=False）不得复活已离场窗口，
    #   否则残留的长轮询会在离场后把它重新点亮。
    if state is not None:
        next_state = state
    elif revive and row["state"] in {"left", "offline"}:
        next_state = "active"
    else:
        next_state = row["state"]
    next_left_at = None if next_state != "left" else row["left_at"]
    next_workspace = workspace if workspace is not None else row["workspace"]
    next_activity = activity if activity is not None else row["activity"]
    next_activity_at = now if activity is not None else row["activity_at"]
    # D-4：离场（left/offline）窗口复活为 active 时，旧 pid 多半已死，置 NULL，
    # 除非本次显式带了新 pid，否则会让后续对账把复活窗口又判死、误自动封卷。
    reviving = row["state"] in {"left", "offline"} and next_state == "active"
    if pid is not None:
        next_pid = _pid_value(pid)
    elif reviving:
        next_pid = None
    else:
        next_pid = row["pid"]
    conn.execute(
        """
        UPDATE windows
        SET workspace = ?, state = ?, activity = ?, activity_at = ?,
            pid = ?, last_seen_at = ?, left_at = ?
        WHERE window_id = ?
        """,
        (
            next_workspace,
            next_state,
            next_activity,
            next_activity_at,
            next_pid,
            now,
            next_left_at,
            window_id,
        ),
    )


def _upsert_project(conn: sqlite3.Connection, workspace: str, name: str | None, now: str, archived: bool | None = None) -> None:
    ws = _workspace(workspace)
    if not ws:
        return
    proj_name = (name or "").strip() or Path(ws).name or ws
    arc = None if archived is None else (1 if archived else 0)
    existing = conn.execute("SELECT * FROM projects WHERE workspace = ?", (ws,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO projects(workspace, name, created_at, updated_at, archived) VALUES (?, ?, ?, ?, ?)",
            (ws, proj_name, now, now, arc or 0),
        )
    else:
        final_name = proj_name if name else (existing["name"] or proj_name)
        if arc is None:
            conn.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE workspace = ?",
                (final_name, now, ws),
            )
        else:
            conn.execute(
                "UPDATE projects SET name = ?, updated_at = ?, archived = ? WHERE workspace = ?",
                (final_name, now, arc, ws),
            )


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
        meta = raw_rows.get(ws) or raw_rows.get(key)
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
            "case_count": total,
            "total_cases": total,
            "cases_total": total,
            "active_cases": doing,
            "doing_cases": doing,
            "cases_active": doing,
            "paused_cases": paused,
            "closed_cases": closed,
            "active_windows": active_wins,
            "window_count": active_wins,
            "counts": {
                "open": open_cnt,
                "doing": open_cnt,
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


def _insert_node(
    conn: sqlite3.Connection,
    case_id: str,
    agent: str,
    window_id: str | None,
    action: str,
    note: str,
    status: str,
    auto: bool,
    now: str,
) -> None:
    final_note = f"[自动] {note}" if (auto and not note.startswith("[自动]")) else note
    conn.execute(
        """
        INSERT INTO case_nodes(case_id, agent, window_id, action, note, status, auto, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, agent, window_id, action, final_note, status, 1 if auto else 0, now),
    )


def _node_view(row: sqlite3.Row) -> dict:
    status = row["status"]
    status_label = CASE_STATUS_LABELS.get(status, status)
    auto = bool(row["auto"])
    actor = "系统" if auto and row["agent"] == "system" else row["agent"]
    if row["window_id"] and not auto:
        actor_display = f"{actor}({row['window_id']})"
    elif row["window_id"] and auto:
        actor_display = f"{actor}({row['window_id']})"
    else:
        actor_display = actor
    hhmm = local_hhmm(row["created_at"])
    note = row["note"]
    if "→" in note:
        line = f"{hhmm} {actor_display} · {note}"
    else:
        line = f"{hhmm} {actor_display} · {note} → {status_label}"
    if auto:
        line += " [自动]"
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "agent": row["agent"],
        "window_id": row["window_id"],
        "action": row["action"],
        "note": note,
        "summary": note,
        "status": status,
        "status_label": status_label,
        "auto": auto,
        "created_at": row["created_at"],
        "created_at_local": local_time(row["created_at"]),
        "line": line,
    }


def _case_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    node_rows = conn.execute(
        "SELECT * FROM case_nodes WHERE case_id = ? ORDER BY id ASC",
        (row["id"],),
    ).fetchall()
    nodes = [_node_view(n) for n in node_rows]
    status = row["status"]
    status_label = CASE_STATUS_LABELS.get(status, status)
    if status == "waiting" and row["waiting_for"]:
        status_display = f"待 {row['waiting_for']}"
    else:
        status_display = status_label
    sealed = bool(row["sealed"]) or status in SEALED_STATUSES
    header_line = f"状态：{status_display}" + ("（已封卷）" if sealed else "")
    lines = [header_line, *(n["line"] for n in nodes)]
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "title": row["title"],
        "subject": row["title"],
        "workspace": row["workspace"],
        "status": status,
        "status_label": status_display,
        "sealed": sealed,
        "owner_agent": row["owner_agent"],
        "owner": row["owner_agent"],
        "owner_window": row["owner_window"],
        "claimed_by": row["owner_window"],
        "waiting_for": row["waiting_for"],
        "created_at": row["created_at"],
        "created_at_local": local_time(row["created_at"]),
        "updated_at": row["updated_at"],
        "updated_at_local": local_time(row["updated_at"]),
        "closed_at": row["closed_at"],
        "node_count": len(nodes),
        "last_note": nodes[-1]["note"] if nodes else "",
        "last_node": nodes[-1] if nodes else None,
        "nodes": nodes,
        "lines": lines,
        "text": "\n".join(lines),
    }


def _pid_running(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _window_summary(row: sqlite3.Row) -> dict:
    state = row["state"]
    return {
        "window_id": row["window_id"],
        "id": row["window_id"],
        "agent": row["agent"],
        "workspace": row["workspace"],
        "state": state,
        "state_label": WINDOW_STATE_LABELS.get(state, state),
        "activity": row["activity"],
        "activity_at": row["activity_at"],
        "activity_at_local": local_time(row["activity_at"]) if row["activity_at"] else "",
        "pid": row["pid"],
        "pid_alive": _pid_running(row["pid"]),
        "first_seen_at": row["first_seen_at"],
        "first_seen": row["first_seen_at"],
        "first_seen_local": local_time(row["first_seen_at"]),
        "last_seen_at": row["last_seen_at"],
        "last_seen": row["last_seen_at"],
        "last_seen_local": local_time(row["last_seen_at"]),
        "left_at": row["left_at"],
    }


def _window_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    view = _window_summary(row)
    case_rows = conn.execute(
        """
        SELECT id, thread_id, title, workspace, status, sealed, updated_at
        FROM cases
        WHERE owner_window = ? AND sealed = 0
        ORDER BY updated_at DESC, id DESC
        """,
        (row["window_id"],),
    ).fetchall()
    view["cases"] = [
        {
            "id": c["id"],
            "thread_id": c["thread_id"],
            "title": c["title"],
            "workspace": c["workspace"],
            "status": c["status"],
            "status_label": CASE_STATUS_LABELS.get(c["status"], c["status"]),
            "updated_at": c["updated_at"],
        }
        for c in case_rows
    ]
    view["case_count"] = len(view["cases"])
    return view


def _workspace_scope(value: str | None, scope: str | None) -> bool:
    """workspace 是否属于项目 scope：全路径、解析后路径、或目录 basename（不区分大小写）都算命中。
    scope 为 None 不过滤；workspace 为空的信不属于任何项目范围。

    M3：basename 回退只在**有一侧是裸目录名**（不含路径分隔符）时启用，
    因此 `"proj"`（守护上报的裸名）能命中 `"/a/proj"`，
    但 `"work/proj"` 与 `"other/proj"`、`"/a/proj"` 与 `"/b/proj"` 不再互串。"""
    if scope is None:
        return True
    text = (value or "").strip()
    target = str(scope).strip()
    if not text or not target:
        return False
    if text == target:
        return True
    left = Path(text).expanduser()
    right = Path(target).expanduser()
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:
        pass
    # 退化为 basename 比较的条件收紧（M3）：
    # - 至少一侧是「裸名」（不含路径分隔符，守护/CLI 上报的常见形式）；或
    # - 两侧都是绝对路径（跨机联邦导出按同名目录对齐的既有约定，见 test_basename_scope_still_exports）。
    # 其余情况（如 `work/proj` 与 `other/proj` 两条相对路径）不再按同名串在一起。
    left_bare = bool(text) and left.name == text
    right_bare = bool(target) and right.name == target
    if not (left_bare or right_bare or (left.is_absolute() and right.is_absolute())):
        return False
    return bool(right.name) and left.name.casefold() == right.name.casefold()


def _upsert(conn: sqlite3.Connection, name: str, description: str | None) -> None:
    now = utcnow()
    if description is None:
        conn.execute(
            """
            INSERT INTO agents(name, description, created_at, last_seen_at)
            VALUES (?, '', ?, ?)
            ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (name, now, now),
        )
        return
    conn.execute(
        """
        INSERT INTO agents(name, description, created_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            description = excluded.description
        """,
        (name, description, now, now),
    )


def _unread_counts(conn: sqlite3.Connection, window_id: str | None = None) -> dict[str, int]:
    """M1-data：`ic agents` 未读角标与 `events.unread_total` 同口径——
    无 window_id 时只算「未被任何窗口认领」且线程未暂停/未封卷的信；
    有 window_id 时排除被同 agent 其他窗口认领或已暂停/已封卷的信（计划 §4.6）。"""
    rows = conn.execute(
        """
        SELECT d.to_agent AS to_agent, d.claimed_by AS claimed_by, m.thread_id AS thread_id
        FROM deliveries d
        JOIN messages m ON m.id = d.message_id
        WHERE d.status = 'unread'
        """
    ).fetchall()
    counts: dict[str, int] = {}
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
        counts[row["to_agent"]] = counts.get(row["to_agent"], 0) + 1
    return counts


def _must_visible(conn: sqlite3.Connection, message_id: str, agent: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None or not _is_party(conn, row, agent):
        raise MailError("not_found", "没有这封信，或者你不在其中", 404)
    return row


def _is_party(conn: sqlite3.Connection, row: sqlite3.Row, agent: str) -> bool:
    if row["from_agent"] == agent:
        return True
    found = conn.execute(
        "SELECT 1 FROM deliveries WHERE message_id = ? AND to_agent = ?",
        (row["id"], agent),
    ).fetchone()
    return found is not None


def _is_thread_party(conn: sqlite3.Connection, thread_id: str, agent: str) -> bool:
    """D-3：agent 是否是这条线程的当事人（任一回合的发信人或收件人）。"""
    for row in conn.execute("SELECT * FROM messages WHERE thread_id = ?", (thread_id,)):
        if _is_party(conn, row, agent):
            return True
    return False


def _thread_has_messages(conn: sqlite3.Connection, thread_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone() is not None


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
        "created_at_local": local_time(row["created_at"]),
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
        "activity_at_local": local_time(row["activity_at"]) if row["activity_at"] else "",
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "last_seen_local": local_time(row["last_seen_at"]),
        "active_window_count": len(active_wins),
    }
    if windows is not None:
        out["windows"] = windows
    return out


def _subject(value: str) -> str:
    if not isinstance(value, str):
        raise MailError("invalid_subject", "主题必须是字符串")
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        raise MailError("invalid_subject", "主题不能为空，不能换行，最长 200 字")
    return text


def _body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MailError("invalid_body", "正文不能为空")
    if len(value) > 100_000:
        raise MailError("invalid_body", "正文最长 100000 字", 413)
    return value


def _priority(value: str) -> str:
    if value is None:
        return "normal"
    if not isinstance(value, str):
        raise MailError("invalid_priority", "priority 只能是 normal 或 high")
    text = value.strip().lower() or "normal"
    if text not in {"normal", "high"}:
        raise MailError("invalid_priority", "priority 只能是 normal 或 high")
    return text


def _workspace(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError("invalid_workspace", "workspace 必须是字符串")
    text = value.strip()
    if not text:
        return None
    if len(text) > 1024:
        raise MailError("invalid_workspace", "workspace 最长 1024 字")
    return text


def _reply_to(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError("invalid_reply_to", "reply_to 必须是字符串")
    text = value.strip()
    return text or None


def _limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_limit", "limit 必须是整数") from None
    if number < 1 or number > 100:
        raise MailError("invalid_limit", "limit 必须在 1 到 100 之间")
    return number


def _case_status(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MailError("invalid_status", "案卷状态不能为空")
    text = value.strip()
    lower = text.lower()
    if lower in {"doing", "open", "active"}:
        return "open"
    if lower in CASE_STATUS_LABELS:
        return lower
    if text in _LABEL_TO_CASE_STATUS:
        return _LABEL_TO_CASE_STATUS[text]
    raise MailError(
        "invalid_status",
        "案卷状态只能是 open/doing/waiting/paused/aborted/done（或 待办/在办/待谁/暂停/中止/办结）",
    )


def _node_note(value: str) -> str:
    if not isinstance(value, str):
        raise MailError("invalid_node", "案卷节点内容必须是字符串")
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        raise MailError("invalid_node", "案卷节点必须是一行（不能换行），非空且最长 200 字")
    return text


def _node_action(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "note"
    text = value.strip().lower()
    if "\n" in text or "\r" in text or len(text) > 64:
        raise MailError("invalid_node", "action 必须是单行短标识")
    return text


_REPLY_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fw|fwd|回复|答复)\s*[:：]\s*)+", re.IGNORECASE)


def _re_subject(subject: str) -> str:
    """折叠叠加的 Re:/回复: 前缀，主题不再一轮比一轮长。"""
    core = _REPLY_PREFIX_RE.sub("", subject).strip() or subject.strip()
    return f"Re: {core}"[:200]


def _role(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailError("invalid_role", "role 必须是字符串")
    text = value.strip().lower()
    if text in {"", "member"}:
        return ""
    if text == "coordinator":
        return "coordinator"
    raise MailError("invalid_role", "role 只能是空（成员）或 coordinator")


def _activity(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailError("invalid_activity", "activity 必须是字符串")
    text = " ".join(value.split())
    if len(text) > 200:
        raise MailError("invalid_activity", "activity 太长了，200 字以内说清楚你在做什么")
    return text
