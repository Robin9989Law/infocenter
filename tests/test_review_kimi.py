"""评审补测（kimi，针对 M8/M9/M10 定版 12129da）——只放别人文件里没有的用例。

三个点：
1. **锚点之后可伪造**（外锚的保护边界）：锚只钉住 seq ≤ 锚点的历史。锚点之后
   直接改库 + 接一条哈希合法的伪造痕迹，`ic audit`（带锚）照样全绿，
   唯一信号是 unwitnessed +1——与诚实活动无法区分。这不是 bug，
   但这个边界必须有测试钉住，免得以后有人误以为"打了锚 = 全库不可改"。
2. **锁 TTL 有 30s 下限**（store.py `_lock_ttl` 的 max(30, …)）：
   `--ttl 3` 实际拿到 30s。CLI help 只说"默认 1800，最长 12h"，没提下限。
3. **测试进程的宿主 env 泄漏**：`INFOCENTER_WINDOW` / `INFOCENTER_AGENT`
   会经 os.environ 复制进被测子进程。本文件的用例显式清掉它们，
   并断言清掉后子进程不再报 window_conflict（test_quality_mcp 的
   _env() 原先没清 ⇒ 在 agent 会话里跑全量必红 1 个；已修为清掉，
   这里钉住失败模式本身防回退）。
4. **只读 TODO 不许 chmod 硬写**：TODO-<agent>.md 放下钥匙后停在 444。
   没拿到这把钥匙时 `_todo_add` 必须停（`todo_unwritable`），权限位与正文都不变。
   拿得到钥匙再写、写完放回的路径在 `tests/test_todo_handoff.py`。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.server import make_handler
from infocenter.store import Store, _lock_ttl, _task_event_hash

WS = "/tmp/infocenter-demo"


class PostAnchorForgeTest(unittest.TestCase):
    """边界用例：锚点之后的历史可以伪造且 audit 全绿（把边界钉成测试，不是修 bug）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("kimi", "Kimi Code", "")],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _con(self):
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def test_forgery_after_anchor_passes_audit(self):
        task = self.store.task_add(
            "opencode", "诚实标题", owner="kimi", workspace=WS, acceptance="有单测"
        )
        audit0 = self.store.audit_events()
        anchor = {"seq": audit0["tip_seq"], "hash": audit0["tip_hash"], "at": "test"}
        self.assertTrue(self.store.audit_events(anchor=anchor)["ok"])

        # 攻击者（会写 SQL）：锚点之后改任务现状 + 接一条哈希合法的伪造痕迹把改动讲圆
        con = self._con()
        con.execute(
            "UPDATE tasks SET title = '被事后篡改的标题' WHERE id = ?", (task["id"],)
        )
        tip = con.execute("SELECT * FROM task_events ORDER BY seq DESC LIMIT 1").fetchone()
        fields = {
            "task_id": task["id"], "at": "2026-10-15T09:20:00Z", "agent": "kimi",
            "action": "update", "from_status": "", "to_status": "",
            "detail": json.dumps({"title": "被事后篡改的标题"}, ensure_ascii=False),
        }
        con.execute(
            "INSERT INTO task_events(task_id, at, agent, action, from_status,"
            " to_status, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (task["id"], fields["at"], "kimi", "update", "", "",
             fields["detail"], tip["hash"], _task_event_hash(tip["hash"], fields)),
        )
        con.commit()
        con.close()

        res = self.store.audit_events(anchor=anchor)
        # 边界：audit 全绿（链自洽、重放一致、锚点对上），唯一信号是 unwitnessed +1
        self.assertTrue(res["ok"])
        self.assertEqual(res["chain_broken"], [])
        self.assertEqual(res["mismatches"], [])
        self.assertTrue(res["anchor"]["ok"])
        self.assertEqual(res["anchor"]["unwitnessed"], 1)

    def test_pre_anchor_tamper_still_caught(self):
        """对照组：锚点之前的历史被改 ⇒ 外锚必然抓住（保护承诺的另一半）。"""
        task = self.store.task_add(
            "opencode", "锚前标题", owner="kimi", workspace=WS, acceptance="有单测"
        )
        audit0 = self.store.audit_events()
        anchor = {"seq": audit0["tip_seq"], "hash": audit0["tip_hash"], "at": "test"}
        con = self._con()
        # 截短：删掉锚点那一行 ⇒ 锚报"那行找不到了"
        con.execute("DELETE FROM task_events WHERE seq = ?", (anchor["seq"],))
        con.commit()
        con.close()
        res = self.store.audit_events(anchor=anchor)
        self.assertFalse(res["ok"])
        self.assertFalse(res["anchor"]["ok"])


class LockTtlFloorTest(unittest.TestCase):
    """`_lock_ttl` 有 30s 下限：--ttl 3 实际拿到 30s（实测踩过：以为 3s 后能复现到期）。"""

    def test_ttl_floor_is_30_seconds(self):
        self.assertEqual(_lock_ttl(3), 30)
        self.assertEqual(_lock_ttl(0), 30)
        self.assertEqual(_lock_ttl(None), 1800)
        self.assertEqual(_lock_ttl(999999), 12 * 3600)


class HostEnvIsolationTest(unittest.TestCase):
    """宿主 env（INFOCENTER_WINDOW / INFOCENTER_AGENT）必须不进被测子进程。

    复现（在 agent 会话里跑全量）：tests/test_quality_mcp.py 的 _env() 原先直接
    os.environ.copy() ⇒ 子进程 ic mcp 继承宿主的 INFOCENTER_WINDOW ⇒
    先注册 grok↔w-xxx 再注册 kimi↔w-xxx ⇒ window_conflict ⇒ 子进程 exit 1
    ⇒ EOFError，全量 359 红 1 个。_env() 已修为清掉这两个；这里钉住失败模式防回退。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("grok", "Grok", ""), ("kimi", "Kimi Code", "")],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _env(self, scrub: bool) -> dict:
        env = os.environ.copy()
        env["INFOCENTER_URL"] = self.url
        env["INFOCENTER_QUIET"] = "1"
        env["PYTHONPATH"] = str(ROOT)
        if scrub:
            env.pop("INFOCENTER_WINDOW", None)
            env.pop("INFOCENTER_AGENT", None)
        else:
            env["INFOCENTER_WINDOW"] = "w-reviewtest"
        return env

    def _mcp_init(self, env) -> subprocess.CompletedProcess:
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }).encode() + b"\n"
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "ic"), "mcp", "--no-notify"],
            input=payload, capture_output=True, env=env, timeout=15,
        )

    def test_scrubbed_env_initializes_clean(self):
        proc = self._mcp_init(self._env(scrub=True))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"protocolVersion", proc.stdout)

    def test_leaked_window_env_is_the_failure_mode(self):
        """钉住失败模式本身：先有一个 agent 占了窗口，第二个 agent 同窗口 ⇒ 冲突退出。"""
        from infocenter.client import request
        request(self.url, "POST", "/v1/messages", "grok",
                {"to": "kimi", "subject": "s", "body": "b"},
                window="w-reviewtest")
        env = self._env(scrub=False)
        env["INFOCENTER_AGENT"] = "kimi"
        proc = self._mcp_init(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("window_conflict", proc.stderr.decode())


class ReadonlyTodoWriteThroughTest(unittest.TestCase):
    """只读 TODO 不能靠自己 chmod 写穿。

    复现（2026-10-15 实验轮实测）：TODO 被锁过又放回 ⇒ 停在 444。
    没把这把钥匙交给 `_todo_add` 时，它必须停，权限位和正文都保持原样。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_readonly_todo_without_lock_does_not_chmod(self):
        from infocenter.cli import _todo_add
        from infocenter.store import MailError
        path = Path(self.tmp.name) / "TODO-kimi.md"
        path.write_text("# TODO · kimi\n\n- [ ] 旧的一条\n", encoding="utf-8")
        os.chmod(path, 0o444)  # 「锁＝只读」的静置态
        with self.assertRaises(MailError) as ctx:
            _todo_add("kimi", "改 foo.py：换实现（提议 q999）")
        self.assertEqual(ctx.exception.code, "todo_unwritable")
        self.assertIn("没有改权限", ctx.exception.message)
        self.assertEqual(path.stat().st_mode & 0o7777, 0o444)
        text = path.read_text(encoding="utf-8")
        self.assertIn("- [ ] 旧的一条", text)
        self.assertNotIn("q999", text)

    def test_writable_todo_path_unchanged(self):
        from infocenter.cli import _todo_add
        path = Path(self.tmp.name) / "TODO-kimi.md"
        _todo_add("kimi", "第一条")
        self.assertIn("- [ ] 第一条", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
