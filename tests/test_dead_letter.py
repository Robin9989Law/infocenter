"""第 5 道兜底：**死信拒收**（human 2026-10-16 拍「要」；kimi 2026-10-15 提的缺口）。

缺口长什么样：`to` 里写个不存在的名字（打错 / 已退休），程序照样报「已送出」、库里也留一封信，
但**那份投递谁都读不到** —— 话说了没人听，而且看起来还是成功的。这是不可逆的损失，
所以机器在这里兜底：收件人不在册就**直接拒**（422 `no_such_recipient`），并说清怎么办。

在册 = 库里注册过的 ∪ 名册（config.json）里列着的。为什么要有后一半：
`ic crew kickoff` 是**按名册群发**开工信，名册里的新同事可能一次都还没开口。
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import config
from infocenter.httpapi import handle
from infocenter.store import MailError, Store


class DeadLetterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("kimi", "Kimi", "")],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _counts(self) -> tuple[int, int]:
        conn = sqlite3.connect(f"file:{self.store.path}?mode=ro", uri=True)
        agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return agents, messages

    def test_打错名字直接拒_且什么都不留(self):
        before = self._counts()
        with self.assertRaises(MailError) as ctx:
            self.store.send(sender="opencode", to="kim1", subject="手滑", body="正文")
        self.assertEqual(ctx.exception.code, "no_such_recipient")
        self.assertEqual(ctx.exception.status, 422)
        # 不许"悄悄造一个收件人"出来，也不许留下没人收的信
        self.assertEqual(self._counts(), before)

    def test_报文里说清怎么办(self):
        with self.assertRaises(MailError) as ctx:
            self.store.send(sender="opencode", to="查无此人", subject="x", body="y")
        self.assertIn("register", str(ctx.exception))

    def test_已退休的收不到(self):
        self.store.touch("omp")  # 在册
        self.store.send(sender="opencode", to="omp", subject="在", body="在")
        self.store.remove_agent("human", "omp")  # 退休（只有 human 能移除成员，2026-10-16 起是机器护栏）
        before = self._counts()
        with self.assertRaises(MailError) as ctx:
            self.store.send(sender="opencode", to="omp", subject="还在吗", body="在吗")
        self.assertEqual(ctx.exception.code, "no_such_recipient")
        self.assertEqual(self._counts(), before)

    def test_名册里的新人能收(self):
        """名册里有、还没开口过的同事必须收得到 —— 否则 `ic crew kickoff` 群发会被整封拒掉。"""
        config.save(self.home, {"agents": [{"name": "opencode"}, {"name": "newbie"}]})
        out = self.store.send(sender="opencode", to="newbie", subject="欢迎", body="开工")
        self.assertEqual(out["to"], ["newbie"])

    def test_注册过的能收(self):
        self.store.touch("grok")
        out = self.store.send(sender="opencode", to="grok", subject="在", body="在")
        self.assertEqual(out["to"], ["grok"])

    def test_给自己发也行(self):
        out = self.store.send(sender="opencode", to="opencode", subject="备忘", body="给我自己")
        self.assertEqual(out["to"], ["opencode"])

    def test_广播不受影响(self):
        out = self.store.send(sender="opencode", to="*", subject="通告", body="大家好")
        self.assertIn("kimi", out["to"])
        self.assertNotIn("opencode", out["to"])

    def test_立案时的始信也受管(self):
        with self.assertRaises(MailError) as ctx:
            self.store.create_case(
                agent="opencode", title="派给不存在的同事", to="查无此人", body="正文"
            )
        self.assertEqual(ctx.exception.code, "no_such_recipient")
        # 整笔回滚：案卷也不许留下半本
        self.assertEqual(self.store.list_cases(limit=50), [])

    def test_回复不受影响(self):
        sent = self.store.send(sender="kimi", to="opencode", subject="问", body="在吗")
        back = self.store.reply(sender="opencode", message_id=sent["id"], body="在")
        self.assertTrue(back.get("id"))

    def test_路由把错误照实报出来(self):
        body = json.dumps({"to": "查无此人", "subject": "x", "body": "y"}).encode()
        with self.assertRaises(MailError) as ctx:
            handle("POST", "/v1/messages", {"X-Agent": "opencode"}, body, self.store)
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "no_such_recipient")

    def test_配置读不到也不误拒(self):
        """config.json 坏掉时退回"只看库"：漏拒可以，误拒不行。"""
        (self.home / "config.json").write_text("{ 这不是 JSON", encoding="utf-8")
        self.store.touch("grok")
        out = self.store.send(sender="opencode", to="grok", subject="在", body="在")
        self.assertEqual(out["to"], ["grok"])


if __name__ == "__main__":
    unittest.main()
