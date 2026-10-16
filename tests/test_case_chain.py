"""案卷状态机进既有哈希链（不新增列）。

口径：链只钉状态机，不钉属性。
要进链：立案 / 认领 / 接手 / 转办 / 暂停 / 恢复 / 中止 / 办结 / 重开。
不上链：acceptance / milestone / wbs / 普通节点 / 阅读即认领。
`task_id=""` 与 `project_coordinator` / `propose` 同例，任务重放不看这些行。
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

from infocenter.store import Store


class CaseChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
                ("grok", "Grok", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, action: str | None = None, case_id: str | None = None) -> list[dict]:
        con = sqlite3.connect(self.store.path)
        con.row_factory = sqlite3.Row
        try:
            sql = "SELECT * FROM task_events WHERE action LIKE 'case_%'"
            params: list[str] = []
            if action is not None:
                sql += " AND action = ?"
                params.append(action)
            sql += " ORDER BY seq"
            rows = [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()
        if case_id is None:
            return rows
        return [row for row in rows if json.loads(row["detail"])["case_id"] == case_id]

    def test_办结进链_改节点不破链(self):
        """开卷 → 办结：`ic audit` 走的那条链里有 case_close；改 case_nodes.note 不破链。"""
        case = self.store.create_case(agent="cursor", title="办事状态机", acceptance="链上有 case_close")
        cid = case["id"]
        self.store.close_case(cid, agent="cursor", note="办结（结案封档）")

        closed = self._rows("case_close", cid)
        self.assertEqual(len(closed), 1)
        row = closed[0]
        self.assertEqual(row["task_id"], "")
        self.assertEqual(row["agent"], "cursor")
        self.assertEqual(row["from_status"], "pending")
        self.assertEqual(row["to_status"], "done")
        detail = json.loads(row["detail"])
        self.assertEqual(detail["case_id"], cid)
        self.assertIn("办结", detail["note"])

        audit = self.store.audit_events()
        self.assertTrue(audit["ok"], audit)
        self.assertGreaterEqual(audit["events"], 2)

        con = sqlite3.connect(self.store.path)
        con.execute(
            "UPDATE case_nodes SET note = ? WHERE case_id = ?",
            ("被改过的流水", cid),
        )
        con.commit()
        con.close()
        again = self.store.audit_events()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["tip_hash"], audit["tip_hash"])
        self.assertEqual(self._rows("case_close", cid)[0]["hash"], row["hash"])

    def test_改链上的办结行会破链(self):
        case = self.store.create_case(agent="cursor", title="钉住封卷", acceptance="改链上的办结行会破链")
        self.store.close_case(case["id"], agent="cursor", note="办结")
        self.assertTrue(self.store.audit_events()["ok"])
        con = sqlite3.connect(self.store.path)
        con.execute(
            "UPDATE task_events SET detail = ? WHERE action = 'case_close'",
            ('{"case_id":"x"}',),
        )
        con.commit()
        con.close()
        broken = self.store.audit_events()
        self.assertFalse(broken["ok"])
        self.assertTrue(broken["chain_broken"])

    def test_状态机全套进链_属性与流水不上链(self):
        case = self.store.create_case(agent="cursor", title="全套")
        cid = case["id"]
        self.store.claim_case(cid, "cursor", "w-1")
        before = len(self._rows(case_id=cid))
        self.store.claim_case(cid, "cursor", "w-1")  # 同一窗口再认领：没有新的状态跃迁
        self.assertEqual(len(self._rows(case_id=cid)), before)

        self.store.append_case_node(cid, "cursor", "普通流水", status="waiting", waiting_for="grok")
        self.store.case_acceptance_set(cid, "cursor", "链上能查到办结")
        self.store.case_wbs_set(cid, "human", "状态机")
        self.store.case_milestone_add(cid, "human", "钉住封卷")
        self.assertEqual(len(self._rows(case_id=cid)), before)

        self.store.transfer_case(cid, "cursor", to_window="w-2", to_agent="grok", window_id="w-1")
        self.store.pause_case(cid, agent="cursor", window_id="w-2")
        self.store.resume_case(cid, agent="grok", window_id="w-2")
        self.store.close_case(cid, agent="human", note="办结")
        self.store.reopen_case(cid, agent="human", window_id="w-2")
        self.store.abort_case(cid, agent="human", note="中止")

        actions = [row["action"] for row in self._rows(case_id=cid)]
        self.assertEqual(
            actions,
            [
                "case_create", "case_claim", "case_transfer", "case_pause",
                "case_resume", "case_close", "case_reopen", "case_abort",
            ],
        )
        self.assertTrue(all(row["task_id"] == "" for row in self._rows(case_id=cid)))
        self.assertTrue(self.store.audit_events()["ok"])

        taken = self.store.create_case(
            agent="cursor", title="接手", owner_agent="cursor", owner_window="w-a",
        )
        self.store.claim_case(taken["id"], "grok", "w-b", force=True)
        takeover = self._rows("case_takeover", taken["id"])
        self.assertEqual(len(takeover), 1)
        self.assertEqual(takeover[0]["agent"], "grok")
        self.assertEqual((takeover[0]["from_status"], takeover[0]["to_status"]), ("open", "open"))

    def test_阅读即认领不上链(self):
        sent = self.store.send(sender="opencode", to="cursor", subject="看一眼", body="正文")
        before = self._rows()
        self.store.mark("cursor", sent["id"], "read", window_id="w-read")
        self.assertEqual(self._rows(), before)
        view = self.store.get_case(sent["thread_id"])
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["owner_window"], "w-read")
        self.assertTrue(self.store.audit_events()["ok"])

    def test_对线程暂停先立案再暂停_两步都进链(self):
        sent = self.store.send(sender="opencode", to="cursor", subject="先暂停", body="正文")
        self.store.pause_case(sent["thread_id"], agent="human", note="先停")
        rows = self._rows(case_id=sent["thread_id"])
        self.assertEqual([row["action"] for row in rows], ["case_create", "case_pause"])
        self.assertEqual(rows[0]["to_status"], "pending")
        self.assertEqual((rows[1]["from_status"], rows[1]["to_status"]), ("pending", "paused"))
        self.assertTrue(self.store.audit_events()["ok"])

    def test_写节点直接封卷也进链(self):
        case = self.store.create_case(agent="cursor", title="直接封", acceptance="写节点直接封卷")
        self.store.append_case_node(case["id"], "cursor", "收工", status="done")
        rows = self._rows("case_close", case["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to_status"], "done")
        self.assertTrue(self.store.audit_events()["ok"])

    def test_案卷链不干扰任务重放(self):
        task = self.store.task_add(
            "human", "旁边的任务", owner="cursor", workspace=str(self.home),
        )
        self.store.task_start("cursor", task["id"])
        case = self.store.create_case(
            agent="cursor", title="旁边的案卷", workspace=str(self.home), acceptance="不干扰任务重放"
        )
        self.store.close_case(case["id"], agent="cursor", note="办结")
        whole = self.store.audit_events()
        one = self.store.audit_events(task["id"])
        self.assertTrue(whole["ok"], whole)
        self.assertTrue(one["ok"], one)
        self.assertEqual(one["mismatches"], [])


if __name__ == "__main__":
    unittest.main()
