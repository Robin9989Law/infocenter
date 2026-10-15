"""独立评审：拿不到钥匙时，人话必须指向 queue / 自己的 TODO，不能再叫人发信。

守护提示词（crew.py）和 409 正文（store.py）当时被别人拿着钥匙，记在 TODO-grok.md。
这里盖住已经能改的两处出口：`ic guard` 拦住时的打印，以及 OpenCode 写锁插件的拒绝文案。
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import _emit


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


if __name__ == "__main__":
    unittest.main()
