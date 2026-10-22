"""The owner's capability profiles: show, enable or disable one named profile in the installed runtime.toml.

Only the `enabled_capabilities` line changes; every other byte stays, unknown keys and comments included. The new
file is validated as a policy before it replaces the old one (owner-only temporary, fsync, compare-and-swap
rename), and only after the running broker reports no pending send. Then the receipt-bound broker LaunchAgent
alone is restarted, and only when it is loaded; connected MCP clients see `config_stale` until they reconnect.
Refusals are fixed codes; nothing read from the file is echoed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Callable

from .config import (CAPABILITY_PROFILES, ConfigurationError, KNOWN_CAPABILITIES, PROFILE_DEPENDENCIES, _owner_paths,
                     load_runtime_policy)

ACTIONS = ("show", "enable", "disable")
CODES = frozenset({"policy_unsupported", "policy_conflict", "pending_send_unresolved", "service_restart_failed"})
MAX_POLICY_BYTES = 65536
# The one line that changes: a single-line array, optionally followed by a comment, which is kept.
_LINE = re.compile(rb'^enabled_capabilities[ \t]*=[ \t]*\[[^\]\n]*\][ \t]*(#[^\n]*)?$', re.M)


class ProfileError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code: str):
        super().__init__(code if code in CODES else "policy_unsupported")
        self.code = self.args[0]


def states(enabled) -> dict[str, str]:
    """Each profile: `on` (all its capabilities enabled), `off` (none) or `partial`."""
    present = set(enabled)
    return {name: "on" if set(members) <= present else "off" if not set(members) & present else "partial"
            for name, members in CAPABILITY_PROFILES.items()}


def planned(enabled, action: str, profile: str) -> tuple[str, ...]:
    """The enabled capabilities after the action, sorted as load_runtime_policy returns them: enabling adds the
    profile and the profiles it depends on; disabling removes the profile and every profile that depends on it."""
    if action not in ("enable", "disable") or profile not in CAPABILITY_PROFILES:
        raise ProfileError("policy_unsupported")
    current = set(enabled)
    if action == "enable":
        names = (profile, *PROFILE_DEPENDENCIES.get(profile, ()))
        return tuple(sorted(current | {member for name in names for member in CAPABILITY_PROFILES[name]}))
    names = {profile} | {name for name, needs in PROFILE_DEPENDENCIES.items() if profile in needs}
    return tuple(sorted(current - {member for name in names for member in CAPABILITY_PROFILES[name]}))


def rewrite(data: bytes, enabled) -> bytes:
    """`data` with its one `enabled_capabilities` line replaced; refuses when there is not exactly one."""
    lines = list(_LINE.finditer(data))
    if len(lines) != 1 or not all(capability in KNOWN_CAPABILITIES for capability in enabled):
        raise ProfileError("policy_unsupported")
    match = lines[0]
    value = b"enabled_capabilities = [" + b", ".join(b'"' + c.encode() + b'"' for c in enabled) + b"]"
    if match.group(1):
        value += b" " + match.group(1)
    return data[:match.start()] + value + data[match.end():]


def _read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ProfileError("policy_unsupported") from None
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > MAX_POLICY_BYTES):
            raise ProfileError("policy_unsupported")
        data = stream.read(MAX_POLICY_BYTES + 1)
    if len(data) != metadata.st_size:
        raise ProfileError("policy_conflict")
    return data


def _policy(path: Path):
    try:
        return load_runtime_policy(path)
    except (ConfigurationError, OSError, ValueError):
        raise ProfileError("policy_unsupported") from None


def show(home: Path) -> dict:
    policy = _policy(_owner_paths(Path(home))[0] / "runtime.toml")
    return {"status": "ok", "profiles": states(policy.enabled_capabilities),
            "enabled_capabilities": list(policy.enabled_capabilities)}


def apply(home: Path, action: str, profile: str, *, pending: Callable[[], str],
          restart: Callable[[], str]) -> dict:
    """Enable or disable one profile; `pending()` answers 'none', 'pending' or 'unknown' for the running broker,
    `restart()` restarts the receipt-bound broker when it is loaded and answers 'restarted' or 'not_running'."""
    path = _owner_paths(Path(home))[0] / "runtime.toml"
    before = _read(path)
    current = _policy(path)
    enabled = planned(current.enabled_capabilities, action, profile)
    if enabled == tuple(current.enabled_capabilities):
        return {"status": "unchanged", "profiles": states(enabled), "enabled_capabilities": list(enabled),
                "broker": "unchanged"}
    candidate = rewrite(before, enabled)
    temporary = path.with_name(".runtime.toml." + os.urandom(8).hex() + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        after = _policy(temporary)
        expected = dict(current.public_settings(), enabled_capabilities=list(enabled))
        if after.public_settings() != expected:
            raise ProfileError("policy_unsupported")
        if pending() != "none":
            raise ProfileError("pending_send_unresolved")
        if _read(path) != before:
            raise ProfileError("policy_conflict")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    broker = restart()
    if broker not in ("restarted", "not_running"):
        raise ProfileError("service_restart_failed")
    return {"status": "changed", "profiles": states(enabled), "enabled_capabilities": list(enabled),
            "broker": broker, "clients": "reconnect"}
