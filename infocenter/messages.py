"""信息中心消息队列模块（Message Queue & Mail Delivery）。

负责信件投递（send / reply）、收件箱与未读视图（inbox / get_message / thread / sent / agent_messages）、
签收与归档（mark）、长轮询通知流（events / _events_snapshot）以及跨节点远端同步导入（import_remote / apply_receipt）。
从 store.py 解耦为独立业务结构 MessagesMixin。
"""

import sqlite3
import time
from pathlib import Path

from infocenter.common import (
    HUMAN,
    MailError,
    _body,
    _collapse_alias_deliveries,
    _is_party,
    _is_wire_name,
    _limit,
    _pid_value,
    _priority,
    _re_subject,
    _recipient_names,
    _reply_to,
    _resolve_own_delivery,
    _same_agent,
    _subject,
    _upsert_agent,
    _upsert_project,
    _workspace,
    _workspace_scope,
    beijing_time,
    is_person,
    new_id,
    normalize_case_id,
    normalize_name,
    normalize_window,
    parse_recipients,
    preview,
    utcnow,
)
from infocenter.cases import (
    SEALED_STATUSES,
    _find_case,
    _insert_node,
)
from infocenter.windows import (
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
    _window_workspace,
)


def _exact_send_id(
    conn: sqlite3.Connection,
    sender: str,
    reply_to: str | None,
    subject: str,
    body: str,
) -> str | None:
    """同一发件人、同一 reply_to、同一主题、正文逐字节相同的最早一封。"""
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
    """客户端 cwd 命中的未归档 project。不读进程 cwd。"""
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


def _known_names(conn: sqlite3.Connection, home=None) -> set[str]:
    """在册的身份 = 库里注册过的 ∪ 名册（config.json）里列着的。"""
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


def _unread_counts(conn: sqlite3.Connection, window_id: str | None = None) -> dict[str, int]:
    """M1-data：`ic agents` 未读角标与 `events.unread_total` 同口径。"""
    rows = conn.execute(
        """
        SELECT d.message_id AS message_id, d.to_agent AS to_agent,
               d.claimed_by AS claimed_by, m.thread_id AS thread_id
        FROM deliveries d
        JOIN messages m ON m.id = d.message_id
        WHERE d.status = 'unread'
        """
    ).fetchall()
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


def _on_message_sent_case_update(
    conn: sqlite3.Connection,
    thread_id: str,
    sender: str,
    recipients: list[str],
    window_id: str | None,
    created: str,
) -> None:
    """发信生命周期钩子：若线程已立案且未封卷，按收发方向自动推进案卷 waiting/open 状态与流水节点。"""
    case_row = _find_case(conn, thread_id)
    if case_row is None or case_row["sealed"] or case_row["status"] in SEALED_STATUSES:
        return
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


class MessagesMixin:
    """消息队列业务混入类：提供信件投递、收件箱、会话树、事件长轮询与跨节点导入回执。"""

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
        """等新信。since 是上次返回的 deliveries rowid 游标；wait 秒内没有新信就返回现状。"""
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
        """M1：只取最旧的 limit 条，cursor = 实际返回的最大 delivery_seq（无则 = since）。"""
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
            self._lazy_reconcile_all(conn, now)
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
            _upsert_agent(conn, sender, None)
            now = utcnow()
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
            replay_id = _exact_send_id(conn, sender, parent_id, subject, body)
            if replay_id is not None:
                message_id = replay_id
            else:
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
                        _upsert_agent(conn, name, None)
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
                    _on_message_sent_case_update(
                        conn, final_thread_id, sender, recipients, window_id, created
                    )
        if replay_id is None:
            with self._session(immediate=True) as conn:
                _release_stale_claims(conn)
            self._signal()
        result = self.get_message(sender, message_id, full=True)
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
        """B3 §10：从网盘 JSON 导入他机消息。按 id + 不可变字段幂等，不 upsert 发信方。"""
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
        """B3 §10：应用他机已读回执。仅当 to_agent==by 且当前为 unread 时标成 read。"""
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
            _upsert_agent(conn, agent, None)
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
                self._lazy_reconcile_all(conn, now)
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
            if workspace is not None:
                rows = [row for row in rows if _workspace_scope(row["workspace"], workspace)]
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
            target = delivery["to_agent"]
            now = utcnow()
            read_at = delivery["read_at"] or now
            if window_id is None:
                if status == "read":
                    _mark_own_delivery_read(conn, message_id, agent, now)
                else:
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
                view["parent_id"] = row["reply_to"]
                view["depth"] = depth_map[row["id"]]
                view["root_id"] = root_map[row["id"]]
                view["children"] = children_map[row["id"]]
                if _same_agent(row["from_agent"], agent):
                    view["recipient_receipts"] = _delivery_receipts(conn, row["id"])
                messages.append(view)
        return {"thread_id": thread_id, "count": len(messages), "messages": messages}

    def sent(self, agent: str, limit: int = 20) -> dict:
        """只读。我发出的信，以及每个收件人的未读/已读时间和是否已在本线程回过。"""
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
