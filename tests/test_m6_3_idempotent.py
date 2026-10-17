"""M6-3：发送幂等只认正文全同。

键是发件人 + reply_to + 主题 + 正文逐字节。命中则不新建，静默返回最早那条的 id。
不看收件人、优先级、workspace。不模糊匹配，不加列，不加开关。
已有的全同行不合并。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import MailError, Store

MESSAGE_COLUMNS = (
    "id",
    "thread_id",
    "from_agent",
    "subject",
    "body",
    "priority",
    "workspace",
    "reply_to",
    "created_at",
)


class IdempotentSendTest(unittest.TestCase):
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
                ("grok", "Grok", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _counts(self) -> tuple[int, int]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            deliveries = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            return messages, deliveries
        finally:
            conn.close()

    def _message_columns(self) -> tuple[str, ...]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return tuple(row[1] for row in conn.execute("PRAGMA table_info(messages)"))
        finally:
            conn.close()

    def _workspace_of(self, message_id: str) -> str | None:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT workspace FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_连发两封全同只留一条且投递不翻倍(self):
        first = self.store.send("opencode", ["cursor", "grok"], "同题", "逐字节", priority="high")
        before = self._counts()
        calls = []
        original = self.store._signal

        def spy():
            calls.append("signal")
            return original()

        self.store._signal = spy
        again = self.store.send("opencode", ["cursor", "grok"], "同题", "逐字节", priority="high")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._counts(), before)
        self.assertEqual(before, (1, 2))
        self.assertEqual(calls, [])
        self.assertEqual(self._message_columns(), MESSAGE_COLUMNS)

    def test_正文差一个字符就是两条(self):
        first = self.store.send("opencode", "cursor", "同题", "逐字节")
        other = self.store.send("opencode", "cursor", "同题", "逐字节 ")
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(self._counts()[0], 2)

    def test_同正文不同主题或不同回复父信就是两条(self):
        root = self.store.send("cursor", "opencode", "问", "父信")
        first = self.store.send(
            "opencode", "cursor", "同题", "逐字节", reply_to=root["id"]
        )
        other_subject = self.store.send(
            "opencode", "cursor", "另一题", "逐字节", reply_to=root["id"]
        )
        other_parent = self.store.send("opencode", "cursor", "同题", "逐字节")
        self.assertNotEqual(other_subject["id"], first["id"])
        self.assertNotEqual(other_parent["id"], first["id"])
        self.assertEqual(self._counts()[0], 4)

    def test_换收件人换优先级换项目仍命中且不改旧行(self):
        first = self.store.send(
            "opencode", "cursor", "同题", "逐字节", workspace="/tmp/proj-a"
        )
        before = self._counts()
        again = self.store.send(
            "opencode",
            "grok",
            "同题",
            "逐字节",
            priority="high",
            workspace="/tmp/proj-b",
        )
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._workspace_of(first["id"]), "/tmp/proj-a")
        self.assertEqual(again["workspace"], "/tmp/proj-a")

    def test_另一个发件人相同正文仍是新信(self):
        first = self.store.send("opencode", "cursor", "同题", "逐字节")
        other = self.store.send("grok", "cursor", "同题", "逐字节")
        self.assertNotEqual(other["id"], first["id"])

    def test_已有全同行不合并_再发返回最早那条(self):
        first = self.store.send("opencode", "cursor", "同题", "逐字节")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO messages(
                    id, thread_id, from_agent, subject, body, priority,
                    workspace, reply_to, created_at
                ) VALUES ('m-hist', 'm-hist', 'opencode', '同题', '逐字节', 'normal', NULL, NULL, '2099-01-01T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO deliveries(message_id, to_agent, status, read_at)
                VALUES ('m-hist', 'cursor', 'unread', NULL)
                """
            )
            conn.commit()
        finally:
            conn.close()
        again = self.store.send("opencode", "cursor", "同题", "逐字节")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._counts()[0], 2)

    def test_回复连发两次返回同一封(self):
        root = self.store.send("opencode", "cursor", "派单", "请办")
        first = self.store.reply("cursor", root["id"], "办好了")
        again = self.store.reply("cursor", root["id"], "办好了")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._counts()[0], 2)

    def test_首次发给不存在的身份仍拒绝_重放不报错(self):
        first = self.store.send("opencode", "cursor", "同题", "逐字节")
        with self.assertRaises(MailError) as ctx:
            self.store.send("opencode", "nobody", "新题", "别的正文")
        self.assertEqual(ctx.exception.code, "no_such_recipient")
        again = self.store.send("opencode", "nobody", "同题", "逐字节")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self._counts()[0], 1)


if __name__ == "__main__":
    unittest.main()
