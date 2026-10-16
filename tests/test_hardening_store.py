"""M5 最后评审加固回归测试：D-1/D-2/D-3/D-4/D-5/D-6/D-7/D-8、M1/M1-data/M2/M3、L2。

只覆盖 `store.py` / `httpapi.py` 的修复点，走临时目录，不动现网。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.server import make_handler
from infocenter.store import MailError, Store, _workspace_scope


def _http(url: str, method: str, path: str, agent=None, window=None, body=None, pid=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    if agent is not None:
        req.add_header("X-Agent", agent)
    if window is not None:
        req.add_header("X-Window", window)
    if pid is not None:
        req.add_header("X-Pid", str(pid))
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        exc.close()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


class HardeningStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.proj = str((self.home / "proj").resolve())
        Path(self.proj).mkdir(parents=True, exist_ok=True)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("grok", "Grok", ""),
                ("kimi", "Kimi Code", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
                ("human", "用户", ""),
            ],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    # ── D-1：inbox 先过滤后 LIMIT，本窗口未读不被他人认领的行吃掉配额 ──────────
    def test_d1_inbox_window_filter_applies_before_limit(self):
        self.store.touch("cursor", window_id="w-1", workspace=self.proj)
        self.store.touch("cursor", window_id="w-2", workspace=self.proj)
        oldest = self.store.send("opencode", ["cursor"], "最旧的可见信", "正文", workspace=self.proj)
        # 记下游标但不认领（非窗口 events 不写 claimed_by）
        cursor = self.store.events("cursor", since=0, wait=0)["cursor"]
        newer_a = self.store.send("opencode", ["cursor"], "较新A", "正文", workspace=self.proj)
        newer_b = self.store.send("opencode", ["cursor"], "较新B", "正文", workspace=self.proj)
        # w-1 认领较新的两封（保持 unread）
        claimed = self.store.events("cursor", since=cursor, wait=0, window_id="w-1")
        self.assertEqual({m["id"] for m in claimed["new"]}, {newer_a["id"], newer_b["id"]})

        # w-2 看未读，limit=1：被 w-1 认领的较新两封必须先被过滤，再取 1 封，
        # 否则 LIMIT 先吃掉配额 → count=0（旧实现）。修复后会拿到最旧那封。
        box = self.store.inbox("cursor", status="unread", limit=1, window_id="w-2")
        self.assertEqual(box["count"], 1)
        self.assertEqual(box["messages"][0]["id"], oldest["id"])

    # ── M1：events 取最旧 limit 条且 cursor 跟随实际返回，>limit 不丢信 ────────
    def test_m1_events_more_than_limit_does_not_lose_old_mail(self):
        sent = [self.store.send("opencode", ["cursor"], f"信{i}", "正文") for i in range(3)]
        first = self.store.events("cursor", since=0, wait=0, limit=2)
        # 修复点：返回最旧两条（而不是最新两条），所以第一封必须在
        self.assertEqual([m["id"] for m in first["new"]], [sent[0]["id"], sent[1]["id"]])
        second = self.store.events("cursor", since=first["cursor"], wait=0, limit=2)
        self.assertEqual([m["id"] for m in second["new"]], [sent[2]["id"]])
        seen = {m["id"] for m in first["new"]} | {m["id"] for m in second["new"]}
        self.assertEqual(seen, {s["id"] for s in sent})

    def test_m1_events_cursor_stays_when_nothing_fresh(self):
        self.store.send("opencode", ["cursor"], "一", "正文")
        snapshot = self.store.events("cursor", since=0, wait=0)
        # cursor 是实际返回的最大 delivery_seq；没有新信时保持 since
        empty = self.store.events("cursor", since=snapshot["cursor"], wait=0)
        self.assertEqual(empty["new"], [])
        self.assertEqual(empty["cursor"], snapshot["cursor"])

    # ── D-2：跨 agent 转办释放源 owner 认领，避免双归属/僵尸认领 ─────────────
    def test_d2_cross_agent_transfer_releases_source_claims(self):
        self.store.touch("cursor", window_id="w-1", workspace=self.proj)
        self.store.touch("grok", window_id="w-2", workspace=self.proj)
        m1 = self.store.send("opencode", ["cursor"], "跨人转办", "正文", workspace=self.proj)
        self.store.mark("cursor", m1["id"], "read", window_id="w-1")

        moved = self.store.transfer_case(
            m1["thread_id"], agent="cursor", to_window="w-2", to_agent="grok", window_id="w-1"
        )
        self.assertEqual(moved["owner_agent"], "grok")
        self.assertEqual(moved["owner_window"], "w-2")
        # 源 owner（cursor）名下该线程的投递认领被释放，旧窗口不再主办
        self.assertIsNone(self.store.get_message("cursor", m1["id"])["claimed_by"])
        # 新信不再继承源侧僵尸认领
        follow = self.store.reply("opencode", m1["id"], "后续", to=["cursor"])
        self.assertIsNone(self.store.get_message("cursor", follow["id"])["claimed_by"])

    # ── D-3：非当事人不得立案 ──────────────────────────────────────────────
    def test_d3_non_party_cannot_claim_or_create_case(self):
        m = self.store.send("opencode", ["kimi"], "二人私事", "正文")
        with self.assertRaises(MailError) as claim:
            self.store.claim_case(m["thread_id"], agent="grok", window_id="w-g")
        self.assertEqual(claim.exception.code, "not_found")
        self.assertEqual(claim.exception.status, 404)
        with self.assertRaises(MailError) as create:
            self.store.create_case(agent="grok", title="抢立案", thread_id=m["thread_id"])
        self.assertEqual(create.exception.code, "not_found")
        self.assertEqual(create.exception.status, 404)
        # 当事人仍可正常立案
        case = self.store.create_case(agent="kimi", title="正常立案", thread_id=m["thread_id"])
        self.assertEqual(case["id"], m["thread_id"])

    # ── D-4：死 pid 窗口复活时清 pid（除非显式给新 pid） ────────────────────
    def test_d4_reviving_left_window_clears_dead_pid(self):
        self.store.touch("cursor", window_id="w-r", pid=99999999)
        self.store.leave_window("w-r", agent="cursor")
        self.assertEqual(self.store.get_window("w-r")["state"], "left")
        revived = self.store.touch_window("w-r", agent="cursor", state="active")
        self.assertEqual(revived["state"], "active")
        self.assertIsNone(revived["pid"])
        reborn = self.store.touch_window("w-r", agent="cursor", state="active", pid=os.getpid())
        self.assertEqual(reborn["pid"], os.getpid())

    def test_d4_soft_touch_does_not_revive_but_presence_does(self):
        # 软触达（events/inbox 等）不得把已离场窗口复活，否则残留长轮询会点亮它
        self.store.touch("cursor", window_id="w-l", pid=99999999)
        self.store.leave_window("w-l", agent="cursor")
        self.store.inbox("cursor", status="unread", window_id="w-l")
        self.assertEqual(self.store.get_window("w-l")["state"], "left")
        # 「本人到场」的 touch 属显式复活，清掉死 pid
        self.store.touch("cursor", window_id="w-l", workspace=self.proj)
        revived = self.store.get_window("w-l")
        self.assertEqual(revived["state"], "active")
        self.assertIsNone(revived["pid"])

    # ── D-5：请求体字段类型错误 → 400（而非 AttributeError/TypeError → 500） ─
    def test_d5_bad_types_return_400_not_500(self):
        cases = [
            ("POST", "/v1/windows", {"window_id": "w-t", "state": {}}, "cursor", "invalid_state"),
            ("POST", "/v1/cases", {"title": "t", "owner_window": {}}, "human", "invalid_owner_window"),
            ("POST", "/v1/cases", {"title": "t", "thread_id": 123}, "human", "invalid_thread_id"),
            ("POST", "/v1/windows", {"window_id": "w-t2", "pid": "abc"}, "cursor", "invalid_pid"),
            ("POST", "/v1/messages", {"to": "kimi", "subject": 123, "body": "x"}, "cursor", "invalid_subject"),
        ]
        for method, path, body, agent, code in cases:
            with self.subTest(path=path, body=body):
                status, payload = _http(self.url, method, path, agent=agent, body=body)
                self.assertEqual(status, 400)
                self.assertEqual(payload.get("error"), code)

    # ── D-6：create_case 半字段 → 400 ──────────────────────────────────────
    def test_d6_half_fields_return_400(self):
        with self.assertRaises(MailError) as only_to:
            self.store.create_case(agent="opencode", title="半字段", to="kimi")
        self.assertEqual(only_to.exception.code, "invalid_request")
        self.assertEqual(only_to.exception.status, 400)
        with self.assertRaises(MailError) as only_body:
            self.store.create_case(agent="opencode", title="半字段", body="只有正文")
        self.assertEqual(only_body.exception.code, "invalid_request")
        self.assertEqual(only_body.exception.status, 400)
        # 同时给则正常发始信并沿用其线程
        full = self.store.create_case(agent="opencode", title="完整", to="kimi", body="正文")
        self.assertTrue(full["id"])

    # ── D-7：离开 waiting 时清 waiting_for ─────────────────────────────────
    def test_d7_leaving_waiting_clears_waiting_for(self):
        self.store.touch("cursor", window_id="w-1", workspace=self.proj)
        m = self.store.send("opencode", ["cursor"], "待谁", "正文", workspace=self.proj)
        self.store.mark("cursor", m["id"], "read", window_id="w-1")
        cid = m["thread_id"]

        waiting = self.store.append_case_node(
            cid, agent="cursor", note="等评审", status="waiting", window_id="w-1", waiting_for="opencode"
        )
        self.assertEqual(waiting["waiting_for"], "opencode")
        resumed = self.store.resume_case(cid, agent="cursor", window_id="w-1")
        self.assertEqual(resumed["status"], "open")
        self.assertIsNone(resumed["waiting_for"])

        self.store.append_case_node(
            cid, agent="cursor", note="再等", status="waiting", window_id="w-1", waiting_for="opencode"
        )
        self.store.touch("cursor", window_id="w-2", workspace=self.proj)
        transferred = self.store.transfer_case(cid, agent="cursor", to_window="w-2", window_id="w-1")
        self.assertEqual(transferred["status"], "open")
        self.assertIsNone(transferred["waiting_for"])

        self.store.touch("cursor", window_id="w-3", workspace=self.proj)
        pending = self.store.create_case(agent="opencode", title="待认领", workspace=self.proj)
        self.store.append_case_node(
            pending["id"], agent="opencode", note="等人", status="waiting", waiting_for="cursor"
        )
        claimed = self.store.claim_case(pending["id"], agent="cursor", window_id="w-3")
        self.assertEqual(claimed["status"], "open")
        self.assertIsNone(claimed["waiting_for"])

    # ── D-8：窗口 ↔ agent 绑定不可被异 agent 改写 ──────────────────────────
    def test_d8_window_agent_binding_preserved_and_spoof_rejected(self):
        # 存储层：异 agent 触达保留既有 agent
        self.store.touch("cursor", window_id="w-own", workspace=self.proj)
        self.store.touch("kimi", window_id="w-own", workspace=self.proj)
        self.assertEqual(self.store.get_window("w-own")["agent"], "cursor")

        # HTTP 层：凭 X-Window 冒用他人窗口号 → 409
        status, _ = _http(
            self.url, "POST", "/v1/windows", agent="cursor", body={"window_id": "w-http", "workspace": self.proj}
        )
        self.assertEqual(status, 200)
        reused = _http(self.url, "GET", "/v1/inbox", agent="kimi", window="w-http")
        self.assertEqual(reused[0], 409)
        self.assertEqual(reused[1].get("error"), "window_conflict")

    # ── M1-data：`ic agents` 未读口径与 events.unread_total 对齐 ───────────
    def test_m1_data_unread_counts_exclude_claimed_and_sealed(self):
        self.store.touch("cursor", window_id="w-1", workspace=self.proj)
        m1 = self.store.send("opencode", ["cursor"], "未读一", "正文", workspace=self.proj)
        self.assertEqual({a["name"]: a["unread"] for a in self.store.list_agents()}["cursor"], 1)
        # 被窗口认领后，角标不再虚高
        self.store.events("cursor", since=0, wait=0, window_id="w-1")
        self.assertEqual({a["name"]: a["unread"] for a in self.store.list_agents()}["cursor"], 0)

        # 封卷线程里的新信（不继承认领）也不计入角标
        m2 = self.store.send("opencode", ["cursor"], "要封卷", "正文", workspace=self.proj)
        self.store.mark("cursor", m2["id"], "read", window_id="w-1")
        self.store.case_acceptance_set(m2["thread_id"], "cursor", "封卷后的新信不计未读")
        self.store.close_case(m2["thread_id"], agent="cursor", window_id="w-1")
        self.store.reply("opencode", m2["id"], "封卷后回音", to=["cursor"])
        self.assertEqual({a["name"]: a["unread"] for a in self.store.list_agents()}["cursor"], 0)

    # ── M2：X-Pid 请求头补齐窗口进程号 ─────────────────────────────────────
    def test_m2_x_pid_header_populates_window_pid(self):
        status, _ = _http(self.url, "GET", "/v1/inbox", agent="cursor", window="w-pid", pid=4321)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get_window("w-pid")["pid"], 4321)
        status, _ = _http(self.url, "GET", "/v1/events", agent="cursor", window="w-pid2", pid=555)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get_window("w-pid2")["pid"], 555)

    # ── M3：同名目录跨项目不串信（收紧 basename 回退） ─────────────────────
    def test_m3_workspace_scope_tightening(self):
        # 裸目录名 ↔ 全路径 仍命中（守护上报形态）
        self.assertTrue(_workspace_scope("proj", "/tmp/a/proj"))
        # 两条相对多段路径即便同名也不再互串
        self.assertFalse(_workspace_scope("work/proj", "other/proj"))
        # 既有联邦约定：两个绝对路径同名目录仍对齐导出
        self.assertTrue(_workspace_scope("/a/proj", "/b/proj"))
        self.assertFalse(_workspace_scope("/a/proj", "/b/other"))

    # ── L2：case id / thread_id 安全字符集与长度 ───────────────────────────
    def test_l2_invalid_case_ids_rejected(self):
        for bad in ("bad id", "a" * 65, "line\nbreak", "quote'id", "slash/id", "semi;id"):
            with self.subTest(bad=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.get_case(bad)
                self.assertEqual(ctx.exception.code, "invalid_case_id")
                self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(MailError) as create:
            self.store.create_case(agent="opencode", title="t", thread_id="bad id")
        self.assertEqual(create.exception.code, "invalid_case_id")
        with self.assertRaises(MailError) as thread:
            self.store.thread("cursor", "bad id")
        self.assertEqual(thread.exception.code, "invalid_case_id")
        # HTTP：非法 id → 400（而不是 500 / 落到别的路由）
        status, payload = _http(self.url, "GET", "/v1/cases/bad%20id")
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("error"), "invalid_case_id")


if __name__ == "__main__":
    unittest.main()
