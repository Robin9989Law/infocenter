"""人的两个名字在收件箱以外的入口也算同一个人。

口径（派单 m1a14583908c06f543）：
- thread / reply / events 复用 _recipient_names 与 _same_agent，不各写一份判断。
- 信只投给 human 时，manager 能看见、能回；events 的 new 与 unread_total 认这封。
- 案卷主人落库 human、cursor 回给 manager 后，从 waiting 回到在办。
- 名册上 human / manager 的未读数一致；同一封信两行都未读只算一次。
- 控制台点人的任一名字都能看往来、都能标已读。
- 非人照旧看不见。认领不改名、不新建行。

反例：把 _recipient_names 短路成本名，经理看不见只投给 human 的信。
把 _same_agent 短路成逐字相等，cursor 回给 manager 后案卷仍停在 waiting。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.httpapi import handle
from infocenter.store import MailError, Store


class PersonAliasSurfacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("cursor", "Cursor", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("manager", "人", "manager"),
                ("human", "人", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _deliveries(self, message_id: str | None = None) -> list[tuple]:
        conn = sqlite3.connect(self.store.path)
        try:
            sql = "SELECT message_id, to_agent, status, claimed_by FROM deliveries"
            args: tuple = ()
            if message_id is not None:
                sql += " WHERE message_id = ?"
                args = (message_id,)
            sql += " ORDER BY rowid"
            return [tuple(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def _unread(self) -> dict[str, int]:
        return {item["name"]: item["unread"] for item in self.store.list_agents()}

    def _assert_manager_sees(self, msg: dict) -> None:
        thread = self.store.thread("manager", msg["thread_id"])
        self.assertIn(msg["id"], [item["id"] for item in thread["messages"]])
        events = self.store.events("manager", since=0, wait=0)
        self.assertIn(msg["id"], [item["id"] for item in events["new"]])
        self.assertGreaterEqual(events["unread_total"], 1)
        replied = self.store.reply("manager", msg["id"], "收到")
        self.assertIn("opencode", replied["to"])
        self.assertNotIn("human", replied["to"])

    def test_只投给human时经理能看能回且长轮询看得见(self):
        before = len(self._deliveries())
        msg = self.store.send("opencode", "human", "只给旧名", "正文")
        self._assert_manager_sees(msg)
        rows = self._deliveries(msg["id"])
        self.assertEqual(rows, [(msg["id"], "human", "unread", None)])
        self.assertEqual(len(self._deliveries()), before + 2)

    def test_带窗口的长轮询认领落在别名那一行(self):
        msg = self.store.send(
            "opencode", "human", "只给旧名", "正文", workspace="/tmp/alias-proj"
        )
        events = self.store.events(
            "manager", since=0, wait=0, window_id="w-mgr", workspace="/tmp/alias-proj"
        )
        self.assertEqual([item["id"] for item in events["new"]], [msg["id"]])
        self.assertEqual(events["unread_total"], 1)
        self.assertEqual(
            self._deliveries(msg["id"]),
            [(msg["id"], "human", "unread", "w-mgr")],
        )

    def test_cursor回给manager后从waiting回到在办(self):
        root = self.store.send("cursor", "human", "问", "在吗")
        self.store.create_case(
            agent="human",
            title="问",
            owner_agent="human",
            owner_window="w-human",
            thread_id=root["thread_id"],
            status="open",
        )
        mine = self.store.reply("manager", root["id"], "我来处理")
        waiting = self.store.get_case(root["thread_id"])
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["waiting_for"], "cursor")
        self.store.reply("cursor", mine["id"], "好")
        back = self.store.get_case(root["thread_id"])
        self.assertEqual(back["status"], "open")
        self.assertIsNone(back["waiting_for"])

    def test_名册两名未读一致且双行只算一次(self):
        self.store.send("opencode", "human", "只给旧名", "一")
        once = self._unread()
        self.assertEqual(once["human"], once["manager"])
        self.assertEqual(once["human"], 1)
        self.assertEqual(self.store.events("manager", since=0, wait=0)["unread_total"], 1)
        self.store.send("opencode", "human,manager", "两份", "二")
        twice = self._unread()
        self.assertEqual(twice["human"], twice["manager"])
        self.assertEqual(twice["human"], 2)
        events = self.store.events("manager", since=0, wait=0)
        self.assertEqual(events["unread_total"], 2)
        self.assertEqual(len(events["new"]), 2)

    def test_非人看不见人的信自己的信仍在(self):
        hidden = self.store.send("opencode", "human", "只给旧名", "一")
        with self.assertRaises(MailError) as ctx:
            self.store.thread("cursor", hidden["thread_id"])
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(MailError) as reply_ctx:
            self.store.reply("cursor", hidden["id"], "不该看见")
        self.assertEqual(reply_ctx.exception.code, "not_found")
        quiet = self.store.events("cursor", since=0, wait=0)
        self.assertEqual(quiet["new"], [])
        self.assertEqual(quiet["unread_total"], 0)
        own = self.store.send("opencode", "cursor", "给你", "二")
        seen = self.store.events("cursor", since=0, wait=0)
        self.assertEqual([item["id"] for item in seen["new"]], [own["id"]])
        self.assertEqual(seen["unread_total"], 1)
        self.assertEqual(self._unread()["cursor"], 1)
        self.assertNotIn(hidden["id"], [item["id"] for item in seen["new"]])

    def test_控制台两个名字都能看往来且能标已读(self):
        html = (ROOT / "infocenter" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function isPersonName", html)
        self.assertIn('loadAgentMail("manager")', html)
        self.assertIn("isPersonName(name) && m.direction", html)
        self.assertNotIn('name === "human" && m.direction', html)
        msg = self.store.send("opencode", "human", "只给旧名", "正文")
        for name in ("human", "manager"):
            status, payload = handle(
                "GET",
                f"/v1/agents/{name}/messages",
                {"X-Agent": "manager"},
                b"",
                self.store,
            )
            self.assertEqual(status, 200)
            hit = next(item for item in payload["messages"] if item["id"] == msg["id"])
            self.assertEqual(hit["direction"], "in")
            self.assertEqual(hit["my_status"], "unread")
        status, marked = handle(
            "POST",
            f"/v1/messages/{msg['id']}/read",
            {"X-Agent": "manager"},
            b"",
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertEqual(marked["delivered_to"], "human")
        self.assertEqual(marked["delivery_status"], "read")
        self.assertEqual(self._deliveries(msg["id"]), [(msg["id"], "human", "read", None)])

    def test_反例_名字解析短路则经理看不见别名信(self):
        import infocenter.store as store_mod

        msg = self.store.send("opencode", "human", "只给旧名", "正文")
        real = store_mod._recipient_names

        def only_self(agent: str) -> tuple[str, ...]:
            return (store_mod.normalize_name(agent),)

        store_mod._recipient_names = only_self
        try:
            with self.assertRaises(MailError):
                self.store.thread("manager", msg["thread_id"])
            events = self.store.events("manager", since=0, wait=0)
            self.assertEqual(events["new"], [])
            self.assertEqual(events["unread_total"], 0)
            with self.assertRaises(MailError):
                self.store.reply("manager", msg["id"], "看不见")
            unread = self._unread()
            self.assertNotEqual(unread.get("manager", 0), unread.get("human", 0))
            with self.assertRaises(MailError):
                self._assert_manager_sees(msg)
        finally:
            store_mod._recipient_names = real
        self._assert_manager_sees(msg)

    def test_反例_持有者比较短路则回不到在办(self):
        import infocenter.store as store_mod

        root = self.store.send("cursor", "human", "问", "在吗")
        self.store.create_case(
            agent="human",
            title="问",
            owner_agent="human",
            owner_window="w-human",
            thread_id=root["thread_id"],
            status="open",
        )
        mine = self.store.reply("manager", root["id"], "我来处理")
        real = store_mod._same_agent
        store_mod._same_agent = lambda left, right: left == right
        try:
            self.store.reply("cursor", mine["id"], "好")
            stuck = self.store.get_case(root["thread_id"])
            self.assertEqual(stuck["status"], "waiting")
            with self.assertRaises(AssertionError):
                self.assertEqual(stuck["status"], "open")
        finally:
            store_mod._same_agent = real


if __name__ == "__main__":
    unittest.main()
