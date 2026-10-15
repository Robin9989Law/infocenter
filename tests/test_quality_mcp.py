import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import request
from infocenter.server import make_handler
from infocenter.store import Store

IC = str(ROOT / "bin" / "ic")


def lsp_frame(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data


def line_frame(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def read_lsp(stream, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError(buf)
        ready, _, _ = select.select([stream], [], [], 0.1)
        if not ready:
            continue
        ch = stream.read(1)
        if not ch:
            raise EOFError(buf)
        buf += ch
    head, rest = buf.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        if time.time() > deadline:
            raise TimeoutError(rest)
        ready, _, _ = select.select([stream], [], [], 0.1)
        if not ready:
            continue
        chunk = stream.read(length - len(rest))
        if not chunk:
            raise EOFError(rest)
        rest += chunk
    return json.loads(rest[:length].decode("utf-8"))


def read_line(stream, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    buf = b""
    while b"\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError(buf)
        ready, _, _ = select.select([stream], [], [], 0.1)
        if not ready:
            continue
        ch = stream.read(1)
        if not ch:
            raise EOFError(buf)
        buf += ch
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


class McpQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("grok", "Grok", ""),
                ("kimi", "Kimi Code", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
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

    def _env(self) -> dict:
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        # 宿主若是 agent 会话，INFOCENTER_AGENT/WINDOW 会漏进被测子进程
        # （身份被预绑 ⇒ tools/list 不再要求显式 agent 参数，test_line_json 必红）
        env.pop("INFOCENTER_AGENT", None)
        env.pop("INFOCENTER_WINDOW", None)
        return env

    def _spawn(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, IC, "mcp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            bufsize=0,
        )

    def _close_proc(self, proc: subprocess.Popen) -> None:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def test_lsp_framing_initial_watch_backlog_and_reminder_tail(self):
        # 启动前先塞 2 封未读，验证 LSP 模式下 _Watch 首帧 since=0 补报也是 LSP 帧格式
        first = request(self.url, "POST", "/v1/messages", "opencode", {
            "to": "cursor", "subject": "积压一", "body": "第一封",
        })
        second = request(self.url, "POST", "/v1/messages", "opencode", {
            "to": "cursor", "subject": "积压二", "body": "第二封", "workspace": "/tmp/ws",
        })
        proc = self._spawn("--agent", "cursor")
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(lsp_frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }))
            init = read_lsp(proc.stdout)
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

            # _Watch 首帧 since=0 补报积压未读，同样使用 Content-Length 帧
            note = read_lsp(proc.stdout)
            self.assertEqual(note["method"], "notifications/message")
            self.assertIn("2 封未读", note["params"]["data"])
            self.assertIn("积压二", note["params"]["data"])
            self.assertIn("等 2 封", note["params"]["data"])
            self.assertIn("workspace：/tmp/ws", note["params"]["data"])

            # 未读未清零前调用非 ic_inbox 工具，返回文本尾部带未读提醒
            proc.stdin.write(lsp_frame({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ic_peek", "arguments": {"id": first["id"]}},
            }))
            peek_res = read_lsp(proc.stdout)
            self.assertFalse(peek_res["result"]["isError"])
            self.assertIn("提醒：你还有 2 封未读留言", peek_res["result"]["content"][0]["text"])

            # 逐封签收后，未读数递减至 0，工具尾巴不再带提醒
            proc.stdin.write(lsp_frame({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ic_read", "arguments": {"id": first["id"]}},
            }))
            read1 = read_lsp(proc.stdout)
            self.assertIn("提醒：你还有 1 封未读留言", read1["result"]["content"][0]["text"])

            proc.stdin.write(lsp_frame({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "ic_archive", "arguments": {"id": second["id"]}},
            }))
            read2 = read_lsp(proc.stdout)
            self.assertNotIn("提醒：你还有", read2["result"]["content"][0]["text"])
        finally:
            self._close_proc(proc)

    def test_line_json_no_notify_and_unlocked_agent_tools(self):
        request(self.url, "POST", "/v1/messages", "grok", {
            "to": "kimi", "subject": "静默测试", "body": "不推通知",
        })
        # 不传 --agent 且开启 --no-notify：验证每个工具显式传 agent、无通知帧、无提醒尾巴
        proc = self._spawn("--no-notify")
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(b"\n  \n" + line_frame({
                "jsonrpc": "2.0", "id": 10, "method": "initialize",
                "params": {"protocolVersion": "1999-01-01", "capabilities": {}},
            }))
            init = read_line(proc.stdout)
            # 非 202x 协议号自动回退到默认 PROTOCOL
            self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

            proc.stdin.write(line_frame({"jsonrpc": "2.0", "id": 11, "method": "ping"}))
            ping = read_line(proc.stdout)
            self.assertEqual(ping["result"], {})

            proc.stdin.write(line_frame({"jsonrpc": "2.0", "id": 12, "method": "tools/list"}))
            tools = read_line(proc.stdout)["result"]["tools"]
            self.assertEqual(len(tools), 10)
            for t in tools:
                self.assertIn("agent", t["inputSchema"]["required"])

            # 缺少 agent 参数应报错 isError=True
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 13, "method": "tools/call",
                "params": {"name": "ic_agents", "arguments": {}},
            }))
            missing_agent = read_line(proc.stdout)
            self.assertTrue(missing_agent["result"]["isError"])
            self.assertIn("缺少 agent", missing_agent["result"]["content"][0]["text"])

            # 传入 agent=kimi 调用 ic_activity，结果应为纯 JSON，无未读提醒尾巴
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"name": "ic_activity", "arguments": {"agent": "kimi", "activity": "独立复核中"}},
            }))
            act = read_line(proc.stdout)
            self.assertFalse(act["result"]["isError"])
            text = act["result"]["content"][0]["text"]
            self.assertNotIn("提醒：你还有", text)
            self.assertEqual(json.loads(text)["activity"], "独立复核中")

            # 未知方法与未知工具
            proc.stdin.write(line_frame({"jsonrpc": "2.0", "id": 15, "method": "unknown/method"}))
            unk_method = read_line(proc.stdout)
            self.assertEqual(unk_method["error"]["code"], -32601)

            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 16, "method": "tools/call",
                "params": {"name": "ic_nonexistent", "arguments": {"agent": "kimi"}},
            }))
            unk_tool = read_line(proc.stdout)
            self.assertTrue(unk_tool["result"]["isError"])
            self.assertIn("未知工具", unk_tool["result"]["content"][0]["text"])
        finally:
            self._close_proc(proc)

    def test_p1_u4_invalid_mcp_window_or_agent_exits_cleanly_without_traceback(self):
        env_bad_win = os.environ.copy()
        env_bad_win["INFOCENTER_WINDOW"] = "../bad window"
        res1 = subprocess.run(
            [sys.executable, IC, "--url", self.url, "--as", "cursor", "mcp"],
            env=env_bad_win,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(res1.returncode, 0)
        self.assertIn("invalid_window:", res1.stderr)
        self.assertNotIn("Traceback", res1.stderr)

        env_ok = os.environ.copy()
        env_ok.pop("INFOCENTER_WINDOW", None)
        res2 = subprocess.run(
            [sys.executable, IC, "--url", self.url, "--as", "bad/agent", "mcp"],
            env=env_ok,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(res2.returncode, 0)
        self.assertIn("invalid_name:", res2.stderr)
        self.assertNotIn("Traceback", res2.stderr)


if __name__ == "__main__":
    unittest.main()
