"""拿到的钥匙必须**真的能写**（grok 实测踩到的洞）。

现场：grok 拿着 `store.py` 的钥匙，文件却还是 444，得**再锁一次**才成——它只能自己猜。
根因是一类"静默失败"：
1. `_chmod_writable` 尽力而为、把 `OSError` 悄悄吞掉 ⇒ 权限位没落地，谁都不知道；
2. 放钥匙时按**这一行的路径**盲写 444 ⇒ 同一个文件若还写着别人的活锁（换种写法/硬链接），
   会把那个持有者正拿着的写权限当场打掉。

现在两条都立规矩：
- 拿钥匙时**复核**：做不到可写就整笔回滚、当场报 `lock_not_writable`（钥匙要么能用，要么明说不能用）；
- 放钥匙时先看**这个文件还有没有活锁**：有 ⇒ 保持可写；没有 ⇒ 才是只读态。
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import MailError, Store


def _mode(path) -> int:
    return os.stat(str(path)).st_mode & 0o7777


class LockKeyWritableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db = self.home / "mailbox.db"
        self.store = Store(
            self.db,
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", ""), ("grok", "Grok", "")],
        )
        self.target = self.home / "target.py"
        self.target.write_text("x\n", encoding="utf-8")
        os.chmod(self.target, 0o444)  # 「锁＝只读」的静置态

    def tearDown(self):
        self.tmp.cleanup()

    def _paths(self):
        return [row["path"] for row in self.store.lock_list()["locks"]]

    def _insert_row(self, path, agent, seconds=600):
        """直接写一行锁（模拟历史遗留 / 另一种写法留下的行，绕过拿锁时的同文件清理）。"""
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        acquired = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT OR REPLACE INTO locks (path, agent, window_id, task_id, note, acquired_at, expires_at) "
                "VALUES (?, ?, '', '', '', ?, ?)",
                (str(path), agent, acquired, expires),
            )
            con.commit()
        finally:
            con.close()

    # ---- 拿钥匙必须真的能写 -------------------------------------------------

    def test_lock_makes_readonly_file_writable_and_release_restores_readonly(self):
        self.assertEqual(_mode(self.target), 0o444)
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self.assertEqual(_mode(self.target), 0o644, "拿了钥匙就该能写")
        self.store.lock_release("cursor", [str(self.target)])
        self.assertEqual(_mode(self.target), 0o444, "放了钥匙就该回到只读")

    def test_lock_on_not_yet_created_file_is_allowed(self):
        """先锁、后建是合法的：文件还不存在时不该报错。"""
        fresh = self.home / "brand-new.py"
        res = self.store.lock_acquire("cursor", [str(fresh)], ttl=600)
        self.assertEqual(len(res["locks"]), 1)
        got = res["locks"][0]["path"]
        self.assertEqual(os.path.realpath(got), os.path.realpath(str(fresh)))

    def test_chmod_failure_is_reported_loudly_and_lock_rolls_back(self):
        """chmod 失败（ACL / 只读挂载 / 权限不足）：不再静默——整笔回滚 + 说清原因。"""
        real_chmod = os.chmod
        target = str(self.target)

        def fake_chmod(path, mode, *args, **kwargs):
            if os.path.realpath(str(path)) == os.path.realpath(target):
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch("infocenter.store.os.chmod", side_effect=fake_chmod):
            with self.assertRaises(MailError) as ctx:
                self.store.lock_acquire("cursor", [target], ttl=600)

        self.assertEqual(ctx.exception.code, "lock_not_writable")
        self.assertIn(target, ctx.exception.message)
        self.assertIn("改成可写失败", ctx.exception.message)
        self.assertEqual(self._paths(), [], "没拿到能用的钥匙，库里就不该留下这一行")
        self.assertEqual(_mode(self.target), 0o444, "权限位不该被动过")
        # 回滚干净了：环境正常后立刻能重新拿
        self.store.lock_acquire("cursor", [target], ttl=600)
        self.assertEqual(_mode(self.target), 0o644)

    def _fail_chmod_for(self, doomed: Path):
        """让某个路径的 chmod 一定失败（ACL / 只读挂载 / 权限不足那类），别的照常。"""
        real_chmod = os.chmod
        bad = os.path.realpath(str(doomed))

        def fake_chmod(path, mode, *args, **kwargs):
            if os.path.realpath(str(path)) == bad:
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        return mock.patch("infocenter.store.os.chmod", side_effect=fake_chmod)

    def test_multi_lock_failure_puts_back_the_files_already_made_writable(self):
        """kimi 2026-10-15 报的洞：一次拿多个文件、中途失败时，
        前面**已经改成可写**的文件必须收回去——否则等于「没拿到钥匙却能写」，
        锁＝只读当场破功，而且没人知道。"""
        second = self.home / "second.py"
        second.write_text("y\n", encoding="utf-8")
        os.chmod(second, 0o444)

        with self._fail_chmod_for(second):
            with self.assertRaises(MailError) as ctx:
                self.store.lock_acquire("cursor", [str(self.target), str(second)], ttl=600)

        self.assertEqual(ctx.exception.code, "lock_not_writable")
        self.assertEqual(self._paths(), [], "整笔回滚：一把钥匙都不该留下")
        self.assertEqual(_mode(self.target), 0o444, "前面已经改成可写的那个要收回去")
        self.assertEqual(_mode(second), 0o444)

    def test_failed_renewal_keeps_my_own_key_writable(self):
        """续期时中途失败：我已经拿着钥匙的那个文件**不能**被收回成只读（我的锁还在）。"""
        second = self.home / "second.py"
        second.write_text("y\n", encoding="utf-8")
        os.chmod(second, 0o444)
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self.assertEqual(_mode(self.target), 0o644)

        with self._fail_chmod_for(second):
            with self.assertRaises(MailError):
                self.store.lock_acquire("cursor", [str(self.target), str(second)], ttl=600)

        self.assertEqual(
            [os.path.realpath(p) for p in self._paths()],
            [os.path.realpath(str(self.target))],
            "回滚后我还是只拿着原来那把",
        )
        self.assertEqual(_mode(self.target), 0o644, "我的钥匙还在 ⇒ 我的文件要保持可写")

    def test_release_that_cannot_go_readonly_is_reported(self):
        """放钥匙时若没能改回只读（ACL / 只读挂载 / 别的进程又改回去），必须说出来——
        旧版在这里静默吞 `OSError`（kimi 2026-10-15 提示级），「锁＝只读」只剩嘴上说说。"""
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self.assertEqual(_mode(self.target), 0o644)
        real_chmod = os.chmod
        bad = os.path.realpath(str(self.target))

        def fake_chmod(path, mode, *args, **kwargs):
            # 只挡"改回只读"那一下（去掉写位），别影响拿钥匙时的"改成可写"
            if os.path.realpath(str(path)) == bad and not (mode & 0o200):
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch("infocenter.store.os.chmod", side_effect=fake_chmod):
            res = self.store.lock_release("cursor", [str(self.target)])

        self.assertEqual(self._paths(), [], "钥匙照放（库里那行清掉了）")
        self.assertIn(
            bad, [os.path.realpath(p) for p in res["stuck"]], "改不回去要说出来，别静默"
        )
        self.assertIn("改回只读失败", list(res["stuck"].values())[0])

    def test_chmod_that_silently_does_not_land_is_caught(self):
        """grok 的症状本体：chmod「成功」了但权限位没落地，文件仍是 444——复核当场抓出来。"""
        real_chmod = os.chmod
        target = str(self.target)

        def noop_chmod(path, mode, *args, **kwargs):
            if os.path.realpath(str(path)) == os.path.realpath(target):
                return None  # 假装成功、其实没改（静默失败）
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch("infocenter.store.os.chmod", side_effect=noop_chmod):
            with self.assertRaises(MailError) as ctx:
                self.store.lock_acquire("grok", [target], ttl=600)

        self.assertEqual(ctx.exception.code, "lock_not_writable")
        self.assertIn("权限位没落地", ctx.exception.message)
        self.assertEqual(self._paths(), [], "钥匙没能用 ⇒ 不留行")
        self.assertEqual(_mode(self.target), 0o444)

    def test_relock_heals_file_that_was_made_readonly_behind_holder(self):
        """「再锁一次才成」现在是有保证的兜底：拿钥匙的人重锁一定把权限位按回来。"""
        self.store.lock_acquire("grok", [str(self.target)], ttl=600)
        os.chmod(self.target, 0o444)  # 别人/别的机制在背后改回只读
        # chmod 不改 mtime。盘上没写过时，普通再拿会被 no_progress 拒绝；stay 才能把权限位按回来。
        self.store.lock_acquire("grok", [str(self.target)], ttl=600, stay=True)
        self.assertEqual(_mode(self.target), 0o644)

    # ---- 放钥匙不能打掉同文件别的持有者 ------------------------------------

    def test_release_keeps_writable_while_same_file_still_has_a_live_key(self):
        """同一个文件（另一种写法/硬链接）还有活锁 ⇒ 放掉这一行**不能**把它变回只读。"""
        link = self.home / "target-link.py"
        os.link(self.target, link)  # 硬链接：同 inode、路径不同

        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self.assertEqual(_mode(self.target), 0o644)
        self._insert_row(link, "grok")  # 库里另有一样「同文件」的行（历史遗留形态）

        self.store.lock_release("cursor", [str(self.target)])
        self.assertEqual(len(self._paths()), 1, "grok 那一行还在")
        self.assertEqual(_mode(link), 0o644, "还有人拿着这个文件 ⇒ 必须保持可写")
        self.assertEqual(_mode(self.target), 0o644)

        self.store.lock_release("grok", [str(link)])
        self.assertEqual(self._paths(), [])
        self.assertEqual(_mode(self.target), 0o444, "没人拿着了 ⇒ 才回到只读")

    def test_expired_lock_cleanup_keeps_writable_while_same_file_still_held(self):
        """过期回收（自动动作）同样按「文件」判断，不按这一行的路径判断。"""
        link = self.home / "target-link2.py"
        os.link(self.target, link)

        from infocenter.store import _locks_sweep

        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self._insert_row(link, "grok", seconds=-30)  # 过期的一行（另一种写法）
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            swept = _locks_sweep(con, now)
            con.commit()
        finally:
            con.close()
        self.assertEqual(swept, 1, "只回收过期那一行")
        self.assertEqual([os.path.realpath(p) for p in self._paths()],
                         [os.path.realpath(str(self.target))], "cursor 的活锁还在")
        self.assertEqual(_mode(self.target), 0o644, "还有人拿着这个文件 ⇒ 回收不能打掉他的写权限")

        # 连 cursor 的行也过期：回收后没人拿着 ⇒ 才回到只读
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            con.execute("UPDATE locks SET expires_at = ? WHERE agent = 'cursor'", (now,))
            swept = _locks_sweep(con, now)
            con.commit()
        finally:
            con.close()
        self.assertEqual(swept, 1)
        self.assertEqual(self._paths(), [])
        self.assertEqual(_mode(self.target), 0o444)


if __name__ == "__main__":
    unittest.main()
