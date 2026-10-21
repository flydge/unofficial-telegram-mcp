"""Every user-visible product name in one place; internal compatibility names stay elsewhere.

Not renamed here, on purpose: the Python package, the `TelegramSearchMCP` support paths, the
Keychain service, launchd labels, `TELEGRAM_SEARCH_*` environment keys and the intent file.
Callers read these through the module (`identity.DISPLAY_NAME`), so one edit here renames the
product everywhere. A few values are captured at import (the connect page assets and the
registration modules' SERVER_NAME), so patching them at runtime does not reach those.
"""
from __future__ import annotations
from collections.abc import Iterable
import re
from typing import Final

DISPLAY_NAME: Final = 'Unofficial Telegram MCP'
MCP_SERVER_NAME: Final = 'telegram_search'
PLUGIN_NAME: Final = 'telegram-local'
PLUGIN_SERVER_NAME: Final = 'tg'
MARKETPLACE_NAME: Final = 'unofficial-telegram-mcp'
CODESIGN_ID_PREFIX: Final = 'io.github.flydge.unofficial-telegram-mcp'
BUNDLE_ROOT_NAME: Final = 'TelegramSearch'
DMG_VOLUME_NAME: Final = 'Unofficial Telegram MCP'
DEVICE_MODEL: Final = 'Unofficial Telegram MCP'

RESERVED_PLUGIN_NAMES: Final = frozenset({'telegram'})
TOOL_NAME_LIMIT: Final = 64
PLUGIN_TOOL_PREFIX: Final = 'mcp__plugin_'
_MAX_TEXT = 64
_MAX_VOLUME = 27
_SLUG = re.compile(r'[a-z][a-z0-9_-]*\Z')
_LABEL = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
_REVERSE_DNS = re.compile(r'[a-z][a-z0-9]*(?:\.' + _LABEL + r'){2,}\Z')
_TEXT = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9])?\Z')
_ROOT = re.compile(r'[A-Za-z][A-Za-z0-9]*\Z')


class IdentityError(ValueError):
    """A product name broke a constraint; carries only a fixed code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _need(ok: bool, code: str) -> None:
    if not ok:
        raise IdentityError(code)


def check_names(*, display_name: str | None = None, mcp_server_name: str | None = None,
                plugin_name: str | None = None, plugin_server_name: str | None = None,
                marketplace_name: str | None = None, codesign_id_prefix: str | None = None,
                bundle_root_name: str | None = None, dmg_volume_name: str | None = None,
                device_model: str | None = None, tool_names: Iterable[str] | None = None) -> None:
    """Raise `IdentityError(code)` unless the names are valid; an omitted name is the module's own.

    `mcp__plugin_<plugin>_<server>__<tool>` must fit 64 characters for every name in `tool_names`,
    which defaults to every registered tool; with the longest one (29 characters) that is
    `len(plugin) + len(server) <= 20`."""
    if tool_names is None:
        from .server import _TOOL_NAMES
        tool_names = _TOOL_NAMES
    display = DISPLAY_NAME if display_name is None else display_name
    server = MCP_SERVER_NAME if mcp_server_name is None else mcp_server_name
    plugin = PLUGIN_NAME if plugin_name is None else plugin_name
    plugin_server = PLUGIN_SERVER_NAME if plugin_server_name is None else plugin_server_name
    market = MARKETPLACE_NAME if marketplace_name is None else marketplace_name
    prefix = CODESIGN_ID_PREFIX if codesign_id_prefix is None else codesign_id_prefix
    root = BUNDLE_ROOT_NAME if bundle_root_name is None else bundle_root_name
    volume = DMG_VOLUME_NAME if dmg_volume_name is None else dmg_volume_name
    model = DEVICE_MODEL if device_model is None else device_model
    for value in (server, plugin, plugin_server, market):
        _need(type(value) is str and bool(_SLUG.match(value)), 'name_grammar')
    _need(plugin not in RESERVED_PLUGIN_NAMES, 'plugin_name_reserved')
    for value, limit in ((display, _MAX_TEXT), (volume, _MAX_VOLUME), (model, _MAX_TEXT)):
        _need(type(value) is str and len(value) <= limit and bool(_TEXT.match(value)), 'display_grammar')
    _need(type(root) is str and bool(_ROOT.match(root)), 'bundle_root_grammar')
    _need(type(prefix) is str and len(prefix) <= 100 and bool(_REVERSE_DNS.match(prefix)), 'codesign_prefix')
    _need(len(plugin) + len(plugin_server) <= 20, 'plugin_name_budget')
    head = PLUGIN_TOOL_PREFIX + plugin + '_' + plugin_server + '__'
    for tool in tool_names:
        _need(type(tool) is str and len(head) + len(tool) <= TOOL_NAME_LIMIT, 'plugin_tool_name_length')
