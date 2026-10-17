import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import ApiError, request
from infocenter.server import make_handler
from infocenter.store import MailError, Store, normalize_name, parse_recipients


class StoreBoundaryAndConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        # 宿主 agent 会话的窗口号会经 client.request 漏进被测请求
        # （窗口先被前一个身份占掉 ⇒ 后一个身份 409 window_conflict）
        for key in ("INFOCENTER_WINDOW", "INFOCENTER_AGENT", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
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

    def test_limit_boundaries(self):
        for bad in (0, -1, 101, "abc", None):
            with self.subTest(limit=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.inbox("cursor", limit=bad)
                self.assertEqual(ctx.exception.code, "invalid_limit")
                self.assertEqual(ctx.exception.status, 400)
                with self.assertRaises(MailError) as ev_ctx:
                    self.store.events("cursor", limit=bad)
                self.assertEqual(ev_ctx.exception.code, "invalid_limit")

        self.assertEqual(self.store.inbox("cursor", limit=1)["count"], 0)
        self.assertEqual(self.store.inbox("cursor", limit=100)["count"], 0)

    def test_subject_empty_overlong_and_newline(self):
        for bad in ("", "   ", "a" * 201, "第一行\n第二行", "回车\r换行"):
            with self.subTest(subject=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.send("cursor", "kimi", bad, "正文")
                self.assertEqual(ctx.exception.code, "invalid_subject")
                self.assertEqual(ctx.exception.status, 400)

        msg = self.store.send("cursor", "kimi", "a" * 200, "刚好两百字")
        self.assertEqual(len(msg["subject"]), 200)

    def test_body_empty_and_over_100k(self):
        for bad in ("", "   \n\t ", None, 123):
            with self.subTest(body=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.send("cursor", "kimi", "主题", bad)
                self.assertEqual(ctx.exception.code, "invalid_body")
                self.assertEqual(ctx.exception.status, 400)

        with self.assertRaises(MailError) as over:
            self.store.send("cursor", "kimi", "超长", "x" * 100_001)
        self.assertEqual(over.exception.code, "invalid_body")
        self.assertEqual(over.exception.status, 413)

        ok = self.store.send("cursor", "kimi", "满额", "x" * 100_000)
        self.assertEqual(ok["body_length"], 100_000)

    def test_broadcast_variants_and_mixed_recipients_rejected(self):
        for token in ("*", "all", "everyone", "ALL", " Everyone "):
            with self.subTest(token=token):
                self.assertEqual(parse_recipients(token), ["*"])
                sent = self.store.send("cursor", token, f"广播 {token}", "内容")
                self.assertNotIn("cursor", sent["to"])
                self.assertNotIn("all", sent["to"])
                self.assertNotIn("everyone", sent["to"])
                self.assertIn("kimi", sent["to"])

        for mixed in ("*,kimi", "kimi,*", "all,kimi", "everyone,grok", ["*", "kimi"], ["all", "kimi"], "*,*"):
            with self.subTest(mixed=mixed):
                with self.assertRaises(MailError) as ctx:
                    self.store.send("cursor", mixed, "混投", "内容")
                self.assertEqual(ctx.exception.code, "invalid_recipient")
                self.assertEqual(ctx.exception.status, 400)

        for empty in ("", " , , ", [], None, 123, [123]):
            with self.subTest(empty=empty):
                with self.assertRaises(MailError) as ctx:
                    parse_recipients(empty)
                self.assertEqual(ctx.exception.code, "invalid_recipient")

    def test_invalid_agent_names(self):
        for bad in ("", " ", "*", "all", "everyone", "ALL", ".lead", "-lead", "a..b", "a/b", "a b", "x" * 65):
            with self.subTest(name=bad):
                with self.assertRaises(MailError) as ctx:
                    normalize_name(bad)
                self.assertEqual(ctx.exception.code, "invalid_name")
                self.assertEqual(ctx.exception.status, 400)

        # 允许包含单点号（如节点.名字）但禁止连续点号 ..
        self.assertEqual(normalize_name("node1.grok"), "node1.grok")
        self.assertEqual(normalize_name("Agent_01-x"), "agent_01-x")

    def test_mark_non_recipient_and_visibility_isolation(self):
        msg = self.store.send("cursor", "kimi", "私信", "只有 kimi 收")
        # 发信人自己不是收件人，签收应返回 409 not_a_recipient
        with self.assertRaises(MailError) as sender_ack:
            self.store.mark("cursor", msg["id"], "read")
        self.assertEqual(sender_ack.exception.code, "not_a_recipient")
        self.assertEqual(sender_ack.exception.status, 409)

        # 局外人不可见，签收或读取都返回 404 not_found
        for action in (
            lambda: self.store.get_message("grok", msg["id"]),
            lambda: self.store.mark("grok", msg["id"], "read"),
            lambda: self.store.reply("grok", msg["id"], "插话"),
            lambda: self.store.thread("grok", msg["thread_id"]),
        ):
            with self.assertRaises(MailError) as outsider:
                action()
            self.assertEqual(outsider.exception.code, "not_found")
            self.assertEqual(outsider.exception.status, 404)

        with self.assertRaises(MailError) as bad_status:
            self.store.mark("kimi", msg["id"], "unread")
        self.assertEqual(bad_status.exception.code, "invalid_status")

    def test_thread_visibility_partial_participation(self):
        root = self.store.send("opencode", "cursor,kimi", "启动", "大家看")
        priv = self.store.reply("cursor", root["id"], "只回 opencode")
        sub_priv = self.store.reply("opencode", priv["id"], "收到，单独交代", to="cursor")

        # opencode 与 cursor 能看到全部 3 条
        self.assertEqual(self.store.thread("opencode", root["thread_id"])["count"], 3)
        self.assertEqual(self.store.thread("cursor", root["thread_id"])["count"], 3)
        # kimi 只参与了第 1 条，看不到后面 2 条私聊回合
        kimi_thread = self.store.thread("kimi", root["thread_id"])
        self.assertEqual([m["id"] for m in kimi_thread["messages"]], [root["id"]])
        with self.assertRaises(MailError) as ctx:
            self.store.get_message("kimi", sub_priv["id"])
        self.assertEqual(ctx.exception.status, 404)

    def test_thread_exposes_tree_structure_for_agents(self):
        root = self.store.send("opencode", "cursor,kimi", "启动", "大家看")
        first = self.store.reply("cursor", root["id"], "cursor 回根")
        second = self.store.reply("kimi", root["id"], "kimi 回根")
        sub = self.store.reply("opencode", first["id"], "回 cursor 那一支", to="cursor")

        payload = self.store.thread("opencode", root["thread_id"])
        by_id = {m["id"]: m for m in payload["messages"]}
        self.assertEqual(payload["count"], 4)

        # 根：无父、第 0 层、root 是自己、两个孩子
        self.assertIsNone(by_id[root["id"]]["parent_id"])
        self.assertEqual(by_id[root["id"]]["depth"], 0)
        self.assertEqual(by_id[root["id"]]["root_id"], root["id"])
        self.assertEqual(
            sorted(by_id[root["id"]]["children"]), sorted([first["id"], second["id"]])
        )

        # 两条直接回复：第 1 层、root 都是 root
        for child in (first, second):
            self.assertEqual(by_id[child["id"]]["parent_id"], root["id"])
            self.assertEqual(by_id[child["id"]]["depth"], 1)
            self.assertEqual(by_id[child["id"]]["root_id"], root["id"])

        # 深一层：只挂在 first 下，depth 递增
        self.assertEqual(by_id[sub["id"]]["parent_id"], first["id"])
        self.assertEqual(by_id[sub["id"]]["depth"], 2)
        self.assertEqual(by_id[sub["id"]]["root_id"], root["id"])
        self.assertEqual(by_id[first["id"]]["children"], [sub["id"]])
        self.assertEqual(by_id[second["id"]]["children"], [])

        # 仍按时间序（不改成先序）
        stamps = [m["created_at"] for m in payload["messages"]]
        self.assertEqual(stamps, sorted(stamps))

        # 部分可见：kimi 看不到 sub 那一支，其可见集内 root 的孩子只剩自己那条
        kimi_view = self.store.thread("kimi", root["thread_id"])
        kimi_by_id = {m["id"]: m for m in kimi_view["messages"]}
        self.assertEqual(sorted(kimi_by_id), sorted([root["id"], second["id"]]))
        self.assertEqual(kimi_by_id[root["id"]]["children"], [second["id"]])
        self.assertEqual(kimi_by_id[second["id"]]["depth"], 1)
        self.assertEqual(kimi_by_id[second["id"]]["root_id"], root["id"])

    def test_http_error_codes_and_validation_matrix(self):
        # 缺少 X-Agent -> 401
        req = urllib.request.Request(self.url + "/v1/inbox", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as no_agent:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(no_agent.exception.code, 401)
        no_agent.exception.close()

        # PUT 他人身份 -> 403；非 human DELETE 他人 -> 403
        with self.assertRaises(ApiError) as put_other:
            request(self.url, "PUT", "/v1/agents/kimi", "cursor", {"description": "越权"})
        self.assertEqual(put_other.exception.status, 403)
        self.assertEqual(put_other.exception.code, "not_you")

        with self.assertRaises(ApiError) as del_other:
            request(self.url, "DELETE", "/v1/agents/kimi", "cursor", {})
        self.assertEqual(del_other.exception.status, 403)
        self.assertEqual(del_other.exception.code, "not_you")

        # priority / workspace / reply_to 非法类型 -> 400
        for field, bad_val, expected_code in (
            ("priority", "urgent", "invalid_priority"),
            ("priority", False, "invalid_priority"),
            ("priority", 123, "invalid_priority"),
            ("workspace", ["bad"], "invalid_workspace"),
            ("workspace", "x" * 1025, "invalid_workspace"),
            ("reply_to", ["bad"], "invalid_reply_to"),
        ):
            payload = {"to": "kimi", "subject": "测", "body": "正文", field: bad_val}
            with self.subTest(field=field, val=bad_val):
                with self.assertRaises(ApiError) as ctx:
                    request(self.url, "POST", "/v1/messages", "cursor", payload)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, expected_code)

        # events 非法 since / wait -> 400
        with self.assertRaises(ApiError) as bad_since:
            request(self.url, "GET", "/v1/events", "cursor", query={"since": "abc"})
        self.assertEqual(bad_since.exception.code, "invalid_since")
        with self.assertRaises(ApiError) as bad_wait:
            request(self.url, "GET", "/v1/events", "cursor", query={"wait": "abc"})
        self.assertEqual(bad_wait.exception.code, "invalid_wait")

    def test_concurrent_long_poll_wake_all_and_concurrent_writes(self):
        waiters = ["kimi", "cursor", "grok", "opencode"]
        results: dict[str, dict] = {}
        errors: list[Exception] = []
        ready = threading.Barrier(len(waiters) + 1)

        def poll_worker(agent_name: str):
            try:
                ready.wait(timeout=5)
                res = request(
                    self.url,
                    "GET",
                    "/v1/events",
                    agent_name,
                    query={"since": "0", "wait": "10"},
                    timeout=15,
                )
                results[agent_name] = res
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=poll_worker, args=(a,), daemon=True) for a in waiters]
        for t in threads:
            t.start()
        ready.wait(timeout=5)
        time.sleep(0.15)

        # 同时并发 touch 新 agent 与广播发信，验证无锁饥饿、无 IntegrityError、所有等待者被唤醒
        def touch_worker(idx: int):
            try:
                request(self.url, "PUT", f"/v1/agents/bot{idx % 3}", f"bot{idx % 3}", {"activity": f"step {idx}"})
            except Exception as exc:
                errors.append(exc)

        writers = [threading.Thread(target=touch_worker, args=(i,), daemon=True) for i in range(12)]
        for w in writers:
            w.start()

        start_t = time.monotonic()
        request(self.url, "POST", "/v1/messages", "human", {
            "to": "*",
            "subject": "全体唤醒",
            "body": "并发长轮询唤醒验证",
        })
        for w in writers:
            w.join(timeout=5)
        for t in threads:
            t.join(timeout=5)
        elapsed = time.monotonic() - start_t

        self.assertEqual(errors, [])
        self.assertLess(elapsed, 3.0, "notify_all 应立即唤醒所有长轮询等待者，不应等到超时")
        for agent_name in waiters:
            self.assertIn(agent_name, results)
            self.assertEqual(len(results[agent_name]["new"]), 1)
            self.assertEqual(results[agent_name]["new"][0]["subject"], "全体唤醒")

    def test_p1_d5_non_string_types_return_400_not_500(self):
        cases = [
            ("POST", "/v1/messages", {"to": "kimi", "subject": 123, "body": "x"}, "invalid_subject"),
            ("POST", "/v1/messages", {"to": [123], "subject": "s", "body": "x"}, "invalid_recipient"),
            ("POST", "/v1/messages", {"to": "kimi", "subject": "s", "body": "x", "window": 123}, "invalid_window"),
            ("PUT", "/v1/agents/cursor", {"description": 123}, "invalid_description"),
            ("PUT", "/v1/agents/cursor", {"role": 123}, "invalid_role"),
            ("PUT", "/v1/agents/cursor", {"activity": 123}, "invalid_activity"),
            ("PUT", "/v1/agents/cursor", {"pid": "not-an-int"}, "invalid_pid"),
            ("PUT", "/v1/agents/cursor", {"pid": True}, "invalid_pid"),
            ("PUT", "/v1/agents/cursor", {"pid": -5}, "invalid_pid"),
            ("POST", "/v1/windows", {"id": "w-t1", "agent": 123}, "invalid_agent"),
            ("POST", "/v1/windows", {"id": "w-t1", "agent": "cursor", "state": 123}, "invalid_state"),
            ("POST", "/v1/projects", {"workspace": 123}, "invalid_workspace"),
            ("POST", "/v1/cases", {"id": "c-t1", "title": 123}, "invalid_title"),
            ("POST", "/v1/cases", {"id": 123, "title": "ok"}, "invalid_id"),
        ]
        for method, path, payload, expected_code in cases:
            with self.subTest(method=method, path=path, payload=payload):
                with self.assertRaises(ApiError) as ctx:
                    request(self.url, method, path, "cursor", payload)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, expected_code)

    def test_p1_u2_negative_and_invalid_content_length_returns_400(self):
        host, port = self.httpd.server_address
        for bad_cl in ("-5", "abc"):
            with self.subTest(content_length=bad_cl):
                with socket.create_connection((host, port), timeout=5) as sock:
                    raw_req = (
                        f"POST /v1/cases HTTP/1.1\r\n"
                        f"Host: {host}:{port}\r\n"
                        f"X-Agent: cursor\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {bad_cl}\r\n"
                        f"Connection: close\r\n\r\n"
                    ).encode("ascii")
                    sock.sendall(raw_req)
                    chunks = []
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    resp = b"".join(chunks).decode("utf-8", errors="replace")
                self.assertIn("400 Bad Request", resp)
                self.assertIn("invalid_content_length", resp)

    def test_p1_l1_csrf_origin_sec_fetch_site_and_content_type_guards(self):
        def raw_http(path: str, headers: dict[str, str], body: bytes = b"{}") -> tuple[int, dict]:
            req = urllib.request.Request(self.url + path, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    data = json.loads(exc.read().decode("utf-8"))
                finally:
                    exc.close()
                return exc.code, data

        # 1. 跨源 Origin 拒绝 -> 403 csrf_rejected
        code, data = raw_http(
            "/v1/cases",
            {"X-Agent": "cursor", "Content-Type": "application/json", "Origin": "https://evil.example"},
            b'{"title": "csrf"}',
        )
        self.assertEqual(code, 403)
        self.assertEqual(data["error"], "csrf_rejected")

        # 2. Origin: null 拒绝 -> 403 csrf_rejected
        code, data = raw_http(
            "/v1/cases",
            {"X-Agent": "cursor", "Content-Type": "application/json", "Origin": "null"},
            b'{"title": "csrf"}',
        )
        self.assertEqual(code, 403)
        self.assertEqual(data["error"], "csrf_rejected")

        # 3. Sec-Fetch-Site: cross-site 拒绝 -> 403 csrf_rejected
        code, data = raw_http(
            "/v1/cases",
            {"X-Agent": "cursor", "Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            b'{"title": "csrf"}',
        )
        self.assertEqual(code, 403)
        self.assertEqual(data["error"], "csrf_rejected")

        # 4. Content-Type: text/plain 简单请求伪装拒绝 -> 415 invalid_content_type
        code, data = raw_http(
            "/v1/cases",
            {"X-Agent": "cursor", "Content-Type": "text/plain"},
            b'{"title": "csrf"}',
        )
        self.assertEqual(code, 415)
        self.assertEqual(data["error"], "invalid_content_type")

        # 5. 无 X-Agent 且无 Content-Type 的 D 路由写操作拒绝 -> 415 invalid_content_type
        req = urllib.request.Request(self.url + "/v1/cases", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as exc_ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(exc_ctx.exception.code, 415)
        exc_ctx.exception.close()

        # 6. 同源 Origin + Sec-Fetch-Site: same-origin + application/json 正常放行
        code, data = raw_http(
            "/v1/cases",
            {
                "X-Agent": "cursor",
                "Content-Type": "application/json",
                "Origin": self.url,
                "Sec-Fetch-Site": "same-origin",
            },
            b'{"id": "c-same-origin", "title": "safe"}',
        )
        self.assertIn(code, (200, 201))
        self.assertEqual(data["id"], "c-same-origin")


if __name__ == "__main__":
    unittest.main()
