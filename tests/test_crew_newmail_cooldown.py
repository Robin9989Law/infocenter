"""空闲治理的两个洞（human 2026-10-15 点名要修）：

1. **冷却一刀切**：被冷却的 agent 连「新指令（高优先级信）」都收不到——
   冷却该治「模型不可用」，不该把别人发来的活一起关在门外。
   ⇒ 冷却期内仍探测高优先级新信，有就**提前结束冷却**（`_urgent_mail_waiting`）。

2. **超时被判死**：判定超时（配置里 pi 是 300s）比 `HEADLESS_TIMEOUT`（1800s）短得多，
   而「连续超时」被当成「连续失败」⇒ 慢模型必进 1 小时冷却 ⇒ **必然空转**。
   ⇒ **超时不算失败**：不涨失败计数、不进冷却，只退避重试（`_cooldown_failure` / `_should_cooldown`）。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew
from infocenter.client import ApiError


class TimeoutIsNotFailureTest(unittest.TestCase):
    """超时 ≠ 模型坏了。"""

    def test_timeout_not_counted_as_failure(self):
        self.assertFalse(crew._cooldown_failure("timeout"))

    def test_real_failures_still_count(self):
        for outcome in ("fail", "start_failed", "interrupted", "quota"):
            self.assertTrue(crew._cooldown_failure(outcome), outcome)

    def test_many_timeouts_never_trigger_cooldown(self):
        """连续多少轮超时都不进冷却（否则慢模型 = 死循环）。"""
        for streak in (1, 3, 9, 100):
            self.assertFalse(crew._should_cooldown("timeout", streak, 3), streak)

    def test_fail_streak_at_threshold_cools_down(self):
        self.assertFalse(crew._should_cooldown("fail", 2, 3))
        self.assertTrue(crew._should_cooldown("fail", 3, 3))

    def test_quota_cools_down_immediately(self):
        self.assertTrue(crew._should_cooldown("quota", 0, 3))

    def test_threshold_is_at_least_one(self):
        self.assertTrue(crew._should_cooldown("fail", 1, 0))


class HeadlessTimeoutTest(unittest.TestCase):
    def test_default_when_unset(self):
        self.assertEqual(crew._headless_timeout_seconds({}), crew.HEADLESS_TIMEOUT)

    def test_config_override_is_honored(self):
        """配置仍说话（有人故意压短就压短）——但压短已不再会「判死」（见上面一组测试）。"""
        self.assertEqual(crew._headless_timeout_seconds({"headless_timeout": 300}), 300)

    def test_junk_falls_back_to_default(self):
        self.assertEqual(crew._headless_timeout_seconds({"headless_timeout": "abc"}), crew.HEADLESS_TIMEOUT)
        self.assertEqual(crew._headless_timeout_seconds({"headless_timeout": 0}), crew.HEADLESS_TIMEOUT)


class UrgentMailProbeTest(unittest.TestCase):
    """冷却期内的「新指令」探测。"""

    def _call(self, payload=None, exc=None):
        def fake_request(url, method, path, agent, query=None, timeout=None):
            if exc is not None:
                raise exc
            return payload

        with patch.object(crew, "request", fake_request):
            return crew._urgent_mail_waiting("http://x", "pi", "/proj", 7)

    def test_counts_only_high_priority(self):
        payload = {"new": [
            {"id": "a", "priority": "high"},
            {"id": "b", "priority": "normal"},
            {"id": "c", "priority": "HIGH"},
            {"id": "d"},
        ]}
        self.assertEqual(self._call(payload), 2)

    def test_no_new_mail_is_zero(self):
        self.assertEqual(self._call({"new": []}), 0)
        self.assertEqual(self._call({}), 0)

    def test_probe_failure_is_zero_not_a_crash(self):
        """探测本身出错不能搅乱冷却状态（也不能把守护带崩）。"""
        self.assertEqual(self._call(exc=ApiError("boom", "x")), 0)
        self.assertEqual(self._call(exc=OSError("boom")), 0)
        self.assertEqual(self._call(exc=KeyError("boom")), 0)

    def test_probe_does_not_block(self):
        """探测必须 wait=0（只问一句就走），否则冷却期内会一直挂着。"""
        seen = {}

        def fake_request(url, method, path, agent, query=None, timeout=None):
            seen.update(query or {})
            return {"new": []}

        with patch.object(crew, "request", fake_request):
            crew._urgent_mail_waiting("http://x", "pi", "/proj", 7)
        self.assertEqual(seen.get("wait"), "0")
        self.assertEqual(seen.get("since"), "7")
        self.assertEqual(seen.get("workspace"), "/proj")


if __name__ == "__main__":
    unittest.main()
