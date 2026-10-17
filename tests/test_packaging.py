"""发布卫生：**真打一个包，看包里有什么**。

现场（2026-10-16，v0.5.0 发布前）：`pip wheel .` 出来的 wheel 只有 18 个文件、**没有
`infocenter/web/index.html`** —— 外人 `pip install` 完能起服务，但**控制台 `/ui` 404**。
内部跑一万次测试都发现不了：所有测试都从**源码树**读文件，只有"装出来的那份"才有这个问题。
所以这条测试不查配置、**真的打个包再打开看**。

口径：pip/构建环境不在 ⇒ 跳过并说明（不假装绿）。构建只在临时目录里落 wheel，
`build/`、`*.egg-info/` 都已在 .gitignore 里。
"""

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import __version__


def _wheel() -> Path | None:
    """打一个 wheel 出来；打不了（没有 pip / 没有 setuptools）就返回 None。"""
    out = Path(tempfile.mkdtemp(prefix="ic-wheel-"))
    try:
        done = subprocess.run(
            # 用**默认的隔离构建**：pip 会自己准备 setuptools（有缓存时不需要网）。
            # 不加 `--no-build-isolation`——那要求**当前解释器**里就有 setuptools，而它常常没有
            # （实测：本机 python3 与 .venv 都没有）。
            [sys.executable, "-m", "pip", "wheel", ".", "-w", str(out), "--no-deps", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    wheels = list(out.glob("infocenter-*.whl"))
    return wheels[0] if wheels else None


class PackagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wheel = _wheel()
        if cls.wheel is None:
            raise unittest.SkipTest("这台机器打不了包（没有 pip / setuptools）——不假装绿，但也不误报红")

    def _names(self) -> list[str]:
        with zipfile.ZipFile(self.wheel) as zf:
            return zf.namelist()

    def test_控制台单页在包里(self):
        # 这一条就是今晚那个洞（装完 /ui 404）：抓的就是它。
        names = self._names()
        self.assertIn("infocenter/web/index.html", names)
        self.assertGreater(len(names), 18, "包里的文件数不该比「最小可跑」那版还少")

    def test_版本号一致(self):
        with zipfile.ZipFile(self.wheel) as zf:
            text = zf.read("infocenter/__init__.py").decode("utf-8")
        self.assertIn(f'__version__ = "{__version__}"', text)
        self.assertIn(__version__, self.wheel.name)

    def test_命令行入口在包里(self):
        with zipfile.ZipFile(self.wheel) as zf:
            entries = [n for n in zf.namelist() if n.endswith("entry_points.txt")]
            self.assertTrue(entries, "wheel 里没有 entry_points.txt")
            text = zf.read(entries[0]).decode("utf-8")
        self.assertIn("ic = infocenter.cli:main", text)

    def test_关键模块都在(self):
        names = set(self._names())
        for mod in (
            "store.py", "cli.py", "httpapi.py", "server.py", "crew.py", "mcp_server.py",
            "governance.py", "flock.py", "common.py", "cases.py", "windows.py",
        ):
            self.assertIn(f"infocenter/{mod}", names)


if __name__ == "__main__":
    unittest.main()
