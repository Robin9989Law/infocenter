"""M6-1 判据：窗口 workspace、cwd 命中 project、都不命中仍空。

父信和案卷仍先于窗口（回复不改道）。推断不登记新项目，不改窗口已登记值。
服务端不读自己的进程目录；cwd 由客户端在发信/回信时带上。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import request
from infocenter.server import make_handler
from infocenter.store import Store


class M6WsInferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db_path = self.home / "mailbox.db"
        self.store = Store(
            self.db_path,
            seeds=[
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
            ],
        )
        self.proj = self.home / "CanonProj"
        self.proj.mkdir()
        self.canonical = str(self.proj.resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def _projects(self) -> dict[str, tuple]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return {
                row[0]: (row[1], row[2])
                for row in con.execute("SELECT workspace, archived, updated_at FROM projects")
            }
        finally:
            con.close()

    def test_window_workspace_when_send_omits_it(self):
        self.store.touch("opencode", window_id="w-win", workspace=self.canonical)
        before = self._projects()
        sent = self.store.send("opencode", ["cursor"], "只靠窗口", "正文", window_id="w-win")
        self.assertEqual(sent["workspace"], self.canonical)
        self.assertEqual(sent["workspace_source"], "window")
        self.assertFalse(sent["workspace_unscoped"])
        self.assertEqual(self.store.get_window("w-win")["workspace"], self.canonical)
        self.assertEqual(self._projects(), before)

    def test_cwd_hits_registered_project(self):
        self.store.add_project(self.canonical)
        self.store.touch("opencode", window_id="w-blank", workspace=None)
        before = self._projects()
        sent = self.store.send(
            "opencode",
            ["cursor"],
            "只靠 cwd",
            "正文",
            window_id="w-blank",
            client_cwd=self.canonical,
        )
        self.assertEqual(sent["workspace"], self.canonical)
        self.assertEqual(sent["workspace_source"], "project")
        self.assertFalse(sent["workspace_unscoped"])
        self.assertIsNone(self.store.get_window("w-blank")["workspace"])
        self.assertEqual(self._projects(), before)

    def test_neither_stays_empty_and_does_not_guess(self):
        left = self.home / "left" / "samename"
        right = self.home / "right" / "samename"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        self.store.add_project(str(left.resolve()))
        self.store.add_project(str(right.resolve()))
        before = self._projects()
        ambiguous = self.store.send(
            "opencode",
            ["cursor"],
            "同名不猜",
            "正文",
            client_cwd="samename",
        )
        self.assertIsNone(ambiguous["workspace"])
        self.assertEqual(ambiguous["workspace_source"], "empty")
        self.assertTrue(ambiguous["workspace_unscoped"])

        missing = self.store.send(
            "opencode",
            ["cursor"],
            "对不上",
            "正文",
            client_cwd=str((self.home / "nowhere").resolve()),
        )
        self.assertIsNone(missing["workspace"])
        self.assertEqual(missing["workspace_source"], "empty")
        self.assertEqual(self._projects(), before)

    def test_explicit_beats_window_and_parent_beats_window(self):
        self.store.add_project(self.canonical)
        other = str((self.home / "other").resolve())
        self.store.touch("opencode", window_id="w-win", workspace=self.canonical)
        explicit = self.store.send(
            "opencode",
            ["cursor"],
            "显式",
            "正文",
            workspace=other,
            window_id="w-win",
        )
        self.assertEqual(explicit["workspace"], other)
        self.assertEqual(explicit["workspace_source"], "explicit")

        parent = self.store.send("opencode", ["cursor"], "父信", "正文", workspace=other)
        child = self.store.send(
            "cursor",
            ["opencode"],
            "跟父信",
            "正文",
            reply_to=parent["id"],
            window_id="w-win",
            client_cwd=self.canonical,
        )
        self.assertEqual(child["workspace"], other)
        self.assertEqual(child["workspace_source"], "parent")

    def test_empty_parent_falls_through_to_window(self):
        self.store.touch("cursor", window_id="w-cur", workspace=self.canonical)
        parent = self.store.send("opencode", ["cursor"], "无范围", "正文")
        self.assertIsNone(parent["workspace"])
        child = self.store.reply("cursor", parent["id"], "补上窗口", window_id="w-cur")
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "window")

    def test_client_brings_cwd_server_does_not_invent(self):
        self.store.add_project(self.canonical)
        self.store.touch("opencode", window_id="w-blank", workspace=None)
        httpd = threading.Thread
        server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.store)
        )
        url = f"http://127.0.0.1:{server.server_address[1]}"
        thread = httpd(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("infocenter.client.os.getcwd", return_value=self.canonical):
                hit = request(
                    url,
                    "POST",
                    "/v1/messages",
                    "opencode",
                    {"to": "cursor", "subject": "客户端 cwd", "body": "正文"},
                    window="w-blank",
                )
            self.assertEqual(hit["workspace"], self.canonical)
            self.assertEqual(hit["workspace_source"], "project")

            unknown = str((self.home / "not-registered").resolve())
            with patch("infocenter.client.os.getcwd", return_value=unknown):
                miss = request(
                    url,
                    "POST",
                    "/v1/messages",
                    "opencode",
                    {"to": "cursor", "subject": "对不上", "body": "正文"},
                    window="w-blank",
                )
            self.assertIsNone(miss["workspace"])
            self.assertEqual(miss["workspace_source"], "empty")
            self.assertTrue(miss["workspace_unscoped"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
