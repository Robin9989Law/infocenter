"""`ic reply` 与 `ic send` 一样接受 `--body-file`。

pi 2026-10-15：只有 `--body` 时，正文里的反引号会被 shell 当命令执行。
文件路径进 argv，正文不进 shell。
"""

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.cli import main

_TRICKY = "反引号 `echo hi` 与 $(whoami) 必须原样进正文\n"


class ReplyBodyFileTest(unittest.TestCase):
    def test_body_file_is_posted_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.txt"
            path.write_text(_TRICKY, encoding="utf-8")
            with patch("infocenter.cli.call", return_value={"id": "m1"}) as call, redirect_stdout(io.StringIO()):
                rc = main(
                    [
                        "--as",
                        "grok",
                        "--url",
                        "http://127.0.0.1:9",
                        "--home",
                        tmp,
                        "--json",
                        "reply",
                        "mabc",
                        "--body-file",
                        str(path),
                    ]
                )
        self.assertEqual(rc, 0)
        posted = call.call_args.args[4]
        self.assertEqual(posted["body"], _TRICKY)
        self.assertEqual(call.call_args.args[2], "POST")
        self.assertTrue(str(call.call_args.args[3]).endswith("/mabc/reply"))

    def test_body_still_works(self):
        with patch("infocenter.cli.call", return_value={"id": "m1"}) as call, redirect_stdout(io.StringIO()):
            rc = main(
                [
                    "--as",
                    "grok",
                    "--url",
                    "http://127.0.0.1:9",
                    "--json",
                    "reply",
                    "mabc",
                    "-b",
                    "一行",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[4]["body"], "一行")

    def test_to_is_repeatable_like_send(self):
        """pi 2026-10-15：`reply --to` 曾是单值 ⇒ `--to A --to B` **静默只留最后一个**。

        收件人被悄悄缩窄，和我们在灭的那批「静默」同类；这里钉死与 `send` 对称。
        """
        with patch("infocenter.cli.call", return_value={"id": "m1"}) as call, redirect_stdout(io.StringIO()):
            rc = main(
                ["--as", "grok", "--url", "http://127.0.0.1:9", "--json",
                 "reply", "mabc", "-b", "x", "--to", "kimi", "--to", "cursor"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[4]["to"], "kimi,cursor")

    def test_to_takes_a_comma_list_too(self):
        with patch("infocenter.cli.call", return_value={"id": "m1"}) as call, redirect_stdout(io.StringIO()):
            rc = main(
                ["--as", "grok", "--url", "http://127.0.0.1:9", "--json",
                 "reply", "mabc", "-b", "x", "--to", "kimi,cursor"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[4]["to"], "kimi,cursor")

    def test_single_to_still_works(self):
        with patch("infocenter.cli.call", return_value={"id": "m1"}) as call, redirect_stdout(io.StringIO()):
            rc = main(
                ["--as", "grok", "--url", "http://127.0.0.1:9", "--json",
                 "reply", "mabc", "-b", "x", "--to", "kimi"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_args.args[4]["to"], "kimi")

    def test_both_and_neither_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.txt"
            path.write_text("x\n", encoding="utf-8")
            err = io.StringIO()
            with patch("infocenter.cli.call") as call, redirect_stderr(err):
                both = main(
                    [
                        "--as",
                        "grok",
                        "--url",
                        "http://127.0.0.1:9",
                        "reply",
                        "mabc",
                        "-b",
                        "x",
                        "--body-file",
                        str(path),
                    ]
                )
                neither = main(["--as", "grok", "--url", "http://127.0.0.1:9", "reply", "mabc"])
        self.assertIn("只能用一个", err.getvalue())
        self.assertIn("需要 --body 或 --body-file", err.getvalue())
        self.assertEqual(both, 1)
        self.assertEqual(neither, 1)
        call.assert_not_called()

    def test_cli_reads_file_before_asking_the_server(self):
        """真入口：死端口上不应发信，但必须先把文件读进去（过了 argparse）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.txt"
            path.write_text(_TRICKY, encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin" / "ic"),
                    "--url",
                    "http://127.0.0.1:9",
                    "--home",
                    tmp,
                    "reply",
                    "mabc",
                    "--body-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("没有在 http://127.0.0.1:9 上应答", proc.stderr)
        self.assertNotIn("unrecognized", proc.stderr)
        self.assertNotIn("invalid_body", proc.stderr)
        self.assertNotIn("echo hi", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
