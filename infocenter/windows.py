"""信息中心办公室管理模块（Windows & Project Workspaces）。

负责办公室（会话/窗口）的登记、心跳刷新、进程对账、离场清理、暂停/恢复与惰性对账。
从 store.py 解耦，作为 Layer 1 业务 Mixin，保持纯净 DAG。
"""

import sqlite3
import sys
from pathlib import Path

from infocenter.common import (
    CLAIM_TTL,
    HUMAN,
    HUMAN_NAMES,
    NAME_RE,
    WINDOW_RE,
    MailError,
    _emit_task_event,
    _is_thread_party,
    _is_window_gone,
    _limit,
    _pid_value,
    _recipient_names,
    _resolve_own_delivery,
    _same_agent,
    _upsert_agent,
    _upsert_project,
    _workspace,
    _workspace_scope,
    beijing_hhmm,
    beijing_time,
    is_person,
    new_window_id,
    normalize_name,
    normalize_window,
    pid_alive,
    utcnow,
)
from infocenter.cases import (
    CASE_STATUS_LABELS,
    SEALED_STATUSES,
    _find_case,
    _insert_node,
    _lazy_reconcile_case,
    _must_unsealed,
    _node_note,
    _orphan_window_cases,
)
from infocenter.locks import (
    _lock_expiry,
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
                _upsert_agent(conn, agent_name, None)
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


    def _lazy_reconcile_all(self, conn: sqlite3.Connection, now: str | None = None) -> None:
        store_mod = sys.modules.get("infocenter.store")
        fn = getattr(store_mod, "_lazy_reconcile_all", _lazy_reconcile_all)
        fn(conn, now)

    def _upsert_window(
        self,
        conn: sqlite3.Connection,
        window_id: str,
        agent: str,
        workspace: str | None = None,
        activity: str | None = None,
        pid: int | None = None,
        now: str | None = None,
        state: str | None = None,
        revive: bool = False,
    ) -> None:
        _upsert_window(
            conn,
            window_id=window_id,
            agent=agent,
            workspace=workspace,
            activity=activity,
            pid=pid,
            now=now,
            state=state,
            revive=revive,
        )

    def _propagate_thread_claim(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        agent: str,
        window_id: str,
        now: str,
        overwrite: bool = False,
    ) -> None:
        _propagate_thread_claim(conn, thread_id, agent, window_id, now, overwrite=overwrite)

    def _claim_thread_on_touch(
        self,
        conn: sqlite3.Connection,
        agent: str,
        window_id: str,
        message_row: sqlite3.Row,
        force: bool,
        mark_read: bool,
        now: str,
    ) -> None:
        _claim_thread_on_touch(
            conn,
            agent=agent,
            window_id=window_id,
            message_row=message_row,
            force=force,
            mark_read=mark_read,
            now=now,
        )


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

