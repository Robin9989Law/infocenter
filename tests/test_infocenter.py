import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http.server import ThreadingHTTPServer

from infocenter.client import ApiError, request
from infocenter.server import healthy, make_handler
from infocenter.store import MailError, Store

IC = str(ROOT / "bin" / "ic")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def frame(obj) -> bytes:
    data = json.dumps(obj).encode()
    return f"Content-Length: {len(data)}\r\n\r\n".encode() + data


def line_frame(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def read_line_message(stream, timeout=5):
    deadline = time.time() + timeout
    buf = b""
    while b"\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError(buf)
        ready, _, _ = select.select([stream], [], [], 0.2)
        if not ready:
            continue
        chunk = stream.read(1)
        if not chunk:
            raise EOFError(buf)
        buf += chunk
    line, _rest = buf.split(b"\n", 1)
    return json.loads(line)


def read_frame(stream, timeout=5):
    deadline = time.time() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError(buf)
        ready, _, _ = select.select([stream], [], [], 0.2)
        if not ready:
            continue
        chunk = stream.read(1)
        if not chunk:
            raise EOFError(buf)
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        if time.time() > deadline:
            raise TimeoutError(rest)
        ready, _, _ = select.select([stream], [], [], 0.2)
        if not ready:
            continue
        chunk = stream.read(length - len(rest))
        if not chunk:
            raise EOFError(rest)
        rest += chunk
    return json.loads(rest[:length].decode())


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""), ("kimi", "Kimi Code", ""), ("opencode", "OpenCode", ""),
            ("cursor", "Cursor", ""), ("human", "用户", ""),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_peek_does_not_consume_and_read_is_per_recipient(self):
        sent = request(self.url, "POST", "/v1/messages", "grok", {
            "to": "kimi,cursor",
            "subject": "看一下",
            "body": "接口改了",
            "priority": "high",
        })
        self.assertEqual(sent["to"], ["cursor", "kimi"])
        self.assertEqual(sent["priority"], "high")
        kimi = request(self.url, "GET", "/v1/inbox", "kimi")
        self.assertEqual(kimi["count"], 1)
        self.assertNotIn("body", kimi["messages"][0])
        again = request(self.url, "GET", "/v1/inbox", "kimi")
        self.assertEqual(again["count"], 1)
        read = request(self.url, "POST", f"/v1/messages/{sent['id']}/read", "kimi", {})
        self.assertEqual(read["body"], "接口改了")
        self.assertEqual(read["delivery_status"], "read")
        self.assertEqual(request(self.url, "GET", "/v1/inbox", "kimi")["count"], 0)
        self.assertEqual(request(self.url, "GET", "/v1/inbox", "cursor")["count"], 1)

    def test_broadcast_skips_sender_and_private_thread_stays_private(self):
        sent = request(self.url, "POST", "/v1/messages", "grok", {
            "to": "*",
            "subject": "都看一下",
            "body": "下午对一下",
        })
        self.assertNotIn("grok", sent["to"])
        self.assertIn("kimi", sent["to"])
        self.assertEqual(request(self.url, "GET", "/v1/inbox", "grok")["count"], 0)
        reply = request(self.url, "POST", f"/v1/messages/{sent['id']}/reply", "kimi", {
            "body": "我来",
        })
        self.assertEqual(reply["subject"], "Re: 都看一下")
        self.assertEqual(reply["thread_id"], sent["thread_id"])
        self.assertEqual(reply["to"], ["grok"])
        thread = request(self.url, "GET", f"/v1/threads/{sent['thread_id']}", "grok")
        self.assertEqual(thread["count"], 2)
        visible = request(self.url, "GET", f"/v1/threads/{sent['thread_id']}", "cursor")
        self.assertEqual([item["id"] for item in visible["messages"]], [sent["id"]])
        with self.assertRaises(ApiError) as hidden:
            request(self.url, "GET", f"/v1/messages/{reply['id']}", "opencode")
        self.assertEqual(hidden.exception.status, 404)
        with self.assertRaises(ApiError) as ack:
            request(self.url, "POST", f"/v1/messages/{sent['id']}/read", "grok", {})
        self.assertEqual(ack.exception.status, 409)

    def test_note_to_self_unknown_agent_and_sender_cannot_ack(self):
        note = request(self.url, "POST", "/v1/messages", "human", {
            "to": "human",
            "subject": "记得",
            "body": "自己留一条",
        })
        self.assertEqual(request(self.url, "GET", "/v1/inbox", "human")["messages"][0]["id"], note["id"])
        request(self.url, "POST", "/v1/messages", "grok", {
            "to": "futurebot",
            "subject": "等你",
            "body": "先放着",
        })
        names = [item["name"] for item in request(self.url, "GET", "/v1/agents", "grok")["agents"]]
        self.assertIn("futurebot", names)
        with self.assertRaises(ApiError) as caught:
            request(self.url, "POST", f"/v1/messages/{note['id']}/read", "grok", {})
        self.assertEqual(caught.exception.status, 404)

    def test_join_page_and_register(self):
        health = request(self.url, "GET", "/health", "nobody")
        self.assertEqual(health["status"], "ok")
        # health does not require the header; request() always sends one. Call without it.
        import urllib.request

        with urllib.request.urlopen(self.url + "/v1", timeout=5) as response:
            page = json.loads(response.read().decode())
        self.assertIn("X-Agent", page["how_to_join"])
        with urllib.request.urlopen(self.url + "/openapi.json", timeout=5) as response:
            spec = json.loads(response.read().decode())
        self.assertIn("/v1/messages", spec["paths"])
        updated = request(self.url, "PUT", "/v1/agents/kimi", "Kimi", {"description": "Kimi Code 接入"})
        self.assertEqual(updated["name"], "kimi")
        self.assertEqual(updated["description"], "Kimi Code 接入")

    def test_empty_broadcast(self):
        lonely = Store(self.home / "lonely.db", seed=False)
        lonely.touch("solo")
        with self.assertRaises(MailError) as caught:
            lonely.send("solo", "*", "hi", "nobody")
        self.assertEqual(caught.exception.status, 422)

    def test_cli_and_mcp(self):
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        sent = subprocess.run(
            [sys.executable, IC, "--as", "cursor", "send", "--to", "opencode", "-s", "CLI", "-b", "from cursor", "--json"],
            check=True, capture_output=True, text=True, env=env,
        )
        message = json.loads(sent.stdout)
        self.assertEqual(message["from"], "cursor")
        inbox = subprocess.run(
            [sys.executable, IC, "--as", "opencode", "inbox", "--json"],
            check=True, capture_output=True, text=True, env=env,
        )
        self.assertEqual(json.loads(inbox.stdout)["count"], 1)

        proc = subprocess.Popen(
            [sys.executable, IC, "mcp", "--agent", "grok"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, bufsize=0,
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            }))
            init = read_frame(proc.stdout)
            self.assertEqual(init["result"]["serverInfo"]["name"], "infocenter")
            proc.stdin.write(frame({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            proc.stdin.write(frame({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ic_send", "arguments": {"to": "kimi", "subject": "MCP", "body": "via tool"}},
            }))
            called = read_frame(proc.stdout)
            self.assertFalse(called["result"]["isError"])
            self.assertIn("via tool", called["result"]["content"][0]["text"])
            proc.stdin.close()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.assertEqual(request(self.url, "GET", "/v1/inbox", "kimi")["messages"][0]["subject"], "MCP")

    def test_mcp_newline_framing(self):
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.Popen(
            [sys.executable, IC, "mcp", "--agent", "opencode"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, bufsize=0,
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "opencode", "version": "2"}},
            }))
            init = read_line_message(proc.stdout)
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(init["result"]["serverInfo"]["name"], "infocenter")
            proc.stdin.write(line_frame({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            proc.stdin.write(line_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
            listed = read_line_message(proc.stdout)
            names = [tool["name"] for tool in listed["result"]["tools"]]
            self.assertIn("ic_inbox", names)
            proc.stdin.close()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_detach_and_stop(self):
        home = self.home / "daemon"
        port = free_port()
        env = os.environ.copy()
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        env.pop("INFOCENTER_URL", None)
        started = subprocess.run(
            [sys.executable, IC, "serve", "--detach", "--home", str(home), "--port", str(port)],
            check=True, capture_output=True, text=True, env=env,
        )
        url = started.stdout.strip()
        self.assertTrue(healthy(url))
        try:
            request(url, "POST", "/v1/messages", "human", {"to": "grok", "subject": "后台", "body": "在"})
            self.assertEqual(request(url, "GET", "/v1/inbox", "grok")["count"], 1)
        finally:
            subprocess.run(
                [sys.executable, IC, "stop", "--home", str(home)],
                check=True, capture_output=True, text=True, env=env,
            )
        self.assertFalse(healthy(url))

    def _env(self) -> dict:
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        return env

    def test_agents_add_role_and_unread_counts(self):
        home = self.home / "cfg"
        added = subprocess.run(
            [sys.executable, IC, "--home", str(home), "--as", "planner",
             "agents", "add", "planner", "-d", "总协调", "--role", "coordinator"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("planner", added.stdout)
        saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
        entry = next(item for item in saved["agents"] if item["name"] == "planner")
        self.assertEqual(entry["role"], "coordinator")
        request(self.url, "POST", "/v1/messages", "grok", {"to": "kimi", "subject": "一", "body": "1"})
        request(self.url, "POST", "/v1/messages", "grok", {"to": "kimi", "subject": "二", "body": "2"})
        agents = {item["name"]: item for item in request(self.url, "GET", "/v1/agents", "human")["agents"]}
        self.assertEqual(agents["kimi"]["unread"], 2)
        self.assertEqual(agents["grok"]["unread"], 0)
        self.assertEqual(agents["planner"]["role"], "coordinator")
        listed = subprocess.run(
            [sys.executable, IC, "--as", "human", "agents"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("planner [coordinator]", listed.stdout)
        self.assertIn("kimi  未读 2", listed.stdout)

    def test_mcp_notify_and_no_notify(self):
        request(self.url, "POST", "/v1/messages", "grok", {
            "to": "kimi", "subject": "提醒我", "body": "内容", "workspace": "/tmp/proj",
        })
        proc = subprocess.Popen(
            [sys.executable, IC, "mcp", "--agent", "kimi"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env(), bufsize=0,
        )
        try:
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }))
            init = read_line_message(proc.stdout)
            self.assertEqual(init["result"]["serverInfo"]["name"], "infocenter")
            note = read_line_message(proc.stdout)
            self.assertEqual(note["method"], "notifications/message")
            self.assertIn("提醒我", note["params"]["data"])
            self.assertIn("workspace：/tmp/proj", note["params"]["data"])
        finally:
            proc.kill()
            proc.wait(timeout=5)
        proc = subprocess.Popen(
            [sys.executable, IC, "mcp", "--agent", "kimi", "--no-notify"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env(), bufsize=0,
        )
        try:
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }))
            read_line_message(proc.stdout)
            proc.stdin.write(line_frame({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ic_agents", "arguments": {}},
            }))
            called = read_line_message(proc.stdout)
            text = called["result"]["content"][0]["text"]
            self.assertNotIn("提醒：你还有", text)
            json.loads(text)  # 纯 payload，没有尾巴
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_crew_kickoff_dryrun_status_down(self):
        home = self.home / "crew"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"agents": [
            {"name": "opencode", "description": "OpenCode", "role": "coordinator"},
            {"name": "fakebot", "description": "测试", "headless": "/usr/bin/tee -a triggered.txt"},
            {"name": "clibot", "launch": "echo hi"},
            {"name": "deskbot", "app": "Safari"},
        ]}), encoding="utf-8")
        project = Path(tempfile.mkdtemp(dir=str(self.home))).resolve()
        plan = self.home / "plan.md"
        plan.write_text("# 分工\n- fakebot 负责数据\n", encoding="utf-8")
        kicked = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "kickoff",
             "--plan", str(plan), "--dir", str(project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("opencode", kicked.stdout)
        inbox = request(self.url, "GET", "/v1/inbox", "fakebot")
        self.assertEqual(inbox["count"], 1)
        message = inbox["messages"][0]
        self.assertEqual(message["priority"], "high")
        self.assertEqual(message["workspace"], str(project))
        self.assertEqual(sorted(message["to"]), ["clibot", "deskbot", "fakebot"])
        body = request(self.url, "GET", f"/v1/messages/{message['id']}", "fakebot")["body"]
        self.assertIn("fakebot 负责数据", body)
        self.assertIn("单一写者", body)
        dry = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "up",
             "--dir", str(project), "--dry-run"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("open -a Safari", dry.stdout)
        self.assertIn("crew watch fakebot", dry.stdout)
        if shutil.which("tmux"):
            self.assertIn("new-window", dry.stdout)
        else:
            self.assertIn("osascript", dry.stdout)
        status = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "status"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("fakebot", status.stdout)
        self.assertIn("守护未运行", status.stdout)
        down = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "down"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("没有需要清理", down.stdout)
        (home / "config.json").write_text(json.dumps({"agents": [{"name": "solo"}]}), encoding="utf-8")
        bad = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "kickoff",
             "--plan", str(plan), "--dir", str(project)],
            capture_output=True, text=True, env=self._env(),
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("no_coordinator", bad.stderr)

    def test_crew_watch_daemon_triggers_headless(self):
        home = self.home / "watch"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"agents": [
            {"name": "fakebot", "headless": "/usr/bin/tee -a triggered.txt"},
        ]}), encoding="utf-8")
        project = Path(tempfile.mkdtemp(dir=str(self.home))).resolve()
        proc = subprocess.Popen(
            [sys.executable, IC, "--home", str(home), "--url", self.url,
             "crew", "watch", "fakebot", "--dir", str(project), "--poll", "5"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self._env(),
        )
        try:
            time.sleep(1.0)
            request(self.url, "POST", "/v1/messages", "human", {
                "to": "fakebot", "subject": "干活", "body": "处理一下",
            })
            target = project / "triggered.txt"
            deadline = time.time() + 10
            while time.time() < deadline and not target.exists():
                time.sleep(0.2)
            self.assertTrue(target.exists(), "守护进程没有触发 headless")
            content = target.read_text(encoding="utf-8")
            self.assertIn("干活", content)
            self.assertIn("ic --as fakebot", content)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
