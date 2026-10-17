"""人的收件箱：manager / human 两名在视图层合并，数据一行不动。

口径（派单 m1a1443051085b5655）：
- 「是不是人」只用 store.is_person()。
- inbox(agent=人) 把收件人是 manager 或 human 的 delivery 都算进来，每条带 delivered_to。
- read：先找本名那一份，没有再找另一个别名。两行都未读时一次都标已读；本名已不是未读而别名仍未读时改签别名。不改名、不新建行。
- archive：仍只改先找到的那一行。
- /v1/agents/{name}/messages 在 name 是人时同样合并。
- 非人的邮箱不动。

反例（防假绿）：把 _recipient_names 短路成本名，T1 的「两侧都看到对方那封」必须红。
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


class PersonInboxUnionTest(unittest.TestCase):
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

    def _seed(self):
        self.to_human = self.store.send("opencode", "human", "给旧名", "寄给 human")
        self.to_manager = self.store.send("opencode", "manager", "给现名", "寄给 manager")
        self.to_opencode = self.store.send("cursor", "opencode", "给机器", "寄给 opencode")

    def _inbox(self, agent: str, status: str = "all") -> dict:
        return self.store.inbox(agent, status=status, limit=100)

    def _deliveries(self) -> list[tuple]:
        conn = sqlite3.connect(self.store.path)
        try:
            return [
                tuple(row)
                for row in conn.execute(
                    "SELECT message_id, to_agent, status, claimed_by FROM deliveries ORDER BY rowid"
                )
            ]
        finally:
            conn.close()

    def _agent_names(self) -> list[str]:
        conn = sqlite3.connect(self.store.path)
        try:
            return [row[0] for row in conn.execute("SELECT name FROM agents ORDER BY name")]
        finally:
            conn.close()

    def _chain_count(self) -> int:
        conn = sqlite3.connect(self.store.path)
        try:
            return conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        finally:
            conn.close()

    def _assert_t1(self) -> None:
        mgr = self._inbox("manager")
        hum = self._inbox("human")
        mgr_ids = [item["id"] for item in mgr["messages"]]
        hum_ids = [item["id"] for item in hum["messages"]]
        self.assertEqual(mgr["count"], hum["count"])
        self.assertEqual(sorted(mgr_ids), sorted(hum_ids))
        self.assertIn(self.to_human["id"], mgr_ids)
        self.assertIn(self.to_manager["id"], hum_ids)
        self.assertNotIn(self.to_opencode["id"], mgr_ids)
        human_row = next(item for item in mgr["messages"] if item["id"] == self.to_human["id"])
        manager_row = next(item for item in hum["messages"] if item["id"] == self.to_manager["id"])
        self.assertEqual(human_row["delivered_to"], "human")
        self.assertEqual(manager_row["delivered_to"], "manager")
        own = next(item for item in mgr["messages"] if item["id"] == self.to_manager["id"])
        self.assertEqual(own["delivered_to"], "manager")

    def test_t1_两侧都看到全部(self):
        self._seed()
        self._assert_t1()
        mgr_unread = self.store.inbox("manager", status="unread", limit=100)
        hum_unread = self.store.inbox("human", status="unread", limit=100)
        self.assertEqual(mgr_unread["count"], hum_unread["count"])
        self.assertIn(self.to_human["id"], {item["id"] for item in mgr_unread["messages"]})
        self.assertIn(self.to_manager["id"], {item["id"] for item in hum_unread["messages"]})

    def test_t2_签收落在别名那一份且行数不变(self):
        self._seed()
        before = self._deliveries()
        names = self._agent_names()
        chain = self._chain_count()
        marked = self.store.mark("manager", self.to_human["id"], "read")
        after = self._deliveries()
        self.assertEqual(len(after), len(before))
        self.assertEqual(
            [(row[0], row[1]) for row in before],
            [(row[0], row[1]) for row in after],
        )
        self.assertEqual(self._agent_names(), names)
        self.assertEqual(self._chain_count(), chain)
        human = next(row for row in after if row[0] == self.to_human["id"])
        self.assertEqual(human, (self.to_human["id"], "human", "read", None))
        self.assertEqual(sum(1 for row in after if row[0] == self.to_human["id"]), 1)
        self.assertEqual(marked["delivery_status"], "read")
        self.assertEqual(marked["delivered_to"], "human")

    def test_t2_两边都未读时一次签收两行(self):
        self._seed()
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        before_count = len(self._deliveries())
        names = self._agent_names()
        self.store.mark("manager", both["id"], "read")
        rows = [row for row in self._deliveries() if row[0] == both["id"]]
        self.assertEqual(len(self._deliveries()), before_count)
        self.assertEqual(self._agent_names(), names)
        self.assertEqual(
            sorted((row[1], row[2]) for row in rows),
            [("human", "read"), ("manager", "read")],
        )

    def test_t2_带窗口签收也落在别名那一行(self):
        self._seed()
        before_count = len(self._deliveries())
        self.store.mark("manager", self.to_human["id"], "read", window_id="w-person")
        after = self._deliveries()
        self.assertEqual(len(after), before_count)
        human = next(row for row in after if row[0] == self.to_human["id"])
        self.assertEqual(human[1], "human")
        self.assertEqual(human[2], "read")
        self.assertFalse(any(row[0] == self.to_human["id"] and row[1] == "manager" for row in after))

    def test_t3_非人不受影响(self):
        self._seed()
        box = self._inbox("opencode")
        ids = {item["id"] for item in box["messages"]}
        self.assertIn(self.to_opencode["id"], ids)
        self.assertNotIn(self.to_human["id"], ids)
        self.assertNotIn(self.to_manager["id"], ids)
        self.assertTrue(all(item["delivered_to"] == "opencode" for item in box["messages"]))
        before = self._deliveries()
        with self.assertRaises(MailError) as ctx:
            self.store.mark("opencode", self.to_human["id"], "read")
        self.assertEqual(ctx.exception.code, "not_a_recipient")
        self.assertEqual(self._deliveries(), before)

    def test_t4_archive与read同口径(self):
        self._seed()
        before_count = len(self._deliveries())
        marked = self.store.mark("human", self.to_manager["id"], "archived")
        after = self._deliveries()
        self.assertEqual(len(after), before_count)
        row = next(item for item in after if item[0] == self.to_manager["id"])
        self.assertEqual(row, (self.to_manager["id"], "manager", "archived", None))
        self.assertFalse(any(item[0] == self.to_manager["id"] and item[1] == "human" for item in after))
        self.assertEqual(marked["delivery_status"], "archived")
        self.assertEqual(marked["delivered_to"], "manager")

        both = self.store.send("opencode", "human,manager", "再两份", "归档只动本名")
        both_before = len(self._deliveries())
        self.store.mark("human", both["id"], "archived")
        both_rows = [item for item in self._deliveries() if item[0] == both["id"]]
        self.assertEqual(len(self._deliveries()), both_before)
        self.assertEqual(
            sorted((item[1], item[2]) for item in both_rows),
            [("human", "archived"), ("manager", "unread")],
        )

    def test_t5_合并短路则T1变红(self):
        """把合并短路成本名：T1 的「必须看见寄给另一个名字的信」会红。恢复后仍绿。"""
        import infocenter.store as store_mod

        self._seed()
        real = store_mod._recipient_names

        def only_self(agent: str) -> tuple[str, ...]:
            return (store_mod.normalize_name(agent),)

        store_mod._recipient_names = only_self
        try:
            during = self._inbox("manager")
            during_ids = {item["id"] for item in during["messages"]}
            self.assertNotIn(self.to_human["id"], during_ids)
            self.assertIn(self.to_manager["id"], during_ids)
            with self.assertRaises(AssertionError):
                self._assert_t1()
        finally:
            store_mod._recipient_names = real
        self._assert_t1()

    def test_控制台往来人的两名合并(self):
        self._seed()
        sent = self.store.send("human", "cursor", "旧名发出", "从 human 发出")
        status, payload = handle(
            "GET", "/v1/agents/manager/messages", {"X-Agent": "manager"}, b"", self.store
        )
        self.assertEqual(status, 200)
        by_id = {item["id"]: item for item in payload["messages"]}
        self.assertIn(self.to_human["id"], by_id)
        self.assertIn(self.to_manager["id"], by_id)
        self.assertIn(sent["id"], by_id)
        self.assertEqual(by_id[self.to_human["id"]]["delivered_to"], "human")
        self.assertEqual(by_id[self.to_human["id"]]["direction"], "in")
        self.assertEqual(by_id[self.to_human["id"]]["my_status"], "unread")
        self.assertEqual(by_id[sent["id"]]["direction"], "out")

        status_h, payload_h = handle(
            "GET", "/v1/agents/human/messages", {"X-Agent": "human"}, b"", self.store
        )
        self.assertEqual(status_h, 200)
        self.assertEqual(
            {item["id"] for item in payload["messages"]},
            {item["id"] for item in payload_h["messages"]},
        )
        status_o, payload_o = handle(
            "GET", "/v1/agents/opencode/messages", {"X-Agent": "opencode"}, b"", self.store
        )
        self.assertEqual(status_o, 200)
        self.assertNotIn(sent["id"], {item["id"] for item in payload_o["messages"]})


if __name__ == "__main__":
    unittest.main()
