"""案卷的「怎样算完」（`ic case <id> --acceptance "…"`）。

human 定的毛病（2026-10-15）：任务一多，**没人写清"做成什么算做成"**，
"做完没有"就靠事后吵。任务那一层本来就有 `--acceptance`，案卷这一层现在补上。

规矩：
- 谁能写：该案卷的 owner，或 coordinator（自己的活自己写清标准）；
- **空着不拦写入**：看板把它标出来（"⚠ 没写怎样算完"）；
- **办结才拦**：没写 acceptance 就 close ⇒ 409 `no_acceptance`；中止不要求；
- 封了卷就不能再改；写一次留一条节点（只增痕迹）。
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
from infocenter.store import CASE_ACCEPTANCE_MAX, MailError, Store


class CaseAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("pi", "Pi", ""), ("kimi", "Kimi", ""), ("opencode", "OpenCode", "coordinator")],
        )
        self.case = self.store.create_case(
            agent="pi", title="把锁换成一把钥匙", owner_agent="pi"
        )
        self.cid = self.case["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_立案时就能写怎样算完(self):
        case = self.store.create_case(
            agent="pi", title="带验收立案", owner_agent="pi", acceptance="全量测试绿，且没有裸改动落地"
        )
        self.assertEqual(case["acceptance"], "全量测试绿，且没有裸改动落地")
        self.assertIn("acceptance", self.store.get_case(case["id"]))

    def test_没写就是空串_不拦(self):
        self.assertEqual(self.case["acceptance"], "")
        self.assertEqual(self.store.get_case(self.cid)["acceptance"], "")

    def test_owner_能写(self):
        out = self.store.case_acceptance_set(self.cid, "pi", "三处测试都补了、全量绿")
        self.assertEqual(out["acceptance"], "三处测试都补了、全量绿")
        # 留痕：写一次留一条节点
        actions = [node["action"] for node in out["nodes"]]
        self.assertIn("acceptance", actions)

    def test_coordinator_能写(self):
        out = self.store.case_acceptance_set(self.cid, "opencode", "由 coordinator 定的收尾判据")
        self.assertEqual(out["acceptance"], "由 coordinator 定的收尾判据")

    def test_别人不能写(self):
        with self.assertRaises(MailError) as ctx:
            self.store.case_acceptance_set(self.cid, "kimi", "我来定标准")
        self.assertEqual(ctx.exception.code, "not_yours")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.store.get_case(self.cid)["acceptance"], "")

    def test_空串等于撤掉(self):
        self.store.case_acceptance_set(self.cid, "pi", "先这么算")
        out = self.store.case_acceptance_set(self.cid, "pi", "")
        self.assertEqual(out["acceptance"], "")

    def test_封卷后不能再改(self):
        self.store.case_acceptance_set(self.cid, "pi", "先写清怎样算完")
        self.store.close_case(self.cid, agent="human", note="办结")
        with self.assertRaises(MailError) as ctx:
            self.store.case_acceptance_set(self.cid, "pi", "补一句")
        self.assertEqual(ctx.exception.code, "case_sealed")
        self.assertEqual(ctx.exception.status, 409)

    def test_太长就报400_只说理不硬切(self):
        """跟 `_task_text` 一个口径：太长**报错**（让写进文件、这里留引用），不偷偷截半句。"""
        with self.assertRaises(MailError) as ctx:
            self.store.case_acceptance_set(self.cid, "pi", "长" * (CASE_ACCEPTANCE_MAX + 50))
        self.assertEqual(ctx.exception.code, "task_text_too_long")
        self.assertEqual(ctx.exception.status, 400)

    def test_没有这个案卷就404(self):
        with self.assertRaises(MailError) as ctx:
            self.store.case_acceptance_set("c_not_exist", "pi", "写点什么")
        self.assertEqual(ctx.exception.status, 404)

    def test_老库没有这一列也能开(self):
        """老 mailbox.db 的 cases 表没有 acceptance：开场要自己补上，不能炸。"""
        old_home = Path(self.tmp.name) / "old"
        old_home.mkdir()
        db = old_home / "mailbox.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE cases (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                workspace TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                owner_agent TEXT,
                owner_window TEXT,
                waiting_for TEXT,
                sealed INTEGER NOT NULL DEFAULT 0,
                wbs_path TEXT NOT NULL DEFAULT '',
                milestones TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()
        store = Store(db, seeds=[("pi", "Pi", "")])
        case = store.create_case(agent="pi", title="老库立案", owner_agent="pi", acceptance="老库也要能写")
        self.assertEqual(case["acceptance"], "老库也要能写")


class CaseAcceptanceRouteTest(unittest.TestCase):
    """/v1 那一条：命令行/别的客户端都走它。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("pi", "Pi", ""), ("opencode", "OpenCode", "coordinator")],
        )
        self.cid = self.store.create_case(agent="pi", title="路由测试", owner_agent="pi")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_post_能写_看板读得到(self):
        body = json.dumps({"acceptance": "谁都能读、只有 owner/coordinator 能写"}).encode()
        status, payload = handle(
            "POST", f"/v1/cases/{self.cid}/acceptance", {"X-Agent": "pi"}, body, self.store
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["acceptance"], "谁都能读、只有 owner/coordinator 能写")

        status, listed = handle("GET", "/v1/cases", {"X-Agent": "opencode"}, b"", self.store)
        self.assertEqual(status, 200)
        row = [c for c in listed["cases"] if c["id"] == self.cid][0]
        self.assertEqual(row["acceptance"], "谁都能读、只有 owner/coordinator 能写")

    def test_缺字段就400(self):
        with self.assertRaises(MailError) as ctx:
            handle("POST", f"/v1/cases/{self.cid}/acceptance", {"X-Agent": "pi"}, b"{}", self.store)
        self.assertEqual(ctx.exception.status, 400)

    def test_立案时带上(self):
        body = json.dumps({"title": "带验收立案", "acceptance": "做成这样就算成"}).encode()
        status, payload = handle("POST", "/v1/cases", {"X-Agent": "pi"}, body, self.store)
        self.assertEqual(status, 201)
        self.assertEqual(payload["acceptance"], "做成这样就算成")


if __name__ == "__main__":
    unittest.main()
