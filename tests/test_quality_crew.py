import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew
from infocenter.client import request
from infocenter.server import make_handler
from infocenter.store import Store, _workspace_scope

IC = str(ROOT / "bin" / "ic")


class CrewQualityTest(unittest.TestCase):
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

    def _env(self) -> dict:
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        return env

    def test_poll_and_heartbeat_clamp(self):
        # poll clamp 范围：5–3600（默认 300）
        self.assertEqual(crew._poll_seconds({}), 300)
        self.assertEqual(crew._poll_seconds({"poll": "bad"}), 300)
        self.assertEqual(crew._poll_seconds({"poll": 1}), 5)
        self.assertEqual(crew._poll_seconds({"poll": -10}), 5)
        self.assertEqual(crew._poll_seconds({"poll": 60}), 60)
        self.assertEqual(crew._poll_seconds({"poll": 99999}), 3600)

        # heartbeat clamp 范围：0–86400（默认 0）
        self.assertEqual(crew._heartbeat_seconds({}), 0)
        self.assertEqual(crew._heartbeat_seconds({"heartbeat": "bad"}), 0)
        self.assertEqual(crew._heartbeat_seconds({"heartbeat": -5}), 0)
        self.assertEqual(crew._heartbeat_seconds({"heartbeat": 900}), 900)
        self.assertEqual(crew._heartbeat_seconds({"heartbeat": 100000}), 86400)

    def test_workspace_scope_filtering(self):
        proj = Path(tempfile.mkdtemp(dir=str(self.home), prefix="MyProj")).resolve()
        # 全路径、大小写不敏感目录名均命中；空 workspace 或其他项目不命中
        self.assertTrue(_workspace_scope(str(proj), str(proj)))
        self.assertTrue(_workspace_scope(proj.name.lower(), str(proj)))
        self.assertTrue(_workspace_scope(proj.name.upper(), str(proj)))
        self.assertFalse(_workspace_scope(None, str(proj)))
        self.assertFalse(_workspace_scope("", str(proj)))
        self.assertFalse(_workspace_scope("/tmp/other-project", str(proj)))
        self.assertTrue(_workspace_scope(None, None))

        # Store.events 带 workspace 时只返回匹配该项目的未读信
        self.store.send("opencode", "cursor", "无范围", "交互处理")
        self.store.send("opencode", "cursor", "别家项目", "忽略", workspace="/tmp/another")
        hit = self.store.send("opencode", "cursor", "本家项目", "处理", workspace=proj.name)
        ev = self.store.events("cursor", since=0, wait=0, workspace=str(proj))
        self.assertEqual(ev["unread_total"], 1)
        self.assertEqual([m["id"] for m in ev["new"]], [hit["id"]])

    def test_heartbeat_prompt_custom_override_and_agent_env(self):
        # A5-1 + A5-2：验证配置了 heartbeat_prompt 时采用自定义模板（展开 {agent}/{project}），
        # 且守护进程向 headless 子进程注入 INFOCENTER_AGENT 身份环境变量
        unit_custom = crew.build_heartbeat_prompt(
            "opencode",
            "/tmp/demo",
            coordinator=True,
            custom_prompt="自定义心跳：身份={agent} 目录={project}",
        )
        self.assertEqual(unit_custom, "自定义心跳：身份=opencode 目录=/tmp/demo")
        self.assertNotIn("心跳检查（没有新信件）", unit_custom)

        unit_fallback = crew.build_heartbeat_prompt("opencode", "/tmp/demo", coordinator=True, custom_prompt="")
        self.assertIn("你是 opencode（coordinator）", unit_fallback)

        home = self.home / "custom_beat"
        home.mkdir()
        project = Path(tempfile.mkdtemp(dir=str(self.home), prefix="projCustom")).resolve()
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {
                        "name": "cursor",
                        "headless": 'sh -c \'cat > beat_out.txt; printf "\\nAGENT=%s\\n" "$INFOCENTER_AGENT" >> beat_out.txt\'',
                        "poll": 5,
                        "heartbeat": 2,
                        "heartbeat_prompt": "【自定义维护】我是 {agent}，正在巡检 {project}",
                    }
                ]
            }),
            encoding="utf-8",
        )
        up = subprocess.run(
            [sys.executable, IC, "--home", str(home), "crew", "up", "--dir", str(project), "--only", "cursor"],
            check=True, capture_output=True, text=True, env=self._env(),
        )
        self.assertIn("[守护] cursor pid", up.stdout)
        try:
            out_file = project / "beat_out.txt"
            deadline = time.time() + 12
            content = ""
            while time.time() < deadline:
                if out_file.exists():
                    content = out_file.read_text(encoding="utf-8")
                    if "AGENT=cursor" in content:
                        break
                time.sleep(0.1)
            self.assertTrue(out_file.exists(), "自定义 heartbeat_prompt 未触发")
            self.assertIn(f"【自定义维护】我是 cursor，正在巡检 {project}", content)
            self.assertNotIn("心跳检查（没有新信件）", content)
            self.assertIn("AGENT=cursor", content)
        finally:
            subprocess.run(
                [sys.executable, IC, "--home", str(home), "crew", "down", "--dir", str(project)],
                check=True, capture_output=True, text=True, env=self._env(),
            )

    def test_multi_project_status_and_down_dir_isolation(self):
        home = self.home / "iso"
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "cursor", "headless": "/usr/bin/tee -a out.txt", "poll": 5},
                ]
            }),
            encoding="utf-8",
        )
        p1 = Path(tempfile.mkdtemp(dir=str(self.home), prefix="isoA")).resolve()
        p2 = Path(tempfile.mkdtemp(dir=str(self.home), prefix="isoB")).resolve()
        env = self._env()
        try:
            for proj in (p1, p2):
                subprocess.run(
                    [sys.executable, IC, "--home", str(home), "crew", "up", "--dir", str(proj), "--only", "cursor"],
                    check=True, capture_output=True, text=True, env=env,
                )

            st_all = crew.status(home)
            self.assertEqual(len([a for a in st_all["agents"] if a["daemon_alive"]]), 2)
            st_p1 = crew.status(home, str(p1))
            self.assertEqual(len(st_p1["agents"]), 1)
            self.assertEqual(st_p1["agents"][0]["dir"], str(p1))

            # down --dir p1 只清理 p1，p2 守护仍在运行
            down_p1 = crew.down(home, str(p1))
            self.assertEqual(len(down_p1["killed"]), 1)
            st_after = crew.status(home)
            alive = [a for a in st_after["agents"] if a["daemon_alive"]]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0]["dir"], str(p2))
        finally:
            crew.down(home)

    def test_member_heartbeat_prompt_no_bare_coordinator_recipient(self):
        # A6-2：内置 member 心跳模板不得包含「给 coordinator 发信」等裸身份名措辞，支持注入真实协调者名
        prompt_default = crew.build_heartbeat_prompt("kimi", "/tmp/demo", coordinator=False)
        self.assertNotIn("给 coordinator", prompt_default)
        self.assertNotIn("coordinator 靠它", prompt_default)
        self.assertIn("主协调 agent（`ic agents` 里 role=coordinator 的那位）", prompt_default)

        prompt_with_coord = crew.build_heartbeat_prompt(
            "kimi", "/tmp/demo", coordinator=False, coordinator_name="opencode",
        )
        self.assertNotIn("给 coordinator", prompt_with_coord)
        self.assertIn("主协调 agent `opencode`（`ic agents` 里 role=coordinator 的那位）", prompt_with_coord)

    def test_status_marks_manual_coordinator(self):
        # coordinator 的前台由人驱动：不配 launch/headless 时不应显示成「未配置启动方式」。
        home = self.home / "stat_coord"
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "coordinator"},
                    {"name": "grok"},
                ]
            }),
            encoding="utf-8",
        )
        text = "\n".join(crew.status(home)["lines"])
        self.assertIn("opencode: manager；前台（人工驱动）", text)
        self.assertIn("grok: 未配置启动方式", text)

    def test_manager_misconfig_detection(self):
        # 只对「coordinator 且配了 headless/launch」告警；成员配 headless 属正常。
        self.assertEqual(crew.manager_misconfig({"name": "grok", "headless": "x"}), "")
        self.assertEqual(crew.manager_misconfig({"name": "o", "role": "coordinator"}), "")
        only_headless = crew.manager_misconfig(
            {"name": "o", "role": "coordinator", "headless": "x"}
        )
        self.assertIn("headless", only_headless)
        self.assertNotIn("launch ⇒", only_headless)
        only_launch = crew.manager_misconfig(
            {"name": "o", "role": "coordinator", "launch": "opencode"}
        )
        self.assertIn("launch", only_launch)
        self.assertNotIn("headless ⇒", only_launch)

    def test_plan_up_and_status_warn_manager_misconfig(self):
        home = self.home / "misconfig"
        home.mkdir()
        project = str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="projWarn")).resolve())
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "coordinator", "headless": "echo ok"},
                    {"name": "grok", "headless": "echo ok"},
                ]
            }),
            encoding="utf-8",
        )
        actions = crew.plan_up(home, self.url, project)
        warns = [a for a in actions if a["kind"] == "warn"]
        self.assertEqual([a["agent"] for a in warns], ["opencode"])

        out = crew.up(home, self.url, project, dry_run=True)
        self.assertTrue(any(line.startswith("[警告] opencode") for line in out["lines"]))

        text = "\n".join(crew.status(home)["lines"])
        self.assertIn("opencode: manager", text)
        self.assertIn("配了 headless", text)

    def test_watch_elif_at_most_one_headless_per_round(self):
        # A6-1：heartbeat_mode=always 且同一轮既有新信又到心跳点时，单轮至多跑一次 headless（elif），
        # 但不重置 last_run，因此下一轮无新信时补跑心跳；而 idle 模式新信会重置 last_run。
        from unittest.mock import patch

        home = self.home / "elif_check"
        home.mkdir()
        project = str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="projElif")).resolve())
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "coordinator"},
                    {
                        "name": "cursor",
                        "headless": "echo ok",
                        "poll": 5,
                        "heartbeat": 10,
                        "heartbeat_mode": "always",
                    },
                ]
            }),
            encoding="utf-8",
        )

        # 模拟两轮 events：
        # 第 1 轮（t=100 -> 115，已满 10s 心跳）既有新信又到心跳点 → 只应跑 1 次（新信），不跑心跳
        # 第 2 轮（t=116，无新信）因 always 模式未重置 last_run(100) → 触发 1 次心跳后抛 StopIteration 退出
        events_seq = [
            {
                "cursor": 1,
                "unread_total": 1,
                "new": [{"id": "m1", "from": "opencode", "subject": "新信", "workspace": project}],
            },
            {
                "cursor": 1,
                "unread_total": 0,
                "new": [],
            },
        ]
        headless_calls: list[str] = []
        round_after_calls: list[int] = []
        clock = [100.0]

        def fake_monotonic():
            return clock[0]

        def fake_request(_url, _method, _path, _agent, query=None, timeout=None):
            if round_after_calls:
                # 记录上一轮结束时累计的 headless 调用次数
                pass
            if not events_seq:
                raise StopIteration("done")
            item = events_seq.pop(0)
            if item["new"]:
                clock[0] = 115.0  # 第 1 轮返回时已超过 heartbeat=10s
            else:
                round_after_calls.append(len(headless_calls))
                clock[0] = 116.0  # 第 2 轮无新信
            return item

        def fake_headless(template, prompt, proj, agent=None, timeout=None):
            headless_calls.append(prompt)

        with (
            patch.object(crew.time, "monotonic", side_effect=fake_monotonic),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(home, "cursor", project, self.url, poll=5, heartbeat=10)

        # 第 1 轮结束时只跑了 1 次 headless（新信），绝未在同一轮连跑第 2 次心跳
        self.assertEqual(round_after_calls, [1])
        # 第 2 轮无新信时补触发了 1 次心跳，且注入了真实协调者名 opencode
        self.assertEqual(len(headless_calls), 2)
        self.assertIn("[m1] 来自 opencode《新信》", headless_calls[0])
        self.assertIn("心跳检查（没有新信件）", headless_calls[1])
        self.assertIn("主协调 agent `opencode`", headless_calls[1])
        self.assertNotIn("给 coordinator", headless_calls[1])

        # 对照：heartbeat_mode=idle 时，第 1 轮新信重置 last_run=115，第 2 轮 t=116 未满 10s 不触发心跳
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "coordinator"},
                    {
                        "name": "cursor",
                        "headless": "echo ok",
                        "poll": 5,
                        "heartbeat": 10,
                        "heartbeat_mode": "idle",
                    },
                ]
            }),
            encoding="utf-8",
        )
        events_seq[:] = [
            {
                "cursor": 2,
                "unread_total": 1,
                "new": [{"id": "m2", "from": "opencode", "subject": "第二封", "workspace": project}],
            },
            {
                "cursor": 2,
                "unread_total": 0,
                "new": [],
            },
        ]
        headless_calls.clear()
        round_after_calls.clear()
        clock[0] = 100.0

        with (
            patch.object(crew.time, "monotonic", side_effect=fake_monotonic),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(home, "cursor", project, self.url, poll=5, heartbeat=10)

        self.assertEqual(round_after_calls, [1])
        self.assertEqual(len(headless_calls), 1)



if __name__ == "__main__":
    unittest.main()
