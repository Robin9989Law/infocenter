"""未签收回信：409 人话原样印，`ic cases` / `ic case` 只在字段在且 >0 时亮黄灯。

灯只提示、不拦。N 不在客户端重算——store 没给 `unread_replies` 就不显示。
「最早 / 来自」同样只读伴随字段，缺了不编。
"""

import contextlib
import io
import json
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
from infocenter.store import MailError, Store


SERVER_MESSAGE = (
    "案卷 c9 这条线上你还有 2 封没收（最早 2026-10-16 17:42:21 +0800，来自 pi）。"
    "先把回信看完再封卷（`ic inbox --status unread`）。"
)


def _quiet() -> None:
    for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID", "INFOCENTER_URL"):
        os.environ.pop(key, None)
    os.environ["INFOCENTER_QUIET"] = "1"


class UnreadRepliesCopyTest(unittest.TestCase):
    def test_服务端有话就原样印_不盖掉案卷号和来自谁(self):
        text = cli._format_mail_error(MailError("unread_replies", SERVER_MESSAGE, 409))
        self.assertEqual(text, f"unread_replies: {SERVER_MESSAGE}\n")
        self.assertEqual(text.count("案卷 c9"), 1)
        self.assertIn("来自 pi", text)
        self.assertIn("2026-10-16 17:42:21 +0800", text)
        self.assertNotIn("别硬写", text)
        self.assertNotIn("TODO", text)

    def test_不含关键字的事由也不被换成空话(self):
        message = "案卷 c9 不能办结：pi 在 2026-10-16 17:42:21 +0800 还有回信没看"
        text = cli._format_mail_error(MailError("unread_replies", message, 409))
        self.assertIn(message, text)
        self.assertNotIn("没签收的回信。先把回信看完", text)

    def test_只回错误码才用兜底_不编来信人(self):
        for message in ("unread_replies", "", "   "):
            text = cli._format_mail_error(MailError("unread_replies", message, 409))
            self.assertIn("先把回信看完再封卷", text)
            self.assertIn("`ic inbox --status unread`", text)
            self.assertNotIn("来自", text)
            self.assertNotIn("案卷", text)
            self.assertNotIn("别硬写", text)

    def test_办结命令收到409就原样印(self):
        def fake_call(url, agent, method, path, body=None, query=None, home=None, window=None, pid=None):
            if method == "GET":
                return {"id": "c9", "milestones": [], "title": "有回信", "text": "状态：在办"}
            raise MailError("unread_replies", SERVER_MESSAGE, 409)

        _quiet()
        err = io.StringIO()
        with mock.patch.object(cli, "call", side_effect=fake_call):
            with contextlib.redirect_stderr(err):
                code = cli.main([
                    "--as", "grok", "--url", "http://127.0.0.1:9",
                    "case", "c9", "--note", "办结", "--status", "done",
                ])
        self.assertEqual(code, 1)
        self.assertIn(f"unread_replies: {SERVER_MESSAGE}", err.getvalue())
        self.assertEqual(err.getvalue().count("案卷 c9"), 1)

    def test_no_acceptance仍走原来的人话(self):
        text = cli._format_mail_error(MailError("no_acceptance", "no_acceptance", 409))
        self.assertIn("怎样算完", text)
        self.assertIn("别硬写", text)


class UnreadRepliesLightTest(unittest.TestCase):
    def setUp(self):
        _quiet()

    def _cases(self):
        return {
            "count": 4,
            "cases": [
                {
                    "id": "c-hot",
                    "title": "有回信",
                    "status": "open",
                    "status_label": "在办",
                    "owner_agent": "grok",
                    "substantive": True,
                    "acceptance": "看完再封",
                    "unread_replies": 2,
                    "unread_replies_earliest": "2026-10-16T09:42:21Z",
                    "unread_replies_from": "pi",
                },
                {
                    "id": "c-zero",
                    "title": "已看完",
                    "status": "open",
                    "status_label": "在办",
                    "owner_agent": "grok",
                    "substantive": True,
                    "acceptance": "无",
                    "unread_replies": 0,
                    "unread_replies_from": "pi",
                },
                {
                    "id": "c-old",
                    "title": "旧服务",
                    "status": "open",
                    "status_label": "在办",
                    "owner_agent": "grok",
                    "substantive": True,
                    "acceptance": "无",
                },
                {
                    "id": "c-count",
                    "title": "只有数字",
                    "status": "open",
                    "status_label": "在办",
                    "owner_agent": "grok",
                    "substantive": True,
                    "acceptance": "无",
                    "unread_replies": 1,
                },
            ],
        }

    def test_列表两处口径_有数字才亮_时间换成北京时间(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "call", return_value=self._cases()):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "cases"])
        self.assertEqual(code, 0)
        lines = buf.getvalue().splitlines()
        self.assertIn("  ⚠ 有 2 封未签收回信（最早 2026-10-16 17:42:21 +0800，来自 pi）", lines)
        self.assertIn("  ⚠ 有 1 封未签收回信", lines)
        self.assertNotIn("  ⚠ 有 1 封未签收回信（", buf.getvalue())
        self.assertNotIn("有 0 封", buf.getvalue())
        self.assertNotIn("旧服务\n  ⚠", buf.getvalue())

    def test_详情亮同一行_北京时间字段优先(self):
        payload = {
            "id": "c-hot",
            "title": "有回信",
            "text": "状态：在办",
            "milestones": [],
            "acceptance": "看完再封",
            "unread_replies": 2,
            "unread_replies_earliest_beijing": "2026-10-16 17:01:02 +0800",
            "unread_replies_earliest": "2026-10-16T00:00:00Z",
            "unread_replies_from": "cursor",
        }
        buf = io.StringIO()
        with mock.patch.object(cli, "call", return_value=payload):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "case", "c-hot"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("  ⚠ 有 2 封未签收回信（最早 2026-10-16 17:01:02 +0800，来自 cursor）", text)
        self.assertNotIn("08:00:00", text)

    def test_零封_字段不在_读不成整数都不显示(self):
        self.assertEqual(cli._unread_replies_line({"unread_replies": 0}), "")
        self.assertEqual(cli._unread_replies_line({"title": "旧服务"}), "")
        self.assertEqual(cli._unread_replies_line({"unread_replies": None}), "")
        self.assertEqual(cli._unread_replies_line({"unread_replies": True}), "")
        self.assertEqual(cli._unread_replies_line({"unread_replies": "两封"}), "")
        self.assertEqual(cli._unread_replies_line(None), "")

    def test_json不另印黄灯(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "call", return_value=self._cases()):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--as", "grok", "--url", "http://127.0.0.1:9", "--json", "cases"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertNotIn("未签收回信", text)
        payload = json.loads(text)
        self.assertEqual(payload["cases"][0]["unread_replies"], 2)


class UnreadRepliesLiveTest(unittest.TestCase):
    """造一条「别人回我、我没签收」的线程，走真的 CLI 和临时服务。

    store 还没给 `unread_replies` 时，两处都不亮（不自己数）。
    字段落了之后，同一条测试改为：亮那一行，签收后不亮。
    """

    def setUp(self):
        _quiet()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""),
            ("pi", "Pi", ""),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _run(self, *argv) -> str:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(["--home", str(self.home), "--url", self.url, "--as", "grok", *argv])
        self.assertEqual(code, 0, err.getvalue())
        return buf.getvalue()

    def test_实跑_字段在就亮_签收后不亮(self):
        sent = self.store.send("grok", "pi", "问一下", "在吗")
        reply = self.store.reply("pi", sent["id"], "别封，这事还没完")
        case = self.store.create_case(
            agent="grok", thread_id=sent["thread_id"], title="问一下",
        )
        listed_json = json.loads(self._run("cases", "--json"))
        item = next(row for row in listed_json["cases"] if row["id"] == case["id"])
        detail_json = json.loads(self._run("case", case["id"], "--json"))
        listed = self._run("cases")
        detail = self._run("case", case["id"])

        if "unread_replies" not in item and "unread_replies" not in detail_json:
            self.assertNotIn("未签收回信", listed)
            self.assertNotIn("未签收回信", detail)
            return

        self.assertIn("unread_replies", item)
        self.assertIn("unread_replies", detail_json)
        count = int(item["unread_replies"])
        self.assertGreater(count, 0)
        self.assertEqual(int(detail_json["unread_replies"]), count)
        needle = f"⚠ 有 {count} 封未签收回信"
        self.assertIn(needle, listed)
        self.assertIn(needle, detail)
        if (
            item.get("unread_replies_from")
            and (item.get("unread_replies_earliest_beijing") or item.get("unread_replies_earliest"))
        ):
            self.assertIn("最早", listed)
            self.assertIn("来自", listed)
            self.assertIn("最早", detail)
            self.assertIn("来自", detail)

        self._run("read", reply["id"])
        listed_after = self._run("cases")
        detail_after = self._run("case", case["id"])
        again = json.loads(self._run("cases", "--json"))
        item_after = next(row for row in again["cases"] if row["id"] == case["id"])
        if "unread_replies" in item_after:
            self.assertEqual(int(item_after["unread_replies"]), 0)
        self.assertNotIn("未签收回信", listed_after)
        self.assertNotIn("未签收回信", detail_after)


if __name__ == "__main__":
    unittest.main()
