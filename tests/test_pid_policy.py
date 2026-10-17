"""对账轮：四套 pid 判活收成 store.pid_alive 的三种口径。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infocenter import crew, server
from infocenter.store import pid_alive


class PidPolicyTest(unittest.TestCase):
    def test_kill_zero_succeeds_but_managed_rejects_it(self):
        # 这个洞的前提：信号 0 打到进程组会成功。护栏必须比这次调用更严。
        os.kill(0, 0)
        self.assertFalse(pid_alive(0, policy="managed"))
        self.assertFalse(pid_alive(1, policy="managed"))
        self.assertFalse(pid_alive(-5, policy="managed"))

    def test_low_pids_do_not_signal(self):
        with patch("infocenter.common.os.kill") as kill:
            self.assertFalse(pid_alive(0, policy="not_dead"))
            self.assertFalse(pid_alive(-3, policy="running"))
            self.assertFalse(pid_alive(1, policy="managed"))
            kill.assert_not_called()

    def test_none_is_not_dead_and_not_running(self):
        self.assertTrue(pid_alive(None, policy="not_dead"))
        self.assertFalse(pid_alive(None, policy="running"))
        self.assertFalse(pid_alive(None, policy="managed"))

    def test_invalid_is_not_dead_and_not_running(self):
        self.assertTrue(pid_alive("nope", policy="not_dead"))
        self.assertFalse(pid_alive("nope", policy="running"))
        self.assertFalse(pid_alive("nope", policy="managed"))
        self.assertFalse(pid_alive(True, policy="managed"))
        self.assertFalse(pid_alive(True, policy="running"))

    def test_string_pid_only_running_and_not_dead_coerce(self):
        me = os.getpid()
        self.assertFalse(pid_alive(str(me), policy="managed"))
        self.assertTrue(pid_alive(str(me), policy="running"))
        self.assertTrue(pid_alive(str(me), policy="not_dead"))
        self.assertTrue(pid_alive(me, policy="managed"))

    def test_pid_one_is_excluded_from_managed_but_not_from_not_dead(self):
        # managed/running 在发信号前就排除 pid 1。not_dead 仍问内核：
        # 成功或 EPERM 都「不算死」，进程不存在才算死。
        self.assertFalse(pid_alive(1, policy="managed"))
        self.assertFalse(pid_alive(1, policy="running"))
        try:
            os.kill(1, 0)
        except PermissionError:
            self.assertTrue(pid_alive(1, policy="not_dead"))
        except OSError:
            self.assertFalse(pid_alive(1, policy="not_dead"))
        else:
            self.assertTrue(pid_alive(1, policy="not_dead"))

    def test_permission_is_not_death(self):
        with patch("infocenter.common.os.kill", side_effect=PermissionError):
            self.assertTrue(pid_alive(os.getpid(), policy="not_dead"))
            self.assertFalse(pid_alive(os.getpid(), policy="running"))
            self.assertFalse(pid_alive(os.getpid(), policy="managed"))

    def test_missing_process_is_dead_for_every_policy(self):
        with patch("infocenter.common.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(pid_alive(12345, policy="not_dead"))
            self.assertFalse(pid_alive(12345, policy="running"))
            self.assertFalse(pid_alive(12345, policy="managed"))

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            pid_alive(os.getpid(), policy="alive")

    def test_crew_delegates_to_managed(self):
        self.assertFalse(crew._pid_alive(0))
        self.assertFalse(crew._pid_alive(1))
        self.assertFalse(crew._pid_alive(True))
        self.assertTrue(crew._pid_alive(os.getpid()))

    def test_claim_does_not_treat_pid_zero_as_running(self):
        # 旧 server.pid_alive(0) 为真时，claim 会在打开 server.lock 之前就返回 running。
        # 护栏生效后先否掉 pid 0，才会去抢锁；健康检查仍可能随后认定「有人在应答」。
        os.kill(0, 0)
        previous = server._LOCK
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "server.json").write_text(
                json.dumps({"pid": 0, "url": "http://127.0.0.1:9"}),
                encoding="utf-8",
            )
            try:
                with patch.object(server, "healthy", return_value=True):
                    state, _url = server.claim(home)
                self.assertEqual(state, "running")
                self.assertTrue((home / "server.lock").exists())
            finally:
                held = server._LOCK
                server._LOCK = previous
                if held is not None and held is not previous:
                    held.close()


if __name__ == "__main__":
    unittest.main()
