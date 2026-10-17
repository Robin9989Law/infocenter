import contextlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from infocenter.httpapi import handle
from infocenter.mcp_server import _call_tool, resolve_mcp_window
from infocenter.server import make_handler
from infocenter.store import MailError, Store


def _http_json(url: str, method: str, path: str, agent: str | None = None, window: str | None = None, body: dict | None = None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    if agent is not None:
        req.add_header("X-Agent", agent)
    if window is not None:
        req.add_header("X-Window", window)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        exc.close()
        return exc.code, json.loads(raw)


class WindowAndCaseQualityTest(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("grok", "Grok", ""),
                ("kimi", "Kimi Code", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    # 1. 单窗口兼容：不传窗口号时，inbox / read / reply / events 行为与现有完全一致
    def test_01_single_window_backward_compatibility(self):
        sent = self.store.send("opencode", ["cursor"], "单窗口任务", "内容A", workspace="/tmp/proj-a")
        ev = self.store.events("cursor", since=0, wait=0)
        self.assertEqual(ev["unread_total"], 1)
        self.assertEqual(len(ev["new"]), 1)
        self.assertIsNone(ev["new"][0]["claimed_by"])

        box = self.store.inbox("cursor", status="unread", full=True)
        self.assertEqual(box["count"], 1)
        self.assertIsNone(box["messages"][0]["claimed_by"])

        ack = self.store.mark("cursor", sent["id"], "read")
        self.assertEqual(ack["delivery_status"], "read")
        self.assertIsNone(ack["claimed_by"])

        rep = self.store.reply("cursor", sent["id"], "已完成")
        self.assertEqual(rep["thread_id"], sent["thread_id"])
        # 未带窗口号且未显式立案时，不强制生成窗口绑定
        self.assertEqual(self.store.list_cases(), [])

        # D1-a: 旧库 schema（无 claimed_by/claimed_at 列、无 cases/windows/projects 表）自动加列/建表迁移且旧数据可读
        legacy_db = self.home / "legacy.db"
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            conn.executescript(
                """
                CREATE TABLE agents (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE messages (
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
                CREATE TABLE deliveries (
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    to_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    read_at TEXT,
                    PRIMARY KEY (message_id, to_agent)
                );
                INSERT INTO agents(name, description, created_at, last_seen_at)
                VALUES ('opencode', 'OpenCode', '2026-10-14T00:00:00Z', '2026-10-14T00:00:00Z'),
                       ('cursor', 'Cursor', '2026-10-14T00:00:00Z', '2026-10-14T00:00:00Z');
                INSERT INTO messages(id, thread_id, from_agent, subject, body, priority, workspace, reply_to, created_at)
                VALUES ('m-legacy-1', 'm-legacy-1', 'opencode', '旧库历史信件', '旧库正文', 'normal', '/tmp/legacy', NULL, '2026-10-14T00:00:00Z');
                INSERT INTO deliveries(message_id, to_agent, status, read_at)
                VALUES ('m-legacy-1', 'cursor', 'unread', NULL);
                """
            )
            conn.commit()
        migrated = Store(legacy_db)
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"projects", "windows", "cases", "case_nodes"}.issubset(tables))
            deliv_cols = {r[1] for r in conn.execute("PRAGMA table_info(deliveries)")}
            self.assertIn("claimed_by", deliv_cols)
            self.assertIn("claimed_at", deliv_cols)
        legacy_inbox = migrated.inbox("cursor", status="unread", full=True)
        self.assertEqual(legacy_inbox["count"], 1)
        self.assertEqual(legacy_inbox["messages"][0]["id"], "m-legacy-1")
        self.assertIsNone(legacy_inbox["messages"][0]["claimed_by"])
        claimed_legacy = migrated.mark("cursor", "m-legacy-1", "read", window_id="w-leg")
        self.assertEqual(claimed_legacy["claimed_by"], "w-leg")

    # 2. 双窗口抢单：同一 agent 起 w-1、w-2；w-1 先 read 绑定 claimed_by=w-1；w-2 再 read 报 already_claimed，force 可接手
    def test_02_dual_window_claim_and_force_takeover(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")
        msg = self.store.send("opencode", ["cursor"], "实现存储层", "请处理", workspace="/tmp/proj")

        # w-1 先签收认领
        res1 = self.store.mark("cursor", msg["id"], "read", window_id="w-1")
        self.assertEqual(res1["delivery_status"], "read")
        self.assertEqual(res1["claimed_by"], "w-1")
        self.assertIsNotNone(res1["claimed_at"])

        # 自动建案卷，归属 w-1
        case = self.store.get_case(msg["thread_id"])
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["owner_agent"], "cursor")
        self.assertEqual(case["owner_window"], "w-1")

        # w-2 再 read 被拒（409 already_claimed）
        with self.assertRaises(MailError) as ctx:
            self.store.mark("cursor", msg["id"], "read", window_id="w-2")
        self.assertEqual(ctx.exception.code, "already_claimed")
        self.assertEqual(ctx.exception.status, 409)

        # w-2 使用 force=True 强制接手
        res2 = self.store.mark("cursor", msg["id"], "read", window_id="w-2", force=True)
        self.assertEqual(res2["claimed_by"], "w-2")
        case_after = self.store.get_case(msg["thread_id"])
        self.assertEqual(case_after["owner_window"], "w-2")

    # 3. 提醒不重响：w-1 与 w-2 轮询 /v1/events，新信到达时只有先触达/认领的窗口收到提醒，另一窗口被过滤
    def test_03_events_alert_filtering_across_windows(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")
        msg = self.store.send("opencode", ["cursor"], "紧急修复", "详情", workspace="/tmp/proj")

        # w-1 先拉取 events，触发原子软认领
        ev1 = self.store.events("cursor", since=0, wait=0, window_id="w-1")
        self.assertEqual(len(ev1["new"]), 1)
        self.assertEqual(ev1["new"][0]["id"], msg["id"])
        self.assertEqual(ev1["new"][0]["claimed_by"], "w-1")
        self.assertEqual(ev1["unread_total"], 1)

        # w-2 随后拉取 events，因该信已被 w-1 认领，w-2 不会收到重复提醒，未读计数也为 0
        ev2 = self.store.events("cursor", since=0, wait=0, window_id="w-2")
        self.assertEqual(ev2["new"], [])
        self.assertEqual(ev2["unread_total"], 0)

        # w-2 查看默认未读收件箱也已被过滤（不干扰 w-2）
        inbox2 = self.store.inbox("cursor", status="unread", window_id="w-2")
        self.assertEqual(inbox2["count"], 0)
        inbox1 = self.store.inbox("cursor", status="unread", window_id="w-1")
        self.assertEqual(inbox1["count"], 1)

    # 4. 线程后续归属：w-1 认领首信后，对方 reply 的新信自动归入 w-1，只唤醒 w-1
    def test_04_thread_propagation_to_claiming_window(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")
        first = self.store.send("opencode", ["cursor"], "第一轮派单", "请开始", workspace="/tmp/proj")
        self.store.mark("cursor", first["id"], "read", window_id="w-1")
        cursor_reply = self.store.reply("cursor", first["id"], "已做好初版", window_id="w-1")
        # 此时案卷进入待回（waiting）
        self.assertEqual(self.store.get_case(first["thread_id"])["status"], "waiting")

        cursor_seq = self.store.events("cursor", since=0, wait=0, window_id="w-1")["cursor"]
        # opencode 在同一线程回复 cursor
        followup = self.store.reply("opencode", cursor_reply["id"], "请补充边界测试")

        # 新投递自动沿用线程归属 w-1，案卷自动回到 open
        ev_w2 = self.store.events("cursor", since=cursor_seq, wait=0, window_id="w-2")
        self.assertEqual(ev_w2["new"], [])
        self.assertEqual(ev_w2["unread_total"], 0)

        ev_w1 = self.store.events("cursor", since=cursor_seq, wait=0, window_id="w-1")
        self.assertEqual(len(ev_w1["new"]), 1)
        self.assertEqual(ev_w1["new"][0]["id"], followup["id"])
        self.assertEqual(ev_w1["new"][0]["claimed_by"], "w-1")
        self.assertEqual(self.store.get_case(first["thread_id"])["status"], "open")

    # 5. 跨窗口活动隔离：w-1 与 w-2 各自上报 activity，互不覆盖；list_agents 汇总展示两个办公室
    def test_05_window_activity_isolation_and_summary(self):
        alive_pid = os.getpid()
        self.store.touch("cursor", activity="重构 store.py", window_id="w-1", workspace="/tmp/proj-1", pid=alive_pid)
        self.store.touch("cursor", activity="编写窗口测试", window_id="w-2", workspace="/tmp/proj-2", pid=alive_pid)

        w1 = self.store.get_window("w-1")
        w2 = self.store.get_window("w-2")
        self.assertEqual(w1["activity"], "重构 store.py")
        self.assertEqual(w2["activity"], "编写窗口测试")
        self.assertEqual(w1["workspace"], "/tmp/proj-1")
        self.assertEqual(w2["workspace"], "/tmp/proj-2")

        agents = {a["name"]: a for a in self.store.list_agents()}
        cursor_info = agents["cursor"]
        self.assertEqual(cursor_info["active_window_count"], 2)
        self.assertIn("[w-1] 重构 store.py", cursor_info["activity"])
        self.assertIn("[w-2] 编写窗口测试", cursor_info["activity"])

    # 6. 案卷流转：收信认领 → 自动建 open 案卷 → 追加单行节点（禁换行）→ 转 waiting → done 全链路
    def test_06_case_lifecycle_and_single_line_nodes(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        msg = self.store.send("opencode", ["cursor"], "实现案卷流转", "派单正文", workspace="/tmp/proj")
        self.store.mark("cursor", msg["id"], "read", window_id="w-1")

        cid = msg["thread_id"]
        c = self.store.get_case(cid)
        self.assertEqual(c["status"], "open")
        self.assertEqual(len(c["nodes"]), 1)
        self.assertIn("认领 → 在办", c["nodes"][0]["note"])

        # 节点禁止多行
        with self.assertRaises(MailError) as ctx:
            self.store.append_case_node(cid, agent="cursor", note="第一行\n第二行", window_id="w-1")
        self.assertEqual(ctx.exception.code, "invalid_node")

        # 正常追加单行节点并切为 waiting
        c = self.store.append_case_node(
            cid,
            agent="cursor",
            note="已提交 PR，等待 opencode 审阅",
            status="waiting",
            window_id="w-1",
            waiting_for="opencode",
        )
        self.assertEqual(c["status"], "waiting")
        self.assertEqual(c["waiting_for"], "opencode")
        self.assertEqual(c["last_note"], "已提交 PR，等待 opencode 审阅")

        # 办结案卷。没写「怎样算完」会被 409 拦住。
        self.store.case_acceptance_set(cid, "cursor", "验收通过，归档结案")
        c = self.store.close_case(cid, agent="opencode", note="验收通过，归档结案")
        self.assertEqual(c["status"], "done")
        self.assertIsNotNone(c["closed_at"])
        self.assertIsNone(c["owner_window"])
        # 认领、waiting、怎样算完、办结，四条节点。
        self.assertEqual(len(c["nodes"]), 4)
        self.assertEqual(c["nodes"][-1]["note"], "验收通过，归档结案")

    # 7. 暂停与恢复：pause 后状态为 paused，保留 owner_window，不推新提醒，可 resume 回 open
    def test_07_pause_and_resume_case(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        msg = self.store.send("opencode", ["cursor"], "长线特性", "先做第一阶段", workspace="/tmp/proj")
        self.store.mark("cursor", msg["id"], "read", window_id="w-1")
        cid = msg["thread_id"]

        paused = self.store.pause_case(cid, agent="cursor", window_id="w-1", note="等待外部依赖就绪")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["owner_window"], "w-1")
        self.assertIsNone(paused["closed_at"])

        # 暂停期间同线程来信，events 对 w-1 也不推送打扰（直至恢复）
        seq = self.store.events("cursor", since=0, wait=0, window_id="w-1")["cursor"]
        self.store.reply("opencode", msg["id"], "补充一点资料", to=["cursor"])
        ev_paused = self.store.events("cursor", since=seq, wait=0, window_id="w-1")
        self.assertEqual(ev_paused["new"], [])

        # 恢复案卷后回到 open，仍归属 w-1
        resumed = self.store.resume_case(cid, agent="cursor", window_id="w-1", note="依赖已就绪，继续推进")
        self.assertEqual(resumed["status"], "open")
        self.assertEqual(resumed["owner_window"], "w-1")

        # D1-b: 未封卷案卷正向转办（transfer_case）：owner_window 切换、节点文案落库、thread 归属覆写
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")
        transferred = self.store.transfer_case(cid, agent="cursor", to_window="w-2", window_id="w-1")
        self.assertEqual(transferred["status"], "open")
        self.assertEqual(transferred["owner_agent"], "cursor")
        self.assertEqual(transferred["owner_window"], "w-2")
        self.assertEqual(transferred["nodes"][-1]["action"], "transfer")
        self.assertIn("转办 → 交接给 w-2", transferred["nodes"][-1]["note"])
        # 线程内既有投递的 claimed_by 已被覆写为 w-2
        msg_view = self.store.get_message("cursor", msg["id"])
        self.assertEqual(msg_view["claimed_by"], "w-2")

    # 8. 中途退出**不中止案卷**（M7 宪法）：窗口正常退出 / 进程崩溃，案卷都保持未封卷，只摘指针 + 记一行
    def test_08_window_exit_and_crash_does_not_abort_case(self):
        # 8a: 正常退出 leave_window
        self.store.touch("cursor", window_id="w-exit", workspace="/tmp/proj")
        m1 = self.store.send("opencode", ["cursor"], "任务一", "内容", workspace="/tmp/proj")
        self.store.mark("cursor", m1["id"], "read", window_id="w-exit")

        leave_res = self.store.leave_window("w-exit", agent="cursor")
        self.assertEqual(leave_res["state"], "left")
        self.assertEqual(leave_res["aborted_cases"], [], "不再自动中止任何案卷")
        self.assertIn(m1["thread_id"], leave_res["orphaned_cases"], "只是摘了主办窗口指针")

        c1 = self.store.get_case(m1["thread_id"])
        self.assertEqual(c1["status"], "open", "案卷不因窗口离场而中止")
        self.assertFalse(c1["sealed"])
        self.assertIsNone(c1["owner_window"])
        self.assertIsNone(c1["closed_at"])
        self.assertTrue(c1["nodes"][-1]["auto"])
        self.assertIn("保持未封卷", c1["nodes"][-1]["note"])

        # 窗口没了，案卷照样能继续办（M7 的核心：事没做完，案卷就该在）
        self.store.append_case_node(m1["thread_id"], agent="cursor", note="接手继续办")
        self.assertEqual(self.store.get_case(m1["thread_id"])["nodes"][-1]["note"], "接手继续办")

        # 8b: 进程崩溃未调离场接口（死 PID），下次读案卷时惰性对账：同样不封卷
        dead_pid = 99999999
        self.store.touch("cursor", window_id="w-crash", workspace="/tmp/proj", pid=dead_pid)
        m2 = self.store.send("opencode", ["cursor"], "任务二", "内容", workspace="/tmp/proj")
        self.store.mark("cursor", m2["id"], "read", window_id="w-crash")

        c2 = self.store.get_case(m2["thread_id"])  # 触发 _lazy_reconcile_case
        self.assertEqual(c2["status"], "open")
        self.assertFalse(c2["sealed"])
        self.assertIsNone(c2["owner_window"])
        self.assertTrue(c2["nodes"][-1]["auto"])
        self.assertIn("保持未封卷", c2["nodes"][-1]["note"])
        self.assertEqual(self.store.get_window("w-crash")["state"], "left")

    # 8c: 要中止/办结，必须显式做（程序绝不自动改语义状态）
    def test_08c_abort_is_explicit_only(self):
        self.store.touch("cursor", window_id="w-x", workspace="/tmp/proj")
        m = self.store.send("opencode", ["cursor"], "显式中止", "内容", workspace="/tmp/proj")
        self.store.mark("cursor", m["id"], "read", window_id="w-x")
        self.store.leave_window("w-x", agent="cursor")
        self.assertEqual(self.store.get_case(m["thread_id"])["status"], "open")

        aborted = self.store.abort_case(m["thread_id"], agent="opencode", note="确实不做了")
        self.assertEqual(aborted["status"], "aborted")
        self.assertTrue(aborted["sealed"])

    # 9. 办结与封卷只读：close 后状态为 done，释放窗口绑定；封卷后（aborted/done）禁止写节点或改状态（重开除外）
    def test_09_close_and_sealed_readonly_enforcement(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        msg = self.store.send("opencode", ["cursor"], "封卷测试", "内容", workspace="/tmp/proj")
        self.store.mark("cursor", msg["id"], "read", window_id="w-1")
        cid = msg["thread_id"]

        self.store.case_acceptance_set(cid, "cursor", "全部测试绿灯")
        closed = self.store.close_case(cid, agent="cursor", window_id="w-1", note="全部测试绿灯")
        self.assertEqual(closed["status"], "done")
        self.assertIsNone(closed["owner_window"])

        # 封卷后禁止追加节点、暂停、恢复、转办、再办结
        for fn in (
            lambda: self.store.append_case_node(cid, agent="cursor", note="试图追加"),
            lambda: self.store.pause_case(cid, agent="cursor"),
            lambda: self.store.resume_case(cid, agent="cursor"),
            lambda: self.store.transfer_case(cid, agent="cursor", to_window="w-2"),
            lambda: self.store.close_case(cid, agent="cursor"),
        ):
            with self.assertRaises(MailError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "case_sealed")
            self.assertEqual(ctx.exception.status, 409)

        # 显式 reopen 后才允许再次修改
        reopened = self.store.reopen_case(cid, agent="opencode", window_id="w-1", note="发现回归问题，重开")
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["closed_at"])
        updated = self.store.append_case_node(cid, agent="cursor", note="已定位回归原因", window_id="w-1")
        self.assertEqual(updated["last_note"], "已定位回归原因")

    # 10. 项目归集 + HTTP / MCP 端到端接口验证
    def test_10_projects_aggregation_and_http_mcp_endpoints(self):
        ws_a = str((self.home / "projA").resolve())
        ws_b = str((self.home / "projB").resolve())
        Path(ws_a).mkdir(parents=True, exist_ok=True)
        Path(ws_b).mkdir(parents=True, exist_ok=True)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # 登记项目（模拟浏览器管理台不带 X-Agent 直接请求）
            st, p_res = _http_json(url, "POST", "/v1/projects", body={"workspace": ws_a, "name": "项目A"})
            self.assertEqual(st, 201)
            self.assertEqual(p_res["name"], "项目A")

            # 两个窗口登记在不同项目
            _http_json(url, "POST", "/v1/windows", agent="cursor", window="w-a1", body={"workspace": ws_a, "activity": "做A1"})
            _http_json(url, "POST", "/v1/windows", agent="grok", window="w-b1", body={"workspace": ws_b, "activity": "做B1"})

            # 在项目A建两个案卷（1个 open，1个 paused），项目B建一个案卷并办结（done）
            _, m_a1 = _http_json(url, "POST", "/v1/messages", agent="opencode", body={
                "to": ["cursor"], "subject": "A1任务", "body": "内容", "workspace": ws_a,
            })
            _http_json(url, "POST", f"/v1/messages/{m_a1['id']}/read", agent="cursor", window="w-a1")

            _, m_a2 = _http_json(url, "POST", "/v1/messages", agent="opencode", body={
                "to": ["cursor"], "subject": "A2任务", "body": "内容", "workspace": ws_a,
            })
            _http_json(url, "POST", f"/v1/messages/{m_a2['id']}/read", agent="cursor", window="w-a1")
            _http_json(url, "POST", f"/v1/cases/{m_a2['thread_id']}/pause", body={"note": "先挂起"})

            _, c_b1 = _http_json(url, "POST", "/v1/cases", agent="opencode", body={
                "title": "B1直接立案", "workspace": ws_b, "owner_agent": "grok", "owner_window": "w-b1",
                "acceptance": "项目 B 办结计数",
            })
            _http_json(url, "POST", f"/v1/cases/{c_b1['id']}/close", body={"note": "B1完成"})

            # 按项目过滤案卷与窗口
            _, cases_a = _http_json(url, "GET", f"/v1/cases?workspace={ws_a}")
            self.assertEqual(cases_a["count"], 2)
            _, wins_a = _http_json(url, "GET", f"/v1/windows?workspace={ws_a}")
            self.assertEqual(wins_a["count"], 1)
            self.assertEqual(wins_a["windows"][0]["window_id"], "w-a1")

            # 项目汇总统计验证
            _, projs = _http_json(url, "GET", "/v1/projects")
            by_ws = {p["workspace"]: p for p in projs["projects"]}
            self.assertIn(ws_a, by_ws)
            self.assertIn(ws_b, by_ws)
            self.assertEqual(by_ws[ws_a]["counts"]["open"], 1)
            self.assertEqual(by_ws[ws_a]["counts"]["paused"], 1)
            self.assertEqual(by_ws[ws_a]["active_windows"], 1)
            self.assertEqual(by_ws[ws_b]["counts"]["done"], 1)

            # MCP ic_case 只读工具与窗口号生成验证
            win_auto = resolve_mcp_window(None)
            self.assertTrue(win_auto.startswith("w-"))
            res_single = _call_tool(url, "cursor", "ic_case", {"id": m_a1["thread_id"]}, window_id="w-a1")
            self.assertFalse(res_single["isError"])
            self.assertEqual(json.loads(res_single["content"][0]["text"])["title"], "A1任务")

            res_list = _call_tool(url, "cursor", "ic_case", {"workspace": ws_a}, window_id="w-a1")
            self.assertFalse(res_list["isError"])
            self.assertEqual(json.loads(res_list["content"][0]["text"])["count"], 2)
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 13. D3（/ui 管理台 + D3-a/D3-b/D3-c）端到端冒烟验证
    def test_13_ui_console_smoke_and_d3_abc(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # 1) GET /ui 返回 HTML 且包含 D3-a / D3-b / D3-c 关键实现
            req = urllib.request.Request(f"{url}/ui", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                html = resp.read().decode("utf-8")

            # D3-a: 兼容 last_node 为对象（取 c.last_note || c.last_node.note），不再把对象直接拼成 [object Object]
            self.assertIn("c.last_note", html)
            self.assertIn("c.last_node.note", html)
            self.assertNotIn("const last = c.last_node || c.last_note", html)
            # D3-b: 按原始 c.status 判断可暂停状态（open/doing/pending/waiting）
            self.assertIn('"open", "doing", "pending", "waiting"', html)
            # D3-c: 停用会话前有 confirm() 二次确认；**文案必须与 M7 新口径一致**
            # 假文案类＝声称会「自动…封卷」或按钮「离场并封卷」。
            # 「中止」与「封卷」之间：且/并、逗号（，、,）、空白，有没有都要抓住。
            # 否定句是正确口径，要留：不会被自动封卷 / 不再自动封卷。别把「已封卷」这类无关文案算进去。
            self.assertIn("confirm(", html)
            self.assertIn("不会被自动封卷", html)
            self.assertIn("不再自动封卷", html)
            self.assertNotIn("离场并封卷", html)
            false_seal = re.compile(
                r"(?<!不会被)(?<!不再)自动(?:中止)?\s*(?:[，、,]\s*)?(?:且|并)?\s*封卷"
            )
            # 自反例：三句变体都必须判红（上一版正则漏了逗号）。
            self.assertIsNotNone(false_seal.search("会自动中止并封卷"), "「并」变体必须判红")
            self.assertIsNotNone(false_seal.search("会自动中止且封卷"), "「且」变体必须判红")
            self.assertIsNotNone(false_seal.search("会自动中止，封卷"), "逗号变体必须判红")
            self.assertIsNotNone(false_seal.search("会自动中止,封卷"), "英文逗号变体必须判红")
            self.assertIsNotNone(false_seal.search("会自动中止、封卷"), "顿号变体必须判红")
            self.assertIsNotNone(false_seal.search("会自动中止 封卷"), "空白变体必须判红")
            # 边界不放宽：否定句与无关文案不算假文案。
            self.assertIsNone(false_seal.search("不会被自动封卷"), "否定句「不会被自动封卷」要留")
            self.assertIsNone(false_seal.search("不再自动封卷"), "否定句「不再自动封卷」要留")
            self.assertIsNone(false_seal.search("已封卷"), "「已封卷」不是假文案")
            self.assertIsNone(false_seal.search("未封卷"), "「未封卷」不是假文案")
            hit = false_seal.search(html)
            self.assertIsNone(hit, f"看板仍有自动封卷假文案：{hit.group(0) if hit else ''}")

            # M7-3: 控制台三层 = 项目 / WBS / 案卷；会话不进控制台
            self.assertIn('id="wbs"', html)
            self.assertIn("buildWbsTree", html)
            self.assertIn("未归类", html)
            self.assertIn("项目团队会话", html)
            self.assertNotIn('id="windows"', html, "会话栏必须从控制台移走")
            self.assertNotIn("f-win-state", html)

            # 2) 模拟管理台 API 全链路：设定项目地址 -> 办公室上报 -> 立案（waiting/pending/open）-> 暂停/恢复 -> 停用窗口级联封卷
            ws = str((self.home / "ui_smoke_proj").resolve())
            Path(ws).mkdir(parents=True, exist_ok=True)
            st, _ = _http_json(url, "POST", "/v1/projects", body={"workspace": ws})
            self.assertEqual(st, 201)

            st, _ = _http_json(url, "POST", "/v1/windows", agent="cursor", window="w-ui-1", body={"workspace": ws, "activity": "UI冒烟"})
            self.assertEqual(st, 200)

            st, created = _http_json(url, "POST", "/v1/cases", body={
                "title": "D3冒烟案件", "workspace": ws, "owner_agent": "cursor", "owner_window": "w-ui-1", "note": "起始信内容",
            })
            self.assertEqual(st, 201)
            cid = created["id"]
            # 验证服务端返回的 last_node 是 dict 且 last_note 是字符串
            self.assertIsInstance(created["last_node"], dict)
            self.assertTrue(isinstance(created["last_note"], str) and len(created["last_note"]) > 0)

            # 将案件置为 waiting（待 opencode），验证后端允许对 waiting 状态直接 pause（对应 D3-b 前端放开暂停按钮）
            self.store.append_case_node(cid, agent="cursor", window_id="w-ui-1", note="等待评审", status="waiting", waiting_for="opencode")
            _, c_wait = _http_json(url, "GET", f"/v1/cases/{cid}")
            self.assertEqual(c_wait["status"], "waiting")
            self.assertEqual(c_wait["status_label"], "待 opencode")

            st, c_paused = _http_json(url, "POST", f"/v1/cases/{cid}/pause", body={})
            self.assertEqual(st, 200)
            self.assertEqual(c_paused["status"], "paused")

            st, c_resumed = _http_json(url, "POST", f"/v1/cases/{cid}/resume", body={})
            self.assertEqual(st, 200)
            self.assertEqual(c_resumed["status"], "open")

            # 停用办公室（leave）：**不**级联中止案卷（M7 宪法）——只摘主办窗口指针
            st, w_left = _http_json(url, "POST", "/v1/windows/w-ui-1/leave", body={})
            self.assertEqual(st, 200)
            self.assertEqual(w_left["state"], "left")
            self.assertEqual(w_left["aborted_cases"], [])
            self.assertIn(cid, w_left["orphaned_cases"])

            _, c_still_open = _http_json(url, "GET", f"/v1/cases/{cid}")
            self.assertEqual(c_still_open["status"], "open")
            self.assertFalse(c_still_open["sealed"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    # 11. D1-a 专项：旧库 schema（无 claimed_by/claimed_at 列、无 cases/windows/projects/case_nodes 表）自动迁移
    def test_11_legacy_schema_auto_migration(self):
        legacy_db = self.home / "legacy_standalone.db"
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            conn.executescript(
                """
                CREATE TABLE agents (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE messages (
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
                CREATE TABLE deliveries (
                    message_id TEXT NOT NULL REFERENCES messages(id),
                    to_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    read_at TEXT,
                    PRIMARY KEY (message_id, to_agent)
                );
                INSERT INTO agents(name, description, created_at, last_seen_at)
                VALUES ('opencode', 'OpenCode', '2026-10-14T00:00:00Z', '2026-10-14T00:00:00Z'),
                       ('cursor', 'Cursor', '2026-10-14T00:00:00Z', '2026-10-14T00:00:00Z');
                INSERT INTO messages(id, thread_id, from_agent, subject, body, priority, workspace, reply_to, created_at)
                VALUES ('m-old-01', 'm-old-01', 'opencode', '旧库派单', '旧库内容', 'normal', '/tmp/old', NULL, '2026-10-14T00:00:00Z');
                INSERT INTO deliveries(message_id, to_agent, status, read_at)
                VALUES ('m-old-01', 'cursor', 'unread', NULL);
                """
            )
            conn.commit()

        migrated = Store(legacy_db)
        with contextlib.closing(sqlite3.connect(legacy_db)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"projects", "windows", "cases", "case_nodes"}.issubset(tables))
            agent_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
            self.assertTrue({"role", "activity", "activity_at"}.issubset(agent_cols))
            deliv_cols = {r[1] for r in conn.execute("PRAGMA table_info(deliveries)")}
            self.assertTrue({"claimed_by", "claimed_at"}.issubset(deliv_cols))

        inbox = migrated.inbox("cursor", status="unread", full=True)
        self.assertEqual(inbox["count"], 1)
        self.assertEqual(inbox["messages"][0]["id"], "m-old-01")
        self.assertIsNone(inbox["messages"][0]["claimed_by"])

        ack = migrated.mark("cursor", "m-old-01", "read", window_id="w-migrated")
        self.assertEqual(ack["claimed_by"], "w-migrated")
        case = migrated.get_case("m-old-01")
        self.assertEqual(case["owner_window"], "w-migrated")

    # 12. D1-b 专项：未封卷案卷正向转办（transfer_case）切换 owner_window、落转办节点、覆写 thread 归属
    def test_12_transfer_case_positive_flow(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")
        m1 = self.store.send("opencode", ["cursor"], "转办任务", "第一轮", workspace="/tmp/proj")
        self.store.mark("cursor", m1["id"], "read", window_id="w-1")
        m2 = self.store.reply("opencode", m1["id"], "补充说明", to=["cursor"])
        cid = m1["thread_id"]

        transferred = self.store.transfer_case(cid, agent="cursor", to_window="w-2", window_id="w-1")
        self.assertEqual(transferred["status"], "open")
        self.assertEqual(transferred["owner_agent"], "cursor")
        self.assertEqual(transferred["owner_window"], "w-2")
        self.assertEqual(transferred["nodes"][-1]["action"], "transfer")
        self.assertEqual(transferred["nodes"][-1]["note"], "转办 → 交接给 w-2")

        # 线程下所有给 cursor 的投递归属均覆写为 w-2
        self.assertEqual(self.store.get_message("cursor", m1["id"])["claimed_by"], "w-2")
        self.assertEqual(self.store.get_message("cursor", m2["id"])["claimed_by"], "w-2")
        # 此时 w-1 再试图不带 force 签收 m2 会被 already_claimed 拦截，w-2 则可正常签收
        with self.assertRaises(MailError) as ctx:
            self.store.mark("cursor", m2["id"], "read", window_id="w-1")
        self.assertEqual(ctx.exception.code, "already_claimed")
        ack_w2 = self.store.mark("cursor", m2["id"], "read", window_id="w-2")
        self.assertEqual(ack_w2["claimed_by"], "w-2")

    # 13. P0 · U-1 专项：case_id / thread_id 严格字符集校验与 Web 控制台零内联 onclick
    def test_13_p0_u1_case_id_validation_and_web_no_inline_onclick(self):
        bad_ids = [
            "m' + alert(1) + '",
            "a/b",
            "a#b",
            "../etc",
            ".hidden",
            "-dash",
            ":colon",
            "",
            "x" * 65,
            123,
        ]
        for bad in bad_ids:
            with self.subTest(bad_id=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.create_case("opencode", thread_id=bad, title="bad")  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, "invalid_case_id")

                if isinstance(bad, str) and bad.strip():
                    with self.assertRaises(MailError) as get_ctx:
                        self.store.get_case(bad)
                    self.assertEqual(get_ctx.exception.status, 400)
                    self.assertEqual(get_ctx.exception.code, "invalid_case_id")

        html = (Path(__file__).resolve().parents[1] / "infocenter" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("onclick=", html)
        self.assertIn("data-case-id", html)
        self.assertIn("encodeURIComponent", html)

    # 14. P0 · M1(events) 专项：单批超 limit 封未读时按 ASC 推进游标，分页拉取零丢信
    def test_14_p0_m1_events_pagination_cursor_no_message_loss(self):
        self.store.touch("cursor", window_id="w-ev", workspace="/tmp/proj")
        sent_ids = []
        for idx in range(55):
            msg = self.store.send("opencode", ["cursor"], f"batch-{idx}", f"body-{idx}", workspace="/tmp/proj")
            sent_ids.append(msg["id"])

        # 无窗口模式分两页拉取 55 封（limit=50）
        page1 = self.store.events("cursor", since=0, wait=0, limit=50)
        self.assertEqual(len(page1["new"]), 50)
        self.assertEqual([m["id"] for m in page1["new"]], sent_ids[:50])
        page2 = self.store.events("cursor", since=page1["cursor"], wait=0, limit=50)
        self.assertEqual(len(page2["new"]), 5)
        self.assertEqual([m["id"] for m in page2["new"]], sent_ids[50:])

        # 带窗口模式分两页拉取 55 封（limit=50）
        w_page1 = self.store.events("cursor", since=0, wait=0, limit=50, window_id="w-ev")
        self.assertEqual(len(w_page1["new"]), 50)
        self.assertEqual([m["id"] for m in w_page1["new"]], sent_ids[:50])
        w_page2 = self.store.events("cursor", since=w_page1["cursor"], wait=0, limit=50, window_id="w-ev")
        self.assertEqual(len(w_page2["new"]), 5)
        self.assertEqual([m["id"] for m in w_page2["new"]], sent_ids[50:])

    # 15. P1 · D-1 + M1-data 专项：inbox SQL 过滤在 LIMIT 之前执行，且未读统计口径一致
    def test_15_p1_d1_inbox_limit_before_filter_and_unread_counts_consistency(self):
        self.store.touch("cursor", window_id="w-1", workspace="/tmp/proj")
        self.store.touch("cursor", window_id="w-2", workspace="/tmp/proj")

        # 先发 5 封并让 w-1 认领其线程（再由 opencode 回复产生 5 封属于 w-1 的未读）
        for i in range(5):
            m = self.store.send("opencode", ["cursor"], f"w1-task-{i}", "body", workspace="/tmp/proj")
            self.store.mark("cursor", m["id"], "read", window_id="w-1")
            self.store.reply("opencode", m["id"], f"w1-followup-{i}", to=["cursor"])

        # 再发 1 封已暂停案卷的未读信
        m_paused = self.store.send("opencode", ["cursor"], "paused-task", "body", workspace="/tmp/proj")
        self.store.create_case("opencode", thread_id=m_paused["thread_id"], title="paused-task", workspace="/tmp/proj")
        self.store.pause_case(m_paused["thread_id"], agent="human")

        # 最后发 1 封真正属于公海的新未读信
        m_free = self.store.send("opencode", ["cursor"], "free-task", "body", workspace="/tmp/proj")

        # w-2 以 limit=2 查询未读，不应被前面 5 封 w-1 认领信和 1 封暂停信截断成空列表
        inbox_w2 = self.store.inbox("cursor", status="unread", limit=2, window_id="w-2")
        self.assertEqual([m["id"] for m in inbox_w2["messages"]], [m_free["id"]])
        self.assertEqual(inbox_w2["unread_total"], 1)

        # 不带窗口查询未读统计，也应排除已被 w-1 认领及已暂停的投递，只算公海未读 1 封
        inbox_all = self.store.inbox("cursor", status="unread", limit=20)
        self.assertEqual(inbox_all["unread_total"], 1)
        t_res = self.store.touch("cursor")
        self.assertEqual(t_res["unread"], 1)
        agents_res = self.store.list_agents()
        cursor_row = next(a for a in agents_res if a["name"] == "cursor")
        self.assertEqual(cursor_row["unread"], 1)

    # 16. P1 · D-2 专项：跨 agent 转办释放原 owner_agent 窗口认领并清空 waiting_for
    def test_16_p1_d2_cross_agent_transfer_releases_source_claim_and_clears_waiting_for(self):
        self.store.touch("cursor", window_id="w-cur", workspace="/tmp/proj")
        self.store.touch("kimi", window_id="w-kimi", workspace="/tmp/proj")

        m1 = self.store.send("opencode", ["cursor", "kimi"], "跨智能体协作", "请处理", workspace="/tmp/proj")
        self.store.mark("cursor", m1["id"], "read", window_id="w-cur")
        self.store.reply("cursor", m1["id"], "请补充材料", to=["opencode"], window_id="w-cur")
        m2 = self.store.reply("opencode", m1["id"], "材料已补齐", to=["cursor", "kimi"])
        cid = m1["thread_id"]

        # 转办前：cursor 的 m2 已被 w-cur 自动认领
        self.assertEqual(self.store.get_message("cursor", m2["id"])["claimed_by"], "w-cur")

        transferred = self.store.transfer_case(
            cid, agent="opencode", to_agent="kimi", to_window="w-kimi"
        )
        self.assertEqual(transferred["owner_agent"], "kimi")
        self.assertEqual(transferred["owner_window"], "w-kimi")
        self.assertIsNone(transferred["waiting_for"])

        # 转办后：cursor 名下投递的 claimed_by 已被释放为 None，kimi 名下投递归属 w-kimi
        self.assertIsNone(self.store.get_message("cursor", m1["id"])["claimed_by"])
        self.assertIsNone(self.store.get_message("cursor", m2["id"])["claimed_by"])
        self.assertEqual(self.store.get_message("kimi", m1["id"])["claimed_by"], "w-kimi")
        self.assertEqual(self.store.get_message("kimi", m2["id"])["claimed_by"], "w-kimi")

    # 17. P1 · D-3 专项：非线程当事人不得针对他人私密线程立案或认领案卷
    def test_17_p1_d3_non_party_cannot_create_or_claim_case_on_existing_thread(self):
        m1 = self.store.send("opencode", ["cursor"], "机密派单", "仅限 cursor", workspace="/tmp/proj")
        tid = m1["thread_id"]

        with self.assertRaises(MailError) as ctx1:
            self.store.create_case("grok", thread_id=tid, title="刺探")
        self.assertEqual(ctx1.exception.status, 404)
        self.assertEqual(ctx1.exception.code, "not_found")

        # 触发案卷创建后，局外人 grok 试图 claim_case 也应返回 404
        self.store.mark("cursor", m1["id"], "read", window_id="w-cur")
        with self.assertRaises(MailError) as ctx2:
            self.store.claim_case(tid, "grok", "w-grok")
        self.assertEqual(ctx2.exception.status, 404)
        self.assertEqual(ctx2.exception.code, "not_found")

    # 18. P1 · D-4 专项：窗口离场或死 pid 后重新激活且未传 pid 时清空残留死 pid
    def test_18_p1_d4_revived_window_clears_stale_pid(self):
        self.store.touch_window("w-dead", agent="cursor", workspace="/tmp/proj", pid=999_999_999)
        self.store.leave_window("w-dead", agent="cursor")
        self.assertEqual(self.store.get_window("w-dead")["state"], "left")

        # 显式将窗口恢复为 active 但未传新 pid，应清空死 pid
        self.store.touch_window("w-dead", agent="cursor", workspace="/tmp/proj", state="active")
        w = self.store.get_window("w-dead")
        self.assertEqual(w["state"], "active")
        self.assertIsNone(w["pid"])


if __name__ == "__main__":
    unittest.main()
