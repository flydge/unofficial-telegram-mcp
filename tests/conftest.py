"""Run the suite against a fresh owner-only HOME, never the live runtime tree.

config.py derives every runtime path from Path.home() at import time, so HOME
must change before any telegram_search_mcp module is imported. The process that made the HOME
removes it at exit, also when pytest never configures (a usage error) or no pytest runs at all;
only that owner-only directory directly inside $TMPDIR, never through a link, never from a fork.

The process umask is process-wide: a test that reaches TDLibClient.from_defaults (os.umask(0o077))
without patching it would make every later test create owner-only directories. Each test must leave
the umask it found; the guard fails the leaking test and restores the session value for the rest.
"""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

import pytest

if any(name == "telegram_search_mcp" or name.startswith("telegram_search_mcp.") for name in sys.modules):
    raise RuntimeError("telegram_search_mcp was imported before tests/conftest.py isolated HOME; "
                       "run pytest without plugins that import it")

HOME_PREFIX = "tgmcp-test-home-"
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
TEST_HOME = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=TEMP_ROOT))
os.chmod(TEST_HOME, 0o700)
os.environ["HOME"] = str(TEST_HOME)
_CREATOR = os.getpid()


def remove_session_home(home: Path, temp_root: Path) -> bool:
    """Remove a session HOME: only this user's owner-only HOME_PREFIX directory directly inside `temp_root`.

    The path itself is never a link, and rmtree's descriptor walk follows none below it. Returns whether it is gone.
    """
    try:
        info = os.lstat(home)
    except FileNotFoundError:
        return False
    if (home.parent != temp_root or not home.name.startswith(HOME_PREFIX) or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or
            not shutil.rmtree.avoids_symlink_attacks):
        return False
    shutil.rmtree(home, ignore_errors=True)
    return not os.path.lexists(home)


def _remove_own_session_home() -> None:
    if os.getpid() == _CREATOR:  # A forked child inherits the handler; only the process that made the HOME removes it.
        remove_session_home(TEST_HOME, TEMP_ROOT)


atexit.register(_remove_own_session_home)


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
    _remove_own_session_home()
