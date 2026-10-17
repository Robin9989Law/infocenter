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

# Windows 的 select 只能等套接字。匿名管道上直接 read() 是同步 ReadFile，
# 没有数据时线程会被挂起，外层 deadline 永远执行不到。先探测再读。
if sys.platform == "win32":
    import msvcrt
    from ctypes import WinDLL, byref, get_last_error, wintypes

    _kernel32 = WinDLL("kernel32", use_last_error=True)
    _kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL
    # 写端已关闭。缓冲区排空后 PeekNamedPipe 失败，随后的 read 会立刻得到 b""。
    _PIPE_EOF = {109, 232, 233}  # ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED

    def _peek_avail(stream) -> int | None:
        handle = msvcrt.get_osfhandle(stream.fileno())
        avail = wintypes.DWORD()
        ok = _kernel32.PeekNamedPipe(handle, None, 0, None, byref(avail), None)
        if ok:
            return int(avail.value)
        if get_last_error() in _PIPE_EOF:
            return None
        raise OSError(get_last_error(), "PeekNamedPipe failed")

    def _wait_readable(stream, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            avail = _peek_avail(stream)
            if avail is None or avail > 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))

    def _read_ready(stream, max_n: int) -> bytes:
        # ReadFile 请求的字节数多于当前缓冲时会一直阻塞到凑齐。只读已经在管道里的。
        avail = _peek_avail(stream)
        if avail is None or avail <= 0 or max_n <= 0:
            return b""
        return stream.read(min(max_n, avail)) or b""
else:
    def _wait_readable(stream, timeout: float) -> bool:
        ready, _, _ = select.select([stream], [], [], max(0.0, timeout))
        return bool(ready)

    def _read_ready(stream, max_n: int) -> bytes:
        if max_n <= 0:
            return b""
        return stream.read(max_n) or b""


def _read_some(stream, max_n: int, deadline: float, pending: bytes) -> bytes:
    """读最多 max_n 个已到达的字节。超时抛 TimeoutError，管道关闭返回 b""。"""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _wait_readable(stream, remaining):
            raise TimeoutError(f"timed out, received: {pending!r}")
        if sys.platform == "win32" and _peek_avail(stream) is None:
            return b""
        chunk = _read_ready(stream, max_n)
        if chunk:
            return chunk
        if sys.platform != "win32":
            return b""
        # 探测到数据但这一次没读到。立刻再转会占满一个核，语言服务的看门狗会把宿主一起杀掉。
        time.sleep(0.02)


def lsp_frame(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data


def line_frame(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def read_lsp(stream, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        ch = _read_some(stream, 1, deadline, buf)
        if not ch:
            raise EOFError(buf)
        buf += ch
    head, rest = buf.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        chunk = _read_some(stream, length - len(rest), deadline, rest)
        if not chunk:
            raise EOFError(rest)
        rest += chunk
    return json.loads(rest[:length].decode("utf-8"))


def read_line(stream, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        ch = _read_some(stream, 1, deadline, buf)
        if not ch:
            raise EOFError(buf)
        buf += ch
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


class McpQualityTest(unittest.TestCase):
    def setUp(self):
        self._proc: subprocess.Popen | None = None
        # Antigravity 把超过 WaitMsBeforeAsync（常见 5–8 秒）还活着的命令收成后台任务，
        # 语言服务随后会以退出码 0 重启，整段会话被取消。这里到点直接结束进程。
        self._watchdog = threading.Timer(7, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()
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

    def _expire(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
        os._exit(3)

    def tearDown(self):
        stopper = threading.Thread(target=self.httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(2)
        try:
            self.httpd.server_close()
        except OSError:
            pass
        try:
            self.tmp.cleanup()
        except OSError:
            pass
        self._watchdog.cancel()

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
        kwargs: dict = {}
        if sys.platform == "win32":
            # 不挂到 Antigravity 语言服务的控制台上，否则结束这个子进程会把宿主一起带走。
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, IC, "mcp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env(),
            bufsize=0,
            **kwargs,
        )
        self._proc = proc
        return proc

    def _send(self, proc: subprocess.Popen, payload: bytes) -> None:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.flush()

    def _close_proc(self, proc: subprocess.Popen) -> None:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if self._proc is proc:
            self._proc = None

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
            self._send(proc, lsp_frame({
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
            self._send(proc, lsp_frame({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ic_peek", "arguments": {"id": first["id"]}},
            }))
            peek_res = read_lsp(proc.stdout)
            self.assertFalse(peek_res["result"]["isError"])
            self.assertIn("提醒：你还有 2 封未读留言", peek_res["result"]["content"][0]["text"])

            # 逐封签收后，未读数递减至 0，工具尾巴不再带提醒
            self._send(proc, lsp_frame({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ic_read", "arguments": {"id": first["id"]}},
            }))
            read1 = read_lsp(proc.stdout)
            self.assertIn("提醒：你还有 1 封未读留言", read1["result"]["content"][0]["text"])

            self._send(proc, lsp_frame({
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
            self._send(proc, b"\n  \n" + line_frame({
                "jsonrpc": "2.0", "id": 10, "method": "initialize",
                "params": {"protocolVersion": "1999-01-01", "capabilities": {}},
            }))
            init = read_line(proc.stdout)
            # 非 202x 协议号自动回退到默认 PROTOCOL
            self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

            self._send(proc, line_frame({"jsonrpc": "2.0", "id": 11, "method": "ping"}))
            ping = read_line(proc.stdout)
            self.assertEqual(ping["result"], {})

            self._send(proc, line_frame({"jsonrpc": "2.0", "id": 12, "method": "tools/list"}))
            tools = read_line(proc.stdout)["result"]["tools"]
            self.assertEqual(len(tools), 15)
            tool_names = {t["name"] for t in tools}
            for expected in ("ic_lock", "ic_unlock", "ic_queue", "ic_case_note", "ic_task"):
                self.assertIn(expected, tool_names)
            for t in tools:
                self.assertIn("agent", t["inputSchema"]["required"])

            # 缺少 agent 参数应报错 isError=True
            self._send(proc, line_frame({
                "jsonrpc": "2.0", "id": 13, "method": "tools/call",
                "params": {"name": "ic_agents", "arguments": {}},
            }))
            missing_agent = read_line(proc.stdout)
            self.assertTrue(missing_agent["result"]["isError"])
            self.assertIn("缺少 agent", missing_agent["result"]["content"][0]["text"])

            # 传入 agent=kimi 调用 ic_activity，结果应为纯 JSON，无未读提醒尾巴
            self._send(proc, line_frame({
                "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"name": "ic_activity", "arguments": {"agent": "kimi", "activity": "独立复核中"}},
            }))
            act = read_line(proc.stdout)
            self.assertFalse(act["result"]["isError"])
            text = act["result"]["content"][0]["text"]
            self.assertNotIn("提醒：你还有", text)
            self.assertEqual(json.loads(text)["activity"], "独立复核中")

            # 未知方法与未知工具
            self._send(proc, line_frame({"jsonrpc": "2.0", "id": 15, "method": "unknown/method"}))
            unk_method = read_line(proc.stdout)
            self.assertEqual(unk_method["error"]["code"], -32601)

            self._send(proc, line_frame({
                "jsonrpc": "2.0", "id": 16, "method": "tools/call",
                "params": {"name": "ic_nonexistent", "arguments": {"agent": "kimi"}},
            }))
            unk_tool = read_line(proc.stdout)
            self.assertTrue(unk_tool["result"]["isError"])
            self.assertIn("未知工具", unk_tool["result"]["content"][0]["text"])
        finally:
            self._close_proc(proc)

    def test_p1_u4_invalid_mcp_window_or_agent_exits_cleanly_without_traceback(self):
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        env_bad_win = os.environ.copy()
        env_bad_win["INFOCENTER_WINDOW"] = "../bad window"
        res1 = subprocess.run(
            [sys.executable, IC, "--url", self.url, "--as", "cursor", "mcp"],
            env=env_bad_win,
            capture_output=True,
            text=True,
            timeout=5,
            **flags,
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
            **flags,
        )
        self.assertNotEqual(res2.returncode, 0)
        self.assertIn("invalid_name:", res2.stderr)
        self.assertNotIn("Traceback", res2.stderr)


class PipeReadTimeoutTest(unittest.TestCase):
    """读子进程管道必须在超时内返回。漏掉 Windows 就绪检测会把测试进程永久挂死。"""

    def _close(self, proc: subprocess.Popen) -> None:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def test_silent_pipe_times_out(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdout is not None
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                read_lsp(proc.stdout, timeout=0.4)
            with self.assertRaises(TimeoutError):
                read_line(proc.stdout, timeout=0.4)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            self._close(proc)

    def test_partial_frame_then_silence_times_out(self):
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.stdout.buffer.write(b'Content-Length: 5\\r\\n');"
             "sys.stdout.buffer.flush(); time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdout is not None
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                read_lsp(proc.stdout, timeout=0.4)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            self._close(proc)

    def test_closed_pipe_is_eof(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdout is not None
            proc.wait(timeout=5)
            started = time.monotonic()
            with self.assertRaises(EOFError):
                read_lsp(proc.stdout, timeout=2)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            self._close(proc)

    def test_lsp_frame_roundtrip(self):
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(lsp_frame({"ok": True}))
            proc.stdin.close()
            self.assertEqual(read_lsp(proc.stdout, timeout=2), {"ok": True})
        finally:
            self._close(proc)


if __name__ == "__main__":
    unittest.main()
