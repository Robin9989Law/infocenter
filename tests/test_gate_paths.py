"""闸门取路径必须"真·正确"（pi 2026-10-15 实测报的 P1：非 ASCII 路径下互斥**静默**失效）。

三个坑都在 `_changed_paths` / 外锚路径这一步，坏起来**不报错**：
1. `git status --porcelain` 默认把中文名转义成 `"\\351\\234\\200…"` ⇒ 解析出不存在的路径；
   lock 与 guard 用同一个假 key、互相自洽、**照样放行**，而互斥已经没了。
2. `--porcelain` 的路径是**仓库根**相对，代码却按 cwd 解析 ⇒ 子目录里跑全是假路径。
3. rename 在文本输出里是 `旧 -> 新`，文件名本身含 ` -> ` 时会切错；`-z` 下是两条 NUL 记录。

本文件在**真 git 仓库**里跑（临时目录），把这三条钉死。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli


class _TempRepo:
    """临时 git 仓库（带中文目录、带空格的目录、含 ` -> ` 的文件名）。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self._run("init", "-q")
        self._run("config", "user.email", "t@t")
        self._run("config", "user.name", "t")
        (self.path / "中文目录").mkdir()
        (self.path / "有 空格").mkdir()
        (self.path / "keep.txt").write_text("1\n", encoding="utf-8")
        (self.path / "中文目录" / "起始.txt").write_text("1\n", encoding="utf-8")
        self._run("add", "-A")
        self._run("commit", "-qm", "init")

    def _run(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.path, capture_output=True, text=True
        )

    def cwd(self):
        return self.path

    def close(self):
        self.tmp.cleanup()


class GatePathTest(unittest.TestCase):
    def setUp(self):
        self.repo = _TempRepo()
        self._old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self.repo.close()

    def _changed(self, cwd=None):
        os.chdir(cwd or self.repo.cwd())
        return cli._changed_paths(subprocess)

    def test_non_ascii_path_is_the_real_path(self):
        target = self.repo.path / "中文目录" / "改.txt"
        target.write_text("x\n", encoding="utf-8")
        got = self._changed(target.parent)
        self.assertIn(str(target.resolve()), got, "中文路径必须解析成**真实**绝对路径")
        self.assertNotIn("\\351", " ".join(got), "不许出现八进制转义")

    def test_space_in_directory_name(self):
        target = self.repo.path / "有 空格" / "改.txt"
        target.write_text("x\n", encoding="utf-8")
        self.assertIn(str(target.resolve()), self._changed())

    def test_same_result_from_repo_root_and_subdir(self):
        (self.repo.path / "中文目录" / "改.txt").write_text("x\n", encoding="utf-8")
        from_root = self._changed()
        from_subdir = self._changed(self.repo.path / "中文目录")
        self.assertEqual(from_root, from_subdir, "换目录跑必须逐字一致（不许按 cwd 拼）")

    def test_rename_with_arrow_in_name(self):
        old = self.repo.path / "中文目录" / "起始.txt"
        new = self.repo.path / "中文目录" / "带 -> 箭头.txt"
        self.repo._run("mv", str(old.relative_to(self.repo.path)), str(new.relative_to(self.repo.path)))
        got = self._changed()
        self.assertIn(str(new.resolve()), got)
        # 旧路径（rename 的第二条记录）不该被当成一个"要锁的文件"
        self.assertNotIn(str(old.resolve()), got)

    def test_deleted_files_are_skipped(self):
        (self.repo.path / "keep.txt").unlink()
        self.assertNotIn(str((self.repo.path / "keep.txt").resolve()), self._changed())

    def test_gate_actually_blocks_on_chinese_path(self):
        """端到端：锁「真实路径」后，另一个人拿同一路径必须 409（闸门真的在）。"""
        target = self.repo.path / "中文目录" / "改.txt"
        target.write_text("x\n", encoding="utf-8")
        paths = self._changed()
        self.assertIn(str(target.resolve()), paths)

        store = cli_test_store()
        store.lock_acquire("opencode", [str(target.resolve())], ttl=600)
        from infocenter.store import MailError

        with self.assertRaises(MailError) as ctx:
            store.lock_acquire("cursor", [str(target.resolve())], ttl=600)
        self.assertEqual(ctx.exception.code, "locked")

    def test_staged_paths_only_see_the_index(self):
        (self.repo.path / "keep.txt").write_text("2\n", encoding="utf-8")
        junk = self.repo.path / "junk.tmp"
        junk.write_text("x\n", encoding="utf-8")  # 未跟踪、未暂存
        os.chdir(self.repo.cwd())
        self.assertEqual(cli._staged_paths(subprocess), [], "没暂存就什么都不该查")
        self.repo._run("add", "keep.txt")
        staged = cli._staged_paths(subprocess)
        self.assertEqual(staged, [str((self.repo.path / "keep.txt").resolve())])
        self.assertNotIn(str(junk.resolve()), staged, "未跟踪文件不该影响提交闸")

    def test_anchor_path_is_relative_to_repo_root(self):
        """外锚文件：在子目录跑也必须落到**仓库根**（否则锚校验被静默跳过）。"""
        os.chdir(self.repo.path / "中文目录")
        got = cli._anchor_path(".infocenter-audit-anchor")
        self.assertEqual(got, (self.repo.path / ".infocenter-audit-anchor").resolve())
        absolute = Path("/tmp/xxx-anchor")
        self.assertEqual(cli._anchor_path(str(absolute)), absolute)


def cli_test_store():
    """一个临时 Store（只为了验闸门真的拦得住跨身份抢锁）。"""
    from infocenter.store import Store

    home = Path(tempfile.mkdtemp())
    os.environ["INFOCENTER_QUIET"] = "1"
    return Store(home / "mailbox.db", seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")])


if __name__ == "__main__":
    unittest.main()
