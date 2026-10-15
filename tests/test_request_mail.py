"""对账轮：ApiError→MailError 只留 client.request_mail。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import client, crew
from infocenter.client import ApiError
from infocenter.store import MailError


class RequestMailTest(unittest.TestCase):
    def test_translates_api_error(self):
        err = ApiError(404, {"error": "not_found", "message": "没有这个接口"})
        with patch.object(client, "request", side_effect=err):
            with self.assertRaises(MailError) as ctx:
                client.request_mail("http://127.0.0.1:9", "GET", "/v1/x", "cursor")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(str(ctx.exception), "没有这个接口")
        self.assertIs(ctx.exception.__cause__, err)

    def test_call_still_ensures_server_first(self):
        order = []

        def ensure(*_a, **_k):
            order.append("ensure")
            return "http://127.0.0.1:9"

        def req(*_a, **_k):
            order.append("request")
            return {"ok": True}

        with patch.object(client, "ensure_server", side_effect=ensure), \
                patch.object(client, "request", side_effect=req):
            self.assertEqual(client.call(None, "cursor", "GET", "/health"), {"ok": True})
        self.assertEqual(order, ["ensure", "request"])

    def test_crew_api_does_not_start_the_server(self):
        def ensure(*_a, **_k):
            raise AssertionError("守护循环不应该把服务拉起来")

        err = ApiError(409, {"error": "conflict", "message": "窗口被占用"})
        with patch.object(client, "ensure_server", side_effect=ensure), \
                patch.object(client, "request", return_value={"windows": []}):
            self.assertEqual(
                crew._api("http://127.0.0.1:9", "GET", "/v1/windows", "cursor"),
                {"windows": []},
            )
        with patch.object(client, "ensure_server", side_effect=ensure), \
                patch.object(client, "request", side_effect=err):
            with self.assertRaises(MailError) as ctx:
                crew._api("http://127.0.0.1:9", "POST", "/v1/windows/w/pause", "cursor", {})
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIs(ctx.exception.__cause__, err)


if __name__ == "__main__":
    unittest.main()
