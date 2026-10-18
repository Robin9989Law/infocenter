"""信息中心 · 任务队列与留痕审计模块（Task Queue, Sweep & Audit）。

负责：
1. 任务队列（Tasks）：严格状态机（todo/doing/blocked/done/stopped），终态不可逆，supersedes 单向成链，停任务强制交接。
2. 留痕与双重外锚审计（Audit）：所有治理变动同一事务写入 task_events 只增哈希链，重放一致性校验，盘上锚与 Git HEAD 双锚见证。
3. 探针与死流程回收（Probe & Sweep）：产出统计、过期锁清扫、离场/崩溃会话释放写锁并留痕。
文件写锁、排队意图与提交闸门已解耦至 infocenter/locks.py（LocksMixin）。
"""

import json
import sqlite3

from infocenter.common import (
    MailError,
    TASK_ACCEPTANCE_MAX,
    TASK_HANDOFF_MAX,
    _emit_task_event,
    _is_coordinator,
    _limit,
    _must_coordinator,
    _same_agent,
    _subject,
    _task_event_hash,
    _task_text,
    _upsert_agent,
    _workspace,
    _workspace_scope,
    beijing_time,
    new_id,
    normalize_case_id,
    normalize_name,
    utcnow,
)

TASK_STATUSES = ("todo", "doing", "blocked", "stopped", "done")
TASK_TERMINAL = ("stopped", "done")

# 状态机合法流转规则
# owner 含本人与 coordinator；coord 仅 coordinator。不允许暂停/回退/换规格。
_TASK_OWNER_MOVES = {("todo", "doing"), ("doing", "blocked"), ("blocked", "doing")}
_TASK_COORD_MOVES = {
    ("todo", "stopped"),   # 没开工就放弃（现实里常见）
    ("doing", "done"),
    ("blocked", "done"),
    ("doing", "stopped"),
    ("blocked", "stopped"),
}

PROBE_MACHINERY_ACTIONS = {
    "lock", "unlock", "lock_expire", "claim", "claim_release", "offer", "offer_expired",
}

TASK_EVENT_FIELDS = (
    "workspace", "case_id", "owner", "title", "status",
    "blocked_reason", "deps", "acceptance", "handoff", "supersedes", "priority",
)


def _task_status(value) -> str:
    text = str(value or "").strip().lower()
    if text not in TASK_STATUSES:
        raise MailError(
            "invalid_task_status", f"任务状态只能是 {'/'.join(TASK_STATUSES)}", 400
        )
    return text


def _task_priority(value) -> int:
    """优先级：整数（信只能 bump 它；越大越靠前）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise MailError("invalid_priority", "priority 必须是整数", 400) from None
    return max(-99, min(n, 99))


def _check_handoff(value) -> str:
    """停任务的交接段：一列自由文本，命令层只要求非空。"""
    text = _task_text(value, TASK_HANDOFF_MAX)
    if not text:
        raise MailError(
            "handoff_required",
            "停任务必须写交接段（做到哪 / 产物在哪 / 别踩什么坑）；不写不许停",
            400,
        )
    return text


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


class GovernanceMixin:
    """任务队列与留痕审计混入类。"""

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
        """建任务：只有 coordinator，且必须带 workspace。"""
        from infocenter.cases import _find_case

        agent = normalize_name(agent)
        title_text = _subject(title if title is not None else "")
        workspace = _workspace(workspace)
        if not workspace:
            raise MailError("workspace_required", "任务必须带 workspace（否则 --mine 会串项目）", 400)
        owner_name = normalize_name(owner or agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            _must_coordinator(conn, agent, workspace)
            _upsert_agent(conn, owner_name, None)
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
        """列表：默认只列活线；被取代的折叠进 superseded。"""
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
        """详情：含完整 supersedes 链。"""
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
        """开工：todo → doing。"""
        agent = normalize_name(agent)
        now = utcnow()
        with self._session(immediate=True) as conn:
            row = _find_task(conn, task_id)
            if row["status"] != "todo":
                raise MailError("invalid_transition", f"只有 todo 能开工（当前 {row['status']}）", 409)
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
        """改状态：owner 可 doing↔blocked；done/stopped 仅 coordinator；stopped 必带交接段。"""
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
        """改规格：仅 coordinator，且只在 todo。"""
        from infocenter.cases import _find_case

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
                    _upsert_agent(conn, value, None)
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
            _emit_task_event(
                conn, row["id"], agent, "update", after["status"], after["status"],
                {key: ("" if after[key] is None else after[key]) for key in patch},
                now,
            )
            res = _task_view(after)
        self._signal()
        return res

    def task_bump(self, agent: str, task_id: str, by: int = 1) -> dict:
        """调优先级。仅 coordinator。"""
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

    def probe_counts(self, since: str) -> dict:
        """窗口内每家的产出与机械动作计数。"""
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

    def sweep(self, now: str | None = None) -> dict:
        """死流程回收：过期锁、死进程窗口、僵尸认领。全部留痕。"""
        from infocenter.locks import _locks_sweep
        from infocenter.windows import _mark_dead_windows, _release_stale_claims

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
        """机器验证留痕：① 哈希链完整；② 每个任务重放痕迹 == 当前行；③ 外锚核验。"""
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
