"""死流程兜底（第 3 条线里唯一会自动做的事）+ 看板黄灯（pi 2026-10-15 报的洞）。

pi 的原话：认领（`claimed_by`）**没有 TTL**，锁到期能自动清、认领不能 ⇒
**僵尸窗口（pid 活着但不再干活）的未读认领永不回收**，那条线对团队长时间不可见；
而且回收只靠"别人 inbox 时顺手清"（低活跃项目可能一直不触发）。

本文件钉死：
1. 认领 TTL（窗口超过 TTL 没跟服务说过话 ⇒ 它的认领算僵尸，回收）；
2. 回收**不再靠人触发**：服务端后台定时器 `_start_sweeper`；
3. 所有自动回收**只做无争议回收**（不改任何语义状态）且**全部留痕**；
4. 看板「停滞」黄灯：未封卷 + 长时间没人动 ⇒ 标黄提示（**只提示，程序不处置**）。
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import server
from infocenter.store import CLAIM_TTL, STALL_HOURS, Store

WS = "/tmp/infocenter-demo"
A = "/tmp/infocenter-demo/infocenter/store.py"


def _age(db: Path, sql: str, params=()) -> None:
    con = sqlite3.connect(db, timeout=10)
    con.execute(sql, params)
    con.commit()
    con.close()


def _actions(db: Path) -> list[str]:
    con = sqlite3.connect(db, timeout=10)
    rows = [r[0] for r in con.execute("SELECT action FROM task_events ORDER BY seq")]
    con.close()
    return rows


class DeadProcessSweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db = self.home / "mailbox.db"
        self.store = Store(self.db, seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")])

    def tearDown(self):
        self.tmp.cleanup()

    def _claimed_message(self, window_id="w-z", agent="cursor"):
        """造一封"被窗口认领了"的信。"""
        self.store.touch(agent, window_id=window_id, workspace=WS)
        msg = self.store.send("opencode", [agent], "派单", "正文", workspace=WS)
        self.store.events(agent, since=0, wait=0, workspace=WS, window_id=window_id)
        return msg

    def _claim_of(self, message_id, agent="cursor"):
        con = sqlite3.connect(self.db, timeout=10)
        row = con.execute(
            "SELECT claimed_by FROM deliveries WHERE message_id = ? AND to_agent = ?",
            (message_id, agent),
        ).fetchone()
        con.close()
        return row[0] if row else None

    # ── 1) 认领 TTL ──────────────────────────────────────────────────────
    def test_silent_window_claim_expires(self):
        msg = self._claimed_message("w-z")
        self.assertEqual(self._claim_of(msg["id"]), "w-z")
        # 窗口"活着"（pid 是活的：不写 pid 就不判死）但已经很久没跟服务说话
        old = "2020-01-01T00:00:00Z"
        _age(self.db, "UPDATE deliveries SET claimed_at = ? WHERE message_id = ?", (old, msg["id"]))
        _age(self.db, "UPDATE windows SET last_seen_at = ? WHERE window_id = 'w-z'", (old,))
        out = self.store.sweep()
        self.assertEqual(out["released_claims"], 1, "僵尸认领必须被回收")
        self.assertIsNone(self._claim_of(msg["id"]), "回收后信重新可见")
        self.assertIn("claim_release", _actions(self.db), "回收也必须留痕")
        self.assertTrue(self.store.audit_events()["ok"])

    def test_fresh_claim_is_not_touched(self):
        msg = self._claimed_message("w-live")
        self.store.sweep()
        self.assertEqual(self._claim_of(msg["id"]), "w-live", "还在干活的窗口的认领不许被动")

    def test_long_headless_run_is_not_touched(self):
        """TTL 必须大于 headless 单轮上限：长任务跑起来时窗口是安静、不轮询的。"""
        self.assertGreater(CLAIM_TTL, 1800, "TTL 要比 headless_timeout 大，否则会误收正在跑的认领")
        msg = self._claimed_message("w-busy")
        silent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1700))
        _age(self.db, "UPDATE deliveries SET claimed_at = ? WHERE message_id = ?", (silent, msg["id"]))
        _age(self.db, "UPDATE windows SET last_seen_at = ? WHERE window_id = 'w-busy'", (silent,))
        self.store.sweep()
        self.assertEqual(self._claim_of(msg["id"]), "w-busy", "安静 28 分钟可能只是任务在跑")

    def test_dead_window_claim_is_released(self):
        msg = self._claimed_message("w-dead")
        _age(self.db, "UPDATE windows SET pid = 99999999, state = 'active' WHERE window_id = 'w-dead'")
        self.store.sweep()
        self.assertIsNone(self._claim_of(msg["id"]))
        self.assertIn("claim_release", _actions(self.db))

    # ── 2) 死流程回收：一次扫干净，且留痕 ────────────────────────────────
    def test_sweep_reclaims_locks_windows_claims_in_one_pass(self):
        msg = self._claimed_message("w-dead2")
        self.store.lock_acquire("cursor", [A], window_id="w-dead2", ttl=600)
        _age(self.db, "UPDATE windows SET pid = 99999999 WHERE window_id = 'w-dead2'")
        out = self.store.sweep()
        self.assertGreaterEqual(out["dead_windows"], 1)
        self.assertEqual(self.store.lock_list()["count"], 0, "死窗口的锁要放掉")
        self.assertIsNone(self._claim_of(msg["id"]))
        actions = _actions(self.db)
        self.assertIn("unlock", actions, "放锁留痕")
        self.assertIn("claim_release", actions, "放认领留痕")

    def test_expired_lock_is_swept(self):
        self.store.lock_acquire("cursor", [A], ttl=600)
        _age(self.db, "UPDATE locks SET expires_at = '2020-01-01T00:00:00Z'")
        out = self.store.sweep()
        self.assertEqual(out["expired_locks"], 1)
        self.assertIn("lock_expire", _actions(self.db))

    def test_sweep_never_touches_semantic_state(self):
        """宪法：程序自动**只做无争议回收**，绝不改任务/案卷状态。"""
        task = self.store.task_add("opencode", "别动我", owner="cursor", workspace=WS, acceptance="x")
        self.store.task_start("cursor", task["id"])
        case = self.store.create_case(agent="cursor", title="别封我", workspace=WS, owner_window="w-x")
        self.store.sweep()
        self.assertEqual(self.store.task_get("opencode", task["id"])["status"], "doing")
        self.assertEqual(self.store.get_case(case["id"])["status"], "open")
        self.assertFalse(self.store.get_case(case["id"])["sealed"])

    # ── 3) 不靠人触发：服务端后台定时器 ─────────────────────────────────
    def test_server_starts_a_background_sweeper(self):
        calls = []
        original = self.store.sweep

        def fake_sweep(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        self.store.sweep = fake_sweep
        thread = server._start_sweeper(self.store, interval=0.05)
        self.assertTrue(thread.daemon, "必须是守护线程，别拖住进程退出")
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(calls), 1, "后台清扫必须自己跑起来（不靠 inbox 触发）")
        thread.stop_event.set()  # 干净停掉，别影响后面的测试
        thread.join(timeout=2)

    def test_sweeper_does_not_use_time_sleep(self):
        """**血的教训**：idle guard 的测试会打桩全局 `time.sleep`；清扫线程若调它，
        就会被假时钟带着空转冲刺，把那条测试的时间轴冲飞（实测心跳间隔被算成 10568s）。"""
        source = (ROOT / "infocenter" / "server.py").read_text(encoding="utf-8")
        body = source[source.index("def _start_sweeper("):source.index("def bind_and_serve(")]
        self.assertIn("stop.wait(", body, "必须用 Event.wait")
        self.assertNotIn("time.sleep(", body, "不许用 time.sleep")

    # ── 4) 看板黄灯：停滞 ────────────────────────────────────────────────
    def test_stalled_flag_for_idle_case(self):
        case = self.store.create_case(agent="cursor", title="放着没人管", workspace=WS)
        self.assertFalse(self.store.get_case(case["id"])["stalled"], "刚建的案子不算停滞")
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - (STALL_HOURS + 2) * 3600))
        _age(self.db, "UPDATE cases SET created_at = ? WHERE id = ?", (stale, case["id"]))
        _age(self.db, "UPDATE case_nodes SET created_at = ? WHERE case_id = ?", (stale, case["id"]))
        got = self.store.get_case(case["id"])
        self.assertTrue(got["stalled"], "很久没人动的未封卷案卷要标黄")
        self.assertGreaterEqual(got["stalled_hours"], STALL_HOURS - 0.1)

    def test_sealed_case_is_not_stalled(self):
        case = self.store.create_case(
            agent="cursor", title="做完了", workspace=WS, acceptance="封卷不算停滞"
        )
        self.store.close_case(case["id"], agent="cursor", note="完事")
        _age(self.db, "UPDATE cases SET created_at = ?, updated_at = ? WHERE id = ?",
             ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z", case["id"]))
        self.assertFalse(self.store.get_case(case["id"])["stalled"], "封卷的案子不该标停滞")

    def test_stalled_shows_up_in_the_case_list(self):
        case = self.store.create_case(agent="cursor", title="停滞的", workspace=WS)
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - (STALL_HOURS + 5) * 3600))
        _age(self.db, "UPDATE cases SET created_at = ? WHERE id = ?", (stale, case["id"]))
        _age(self.db, "UPDATE case_nodes SET created_at = ? WHERE case_id = ?", (stale, case["id"]))
        row = next(r for r in self.store.list_cases() if r["id"] == case["id"])
        self.assertTrue(row["stalled"], "看板（cases 列表）要能看见")


if __name__ == "__main__":
    unittest.main()
