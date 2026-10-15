"""时区回归：全项目所有"给人看的时间"一律**北京时间（固定 UTC+8）**。

背景（human 2026-10-15）：各 agent 机器时区不一样，同一封信在四家显示四个时间 ⇒ 无法对齐。
口径：**存储**仍旧 UTC（`...Z`，排序比较用）；**展示**一律 +0800，**不随机器时区变**。

本文件就是这条口径的**机器验证**：把机器时区改成纽约/伦敦/东京，展示必须一字不变。
"""

import os
import sys
import tempfile
import threading
import time as _time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.client import request
from infocenter.server import make_handler
from infocenter.store import BEIJING, Store, beijing_hhmm, beijing_stamp, beijing_time, utcnow

SAMPLE_UTC = "2026-10-15T08:29:01Z"
SAMPLE_BJ = "2026-10-15 16:29:01 +0800"
FOREIGN_ZONES = ("UTC", "America/New_York", "Europe/London", "Asia/Tokyo", "Pacific/Kiritimati")


class _ForeignTimezone:
    """临时把机器时区改成别的（模拟"别的 agent 的机器"）。"""

    def __init__(self, zone: str):
        self.zone = zone

    def __enter__(self):
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = self.zone
        _time.tzset()
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        _time.tzset()
        return False


class BeijingConversionTest(unittest.TestCase):
    def test_utc_to_beijing_is_plus_8(self):
        self.assertEqual(beijing_time(SAMPLE_UTC), SAMPLE_BJ)
        self.assertEqual(beijing_hhmm(SAMPLE_UTC), "16:29")
        self.assertEqual(BEIJING.utcoffset(None), timedelta(hours=8))

    def test_cross_midnight(self):
        # UTC 20:00 ⇒ 北京时间次日 04:00（跨日不能算错）
        self.assertEqual(beijing_time("2026-10-15T20:00:00Z"), "2026-10-16 04:00:00 +0800")
        self.assertEqual(beijing_hhmm("2026-10-15T20:00:00Z"), "04:00")

    def test_bad_input_is_passed_through(self):
        self.assertEqual(beijing_time(""), "")
        self.assertEqual(beijing_time("不是时间"), "不是时间")

    def test_display_ignores_machine_timezone(self):
        """**核心**：机器时区随便改，展示永远是北京时间。"""
        for zone in FOREIGN_ZONES:
            with self.subTest(zone=zone), _ForeignTimezone(zone):
                self.assertEqual(beijing_time(SAMPLE_UTC), SAMPLE_BJ)
                self.assertEqual(beijing_hhmm(SAMPLE_UTC), "16:29")

    def test_stamp_is_now_in_beijing(self):
        moment = datetime.strptime(beijing_stamp(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        drift = abs((datetime.now(BEIJING) - moment).total_seconds())
        self.assertLess(drift, 10, "日志戳必须是当前的北京时间")

    def test_storage_stays_utc(self):
        """存储不许改成北京时间：`Z` 结尾、可被 UTC 解析。"""
        now = utcnow()
        self.assertTrue(now.endswith("Z"), now)
        self.assertEqual(
            datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).utcoffset(),
            timedelta(0),
        )


class BeijingEndToEndTest(unittest.TestCase):
    """真起一遍服务：接口吐出来的时间字段必须是北京时间（换机器时区也一样）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(self.home / "mailbox.db", seeds=[("opencode", "OpenCode", "coordinator")])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_inbox_exposes_beijing_under_any_machine_timezone(self):
        for zone in FOREIGN_ZONES:
            with self.subTest(zone=zone), _ForeignTimezone(zone):
                sent = self.store.send(
                    "opencode", to=["opencode"], subject=f"时区探针 {zone}", body="正文", workspace="/tmp"
                )
                inbox = request(self.url, "GET", "/v1/inbox", "opencode", query={"status": "all"})
                item = next(m for m in inbox["messages"] if m["id"] == sent["id"])
                # 存储：UTC（带 Z）
                self.assertTrue(item["created_at"].endswith("Z"), item["created_at"])
                # 展示：北京时间，且与 UTC 严格差 8 小时
                self.assertTrue(item["created_at_beijing"].endswith("+0800"), item["created_at_beijing"])
                self.assertEqual(item["created_at_beijing"], beijing_time(item["created_at"]))
                self.assertNotIn("_local", item)

    def test_locks_and_agents_expose_beijing(self):
        got = self.store.lock_acquire("opencode", paths=["/tmp/a.py"], ttl=600)
        self.assertTrue(got["expires_at"].endswith("Z"), got["expires_at"])  # 存储：UTC
        self.assertTrue(got["expires_at_beijing"].endswith("+0800"), got["expires_at_beijing"])
        row = got["locks"][0]
        self.assertTrue(row["acquired_at_beijing"].endswith("+0800"), row["acquired_at_beijing"])
        self.assertTrue(row["expires_at_beijing"].endswith("+0800"), row["expires_at_beijing"])

        locks = request(self.url, "GET", "/v1/locks", "opencode")
        self.assertTrue(locks["locks"], "刚拿的锁要能查到")
        self.assertNotIn("acquired_at_local", locks["locks"][0])

        agents = request(self.url, "GET", "/v1/agents", "opencode")
        me = next(a for a in agents["agents"] if a["name"] == "opencode")
        self.assertNotIn("last_seen_local", me)
        if me["last_seen_beijing"]:  # 没活动过就没有这个时间
            self.assertTrue(me["last_seen_beijing"].endswith("+0800"), me["last_seen_beijing"])

    def test_machine_timezone_does_not_leak_into_service(self):
        """最后一道：服务跑在纽约时区，接口吐的时间必须还是 +0800。"""
        with _ForeignTimezone("America/New_York"):
            sent = self.store.send(
                "opencode", to=["opencode"], subject="纽约探针", body="正文", workspace="/tmp"
            )
            item = self.store.get_message("opencode", sent["id"])
            self.assertTrue(item["created_at_beijing"].endswith("+0800"), item["created_at_beijing"])
            self.assertEqual(item["created_at_beijing"], beijing_time(item["created_at"]))


if __name__ == "__main__":
    unittest.main()
