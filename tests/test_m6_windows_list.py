"""M6-4 列表收口：`ic windows` 默认只列在办、暂停和 1 小时内离场，`--all` 看全。

硬删不在本项。服务端没有离场时间参数，收口在客户端做。
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

from infocenter.cli import _RECENT_LEFT_SECONDS, _collapse_windows, _window_kept_by_default
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
    def test_keeps_present_and_recent_left_only(self):
        recent = _iso(NOW - timedelta(seconds=_RECENT_LEFT_SECONDS))
        just_over = _iso(NOW - timedelta(seconds=_RECENT_LEFT_SECONDS + 1))
        payload = {
            "windows": [
                _window("w-active", "active"),
                _window("w-paused", "paused"),
                _window("w-edge", "left", left_at=recent),
                _window("w-old", "left", left_at=just_over),
                _window("w-offline", "offline", left_at=_iso(NOW - timedelta(minutes=5))),
                _window("w-nolat", "left", left_at=None, last_seen_at=_iso(NOW - timedelta(minutes=10))),
                _window("w-blank", "left", seen=False),
                _window("w-other", "crashed"),
            ],
            "count": 8,
        }
        out = _collapse_windows(payload, now=NOW)
        self.assertEqual(
            [item["window_id"] for item in out["windows"]],
            ["w-active", "w-paused", "w-edge", "w-offline", "w-nolat"],
        )
        self.assertEqual(out["count"], 5)
        self.assertTrue(out["collapsed"])
        self.assertEqual(out["hidden"], 3)
        self.assertEqual(payload["count"], 8)

    def test_future_left_at_stays_visible(self):
        item = _window("w-future", "left", left_at=_iso(NOW + timedelta(minutes=1)))
        self.assertTrue(_window_kept_by_default(item, NOW))


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

    def test_default_hides_old_left_and_flags_keep_meaning(self):
        default = self._run("windows")
        self.assertIn("w-active", default.stdout)
        self.assertIn("w-paused", default.stdout)
        self.assertIn("w-recent", default.stdout)
        self.assertNotIn("w-old", default.stdout)
        self.assertIn("--all 看全", default.stdout)
        self.assertIn("另有 1 间未列出", default.stdout)

        everything = self._run("windows", "--all")
        self.assertIn("w-old", everything.stdout)
        self.assertIn("办公室 4 间", everything.stdout)
        self.assertNotIn("未列出", everything.stdout)

        left = self._run("windows", "--state", "left")
        self.assertIn("w-recent", left.stdout)
        self.assertIn("w-old", left.stdout)
        self.assertNotIn("w-active", left.stdout)
        self.assertNotIn("w-paused", left.stdout)

        grok_only = self._run("windows", "--agent", "grok")
        self.assertIn("w-active", grok_only.stdout)
        self.assertNotIn("w-paused", grok_only.stdout)
        self.assertNotIn("w-recent", grok_only.stdout)

        payload = json.loads(self._run("windows", "--json").stdout)
        self.assertEqual(payload["count"], len(payload["windows"]))
        self.assertEqual(payload["hidden"], 1)
        self.assertTrue(payload["collapsed"])
        self.assertNotIn("w-old", {item["window_id"] for item in payload["windows"]})

    def test_help_states_the_default(self):
        shown = self._run("windows", "--help")
        self.assertIn("--all", shown.stdout)
        self.assertIn("1 小时", shown.stdout)
        self.assertIn("不再按离场时间收口", shown.stdout)


if __name__ == "__main__":
    unittest.main()
