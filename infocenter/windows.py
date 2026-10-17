"""信息中心办公室管理模块（Windows & Project Workspaces）。

负责办公室（会话/窗口）的登记、心跳刷新、进程对账、离场清理、暂停/恢复与惰性对账。
从 store.py 解耦，作为 Layer 1 业务 Mixin，保持纯净 DAG。
"""

import sqlite3
from pathlib import Path

from infocenter.common import (
    HUMAN,
    HUMAN_NAMES,
    NAME_RE,
    WINDOW_RE,
    MailError,
    _limit,
    _same_agent,
    _workspace,
    _workspace_scope,
    beijing_hhmm,
    beijing_time,
    new_window_id,
    normalize_name,
    normalize_window,
    pid_alive,
    _pid_value,
    utcnow,
    _upsert_project,
)
from infocenter.cases import (
    CASE_STATUS_LABELS,
    _orphan_window_cases,
    _insert_node,
    _node_note,
    _lazy_reconcile_case,
)
from infocenter.governance import (
    _release_window_locks,
)

WINDOW_STATE_LABELS = {
    "active": "在办",
    "paused": "暂停",
    "left": "离场",
    "offline": "离场",
}


def _activity(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MailError("invalid_activity", "activity 必须是字符串")
    text = " ".join(value.split())
    if len(text) > 200:
        raise MailError("invalid_activity", "activity 太长了，200 字以内说清楚你在做什么")
    return text


def _workspace_compatible(message_workspace: str | None, window_workspace: str | None) -> bool:
    """信的 workspace 与窗口的 workspace 是否相容。任一侧为空 ⇒ 相容。"""
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


def _auto_claim_allowed(conn: sqlite3.Connection, window_id: str, message_workspace: str | None) -> bool:
    """长轮询只自动认领「属于本窗口 workspace」的信。"""
    if not (message_workspace or "").strip():
        return False
    return _workspace_compatible(message_workspace, _window_workspace(conn, window_id))


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


class WindowsMixin:
    """办公室与窗口生命周期管理 Mixin。"""

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
                if hasattr(self, "_upsert_agent"):
                    self._upsert_agent(conn, agent_name, None)
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
            self._lazy_reconcile_all(conn, now)
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
            self._lazy_reconcile_all(conn, now)
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
            self._lazy_reconcile_all(conn, now)
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
            conn.execute(
                "UPDATE deliveries SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by = ?",
                (wid,),
            )
            _release_window_locks(conn, wid, now, "窗口离场")
            orphaned_ids = _orphan_window_cases(conn, window_id=wid, agent=agent_name, now=now)
            if note:
                for cid in orphaned_ids:
                    _insert_node(
                        conn, case_id=cid, agent=agent_name, window_id=wid,
                        action="note", note=note, status="", auto=bool(auto), now=now,
                    )
            win_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (wid,)).fetchone()
            view = _window_view(conn, win_row)
            view["orphaned_cases"] = orphaned_ids
            view["aborted_cases"] = []
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

    def _lazy_reconcile_all(self, conn: sqlite3.Connection, now: str | None = None) -> None:
        from infocenter.store import _lazy_reconcile_all

        _lazy_reconcile_all(conn, now)
