"""M9 · 写锁：同一时间只有一个 agent 能改同一个路径。

背景（实测事故）：两个 agent 同时改同一批文件 ⇒ 两版改动混写，文件一度不自洽。
规矩：写文件前先 `ic lock <路径>`；拿不到（409）就停手回信，不要硬写。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import ApiError, request
from infocenter.server import make_handler
from infocenter.store import MailError, Store

A = str(Path("/tmp/lock-demo/store.py").resolve())   # macOS 下 /tmp → /private/tmp
B = str(Path("/tmp/lock-demo/cli.py").resolve())


class WriteLockStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
                ("grok", "Grok", ""),
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

    def test_single_writer_per_path(self):
        got = self.store.lock_acquire("cursor", [A], window_id="w-c1")
        self.assertEqual([item["path"] for item in got["locks"]], [A])

        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("grok", [A], window_id="w-g1")
        self.assertEqual(ctx.exception.code, "locked")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("别硬写", str(ctx.exception))

        # 同一个 agent 再拿一次 = 续期，不是冲突
        again = self.store.lock_acquire("cursor", [A], window_id="w-c1")
        self.assertEqual(again["locks"][0]["agent"], "cursor")

        # 换个人也不许绕过（scope 只看路径）
        self.assertEqual(self.store.lock_list()["count"], 1)

    def test_multi_path_acquire_is_all_or_nothing(self):
        self.store.lock_acquire("grok", [B], window_id="w-g1")
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [A, B], window_id="w-c1")
        self.assertEqual(ctx.exception.code, "locked")
        # 一个都没拿到（不是拿到 A、丢了 B）
        self.assertIsNone(
            [item for item in self.store.lock_list()["locks"] if item["agent"] == "cursor"]
            or None
        )

    def test_release_only_own_and_force_needs_coordinator(self):
        self.store.lock_acquire("cursor", [A], window_id="w-c1")
        self.store.lock_release("grok", [A])
        self.assertEqual(self.store.lock_list()["count"], 1, "不能放别人的锁")
        with self.assertRaises(MailError) as ctx:
            self.store.lock_release("cursor", force=True)
        self.assertEqual(ctx.exception.code, "not_coordinator")
        self.store.lock_release("opencode", force=True)
        self.assertEqual(self.store.lock_list()["count"], 0, "coordinator 可清场")

    def test_expired_lock_is_swept(self):
        self.store.lock_acquire("cursor", [A], window_id="w-c1")
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.execute("UPDATE locks SET expires_at = '2020-01-01T00:00:00Z' WHERE path = ?", (A,))
        con.commit()
        con.close()
        got = self.store.lock_acquire("grok", [A], window_id="w-g1")
        self.assertEqual(got["locks"][0]["agent"], "grok")

    def test_window_leave_releases_its_locks(self):
        self.store.lock_acquire("cursor", [A, B], window_id="w-c1")
        self.store.leave_window("w-c1", agent="cursor")
        self.assertEqual(self.store.lock_list()["count"], 0)

    def test_http_roundtrip(self):
        got = request(self.url, "POST", "/v1/locks", "cursor", {"paths": [A], "task_id": "t1"})
        self.assertEqual(got["locks"][0]["path"], A)
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", "/v1/locks", "grok", {"paths": [A]})
        self.assertEqual(ctx.exception.status, 409)
        listed = request(self.url, "GET", "/v1/locks", "grok")
        self.assertEqual(listed["count"], 1)
        request(self.url, "POST", "/v1/locks/release", "cursor", {"paths": [A]})
        self.assertEqual(request(self.url, "GET", "/v1/locks", "grok")["count"], 0)


if __name__ == "__main__":
    unittest.main()
