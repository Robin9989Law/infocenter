"""M7 · 案卷 = 控制包：WBS 路径 + 里程碑（一列 JSON）+ 「离场不封卷」。

口径（human 定稿 + PLAN §十三）：
- 层级 `项目(workspace) → WBS(树) → 案卷(控制包) → 任务`；**一个案卷唯一归属一个 WBS**；
- **WBS 不建表**，用路径（`父/子`）；空 = 未归类（老案卷只归档不转）；
- **里程碑不建表**，是案卷上的**一列 JSON**（`[{"name","owner","done"}]`）；**只有 coordinator 能改**；
- 里程碑由 agent 提议、coordinator 落；一个里程碑可对应若干任务，案卷**只显示里程碑**；
- 案卷**只随"事做完"关闭**：窗口离场/进程死**不**自动中止（`_orphan_window_cases` 只摘指针 + 记一行）。
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

WS = "/Users/robinlaw/Downloads/信息中心"


class CaseWbsStoreTest(unittest.TestCase):
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

    def _case(self, **kw):
        kw.setdefault("agent", "cursor")
        kw.setdefault("title", "控制包")
        kw.setdefault("workspace", WS)
        return self.store.create_case(**kw)

    # ── WBS 路径 ──────────────────────────────────────────────────────────
    def test_wbs_path_basics(self):
        case = self._case(wbs_path="防混写/锁与闸")
        self.assertEqual(case["wbs_path"], "防混写/锁与闸")
        self.assertEqual(case["wbs"], "防混写/锁与闸")
        self.assertEqual(self._case(title="没归类")["wbs_path"], "")

    def test_wbs_path_is_normalized_and_validated(self):
        # 前后斜杠会被规整，中间的段必须非空
        self.assertEqual(self._case(wbs_path="/防混写/锁与闸/")["wbs_path"], "防混写/锁与闸")
        with self.assertRaises(MailError) as ctx:
            self._case(wbs_path="a//b")
        self.assertEqual(ctx.exception.code, "invalid_wbs")
        with self.assertRaises(MailError):
            self._case(wbs_path="x" * 201)

    def test_list_cases_by_wbs_prefix(self):
        a = self._case(title="根下", wbs_path="防混写")
        b = self._case(title="子下", wbs_path="防混写/锁与闸")
        c = self._case(title="别处", wbs_path="时区")
        d = self._case(title="没归类")
        got = {row["id"] for row in self.store.list_cases(wbs="防混写")}
        self.assertEqual(got, {a["id"], b["id"]}, "给父路径要带出子路径")
        self.assertEqual({row["id"] for row in self.store.list_cases(wbs="时区")}, {c["id"]})
        self.assertIn(d["id"], {row["id"] for row in self.store.list_cases()})

    def test_only_coordinator_can_set_wbs(self):
        case = self._case()
        with self.assertRaises(MailError) as ctx:
            self.store.case_wbs_set(case["id"], agent="cursor", wbs_path="偷改")
        self.assertEqual(ctx.exception.code, "not_coordinator")
        got = self.store.case_wbs_set(case["id"], agent="opencode", wbs_path="防混写/锁与闸")
        self.assertEqual(got["wbs_path"], "防混写/锁与闸")

    # ── 里程碑 ───────────────────────────────────────────────────────────
    def test_milestones_start_empty_and_are_json_column(self):
        case = self._case()
        self.assertEqual(case["milestones"], [])
        self.assertEqual(case["milestones_total"], 0)
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        raw = con.execute("SELECT milestones FROM cases WHERE id = ?", (case["id"],)).fetchone()[0]
        con.close()
        self.assertEqual(raw, "[]", "存的是 JSON 文本（不建表）")

    def test_milestone_add_and_done(self):
        case = self._case()
        self.store.case_milestone_add(case["id"], agent="opencode", name="锁到期不许丢", owner="cursor")
        got = self.store.case_milestone_add(case["id"], agent="opencode", name="外锚进 git")
        self.assertEqual([m["name"] for m in got["milestones"]], ["锁到期不许丢", "外锚进 git"])
        self.assertEqual(got["milestones"][0]["owner"], "cursor")
        self.assertEqual(got["milestones_done"], 0)

        done = self.store.case_milestone_done(case["id"], agent="opencode", index=0)
        self.assertEqual(done["milestones_done"], 1)
        self.assertTrue(done["milestones"][0]["done"])
        again = self.store.case_milestone_done(case["id"], agent="opencode", index=0, done=False)
        self.assertEqual(again["milestones_done"], 0, "勾错了要能撤回")
        with self.assertRaises(MailError) as ctx:
            self.store.case_milestone_done(case["id"], agent="opencode", index=9)
        self.assertEqual(ctx.exception.code, "invalid_milestones")

    def test_only_coordinator_can_touch_milestones(self):
        case = self._case()
        for call in (
            lambda: self.store.case_milestone_add(case["id"], agent="cursor", name="偷加"),
            lambda: self.store.case_milestone_done(case["id"], agent="cursor", index=0),
        ):
            with self.subTest(call=call):
                with self.assertRaises(MailError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "not_coordinator")

    def test_milestone_changes_are_recorded_on_the_case(self):
        case = self._case()
        self.store.case_milestone_add(case["id"], agent="opencode", name="里程碑甲")
        got = self.store.case_milestone_done(case["id"], agent="opencode", index=0)
        actions = [n["action"] for n in got["nodes"]]
        self.assertIn("milestone", actions)
        notes = " ".join(n["note"] for n in got["nodes"])
        self.assertIn("里程碑甲", notes)
        self.assertIn("✔", notes)

    def test_sealed_case_rejects_milestone_and_wbs_changes(self):
        case = self._case()
        self.store.close_case(case["id"], agent="cursor", note="做完了")
        for call in (
            lambda: self.store.case_milestone_add(case["id"], agent="opencode", name="封后加"),
            lambda: self.store.case_milestone_done(case["id"], agent="opencode", index=0),
            lambda: self.store.case_wbs_set(case["id"], agent="opencode", wbs_path="封后改"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(MailError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, "case_sealed")

    def test_milestones_json_validation(self):
        case = self._case()
        with self.assertRaises(MailError) as ctx:
            self.store.create_case(agent="cursor", title="坏里程碑", workspace=WS, milestones='{"a":1}')
        self.assertEqual(ctx.exception.code, "invalid_milestones")
        with self.assertRaises(MailError):
            self.store.create_case(agent="cursor", title="缺名字", workspace=WS, milestones=[{"owner": "x"}])
        ok = self.store.create_case(
            agent="cursor", title="好里程碑", workspace=WS,
            milestones=[{"name": "甲", "owner": "cursor", "done": True}],
        )
        self.assertEqual(ok["milestones_done"], 1)

    # ── 离场不封卷（M7-1 的行为，一并钉在这里） ──────────────────────────
    def test_window_leave_does_not_seal_case(self):
        case = self._case(owner_window="w-x", owner_agent="cursor")
        left = self.store.leave_window("w-x", agent="cursor")
        self.assertEqual(left["aborted_cases"], [])
        got = self.store.get_case(case["id"])
        self.assertEqual(got["status"], "open")
        self.assertFalse(got["sealed"])
        self.assertIsNone(got["owner_window"])
        self.assertEqual(got["wbs_path"], "", "离场不影响归属字段")
        # 案卷还在，里程碑照样能加（只有 coordinator）
        after = self.store.case_milestone_add(case["id"], agent="opencode", name="离场后仍可推进")
        self.assertEqual(after["milestones_total"], 1)

    def test_http_roundtrip_milestones_and_wbs(self):
        case = self._case()
        posted = request(
            self.url, "POST", f"/v1/cases/{case['id']}/milestone", "opencode",
            {"name": "线上加的", "owner": "cursor"},
        )
        self.assertEqual(posted["milestones"][0]["name"], "线上加的")
        done = request(
            self.url, "POST", f"/v1/cases/{case['id']}/milestone-done", "opencode",
            {"index": 0, "done": True},
        )
        self.assertEqual(done["milestones_done"], 1)
        wbs = request(
            self.url, "POST", f"/v1/cases/{case['id']}/wbs", "opencode", {"wbs_path": "留痕/评审"}
        )
        self.assertEqual(wbs["wbs_path"], "留痕/评审")

        listed = request(self.url, "GET", "/v1/cases", "opencode", query={"wbs": "留痕"})
        self.assertEqual([row["id"] for row in listed["cases"]], [case["id"]])

        with self.assertRaises(ApiError) as ctx:
            request(self.url, "POST", f"/v1/cases/{case['id']}/milestone", "cursor", {"name": "偷加"})
        self.assertEqual(ctx.exception.status, 403, "权限错是 403，不是 400")
        self.assertEqual(ctx.exception.code, "not_coordinator")


class CaseSchemaMigrationTest(unittest.TestCase):
    """老库（没有这两列）打开时必须自愈：ALTER 加列 + 老行读成 空/[]。"""

    def test_old_cases_table_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mailbox.db"
            con = sqlite3.connect(db)
            con.execute(
                """
                CREATE TABLE cases (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    workspace TEXT, status TEXT NOT NULL DEFAULT 'open', owner_agent TEXT,
                    owner_window TEXT, waiting_for TEXT, sealed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT
                )
                """
            )
            con.execute(
                "INSERT INTO cases(id, thread_id, title, status, sealed, created_at, updated_at)"
                " VALUES ('c-old','c-old','老案卷','open',0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            con.commit()
            con.close()

            os.environ["INFOCENTER_QUIET"] = "1"
            store = Store(db, seeds=[("opencode", "OpenCode", "coordinator")])
            con = sqlite3.connect(db)
            try:
                cols = {row[1] for row in con.execute("PRAGMA table_info(cases)")}
            finally:
                con.close()
            self.assertIn("wbs_path", cols)
            self.assertIn("milestones", cols)
            old = store.get_case("c-old")
            self.assertEqual(old["wbs_path"], "", "老案卷只归档不转：默认未归类")
            self.assertEqual(old["milestones"], [])
            # 迁移后新功能可用
            got = store.case_milestone_add("c-old", agent="opencode", name="迁移后可用")
            self.assertEqual(got["milestones_total"], 1)


if __name__ == "__main__":
    unittest.main()
