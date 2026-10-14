"""本轮（管理台后端增量）发现的真 bug 回归测试。

覆盖：
- crew._substitute_prompt：嵌套引号绕过转义 → 命令注入（安全）
- crew.headless_runs / running_daemons：坏日志行不再让整个端点 500；负数 limit 不再误裁记录
- store._collect_projects：同一项目的绝对路径/裸目录名合并结果稳定，不丢 archived
- store.add_project：返回项始终带 archived、并与 list_projects 同口径
- httpapi /v1/crew：up 指向不存在的目录应 400 而不是 500

全部走临时目录/临时库，不动现网。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import client, crew
from infocenter.server import make_handler
from infocenter.store import MailError, Store


class _ServerBase(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("grok", "Grok", ""), ("human", "用户", "")],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()


class PromptSubstitutionTest(unittest.TestCase):
    def test_nested_quote_bypass_is_rejected(self):
        # `echo '"{prompt}"'` 里的 {prompt} 其实处在单引号串内部；旧写法会把外层 `'` 丢掉，
        # 让注入的 `; cmd` 变成独立 shell 语句。现在必须拒绝。
        with self.assertRaises(MailError) as ctx:
            crew._substitute_prompt('echo \'"{prompt}"\'', "; echo PWNED")
        self.assertEqual(ctx.exception.code, "unsafe_template")
        with self.assertRaises(MailError):
            crew._substitute_prompt('echo \' "{prompt}"', "; echo PWNED")

    def test_legit_forms_unchanged(self):
        self.assertEqual(crew._substitute_prompt("echo {prompt}", "hi"), "echo hi")
        self.assertEqual(crew._substitute_prompt('echo "{prompt}"', "a b"), "echo 'a b'")
        self.assertEqual(crew._substitute_prompt("echo '{prompt}'", "a b"), "echo 'a b'")
        with self.assertRaises(MailError) as ctx:
            crew._substitute_prompt('echo "{prompt} extra"', "x")
        self.assertEqual(ctx.exception.code, "unsafe_template")


class HeadlessRunsRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.sd = crew.state_dir(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_object_lines_are_skipped(self):
        # 合法 JSON 但非对象（null / [] / 数字）会触发 AttributeError → 整个端点 500。
        (self.sd / "grok--p.runs.jsonl").write_text(
            'null\n'
            '{"end":"2026-01-02T00:00:00Z","dir":"/p","outcome":"ok"}\n'
            '[]\n'
            '42\n'
            '{"end":"2026-01-03T00:00:00Z","dir":"/p","outcome":"fail"}\n',
            encoding="utf-8",
        )
        runs = crew.headless_runs(self.home)
        self.assertEqual([r["outcome"] for r in runs], ["fail", "ok"])

    def test_negative_limit_does_not_drop_records(self):
        (self.sd / "grok--p.runs.jsonl").write_text(
            "\n".join(
                json.dumps({"end": f"2026-01-0{i}T00:00:00Z", "dir": "/p"}) for i in range(1, 6)
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(len(crew.headless_runs(self.home, None, 5)), 5)
        # 旧实现 out[:-2] 会静默丢掉末尾两条；负数一律当 0。
        self.assertEqual(crew.headless_runs(self.home, None, -2), [])

    def test_non_object_meta_is_skipped(self):
        (self.sd / "grok--p.json").write_text("[1, 2, 3]", encoding="utf-8")
        (self.sd / "human--p.json").write_text('{"pid": 99999999, "dir": "/p"}', encoding="utf-8")
        daemons = crew.running_daemons(self.home)
        self.assertEqual([d["key"] for d in daemons], ["human--p"])
        self.assertEqual(crew.running_headless(self.home), [])


class ProjectsMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.project = (Path(self.tmp.name) / "myproj").resolve()
        self.project.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "mailbox.db", seeds=[("human", "用户", "")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_prefers_anchor_metadata(self):
        self.store.add_project(str(self.project), archived=True)
        # 守护/CLI 常以裸目录名上报 workspace，会插一行默认 archived=0 的项目；
        # 合并时必须保留锚点（绝对路径）那行的 archived，且只出现一项。
        self.store.send("human", ["human"], "s", "b", workspace="myproj")
        projects = self.store.list_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["workspace"], str(self.project))
        self.assertTrue(projects[0]["archived"])

    def test_add_project_return_matches_list(self):
        self.store.add_project(str(self.project), archived=True)
        returned = self.store.add_project("myproj", name="别名", archived=False)
        self.assertIn("archived", returned)
        listing = [p for p in self.store.list_projects() if p["workspace"] == returned["workspace"]]
        self.assertEqual(len(listing), 1)
        # detail（POST 返回）与 list 必须同口径。
        self.assertEqual(bool(returned["archived"]), bool(listing[0]["archived"]))


class CrewRouteSecurityTest(_ServerBase):
    def test_up_missing_dir_is_400(self):
        with self.assertRaises(client.ApiError) as ctx:
            client.request(self.url, "POST", "/v1/crew", "human", {"action": "up", "dir": "/no/such/proj/xyz"})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_dir")

    def test_down_missing_dir_still_ok(self):
        payload = client.request(self.url, "POST", "/v1/crew", "human", {"action": "down", "dir": "/no/such/proj/xyz"})
        self.assertEqual(payload["killed"], [])


class HeadlessRunsRouteTest(_ServerBase):
    def test_corrupt_line_does_not_500(self):
        sd = crew.state_dir(self.home)
        (sd / "grok--p.runs.jsonl").write_text(
            "null\n"
            '{"end":"2026-01-02T00:00:00Z","dir":"/p","outcome":"ok"}\n',
            encoding="utf-8",
        )
        payload = client.request(self.url, "GET", "/v1/headless/runs", "human", query={"limit": 50})
        self.assertEqual([r["outcome"] for r in payload["runs"]], ["ok"])


class WatchConfigHotReloadTest(unittest.TestCase):
    """F-1：守护每轮重读 config.json，headless 模板改动无需重启即刻生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "hot"
        self.home.mkdir()
        self.project = str(Path(tempfile.mkdtemp(dir=self.tmp.name, prefix="projHot")).resolve())
        self.cfg = self.home / "config.json"

    def _write(self, headless):
        self.cfg.write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "coordinator"},
                    {"name": "cursor", "headless": headless, "poll": 5, "heartbeat": 0},
                ]
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def _mail(self, mid, cursor):
        return {
            "cursor": cursor,
            "unread_total": 1,
            "new": [{"id": mid, "from": "opencode", "subject": "s", "workspace": self.project}],
        }

    def _run_watch(self, fake_request, templates, expect_stop=True):
        from unittest.mock import patch

        def fake_headless(template, prompt, proj, agent=None):
            templates.append(template)

        with (
            patch.object(crew, "_office_request", side_effect=lambda *a, **k: {"window_id": "w", "state": "open"}),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            if expect_stop:
                with self.assertRaises(StopIteration):
                    crew.watch(self.home, "cursor", self.project, "http://127.0.0.1:9", poll=5, heartbeat=0)
                return None
            return crew.watch(self.home, "cursor", self.project, "http://127.0.0.1:9", poll=5, heartbeat=0)

    def test_headless_template_change_applies_without_restart(self):
        self._write("echo OLD")
        state = {"n": 0}
        templates = []

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                # 第 1 轮跑到一半：运行中热改 config，下一轮 loop-top 应读到新模板。
                self._write("echo NEW")
                return self._mail("m1", 1)
            if state["n"] == 2:
                return self._mail("m2", 2)
            raise StopIteration("done")

        self._run_watch(fake_request, templates)
        self.assertEqual(templates, ["echo OLD", "echo NEW"])

    def test_clearing_headless_exits_daemon(self):
        self._write("echo OLD")
        state = {"n": 0}
        templates = []

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                self._write("")  # 运行中清空 headless
                return self._mail("m1", 1)
            raise StopIteration("done")

        code = self._run_watch(fake_request, templates, expect_stop=False)
        self.assertEqual(code, 0)
        self.assertEqual(templates, ["echo OLD"])


if __name__ == "__main__":
    unittest.main()
