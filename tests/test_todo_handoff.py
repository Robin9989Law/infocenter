"""「忘了怎么办」的答案：**把待改的事写进自己的 TODO**（human 2026-10-15）。

原话：**不通知了。每个 agent 跟着自己的 todo 轮。能做就做，不做就下一个。agent 多了，反正就是无需乱做。**

所以机制只剩：
1. `ic queue <路径> --note "要改什么"` ⇒ 库里记一笔（只增链）+ **写进提出者自己的 `TODO-<名字>.md`**；
2. agent 照自己 TODO 往下走，轮到那条就 `ic lock`：能拿到就做，拿不到就跳过做下一条；
3. **没有任何通知/来信/排队**——不打扰任何人。
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import _todo_add, _todo_path, main


class TodoWriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_creates_file_with_header(self):
        path = _todo_add("cursor", "改 a.py：加个字段")
        self.assertEqual(Path(path), _todo_path("cursor"))
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("# TODO · cursor", text)
        self.assertIn("- [ ] 改 a.py：加个字段", text)

    def test_appends_to_existing_todo(self):
        _todo_add("cursor", "第一件")
        _todo_add("cursor", "第二件")
        text = Path(_todo_path("cursor")).read_text(encoding="utf-8")
        self.assertEqual(text.count("- [ ]"), 2)
        self.assertLess(text.index("第一件"), text.index("第二件"))

    def test_is_idempotent_for_the_same_line(self):
        _todo_add("cursor", "同一件")
        _todo_add("cursor", "同一件")
        text = Path(_todo_path("cursor")).read_text(encoding="utf-8")
        self.assertEqual(text.count("同一件"), 1, "同一条不许写两遍")


class QueueWritesTodoTest(unittest.TestCase):
    """`ic queue` 只做两件事：库里记一笔 + 写进自己的 TODO。**不发通知**。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def _run(self, argv, payload):
        calls = []

        def fake_call(url, agent, method, path, body=None, **kw):
            calls.append((method, path, body))
            return payload

        with patch("infocenter.cli.call", side_effect=fake_call), redirect_stdout(io.StringIO()) as out:
            code = main(argv)
        return code, calls, out.getvalue()

    def test_queue_records_and_writes_my_todo(self):
        payload = {
            "id": "q77", "seq": 77, "path": "/proj/store.py", "agent": "cursor",
            "note": "把 foo() 改成 bar()", "patch": "", "held_by": "opencode",
        }
        code, calls, out = self._run(
            ["--as", "cursor", "queue", "/proj/store.py", "--note", "把 foo() 改成 bar()"], payload
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][1], "/v1/locks/queue", "先记一笔")
        text = Path(self.tmp.name, "TODO-cursor.md").read_text(encoding="utf-8")
        self.assertIn("改 /proj/store.py", text)
        self.assertIn("钥匙在 opencode 手里", text)
        self.assertIn("能拿到钥匙就做", text, "要有这句：轮到时能做就做")
        self.assertIn("q77", text)

    def test_queue_does_not_notify_anyone(self):
        payload = {"id": "q78", "seq": 78, "path": "/proj/store.py", "agent": "cursor",
                   "note": "x", "patch": "", "held_by": "opencode"}
        _, calls, _ = self._run(["--as", "cursor", "queue", "/proj/store.py", "--note", "x"], payload)
        self.assertFalse(
            [c for c in calls if "messages" in str(c[1])],
            "「不通知了」：queue 不该给任何人发信",
        )

    def test_no_todo_flag_skips_the_file(self):
        payload = {"id": "q79", "seq": 79, "path": "/proj/store.py", "agent": "cursor",
                   "note": "x", "patch": "", "held_by": ""}
        self._run(["--as", "cursor", "queue", "/proj/store.py", "--note", "x", "--no-todo"], payload)
        self.assertFalse(Path(self.tmp.name, "TODO-cursor.md").exists())

    def test_free_file_tells_you_to_just_take_the_key(self):
        payload = {"id": "q80", "seq": 80, "path": "/proj/store.py", "agent": "cursor",
                   "note": "x", "patch": "", "held_by": ""}
        _, _, out = self._run(["--as", "cursor", "queue", "/proj/store.py", "--note", "x"], payload)
        self.assertIn("现在没人拿钥匙", out)
        self.assertIn("ic lock", out, "没人拿钥匙就直接拿钥匙做")

    def test_unlock_does_not_send_letters(self):
        payload = {"agent": "opencode", "released": ["/proj/store.py"], "released_count": 1,
                   "force": False, "pending_left": [{"path": "/proj/store.py", "count": 2}]}
        calls = []

        def fake_call(url, agent, method, path, body=None, **kw):
            calls.append((method, path))
            return payload

        with patch("infocenter.cli.call", side_effect=fake_call), redirect_stdout(io.StringIO()) as out:
            main(["--as", "opencode", "unlock", "/proj/store.py"])
        self.assertFalse([c for c in calls if "messages" in str(c[1])], "放钥匙不发通知")
        self.assertIn("待改", out.getvalue())


if __name__ == "__main__":
    unittest.main()
