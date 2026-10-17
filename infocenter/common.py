"""信息中心基础常量、异常与通用工具函数（无依赖层）。"""

import ast
import os
import re
import secrets
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


class MailError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# 人的身份：现名 manager，旧名 human 永久只认
HUMAN = "manager"
HUMAN_NAMES = frozenset({"manager", "human"})

# 正则与保留字
NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")
WINDOW_RE = re.compile(r"^[\w][\w.:-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BROADCAST = {"*", "all", "everyone"}

# 全项目统一时区：北京时间（UTC+8，固定偏移）
BEIJING = timezone(timedelta(hours=8))


def utcnow() -> str:
    """存储用：UTC ISO（...Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ago(minutes: float) -> str:
    """minutes 分钟之前的 UTC ISO。"""
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
    """给人/agent 看的时间：UTC ISO → 北京时间，固定 +0800。"""
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
    """当前北京时间（给人看的日志戳）：2026-10-15 16:29:01。"""
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
    """收件箱和往来列表要算进来的落库收件名。"""
    agent = normalize_name(agent)
    if not is_person(agent):
        return (agent,)
    return tuple(sorted(HUMAN_NAMES, key=lambda name: (name != HUMAN, name)))


def _own_delivery_fallback_names(agent: str) -> tuple[str, ...]:
    """本名那一行不在时才看的别名。人只有另一个名字；别人没有。"""
    agent = normalize_name(agent)
    if not is_person(agent):
        return ()
    return tuple(name for name in _recipient_names(agent) if name != agent)


def _is_wire_name(value: str) -> bool:
    """带一个点、且两段都是合法节点名：合法。"""
    if not isinstance(value, str):
        return False
    name = value.strip().lower()
    left, dot, right = name.partition(".")
    if not dot or "." in right:
        return False
    if not _SEGMENT_RE.fullmatch(left) or not _SEGMENT_RE.fullmatch(right):
        return False
    return bool(NAME_RE.fullmatch(name)) and ".." not in name


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
        raise MailError(
            "invalid_window",
            "窗口号需为 1–64 位字母、数字、_、-、. 或 :",
        )
    return text


def _subject(value: str) -> str:
    if not isinstance(value, str):
        raise MailError("invalid_subject", "主题必须是字符串")
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        raise MailError("invalid_subject", "主题不能为空，不能换行，最长 200 字")
    return text


def _workspace(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError("invalid_workspace", "workspace 必须是字符串")
    text = value.strip()
    if not text:
        return None
    if len(text) > 500 or "\n" in text or "\r" in text:
        raise MailError("invalid_workspace", "workspace 最长 500 字，不能换行")
    return text


def _limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_limit", "limit 必须是整数") from None
    if number < 1 or number > 100:
        raise MailError("invalid_limit", "limit 必须在 1 到 100 之间")
    return number


def _workspace_scope(value: str | None, scope: str | None) -> bool:
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
    left_bare = bool(text) and left.name == text
    right_bare = bool(target) and right.name == target
    if not (left_bare or right_bare or (left.is_absolute() and right.is_absolute())):
        return False
    return bool(right.name) and left.name.casefold() == right.name.casefold()


# ── 统一治理参数配置（支持环境变量覆盖）──────────────────────────────
LOCK_TTL = int(os.environ.get("INFOCENTER_LOCK_TTL", 1800))
LOCK_TTL_MAX = int(os.environ.get("INFOCENTER_LOCK_TTL_MAX", 12 * 3600))
CLAIM_TTL = int(os.environ.get("INFOCENTER_CLAIM_TTL", 7200))
STALL_HOURS = int(os.environ.get("INFOCENTER_STALL_HOURS", 24))
SWEEP_INTERVAL = float(os.environ.get("INFOCENTER_SWEEP_INTERVAL", 60.0))
TASK_ACCEPTANCE_MAX = int(os.environ.get("INFOCENTER_TASK_ACCEPTANCE_MAX", 200))
CASE_ACCEPTANCE_MAX = int(os.environ.get("INFOCENTER_CASE_ACCEPTANCE_MAX", 400))
TASK_TEXT_MAX = int(os.environ.get("INFOCENTER_TASK_TEXT_MAX", 2000))
TASK_HANDOFF_MAX = int(os.environ.get("INFOCENTER_TASK_HANDOFF_MAX", 4000))


def _pid_kill_alive(pid: int, *, permission_counts_alive: bool) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32" and not hasattr(os.kill, "mock_calls"):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            if kernel32.GetLastError() == 5:
                return permission_counts_alive
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return permission_counts_alive
    except OSError:
        return False
    return True


def pid_alive(pid, *, policy: str) -> bool:
    """进程是否按调用方的口径算「还在」。四套旧判断收成这一处，口径必须写明。"""
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
    for fallback in _own_delivery_fallback_names(agent):
        row = conn.execute(
            """
            SELECT message_id, to_agent, status, read_at, claimed_by, claimed_at
            FROM deliveries
            WHERE message_id = ? AND to_agent = ?
            """,
            (message_id, fallback),
        ).fetchone()
        if row is not None:
            return row
    return None


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


def _upsert_agent(conn: sqlite3.Connection, name: str, description: str | None = None) -> None:
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


def duplicate_module_level_defs(package: Path) -> dict[str, list[str]]:
    """模块级函数同名出现在两个文件里。类方法不算。"""
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted(Path(package).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[node.name].append(path.name)
    return {name: files for name, files in sorted(found.items()) if len(files) > 1}


def duplicate_store_mixin_methods(package: Path) -> dict[str, list[str]]:
    """Store 类与 Mixin 基类（GovernanceMixin, CasesMixin, WindowsMixin）之间同名方法检测。"""
    target_classes = {
        "store.py": "Store",
        "governance.py": "GovernanceMixin",
        "cases.py": "CasesMixin",
        "windows.py": "WindowsMixin",
    }
    methods: dict[str, list[str]] = defaultdict(list)
    for filename, cls_name in target_classes.items():
        file_path = Path(package) / filename
        if not file_path.exists():
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[item.name].append(f"{cls_name} ({filename})")
    return {name: locs for name, locs in sorted(methods.items()) if len(locs) > 1}

