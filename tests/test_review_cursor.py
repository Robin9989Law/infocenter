"""独立评审：钥匙按文件算，退路不能把自己打崩。

两处必须改（不看别人的结论，对着 store 现码）：

1. `skip_locked=True` 且每条路径都被别人占着时，`wanted` 被滤成空列表，
   接着拼出 `WHERE path IN ()`。这是非法 SQL。`ic lock --changed` 在
   「脏文件全是别人的」时应当回报 skipped，而不是 500。
2. 拿钥匙已经用 inode 把硬链接 / 大小写不同的写法收成一把，放钥匙和提交闸
   却仍按路径字符串对。换个写法：闸门说「没锁」（人就被推向 --no-verify），
   unlock 也删不掉库里那一行（钥匙放不回去）。
"""

import os
import tempfile
import unittest
from pathlib import Path

from infocenter.store import MailError, Store


class SkipLockedEmptyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "m.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")],
        )
        self.held = self.home / "held.py"
        self.held.write_text("h\n", encoding="utf-8")
        self.free = self.home / "free.py"
        self.free.write_text("f\n", encoding="utf-8")

    def tearDown(self):
        for path in (self.held, self.free):
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
        self.tmp.cleanup()

    def test_all_skipped_returns_empty_locks_not_sql_error(self):
        self.store.lock_acquire("cursor", [str(self.held)], ttl=600)
        result = self.store.lock_acquire(
            "opencode", [str(self.held)], skip_locked=True, ttl=600
        )
        self.assertEqual(result["locks"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["agent"], "cursor")
        holders = {row["agent"] for row in self.store.lock_list()["locks"]}
        self.assertEqual(holders, {"cursor"}, "一个都没拿到时，不许把别人的钥匙清掉")

    def test_without_skip_still_conflicts(self):
        self.store.lock_acquire("cursor", [str(self.held)], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("opencode", [str(self.held)], ttl=600)
        self.assertEqual(ctx.exception.code, "locked")

    def test_skip_keeps_the_free_file(self):
        self.store.lock_acquire("cursor", [str(self.held)], ttl=600)
        result = self.store.lock_acquire(
            "opencode", [str(self.held), str(self.free)], skip_locked=True, ttl=600
        )
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual([row["path"] for row in result["locks"]], [str(self.free.resolve())])
        self.free.write_text("拿到了\n", encoding="utf-8")


class SameFileGateAndReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "m.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", ""), ("kimi", "Kimi", "")],
        )
        self.target = self.home / "target.py"
        self.target.write_text("x\n", encoding="utf-8")
        self.hard = self.home / "hard.py"
        os.link(self.target, self.hard)
        self.other = self.home / "other.py"
        self.other.write_text("y\n", encoding="utf-8")

    def tearDown(self):
        for path in (self.target, self.hard, self.other):
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
        self.tmp.cleanup()

    def test_guard_sees_the_key_held_under_another_name(self):
        self.store.lock_acquire("cursor", [str(self.hard)], ttl=600)
        mine = self.store.guard_changes("cursor", [str(self.target)])
        self.assertTrue(mine["ok"], mine)
        self.assertEqual(mine["covered"][0]["holder"], "cursor")
        self.assertEqual(mine["unlocked"], [])
        foreign = self.store.guard_changes("kimi", [str(self.target)])
        self.assertFalse(foreign["ok"])
        self.assertEqual(foreign["unlocked"], [], "别人拿着同一文件的钥匙，不能报成「没锁」")
        self.assertEqual(foreign["locked_by_others"][0]["holder"], "cursor")

    def test_release_by_the_other_name_drops_that_key_only(self):
        self.store.lock_acquire("cursor", [str(self.hard)], ttl=600)
        self.store.lock_acquire("kimi", [str(self.other)], ttl=600)
        released = self.store.lock_release("cursor", [str(self.target)])
        self.assertEqual(released["released"], [str(self.hard.resolve())])
        left = {row["path"]: row["agent"] for row in self.store.lock_list()["locks"]}
        self.assertNotIn(str(self.hard.resolve()), left)
        self.assertEqual(left.get(str(self.other.resolve())), "kimi")
        self.assertEqual(self.target.stat().st_mode & 0o222, 0, "放下钥匙后文件只读")
        with self.assertRaises(PermissionError):
            self.target.write_text("偷改\n", encoding="utf-8")

    def test_case_spelling_is_the_same_key_when_the_fs_folds_case(self):
        folded = self.target.with_name("TARGET.py")
        try:
            same = os.path.samestat(self.target.stat(), folded.stat())
        except OSError:
            self.skipTest("这个文件系统区分大小写")
        if not same:
            self.skipTest("这个文件系统区分大小写")
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        report = self.store.guard_changes("cursor", [str(folded)])
        self.assertTrue(report["ok"], report)
        self.store.lock_release("cursor", [str(folded)])
        self.assertEqual(self.store.lock_list()["count"], 0)


if __name__ == "__main__":
    unittest.main()
