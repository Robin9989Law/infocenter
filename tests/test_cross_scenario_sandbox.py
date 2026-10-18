"""跨场景任务沙箱与通用交付物核验测试（代码研发 / 科研攻关 / 商务工作）。"""

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

from infocenter import cli
from infocenter.common import MailError
from infocenter.sandbox import manage_task_sandbox, verify_deliverables
from infocenter.server import make_handler
from infocenter.store import Store


class CrossScenarioSandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_research_scenario_draft_sandbox_and_file_deliverable_verify(self):
        """科研场景（非 Git 目录）：自动创建 draft-dir 沙箱、拷入依赖数据、撰写手稿后 --apply 回主目录并校验 file:<path>。"""
        lab_dir = self.root / "iclr_paper_project"
        lab_dir.mkdir()
        raw_data = lab_dir / "dataset.csv"
        raw_data.write_text("epoch,loss\n1,0.42\n", encoding="utf-8")
        data_hash = hashlib.sha256(raw_data.read_bytes()).hexdigest()

        task_data = {
            "id": "t-research-01",
            "workspace": str(lab_dir),
            "deps": f"dataset.csv@sha256:{data_hash} file:sections/theorem1.tex",
            "acceptance": "定理1推导完成且 sections/theorem1.tex 非空",
        }

        # 1. 创建科研草稿沙箱（自动拷入已存在的 dataset.csv）
        created = manage_task_sandbox(task_data)
        self.assertEqual(created["mode"], "draft-dir")
        sbox = Path(created["sandbox_dir"])
        self.assertTrue((sbox / "dataset.csv").is_file())

        # 2. 尚未产出 theorem1.tex 时，核验必须报错
        with self.assertRaises(MailError) as ctx:
            verify_deliverables("t-research-01", task_data["acceptance"], task_data["deps"], base_dir=lab_dir)
        self.assertIn("交付物文件不存在：sections/theorem1.tex", ctx.exception.message)

        # 3. 在沙箱内撰写科研手稿，主目录此时仍保持干净无污染
        draft_tex = sbox / "sections" / "theorem1.tex"
        draft_tex.parent.mkdir(parents=True, exist_ok=True)
        draft_tex.write_text("\\begin{theorem} Convergence holds. \\end{theorem}\n", encoding="utf-8")
        self.assertFalse((lab_dir / "sections" / "theorem1.tex").exists())

        # 4. --apply 将沙箱产物同步回主目录
        applied = manage_task_sandbox(task_data, apply=True)
        self.assertEqual(applied["applied_files"], ["sections/theorem1.tex"])
        self.assertTrue((lab_dir / "sections" / "theorem1.tex").is_file())

        # 5. 再次运行核验，精确哈希与交付物非空校验均通过
        acc, deps = verify_deliverables("t-research-01", task_data["acceptance"], task_data["deps"], base_dir=lab_dir)
        self.assertIn("定理1", acc)
        self.assertIn("file:sections/theorem1.tex", deps)

        # 6. --clean 清理沙箱
        cleaned = manage_task_sandbox(task_data, clean=True)
        self.assertEqual(cleaned["action"], "clean")
        self.assertFalse(sbox.exists())

    def test_business_scenario_empty_file_rejected(self):
        """商务场景：0 字节空报告不能蒙混过关，必须有实质内容落盘。"""
        biz_dir = self.root / "q4_proposal"
        biz_dir.mkdir()
        empty_doc = biz_dir / "proposal.md"
        empty_doc.write_text("", encoding="utf-8")

        with self.assertRaises(MailError) as ctx:
            verify_deliverables("t-biz-01", "file:proposal.md", "", base_dir=biz_dir)
        self.assertIn("0 字节", ctx.exception.message)

        empty_doc.write_text("# Q4 商务合作方案\n\n## 核心条款\n", encoding="utf-8")
        acc, _ = verify_deliverables("t-biz-01", "file:proposal.md", "", base_dir=biz_dir)
        self.assertEqual(acc, "file:proposal.md")

    def test_code_scenario_git_worktree_sandbox_lifecycle(self):
        """代码研发场景（Git 仓库）：自动创建 git-worktree 隔离沙箱，修改后可 apply 回主工作树并 clean 移除。"""
        if not shutil.which("git"):
            self.skipTest("当前环境未安装 git")
        repo_dir = self.root / "code_repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

        task_data = {
            "id": "t-code-01",
            "workspace": str(repo_dir),
            "deps": "file:app.py",
            "acceptance": "python -c 'import app; assert app.x == 2'",
        }
        created = manage_task_sandbox(task_data)
        self.assertEqual(created["mode"], "git-worktree")
        sbox = Path(created["sandbox_dir"])
        self.assertTrue((sbox / "app.py").is_file())

        # 在 worktree 沙箱中修改代码，主工作树不受影响
        (sbox / "app.py").write_text("x = 2\n", encoding="utf-8")
        self.assertEqual((repo_dir / "app.py").read_text(encoding="utf-8"), "x = 1\n")

        # 归档回主工作树并清理 worktree
        applied = manage_task_sandbox(task_data, apply=True)
        self.assertEqual(applied["applied_files"], ["app.py"])
        self.assertEqual((repo_dir / "app.py").read_text(encoding="utf-8"), "x = 2\n")

        manage_task_sandbox(task_data, clean=True)
        self.assertFalse(sbox.exists())


class CliTaskSandboxEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("manager", "Manager", "coordinator"), ("kimi", "Kimi", "")],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_cli_task_sandbox_and_verify_workflow(self):
        ws = Path(self.tmp.name) / "biz_workspace"
        ws.mkdir()
        task = self.store.task_add(
            agent="manager",
            title="撰写市场调研报告",
            owner="kimi",
            workspace=str(ws),
            deps="file:market_report.md",
            acceptance="file:market_report.md",
        )
        tid = task["id"]

        # 1. CLI 创建沙箱
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--home", str(self.home), "--url", self.url, "--as", "kimi", "task", "sandbox", tid])
        self.assertEqual(rc, 0)
        self.assertIn("跨场景独立草稿沙箱", buf.getvalue())

        # 2. 在沙箱中写入报告并 --apply
        sbox_file = ws / ".ic-sandboxes" / tid / "market_report.md"
        sbox_file.write_text("# 2026 市场调研报告\n结论：增长显著。\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--home", str(self.home), "--url", self.url, "--as", "kimi", "task", "sandbox", tid, "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("market_report.md", buf.getvalue())

        # 3. CLI verify 校验通过
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--home", str(self.home), "--url", self.url, "--as", "manager", "task", "verify", tid])
        self.assertEqual(rc, 0)
        self.assertIn("硬验证通过", buf.getvalue())

        # 4. CLI --clean 清理沙箱
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--home", str(self.home), "--url", self.url, "--as", "kimi", "task", "sandbox", tid, "--clean"])
        self.assertEqual(rc, 0)
        self.assertIn("已清理任务", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
