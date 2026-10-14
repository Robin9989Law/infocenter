"""P0/P1：守护与客户端的回归。对应 _work/final-review.md 的 C-1、C-3、C-4、C-5、C-6、C-7、C-8。"""

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.client import RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew
from infocenter.client import ApiError, call, request
from infocenter.server import make_handler
from infocenter.store import MailError, Store, utcnow


class DebugP0Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        os.environ.pop("INFOCENTER_WINDOW", None)
        crew._daemon_ctx.clear()
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""),
            ("kimi", "Kimi", ""),
            ("human", "用户", ""),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.project = self.home / "proj"
        self.project.mkdir()
        self.project_s = str(self.project.resolve())
        (self.home / "config.json").write_text(
            json.dumps({"agents": [{"name": "grok", "headless": "echo ok"}]}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        crew._daemon_ctx.clear()
        self.tmp.cleanup()
        os.environ.pop("INFOCENTER_WINDOW", None)

    def _env(self) -> dict:
        env = os.environ.copy()
        env.pop("INFOCENTER_WINDOW", None)
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        return env

    def test_adopt_window_ignores_inherited_env(self):
        os.environ["INFOCENTER_WINDOW"] = "w-parent"
        window_id, previous = crew._adopt_window()
        self.assertEqual(previous, "w-parent")
        self.assertNotEqual(window_id, "w-parent")
        self.assertTrue(window_id)
        self.assertEqual(os.environ["INFOCENTER_WINDOW"], window_id)
        explicit, _previous = crew._adopt_window("w-explicit")
        self.assertEqual(explicit, "w-explicit")
        self.assertEqual(os.environ["INFOCENTER_WINDOW"], "w-explicit")

    def test_start_daemon_drops_window_and_records_start(self):
        os.environ["INFOCENTER_WINDOW"] = "w-parent"
        seen = {}

        class Proc:
            pid = 4321

        def fake_popen(_cmd, **kwargs):
            seen["env"] = kwargs["env"]
            return Proc()

        key = crew._key("grok", self.project_s)
        with patch.object(crew.subprocess, "Popen", side_effect=fake_popen):
            pid = crew._start_daemon(
                self.home, key, self.project_s,
                [sys.executable, "-m", "infocenter", "crew", "watch", "grok"],
            )
        self.assertEqual(pid, 4321)
        self.assertNotIn("INFOCENTER_WINDOW", seen["env"])
        self.assertEqual(seen["env"]["INFOCENTER_AGENT"], "grok")
        record = json.loads(crew._pid_file(self.home, key).read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], 4321)
        self.assertEqual(record["marker"], "ic-crew")
        self.assertIn("started_at", record)

    def test_reused_pid_is_not_our_daemon(self):
        sleep = subprocess.Popen(["sleep", "30"])
        key = crew._key("grok", self.project_s)
        try:
            crew._pid_file(self.home, key).write_text(json.dumps({
                "pid": sleep.pid,
                "started_at": "2020-01-01T00:00:00Z",
                "marker": "ic-crew",
            }), encoding="utf-8")
            crew._write_meta(self.home, key, {
                "agent": "grok", "key": key, "dir": self.project_s, "pid": sleep.pid,
            })
            self.assertIsNone(crew._read_pid(self.home, key))
            result = crew.down(self.home, self.project_s, "grok")
            self.assertNotIn(key, result["killed"])
            self.assertIsNone(sleep.poll())
        finally:
            if sleep.poll() is None:
                sleep.kill()
                sleep.wait(timeout=5)

    def test_live_watch_pid_matches_and_unlinks_on_stop(self):
        key = crew._key("grok", self.project_s)
        proc = subprocess.Popen(
            [sys.executable, "-m", "infocenter", "crew", "watch", "grok",
             "--dir", self.project_s, "--url", self.url, "--home", str(self.home), "--poll", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self._env(),
        )
        try:
            deadline = time.time() + 5
            found = False
            while time.time() < deadline:
                payload = request(self.url, "GET", "/v1/windows", "human", query={"agent": "grok"})
                if payload["windows"]:
                    found = True
                    break
                time.sleep(0.05)
            self.assertTrue(found, "守护没有登记办公室")
            crew._write_pid_record(self.home, key, proc.pid, utcnow())
            self.assertEqual(crew._read_pid(self.home, key), proc.pid)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        self.assertFalse(crew._pid_file(self.home, key).exists())

    def test_headless_replaces_non_utf8_and_reports_failure(self):
        script = "import sys; sys.stdout.buffer.write(bytes([255, 254]))"
        command = f"{sys.executable} -c {shlex.quote(script)}"
        self.assertEqual(
            crew._run_headless(command, "x", self.project_s, agent="grok"),
            "ok",
        )
        self.assertEqual(crew._run_headless("false", "x", self.project_s, agent="grok"), "fail")

    def test_watch_continues_after_headless_decode_error(self):
        key = crew._key("grok", self.project_s)
        cursor_path = crew._cursor_file(self.home, key)
        state = {"n": 0, "saw_cursor": []}

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            state["n"] += 1
            if state["n"] > 2:
                raise StopIteration("done")
            return {
                "cursor": 3,
                "unread_total": 1,
                "new": [{
                    "id": "m-decode",
                    "thread_id": "m-decode",
                    "from": "kimi",
                    "subject": "乱码",
                    "workspace": self.project_s,
                }],
            }

        def fake_headless(*_args, **_kwargs):
            state["saw_cursor"].append(cursor_path.read_text() if cursor_path.exists() else "")
            if len(state["saw_cursor"]) == 1:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return "ok"

        with (
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
            patch.object(crew.time, "sleep"),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", self.project_s, self.url, poll=5, heartbeat=0)
        self.assertEqual(state["saw_cursor"], ["", ""])
        self.assertEqual(cursor_path.read_text(encoding="utf-8"), "3")

    def test_failed_headless_keeps_cursor_and_notes_interrupt(self):
        self.store.create_case(
            agent="grok",
            title="触发信",
            workspace=self.project_s,
            owner_agent="grok",
            owner_window="w-seed",
            thread_id="m-thread",
        )
        key = crew._key("grok", self.project_s)
        cursor_path = crew._cursor_file(self.home, key)
        cursor_path.write_text("4")
        state = {"n": 0, "cursors": []}

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            state["n"] += 1
            if state["n"] > 2:
                raise StopIteration("done")
            return {
                "cursor": 9,
                "unread_total": 1,
                "new": [{
                    "id": "m-msg",
                    "thread_id": "m-thread",
                    "from": "kimi",
                    "subject": "触发",
                    "workspace": self.project_s,
                }],
            }

        outcomes = iter(["timeout", "ok"])

        def fake_headless(*_args, **_kwargs):
            state["cursors"].append(cursor_path.read_text(encoding="utf-8"))
            return next(outcomes)

        with (
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
            patch.object(crew.time, "sleep"),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", self.project_s, self.url, poll=5, heartbeat=0)
        self.assertEqual(state["cursors"], ["4", "4"])
        self.assertEqual(cursor_path.read_text(encoding="utf-8"), "9")
        notes = [node["note"] for node in self.store.get_case("m-thread")["nodes"]]
        self.assertIn("[自动] 中断", notes)

    def test_pause_interrupts_before_office_lookup(self):
        key = crew._key("grok", self.project_s)
        crew._daemon_ctx.update({
            "home": self.home, "key": key, "agent": "grok", "dir": self.project_s,
        })
        thread = threading.Thread(
            target=crew._run_headless,
            args=("sleep 30", "x", self.project_s),
            kwargs={"agent": "grok"},
            daemon=True,
        )
        thread.start()
        pid = None
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                pid = crew._read_meta(self.home, key).get("headless_pid")
                if pid:
                    break
                time.sleep(0.05)
            self.assertTrue(pid)
            with patch.object(crew, "_api", side_effect=MailError("not_found", "没有这个接口", 404)):
                with self.assertRaises(MailError):
                    crew.control(self.home, self.url, "pause", "grok", self.project_s)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(crew._pid_alive(int(pid)))
        finally:
            if pid and crew._pid_alive(int(pid)):
                crew._signal_group(int(pid), signal.SIGKILL)
            thread.join(timeout=2)

    def test_watch_exits_when_office_never_registers(self):
        with (
            patch.object(
                crew, "_office_request",
                side_effect=ApiError(404, {"error": "not_found", "message": "没有这个接口"}),
            ),
            patch.object(crew.time, "sleep") as slept,
            patch.object(crew, "_run_headless") as headless,
        ):
            code = crew.watch(self.home, "grok", self.project_s, self.url, poll=5, heartbeat=0)
        self.assertEqual(code, 1)
        headless.assert_not_called()
        self.assertGreaterEqual(slept.call_count, OFFICE_ATTEMPTS_BEFORE_EXIT)
        meta = crew._read_meta(self.home, crew._key("grok", self.project_s))
        self.assertIs(meta.get("office_registered"), False)

    def test_status_marks_unregistered_office_in_red(self):
        key = crew._key("grok", self.project_s)
        crew._pid_file(self.home, key).write_text("999999", encoding="utf-8")
        crew._write_meta(self.home, key, {
            "agent": "grok",
            "key": key,
            "dir": self.project_s,
            "pid": 999999,
            "office_registered": False,
        })
        text = "\n".join(crew.status(self.home, self.project_s)["lines"])
        self.assertIn("已死", text)
        self.assertIn("办公室未登记", text)
        self.assertIn("\033[31m办公室未登记\033[0m", text)

    def test_request_wraps_timeout_and_remote_disconnect(self):
        with patch("infocenter.client.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(ApiError) as timed:
                request(self.url, "GET", "/v1/inbox", "grok")
        self.assertEqual(timed.exception.code, "transport")
        self.assertEqual(timed.exception.status, 0)
        with patch("infocenter.client.urllib.request.urlopen", side_effect=RemoteDisconnected("closed")):
            with self.assertRaises(ApiError) as closed:
                request(self.url, "GET", "/v1/inbox", "grok")
        self.assertEqual(closed.exception.code, "transport")
        with (
            patch("infocenter.client.healthy", return_value=True),
            patch("infocenter.client.urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        ):
            with self.assertRaises(MailError) as via_call:
                call(self.url, "grok", "GET", "/v1/inbox", home=self.home)
        self.assertEqual(via_call.exception.code, "transport")
        with patch("infocenter.client.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit):
                request(self.url, "GET", "/v1/inbox", "grok")


OFFICE_ATTEMPTS_BEFORE_EXIT = crew.OFFICE_REGISTER_ATTEMPTS - 1


if __name__ == "__main__":
    unittest.main()
