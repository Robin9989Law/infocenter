"""缺省身份与 config.manager。

人的现名是 manager，旧名 human 只在比较时认。名字优先于 role；
两个名字都在名册里时认现名。符号 HUMAN / HUMAN_NAMES / is_person 以 store 为唯一出处。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import config
from infocenter.cli import _env_agent


class IdentityConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._agent = os.environ.pop("INFOCENTER_AGENT", None)

    def tearDown(self):
        if self._agent is not None:
            os.environ["INFOCENTER_AGENT"] = self._agent
        else:
            os.environ.pop("INFOCENTER_AGENT", None)
        self.tmp.cleanup()

    def test_symbols_follow_store_when_it_exports_them(self):
        import infocenter.store as store

        if not all(hasattr(store, name) for name in ("HUMAN", "HUMAN_NAMES", "is_person")):
            self.skipTest("store 还没导出 HUMAN / HUMAN_NAMES / is_person")
        self.assertIs(config.HUMAN, store.HUMAN)
        self.assertIs(config.HUMAN_NAMES, store.HUMAN_NAMES)
        self.assertIs(config.is_person, store.is_person)

    def test_fresh_seed_names_the_person_manager(self):
        agents = {item["name"]: item for item in config.load(self.home)["agents"]}
        self.assertIn(config.HUMAN, agents)
        self.assertNotIn("human", agents)
        person = agents[config.HUMAN]
        self.assertEqual(person["role"], "manager")
        self.assertEqual(
            person["description"],
            "用户（全局管理者：只有他能指定项目的 coordinator、移除成员）",
        )
        self.assertEqual(config.manager(self.home), config.HUMAN)
        self.assertTrue(config.is_person("manager"))
        self.assertTrue(config.is_person("human"))
        self.assertFalse(config.is_person("grok"))

    def test_canonical_name_beats_alias_and_alias_beats_role(self):
        self._write([
            {"name": "opencode", "role": "manager"},
            {"name": "human", "description": "旧"},
        ])
        self.assertEqual(config.manager(self.home), "human")

        self._write([
            {"name": "human"},
            {"name": "manager"},
            {"name": "opencode", "role": "coordinator"},
        ])
        self.assertEqual(config.manager(self.home), "manager")

    def test_role_fallback_accepts_legacy_coordinator(self):
        self._write([
            {"name": "grok"},
            {"name": "opencode", "role": "coordinator"},
        ])
        self.assertEqual(config.manager(self.home), "opencode")

    def test_none_when_no_person_and_no_role(self):
        self._write([{"name": "grok"}])
        self.assertIsNone(config.manager(self.home))

    def test_default_agent_is_manager_unless_env_set(self):
        os.environ.pop("INFOCENTER_AGENT", None)
        self.assertEqual(_env_agent(), config.HUMAN)
        self.assertEqual(_env_agent(), "manager")
        os.environ["INFOCENTER_AGENT"] = "kimi"
        self.assertEqual(_env_agent(), "kimi")
        os.environ["INFOCENTER_AGENT"] = "   "
        self.assertEqual(_env_agent(), "manager")

    def _write(self, agents: list) -> None:
        (self.home / "config.json").write_text(
            json.dumps({"agents": agents}), encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
