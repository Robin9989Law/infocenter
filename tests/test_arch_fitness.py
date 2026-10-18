"""架构适应度门禁测试（Architecture Fitness Functions）。

将宏观解耦架构固化为自动化 AST 契约，防止后续迭代或自治 Agent 退化回巨石文件：
1. 分层单向依赖契约（Acyclic Layering）：领域模块（messages/governance/locks/cases/windows）互不直接导入，且严禁反向依赖传输/表现层（httpapi/cli/crew/mcp_server）。
2. 模块体积红线（File Line Ceilings）：严禁 store.py 或任何领域模块再次膨胀为数千行巨石。
3. 零重复符号定义与零 Mixin 方法覆盖冲突（Zero Duplicate Definitions & Collisions）。
"""

import ast
import unittest
from pathlib import Path

from infocenter.cases import CasesMixin
from infocenter.governance import GovernanceMixin
from infocenter.locks import LocksMixin
from infocenter.messages import MessagesMixin
from infocenter.store import Store
from infocenter.windows import WindowsMixin

PKG_DIR = Path(__file__).resolve().parents[1] / "infocenter"

# 各模块最大行数硬上限（防止向单一文件无休止追加代码）
MODULE_LINE_BUDGETS = {
    "store.py": 750,
    "common.py": 900,
    "governance.py": 1000,
    "locks.py": 1000,
    "windows.py": 1000,
    "messages.py": 1450,
    "cases.py": 1450,
    "httpapi.py": 1500,
    "crew.py": 2500,
    "cli.py": 2800,
}

# 严格单向无环分层（DAG Layer Ranks）：任何模块的顶层导入只能指向更低层级（rank 更小）的模块
MODULE_LAYER_RANK = {
    "paths": 0,
    "flock": 0,
    "common": 1,
    "config": 2,
    # 核心领域层（叶节点，零横向耦合）
    "locks": 3,
    "cases": 3,
    "governance": 3,
    # 会话窗口层（协调锁与案卷认领）
    "windows": 4,
    # 消息投递与事件流层
    "messages": 5,
    # 聚合存储门面
    "store": 6,
    # 传输、协议与控制外层
    "httpapi": 7,
    "server": 8,
    "client": 9,
    "sync": 10,
    "crew": 10,
    "mcp_server": 10,
    "cli": 11,
}


def _top_level_infocenter_imports(py_file: Path) -> set[str]:
    """解析模块顶层导入的 infocenter 子模块名集合。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("infocenter."):
                    imported.add(alias.name.split(".", 1)[1].split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "infocenter":
                for alias in node.names:
                    imported.add(alias.name)
            elif mod.startswith("infocenter."):
                imported.add(mod.split(".", 1)[1].split(".", 1)[0])
    return imported


class TestArchitectureFitness(unittest.TestCase):
    def test_module_line_budgets(self):
        """所有核心模块行数必须控制在预算红线以内，杜绝巨石文件复辟。"""
        violations = []
        for name, budget in MODULE_LINE_BUDGETS.items():
            path = PKG_DIR / name
            self.assertTrue(path.is_file(), f"缺少核心模块 {name}")
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count > budget:
                violations.append(f"{name}: {line_count} 行 > 上限 {budget} 行")
        self.assertEqual(violations, [], "模块体积超出架构上限：\n" + "\n".join(violations))

    def test_strict_dag_layering_and_leaf_isolation(self):
        """全包模块必须严格满足单向无环分层（DAG）：低层严禁反向导入高层，叶节点领域模块（locks/cases/governance）互不导入。"""
        violations = []
        for mod_name, rank in MODULE_LAYER_RANK.items():
            py_file = PKG_DIR / f"{mod_name}.py"
            if not py_file.is_file():
                continue
            for target in sorted(_top_level_infocenter_imports(py_file)):
                if target == mod_name or target not in MODULE_LAYER_RANK:
                    continue
                target_rank = MODULE_LAYER_RANK[target]
                if target_rank >= rank:
                    violations.append(
                        f"{mod_name}.py (L{rank}) 顶层违规导入同层或高层模块 {target}.py (L{target_rank})"
                    )
        self.assertEqual(violations, [], "违反单向无环分层（DAG）契约：\n" + "\n".join(violations))

    def test_no_duplicate_function_or_method_definitions_in_any_module(self):
        """任何模块同一作用域内不得重复定义同名函数或方法（防粘贴覆盖隐患）。"""
        duplicates = []
        for py_file in sorted(PKG_DIR.glob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            seen_top: dict[str, int] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen_top:
                        duplicates.append(
                            f"{py_file.name}: 顶层符号 {node.name!r} 在第 {seen_top[node.name]} 行与第 {node.lineno} 行重复定义"
                        )
                    seen_top[node.name] = node.lineno
                if isinstance(node, ast.ClassDef):
                    seen_methods: dict[str, int] = {}
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if item.name in seen_methods:
                                duplicates.append(
                                    f"{py_file.name}:{node.name}.{item.name} 在第 {seen_methods[item.name]} 行与第 {item.lineno} 行重复定义"
                                )
                            seen_methods[item.name] = item.lineno
        self.assertEqual(duplicates, [], "发现重复定义符号：\n" + "\n".join(duplicates))

    def test_mixin_methods_are_disjoint(self):
        """Store 自身方法与五大领域 Mixin 的方法集合必须完全正交，禁止静默覆盖。"""
        classes = [
            ("Store", Store),
            ("MessagesMixin", MessagesMixin),
            ("GovernanceMixin", GovernanceMixin),
            ("LocksMixin", LocksMixin),
            ("CasesMixin", CasesMixin),
            ("WindowsMixin", WindowsMixin),
        ]
        owner_by_method: dict[str, str] = {}
        collisions: list[str] = []
        for cls_name, cls in classes:
            for attr, val in cls.__dict__.items():
                if attr.startswith("__") and attr.endswith("__"):
                    continue
                if callable(val):
                    if attr in owner_by_method:
                        collisions.append(
                            f"方法 {attr!r} 同时定义在 {owner_by_method[attr]} 与 {cls_name}"
                        )
                    else:
                        owner_by_method[attr] = cls_name
        self.assertEqual(collisions, [], "发现 Mixin 方法覆盖冲突：\n" + "\n".join(collisions))


if __name__ == "__main__":
    unittest.main()
