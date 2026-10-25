"""The td_api.h the TDLib schema pins read: the installed pinned keg's, or a candidate build's.

TGMCP_TDLIB_HEADER, an absolute path to a generated td_api.h (build_tdlib_source.py writes one to
include/td/telegram/td_api.h of its output), points the pins at a candidate without relinking Homebrew
(docs/tdlib-upgrade.md, step 4). An override that names no regular file fails; only a missing keg header skips.
"""
from __future__ import annotations

import os
from pathlib import Path

from telegram_search_mcp.config import TDLIB_LIBRARY

ENVIRONMENT = 'TGMCP_TDLIB_HEADER'


def header_path() -> tuple[Path, bool]:
    """(path, overridden)."""
    override = os.environ.get(ENVIRONMENT)
    if override:
        return Path(override), True
    return TDLIB_LIBRARY.resolve().parent.parent / 'include/td/telegram/td_api.h', False


def pinned_header(test) -> str:
    path, overridden = header_path()
    if not path.is_file():
        if overridden:
            test.fail(f'{ENVIRONMENT} does not name a regular file')
        test.skipTest('pinned TDLib development header is not installed')
    return path.read_text()
