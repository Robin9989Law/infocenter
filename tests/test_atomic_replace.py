"""对账轮：JSON 落盘统一走 store.atomic_replace。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import config, crew, server
from infocenter.store import atomic_replace
from infocenter.sync import _atomic_write


class AtomicReplaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_replaces_without_leaving_a_temp_file(self):
        path = self.home / "note.json"
        path.write_bytes(b"old")
        with patch("infocenter.store.os.fsync") as fsync:
            atomic_replace(path, b'{"ok": true}')
        fsync.assert_called_once()
        self.assertEqual(path.read_bytes(), b'{"ok": true}')
        self.assertEqual(list(self.home.glob(".*tmp")), [])
        self.assertFalse((self.home / "note.json.tmp").exists())

    def test_failed_fsync_keeps_the_old_file_and_drops_the_temp(self):
        path = self.home / "note.json"
        path.write_bytes(b"OLD")
        with patch("infocenter.store.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                atomic_replace(path, b"NEW")
        self.assertEqual(path.read_bytes(), b"OLD")
        self.assertEqual(list(self.home.glob(".*tmp")), [])

    def test_config_save_keeps_indented_unicode(self):
        payload = {"agents": [{"name": "甲", "description": "测试"}]}
        config.save(self.home, payload)
        text = (self.home / "config.json").read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self.assertIn("甲", text)
        self.assertEqual(list(self.home.glob(".*tmp")), [])

    def test_server_meta_stays_ascii_escaped(self):
        server._write_server_meta(self.home, {"url": "http://127.0.0.1:9", "note": "甲"})
        raw = (self.home / "server.json").read_bytes()
        self.assertEqual(raw, json.dumps({"url": "http://127.0.0.1:9", "note": "甲"}).encode("utf-8"))
        self.assertNotIn("甲".encode("utf-8"), raw)

    def test_crew_meta_keeps_unicode_and_pid_record_is_atomic(self):
        key = crew._key("grok", str(self.home))
        meta = {"agent": "甲", "key": key}
        crew._write_meta(self.home, key, meta)
        meta_path = crew._meta_file(self.home, key)
        self.assertEqual(
            meta_path.read_text(encoding="utf-8"),
            json.dumps(meta, ensure_ascii=False),
        )
        crew._write_pid_record(self.home, key, 42, "2026-10-16T00:00:00Z")
        pid_path = crew._pid_file(self.home, key)
        original = pid_path.read_bytes()
        self.assertEqual(
            original,
            json.dumps({"pid": 42, "started_at": "2026-10-16T00:00:00Z", "marker": "ic-crew"}).encode(),
        )
        with patch("infocenter.store.os.replace", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                crew._write_pid_record(self.home, key, 99, "2026-10-16T01:00:00Z")
        self.assertEqual(pid_path.read_bytes(), original)
        self.assertEqual(list(pid_path.parent.glob(".*tmp")), [])

    def test_sync_still_writes_the_given_bytes(self):
        directory = self.home / "out"
        _atomic_write(directory, "m1.json", b'{"id":"m1"}\n')
        self.assertEqual((directory / "m1.json").read_bytes(), b'{"id":"m1"}\n')
        self.assertEqual(list(directory.glob(".*tmp")), [])


if __name__ == "__main__":
    unittest.main()
