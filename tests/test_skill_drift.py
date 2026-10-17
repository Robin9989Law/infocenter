"""SKILL 副本漂移检查（README 那句承诺得兑现，不能只写在文档里）。

背景（kimi 2026-10-15 追审报的「虚报」）：README 写着「`ic status` / `ic guard` 会比对两份 SKILL
副本、不一致出一行」，代码里根本没有这套机制。而漂移是真事：交互会话读的是**安装副本**
（`~/.agents/skills/infocenter/SKILL.md`），仓库那份改了它看不到——两份真相靠人记一定再犯。

口径（跟黄灯一致）：**只提示、不拦**。判断「该不该现在同步」要靠人（SKILL 正在修订时本来就该先漂着）。
"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import Store


class SkillDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.repo_skill = self.home / "repo" / "SKILL.md"
        self.repo_skill.parent.mkdir()
        self.repo_skill.write_text("仓库那份：第一行\n", encoding="utf-8")
        self.installed = self.home / "installed" / "SKILL.md"
        self.installed.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self, hints=None):
        return cli.skill_drift_lines(self.repo_skill, hints if hints is not None else [self.installed])

    def test_内容一样就不吭声(self):
        self.installed.write_text("仓库那份：第一行\n", encoding="utf-8")
        self.assertEqual(self._lines(), [])

    def test_内容不一样就出一行_并点名副本在哪(self):
        self.installed.write_text("安装副本：停在 10-14 的老版本\n", encoding="utf-8")
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(str(self.installed), lines[0])
        self.assertIn("漂移", lines[0])

    def test_软链到仓库那份不算漂移(self):
        """优先软链（一份真相）：同一个 inode 就不该报。"""
        try:
            os.symlink(self.repo_skill, self.installed)
        except OSError as e:
            self.skipTest(f"Symlinks not supported or permitted on this platform: {e}")
        self.assertEqual(self._lines(), [])

    def test_没装副本就不吭声(self):
        self.assertEqual(self._lines(hints=[self.home / "没人装在这儿" / "SKILL.md"]), [])

    def test_仓库那份没了就不吭声(self):
        """不猜、不报：连基准都没有的时候闭嘴。"""
        self.repo_skill.unlink()
        self.assertEqual(self._lines(), [])

    def test_副本读不了就跳过不崩(self):
        """安装位点若是个目录（或读不动），跳过它，别把 `ic status` 弄崩。"""
        self.assertEqual(self._lines(hints=[self.installed.parent]), [])

    def test_多个副本各报各的(self):
        self.installed.write_text("旧的\n", encoding="utf-8")
        other = self.home / "other" / "SKILL.md"
        other.parent.mkdir()
        other.write_text("也是旧的\n", encoding="utf-8")
        self.assertEqual(len(self._lines(hints=[self.installed, other])), 2)


class StatusWiringTest(unittest.TestCase):
    """`ic status` 真的会把漂移那行打出来（README 说的就是这一行）。"""

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

    def _run_status(self, as_json: bool) -> str:
        args = argparse.Namespace(url=self.url, json=as_json)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli._status(args, self.home, "opencode")
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_status_把漂移那行打出来(self):
        with mock.patch("infocenter.cli.skill_drift_lines", return_value=["  ⚠ SKILL 副本漂移：x"]):
            out = self._run_status(as_json=False)
        self.assertIn("SKILL 副本漂移", out)

    def test_status_json_里也带上漂移(self):
        with mock.patch("infocenter.cli.skill_drift_lines", return_value=["  ⚠ SKILL 副本漂移：x"]):
            out = self._run_status(as_json=True)
        self.assertIn("skill_drift", out)

    def test_没有漂移就一个字都不多(self):
        with mock.patch("infocenter.cli.skill_drift_lines", return_value=[]):
            out = self._run_status(as_json=False)
        self.assertNotIn("漂移", out)


if __name__ == "__main__":
    unittest.main()
