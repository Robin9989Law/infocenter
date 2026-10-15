"""M8 · 任务队列（工作包）回归。

口径 = PLAN §12.5：单表；workspace 必填；deps 字符串列；handoff 停任务必填（命令层硬拒）；
权限写法二（owner 改 todo→doing / doing↔blocked，done/stopped 仅 coordinator）；
开工即冻结规格；done/stopped 终态不可逆；supersedes 成链不成树。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import ApiError, request
from infocenter.server import make_handler
from infocenter.store import MailError, Store

WS = "/tmp/infocenter-demo"
OTHER = "/tmp/another-project"


class TaskQueueStoreTest(unittest.TestCase):
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
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    HANDOFF = "progress: 做到 blocked\nartifacts: store.py@sha256:abc\npitfalls: 上游 sha 会变"

    def _add(self, title="store 侧", owner="cursor", **kw):
        kw.setdefault("workspace", WS)
        kw.setdefault("acceptance", "有单测")
        kw.setdefault("deps", "无")
        return self.store.task_add("opencode", title, owner=owner, **kw)

    # ── 建 / 列 ──────────────────────────────────────────────────────────
    def test_only_coordinator_can_add_and_workspace_required(self):
        with self.assertRaises(MailError) as ctx:
            self.store.task_add("cursor", "越权", workspace=WS)
        self.assertEqual(ctx.exception.code, "not_coordinator")
        self.assertEqual(ctx.exception.status, 403)

        with self.assertRaises(MailError) as ctx:
            self.store.task_add("opencode", "没带项目")
        self.assertEqual(ctx.exception.code, "workspace_required")
        self.assertEqual(ctx.exception.status, 400)

    def test_list_isolates_by_workspace_and_owner(self):
        a = self._add("本仓 A", owner="cursor")
        self._add("他仓 B", owner="cursor", workspace=OTHER)
        self._add("本仓 C", owner="grok")
        # agent 参数不是过滤器：--mine 由 CLI 落成 owner=自己
        mine = self.store.task_list("cursor", owner="cursor", workspace=WS)
        self.assertEqual([t["id"] for t in mine["tasks"]], [a["id"]])
        self.assertEqual(len(self.store.task_list("cursor", owner="cursor", workspace=OTHER)["tasks"]), 1)
        self.assertEqual(len(self.store.task_list("cursor", owner="grok", workspace=WS)["tasks"]), 1)
        self.assertEqual(len(self.store.task_list("cursor")["tasks"]), 3)

    # ── 开工 / 冻结 ──────────────────────────────────────────────────────
    def test_start_only_owner_or_coordinator(self):
        t = self._add()
        with self.assertRaises(MailError) as ctx:
            self.store.task_start("grok", t["id"])
        self.assertEqual(ctx.exception.code, "not_yours")
        self.assertEqual(self.store.task_start("cursor", t["id"])["status"], "doing")

    def test_spec_frozen_after_start(self):
        t = self._add()
        self.store.task_update("opencode", t["id"], acceptance="开工前可改")
        self.store.task_start("cursor", t["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_update("opencode", t["id"], title="改标题")
        self.assertEqual(ctx.exception.code, "spec_frozen")
        self.assertEqual(ctx.exception.status, 409)

    def test_update_is_coordinator_only(self):
        t = self._add()
        with self.assertRaises(MailError) as ctx:
            self.store.task_update("cursor", t["id"], title="自己改")
        self.assertEqual(ctx.exception.code, "not_coordinator")

    # ── 状态机 / 权限（写法二）───────────────────────────────────────────
    def test_blocked_requires_reason_and_owner_may_set_it(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("cursor", t["id"], "blocked")
        self.assertEqual(ctx.exception.code, "reason_required")
        got = self.store.task_set_status("cursor", t["id"], "blocked", reason="等上游产物")
        self.assertEqual(got["status"], "blocked")
        self.assertEqual(got["blocked_reason"], "等上游产物")
        # 回到 doing 也允许 owner 自己改
        self.assertEqual(self.store.task_set_status("cursor", t["id"], "doing")["status"], "doing")

    def test_done_and_stopped_are_coordinator_only(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("cursor", t["id"], "done")
        self.assertEqual(ctx.exception.code, "not_coordinator")
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("cursor", t["id"], "stopped", handoff=self.HANDOFF)
        self.assertEqual(ctx.exception.code, "not_coordinator")

    def test_stop_requires_handoff(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        with self.assertRaises(MailError) as ctx:
            self.store.task_set_status("opencode", t["id"], "stopped")
        self.assertEqual(ctx.exception.code, "handoff_required")
        self.assertEqual(ctx.exception.status, 400)
        got = self.store.task_set_status("opencode", t["id"], "stopped", handoff=self.HANDOFF)
        self.assertEqual(got["status"], "stopped")
        self.assertIn("pitfalls", got["handoff"])

    def test_terminal_states_are_irreversible(self):
        t = self._add()
        self.store.task_set_status("opencode", t["id"], "stopped", handoff=self.HANDOFF)
        for target in ("doing", "blocked", "done", "stopped"):
            with self.subTest(target=target):
                with self.assertRaises(MailError) as ctx:
                    self.store.task_set_status("opencode", t["id"], target, handoff=self.HANDOFF)
                self.assertEqual(ctx.exception.code, "task_closed")

    def test_spec_incomplete_does_not_block_start(self):
        # 第三条线只兜底：规格不全**不拦人**（看板自己标黄）
        t = self.store.task_add("opencode", "缺规格", owner="cursor", workspace=WS)
        self.assertEqual(self.store.task_start("cursor", t["id"])["status"], "doing")

    def test_stop_requires_non_empty_handoff(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        for bad in ("", "   "):
            with self.subTest(handoff=bad):
                with self.assertRaises(MailError) as ctx:
                    self.store.task_set_status("opencode", t["id"], "stopped", handoff=bad)
                self.assertEqual(ctx.exception.code, "handoff_required")
        # 一整段自由文本即可（不切三段）
        ok = self.store.task_set_status(
            "opencode", t["id"], "stopped", handoff="做到一半就停了；产物在 _work/x.md；坑：上游 sha 会变"
        )
        self.assertEqual(ok["status"], "stopped")

    # ── 留痕：程序自己写 + 机器验证 ──────────────────────────────────────
    def test_every_mutation_leaves_a_trace_and_audit_passes(self):
        t = self._add()
        self.store.task_update("opencode", t["id"], acceptance="改过的验收")
        self.store.task_start("cursor", t["id"])
        self.store.task_set_status("cursor", t["id"], "blocked", reason="等上游")
        self.store.task_set_status("cursor", t["id"], "doing")
        self.store.task_bump("opencode", t["id"], by=2)
        self.store.task_set_status("opencode", t["id"], "stopped", handoff=self.HANDOFF)

        result = self.store.audit_events(t["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["events"], 7)  # create+update+start+blocked+doing+bump+stopped

    def test_audit_detects_unlogged_change(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        # 绕过接口直接改行 = "少写一行痕迹"
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t["id"],))
        con.commit()
        con.close()
        result = self.store.audit_events(t["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(any(item.get("field") == "status" for item in result["mismatches"]))

    def test_audit_detects_tampered_or_deleted_trace(self):
        t = self._add()
        self.store.task_start("cursor", t["id"])
        self.assertTrue(self.store.audit_events(t["id"])["ok"])

        # 篡改一行内容 ⇒ 哈希对不上
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.execute("UPDATE task_events SET detail = '{\"status\": \"done\"}' WHERE action = 'start'")
        con.commit()
        con.close()
        tampered = self.store.audit_events(t["id"])
        self.assertFalse(tampered["ok"])
        self.assertTrue(any("改" in item["why"] for item in tampered["chain_broken"]))

        # 删掉一行 ⇒ 链断
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.execute("DELETE FROM task_events WHERE action = 'create'")
        con.commit()
        con.close()
        deleted = self.store.audit_events(t["id"])
        self.assertFalse(deleted["ok"])
        self.assertTrue(deleted["chain_broken"] or deleted["mismatches"])

    # ── supersedes 链 ───────────────────────────────────────────────────
    def test_supersedes_chain_not_tree(self):
        old = self._add("第一条线")
        self.store.task_start("cursor", old["id"])
        self.store.task_set_status("opencode", old["id"], "stopped", handoff=self.HANDOFF)
        new = self._add("第二条线", supersedes=old["id"])
        detail = self.store.task_get("opencode", new["id"])
        self.assertEqual(detail["attempt"], 2)
        self.assertEqual([p["id"] for p in detail["supersedes_chain"]], [old["id"]])
        self.assertEqual(detail["supersedes_chain"][0]["handoff"], self.HANDOFF)
        # 不能成树：old 已被 new 取代，再来一条也不行
        with self.assertRaises(MailError) as ctx:
            self._add("第三条线", supersedes=old["id"])
        self.assertEqual(ctx.exception.code, "supersedes_branch")
        # 未终结的任务不能被取代
        live = self._add("还活着")
        with self.assertRaises(MailError) as ctx:
            self._add("取代活线", supersedes=live["id"])
        self.assertEqual(ctx.exception.code, "task_not_terminal")

    def test_list_folds_superseded(self):
        old = self._add("旧线")
        self.store.task_set_status("opencode", old["id"], "stopped", handoff=self.HANDOFF)
        new = self._add("新线", supersedes=old["id"])
        got = self.store.task_list("opencode", workspace=WS)
        self.assertEqual([t["id"] for t in got["tasks"]], [new["id"]])
        self.assertEqual([s["id"] for s in got["superseded"]], [old["id"]])
        self.assertEqual(got["superseded"][0]["superseded_by"], new["id"])
        everything = self.store.task_list("opencode", workspace=WS, include_superseded=True)
        self.assertEqual({t["id"] for t in everything["tasks"]}, {old["id"], new["id"]})

    def test_bump_is_coordinator_only_and_reorders(self):
        low = self._add("先建的")
        high = self._add("后建的")
        self.assertEqual([t["id"] for t in self.store.task_list("opencode", workspace=WS)["tasks"]], [high["id"], low["id"]])
        with self.assertRaises(MailError):
            self.store.task_bump("cursor", low["id"])
        self.store.task_bump("opencode", low["id"], by=5)
        self.assertEqual([t["id"] for t in self.store.task_list("opencode", workspace=WS)["tasks"]], [low["id"], high["id"]])

    # ── HTTP 面 ─────────────────────────────────────────────────────────
    def test_http_roundtrip(self):
        created = request(self.url, "POST", "/v1/tasks", "opencode", {
            "title": "HTTP 建任务", "owner": "cursor", "workspace": WS,
            "deps": f"{WS}/infocenter/store.py@sha256:abc", "acceptance": "有单测",
        })
        tid = created["id"]
        self.assertEqual(created["status"], "todo")

        listed = request(self.url, "GET", "/v1/tasks", "cursor", query={"workspace": WS})
        self.assertEqual([t["id"] for t in listed["tasks"]], [tid])

        detail = request(self.url, "GET", f"/v1/tasks/{tid}", "cursor")
        self.assertEqual(detail["acceptance"], "有单测")

        request(self.url, "POST", f"/v1/tasks/{tid}/start", "cursor", {})
        # 非 coordinator 建任务 → 403
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", "/v1/tasks", "cursor", {"title": "x", "workspace": WS})
        self.assertEqual(ctx.exception.status, 403)

        # 停任务缺交接 → 400
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", f"/v1/tasks/{tid}/status", "opencode", {"status": "stopped"})
        self.assertEqual(ctx.exception.status, 400)

        stopped = request(self.url, "POST", f"/v1/tasks/{tid}/status", "opencode", {
            "status": "stopped",
            "handoff": "progress: HTTP 层\nartifacts: store.py@sha256:abc\npitfalls: 无",
        })
        self.assertEqual(stopped["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
