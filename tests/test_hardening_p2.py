"""M5 debug 轮 P2 剩余项的回归测试。

覆盖：D-6/D-7/D-8、C-2/C-9/C-10/C-11/C-12/C-13/C-14/C-15/C-16、
M2/M4、U-3/U-5/U-6、N1/N2/N3、L2/L3。全部走临时目录，不动现网。
"""

import contextlib
import http.client
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli, client, config, crew, mcp_server
from infocenter.server import make_handler
from infocenter.store import MailError, Store


class _WatchStub:
    def __init__(self, unread=0):
        self.unread = unread


class P2TestBase(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        # 宿主会话的环境不能漏进被测请求：守护给 headless 子进程注入
        # INFOCENTER_NO_PID=1（防短命进程改宿主窗口 pid），而 `client.request` 看到这个开关
        # 就把 X-Pid 与体里的 pid 一起丢掉 ⇒ 本模块里“显式上报 pid”的用例在 agent 会话里**单跑必红**
        # （全量绿只是被 test_d2_commands 先行清环境掩盖了，同已修的 env 泄漏三处一类）。
        # 要测“抑制”本身的那些用例自己设这个变量（见 test_d2_commands），不靠宿主漏进来。
        os.environ.pop("INFOCENTER_NO_PID", None)
        os.environ.pop("INFOCENTER_WINDOW", None)
        os.environ.pop("INFOCENTER_AGENT", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.project = str((Path(self.tmp.name) / "proj").resolve())
        Path(self.project).mkdir(parents=True, exist_ok=True)
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


# ── store / httpapi：D-6/D-7/D-8、U-3、N3 ────────────────────────────────────


class StoreP2Test(P2TestBase):
    def test_d6_half_fields_strictly_rejected(self):
        # to-only、body-only（即便指定了主办）都一律 400，不再有「兼容变体」。
        with self.assertRaises(MailError) as only_to:
            self.store.create_case(agent="opencode", title="半字段", to="kimi")
        self.assertEqual(only_to.exception.code, "invalid_request")
        with self.assertRaises(MailError) as only_body:
            self.store.create_case(
                agent="opencode", title="半字段", body="只有正文",
                owner_agent="opencode", owner_window="w-9",
            )
        self.assertEqual(only_body.exception.code, "invalid_request")
        # 成对给则正常。
        full = self.store.create_case(agent="opencode", title="完整", to="kimi", body="正文")
        self.assertTrue(full["id"])
        # 只写开篇节点请走 note。
        noted = self.store.create_case(agent="opencode", title="只有节点", note="开篇")
        self.assertEqual(noted["nodes"][0]["note"], "开篇")

    def test_d6_http_body_without_to_is_400(self):
        try:
            client.request(self.url, "POST", "/v1/cases", "human", {"title": "t", "body": "x"})
        except client.ApiError as exc:
            self.assertEqual(exc.code, "invalid_request")
            self.assertEqual(exc.status, 400)
        else:  # pragma: no cover - 防御
            self.fail("body 缺 to 应被 400 拒绝")

    def test_d7_reopen_clears_waiting_for(self):
        self.store.touch("cursor", window_id="w-1", workspace=self.project)
        m = self.store.send("opencode", ["cursor"], "待谁", "正文", workspace=self.project)
        self.store.mark("cursor", m["id"], "read", window_id="w-1")
        cid = m["thread_id"]
        self.store.append_case_node(
            cid, agent="cursor", note="等评审", status="waiting", window_id="w-1", waiting_for="opencode"
        )
        # 封卷后再重开，waiting_for 必须清掉。
        self.store.close_case(cid, agent="cursor", window_id="w-1")
        reopened = self.store.reopen_case(cid, agent="cursor", window_id="w-1")
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["waiting_for"])

    def test_d8_window_agent_binding_blocks_spoof_http(self):
        self.store.touch("cursor", window_id="w-own", workspace=self.project)
        # 存储层：异 agent 触达保留既有 agent。
        self.store.touch("kimi", window_id="w-own", workspace=self.project)
        self.assertEqual(self.store.get_window("w-own")["agent"], "cursor")
        # HTTP：凭 X-Window 冒用他人窗口号建卷 → 409。
        try:
            client.request(
                self.url, "POST", "/v1/cases", "kimi",
                {"title": "冒用", "workspace": self.project}, window="w-own",
            )
        except client.ApiError as exc:
            self.assertEqual(exc.code, "window_conflict")
            self.assertEqual(exc.status, 409)
        else:  # pragma: no cover
            self.fail("异 agent X-Window 应 409")

    def test_u3_duplicate_ignores_remote_recipients(self):
        envelope = {
            "v": 1, "kind": "message", "id": "m00000000000001",
            "thread_id": "m00000000000001", "reply_to": None,
            "from": "alice.grok", "to": ["bob.cursor", "carol.kimi"],
            "subject": "联邦", "body": "正文", "priority": "normal",
            "workspace": "/p", "workspace_name": None,
            "created_at": "2026-10-14T00:00:00Z",
        }
        first = self.store.import_remote(envelope, ["cursor"])
        self.assertEqual(first["status"], "imported")
        # 只改他机收件人：按降级规格仍是 duplicate（本地投递收件人一致）。
        changed = dict(envelope)
        changed["to"] = ["bob.cursor", "dave.opencode"]
        self.assertEqual(self.store.import_remote(changed, ["cursor"])["status"], "duplicate")
        # 本地投递收件人变化 → conflict。
        self.assertEqual(self.store.import_remote(envelope, ["kimi"])["status"], "conflict")

    def test_n3_events_unread_total_excludes_claimed(self):
        self.store.touch("cursor", window_id="w-n3", workspace=self.project)
        self.store.send("opencode", ["cursor"], "一", "正文", workspace=self.project)
        self.store.send("opencode", ["cursor"], "二", "正文", workspace=self.project)
        self.store.events("cursor", since=0, wait=0, limit=10, window_id="w-n3")
        # 无窗口路径未读数应与 _unread_counts(None) 口径一致：被认领的不计。
        snap = self.store.events("cursor", since=0, wait=0)
        self.assertEqual(snap["unread_total"], 0)

    def test_m3_workspace_scope_no_cross_project(self):
        from infocenter.store import _workspace_scope

        self.assertTrue(_workspace_scope("proj", self.project))
        self.assertFalse(_workspace_scope("work/proj", "other/proj"))

    def test_m2_client_sends_x_pid(self):
        window = "w-pid-m2"
        client.request(
            self.url, "GET", "/v1/inbox", "cursor",
            query={"window": window}, pid=5432,
        )
        self.assertEqual(self.store.get_window(window)["pid"], 5432)


# ── mcp：U-6 ────────────────────────────────────────────────────────────────


class McpP2Test(P2TestBase):
    def test_u6_read_recalibrates_unread_from_server(self):
        self.store.touch("cursor", window_id="w-u6", workspace=self.project)
        self.store.send("opencode", ["cursor"], "一", "正文", workspace=self.project)
        m2 = self.store.send("opencode", ["cursor"], "二", "正文", workspace=self.project)
        watch = _WatchStub(unread=99)
        result = mcp_server._call_tool(
            self.url, "cursor", "ic_read", {"id": m2["id"]}, watch=watch, window_id="w-u6"
        )
        self.assertFalse(result["isError"])
        # 服务端真实未读应为 1（读掉一封），不能被无条件自减到 98。
        self.assertEqual(watch.unread, 1)


# ── client：N1 ──────────────────────────────────────────────────────────────


class ClientP2Test(unittest.TestCase):
    def test_n1_http_exception_wrapped(self):
        def boom(*_a, **_k):
            raise http.client.IncompleteRead(b"partial", 100)

        with patch.object(client.urllib.request, "urlopen", side_effect=boom):
            with self.assertRaises(client.ApiError) as ctx:
                client.request("http://127.0.0.1:1", "GET", "/v1/inbox", "cursor")
        self.assertEqual(ctx.exception.code, "transport")


# ── crew：C-2/C-9/C-10/C-11/C-12/C-13/C-14/C-15、M4、N2 ─────────────────────


class CrewP2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.project = str((Path(self.tmp.name) / "proj").resolve())
        Path(self.project).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_c2_pid_guards(self):
        self.assertFalse(crew._pid_alive(0))
        self.assertFalse(crew._pid_alive(1))
        self.assertFalse(crew._pid_alive(-5))
        key = crew._key("grok", self.project)
        crew._write_pid_record(self.home, key, 0, "2026-10-14T00:00:00Z")
        self.assertIsNone(crew._read_pid(self.home, key))
        # down 不应对脏 pid 发信号。
        crew._write_meta(self.home, key, {"agent": "grok", "key": key, "dir": self.project})
        result = crew.down(self.home, self.project)
        self.assertEqual(result["killed"], [])

    def test_c9_reserve_pid_file_concurrency(self):
        key = crew._key("grok", self.project)
        # 并发 up 的占位（pid==0）→ already_running，不会各起一个守护。
        crew._write_pid_record(self.home, key, 0, "2026-10-14T00:00:00Z")
        with self.assertRaises(MailError) as ctx:
            crew._start_daemon(self.home, key, self.project, [sys.executable, "-c", "pass"])
        self.assertEqual(ctx.exception.code, "already_running")

    def test_c9_stale_pid_replaced_and_failure_cleans_up(self):
        key = crew._key("grok", self.project)
        crew._write_pid_record(self.home, key, 99999999, "2020-01-01T00:00:00Z")

        class Proc:
            pid = 5555

        with patch.object(crew.subprocess, "Popen", return_value=Proc()):
            pid = crew._start_daemon(self.home, key, self.project, [sys.executable, "-c", "pass"])
        self.assertEqual(pid, 5555)
        self.assertEqual(crew._load_pid_record(self.home, key)["pid"], 5555)

        key2 = crew._key("kimi", self.project)
        with patch.object(crew.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                crew._start_daemon(self.home, key2, self.project, [sys.executable, "-c", "pass"])
        self.assertFalse(crew._pid_file(self.home, key2).exists())

    def test_c9_live_daemon_rejected(self):
        key = crew._key("grok", self.project)
        crew._write_pid_record(self.home, key, 1234, "2026-10-14T00:00:00Z")
        with patch.object(crew, "_read_pid", return_value=1234):
            with self.assertRaises(MailError) as ctx:
                crew._start_daemon(self.home, key, self.project, [sys.executable, "-c", "pass"])
        self.assertEqual(ctx.exception.code, "already_running")

    def test_c10_signal_group_falls_back_to_kill(self):
        calls = []
        with patch.object(crew.os, "killpg", side_effect=ProcessLookupError), \
                patch.object(crew.os, "kill", side_effect=lambda pid, sig: calls.append((pid, sig))):
            ok = crew._signal_group(4321, signal.SIGTERM)
        self.assertTrue(ok)
        self.assertEqual(calls, [(4321, signal.SIGTERM)])

    def test_c11_interrupt_escalates_to_sigkill(self):
        key = crew._key("grok", self.project)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            crew._write_meta(self.home, key, {
                "agent": "grok", "key": key, "dir": self.project,
                "pid": os.getpid(), "headless_pid": proc.pid,
            })
            lines = crew._interrupt_headless(self.home, "grok", self.project)
            self.assertTrue(any("SIGKILL" in line for line in lines))
            deadline = time.time() + 3
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

    def test_c12_empty_agent_rejected(self):
        for action in ("pause", "abort"):
            with self.subTest(action=action):
                with self.assertRaises(MailError) as ctx:
                    crew.control(self.home, "http://127.0.0.1:1", action, "")
                self.assertEqual(ctx.exception.code, "unknown_agent")

    def test_c13_key_with_double_dash_identity(self):
        key = crew._key("a--b", self.project)
        seen = {}

        class Proc:
            pid = 7007

        def fake_popen(_cmd, **kwargs):
            seen["env"] = kwargs["env"]
            return Proc()

        with patch.object(crew.subprocess, "Popen", side_effect=fake_popen):
            crew._start_daemon(self.home, key, self.project, [sys.executable, "-c", "pass"])
        self.assertEqual(seen["env"]["INFOCENTER_AGENT"], "a--b")

    def test_c15_prompt_placeholder_hardening(self):
        self.assertEqual(crew._substitute_prompt("echo {prompt}", "hi"), "echo hi")
        self.assertEqual(crew._substitute_prompt('echo "{prompt}"', "a b"), "echo 'a b'")
        self.assertEqual(crew._substitute_prompt("echo '{prompt}'", "a b"), "echo 'a b'")
        with self.assertRaises(MailError) as ctx:
            crew._substitute_prompt('echo "{prompt} extra"', "x")
        self.assertEqual(ctx.exception.code, "unsafe_template")

    def test_m4_abort_and_close_interrupt_headless(self):
        config.load(self.home)  # 生成默认名单
        calls = []
        with patch.object(crew, "_api", return_value={}), \
                patch.object(crew, "_interrupt_headless", side_effect=lambda *a, **k: calls.append(a[1]) or []):
            crew.control(self.home, "http://127.0.0.1:1", "abort", "grok", self.project)
            crew.control(self.home, "http://127.0.0.1:1", "close", "grok", self.project)
            crew.control(self.home, "http://127.0.0.1:1", "resume", "grok", self.project)
        self.assertEqual(calls, ["grok", "grok"])

    def test_n2_retry_delay_backs_off_and_caps(self):
        self.assertEqual(crew._retry_delay(1), 2)
        self.assertEqual(crew._retry_delay(2), 4)
        self.assertEqual(crew._retry_delay(3), 8)
        self.assertEqual(crew._retry_delay(50), crew._HEADLESS_RETRY_MAX)


# ── config：C-14 ────────────────────────────────────────────────────────────


class ConfigP2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_c14_add_remove_keep_entry_unknown_fields(self):
        (self.home / "config.json").write_text(json.dumps({
            "agents": [
                {"name": "grok", "description": "Grok", "custom_flag": True},
                {"name": "kimi", "description": "Kimi", "note_x": "keep"},
            ],
            "node": "alice",
        }), encoding="utf-8")
        config.add(self.home, "opencode", "OpenCode")
        raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        by_name = {item["name"]: item for item in raw["agents"]}
        self.assertTrue(by_name["grok"]["custom_flag"])
        self.assertEqual(by_name["kimi"]["note_x"], "keep")
        self.assertEqual(raw["node"], "alice")
        config.remove(self.home, "kimi")
        raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        by_name = {item["name"]: item for item in raw["agents"]}
        self.assertNotIn("kimi", by_name)
        self.assertTrue(by_name["grok"]["custom_flag"])


# ── cli：C-16 ───────────────────────────────────────────────────────────────


class CliP2Test(unittest.TestCase):
    def _run_main(self, argv):
        buf = io.StringIO()
        stub = mock.Mock(return_value={"ok": 1})
        with patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf):
                code = cli.main(argv)
        return code, buf.getvalue(), stub

    def test_c16_globals_before_and_after_subcommand(self):
        code, out, stub = self._run_main(["--as", "x", "inbox", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": 1})
        self.assertEqual(stub.call_args[0][1], "x")

        code, out, _ = self._run_main(["--json", "--as", "y", "inbox"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": 1})

    def test_c16_value_equal_to_global_not_stolen(self):
        # --note=--json：值就是 --json，不能被当全局参数吞掉。
        code, _out, stub = self._run_main(["--as", "x", "case", "cid", "--note=--json"])
        self.assertEqual(code, 0)
        # 传给 call 的 body 里 note 应是 "--json"。
        body = stub.call_args[0][4]
        self.assertEqual(body.get("note"), "--json")


# ── web：U-5 / L3 ───────────────────────────────────────────────────────────


class WebP2Test(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "infocenter" / "web" / "index.html").read_text(encoding="utf-8")

    def test_u5_select_case_request_token(self):
        # UI 重设计后：案件详情改在设置弹窗内按需加载（loadCaseNodes），不再有分离详情面板的竞态
        self.assertIn("loadCaseNodes", self.html)
        self.assertIn('openSettings("case"', self.html)

    def test_l3_action_urls_encoded(self):
        self.assertIn('"/v1/cases/" + encodeURIComponent(id) + "/" + action', self.html)
        self.assertIn('"/v1/windows/" + encodeURIComponent(id) + "/leave"', self.html)
        self.assertIn('"/v1/windows/" + encodeURIComponent(id) + "/pause"', self.html)


class CaseStatusNeedsNoteTest(unittest.TestCase):
    """`ic case --status` 单独用是**静默失效**（走成"只看一眼"），实测踩过两次。

    状态变更必须留一行节点（服务端 `POST /v1/cases/{id}/nodes` 要求 note 非空）——
    所以 CLI 要么带着 `--note` 一起用，要么明确报错；绝不能"看着像改了、其实没改"。
    """

    def _run(self, argv):
        buf, err = io.StringIO(), io.StringIO()
        stub = mock.Mock(return_value={"ok": 1})
        with patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        return code, buf.getvalue(), err.getvalue(), stub

    def test_单独给_status_明确报错而不是静默看一眼(self):
        code, out, err, stub = self._run(["--as", "x", "case", "cid", "--status", "待谁"])
        self.assertEqual(code, 1)
        self.assertIn("status_needs_note", err)
        self.assertIn("--note", err)
        self.assertFalse(stub.called, "没报错就该拒掉，而不是去打服务端")

    def test_status_带_note_照旧生效(self):
        code, _out, err, stub = self._run(
            ["--as", "x", "case", "cid", "--note", "为什么改", "--status", "待谁"]
        )
        self.assertEqual(code, 0, err)
        body = stub.call_args[0][4]
        self.assertEqual(body.get("status"), "待谁")
        self.assertEqual(body.get("note"), "为什么改")

    def test_milestone_done_那条路也不许悄悄吞_status(self):
        code, _out, err, _stub = self._run(
            ["--as", "x", "case", "cid", "--milestone-done", "1", "--status", "待谁"]
        )
        self.assertEqual(code, 1)
        self.assertIn("status_needs_note", err)



if __name__ == "__main__":
    unittest.main()
