"""Bounded, ownership-aware edits of the Codex user MCP registration.

Callers own the installation transaction lock. The final source comparison is
an immediate compare-before-replace guard, not a lock over arbitrary editors.
Source and candidate config bytes can contain unrelated secrets: never log them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping
import uuid

import tomlkit
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.items import InlineTable, Table
from . import product_identity as identity
from .bundle_paths import _validate_descriptor_acl, file_metadata_identity, RuntimePathError

SERVER_NAME = identity.MCP_SERVER_NAME
MAX_CONFIG_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_SUBTREE_BYTES = 32 * 1024
PRIVATE_MODE = 0o600
# Ownership receipts name one client. Claude Desktop entries are only ever added where none existed, so
# their receipts carry no preimage; adopting a foreign entry is a separate, explicit path.
RECEIPT_CLIENTS = ('codex', 'claude-desktop')
ENV_KEYS = frozenset(('TELEGRAM_SEARCH_INSTALLATION_ROOT', 'TELEGRAM_SEARCH_PROFILE',
                      'TELEGRAM_SEARCH_RELEASE_ROOT', 'TELEGRAM_SEARCH_INSTALLATION_ID'))
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_TOOL = re.compile(r'_?[a-z][a-z0-9_]{0,63}\Z')
TEMPORARY_PREFIX = '.telegram-search-'
_TEMPORARY = re.compile(r'\.telegram-search-[0-9a-f]{32}\.tmp\Z')
_SECRET_ARG = re.compile(r'(?i)(api[_-]?hash|api[_-]?id|password|passwd|phone|token|secret|credential|auth[_-]?code|qr)')


class ConfigConflict(RuntimeError):
    """Redacted typed failure; no untrusted input is included in the message."""
    CODES = frozenset(('invalid_registration', 'invalid_config', 'config_too_large',
                       'ownership_conflict', 'concurrent_change', 'unsafe_path',
                       'io_failure', 'invalid_receipt', 'invalid_change'))

    def __init__(self, code: str):
        self.code = code if code in self.CODES else 'invalid_change'
        super().__init__(self.code)


def _digest(source: bytes | None) -> str | None:
    return hashlib.sha256(source).hexdigest() if source is not None else None


def _identifier(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _text(value: object, limit: int = 4096) -> bool:
    try:
        return (type(value) is str and 0 < len(value.encode('utf-8')) <= limit
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


@dataclass(frozen=True, repr=False)
class Registration:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    enabled_tools: tuple[str, ...]
    startup_timeout_sec: int = 30
    tool_timeout_sec: int = 600
    enabled: bool = True
    required: bool = True

    def __post_init__(self):
        isolated = (type(self.command) is str and type(self.args) is tuple and len(self.args)==5 and self.args[:3]==('-I','-S','-B')
                    and self.args[-1]=='mcp' and type(self.args[3]) is str
                    and Path(self.command).name=='python3' and Path(self.command).parent.name=='bin'
                    and Path(self.command).parent.parent.name=='python'
                    and Path(self.args[3])==Path(self.command).parent.parent.parent/'launcher.py'
                    and '..' not in Path(self.command).parts and '..' not in Path(self.args[3]).parts
                    and not self.env)
        if (not _text(self.command) or not Path(self.command).is_absolute()
                or type(self.args) is not tuple or len(self.args) > 32
                or any(not _text(a, 4096) for a in self.args)
                or not isolated
                or not isinstance(self.env, Mapping) or len(self.env) > len(ENV_KEYS)
                or set(self.env) - ENV_KEYS
                or type(self.enabled_tools) is not tuple or not 1 <= len(self.enabled_tools) <= 128
                or any(type(t) is not str or not _TOOL.fullmatch(t) for t in self.enabled_tools)
                or len(set(self.enabled_tools)) != len(self.enabled_tools)
                or type(self.enabled) is not bool or type(self.required) is not bool
                or type(self.startup_timeout_sec) is not int or not 1 <= self.startup_timeout_sec <= 300
                or type(self.tool_timeout_sec) is not int or not 1 <= self.tool_timeout_sec <= 3600):
            raise ConfigConflict('invalid_registration')
        for key, value in self.env.items():
            if not _text(value):
                raise ConfigConflict('invalid_registration')
            if key in ('TELEGRAM_SEARCH_PROFILE', 'TELEGRAM_SEARCH_INSTALLATION_ID'):
                if not _identifier(value):
                    raise ConfigConflict('invalid_registration')
            elif not Path(value).is_absolute() or '..' in Path(value).parts:
                raise ConfigConflict('invalid_registration')
        object.__setattr__(self, 'env', MappingProxyType(dict(self.env)))

    def __repr__(self):
        return 'Registration(<redacted>)'

    def _values(self):
        return dict(command=self.command, args=list(self.args), enabled=self.enabled,
                    required=self.required, startup_timeout_sec=self.startup_timeout_sec,
                    tool_timeout_sec=self.tool_timeout_sec, enabled_tools=list(self.enabled_tools),
                    env=dict(self.env))


def _parse(source: bytes):
    if type(source) is not bytes or len(source) > MAX_CONFIG_BYTES:
        raise ConfigConflict('config_too_large')
    try:
        return tomlkit.parse(source.decode('utf-8'))
    except Exception:
        raise ConfigConflict('invalid_config') from None


def _owned(document):
    servers = document.get('mcp_servers')
    if servers is None:
        return None
    if not isinstance(servers, (Table, InlineTable, OutOfOrderTableProxy)):
        raise ConfigConflict('invalid_config')
    entry = servers.get(SERVER_NAME)
    if entry is not None and not isinstance(entry, (Table, InlineTable, OutOfOrderTableProxy)):
        raise ConfigConflict('invalid_config')
    return entry


def _subtree(entry) -> bytes | None:
    if entry is None:
        return None
    if isinstance(entry, InlineTable):
        # Render before copying: tomlkit deepcopy drops InlineTable's _new
        # formatting flag, which otherwise changes generated comma spacing.
        encoded = ('[mcp_servers]\n' + SERVER_NAME + ' = ' + entry.as_string() + '\n').encode('utf-8')
        if len(encoded) > MAX_SUBTREE_BYTES:
            raise ConfigConflict('invalid_registration')
        return encoded
    document = tomlkit.document()
    servers = tomlkit.table(is_super_table=True)
    servers.add(SERVER_NAME, deepcopy(entry))
    document.add('mcp_servers', servers)
    encoded = tomlkit.dumps(document).encode('utf-8')
    # tomlkit attaches blank separators preceding the next table to this table.
    # Appending an unrelated neighbor must not change the ownership hash. Keep
    # every value/comment byte; remove only trailing whitespace-only lines.
    lines = encoded.splitlines(keepends=True)
    while len(lines) > 1 and not lines[-1].strip(b' \t\r\n'):
        lines.pop()
    encoded = b''.join(lines)
    if len(encoded) > MAX_SUBTREE_BYTES:
        raise ConfigConflict('invalid_registration')
    return encoded


def _entry(desired: Registration, *, inline: bool = False):
    if type(desired) is not Registration:
        raise ConfigConflict('invalid_registration')
    table = tomlkit.inline_table() if inline else tomlkit.table()
    for key, value in desired._values().items():
        if key != 'env' or value:
            table.add(key, value)
    return table


def _validate_preimage(encoded: bytes | None, client: str = 'codex'):
    if encoded is None:
        return
    if client != 'codex' or type(encoded) is not bytes or len(encoded) > MAX_SUBTREE_BYTES:
        raise ConfigConflict('invalid_receipt')
    try:
        document = _parse(encoded)
        entry = _owned(document)
        if entry is None or set(document) != {'mcp_servers'} or set(document['mcp_servers']) != {SERVER_NAME}:
            raise ConfigConflict('invalid_receipt')
        values = entry.unwrap()
        expected = {'command', 'args', 'enabled_tools', 'startup_timeout_sec', 'tool_timeout_sec', 'enabled', 'required'}
        if set(values) - (expected | {'env'}) or expected - set(values):
            raise ConfigConflict('invalid_receipt')
        desired = Registration(command=values['command'], args=tuple(values['args']), env=values.get('env', {}),
                               enabled_tools=tuple(values['enabled_tools']), startup_timeout_sec=values['startup_timeout_sec'],
                               tool_timeout_sec=values['tool_timeout_sec'], enabled=values['enabled'], required=values['required'])
        # Only our generated nonsecret registration may be persisted as preimage.
        if encoded != _subtree(_entry(desired, inline=isinstance(entry, InlineTable))):
            raise ConfigConflict('invalid_receipt')
    except Exception:
        raise ConfigConflict('invalid_receipt') from None


def _json_bytes(data: dict) -> bytes:
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_METADATA_BYTES:
        raise ConfigConflict('invalid_receipt')
    return encoded


def _strict_json(source: bytes) -> dict:
    if type(source) is not bytes or len(source) > MAX_METADATA_BYTES:
        raise ConfigConflict('invalid_receipt')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_):
        raise ValueError()
    try:
        data = json.loads(source, object_pairs_hook=pairs, parse_constant=constant)
        if type(data) is not dict:
            raise ValueError()
        return data
    except Exception:
        raise ConfigConflict('invalid_receipt') from None


@dataclass(frozen=True, repr=False)
class Receipt:
    installation_id: str
    transaction_id: str
    pre_subtree_hash: str | None
    post_subtree_hash: str | None
    pre_subtree: bytes | None = field(repr=False)
    schema_version: int = 1
    client: str = 'codex'
    scope: str = 'user'

    def __post_init__(self):
        if (type(self.schema_version) is not int or self.schema_version != 1
                or type(self.client) is not str or self.client not in RECEIPT_CLIENTS
                or self.scope != 'user' or not _identifier(self.installation_id) or not _identifier(self.transaction_id)
                or any(v is not None and (type(v) is not str or not _SHA.fullmatch(v))
                       for v in (self.pre_subtree_hash, self.post_subtree_hash))
                or self.pre_subtree is not None and type(self.pre_subtree) is not bytes
                or _digest(self.pre_subtree) != self.pre_subtree_hash):
            raise ConfigConflict('invalid_receipt')
        _validate_preimage(self.pre_subtree, self.client)

    def __repr__(self):
        return 'Receipt(<redacted>)'

    def to_bytes(self) -> bytes:
        return _json_bytes(dict(schema_version=self.schema_version, client=self.client, scope=self.scope,
                                installation_id=self.installation_id, transaction_id=self.transaction_id,
                                pre_subtree_hash=self.pre_subtree_hash, post_subtree_hash=self.post_subtree_hash,
                                pre_subtree=self.pre_subtree.decode('utf-8') if self.pre_subtree is not None else None))

    @classmethod
    def from_bytes(cls, source: bytes):
        data = _strict_json(source)
        if set(data) != {'schema_version', 'client', 'scope', 'installation_id', 'transaction_id',
                         'pre_subtree_hash', 'post_subtree_hash', 'pre_subtree'}:
            raise ConfigConflict('invalid_receipt')
        try:
            if data['pre_subtree'] is not None:
                if type(data['pre_subtree']) is not str:
                    raise ValueError()
                data['pre_subtree'] = data['pre_subtree'].encode('utf-8')
            return cls(**data)
        except Exception:
            raise ConfigConflict('invalid_receipt') from None


@dataclass(frozen=True, repr=False)
class ConfigChange:
    source: bytes = field(repr=False)
    candidate: bytes = field(repr=False)
    pre_subtree: bytes | None = field(repr=False)
    post_subtree: bytes | None = field(repr=False)
    ownership: Receipt | None = field(default=None, repr=False)

    @property
    def expected_source_hash(self) -> str:
        return _digest(self.source)

    @property
    def pre_subtree_hash(self) -> str | None:
        return _digest(self.pre_subtree)

    @property
    def post_subtree_hash(self) -> str | None:
        return _digest(self.post_subtree)

    @property
    def is_noop(self) -> bool:
        return self.source == self.candidate

    def __repr__(self):
        return 'ConfigChange(<redacted>)'


def _check_ownership(current: bytes | None, ownership: Receipt | None):
    if ownership is not None and (type(ownership) is not Receipt or ownership.client != 'codex'):
        raise ConfigConflict('ownership_conflict')
    if (current is not None and ownership is None
            or ownership is not None and _digest(current) != ownership.post_subtree_hash):
        raise ConfigConflict('ownership_conflict')


def merge_registration(source: bytes, desired: Registration, ownership: Receipt | None = None) -> ConfigChange:
    document = _parse(source)
    current = _subtree(_owned(document))
    _check_ownership(current, ownership)
    inline = isinstance(document.get('mcp_servers'), InlineTable)
    replacement = _entry(desired, inline=inline)
    wanted = _subtree(replacement)
    if current == wanted:
        return ConfigChange(source, source, current, current, ownership)
    if current is None and not inline:
        # Append the new child rather than asking a composite parent proxy to
        # choose an insertion site among interleaved unrelated sections.
        separator = b'\n' if not source or source.endswith(b'\n') else b'\n\n'
        candidate = source + separator + wanted
    else:
        document['mcp_servers'][SERVER_NAME] = replacement
        candidate = tomlkit.dumps(document).encode('utf-8')
    after = _subtree(_owned(_parse(candidate)))
    return ConfigChange(source, candidate, current, after, ownership)


def rollback_registration(source: bytes, receipt: Receipt) -> ConfigChange:
    if type(receipt) is not Receipt:
        raise ConfigConflict('ownership_conflict')
    document = _parse(source)
    current = _subtree(_owned(document))
    _check_ownership(current, receipt)
    if receipt.pre_subtree is None:
        if current is not None:
            del document['mcp_servers'][SERVER_NAME]
            if (not document['mcp_servers'] and isinstance(document['mcp_servers'], Table)
                    and document['mcp_servers'].is_super_table()):
                del document['mcp_servers']
    else:
        if 'mcp_servers' not in document:
            document.add('mcp_servers', tomlkit.table(is_super_table=True))
        document['mcp_servers'][SERVER_NAME] = deepcopy(_owned(_parse(receipt.pre_subtree)))
    candidate = tomlkit.dumps(document).encode('utf-8')
    after = _subtree(_owned(_parse(candidate)))
    return ConfigChange(source, candidate, current, after, receipt)


def _open_parent(path: Path) -> int:
    """Walk every component through held dirfds, rejecting symlink ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name in ('', '.', '..'):
        raise ConfigConflict('unsafe_path')
    fd = None
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _validate_descriptor_acl(fd)
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            metadata = os.fstat(fd)
            if metadata.st_uid not in (0, os.getuid()) or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ConfigConflict('unsafe_path')
            _validate_descriptor_acl(fd)
        if os.fstat(fd).st_uid != os.getuid():
            raise ConfigConflict('unsafe_path')
        return fd
    except (OSError, ValueError, RuntimePathError):
        if fd is not None:
            os.close(fd)
        raise ConfigConflict('unsafe_path') from None
    except ConfigConflict:
        if fd is not None:
            os.close(fd)
        raise


def _read_at(directory_fd: int, name: str, maximum: int, max_mode: int = PRIVATE_MODE) -> tuple[bytes, bool]:
    """Owner-owned single-link regular file: owner read/write, other bits only within `max_mode`.

    The default admits exactly 0600. Claude Desktop (Electron) writes 0644, so its caller passes
    0o644: never group or world writable, never executable or set-id.
    """
    return _read_at_mode(directory_fd, name, maximum, max_mode)[:2]


def _read_at_mode(directory_fd: int, name: str, maximum: int, max_mode: int = PRIVATE_MODE) -> tuple[bytes, bool, int | None]:
    """`_read_at` plus the admitted file mode (None when absent)."""
    if max_mode not in (PRIVATE_MODE, 0o644):
        raise ConfigConflict('unsafe_path')
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        metadata = os.fstat(fd)
        _validate_descriptor_acl(fd)
        mode = stat.S_IMODE(metadata.st_mode)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or mode & PRIVATE_MODE != PRIVATE_MODE or mode & ~max_mode or metadata.st_nlink != 1):
            raise ConfigConflict('unsafe_path')
        if metadata.st_size > maximum:
            raise ConfigConflict('config_too_large')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            source = stream.read(maximum + 1)
            if len(source) != metadata.st_size or file_metadata_identity(metadata) != file_metadata_identity(os.fstat(stream.fileno())):
                raise ConfigConflict('concurrent_change')
        return source, True, mode
    except FileNotFoundError:
        return b'', False, None
    except (OSError, RuntimePathError):
        raise ConfigConflict('unsafe_path') from None
    finally:
        if fd is not None:
            os.close(fd)


def read_config(config_path: Path) -> bytes:
    fd = _open_parent(config_path)
    try:
        return _read_at(fd, Path(config_path).name, MAX_CONFIG_BYTES)[0]
    finally:
        os.close(fd)


def _atomic_write(path: Path, candidate: bytes, maximum: int, expected: bytes | None = None,
                  require_existing: bool | None = None, before_replace=None, max_mode: int = PRIVATE_MODE,
                  preserve_mode: bool = False):
    """Owner-safe atomic replacement, optionally guarding exact source bytes.

    `max_mode` only widens which existing file modes are read (see `_read_at`). The replacement is
    0600, or with `preserve_mode` the admitted mode of the file it replaces (a new file is 0600).
    """
    if type(candidate) is not bytes or len(candidate) > maximum:
        raise ConfigConflict('invalid_change')
    directory_fd = _open_parent(path)
    name = Path(path).name
    temporary = TEMPORARY_PREFIX + uuid.uuid4().hex + '.tmp'
    temporary_created = False
    try:
        original, existed, mode = _read_at_mode(directory_fd, name, maximum, max_mode)
        if expected is not None and original != expected:
            raise ConfigConflict('concurrent_change')
        if require_existing is not None and existed != require_existing:
            raise ConfigConflict('concurrent_change')
        mode = mode if preserve_mode and existed else PRIVATE_MODE
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        temporary_created = True
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
            publication = os.fstat(stream.fileno())
        if before_replace is not None:
            before_replace({'dev':publication.st_dev,'ino':publication.st_ino,
                            'uid':publication.st_uid,'mode':stat.S_IMODE(publication.st_mode),
                            'bytes':publication.st_size,'temporary':temporary})
        # Compare using the same held directory immediately before replacement.
        now, exists_now = _read_at(directory_fd, name, maximum, max_mode)
        if now != original or exists_now != existed:
            raise ConfigConflict('concurrent_change')
        parent_now = _open_parent(path)
        try:
            a, b = os.fstat(directory_fd), os.fstat(parent_now)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise ConfigConflict('concurrent_change')
        finally:
            os.close(parent_now)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    except OSError:
        raise ConfigConflict('io_failure') from None
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def discard_temporary(path: Path, record: object) -> bool:
    """Unlink a journaled temporary left by a killed `_atomic_write`, only while it is that exact inode.

    `record` is the journaled `{'object': binding, 'temporary': name}`; True when it was removed.
    """
    if (type(record) is not dict or type(record.get('temporary')) is not str
            or _TEMPORARY.fullmatch(record['temporary']) is None or type(record.get('object')) is not dict):
        return False
    binding = record['object']
    directory_fd = _open_parent(path)
    try:
        try:
            metadata = os.stat(record['temporary'], dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        observed = {'kind': 'file', 'dev': metadata.st_dev, 'ino': metadata.st_ino, 'uid': metadata.st_uid,
                    'mode': stat.S_IMODE(metadata.st_mode), 'bytes': metadata.st_size, 'nlink': metadata.st_nlink}
        if not stat.S_ISREG(metadata.st_mode) or observed != binding:
            return False
        os.unlink(record['temporary'], dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    except OSError:
        raise ConfigConflict('io_failure') from None
    finally:
        os.close(directory_fd)


def adopt_registration(source: bytes, desired: Registration) -> ConfigChange:
    """`--adopt-legacy` only: replace a foreign entry the caller has recorded and backed up.

    The change's `pre_subtree` is the foreign subtree (never persisted in a Receipt); its ownership
    receipt carries no preimage, so a later rollback removes ours instead of restoring theirs.
    """
    document = _parse(source)
    current = _subtree(_owned(document))
    if current is None:
        raise ConfigConflict('ownership_conflict')
    replacement = _entry(desired, inline=isinstance(document.get('mcp_servers'), InlineTable))
    document['mcp_servers'][SERVER_NAME] = replacement
    candidate = tomlkit.dumps(document).encode('utf-8')
    after = _subtree(_owned(_parse(candidate)))
    if after != _subtree(replacement) or _neighbors(source) != _neighbors(candidate):
        raise ConfigConflict('invalid_change')
    return ConfigChange(source, candidate, current, after, None)


def owned_subtree(source: bytes) -> bytes | None:
    """The exact bytes of the current `telegram_search` subtree (for an adoption backup), or None."""
    return _subtree(_owned(_parse(source)))


def _neighbors(source: bytes) -> bytes:
    document = _parse(source)
    if _owned(document) is not None:
        del document['mcp_servers'][SERVER_NAME]
    if ('mcp_servers' in document and not document['mcp_servers']
            and isinstance(document['mcp_servers'], Table) and document['mcp_servers'].is_super_table()):
        del document['mcp_servers']
    # tomlkit adds separating newlines when appending a table. They are the
    # only permitted difference outside the target; values/comments stay exact.
    return tomlkit.dumps(document).encode('utf-8').rstrip(b'\r\n')


def commit_transaction(change: ConfigChange, expected_hash: str, config_path: Path, *,
                       installation_id: str, transaction_id: str) -> Receipt:
    if (type(change) is not ConfigChange or not _identifier(installation_id) or not _identifier(transaction_id)
            or type(expected_hash) is not str or expected_hash != change.expected_source_hash):
        raise ConfigConflict('invalid_change')
    if change.ownership is not None and change.ownership.installation_id != installation_id:
        raise ConfigConflict('ownership_conflict')
    if (_subtree(_owned(_parse(change.source))) != change.pre_subtree
            or _subtree(_owned(_parse(change.candidate))) != change.post_subtree
            or _neighbors(change.source) != _neighbors(change.candidate)):
        raise ConfigConflict('invalid_change')
    _check_ownership(change.pre_subtree, change.ownership)
    _validate_preimage(change.pre_subtree)
    _validate_preimage(change.post_subtree)
    if change.is_noop and change.ownership is not None:
        # This operation uses the existing receipt, not a new rollback snapshot.
        # Revalidate its bounded representation and compare the actual source.
        change.ownership.to_bytes()
        if read_config(config_path) != change.source:
            raise ConfigConflict('concurrent_change')
        return change.ownership
    # Changed operations must validate the actual new receipt before mutation.
    receipt = Receipt(installation_id, transaction_id, change.pre_subtree_hash,
                      change.post_subtree_hash, change.pre_subtree)
    # JSON escaping can expand a bounded TOML preimage; check the actual
    # receipt representation before replacing the config.
    receipt.to_bytes()
    if change.is_noop:
        if read_config(config_path) != change.source:
            raise ConfigConflict('concurrent_change')
        return change.ownership if change.ownership is not None else receipt
    _atomic_write(config_path, change.candidate, MAX_CONFIG_BYTES, expected=change.source)
    return receipt
