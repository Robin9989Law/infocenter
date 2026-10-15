"""M10 · 留痕外锚 + 锁痕迹（机器验证的加强件）。

为什么要有外锚（kimi / cursor / grok 三家评审一致指出的缺口）：
现在哈希链**整个都在库里** ⇒ 会写 SQL 的人可以「改一行 → 把后面所有哈希重算一遍」，
链**依然自洽**，`ic audit` 照样绿。把链头（tip）写成仓库里的一个文件、连同 git 提交，
锚点就落在**库外**：库内链被改/被截短/整表重做，`ic audit` 一律报「外锚对不上」。

本文件就是这套的机器验证（含"攻击"用例：真的去改库、真的去重算哈希）。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import Store, _task_event_hash

WS = "/tmp/infocenter-demo"
A = "/tmp/infocenter-demo/infocenter/store.py"


class TraceAnchorTest(unittest.TestCase):
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

    def _con(self):
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _task_with_trace(self):
        task = self.store.task_add(
            "opencode", "外锚用例", owner="cursor", workspace=WS, acceptance="有单测"
        )
        self.store.task_start("cursor", task["id"])
        return task

    def _rehash_all(self, con, mutate=None):
        """**模拟攻击者**：按行重算整条链的哈希（可顺带改内容）⇒ 链重新自洽。"""
        prev = ""
        for row in con.execute("SELECT * FROM task_events ORDER BY seq").fetchall():
            fields = {
                "task_id": row["task_id"], "at": row["at"], "agent": row["agent"],
                "action": row["action"], "from_status": row["from_status"],
                "to_status": row["to_status"], "detail": row["detail"],
            }
            if mutate:
                mutate(fields, row)
            digest = _task_event_hash(prev, fields)
            con.execute(
                "UPDATE task_events SET agent = ?, detail = ?, prev_hash = ?, hash = ? WHERE seq = ?",
                (fields["agent"], fields["detail"], prev, digest, row["seq"]),
            )
            prev = digest

    # ── 外锚本体 ──────────────────────────────────────────────────────────
    def test_anchor_matches_when_untouched(self):
        self._task_with_trace()
        audit = self.store.audit_events()
        self.assertTrue(audit["ok"], audit)
        anchor = {"seq": audit["tip_seq"], "hash": audit["tip_hash"], "at": "测试"}
        again = self.store.audit_events(anchor=anchor)
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["anchor"]["ok"])
        self.assertEqual(again["anchor"]["unwitnessed"], 0)

    def test_external_anchor_catches_rehashed_tamper(self):
        """**关键用例**：改一行 + 把后面哈希全部重算（库内自洽）⇒ 只有外锚抓得到。"""
        task = self._task_with_trace()
        audit = self.store.audit_events()
        anchor = {"seq": audit["tip_seq"], "hash": audit["tip_hash"]}

        con = self._con()

        # 攻击：偷偷把第一条（create）里的验收标准换掉，再把整条链重算成自洽；
        # 任务行也一起改成"和改后的痕迹对得上" ⇒ 重放校验自然过（这才是真威胁）。
        def mutate(fields, row):
            if row["seq"] != 1:
                return
            detail = json.loads(fields["detail"])
            detail["acceptance"] = "被换过的验收"
            fields["detail"] = json.dumps(detail, ensure_ascii=False, sort_keys=True)

        self._rehash_all(con, mutate=mutate)
        con.execute("UPDATE tasks SET acceptance = ? WHERE id = ?", ("被换过的验收", task["id"]))
        con.commit()
        con.close()

        plain = self.store.audit_events(task["id"])
        self.assertTrue(plain["ok"], "光靠库内链，自洽的篡改是看不出来的（这正是要外锚的原因）")
        self.assertEqual(plain["mismatches"], [])

        caught = self.store.audit_events(task["id"], anchor=anchor)
        self.assertFalse(caught["ok"], "外锚必须抓到")
        self.assertFalse(caught["anchor"]["ok"])
        self.assertIn("哈希对不上", caught["anchor"]["why"])

    def test_external_anchor_catches_rebuilt_table(self):
        """整表删掉、重建一条同长度自洽假链 ⇒ 外锚对不上。"""
        task = self._task_with_trace()
        audit = self.store.audit_events()
        anchor = {"seq": audit["tip_seq"], "hash": audit["tip_hash"]}

        con = self._con()
        con.execute("DELETE FROM task_events")
        detail = '{"status": "done"}'
        fields = {
            "task_id": task["id"], "at": "2026-10-15T00:00:00Z", "agent": "attacker",
            "action": "status", "from_status": "doing", "to_status": "done", "detail": detail,
        }
        for seq in range(1, anchor["seq"] + 1):
            digest = _task_event_hash("" if seq == 1 else prev, fields)
            con.execute(
                """INSERT INTO task_events(seq, task_id, at, agent, action, from_status, to_status,
                                           detail, prev_hash, hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (seq, fields["task_id"], fields["at"], "attacker", "status", "doing", "done",
                 detail, "" if seq == 1 else prev, digest),
            )
            prev = digest
        con.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task["id"],))
        con.commit()
        con.close()

        caught = self.store.audit_events(task["id"], anchor=anchor)
        self.assertFalse(caught["ok"], caught)
        self.assertFalse(caught["anchor"]["ok"])

    def test_anchor_reports_unwitnessed_tail(self):
        self._task_with_trace()
        audit = self.store.audit_events()
        anchor = {"seq": audit["tip_seq"], "hash": audit["tip_hash"]}
        task = self.store.task_list("opencode", workspace=WS)["tasks"][0]["id"]
        self.store.task_bump("opencode", task, by=1)
        later = self.store.audit_events(anchor=anchor)
        self.assertTrue(later["ok"])
        self.assertEqual(later["anchor"]["unwitnessed"], 1, "锚点之后的新痕迹要显式可见")

    def test_anchor_does_not_mask_real_problems(self):
        """外锚只是加法：链断了、重放对不上，照样不通过。"""
        task = self._task_with_trace()
        audit = self.store.audit_events()
        anchor = {"seq": audit["tip_seq"], "hash": audit["tip_hash"]}
        con = self._con()
        con.execute("UPDATE task_events SET agent = 'x' WHERE seq = 1")
        con.commit()
        con.close()
        caught = self.store.audit_events(task["id"], anchor=anchor)
        self.assertFalse(caught["ok"])
        self.assertTrue(caught["chain_broken"])

    # ── 锁的痕迹 ──────────────────────────────────────────────────────────
    def test_lock_lifecycle_leaves_trace(self):
        task = self._task_with_trace()
        self.store.lock_acquire("cursor", [A], task_id=task["id"], ttl=60)
        self.store.lock_release("cursor", [A])
        con = self._con()
        actions = [r["action"] for r in con.execute("SELECT action FROM task_events ORDER BY seq")]
        con.close()
        self.assertIn("lock", actions)
        self.assertIn("unlock", actions)
        audit = self.store.audit_events(task["id"])
        self.assertTrue(audit["ok"], "锁的痕迹不能污染任务重放")

    def test_expired_lock_sweep_leaves_trace(self):
        self.store.lock_acquire("cursor", [A], ttl=60)
        con = self._con()
        con.execute("UPDATE locks SET expires_at = '2020-01-01T00:00:00Z'")
        con.commit()
        con.close()
        self.store.lock_list()  # 触发 sweep
        con = self._con()
        rows = con.execute("SELECT * FROM task_events WHERE action = 'lock_expire'").fetchall()
        con.close()
        self.assertEqual(len(rows), 1, "自动回收锁也要留痕")
        self.assertIn(A, rows[0]["detail"])
        self.assertTrue(self.store.audit_events()["ok"], "回收痕迹不能破坏链")

    def test_lock_reply_shows_remaining_seconds(self):
        got = self.store.lock_acquire("cursor", [A], ttl=600)
        left = got["locks"][0]["expires_in_seconds"]
        self.assertGreater(left, 540)
        self.assertLessEqual(left, 600)


class CommitHookInstallTest(unittest.TestCase):
    """第 2 道闸不能是"可选项"：新 clone 一条命令装上（`ic guard --install-hook`）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / ".githooks").mkdir()
        hook = self.repo / ".githooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o644)  # 故意不给执行位

    def tearDown(self):
        self.tmp.cleanup()

    def _hooks_path(self):
        return subprocess.run(
            ["git", "config", "core.hooksPath"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()

    def test_install_hook_sets_config_and_exec_bit(self):
        self.assertEqual(self._hooks_path(), "")
        self.assertEqual(cli._install_commit_hook(subprocess, cwd=self.repo), 0)
        self.assertEqual(self._hooks_path(), ".githooks")
        hook = self.repo / ".githooks" / "pre-commit"
        self.assertTrue(os.access(hook, os.X_OK), "钩子必须可执行，否则 git 不会跑它")

    def test_install_hook_is_idempotent(self):
        cli._install_commit_hook(subprocess, cwd=self.repo)
        self.assertEqual(cli._install_commit_hook(subprocess, cwd=self.repo), 0)
        self.assertEqual(self._hooks_path(), ".githooks")

    def test_install_hook_without_git_repo_fails_cleanly(self):
        # 注意：不能放在 setUp 那个仓库里面（git 会往上找到它）
        with tempfile.TemporaryDirectory() as outside:
            plain = Path(outside) / "not-a-repo"
            plain.mkdir()
            self.assertEqual(cli._install_commit_hook(subprocess, cwd=plain), 1)

    def test_hook_warning_when_not_installed(self):
        cwd = os.getcwd()
        try:
            os.chdir(self.repo)
            self.assertIn("没装提交闸钩子", cli._hook_warning())
            cli._install_commit_hook(subprocess, cwd=self.repo)
            self.assertEqual(cli._hook_warning(), "")
        finally:
            os.chdir(cwd)


class AnchorFileTest(unittest.TestCase):
    """外锚文件本身的读写（给人/给 git 用的那一行）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".infocenter-audit-anchor"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(cli._read_audit_anchor(self.path))

    def test_round_trip(self):
        line = f"{cli.ANCHOR_HEADER}7 {'a' * 64} 2026-10-15 16:42:11\n"
        self.path.write_text(line, encoding="utf-8")
        anchor = cli._read_audit_anchor(self.path)
        self.assertEqual(anchor["seq"], 7)
        self.assertEqual(anchor["hash"], "a" * 64)
        self.assertEqual(anchor["at"], "2026-10-15 16:42:11")

    def test_garbage_is_ignored(self):
        self.path.write_text("这里不是锚点\n# 注释\n", encoding="utf-8")
        self.assertIsNone(cli._read_audit_anchor(self.path))


if __name__ == "__main__":
    unittest.main()
