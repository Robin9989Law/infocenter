"""双投残余：同一封信落在 human 与 manager 两行时，收件箱折成一份，read 一次签完。

口径（笔3裁定 a）：
- inbox() 在 workspace 过滤后调 _collapse_alias_deliveries。
- read：两行都未读则一次都标 read；本名已不是未读、别名仍未读则改签别名。
- 不新建行、不改 to_agent。archive 仍只动先找到的那一行。别人的行不动。
- 只投一个名字时仍只落在那一行。
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


class DualDeliverySignoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("cursor", "Cursor", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("kimi", "Kimi", ""),
                ("manager", "人", "manager"),
                ("human", "人", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, message_id: str) -> list[tuple]:
        conn = sqlite3.connect(self.store.path)
        try:
            return [
                tuple(row)
                for row in conn.execute(
                    """
                    SELECT to_agent, status FROM deliveries
                    WHERE message_id = ? ORDER BY to_agent
                    """,
                    (message_id,),
                )
            ]
        finally:
            conn.close()

    def _count(self) -> int:
        conn = sqlite3.connect(self.store.path)
        try:
            return conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        finally:
            conn.close()

    def test_inbox折成一行且未读数不翻倍(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        box = self.store.inbox("manager", status="unread", limit=20)
        ids = [item["id"] for item in box["messages"]]
        self.assertEqual(ids, [both["id"]])
        self.assertEqual(box["count"], 1)
        self.assertEqual(box["unread_total"], 1)
        self.assertEqual(box["messages"][0]["delivered_to"], "manager")
        self.assertEqual(self._rows(both["id"]), [("human", "unread"), ("manager", "unread")])

    def test_两行都未读时签一次名册归零(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        before = self._count()
        self.store.mark("manager", both["id"], "read")
        self.assertEqual(self._count(), before)
        self.assertEqual(self._rows(both["id"]), [("human", "read"), ("manager", "read")])
        box = self.store.inbox("human", status="unread", limit=20)
        self.assertEqual(box["unread_total"], 0)
        self.assertEqual(box["messages"], [])

    def test_本名已读别名仍未读时改签别名(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(
                """
                UPDATE deliveries SET status = 'read', read_at = '2026-10-17T04:00:00Z'
                WHERE message_id = ? AND to_agent = 'manager'
                """,
                (both["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        before = self._count()
        self.store.mark("manager", both["id"], "read")
        self.assertEqual(self._count(), before)
        self.assertEqual(self._rows(both["id"]), [("human", "read"), ("manager", "read")])
        self.assertEqual(self.store.inbox("manager", status="unread", limit=20)["unread_total"], 0)

    def test_带窗口签收也清掉另一行(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        self.store.mark("human", both["id"], "read", window_id="w-person")
        self.assertEqual(self._rows(both["id"]), [("human", "read"), ("manager", "read")])

    def test_归档仍只动本名(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        self.store.mark("manager", both["id"], "archived")
        self.assertEqual(self._rows(both["id"]), [("human", "unread"), ("manager", "archived")])

    def test_别人的行不动且只投一名仍落在那一行(self):
        foreign = self.store.send("opencode", "kimi,manager", "带外人", "各一份")
        before = self._count()
        self.store.mark("manager", foreign["id"], "read")
        self.assertEqual(self._count(), before)
        self.assertEqual(
            self._rows(foreign["id"]),
            [("kimi", "unread"), ("manager", "read")],
        )
        single = self.store.send("opencode", "human", "只给旧名", "一份")
        self.store.mark("manager", single["id"], "read")
        self.assertEqual(self._rows(single["id"]), [("human", "read")])
