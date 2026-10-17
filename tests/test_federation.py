import inspect
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from infocenter import config
from infocenter.cli import main
from infocenter.httpapi import handle
from infocenter.store import Store
from infocenter.sync import COUNT_KEYS, MAX_ENVELOPE, is_wire_name

_HAS_IMPORT = hasattr(Store, "import_remote")
_HAS_RECEIPT = hasattr(Store, "apply_receipt")
_HAS_SEND_NODE = "local_node" in inspect.signature(Store.send).parameters
_HAS_REPLY_NODE = "local_node" in inspect.signature(Store.reply).parameters


def _zero():
    return {key: 0 for key in COUNT_KEYS}


def _write_node(home: Path, node: str, peers=None, outbox_dir=None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    document = {
        "agents": [
            {"name": "grok", "description": "Grok"},
            {"name": "kimi", "description": "Kimi"},
            {"name": "opencode", "description": "OpenCode"},
        ],
        "node": node,
    }
    if peers is not None:
        document["peers"] = peers
    if outbox_dir is not None:
        document["outbox_dir"] = outbox_dir
    (home / "config.json").write_text(json.dumps(document), encoding="utf-8")


def _store(home: Path) -> Store:
    return Store(
        home / "mailbox.db",
        seeds=[("grok", "Grok", ""), ("kimi", "Kimi", ""), ("opencode", "OpenCode", "")],
    )


def _run(home, project, command="export", as_json=True):
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["--home", str(home), "sync", command, "--dir", str(project)]
    if as_json:
        argv.append("--json")
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _plant(directory: Path, **overrides) -> Path:
    payload = {
        "v": 1,
        "kind": "message",
        "id": "m12345678abcdef",
        "thread_id": "m12345678abcdef",
        "reply_to": None,
        "from": "bob.grok",
        "to": ["alice.kimi"],
        "subject": "外来",
        "body": "正文",
        "priority": "normal",
        "workspace": "/tmp/elsewhere",
        "workspace_name": directory.parent.parent.parent.name
        if directory.parent.name == "outbox" and directory.parent.parent.name == ".infocenter"
        else "elsewhere",
        "created_at": "2026-10-14T00:00:00Z",
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class WireNameTest(unittest.TestCase):
    def test_shapes(self):
        self.assertTrue(is_wire_name("robin.kimi"))
        self.assertTrue(is_wire_name("node1.grok"))
        self.assertTrue(is_wire_name("Robin.Kimi"))
        self.assertTrue(is_wire_name("a-b.c"))
        self.assertTrue(is_wire_name(("a" * 32) + ".b"))
        self.assertTrue(is_wire_name(("a" * 31) + "." + ("b" * 32)))
        self.assertFalse(is_wire_name("grok"))
        self.assertFalse(is_wire_name("a.b.c"))
        self.assertFalse(is_wire_name("robin."))
        self.assertFalse(is_wire_name(".grok"))
        self.assertFalse(is_wire_name("-a.b"))
        self.assertFalse(is_wire_name("a..b"))
        self.assertFalse(is_wire_name(("a" * 32) + "." + ("b" * 32)))
        self.assertFalse(is_wire_name(None))


class FederationConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_do_not_create_config(self):
        settings = config.federation_settings(self.home)
        self.assertEqual(settings, {"node": None, "peers": [], "outbox_dir": None})
        self.assertFalse((self.home / "config.json").exists())

    def test_add_and_remove_keep_federation_keys(self):
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "agents": [{"name": "grok", "description": "Grok"}],
            "node": "Alice",
            "peers": ["robin"],
            "outbox_dir": "relay",
            "extra": 1,
        }), encoding="utf-8")
        self.assertEqual(set(config.load(self.home)), {"agents"})
        config.add(self.home, "kimi", "Kimi")
        raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["node"], "Alice")
        self.assertEqual(raw["peers"], ["robin"])
        self.assertEqual(raw["outbox_dir"], "relay")
        self.assertEqual(raw["extra"], 1)
        self.assertEqual({item["name"] for item in raw["agents"]}, {"grok", "kimi"})
        settings = config.federation_settings(self.home)
        self.assertEqual(settings["node"], "alice")
        self.assertEqual(settings["peers"], ["robin"])
        self.assertEqual(settings["outbox_dir"], "relay")
        self.assertTrue(config.remove(self.home, "kimi"))
        raw = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["node"], "Alice")
        self.assertEqual([item["name"] for item in raw["agents"]], ["grok"])


class FederationSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ok(self, home, project, command="export"):
        code, out, err = _run(home, project, command)
        self.assertEqual(code, 0, err or out)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(set(payload), set(COUNT_KEYS))
        return payload

    def test_unconfigured_node_exits_2_and_writes_nothing(self):
        home = self.root / "fresh"
        project = self.root / "proj"
        project.mkdir()
        code, _out, err = _run(home, project, "once")
        self.assertEqual(code, 2)
        self.assertIn("node_not_configured", err)
        self.assertFalse(home.exists())
        self.assertFalse((project / ".infocenter").exists())

    def test_blank_node_is_not_configured(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
        raw["node"] = ""
        (home / "config.json").write_text(json.dumps(raw), encoding="utf-8")
        code, _out, err = _run(home, project, "once")
        self.assertEqual(code, 2)
        self.assertIn("node_not_configured", err)
        self.assertFalse((home / "mailbox.db").exists())
        self.assertFalse((home / "federation.lock").exists())
        self.assertFalse((project / ".infocenter").exists())

    def test_invalid_node_and_missing_dir_exit_2(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "bad.node")
        code, _out, err = _run(home, project, "export")
        self.assertEqual(code, 2)
        self.assertIn("invalid_node", err)
        self.assertFalse((home / "mailbox.db").exists())
        self.assertFalse((home / "federation.lock").exists())
        self.assertFalse((project / ".infocenter").exists())
        code, _out, err = _run(home, self.root / "missing", "once")
        self.assertEqual(code, 2)
        self.assertIn("invalid_dir", err)

    def test_invalid_peers_and_outbox(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice", peers=["bad.node"])
        code, _out, err = _run(home, project, "import")
        self.assertEqual(code, 2)
        self.assertIn("invalid_peers", err)
        self.assertFalse((project / ".infocenter").exists())
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        _write_node(home, "alice", outbox_dir=str(blocker))
        code, _out, err = _run(home, project, "export")
        self.assertEqual(code, 2)
        self.assertIn("invalid_outbox", err)

    def test_star_and_bare_recipients_do_not_export(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        store.send("grok", "*", "广播", "正文", workspace=str(project))
        store.send("grok", ["kimi"], "本地", "正文", workspace=str(project))
        store.send("grok", ["robin.kimi"], "无主", "正文")
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 0)
        self.assertFalse((project / ".infocenter").exists())
        self.assertFalse((home / "server.json").exists())

    def test_other_basename_stays_home(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        store.send("grok", ["robin.kimi"], "别的项目", "正文", workspace="/var/other-project-name")
        kept = store.send("grok", ["robin.kimi"], "这条要", "正文", workspace=str(project))
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 1)
        files = list((project / ".infocenter" / "outbox" / "alice").glob("*.json"))
        self.assertEqual([path.name for path in files], [f"{kept['id']}.json"])

    def test_basename_scope_still_exports(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        unused_base = (self.root / "unused").resolve()
        store.send(
            "grok",
            ["robin.kimi"],
            "同名目录",
            "正文",
            workspace=str(unused_base / project.name),
        )
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 1)

    def test_mixed_recipients_export_once(self):
        home = self.root / "home"
        project = (self.root / "信息中心").resolve()
        project.mkdir()
        _write_node(home, "Alice")
        store = _store(home)
        sent = store.send(
            "grok",
            ["kimi", "robin.opencode"],
            "混合",
            "两边都有",
            workspace=str(project),
            priority="high",
        )
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 1)
        self.assertIn("kimi", sent["to"])
        self.assertIn("robin.opencode", sent["to"])
        self.assertEqual(store.get_message("grok", sent["id"])["from"], "grok")
        directory = project / ".infocenter" / "outbox" / "alice"
        files = list(directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, f"{sent['id']}.json")
        raw = files[0].read_bytes()
        self.assertIn("两边都有".encode(), raw)
        self.assertNotIn(b"\\u", raw)
        data = json.loads(raw)
        self.assertEqual(data["from"], "alice.grok")
        self.assertEqual(data["to"], ["alice.kimi", "robin.opencode"])
        self.assertEqual(data["subject"], "混合")
        self.assertEqual(data["priority"], "high")
        self.assertEqual(data["created_at"], sent["created_at"])
        self.assertEqual(data["workspace"], str(project))
        self.assertEqual(data["workspace_name"], project.name)
        self.assertFalse(list(directory.glob(".*.tmp")))
        again = self._ok(home, project)
        self.assertEqual(again["exported"], 0)
        self.assertEqual(files[0].read_bytes(), raw)
        code, out, err = _run(home, project, "export", as_json=False)
        self.assertEqual(code, 0, err)
        self.assertIn("exported=0", out)
        self.assertIn("imported=0", out)
        lock = home / "federation.lock"
        with open(lock, "r+") as fp:
            from infocenter.flock import lock_file_ex_nb, unlock_file

            lock_file_ex_nb(fp)
            unlock_file(fp)
        self.assertFalse((home / "server.json").exists())

    def test_relative_outbox_and_tmp_sweep(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice", outbox_dir="relay")
        store = _store(home)
        sent = store.send("grok", ["robin.kimi"], "相对路径", "正文", workspace=str(project))
        node_dir = project / "relay" / "alice"
        node_dir.mkdir(parents=True)
        now = time.time()
        old = node_dir / ".stale.json.1.abcd.tmp"
        old.write_bytes(b"x")
        os.utime(old, (now - 7200, now - 7200))
        fresh = node_dir / ".fresh.json.1.abcd.tmp"
        fresh.write_bytes(b"y")
        foreign_dir = project / "relay" / "robin"
        foreign_dir.mkdir()
        foreign = foreign_dir / ".stale.json.1.abcd.tmp"
        foreign.write_bytes(b"z")
        os.utime(foreign, (now - 7200, now - 7200))
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 1)
        self.assertTrue((node_dir / f"{sent['id']}.json").is_file())
        self.assertFalse((project / ".infocenter").exists())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(foreign.exists())

    def test_export_oversize_is_rejected_once(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        store.send("grok", ["robin.kimi"], "太大", "这是一段用来撑破测试上限的正文", workspace=str(project))
        import infocenter.sync as sync

        old = sync.MAX_ENVELOPE
        sync.MAX_ENVELOPE = 80
        try:
            first = self._ok(home, project)
            second = self._ok(home, project)
        finally:
            sync.MAX_ENVELOPE = old
        self.assertEqual(first["rejected"], 1)
        self.assertEqual(first["exported"], 0)
        self.assertEqual(second["rejected"], 0)
        self.assertFalse((project / ".infocenter" / "outbox" / "alice").exists())

    def test_import_ignores_own_outbox_copies_and_symlinks(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        own = project / ".infocenter" / "outbox" / "alice"
        _plant(own, **{"from": "bob.grok", "to": ["alice.kimi"], "id": "m12345678aaa111", "thread_id": "m12345678aaa111"})
        copied = project / ".infocenter" / "outbox" / "robin (1)"
        _plant(copied, id="m12345678bbb222", thread_id="m12345678bbb222")
        conflict = project / ".infocenter" / "outbox" / "conflict"
        _plant(conflict, id="m12345678ccc333", thread_id="m12345678ccc333")
        outside = self.root / "secret.json"
        outside.write_text("{}", encoding="utf-8")
        peer = project / ".infocenter" / "outbox" / "robin"
        peer.mkdir(parents=True)
        link = peer / "m12345678ddd444.json"
        try:
            link.symlink_to(outside)
            real = self.root / "realbob"
            _plant(real, id="m12345678eee555", thread_id="m12345678eee555")
            (project / ".infocenter" / "outbox" / "bob").symlink_to(real)
        except OSError as e:
            self.skipTest(f"Symlinks not supported or permitted on this platform: {e}")
        payload = self._ok(home, project, "import")
        self.assertEqual(payload, _zero())
        self.assertFalse((home / "mailbox.db").exists())

    def test_loopback_and_foreign_only_are_skipped(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "robin"
        _plant(peer, **{"from": "alice.grok", "id": "m12345678aaa111", "thread_id": "m12345678aaa111"})
        _plant(
            peer,
            **{
                "from": "bob.grok",
                "to": ["carol.kimi"],
                "id": "m12345678bbb222",
                "thread_id": "m12345678bbb222",
            },
        )
        payload = self._ok(home, project, "import")
        self.assertEqual(payload["skipped"], 2)
        self.assertEqual(payload["imported"], 0)
        self.assertEqual(payload["rejected"], 0)
        self.assertFalse((home / "mailbox.db").exists())

    def test_peers_limit_which_directories_are_scanned(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice", peers=["Robin"])
        _plant(
            project / ".infocenter" / "outbox" / "bob",
            id="m12345678aaa111",
            thread_id="m12345678aaa111",
        )
        _plant(
            project / ".infocenter" / "outbox" / "robin",
            **{"to": ["carol.kimi"], "id": "m12345678bbb222", "thread_id": "m12345678bbb222"},
        )
        payload = self._ok(home, project, "import")
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["imported"], 0)
        self.assertFalse((home / "mailbox.db").exists())

    def test_broken_json_and_unknown_version_are_not_rejected(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "robin"
        peer.mkdir(parents=True)
        (peer / "m12345678aaa111.json").write_text("{", encoding="utf-8")
        _plant(peer, v=2, id="m12345678bbb222", thread_id="m12345678bbb222")
        first = self._ok(home, project, "import")
        second = self._ok(home, project, "import")
        self.assertEqual(first["skipped"], 2)
        self.assertEqual(first["rejected"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(second["rejected"], 0)
        self.assertFalse((home / "mailbox.db").exists())

    def test_invalid_schema_and_oversize_reject_once(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "robin"
        _plant(peer, body="")
        huge = peer / "m12345678bbb222.json"
        huge.write_bytes(b"{" + b"x" * MAX_ENVELOPE)
        first = self._ok(home, project, "import")
        second = self._ok(home, project, "import")
        self.assertEqual(first["rejected"], 2)
        self.assertEqual(first["imported"], 0)
        self.assertEqual(second["rejected"], 0)
        self.assertFalse((home / "mailbox.db").exists())

    @unittest.skipUnless(_HAS_SEND_NODE, "等 cursor 合入 Store.send(..., local_node)")
    def test_star_with_local_node_skips_wire_names(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        direct = store.send(
            "grok",
            ["robin.kimi"],
            "点名",
            "正文",
            workspace=str(project),
            local_node="alice",
        )
        store.send("grok", "*", "广播", "正文", workspace=str(project), local_node="alice")
        payload = self._ok(home, project)
        self.assertEqual(payload["exported"], 1)
        files = list((project / ".infocenter" / "outbox" / "alice").glob("*.json"))
        self.assertEqual([path.name for path in files], [f"{direct['id']}.json"])
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["subject"], "点名")

    @unittest.skipUnless(_HAS_SEND_NODE, "等 cursor 合入 Store.send(..., local_node)")
    def test_http_star_skips_wire_names(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        store.touch("robin.kimi")
        raw = json.dumps({"to": "*", "subject": "广播", "body": "正文"}).encode()
        status, message = handle("POST", "/v1/messages", {"X-Agent": "grok"}, raw, store)
        self.assertEqual(status, 201)
        self.assertEqual(sorted(message["to"]), ["kimi", "opencode"])

    @unittest.skipUnless(_HAS_REPLY_NODE, "等 cursor 合入 Store.reply(..., local_node)")
    def test_reply_passes_local_node(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        parent = store.send("kimi", ["grok"], "问", "在吗", workspace=str(project))
        store.touch("robin.opencode")
        reply = store.reply("grok", parent["id"], "在", to="*", local_node="alice")
        self.assertEqual(sorted(reply["to"]), ["kimi", "opencode"])

    @unittest.skipUnless(_HAS_IMPORT, "等 cursor 合入 Store.import_remote")
    def test_two_nodes_keep_id_and_import_twice_is_one_row(self):
        alice = self.root / "alice"
        robin = self.root / "robin"
        project = (self.root / "box").resolve()
        project.mkdir()
        _write_node(alice, "alice")
        _write_node(robin, "robin")
        sender = _store(alice)
        sent = sender.send("grok", ["kimi", "robin.kimi"], "主题", "正文", workspace=str(project))
        exported = self._ok(alice, project, "once")
        self.assertEqual(exported["exported"], 1)
        data = json.loads(next((project / ".infocenter" / "outbox" / "alice").glob("*.json")).read_text())
        self.assertEqual(data["to"], ["alice.kimi", "robin.kimi"])
        incoming = self._ok(robin, project, "once")
        self.assertEqual(incoming["imported"], 1)
        receiver = _store(robin)
        box = receiver.inbox("kimi", status="all", full=True)
        self.assertEqual(box["count"], 1)
        message = box["messages"][0]
        self.assertEqual(message["id"], sent["id"])
        self.assertEqual(message["subject"], "主题")
        self.assertEqual(message["body"], "正文")
        self.assertEqual(message["from"], "alice.grok")
        self.assertEqual(message["workspace"], str(project))
        self.assertEqual(sender.get_message("grok", sent["id"])["from"], "grok")
        names = [row["name"] for row in receiver.list_agents()]
        self.assertNotIn("alice.grok", names)
        again = self._ok(robin, project, "once")
        self.assertEqual(again, _zero())
        self.assertEqual(receiver.inbox("kimi", status="all")["count"], 1)

    @unittest.skipUnless(_HAS_IMPORT, "等 cursor 合入 Store.import_remote")
    def test_same_id_different_body_does_not_overwrite(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "bob"
        path = _plant(peer)
        self.assertEqual(self._ok(home, project, "import")["imported"], 1)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["body"] = "另一份正文"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = self._ok(home, project, "import")
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(result["imported"], 0)
        body = _store(home).inbox("kimi", status="all", full=True)["messages"][0]["body"]
        self.assertEqual(body, "正文")

    @unittest.skipUnless(_HAS_IMPORT, "等 cursor 合入 Store.import_remote")
    def test_partial_file_then_replace_imports_once(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "bob"
        peer.mkdir(parents=True)
        path = peer / "m12345678abcdef.json"
        path.write_text("{", encoding="utf-8")
        first = self._ok(home, project, "import")
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(first["imported"], 0)
        payload = {
            "v": 1,
            "kind": "message",
            "id": "m12345678abcdef",
            "thread_id": "m12345678abcdef",
            "reply_to": None,
            "from": "bob.grok",
            "to": ["alice.kimi"],
            "subject": "补全",
            "body": "正文",
            "priority": "normal",
            "workspace": "/tmp/elsewhere",
            "workspace_name": project.name,
            "created_at": "2026-10-14T00:00:00Z",
        }
        tmp = peer / f".{path.name}.{os.getpid()}.abcd.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        second = self._ok(home, project, "import")
        third = self._ok(home, project, "import")
        self.assertEqual(second["imported"], 1)
        self.assertEqual(third["imported"], 0)
        self.assertEqual(_store(home).inbox("kimi", status="all")["count"], 1)

    @unittest.skipUnless(_HAS_IMPORT, "等 cursor 合入 Store.import_remote")
    def test_reply_can_arrive_before_parent(self):
        home = self.root / "home"
        project = self.root / "proj"
        project.mkdir()
        _write_node(home, "alice")
        peer = project / ".infocenter" / "outbox" / "bob"
        parent_id = "m12345678aaa111"
        reply_id = "m12345678bbb222"
        _plant(
            peer,
            id=reply_id,
            thread_id=parent_id,
            reply_to=parent_id,
            subject="Re: 问",
            body="先到的回复",
            created_at="2026-10-14T00:01:00Z",
        )
        # 父信未到仍导入。sync 打一行 parent=missing 警告（M6-9），计数仍算 imported。
        code, out, err = _run(home, project, "import")
        self.assertEqual(code, 0, err or out)
        self.assertEqual(json.loads(out)["imported"], 1)
        self.assertIn(
            f"warning: {reply_id}.json 的 parent=missing，回复的父信还不在库里，仍导入\n",
            err,
        )
        _plant(
            peer,
            id=parent_id,
            thread_id=parent_id,
            reply_to=None,
            subject="问",
            body="后到的父信",
            created_at="2026-10-14T00:00:00Z",
        )
        self.assertEqual(self._ok(home, project, "import")["imported"], 1)
        thread = _store(home).thread("kimi", parent_id)
        self.assertEqual([item["id"] for item in thread["messages"]], [parent_id, reply_id])
        self.assertEqual(thread["messages"][1]["reply_to"], parent_id)

    @unittest.skipUnless(_HAS_IMPORT and _HAS_RECEIPT, "等 cursor 合入 import_remote 与 apply_receipt")
    def test_read_receipt_round_trip_is_stable(self):
        alice = self.root / "alice"
        robin = self.root / "robin"
        project = (self.root / "box").resolve()
        project.mkdir()
        _write_node(alice, "alice")
        _write_node(robin, "robin")
        sender = _store(alice)
        sent = sender.send("grok", ["robin.kimi"], "主题", "正文", workspace=str(project))
        self.assertEqual(self._ok(alice, project, "once")["exported"], 1)
        self.assertEqual(self._ok(robin, project, "once")["imported"], 1)
        _store(robin).mark("kimi", sent["id"], "read")
        out = self._ok(robin, project, "once")
        self.assertEqual(out["receipts_out"], 1)
        self.assertTrue((project / ".infocenter" / "outbox" / "robin" / "receipts" / f"{sent['id']}--kimi.json").is_file())
        incoming = self._ok(alice, project, "once")
        self.assertEqual(incoming["receipts_in"], 1)
        self.assertEqual(incoming["exported"], 0)
        conn = sqlite3.connect(alice / "mailbox.db")
        try:
            row = conn.execute(
                "SELECT status FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (sent["id"], "robin.kimi"),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "read")
        self.assertEqual(self._ok(alice, project, "once"), _zero())
        self.assertEqual(self._ok(robin, project, "once"), _zero())

    @unittest.skipUnless(_HAS_RECEIPT, "等 cursor 合入 Store.apply_receipt")
    def test_archived_delivery_ignores_receipt(self):
        home = self.root / "home"
        project = (self.root / "proj").resolve()
        project.mkdir()
        _write_node(home, "alice")
        store = _store(home)
        sent = store.send("grok", ["robin.kimi"], "主题", "正文", workspace=str(project))
        store.mark("robin.kimi", sent["id"], "archived")
        conn = sqlite3.connect(home / "mailbox.db")
        try:
            before = conn.execute(
                "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (sent["id"], "robin.kimi"),
            ).fetchone()
        finally:
            conn.close()
        receipts = project / ".infocenter" / "outbox" / "robin" / "receipts"
        receipts.mkdir(parents=True)
        payload = {
            "v": 1,
            "kind": "receipt",
            "message_id": sent["id"],
            "by": "robin.kimi",
            "status": "read",
            "read_at": "2026-10-14T01:00:00Z",
        }
        (receipts / f"{sent['id']}--kimi.json").write_text(json.dumps(payload), encoding="utf-8")
        result = self._ok(home, project, "import")
        self.assertEqual(result["receipts_in"], 0)
        conn = sqlite3.connect(home / "mailbox.db")
        try:
            after = conn.execute(
                "SELECT status, read_at FROM deliveries WHERE message_id = ? AND to_agent = ?",
                (sent["id"], "robin.kimi"),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(after, before)
