"""信息中心案卷管理模块（Cases & Case Nodes）。

负责案卷的建立、状态流转、WBS 分类、验收标准、里程碑追踪、只增节点链以及案卷认领/转办/封卷/重开。
从 store.py 解耦，作为 Layer 1 业务 Mixin，保持纯净 DAG。
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from infocenter.common import (
    CASE_ACCEPTANCE_MAX,
    CASE_ID_RE,
    HUMAN,
    HUMAN_NAMES,
    NAME_RE,
    STALL_HOURS,
    TASK_TEXT_MAX,
    WINDOW_RE,
    MailError,
    _is_wire_name,
    _limit,
    _parse_utc,
    _same_agent,
    _subject,
    _workspace,
    _workspace_scope,
    beijing_hhmm,
    beijing_time,
    is_person,
    new_id,
    normalize_case_id,
    normalize_name,
    normalize_window,
    pid_alive,
    utcnow,
    _is_window_gone,
    _resolve_own_delivery,
    _upsert_project,
)
from infocenter.governance import (
    _emit_case_event,
    _human_voices,
    _is_coordinator,
    _must_coordinator,
    _task_text,
)

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
    """里程碑：案卷上一列 JSON（不建表）。[{"name","owner","done"}]，只有 coordinator 能改。"""
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


def _own_unread_replies(conn: sqlite3.Connection, thread_id: str, agent: str) -> list[sqlite3.Row]:
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


def _must_unsealed(row: sqlite3.Row) -> None:
    if row["sealed"] or row["status"] in SEALED_STATUSES:
        raise MailError("case_sealed", f"案卷 {row['id']} 已封卷（{row['status']}），只读不可修改", 409)


def _find_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cases WHERE id = ? OR thread_id = ?",
        (case_id, case_id),
    ).fetchone()


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


def _lazy_reconcile_case(conn: sqlite3.Connection, case_row: sqlite3.Row, now: str | None = None) -> sqlite3.Row:
    """崩溃惰性补齐：主办窗口已不在时只摘指针 + 记一行。"""
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
    _orphan_window_cases(conn, window_id=owner_window, agent="system", now=now)
    return conn.execute("SELECT * FROM cases WHERE id = ?", (case_row["id"],)).fetchone()


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
    voices = _human_voices(conn)
    human = None
    for node in reversed(nodes):
        if node.get("agent") in voices and not node.get("auto"):
            human = node
            break
    human_stale = bool(human and nodes and nodes[-1].get("id") != human.get("id"))
    stalled_hours = 0.0
    last_at = (nodes[-1].get("created_at") if nodes else row["created_at"]) or ""
    moment = _parse_utc(last_at)
    if moment is not None:
        stalled_hours = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 3600)
    stalled = bool(not sealed and moment is not None and stalled_hours >= STALL_HOURS)
    header_line = f"状态：{status_display}" + ("（已封卷）" if sealed else "")
    lines = [header_line, *(n["line"] for n in nodes)]
    substantive = bool((row["acceptance"] or "").strip() or milestones or (row["wbs_path"] or "").strip())
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
        "acceptance": row["acceptance"] or "",
        "human_note": (human or {}).get("note", ""),
        "human_by": (human or {}).get("agent", ""),
        "human_at": (human or {}).get("created_at", ""),
        "human_at_beijing": (human or {}).get("created_at_beijing", ""),
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


class CasesMixin:
    """案卷与案卷节点管理 Mixin。"""

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

        if (to is None) != (body is None):
            raise MailError(
                "invalid_request",
                "to 与 body 必须同时提供：这一对用于「立案并同时发始信」；只写开篇节点请用 note",
                400,
            )
        requested = normalize_case_id(thread_id) if thread_id is not None else None

        if to is not None and body is not None:
            sent = self.send(
                sender=agent,
                to=to,
                subject=title_text,
                body=body,
                workspace=workspace,
                thread_id=requested,
                window_id=owner_window,
                pid=pid,
            )
            real_thread_id = sent["thread_id"]
            now = utcnow()
            with self._session(immediate=True) as conn:
                row = _find_case(conn, real_thread_id)
                if row is None:
                    row = _ensure_case_from_thread(conn, real_thread_id, agent, now)
                wbs_clean = _wbs_path(wbs_path) if wbs_path else (row["wbs_path"] or "")
                milestones_clean = _milestones_json(milestones) if milestones is not None else row["milestones"]
                acc_clean = _task_text(acceptance, CASE_ACCEPTANCE_MAX) if acceptance is not None else (row["acceptance"] or "")
                conn.execute(
                    "UPDATE cases SET wbs_path = ?, milestones = ?, acceptance = ?, updated_at = ? WHERE id = ?",
                    (wbs_clean, milestones_clean, acc_clean, now, row["id"]),
                )
                if note:
                    _insert_node(
                        conn,
                        case_id=row["id"],
                        agent=agent,
                        window_id=owner_window,
                        action="note",
                        note=_node_note(note),
                        status=row["status"],
                        auto=False,
                        now=now,
                    )
                updated = _find_case(conn, row["id"])
                return _case_view(conn, updated, viewer=agent)

        now = utcnow()
        case_id = requested or new_id("case")
        thread_id = case_id
        initial_status = _case_status(status) if status else "pending"
        wbs_clean = _wbs_path(wbs_path)
        milestones_clean = _milestones_json(milestones)
        acc_clean = _task_text(acceptance, CASE_ACCEPTANCE_MAX)

        with self._session(immediate=True) as conn:
            if _find_case(conn, case_id) is not None:
                raise MailError("conflict", f"案卷或线程编号已存在：{case_id}", 409)
            if owner_window:
                if hasattr(self, "_upsert_window"):
                    self._upsert_window(
                        conn,
                        window_id=owner_window,
                        agent=owner_agent or agent,
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
                    waiting_for, sealed, created_at, updated_at, closed_at,
                    wbs_path, milestones, acceptance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    case_id,
                    thread_id,
                    title_text,
                    workspace,
                    initial_status,
                    owner_agent,
                    owner_window,
                    now,
                    now,
                    wbs_clean,
                    milestones_clean,
                    acc_clean,
                ),
            )
            _insert_node(
                conn,
                case_id=case_id,
                agent=agent,
                window_id=owner_window,
                action="create",
                note="立案",
                status=initial_status,
                auto=False,
                now=now,
            )
            if note:
                _insert_node(
                    conn,
                    case_id=case_id,
                    agent=agent,
                    window_id=owner_window,
                    action="note",
                    note=_node_note(note),
                    status=initial_status,
                    auto=False,
                    now=now,
                )
            _emit_case_event(
                conn, agent, "create", "", initial_status,
                case_id, owner_window, "立案", now,
            )
            row = _find_case(conn, case_id)
            out = _case_view(conn, row, viewer=agent)
        self._signal()
        return out

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
        wait_filter = normalize_name(waiting_for) if waiting_for else None
        viewer_name = normalize_name(viewer) if viewer else None
        now = utcnow()
        with self._session(immediate=True) as conn:
            if hasattr(self, "_lazy_reconcile_all"):
                self._lazy_reconcile_all(conn, now)
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
                if hasattr(self, "_upsert_window"):
                    self._upsert_window(
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
            if hasattr(self, "_upsert_window"):
                self._upsert_window(conn, window_id=window_id, agent=agent, workspace=None, activity=None, pid=pid, now=now)
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
                    and not self._is_thread_party(conn, row["thread_id"], agent)
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
                    self._propagate_thread_claim(conn, row["thread_id"], agent, window_id, now, overwrite=force)
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
            if not self._is_thread_party(conn, msg["thread_id"], agent):
                raise MailError("not_found", f"没有找到案卷或线程：{case_id}", 404)
            self._claim_thread_on_touch(
                conn,
                agent=agent,
                window_id=window_id,
                message_row=msg,
                force=bool(force),
                mark_read=False,
                now=now,
            )
            created = _find_case(conn, msg["thread_id"])
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
            if hasattr(self, "_upsert_window"):
                self._upsert_window(
                    conn,
                    window_id=target_window,
                    agent=target_agent,
                    workspace=row["workspace"],
                    activity=None,
                    pid=pid if _same_agent(target_agent, agent) else None,
                    now=now,
                )
            note_text = _node_note(note or f"转办 → 交接给 {target_window}")
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
            self._propagate_thread_claim(conn, row["thread_id"], target_agent, target_window, now, overwrite=True)
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
                if hasattr(self, "_upsert_window"):
                    self._upsert_window(
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
                self._propagate_thread_claim(conn, row["thread_id"], new_owner_agent, window_id, now, overwrite=True)
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
        _emit_case_event(
            conn, agent, action, row["status"], new_status,
            row["id"], node_window, note, now,
        )
        updated = _find_case(conn, row["id"])
        return _case_view(conn, updated, viewer=agent)
