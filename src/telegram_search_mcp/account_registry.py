"""The local alias registry (0.55 spec 2): which named accounts exist, and which Telegram user each one is.

`TelegramSearchMCP-accounts/registry.json`, owner-only, with a closed schema:
`{"version": 1, "aliases": {"<alias>": {"user_id": int|null}}, "default_user_id": int|null}`, at most eight
aliases. It exists only after the first alias is added; without it every path behaves as in 0.54 and nothing
here writes. Writes are atomic and owner-only, under the registry's own lease.

Every refusal is a fixed code from CODES. An alias, a path or a user id is never echoed, and the user id is
never printed, logged or returned: only the pin check compares it.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import (ALIAS_GRAMMAR, DEFAULT_ALIAS, ConfigurationError, accounts_root, alias_server_name,
                     alias_socket_path)
from .session_owner import OwnerLeaseError, open_private_directory

REGISTRY_NAME = "registry.json"
LEASE_NAME = "registry.lock"
MAX_ALIASES = 8
MAX_REGISTRY_BYTES = 4096
# macOS sun_path holds 104 bytes with the terminating NUL; a longer socket path is refused, never truncated.
SOCKET_PATH_LIMIT = 103
# Clients name a tool `mcp__<server>__<tool>` and refuse names longer than this.
TOOL_NAME_LIMIT = 64
USER_ID_BOUND = 2**53
LEASE_ATTEMPTS, LEASE_INTERVAL = 100, 0.02
CODES = frozenset({
    "registry_invalid",     # the file is not the closed schema
    "registry_unsafe",      # the file or its directory is not owner-only, regular and unlinked
    "registry_busy",        # another writer holds the lease past the bounded wait
    "registry_full",        # an add beyond MAX_ALIASES
    "alias_invalid",        # outside the grammar, or the reserved default
    "alias_exists",
    "alias_unknown",
    "alias_path_too_long",  # the alias's socket path exceeds SOCKET_PATH_LIMIT
    "alias_name_too_long",  # an alias tool name exceeds TOOL_NAME_LIMIT
    "alias_unavailable",    # a pin for an alias the registry no longer lists
    "account_mismatch",     # the authorized user is not the alias's pinned user
    "account_duplicate",    # the authorized user is pinned under another alias, the default included
})
_KEYS = frozenset({"version", "aliases", "default_user_id"})


class RegistryError(ConfigurationError):
    """A fixed refusal code; nothing the caller supplied is part of it."""

    def __init__(self, code: str) -> None:
        self.code = code if code in CODES else "registry_invalid"
        super().__init__(self.code)


def _named(alias: object) -> bool:
    return type(alias) is str and alias != DEFAULT_ALIAS and ALIAS_GRAMMAR.fullmatch(alias) is not None


def _pin(value: object) -> bool:
    return value is None or (type(value) is int and 0 < value < USER_ID_BOUND)


@dataclass(frozen=True)
class Registry:
    """A parsed registry. The pins never appear in its repr."""
    entries: tuple[tuple[str, int | None], ...] = field(repr=False)
    default_user_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        names = [alias for alias, _ in self.entries]
        pins = [pin for _, pin in self.entries if pin is not None]
        if self.default_user_id is not None:
            pins.append(self.default_user_id)
        if (type(self.entries) is not tuple or len(self.entries) > MAX_ALIASES
                or any(type(entry) is not tuple or len(entry) != 2 for entry in self.entries)
                or not all(_named(alias) for alias in names) or names != sorted(set(names))
                or not all(_pin(pin) for _, pin in self.entries) or not _pin(self.default_user_id)
                or len(pins) != len(set(pins))):
            raise RegistryError("registry_invalid")

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(alias for alias, _ in self.entries)

    def _replace(self, entries: dict[str, int | None], default_user_id: int | None) -> Registry:
        return Registry(tuple(sorted(entries.items())), default_user_id)

    def with_alias(self, alias: str) -> Registry:
        return self._replace({**dict(self.entries), alias: None}, self.default_user_id)

    def without_alias(self, alias: str) -> Registry:
        return self._replace({name: pin for name, pin in self.entries if name != alias}, self.default_user_id)

    def with_pin(self, alias: str, user_id: int) -> Registry:
        if alias == DEFAULT_ALIAS:
            return self._replace(dict(self.entries), user_id)
        return self._replace({**dict(self.entries), alias: user_id}, self.default_user_id)

    def needs_pin(self, alias: str, user_id: int) -> bool:
        """Whether `alias` still needs its pin; refuses a mismatch, a duplicate or an unlisted alias."""
        if alias == DEFAULT_ALIAS:
            own = self.default_user_id
        elif alias in self.aliases:
            own = dict(self.entries)[alias]
        else:
            raise RegistryError("alias_unavailable")
        if own is not None and own != user_id:
            raise RegistryError("account_mismatch")
        others = [pin for name, pin in self.entries if name != alias]
        if alias != DEFAULT_ALIAS:
            others.append(self.default_user_id)
        if user_id in others:
            raise RegistryError("account_duplicate")
        return own is None

    def encode(self) -> bytes:
        value = {"version": 1, "aliases": {alias: {"user_id": pin} for alias, pin in self.entries},
                 "default_user_id": self.default_user_id}
        return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("ascii")


EMPTY = Registry(())


def parse(data: bytes) -> Registry:
    """The closed parser: exactly the schema, or `registry_invalid`."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def refuse(_):
        raise ValueError

    if type(data) is not bytes or len(data) > MAX_REGISTRY_BYTES:
        raise RegistryError("registry_invalid")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=refuse, parse_float=refuse)
    except (ValueError, UnicodeError, RecursionError):
        raise RegistryError("registry_invalid") from None
    if (type(value) is not dict or set(value) != _KEYS or type(value["version"]) is not int
            or value["version"] != 1 or type(value["aliases"]) is not dict
            or len(value["aliases"]) > MAX_ALIASES or not _pin(value["default_user_id"])):
        raise RegistryError("registry_invalid")
    entries = []
    for alias, entry in value["aliases"].items():
        if not _named(alias) or type(entry) is not dict or set(entry) != {"user_id"} or not _pin(entry["user_id"]):
            raise RegistryError("registry_invalid")
        entries.append((alias, entry["user_id"]))
    return Registry(tuple(sorted(entries)), value["default_user_id"])


# Locations and I/O -------------------------------------------------------------------------------------------

def present(root: Path) -> bool:
    """False only when the registry is certainly absent; anything else is for `load` to judge."""
    try:
        os.lstat(Path(root) / REGISTRY_NAME)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _open_root(root: Path, *, create: bool = False) -> int:
    try:
        return open_private_directory(Path(root), create=create)
    except OwnerLeaseError:
        raise RegistryError("registry_unsafe") from None


def load(root: Path) -> Registry | None:
    """The registry in `root`, or None when there is none (0.54). Never creates anything."""
    try:
        os.lstat(root)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise RegistryError("registry_unsafe") from None
    directory = _open_root(root)
    try:
        return _load_at(directory)
    finally:
        os.close(directory)


def _load_at(directory: int) -> Registry | None:
    try:
        descriptor = os.open(REGISTRY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                             dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise RegistryError("registry_unsafe") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RegistryError("registry_unsafe")
        if metadata.st_size > MAX_REGISTRY_BYTES:
            raise RegistryError("registry_invalid")
        chunks, total = [], 0
        for _ in range(MAX_REGISTRY_BYTES + 2):
            chunk = os.read(descriptor, MAX_REGISTRY_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_REGISTRY_BYTES:
                break
        data = b"".join(chunks)
        if len(data) != metadata.st_size:
            raise RegistryError("registry_unsafe")
    except OSError:
        raise RegistryError("registry_unsafe") from None
    finally:
        os.close(descriptor)
    return parse(data)


@contextmanager
def _lease(directory: int, sleeper: Callable[[float], None]) -> Iterator[None]:
    """The registry's exclusive writer lease: bounded waiting, then `registry_busy`."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            descriptor = os.open(LEASE_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            descriptor = os.open(LEASE_NAME, flags, dir_fd=directory)
    except OSError:
        raise RegistryError("registry_unsafe") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RegistryError("registry_unsafe")
        for attempt in range(LEASE_ATTEMPTS):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise RegistryError("registry_unsafe") from None
                if attempt == LEASE_ATTEMPTS - 1:
                    raise RegistryError("registry_busy") from None
                sleeper(LEASE_INTERVAL)
        visible = os.stat(LEASE_NAME, dir_fd=directory, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RegistryError("registry_unsafe")
        yield
    except OSError:
        raise RegistryError("registry_unsafe") from None
    finally:
        os.close(descriptor)


def _write_at(directory: int, registry: Registry) -> None:
    """Atomic owner-only replacement: an exclusive temporary, fsync, rename, directory fsync."""
    data = registry.encode()
    if len(data) > MAX_REGISTRY_BYTES:
        raise RegistryError("registry_full")
    temporary = f".{REGISTRY_NAME}.{secrets.token_hex(16)}.tmp"
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=directory)
        created = True
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            for _ in range(len(data) + 1):
                if not view:
                    break
                view = view[os.write(descriptor, view):]
            if view:
                raise RegistryError("registry_unsafe")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(temporary, REGISTRY_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
    except OSError:
        raise RegistryError("registry_unsafe") from None
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass


def _change(root: Path, change: Callable[[Registry | None], Registry | None], *, create: bool,
            sleeper: Callable[[float], None]) -> None:
    """Read, decide and write under one lease; `change` returning None writes nothing."""
    directory = _open_root(root, create=create)
    try:
        with _lease(directory, sleeper):
            updated = change(_load_at(directory))
            if updated is not None:
                _write_at(directory, updated)
    finally:
        os.close(directory)


# Aliases -----------------------------------------------------------------------------------------------------

def longest_tool_name() -> str:
    """The longest MCP tool name this release registers (the finalized SDK registration, no I/O)."""
    from .config import RuntimePolicy
    from .server import build_server
    return max((tool.name for tool in build_server(policy=RuntimePolicy())._tool_manager.list_tools()), key=len)


def check_budgets(alias: str, home: Path, *, longest_tool: str | None = None) -> None:
    """The socket-path and tool-name budgets of a named alias under `home`; nothing is created."""
    if not _named(alias):
        raise RegistryError("alias_invalid")
    if len(os.fsencode(str(alias_socket_path(alias, Path(home))))) > SOCKET_PATH_LIMIT:
        raise RegistryError("alias_path_too_long")
    tool = longest_tool_name() if longest_tool is None else longest_tool
    if len(f"mcp__{alias_server_name(alias)}__{tool}".encode("utf-8")) > TOOL_NAME_LIMIT:
        raise RegistryError("alias_name_too_long")


def admits(alias: str, home: Path) -> bool:
    """Whether the registry under `home` lists `alias` within its socket budget; reads, never creates."""
    if not _named(alias):
        return False
    registry = load(accounts_root(Path(home)))
    if registry is None or alias not in registry.aliases:
        return False
    if len(os.fsencode(str(alias_socket_path(alias, Path(home))))) > SOCKET_PATH_LIMIT:
        raise RegistryError("alias_path_too_long")
    return True


def add_alias(alias: str, home: Path, *, longest_tool: str | None = None,
              sleeper: Callable[[float], None] = time.sleep) -> None:
    """Register a named alias, unpinned. The first add creates the owner-only accounts root and the registry.

    The grammar and both budgets are checked before any directory is created.
    """
    check_budgets(alias, home, longest_tool=longest_tool)

    def change(current):
        current = EMPTY if current is None else current
        if alias in current.aliases:
            raise RegistryError("alias_exists")
        if len(current.aliases) >= MAX_ALIASES:
            raise RegistryError("registry_full")
        return current.with_alias(alias)

    _change(accounts_root(Path(home)), change, create=True, sleeper=sleeper)


def remove_alias(alias: str, home: Path, *, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Drop a named alias and its pin from the registry; the registry itself stays."""
    if not _named(alias):
        raise RegistryError("alias_invalid")
    root = accounts_root(Path(home))
    if load(root) is None:
        raise RegistryError("alias_unknown")

    def change(current):
        if current is None or alias not in current.aliases:
            raise RegistryError("alias_unknown")
        return current.without_alias(alias)

    _change(root, change, create=False, sleeper=sleeper)


def pin_account(root: Path, alias: str, user_id: int, *, sleeper: Callable[[float], None] = time.sleep) -> None:
    """The account pin, for the broker of `alias` whose session is authorized as `user_id`.

    Without a registry nothing happens (0.54). Otherwise the first authorized user is pinned (the default
    alias's in `default_user_id`); a different user later refuses with `account_mismatch`, and a user pinned
    under another alias, the default included, refuses with `account_duplicate`.
    """
    if alias != DEFAULT_ALIAS and not _named(alias):
        raise RegistryError("alias_unavailable")
    if type(user_id) is not int or not 0 < user_id < USER_ID_BOUND:
        raise RegistryError("account_mismatch")
    registry = load(root)
    if registry is None or not registry.needs_pin(alias, user_id):
        return

    def change(current):
        if current is None or not current.needs_pin(alias, user_id):
            return None
        return current.with_pin(alias, user_id)

    _change(root, change, create=False, sleeper=sleeper)
