"""轻薄协作底座：大局观态势感知、架构观提示与全场景（代码/科研/商务）交付核验测试。"""

import argparse
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from infocenter.cli import _verify_task_deliverables
from infocenter.common import MailError
from infocenter.crew import _active_task_line, build_heartbeat_prompt, build_prompt


class ThinSubstratePanoramaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ic-test-panorama-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_active_task_line_injects_own_task_and_peer_big_picture(self):
        """大局观态势感知：不仅返回当前 Agent 的任务，还注入同项目同伴正在推进（doing）的并行态势。"""
        fake_tasks = {
            "tasks": [
                {
                    "id": 101,
                    "title": "撰写第3节实验对照与显著性检验",
                    "owner": "cursor",
                    "status": "doing",
                    "case_id": 12,
                },
                {
                    "id": 102,
                    "title": "绘制主架构图与收敛曲线",
                    "owner": "claude",
                    "status": "doing",
                    "case_id": 12,
                },
                {
                    "id": 103,
                    "title": "补充附录超参数表",
                    "owner": "kimi",
                    "status": "todo",
                    "case_id": 12,
                },
            ]
        }
        with mock.patch("infocenter.crew._api", return_value=fake_tasks):
            line = _active_task_line("http://127.0.0.1:8787", "cursor", str(self.tmp))

        self.assertIn("[101]", line)
        self.assertIn("撰写第3节实验对照与显著性检验", line)
        self.assertIn("同项目并行态势（大局观）", line)
        self.assertIn("claude《绘制主架构图与收敛曲线》", line)
        # 尚未开工的 todo 同伴任务不挤占并行态势行
        self.assertNotIn("kimi", line)

    def test_active_task_line_shows_peer_panorama_even_when_self_has_no_task(self):
        """即使自身暂无直接挂载的 task，只要同项目有同伴在 doing，依然注入同项目并行态势（大局观）。"""
        fake_tasks = {
            "tasks": [
                {
                    "id": 201,
                    "title": "商务尽职调查财务章节核算",
                    "owner": "codex",
                    "status": "doing",
                    "case_id": 88,
                },
            ]
        }
        with mock.patch("infocenter.crew._api", return_value=fake_tasks):
            line = _active_task_line("http://127.0.0.1:8787", "cursor", str(self.tmp))

        self.assertIn("同项目并行态势（大局观）", line)
        self.assertIn("codex《商务尽职调查财务章节核算》", line)

    def test_prompts_contain_big_picture_and_architectural_stewardship(self):
        """唤醒提示词与心跳提示词均包含大局观与架构观引导，且充分赋能 Agent Harness 自由度。"""
        p_wake = build_prompt(
            "cursor",
            str(self.tmp),
            [{"id": 1, "from": "manager", "subject": "架构收敛", "created_at": "2026-10-18T00:00:00Z"}],
            1,
        )
        p_hb = build_heartbeat_prompt(
            "cursor",
            str(self.tmp),
            coordinator=False,
        )

        self.assertIn("【大局观与架构观】", p_wake)
        for prompt in (p_wake, p_hb):
            self.assertIn("Harness", prompt)
            self.assertIn("大局观与架构观", prompt)
            self.assertIn("减法", prompt)

    def test_verify_deliverables_supports_file_prefix_and_sha256(self):
        """跨场景核验（代码/科研/商务）：支持 file:<path> 存在且非空核验，以及 @sha256:<hex> 哈希核验。"""
        paper = self.tmp / "paper_section3.tex"
        paper.write_text("\\section{Methodology}\nProof of Theorem 1.\n", encoding="utf-8")
        digest = hashlib.sha256(paper.read_bytes()).hexdigest()

        empty_doc = self.tmp / "empty_proposal.md"
        empty_doc.write_text("", encoding="utf-8")

        args = argparse.Namespace(id="t-301", url="http://127.0.0.1:8787", workspace=str(self.tmp))

        # 1. file:<path> 存在且非空 + @sha256 匹配 -> 通过
        task_ok = {
            "id": "t-301",
            "workspace": str(self.tmp),
            "acceptance": "file:paper_section3.tex",
            "deps": f"paper_section3.tex@sha256:{digest}; task#99",
        }
        with mock.patch("infocenter.cli.call", return_value=task_ok):
            acc, deps = _verify_task_deliverables(args, "cursor", self.tmp)
        self.assertEqual(acc, "file:paper_section3.tex")
        self.assertIn(digest, deps)

        # 2. file:<path> 为空文件 -> 抛 MailError
        task_empty = {
            "id": "t-302",
            "workspace": str(self.tmp),
            "acceptance": "file:empty_proposal.md",
            "deps": "",
        }
        with mock.patch("infocenter.cli.call", return_value=task_empty):
            with self.assertRaises(MailError) as ctx:
                _verify_task_deliverables(args, "cursor", self.tmp)
        self.assertIn("不存在或为空", str(ctx.exception))

        # 3. file:<path> 不存在 -> 抛 MailError
        task_missing = {
            "id": "t-303",
            "workspace": str(self.tmp),
            "acceptance": "",
            "deps": "file:missing_report.xlsx",
        }
        with mock.patch("infocenter.cli.call", return_value=task_missing):
            with self.assertRaises(MailError) as ctx2:
                _verify_task_deliverables(args, "cursor", self.tmp)
        self.assertIn("不存在或为空", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
