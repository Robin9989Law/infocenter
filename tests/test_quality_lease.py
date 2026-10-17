"""锁的第二把租约，和案卷办结的验收门槛。

口径（派单 m1a143d6729f42ebff）：
- 盘上自 acquired_at 起没写过，就不给第二把租约（409 no_progress）。已有锁不动。
- stay 可以在没写过时再拿一会儿：acquired_at 不刷新，最迟到拿锁后 2×LOCK_TTL。
- 锁列表加 no_progress（黄灯）和 orphan_queue（有人想改、现在没人拿钥匙）。
- 办结时 acceptance 为空 ⇒ 409 no_acceptance；中止不拦。

反例（防假绿）：把 `_lock_has_progress` 短路成恒真，T1 的 no_progress 断言必须红。
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.httpapi import handle
from infocenter.store import LOCK_TTL, MailError, Store, _lock_stay_cap


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeaseQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        os.environ.pop("INFOCENTER_WINDOW", None)
        self.db = self.home / "mailbox.db"
        self.store = Store(
            self.db,
            seeds=[
                ("opencode", "OpenCode", "coordinator"),
                ("cursor", "Cursor", ""),
                ("grok", "Grok", ""),
                ("kimi", "Kimi", ""),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name: str) -> str:
        path = self.home / name
        path.write_text("x\n", encoding="utf-8")
        return str(path.resolve())

    def _age(self, path: str, seconds_ago: float) -> None:
        moment = time.time() - seconds_ago
        os.utime(path, (moment, moment))

    def _set_lock(self, path: str, *, acquired_ago: float | None = None, expires_in: float | None = None) -> None:
        now = datetime.now(timezone.utc)
        sets, params = [], []
        if acquired_ago is not None:
            sets.append("acquired_at = ?")
            params.append(_iso(now - timedelta(seconds=acquired_ago)))
        if expires_in is not None:
            sets.append("expires_at = ?")
            params.append(_iso(now + timedelta(seconds=expires_in)))
        params.append(path)
        con = sqlite3.connect(self.db, timeout=10)
        try:
            con.execute(f"UPDATE locks SET {', '.join(sets)} WHERE path = ?", params)
            con.commit()
        finally:
            con.close()

    def _lock_details(self, path: str) -> list[dict]:
        con = sqlite3.connect(self.db, timeout=10)
        try:
            rows = con.execute(
                "SELECT detail FROM task_events WHERE action = 'lock' ORDER BY seq"
            ).fetchall()
        finally:
            con.close()
        out = []
        for (raw,) in rows:
            detail = json.loads(raw)
            if detail.get("path") == path:
                out.append(detail)
        return out

    def test_t1_no_second_lease_without_progress(self):
        """拿锁不写不能续；写过能续且刷新 acquired_at；stay 不刷新并在链上做记号；到 1 小时上限就停。"""
        path = self._file("a.py")
        self._age(path, 30)
        first = self.store.lock_acquire("cursor", [path], ttl=600)
        acquired = first["locks"][0]["acquired_at"]
        expires = first["locks"][0]["expires_at"]
        stored = first["locks"][0]["path"]

        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [stored], ttl=600)
        self.assertEqual(ctx.exception.code, "no_progress")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("别硬写", ctx.exception.message)
        self.assertIn("TODO", ctx.exception.message)
        held = self.store.lock_list()["locks"][0]
        self.assertEqual(held["acquired_at"], acquired, "拒续期不许动已有锁")
        self.assertEqual(held["expires_at"], expires)
        self.assertEqual(self.store.lock_list()["count"], 1)

        # 秒级、相等算有（宁松勿误杀）：mtime 落在拿锁那一秒，续期必须成功，不能报 no_progress。
        moment = datetime.strptime(acquired, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        os.utime(stored, (moment.timestamp(), moment.timestamp()))
        self.store.lock_acquire("cursor", [stored], ttl=600)
        self.assertFalse(any(item.get("no_progress_stay") for item in self._lock_details(stored)))

        # 写在拿锁之后：acquired_at 要刷新。先把拿锁时刻拨早几秒，避免落在同一秒上看不出刷新。
        self._set_lock(stored, acquired_ago=5, expires_in=60)
        self._age(stored, 5)
        os.utime(stored, None)
        renewed = self.store.lock_acquire("cursor", [stored], ttl=600)
        self.assertGreater(renewed["locks"][0]["acquired_at"], _iso(datetime.now(timezone.utc) - timedelta(seconds=5)))

        stayed_path = self._file("b.py")
        self._age(stayed_path, 30)
        stayed_first = self.store.lock_acquire("cursor", [stayed_path], ttl=600)
        stayed_acquired = stayed_first["locks"][0]["acquired_at"]
        stayed_key = stayed_first["locks"][0]["path"]
        # 同一秒里 now+ttl 和原到期看不出延长：先把到期拨近，stay 必须把它推回去，且不刷新 acquired_at。
        self._set_lock(stayed_key, expires_in=30)
        short = self.store.lock_list()["locks"]
        short_expires = next(row["expires_at"] for row in short if row["path"] == stayed_key)
        stayed = self.store.lock_acquire("cursor", [stayed_key], stay=True, ttl=600)
        self.assertEqual(stayed["locks"][0]["acquired_at"], stayed_acquired)
        self.assertGreater(stayed["locks"][0]["expires_at"], short_expires)
        self.assertLessEqual(stayed["locks"][0]["expires_at"], _lock_stay_cap(stayed_acquired))
        marks = self._lock_details(stayed_key)
        self.assertTrue(any(item.get("no_progress_stay") is True for item in marks))

        self._set_lock(stayed_key, acquired_ago=2 * LOCK_TTL + 5, expires_in=500)
        self._age(stayed_path, 2 * LOCK_TTL + 60)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [stayed_key], stay=True)
        self.assertEqual(ctx.exception.code, "stay_exhausted")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("延过一次了，要再拿请先 `ic unlock` 放一次再拿", ctx.exception.message)
        self.assertEqual(self.store.lock_list()["count"], 2, "到上限也不许把锁放掉")

    def test_t1_one_stale_path_rejects_the_whole_batch(self):
        fresh = self._file("fresh.py")
        stale = self._file("stale.py")
        self._age(stale, 30)
        self.store.lock_acquire("cursor", [fresh, stale], ttl=600)
        os.utime(fresh, None)
        before = {row["path"]: row["acquired_at"] for row in self.store.lock_list()["locks"]}
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [fresh, stale], ttl=600)
        self.assertEqual(ctx.exception.code, "no_progress")
        after = {row["path"]: row["acquired_at"] for row in self.store.lock_list()["locks"]}
        self.assertEqual(after, before, "整笔拒：有进度的那条也不许被顺手续上")

    def test_t2_rejected_renewal_expires_then_someone_else_can_take_it(self):
        path = self._file("c.py")
        self._age(path, 30)
        got = self.store.lock_acquire("cursor", [path], ttl=600)
        stored = got["locks"][0]["path"]
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [stored])
        self.assertEqual(ctx.exception.code, "no_progress")
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("grok", [stored])
        self.assertEqual(ctx.exception.code, "locked")
        self.assertEqual(ctx.exception.status, 409)

        self._set_lock(stored, expires_in=-30)
        taken = self.store.lock_acquire("grok", [stored], ttl=600)
        self.assertEqual(taken["locks"][0]["agent"], "grok")
        con = sqlite3.connect(self.db, timeout=10)
        try:
            expired = con.execute(
                "SELECT detail FROM task_events WHERE action = 'lock_expire'"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(expired), 1)
        self.assertEqual(json.loads(expired[0][0]).get("path"), stored)

    def test_t3_no_progress_and_orphan_queue_match_the_rows(self):
        path = self._file("d.py")
        self._age(path, 10)
        held = self.store.lock_acquire("cursor", [path], ttl=LOCK_TTL)
        stored = held["locks"][0]["path"]
        self.assertFalse(self.store.lock_list()["locks"][0]["no_progress"], "刚拿到的锁不亮黄灯")

        self._set_lock(stored, acquired_ago=LOCK_TTL + 30, expires_in=600)
        self._age(path, LOCK_TTL + 90)
        yellow = self.store.lock_list()["locks"][0]
        self.assertTrue(yellow["no_progress"])
        self.assertEqual(yellow["path"], stored)

        os.utime(path, None)
        self.assertFalse(self.store.lock_list()["locks"][0]["no_progress"], "盘上写过，黄灯灭")

        orphan_path = self._file("e.py")
        first = self.store.queue_change("grok", orphan_path, note="先记一笔")
        second = self.store.queue_change("kimi", orphan_path, note="后记一笔")
        orphans = self.store.lock_list()["orphan_queue"]
        match = [item for item in orphans if item["path"] == first["path"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["count"], 2)
        self.assertEqual(match[0]["agent"], "grok", "最早的提议人")
        self.assertTrue(match[0]["at"])
        self.assertTrue(str(match[0]["at_beijing"]).endswith("+0800"))
        self.assertNotEqual(match[0]["agent"], second["agent"])

        self.store.lock_acquire("cursor", [orphan_path])
        self.assertFalse(
            any(item["path"] == first["path"] for item in self.store.lock_list()["orphan_queue"])
        )
        self.store.queue_change("kimi", stored, note="钥匙还在别人手里")
        self.assertFalse(
            any(item["path"] == stored for item in self.store.lock_list()["orphan_queue"]),
            "有活锁的提议不是孤儿",
        )

    def test_t4_close_needs_acceptance_abort_does_not(self):
        case = self.store.create_case(agent="cursor", title="没写怎样算完", owner_agent="cursor")
        with self.assertRaises(MailError) as ctx:
            self.store.close_case(case["id"], agent="cursor", note="办结")
        self.assertEqual(ctx.exception.code, "no_acceptance")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("--acceptance", ctx.exception.message)
        left = self.store.get_case(case["id"])
        self.assertNotEqual(left["status"], "done")
        self.assertFalse(left["sealed"])

        aborted = self.store.abort_case(case["id"], agent="cursor", note="不做了")
        self.assertEqual(aborted["status"], "aborted")
        self.assertTrue(aborted["sealed"])

        done = self.store.create_case(
            agent="cursor", title="写了才办结", owner_agent="cursor", acceptance="测试绿"
        )
        closed = self.store.close_case(done["id"], agent="cursor", note="办结")
        self.assertEqual(closed["status"], "done")
        self.assertTrue(closed["sealed"])

    def test_http_stay_and_no_progress(self):
        path = self._file("http.py")
        self._age(path, 30)
        body = json.dumps({"paths": [path], "ttl": 600}).encode()
        status, payload = handle("POST", "/v1/locks", {"X-Agent": "cursor"}, body, self.store)
        self.assertEqual(status, 201)
        stored = payload["locks"][0]["path"]
        acquired = payload["locks"][0]["acquired_at"]
        with self.assertRaises(MailError) as ctx:
            handle(
                "POST", "/v1/locks", {"X-Agent": "cursor"},
                json.dumps({"paths": [stored]}).encode(), self.store,
            )
        self.assertEqual(ctx.exception.code, "no_progress")
        status, stayed = handle(
            "POST", "/v1/locks", {"X-Agent": "cursor"},
            json.dumps({"paths": [stored], "stay": True}).encode(), self.store,
        )
        self.assertEqual(status, 201)
        self.assertEqual(stayed["locks"][0]["acquired_at"], acquired)


if __name__ == "__main__":
    unittest.main()
