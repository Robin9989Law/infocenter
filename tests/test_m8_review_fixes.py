"""cursor M8/M9/M10 评审（2026-10-15）指出的问题 → 逐条回归。

每条都对应他复现过的一个具体行为，六条：
A1 痕迹要与**行里的值**一致（否则 audit 变红、之后每次提交都被钩子挡）
A2 状态机要真查两张表：doing→todo 必须 409、todo→done 必须 409、todo→stopped 合法
A3 终态不许再调优先级（task_closed）；优先级统一走 `_task_priority`（带上限）
B1 窗口离场/进程死那两条路放锁**必须留痕**（原来裸 DELETE，事后查不到）
D1 打外锚前先用**现有锚**验一次，不通过就拒绝覆盖（防"洗锚"）
C1 `ic guard --staged` 只盯暂存区（未跟踪文件不该影响提交）
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import MailError, Store, _task_event_hash

WS = "/tmp/infocenter-demo"
A = "/tmp/infocenter-demo/infocenter/store.py"


class TaskReviewFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _add(self, **kw):
        kw.setdefault("owner", "cursor")
        kw.setdefault("workspace", WS)
        kw.setdefault("acceptance", "有单测")
        return self.store.task_add("opencode", kw.pop("title", "任务"), **kw)

    # ── A1：痕迹 == 行里的值（规范化之后的值） ────────────────────────────
    def test_trace_matches_normalized_values(self):
        """owner 传 `Cursor`、标题/验收带空格 ⇒ 痕迹必须照抄**写进行里的**值，audit 不红。"""
        task = self.store.task_add(
            "opencode", "  带空格的标题  ", owner="Cursor", workspace=WS, acceptance="  有 单测  "
        )
        self.assertEqual(task["owner"], "cursor")
        self.store.task_update("opencode", task["id"], acceptance="  改过的验收  ", deps="  a@sha256:x  ")
        self.store.task_update("opencode", task["id"], title=" 再一次  ", priority=1000)
        self.assertEqual(self.store.task_get("opencode", task["id"])["priority"], 99)
        self.store.task_start("cursor", task["id"])
        self.store.task_set_status("cursor", task["id"], "blocked", reason="  等上游  ")
        result = self.store.audit_events(task["id"])
        self.assertTrue(result["ok"], f"痕迹必须与行一致：{result['mismatches']}")

    def test_blocked_reason_and_handoff_in_trace_match_row(self):
        task = self._add()
        self.store.task_start("cursor", task["id"])
        self.store.task_set_status("cursor", task["id"], "blocked", reason="等上游产物")
        got = self.store.task_set_status("opencode", task["id"], "stopped", handoff="做到一半；产物在 _work/x.md；坑：sha 会变")
        self.assertEqual(got["status"], "stopped")
        self.assertTrue(self.store.audit_events(task["id"])["ok"])

    # ── A2：状态机真查表 ─────────────────────────────────────────────────
    def test_state_machine_rejects_backwards_moves(self):
        task = self._add()
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("opencode", task["id"], "done")
        self.assertEqual(ctx.exception.code, "invalid_transition", "没开工不能算跑完")

        self.store.task_start("cursor", task["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("opencode", task["id"], "todo")
        self.assertEqual(ctx.exception.code, "invalid_transition", "doing 不能倒回 todo")

    def test_owner_cannot_skip_to_done(self):
        task = self._add()
        self.store.task_start("cursor", task["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("cursor", task["id"], "done")
        self.assertEqual(ctx.exception.code, "not_coordinator")

    def test_supersedes_rejects_other_workspace(self):
        """旧线在 ws-a、新线在 ws-b ⇒ 409，旧项目看板仍把旧线当活线。"""
        old = self.store.task_add(
            "opencode", "旧线", owner="cursor", workspace="/tmp/ws-a", acceptance="x"
        )
        self.store.task_set_status("opencode", old["id"], "stopped", handoff="停")
        with self.assertRaises(MailError) as ctx:
            self.store.task_add(
                "opencode", "新线", owner="cursor", workspace="/tmp/ws-b",
                supersedes=old["id"], acceptance="x",
            )
        self.assertEqual(ctx.exception.code, "supersedes_workspace")
        self.assertEqual(ctx.exception.status, 409)
        listed = self.store.task_list("opencode", workspace="/tmp/ws-a")
        self.assertEqual([t["id"] for t in listed["tasks"]], [old["id"]])
        self.assertEqual(listed["superseded"], [])

    def test_supersedes_same_workspace_still_folds(self):
        old = self.store.task_add(
            "opencode", "旧线", owner="cursor", workspace="/tmp/ws-a", acceptance="x"
        )
        self.store.task_set_status("opencode", old["id"], "stopped", handoff="停")
        new = self.store.task_add(
            "opencode", "新线", owner="cursor", workspace="/tmp/ws-a",
            supersedes=old["id"], acceptance="x",
        )
        listed = self.store.task_list("opencode", workspace="/tmp/ws-a")
        self.assertEqual([t["id"] for t in listed["tasks"]], [new["id"]])
        self.assertEqual([item["id"] for item in listed["superseded"]], [old["id"]])

    def test_todo_can_be_stopped(self):
        task = self._add()
        got = self.store.task_set_status("opencode", task["id"], "stopped", handoff="没开工就放弃")
        self.assertEqual(got["status"], "stopped")
        self.assertTrue(self.store.audit_events(task["id"])["ok"])

    # ── A3：终态不许调优先级；优先级有上限 ───────────────────────────────
    def test_bump_on_terminal_task_is_rejected(self):
        task = self._add()
        self.store.task_start("cursor", task["id"])
        self.store.task_set_status("opencode", task["id"], "stopped", handoff="停")
        with self.assertRaises(MailError) as ctx:
            self.store.task_bump("opencode", task["id"], by=5)
        self.assertEqual(ctx.exception.code, "task_closed")

    def test_bump_respects_priority_ceiling(self):
        task = self._add(priority=90)
        got = self.store.task_bump("opencode", task["id"], by=50)
        self.assertLessEqual(got["priority"], 99, "优先级不能越过 _task_priority 的上限")
        self.assertEqual(got["priority"], 99)
        self.assertTrue(self.store.audit_events(task["id"])["ok"])

    # ── B1：放锁留痕（离场 / 死 pid） ────────────────────────────────────
    def test_window_leave_traces_lock_release(self):
        self.store.lock_acquire("cursor", [A], window_id="w-lv", task_id=self._add()["id"], ttl=600)
        self.store.leave_window("w-lv", agent="cursor")
        self.assertEqual(self.store.lock_list()["count"], 0, "离场要放锁")
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        actions = [r[0] for r in con.execute("SELECT action FROM task_events ORDER BY seq")]
        con.close()
        self.assertIn("unlock", actions, "放锁必须留痕（cursor：原来裸 DELETE，查不到）")
        self.assertTrue(self.store.audit_events()["ok"])

    def test_dead_pid_reconcile_traces_lock_release(self):
        task = self._add()
        self.store.touch("cursor", window_id="w-dead", pid=99999999)
        self.store.lock_acquire("cursor", [A], window_id="w-dead", task_id=task["id"], ttl=600)
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.execute("UPDATE windows SET pid = 99999999, state = 'active' WHERE window_id = 'w-dead'")
        con.commit()
        con.close()
        self.store.get_window("w-dead")  # 触发惰性对账
        self.assertEqual(self.store.lock_list()["count"], 0, "死窗口的锁不该僵死到 TTL")
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        notes = " ".join(r[0] for r in con.execute("SELECT detail FROM task_events ORDER BY seq"))
        con.close()
        self.assertIn("窗口进程已死", notes)

    # ── D1：防"洗锚" ─────────────────────────────────────────────────────
    def test_anchor_refuses_to_overwrite_a_mismatching_anchor(self):
        task = self._add()
        self.store.task_start("cursor", task["id"])
        audit = self.store.audit_events()
        anchor_file = self.home / ".infocenter-audit-anchor"
        anchor_file.write_text(
            cli.ANCHOR_HEADER + f"{audit['tip_seq']} {audit['tip_hash']} 2026-10-15 17:00:00\n",
            encoding="utf-8",
        )
        args = Namespace(url=self.url, task=None, anchor_file=str(anchor_file))

        # 先伪造：改一条痕迹的内容并把整条链重算成自洽（库内看不出来）
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.row_factory = sqlite3.Row
        prev = ""
        for row in con.execute("SELECT * FROM task_events ORDER BY seq").fetchall():
            detail = row["detail"]
            if row["seq"] == 1:
                payload = json.loads(detail)
                payload["acceptance"] = "被偷换的验收"
                detail = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            fields = {
                "task_id": row["task_id"], "at": row["at"], "agent": row["agent"],
                "action": row["action"], "from_status": row["from_status"],
                "to_status": row["to_status"], "detail": detail,
            }
            digest = _task_event_hash(prev, fields)
            con.execute(
                "UPDATE task_events SET detail = ?, prev_hash = ?, hash = ? WHERE seq = ?",
                (detail, prev, digest, row["seq"]),
            )
            prev = digest
        con.execute("UPDATE tasks SET acceptance = '被偷换的验收' WHERE id = ?", (task["id"],))
        con.commit()
        con.close()

        before = anchor_file.read_text(encoding="utf-8")
        code = cli._write_audit_anchor(args, "opencode", home=None)
        self.assertEqual(code, 1, "锚对不上就**拒绝**打锚，不能把旧锚作废")
        self.assertEqual(anchor_file.read_text(encoding="utf-8"), before, "锚文件不许被覆盖")

    def test_anchor_written_when_chain_is_fine(self):
        task = self._add()
        anchor_file = self.home / ".infocenter-audit-anchor"
        args = Namespace(url=self.url, task=None, anchor_file=str(anchor_file))
        self.assertEqual(cli._write_audit_anchor(args, "opencode", home=None), 0)
        anchor = cli._read_audit_anchor(anchor_file)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["seq"], self.store.audit_events()["tip_seq"])
        self.assertTrue(self.store.audit_events(anchor=anchor)["ok"])


if __name__ == "__main__":
    unittest.main()
