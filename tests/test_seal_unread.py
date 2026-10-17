"""办结门槛：发给封卷人自己的未签收回信不许办结；签收不受封卷拦。

口径（派单 m1a1441753151252f6）：
- seal 且 new_status=done 时，这条线程上还有发给封卷人自己的未读 ⇒ 409 unread_replies。
- 只算封卷人自己那一份（别人的副本他签不了，拿它卡人＝死锁）。中止不要求。
- 封卷线程里的信仍可签收：只标已读、不认领、不动案卷、不破只读。
- cases_list / case_view 的 unread_replies 按请求者自己算。零新增表/列。

反例（防假绿）：把 `_block_unread_replies` 短路成放行，T1 的「必须 409」断言必须红。
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.httpapi import handle
from infocenter.store import MailError, Store, beijing_time

ACCEPTANCE = "pytest tests/test_seal_unread.py"


class SealUnreadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("cursor", "Cursor", ""),
                ("kimi", "Kimi", ""),
                ("opencode", "OpenCode", "coordinator"),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _delivery(self, message_id: str, agent: str) -> sqlite3.Row:
        conn = sqlite3.connect(self.store.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT status, read_at, claimed_by FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
        finally:
            conn.close()

    def _open_with_reply_to_sealer(self, body: str = "请办") -> tuple[dict, dict]:
        """cursor 发开工信，kimi 回一封还没被 cursor 签收。

        同一库里再开一卷时正文必须不同：M6-3 对正文全同的重发返回同一封。
        """
        root = self.store.send("cursor", "kimi", "开工", body)
        case = self.store.create_case(
            agent="cursor",
            title="开工",
            owner_agent="cursor",
            thread_id=root["thread_id"],
            acceptance=ACCEPTANCE,
        )
        reply = self.store.reply("kimi", root["id"], "别封，这事还没完")
        return case, reply

    def _assert_t1_rejects(self, case_id: str) -> None:
        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case_id, agent="cursor", note="办结")
        self.assertEqual(ctx.exception.code, "unread_replies")
        self.assertEqual(ctx.exception.status, 409)

    def test_t1_未签收则拒_签收后可办结(self):
        case, reply = self._open_with_reply_to_sealer()
        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case["id"], agent="cursor", note="办结")
        self.assertEqual(ctx.exception.code, "unread_replies")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("1 封没收", ctx.exception.message)
        self.assertIn("来自 kimi", ctx.exception.message)
        self.assertIn("+0800", ctx.exception.message)
        self.assertIn("`ic inbox --status unread`", ctx.exception.message)
        self.assertIn("中止不要求", ctx.exception.message)
        left = self.store.get_case(case["id"], viewer="cursor")
        self.assertNotEqual(left["status"], "done")
        self.assertFalse(left["sealed"])
        self.assertEqual(left["unread_replies"], 1)

        marked = self.store.mark("cursor", reply["id"], "read", window_id="w-t1")
        self.assertEqual(marked["delivery_status"], "read")
        closed = self.store.close_case(case["id"], agent="cursor", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertTrue(closed["sealed"])
        self.assertEqual(closed["unread_replies"], 0)

    def test_t1_http_办结同样409(self):
        case, _reply = self._open_with_reply_to_sealer()
        body = json.dumps({"note": "办结"}).encode()
        with self.assertRaises(MailError) as ctx:
            handle("POST", f"/v1/cases/{case['id']}/close", {"X-Agent": "cursor"}, body, self.store)
        self.assertEqual(ctx.exception.code, "unread_replies")
        self.assertEqual(ctx.exception.status, 409)

    def test_t2_中止不拦(self):
        case, reply = self._open_with_reply_to_sealer()
        aborted = self.store.abort_case(case["id"], agent="cursor", note="不做了")
        self.assertEqual(aborted["status"], "aborted")
        self.assertTrue(aborted["sealed"])
        self.assertEqual(self._delivery(reply["id"], "cursor")["status"], "unread")

    def test_t3_只算封卷人自己那份(self):
        root = self.store.send("cursor", "kimi", "给你看", "别人还没签收")
        case = self.store.create_case(
            agent="cursor",
            title="给你看",
            owner_agent="cursor",
            thread_id=root["thread_id"],
            acceptance=ACCEPTANCE,
        )
        self.assertEqual(self._delivery(root["id"], "kimi")["status"], "unread")
        self.assertIsNone(self._delivery(root["id"], "cursor"))
        closed = self.store.close_case(case["id"], agent="cursor", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertTrue(closed["sealed"])
        self.assertEqual(self._delivery(root["id"], "kimi")["status"], "unread")

    def test_t4_封卷后仍能签收且不认领不动案卷(self):
        root = self.store.send("cursor", "kimi", "开工", "请办")
        case = self.store.create_case(
            agent="cursor",
            title="开工",
            owner_agent="cursor",
            thread_id=root["thread_id"],
            acceptance=ACCEPTANCE,
        )
        closed = self.store.close_case(case["id"], agent="cursor", note="办结")
        reply = self.store.reply("kimi", root["id"], "晚到的回执")
        before_nodes = closed["node_count"]
        before_updated = closed["updated_at"]
        marked = self.store.mark(
            "cursor", reply["id"], "read", window_id="w-seal-read", force=True
        )
        self.assertEqual(marked["delivery_status"], "read")
        self.assertIsNone(marked["claimed_by"])
        row = self._delivery(reply["id"], "cursor")
        self.assertEqual(row["status"], "read")
        self.assertIsNone(row["claimed_by"])
        after = self.store.get_case(case["id"], viewer="cursor")
        self.assertEqual(after["status"], "done")
        self.assertTrue(after["sealed"])
        self.assertEqual(after["node_count"], before_nodes)
        self.assertEqual(after["updated_at"], before_updated)
        self.assertIsNone(after["owner_window"])
        self.assertEqual(after["unread_replies"], 0)
        with self.assertRaises(MailError) as ctx:
            self.store.append_case_node(case["id"], agent="cursor", note="还想写一行")
        self.assertEqual(ctx.exception.code, "case_sealed")

    def test_t5_unread_replies按请求者自己算(self):
        sent = self.store.send("opencode", ["cursor", "kimi"], "通报", "请看")
        case = self.store.create_case(
            agent="opencode",
            title="通报",
            owner_agent="opencode",
            thread_id=sent["thread_id"],
            acceptance=ACCEPTANCE,
        )
        self.store.mark("cursor", sent["id"], "read")
        seen_by_cursor = self.store.get_case(case["id"], viewer="cursor")
        seen_by_kimi = self.store.get_case(case["id"], viewer="kimi")
        self.assertEqual(seen_by_cursor["unread_replies"], 0)
        self.assertEqual(seen_by_kimi["unread_replies"], 1)
        self.assertNotEqual(seen_by_cursor["unread_replies"], seen_by_kimi["unread_replies"])
        self.assertIsInstance(seen_by_cursor["unread_replies"], int)
        self.assertIsInstance(seen_by_kimi["unread_replies"], int)

        listed_cursor = next(row for row in self.store.list_cases(viewer="cursor") if row["id"] == case["id"])
        listed_kimi = next(row for row in self.store.list_cases(viewer="kimi") if row["id"] == case["id"])
        self.assertEqual(listed_cursor["unread_replies"], 0)
        self.assertEqual(listed_kimi["unread_replies"], 1)

        status, detail = handle(
            "GET", f"/v1/cases/{case['id']}", {"X-Agent": "kimi"}, b"", self.store
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["unread_replies"], 1)
        status, listing = handle("GET", "/v1/cases", {"X-Agent": "cursor"}, b"", self.store)
        self.assertEqual(status, 200)
        item = next(row for row in listing["cases"] if row["id"] == case["id"])
        self.assertEqual(item["unread_replies"], 0)

        conn = sqlite3.connect(self.store.path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertNotIn("unread_replies", cols)
        self.assertNotIn("unread_replies", tables)

    def test_反例_门槛短路则T1变红(self):
        """把门槛短路成放行：T1 的「close 必须 409」会红。恢复后新案卷仍被拦。"""
        import infocenter.store as store_mod

        case, _reply = self._open_with_reply_to_sealer()
        real = store_mod._block_unread_replies
        store_mod._block_unread_replies = lambda *_args, **_kwargs: None
        try:
            with self.assertRaises(AssertionError):
                self._assert_t1_rejects(case["id"])
        finally:
            store_mod._block_unread_replies = real
        sealed = self.store.get_case(case["id"])
        self.assertEqual(sealed["status"], "done", "短路后办结应已通过，否则 T1 不会红")
        fresh, _reply = self._open_with_reply_to_sealer(body="请办·第二封")
        self._assert_t1_rejects(fresh["id"])


class SealUnreadAliasTest(unittest.TestCase):
    """办结门槛与签收同一口径：先本名，没有才认另一个别名。

    口径（派单 m1a145812bb5a68ea6）：
    - 只有别名那一行未读 ⇒ 请求者 unread_replies=1，未签收办结 409；签收落在那一行之后可办结。
    - 把别名回退短路成空 ⇒ 上面这条必须红，且办结会放行。
    - 同一封信两行都在：签收只动本名，之后能办结（另一行仍未读也不 409）。
    - 别人的未读仍不卡人。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("cursor", "Cursor", ""),
                ("kimi", "Kimi", ""),
                ("opencode", "OpenCode", "coordinator"),
                ("manager", "人", "manager"),
                ("human", "人", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _delivery(self, message_id: str, agent: str) -> sqlite3.Row:
        conn = sqlite3.connect(self.store.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT status, read_at, claimed_by, to_agent FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
        finally:
            conn.close()

    def _open_alias_only(self, to_agent: str = "human", body: str | None = None) -> tuple[dict, dict]:
        """寄给一个名字、另一个名字名下没有行。立案人是发信人（线程当事人）。

        同一库里再开一卷时正文必须不同：M6-3 对正文全同的重发返回同一封。
        """
        root = self.store.send("opencode", to_agent, "探针", body or f"寄给 {to_agent}")
        case = self.store.create_case(
            agent="opencode",
            title="探针",
            thread_id=root["thread_id"],
            acceptance=ACCEPTANCE,
        )
        return case, root

    def _assert_alias_blocks(self, case_id: str, agent: str = "manager") -> None:
        seen = self.store.get_case(case_id, viewer=agent)
        self.assertEqual(seen["unread_replies"], 1)
        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case_id, agent=agent, note="办结")
        self.assertEqual(ctx.exception.code, "unread_replies")
        self.assertEqual(ctx.exception.status, 409)
        left = self.store.get_case(case_id, viewer=agent)
        self.assertNotEqual(left["status"], "done")
        self.assertFalse(left["sealed"])

    def test_只有别名那一行未读则拒_签收落在那一行后可办结(self):
        case, root = self._open_alias_only("human")
        self.assertIsNone(self._delivery(root["id"], "manager"))
        self.assertEqual(self._delivery(root["id"], "human")["status"], "unread")
        seen = self.store.get_case(case["id"], viewer="manager")
        self.assertEqual(seen["unread_replies"], 1)
        listed = next(row for row in self.store.list_cases(viewer="manager") if row["id"] == case["id"])
        self.assertEqual(listed["unread_replies"], 1)
        status, detail = handle(
            "GET", f"/v1/cases/{case['id']}", {"X-Agent": "manager"}, b"", self.store
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["unread_replies"], 1)

        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case["id"], agent="manager", note="办结")
        self.assertEqual(ctx.exception.code, "unread_replies")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("1 封没收", ctx.exception.message)
        self.assertIn("来自 opencode", ctx.exception.message)
        body = json.dumps({"note": "办结"}).encode()
        with self.assertRaises(MailError) as ctx:
            handle("POST", f"/v1/cases/{case['id']}/close", {"X-Agent": "manager"}, body, self.store)
        self.assertEqual(ctx.exception.code, "unread_replies")

        marked = self.store.mark("manager", root["id"], "read")
        self.assertEqual(marked["delivery_status"], "read")
        self.assertEqual(marked["delivered_to"], "human")
        self.assertEqual(self._delivery(root["id"], "human")["status"], "read")
        self.assertIsNone(self._delivery(root["id"], "manager"))
        closed = self.store.close_case(case["id"], agent="manager", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertTrue(closed["sealed"])
        self.assertEqual(closed["unread_replies"], 0)

    def test_反向_只有现名那一行时旧名同样被拦(self):
        case, root = self._open_alias_only("manager")
        self._assert_alias_blocks(case["id"], agent="human")
        marked = self.store.mark("human", root["id"], "read")
        self.assertEqual(marked["delivered_to"], "manager")
        self.assertEqual(self._delivery(root["id"], "manager")["status"], "read")
        self.assertIsNone(self._delivery(root["id"], "human"))
        closed = self.store.close_case(case["id"], agent="human", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertEqual(closed["unread_replies"], 0)

    def test_反例_别名回退短路则判据变红且办结放行(self):
        """把别名回退短路成空：别名那一行不再算未签收，办结放行，判据变红。恢复后新案卷仍被拦。"""
        import infocenter.store as store_mod

        case, root = self._open_alias_only("human")
        real = store_mod._own_delivery_fallback_names
        store_mod._own_delivery_fallback_names = lambda _agent: ()
        try:
            seen = self.store.get_case(case["id"], viewer="manager")
            self.assertEqual(seen["unread_replies"], 0)
            closed = self.store.close_case(case["id"], agent="manager", note="办结")
            self.assertEqual(closed["status"], "done")
            with self.assertRaises(AssertionError):
                self._assert_alias_blocks(case["id"])
        finally:
            store_mod._own_delivery_fallback_names = real
        self.assertEqual(self.store.get_case(case["id"])["status"], "done")
        self.assertEqual(self._delivery(root["id"], "human")["status"], "unread")
        fresh, _root = self._open_alias_only("human", body="寄给 human · 第二封")
        self._assert_alias_blocks(fresh["id"])

    def test_两行都未读时签一次后另一行不再卡住办结(self):
        both = self.store.send("opencode", "human,manager", "两份", "各一份")
        case = self.store.create_case(
            agent="opencode",
            title="两份",
            thread_id=both["thread_id"],
            acceptance=ACCEPTANCE,
        )
        self.assertEqual(self.store.get_case(case["id"], viewer="manager")["unread_replies"], 1)
        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case["id"], agent="manager", note="办结")
        self.assertEqual(ctx.exception.code, "unread_replies")

        self.store.mark("manager", both["id"], "read")
        self.assertEqual(self._delivery(both["id"], "manager")["status"], "read")
        self.assertEqual(self._delivery(both["id"], "human")["status"], "read")
        self.assertEqual(self.store.get_case(case["id"], viewer="manager")["unread_replies"], 0)
        self.assertEqual(self.store.get_case(case["id"], viewer="human")["unread_replies"], 0)

        closed = self.store.close_case(case["id"], agent="manager", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertTrue(closed["sealed"])
        self.assertEqual(self._delivery(both["id"], "human")["status"], "read")

    def test_别人的未读仍不卡人(self):
        root = self.store.send("manager", "kimi", "给你看", "别人还没签收")
        case = self.store.create_case(
            agent="manager",
            title="给你看",
            thread_id=root["thread_id"],
            acceptance=ACCEPTANCE,
        )
        self.assertEqual(self._delivery(root["id"], "kimi")["status"], "unread")
        self.assertEqual(self.store.get_case(case["id"], viewer="manager")["unread_replies"], 0)
        closed = self.store.close_case(case["id"], agent="manager", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertEqual(self._delivery(root["id"], "kimi")["status"], "unread")

    def test_看板下发最早时间和来自谁_零封则为空_不增列(self):
        case, root = self._open_alias_only("human")
        seen = self.store.get_case(case["id"], viewer="manager")
        self.assertEqual(seen["unread_replies"], 1)
        self.assertEqual(seen["unread_replies_from"], "opencode")
        self.assertEqual(seen["unread_replies_earliest"], root["created_at"])
        self.assertEqual(seen["unread_replies_earliest_beijing"], beijing_time(root["created_at"]))
        self.assertTrue(seen["unread_replies_earliest_beijing"].endswith("+0800"))

        first = self.store.send("opencode", ["manager", "kimi"], "开工", "先说")
        second = self.store.reply("kimi", first["id"], "后说", to="manager")
        threaded = self.store.create_case(
            agent="opencode",
            title="开工",
            thread_id=first["thread_id"],
            acceptance=ACCEPTANCE,
        )
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                ("2026-10-16T01:00:00Z", first["id"]),
            )
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                ("2026-10-16T02:00:00Z", second["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        seen = self.store.get_case(threaded["id"], viewer="manager")
        self.assertEqual(seen["unread_replies"], 2)
        self.assertEqual(seen["unread_replies_from"], "opencode")
        self.assertEqual(seen["unread_replies_earliest"], "2026-10-16T01:00:00Z")
        self.assertEqual(seen["unread_replies_earliest_beijing"], "2026-10-16 09:00:00 +0800")
        listed = next(row for row in self.store.list_cases(viewer="manager") if row["id"] == threaded["id"])
        self.assertEqual(listed["unread_replies_from"], "opencode")
        self.assertEqual(listed["unread_replies_earliest_beijing"], "2026-10-16 09:00:00 +0800")
        status, detail = handle(
            "GET", f"/v1/cases/{threaded['id']}", {"X-Agent": "manager"}, b"", self.store
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["unread_replies_from"], "opencode")
        self.assertEqual(detail["unread_replies_earliest"], "2026-10-16T01:00:00Z")

        self.store.mark("manager", first["id"], "read")
        self.store.mark("manager", second["id"], "read")
        done = self.store.get_case(threaded["id"], viewer="manager")
        self.assertEqual(done["unread_replies"], 0)
        self.assertEqual(done["unread_replies_earliest"], "")
        self.assertEqual(done["unread_replies_earliest_beijing"], "")
        self.assertEqual(done["unread_replies_from"], "")

        conn = sqlite3.connect(self.store.path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
        finally:
            conn.close()
        self.assertNotIn("unread_replies", cols)
        self.assertNotIn("unread_replies_earliest", cols)
        self.assertNotIn("unread_replies_earliest_beijing", cols)
        self.assertNotIn("unread_replies_from", cols)


if __name__ == "__main__":
    unittest.main()
