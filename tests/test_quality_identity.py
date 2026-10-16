"""人的两个名字：manager 是现名，human 是旧名。

口径（派单 m1a143c880465a8a6c）：权限和持有者比较等价，落库不归一。
旧行仍写 human，审计必须还能过。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import config
from infocenter.httpapi import handle
from infocenter.store import (
    HUMAN,
    HUMAN_NAMES,
    MailError,
    Store,
    _must_human,
    is_person,
)

WS = "/tmp/proj-identity"


class IdentityQualityTest(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("human", "人", ""),
                ("manager", "人", "manager"),
                ("cursor", "Cursor", ""),
                ("kimi", "Kimi", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name: str) -> Path:
        path = self.home / name
        path.write_text("x\n", encoding="utf-8")
        return path

    def test_is_person_two_names_and_symbols_live_in_store(self):
        self.assertEqual(HUMAN, "manager")
        self.assertEqual(set(HUMAN_NAMES), {"manager", "human"})
        self.assertTrue(is_person("manager"))
        self.assertTrue(is_person("human"))
        self.assertTrue(is_person(" Human "))
        self.assertFalse(is_person("cursor"))
        self.assertFalse(is_person(""))
        self.assertIs(config.HUMAN, HUMAN)
        self.assertIs(config.HUMAN_NAMES, HUMAN_NAMES)
        self.assertIs(config.is_person, is_person)

    def test_must_human_and_coordinator_accept_manager(self):
        _must_human("manager")
        _must_human("human")
        with self.assertRaises(MailError) as ctx:
            _must_human("cursor")
        self.assertEqual(ctx.exception.code, "human_only")
        self.assertEqual(ctx.exception.status, 403)

        cid = self.store.create_case(agent="cursor", title="卷", workspace=WS)["id"]
        got = self.store.case_milestone_add(cid, agent="manager", name="人拍的")
        self.assertEqual([m["name"] for m in got["milestones"]], ["人拍的"])

    def test_http_remove_by_manager_is_a_business_miss(self):
        status, payload = handle(
            "DELETE", "/v1/agents/zzz-not-exist", {"X-Agent": "manager"}, b"{}", self.store
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["removed"])
        self.assertEqual(payload["name"], "zzz-not-exist")

        status, payload = handle(
            "DELETE", "/v1/agents/zzz-not-exist", {"X-Agent": "human"}, b"{}", self.store
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["removed"])

        with self.assertRaises(MailError) as ctx:
            handle(
                "DELETE", "/v1/agents/zzz-not-exist", {"X-Agent": "cursor"}, b"{}", self.store
            )
        self.assertEqual(ctx.exception.code, "not_you")
        self.assertNotEqual(ctx.exception.code, "human_only")

    def test_alias_releases_the_other_names_lock_without_rewriting_it(self):
        path = str(self._file("a.py"))
        self.store.lock_acquire("manager", [path])
        released = self.store.lock_release("human", [path])
        self.assertEqual(released["released_count"], 1)
        self.assertEqual(self.store.lock_list()["count"], 0)

        self.store.lock_acquire("human", [path])
        self.assertEqual(self.store.lock_list()["locks"][0]["agent"], "human")
        self._file("a.py").write_text("y\n", encoding="utf-8")
        self.store.lock_acquire("manager", [path])
        self.assertEqual(
            self.store.lock_list()["locks"][0]["agent"],
            "human",
            "别名续期不得把落库名字改成 manager",
        )
        released = self.store.lock_release("manager", [path])
        self.assertEqual(released["released_count"], 1)
        self.assertEqual(self.store.lock_list()["count"], 0)

    def test_guard_and_merge_treat_the_alias_as_the_holder(self):
        path = str(self._file("b.py"))
        self.store.lock_acquire("human", [path])
        mine = self.store.guard_changes("manager", [path])
        self.assertTrue(mine["ok"], mine)
        self.assertEqual(mine["locked_by_others"], [])
        foreign = self.store.guard_changes("kimi", [path])
        self.assertFalse(foreign["ok"])
        self.assertEqual(foreign["locked_by_others"][0]["holder"], "human")

        self.store.queue_change("kimi", path, note="改一行")
        merged = self.store.merge_queue("manager", path, note="按 kimi 的提议收了")
        self.assertEqual(merged["by"], "manager")
        self.assertGreaterEqual(len(merged["merged"]), 1)

    def test_task_and_case_owner_alias(self):
        task = self.store.task_add(
            "human", "活", workspace=WS, owner="human", acceptance="做完",
        )
        started = self.store.task_start("manager", task["id"])
        self.assertEqual(started["status"], "doing")
        self.assertEqual(started["owner"], "human")

        case = self.store.create_case(
            agent="human", title="案", workspace=WS, owner_window="w-person",
        )
        self.assertEqual(case["owner_agent"], "human")
        updated = self.store.case_acceptance_set(case["id"], "manager", "怎样算完")
        self.assertEqual(updated["acceptance"], "怎样算完")

    def test_old_human_row_still_audits(self):
        self.store.add_project(WS, coordinator="cursor", actor="human")
        audit = self.store.audit_events()
        self.assertTrue(audit["ok"], audit)
        con = sqlite3.connect(self.store.path)
        try:
            agent = con.execute(
                "SELECT agent FROM task_events WHERE action = 'project_coordinator'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(agent, "human")

    def test_board_shows_human_as_manager_and_keeps_the_stored_name(self):
        html = (ROOT / "infocenter" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name === "human" ? "manager"', html)
        self.assertIn("manager · 干预中枢", html)
        self.assertIn('"X-Agent": "human"', html)
        self.assertIn('h === "human"', html)


if __name__ == "__main__":
    unittest.main()
