"""M6-1 / M6-2：send 不丢 workspace，出站回执只在读侧可见。

解析顺序：显式 > 父信 > 案卷 > 窗口已登记 workspace >
客户端 cwd 命中已登记 project。窗口单独有值即可，不再要求与 cwd 一致。
不读进程 cwd，推断路径不调用 _upsert_project。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import Store


class M6WorkspaceReceiptTest(unittest.TestCase):
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
                ("grok", "Grok", ""),
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

    def _backdate(self, message_id: str, created_at: str) -> None:
        con = sqlite3.connect(self.db_path, timeout=10)
        try:
            con.execute("UPDATE messages SET created_at = ? WHERE id = ?", (created_at, message_id))
            con.commit()
        finally:
            con.close()

    def test_send_reply_to_inherits_parent_without_new_project(self):
        parent = self.store.send("opencode", ["cursor"], "父信", "正文", workspace=self.canonical)
        before = self._projects()
        child = self.store.send("opencode", ["cursor"], "跟帖", "正文", reply_to=parent["id"])
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "parent")
        self.assertFalse(child["workspace_unscoped"])
        self.assertEqual(self._projects(), before)

    def test_reply_inherits_parent_workspace(self):
        parent = self.store.send("opencode", ["cursor"], "父信", "正文", workspace=self.canonical)
        child = self.store.reply("cursor", parent["id"], "收到")
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "parent")

    def test_explicit_workspace_overrides_parent_and_stays_verbatim(self):
        self.store.add_project(self.canonical)
        parent = self.store.send("opencode", ["cursor"], "父信", "正文", workspace=self.canonical)
        child = self.store.send(
            "opencode",
            ["cursor"],
            "改道",
            "正文",
            workspace=self.proj.name,
            reply_to=parent["id"],
        )
        self.assertEqual(child["workspace"], self.proj.name)
        self.assertEqual(child["workspace_source"], "explicit")
        self.assertNotEqual(child["workspace"], self.canonical)

    def test_case_workspace_used_when_parent_empty(self):
        parent = self.store.send("opencode", ["cursor"], "无范围", "正文")
        self.assertIsNone(parent["workspace"])
        self.store.create_case(
            agent="opencode",
            title="补范围",
            workspace=self.canonical,
            thread_id=parent["thread_id"],
        )
        before = self._projects()
        child = self.store.send("cursor", ["opencode"], "跟案卷", "正文", reply_to=parent["id"])
        replied = self.store.reply("opencode", parent["id"], "reply 也走案卷")
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "case")
        self.assertEqual(replied["workspace"], self.canonical)
        self.assertEqual(replied["workspace_source"], "case")
        self.assertEqual(self._projects(), before)

    def test_parent_beats_case(self):
        parent = self.store.send("opencode", ["cursor"], "父信", "正文", workspace=self.canonical)
        other = str((self.home / "case-other").resolve())
        self.store.create_case(
            agent="opencode",
            title="另一案卷",
            workspace=other,
            thread_id=parent["thread_id"],
        )
        child = self.store.send("cursor", ["opencode"], "跟父信", "正文", reply_to=parent["id"])
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "parent")

    def test_case_beats_window_cwd_agreement(self):
        parent = self.store.send("opencode", ["cursor"], "无范围", "正文")
        self.store.create_case(
            agent="opencode",
            title="案卷优先",
            workspace=self.canonical,
            thread_id=parent["thread_id"],
        )
        other = self.home / "OtherProj"
        other.mkdir()
        other_path = str(other.resolve())
        self.store.add_project(other_path)
        self.store.touch("opencode", window_id="w-other", workspace=other_path)
        child = self.store.send(
            "opencode",
            ["cursor"],
            "仍跟案卷",
            "正文",
            reply_to=parent["id"],
            window_id="w-other",
            client_cwd=other_path,
        )
        self.assertEqual(child["workspace"], self.canonical)
        self.assertEqual(child["workspace_source"], "case")

    def test_window_workspace_is_used_verbatim_without_upsert(self):
        self.store.add_project(self.canonical)
        self.store.add_project(self.proj.name)
        self.store.touch("opencode", window_id="w-bare", workspace=self.proj.name)
        before = self._projects()
        sent = self.store.send(
            "opencode",
            ["cursor"],
            "对齐",
            "正文",
            window_id="w-bare",
            client_cwd=self.canonical,
        )
        self.assertEqual(sent["workspace"], self.proj.name)
        self.assertEqual(sent["workspace_source"], "window")
        self.assertFalse(sent["workspace_unscoped"])
        self.assertEqual(self.store.get_window("w-bare")["workspace"], self.proj.name)
        self.assertEqual(self._projects(), before)

    def test_window_beats_other_cwd_and_cwd_alone_hits_project(self):
        other = self.home / "ElseProj"
        other.mkdir()
        other_path = str(other.resolve())
        self.store.add_project(self.canonical)
        self.store.add_project(other_path)
        self.store.touch("opencode", window_id="w-a", workspace=self.canonical)
        before = self._projects()

        mismatch = self.store.send(
            "opencode",
            ["cursor"],
            "不一致",
            "正文",
            window_id="w-a",
            client_cwd=other_path,
        )
        self.assertEqual(mismatch["workspace"], self.canonical)
        self.assertEqual(mismatch["workspace_source"], "window")
        self.assertFalse(mismatch["workspace_unscoped"])

        window_only = self.store.send(
            "opencode", ["cursor"], "只有窗口", "正文", window_id="w-a"
        )
        self.assertEqual(window_only["workspace"], self.canonical)
        self.assertEqual(window_only["workspace_source"], "window")

        self.store.touch("opencode", window_id="w-empty", workspace=None)
        cwd_only = self.store.send(
            "opencode",
            ["cursor"],
            "只有 cwd",
            "正文",
            window_id="w-empty",
            client_cwd=self.canonical,
        )
        self.assertEqual(cwd_only["workspace"], self.canonical)
        self.assertEqual(cwd_only["workspace_source"], "project")

        ghost = str((self.home / "not-a-project").resolve())
        self.assertNotIn(ghost, self._projects())
        self.assertEqual(self._projects(), before)

    def test_two_absolute_projects_are_not_guessed(self):
        left = self.home / "left" / "samename"
        right = self.home / "right" / "samename"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        left_path = str(left.resolve())
        right_path = str(right.resolve())
        self.store.add_project(left_path)
        self.store.add_project(right_path)
        self.store.touch("opencode", window_id="w-blank", workspace=None)
        sent = self.store.send(
            "opencode",
            ["cursor"],
            "同名两条",
            "正文",
            window_id="w-blank",
            client_cwd="samename",
        )
        self.assertIsNone(sent["workspace"])
        self.assertEqual(sent["workspace_source"], "empty")

    def test_archived_project_is_not_adopted(self):
        self.store.add_project(self.canonical, archived=True)
        self.store.touch("opencode", window_id="w-arch", workspace=None)
        sent = self.store.send(
            "opencode",
            ["cursor"],
            "已归档",
            "正文",
            window_id="w-arch",
            client_cwd=self.canonical,
        )
        self.assertIsNone(sent["workspace"])
        self.assertTrue(sent["workspace_unscoped"])
        self.assertEqual(self._projects()[self.canonical][0], 1)

    def test_server_cwd_is_not_consulted(self):
        here = str(Path.cwd().resolve())
        other = str((self.home / "not-here").resolve())
        self.store.add_project(here)
        self.store.touch("opencode", window_id="w-other", workspace=other)
        sent = self.store.send("opencode", ["cursor"], "不读服务端 cwd", "正文", window_id="w-other")
        self.assertEqual(sent["workspace"], other)
        self.assertEqual(sent["workspace_source"], "window")
        self.store.touch("opencode", window_id="w-blank", workspace=None)
        blank = self.store.send("opencode", ["cursor"], "没带客户端 cwd", "正文", window_id="w-blank")
        self.assertIsNone(blank["workspace"])
        self.assertEqual(blank["workspace_source"], "empty")
        self.assertTrue(blank["workspace_unscoped"])

    def test_explicit_send_still_registers_project(self):
        fresh = str((self.home / "brand-new").resolve())
        sent = self.store.send("opencode", ["cursor"], "显式", "正文", workspace=fresh)
        self.assertEqual(sent["workspace_source"], "explicit")
        self.assertIn(fresh, self._projects())

    def test_inbox_uses_same_scope_as_events(self):
        bare = self.store.send("opencode", ["cursor"], "裸名", "正文", workspace=self.proj.name)
        full = self.store.send("opencode", ["cursor"], "全路径", "正文", workspace=self.canonical)
        empty = self.store.send("opencode", ["cursor"], "空", "正文")
        other = str((self.home / "elsewhere").resolve())
        foreign = self.store.send("opencode", ["cursor"], "别的", "正文", workspace=other)

        box = self.store.inbox("cursor", status="unread", workspace=self.canonical)
        ev = self.store.events("cursor", since=0, wait=0, workspace=self.canonical)
        box_ids = {m["id"] for m in box["messages"]}
        ev_ids = {m["id"] for m in ev["new"]}
        self.assertEqual(box_ids, {bare["id"], full["id"]})
        self.assertEqual(ev_ids, box_ids)
        self.assertNotIn(empty["id"], box_ids)
        self.assertNotIn(foreign["id"], box_ids)
        self.assertEqual(box["unread_total"], 2)

        by_bare = {m["id"] for m in self.store.inbox("cursor", status="unread", workspace=self.proj.name)["messages"]}
        self.assertEqual(by_bare, {bare["id"], full["id"]})

    def test_thread_receipts_keep_own_delivery_fields(self):
        msg = self.store.send("opencode", ["cursor", "grok"], "派单", "请看", workspace=self.canonical)
        self.assertNotIn("recipient_receipts", msg)

        self.store.mark("cursor", msg["id"], "read")
        mine = self.store.thread("opencode", msg["thread_id"])["messages"][0]
        self.assertIsNone(mine["delivery_status"])
        self.assertIsNone(mine["read_at"])
        receipts = {item["name"]: item for item in mine["recipient_receipts"]}
        self.assertEqual(set(receipts), {"cursor", "grok"})
        self.assertEqual(receipts["cursor"]["status"], "read")
        self.assertIsNotNone(receipts["cursor"]["read_at"])
        self.assertEqual(receipts["grok"]["status"], "unread")
        self.assertIsNone(receipts["grok"]["read_at"])
        self.assertEqual(set(receipts["cursor"]), {"name", "status", "read_at"})

        cursor_view = self.store.thread("cursor", msg["thread_id"])["messages"][0]
        self.assertNotIn("recipient_receipts", cursor_view)
        self.assertEqual(cursor_view["delivery_status"], "read")
        self.assertIsNotNone(cursor_view["read_at"])

        outbound = next(row for row in self.store.agent_messages("opencode") if row["id"] == msg["id"])
        self.assertEqual(outbound["direction"], "out")
        self.assertIsNone(outbound["my_status"])

    def test_self_send_keeps_own_status_and_lists_every_recipient(self):
        msg = self.store.send("opencode", ["opencode", "cursor"], "自投", "正文")
        view = self.store.thread("opencode", msg["thread_id"])["messages"][0]
        self.assertEqual(view["delivery_status"], "unread")
        self.assertIsNone(view["read_at"])
        self.assertEqual([item["name"] for item in view["recipient_receipts"]], ["cursor", "opencode"])

    def test_sent_reports_read_and_reply_without_marking(self):
        first = self.store.send("opencode", ["cursor", "grok"], "第一封", "正文", workspace=self.canonical)
        self._backdate(first["id"], "2020-01-01T00:00:00Z")
        middle = self.store.send(
            "opencode",
            ["cursor"],
            "中间",
            "正文",
            reply_to=first["id"],
        )
        self._backdate(middle["id"], "2020-01-02T00:00:00Z")
        late = self.store.reply("cursor", middle["id"], "回的是中间那封")
        self._backdate(late["id"], "2020-01-03T00:00:00Z")
        latest = self.store.send("opencode", ["cursor"], "更晚", "正文", reply_to=first["id"])

        before = {
            item["name"]: item["status"]
            for item in self.store.thread("opencode", first["thread_id"])["messages"][0]["recipient_receipts"]
        }
        listed = self.store.sent("opencode", limit=20)
        after = {
            item["name"]: item["status"]
            for item in self.store.thread("opencode", first["thread_id"])["messages"][0]["recipient_receipts"]
        }
        self.assertEqual(before, after)
        self.assertEqual(before["cursor"], "unread")

        by_id = {item["id"]: item for item in listed["messages"]}
        first_cursor = next(r for r in by_id[first["id"]]["recipients"] if r["name"] == "cursor")
        first_grok = next(r for r in by_id[first["id"]]["recipients"] if r["name"] == "grok")
        self.assertTrue(first_cursor["replied"])
        self.assertFalse(first_grok["replied"])
        self.assertEqual(first_cursor["status"], "unread")
        self.assertIsNone(first_cursor["read_at"])

        latest_cursor = next(r for r in by_id[latest["id"]]["recipients"] if r["name"] == "cursor")
        self.assertFalse(latest_cursor["replied"])

        self.store.mark("cursor", latest["id"], "read")
        refreshed = next(item for item in self.store.sent("opencode")["messages"] if item["id"] == latest["id"])
        seen = next(r for r in refreshed["recipients"] if r["name"] == "cursor")
        self.assertEqual(seen["status"], "read")
        self.assertIsNotNone(seen["read_at"])
        self.assertFalse(seen["replied"])

        top = self.store.sent("opencode", limit=1)
        self.assertEqual(top["count"], 1)
        self.assertEqual(top["messages"][0]["id"], latest["id"])


if __name__ == "__main__":
    unittest.main()
