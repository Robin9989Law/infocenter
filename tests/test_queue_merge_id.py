"""`ic queue --merge --id` 的写法与坏输入（实测踩到的 500）。

现场：`ic --as opencode queue README.md --merge --id q377,q457` ⇒ **服务器内部错误**。
原因：`--id` 是 `action="append"`，逗号写法整串进来（`"q377,q457"`），store 拿它去 `int()`
⇒ `ValueError` ⇒ 未处理异常 ⇒ 500，人只看到"服务器内部错误"，不知道该怎么写。

现在两条都立规矩：
1. 逗号 / 重复给都认（`--id q1,q2` / `--id q1 --id q2`）；
2. 真写成坏东西（`--id 377`、`--id q-ab`）⇒ 干净的 **400 + 说清该写成什么样**，不是 500。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import _split_ids
from infocenter.store import MailError, Store


class SplitIdsTest(unittest.TestCase):
    def test_逗号写法拆开(self):
        self.assertEqual(_split_ids(["q377,q457"]), ["q377", "q457"])
        self.assertEqual(_split_ids(["q1, q2 ,q3"]), ["q1", "q2", "q3"])

    def test_重复给与单个(self):
        self.assertEqual(_split_ids(["q1", "q2"]), ["q1", "q2"])
        self.assertEqual(_split_ids(["q1"]), ["q1"])

    def test_没给就是空(self):
        self.assertEqual(_split_ids(None), [])
        self.assertEqual(_split_ids([]), [])
        self.assertEqual(_split_ids(["", " , "]), [])


class MergeIdCliTest(unittest.TestCase):
    """显式给了个空 id（`--id ""` / `--id ","`）**不许**静默当成「没给 id」。

    为什么单独钉死（kimi 2026-10-15 提示级）：不写 id 的语义是「把还没落的都落掉」＝合全部，
    而合并**不可撤销**；`--id ""` 拆完就是空列表，跟「没给」长得一模一样，于是手一滑就把台账全合了。
    """

    def _run(self, argv):
        import contextlib
        import io

        from infocenter import cli

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_显式空id_给干净的400而不是合全部(self):
        for raw in ("", " , "):
            code, err = self._run(["--as", "opencode", "queue", "some/path.py", "--merge", "--id", raw])
            self.assertEqual(code, 1)
            self.assertIn("invalid_id", err)
            self.assertIn("合全部", err)


class MergeIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")],
        )
        self.target = self.home / "target.py"
        self.target.write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_坏输入给400而不是500(self):
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        self.store.queue_change("cursor", str(self.target), note="第一条")
        with self.assertRaises(MailError) as ctx:
            self.store.merge_queue("cursor", str(self.target), ids=["q377,q457"])
        self.assertEqual(ctx.exception.code, "invalid_id")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("逗号", ctx.exception.message)

    def test_按编号合并且逗号写法等价于重复给(self):
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        first = self.store.queue_change("cursor", str(self.target), note="第一条")
        second = self.store.queue_change("opencode", str(self.target), note="第二条")
        # 逗号一条串进来（CLI 会拆，但 store 也得认）
        res = self.store.merge_queue("cursor", str(self.target), ids=[f"q{first['seq']}"])
        self.assertEqual(res["merged"], [first["seq"]])
        self.assertEqual(self.store.list_queue(str(self.target))["count"], 1)
        res2 = self.store.merge_queue("cursor", str(self.target), ids=[f"q{second['seq']}"])
        self.assertEqual(res2["merged"], [second["seq"]])
        self.assertEqual(self.store.list_queue(str(self.target))["count"], 0)

    def test_不写编号就是把还没落的都落掉(self):
        self.store.lock_acquire("cursor", [str(self.target)], ttl=600)
        first = self.store.queue_change("cursor", str(self.target), note="第一条")
        second = self.store.queue_change("cursor", str(self.target), note="第二条")
        res = self.store.merge_queue("cursor", str(self.target))
        self.assertEqual(sorted(res["merged"]), sorted([first["seq"], second["seq"]]))


if __name__ == "__main__":
    unittest.main()
