"""路径里的非 ASCII 先百分号编码。请求行必须是 ASCII，失败时给人话，不甩 traceback。"""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.client import ApiError, call, request
from infocenter.client import _ascii_path
from infocenter.server import make_handler
from infocenter.store import MailError, Store


class AsciiPathTest(unittest.TestCase):
    def test_斜杠保留_中文编码_百分号不留原文(self):
        self.assertEqual(_ascii_path("/v1/agents/grok"), "/v1/agents/grok")
        encoded = _ascii_path("/v1/agents/中文名")
        self.assertTrue(encoded.isascii())
        self.assertNotIn("中", encoded)
        self.assertEqual(unquote(encoded), "/v1/agents/中文名")
        self.assertIn("%25", _ascii_path("/v1/agents/a%b"))


class NonAsciiPathLiveTest(unittest.TestCase):
    def setUp(self):
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("manager", "人", "manager"),
            ("grok", "Grok", ""),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_remove_中文名走通且服务端看到原文(self):
        self.store.touch("中文名", "测")
        payload = request(self.url, "DELETE", "/v1/agents/中文名", "manager", {})
        self.assertEqual(payload["name"], "中文名")
        self.assertTrue(payload["removed"])
        names = {item["name"] for item in request(self.url, "GET", "/v1/agents", "manager")["agents"]}
        self.assertNotIn("中文名", names)

    def test_另一条按名路径也不甩栈(self):
        self.store.touch("中文名", "测")
        payload = request(self.url, "GET", "/v1/agents/中文名/messages", "manager")
        self.assertEqual(payload["agent"], "中文名")
        self.assertEqual(payload["messages"], [])

    def test_案卷编号里的中文是业务错误不是栈(self):
        with self.assertRaises(MailError) as ctx:
            call(self.url, "manager", "GET", "/v1/cases/中文卷", home=self.home)
        self.assertEqual(ctx.exception.code, "invalid_case_id")
        self.assertNotIn("Traceback", ctx.exception.message)

    def test_查询串里的中文也能送出(self):
        payload = request(
            self.url, "GET", "/v1/cases", "manager",
            query={"workspace": "中文项目", "limit": "5"},
        )
        self.assertIn("cases", payload)

    def test_身份名非_ASCII_给人话(self):
        with self.assertRaises(ApiError) as ctx:
            request(self.url, "GET", "/v1/agents", "中文身份")
        self.assertEqual(ctx.exception.code, "bad_request_target")
        self.assertIn("没有改任何东西", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_命令行移除中文名不甩栈(self):
        self.store.touch("中文名", "测")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "--home", str(self.home),
                "--url", self.url,
                "--as", "manager",
                "agents", "remove", "中文名",
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("已移除 中文名", out.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("UnicodeEncodeError", err.getvalue())

    def test_命令行身份名非_ASCII_印人话(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "--home", str(self.home),
                "--url", self.url,
                "--as", "中文身份",
                "agents", "list",
            ])
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("bad_request_target", text)
        self.assertIn("没有改任何东西", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("UnicodeEncodeError", text)


if __name__ == "__main__":
    unittest.main()
