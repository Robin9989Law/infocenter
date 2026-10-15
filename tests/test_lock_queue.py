"""待改清单 + 自己的 TODO（human 2026-10-15 最终定稿）。

规矩（原话）：
- **不通知了。每个 agent 跟着自己的 todo 轮。能做就做，不做就下一个。agent 多了，反正就是无需乱做。**

所以这套东西只剩两件事：
1. **记一笔**：想改一个（可能被别人锁着的）文件 ⇒ `ic queue <路径> --note "要改什么"`
   —— **不需要钥匙**，并且**自动写进提出者自己的 `TODO-<名字>.md`**（这才是"别忘了"）。
2. **照自己的 TODO 轮**：轮到那条时 `ic lock <路径>`；**能拿到就做，拿不到就跳过、做下一条**。
   没有排队、没有队首、没有通知、没有"顶上"——锁的归属与 TTL 就是全部协调。
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

WS = "/tmp/infocenter-demo"


class PendingChangeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db = self.home / "mailbox.db"
        self.store = Store(
            self.db,
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", ""), ("grok", "Grok", "")],
        )
        self.file = self.home / "shared.py"
        self.file.write_text("x\n", encoding="utf-8")
        self.path = str(self.file.resolve())

    def tearDown(self):
        try:
            os.chmod(self.file, 0o644)
        except OSError:
            pass
        self.tmp.cleanup()

    def _actions(self):
        con = sqlite3.connect(self.db, timeout=10)
        rows = [row[0] for row in con.execute("SELECT action FROM task_events ORDER BY seq")]
        con.close()
        return rows

    # ── 记一笔：不需要钥匙 ────────────────────────────────────────────────
    def test_record_without_key_and_tell_who_holds_it(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        item = self.store.queue_change("cursor", self.path, note="把 foo() 改成 bar()")
        self.assertEqual(item["path"], self.path)
        self.assertEqual(item["held_by"], "grok", "要告诉提出者：钥匙现在在谁手里")
        listed = self.store.list_queue(self.path)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["pending"][0]["note"], "把 foo() 改成 bar()")
        self.assertIn("propose", self._actions(), "记的这一笔要进只增链")

    def test_record_rejects_empty_change(self):
        with self.assertRaises(MailError) as ctx:
            self.store.queue_change("cursor", self.path)
        self.assertEqual(ctx.exception.code, "empty_change")

    def test_record_can_carry_a_patch(self):
        item = self.store.queue_change("cursor", self.path, patch="--- a\n+++ b\n")
        self.assertIn("+++ b", item["patch"])

    def test_holder_sees_pending_count_on_ic_locks(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        self.store.queue_change("opencode", self.path, note="乙")
        listed = self.store.lock_list()
        row = next(r for r in listed["locks"] if r["path"] == self.path)
        self.assertEqual(row["queued"], 2)
        self.assertEqual(listed["queued_total"], 2)

    # ── 能做就做，不做就下一个 ────────────────────────────────────────────
    def test_cannot_do_it_now_means_leave_it_and_move_on(self):
        """拿不到钥匙 ⇒ 它**就跳过**（不阻塞、不等别人做完）；那一笔留在清单里等下次。"""
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [self.path], ttl=600)
        self.assertEqual(ctx.exception.code, "locked")
        self.assertEqual(self.store.list_queue(self.path)["count"], 1, "跳过之后清单不变")

        # 钥匙空了，轮到它时（下一次心跳走到这条）就能做
        self.store.lock_release("grok", [self.path])
        got = self.store.lock_acquire("cursor", [self.path], ttl=600)
        self.assertEqual(got["locks"][0]["agent"], "cursor")

    def test_nobody_needs_to_wait_or_be_notified(self):
        """不排队、不通知：放钥匙不产生任何「交给谁」的东西，也不发信。"""
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        released = self.store.lock_release("grok", [self.path])
        self.assertNotIn("offer", released, "不再有「通知下一个」这种机制")
        self.assertEqual(self.store.list_queue(self.path)["count"], 1)
        # 没有任何 offer/offer_expired/offer_taken 事件
        actions = self._actions()
        for gone in ("offer", "offer_expired", "offer_taken"):
            self.assertNotIn(gone, actions, f"不该再有 {gone}")

    def test_note_is_marked_done_when_it_lands(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        item = self.store.queue_change("cursor", self.path, note="甲")
        self.store.lock_release("grok", [self.path])
        self.store.lock_acquire("cursor", [self.path], ttl=600)  # 轮到自己：拿钥匙
        got = self.store.merge_queue("cursor", self.path, ids=[item["id"]])
        self.assertEqual(got["merged"], [item["seq"]], "落地后把自己那一笔勾掉")
        after = self.store.list_queue(self.path)
        self.assertEqual(after["count"], 0)
        history = self.store.list_queue(self.path, include_done=True)["merged"]
        self.assertEqual(history[0]["note"], "甲", "勾掉的那笔仍可查（谁提的、什么时候落的）")

    # ── 权限：只有拿钥匙的人（或 coordinator）能把那一笔标成落地 ──────────
    def test_only_key_holder_can_mark_done(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("cursor", self.path)
        self.assertEqual(ctx.exception.code, "not_yours")
        self.assertEqual(ctx.exception.status, 403)
        self.store.merge_queue("grok", self.path)

    def test_coordinator_can_mark_done_when_nobody_holds_the_key(self):
        self.store.queue_change("cursor", self.path, note="甲")
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("grok", self.path)
        self.assertEqual(ctx.exception.code, "lock_required")
        self.store.merge_queue("opencode", self.path)

    def test_marking_done_with_nothing_pending_is_an_error(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("grok", self.path)
        self.assertEqual(ctx.exception.code, "empty_queue")

    def test_conflict_message_no_longer_talks_about_queueing(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [self.path], ttl=600)
        self.assertIn("ic queue", ctx.exception.message, "要教它把这件事记下来（写进自己 TODO）")

    def test_audit_stays_green_with_pending_changes(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        self.store.merge_queue("grok", self.path)
        self.assertTrue(self.store.audit_events()["ok"])


class PendingChangeHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        # 宿主若是 agent 会话，INFOCENTER_WINDOW 会经 client.request 漏进被测请求
        # （窗口先被前一个身份占掉 ⇒ 后一个身份 window_conflict）
        os.environ.pop("INFOCENTER_WINDOW", None)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", ""), ("grok", "Grok", "")],
        )
        self.file = self.home / "shared.py"
        self.file.write_text("x\n", encoding="utf-8")
        self.path = str(self.file.resolve())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            os.chmod(self.file, 0o644)
        except OSError:
            pass
        self.tmp.cleanup()

    def test_http_roundtrip(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        posted = request(
            self.url, "POST", "/v1/locks/queue", "cursor",
            {"path": self.path, "note": "把 X 改成 Y", "task": ""},
        )
        self.assertEqual(posted["note"], "把 X 改成 Y")
        self.assertEqual(posted["held_by"], "grok")
        listed = request(self.url, "GET", "/v1/locks/queue", "opencode", query={"path": self.path})
        self.assertEqual(listed["count"], 1)
        merged = request(self.url, "POST", "/v1/locks/queue/merge", "grok", {"path": self.path})
        self.assertEqual(len(merged["merged"]), 1)
        after = request(self.url, "GET", "/v1/locks/queue", "opencode", query={"path": self.path})
        self.assertEqual(after["count"], 0)

    def test_http_mark_done_by_non_holder_is_403(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        request(self.url, "POST", "/v1/locks/queue", "cursor", {"path": self.path, "note": "甲"})
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", "/v1/locks/queue/merge", "cursor", {"path": self.path})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "not_yours")


if __name__ == "__main__":
    unittest.main()
