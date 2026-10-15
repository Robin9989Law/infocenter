"""独立评审：拿不到钥匙时，人话必须指向 queue / 自己的 TODO，不能再叫人发信。

另外：尚未创建的文件在 macOS 上大小写不同的写法只能有一把钥匙。
crew.py 的提示词当时被别人拿着钥匙，记在 TODO-grok.md。
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
