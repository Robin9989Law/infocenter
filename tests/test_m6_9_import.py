"""M6-9：联邦导入校验父信、workspace 进幂等比较、提交后才叫醒。

八条判据都在这个文件里。不改历史库。临时目录，不动现网。
"""

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from infocenter.cli import main
from infocenter.store import Store

DANGLING_SQL = """
SELECT id, thread_id, reply_to, from_agent
FROM messages m
WHERE m.reply_to IS NOT NULL AND TRIM(m.reply_to) != ''
  AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.id = m.reply_to)
ORDER BY id
"""


def _store(home: Path) -> Store:
    return Store(
        home / "mailbox.db",
        seeds=[("grok", "Grok", ""), ("kimi", "Kimi", ""), ("opencode", "OpenCode", "")],
    )


def _envelope(msg_id: str, **overrides) -> dict:
    payload = {
        "v": 1,
        "kind": "message",
        "id": msg_id,
        "thread_id": msg_id,
        "reply_to": None,
        "from": "bob.grok",
        "to": ["alice.kimi"],
        "subject": "外来",
        "body": "正文",
        "priority": "normal",
        "workspace": "/tmp/box",
        "created_at": "2026-10-16T00:00:00Z",
    }
    payload.update(overrides)
    payload["id"] = msg_id
    return payload


def _rows(store: Store, sql: str, args=()):
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _count(store: Store, msg_id: str) -> int:
    return _rows(store, "SELECT COUNT(*) FROM messages WHERE id = ?", (msg_id,))[0][0]


def _workspace_of(store: Store, msg_id: str):
    return _rows(store, "SELECT workspace FROM messages WHERE id = ?", (msg_id,))[0][0]


def _dangling(store: Store):
    return _rows(store, DANGLING_SQL)


def _spy(store: Store) -> list:
    called = []

    def _signal():
        called.append(1)

    store._signal = _signal
    return called


def _block_events(store: Store, **kwargs):
    entered = threading.Event()
    real_wait = store._cond.wait

    def wrapped(timeout=None):
        entered.set()
        return real_wait(timeout)

    store._cond.wait = wrapped
    box = {}

    def run():
        box["t0"] = time.monotonic()
        box["result"] = store.events("kimi", **kwargs)
        box["elapsed"] = time.monotonic() - box["t0"]

    thread = threading.Thread(target=run)
    thread.start()
    if not entered.wait(2):
        thread.join(0.2)
        raise AssertionError(f"events 没有进入等待：{box}")
    return thread, box


class M69ImportTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("INFOCENTER_AGENT", None)
        os.environ.pop("INFOCENTER_WINDOW", None)
        os.environ.pop("INFOCENTER_NO_PID", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.store = _store(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_parent_imports_then_links(self):
        parent_id = "m6909000000000001"
        reply_id = "m6909000000000002"
        reply = self.store.import_remote(
            _envelope(
                reply_id,
                thread_id=parent_id,
                reply_to=parent_id,
                subject="Re: 问",
                body="先到的回复",
                created_at="2026-10-16T00:01:00Z",
            ),
            ["kimi"],
        )
        self.assertEqual(reply, {"status": "imported", "parent": "missing"})
        self.assertEqual(_count(self.store, reply_id), 1)
        self.assertEqual([row[0] for row in _dangling(self.store)], [reply_id])

        parent = self.store.import_remote(
            _envelope(
                parent_id,
                thread_id=parent_id,
                reply_to=None,
                subject="问",
                body="后到的父信",
                created_at="2026-10-16T00:00:00Z",
            ),
            ["kimi"],
        )
        self.assertEqual(parent, {"status": "imported", "parent": "none"})
        thread = self.store.thread("kimi", parent_id)
        self.assertEqual([item["id"] for item in thread["messages"]], [parent_id, reply_id])
        self.assertEqual(thread["messages"][1]["reply_to"], parent_id)
        self.assertEqual(_dangling(self.store), [])

        present = self.store.import_remote(
            _envelope(
                "m6909000000000003",
                thread_id=parent_id,
                reply_to=parent_id,
                subject="Re: 问",
                body="父信已在",
                created_at="2026-10-16T00:02:00Z",
            ),
            ["kimi"],
        )
        self.assertEqual(present, {"status": "imported", "parent": "present"})

    def test_parent_thread_mismatch_conflicts(self):
        parent_id = "m6909000000000011"
        child_id = "m6909000000000012"
        parent = self.store.import_remote(
            _envelope(parent_id, thread_id=parent_id),
            ["kimi"],
        )
        self.assertEqual(parent["parent"], "none")
        calls = _spy(self.store)
        conflict = self.store.import_remote(
            _envelope(
                child_id,
                thread_id="m6909000000000019",
                reply_to=parent_id,
            ),
            ["kimi"],
        )
        self.assertEqual(conflict, {"status": "conflict"})
        self.assertEqual(_count(self.store, child_id), 0)
        self.assertEqual(calls, [])

    def test_reply_to_self_conflicts(self):
        msg_id = "m6909000000000021"
        calls = _spy(self.store)
        result = self.store.import_remote(
            _envelope(msg_id, thread_id=msg_id, reply_to=msg_id),
            ["kimi"],
        )
        self.assertEqual(result, {"status": "conflict"})
        self.assertEqual(_count(self.store, msg_id), 0)
        self.assertEqual(calls, [])

    def test_workspace_mismatch_keeps_first(self):
        msg_id = "m6909000000000031"
        first = self.store.import_remote(
            _envelope(msg_id, workspace="/proj/a"),
            ["kimi"],
        )
        self.assertEqual(first["status"], "imported")
        calls = _spy(self.store)
        second = self.store.import_remote(
            _envelope(msg_id, workspace="/proj/b"),
            ["kimi"],
        )
        self.assertEqual(second, {"status": "conflict"})
        self.assertEqual(_workspace_of(self.store, msg_id), "/proj/a")
        self.assertEqual(_count(self.store, msg_id), 1)
        self.assertEqual(calls, [])

    def test_same_workspace_including_blank_is_duplicate(self):
        msg_id = "m6909000000000041"
        blank = self.store.import_remote(_envelope(msg_id, workspace=None), ["kimi"])
        self.assertEqual(blank["status"], "imported")
        again = self.store.import_remote(_envelope(msg_id, workspace=""), ["kimi"])
        self.assertEqual(again, {"status": "duplicate"})
        spaces = self.store.import_remote(_envelope(msg_id, workspace="   "), ["kimi"])
        self.assertEqual(spaces, {"status": "duplicate"})
        self.assertEqual(_count(self.store, msg_id), 1)

        # 库里留着空串（历史行）。再导入空 workspace 仍是 duplicate，且不回写成 NULL。
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("UPDATE messages SET workspace = '' WHERE id = ?", (msg_id,))
            conn.commit()
        finally:
            conn.close()
        calls = _spy(self.store)
        kept = self.store.import_remote(_envelope(msg_id, workspace=None), ["kimi"])
        self.assertEqual(kept, {"status": "duplicate"})
        self.assertEqual(_workspace_of(self.store, msg_id), "")
        self.assertEqual(calls, [])

        same = "m6909000000000042"
        self.store.import_remote(_envelope(same, workspace="/proj/a"), ["kimi"])
        dup = self.store.import_remote(_envelope(same, workspace="/proj/a"), ["kimi"])
        self.assertEqual(dup, {"status": "duplicate"})
        self.assertEqual(_count(self.store, same), 1)

    def test_import_wakes_events_within_two_seconds(self):
        msg_id = "m6909000000000051"
        thread, box = _block_events(self.store, since=0, wait=5)
        # 与这次等待无关的叫醒不该把空轮询提前放回去。
        self.store._signal()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive())
        result = self.store.import_remote(_envelope(msg_id), ["kimi"])
        self.assertEqual(result["status"], "imported")
        thread.join(3)
        self.assertFalse(thread.is_alive(), "导入后 events 仍在睡")
        self.assertLess(box["elapsed"], 2)
        self.assertIn(msg_id, [item["id"] for item in box["result"]["new"]])

    def test_receipt_wakes_events_with_fresh_unread_total(self):
        msg_id = "m6909000000000061"
        imported = self.store.import_remote(_envelope(msg_id), ["kimi"])
        self.assertEqual(imported["status"], "imported")
        cursor = self.store.events("kimi", since=0, wait=0)["cursor"]
        thread, box = _block_events(self.store, since=cursor, wait=5)
        receipt = self.store.apply_receipt(msg_id, "kimi", "2026-10-16T01:00:00Z")
        self.assertEqual(receipt, {"status": "applied"})
        thread.join(3)
        self.assertFalse(thread.is_alive(), "回执后 events 仍在睡")
        self.assertLess(box["elapsed"], 2)
        self.assertEqual(box["result"]["unread_total"], 0)
        self.assertEqual(box["result"]["new"], [])

        calls = _spy(self.store)
        self.assertEqual(
            self.store.apply_receipt(msg_id, "kimi", "2026-10-16T01:00:00Z"),
            {"status": "duplicate"},
        )
        self.assertEqual(self.store.apply_receipt("m6909000000000069", "kimi", "2026-10-16T01:00:00Z"), {"status": "absent"})
        self.assertEqual(calls, [])

    def test_archived_receipt_does_not_signal(self):
        msg_id = "m6909000000000071"
        self.store.import_remote(_envelope(msg_id), ["kimi"])
        self.store.mark("kimi", msg_id, "archived")
        calls = _spy(self.store)
        result = self.store.apply_receipt(msg_id, "kimi", "2026-10-16T01:00:00Z")
        self.assertEqual(result, {"status": "archived"})
        self.assertEqual(calls, [])

    def test_duplicate_import_does_not_signal(self):
        msg_id = "m6909000000000081"
        self.store.import_remote(_envelope(msg_id), ["kimi"])
        calls = _spy(self.store)
        self.assertEqual(self.store.import_remote(_envelope(msg_id), ["kimi"])["status"], "duplicate")
        self.assertEqual(self.store.import_remote(_envelope(msg_id), [])["status"], "skipped")
        self.assertEqual(calls, [])

    def test_existing_child_thread_mismatch_blocks_parent(self):
        parent_id = "m6909000000000091"
        child_id = "m6909000000000092"
        child_thread = "m6909000000000093"
        child = self.store.import_remote(
            _envelope(
                child_id,
                thread_id=child_thread,
                reply_to=parent_id,
                created_at="2026-10-16T00:01:00Z",
            ),
            ["kimi"],
        )
        self.assertEqual(child["parent"], "missing")
        calls = _spy(self.store)
        blocked = self.store.import_remote(
            _envelope(parent_id, thread_id=parent_id, created_at="2026-10-16T00:00:00Z"),
            ["kimi"],
        )
        self.assertEqual(blocked, {"status": "conflict"})
        self.assertEqual(_count(self.store, parent_id), 0)
        self.assertEqual([row[0] for row in _dangling(self.store)], [child_id])
        self.assertEqual(calls, [])

        aligned = self.store.import_remote(
            _envelope(parent_id, thread_id=child_thread, created_at="2026-10-16T00:00:00Z"),
            ["kimi"],
        )
        self.assertEqual(aligned, {"status": "imported", "parent": "none"})
        self.assertEqual(_dangling(self.store), [])


class M69SyncWarningTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("INFOCENTER_AGENT", None)
        os.environ.pop("INFOCENTER_WINDOW", None)
        os.environ.pop("INFOCENTER_NO_PID", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, home: Path, project: Path):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--home", str(home), "sync", "import", "--dir", str(project), "--json"]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_sync_warns_when_parent_missing_and_not_again(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps({
                "agents": [
                    {"name": "grok", "description": "Grok"},
                    {"name": "kimi", "description": "Kimi"},
                ],
                "node": "alice",
            }),
            encoding="utf-8",
        )
        reply_id = "m69090000000000a1"
        parent_id = "m69090000000000a2"
        peer = project / ".infocenter" / "outbox" / "bob"
        peer.mkdir(parents=True)
        payload = {
            "v": 1,
            "kind": "message",
            "id": reply_id,
            "thread_id": parent_id,
            "reply_to": parent_id,
            "from": "bob.grok",
            "to": ["alice.kimi"],
            "subject": "Re: 问",
            "body": "先到",
            "priority": "normal",
            "workspace": "/tmp/elsewhere",
            "workspace_name": project.name,
            "created_at": "2026-10-16T00:01:00Z",
        }
        (peer / f"{reply_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        code, out, err = self._run(home, project)
        self.assertEqual(code, 0, err or out)
        self.assertEqual(json.loads(out)["imported"], 1)
        self.assertEqual(
            err,
            f"warning: {reply_id}.json 的 parent=missing，回复的父信还不在库里，仍导入\n",
        )
        code, out, err = self._run(home, project)
        self.assertEqual(code, 0, err or out)
        self.assertEqual(json.loads(out)["imported"], 0)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
