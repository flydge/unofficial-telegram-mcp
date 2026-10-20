"""Run the suite against a fresh owner-only HOME, never the live runtime tree.

config.py derives every runtime path from Path.home() at import time, so HOME
must change before any telegram_search_mcp module is imported.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

if any(name == "telegram_search_mcp" or name.startswith("telegram_search_mcp.") for name in sys.modules):
    raise RuntimeError("telegram_search_mcp was imported before tests/conftest.py isolated HOME; "
                       "run pytest without plugins that import it")

TEST_HOME = Path(tempfile.mkdtemp(prefix="tgmcp-test-home-")).resolve()
os.chmod(TEST_HOME, 0o700)
os.environ["HOME"] = str(TEST_HOME)


def pytest_unconfigure(config) -> None:
    shutil.rmtree(TEST_HOME, ignore_errors=True)
