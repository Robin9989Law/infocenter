"""CLI 表面对账：每个子命令叫得起来、每个开关都有人读。

为什么加（今晚实测）：`ic case <id> --status 待谁` 单独用会**静默**走成"只看一眼"——看着像改了、
其实只把案卷显示了一遍。这类"开关收了但没人处理"的洞，最便宜的机器兜底是两件事：

1. **静态对账**：`add_argument` 定义的每个开关，都要在 `cli.py` 里被 `args.<dest>`（或
   `getattr(args, "<dest>")`）读到；白名单必须**写明理由**（目前只有 argparse 自己消化的 `--version`）。
2. **冒烟矩阵**：每个子命令一条最小 argv——桩掉 HTTP 客户端/入口之后，必须**真的打到**那条路
   （HTTP 类打到 `call`，其余打到各自的入口），且不许抛非 `MailError` 的异常。
   新加子命令若忘了进这张表，测试会红（"忘了接线"比"接错线"更常见）。

**已知不够（登记，未做）**：
1. 挡不住"开关只在某个分支里被读"那类——`--status` 正是这种（它确实被读了，但只在带 `--note` 的那条路上）。
   要机器化，得先把 parser 抽成可构造的（`build_parser()`），再逐个开关"单独给一次"扫一遍。
2. **循环生成的子解析器扫不到**（kimi 2026-10-16 追审登记）：静态那段只认字面 `sub.add_parser("x")`，
   `crew` / `sync` 里在循环里建的那些看不见 ⇒ "新加子命令忘了进表"只对字面写得出的生效。
   同样得等 `build_parser()` 才能根治。
"""

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

# `crew` 下面还有一层子动作（`ic crew up|kickoff|down|watch`）——它们由 `ic crew` 那条冒烟项
# 连同 crew 自己的测试覆盖，不必各来一条最小 argv。
NESTED_SUBCOMMANDS = {"up", "kickoff", "down", "watch"}


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


def _subcommands_in_source() -> list[str]:
    names = []
    for m in re.finditer(r'sub\.add_parser\(\s*"([a-z0-9\-]+)"', CLI_SRC):
        names.append(m.group(1))
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
        missing = [name for name in _subcommands_in_source()
                   if name not in SMOKE and name not in NESTED_SUBCOMMANDS]
        self.assertEqual(missing, [], f"这些子命令没有冒烟项（新加的？加一条最小 argv）：{missing}")

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


if __name__ == "__main__":
    unittest.main()
