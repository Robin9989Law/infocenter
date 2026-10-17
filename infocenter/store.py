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

BROADCAST = {"*", "all", "everyone"}
NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$", re.UNICODE)
WINDOW_RE = re.compile(r"^[\w][\w.:-]{0,63}$", re.UNICODE)
# L2：案卷编号 / thread_id 的安全字符集与长度（与窗口号同风格，首字符须为字母/数字/下划线）。
# 服务端源头堵住 XSS：id 会进 URL / 前端属性，禁止换行、引号、斜杠、前导 . - : 等。
CASE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[\w][\w-]{0,31}$", re.UNICODE)

# 人的身份只有这一份：现名 manager，旧名 human 永久只认。
# 不做写时归一、不做数据迁移（哈希链里的 human 留着）。grok 从这里 import，别改这三个名字。
HUMAN = "manager"
HUMAN_NAMES = frozenset({"manager", "human"})

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

# M9 写锁：单次锁的默认有效秒数与上限（默认参数在 class Store 之前求值，常量必须定义在此处）
# 认领（claimed_by）的兜底 TTL：窗口**都没在跟服务说话**这么久 ⇒ 它手上那封信的认领算僵尸。
# 必须大于 headless 单轮上限（默认 1800s）：长任务跑起来时窗口是安静、不轮询的。
CLAIM_TTL = 7200
# 看板「停滞」黄灯：未封卷、且最后一个节点超过这么久没动 ⇒ 标黄提示（**只提示，不自动处置**）。
STALL_HOURS = 24
LOCK_TTL = 1800
LOCK_TTL_MAX = 12 * 3600

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


# 全项目统一时区：**北京时间（UTC+8，固定偏移）**。
# 各 agent 机器时区不同 ⇒ **一律不用机器本地时区**（`.astimezone()` 无参那种写法禁用）。
BEIJING = timezone(timedelta(hours=8))


def utcnow() -> str:
    """**存储用**：UTC ISO（`...Z`）。带 Z 无歧义，排序/比较都用它。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ago(minutes: float) -> str:
    """`minutes` 分钟之前的 UTC ISO（跟 `utcnow()` 同一口径 ⇒ 能直接比字符串）。采样窗口用。"""
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        minutes = 0.0
    moment = datetime.now(timezone.utc) - timedelta(minutes=max(0.0, minutes))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(iso: str) -> datetime | None:
    try:
        return datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def beijing_time(iso: str) -> str:
    """**给人/agent 看的时间**：UTC ISO → 北京时间，固定 +0800（不随机器时区变）。

    例：`2026-10-15T08:29:01Z` → `2026-10-15 16:29:01 +0800`。
    """
    moment = _parse_utc(iso)
    if moment is None:
        return str(iso)
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S %z")


def beijing_hhmm(iso: str) -> str:
    """北京时间只取时:分。"""
    moment = _parse_utc(iso)
    if moment is None:
        return str(iso)[:5]
    return moment.astimezone(BEIJING).strftime("%H:%M")


def beijing_stamp() -> str:
    """**当前**北京时间（给人看的日志戳）：`2026-10-15 16:29:01`。"""
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


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


def is_person(name: str) -> bool:
    """manager 与 human 是同一个人。非法名字当不是。"""
    try:
        return normalize_name(name) in HUMAN_NAMES
    except MailError:
        return False


def _same_agent(left: str, right: str) -> bool:
    """持有者比较：逐字相同，或两边都是人。不改落库的名字。"""
    if left == right:
        return True
    return is_person(left) and is_person(right)


def _holder_names(agent: str) -> tuple[str, ...]:
    """放「自己的」锁时要覆盖的落库名字。人的两个别名都算；其它名字只有自己。"""
    agent = normalize_name(agent)
    if agent in HUMAN_NAMES:
        return tuple(HUMAN_NAMES)
    return (agent,)


def _recipient_names(agent: str) -> tuple[str, ...]:
    """收件箱和往来列表要算进来的落库收件名。

    人只认 `is_person`：manager 与 human 都算上。别人只有自己。
    不改 deliveries 里的名字。反例测试短路这一处，合并就没了。
    """
    agent = normalize_name(agent)
    if not is_person(agent):
        return (agent,)
    return tuple(sorted(HUMAN_NAMES, key=lambda name: (name != HUMAN, name)))


def _own_delivery_fallback_names(agent: str) -> tuple[str, ...]:
    """本名那一行不在时才看的别名。人只有另一个名字；别人没有。

    签收（`_resolve_own_delivery`）和办结门槛（`_own_unread_replies`）共用这一处，
    免得一边认别名、一边仍按本名精确匹配。
    反例测试把它短路成空元组：别名那一行就不算「我的那一份」。
    """
    agent = normalize_name(agent)
    if not is_person(agent):
        return ()
    return tuple(name for name in _recipient_names(agent) if name != agent)


def _resolve_own_delivery(conn: sqlite3.Connection, message_id: str, agent: str) -> sqlite3.Row | None:
    """先找收件人正好是这个名字的那一份，没有再找另一个人的别名。

    只读：不插入、不改 to_agent。两行都在时只返回本名那一行。
    """
    agent = normalize_name(agent)
    row = conn.execute(
        """
        SELECT message_id, to_agent, status, read_at, claimed_by, claimed_at
        FROM deliveries
        WHERE message_id = ? AND to_agent = ?
        """,
        (message_id, agent),
    ).fetchone()
    if row is not None:
        return row
    for other in _own_delivery_fallback_names(agent):
        row = conn.execute(
            """
            SELECT message_id, to_agent, status, read_at, claimed_by, claimed_at
            FROM deliveries
            WHERE message_id = ? AND to_agent = ?
            """,
            (message_id, other),
        ).fetchone()
        if row is not None:
            return row
    return None


def _collapse_alias_deliveries(rows: list, agent: str) -> list:
    """同一封信在人的两个名字上各有一行时，只留一份，避免未读数翻倍。

    先留本名那一行，没有才留别名。名字只问 `_recipient_names`。
    非人本来就只有一行。未读查询里若本名那行已读、不在这批里，就留下仍未读的别名行。
    """
    try:
        agent = normalize_name(agent)
    except MailError:
        return list(rows)
    if len(_recipient_names(agent)) < 2:
        return list(rows)
    best: dict[str, object] = {}
    order: list[str] = []
    for row in rows:
        mid = row["id"]
        prev = best.get(mid)
        if prev is None:
            best[mid] = row
            order.append(mid)
            continue
        if row["delivered_to"] == agent and prev["delivered_to"] != agent:
            best[mid] = row
    return [best[mid] for mid in order]


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
                    # M6-0：只认领属于本窗 workspace 的信；他项目窗口**照常看见**但**不认领**，
                    # 认领留给该项目自己的窗口/守护（否则它会被永久挡住）。
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
        # 投递已经提交。趁这次写信，把死窗口的认领放掉。
        # 不改 events 游标，也不把已经越过的未读塞回 new。
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

    # ── D1 案卷（cases & case_nodes）─────────────────────────────────────

    def create_case(
        self,
        agent: str = HUMAN,
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
        wbs_path: str | None = None,
        milestones=None,
        acceptance: str | None = None,
    ) -> dict:
        agent = normalize_name(agent or HUMAN)
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
                window_id=owner_window if _same_agent(owner_agent or "", agent) else None,
                pid=pid,
            )
            thread_id = sent["thread_id"]

        cid = requested if requested is not None else ((thread_id or "").strip() or new_id())
        init_status = _case_status(status or ("open" if owner_window else "pending"))
        if init_status in SEALED_STATUSES:
            raise MailError("invalid_status", "新建案卷状态不能直接为已封卷状态")
        init_note = _node_note(note or ("认领 → 在办" if owner_window else "立案"))
        wbs = _wbs_path(wbs_path)
        stone_json = _milestones_json(milestones)
        # 「怎样算完」：一行验收。空 = 没说清（看板会标出来，机器不拦）。
        acc = _task_text(acceptance, CASE_ACCEPTANCE_MAX)
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
                    waiting_for, sealed, wbs_path, milestones, acceptance, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    cid, cid, title_text, workspace, init_status, owner_agent, owner_window,
                    wbs, stone_json, acc, now, now,
                ),
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
            _emit_case_event(
                conn, owner_agent or agent, "create", "", init_status,
                cid, owner_window, init_note, now,
            )
            row = _find_case(conn, cid)
            return _case_view(conn, row, viewer=agent)

    def get_case(self, case_id: str, viewer: str | None = None) -> dict:
        if not isinstance(case_id, str) or not case_id.strip():
            raise MailError("not_found", "缺少案卷编号", 404)
        cid = normalize_case_id(case_id)
        viewer_name = normalize_name(viewer) if viewer else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            row = _lazy_reconcile_case(conn, row, now)
            return _case_view(conn, row, viewer=viewer_name)

    def case_wbs_set(
        self,
        case_id: str,
        agent: str,
        wbs_path: str | None,
        window_id: str | None = None,
    ) -> dict:
        """把案卷归到一个 WBS（路径，空串 = 未归类）。**只有本项目的 coordinator 或 human 能改**。

        一个案卷**唯一**归属一个 WBS；WBS 是树（`父/子`），不建表、只用路径。
        """
        cid = normalize_case_id(case_id)
        path = _wbs_path(wbs_path)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            _must_coordinator(conn, agent, row["workspace"] if row else None)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            if row["sealed"] or row["status"] in SEALED_STATUSES:
                raise MailError("case_sealed", f"案卷 {cid} 已封卷，不能再改归属", 409)
            conn.execute(
                "UPDATE cases SET wbs_path = ?, updated_at = ? WHERE id = ?", (path, now, cid)
            )
            _insert_node(
                conn,
                case_id=cid,
                agent=agent,
                window_id=window_id,
                action="wbs",
                note=_node_note(f"归入 WBS：{path or '（未归类）'}"),
                status=row["status"],
                auto=False,
                now=now,
            )
            out = _case_view(conn, _find_case(conn, cid), viewer=agent)
        self._signal()
        return out

    def case_acceptance_set(
        self,
        case_id: str,
        agent: str,
        acceptance: str | None,
        window_id: str | None = None,
    ) -> dict:
        """写案卷的「**怎样算完**」（一行验收，空串 = 撤掉）。

        human 定的毛病（2026-10-15）：任务一多，**没人写清"做成什么算做成"**，于是
        "做完没有"全靠事后吵。案卷是控制包，它这一层也要有一句标准。
        谁能写：该案卷的 owner，或 coordinator（跟 `ic task add --acceptance` 一个口径：
        自己的活自己写清标准）。写的时候空着不拦；**办结**时这一栏空着会 409 `no_acceptance`
        （先 `ic case <id> --acceptance "…"`），中止不要求。
        """
        cid = normalize_case_id(case_id)
        text = _task_text(acceptance, CASE_ACCEPTANCE_MAX)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            if row["sealed"] or row["status"] in SEALED_STATUSES:
                raise MailError("case_sealed", f"案卷 {cid} 已封卷，不能再改「怎样算完」", 409)
            if not _same_agent(agent, row["owner_agent"] or "") and not _is_coordinator(conn, agent, row["workspace"]):
                owner = row["owner_agent"] or "（还没人）"
                raise MailError(
                    "not_yours",
                    f"只有该案卷的 owner（{owner}）或 coordinator 能写「怎样算完」",
                    403,
                )
            conn.execute(
                "UPDATE cases SET acceptance = ?, updated_at = ? WHERE id = ?", (text, now, cid)
            )
            _insert_node(
                conn,
                case_id=cid,
                agent=agent,
                window_id=window_id,
                action="acceptance",
                note=_node_note(f"怎样算完：{text or '（撤掉）'}"),
                status=row["status"],
                auto=False,
                now=now,
            )
            out = _case_view(conn, _find_case(conn, cid), viewer=agent)
        self._signal()
        return out

    def case_milestone_add(
        self,
        case_id: str,
        agent: str,
        name: str,
        owner: str | None = None,
        window_id: str | None = None,
    ) -> dict:
        """给案卷加一条里程碑。**只有 coordinator 能改**（human 定稿：里程碑由 agent 提议、coordinator 落）。

        里程碑 = 案卷上的**一列 JSON**（不建表）：一个里程碑可对应若干任务，案卷只显示里程碑。
        """
        cid = normalize_case_id(case_id)
        text = str(name or "").strip()[:MILESTONE_MAX]
        if not text:
            raise MailError("invalid_milestones", "里程碑要有名字")
        who = str(owner or "").strip()
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            _must_coordinator(conn, agent, row["workspace"] if row else None)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            if row["sealed"] or row["status"] in SEALED_STATUSES:
                raise MailError("case_sealed", f"案卷 {cid} 已封卷，不能再改里程碑", 409)
            items = _milestones(row["milestones"])
            items.append({"name": text, "owner": who, "done": False})
            conn.execute(
                "UPDATE cases SET milestones = ?, updated_at = ? WHERE id = ?",
                (_milestones_json(items), now, cid),
            )
            _insert_node(
                conn,
                case_id=cid,
                agent=agent,
                window_id=window_id,
                action="milestone",
                note=f"里程碑 +「{text}」" + (f"（负责人 {who}）" if who else ""),
                status=row["status"],
                auto=False,
                now=now,
            )
            out = _case_view(conn, _find_case(conn, cid), viewer=agent)
        self._signal()
        return out

    def case_milestone_done(
        self,
        case_id: str,
        agent: str,
        index: int,
        done: bool = True,
        window_id: str | None = None,
        note: str | None = None,
    ) -> dict:
        """勾掉 / 撤回一条里程碑（下标从 0 起）。**只有 coordinator 能改**。"""
        cid = normalize_case_id(case_id)
        idx = int(index)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_case(conn, cid)
            _must_coordinator(conn, agent, row["workspace"] if row else None)
            if row is None:
                raise MailError("not_found", f"没有找到案卷：{cid}", 404)
            if row["sealed"] or row["status"] in SEALED_STATUSES:
                raise MailError("case_sealed", f"案卷 {cid} 已封卷，不能再改里程碑", 409)
            items = _milestones(row["milestones"])
            if idx < 0 or idx >= len(items):
                raise MailError("invalid_milestones", f"没有第 {idx} 条里程碑（共 {len(items)} 条，下标从 0 起）")
            items[idx]["done"] = bool(done)
            conn.execute(
                "UPDATE cases SET milestones = ?, updated_at = ? WHERE id = ?",
                (_milestones_json(items), now, cid),
            )
            mark = "✔" if done else "↺"
            _insert_node(
                conn,
                case_id=cid,
                agent=agent,
                window_id=window_id,
                action="milestone",
                note=_node_note(
                    note or f"里程碑 {mark}「{items[idx]['name']}」（{'完成' if done else '撤回完成'}）"
                ),
                status=row["status"],
                auto=False,
                now=now,
            )
            out = _case_view(conn, _find_case(conn, cid), viewer=agent)
        self._signal()
        return out

    def list_cases(
        self,
        workspace: str | None = None,
        status: str | None = None,
        owner_window: str | None = None,
        owner_agent: str | None = None,
        waiting_for: str | None = None,
        limit: int = 100,
        wbs: str | None = None,
        viewer: str | None = None,
    ) -> list[dict]:
        limit = _limit(limit)
        wbs_filter = _wbs_path(wbs) if wbs else None
        status_filter = _case_status(status) if status else None
        window_filter = normalize_window(owner_window) if owner_window else None
        agent_filter = normalize_name(owner_agent) if owner_agent else None
        # 「在等谁」：只按名字比（空表示不在等）
        wait_filter = normalize_name(waiting_for) if waiting_for else None
        viewer_name = normalize_name(viewer) if viewer else None
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
                if agent_filter is not None and not _same_agent(row["owner_agent"] or "", agent_filter):
                    continue
                if wait_filter is not None and (row["waiting_for"] or "") != wait_filter:
                    continue
                if wbs_filter is not None:
                    # 前缀匹配：给 `防混写` 就带出 `防混写/锁与闸` 下面的案卷
                    path = row["wbs_path"] or ""
                    if path != wbs_filter and not path.startswith(wbs_filter + "/"):
                        continue
                matched.append(_case_view(conn, row, viewer=viewer_name))
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
        agent = normalize_name(agent or HUMAN)
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
            return _case_view(conn, updated, viewer=agent)

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
                    and not is_person(agent)
                    and not _same_agent(row["owner_agent"] or "", agent)
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
                    _emit_case_event(
                        conn, agent, action, row["status"], "open",
                        row["id"], window_id, note_text, now,
                    )
                updated = _find_case(conn, row["id"])
                return _case_view(conn, updated, viewer=agent)

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
            # 显式认领才进链。阅读/回复顺带认领走的是同一套落库，但不从这里经过。
            last = conn.execute(
                "SELECT action, note FROM case_nodes WHERE case_id = ? ORDER BY id DESC LIMIT 1",
                (created["id"],),
            ).fetchone()
            if last is not None and last["action"] in {"claim", "takeover"}:
                _emit_case_event(
                    conn, agent, last["action"], "", "open",
                    created["id"], window_id, last["note"], now,
                )
            return _case_view(conn, created, viewer=agent)

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
        agent = normalize_name(agent or HUMAN)
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
                pid=pid if _same_agent(target_agent, agent) else None,
                now=now,
            )
            note_text = _node_note(note or f"转办 → 交接给 {target_window}")
            # D-2：跨 agent 转办时，先释放源 owner 名下这条线程的投递认领，
            # 避免旧窗口仍被当成主办、新信继续继承僵尸认领（双归属）。
            source_owner_agent = row["owner_agent"]
            if source_owner_agent and not _same_agent(source_owner_agent, target_agent):
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
            node_window = source_window or row["owner_window"] or target_window
            _insert_node(
                conn,
                case_id=row["id"],
                agent=agent,
                window_id=node_window,
                action="transfer",
                note=note_text,
                status="open",
                auto=False,
                now=now,
            )
            _emit_case_event(
                conn, agent, "transfer", row["status"], "open",
                row["id"], node_window, note_text, now,
            )
            updated = _find_case(conn, row["id"])
            res = _case_view(conn, updated, viewer=agent)
        self._signal()
        return res

    def pause_case(
        self,
        case_id: str,
        agent: str = HUMAN,
        window_id: str | None = None,
        note: str | None = None,
        interrupted: bool = False,
        actor: str | None = None,
    ) -> dict:
        agent = normalize_name(actor or agent or HUMAN)
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
        agent: str = HUMAN,
        window_id: str | None = None,
        note: str | None = None,
    ) -> dict:
        agent = normalize_name(agent or HUMAN)
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
        agent: str = HUMAN,
        window_id: str | None = None,
        note: str | None = None,
        auto: bool = False,
    ) -> dict:
        agent = normalize_name(agent or HUMAN)
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
        agent: str = HUMAN,
        window_id: str | None = None,
        note: str | None = None,
        auto: bool = False,
    ) -> dict:
        agent = normalize_name(agent or HUMAN)
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
        agent: str = HUMAN,
        window_id: str | None = None,
        note: str | None = None,
        pid: int | None = None,
    ) -> dict:
        agent = normalize_name(agent or HUMAN)
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
                    pid=pid if _same_agent(new_owner_agent, agent) else None,
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
            _emit_case_event(
                conn, agent, "reopen", row["status"], new_status,
                row["id"], window_id, note_text, now,
            )
            updated = _find_case(conn, row["id"])
            res = _case_view(conn, updated, viewer=agent)
        self._signal()
        return res

    # ── M8 任务队列（工作包）────────────────────────────────────────────
    # 口径见 PLAN §12.5（覆盖 ⑬ 里已撤销的两处）：单表；deps=字符串列（可空串，不 NULL）；
    # handoff=停任务必填（命令层硬拒，自由文本）；权限=写法二；
    # 开工三件套硬拒已撤（规格不全只提示不拦）；supersedes **仍执法**成链不成树；
    # 任务无窗口归属；不复用案卷的 waiting_for/sealed/回复翻转。无 priority 列。

    # ── M8 任务队列（工作包）────────────────────────────────────────────
    # 口径见 PLAN §12.5：单表 tasks；deps=字符串列（不建 artifacts/task_deps）；
    # handoff=停任务必填（非空自由文本，三段不在这里硬切）；权限=写法二；任务无窗口归属。

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
        """建任务：**只有 coordinator**，且**必须带 workspace**（否则 --mine 会串项目）。"""
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
        """列表：默认只列**活线**；被取代的折叠进 `superseded` 供一行灰字展示。"""
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
        """详情：含完整 supersedes 链（一眼看出"第几次尝试、前几次停在哪"）。"""
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
        """开工：todo → doing（owner 或 coordinator）。**开工即冻结规格**（规格不全不拦人，看板标黄）。"""
        agent = normalize_name(agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            if row["status"] != "todo":
                raise MailError("invalid_transition", f"只有 todo 能开工（当前 {row['status']}）", 409)
            # 第三条线只兜底：规格不全**不拦人**（看板会把它标黄）。
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
        """改状态（写法二）：owner 可 doing↔blocked（blocked 必带原因）；done/stopped 仅 coordinator；
        stopped **必须**带**非空交接段**（三段模板在 SKILL 里，不在这里硬切）；done/stopped 是终态、不可逆。"""
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
            # 状态机 = 两张表的**并集**，对谁都一样（表就是唯一事实，别再散判）。
            # 注意：`doing → todo` 对 coordinator **也不许**——倒回 todo 等于"解冻规格"，
            # 而"开工即冻结规格"是硬口径；要改规格就 stop 后新建任务线。
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
            # 痕迹必须与**行里的值**逐字一致（规范化在写行那一步完成，这里只许照抄行）
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
        """改规格：**仅 coordinator**，且**只在 todo**（开工即冻结；要改就 stop 后新建任务线）。"""
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
            # 照抄行里的值（不是调用方给的原文）——否则"痕迹重放 == 行"会被空格/大小写搞红
            _emit_task_event(
                conn, row["id"], agent, "update", after["status"], after["status"],
                {key: ("" if after[key] is None else after[key]) for key in patch},
                now,
            )
            res = _task_view(after)
        self._signal()
        return res

    def task_bump(self, agent: str, task_id: str, by: int = 1) -> dict:
        """调优先级（信只能做这件事；**永不打断**运行中的任务）。仅 coordinator。"""
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

    # ── M9 写锁：同一时间只有一个 agent 能改同一个路径 ─────────────────────
    # 背景：两个 agent 同时改同一文件 ⇒ 两版改动混写（实测发生过）。
    # 规矩：写文件前先 `ic lock <路径>`；拿不到（409）就 `ic queue` 记进自己的 TODO，不要发信、不要硬写。

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
    ) -> dict:
        """拿锁（可一次拿多个路径）。

        - 默认（显式给路径）：**全有或全无**——任一被他人占着就整体失败（你要知道"有人正在改"）。
        - `skip_locked=True`（`ic lock --changed` 用）：别人锁着的**跳过并在 `skipped` 里报告**，
          其余照拿。理由：`--changed` 的语义是"锁我这批脏文件"，而别人锁着的脏文件**本来就不是我的**
          （实测撞车成因：`--changed` 把对方的在飞文件、连外锚文件一起抓走 ⇒ 互相死锁）。
        - `dirty`（可选，`{路径: 摘要}`）：**拿钥匙那一刻盘上本来就有**的未提交改动。
          由命令行嗅出来（`git diff`）后带进来登记，好让提交那一刻再提醒一句：
          "这套改动不是这次拿钥匙写的"。这里**不做任何判定**，只登记。
        - 续期：这次要的路径里、**当前由我持有**的，自 `acquired_at` 起盘上没有写入痕迹
          ⇒ 不给第二把租约（409 `no_progress`，已有锁原样留下）。`stay=True` 可以在没写过的
          情况下再拿一会儿，但 `acquired_at` 不刷新，且最迟到拿锁后 `2×LOCK_TTL`。
        """
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
            if force:  # 抢锁＝不可逆干扰：项目内的路径看**那个项目**的 coordinator，项目外回落 manager
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
                    # 字符串没命中，还可能被**同一个文件的另一种写法**占着（大小写/硬链接）
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
            # 续期：只看「这次要、且现在是我拿着」的路径。盘上自 acquired_at 起没写过 ⇒ 整笔不给第二把。
            # 拒绝发生在任何 INSERT/chmod 之前；抛错会回滚本事务，已有锁（含这次 sweep 到的）都不动。
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
            # skip_locked 把要的路径全跳过时 wanted 为空。这时不能拼 `IN ()`
            # （SQLite 语法错误）——「别人全占着、我一个都不拿」是正常退路，不是 500。
            if wanted:
                for path in wanted:
                    # 同一个文件的另一种写法若还留着钥匙 ⇒ 清掉那一把（**只清它**，别误伤别人的锁）
                    for stale in [
                        r["path"] for r in conn.execute("SELECT path FROM locks").fetchall()
                        if r["path"] != path and _same_file(r["path"], path)
                    ]:
                        conn.execute("DELETE FROM locks WHERE path = ?", (stale,))
                    # 拿钥匙这一刻盘上本来就有的未提交改动（换写法/硬链接也算同一个文件）
                    dirt = dirty_text.get(path, "")
                    if not dirt and dirty_text:
                        dirt = next(
                            (val for key, val in dirty_text.items() if _same_file(key, path)), ""
                        )
                    acquired_at, path_expires, _stay_mark = plans[path]
                    # 别名续期不改落库名字：human 拿的锁，manager 来续，行里仍是 human。
                    prior = renewals.get(path)
                    stored_agent = prior["agent"] if prior is not None else agent
                    conn.execute(
                        """
                        INSERT INTO locks(path, agent, window_id, task_id, note, dirty_at_lock, acquired_at, expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            agent = excluded.agent, window_id = excluded.window_id,
                            task_id = excluded.task_id, note = excluded.note,
                            -- 续期（`--changed` 把已在手里的路径再锁一遍）时别把登记过的脏改动抹掉：
                            -- 这次没嗅到（`--changed` 只嗅命令行点名的路径）不等于"盘上就干净"，
                            -- 抹掉等于让提交那一刻的提醒凭空消失。
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
                    # 无进度续期才打记号；普通拿锁 / 有进度的续期不加这个键。
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
                    if (why := _chmod_writable(path))  # 钥匙在手 ⇒ 文件必须**真的**变可写
                ]
                if unwritable:
                    # 拿不到可写的钥匙就不算拿到钥匙：整笔回滚，并当场说清为什么（不静默、不猜）。
                    #
                    # 回滚要回滚**干净**：DB 由事务回滚，盘上的权限位也得收回去。
                    # 否则（kimi 2026-10-15 报的洞）一次拿多个文件、中途失败时，前面**已经改成可写**
                    # 的文件会留在「没有钥匙却可写」的状态——锁＝只读被静默破坏，还没人知道。
                    conn.rollback()
                    live = [row["path"] for row in conn.execute("SELECT path FROM locks").fetchall()]
                    for path in wanted:
                        if any(_same_file(held_path, path) for held_path in live):
                            continue  # 回滚后这个文件仍有活锁（多半是我自己续期的那把）：别动盘面
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
                "expires_at": response_expires,  # 存储口径：UTC
                "expires_at_beijing": beijing_time(response_expires),  # 给人看：北京时间
                "locks": [_lock_view(r) for r in rows],
                "skipped": skipped,  # 别人锁着、这次跳过的（只可能是 --changed 那一类）
                # 拿钥匙前盘上本来就有的未提交改动（不是这次写的）：登记下来，
                # 提交那一刻由 `ic guard` 再提醒一句
                "dirty_at_lock": {r["path"]: r["dirty_at_lock"] for r in rows if r["dirty_at_lock"]},
            }
        self._signal()
        return res

    def lock_release(self, agent: str, paths=None, force: bool = False) -> dict:
        """放锁：只放自己的；`force` 仅 coordinator 可用（清场）。**放锁留痕**（谁放了什么）。"""
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
                    # 按文件放，不按写法放：硬链接 / 大小写不同的路径也要能放下那一把
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
            # 放完钥匙仍是可写的（chmod 没落地 / ACL / 只读挂载）：不是"放好了"，得说一声
            "stuck": stuck,
            # 不看谁在等、不点名、不发信：待改条数照旧挂在库里，各人照自己的 TODO 轮。
            "pending_left": pending_left,
        }

    # ── 「不排队」：拿不到钥匙就把改动**记一笔**留在库里；不通知、不点名、不排序 ──
    def _queue_head(self, path: str) -> tuple[int, str]:
        """这个文件被几个人记了笔，以及现在谁正拿着钥匙。"""
        queue = self.list_queue(path)
        with self._session(readonly=True) as conn:
            row = conn.execute("SELECT agent FROM locks WHERE path = ?", (path,)).fetchone()
        return queue["count"], (row["agent"] if row else "")

    # ── 改同一个文件：拿不到钥匙就把改动**记一笔**（`ic queue`），之后照自己的 TODO 轮 ──
    # human 定稿：**不排队、不通知**——谁想做谁做，先拿到钥匙的那个做；
    # 「合并」只是**可选加速**（愿意才合，合了要写明按谁的要求改了什么），不是派活。

    def queue_change(
        self,
        agent: str,
        path: str,
        note: str | None = None,
        patch: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """**提议**一处改动（拿不到钥匙时用这个，而不是硬等/硬写）。

        提议本身**不需要钥匙**（它只是把话留给持有者），但会进只增链 ⇒ 谁提了什么、谁合并了都有痕迹。
        """
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
        """看排队：默认只列**还没合并**的提议。

        台账是纯派生：`propose` 事件若被后面的 `propose_merged` 覆盖（按 seq）就算合并过。
        """
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
            # 规矩（human 定稿）：**不排队、不通知**——各人照自己的 TODO 轮，能做就做，不做就下一个。
        }

    def merge_queue(
        self,
        agent: str,
        path: str,
        ids=None,
        note: str | None = None,
    ) -> dict:
        """**合并排队内容**（**可选加速，不是义务**）：谁拿钥匙谁可以做；coordinator 例外（钥匙主人不在时收口）。

        默认规矩是**谁提谁落**：轮到你时只落你自己那条，别人那条留给它自己。
        只有当队列里恰好就是"你正在改的那一行"、或极小改动时，顺手合才划算——合了就要写清
        「按谁的要求改了什么」（自动记 `ids`，`note` 里补一句更好）。
        """
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
                        # 不许把坏输入抛成 500：说清"该写成什么样"（实测踩过：`--id q377,q457`
                        # 整串被当成一个 id ⇒ ValueError ⇒ 500，人只看到「服务器内部错误」）。
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
        # 谁拿钥匙谁就能合并；按 human 定的规矩，接手的是**第一个排队的**那个
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
            # 黄灯：拿满一个 TTL 仍没在盘上留下写入痕迹。刚拿到的锁不亮。
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
            # 有人记了要改、现在没有活锁：灯用，不加表。
            "orphan_queue": orphan_queue,
        }

    def probe_counts(self, since: str) -> dict:
        """窗口内每家的**产出**与机械动作计数（只读，不改任何状态）。`ic probe` 用。

        **空转要成对看**：只数"触发"会被骗（实测 pi 每分钟 36 次 headless，而在外面看
        它的心跳计数一动不动）。这里给出"产出"这一半；另一半（**触发数**）由
        `crew.run_stats` 从运行记录里数——两边并排摆出来，才看得出谁在原地踏步。
        """
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
        """**提交闸**：工作区里每个被改的文件都必须被一个**未过期锁**覆盖。

        规则：① 没有任何锁 ⇒ 违规（谁都没声明在改它）；② `require_mine=True`（默认，成员自己提交）时
        锁必须是自己的，除非调用者是 coordinator（可替成员落地）；③ `require_mine=False`（**git 钩子用**）
        只要求"有锁覆盖"——互斥已在"拿锁"那一步生效，钩子只需保证**没有裸改动落地**。
        """
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
            # 跟拿钥匙同一套身份：硬链接 / 大小写不同的写法也算盖住了。
            # 只比字符串的话，人拿着钥匙、闸门却说「没锁」，就会被推向 --no-verify。
            covering = _rows_covering(held_rows, [path])
            row = covering[0] if covering else None
            if row is None:
                unlocked.append(path)
            elif (not require_mine) or _same_agent(row["agent"], agent) or is_coord:
                mine.append({"path": path, "holder": row["agent"]})
                if row["dirty_at_lock"]:
                    # 拿钥匙时盘上就已经有别处来的改动：这次提交会把它们一起带走（黄灯，不拦）
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

    def sweep(self, now: str | None = None) -> dict:
        """**死流程回收**（第 3 条线里唯一会自动做的事）：过期锁、死进程窗口、僵尸认领。

        三条都是**无争议回收**（只回收"没人认领的东西"，绝不改任何语义状态），且**全部留痕**。
        由服务端后台定时器调用 ⇒ 不再依赖"有人 inbox 时才顺手清"（pi 报的洞：
        低活跃项目里僵尸认领可能一直不触发回收）。
        """
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
        """**机器验证留痕**：① 哈希链完整（防篡改/防删行）；② 每个任务「重放痕迹 == 当前行」；
        ③ **外锚**（可选，见 `anchor`）——链头被提交进 git 后，整表重做/改链也能查出来。

        漏写一行 ⇒ 重放结果与当前行不一致；改过一行 ⇒ 链断；整表重做 ⇒ 外锚对不上。
        """
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
            # ③ 外锚：链头曾经被写进 git（仓库外），这里核对"那个点还在不在、还是不是原来的哈希"。
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
                    # 锚点之后的新痕迹"未被见证"——不可怕，但必须显式可见，好再打一次锚。
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
        # 办结才要求「怎样算完」，并且这条线上发给封卷人自己的信都已签收。
        # 中止（aborted）两条都不要求。任务侧的开工门槛不在这里再加一遍。
        if seal and new_status == "done" and not str(row["acceptance"] or "").strip():
            raise MailError(
                "no_acceptance",
                f"案卷 {row['id']} 还没写「怎样算完」，不能办结。"
                f"先 `ic case {row['id']} --acceptance \"…\"`。中止不要求这一句。",
                409,
            )
        if seal and new_status == "done":
            _block_unread_replies(conn, row, agent)
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
        # 暂停 / 恢复 / 中止 / 办结（含「写节点时直接封卷」和窗口暂停带上的案卷）同一事务进链。
        _emit_case_event(
            conn, agent, action, row["status"], new_status,
            row["id"], node_window, note, now,
        )
        updated = _find_case(conn, row["id"])
        return _case_view(conn, updated, viewer=agent)

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

    def write_pid_then_reconcile(self, window_id: str, agent: str, pid: int | None = None) -> None:
        """H2：同一事务内先写回本次 pid，再惰性对账。

        事务边界：一次 BEGIN IMMEDIATE 里完成「更新 pid → _lazy_reconcile_all → commit」。
        对账若失败，pid 写入一并回滚。原 pid 仍活着时不覆盖，规则在 _upsert_window。
        窗口还不存在：只对账，不建行（留给随后的 touch）。
        窗口属于别的 agent：不写 pid；对账照旧提交，提交后再 409。
        """
        wid = normalize_window(window_id)
        if not wid:
            return
        agent_name = normalize_name(agent)
        pid = _pid_value(pid)
        now = utcnow()
        conflict_owner = None
        with self._session(immediate=True) as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            if row is not None:
                owner = row["agent"]
                if owner and owner != agent_name:
                    conflict_owner = owner
                elif pid is not None:
                    _upsert_window(
                        conn,
                        window_id=wid,
                        agent=agent_name,
                        workspace=None,
                        activity=None,
                        pid=pid,
                        now=now,
                    )
            _lazy_reconcile_all(conn, now)
        if conflict_owner:
            raise MailError(
                "window_conflict",
                f"窗口 {wid} 已属于 {conflict_owner}，不能被 {agent_name} 使用",
                409,
            )

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
                if agent_filter and not _same_agent(row["agent"], agent_filter):
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
        """办公室正常退出上报：窗口标为 left、释放它名下的认领与写锁。

        **不再**自动中止/封卷它名下的案卷——案卷只随"事做完"关闭（宪法）。
        只摘掉主办窗口指针并记一行 `[自动]` 说明，等接办或显式收工。
        """
        wid = normalize_window(window_id)
        if not wid:
            raise MailError("window_required", "需要提供窗口号 window_id")
        acting = actor or agent
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            agent_name = normalize_name(acting) if acting else (row["agent"] if row else HUMAN)
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
            # M6-0：窗口离场即释放它名下的认领，避免僵尸认领把信永远挡住（其它窗口/守护可接手）。
            conn.execute(
                "UPDATE deliveries SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by = ?",
                (wid,),
            )
            # M9：窗口离场同样释放它拿着的写锁（僵死锁会把别人永远挡在门外）；**放锁留痕**。
            _release_window_locks(conn, wid, now, "窗口离场")
            orphaned_ids = _orphan_window_cases(conn, window_id=wid, agent=agent_name, now=now)
            if note:
                # 离场的人若写了话，照样记一行（**状态不动**）
                for cid in orphaned_ids:
                    _insert_node(
                        conn, case_id=cid, agent=agent_name, window_id=wid,
                        action="note", note=note, status="", auto=bool(auto), now=now,
                    )
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            view = _window_view(conn, win_row)
            view["orphaned_cases"] = orphaned_ids  # 只是摘了主办窗口指针，**没封卷**
            view["aborted_cases"] = []  # 保留字段：现在**不再**自动中止任何案卷
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
                if (
                    row["state"] in {"left", "offline"}
                    or (row["pid"] is not None and not pid_alive(row["pid"], policy="not_dead"))
                )
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


def atomic_replace(path, data: bytes) -> None:
    """把 data 换到 path 上，目标文件不会停在写了一半的状态。

    与旧 sync._atomic_write 同一条纪律：同目录临时名、写入后 fsync、os.replace，
    失败删掉临时文件。不创建父目录，也不 fsync 目录项（原先 sync 也没有）。
    最终字节由调用方自己序列化，因为 indent / ensure_ascii 各处不同。

    相对 config.save、server._write_meta、crew._write_meta（固定 *.json.tmp、不 fsync）
    和 crew._write_pid_record（直接 write_text）的变化：
    - 临时文件改成 `.原名.<pid>.<随机>.tmp`，不再占用固定的 `*.json.tmp`。
    - 写入会 fsync；失败清掉临时文件。
    - pid 记录不再直接覆盖目标，中途失败时旧文件还在。
    成功后的文件内容与各调用方原来的 json.dumps 参数一致。
    """
    path = Path(path)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def pid_alive(pid, *, policy: str) -> bool:
    """进程是否按调用方的口径算「还在」。四套旧判断收成这一处，口径必须写明。

    三种口径对 None、非法值、pid==1、EPERM 的答案不一样，不能用其中一种的否定去代替另一种。

    policy="managed"
        守护和停服务。只认真正的 int（bool 不算），且 pid>1，并且 os.kill(pid, 0) 成功。
        None、字符串、bool、pid<=1、进程不存在、EPERM、其他 OSError 都是 False。
        相对旧 server.pid_alive 的变化：pid<=1 一律 False。旧实现没有这道护栏，
        os.kill(0, 0) 在 POSIX 上会成功，缺失 pid（int(meta.get("pid") or 0)）会被当成服务还活着。

    policy="running"
        窗口展示字段 pid_alive。先 int()（字符串 "12" 可以），pid>1 且 kill 成功才 True。
        None、非法、bool、pid<=1、EPERM、进程不存在都是 False。与旧 store._pid_running 相同。

    policy="not_dead"
        对账用的「没有证据表明这个 pid 已经死了」。返回 True 表示先别把它当死的。
        None → True（没有 pid，不算死，也不是「在跑」）。
        转不成整数 → True（不算死）。
        pid<=0 → False（算死，而且不发信号；0 会打到整个进程组）。
        pid>=1 且 kill 成功 → True（含 pid==1；managed/running 把 1 排除）。
        PermissionError → True（权限不够，不算死）。
        其他 OSError → False（算死）。
        与旧 store._pid_dead 取反后逐输入相同。
    """
    if policy == "managed":
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return False
        return _pid_kill_alive(pid, permission_counts_alive=False)
    if policy == "running":
        try:
            number = int(pid)
        except (TypeError, ValueError):
            return False
        if number <= 1:
            return False
        return _pid_kill_alive(number, permission_counts_alive=False)
    if policy == "not_dead":
        if pid is None:
            return True
        try:
            number = int(pid)
        except (TypeError, ValueError):
            return True
        if number <= 0:
            return False
        return _pid_kill_alive(number, permission_counts_alive=True)
    raise ValueError(f"未知的 pid 判活口径：{policy}")


def _pid_kill_alive(pid: int, *, permission_counts_alive: bool) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return permission_counts_alive
    except OSError:
        return False
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
    """窗口是否已离场：没有这行、显式离场（state / left_at），或记录中的 pid 已死。

    不用 last_seen_at 空闲当离场。活着的守护可以很久不写库，空闲阈值会把安静的活窗口误封。
    pid 被短命进程改写的误判，堵在 _upsert_window（不覆盖仍活着的 pid），不在这里再加一套启发式。
    """
    if win_row is None:
        return True
    if win_row["state"] in {"left", "offline", "crashed", "dead", "exited", "aborted", "stopped"}:
        return True
    if win_row["left_at"]:
        return True
    if win_row["pid"] is not None and not pid_alive(win_row["pid"], policy="not_dead"):
        return True
    return False


def _workspace_compatible(message_workspace: str | None, window_workspace: str | None) -> bool:
    """信的 workspace 与窗口的 workspace 是否相容。

    任一侧为空 ⇒ 相容（信息不足，不下「跨项目」判断，保持既有行为）。
    """
    if not (message_workspace or "").strip() or not (window_workspace or "").strip():
        return True
    return _workspace_scope(message_workspace, window_workspace)


def _window_workspace(conn: sqlite3.Connection, window_id: str | None) -> str | None:
    if not window_id:
        return None
    row = conn.execute(
        "SELECT workspace FROM windows WHERE window_id = ?", (window_id,)
    ).fetchone()
    return row["workspace"] if row else None


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


def _auto_claim_allowed(conn: sqlite3.Connection, window_id: str, message_workspace: str | None) -> bool:
    """M6-0：只允许认领「属于本窗口 workspace」的信。

    否则，一个**不做 workspace 过滤**的长轮询窗口（常见于他项目的 MCP/CLI 会话）会把别的项目的
    信一并认领走；而认领一旦落在别窗，正确项目的守护/窗口在 `/v1/events` 与 `ic inbox` 里就
    **再也看不到它**（过滤条件 `claimed_by IS NULL OR = 本窗`）⇒ 派单/来信**静默丢失**。
    """
    return _workspace_compatible(message_workspace, _window_workspace(conn, window_id))


def _release_stale_claims(conn: sqlite3.Connection, now: str | None = None, ttl: int = CLAIM_TTL) -> int:
    """释放**僵尸认领**（无争议回收），并逐条留痕。三类：

    ① 认领窗口已离场/已死；② 认领窗口与信的 workspace 不相容；
    ③ **认领超时**：`claimed_at` 超过 TTL，且那个窗口也超过 TTL 没跟服务说过话
       （pi 报的洞：认领原来**没有 TTL**，僵尸窗口 pid 活着但不再干活 ⇒ 它的认领永不回收，
        那封信对团队长时间不可见；而且回收只在"别人 inbox 时顺手"才触发）。
    """
    now = now or utcnow()
    cutoff = _lock_expiry(now, -abs(int(ttl)))  # now - ttl
    rows = conn.execute(
        """
        SELECT d.message_id, d.to_agent, d.claimed_by, d.claimed_at,
               m.workspace AS message_workspace,
               w.workspace, w.state, w.left_at, w.pid, w.last_seen_at
        FROM deliveries d
        JOIN messages m ON m.id = d.message_id
        JOIN windows w ON w.window_id = d.claimed_by
        WHERE d.claimed_by IS NOT NULL
        """
    ).fetchall()
    released = 0
    for row in rows:
        stale = _is_window_gone(row) or not _workspace_compatible(
            row["message_workspace"], row["workspace"]
        )
        why = "认领窗口已离场/已死或与项目不相容"
        if not stale:
            silent_since = row["last_seen_at"] or row["claimed_at"] or ""
            if (row["claimed_at"] or "") < cutoff and silent_since < cutoff:
                stale = True
                why = f"认领超时（窗口 {ttl // 60} 分钟没跟服务说过话）"
        if not stale:
            continue
        conn.execute(
            """
            UPDATE deliveries SET claimed_by = NULL, claimed_at = NULL
            WHERE message_id = ? AND to_agent = ? AND claimed_by = ?
            """,
            (row["message_id"], row["to_agent"], row["claimed_by"]),
        )
        _emit_task_event(
            conn, "", row["to_agent"], "claim_release", "", "",
            {
                "message_id": row["message_id"], "window_id": row["claimed_by"],
                "claimed_at": row["claimed_at"] or "", "why": why,
            },
            now,
        )
        released += 1
    return released


def _mark_dead_windows(conn: sqlite3.Connection, now: str) -> int:
    """把 pid 已死的窗口标成离场，并替它放锁（留痕）。返回处理了几个窗口。"""
    active_wins = conn.execute(
        "SELECT * FROM windows WHERE state != 'left' AND pid IS NOT NULL"
    ).fetchall()
    marked = 0
    for w_row in active_wins:
        # None 不算死（也没有在跑）。查询已排除 NULL，这里再显式挡一层。
        if w_row["pid"] is None or pid_alive(w_row["pid"], policy="not_dead"):
            continue
        conn.execute(
            "UPDATE windows SET state = 'left', left_at = COALESCE(left_at, ?) WHERE window_id = ?",
            (now, w_row["window_id"]),
        )
        _release_window_locks(conn, w_row["window_id"], now, "窗口进程已死")
        marked += 1
    return marked


def _lazy_reconcile_case(conn: sqlite3.Connection, case_row: sqlite3.Row, now: str | None = None) -> sqlite3.Row:
    """崩溃惰性补齐：主办窗口已不在（进程死/窗口被删/状态已离场）时**只摘指针 + 记一行**。

    绝不自动中止/封卷（宪法：案卷只随"事做完"关闭，与办公室/agent 在不在无关）。
    """
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
    # 宪法：案卷只随"事做完"关闭。主办窗口死了**不封卷**——只摘指针 + 记一行说明。
    _orphan_window_cases(conn, window_id=owner_window, agent="system", now=now)
    return conn.execute("SELECT * FROM cases WHERE id = ?", (case_row["id"],)).fetchone()


def _lazy_reconcile_all(conn: sqlite3.Connection, now: str | None = None) -> None:
    now = now or utcnow()
    open_cases = conn.execute(
        "SELECT * FROM cases WHERE sealed = 0 AND owner_window IS NOT NULL"
    ).fetchall()
    for c_row in open_cases:
        _lazy_reconcile_case(conn, c_row, now)
    _mark_dead_windows(conn, now)
    # M6-0：对账顺带回收陈旧/错位/超时认领（否则来信会被静默隐藏）。
    _release_stale_claims(conn, now)


def _orphan_window_cases(
    conn: sqlite3.Connection,
    window_id: str,
    agent: str,
    now: str,
) -> list[str]:
    """窗口离场：**案卷不因此中止、不封卷**。

    宪法（human 定稿）：案卷 = 一件事的持久记录，**只随"事做完"关闭，与办公室/agent 在不在无关**。
    所以程序只做两件**无争议**的事：
      ① 摘掉主办窗口指针（窗口没了，那个指针就是假的）；
      ② 记一行**只增**的说明节点（**状态不动、不封卷**）——把"主办窗口没了"说出来，别让人猜。
    要中止/办结，必须由人/coordinator 显式做（`ic case --abort/--close`）。
    """
    open_cases = conn.execute(
        "SELECT * FROM cases WHERE owner_window = ? AND sealed = 0",
        (window_id,),
    ).fetchall()
    orphaned: list[str] = []
    for c_row in open_cases:
        conn.execute(
            "UPDATE cases SET owner_window = NULL, updated_at = ? WHERE id = ?",
            (now, c_row["id"]),
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
            action="note",
            note=(
                "主办窗口离场：案卷**保持未封卷**（不自动中止）。"
                "要继续办就接手（写节点/转办），要收工就显式中止或办结。"
            ),
            status=c_row["status"],
            auto=True,
            now=now,
        )
        orphaned.append(c_row["id"])
    return orphaned


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
    _emit_case_event(conn, agent, "create", "", "pending", thread_id, None, "立案", now)
    return conn.execute("SELECT * FROM cases WHERE id = ?", (thread_id,)).fetchone()


def _must_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"] or row["status"] in SEALED_STATUSES:
        raise MailError("case_sealed", f"案卷 {row['id']} 已封卷（{row['status']}），只读不可修改", 409)


def _own_unread_replies(conn: sqlite3.Connection, thread_id: str, agent: str) -> list[sqlite3.Row]:
    """这条线程上「我的那一份」还没签收的信（按时间从早到晚）。

    与签收同一口径：先找本名那一行；本名没有，才找另一个别名那一行。
    两行都在时只认本名，避免一封信算成两封。read 会把这个人还没签的行一起标掉。
    别人的未读不算（他签不了，拿它卡人＝死锁）。
    """
    if not thread_id or not agent:
        return []
    try:
        agent = normalize_name(agent)
    except MailError:
        return []
    messages = conn.execute(
        """
        SELECT id, from_agent, created_at
        FROM messages
        WHERE thread_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (thread_id,),
    ).fetchall()
    hits: list[sqlite3.Row] = []
    for msg in messages:
        delivery = _resolve_own_delivery(conn, msg["id"], agent)
        if delivery is not None and delivery["status"] == "unread":
            hits.append(msg)
    return hits


def _block_unread_replies(conn: sqlite3.Connection, row: sqlite3.Row, agent: str) -> None:
    """办结门槛：封卷人自己在这条线程上还有未签收信 ⇒ 409 unread_replies。

    只算封卷人自己那一份：别人未签收的副本他签不了，拿它卡人＝死锁。
    中止（aborted）不走这里。
    """
    hits = _own_unread_replies(conn, row["thread_id"], agent)
    if not hits:
        return
    earliest = hits[0]
    when = beijing_time(str(earliest["created_at"] or ""))
    who = earliest["from_agent"]
    raise MailError(
        "unread_replies",
        f"案卷 {row['id']} 这条线上你还有 {len(hits)} 封没收"
        f"（最早 {when}，来自 {who}）。"
        "先把回信看完再封卷（`ic inbox --status unread`）。中止不要求。",
        409,
    )


def _signoff_read_targets(conn: sqlite3.Connection, message_id: str, agent: str) -> list[sqlite3.Row]:
    """这次 read 要改状态的已有行。不插入、不改 to_agent。

    只有一行，或不是人：就是那一行。
    人的两行都未读：一次都标上。
    调用名那行已经不是未读、另一个名字仍未读：改签另一个名字那一行。
    其余：只动调用名那一行；调用名没有行时，沿用别名回退。
    """
    agent = normalize_name(agent)
    names = _recipient_names(agent)
    name_sql = ",".join("?" for _ in names)
    rows = list(
        conn.execute(
            f"""
            SELECT message_id, to_agent, status, read_at, claimed_by, claimed_at
            FROM deliveries
            WHERE message_id = ? AND to_agent IN ({name_sql})
            """,
            (message_id, *names),
        ).fetchall()
    )
    if not rows:
        return []
    if len(rows) == 1 or not is_person(agent):
        return rows
    own = next((row for row in rows if row["to_agent"] == agent), None)
    others_unread = [row for row in rows if row["to_agent"] != agent and row["status"] == "unread"]
    if own is None:
        return others_unread or rows[:1]
    if own["status"] == "unread" and others_unread:
        return [own, *others_unread]
    if own["status"] != "unread" and others_unread:
        return others_unread
    return [own]


def _mark_own_delivery_read(
    conn: sqlite3.Connection, message_id: str, agent: str, now: str
) -> None:
    """把这次 read 该清的行标成已读。不改认领，不动案卷，不改名字，不新建行。"""
    for row in _signoff_read_targets(conn, message_id, agent):
        conn.execute(
            "UPDATE deliveries SET status = 'read', read_at = ? WHERE message_id = ? AND to_agent = ?",
            (row["read_at"] or now, message_id, row["to_agent"]),
        )


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
    if case_row and not case_row["sealed"] and case_row["owner_window"] and _same_agent(case_row["owner_agent"] or "", agent):
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
    delivery = _resolve_own_delivery(conn, msg_id, agent)
    if delivery is None and not _is_thread_party(conn, thread_id, agent):
        raise MailError("not_found", f"没有找到案卷或线程：{thread_id}", 404)
    case_row = _find_case(conn, thread_id)
    if case_row is not None:
        case_row = _lazy_reconcile_case(conn, case_row, now)
        # 签收不是改案卷。封卷线程里的信仍可标已读：不认领、不动案卷、不破只读。
        if mark_read and (case_row["sealed"] or case_row["status"] in SEALED_STATUSES):
            _mark_own_delivery_read(conn, msg_id, agent, now)
            return
        _must_unsealed(case_row)

    if mark_read:
        # 封卷分支已经在上面标过并返回。这里清掉同一人其余未读行，认领仍写在解析到的那一行。
        _mark_own_delivery_read(conn, msg_id, agent, now)

    direct_claim = delivery["claimed_by"] if delivery else None
    existing_claim = _effective_thread_claim(conn, thread_id, agent, direct_claim)
    if existing_claim is None and case_row is not None and case_row["owner_window"] and _same_agent(case_row["owner_agent"] or "", agent):
        existing_claim = case_row["owner_window"]

    if existing_claim is not None and existing_claim != window_id and not force:
        raise MailError("already_claimed", f"这条线已被 {existing_claim} 主办，如需接手请使用 force=True", 409)

    if delivery is not None:
        target = delivery["to_agent"]
        read_at = (delivery["read_at"] or now) if mark_read else delivery["read_at"]
        new_status = "read" if mark_read else delivery["status"]
        if force and existing_claim is not None and existing_claim != window_id:
            conn.execute(
                """
                UPDATE deliveries
                SET status = ?, read_at = ?, claimed_by = ?, claimed_at = ?
                WHERE message_id = ? AND to_agent = ?
                """,
                (new_status, read_at, window_id, now, msg_id, target),
            )
        else:
            cur = conn.execute(
                """
                UPDATE deliveries
                SET status = ?, read_at = ?, claimed_by = ?, claimed_at = COALESCE(claimed_at, ?)
                WHERE message_id = ? AND to_agent = ?
                  AND (claimed_by IS NULL OR claimed_by = ?)
                """,
                (new_status, read_at, window_id, now, msg_id, target, window_id),
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
    # M6-0b：已有且仍活着的 pid 不被这次 touch 改写。headless 里的短命进程会继承
    # 宿主窗口号并带上自己的 pid；若照单覆盖，进程一退出，活窗口就被判离场，
    # 名下未封卷案卷自动中止、未读认领被释放。只在新建行，或原 pid 为空/已死时写入。
    reviving = row["state"] in {"left", "offline"} and next_state == "active"
    incoming = _pid_value(pid)
    stored = row["pid"]
    if stored is not None and pid_alive(stored, policy="not_dead"):
        next_pid = stored
    elif incoming is not None:
        next_pid = incoming
    elif reviving:
        next_pid = None
    else:
        next_pid = stored
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


def _upsert_project(
    conn: sqlite3.Connection, workspace: str, name: str | None, now: str,
    archived: bool | None = None, coordinator: str | None = None,
) -> None:
    ws = _workspace(workspace)
    if not ws:
        return
    proj_name = (name or "").strip() or Path(ws).name or ws
    arc = None if archived is None else (1 if archived else 0)
    existing = conn.execute("SELECT * FROM projects WHERE workspace = ?", (ws,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO projects(workspace, name, created_at, updated_at, archived, coordinator) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ws, proj_name, now, now, arc or 0, (coordinator or "").strip()),
        )
    else:
        final_name = proj_name if name else (existing["name"] or proj_name)
        if coordinator is not None:
            # 只有**显式**传才动 coordinator（消息/案卷顺带登记项目时绝不能碰它）
            conn.execute(
                "UPDATE projects SET coordinator = ?, updated_at = ? WHERE workspace = ?",
                (coordinator.strip(), now, ws),
            )
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
    hhmm = beijing_hhmm(row["created_at"])
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
        "created_at_beijing": beijing_time(row["created_at"]),
        "line": line,
    }


TASK_STATUSES = ("todo", "doing", "blocked", "stopped", "done")
TASK_TERMINAL = ("stopped", "done")
TASK_ACCEPTANCE_MAX = 200
# 案卷的「怎样算完」：比任务的一行验收宽一点（一件事的做成标准往往要一句话讲清几件）。
CASE_ACCEPTANCE_MAX = 400
TASK_TEXT_MAX = 2000
TASK_HANDOFF_MAX = 4000
# owner 含本人与 coordinator；coord 仅 coordinator。不允许暂停/回退/换规格。
_TASK_OWNER_MOVES = {("todo", "doing"), ("doing", "blocked"), ("blocked", "doing")}
_TASK_COORD_MOVES = {
    ("todo", "stopped"),   # 没开工就放弃（现实里常见）
    ("doing", "done"),
    ("blocked", "done"),
    ("doing", "stopped"),
    ("blocked", "stopped"),
}


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






def _lock_paths(paths) -> list[str]:
    """把路径规整成绝对路径列表（去重）；空 ⇒ 400。

    「每个文件只有一把钥匙」的第一步：`resolve()` 会把 `./a.py`、`a.py`、符号链接、
    `/var` vs `/private/var` 这类**同一个文件的不同写法**归一（实测踩过 tmpdir 符号链接）。
    第二步（inode 比对）在 `_same_file()`：macOS 文件系统默认大小写不敏感 ⇒
    `A.py` 与 `a.py` 是同一个文件、却是两个字符串，光靠字符串会**长出两把钥匙**。"""
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
    """**锁＝只读**：没人拿钥匙时，文件只能读、不能改（`u+w` 去掉、别人的写位也去掉）。

    为什么落到文件系统（而不只是数据库里记一笔）：这样**所有**写者都被挡住——
    带插件的、不带插件的、`shell > file`、别的 agent 的编辑器，一律先得拿钥匙。
    读权限**一个都不动**（可以只读）。

    返回 `None` = 已经是只读（或文件不在，不用管）；返回一句短话 = **没能**变回只读，原因在此。
    （kimi 2026-10-15 追的提示级：旧版 here 把 `OSError` 静默吞掉——改不回去就没人知道，
    「锁＝只读」就只剩嘴上说说。现在把原因交出去，由调用方摆到人眼前。）
    """
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None  # 不存在（还没建 / 已经删了）：没什么可锁的
    except OSError as exc:
        return f"读权限位失败：{exc.strerror or exc}"
    readonly = mode & ~0o222
    if readonly == mode:
        return None
    try:
        os.chmod(path, readonly)
    except OSError as exc:
        return f"改回只读失败：{exc.strerror or exc}"
    # 复核一次：ACL、只读挂载、或别的进程又把它改回去，都在这里当场暴露
    try:
        after = os.stat(path).st_mode & 0o7777
    except OSError as exc:
        return f"复核权限位失败：{exc.strerror or exc}"
    if after & 0o222:
        return "权限位没落地（改完仍是可写）"
    return None


def _chmod_writable(path: str) -> str | None:
    """**拿钥匙期间可写**：只给 owner 加写位，读位不动。

    返回 `None` = 现在真的可写（或文件还没建，先锁后建是合法的）；返回一个短句 = 做不到，原因在此。

    实测踩过：grok 拿着 `store.py` 的钥匙，文件却还是 444，得再锁一次才成——旧版这里是
    「尽力而为 + 静默吞掉 OSError」，chmod 没落地谁都不知道，人只能自己猜。**拿钥匙的人必须
    立刻知道权限位没落地**，而不是写到一半撞 `PermissionError`。
    """
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None  # 还没这个文件：正常（先锁、后建；建的时候本来就可写）
    except OSError as exc:
        return f"读权限位失败：{exc.strerror or exc}"
    writable = mode | 0o200
    if writable != mode:
        try:
            os.chmod(path, writable)
        except OSError as exc:
            return f"改成可写失败：{exc.strerror or exc}"
    # 复核一次：ACL、只读挂载、或别的进程又把它改回去，都在这里当场暴露
    try:
        if os.stat(path).st_mode & 0o200:
            return None
    except OSError as exc:
        return f"复核不了：{exc.strerror or exc}"
    return "权限位没落地（复核仍是只读）"


def _fingerprint(path: str) -> tuple[int, int] | None:
    """文件的 (设备号, inode)。取不到（不存在/没权限）⇒ None。"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


_CASE_FOLD_CACHE: dict[str, bool] = {}


def _parent_folds_case(path: str) -> bool:
    """父目录会不会把大小写不同的名字当成同一个文件。结果按目录缓存。

    `os.path.normcase` 在 macOS 上并不改大小写（它只在 Windows 上折叠），
    所以不能靠它判断。探测：建一个点文件，看大写名字是否已经存在，然后删掉。
    父目录不可写或还不存在 ⇒ 当「区分大小写」（宁可不合并，也不把两个真文件收成一把）。
    """
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
    """两个路径是不是**同一个文件**。

    字符串相同，或 inode 相同。两边都还不存在时 inode 比不了：父目录若把大小写
    当成同一个名字（macOS 默认卷），`NewFile.py` 与 `newfile.py` 会落成同一个文件，
    却能各拿一把钥匙。两边都有 inode 且不同 ⇒ 真是两个文件，不合并。
    """
    if a == b:
        return True
    left, right = _fingerprint(a), _fingerprint(b)
    if left is not None and left == right:
        return True
    if left is None and right is None and _parent_folds_case(a) and _parent_folds_case(b):
        return os.path.abspath(a).casefold() == os.path.abspath(b).casefold()
    return False


def _rows_covering(rows, paths: list[str]) -> list:
    """锁行里，和 paths 中任一条是同一个文件的那些。

    钥匙按文件发。放钥匙和提交闸若只比路径字符串，换个写法（硬链接、
    大小写不敏感文件系统上的 `A.py` / `a.py`）就会对不上：闸门说没锁，
    unlock 也删不掉库里那一行。
    """
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
    """自这把锁的 acquired_at 起，盘上有没有写入痕迹。

    文件不存在＝无痕迹。比到秒：mtime 的秒 ≥ acquired_at 的秒就算有（宁松勿误杀）。
    chmod 不改 mtime，所以拿锁 / 改权限本身不会被算成进度。
    """
    try:
        mtime = int(os.stat(path).st_mtime)
    except OSError:
        return False
    moment = _parse_utc(acquired_at)
    if moment is None:
        return True
    return mtime >= int(moment.timestamp())


def _matching_lock(rows, path: str):
    """锁表里和 path 是同一个文件的那一行（字符串或 inode）。没有就 None。"""
    for row in rows:
        if row["path"] == path or _same_file(row["path"], path):
            return row
    return None


def _restore_readonly(conn: sqlite3.Connection, rows) -> dict[str, str]:
    """放钥匙 ⇒ 文件**变回只读**（锁＝只读；读不受影响）。

    **但**：同一个文件（换种写法、硬链接、大小写不敏感卷）若还有**活着的**钥匙，就不能
    444——那会把另一个持有者正拿在手里的写权限当场打掉：他拿着钥匙、文件却变只读，
    只能再锁一次（grok 实测踩到的就是这个形态）。**一个文件的权限位由「还有没有人拿着
    这个文件的钥匙」决定，不是由「这次放掉的那一行路径」决定。**

    返回**没能变回只读**的那几个 `{路径: 原因}`（空 = 都收干净了），让人能当场说一句。
    """
    remaining = conn.execute("SELECT path FROM locks").fetchall()
    stuck: dict[str, str] = {}
    for row in rows:
        path = row["path"]
        if any(_same_file(other["path"], path) for other in remaining):
            _chmod_writable(path)  # 还有人拿着这个文件 ⇒ 保持可写
            continue
        why = _chmod_locked(path)
        if why:
            stuck[path] = why
    return stuck


def _release_window_locks(conn: sqlite3.Connection, window_id: str, now: str, why: str) -> int:
    """窗口离场 / 被判死：放掉它名下的写锁，**并逐条留痕**。

    cursor 报的洞：原来这里是裸 `DELETE`，事后痕迹里只有 `lock`、没有 `unlock`，
    "谁知道那把锁是被谁弄没的"就查不出来了。
    """
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
    """锁还剩多少秒（给"快到期、该续期了"的提示用）。"""
    moment = _parse_utc(expires_iso)
    if moment is None:
        return 0
    return int((moment - datetime.now(timezone.utc)).total_seconds())


def _locks_sweep(conn: sqlite3.Connection, now_iso: str) -> int:
    """过期锁自动释放（防僵死）。**顺手留痕**：自动回收也是改动，必须留下谁掉了什么锁。

    这是"死流程"里唯一自动改的东西（只回收，绝不改语义状态），所以更要留痕。
    """
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


# 机器自己在地板上走的那几步，不算「产出」——空转观测要看的是人看得见的动静：
# 信、案卷节点、任务/队列痕迹。拿钥匙、放钥匙、锁到期、旧的通知模型都只是原地踏步。
PROBE_MACHINERY_ACTIONS = {
    "lock", "unlock", "lock_expire", "claim", "claim_release", "offer", "offer_expired",
}


TASK_EVENT_FIELDS = (
    "workspace", "case_id", "owner", "title", "status",
    "blocked_reason", "deps", "acceptance", "handoff", "supersedes", "priority",
)


def _task_event_hash(prev_hash: str, fields: dict) -> str:
    """一条痕迹的哈希 = sha256(上一条的哈希 + 本条全部字段) ⇒ 改一个字就断链。"""
    blob = "\x1f".join([prev_hash] + [str(fields.get(key, "")) for key in
                                       ("task_id", "at", "agent", "action", "from_status", "to_status", "detail")])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _emit_case_event(
    conn, agent, action, from_status, to_status, case_id, window_id, note, now: str,
) -> None:
    """案卷状态机进既有哈希链。`task_id=""` 与 `project_coordinator` / `propose` 同例，任务重放不看它。

    action 加 `case_` 前缀（`case_close` / `case_abort`），避免和任务自己的 create/status 撞名。
    只钉状态跃迁；acceptance / milestone / wbs / 普通节点 / 阅读即认领不走这里。
    """
    name = action if str(action).startswith("case_") else f"case_{action}"
    _emit_task_event(
        conn, "", agent, name, from_status or "", to_status or "",
        {"case_id": case_id, "window_id": window_id or "", "note": note or ""},
        now,
    )


def _emit_task_event(conn, task_id, agent, action, from_status, to_status, detail: dict, now: str) -> None:
    """写一行痕迹（**必须与业务改动同一个事务**：改成功才有痕迹，改失败一起回滚）。

    痕迹由**程序**生成（字段取自真实参数），不接受调用方自由发挥 ⇒ 少写不了、也乱写不了。
    """
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


def _task_priority(value) -> int:
    """优先级：整数（信只能 bump 它；越大越靠前）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_priority", "priority 必须是整数", 400) from None
    return max(-99, min(n, 99))


def _must_human(agent: str) -> None:
    """只有人能指定项目的 coordinator、移除别人。现名 manager，旧名 human 同样算。"""
    if not is_person(agent):
        raise MailError(
            "human_only",
            f"{agent} 不是 manager；这件事只有 manager 能做（旧名 human 仍认）",
            403,
        )


def _is_manager(conn: sqlite3.Connection, agent: str) -> bool:
    """**全局管理者**（旧称 coordinator）：名册里 `role=manager` 的；人（manager / human）天然算。"""
    agent = normalize_name(agent)
    if is_person(agent):
        return True
    row = conn.execute("SELECT role FROM agents WHERE name = ?", (agent,)).fetchone()
    return bool(row and str(row["role"] or "") in {"manager", "coordinator"})


def _project_for(conn: sqlite3.Connection, workspace: str | None) -> sqlite3.Row | None:
    """按 workspace 找项目行（全路径 / 解析后 / 裸目录名都算命中，跟别处一套口径）。"""
    if not (workspace or "").strip():
        return None
    for row in conn.execute("SELECT * FROM projects").fetchall():
        if _workspace_scope(workspace, row["workspace"]):
            return row
    return None


def _project_for_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """按**文件路径**找它属于哪个项目（锁与提交闸只有路径、没有项目）。

    取**最长**匹配（最具体的那个项目）；不属于任何项目 ⇒ None。
    """
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
    """**这个项目**的协调者。

    人拍的口径（2026-10-16）：每个项目 coordinator **不固定、由用户指定**；**项目在、没指定 ⇒ 拒**
    （不许默认兜底）。三种边角：
    - 人（manager，旧名 human）天然算（拍板的人）；
    - **不属于任何项目**（空 workspace / 没登记过的目录）⇒ 回落 `_is_manager`（跟锁那条规则同源）；
    - 项目在而 `coordinator` 为空 ⇒ **False**（必须指定）。
    """
    agent = normalize_name(agent)
    if is_person(agent):
        return True
    row = _project_for(conn, workspace)
    if row is None:
        return _is_manager(conn, agent)
    return (row["coordinator"] or "").strip() == agent


def _may_force(conn: sqlite3.Connection, agent: str, paths) -> bool:
    """抢锁 / 替成员落地这类**不可逆干扰**：按路径找项目 ⇒ 那项目的 coordinator；找不到项目 ⇒ 回落 manager。

    （human 2026-10-16 拍的第 3 条。）`paths=None`（放全部锁＝清场）算 hub 级 ⇒ 只看 manager。
    """
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
            f"项目 {row['workspace']} 还没指定 coordinator（human 2026-10-16 拍：每个项目由人指定、"
            "没指定就拒）。请 human 指定：`ic --as human projects --project <路径> --coordinator <身份>`",
            409,
        )
    raise MailError(
        "not_coordinator",
        f"{agent} 不是这个项目的 coordinator"
        + (f"（{row['workspace']}：coordinator = {row['coordinator']}）" if row is not None else "（这卷没归属项目）")
        + "；建任务 / 改规格只有它或 human 能做",
        403,
    )


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


def _check_handoff(value) -> str:
    """停任务的交接段：**一列自由文本**，命令层**只要求非空**。

    三段式（progress / artifacts / pitfalls）是 SKILL 里的**模板要求**，不在这里硬切——
    第三条线只兜底："不能没有交接"，不规定"怎么写"。
    """
    text = _task_text(value, TASK_HANDOFF_MAX)
    if not text:
        raise MailError(
            "handoff_required",
            "停任务必须写交接段（做到哪 / 产物在哪 / 别踩什么坑）；不写不许停",
            400,
        )
    return text


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


def _human_voices(conn: sqlite3.Connection) -> set[str]:
    """谁有资格代表「人话」：**manager**（及旧值 coordinator）这些身份 + human。

    看板（第 1 条线）上的文字必须由它们用人话重写；agent 之间的信不直接给人看。

    ⚠️ 这是 **SQL 里的**角色比较（不是 Python 的 `==`）——2026-10-16 协调者分层那次改名就漏了它：
    迁移把 role 改写成 `manager` 之后 `WHERE role = 'coordinator'` **永久落空**，而测试还是绿的
    （`seed` 当时裸插旧值、绕过归一化）⇒ 看板的「人话 / 人话过期」判定**静默退化成只有 human**。
    教训：扫角色比较要连 **SQL 谓词**一起扫。
    """
    voices = set(HUMAN_NAMES)
    voices.update(
        row["name"]
        for row in conn.execute("SELECT name FROM agents WHERE role IN ('manager', 'coordinator')")
    )
    return voices


def _case_view(conn: sqlite3.Connection, row: sqlite3.Row, viewer: str | None = None) -> dict:
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
    try:
        milestones = _milestones(row["milestones"])
    except (MailError, TypeError, KeyError):
        milestones = []
    # 「人话」（第 1 条线）：coordinator 写的最后一行节点。**不新增字段**——
    # 机器只做一件事：看这行之后还有没有别人的节点（有 ⇒ 人话过期，该重写了）。
    voices = _human_voices(conn)
    human = None
    for node in reversed(nodes):
        if node.get("agent") in voices and not node.get("auto"):
            human = node
            break
    human_stale = bool(human and nodes and nodes[-1].get("id") != human.get("id"))
    # 停滞黄灯：未封卷 + 最后一个节点超过 STALL_HOURS 没动 ⇒ 看板标黄（**只提示**，程序不处置）。
    stalled_hours = 0.0
    last_at = (nodes[-1].get("created_at") if nodes else row["created_at"]) or ""
    moment = _parse_utc(last_at)
    if moment is not None:
        stalled_hours = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 3600)
    stalled = bool(not sealed and moment is not None and stalled_hours >= STALL_HOURS)
    header_line = f"状态：{status_display}" + ("（已封卷）" if sealed else "")
    lines = [header_line, *(n["line"] for n in nodes)]
    # 「有实质内容」= 有人写过验收 / 里程碑 / WBS 之一。**任何 thread 都能当案卷**，
    # 所以信（派单、回信、报告）也会出现在案卷列表里。它只是个**标记**（看板拿它打 `[信]`），
    # **不是过滤条件**——实测那堆里有真活（派单线程可以是"在办"、有 5 封信、三样全无）；
    # 看板默认折的是**已封卷**（归档；实测 100 条里 52 条是它）。
    substantive = bool((row["acceptance"] or "").strip() or milestones or (row["wbs_path"] or "").strip())
    # 发给请求者自己、还没签收的信。别人那份不算（他签不了，拿它卡人＝死锁）。
    # 最早一封的时间与来信人一并带上，CLI 黄灯才能印「最早 / 来自」。零新列，现查。
    unread_hits = _own_unread_replies(conn, row["thread_id"], viewer or "")
    earliest = unread_hits[0] if unread_hits else None
    earliest_at = str(earliest["created_at"] or "") if earliest is not None else ""
    earliest_from = str(earliest["from_agent"] or "") if earliest is not None else ""
    return {
        "substantive": substantive,
        "unread_replies": len(unread_hits),
        "unread_replies_earliest": earliest_at,
        "unread_replies_earliest_beijing": beijing_time(earliest_at) if earliest_at else "",
        "unread_replies_from": earliest_from,
        "id": row["id"],
        "thread_id": row["thread_id"],
        "title": row["title"],
        "workspace": row["workspace"],
        "status": status,
        "status_label": status_display,
        "sealed": sealed,
        "owner_agent": row["owner_agent"],
        "owner_window": row["owner_window"],
        "waiting_for": row["waiting_for"],
        "wbs_path": row["wbs_path"] or "",
        # 「怎样算完」：空 = 这事没有说清做成什么算做成（看板标出来，不拦）
        "acceptance": row["acceptance"] or "",
        "human_note": (human or {}).get("note", ""),
        "human_by": (human or {}).get("agent", ""),
        "human_at": (human or {}).get("created_at", ""),
        "human_at_beijing": (human or {}).get("created_at_beijing", ""),
        # 人话过期 = 这行之后又发生了别的事 ⇒ 看板上的说法可能已经不准，该由 coordinator 重写
        "human_stale": human_stale or (human is None and bool(nodes)),
        "stalled": stalled,
        "stalled_hours": round(stalled_hours, 1),
        "milestones": milestones,
        "milestones_total": len(milestones),
        "milestones_done": sum(1 for item in milestones if item["done"]),
        "created_at": row["created_at"],
        "created_at_beijing": beijing_time(row["created_at"]),
        "updated_at": row["updated_at"],
        "updated_at_beijing": beijing_time(row["updated_at"]),
        "closed_at": row["closed_at"],
        "node_count": len(nodes),
        "last_note": nodes[-1]["note"] if nodes else "",
        "last_node": nodes[-1] if nodes else None,
        "nodes": nodes,
        "text": "\n".join(lines),
    }


def _window_summary(row: sqlite3.Row) -> dict:
    state = row["state"]
    return {
        "window_id": row["window_id"],
        "agent": row["agent"],
        "workspace": row["workspace"],
        "state": state,
        "state_label": WINDOW_STATE_LABELS.get(state, state),
        "activity": row["activity"],
        "activity_at": row["activity_at"],
        "activity_at_beijing": beijing_time(row["activity_at"]) if row["activity_at"] else "",
        "pid": row["pid"],
        "pid_alive": pid_alive(row["pid"], policy="running"),
        "first_seen_at": row["first_seen_at"],
        "first_seen_beijing": beijing_time(row["first_seen_at"]),
        "last_seen_at": row["last_seen_at"],
        "last_seen_beijing": beijing_time(row["last_seen_at"]),
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


def _is_party(conn: sqlite3.Connection, row: sqlite3.Row, agent: str) -> bool:
    if _same_agent(row["from_agent"] or "", agent):
        return True
    names = _recipient_names(agent)
    marks = ",".join("?" for _ in names)
    found = conn.execute(
        f"SELECT 1 FROM deliveries WHERE message_id = ? AND to_agent IN ({marks})",
        (row["id"], *names),
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


WBS_MAX = 200
MILESTONE_MAX = 200
MILESTONE_MAX_COUNT = 50


def _wbs_path(value) -> str:
    """WBS 树路径：`/` 分隔（如 `防混写/锁与闸`）。空 = 未归类（老案卷只归档不转）。"""
    if value is None:
        return ""
    text = str(value).strip().strip("/")
    if not text:
        return ""
    parts = [part.strip() for part in text.split("/")]
    if any(not part for part in parts):
        raise MailError("invalid_wbs", "WBS 路径不能有空段（如 a//b）")
    joined = "/".join(parts)
    if len(joined) > WBS_MAX:
        raise MailError("invalid_wbs", f"WBS 路径太长（≤ {WBS_MAX}）")
    return joined


def _milestones(value) -> list[dict]:
    """里程碑：案卷上**一列 JSON**（不建表）。`[{"name","owner","done"}]`，只有 coordinator 能改。"""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except ValueError:
            raise MailError("invalid_milestones", "里程碑必须是 JSON 数组")
    else:
        data = value
    if not isinstance(data, (list, tuple)):
        raise MailError("invalid_milestones", "里程碑必须是 JSON 数组")
    if len(data) > MILESTONE_MAX_COUNT:
        raise MailError("invalid_milestones", f"里程碑太多（≤ {MILESTONE_MAX_COUNT} 条）")
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise MailError("invalid_milestones", "每个里程碑都必须有名字 name")
        out.append(
            {
                "name": str(item["name"]).strip()[:MILESTONE_MAX],
                "owner": str(item.get("owner") or "").strip(),
                "done": bool(item.get("done")),
            }
        )
    return out


def _milestones_json(items) -> str:
    return json.dumps(_milestones(items), ensure_ascii=False, sort_keys=True)


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
    if text in {"manager", "coordinator"}:   # coordinator 是**旧名**，一律记成 manager
        return "manager"
    raise MailError("invalid_role", "role 只能是空（成员）或 manager")


def _activity(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailError("invalid_activity", "activity 必须是字符串")
    text = " ".join(value.split())
    if len(text) > 200:
        raise MailError("invalid_activity", "activity 太长了，200 字以内说清楚你在做什么")
    return text
