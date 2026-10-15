"""M6-0：跨 window 的认领必须按 workspace 作用域 —— 他项目窗口不得扣住本项目的信。

真实事故（2026-10-15）：`opencode` 发给 `grok` 的复核信（workspace=信息中心）在发出的同一秒被
`grok` 在**另一个项目**的窗口（`双异构…` 的 `ic mcp`，不做 workspace 过滤地长轮询）认领；于是
`grok` 的 信息中心 守护窗口在 `/v1/events` 与 `ic inbox` 里都看不到它（过滤 `claimed_by IS NULL
OR = 本窗`），每轮心跳都报「本项目未读 0 封」—— 信静默躺在库里。本文件锁定修复。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import Store


class ClaimScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db_path = self.home / "mailbox.db"
        self.store = Store(
            self.db_path,
            seeds=[("grok", "Grok", ""), ("opencode", "OpenCode", "coordinator"), ("human", "用户", "")],
        )
        self.proj_a = str(self.home / "projAlpha")
        self.proj_b = str(self.home / "projBeta")

    def tearDown(self):
        self.tmp.cleanup()

    def _claim_of(self, message_id, agent="grok"):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT claimed_by FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def _force_claim(self, message_id, window_id, agent="grok"):
        """模拟历史脏数据：直接把某条投递认领给指定窗口。"""
        con = sqlite3.connect(self.db_path, timeout=10)
        try:
            con.execute(
                "UPDATE deliveries SET claimed_by = ?, claimed_at = '2026-10-15T00:00:00Z' "
                "WHERE message_id = ? AND to_agent = ?",
                (window_id, message_id, agent),
            )
            con.commit()
        finally:
            con.close()

    def test_cross_project_poller_must_not_claim(self):
        # w-aA 是「A 项目」的窗口（先在 A 的 scope 下登记，拿到 workspace）
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-aA")
        # 一封属于 B 项目的信
        msg = self.store.send("human", ["grok"], "B 的信", "正文", workspace=self.proj_b)

        # w-aA 不带 workspace 长轮询（正是 w-c53009 的形态）：**看得见**（不改变既有可见性），
        # 但**不得认领**——认领要留给 B 项目自己的窗口/守护。
        ev = self.store.events("grok", since=0, wait=0, workspace=None, window_id="w-aA")
        self.assertEqual([m["id"] for m in ev["new"]], [msg["id"]])
        self.assertIsNone(self._claim_of(msg["id"]), "他项目窗口不得认领本项目的信")

        # B 项目自己的窗口应当照常收到
        ev_b = self.store.events("grok", since=0, wait=0, workspace=self.proj_b, window_id="w-bB")
        self.assertEqual([m["id"] for m in ev_b["new"]], [msg["id"]])
        self.assertEqual(self._claim_of(msg["id"]), "w-bB")

    def test_stale_foreign_claim_is_released_and_visible(self):
        # 历史脏数据：B 项目的信被 A 项目窗口认领
        msg = self.store.send("human", ["grok"], "B 的信", "正文", workspace=self.proj_b)
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-aA")
        self._force_claim(msg["id"], "w-aA")
        self.assertEqual(self._claim_of(msg["id"]), "w-aA")

        # B 项目窗口一查收件箱（触发惰性对账）⇒ 错位认领被回收，信可见
        box = self.store.inbox("grok", status="unread", window_id="w-bB", workspace=self.proj_b)
        self.assertIn(msg["id"], [m["id"] for m in box["messages"]])
        self.assertNotEqual(self._claim_of(msg["id"]), "w-aA")

    def test_claim_by_dead_window_is_released(self):
        msg = self.store.send("human", ["grok"], "本项目的信", "正文", workspace=self.proj_a)
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-dead")
        self.assertEqual(self._claim_of(msg["id"]), "w-dead")
        # 该窗口进程已死（pid 不存在）⇒ 下一轮对账回收认领
        self.store.leave_window("w-dead", agent="grok")
        self.assertIsNone(self._claim_of(msg["id"]))
        box = self.store.inbox("grok", status="unread", window_id="w-alive", workspace=self.proj_a)
        self.assertIn(msg["id"], [m["id"] for m in box["messages"]])

    def test_same_project_claim_still_hides_message(self):
        # 回归：同项目另一个窗口的认领照旧遮蔽（不因本次修复而放开）
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-a1")
        msg = self.store.send("human", ["grok"], "同项目的信", "正文", workspace=self.proj_a)
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-a1")
        box = self.store.inbox("grok", status="unread", window_id="w-a2", workspace=self.proj_a)
        self.assertNotIn(msg["id"], [m["id"] for m in box["messages"]])

    def test_workspace_less_message_is_still_claimable(self):
        # 无 workspace 的信（不会自动派给守护）：不做跨项目判断，行为不变
        msg = self.store.send("human", ["grok"], "无 workspace", "正文")
        self.store.events("grok", since=0, wait=0, workspace=self.proj_a, window_id="w-aA")
        ev = self.store.events("grok", since=0, wait=0, workspace=None, window_id="w-aA")
        self.assertIn(msg["id"], [m["id"] for m in ev["new"]])


if __name__ == "__main__":
    unittest.main()
