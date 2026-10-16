"""外锚再对 git HEAD 里那一份。

盘上的 `.infocenter-audit-anchor` 可以和库一起改：整链重算之后就地重新盖章，
只验本地文件会放行。`git show HEAD:<锚>` 是链外见证，两个都过才算绿。
取不到（非 git / 还没提交 / HEAD 里没有这个文件）就跳过，不算红，并打一行
`git 锚：无（跳过）`。
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli
from infocenter.server import make_handler
from infocenter.store import Store, _task_event_hash

WS = "/tmp/infocenter-demo"


class GitAnchorAuditTest(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._saved_env = {
            key: os.environ.get(key) for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES")
        }
        for key in self._saved_env:
            os.environ.pop(key, None)
        self._repos: list[tempfile.TemporaryDirectory] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["INFOCENTER_QUIET"] = "1"
        self.store = Store(
            self.home / "mailbox.db",
            seeds=[("opencode", "OpenCode", "coordinator"), ("cursor", "Cursor", "")],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        os.chdir(self._old_cwd)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.httpd.shutdown()
        self.httpd.server_close()
        for repo in self._repos:
            repo.cleanup()
        self.tmp.cleanup()

    def _git_repo(self, *, commit: bool) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self._repos.append(tmp)
        path = Path(tmp.name)
        self._git(path, "init", "-q")
        self._git(path, "config", "user.email", "t@t")
        self._git(path, "config", "user.name", "t")
        self._git(path, "config", "commit.gpgsign", "false")
        (path / "sub").mkdir()
        (path / "keep.txt").write_text("1\n", encoding="utf-8")
        if commit:
            self._git(path, "add", "keep.txt")
            self._git(path, "commit", "-qm", "init")
        return path

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def _task(self):
        return self.store.task_add(
            "opencode", "git 锚", owner="cursor", workspace=WS, acceptance="有单测"
        )

    def _write_anchor(self, repo: Path, seq: int, digest: str, at: str = "2026-10-16 16:10:00") -> Path:
        path = repo / ".infocenter-audit-anchor"
        path.write_text(
            f"{cli.ANCHOR_HEADER}{seq} {digest} {at}\n",
            encoding="utf-8",
        )
        return path

    def _commit_anchor(self, repo: Path) -> None:
        self._git(repo, "add", ".infocenter-audit-anchor")
        self._git(repo, "commit", "-qm", "anchor")

    def _audit(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "--as", "opencode",
                "--url", self.url,
                "--home", str(self.home),
                "audit",
            ])
        return code, out.getvalue(), err.getvalue()

    def _rehash_acceptance(self, task_id: str, acceptance: str) -> None:
        """模拟攻击：改验收再把整条链重算成自洽，并让任务行跟着重放结果走。"""
        con = sqlite3.connect(self.home / "mailbox.db", timeout=10)
        try:
            con.row_factory = sqlite3.Row
            prev = ""
            for row in con.execute("SELECT * FROM task_events ORDER BY seq"):
                fields = {
                    "task_id": row["task_id"], "at": row["at"], "agent": row["agent"],
                    "action": row["action"], "from_status": row["from_status"],
                    "to_status": row["to_status"], "detail": row["detail"],
                }
                if row["seq"] == 1:
                    detail = json.loads(fields["detail"])
                    detail["acceptance"] = acceptance
                    fields["detail"] = json.dumps(detail, ensure_ascii=False, sort_keys=True)
                digest = _task_event_hash(prev, fields)
                con.execute(
                    "UPDATE task_events SET detail = ?, prev_hash = ?, hash = ? WHERE seq = ?",
                    (fields["detail"], prev, digest, row["seq"]),
                )
                prev = digest
            con.execute("UPDATE tasks SET acceptance = ? WHERE id = ?", (acceptance, task_id))
            con.commit()
        finally:
            con.close()

    def test_git_anchor_matches_chain_is_green(self):
        """① 锚文件与 git 一致、链没动 ⇒ 绿。在子目录跑也必须对上仓库根那一份。"""
        repo = self._git_repo(commit=True)
        self._task()
        audit = self.store.audit_events()
        self.assertTrue(audit["ok"], audit)
        self._write_anchor(repo, audit["tip_seq"], audit["tip_hash"])
        self._commit_anchor(repo)

        os.chdir(repo / "sub")
        kind, got = cli._read_git_head_anchor(".infocenter-audit-anchor")
        self.assertEqual(kind, "anchor", "子目录也必须读到仓库根那份 git 锚")
        self.assertEqual(got["seq"], audit["tip_seq"])
        self.assertEqual(got["hash"], audit["tip_hash"])

        code, out, err = self._audit()
        self.assertEqual(code, 0, out + err)
        self.assertIn("外锚: 对上", out)
        self.assertIn(f"git 锚：对上（seq {audit['tip_seq']}）", out)
        self.assertNotIn("git 锚：无（跳过）", out)
        self.assertNotIn("对不上", out)

    def test_restamped_local_anchor_is_red_while_git_holds_old(self):
        """② 整链重写后把锚文件就地改成新 seq/hash，git 里还是旧的 ⇒ 红。

        只验本地文件会绿（这正是洞）：重新盖章对得上新链。git 那一份必须把这次打红。
        """
        repo = self._git_repo(commit=True)
        task = self._task()
        before = self.store.audit_events()
        self._write_anchor(repo, before["tip_seq"], before["tip_hash"])
        self._commit_anchor(repo)

        self._rehash_acceptance(task["id"], "被换过的验收")
        after = self.store.audit_events()
        self.assertTrue(after["ok"], after)
        self.assertNotEqual(after["tip_hash"], before["tip_hash"])
        self._write_anchor(repo, after["tip_seq"], after["tip_hash"], at="2026-10-16 16:20:00")
        local_only = self.store.audit_events(anchor={"seq": after["tip_seq"], "hash": after["tip_hash"]})
        self.assertTrue(local_only["ok"], "就地重新盖章后，只验本地锚会放行")
        git_only = self.store.audit_events(anchor={"seq": before["tip_seq"], "hash": before["tip_hash"]})
        self.assertFalse(git_only["anchor"]["ok"])

        os.chdir(repo / "sub")
        code, out, err = self._audit()
        self.assertEqual(code, 1, out + err)
        self.assertIn("外锚: 对上", out, "本地新章本身对得上新链")
        self.assertIn("git 锚：**对不上**", out)
        self.assertNotIn("git 锚：无（跳过）", out)

    def test_non_git_skips_and_stays_green(self):
        """③ 非 git 环境 ⇒ 跳过、不红。"""
        plain = self._git_repo(commit=False)
        # _git_repo 总是 init。这里要的是「不是仓库」：把 .git 拆掉，并禁止往上找。
        git_dir = plain / ".git"
        if git_dir.is_dir():
            import shutil
            shutil.rmtree(git_dir)
        os.environ["GIT_CEILING_DIRECTORIES"] = str(plain)
        self._task()
        audit = self.store.audit_events()
        self._write_anchor(plain, audit["tip_seq"], audit["tip_hash"])
        os.chdir(plain)
        self.assertEqual(cli._git_toplevel(subprocess), "")

        code, out, err = self._audit()
        self.assertEqual(code, 0, out + err)
        self.assertIn("git 锚：无（跳过）", out)
        self.assertIn("外锚: 对上", out)
        self.assertNotIn("对不上", out)

    def test_anchor_absent_from_head_skips(self):
        """HEAD 里还没有锚文件（包括首次把锚提交进去之前）⇒ 跳过、不红。"""
        repo = self._git_repo(commit=True)
        self._task()
        audit = self.store.audit_events()
        self._write_anchor(repo, audit["tip_seq"], audit["tip_hash"])
        os.chdir(repo / "sub")
        kind, _got = cli._read_git_head_anchor(".infocenter-audit-anchor")
        self.assertEqual(kind, "skip")

        code, out, err = self._audit()
        self.assertEqual(code, 0, out + err)
        self.assertIn("git 锚：无（跳过）", out)
        self.assertNotIn("对不上", out)

    def test_repo_with_no_commits_skips(self):
        """仓库还没有任何提交（首次提交之前）⇒ 跳过、不红。"""
        repo = self._git_repo(commit=False)
        self._task()
        audit = self.store.audit_events()
        self._write_anchor(repo, audit["tip_seq"], audit["tip_hash"])
        os.chdir(repo)
        kind, _got = cli._read_git_head_anchor(".infocenter-audit-anchor")
        self.assertEqual(kind, "skip")

        code, out, err = self._audit()
        self.assertEqual(code, 0, out + err)
        self.assertIn("git 锚：无（跳过）", out)
        self.assertNotIn("对不上", out)


if __name__ == "__main__":
    unittest.main()
