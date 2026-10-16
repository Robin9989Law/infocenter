"""「钥匙不接别人没干完的活」：拿钥匙前就有的未提交改动，要在两个当口说出来。

现场（2026-10-15）：cursor 改了 `CHANGELOG.md` 里描述自己那一行，但**只提交了代码**，
那行字留在盘上没提交；钥匙还了/过期了，文件变成"干净的钥匙 + 脏的内容"。
后面谁拿到钥匙再提交，git 提交的是**整个文件的当前内容** ⇒ 那行字就算在后提交的人名下
（提交信息是后者的，作者也看不出来是谁——本仓 git 作者全场是同一个用户）。

这里只做**看板那一路**该做的事：拿钥匙时把信息摆到眼前（黄灯 + 登记），提交时再提醒一句。
**不拦**：判断"这摊活是不是我的"要靠人，机器只负责别让它悄悄发生。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import _preexisting_dirt
from infocenter.store import Store


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )


class StoreDirtyAtLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")],
        )
        self.target = self.home / "CHANGELOG.md"
        self.target.write_text("+ 原来就有的一行\n", encoding="utf-8")
        # 库里的路径是规整过的（macOS 上 /var → /private/var）：比对要用同一口径
        self.resolved = str(self.target.resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def test_拿钥匙时登记_提交闸再提醒(self):
        res = self.store.lock_acquire(
            "cursor", [str(self.target)], dirty={str(self.target): "+1/-1：ic queue 往只读 TODO 写……"}
        )
        # ① 拿钥匙那一刻就登记下来（并回给命令行，好当场说一句）
        self.assertEqual(res["dirty_at_lock"], {self.resolved: "+1/-1：ic queue 往只读 TODO 写……"})
        self.assertEqual(res["locks"][0]["dirty_at_lock"], "+1/-1：ic queue 往只读 TODO 写……")

        # ② 提交那一刻再提醒：这个文件里有"拿钥匙前就有"的改动
        guard = self.store.guard_changes("cursor", [str(self.target)])
        self.assertTrue(guard["ok"])
        self.assertEqual(len(guard["inherited"]), 1)
        self.assertEqual(guard["inherited"][0]["path"], self.resolved)
        self.assertIn("ic queue", guard["inherited"][0]["dirty"])

    def test_续期不会抹掉已登记的脏改动(self):
        """kimi 2026-10-15 提示级：`--changed` 续期带进来的 dirt 是**空的**
        （它只嗅命令行点名的路径），旧代码照样覆盖 `dirty_at_lock` ⇒
        登记过的脏改动凭空消失，提交那一刻的提醒也就没了。
        """
        first = self.store.lock_acquire(
            "cursor", [str(self.target)], dirty={str(self.target): "+1/-0：别人留下的那行"}
        )
        self.assertTrue(first["dirty_at_lock"])
        os.utime(self.target, None)  # 盘上有痕迹才给第二把；这一条测的是脏登记不被抹掉
        again = self.store.lock_acquire("cursor", [str(self.target)], dirty={})  # 续期，这次没嗅到
        self.assertEqual(again["dirty_at_lock"][self.resolved], "+1/-0：别人留下的那行")
        self.assertEqual(
            self.store.lock_list()["locks"][0]["dirty_at_lock"], "+1/-0：别人留下的那行"
        )

    def test_干净拿钥匙就不吭声(self):
        self.store.lock_acquire("cursor", [str(self.target)])
        guard = self.store.guard_changes("cursor", [str(self.target)])
        self.assertEqual(guard["inherited"], [])

    def test_提醒不跨文件串味(self):
        other = self.home / "README.md"
        other.write_text("x\n", encoding="utf-8")
        self.store.lock_acquire(
            "cursor", [str(self.target), str(other)], dirty={str(self.target): "+2/-1：别人留的一行"}
        )
        guard = self.store.guard_changes("cursor", [str(self.target), str(other)])
        self.assertEqual([item["path"] for item in guard["inherited"]], [self.resolved])

    def test_老库没有这一列也能开(self):
        """老 mailbox.db 的 locks 表没有 dirty_at_lock：开场要自己补上，不能炸。"""
        import sqlite3

        old = self.home / "old.db"
        conn = sqlite3.connect(old)
        conn.execute(
            """
            CREATE TABLE locks (
                path TEXT PRIMARY KEY, agent TEXT NOT NULL, window_id TEXT, task_id TEXT,
                note TEXT NOT NULL DEFAULT '', acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()
        store = Store(old, seeds=[("cursor", "Cursor", "")])
        res = store.lock_acquire("cursor", [str(self.target)], dirty={str(self.target): "+1/-0：x"})
        self.assertEqual(res["dirty_at_lock"][self.resolved], "+1/-0：x")


class PreexistingDirtTest(unittest.TestCase):
    """命令行那半：只认"点名的路径"，且只认 git 里的改动（新文件/非仓库一律跳过）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init", "-q")
        tracked = self.repo / "a.py"
        tracked.write_text("print(1)\n", encoding="utf-8")
        _git(self.repo, "add", "a.py")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.tracked = tracked

    def tearDown(self):
        self.tmp.cleanup()

    def test_干净文件不吭声(self):
        self.assertEqual(_preexisting_dirt(subprocess, [str(self.tracked)]), {})

    def test_改了没提交_说清改了多少和改了哪一行(self):
        self.tracked.write_text("print(1)\nprint(2)  # 别人留的一行\n", encoding="utf-8")
        dirt = _preexisting_dirt(subprocess, [str(self.tracked)])
        self.assertIn(str(self.tracked), dirt)
        self.assertTrue(dirt[str(self.tracked)].startswith("+1/-0"))
        self.assertIn("别人留的一行", dirt[str(self.tracked)])

    def test_新文件不算_上一手留下的(self):
        fresh = self.repo / "new.py"
        fresh.write_text("x\n", encoding="utf-8")
        self.assertEqual(_preexisting_dirt(subprocess, [str(fresh)]), {})

    def test_不是_git_仓库就闭嘴(self):
        outside = Path(self.tmp.name).parent / "not-a-repo-4f79332a"
        outside.mkdir(exist_ok=True)
        f = outside / "b.py"
        f.write_text("y\n", encoding="utf-8")
        try:
            self.assertEqual(_preexisting_dirt(subprocess, [str(f)]), {})
        finally:
            f.unlink()
            outside.rmdir()

    def test_不给路径就不查(self):
        self.assertEqual(_preexisting_dirt(subprocess, None), {})

    def _dirt_from(self, cwd: Path, rel: str) -> dict:
        """站到 `cwd` 下、按**相对路径**问一句（命令行本来就常这么写）。"""
        here = os.getcwd()
        try:
            os.chdir(cwd)
            return _preexisting_dirt(subprocess, [rel])
        finally:
            os.chdir(here)

    def test_相对路径也认得出脏改动(self):
        """kimi 2026-10-15 报的洞（已复现）：`ic lock a.py` 这种相对路径，
        配上 `-C <文件所在目录>` 会被 git 当成 `a.py/a.py`（路径错位）⇒ rc=0、输出为空
        ⇒ 「本来就有脏改动」被当成「干净」，黄灯静默不亮、也不登记。
        """
        self.tracked.write_text("print(1)\nprint(2)  # 别人留的一行\n", encoding="utf-8")
        dirt = self._dirt_from(self.repo, "a.py")
        self.assertIn("a.py", dirt)
        self.assertTrue(dirt["a.py"].startswith("+1/-0"))
        self.assertIn("别人留的一行", dirt["a.py"])

    def test_带目录的相对路径也认得出(self):
        """`ic lock infocenter/cli.py` 那种写法：错位会变成 `infocenter/infocenter/cli.py`。"""
        sub = self.repo / "pkg"
        sub.mkdir()
        f = sub / "b.py"
        f.write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "pkg/b.py")
        _git(self.repo, "commit", "-q", "-m", "add")
        f.write_text("x = 1\ny = 2  # 别人留的\n", encoding="utf-8")
        dirt = self._dirt_from(self.repo, "pkg/b.py")
        self.assertIn("pkg/b.py", dirt)
        self.assertTrue(dirt["pkg/b.py"].startswith("+1/-0"))

    def test_从别的目录跑也认得出(self):
        """站在仓库外面也一样（`-C` 就是为这个用的）：路径先转绝对，再问 git。"""
        self.tracked.write_text("print(1)\nprint(4)  # 别人留的一行\n", encoding="utf-8")
        outside = Path(self.tmp.name).parent
        rel = os.path.relpath(self.tracked, outside)
        dirt = self._dirt_from(outside, rel)
        self.assertIn(rel, dirt)


if __name__ == "__main__":
    unittest.main()
