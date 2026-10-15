"""`ic status` 上的黄灯：不拦，但要把该看见的摆到日常视角里。

第一盏（2026-10-16 加）：**空钥匙**——盘上有改动、却没有任何未过期写锁覆盖它。
实测两次踩到：pi 写了文档/测试、放了钥匙、**忘了提交** ⇒ 那几行躺在工作区，别人又不能替你提交
（"不许把别人的未提交改动扫进自己的提交"），收口就卡住。它不是错误（可能只是还没提交），
所以只提示不拦。

口径：读不到 git（不是仓库）/ 读不到锁（服务不在）⇒ **一个字都不说**（灯不能挡住看状态）。
"""

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import Store


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )


class UnlockedDirtyLightTest(unittest.TestCase):
    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.store = Store(self.home / "mailbox.db", seeds=[("opencode", "OpenCode", "coordinator")])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        # 一个真的 git 仓库：`_changed_paths` 是按 **cwd** 问 git 的
        self.repo = self.base / "proj"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        self.tracked = self.repo / "a.py"
        self.tracked.write_text("print(1)\n", encoding="utf-8")
        _git(self.repo, "add", "a.py")
        _git(self.repo, "commit", "-qm", "init")
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _status(self, as_json=False) -> str:
        os.chdir(self.repo)
        args = argparse.Namespace(url=self.url, json=as_json)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli._status(args, self.home, "opencode")
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_有改动没人持钥匙_亮灯(self):
        self.tracked.write_text("print(1)\nprint(2)  # 谁留的半截活\n", encoding="utf-8")
        out = self._status()
        self.assertIn("盘上有改动没人持钥匙", out)
        self.assertIn("a.py", out)
        self.assertIn("只提示，不拦", out)

    def test_拿了钥匙就不亮(self):
        self.tracked.write_text("print(1)\nprint(2)\n", encoding="utf-8")
        self.store.lock_acquire("opencode", [str(self.tracked)], ttl=600)
        out = self._status()
        self.assertNotIn("盘上有改动没人持钥匙", out)

    def test_工作区干净就不亮(self):
        self.assertNotIn("盘上有改动没人持钥匙", self._status())

    def test_不是_git_仓库就闭嘴(self):
        os.chdir(self.base)  # 临时目录本身通常不是仓库
        args = argparse.Namespace(url=self.url, json=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli._status(args, self.home, "opencode")
        self.assertEqual(code, 0)
        self.assertNotIn("盘上有改动没人持钥匙", buf.getvalue())

    def test_读不到锁就闭嘴(self):
        self.tracked.write_text("print(1)\nprint(3)\n", encoding="utf-8")
        os.chdir(self.repo)
        with mock.patch.object(cli, "call", side_effect=cli.ApiError(500, {"error": "boom"})):
            loose = cli._unlocked_dirty(subprocess, self.url, "opencode", self.home)
        self.assertEqual(loose, [])

    def test_json_里带上路径(self):
        self.tracked.write_text("print(1)\nprint(4)\n", encoding="utf-8")
        import json

        payload = json.loads(self._status(as_json=True))
        self.assertEqual([Path(p).name for p in payload["unlocked_dirty"]], ["a.py"])


if __name__ == "__main__":
    unittest.main()
