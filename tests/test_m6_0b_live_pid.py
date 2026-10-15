"""M6-0b：活窗口的 pid 不被短命 touch 改写。

headless 子进程继承宿主窗口号，再以自己的 pid 触达。若覆盖成功，子进程一退出，
惰性对账就把仍活着的宿主窗口判死（M7 起：**不中止案卷**，只摘指针 + 记一行）。

H1：继承窗口的 ic mcp 退出不得 leave。H2：重申 pid 必须先写入、再对账，且在同一事务内。
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.httpapi import handle
from infocenter.mcp_server import mcp_reports_own_pid, run_mcp
from infocenter.store import MailError, Store

DEAD_PID = 999_999_999
OTHER_DEAD_PID = 999_999_998


class LivePidGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db_path = self.home / "mailbox.db"
        self.store = Store(
            self.db_path,
            seeds=[("grok", "Grok", ""), ("opencode", "OpenCode", "coordinator")],
        )
        self.proj = str(self.home / "proj")
        self._saved_window = os.environ.pop("INFOCENTER_WINDOW", None)

    def tearDown(self):
        if self._saved_window is None:
            os.environ.pop("INFOCENTER_WINDOW", None)
        else:
            os.environ["INFOCENTER_WINDOW"] = self._saved_window
        self.tmp.cleanup()

    def _pid_of(self, window_id: str):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT pid, state FROM windows WHERE window_id = ?", (window_id,)).fetchone()
            return (row[0], row[1]) if row else (None, None)
        finally:
            con.close()

    def _claim_of(self, message_id: str, agent: str = "grok"):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT claimed_by FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (message_id, agent),
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def test_short_lived_touch_keeps_live_window_case_and_claim(self):
        host = os.getpid()
        self.store.touch("grok", window_id="w-host", workspace=self.proj, pid=host)
        msg = self.store.send("opencode", ["grok"], "派单", "正文", workspace=self.proj)
        claimed = self.store.events(
            "grok", since=0, wait=0, workspace=self.proj, window_id="w-host", pid=host
        )
        self.assertEqual([m["id"] for m in claimed["new"]], [msg["id"]])
        self.assertEqual(self._claim_of(msg["id"]), "w-host")
        case = self.store.create_case(
            agent="grok", title="在办", workspace=self.proj, owner_window="w-host"
        )
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["owner_window"], "w-host")
        self.assertFalse(case["sealed"])

        # 短命子进程：不同 pid，走和 ic mcp 一样的 events 触达。
        self.store.events(
            "grok", since=0, wait=0, workspace=self.proj, window_id="w-host", pid=DEAD_PID
        )
        self.store.touch("grok", window_id="w-host", workspace=self.proj, pid=OTHER_DEAD_PID)

        pid, state = self._pid_of("w-host")
        self.assertEqual(pid, host)
        self.assertEqual(state, "active")

        # get_window / get_case 都会跑惰性对账。活 pid 不得被判死。
        win = self.store.get_window("w-host")
        self.assertEqual(win["pid"], host)
        self.assertTrue(win["pid_alive"])
        self.assertEqual(win["state"], "active")
        again = self.store.get_case(case["id"])
        self.assertEqual(again["status"], "open")
        self.assertFalse(again["sealed"])
        self.assertEqual(again["owner_window"], "w-host")
        self.assertNotIn("主办离场", again["nodes"][-1]["note"])
        self.assertEqual(self._claim_of(msg["id"]), "w-host")

    def test_empty_or_dead_pid_still_accepts_a_new_one(self):
        self.store.touch("grok", window_id="w-empty", workspace=self.proj)
        self.assertIsNone(self._pid_of("w-empty")[0])
        self.store.touch("grok", window_id="w-empty", workspace=self.proj, pid=DEAD_PID)
        self.assertEqual(self._pid_of("w-empty")[0], DEAD_PID)

        # 原 pid 已死：允许改写（守护重启后重申自己的 pid）。
        self.store.touch("grok", window_id="w-empty", workspace=self.proj, pid=os.getpid())
        self.assertEqual(self._pid_of("w-empty")[0], os.getpid())
        # 一旦又活着，短命 pid 再次写不进去。
        self.store.touch("grok", window_id="w-empty", workspace=self.proj, pid=OTHER_DEAD_PID)
        self.assertEqual(self._pid_of("w-empty")[0], os.getpid())

    def test_mcp_reports_pid_only_for_a_self_minted_window(self):
        self.assertTrue(mcp_reports_own_pid(None))
        self.assertFalse(mcp_reports_own_pid("w-host"))
        os.environ["INFOCENTER_WINDOW"] = "w-host"
        self.assertFalse(mcp_reports_own_pid(None))
        # 显式窗口号优先于环境变量：调用方传入窗口时同样不报自己的 pid。
        self.assertFalse(mcp_reports_own_pid("w-other"))

    def _mcp_calls(self, window_id):
        calls = []

        def fake_request(*args, **kwargs):
            body = args[4] if len(args) > 4 else kwargs.get("body")
            calls.append({"method": args[1], "path": args[2], "body": body})
            return {}

        fake_in = type("Stdin", (), {})()
        fake_in.buffer = io.BytesIO(b"")
        with patch("infocenter.mcp_server.ensure_server", return_value="http://127.0.0.1:9"), \
             patch("infocenter.mcp_server.request", side_effect=fake_request), \
             patch("sys.stdin", fake_in):
            run_mcp("grok", "http://127.0.0.1:9", self.home, notify=False, window_id=window_id)
        return calls

    def test_inherited_mcp_exit_does_not_leave(self):
        calls = self._mcp_calls("w-host")
        puts = [c for c in calls if c["method"] == "PUT"]
        leaves = [c for c in calls if str(c["path"]).endswith("/leave")]
        self.assertEqual(len(puts), 1)
        self.assertNotIn("pid", puts[0]["body"])
        self.assertEqual(leaves, [])

    def test_self_minted_mcp_exit_still_leaves(self):
        calls = self._mcp_calls(None)
        puts = [c for c in calls if c["method"] == "PUT"]
        leaves = [c for c in calls if str(c["path"]).endswith("/leave")]
        self.assertEqual(len(puts), 1)
        self.assertIn("pid", puts[0]["body"])
        self.assertEqual(len(leaves), 1)

    def _open_case_on_dead_pid(self, window_id: str):
        self.store.touch("grok", window_id=window_id, workspace=self.proj, pid=DEAD_PID)
        case = self.store.create_case(
            agent="grok", title="在办", workspace=self.proj, owner_window=window_id
        )
        self.assertEqual(self._pid_of(window_id)[0], DEAD_PID)
        self.assertEqual(case["status"], "open")
        self.assertFalse(case["sealed"])
        return case

    def test_reassert_writes_pid_before_reconcile_and_keeps_case(self):
        case = self._open_case_on_dead_pid("w-host")
        body = json.dumps({
            "window_id": "w-host",
            "workspace": self.proj,
            "pid": os.getpid(),
        }).encode()
        status, payload = handle(
            "POST",
            "/v1/windows",
            {"X-Agent": "grok", "Content-Type": "application/json"},
            body,
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["state"], "active")
        again = self.store.get_case(case["id"])
        self.assertEqual(again["status"], "open")
        self.assertFalse(again["sealed"])
        self.assertEqual(again["owner_window"], "w-host")
        self.assertNotIn("主办离场", again["nodes"][-1]["note"])

    def test_pid_write_rolls_back_with_reconcile(self):
        self._open_case_on_dead_pid("w-host")

        def boom(conn, now=None):
            raise RuntimeError("reconcile failed")

        body = json.dumps({
            "window_id": "w-host",
            "workspace": self.proj,
            "pid": os.getpid(),
        }).encode()
        with patch("infocenter.store._lazy_reconcile_all", side_effect=boom):
            with self.assertRaises(RuntimeError):
                handle(
                    "POST",
                    "/v1/windows",
                    {"X-Agent": "grok", "Content-Type": "application/json"},
                    body,
                    self.store,
                )
        # 不走 get_case：那会再对账，把仍是死 pid 的窗口封掉，盖住回滚结果。
        self.assertEqual(self._pid_of("w-host"), (DEAD_PID, "active"))
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            sealed, status_name = con.execute(
                "SELECT sealed, status FROM cases WHERE owner_window = ?", ("w-host",)
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(sealed, 0)
        self.assertEqual(status_name, "open")

    def test_reassert_without_pid_does_not_seal_dead_window(self):
        """M7：主办窗口死了**不封卷**（宪法：案卷只随"事做完"关闭）。

        自动只做无争议的两件：① 窗口标 left ② 摘掉主办窗口指针 + 记一行 [自动] 说明。
        状态必须还是 open、sealed 必须还是 0——中止/办结要人显式做。
        """
        case = self._open_case_on_dead_pid("w-dead")
        status, _payload = handle(
            "GET",
            "/v1/inbox",
            {"X-Agent": "grok", "X-Window": "w-dead"},
            b"",
            self.store,
        )
        self.assertEqual(status, 200)
        again = self.store.get_case(case["id"])
        self.assertFalse(again["sealed"], "不许自动封卷")
        self.assertEqual(again["status"], "open")
        self.assertIsNone(again["owner_window"], "指针要摘（窗口都没了）")
        self.assertIn("保持未封卷", again["nodes"][-1]["note"])
        self.assertTrue(again["nodes"][-1]["auto"])
        # 案卷没封，就能接着办（M7 的关键：事没做完，案卷就该在）
        self.store.append_case_node(case["id"], agent="grok", note="接手继续办")
        self.assertEqual(self.store.get_case(case["id"])["nodes"][-1]["note"], "接手继续办")

    def test_http_guard_keeps_live_pid_and_rejects_other_agent(self):
        host = os.getpid()
        self.store.touch("grok", window_id="w-host", workspace=self.proj, pid=host)
        case = self.store.create_case(
            agent="grok", title="在办", workspace=self.proj, owner_window="w-host"
        )
        body = json.dumps({
            "window_id": "w-host",
            "workspace": self.proj,
            "pid": DEAD_PID,
        }).encode()
        status, _payload = handle(
            "POST",
            "/v1/windows",
            {"X-Agent": "grok", "Content-Type": "application/json"},
            body,
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self._pid_of("w-host")[0], host)
        self.assertFalse(self.store.get_case(case["id"])["sealed"])

        spoof = json.dumps({
            "window_id": "w-host",
            "workspace": self.proj,
            "pid": DEAD_PID,
        }).encode()
        with self.assertRaises(MailError) as ctx:
            handle(
                "POST",
                "/v1/windows",
                {"X-Agent": "kimi", "Content-Type": "application/json"},
                spoof,
                self.store,
            )
        self.assertEqual(ctx.exception.code, "window_conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self._pid_of("w-host")[0], host)


if __name__ == "__main__":
    unittest.main()
