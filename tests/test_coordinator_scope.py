"""coordinator 的作用域：**每个项目由人指定，没指定就拒**（human 2026-10-16 拍）。

背景：以前 `coordinator` 是**一个全局角色**（config/库里 `role=coordinator`），一个身份管所有项目。
现在分两层：

| 层 | 谁 | 管什么 |
| --- | --- | --- |
| 全局 | **manager**（旧名 coordinator） | 名册、hub 级；**不再自动拥有项目里的权** |
| 每个项目 | **coordinator**（`projects.coordinator`） | 里程碑 / WBS / 建任务 / 任务 done·stopped / 抢锁替人落地 |
| 拍板 | **human** | 天然算（它是定这两个角色的人）；**只有它能指定项目的 coordinator** |

三条边角（本文件钉的就是它们）：
1. 项目**在**、`coordinator` 为空 ⇒ **拒**（`no_coordinator`），不许默认兜底；
2. **不属于任何项目**（空 workspace / 没登记的目录）⇒ 回落 manager；
3. 锁与提交闸只有**路径** ⇒ 按路径找项目，找到按那项目的 coordinator 判，找不到回落 manager。
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import MailError, Store, _role

WS_A = "/tmp/proj-a"
WS_B = "/tmp/proj-b"


class CoordinatorScopeTest(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("human", "人", ""),
                ("opencode", "OpenCode", "manager"),   # 全局 manager
                ("kimi", "Kimi", ""),
                ("grok", "Grok", ""),
                ("cursor", "Cursor", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    # ── 铺场 ──────────────────────────────────────────────────────────────

    def _case(self, ws: str | None) -> str:
        return self.store.create_case(agent="opencode", title="控制包", workspace=ws)["id"]

    def _designate(self, ws: str, who: str) -> None:
        self.store.add_project(ws, coordinator=who, actor="human")

    # ── ① 项目在、没指定 ⇒ 拒 ─────────────────────────────────────────────

    def test_项目没指定协调者就拒(self):
        cid = self._case(WS_A)
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_add(cid, agent="kimi", name="甲")
        self.assertEqual(ctx.exception.code, "no_coordinator")
        self.assertIn("还没指定 coordinator", ctx.exception.message)

    def test_指定之后那个项目的协调者能做(self):
        cid = self._case(WS_A)
        self._designate(WS_A, "kimi")
        got = self.store.case_milestone_add(cid, agent="kimi", name="甲")
        self.assertEqual([m["name"] for m in got["milestones"]], ["甲"])

    def test_不是那个项目的人不能插手(self):
        self._designate(WS_A, "kimi")
        self._designate(WS_B, "grok")
        cid_b = self._case(WS_B)
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_add(cid_b, agent="kimi", name="越界")
        self.assertEqual(ctx.exception.code, "not_coordinator")
        self.assertEqual(ctx.exception.status, 403)

    def test_manager_不再自动有项目里的权(self):
        """这是分层最要紧的一条：全局 manager 不等于项目的 coordinator。"""
        cid = self._case(WS_A)
        self._designate(WS_A, "kimi")          # 这个项目的 coordinator 是 kimi
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_add(cid, agent="opencode", name="越权")
        self.assertEqual(ctx.exception.code, "not_coordinator")

    def test_human_天然算(self):
        cid = self._case(WS_A)                  # 谁都没指定
        got = self.store.case_milestone_add(cid, agent="human", name="人拍的")
        self.assertEqual([m["name"] for m in got["milestones"]], ["人拍的"])

    def test_清空就是回到没指定(self):
        self._designate(WS_A, "kimi")
        self.store.add_project(WS_A, coordinator="", actor="human")
        cid = self._case(WS_A)
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_add(cid, agent="kimi", name="甲")
        self.assertEqual(ctx.exception.code, "no_coordinator")

    # ── ② 不属于任何项目 ⇒ 回落 manager ───────────────────────────────────

    def test_没有项目的卷回落_manager(self):
        cid = self._case(None)                  # 空 workspace：不归属任何项目
        got = self.store.case_milestone_add(cid, agent="opencode", name="兜底")
        self.assertEqual([m["name"] for m in got["milestones"]], ["兜底"])

    def test_没项目的卷普通成员还是不行(self):
        cid = self._case(None)
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_add(cid, agent="cursor", name="甲")
        self.assertEqual(ctx.exception.code, "not_coordinator")

    # ── ③ 只有 human 能指定 ──────────────────────────────────────────────

    def test_只有human_能指定(self):
        with self.assertRaises(MailError) as ctx:
            self.store.add_project(WS_A, coordinator="kimi", actor="opencode")
        self.assertEqual(ctx.exception.code, "human_only")
        self.assertEqual(ctx.exception.status, 403)
        # manager 也不行（它不是 human）——这正是"分层"的意思
        with self.assertRaises(MailError):
            self.store.add_project(WS_A, coordinator="kimi", actor="opencode")

    def test_指定一个名册里没有的名字会被拒(self):
        with self.assertRaises(MailError) as ctx:
            self.store.add_project(WS_A, coordinator="查无此人", actor="human")
        self.assertEqual(ctx.exception.code, "unknown_agent")

    # ── ④ 锁：按路径找项目，找不到回落 manager ────────────────────────────

    def test_锁_按路径找项目_找不到回落manager(self):
        self._designate(WS_A, "kimi")
        inside = f"{WS_A}/x.py"                    # 项目 A 里的路径
        outside = str(self.home / "y.py")          # 谁的项目都不是

        # 项目内的：只有 A 的 coordinator（或 human）能抢——**manager 也不行**（分层最要紧那条）
        for who in ("grok", "opencode"):
            with self.assertRaises(MailError) as ctx:
                self.store.lock_acquire(who, [inside], force=True)
            self.assertEqual(ctx.exception.code, "not_coordinator")
        self.store.lock_acquire("kimi", [inside], force=True)          # A 的 coordinator：可以
        self.store.lock_release("kimi", [inside])

        # 项目外的：回落 manager
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("kimi", [outside], force=True)
        self.assertEqual(ctx.exception.code, "not_coordinator")
        self.store.lock_acquire("opencode", [outside], force=True)     # manager：可以
        self.store.lock_release("opencode", [outside])

    def test_指定协调者会留痕(self):
        """治理动作必须进只增痕迹（哈希链覆盖它，外锚把它钉住）——不然"谁是协调者"查不出来。"""
        import sqlite3

        self._designate(WS_A, "kimi")
        self.store.add_project(WS_A, coordinator="", actor="human")     # 再清空
        con = sqlite3.connect(self.store.path)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM task_events WHERE action = 'project_coordinator' ORDER BY seq"
            )]
        finally:
            con.close()
        self.assertEqual([(r["from_status"], r["to_status"]) for r in rows], [("", "kimi"), ("kimi", "")])
        self.assertEqual({r["agent"] for r in rows}, {"human"})
        self.assertIn(WS_A, rows[0]["detail"])

    # ── ⑤ 旧名兼容    # ── ⑤ 旧名兼容 ───────────────────────────────────────────────────────

    def test_旧的_role值当_manager_认(self):
        self.assertEqual(_role("coordinator"), "manager")   # 旧名一律记成 manager，不给它第二个意思
        self.assertEqual(_role("manager"), "manager")
        self.assertEqual(_role(""), "")
        with self.assertRaises(MailError):
            self.assertEqual(_role("别的"), "")


if __name__ == "__main__":
    unittest.main()
