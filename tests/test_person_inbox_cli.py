"""人的收件箱合并显示：列表行只在实际收件名与调用者不同时标一小段。

口径（派单 m1a1443051462e51a6）：文案只读 store 的 `delivered_to`。
字段不在就不标，不读 `to`、不调用 `is_person`、不自己算别名。
同名的信不标，避免日常输出加噪。
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
from infocenter.store import Store


def _quiet() -> None:
    for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID", "INFOCENTER_URL"):
        os.environ.pop(key, None)
    os.environ["INFOCENTER_QUIET"] = "1"


def _header(text: str, message_id: str) -> str:
    for line in text.splitlines():
        if message_id in line and "来自" in line:
            return line
    return ""


class InboxAliasMarkTest(unittest.TestCase):
    def test_字段不在_空_同名_非字符串都不标(self):
        self.assertEqual(cli._inbox_alias_mark({"to": ["human"]}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"id": "m1"}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": "manager"}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": " manager "}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": ""}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": "   "}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": None}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": ["human"]}, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark(None, "manager"), "")
        self.assertEqual(cli._inbox_alias_mark({"delivered_to": "human"}, ""), "")

    def test_与调用者不同才标实际收件名(self):
        self.assertEqual(
            cli._inbox_alias_mark({"delivered_to": "human"}, "manager"),
            "（旧名 human）",
        )
        self.assertEqual(
            cli._inbox_alias_mark({"delivered_to": " manager "}, "human"),
            "（旧名 manager）",
        )


class InboxAliasCopyTest(unittest.TestCase):
    def setUp(self):
        _quiet()

    def _payload(self):
        return {
            "agent": "manager",
            "status": "unread",
            "count": 3,
            "messages": [
                {
                    "id": "m-old",
                    "priority": "normal",
                    "created_at": "2026-10-16T10:00:00Z",
                    "created_at_beijing": "2026-10-16 18:00:00 +0800",
                    "from": "grok",
                    "subject": "给旧名",
                    "preview": "这封寄给 human",
                    "to": ["human"],
                    "delivered_to": "human",
                },
                {
                    "id": "m-same",
                    "priority": "high",
                    "created_at": "2026-10-16T10:01:00Z",
                    "created_at_beijing": "2026-10-16 18:01:00 +0800",
                    "from": "grok",
                    "subject": "给现名",
                    "preview": "这封寄给 manager",
                    "to": ["manager"],
                    "delivered_to": "manager",
                },
                {
                    "id": "m-bare",
                    "priority": "normal",
                    "created_at": "2026-10-16T10:02:00Z",
                    "created_at_beijing": "2026-10-16 18:02:00 +0800",
                    "from": "cursor",
                    "subject": "还没给字段",
                    "preview": "to 里有 human 也不许自己标",
                    "to": ["human"],
                },
            ],
        }

    def _run(self, payload, *argv) -> str:
        buf = io.StringIO()
        with mock.patch.object(cli, "call", return_value=payload):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--as", "manager", "--url", "http://127.0.0.1:9", "inbox", *argv])
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_只有收件名不同的那行带标_同名和缺字段不标(self):
        text = self._run(self._payload())
        old = _header(text, "m-old")
        same = _header(text, "m-same")
        bare = _header(text, "m-bare")
        self.assertIn("（旧名 human）", old)
        self.assertNotIn("（旧名", same)
        self.assertNotIn("（旧名", bare)
        self.assertEqual(text.count("（旧名"), 1)
        self.assertIn("! m-same  ", text)
        self.assertNotIn("（旧名", text.split("给旧名", 1)[1].split("给现名", 1)[0])

    def test_json不另造标(self):
        text = self._run(self._payload(), "--json")
        self.assertNotIn("（旧名", text)
        payload = json.loads(text)
        self.assertEqual(payload["messages"][0]["delivered_to"], "human")
        self.assertNotIn("delivered_to", payload["messages"][2])


class PersonInboxAliasLiveTest(unittest.TestCase):
    """造一封寄给 human、一封寄给 manager，走真的 CLI 和临时服务。

    store 还没把别名信并进来、或还没给 `delivered_to`：两边都不标（不自己算）。
    字段落了且 manager 看得到寄给 human 的那封：那行带「（旧名 human）」；
    寄给 manager 的不标。两边都看到同一批信时，条数一致。
    """

    def setUp(self):
        _quiet()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.store = Store(self.home / "mailbox.db", seeds=[
            ("grok", "Grok", ""),
            ("human", "人", ""),
            ("manager", "人", "manager"),
        ])
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _run(self, agent: str, *argv) -> str:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main([
                "--home", str(self.home), "--url", self.url, "--as", agent, *argv,
            ])
        self.assertEqual(code, 0, err.getvalue())
        return buf.getvalue()

    def test_实跑_字段在就标_同名不标_两边条数一致(self):
        to_human = self.store.send("grok", "human", "给旧名", "这封寄给 human")
        to_manager = self.store.send("grok", "manager", "给现名", "这封寄给 manager")

        manager_json = json.loads(self._run("manager", "inbox", "--status", "all", "--json"))
        human_json = json.loads(self._run("human", "inbox", "--status", "all", "--json"))
        manager_text = self._run("manager", "inbox", "--status", "all")
        human_text = self._run("human", "inbox", "--status", "all")

        def row(payload, message_id):
            return next((item for item in payload["messages"] if item["id"] == message_id), None)

        human_row = row(manager_json, to_human["id"])
        if human_row is None or "delivered_to" not in human_row:
            self.assertNotIn("（旧名", manager_text)
            self.assertNotIn("（旧名", human_text)
            return

        self.assertEqual(human_row.get("delivered_to"), "human")
        self.assertIn("（旧名 human）", _header(manager_text, to_human["id"]))

        manager_row = row(manager_json, to_manager["id"])
        if manager_row is not None and manager_row.get("delivered_to") == "manager":
            self.assertNotIn("（旧名", _header(manager_text, to_manager["id"]))

        human_own = row(human_json, to_human["id"])
        if human_own is not None and human_own.get("delivered_to") == "human":
            self.assertNotIn("（旧名", _header(human_text, to_human["id"]))

        ids_manager = {item["id"] for item in manager_json["messages"]}
        ids_human = {item["id"] for item in human_json["messages"]}
        if to_human["id"] in ids_manager and to_manager["id"] in ids_manager and ids_manager == ids_human:
            self.assertEqual(manager_json["count"], human_json["count"])
            self.assertEqual(human_json["count"], len(ids_human))


if __name__ == "__main__":
    unittest.main()
