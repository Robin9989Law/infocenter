"""空转治理（idle guard）回归。

背景：守护以前每 heartbeat 秒无条件跑一整个 headless 会话；命中模型额度/鉴权上限后
仍照原间隔硬撞，空烧额度、日志刷屏、心跳空转。本文件覆盖三件事：
  1) 永久性失败（额度/鉴权）与普通失败分类；
  2) 连续失败/永久失败 ⇒ 冷却，冷却期内不再触发 headless；
  3) 心跳空转 ⇒ 间隔指数拉长（idle backoff）。
另附 config 往返回归：新增调参键与 headless_timeout 必须能穿过 config.load()。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import config as cfg
from infocenter import crew
from infocenter.server import make_handler
from infocenter.store import Store


class IdleGuardUnitTest(unittest.TestCase):
    def test_config_roundtrip_keeps_tuning_keys(self):
        # headless_timeout 曾经不在 _KNOWN_KEYS 里 ⇒ config.load() 把它丢掉，守护读不到。
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(
            json.dumps({
                "agents": [{
                    "name": "cursor",
                    "headless": "echo ok",
                    "headless_timeout": 300,
                    "idle_backoff": False,
                    "idle_backoff_max": 900,
                    "progress_files": ["TODO-cursor.md"],
                    "failure_threshold": 5,
                    "failure_cooldown": 120,
                    "permanent_cooldown": 7200,
                }]
            }),
            encoding="utf-8",
        )
        entry = cfg.entry(home, "cursor")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("headless_timeout"), 300)
        self.assertEqual(entry.get("idle_backoff"), False)
        self.assertEqual(entry.get("idle_backoff_max"), 900)
        self.assertEqual(entry.get("progress_files"), ["TODO-cursor.md"])
        self.assertEqual(entry.get("failure_threshold"), 5)
        self.assertEqual(entry.get("failure_cooldown"), 120)
        self.assertEqual(entry.get("permanent_cooldown"), 7200)

    def test_classify_failure(self):
        self.assertEqual(crew._classify_failure(""), "fail")
        self.assertEqual(crew._classify_failure("Segmentation fault"), "fail")
        self.assertEqual(
            crew._classify_failure(
                "ActionRequiredError: You've hit your usage limit ... monthly cycle"
            ),
            "quota",
        )
        self.assertEqual(crew._classify_failure("401 invalid credential"), "quota")
        self.assertEqual(crew._classify_failure("insufficient_quota"), "quota")
        self.assertEqual(crew._classify_failure("HTTP 402 payment required"), "quota")

    def test_effective_heartbeat_stretches_and_caps(self):
        # 未空转 / 关闭退避 ⇒ 原样
        self.assertEqual(crew._effective_heartbeat(600, 0, True, 7200), 600)
        self.assertEqual(crew._effective_heartbeat(600, 3, False, 7200), 600)
        self.assertEqual(crew._effective_heartbeat(0, 3, True, 7200), 0)
        # 空转轮数 ×2 拉长，封顶 idle_backoff_max
        self.assertEqual(crew._effective_heartbeat(600, 1, True, 7200), 1200)
        self.assertEqual(crew._effective_heartbeat(600, 2, True, 7200), 2400)
        self.assertEqual(crew._effective_heartbeat(600, 5, True, 7200), 7200)
        # cap 比 heartbeat 还小时不得把间隔缩短
        self.assertEqual(crew._effective_heartbeat(600, 4, True, 100), 600)

    def test_cooldown_seconds_defaults_and_override(self):
        self.assertEqual(crew._cooldown_seconds({}, "quota"), crew.PERMANENT_COOLDOWN)
        self.assertEqual(crew._cooldown_seconds({}, "fail"), crew.FAILURE_COOLDOWN)
        self.assertEqual(
            crew._cooldown_seconds({"permanent_cooldown": 120}, "quota"), 120
        )
        self.assertEqual(crew._cooldown_seconds({"failure_cooldown": 0}, "fail"), 0)

    def test_failure_threshold_clamp(self):
        self.assertEqual(crew._failure_threshold({}), crew.FAILURE_THRESHOLD)
        self.assertEqual(crew._failure_threshold({"failure_threshold": 0}), 1)
        self.assertEqual(crew._failure_threshold({"failure_threshold": "bad"}), crew.FAILURE_THRESHOLD)

    def test_progress_snapshot_none_when_absent(self):
        home = Path(tempfile.mkdtemp())
        paths = [home / "TODO-cursor.md"]
        # 文件不存在 ⇒ 无法判定，返回 None（不据此判定空转）
        self.assertIsNone(crew._progress_snapshot(paths))
        paths[0].write_text("v1", encoding="utf-8")
        first = crew._progress_snapshot(paths)
        self.assertIsNotNone(first)
        paths[0].write_text("v2 changed", encoding="utf-8")
        self.assertNotEqual(first, crew._progress_snapshot(paths))

    def test_progress_files_default_is_todo(self):
        paths = crew._progress_files("/proj", "cursor", {})
        self.assertEqual(paths, [Path("/proj/TODO-cursor.md")])
        paths = crew._progress_files("/proj", "cursor", {"progress_files": "a.md, b.md"})
        self.assertEqual(paths, [Path("/proj/a.md"), Path("/proj/b.md")])

    def test_member_heartbeat_prompt_short_circuits(self):
        prompt = crew.build_heartbeat_prompt("cursor", "/proj", coordinator=False)
        self.assertIn("立即结束本次运行", prompt)
        self.assertIn("禁止把整份 PLAN.md 读进来", prompt)
        self.assertNotIn("给 coordinator", prompt)
        self.assertIn("主协调 agent（`ic agents` 里 role=coordinator 的那位）", prompt)


class IdleGuardWatchTest(unittest.TestCase):
    """用真实 HTTP 服务 + 打桩 events/headless/时钟，跑 crew.watch 的冷却路径。"""

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
        self.threading = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.threading.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _write_config(self, **entry):
        base = {"name": "cursor", "headless": "echo ok", "poll": 5, "heartbeat": 0}
        base.update(entry)
        (self.home / "config.json").write_text(
            json.dumps({"agents": [base]}), encoding="utf-8"
        )

    def test_quota_failure_enters_cooldown_without_hammering(self):
        project = str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="projQuota")).resolve())
        self._write_config(permanent_cooldown=600)
        clock = [1000.0]
        events = [{
            "cursor": 7,
            "unread_total": 1,
            "new": [{"id": "m1", "from": "opencode", "subject": "信", "workspace": project}],
        }]
        polls = []
        headless_calls = []
        cooldown_calls = []

        def fake_request(*_a, **_k):
            polls.append(clock[0])
            if events:
                return events.pop(0)
            raise StopIteration("done")

        def fake_headless(*_a, **_k):
            headless_calls.append(clock[0])
            return "quota"

        def fake_sleep(sec):
            clock[0] += float(sec)

        def fake_cooldown(_home, _key, kind, seconds):
            cooldown_calls.append((kind, seconds))

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(crew.time, "sleep", side_effect=fake_sleep),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
            patch.object(crew, "_set_cooldown_meta", side_effect=fake_cooldown),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "cursor", project, self.url, poll=5)

        # 只跑了 1 次 headless（命中额度即冷却，不再每轮重试）；冷却期内不轮询，
        # 冷却结束才恢复（时钟 1000 起、冷却 600s ⇒ 第二次轮询在 1600，随即 StopIteration）。
        self.assertEqual(len(headless_calls), 1)
        self.assertEqual(polls, [1000.0, 1600.0])
        # 冷却期间隔约等于 permanent_cooldown 之后才恢复
        self.assertGreaterEqual(clock[0] - 1000.0, 600)
        # 进入冷却写了一次「额度/鉴权不可用」，冷却结束清了一次
        self.assertEqual(cooldown_calls[0][0], "额度/鉴权不可用")
        self.assertEqual(cooldown_calls[-1][0], "")
        # 游标未推进（冷却期内的新信要留着，冷却后再投递）
        self.assertFalse(crew._cursor_file(self.home, crew._key("cursor", project)).exists())

    def test_consecutive_failures_enter_cooldown_at_threshold(self):
        project = str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="projFail")).resolve())
        self._write_config(failure_threshold=2, failure_cooldown=300)
        clock = [500.0]
        pending = [{
            "cursor": 9,
            "unread_total": 1,
            "new": [{"id": "m2", "from": "opencode", "subject": "信2", "workspace": project}],
        }]
        headless_calls = []

        def fake_headless(*_a, **_k):
            headless_calls.append(clock[0])
            return "fail"

        def fake_sleep(sec):
            clock[0] += float(sec)

        # 前两次都返回同一封未读信；达到阈值后进冷却，冷却期跳过轮询，随后 StopIteration 退出。
        state = {"n": 0}

        def fake_request_limited(*_a, **_k):
            state["n"] += 1
            if state["n"] <= 2:
                return pending[0]
            raise StopIteration("done")

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(crew.time, "sleep", side_effect=fake_sleep),
            patch.object(crew, "request", side_effect=fake_request_limited),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "cursor", project, self.url, poll=5)

        # 连续 2 次失败即进冷却 ⇒ 恰好 2 次 headless
        self.assertEqual(len(headless_calls), 2)

    def test_idle_heartbeat_stretches_interval(self):
        project = str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="projIdle")).resolve())
        # 进展信号文件存在但始终不变 ⇒ 每轮心跳都算空转，间隔 ×2 拉长
        (Path(project) / "TODO-cursor.md").write_text("未变", encoding="utf-8")
        self._write_config(heartbeat=10, idle_backoff_max=40, poll=5)
        clock = [0.0]
        headless_ticks = []

        def fake_request(*_a, **_k):
            if clock[0] >= 200:
                raise StopIteration("done")
            clock[0] += 5.0  # 模拟 /v1/events 长轮询等待
            return {"cursor": 0, "unread_total": 0, "new": []}

        def fake_headless(*_a, **_k):
            headless_ticks.append(clock[0])
            return "ok"

        def fake_sleep(sec):
            clock[0] += float(sec)

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(crew.time, "sleep", side_effect=fake_sleep),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "cursor", project, self.url, poll=5)

        # 心跳间隔应从 10 逐步拉长到 20/40（封顶 idle_backoff_max=40）
        gaps = [round(b - a) for a, b in zip(headless_ticks, headless_ticks[1:])]
        self.assertTrue(gaps, "应至少触发两次心跳（含一次拉长后的间隔）")
        self.assertLessEqual(max(gaps), 40)
        self.assertIn(20, gaps)


if __name__ == "__main__":
    unittest.main()
