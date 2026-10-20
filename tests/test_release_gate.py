from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import telegram_search_mcp

TESTS = Path(__file__).resolve().parent
SOURCE = Path(telegram_search_mcp.__file__).resolve().parents[1]

PROBE = '''
import os
from pathlib import Path


def test_live_policy_is_never_read():
    planted = Path(os.environ["TGMCP_PLANTED_HOME"])
    from telegram_search_mcp import config
    from telegram_search_mcp.contract import contract_descriptor, require_current
    assert not Path.home().is_relative_to(planted)
    assert not config.RUNTIME_POLICY_PATH.is_relative_to(planted)
    policy = config.load_runtime_policy()
    assert policy.expected_package_version is None
    require_current(policy)
    contract_descriptor(policy)
'''


class HermeticHomeTests(unittest.TestCase):
    def test_suite_ignores_a_mismatching_policy_in_the_original_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "home" / "Library" / "Application Support" / "TelegramSearchMCP"
            tree.mkdir(parents=True)
            os.chmod(tree, 0o700)
            policy = tree / "runtime.toml"
            policy.write_text('config_version = 1\nenabled_capabilities = ["read"]\n'
                              'expected_package_version = "0.0.0"\n')
            os.chmod(policy, 0o600)
            tests = root / "tests"
            tests.mkdir()
            shutil.copyfile(TESTS / "conftest.py", tests / "conftest.py")
            (tests / "test_probe.py").write_text(PROBE)
            env = {**os.environ, "HOME": str(root / "home"), "TGMCP_PLANTED_HOME": str(root / "home"),
                   "PYTHONPATH": str(SOURCE)}
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     "--rootdir", str(root), str(tests)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])
            self.assertIn("1 passed", result.stdout)
            self.assertEqual(sorted(p.name for p in tree.iterdir()), ["runtime.toml"])
