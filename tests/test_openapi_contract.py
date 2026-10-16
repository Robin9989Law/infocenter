"""路由字面 ↔ OPENAPI["paths"] 对账。

GET /v1 和 README 都让新 agent 照 /openapi.json 接。源码里多了一条分发、表里没写，
这里就红——不许再静默漂移。

比对的是分发字面，不是注释：
- `path == "/v1/audit"` 这种精确路径，表里要有同名一条；
- `path.startswith("/v1/cases/")` 并且真的按这个前缀切片（`path[len(...)]` 或 `_one`），
  归一成 `/v1/cases/{}`，再把块里的 `action ==` / `endswith` 收成子路径；
- 只在 CSRF 闸 `is_d_route` 里出现、没有切片分发的前缀，不算接口。
  这类进 `GATE_ONLY_PREFIXES`，每条写理由。
- 确实分发了、但不该进契约的，进 `NOT_IN_CONTRACT`，每条写理由。别默默删。

`/v1/daemons`、`/v1/headless`、`/v1/headless/runs` 本轮写进契约：README 和控制台都在用，
漏掉它们才是这次的缺口，不是「不对外」。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter.httpapi import OPENAPI

SRC_PATH = ROOT / "infocenter" / "httpapi.py"
SRC = SRC_PATH.read_text(encoding="utf-8")

# startswith 了，但没有按这个前缀切片分发。不是接口。
# 真要加子路径：写成 path[len(前缀):] 或 _one(path, 前缀)，并从这里删掉，补进 OPENAPI。
GATE_ONLY_PREFIXES = {
    "/v1/locks/": (
        "只出现在 is_d_route 的 CSRF 分类。"
        "锁的子路径是 /v1/locks、/v1/locks/queue、/v1/locks/queue/merge、/v1/locks/release "
        "四条精确路径，没有 /v1/locks/{id} 这种前缀分发。"
    ),
    "/v1/projects/": (
        "只出现在 is_d_route 的 CSRF 分类。"
        "项目没有 /v1/projects/{id}，GET/POST/PUT 都在精确路径 /v1/projects 上。"
    ),
}

# 分发了、但故意不写进契约的路径（精确路径，或归一后的 /v1/foo/{} ）。每条写理由。
NOT_IN_CONTRACT: dict[str, str] = {}


def _norm(path: str) -> str:
    return re.sub(r"\{[^/}]+\}", "{}", path)


def _prefixes(src: str) -> set[str]:
    return set(re.findall(r'path\.startswith\(\s*"(/[^"]*)"\s*\)', src))


def _is_dispatch_prefix(src: str, prefix: str) -> bool:
    return f'path[len("{prefix}")' in src or f'_one(path, "{prefix}")' in src


def _exact_paths(src: str) -> set[str]:
    return set(re.findall(r'path\s*==\s*"(/[^"]*)"', src))


def _dispatch_blocks(src: str) -> list[tuple[str, str]]:
    """每个真切片的前缀，切出它那一个 `if` 块（到下一个顶格 `if` / 分节注释为止）。"""
    blocks = []
    seen = set()
    for prefix in _prefixes(src):
        if not _is_dispatch_prefix(src, prefix) or prefix in seen:
            continue
        seen.add(prefix)
        idx = src.find(f'path[len("{prefix}")')
        if idx < 0:
            idx = src.find(f'_one(path, "{prefix}")')
        if idx < 0:
            continue
        starts = [m.start() for m in re.finditer(r"(?m)^    if .*$", src[:idx])]
        if not starts:
            continue
        block_start = starts[-1]
        tail = src[idx:]
        boundary = re.search(r"(?m)^    (?:if |# )", tail)
        block_end = idx + boundary.start() if boundary else len(src)
        blocks.append((prefix, src[block_start:block_end]))
    return blocks


def _actions(block: str) -> set[str]:
    names = set(re.findall(r'action\s*==\s*"([^"]+)"', block))
    for group in re.findall(r"action\s+in\s+\{([^}]+)\}", block):
        names.update(re.findall(r'"([^"]+)"', group))
    return names


def _required(src: str) -> set[str]:
    """源码里真实分发的路由，归一成契约路径（参数名一律写成 {}）。"""
    required = {_norm(path) for path in _exact_paths(src)}
    for prefix, block in _dispatch_blocks(src):
        template = prefix.rstrip("/") + "/{}"
        required.add(template)
        for name in _actions(block):
            required.add(template + "/" + name)
        for suffix in re.findall(r'\.endswith\(\s*"(/[^"]+)"\s*\)', block):
            required.add(template + suffix)
    for match in re.finditer(
        r'path\.startswith\(\s*"(/[^"]+)"\s*\).*?path\.endswith\(\s*"(/[^"]+)"\s*\)',
        src,
    ):
        prefix, suffix = match.group(1), match.group(2)
        if _is_dispatch_prefix(src, prefix):
            required.add(prefix.rstrip("/") + "/{}" + suffix)
    return required


def _table(paths: dict) -> set[str]:
    return {_norm(path) for path in paths}


def _skipped(src: str) -> set[str]:
    return {_norm(path) for path in NOT_IN_CONTRACT if _norm(path) in _required(src)}


class OpenApiContractTest(unittest.TestCase):
    def test_源码路由都在契约里_契约也没有多写(self):
        required = _required(SRC) - _skipped(SRC)
        table = _table(OPENAPI["paths"])
        missing = sorted(required - table)
        extra = sorted(table - required)
        self.assertEqual(
            missing,
            [],
            "源码有分发、契约表没有（补 OPENAPI，或进 NOT_IN_CONTRACT 并写理由）：" + "、".join(missing),
        )
        self.assertEqual(
            extra,
            [],
            "契约表有、源码没有这条分发（删表或把路由补回 httpapi.py）：" + "、".join(extra),
        )

    def test_加一条精确路由忘了进表会红(self):
        src = SRC + '\n    if path == "/v1/not-in-the-table":\n        return 200, {}\n'
        missing = _required(src) - _table(OPENAPI["paths"])
        self.assertIn("/v1/not-in-the-table", missing)

    def test_加一条前缀路由忘了进表会红(self):
        src = SRC + (
            '\n    if path.startswith("/v1/widgets/"):\n'
            '        rest = path[len("/v1/widgets/"):]\n'
            '        if action == "spin":\n'
            "            return 200, {}\n"
        )
        missing = _required(src) - _table(OPENAPI["paths"])
        self.assertIn("/v1/widgets/{}", missing)
        self.assertIn("/v1/widgets/{}/spin", missing)

    def test_白名单每条都有理由且确实是源码里的分发(self):
        required = _required(SRC)
        table = _table(OPENAPI["paths"])
        for path, reason in NOT_IN_CONTRACT.items():
            self.assertIsInstance(reason, str)
            self.assertGreaterEqual(len(reason.strip()), 8, path)
            self.assertIn(_norm(path), required, f"{path} 不在源码分发里，白名单过期了")
            self.assertNotIn(_norm(path), table, f"{path} 已经进了契约，不要再挂白名单")

    def test_只做安全闸的前缀不算路由并写明理由(self):
        gate = {prefix for prefix in _prefixes(SRC) if not _is_dispatch_prefix(SRC, prefix)}
        self.assertEqual(gate, set(GATE_ONLY_PREFIXES))
        for prefix, reason in GATE_ONLY_PREFIXES.items():
            self.assertGreaterEqual(len(reason.strip()), 8, prefix)
            self.assertNotIn(prefix.rstrip("/") + "/{}", _table(OPENAPI["paths"]))

    def test_每条契约都有summary(self):
        for path, item in OPENAPI["paths"].items():
            self.assertIsInstance(item, dict, path)
            self.assertTrue(item, path)
            for method, op in item.items():
                summary = op.get("summary") if isinstance(op, dict) else None
                self.assertTrue(summary and str(summary).strip(), f"{method.upper()} {path} 缺 summary")


if __name__ == "__main__":
    unittest.main()
