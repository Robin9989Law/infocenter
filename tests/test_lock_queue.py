"""排队改同一个文件：**由第一个 agent（拿钥匙的那个）合并后边所有要改的内容**（human 2026-10-15）。

场景：A 拿着钥匙在改 store.py，B、C 也想改。旧做法是 B、C 干等 → A 提交放锁 → B 拿钥匙改 → 提交放锁
→ C 再来……每一轮都要重新拿钥匙、重新提交。新做法：

- B、C **不需要钥匙**：`ic queue <路径> --note "我要改什么"`（也可以带 `--patch-file`）把改动**提**给 A；
- A 在 `ic locks` 里一眼看到「📥 有 N 条排队等你合并」，把内容并进自己这一次改动，
  然后 `ic queue <路径> --merge` 收口；
- 提议与合并**都进那条只增链**（`propose` / `propose_merged`）⇒ 谁提了什么、谁合并了，事后可查。

权限：**只有拿钥匙的人能合并**（coordinator 例外——钥匙的主人不在时才有人能收口）。
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

WS = "/Users/robinlaw/Downloads/信息中心"


class QueueStoreTest(unittest.TestCase):
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

    # ── 提议不需要钥匙 ────────────────────────────────────────────────────
    def test_queue_without_key(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        item = self.store.queue_change("cursor", self.path, note="把 foo() 改成 bar()")
        self.assertEqual(item["path"], self.path)
        self.assertEqual(item["held_by"], "grok", "要告诉提议人：钥匙在谁手里")
        queued = self.store.list_queue(self.path)
        self.assertEqual(queued["count"], 1)
        self.assertEqual(queued["pending"][0]["note"], "把 foo() 改成 bar()")
        self.assertIn("propose", self._actions(), "提议必须有痕迹")

    def test_queue_rejects_empty_change(self):
        with self.assertRaises(MailError) as ctx:
            self.store.queue_change("cursor", self.path)
        self.assertEqual(ctx.exception.code, "empty_change")

    def test_queue_can_carry_a_patch(self):
        item = self.store.queue_change("cursor", self.path, patch="--- a\n+++ b\n")
        self.assertIn("+++ b", item["patch"])

    def test_holder_sees_the_queue_on_ic_locks(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="改 docstring")
        self.store.queue_change("opencode", self.path, note="加一个字段")
        listed = self.store.lock_list()
        row = next(r for r in listed["locks"] if r["path"] == self.path)
        self.assertEqual(row["queued"], 2, "持有者要一眼看到排队条数")
        self.assertEqual(listed["queued_total"], 2)
        # 别的文件的锁不该被算上
        other = self.home / "other.py"
        other.write_text("y", encoding="utf-8")
        self.store.lock_acquire("cursor", [str(other.resolve())], ttl=600)
        listed = self.store.lock_list()
        self.assertEqual(next(r for r in listed["locks"] if r["path"] == str(other.resolve()))["queued"], 0)

    # ── 接力：持有者直接放钥匙（不合并），第一个排队的接手合并 ─────────────
    def test_holder_releases_without_merging_then_first_in_line_takes_over(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)          # 持有者
        self.store.queue_change("cursor", self.path, note="甲（第一个排队）")
        self.store.queue_change("kimi", self.path, note="乙（第二个排队）")
        released = self.store.lock_release("grok", [self.path])
        handover = released["handover"]
        self.assertEqual(len(handover), 1, "放钥匙时要如实说出还没合并的排队")
        self.assertEqual(handover[0]["count"], 2)
        self.assertEqual(handover[0]["next_up"], "cursor", "接手的是第一个排队的")
        self.assertEqual(self.store.list_queue(self.path)["count"], 2, "持有者不管合并，队列照旧")

        # 第一个排队的拿钥匙 → 被明确告知"你就是第一个，把整批一并合并"
        got = self.store.lock_acquire("cursor", [self.path], ttl=600)
        self.assertEqual(got["queued"][0]["first"], "cursor")
        self.assertEqual(got["queued"][0]["count"], 2)
        merged = self.store.merge_queue("cursor", self.path)
        self.assertEqual(len(merged["merged"]), 2, "一轮清一批（把乙的也一并合并）")
        self.assertEqual(merged["remaining"], 0)

    def test_queue_list_marks_positions_and_next_up(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        self.store.queue_change("kimi", self.path, note="乙")
        listed = self.store.list_queue(self.path)
        self.assertEqual(listed["next_up"], "cursor")
        self.assertTrue(listed["pending"][0]["is_first"])
        self.assertEqual(listed["pending"][0]["position"], 1)
        self.assertEqual(listed["pending"][1]["position"], 2)
        self.assertFalse(listed["pending"][1]["is_first"])

    def test_acquire_by_someone_other_than_first_is_still_reported(self):
        """不是第一个排队的人抢到了钥匙：机器不拦（谁拿钥匙谁能合并），但会如实告诉他第一个是谁。"""
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        self.store.lock_release("grok", [self.path])
        got = self.store.lock_acquire("kimi", [self.path], ttl=600)   # 插队拿到
        self.assertEqual(got["queued"][0]["first"], "cursor", "要告诉它：第一个排队的是 cursor")
        self.assertEqual(got["queued"][0]["count"], 1)

    # ── 合并只有拿钥匙的人能做 ────────────────────────────────────────────
    def test_only_key_holder_can_merge(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("cursor", self.path)
        self.assertEqual(ctx.exception.code, "not_yours")
        self.assertEqual(ctx.exception.status, 403)
        got = self.store.merge_queue("grok", self.path)  # 拿钥匙的人可以合（按规矩应让第一位，但不拦）
        self.assertEqual(len(got["merged"]), 1)
        self.assertEqual(got["remaining"], 0, "合并后排队清零")

    def test_coordinator_can_merge_when_nobody_holds_the_key(self):
        """钥匙的主人跑了也要有人能收口（否则排队永远挂着）。"""
        self.store.queue_change("cursor", self.path, note="甲")
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("grok", self.path)  # 非 coordinator、且没拿钥匙
        self.assertEqual(ctx.exception.code, "lock_required")
        self.assertEqual(ctx.exception.status, 409)
        got = self.store.merge_queue("opencode", self.path)  # coordinator 收口
        self.assertEqual(len(got["merged"]), 1)

    def test_merge_by_ids_keeps_the_rest_pending(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        first = self.store.queue_change("cursor", self.path, note="甲")
        self.store.queue_change("cursor", self.path, note="乙")
        got = self.store.merge_queue("grok", self.path, ids=[first["id"]])
        self.assertEqual(got["merged"], [first["seq"]])
        rest = self.store.list_queue(self.path)
        self.assertEqual(rest["count"], 1)
        self.assertEqual(rest["pending"][0]["note"], "乙")

    def test_merge_with_nothing_queued_is_an_error(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("grok", self.path)
        self.assertEqual(ctx.exception.code, "empty_queue")

    # ── 留痕与可审计 ──────────────────────────────────────────────────────
    def test_queue_events_are_in_the_append_only_chain(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲", task_id="")
        self.store.merge_queue("grok", self.path)
        actions = self._actions()
        self.assertIn("propose", actions)
        self.assertIn("propose_merged", actions)
        self.assertTrue(self.store.audit_events()["ok"], "排队/合并的痕迹不能破坏链")

    def test_merged_items_are_kept_as_history(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        self.store.queue_change("cursor", self.path, note="甲")
        self.store.merge_queue("grok", self.path)
        history = self.store.list_queue(self.path, include_done=True)["merged"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["note"], "甲", "合并过的提议仍可查（谁提的、谁合的）")

    def test_lock_conflict_message_points_to_the_queue(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [self.path], ttl=600)
        self.assertIn("ic queue", ctx.exception.message, "409 要教他怎么排队")


class QueueHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
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
        listed = request(self.url, "GET", "/v1/locks/queue", "opencode", query={"path": self.path})
        self.assertEqual(listed["count"], 1)
        merged = request(self.url, "POST", "/v1/locks/queue/merge", "grok", {"path": self.path})
        self.assertEqual(len(merged["merged"]), 1)
        after = request(self.url, "GET", "/v1/locks/queue", "opencode", query={"path": self.path})
        self.assertEqual(after["count"], 0)

    def test_http_merge_by_non_holder_is_403(self):
        self.store.lock_acquire("grok", [self.path], ttl=600)
        request(self.url, "POST", "/v1/locks/queue", "cursor", {"path": self.path, "note": "甲"})
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", "/v1/locks/queue/merge", "cursor", {"path": self.path})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "not_yours")


if __name__ == "__main__":
    unittest.main()
