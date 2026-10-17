"""接活钩子：`--wait-for` 把「我在等谁」落到案卷上，被等的人敲 `ic status` 看得到。

为什么（2026-10-16 实测）：SKILL 那卷的验收写着「触发 = kimi 宣布修订冻结」，而 **kimi 的 TODO 里没有这一项**
⇒ 那是个**永远不会响的触发**，事就那么悬着（谁都没接）。规矩是「**触发条件不许只写在信里**」：
写过 `--wait-for` 的案卷，至少要出现在**被等人的 `ic status`** 里。

口径：
- `ic case <id> --note "为什么" --wait-for 身份` ⇒ 状态转「待 <身份>」+ 落 `waiting_for`；**必须带 note**
  （跟 `--status` 同一条理由：状态变更要留一行，看板才知道你在等什么；不许静默）。
- `ic cases --wait-for X` = 只看在等 X 的；`ic status` 那盏灯只算**未封卷**的（封卷=归档，不该再催人）。
- 读不到服务 ⇒ 一个字都不说（灯不能挡住看状态）。
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


class WaitForCommandTest(unittest.TestCase):
    """命令面：`--wait-for` 送出去的 body 对不对，以及不许静默。"""

    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"

    def _run(self, argv):
        stub = mock.Mock(return_value={"ok": 1})
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "call", stub):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                code = cli.main(["--as", "opencode", *argv])
        return code, buf.getvalue(), err.getvalue(), stub

    def test_带_note_写信等着谁(self):
        code, _out, err, stub = self._run(
            ["case", "m1", "--note", "在等 SKILL 冻结", "--wait-for", "kimi"]
        )
        self.assertEqual(code, 0, err)
        body = stub.call_args[0][4]
        self.assertEqual(body.get("waiting_for"), "kimi")
        self.assertEqual(body.get("status"), "待谁")   # 状态同时转「待 X」
        self.assertEqual(body.get("note"), "在等 SKILL 冻结")

    def test_单独给_wait_for_会被拒(self):
        code, _out, err, stub = self._run(["case", "m1", "--wait-for", "kimi"])
        self.assertEqual(code, 1)
        self.assertIn("status_needs_note", err)
        self.assertIn("--note", err)
        self.assertFalse(stub.called, "拒掉就别打服务端")

    def test_显式_status_优先(self):
        _code, _out, _err, stub = self._run(
            ["case", "m1", "--note", "x", "--wait-for", "kimi", "--status", "暂停"]
        )
        body = stub.call_args[0][4]
        self.assertEqual(body.get("status"), "暂停")
        self.assertEqual(body.get("waiting_for"), "kimi")

    def test_cases_支持按_wait_for_过滤(self):
        _code, _out, _err, stub = self._run(["cases", "--wait-for", "kimi"])
        query = stub.call_args[1].get("query") or stub.call_args[0][5]
        self.assertEqual(query.get("waiting_for"), "kimi")


class WaitForStoreAndLightTest(unittest.TestCase):
    """存储过滤 + `ic status` 那盏灯。"""

    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "mailbox.db", seeds=[("opencode", "OpenCode", "coordinator")])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _case_waiting_on(self, who: str, title: str = "在等谁") -> str:
        case = self.store.create_case(agent="opencode", title=title, acceptance="收工后不再催")
        self.store.append_case_node(
            case["id"], agent="opencode", note=f"在等 {who}", status="待谁", waiting_for=who
        )
        return case["id"]

    def _status(self, agent: str, as_json=False) -> str:
        args = argparse.Namespace(url=self.url, json=as_json)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli._status(args, self.home, agent)
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_store_按_waiting_for_过滤(self):
        mine = self._case_waiting_on("kimi")
        self._case_waiting_on("grok")
        got = [c["id"] for c in self.store.list_cases(waiting_for="kimi")]
        self.assertEqual(got, [mine])

    def test_被等的人看得到(self):
        self._case_waiting_on("kimi", "SKILL 漂移：等 kimi 冻结")
        out = self._status("kimi")
        self.assertIn("有 1 卷在等你", out)
        self.assertIn("SKILL 漂移", out)

    def test_没在等你就看不到(self):
        self._case_waiting_on("kimi")
        self.assertNotIn("在等你", self._status("grok"))

    def test_封卷之后不再催(self):
        cid = self._case_waiting_on("kimi")
        self.store.close_case(cid, agent="opencode", note="收工")
        self.assertNotIn("在等你", self._status("kimi"))

    def test_status灯仍是全局数_不分项目也不丢空workspace(self):
        # 守护提示词里的数按项目收；这一盏灯是给人看的总账，两处不要混成一个口径。
        for title, workspace in (
            ("A项目", "/tmp/proj-status-a"),
            ("B项目", "/tmp/proj-status-b"),
            ("无项目", None),
        ):
            case = self.store.create_case(
                agent="opencode", title=title, workspace=workspace, acceptance="收工后不再催",
            )
            self.store.append_case_node(
                case["id"], agent="opencode", note=f"等 kimi：{title}", status="待谁", waiting_for="kimi",
            )
        out = self._status("kimi")
        self.assertIn("有 3 卷在等你", out)

    def test_json_里带上卷号(self):
        cid = self._case_waiting_on("kimi")
        import json

        payload = json.loads(self._status("kimi", as_json=True))
        self.assertEqual(payload["waiting_on_you"], [cid])

    def test_服务读不到就闭嘴(self):
        self._case_waiting_on("kimi")
        args = argparse.Namespace(url="http://127.0.0.1:1", json=False)  # 没人听
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli._status(args, self.home, "kimi")
        self.assertEqual(code, 1)          # 服务不在 ⇒ status 本来就返回 1
        self.assertNotIn("在等你", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
