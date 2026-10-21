"""tools/list follows the loaded policy; the registration, and so the schema fingerprint, does not.

Shared by the SDK boundary tests: tool counts derive from the registration, never a literal.
"""
from __future__ import annotations

from mcp import Client

from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.server import _TOOL_NAMES, available_tools, build_server

FULL_POLICY = RuntimePolicy(enabled_capabilities=tuple(sorted(KNOWN_CAPABILITIES)))
REGISTERED = len(_TOOL_NAMES)


def usable(policy: RuntimePolicy) -> set[str]:
    """The names tools/list must show under `policy`."""
    return set(available_tools(policy.enabled_capabilities))


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError('provider opened')


async def full_listing() -> dict:
    """Every registered tool as the SDK lists it, in registration order, under a policy enabling all."""
    async with Client(build_server(service_factory=_NoProvider, policy=FULL_POLICY)) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}
