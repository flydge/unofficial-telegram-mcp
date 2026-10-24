"""The owner's capability profiles: show, enable or disable one named profile in the installed runtime.toml.

Only the `enabled_capabilities` line changes; every other byte stays, comments included (a policy the loader
refuses, such as one with unknown keys, refuses here too). The new file is validated as a policy before it
replaces the old one (owner-only temporary, fsync, compare-and-swap rename), and only after two checks: the
receipt-bound broker LaunchAgent is not loaded or is exactly the installed release's definition (else
`service_conflict`), and the running broker has no active client context (else `pending_send_unresolved`).
Every refusal comes before the write. After it, the broker is restarted when loaded; a restart that fails then
is reported in the `changed` result (`broker: restart_failed` with the manual `restart_command`), never as a
refusal. Connected MCP clients see `config_stale` until they reconnect. Refusals are fixed codes; nothing read
from the file is echoed. With `--account <alias>` (0.55 spec 3D) the same switch edits that alias's own
runtime.toml, gated and restarted through that alias's broker and label; the default's is untouched.
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
CODES = frozenset({"policy_unsupported", "policy_conflict", "pending_send_unresolved", "service_conflict"})
MAX_POLICY_BYTES = 65536
LAUNCHCTL = "/bin/launchctl"  # launch_agent.LAUNCHCTL, without importing the broker
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


def without_capabilities(data: bytes) -> bytes | None:
    """`data` with its one `enabled_capabilities` line (as `rewrite` finds it) replaced by a fixed marker; None
    unless there is exactly one. The installer records and compares policies by it, so a profile switch, the
    only change to that line, never blocks a rollback."""
    lines = list(_LINE.finditer(data))
    if len(lines) != 1:
        return None
    return data[:lines[0].start()] + b"enabled_capabilities = <profile switch>" + data[lines[0].end():]


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


def _policy_path(home: Path, account=None) -> Path:
    """The runtime.toml the switch edits: the default account's, or a named alias's own (0.55 spec 3D)."""
    return _owner_paths(Path(home))[0] / "runtime.toml" if account is None else account.policy


def show(home: Path, account=None) -> dict:
    """The profiles of the default account's runtime.toml, or of `account`'s (a named alias's AccountPaths)."""
    policy = _policy(_policy_path(home, account))
    return {"status": "ok", "profiles": states(policy.enabled_capabilities),
            "enabled_capabilities": list(policy.enabled_capabilities)}


def restart_command(home: Path, account=None) -> list[str]:
    """The owner's manual restart of the broker LaunchAgent (nonsecret argv): the alias's own label for `account`."""
    label = _owner_paths(Path(home))[2] if account is None else account.broker_label
    return [LAUNCHCTL, "kickstart", "-k", "gui/%d/%s" % (os.getuid(), label)]


def apply(home: Path, action: str, profile: str, *, pending: Callable[[], str], restartable: Callable[[], str],
          restart: Callable[[], str], account=None) -> dict:
    """Enable or disable one profile. Before the write, `restartable()` answers 'loaded' or 'not_running' only when
    the receipt-bound broker can be restarted (it raises, or answers anything else, otherwise) and `pending()`
    answers 'none', 'pending' or 'unknown' for the running broker. After the write, `restart()` restarts the broker
    when it is loaded and answers 'restarted' or 'not_running'; any other answer or exception there is reported as
    `broker: restart_failed` with the manual `restart_command`. `account` is a named alias's AccountPaths: its own
    runtime.toml (the callables gate and restart its own broker); None is the default's, unchanged."""
    path = _policy_path(home, account)
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
        if restartable() not in ("loaded", "not_running"):
            raise ProfileError("service_conflict")
        if pending() != "none":
            raise ProfileError("pending_send_unresolved")
        if _read(path) != before:
            raise ProfileError("policy_conflict")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    # Written: from here on the result is `changed`, whatever the directory sync or the restart does.
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass
    try:
        broker = restart()
    except (Exception, KeyboardInterrupt):
        broker = None
    result = {"status": "changed", "profiles": states(enabled), "enabled_capabilities": list(enabled),
              "broker": broker if broker in ("restarted", "not_running") else "restart_failed", "clients": "reconnect"}
    if result["broker"] == "restart_failed":
        result["restart_command"] = restart_command(home, account)
    return result
