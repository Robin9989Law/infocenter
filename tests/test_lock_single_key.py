"""一把钥匙 + 用完放回去（human 2026-10-15 定稿）。

两句规矩：
1. **每个文件只有一把钥匙** —— 注意是"文件"，不是"路径字符串"：
   `./a.py` / 绝对路径 / 符号链接 / **硬链接（同 inode）** / macOS 大小写不敏感（`A.py` vs `a.py`）
   指向同一个文件时，都只许有一把钥匙。
2. **用完了放回去，别人才能接着用** —— 提交即自动放回（`.githooks/post-commit`），
   且**只放自己的**、放的是**这次提交涉及的文件**；没提交的钥匙不许动。
"""

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.store import MailError, Store


class SingleKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.db = self.home / "mailbox.db"
        self.store = Store(self.db, seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")])
        self.target = self.home / "target.py"
        self.target.write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _paths(self):
        return [row["path"] for row in self.store.lock_list()["locks"]]

    def _resolved(self, path: Path) -> str:
        return str(path.resolve())

    def _assert_only_one_key_for(self, real: Path):
        same = [
            path for path in self._paths()
            if path == str(real) or os.path.samestat(os.stat(path), os.stat(real))
        ]
        self.assertEqual(len(same), 1, f"这个文件只能有一把钥匙，实际有 {len(same)} 把：{self._paths()}")

    def test_relative_and_absolute_are_one_key(self):
        cwd = os.getcwd()
        os.chdir(self.home)
        try:
            self.store.lock_acquire("cursor", ["target.py"], ttl=600)
        finally:
            os.chdir(cwd)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("opencode", [str(self.target)], ttl=600)
        self.assertEqual(ctx.exception.code, "locked", "换个写法就绕过去了 = 有两把钥匙")
        self.assertEqual(ctx.exception.status, 409)
        self._assert_only_one_key_for(self.target)

    def test_symlink_is_one_key(self):
        link = self.home / "link.py"
        os.symlink(self.target, link)
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        with self.assertRaises(MailError):
            self.store.lock_acquire("opencode", [str(link)], ttl=600)
        self._assert_only_one_key_for(self.target)

    def test_hardlink_same_inode_is_one_key(self):
        """硬链接：路径字符串完全不同，inode 相同 ⇒ 还是同一个文件。"""
        hard = self.home / "hard.py"
        os.link(self.target, hard)
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        with self.assertRaises(MailError):
            self.store.lock_acquire("opencode", [str(hard)], ttl=600)
        self._assert_only_one_key_for(self.target)

    def test_acquiring_never_touches_other_files_locks(self):
        """拿自己的钥匙不许误伤别人的锁（这条是给一个真 bug 立的碑）。"""
        other = self.home / "other.py"
        other.write_text("y\n", encoding="utf-8")
        self.store.lock_acquire("cursor", [str(other)], ttl=600)
        self.store.lock_acquire("opencode", [str(self.target)], ttl=600)
        self.store.lock_acquire("opencode", [str(self.target)], ttl=600)  # 再拿一次（续期）
        holders = {row["path"]: row["agent"] for row in self.store.lock_list()["locks"]}
        self.assertEqual(holders.get(str(other.resolve())), "cursor", "别人的钥匙不能被顺手清掉")
        self.assertEqual(len(holders), 2)

    def test_second_spelling_replaces_the_stale_key_only(self):
        """已经存在"同一文件的另一种写法"的钥匙时，拿到新写法 ⇒ 清掉那一把（且只清那一把）。"""
        hard = self.home / "hard2.py"
        os.link(self.target, hard)
        other = self.home / "other2.py"
        other.write_text("z\n", encoding="utf-8")
        self.store.lock_acquire("cursor", [str(hard)], ttl=600)
        self.store.lock_acquire("opencode", [str(other)], ttl=600)
        con = sqlite3.connect(self.db, timeout=10)
        con.execute("UPDATE locks SET agent = 'opencode' WHERE path = ?", (str(hard.resolve()),))
        con.commit()
        con.close()
        self.store.lock_acquire("opencode", [str(self.target)], ttl=600)
        paths = self._paths()
        self.assertNotIn(str(hard.resolve()), paths, "旧写法的钥匙要被换掉")
        self.assertIn(str(other.resolve()), paths, "别的文件的钥匙不许动")
        self._assert_only_one_key_for(self.target)

    def test_fifo_and_missing_path_are_fine(self):
        """特殊文件 / 不存在的路径都不能把闸门搞崩。"""
        missing = self.home / "还没有这个文件.py"
        self.store.lock_acquire("cursor", [str(missing)], ttl=600)
        self.assertEqual(self.store.lock_list()["count"], 1, "不存在的路径也要能锁（写文件前先锁）")


class LockedMeansReadOnlyTest(unittest.TestCase):
    """**锁＝只读**（human 2026-10-15）：能读、不能改；拿钥匙期间才可写；放下钥匙又变回只读。

    落到**文件系统权限位**上（不只是数据库里记一笔）——这样带插件的、不带插件的、`shell > file`、
    别的 agent 的编辑器，**所有写者**都被挡住。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(self.home / "m.db", seeds=[("opencode", "OpenCode", "coordinator")])
        self.file = self.home / "code.py"
        self.file.write_text("x\n", encoding="utf-8")

    def tearDown(self):
        # 只读文件不影响删除（unlink 看父目录权限），但保险起见先放开
        try:
            os.chmod(self.file, 0o644)
        except OSError:
            pass
        self.tmp.cleanup()

    def _mode(self):
        return self.file.stat().st_mode & 0o777

    def test_key_holder_can_write_and_release_makes_it_readonly(self):
        self.store.lock_acquire("opencode", [str(self.file)], ttl=600)
        self.file.write_text("我拿着钥匙，能改\n", encoding="utf-8")  # 不抛异常＝可写
        self.store.lock_release("opencode", [str(self.file)])
        self.assertEqual(self._mode() & 0o222, 0, "放下钥匙 ⇒ 只读（没有任何写位）")
        with self.assertRaises(PermissionError):
            self.file.write_text("偷改\n", encoding="utf-8")
        self.assertEqual(self.file.read_text(encoding="utf-8"), "我拿着钥匙，能改\n", "读一律允许")

    def test_expired_key_also_restores_readonly(self):
        self.store.lock_acquire("opencode", [str(self.file)], ttl=600)
        con = sqlite3.connect(self.home / "m.db", timeout=10)
        con.execute("UPDATE locks SET expires_at = '2020-01-01T00:00:00Z'")
        con.commit()
        con.close()
        self.store.sweep()
        self.assertEqual(self._mode() & 0o222, 0, "过期回收也要把文件变回只读")

    def test_window_leave_restores_readonly(self):
        self.store.touch("opencode", window_id="w-1", workspace=str(self.home))
        self.store.lock_acquire("opencode", [str(self.file)], window_id="w-1", ttl=600)
        self.store.leave_window("w-1", agent="opencode")
        self.assertEqual(self._mode() & 0o222, 0, "会话离场也要把文件变回只读")

    def test_read_only_does_not_block_deleting_or_renaming(self):
        """只读只拦"改内容"，不拦删除/改名（那由目录权限管）——别把工具链搞死。"""
        self.store.lock_acquire("opencode", [str(self.file)], ttl=600)
        self.store.lock_release("opencode", [str(self.file)])
        renamed = self.home / "renamed.py"
        self.file.rename(renamed)
        self.assertTrue(renamed.exists())


class HookPathsResolveToTheSameKeyTest(unittest.TestCase):
    """钩子传的是**仓库根相对路径**；`ic unlock` 必须能把它解析到同一把钥匙上（否则钥匙放不掉）。"""

    def test_relative_path_releases_the_absolute_key(self):
        import tempfile, os
        from infocenter.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["INFOCENTER_QUIET"] = "1"
            store = Store(root / "m.db", seeds=[("opencode", "OpenCode", "coordinator")])
            target = root / "需求文档" / "改了.txt"
            target.parent.mkdir()
            target.write_text("x", encoding="utf-8")
            store.lock_acquire("opencode", [str(target.resolve())], ttl=600)
            self.assertEqual(store.lock_list()["count"], 1)
            cwd = os.getcwd()
            os.chdir(root)
            try:
                store.lock_release("opencode", ["需求文档/改了.txt"])
            finally:
                os.chdir(cwd)
            self.assertEqual(store.lock_list()["count"], 0, "相对路径必须能放掉那把钥匙")


class PostCommitReturnsKeyTest(unittest.TestCase):
    """提交即自动把钥匙放回去（`.githooks/post-commit`）——用真 git 仓库 + 打桩的 `ic` 验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.hooks = Path(self.repo) / ".githooks"
        self.hooks.mkdir()
        source = ROOT / ".githooks" / "post-commit"
        self.assertTrue(source.exists(), "仓里必须有 .githooks/post-commit")
        (self.hooks / "post-commit").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(self.hooks / "post-commit", stat.S_IRWXU)
        # 打桩 ic：把 argv 记到日志，并假装服务是好的（必须在 _git 之前准备好 env）
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "ic.log"
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ.get('PATH','')}"}
        self.env.pop("INFOCENTER_AGENT", None)
        stub = self.bin / "ic"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.log}"\n'
            'case "$1" in status) exit 0 ;; esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(stub, stat.S_IRWXU)
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._git("config", "core.hooksPath", ".githooks")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, env=self.env)

    def _commit(self, name: str):
        # 用真实路径名（含中文），顺带验证 -z 的 NUL 分隔在 hook 里也对
        path = Path(self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "t")
        return path

    def test_returns_key_for_committed_files_only(self):
        self._git("config", "--local", "infocenter.agent", "opencode")
        self._commit("改了的.txt")
        self._commit("另一个.txt")
        lines = [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        unlocks = [line for line in lines if "unlock" in line]
        self.assertTrue(unlocks, f"提交后必须自动放钥匙：{lines}")
        called = unlocks[-1]
        self.assertIn("--as opencode", called)
        self.assertIn("另一个.txt", called, "放的是**这次提交**的文件")
        self.assertNotIn("改了的.txt", called, "上一次提交的文件不该再放一遍")

    def test_only_unlock_runs_never_bare_unlock(self):
        """空输入时不许退化成 `ic unlock`（那就成了"放掉我全部钥匙"）。"""
        self._git("config", "--local", "infocenter.agent", "opencode")
        self._git("commit", "-q", "--allow-empty", "-m", "空提交")
        text = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        for line in text.splitlines():
            if "unlock" in line:
                self.assertNotEqual(line.strip(), "--as opencode unlock", "不许出现无路径的 unlock")
                self.assertTrue(line.replace("--as opencode unlock", "").strip(), f"unlock 必须带路径：{line}")

    def test_chinese_directory_names_survive(self):
        self._git("config", "--local", "infocenter.agent", "opencode")
        self._commit("需求文档/改了.txt")
        unlocks = [line for line in self.log.read_text(encoding="utf-8").splitlines() if "unlock" in line]
        self.assertIn("需求文档/改了.txt", unlocks[-1], "中文路径要原样传给 ic（不能被转义）")

    def test_no_identity_means_no_release(self):
        committed = self._commit("随便.txt")  # 没配 infocenter.agent、也没 INFOCENTER_AGENT
        self.assertFalse(self.log.exists(), "没有身份就不该乱放钥匙")
        self.assertTrue(committed.exists())

    def test_hook_never_blocks_commit(self):
        """post-commit 出任何问题都不该影响提交结果（它跑在提交之后，也不该返回非零）。"""
        self._git("config", "--local", "infocenter.agent", "opencode")
        stub = self.bin / "ic"
        stub.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")  # 连 status 都失败
        os.chmod(stub, stat.S_IRWXU)
        result = self._commit("还是能提交.txt")
        self.assertTrue(result.exists())
        head = self._git("log", "--oneline", "-1").stdout.strip()
        self.assertTrue(head, "提交必须成功落地")


if __name__ == "__main__":
    unittest.main()
