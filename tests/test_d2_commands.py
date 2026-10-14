import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http.server import ThreadingHTTPServer

from infocenter import crew
from infocenter.client import request
from infocenter.server import make_handler
from infocenter.store import Store

IC = str(ROOT / "bin" / "ic")


class D2CommandTest(unittest.TestCase):
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
        (self.home / "config.json").write_text(
            '{"agents": [{"name": "grok", "headless": "true"}, {"name": "kimi", "headless": "true"}]}',
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

    def test_request_sends_window_header_only_when_set(self):
        request(
            self.url, "POST", "/v1/messages", "human",
            {"to": "grok", "subject": "带窗口", "body": "正文", "workspace": str(self.project)},
            window="w-abc123",
        )
        named = request(self.url, "GET", "/v1/windows", "human", query={"agent": "human"})
        self.assertEqual([item["window_id"] for item in named["windows"]], ["w-abc123"])

        os.environ["INFOCENTER_WINDOW"] = "w-fromenv"
        try:
            request(
                self.url, "POST", "/v1/messages", "human",
                {"to": "grok", "subject": "环境变量", "body": "正文", "workspace": str(self.project)},
            )
            request(
                self.url, "POST", "/v1/messages", "human",
                {"to": "grok", "subject": "显式空", "body": "正文", "workspace": str(self.project)},
                window="",
            )
        finally:
            os.environ.pop("INFOCENTER_WINDOW", None)
        windows = request(self.url, "GET", "/v1/windows", "human", query={"agent": "human"})
        ids = {item["window_id"] for item in windows["windows"]}
        self.assertIn("w-fromenv", ids)
        self.assertNotIn("", ids)
        self.assertEqual(len(ids), 2)

    def test_case_commands_print_and_append_node(self):
        created = self.store.create_case(
            agent="grok",
            title="窗口认领",
            workspace=str(self.project),
            owner_agent="grok",
            owner_window="w-111111",
            note="认领立案",
        )
        shown = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url, "--as", "grok", "case", created["id"]],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("窗口认领", shown.stdout)
        self.assertIn("认领立案", shown.stdout)
        noted = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url, "--as", "grok",
             "case", created["id"], "--note", "复核通过"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("复核通过", noted.stdout)
        listed = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url, "--as", "grok", "cases"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn(created["id"], listed.stdout)
        offices = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url, "--as", "grok",
             "windows", "--agent", "grok"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("w-111111", offices.stdout)

    def test_crew_pause_abort_and_down_only(self):
        created = self.store.create_case(
            agent="grok",
            title="联邦实现",
            workspace=str(self.project),
            owner_agent="grok",
            owner_window="w-222222",
        )
        paused = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "pause", "grok", "--dir", str(self.project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("w-222222", paused.stdout)
        case = self.store.get_case(created["id"])
        self.assertEqual(case["status"], "paused")
        self.assertFalse(case["sealed"])
        aborted = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "abort", "grok", "--dir", str(self.project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn(created["id"], aborted.stdout)
        case = self.store.get_case(created["id"])
        self.assertTrue(case["sealed"])
        self.assertEqual(case["status"], "aborted")

        keep = subprocess.Popen(["sleep", "60"])
        stop = subprocess.Popen(["sleep", "60"])
        try:
            self._write_daemon("kimi", keep.pid)
            self._write_daemon("grok", stop.pid)
            result = crew.down(self.home, str(self.project), "grok")
            self.assertTrue(any(item.startswith("grok--") for item in result["killed"]))
            self.assertIsNone(keep.poll())
            self.assertIsNotNone(stop.wait(timeout=2))
        finally:
            for proc in (keep, stop):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_watch_reports_leave(self):
        proc = subprocess.Popen(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "watch", "grok", "--dir", str(self.project), "--poll", "5"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self._env(),
        )
        try:
            deadline = time.time() + 5
            found = []
            while time.time() < deadline:
                payload = request(self.url, "GET", "/v1/windows", "human", query={"agent": "grok"})
                found = payload["windows"]
                if found:
                    break
                time.sleep(0.1)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["state"], "active")
            window_id = found[0]["window_id"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            payload = request(self.url, "GET", "/v1/windows", "human", query={"agent": "grok"})
            match = [item for item in payload["windows"] if item["window_id"] == window_id]
            if match and match[0]["state"] == "left":
                state = "left"
                break
            time.sleep(0.1)
        self.assertEqual(state, "left")

    def test_prompt_tells_headless_to_read_and_write_case(self):
        prompt = crew.build_prompt("grok", str(self.project), [
            {"id": "m1", "from": "kimi", "subject": "干活", "priority": "normal", "workspace": str(self.project)},
        ], 1)
        self.assertIn("读案卷 → 干活 → 写回一行节点", prompt)
        self.assertIn("ic --as grok case <thread>", prompt)
        self.assertIn('case <thread> --note "结论"', prompt)
        self.assertIn("ic --as grok inbox --full", prompt)

    def test_crew_resume_and_close(self):
        created = self.store.create_case(
            agent="grok",
            title="恢复后办结",
            workspace=str(self.project),
            owner_agent="grok",
            owner_window="w-333333",
        )
        subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "pause", "grok", "--dir", str(self.project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        paused = self.store.get_case(created["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["sealed"])
        resumed = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "resume", "grok", "--dir", str(self.project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("w-333333", resumed.stdout)
        case = self.store.get_case(created["id"])
        self.assertEqual(case["status"], "open")
        self.assertFalse(case["sealed"])
        closed = subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url,
             "crew", "close", "grok", "--dir", str(self.project)],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn(created["id"], closed.stdout)
        case = self.store.get_case(created["id"])
        self.assertEqual(case["status"], "done")
        self.assertTrue(case["sealed"])

    def test_run_headless_env_includes_window(self):
        os.environ["INFOCENTER_WINDOW"] = "w-abc123"
        seen = []

        class Proc:
            pid = 7
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return ("", "")

        def fake_popen(cmd, **kwargs):
            seen.append(kwargs.get("env") or {})
            return Proc()

        try:
            with patch.object(crew.subprocess, "Popen", side_effect=fake_popen):
                crew._run_headless("echo {prompt}", "hi", str(self.project), agent="grok")
                crew._run_headless("cat", "hi", str(self.project), agent="grok")
        finally:
            os.environ.pop("INFOCENTER_WINDOW", None)
        self.assertEqual(len(seen), 2)
        for env in seen:
            self.assertEqual(env.get("INFOCENTER_WINDOW"), "w-abc123")
            self.assertEqual(env.get("INFOCENTER_AGENT"), "grok")
            self.assertIsNot(env, os.environ)

    def test_pause_interrupts_running_headless(self):
        project = str(self.project.resolve())
        key = crew._key("grok", project)
        crew._daemon_ctx.update({
            "home": self.home, "key": key, "agent": "grok", "dir": project,
        })
        thread = threading.Thread(
            target=crew._run_headless,
            args=("sleep 30", "x", project),
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
            self.assertTrue(crew._pid_alive(int(pid)))
            result = crew.control(self.home, self.url, "pause", "grok", project)
            self.assertTrue(any("已打断" in line for line in result["lines"]))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(crew._pid_alive(int(pid)))
            self.assertNotIn("headless_pid", crew._read_meta(self.home, key))
        finally:
            if pid and crew._pid_alive(int(pid)):
                crew._signal_group(int(pid), signal.SIGKILL)
            thread.join(timeout=2)

    def test_watch_skips_headless_while_window_paused(self):
        calls = []
        hits = {"n": 0}

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            hits["n"] += 1
            if hits["n"] == 1:
                windows = request(self.url, "GET", "/v1/windows", "grok", query={"agent": "grok"})
                self.assertEqual(len(windows["windows"]), 1)
                wid = windows["windows"][0]["window_id"]
                request(self.url, "POST", f"/v1/windows/{wid}/pause", "grok", {})
                return {
                    "cursor": 1,
                    "unread_total": 1,
                    "new": [{
                        "id": "m-pause",
                        "from": "kimi",
                        "subject": "先别跑",
                        "workspace": str(self.project),
                    }],
                }
            raise StopIteration("done")

        def fake_headless(*args, **kwargs):
            calls.append(args)

        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)

        with (
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
            patch.object(crew.time, "sleep", side_effect=fake_sleep),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", str(self.project), self.url, poll=5, heartbeat=0)
        self.assertEqual(calls, [])
        self.assertIn(2, slept)

    def _write_daemon(self, agent: str, pid: int) -> None:
        project = str(self.project.resolve())
        key = crew._key(agent, project)
        crew._pid_file(self.home, key).write_text(str(pid))
        crew._meta_file(self.home, key).write_text(
            json.dumps({"agent": agent, "key": key, "dir": project, "pid": pid}),
            encoding="utf-8",
        )
