"""笔5：重启脚本解释器用 shlex 包；POST /v1/open 只开已登记项目目录。"""

import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from infocenter.server import make_handler
from infocenter.store import Store


def _http_json(url: str, method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        exc.close()
        return exc.code, json.loads(raw)


class Pen5BoardOpenTest(unittest.TestCase):
    def setUp(self):
        os.environ["INFOCENTER_QUIET"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "mailbox.db", seeds=[("cursor", "Cursor", "")])
        self.proj = self.home / "proj"
        self.proj.mkdir()
        self.other = self.home / "other"
        self.other.mkdir()
        self.store.add_project(str(self.proj), name="笔5")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_unregistered_dir_is_403(self):
        with patch("subprocess.Popen") as popen:
            st, body = _http_json(self.url, "POST", "/v1/open", body={"path": str(self.other)})
        self.assertEqual(st, 403)
        self.assertEqual(body.get("error"), "not_a_project")
        self.assertIn("已登记", body.get("message", ""))
        popen.assert_not_called()

    def test_registered_project_still_opens(self):
        with patch("subprocess.Popen") as popen:
            st, body = _http_json(self.url, "POST", "/v1/open", body={"path": str(self.proj)})
        self.assertEqual(st, 200, body)
        self.assertTrue(body.get("ok"))
        popen.assert_called_once()
        args = popen.call_args.args[0]
        opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
        self.assertEqual(args[0], opener)
        self.assertEqual(os.path.normpath(args[1]), os.path.normpath(str(self.proj)))

    def test_missing_dir_still_400(self):
        st, body = _http_json(self.url, "POST", "/v1/open", body={"path": str(self.home / "nope")})
        self.assertEqual(st, 400)
        self.assertEqual(body.get("error"), "invalid_path")

    def test_restart_dry_run_quotes_interpreter(self):
        weird = "/tmp/py bin/python"
        spaced = str(self.home / "has space")
        with patch("sys.executable", weird), patch(
            "infocenter.crew.running_daemons",
            return_value=[{"dir": spaced, "alive": True}],
        ):
            st, body = _http_json(self.url, "POST", "/v1/restart", body={"dry_run": True})
        self.assertEqual(st, 200, body)
        script = body["script"]
        quoted = shlex.quote(weird)
        self.assertIn(f"{quoted} -m infocenter crew down", script)
        self.assertIn(f"{quoted} -m infocenter stop", script)
        self.assertIn(f"{quoted} -m infocenter serve --detach", script)
        self.assertIn(f"{quoted} -m infocenter crew up --dir {shlex.quote(spaced)}", script)
        self.assertNotIn(f'"{weird}"', script)
