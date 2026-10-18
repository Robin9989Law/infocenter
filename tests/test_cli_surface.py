"""CLI 表面对账：每个子命令叫得起来、每个开关都有人读。

为什么加（今晚实测）：`ic case <id> --status 待谁` 单独用会**静默**走成"只看一眼"——看着像改了、
其实只把案卷显示了一遍。这类"开关收了但没人处理"的洞，最便宜的机器兜底是两件事：

1. **静态对账**：`add_argument` 定义的每个开关，都要在 `cli.py` 里被 `args.<dest>`（或
   `getattr(args, "<dest>")`）读到；白名单必须**写明理由**（目前只有 argparse 自己消化的 `--version`）。
2. **冒烟矩阵**：每个子命令一条最小 argv——桩掉 HTTP 客户端/入口之后，必须**真的打到**那条路
   （HTTP 类打到 `call`，其余打到各自的入口），且不许抛非 `MailError` 的异常。
   新加子命令若忘了进这张表，测试会红（"忘了接线"比"接错线"更常见）。

**已知不够（登记，未做）**：
1. 挡不住"开关只在某个分支里被读"那类——`case --status` 正是这种（它确实被读了，但只在带 `--note` 的那条路上）。
   `build_parser()` 已经可构造；还差逐个开关"单独给一次"扫一遍。

循环生成的子解析器（kimi 2026-10-16 追审登记，本轮已解决）：对账改为从 `build_parser()` 递归内省，
不再认字面 `sub.add_parser("x")`。`crew` 的 pause/resume/abort/close 与 `sync` 的 export/import/once
必须出现在内省集合里，退回正则会红。
"""

import argparse
import ast
import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import cli

CLI_SRC = (ROOT / "infocenter" / "cli.py").read_text(encoding="utf-8")

# `crew` / `sync` 下面还有一层子动作。它们由父命令那条冒烟项和各自的测试覆盖，
# 不必各来一条最小 argv。`crew status` 与顶层 `status` 同名，名字已在 SMOKE 里
# （冒烟 argv 就是 `crew status`）。
NESTED_SUBCOMMANDS = {
    "up", "kickoff", "down", "watch",
    "pause", "resume", "abort", "close",
    "export", "import", "once",
}
# 在循环里 add_parser 的名字。内省必须看见；退回字面正则会漏掉这 7 个。
LOOP_SUBCOMMANDS = {"pause", "resume", "abort", "close", "export", "import", "once"}


class _Any(dict):
    """万能返回值：任何键都给一个"空但能继续"的值。

    冒烟只查**接线**（这条命令有没有真的打到 HTTP），不查返回表的形状——打印器要 `payload["id"]`
    这类字段，给不出就 KeyError，那是"桩太薄"，不是"线断了"。
    """

    def __missing__(self, key):
        return _Any()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

# 白名单：确实不需要 `args.<dest>` 的读点。**每条都要写理由**。
NO_READ_NEEDED = {
    "--version": "argparse 的 action='version' 自己消化，不进 args",
}

# 冒烟矩阵：子命令 → (最小 argv, 这条路该打到谁)
#   "call"   = HTTP 客户端（桩 cli.call）
#   其他名字 = cli 里的入口（桩同名对象 / 模块函数）
SMOKE = {
    "serve": (["serve"], "serve"),
    "stop": (["stop"], "stop"),
    "status": (["status"], "_status"),
    "agents": (["agents"], "call"),
    "register": (["register"], "call"),
    "send": (["send", "--to", "x", "-s", "s", "-b", "b"], "call"),
    "inbox": (["inbox"], "call"),
    "peek": (["peek", "m1"], "call"),
    "read": (["read", "m1"], "call"),
    "archive": (["archive", "m1"], "call"),
    "reply": (["reply", "m1", "-b", "b"], "call"),
    "thread": (["thread", "m1"], "call"),
    "case": (["case", "m1"], "call"),
    "cases": (["cases"], "call"),
    "task": (["task", "show", "t1"], "call"),
    "tasks": (["tasks"], "call"),
    "audit": (["audit"], "call"),
    "guard": (["guard", "--path", "a.py"], "call"),
    "queue": (["queue", "a.py", "--note", "x", "--no-todo"], "call"),
    "locks": (["locks"], "call"),
    "lock": (["lock", "a.py"], "call"),
    "unlock": (["unlock"], "call"),
    "probe": (["probe", "--no-log"], "_probe"),
    "doctor": (["doctor"], "_doctor"),
    "projects": (["projects"], "call"),
    "windows": (["windows"], "call"),
    "mcp": (["mcp"], "mcp_server.run_mcp"),
    "crew": (["crew", "status"], "_crew"),
    "sync": (["sync", "export", "--dir", "."], "_sync"),
}


def _flags_with_dests() -> list[tuple[str, str]]:
    """(开关, dest) —— 从源码里抽，不跑 argparse。"""
    out = []
    tree = ast.parse(CLI_SRC)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        opt = first.value
        if not opt.startswith("-"):
            continue
        dest = None
        for kw in node.keywords:
            if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                dest = kw.value.value
        out.append((opt, dest or opt.lstrip("-").replace("-", "_")))
    return out


def _subcommand_names(parser=None) -> set[str]:
    """从 `build_parser()` 递归收集子命令名（subparsers 逐层下钻，含循环里建的）。"""
    if parser is None:
        parser = cli.build_parser()
    names: set[str] = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            names.add(name)
            names |= _subcommand_names(sub)
    return names


class CliSurfaceTest(unittest.TestCase):
    def setUp(self):
        # 宿主会话的环境不能漏进来（守护会注入这几个），否则身份/窗口被预绑、假红。
        for key in ("INFOCENTER_AGENT", "INFOCENTER_WINDOW", "INFOCENTER_NO_PID"):
            os.environ.pop(key, None)
        os.environ["INFOCENTER_QUIET"] = "1"

    def test_每个开关都有人读(self):
        unread = []
        for opt, dest in _flags_with_dests():
            hits = len(re.findall(rf"args\.{re.escape(dest)}\b", CLI_SRC))
            hits += len(re.findall(rf'getattr\(args,\s*"{re.escape(dest)}"', CLI_SRC))
            if hits == 0 and opt not in NO_READ_NEEDED:
                unread.append(f"{opt}（args.{dest}）")
        self.assertEqual(
            unread, [],
            "这些开关定义了却没人读——要么接上，要么进白名单并写明理由：" + "、".join(unread),
        )

    def test_子命令都在冒烟矩阵里(self):
        names = _subcommand_names()
        missing = sorted(name for name in names if name not in SMOKE and name not in NESTED_SUBCOMMANDS)
        self.assertEqual(missing, [], f"这些子命令没有冒烟项（新加的？加一条最小 argv）：{missing}")
        # 防退回字面正则：那条路看不见循环里建的解析器。
        unseen = sorted(LOOP_SUBCOMMANDS - names)
        self.assertEqual(unseen, [], f"内省没看见循环生成的子命令（退回字面正则了？）：{unseen}")

    def test_每个子命令叫得起来_并且真的打到那条路(self):
        for name, (argv, target) in sorted(SMOKE.items()):
            with self.subTest(cmd=name):
                stubs = {}
                if target == "call":
                    stubs["infocenter.cli.call"] = mock.Mock(return_value=_Any())
                elif target == "serve":
                    stubs["infocenter.cli.serve"] = mock.Mock(return_value=0)
                elif target == "stop":
                    stubs["infocenter.cli.stop"] = mock.Mock(return_value=0)
                elif target.startswith("mcp_server."):
                    stubs[f"infocenter.{target}"] = mock.Mock(return_value=None)
                else:
                    stubs[f"infocenter.cli.{target}"] = mock.Mock(return_value=0)

                patchers = [mock.patch(k, v) for k, v in stubs.items()]
                for p in patchers:
                    p.start()
                try:
                    buf, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                        # HTTP 类用 `--json` 跑：冒烟只查**接线**（call 有没有被打到），
                        # 人话打印器的字段耦合归各命令自己的测试管，不在这儿考。
                        extra = ["--json"] if target == "call" else []
                        code = cli.main(["--as", "human", *argv, *extra])
                finally:
                    for p in reversed(patchers):
                        p.stop()

                self.assertIsInstance(code, int, f"{name}: 返回值不是整数")
                self.assertNotEqual(code, 2, f"{name}: argparse 用法错误（argv 写错了？）{err.getvalue()[:200]}")
                stub = next(iter(stubs.values()))
                self.assertTrue(
                    stub.called,
                    f"{name}: 没打到 {target}（argv={argv}）——接线断了？stdout={buf.getvalue()[:120]}",
                )

    def test_缺省身份是_manager_帮助保留旧名(self):
        from infocenter import config as cfg

        self.assertEqual(cli._env_agent(), "manager")
        self.assertEqual(cli._env_agent(), cfg.HUMAN)

        parser = cli.build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

        def opt_help(command: str, flag: str) -> str:
            for action in sub.choices[command]._actions:
                if flag in action.option_strings:
                    return action.help or ""
            self.fail(f"{command} {flag} 没有帮助")

        as_help = next(a.help or "" for a in cli._GLOBAL._actions if "--as" in a.option_strings)
        self.assertIn("否则 manager", as_help)
        self.assertIn("旧名 human 仍被接受", as_help)
        self.assertNotIn("否则 human", as_help)

        force = opt_help("lock", "--force")
        self.assertIn("manager＝用户本人，天然算", force)
        self.assertIn("旧名 human 仍被接受", force)
        self.assertNotIn("human 天然算", force)

        unlock = opt_help("unlock", "--force")
        self.assertIn("manager＝用户本人", unlock)
        self.assertIn("旧名 human 仍被接受", unlock)

        role = opt_help("agents", "--role")
        self.assertIn("人的身份名是 manager", role)
        self.assertIn("旧名 human 仍被接受", role)

        projects_help = next(
            (a.help or "") for a in sub._choices_actions if a.dest == "projects"
        )
        self.assertIn("manager", projects_help)
        self.assertNotIn("human**", projects_help)
        coord = opt_help("projects", "--coordinator")
        self.assertIn("只有 manager", coord)
        self.assertIn("旧名 human 仍被接受", coord)
        self.assertNotIn("只有 human", coord)


if __name__ == "__main__":
    unittest.main()
