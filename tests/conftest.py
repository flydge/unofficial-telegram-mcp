"""Run the suite against a fresh owner-only HOME, never the live runtime tree.

config.py derives every runtime path from Path.home() at import time, so HOME
must change before any telegram_search_mcp module is imported.

The process umask is process-wide: a test that reaches TDLibClient.from_defaults (os.umask(0o077))
without patching it would make every later test create owner-only directories. Each test must leave
the umask it found; the guard fails the leaking test and restores the session value for the rest.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

if any(name == "telegram_search_mcp" or name.startswith("telegram_search_mcp.") for name in sys.modules):
    raise RuntimeError("telegram_search_mcp was imported before tests/conftest.py isolated HOME; "
                       "run pytest without plugins that import it")

TEST_HOME = Path(tempfile.mkdtemp(prefix="tgmcp-test-home-")).resolve()
os.chmod(TEST_HOME, 0o700)
os.environ["HOME"] = str(TEST_HOME)


def umask_restored(expected: int) -> int | None:
    """Set the umask to `expected`; return the mask it replaced when that differed, else None.

    Writing the expected value is the probe, so when nothing leaked there is no moment with another mask.
    """
    current = os.umask(expected)
    return None if current == expected else current


SESSION_UMASK = os.umask(0o077)  # No test thread exists yet; the next line puts the mask back.
os.umask(SESSION_UMASK)


@pytest.fixture(autouse=True)
def _umask_unchanged():
    yield
    leaked = umask_restored(SESSION_UMASK)
    if leaked is not None:
        pytest.fail(f"the test left the process umask at {leaked:04o}, not {SESSION_UMASK:04o}", pytrace=False)


def pytest_unconfigure(config) -> None:
    shutil.rmtree(TEST_HOME, ignore_errors=True)
