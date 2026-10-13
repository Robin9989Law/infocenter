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


def new_id() -> str:
    return f"m{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def normalize_name(value: str) -> str:
    name = (value or "").strip().lower()
    if name in BROADCAST or not NAME_RE.fullmatch(name) or ".." in name:
        raise MailError("invalid_name", "agent 名字需为 1–64 位字母、数字、_、- 或 .，且不能是 *、all、everyone")
    return name


def parse_recipients(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in BROADCAST:
            return [text.lower()]
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
        if tokens != ["*"] and not (len(tokens) == 1 and tokens[0] in BROADCAST):
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
    def _session(self, immediate: bool = False):
        conn = self._connect()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
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
                    PRIMARY KEY (message_id, to_agent)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_deliveries_inbox ON deliveries(to_agent, status);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
            if "role" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            if "activity" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN activity TEXT NOT NULL DEFAULT ''")
            if "activity_at" not in columns:
                conn.execute("ALTER TABLE agents ADD COLUMN activity_at TEXT NOT NULL DEFAULT ''")

    def seed(self) -> None:
        now = utcnow()
        with self._session() as conn:
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
    ) -> dict:
        name = normalize_name(name)
        role = _role(role) if role is not None else None
        activity = _activity(activity) if activity is not None else None
        now = utcnow()
        with self._session() as conn:
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
            row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
            unread = _unread_counts(conn).get(name, 0)
        return _agent(row, unread)

    def list_agents(self) -> list[dict]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY name"
            ).fetchall()
            unread = _unread_counts(conn)
        return [_agent(row, unread.get(row["name"], 0)) for row in rows]

    def remove_agent(self, name: str) -> dict:
        name = normalize_name(name)
        with self._session() as conn:
            removed = conn.execute("DELETE FROM agents WHERE name = ?", (name,)).rowcount > 0
        return {"name": name, "removed": removed}

    def events(self, agent: str, since=0, wait=0.0, limit: int = 10) -> dict:
        """等新信。since 是上次返回的 deliveries rowid 游标；wait 秒内没有新信就返回现状。"""
        agent = normalize_name(agent)
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
        with self._cond:
            while True:
                snapshot = self._events_snapshot(agent, since, limit)
                if snapshot["new"] or wait <= 0:
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
                self._cond.wait(remaining)

    def _events_snapshot(self, agent: str, since: int, limit: int) -> dict:
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT m.*, d.rowid AS delivery_seq, d.status AS delivery_status,
                       d.read_at AS delivery_read_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent = ? AND d.status = 'unread'
                ORDER BY d.rowid ASC
                """,
                (agent,),
            ).fetchall()
            cursor = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM deliveries").fetchone()[0]
            fresh = [row for row in rows if row["delivery_seq"] > since]
            messages = [
                _view(conn, row, agent, False, row["delivery_status"], row["delivery_read_at"])
                for row in fresh[-limit:]
            ]
        return {"agent": agent, "cursor": cursor, "unread_total": len(rows), "new": messages}

    def _signal(self) -> None:
        with self._cond:
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
    ) -> dict:
        sender = normalize_name(sender)
        subject = _subject(subject if subject_override is None else subject_override)
        body = _body(body)
        priority = _priority(priority)
        workspace = _workspace(workspace)
        recipients_in = parse_recipients(to)
        with self._session() as conn:
            _upsert(conn, sender, None)
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
            created = utcnow()
            conn.execute(
                """
                INSERT INTO messages(
                    id, thread_id, from_agent, subject, body, priority,
                    workspace, reply_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    thread_id or message_id,
                    sender,
                    subject,
                    body,
                    priority,
                    workspace,
                    parent_id,
                    created,
                ),
            )
            conn.executemany(
                """
                INSERT INTO deliveries(message_id, to_agent, status, read_at)
                VALUES (?, ?, 'unread', NULL)
                """,
                [(message_id, name) for name in recipients],
            )
        self._signal()
        return self.get_message(sender, message_id, full=True)

    def reply(self, sender: str, message_id: str, body: str, subject: str | None = None, to=None) -> dict:
        sender = normalize_name(sender)
        with self._session() as conn:
            parent = _must_visible(conn, message_id, sender)
            parent_view = _view(conn, parent, sender, full=False)
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
        )

    def inbox(
        self,
        agent: str,
        status: str = "unread",
        limit: int = 20,
        full: bool = False,
        workspace: str | None = None,
    ) -> dict:
        agent = normalize_name(agent)
        if status not in {"unread", "read", "archived", "all"}:
            raise MailError("invalid_status", "status 只能是 unread、read、archived 或 all")
        limit = _limit(limit)
        workspace = _workspace(workspace) if workspace else None
        with self._session() as conn:
            _upsert(conn, agent, None)
            rows = conn.execute(
                """
                SELECT m.*, d.status AS delivery_status, d.read_at AS delivery_read_at
                FROM deliveries d
                JOIN messages m ON m.id = d.message_id
                WHERE d.to_agent = ?
                  AND (? = 'all' OR d.status = ?)
                  AND (? IS NULL OR m.workspace = ?)
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT ?
                """,
                (agent, status, status, workspace, workspace, limit),
            ).fetchall()
            messages = [_view(conn, row, agent, full, row["delivery_status"], row["delivery_read_at"]) for row in rows]
        return {"agent": agent, "status": status, "count": len(messages), "messages": messages}

    def get_message(self, agent: str, message_id: str, full: bool = True) -> dict:
        agent = normalize_name(agent)
        with self._session() as conn:
            row = _must_visible(conn, message_id, agent)
            delivery = conn.execute(
                "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (row["id"], agent),
            ).fetchone()
            status = delivery["status"] if delivery else None
            read_at = delivery["read_at"] if delivery else None
            return _view(conn, row, agent, full, status, read_at)

    def mark(self, agent: str, message_id: str, status: str) -> dict:
        agent = normalize_name(agent)
        if status not in {"read", "archived"}:
            raise MailError("invalid_status", "只能标成 read 或 archived")
        with self._session() as conn:
            _must_visible(conn, message_id, agent)
            delivery = conn.execute(
                "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
            if delivery is None:
                raise MailError("not_a_recipient", "这封信不是留给你的，不能替别人签收", 409)
            read_at = delivery["read_at"] or utcnow()
            conn.execute(
                "UPDATE deliveries SET status = ?, read_at = ? WHERE message_id = ? AND to_agent = ?",
                (status, read_at, message_id, agent),
            )
        return self.get_message(agent, message_id, full=True)

    def thread(self, agent: str, thread_id: str) -> dict:
        agent = normalize_name(agent)
        with self._session() as conn:
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
                    "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
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
                    )
                )
        return {"thread_id": thread_id, "count": len(messages), "messages": messages}


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


def _unread_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT to_agent, COUNT(*) AS n FROM deliveries WHERE status = 'unread' GROUP BY to_agent"
    ).fetchall()
    return {row["to_agent"]: row["n"] for row in rows}


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


def _recipients(conn: sqlite3.Connection, message_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT to_agent FROM deliveries WHERE message_id = ? ORDER BY to_agent",
        (message_id,),
    ).fetchall()
    return [row["to_agent"] for row in rows]


def _view(conn, row, agent: str, full: bool, delivery_status=None, read_at=None) -> dict:
    body = row["body"]
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
    }
    if full:
        view["body"] = body
    else:
        view["preview"] = preview(body)
    return view


def _agent(row: sqlite3.Row, unread: int = 0) -> dict:
    return {
        "name": row["name"],
        "description": row["description"],
        "role": row["role"],
        "unread": unread,
        "activity": row["activity"],
        "activity_at": row["activity_at"],
        "activity_at_local": local_time(row["activity_at"]) if row["activity_at"] else "",
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "last_seen_local": local_time(row["last_seen_at"]),
    }


def _subject(value: str) -> str:
    text = (value or "").strip()
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
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 1024:
        raise MailError("invalid_workspace", "workspace 最长 1024 字")
    return text


def _limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_limit", "limit 必须是整数") from None
    if number < 1 or number > 100:
        raise MailError("invalid_limit", "limit 必须在 1 到 100 之间")
    return number


_REPLY_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fw|fwd|回复|答复)\s*[:：]\s*)+", re.IGNORECASE)


def _re_subject(subject: str) -> str:
    """折叠叠加的 Re:/回复: 前缀，主题不再一轮比一轮长。"""
    core = _REPLY_PREFIX_RE.sub("", subject).strip() or subject.strip()
    return f"Re: {core}"[:200]


def _role(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if text in {"", "member"}:
        return ""
    if text == "coordinator":
        return "coordinator"
    raise MailError("invalid_role", "role 只能是空（成员）或 coordinator")


def _activity(value) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) > 200:
        raise MailError("invalid_activity", "activity 太长了，200 字以内说清楚你在做什么")
    return text
