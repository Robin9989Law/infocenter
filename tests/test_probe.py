"""空转采样（`ic probe`）：**触发**和**产出**必须并排看。

为什么值得一组测试（都是实测踩出来的）：
- 只看"触发数"会被骗：pi 每分钟触发 36 次 headless，而从外面看它的"心跳空转轮"
  一动不动（4→4）——单看一个数，看不出它在原地踏步；
- `outcome` 分布是同一件事的另一半：那 60 次运行里 57 次 fail ⇒ 模型/配置坏了；
- 机械动作（拿钥匙 / 放钥匙 / 锁到期）**不算产出**，否则"锁 churn 很多"会被当成在出活；
- 日志每家只留最近 60 条 ⇒ 触发数只是**下限**，要标出来。
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew
from infocenter.cli import _probe_history, _probe_lines, _probe_log
from infocenter.httpapi import _probe_payload, handle
from infocenter.store import Store, iso_ago


def _iso(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ProbeCountsTest(unittest.TestCase):
    """库这一半：窗口内的**产出**（信 / 案卷节点 / 语义痕迹）与机械动作。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("pi", "Pi", ""), ("kimi", "Kimi", ""), ("opencode", "OpenCode", "coordinator")],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, agent: str, action: str, minutes_ago: float, task_id: str = "t1") -> None:
        conn = sqlite3.connect(self.store.path)
        conn.execute(
            "INSERT INTO task_events(task_id, at, agent, action, from_status, to_status, detail,"
            " prev_hash, hash) VALUES (?, ?, ?, ?, '', '', '', '', 'x')",
            (task_id, _iso(minutes_ago), agent, action),
        )
        conn.commit()
        conn.close()

    def test_机械动作不算产出(self):
        for _ in range(5):
            self._event("pi", "lock", 5)
        for _ in range(2):
            self._event("pi", "unlock", 5)
        self._event("pi", "propose", 5)
        rows = self.store.probe_counts(iso_ago(60))
        item = rows["pi"]
        self.assertEqual(item["machinery"], 7)  # 5 拿 + 2 放
        self.assertEqual(item["traces"], 1)  # 提议要改 = 真动静
        self.assertEqual(item["produced"], 1)

    def test_窗口外的旧痕迹不算(self):
        self._event("kimi", "propose", 5)
        self._event("kimi", "propose", 120)
        rows = self.store.probe_counts(iso_ago(60))
        self.assertEqual(rows["kimi"]["traces"], 1)

    def test_信和案卷节点也算产出(self):
        self.store.send(sender="pi", to="kimi", subject="一封信", body="正文")
        conn = sqlite3.connect(self.store.path)
        conn.execute(
            "INSERT INTO case_nodes(case_id, agent, window_id, action, note, status, auto, created_at)"
            " VALUES ('c1', 'pi', '', 'note', '一条节点', 'open', 0, ?)",
            (_iso(3),),
        )
        conn.commit()
        conn.close()
        rows = self.store.probe_counts(iso_ago(60))
        self.assertEqual(rows["pi"]["messages"], 1)
        self.assertEqual(rows["pi"]["case_nodes"], 1)
        self.assertEqual(rows["pi"]["produced"], 2)
        # 收件人不算产出（读信/签收不是"出活"）
        self.assertEqual(rows["kimi"]["messages"], 0)
        self.assertEqual(rows["kimi"]["produced"], 0)


class IdleVerdictTest(unittest.TestCase):
    """判语：先说"多半坏了"，再是"空转 / 出活太稀"。"""

    def test_总失败_先说坏了(self):
        verdict, why = crew.idle_verdict(17, 0, {"fail": 15, "ok": 2})
        self.assertEqual(verdict, "broken")
        self.assertIn("15", why)

    def test_触发很多产出零_是空转(self):
        verdict, why = crew.idle_verdict(24, 0, {"ok": 24})
        self.assertEqual(verdict, "idle")
        self.assertIn("原地踏步", why)

    def test_产出太稀(self):
        verdict, _why = crew.idle_verdict(20, 2, {"ok": 20})
        self.assertEqual(verdict, "thin")

    def test_正常和没动静(self):
        self.assertEqual(crew.idle_verdict(3, 1, {"ok": 3})[0], "ok")
        self.assertEqual(crew.idle_verdict(0, 0, {})[0], "quiet")


class RunStatsTest(unittest.TestCase):
    """运行记录这一半：触发数 + 结果分布（且**明说**它可能只是下限）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runs = crew.state_dir(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, records: list[dict]) -> Path:
        path = self.runs / name
        path.write_text(
            "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records) + "\n",
            encoding="utf-8",
        )
        return path

    def _rec(self, agent: str, minutes_ago: float, outcome: str = "ok", directory: str = "/tmp/proj"):
        return {
            "agent": agent,
            "dir": directory,
            "start": _iso(minutes_ago),
            "end": _iso(minutes_ago),
            "outcome": outcome,
        }

    def test_窗口内计数与结果分布(self):
        self._write(
            "pi--abc.runs.jsonl",
            [
                self._rec("pi", 5, "fail"),
                self._rec("pi", 10, "fail"),
                self._rec("pi", 15, "ok"),
                self._rec("pi", 90, "fail"),  # 窗口外
                "这不是 JSON，是半行的日志",
                json.dumps(["合法 JSON 但不是对象"]),
            ],
        )
        stats = crew.run_stats(self.home, iso_ago(60))
        self.assertEqual(stats["pi"]["triggers"], 3)
        self.assertEqual(stats["pi"]["outcomes"], {"fail": 2, "ok": 1})
        self.assertFalse(stats["pi"]["truncated"])

    def test_按项目目录收口(self):
        self._write(
            "pi--a.runs.jsonl",
            [self._rec("pi", 5, "ok", "/tmp/proj"), self._rec("pi", 6, "ok", "/tmp/other")],
        )
        stats = crew.run_stats(self.home, iso_ago(60), project="/tmp/proj")
        self.assertEqual(stats["pi"]["triggers"], 1)

    def test_顶到上限就明说只是下限(self):
        self._write(
            "pi--abc.runs.jsonl",
            [self._rec("pi", 1) for _ in range(crew.RUNS_KEEP)],
        )
        stats = crew.run_stats(self.home, iso_ago(60))
        self.assertTrue(stats["pi"]["truncated"])
        self.assertEqual(stats["pi"]["triggers"], crew.RUNS_KEEP)


class ProbePayloadTest(unittest.TestCase):
    """服务端把两半拼起来：`/v1/probe` 与挂在看板上的 `/v1/agents?probe_minutes=`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(self.home / "mailbox.db", seeds=[("pi", "Pi", ""), ("kimi", "Kimi", "")])
        runs = crew.state_dir(self.home) / "pi--abc.runs.jsonl"
        runs.write_text(
            "\n".join(
                json.dumps({"agent": "pi", "dir": "/tmp/proj", "start": _iso(5), "outcome": "fail"})
                for _ in range(12)
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_两半并排_触发与产出都在(self):
        probe = _probe_payload(self.store, 60)
        by_agent = {item["agent"]: item for item in probe["agents"]}
        self.assertEqual(by_agent["pi"]["triggers"], 12)
        self.assertEqual(by_agent["pi"]["produced"], 0)
        self.assertEqual(by_agent["pi"]["verdict"], "broken")  # 12 次全 fail
        self.assertIn("totals", probe)

    def test_路由给看板一对数(self):
        status, payload = handle("GET", "/v1/probe?minutes=60", {"X-Agent": "kimi"}, b"", self.store)
        self.assertEqual(status, 200)
        self.assertIn("probe", payload)
        self.assertEqual(payload["probe"]["minutes"], 60)

        status, agents = handle(
            "GET", "/v1/agents?probe_minutes=60", {"X-Agent": "kimi"}, b"", self.store
        )
        self.assertEqual(status, 200)
        spot = [a for a in agents["agents"] if a["name"] == "pi"][0]
        self.assertEqual(spot["probe"]["triggers"], 12)
        self.assertEqual(spot["probe"]["produced"], 0)

    def test_没要采样就不带这一对(self):
        status, agents = handle("GET", "/v1/agents", {"X-Agent": "kimi"}, b"", self.store)
        self.assertEqual(status, 200)
        for item in agents["agents"]:
            self.assertNotIn("probe", item)


class ProbeCliTest(unittest.TestCase):
    """命令行这一半：人话 + 只增台账 + 趋势。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "probe.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_台账只增_趋势并排看(self):
        for minutes, triggers, produced, verdict in ((60, 17, 0, "idle"), (60, 3, 5, "ok")):
            _probe_log(
                self.path,
                {
                    "at": _iso(minutes),
                    "at_beijing": "2026-10-15 21:0{}:00 +0800".format(triggers % 10),
                    "minutes": minutes,
                    "totals": {"triggers": triggers, "produced": produced, "machinery": 1},
                    "agents": [
                        {
                            "agent": "pi",
                            "triggers": triggers,
                            "produced": produced,
                            "machinery": 1,
                            "verdict": verdict,
                        }
                    ],
                },
            )
        rows = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 2)
        # 看趋势**不再**添行（看≠采）
        lines = _probe_history(self.path, 10)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 2)
        self.assertIn("空转", "\n".join(lines))
        self.assertIn("✓", lines[-1])

    def test_人话把触发和产出并排(self):
        text = "\n".join(
            _probe_lines(
                {
                    "minutes": 60,
                    "at_beijing": "2026-10-15 21:00:00 +0800",
                    "agents": [
                        {
                            "agent": "pi",
                            "triggers": 17,
                            "produced": 0,
                            "messages": 0,
                            "case_nodes": 0,
                            "traces": 0,
                            "machinery": 17,
                            "verdict": "idle",
                            "why": "17 次触发、0 产出：原地踏步",
                            "activity": "心跳",
                        }
                    ],
                    "totals": {"triggers": 17, "produced": 0, "machinery": 17},
                }
            )
        )
        self.assertIn("触发 17 / 产出 0", text)
        self.assertIn("原地踏步", text)
        self.assertIn("机械 17", text)


if __name__ == "__main__":
    unittest.main()
