"""M6-8 守护那一半：new 为空但本项目还有未读时，按间隔读一次收件箱。

不改 events 游标、不回放。build_prompt 里「只处理上面列出的信」保持原句。
心跳为 0 时兜底间隔等于 poll，并且不得比 poll 更密。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from infocenter import client, crew
from infocenter.server import make_handler
from infocenter.store import Store


class FallbackPromptTest(unittest.TestCase):
    def test_interval_uses_heartbeat_or_poll_floor(self):
        self.assertEqual(crew._unread_fallback_interval(0, 5), 5)
        self.assertEqual(crew._unread_fallback_interval(0, 30), 30)
        self.assertEqual(crew._unread_fallback_interval(10, 5), 10)
        self.assertEqual(crew._unread_fallback_interval(3, 5), 5)

    def test_build_prompt_keeps_listed_only_sentence(self):
        text = crew.build_prompt(
            "grok", "/tmp/p",
            [{"id": "m1", "from": "opencode", "subject": "新"}],
            1,
        )
        self.assertIn("只处理上面列出的、属于本项目的信", text)
        self.assertNotIn("事件流没再送出", text)

    def test_fallback_prompt_names_skipped_mail(self):
        text = crew.build_unread_fallback_prompt(
            "grok", "/tmp/p",
            [{"id": "m-old", "from": "opencode", "subject": "旧", "workspace": "/tmp/p"}],
        )
        self.assertIn("[m-old]", text)
        self.assertIn("这些是事件流没再送出的未读", text)
        self.assertIn("read", text)
        self.assertNotIn("只处理上面列出的、属于本项目的信", text)


class WatchFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self._prev_window = os.environ.pop("INFOCENTER_WINDOW", None)
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[
                ("opencode", "OpenCode", "coordinator"),
                ("grok", "Grok", ""),
            ],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()
        if self._prev_window is None:
            os.environ.pop("INFOCENTER_WINDOW", None)
        else:
            os.environ["INFOCENTER_WINDOW"] = self._prev_window

    def _config(self, heartbeat: int = 0) -> None:
        (self.home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "opencode", "role": "manager"},
                    {"name": "grok", "headless": "echo ok", "poll": 5, "heartbeat": heartbeat},
                ]
            }),
            encoding="utf-8",
        )

    def _project(self) -> str:
        return str(Path(tempfile.mkdtemp(dir=str(self.home), prefix="m68")).resolve())

    def test_heartbeat_zero_matches_poll_and_is_not_denser(self):
        project = self._project()
        self._config(0)
        inbox_queries = []
        fired_at = []

        def fake_request(_url, _method, path, _agent, query=None, timeout=None, **_k):
            if path == "/v1/inbox":
                inbox_queries.append(dict(query or {}))
                return {"messages": [{
                    "id": "m-old", "from": "opencode", "subject": "旧", "workspace": project,
                }]}
            if path != "/v1/events":
                raise AssertionError(path)
            # 长轮询被提前叫醒：每 2s 返回一次，短于 poll=5。兜底不得跟着每轮都起。
            clock_box[0] += 2
            if clock_box[0] > 12:
                raise StopIteration("done")
            return {"cursor": 1, "unread_total": 1, "new": []}

        clock_box = [0.0]

        def fake_monotonic():
            return clock_box[0]

        prompts = []

        def fake_headless(_template, prompt, _proj, agent=None, timeout=None):
            fired_at.append(clock_box[0])
            prompts.append(prompt)
            return "ok"

        with (
            patch.object(crew.time, "monotonic", side_effect=fake_monotonic),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", project, self.url, poll=5, heartbeat=0)

        self.assertEqual(fired_at, [6, 12])
        self.assertEqual(len(inbox_queries), 2)
        self.assertEqual(inbox_queries[0]["status"], "unread")
        self.assertEqual(inbox_queries[0]["workspace"], project)
        self.assertIn("[m-old]", prompts[0])
        self.assertIn("这些是事件流没再送出的未读", prompts[0])

    def test_short_heartbeat_still_waits_at_least_poll(self):
        project = self._project()
        self._config(3)
        fired_at = []
        clock_box = [0.0]

        def fake_request(_url, _method, path, _agent, query=None, timeout=None, **_k):
            if path == "/v1/inbox":
                return {"messages": [{
                    "id": "m-old", "from": "opencode", "subject": "旧", "workspace": project,
                }]}
            clock_box[0] += 2
            if clock_box[0] > 12:
                raise StopIteration("done")
            return {"cursor": 1, "unread_total": 1, "new": []}

        fallback_at = []
        heartbeat_at = []

        def fake_headless(_template, prompt, _proj, agent=None, timeout=None):
            if "这些是事件流没再送出的未读" in prompt:
                fallback_at.append(clock_box[0])
            elif "心跳检查" in prompt:
                heartbeat_at.append(clock_box[0])
            else:
                self.fail(prompt[:120])
            return "ok"

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock_box[0]),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", project, self.url, poll=5, heartbeat=3)

        # 心跳 3s 比 poll 密，兜底仍守 poll 下限。规划心跳走自己的间隔，不并进兜底。
        self.assertEqual(fallback_at, [6, 12])
        self.assertEqual(heartbeat_at, [4, 10])

    def test_configured_heartbeat_is_the_interval_when_slower_than_poll(self):
        project = self._project()
        self._config(30)
        fired_at = []
        clock_box = [0.0]

        def fake_request(_url, _method, path, _agent, query=None, timeout=None, **_k):
            if path == "/v1/inbox":
                return {"messages": [{
                    "id": "m-old", "from": "opencode", "subject": "旧", "workspace": project,
                }]}
            clock_box[0] += 5
            if clock_box[0] > 30:
                raise StopIteration("done")
            return {"cursor": 1, "unread_total": 1, "new": []}

        def fake_headless(*_a, **_k):
            fired_at.append(clock_box[0])
            return "ok"

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock_box[0]),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", project, self.url, poll=5, heartbeat=30)

        self.assertEqual(fired_at, [30])

    def test_fresh_mail_does_not_open_inbox(self):
        project = self._project()
        self._config(0)
        fresh = [{"id": "m-new", "from": "opencode", "subject": "新", "workspace": project}]
        payloads = [
            {"cursor": 3, "unread_total": 2, "new": fresh},
        ]
        inbox_calls = []
        clock_box = [0.0]

        def fake_request(_url, _method, path, _agent, query=None, timeout=None, **_k):
            if path == "/v1/inbox":
                inbox_calls.append(dict(query or {}))
                return {"messages": [{
                    "id": "m-old", "from": "opencode", "subject": "旧", "workspace": project,
                }]}
            if not payloads:
                raise StopIteration("done")
            clock_box[0] += 100
            return payloads.pop(0)

        prompts = []

        def fake_headless(_template, prompt, _proj, agent=None, timeout=None):
            prompts.append(prompt)
            return "ok"

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock_box[0]),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(self.home, "grok", project, self.url, poll=5, heartbeat=0)

        self.assertEqual(inbox_calls, [])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0], crew.build_prompt("grok", project, fresh, 2, waiting=0))
        self.assertIn("只处理上面列出的、属于本项目的信", prompts[0])

    def test_released_claim_is_delivered_once_then_stops_after_read(self):
        project = self._project()
        self._config(0)
        other = "w-m68-other"
        daemon = "w-m68-daemon"
        old = self.store.send("opencode", ["grok"], "旧封", "被越过", workspace=project)
        client.request(
            self.url, "GET", "/v1/events", "grok",
            query={"since": "0", "wait": "0", "workspace": project},
            window=other,
        )
        new = self.store.send("opencode", ["grok"], "新封", "推高游标", workspace=project)
        took_new = client.request(
            self.url, "GET", "/v1/events", "grok",
            query={"since": "0", "wait": "0", "workspace": project},
            window=daemon,
        )
        self.assertNotIn(old["id"], [item["id"] for item in took_new["new"]])
        self.assertIn(new["id"], [item["id"] for item in took_new["new"]])
        cursor = int(took_new["cursor"])
        client.request(
            self.url, "POST", f"/v1/messages/{new['id']}/read", "grok",
            {}, window=daemon,
        )
        self.store.leave_window(other, agent="grok")
        after = client.request(
            self.url, "GET", "/v1/events", "grok",
            query={"since": str(cursor), "wait": "0", "workspace": project},
            window=daemon,
        )
        self.assertNotIn(old["id"], [item["id"] for item in after["new"]])
        self.assertGreaterEqual(after["unread_total"], 1)
        box = client.request(
            self.url, "GET", "/v1/inbox", "grok",
            query={"status": "unread", "workspace": project},
            window=daemon,
        )
        self.assertIn(old["id"], [item["id"] for item in box["messages"]])
        crew._cursor_file(self.home, crew._key("grok", project)).write_text(str(cursor))

        prompts = []
        inbox_ids = []
        clock_box = [1000.0]
        event_calls = {"n": 0}

        def fake_request(url, method, path, agent, body=None, query=None, timeout=30, window=None, pid=None):
            if path == "/v1/events":
                event_calls["n"] += 1
                if event_calls["n"] > 3:
                    raise StopIteration("done")
                query = dict(query or {})
                query["wait"] = "0"
                clock_box[0] += 5
                return client.request(
                    url, method, path, agent, body, query, timeout, window, pid,
                )
            if path == "/v1/inbox":
                payload = client.request(
                    url, method, path, agent, body, query, timeout, window, pid,
                )
                inbox_ids.append([item["id"] for item in payload.get("messages") or []])
                return payload
            raise AssertionError(path)

        def fake_headless(_template, prompt, _proj, agent=None, timeout=None):
            prompts.append(prompt)
            if old["id"] in prompt:
                client.request(
                    self.url, "POST", f"/v1/messages/{old['id']}/read", "grok",
                    {}, window=daemon,
                )
            return "ok"

        with (
            patch.object(crew.time, "monotonic", side_effect=lambda: clock_box[0]),
            patch.object(crew, "request", side_effect=fake_request),
            patch.object(crew, "_run_headless", side_effect=fake_headless),
        ):
            with self.assertRaises(StopIteration):
                crew.watch(
                    self.home, "grok", project, self.url,
                    poll=5, heartbeat=0, window=daemon,
                )

        self.assertGreaterEqual(event_calls["n"], 3)
        self.assertEqual(len(prompts), 1)
        self.assertIn(old["id"], prompts[0])
        self.assertIn("这些是事件流没再送出的未读", prompts[0])
        self.assertNotIn("只处理上面列出的、属于本项目的信", prompts[0])
        self.assertEqual(inbox_ids[0], [old["id"]])
        self.assertTrue(all(old["id"] not in ids for ids in inbox_ids[1:]))


if __name__ == "__main__":
    unittest.main()
