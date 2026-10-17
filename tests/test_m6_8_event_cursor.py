"""M6-8：事件流游标只向前，不把已经越过的未读再放进 new。

判据（临时库）：
1. 旧封被窗口 A 认领，B 取走更新的信后，A 离场：B 的 events.new 不含旧封，inbox 含，unread_total ≥ 1。
2. A 仍在、workspace 相容：旧封仍不出现在 B 的 events.new。
3. 空 workspace 的未读被越过后补上本项目：inbox 滤得见，events.new 不出现。
4. 认领窗口已 left、还没人 inbox/events 时，send 一封无关信就清掉认领。deliveries 不增列，行数只加新信那一行。
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


class M68EventCursorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db_path = self.home / "mailbox.db"
        self.store = Store(
            self.db_path,
            seeds=[
                ("cursor", "Cursor", ""),
                ("opencode", "OpenCode", "coordinator"),
            ],
        )
        self.proj = str(self.home / "proj")

    def tearDown(self):
        self.tmp.cleanup()

    def _claim(self, message_id: str, agent: str = "cursor"):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT claimed_by FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _delivery_columns(self) -> list[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(deliveries)")]
        finally:
            conn.close()

    def _delivery_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        finally:
            conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _ids(self, payload: dict) -> list[str]:
        return [item["id"] for item in payload["new"]]

    def test_越过的旧信不回放但收件箱仍能看到(self):
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-a")
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-b")
        old = self.store.send("opencode", "cursor", "旧", "一", workspace=self.proj)
        taken = self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-a")
        self.assertEqual(self._ids(taken), [old["id"]])
        new = self.store.send("opencode", "cursor", "新", "二", workspace=self.proj)
        jumped = self.store.events(
            "cursor", since=taken["cursor"], wait=0, workspace=self.proj, window_id="w-b"
        )
        self.assertEqual(self._ids(jumped), [new["id"]])
        self.assertGreater(jumped["cursor"], taken["cursor"])
        self.store.leave_window("w-a", agent="cursor")
        after = self.store.events(
            "cursor", since=jumped["cursor"], wait=0, workspace=self.proj, window_id="w-b"
        )
        self.assertNotIn(old["id"], self._ids(after))
        self.assertGreaterEqual(after["unread_total"], 1)
        box = self.store.inbox(
            "cursor", status="unread", limit=20, workspace=self.proj, window_id="w-b"
        )
        self.assertIn(old["id"], [item["id"] for item in box["messages"]])

    def test_活窗口的认领仍遮蔽旧信(self):
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-a")
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-b")
        old = self.store.send("opencode", "cursor", "旧", "一", workspace=self.proj)
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-a")
        new = self.store.send("opencode", "cursor", "新", "二", workspace=self.proj)
        seen = self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-b")
        self.assertIn(new["id"], self._ids(seen))
        self.assertNotIn(old["id"], self._ids(seen))
        self.assertEqual(self._claim(old["id"]), "w-a")

    def test_补上workspace后收件箱看得见事件流不回放(self):
        self.store.events("cursor", since=0, wait=0, window_id="w-hold")
        blank = self.store.send("opencode", "cursor", "空", "没有项目")
        held = self.store.events("cursor", since=0, wait=0, window_id="w-hold")
        self.assertEqual(self._ids(held), [blank["id"]])
        self.assertEqual(self._claim(blank["id"]), "w-hold")
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-b")
        later = self.store.send("opencode", "cursor", "项目内", "二", workspace=self.proj)
        moved = self.store.events(
            "cursor", since=held["cursor"], wait=0, workspace=self.proj, window_id="w-b"
        )
        self.assertEqual(self._ids(moved), [later["id"]])
        self.assertGreater(moved["cursor"], held["cursor"])
        self._exec(
            "UPDATE windows SET state = 'left', left_at = '2026-10-17T04:00:00Z' WHERE window_id = 'w-hold'"
        )
        self._exec("UPDATE messages SET workspace = ? WHERE id = ?", (self.proj, blank["id"]))
        after = self.store.events(
            "cursor", since=moved["cursor"], wait=0, workspace=self.proj, window_id="w-b"
        )
        self.assertNotIn(blank["id"], self._ids(after))
        box = self.store.inbox(
            "cursor", status="unread", limit=20, workspace=self.proj, window_id="w-b"
        )
        self.assertIn(blank["id"], [item["id"] for item in box["messages"]])

    def test_send释放已离场窗口的认领且不增列(self):
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-dead")
        msg = self.store.send("opencode", "cursor", "被扣住", "一", workspace=self.proj)
        self.store.events("cursor", since=0, wait=0, workspace=self.proj, window_id="w-dead")
        self.assertEqual(self._claim(msg["id"]), "w-dead")
        self._exec(
            "UPDATE windows SET state = 'left', left_at = '2026-10-17T04:00:00Z' WHERE window_id = 'w-dead'"
        )
        self.assertEqual(self._claim(msg["id"]), "w-dead")
        columns = self._delivery_columns()
        before = self._delivery_count()
        self.store.send("cursor", "opencode", "无关", "只用来触发对账")
        self.assertIsNone(self._claim(msg["id"]))
        self.assertEqual(self._delivery_columns(), columns)
        self.assertEqual(self._delivery_count(), before + 1)
