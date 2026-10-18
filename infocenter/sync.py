"""网盘 JSON 中继。ic sync 直接读写本地邮箱，不拉起服务。

消息和回执的写入走 Store.import_remote / Store.apply_receipt（cursor 的 §10）。
这里不自己 INSERT messages。
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path

from infocenter import config
from infocenter.common import (
    MailError,
    _SEGMENT_RE,
    _body,
    _is_wire_name,
    _priority,
    _subject,
    _workspace_scope,
    atomic_replace,
    utcnow,
)
from infocenter.store import Store

MAX_ENVELOPE = 1_000_000

ID_RE = re.compile(r"^m[0-9a-f]{8,40}$")
MESSAGE_FILE_RE = re.compile(r"^(m[0-9a-f]{8,40})\.json$")
RECEIPT_FILE_RE = re.compile(
    r"^(m[0-9a-f]{8,40})--([\w][\w-]{0,31})\.json$",
    re.UNICODE,
)
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_COPY_RE = re.compile(r"\(\d+\)")

COUNT_KEYS = (
    "exported",
    "imported",
    "receipts_out",
    "receipts_in",
    "skipped",
    "rejected",
    "conflict",
)


def is_wire_name(value) -> bool:
    """恰好一个点、左右都是合法节点段，且整串合法。"""
    return _is_wire_name(value)


def canonical_wire(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if not is_wire_name(name):
        return None
    return name


def run_sync(home, project, mode: str) -> dict:
    if mode not in {"export", "import", "once"}:
        raise MailError("invalid_sync", "sync 只能是 export、import 或 once")
    home = Path(home).expanduser()
    project = _require_project(project)
    settings = config.federation_settings(home)
    node = _require_node(settings.get("node"))
    peers = _peer_list(settings.get("peers"))
    outbox = _outbox_root(project, settings.get("outbox_dir"))
    counts = {key: 0 for key in COUNT_KEYS}
    ledger = _Ledger(home)
    try:
        with _federation_lock(home):
            _sweep_tmp(outbox / node)
            if mode in {"export", "once"}:
                _export_messages(home, project, node, outbox, counts, ledger)
                _export_receipts(home, project, node, outbox, counts, ledger)
            if mode in {"import", "once"}:
                getter = _store_getter(home)
                _import_messages(project, node, outbox, peers, counts, ledger, getter)
                _import_receipts(node, outbox, peers, counts, ledger, getter)
    finally:
        ledger.close()
    return counts


def _require_project(project) -> Path:
    path = Path(project).expanduser()
    if not path.is_dir():
        raise MailError("invalid_dir", f"没有这个目录：{path}")
    return path.resolve()


def _require_node(node) -> str:
    if node is None or node == "":
        raise MailError("node_not_configured", "没有配置 node，ic sync 不会建目录也不会写库")
    if not isinstance(node, str):
        raise MailError("invalid_node", "node 必须是字符串")
    text = node.strip().lower()
    if not text:
        raise MailError("node_not_configured", "没有配置 node，ic sync 不会建目录也不会写库")
    if not _SEGMENT_RE.fullmatch(text):
        raise MailError("invalid_node", "node 不合法：必须是 1–32 位字母、数字、_ 或 -，不能有点")
    return text


def _peer_list(value) -> list[str]:
    if value is None or value == []:
        return []
    if not isinstance(value, list):
        raise MailError("invalid_peers", "peers 必须是节点段列表")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MailError("invalid_peers", "peers 里的节点段必须是字符串")
        name = item.strip().lower()
        if not name:
            continue
        if not _SEGMENT_RE.fullmatch(name):
            raise MailError("invalid_peers", f"peers 里有非法节点段：{item}")
        if name not in names:
            names.append(name)
    return names


def _outbox_root(project: Path, value) -> Path:
    if value is None or value == "":
        root = project / ".infocenter" / "outbox"
    elif not isinstance(value, str):
        raise MailError("invalid_outbox", "outbox_dir 必须是路径字符串")
    else:
        path = Path(value).expanduser()
        root = path if path.is_absolute() else project / path
    if root.exists() and not root.is_dir():
        raise MailError("invalid_outbox", f"outbox_dir 不是目录：{root}")
    return root


def _federation_lock(home: Path):
    from contextlib import contextmanager

    @contextmanager
    def _locked():
        home.mkdir(parents=True, exist_ok=True)
        from infocenter.flock import lock_file_ex_nb, unlock_file

        handle = open(home / "federation.lock", "a+")
        acquired = False
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    lock_file_ex_nb(handle)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MailError("sync_busy", "另一路 ic sync 还占着锁，等了 30 秒") from None
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                unlock_file(handle)
            handle.close()

    return _locked()


def _sweep_tmp(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink() or not directory.is_dir():
        return
    _sweep_dir(directory)
    receipts = directory / "receipts"
    if receipts.is_dir() and not receipts.is_symlink():
        _sweep_dir(receipts)


def _sweep_dir(directory: Path) -> None:
    cutoff = time.time() - 3600
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            continue
        if not (entry.name.startswith(".") and entry.name.endswith(".tmp")):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


class _Ledger:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "federation.sqlite"
        self.conn = None

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def event(self, action: str, path: str, digest: str | None, message_id: str | None) -> None:
        self._insert(action, path, digest, None, None, None, message_id)

    def reject_hash(self, path: str, reason: str, digest: str, message_id: str | None = None) -> bool:
        row = self._latest(path, reason)
        if row is not None and row["sha256"] == digest:
            return False
        self._insert("reject", path, digest, None, None, reason, message_id)
        return True

    def reject_stat(self, path: str, reason: str, size: int, mtime_ns: int) -> bool:
        row = self._latest(path, reason)
        if row is not None and row["size"] == size and row["mtime_ns"] == mtime_ns:
            return False
        self._insert("reject", path, None, size, mtime_ns, reason, None)
        return True

    def _latest(self, path: str, reason: str):
        if self.conn is None and not self.path.exists():
            return None
        self._ensure()
        return self.conn.execute(
            """
            SELECT sha256, size, mtime_ns FROM events
            WHERE action = 'reject' AND path = ? AND reason = ?
            ORDER BY id DESC LIMIT 1
            """,
            (path, reason),
        ).fetchone()

    def _insert(self, action, path, digest, size, mtime_ns, reason, message_id) -> None:
        self._ensure()
        self.conn.execute(
            """
            INSERT INTO events(action, path, sha256, size, mtime_ns, reason, message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (action, path, digest, size, mtime_ns, reason, message_id, utcnow()),
        )
        self.conn.commit()

    def _ensure(self) -> None:
        if self.conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                path TEXT,
                sha256 TEXT,
                size INTEGER,
                mtime_ns INTEGER,
                reason TEXT,
                message_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        self.conn = conn


def _export_messages(home, project, node, outbox, counts, ledger) -> None:
    for row, deliveries in _mailbox_rows(home / "mailbox.db"):
        names = [item["to_agent"] for item in deliveries]
        if not row.get("workspace") or not _workspace_scope(row["workspace"], str(project)):
            continue
        if "." in row["from_agent"]:
            continue
        if not _has_remote(names, node):
            continue
        wire_from = canonical_wire(f"{node}.{row['from_agent']}")
        target = outbox / node / f"{row['id']}.json"
        if wire_from is None:
            _reject_export(ledger, counts, target, row["id"], "invalid_sender")
            continue
        exported_to = _export_to(names, node)
        if exported_to is None:
            _reject_export(ledger, counts, target, row["id"], "invalid_recipient")
            continue
        if not _has_remote(exported_to, node):
            continue
        payload = {
            "v": 1,
            "kind": "message",
            "id": row["id"],
            "thread_id": row["thread_id"],
            "reply_to": row["reply_to"],
            "from": wire_from,
            "to": exported_to,
            "subject": row["subject"],
            "body": row["body"],
            "priority": row["priority"],
            "workspace": row["workspace"],
            "workspace_name": Path(row["workspace"]).name,
            "created_at": row["created_at"],
        }
        _publish(
            outbox / node,
            target.name,
            payload,
            _same_message,
            "exported",
            counts,
            ledger,
            row["id"],
        )


def _export_receipts(home, project, node, outbox, counts, ledger) -> None:
    for row, deliveries in _mailbox_rows(home / "mailbox.db"):
        if not row.get("workspace") or not _workspace_scope(row["workspace"], str(project)):
            continue
        sender = canonical_wire(row["from_agent"])
        if sender is None or sender.split(".", 1)[0] == node:
            continue
        for item in deliveries:
            bare = item["to_agent"]
            if not isinstance(bare, str) or "." in bare or item["status"] != "read":
                continue
            read_at = item["read_at"]
            if not isinstance(read_at, str) or not TIME_RE.fullmatch(read_at):
                continue
            by = canonical_wire(f"{node}.{bare}")
            filename = f"{row['id']}--{bare}.json"
            target = outbox / node / "receipts" / filename
            if by is None:
                _reject_export(ledger, counts, target, row["id"], "invalid_recipient")
                continue
            payload = {
                "v": 1,
                "kind": "receipt",
                "message_id": row["id"],
                "by": by,
                "status": "read",
                "read_at": read_at,
            }
            _publish(
                target.parent,
                filename,
                payload,
                _same_receipt,
                "receipts_out",
                counts,
                ledger,
                row["id"],
            )


def _reject_export(ledger, counts, target: Path, message_id: str, reason: str) -> None:
    digest = hashlib.sha256(f"{reason}:{message_id}:{target.name}".encode()).hexdigest()
    if ledger.reject_hash(str(target), reason, digest, message_id):
        counts["rejected"] += 1


def _has_remote(names, node: str) -> bool:
    for name in names:
        wire = canonical_wire(name)
        if wire is not None and wire.split(".", 1)[0] != node:
            return True
    return False


def _export_to(recipients, node: str) -> list[str] | None:
    out: list[str] = []
    for name in recipients:
        wire = canonical_wire(name)
        if wire is not None:
            if wire.split(".", 1)[0] != node:
                out.append(wire)
            continue
        if not isinstance(name, str) or "." in name:
            continue
        bare_wire = canonical_wire(f"{node}.{name}")
        if bare_wire is None:
            return None
        out.append(bare_wire)
    return sorted(set(out))


def _publish(directory, filename, payload, same, count_key, counts, ledger, message_id) -> None:
    data = _dumps(payload)
    target = directory / filename
    key = str(target)
    if len(data) > MAX_ENVELOPE:
        digest = hashlib.sha256(data).hexdigest()
        if ledger.reject_hash(key, "envelope_too_large", digest, message_id):
            counts["rejected"] += 1
        return
    if target.is_symlink():
        return
    if target.is_file():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            old = None
        if isinstance(old, dict) and same(old, payload):
            return
    _atomic_write(directory, filename, data)
    counts[count_key] += 1
    ledger.event("export", key, hashlib.sha256(data).hexdigest(), message_id)


def _dumps(payload: dict) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _same_message(old: dict, new: dict) -> bool:
    keys = (
        "v",
        "kind",
        "id",
        "thread_id",
        "reply_to",
        "from",
        "subject",
        "body",
        "priority",
        "workspace",
        "workspace_name",
        "created_at",
    )
    if any(old.get(key) != new.get(key) for key in keys):
        return False
    return _string_list(old.get("to")) == _string_list(new.get("to"))


def _same_receipt(old: dict, new: dict) -> bool:
    keys = ("v", "kind", "message_id", "by", "status", "read_at")
    return all(old.get(key) == new.get(key) for key in keys)


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(item for item in value if isinstance(item, str))


def _atomic_write(directory: Path, name: str, data: bytes) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_replace(directory / name, data)


def _mailbox_rows(path: Path):
    if not path.exists():
        return []
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN DEFERRED")
        messages = conn.execute("SELECT * FROM messages").fetchall()
        rows = []
        for row in messages:
            deliveries = conn.execute(
                "SELECT to_agent, status, read_at FROM deliveries WHERE message_id = ?",
                (row["id"],),
            ).fetchall()
            rows.append((dict(row), [dict(item) for item in deliveries]))
        conn.commit()
        return rows
    finally:
        conn.close()


def _import_messages(project, node, outbox, peers, counts, ledger, getter) -> None:
    for peer in _iter_peers(outbox, node, peers):
        for path in _message_files(peer):
            _import_one_message(path, node, project, counts, ledger, getter)


def _import_receipts(node, outbox, peers, counts, ledger, getter) -> None:
    for peer in _iter_peers(outbox, node, peers):
        receipts = peer / "receipts"
        if receipts.is_symlink() or not receipts.is_dir() or not _contained(peer, receipts):
            continue
        try:
            entries = list(receipts.iterdir())
        except OSError:
            continue
        for path in entries:
            if _ignored_name(path.name) or not RECEIPT_FILE_RE.fullmatch(path.name):
                continue
            if not _contained(receipts, path):
                continue
            _import_one_receipt(path, peer, counts, ledger, getter)


def _import_one_message(path, node, project, counts, ledger, getter) -> None:
    st = _regular_stat(path)
    if st is None:
        return
    key = str(path.resolve())
    if st.st_size > MAX_ENVELOPE:
        if ledger.reject_stat(key, "envelope_too_large", st.st_size, st.st_mtime_ns):
            counts["rejected"] += 1
        return
    if st.st_size == 0:
        counts["skipped"] += 1
        return
    try:
        raw = path.read_bytes()
    except OSError:
        counts["skipped"] += 1
        return
    if len(raw) > MAX_ENVELOPE:
        if ledger.reject_stat(key, "envelope_too_large", len(raw), st.st_mtime_ns):
            counts["rejected"] += 1
        return
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        counts["skipped"] += 1
        return
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("kind") != "message":
        if isinstance(data, dict) and (data.get("v") != 1 or data.get("kind") != "message"):
            counts["skipped"] += 1
            return
        _reject_bytes(ledger, counts, key, raw, "invalid_envelope", None)
        return
    stem = path.name[:-5]
    sender = canonical_wire(data.get("from"))
    wires = [canonical_wire(item) for item in data.get("to")] if isinstance(data.get("to"), list) else None
    try:
        subject = _subject(data["subject"]) if isinstance(data.get("subject"), str) else None
        body = _body(data["body"]) if isinstance(data.get("body"), str) else None
        priority = _priority(data["priority"]) if isinstance(data.get("priority"), str) else None
    except MailError:
        subject = body = priority = None
    reply_to = data.get("reply_to")
    thread_id = data.get("thread_id")
    created_at = data.get("created_at")
    valid = (
        data.get("id") == stem
        and isinstance(thread_id, str)
        and ID_RE.fullmatch(thread_id)
        and (reply_to is None or (isinstance(reply_to, str) and ID_RE.fullmatch(reply_to)))
        and sender is not None
        and wires is not None
        and wires
        and all(item is not None for item in wires)
        and subject is not None
        and body is not None
        and priority is not None
        and isinstance(created_at, str)
        and TIME_RE.fullmatch(created_at)
    )
    if not valid:
        _reject_bytes(ledger, counts, key, raw, "invalid_envelope", stem if ID_RE.fullmatch(stem) else None)
        return
    if sender.split(".", 1)[0] == node:
        counts["skipped"] += 1
        return
    names = sorted({item.split(".", 1)[1] for item in wires if item.split(".", 1)[0] == node})
    if not names:
        counts["skipped"] += 1
        return
    envelope = {
        "v": 1,
        "kind": "message",
        "id": stem,
        "thread_id": thread_id,
        "reply_to": reply_to,
        "from": sender,
        "to": sorted(set(wires)),
        "subject": subject,
        "body": body,
        "priority": priority,
        "workspace": str(project),
        "workspace_name": data.get("workspace_name") if isinstance(data.get("workspace_name"), str) else None,
        "created_at": created_at,
    }
    status, parent = _call_import(getter(), envelope, names)
    if status == "imported":
        counts["imported"] += 1
        ledger.event("import", key, hashlib.sha256(raw).hexdigest(), stem)
        label = data.get("workspace_name")
        if isinstance(label, str) and label and label != project.name:
            sys.stderr.write(
                f"warning: {path.name} 的 workspace_name={label} 与目录名 {project.name} 不一致，仍导入\n"
            )
        if parent == "missing":
            sys.stderr.write(
                f"warning: {path.name} 的 parent=missing，回复的父信还不在库里，仍导入\n"
            )
    elif status == "conflict":
        counts["conflict"] += 1
    elif status == "skipped":
        counts["skipped"] += 1
    elif status != "duplicate":
        raise MailError("invalid_sync", f"import_remote 返回了不认识的状态：{status}")


def _import_one_receipt(path, peer, counts, ledger, getter) -> None:
    st = _regular_stat(path)
    if st is None:
        return
    key = str(path.resolve())
    if st.st_size > MAX_ENVELOPE:
        if ledger.reject_stat(key, "envelope_too_large", st.st_size, st.st_mtime_ns):
            counts["rejected"] += 1
        return
    if st.st_size == 0:
        counts["skipped"] += 1
        return
    match = RECEIPT_FILE_RE.fullmatch(path.name)
    if match is None:
        return
    message_id, bare = match.group(1), match.group(2)
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        counts["skipped"] += 1
        return
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("kind") != "receipt":
        if isinstance(data, dict) and (data.get("v") != 1 or data.get("kind") != "receipt"):
            counts["skipped"] += 1
            return
        _reject_bytes(ledger, counts, key, raw, "invalid_envelope", message_id)
        return
    by = canonical_wire(data.get("by"))
    read_at = data.get("read_at")
    left, _, right = (by or "").partition(".")
    valid = (
        data.get("message_id") == message_id
        and by is not None
        and left == peer.name
        and right == bare
        and data.get("status") == "read"
        and isinstance(read_at, str)
        and TIME_RE.fullmatch(read_at)
    )
    if not valid:
        _reject_bytes(ledger, counts, key, raw, "invalid_envelope", message_id)
        return
    status = _call_receipt(getter(), message_id, by, read_at)
    if status == "applied":
        counts["receipts_in"] += 1
        ledger.event("import", key, hashlib.sha256(raw).hexdigest(), message_id)
    elif status == "absent":
        counts["skipped"] += 1
    elif status not in {"duplicate", "archived"}:
        raise MailError("invalid_sync", f"apply_receipt 返回了不认识的状态：{status}")


def _reject_bytes(ledger, counts, key: str, raw: bytes, reason: str, message_id) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    if ledger.reject_hash(key, reason, digest, message_id):
        counts["rejected"] += 1


def _call_import(store: Store, envelope: dict, names: list[str]) -> tuple[str, str | None]:
    method = getattr(store, "import_remote", None)
    if method is None:
        raise MailError(
            "store_api_missing",
            "Store.import_remote 还没合入。这是给 cursor 的 §10，sync 不写 messages 表",
        )
    # U-3：这里只把「本机 node 段的裸名」交给 store（`names`）。`federation-design.md` §7
    # 原说「两份信封之间仍比排序后的完整 to」，但 messages 表没有 to 列、他机裸名不落库，
    # 全量 to 比较不可实现，按 store.import_remote 的 docstring 降级为「本机投递收件人」比较。
    result = method(envelope, names)
    if not isinstance(result, dict) or "status" not in result:
        raise MailError("invalid_sync", "import_remote 没有返回 status")
    parent = result.get("parent")
    if not isinstance(parent, str):
        parent = None
    return result["status"], parent


def _call_receipt(store: Store, message_id: str, by: str, read_at: str) -> str:
    method = getattr(store, "apply_receipt", None)
    if method is None:
        raise MailError(
            "store_api_missing",
            "Store.apply_receipt 还没合入。这是给 cursor 的 §10，sync 不改 deliveries",
        )
    result = method(message_id, by, read_at)
    if not isinstance(result, dict) or "status" not in result:
        raise MailError("invalid_sync", "apply_receipt 没有返回 status")
    return result["status"]


def _store_getter(home: Path):
    holder = {}

    def get() -> Store:
        if "store" not in holder:
            holder["store"] = Store(home / "mailbox.db", seeds=config.seed_pairs(home))
        return holder["store"]

    return get


def _iter_peers(outbox: Path, node: str, peers: list[str]):
    if outbox.is_symlink() or not outbox.is_dir():
        return
    if peers:
        names = [name for name in peers if name != node and _is_node_dir_name(name) and not _ignored_name(name)]
    else:
        names = []
        try:
            children = list(outbox.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name == node or _ignored_name(child.name) or not _is_node_dir_name(child.name):
                continue
            names.append(child.name)
        names.sort()
    for name in names:
        directory = outbox / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not _contained(outbox, directory):
            continue
        yield directory


def _message_files(peer: Path):
    try:
        entries = list(peer.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == "receipts" or entry.is_symlink() or entry.is_dir():
            continue
        if _ignored_name(entry.name) or not MESSAGE_FILE_RE.fullmatch(entry.name):
            continue
        if not _contained(peer, entry):
            continue
        yield entry


def _is_node_dir_name(name: str) -> bool:
    return name == name.lower() and bool(_SEGMENT_RE.fullmatch(name))


def _ignored_name(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    folded = name.casefold()
    if " " in name or "冲突" in name or "conflict" in folded:
        return True
    return _COPY_RE.search(name) is not None


def _contained(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _regular_stat(path: Path):
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None
    return st
