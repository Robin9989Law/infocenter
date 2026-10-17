"""`ic lock --stay`、三句 409 人话、两盏灯。

灯只提示、不拦。服务端没带 `no_progress` / `orphan_queue` 时，用现成的
`acquired_at` 和盘上 `stat`、以及队列里 `held_by` 为空的提议现算。
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import LOCK_TTL, MailError, Store


class StaySwitchTest(unittest.TestCase):
    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"

    def test_帮助写明再要一段租约(self):
        import argparse

        parser = cli.build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        help_text = ""
        for action in sub.choices["lock"]._actions:
            if "--stay" in action.option_strings:
                help_text = action.help or ""
        self.assertEqual(
            help_text,
            "盘上没动过时，到顶前可以多次再要一段；不刷新计时，到期时间截到拿锁后 1 小时",
        )

    def _lock(self, *extra):
        with mock.patch.object(cli, "call", return_value={"locks": [], "agent": "grok"}) as stub:
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "lock", *extra, "/tmp/stay-probe.py"])
        self.assertEqual(code, 0, err.getvalue())
        body = stub.call_args.args[4]
        self.assertEqual(stub.call_args.args[2], "POST")
        self.assertEqual(stub.call_args.args[3], "/v1/locks")
        return body

    def test_stay_开关交到请求里(self):
        self.assertTrue(self._lock("--stay")["stay"])
        self.assertFalse(self._lock()["stay"])

    def test_三个错误码的人话(self):
        samples = {
            "no_progress": "盘上没动过，不给第二把租约",
            "stay_exhausted": "到顶前可以多次",
            "no_acceptance": "怎样算完",
        }
        for code, mark in samples.items():
            with self.subTest(code=code):
                text = cli._format_mail_error(MailError(code, code, 409))
                self.assertIn(mark, text)
                self.assertIn("别硬写", text)
                self.assertIn("TODO", text)
                self.assertIn("先做别的", text)
                self.assertIn("到期自然回到可拿", text)
                self.assertEqual(text.count("别硬写"), 1)

    def test_服务端已经写了人话就原样印(self):
        message = (
            "这些路径自拿锁后盘上没有写入痕迹：/tmp/a.py。不给第二把租约。别硬写。"
            "把要改的事记下来（`ic queue <路径> --note \"我要改什么\"`，会写进你自己的 TODO）。"
            "先做别的；这把锁到期后自然回到可拿。不要发信、不要干等"
        )
        text = cli._format_mail_error(MailError("no_progress", message, 409))
        self.assertEqual(text.count("别硬写"), 1)
        self.assertIn(message, text)

    def test_旧句改印成到顶_新事由仍接上人话(self):
        stale = "延过一次了，要再拿请先 `ic unlock` 放一次再拿"
        text = cli._format_mail_error(MailError("stay_exhausted", stale, 409))
        self.assertNotIn("延过一次", text)
        self.assertIn("到顶前可以多次", text)
        self.assertIn("拿锁后 1 小时的顶", text)
        self.assertIn("别硬写", text)
        self.assertIn("到期自然回到可拿", text)
        self.assertEqual(text.count("别硬写"), 1)

        message = "路径 /tmp/a.py 已经到拿锁后 1 小时的顶了"
        text = cli._format_mail_error(MailError("stay_exhausted", message, 409))
        self.assertIn(message, text)
        self.assertIn("别硬写", text)
        self.assertIn("到期自然回到可拿", text)
        self.assertEqual(text.count("别硬写"), 1)
        self.assertEqual(text.count("拿锁后 1 小时的顶"), 1)

    def test_命令行收到错误码就印人话(self):
        with mock.patch.object(cli, "call", side_effect=MailError("stay_exhausted", "stay_exhausted", 409)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "lock", "/tmp/stay-probe.py"])
        self.assertEqual(code, 1)
        self.assertIn("stay_exhausted:", err.getvalue())
        self.assertIn("到顶前可以多次", err.getvalue())
        self.assertNotIn("延过一次", err.getvalue())
        self.assertIn("放一次再拿", err.getvalue())
        self.assertIn("到期自然回到可拿", err.getvalue())


class ProgressMarkTest(unittest.TestCase):
    def test_文件不存在是无痕迹(self):
        self.assertFalse(cli._touched_since("/tmp/ic-stay-no-such-file.py", "2020-01-01T00:00:00Z"))

    def test_时间读不懂就不当无进度(self):
        self.assertTrue(cli._touched_since("/tmp/ic-stay-no-such-file.py", "不是时间"))

    def test_秒级比较宁松勿误杀(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.py"
            path.write_text("x\n", encoding="utf-8")
            mtime = path.stat().st_mtime
            # acquired_at 落在同一秒的起点：mtime 不早于它，算有痕迹。
            acquired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
            self.assertTrue(cli._touched_since(str(path), acquired))
            # 锁的起点比 mtime 还晚两秒：这段时间盘上没动过。
            later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime + 2))
            self.assertFalse(cli._touched_since(str(path), later))

    def test_服务端给了_no_progress_就不重算(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh.py"
            path.write_text("x\n", encoding="utf-8")
            item = {
                "path": str(path),
                "acquired_at": "2000-01-01T00:00:00Z",
                "no_progress": True,
            }
            cli._mark_no_progress([item])
            self.assertTrue(item["no_progress"])


class LockLightsTextTest(unittest.TestCase):
    def test_黄灯和空钥匙排队都印出来(self):
        payload = {
            "count": 1,
            "locks": [{
                "path": "/tmp/held.py",
                "agent": "grok",
                "task_id": "",
                "expires_at": "2026-10-16T09:00:00Z",
                "expires_at_beijing": "2026-10-16 17:00:00 +0800",
                "expires_in_seconds": 900,
                "acquired_at": "2026-10-16T08:00:00Z",
                "acquired_at_beijing": "2026-10-16 16:00:00 +0800",
                "no_progress": True,
            }],
            "orphan_queue": [{
                "path": "/tmp/waiting.py",
                "agent": "kimi",
                "at": "2026-10-16T06:00:00Z",
                "hours": 2.0,
                "wait": "2 小时",
            }],
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._emit(False, "locks", payload)
        text = buf.getvalue()
        self.assertIn("⚠ 无进度：自 2026-10-16 16:00:00 +0800 起盘上没变", text)
        self.assertIn("钥匙空着、有人在等：/tmp/waiting.py（kimi 提的，2 小时）", text)

    def test_没有排队就不加空钥匙那一节(self):
        payload = {
            "count": 0,
            "locks": [],
            "orphan_queue": [],
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._emit(False, "locks", payload)
        self.assertNotIn("钥匙空着、有人在等", buf.getvalue())


class OrphanStatusLiveTest(unittest.TestCase):
    """造景：一把锁的文件自拿到后没改过，另一条路径有人排队、钥匙空着。"""

    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""),
            ("kimi", "Kimi", ""),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.stale = self.base / "stale.py"
        self.stale.write_text("print(1)\n", encoding="utf-8")
        self.waiting = self.base / "waiting.py"
        self.waiting.write_text("print(2)\n", encoding="utf-8")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        import gc
        gc.collect()
        self.tmp.cleanup()

    def _run(self, *argv) -> str:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(["--home", str(self.home), "--url", self.url, "--as", "grok", *argv])
        self.assertEqual(code, 0, err.getvalue())
        return buf.getvalue()

    def test_没人排队时_status_不加那一行(self):
        text = self._run("status")
        self.assertNotIn("钥匙空着但有排队", text)

    def test_实跑_locks_和_status_都亮(self):
        self.store.lock_acquire("grok", [str(self.stale)], ttl=1800)
        # 黄灯口径：拿满一把租约、自 acquired_at 起盘上没变。刚拿到的锁不亮。
        acquired = datetime.now(timezone.utc) - timedelta(seconds=LOCK_TTL + 60)
        acquired_iso = acquired.strftime("%Y-%m-%dT%H:%M:%SZ")
        held = self.store.lock_list()["locks"][0]["path"]
        con = sqlite3.connect(self.store.path)
        try:
            con.execute("UPDATE locks SET acquired_at = ? WHERE path = ?", (acquired_iso, held))
            con.commit()
        finally:
            con.close()
        os.utime(self.stale, (acquired.timestamp() - 120, acquired.timestamp() - 120))
        queued = self.store.queue_change("kimi", str(self.waiting), note="等钥匙")

        locks = self._run("locks")
        self.assertIn("⚠ 无进度：自 ", locks)
        self.assertIn("起盘上没变", locks)
        self.assertIn("+0800", locks)
        self.assertIn(f"钥匙空着、有人在等：{queued['path']}（kimi 提的，", locks)

        status = self._run("status")
        self.assertIn("钥匙空着但有排队的路径 1 条（最久 ", status)
        self.assertIn("小时）", status)


if __name__ == "__main__":
    unittest.main()
