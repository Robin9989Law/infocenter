"""M6-4 列表收口：`ic windows` / `ic agents` 默认不列已离开的办公室，`--all` 看全。

硬删不在本项。服务端没有「去掉已离开」的参数，收口在客户端做。数据行不动。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import _collapse_agent_offices, _collapse_windows, _window_is_departed
from infocenter.server import make_handler
from infocenter.store import Store

IC = str(ROOT / "bin" / "ic")
NOW = datetime(2026, 10, 15, 6, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(window_id: str, state: str, *, left_at=None, last_seen_at=None, agent="grok", seen=True):
    item = {"window_id": window_id, "agent": agent, "state": state, "left_at": left_at}
    if seen:
        item["last_seen_at"] = last_seen_at or left_at or _iso(NOW)
    return item


class CollapseWindowsTest(unittest.TestCase):
    def test_hides_every_departed_office(self):
        payload = {
            "windows": [
                _window("w-active", "active"),
                _window("w-paused", "paused"),
                _window("w-edge", "left", left_at=_iso(NOW)),
                _window("w-old", "left", left_at=_iso(NOW - timedelta(days=30))),
                _window("w-offline", "offline", left_at=_iso(NOW - timedelta(minutes=5))),
                _window("w-nolat", "left", left_at=None, last_seen_at=_iso(NOW - timedelta(minutes=10))),
                _window("w-blank", "left", seen=False),
                _window("w-other", "crashed"),
                {"window_id": "w-label", "state": "mystery", "state_label": "离场"},
            ],
            "count": 9,
        }
        out = _collapse_windows(payload)
        self.assertEqual(
            [item["window_id"] for item in out["windows"]],
            ["w-active", "w-paused", "w-other"],
        )
        self.assertEqual(out["count"], 3)
        self.assertTrue(out["collapsed"])
        self.assertEqual(out["hidden"], 6)
        self.assertEqual(payload["count"], 9)
        self.assertFalse(_window_is_departed(_window("w-active", "active")))
        self.assertTrue(_window_is_departed(_window("w-future", "left", left_at=_iso(NOW + timedelta(minutes=1)))))

    def test_agents_collapse_keeps_other_fields(self):
        payload = {
            "agents": [
                {
                    "name": "grok",
                    "unread": 1,
                    "windows": [
                        _window("w-active", "active", agent="grok"),
                        _window("w-left", "left", agent="grok"),
                    ],
                },
                {"name": "kimi", "unread": 0},
            ]
        }
        out = _collapse_agent_offices(payload)
        self.assertEqual([item["window_id"] for item in out["agents"][0]["windows"]], ["w-active"])
        self.assertEqual(out["agents"][0]["unread"], 1)
        self.assertNotIn("windows", out["agents"][1])
        self.assertEqual(out["windows_hidden"], 1)
        self.assertEqual(len(payload["agents"][0]["windows"]), 2)


class WindowsCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        os.environ.pop("INFOCENTER_WINDOW", None)
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""),
            ("kimi", "Kimi", ""),
            ("cursor", "Cursor", ""),
        ])
        self.project = self.home / "proj"
        self.project.mkdir()
        self.store.touch_window("w-active", "grok", workspace=str(self.project), state="active")
        self.store.touch_window("w-paused", "kimi", workspace=str(self.project), state="active")
        self.store.pause_window("w-paused", agent="kimi")
        self.store.touch_window("w-recent", "cursor", workspace=str(self.project), state="active")
        self.store.leave_window("w-recent", agent="cursor")
        self.store.touch_window("w-old", "cursor", workspace=str(self.project), state="active")
        self.store.leave_window("w-old", agent="cursor")
        old = "2020-01-01T00:00:00Z"
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(
                "UPDATE windows SET left_at = ?, last_seen_at = ? WHERE window_id = ?",
                (old, old, "w-old"),
            )
            conn.commit()
        finally:
            conn.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()
        os.environ.pop("INFOCENTER_WINDOW", None)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("INFOCENTER_WINDOW", None)
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, IC, "--home", str(self.home), "--url", self.url, "--as", "grok", *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def _window_rows(self):
        conn = sqlite3.connect(self.store.path)
        try:
            return conn.execute(
                "SELECT window_id, agent, state, left_at, last_seen_at FROM windows ORDER BY window_id"
            ).fetchall()
        finally:
            conn.close()

    def test_default_hides_departed_and_all_keeps_the_label(self):
        before = self._window_rows()
        default = self._run("windows")
        self.assertIn("w-active", default.stdout)
        self.assertIn("w-paused", default.stdout)
        self.assertNotIn("w-recent", default.stdout)
        self.assertNotIn("w-old", default.stdout)
        self.assertNotIn("离场", default.stdout)
        self.assertIn("--all 看全", default.stdout)
        self.assertIn("另有 2 间未列出", default.stdout)

        everything = self._run("windows", "--all")
        self.assertIn("w-recent", everything.stdout)
        self.assertIn("w-old", everything.stdout)
        self.assertIn("离场", everything.stdout)
        self.assertIn("办公室 4 间", everything.stdout)
        self.assertNotIn("未列出", everything.stdout)

        left = self._run("windows", "--state", "left")
        self.assertIn("w-recent", left.stdout)
        self.assertIn("w-old", left.stdout)
        self.assertIn("离场", left.stdout)
        self.assertNotIn("w-active", left.stdout)
        self.assertNotIn("w-paused", left.stdout)

        grok_only = self._run("windows", "--agent", "grok")
        self.assertIn("w-active", grok_only.stdout)
        self.assertNotIn("w-paused", grok_only.stdout)
        self.assertNotIn("w-recent", grok_only.stdout)
        self.assertNotIn("离场", grok_only.stdout)

        payload = json.loads(self._run("windows", "--json").stdout)
        self.assertEqual(payload["count"], len(payload["windows"]))
        self.assertEqual(payload["hidden"], 2)
        self.assertTrue(payload["collapsed"])
        shown = {item["window_id"] for item in payload["windows"]}
        self.assertEqual(shown, {"w-active", "w-paused"})
        self.assertEqual(self._window_rows(), before)

    def test_agents_default_hides_departed_offices(self):
        before = self._window_rows()
        default = self._run("agents")
        self.assertIn("w-active", default.stdout)
        self.assertIn("w-paused", default.stdout)
        self.assertNotIn("w-recent", default.stdout)
        self.assertNotIn("w-old", default.stdout)
        self.assertNotIn("离场", default.stdout)
        self.assertIn("另有 2 间未列出", default.stdout)

        everything = self._run("agents", "--all")
        self.assertIn("w-recent", everything.stdout)
        self.assertIn("w-old", everything.stdout)
        self.assertIn("离场", everything.stdout)

        payload = json.loads(self._run("agents", "--json").stdout)
        ids = {
            office["window_id"]
            for agent in payload["agents"]
            for office in agent.get("windows") or []
        }
        self.assertEqual(ids, {"w-active", "w-paused"})
        self.assertEqual(payload["windows_hidden"], 2)
        self.assertNotIn("离场", json.dumps(payload, ensure_ascii=False))

        full = json.loads(self._run("agents", "--all", "--json").stdout)
        full_ids = {
            office["window_id"]
            for agent in full["agents"]
            for office in agent.get("windows") or []
        }
        self.assertEqual(full_ids, {"w-active", "w-paused", "w-recent", "w-old"})
        labels = {
            office["state_label"]
            for agent in full["agents"]
            for office in agent.get("windows") or []
            if office["window_id"] in {"w-recent", "w-old"}
        }
        self.assertEqual(labels, {"离场"})
        self.assertNotIn("windows_hidden", full)
        self.assertEqual(self._window_rows(), before)

    def test_help_states_the_default(self):
        shown = self._run("windows", "--help")
        self.assertIn("--all", shown.stdout)
        self.assertIn("已离开", shown.stdout)
        self.assertNotIn("1 小时", shown.stdout)
        agents = self._run("agents", "--help")
        self.assertIn("--all", agents.stdout)
        self.assertIn("已离开", agents.stdout)

    def test_board_lists_use_the_same_filter(self):
        html = (ROOT / "infocenter" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function isDepartedOffice", html)
        self.assertIn("function presentOffices", html)
        self.assertIn("const wins = presentOffices(scoped);", html)
        self.assertIn("const wins = presentOffices(allWins);", html)
        self.assertIn("const offices = presentOffices(state.windows);", html)


if __name__ == "__main__":
    unittest.main()
