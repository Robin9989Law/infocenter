"""独立评审：拿不到钥匙时，人话必须指向 queue / 自己的 TODO，不能再叫人发信。

另外：尚未创建的文件在 macOS 上大小写不同的写法只能有一把钥匙。
crew 的两段提示词同样改走 queue，不再叫人回信；PLAN 调整那种发信保留。
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew
from infocenter.cli import _emit
from infocenter.store import MailError, Store


class GuardBlockSaysQueueTest(unittest.TestCase):
    def test_blocked_guard_tells_you_to_queue_not_to_mail(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _emit(
                False,
                "guard",
                {
                    "ok": False,
                    "checked": 1,
                    "unlocked": ["/tmp/a.py"],
                    "locked_by_others": [],
                },
            )
        text = buf.getvalue()
        self.assertIn("ic queue <路径> --note", text)
        self.assertIn("不要发信", text)
        self.assertNotIn("回信", text)


class OpenCodePluginSaysQueueTest(unittest.TestCase):
    def test_rejection_tells_you_to_queue_not_to_wait(self):
        source = (ROOT / ".opencode" / "plugins" / "infocenter-lock" / "index.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn('ic queue <路径> --note "要改什么"', source)
        self.assertNotIn("让 coordinator 协调", source)
        self.assertNotIn("等它提交放锁", source)


class PromptSaysQueueNotMailTest(unittest.TestCase):
    """q436：build_prompt 与成员心跳都含 queue，都不含「回信」。PLAN 那句发信不动。"""

    def test_both_prompts_queue_instead_of_mailing(self):
        fresh = [{
            "id": "m1",
            "from": "kimi",
            "subject": "干活",
            "priority": "normal",
            "workspace": "/tmp/p",
        }]
        letter = crew.build_prompt("grok", "/tmp/p", fresh, 1)
        beat = crew.build_heartbeat_prompt(
            "grok", "/tmp/p", coordinator=False, coordinator_name="opencode",
        )
        for text in (letter, beat):
            self.assertIn("ic queue <路径> --note", text)
            self.assertIn("不要发信", text)
            self.assertIn("不要干等", text)
            self.assertNotIn("回信", text)
        self.assertIn("发现 PLAN.md 有空缺", beat)
        self.assertIn("发信说明", beat)
        self.assertNotIn("回信", crew.build_heartbeat_prompt("opencode", "/tmp/p", coordinator=True))


class MissingCaseVariantsAreOneKeyTest(unittest.TestCase):
    """文件还不存在时，大小写不同的写法也只能有一把钥匙（macOS 默认卷）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "m.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("grok", "Grok", ""), ("cursor", "Cursor", "")],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_spelling_of_a_missing_file_is_refused(self):
        if sys.platform != "darwin":
            self.skipTest("尚未创建的文件靠 normcase 收口，只在 darwin 上成立")
        first = self.home / "NewFile.py"
        second = self.home / "newfile.py"
        self.store.lock_acquire("grok", [str(first)], ttl=600)
        with self.assertRaises(MailError) as ctx:
            self.store.lock_acquire("cursor", [str(second)], ttl=600)
        self.assertEqual(ctx.exception.code, "locked")
        self.assertIn("ic queue", ctx.exception.message)
        self.assertNotIn("回信", ctx.exception.message)
        names = [Path(row["path"]).name for row in self.store.lock_list()["locks"]]
        self.assertEqual(names, ["NewFile.py"])

    def test_one_call_with_both_spellings_keeps_a_single_key(self):
        if sys.platform != "darwin":
            self.skipTest("尚未创建的文件靠 normcase 收口，只在 darwin 上成立")
        first = self.home / "NewFile.py"
        second = self.home / "newfile.py"
        self.store.lock_acquire("grok", [str(first), str(second)], ttl=600)
        names = [Path(row["path"]).name for row in self.store.lock_list()["locks"]]
        self.assertEqual(len(names), 1, names)


if __name__ == "__main__":
    unittest.main()
