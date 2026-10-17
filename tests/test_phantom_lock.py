"""相对路径取锁必须落在敲命令的目录，不能静默落到服务端进程目录。

omp 的复现：在子目录敲 `ic lock 相对路径`，锁键按服务端 cwd 算。
文件还不存在也要能锁（先锁后建）。绝对路径行为不变。
服务端若仍收到相对路径：收下，并给一句人话，不拒绝。
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.client import request, _attach_client_cwd
from infocenter.store import Store


class ClientPathUnitTest(unittest.TestCase):
    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_拿锁放锁排队都按敲命令的目录转绝对路径(self):
        sub = self.home / "sub"
        sub.mkdir()
        os.chdir(sub)
        want = str((sub / "zzz-不存在.md").resolve())
        with mock.patch.object(cli, "call", return_value={"locks": [], "agent": "grok", "released_count": 0}) as stub:
            code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "lock", "zzz-不存在.md"])
        self.assertEqual(code, 0)
        self.assertEqual(stub.call_args.args[4]["paths"], [want])

        with mock.patch.object(cli, "call", return_value={"released_count": 1, "released": [want]}) as stub:
            code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "unlock", "zzz-不存在.md"])
        self.assertEqual(code, 0)
        self.assertEqual(stub.call_args.args[4]["paths"], [want])

        with mock.patch.object(cli, "call", return_value={"pending": [], "count": 0}) as stub:
            code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "queue", "zzz-不存在.md"])
        self.assertEqual(code, 0)
        self.assertEqual(stub.call_args.kwargs["query"]["path"], want)

    def test_绝对路径原样送出(self):
        target = str((self.home / "abs-missing.md").resolve())
        os.chdir(self.home)
        with mock.patch.object(cli, "call", return_value={"locks": [], "agent": "grok"}) as stub:
            code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "lock", target])
        self.assertEqual(code, 0)
        self.assertEqual(stub.call_args.args[4]["paths"], [target])

    def test_人话提示会印出来(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._emit(False, "lock", {
                "locks": [],
                "agent": "grok",
                "path_hint": "收到相对路径，按服务端所在目录解析。没有拒绝。",
            })
        self.assertIn("没有拒绝", buf.getvalue())

    def test_client_cwd_只附在拿锁和发信上(self):
        locked = _attach_client_cwd("POST", "/v1/locks", {"paths": ["a.py"]})
        self.assertEqual(locked["client_cwd"], os.getcwd())
        self.assertNotIn("client_cwd", _attach_client_cwd("POST", "/v1/locks/release", {"paths": ["a.py"]}))
        kept = _attach_client_cwd("POST", "/v1/locks", {"paths": ["a.py"], "client_cwd": None})
        self.assertIsNone(kept["client_cwd"])
        sent = _attach_client_cwd("POST", "/v1/messages", {"to": "kimi"})
        self.assertEqual(sent["client_cwd"], os.getcwd())


class RelativeHintStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(self.home / "m.db", seeds=[("grok", "Grok", "")])
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_相对路径仍收下并说明落点(self):
        os.chdir(self.home)
        res = self.store.lock_acquire("grok", ["missing.md"], client_cwd="/tmp/client-side")
        self.assertEqual(res["locks"][0]["path"], str((self.home / "missing.md").resolve()))
        self.assertIn("没有拒绝", res["path_hint"])
        self.assertIn("missing.md", res["path_hint"])
        self.assertIn("/tmp/client-side", res["path_hint"])
        self.assertEqual(self.store.lock_list()["count"], 1)

    def test_绝对路径不提示(self):
        target = str((self.home / "abs.md").resolve())
        res = self.store.lock_acquire("grok", [target], client_cwd=str(self.home))
        self.assertNotIn("path_hint", res)
        self.assertEqual(res["locks"][0]["path"], target)


class SplitCwdServerTest(unittest.TestCase):
    """服务进程的 cwd 和敲命令的目录不是同一个。这是 omp 那次的形状。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.server_cwd = self.home / "server-cwd"
        self.client_cwd = self.home / "client-cwd"
        self.server_cwd.mkdir()
        self.client_cwd.mkdir()
        self.ic_home = self.home / "ic-home"
        env = os.environ.copy()
        env["INFOCENTER_QUIET"] = "1"
        for key in ("INFOCENTER_WINDOW", "INFOCENTER_AGENT", "INFOCENTER_NO_PID", "INFOCENTER_HOME"):
            env.pop(key, None)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.env = env
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "infocenter", "serve", "--home", str(self.ic_home), "--port", "0"],
            cwd=self.server_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        from tests.test_quality_mcp import _wait_readable

        line = self.proc.stdout.readline().strip() if _wait_readable(self.proc.stdout, 15) else ""
        if not line.startswith("http"):
            err = ""
            if self.proc.stderr is not None:
                err = self.proc.stderr.read()
            self.proc.kill()
            self.fail(f"服务没起来：{line!r} {err}")
        self.url = line

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                pipe.close()
        self.tmp.cleanup()

    def _ic(self, *args, cwd=None):
        return subprocess.run(
            [
                sys.executable, "-m", "infocenter",
                "--as", "grok", "--url", self.url, "--home", str(self.ic_home),
                *args,
            ],
            cwd=cwd or self.client_cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _keys(self):
        listed = self._ic("--json", "locks")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        return [row["path"] for row in payload.get("locks") or []]

    def test_子目录相对路径锁在子目录_不存在也能锁_放锁同一把(self):
        want = str((self.client_cwd / "zzz-不存在.md").resolve())
        other = str((self.server_cwd / "zzz-不存在.md").resolve())
        got = self._ic("lock", "zzz-不存在.md")
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertIn(want, got.stdout)
        self.assertNotIn("相对路径", got.stdout)
        self.assertEqual(self._keys(), [want])
        self.assertNotEqual(want, other)
        self.assertNotIn(other, self._keys())

        released = self._ic("unlock", "zzz-不存在.md")
        self.assertEqual(released.returncode, 0, released.stderr + released.stdout)
        self.assertEqual(self._keys(), [])

    def test_绝对路径锁键不变(self):
        target = str((self.client_cwd / "abs-missing.md").resolve())
        got = self._ic("lock", target)
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertEqual(self._keys(), [target])

    def test_服务端收到相对路径会提示且不拒绝(self):
        payload = request(
            self.url, "POST", "/v1/locks", "grok",
            {"paths": ["from-api.md"], "client_cwd": str(self.client_cwd)},
        )
        self.assertIn("没有拒绝", payload.get("path_hint") or "")
        self.assertIn(str(self.client_cwd), payload["path_hint"])
        self.assertEqual(
            payload["locks"][0]["path"],
            str((self.server_cwd / "from-api.md").resolve()),
        )
