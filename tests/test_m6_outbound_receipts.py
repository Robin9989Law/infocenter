"""M6-2 判据：发件人能看出谁未读、谁已读（时间）。只读，不动 deliveries 表。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import Store

DELIVERY_COLUMNS = [
    ("message_id", "TEXT", 1, None),
    ("to_agent", "TEXT", 1, None),
    ("status", "TEXT", 1, None),
    ("read_at", "TEXT", 0, None),
    ("claimed_by", "TEXT", 0, None),
    ("claimed_at", "TEXT", 0, None),
]


class M6OutboundReceiptTest(unittest.TestCase):
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

    def tearDown(self):
        self.tmp.cleanup()

    def _delivery_snapshot(self) -> tuple:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cols = con.execute("PRAGMA table_info(deliveries)").fetchall()
            rows = con.execute(
                "SELECT message_id, to_agent, status, read_at, claimed_by, claimed_at "
                "FROM deliveries ORDER BY message_id, to_agent"
            ).fetchall()
            return cols, rows
        finally:
            con.close()

    def test_sender_sees_each_recipient_status_and_read_at(self):
        msg = self.store.send("opencode", ["cursor", "grok"], "派单", "请看")
        before_cols, before_rows = self._delivery_snapshot()
        self.assertEqual(
            [(c[1], c[2], c[3], c[4]) for c in before_cols],
            DELIVERY_COLUMNS,
        )
        self.assertEqual(len(before_rows), 2)

        self.store.mark("cursor", msg["id"], "read")
        marked_cols, marked_rows = self._delivery_snapshot()
        self.assertEqual(before_cols, marked_cols)
        self.assertEqual(len(marked_rows), len(before_rows))

        mine = self.store.thread("opencode", msg["thread_id"])["messages"][0]
        self.assertIsNone(mine["delivery_status"])
        self.assertIsNone(mine["read_at"])
        receipts = {item["name"]: item for item in mine["recipient_receipts"]}
        self.assertEqual(receipts["cursor"]["status"], "read")
        self.assertIsNotNone(receipts["cursor"]["read_at"])
        self.assertEqual(receipts["grok"]["status"], "unread")
        self.assertIsNone(receipts["grok"]["read_at"])

        after_cols, after_rows = self._delivery_snapshot()
        self.assertEqual(marked_cols, after_cols)
        self.assertEqual(marked_rows, after_rows)

        other = self.store.thread("grok", msg["thread_id"])["messages"][0]
        self.assertNotIn("recipient_receipts", other)
        self.assertEqual(other["delivery_status"], "unread")


if __name__ == "__main__":
    unittest.main()
