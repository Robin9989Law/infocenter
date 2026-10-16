"""看板减噪：默认只折「已封卷」的（归档），没实质内容的打 `[信]` 标但**不藏**。

为什么：**任何 thread 都能当案卷**（案卷 = 一个 thread 的办事追踪），所以每封派单/回信/报告都长成一"卷"、
还各自挂着「⚠ 没写怎样算完」——实测 100 条里 **52 条是已封卷的历史**，混在一起才显得吵。
（我先前把这条读成「92% 是信」，那是把历史也算进噪声里了，错；真数是 52 封卷 + 48 未封卷、其中 45 条是信。）

口径（**不改数据，只改看法**）：
- 服务端：`_case_view` 出派生标记 `substantive`（验收 / 里程碑 / WBS 三选一）——**只是标记，不是过滤条件**
  （实测那堆里有真活：派单线程可以是"在办"、有 5 封信、三样全无）。
- `ic cases`：默认只列**未封卷**、给没实质内容的打 `[信]` 标、已封卷的折成一行；`--all` 看全（`--json` 不动）。
- 控制台：案卷栏同一口径（折已封卷 + 给信打标签）。
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.store import Store

PAYLOAD = {
    "cases": [
        {"id": "m1", "title": "真案卷", "substantive": True, "owner_agent": "opencode",
         "wbs_path": "维护/看板", "acceptance": "验收：列表只剩有实质内容的", "status": "open",
         "status_label": "在办", "milestones_total": 2, "milestones_done": 1},
        {"id": "m2", "title": "一封信（派单）", "substantive": False, "owner_agent": "",
         "status": "open", "status_label": "在办"},
        {"id": "m3", "title": "一封信（回信）", "substantive": False, "owner_agent": "",
         "status": "open", "status_label": "在办"},
    ],
    "count": 3,
}


class SubstanceFlagTest(unittest.TestCase):
    """服务端那一半：`substantive` 由「验收 / 里程碑 / WBS」三选一决定。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            Path(self.tmp.name) / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("kimi", "Kimi", "")],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _by_id(self):
        return {c["id"]: c for c in self.store.list_cases(limit=50)}

    def test_写了验收就算有实质内容(self):
        case = self.store.create_case(agent="opencode", title="有验收", acceptance="做成什么样算做成")
        self.assertTrue(self._by_id()[case["id"]]["substantive"])

    def test_加了里程碑也算(self):
        case = self.store.create_case(agent="opencode", title="只有里程碑")
        self.store.case_milestone_add(case["id"], agent="opencode", name="第一格")
        self.assertTrue(self._by_id()[case["id"]]["substantive"])

    def test_打了_wbs_也算(self):
        case = self.store.create_case(agent="opencode", title="只有 WBS", wbs_path="维护/看板")
        self.assertTrue(self._by_id()[case["id"]]["substantive"])

    def test_信立案之后仍然不算(self):
        # "信线程"就是这么来的：一封信被立案（谁签收/谁挂卷），但没有验收/里程碑/WBS。
        sent = self.store.send(sender="opencode", to="kimi", subject="派个单", body="正文")
        case = self.store.create_case(agent="opencode", thread_id=sent["thread_id"], title="派个单")
        self.assertFalse(self._by_id()[case["id"]]["substantive"])

    def test_光写节点也不算(self):
        sent = self.store.send(sender="opencode", to="kimi", subject="问", body="在吗")
        case = self.store.create_case(agent="opencode", thread_id=sent["thread_id"], title="问")
        self.store.append_case_node(case["id"], agent="kimi", note="收到")
        self.assertFalse(self._by_id()[case["id"]]["substantive"])


class CasesFoldTest(unittest.TestCase):
    """命令行那一半：默认折叠，`--all` 看全。"""

    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"

    def _run(self, *extra):
        stub = mock.Mock(return_value=PAYLOAD)
        buf = io.StringIO()
        with mock.patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--as", "human", "cases", *extra])
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_默认只折已封卷的_活的都列出来(self):
        # 判据被真数据修正过：折"已封卷"的（归档），**不折没实质内容的**——实测那堆里有真活
        # （派单线程可以是"在办"、有 5 封信），折了就是把活藏起来。
        out = self._run()
        self.assertIn("案卷 3 份", out)          # m1（有实质内容）+ m2/m3（信，但都没封卷 ⇒ 都列）
        self.assertIn("[信]", out)               # 信要看得见、认得出来
        self.assertIn("一封信（派单）", out)
        self.assertNotIn("已封卷", out)          # 这批里没有已封卷的

    def test_已封卷默认折起来_all_看全(self):
        payload_cases = PAYLOAD["cases"] + [
            {"id": "m4", "title": "封过的", "substantive": True, "sealed": True,
             "status": "done", "status_label": "办结"},
        ]
        folders = dict(PAYLOAD, cases=payload_cases, count=4)
        stub = mock.Mock(return_value=folders)
        buf = io.StringIO()
        with mock.patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf):
                cli.main(["--as", "human", "cases"])
        self.assertIn("这批 4 条里另有 1 条已封卷", buf.getvalue())
        self.assertNotIn("封过的", buf.getvalue())

        buf2 = io.StringIO()
        with mock.patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf2):
                cli.main(["--as", "human", "cases", "--all"])
        self.assertIn("封过的", buf2.getvalue())

    def test_json_不动(self):
        stub = mock.Mock(return_value=PAYLOAD)
        buf = io.StringIO()
        with mock.patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf):
                cli.main(["--as", "human", "cases", "--json"])
        import json

        self.assertEqual(len(json.loads(buf.getvalue())["cases"]), 3)


if __name__ == "__main__":
    unittest.main()
