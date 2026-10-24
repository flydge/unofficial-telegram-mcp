"""Claude Desktop MCP registration: one owned `mcpServers` entry in `claude_desktop_config.json`.

The config lives in `<home>/Library/Application Support/Claude`; when that directory does not exist,
Claude Desktop is not evidently present and nothing is read or written. Reads are bounded (1 MiB),
no-follow, ACL-checked descriptor reads of an owner-owned single-link file that is not group or world
writable (Electron writes 0644), parsed as strict JSON. A first install only ever adds an absent entry;
an upgrade or rollback replaces only the entry the installation owns, an uninstall removes only that entry,
and `--adopt-legacy` replaces a foreign entry only after its exact backup is recorded. Each write is a
compare-before-replace atomic replacement that keeps the file's mode; the caller journals the temporary's
name before it is created and its inode before the replace. Every neighbouring value keeps its meaning and every number its exact
lexeme, otherwise nothing is written (`unsupported`). Config bytes can hold other servers' secrets: they
are never logged, echoed or raised; only hashes leave this module, except an adopted entry's own backup.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

from .bundle_paths import RuntimePathError, require_object_binding
from .claude_registration import _strict_json
from .codex_registration import ConfigConflict, Receipt, _atomic_write, _open_parent, _read_at, _name

CONFIG_NAME = 'claude_desktop_config.json'
MAX_CONFIG_BYTES = 1024 * 1024
SHARED_MODE = 0o644
# What the file says about our entry. `unsupported`: absent, but adding it would change a neighbour.
STATES = ('absent', 'exact', 'different', 'unavailable', 'unsupported', 'skipped')
_ENTRY_KEYS = frozenset(('command', 'args', 'env'))
_MAX_DEPTH = 64


def directory(home):
    return Path(home) / 'Library' / 'Application Support' / 'Claude'


def config_path(home):
    return directory(home) / CONFIG_NAME


def entry(command, args):
    return {'command': command, 'args': list(args)}


def manual_entry(command, args, name=None):
    """Nonsecret JSON the user can merge into the config themselves. `name` is a named alias's server name
    (AccountPaths.server_name), an entry beside the default's (0.55 spec 3D); None is the default's."""
    return {'mcpServers': {_name(name): entry(command, args)}}


def subtree(command, args, name=None):
    """Canonical bytes of the owned subtree; its sha256 is the receipt's post-subtree hash."""
    return json.dumps(manual_entry(command, args, name), sort_keys=True, separators=(',', ':')).encode()


def receipt(installation_id, transaction_id, command, args, name=None):
    """Ownership of an entry added where none existed (no preimage)."""
    return Receipt(installation_id, transaction_id, None, hashlib.sha256(subtree(command, args, name)).hexdigest(),
                   None, client='claude-desktop')


class _Unsupported(Exception):
    """A value whose re-encoding would differ; carries nothing from the config."""


def _shallow(document):
    """At most _MAX_DEPTH levels of objects and arrays; one pass over the (bounded) parsed values."""
    pending = [(document, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > _MAX_DEPTH:
            return False
        if type(value) is dict:
            pending.extend((item, depth + 1) for item in value.values())
        elif type(value) is list:
            pending.extend((item, depth + 1) for item in value)
    return True


def _lexeme(text, value):
    if json.dumps(value) != text:
        raise _Unsupported()
    return value


def _document(source):
    """(strict JSON object with an object or no `mcpServers`, whether every number keeps its lexeme).

    Raises ValueError, TypeError, RecursionError or UnicodeError for anything else (unavailable).
    """
    text = source.decode('utf-8')
    document = _strict_json(text)
    if type(document.get('mcpServers', {})) is not dict or not _shallow(document):
        raise ValueError('shape')
    try:
        # A second pass over the same text: every number must re-encode to its exact lexeme.
        json.loads(text, parse_int=lambda s: _lexeme(s, int(s)), parse_float=lambda s: _lexeme(s, float(s)))
    except _Unsupported:
        return document, False
    return document, True


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _encode(document):
    """Claude Desktop's own layout (two-space indent); raises _Unsupported when not encodable."""
    try:
        data = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    except (ValueError, UnicodeError, RecursionError):
        raise _Unsupported() from None
    if len(data) > MAX_CONFIG_BYTES:
        raise _Unsupported()
    return data


def _exact(value, command, args):
    return (type(value) is dict and set(value) <= _ENTRY_KEYS and {'command', 'args'} <= set(value)
            and type(value['command']) is str and value['command'] == command
            and type(value['args']) is list and value['args'] == list(args)
            and ('env' not in value or type(value['env']) is dict and not value['env']))


@dataclass(frozen=True, repr=False)
class Change:
    """One planned edit of our entry: exact source bytes, the candidate and whether the file existed."""
    source: bytes = field(repr=False)
    candidate: bytes = field(repr=False)
    existed: bool

    @property
    def source_sha256(self):
        return hashlib.sha256(self.source).hexdigest()

    @property
    def candidate_sha256(self):
        return hashlib.sha256(self.candidate).hexdigest()

    def __repr__(self):
        return 'Change(<redacted>)'


def _plan(document, source, existed, command, args, name):
    """Our entry `name` set to (command, args) in place, or removed when `command` is None."""
    servers = document.get('mcpServers')
    neighbours = {k: v for k, v in (servers or {}).items() if k != name}
    changed = dict(document)  # Order kept: a new `mcpServers` goes last, an existing one stays in place.
    changed['mcpServers'] = neighbours if command is None else {**(servers or {}), name: entry(command, args)}
    candidate = _encode(changed)
    # Re-read our own bytes: our entry (or none) plus exactly the original neighbours, in their order.
    after, faithful = _document(candidate)
    mine = after['mcpServers'].pop(name, None)
    expected = dict(document)
    if servers is not None:
        expected['mcpServers'] = neighbours
    elif not after['mcpServers']:
        del after['mcpServers']
    if (not faithful or (mine is not None if command is None else not _exact(mine, command, args))
            or _canonical(after) != _canonical(expected)):
        raise _Unsupported()
    return Change(source, candidate, existed)


def backup_sha256(value):
    """Hash of a foreign entry's canonical backup bytes (see `entry_backup`)."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def read(home):
    """(bytes, existed) of the config; raises ConfigConflict on any unsafe or oversized file."""
    fd = _open_parent(config_path(home))
    try:
        return _read_at(fd, CONFIG_NAME, MAX_CONFIG_BYTES, SHARED_MODE)
    finally:
        os.close(fd)


def observe(home, command, args, name=None):
    """(state, Change or None) of the entry `name` (None: the default's). A file read only; a Change only for
    `absent`."""
    name = _name(name)
    if not os.path.lexists(directory(home)):
        return 'skipped', None
    try:
        source, existed = read(home)
        document, faithful = _document(source) if existed else ({}, True)
    except (ConfigConflict, OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return 'unavailable', None
    servers = document.get('mcpServers', {})
    if name in servers:
        return ('exact' if _exact(servers[name], command, args) else 'different'), None
    try:
        if not faithful:
            raise _Unsupported()  # A neighbour's lexeme matters only when we would write.
        return 'absent', _plan(document, source, existed, command, args, name)
    except _Unsupported:
        return 'unsupported', None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return 'unavailable', None


def state(home, command, args, name=None):
    return observe(home, command, args, name)[0]


def _current(home):
    """(document, faithful, source, existed) or a terminal state string."""
    if not os.path.lexists(directory(home)):
        return 'skipped'
    try:
        source, existed = read(home)
        document, faithful = _document(source) if existed else ({}, True)
    except (ConfigConflict, OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return 'unavailable'
    return document, faithful, source, existed


def observe_change(home, owned, command, args, *, adopted=None, name=None):
    """Upgrade/rollback/adoption read: (state, Change or None) of the entry `name` (None: the default's; only the
    default's is ever adopted). A file read only.

    `owned` is the (command, args) our receipt owns, or None. States: `exact` (already the target),
    `owned` (exactly the owned entry: Change replaces it), `adopted` (a foreign entry whose canonical
    backup hash is `adopted`: Change replaces it), `absent` (Change adds it), `different`,
    `unsupported`, `unavailable`, `skipped`.
    """
    name = _name(name)
    if adopted is not None and name != _name():
        raise ConfigConflict('invalid_registration')
    current = _current(home)
    if type(current) is str:
        return current, None
    document, faithful, source, existed = current
    servers = document.get('mcpServers', {})
    if name in servers:
        value = servers[name]
        if _exact(value, command, args):
            return 'exact', None
        if owned is not None and _exact(value, *owned):
            label = 'owned'
        elif adopted is not None and backup_sha256(value) == adopted:
            label = 'adopted'
        else:
            return 'different', None
    else:
        label = 'absent'
    try:
        if not faithful:
            raise _Unsupported()
        return label, _plan(document, source, existed, command, args, name)
    except _Unsupported:
        return 'unsupported', None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return 'unavailable', None


def observe_removal(home, command, args, name=None):
    """Uninstall read of the entry `name` (None: the default's): `exact` with a Change removing only that entry,
    else absent/different/... and None."""
    name = _name(name)
    current = _current(home)
    if type(current) is str:
        return current, None
    document, faithful, source, existed = current
    servers = document.get('mcpServers', {})
    if name not in servers:
        return 'absent', None
    if not _exact(servers[name], command, args):
        return 'different', None
    try:
        if not faithful:
            raise _Unsupported()
        return 'exact', _plan(document, source, existed, None, None, name)
    except _Unsupported:
        return 'unsupported', None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return 'unavailable', None


def entry_backup(home):
    """Canonical bytes of the current entry (an `--adopt-legacy` backup), or None when there is none."""
    current = _current(home)
    if type(current) is str:
        return None
    servers, name = current[0].get('mcpServers', {}), _name()
    return _canonical(servers[name]).encode() if name in servers else None


def write(home, change, before_replace, temporary=None):
    """Compare-before-replace: only over the exact observed bytes; `before_replace` sees the inode first.

    The replacement keeps the replaced file's mode (Electron's 0644); a new file is 0600. `temporary` is
    the name the caller journaled before this call creates it.
    """
    if type(change) is not Change:
        raise ConfigConflict('invalid_change')
    _atomic_write(config_path(home), change.candidate, MAX_CONFIG_BYTES, expected=change.source,
                  require_existing=change.existed, before_replace=before_replace, max_mode=SHARED_MODE,
                  preserve_mode=True, temporary=temporary)


def discard_temporary(home, record):
    """Unlink our journaled temporary config file left by a killed write, only while it is that inode."""
    from .codex_registration import discard_temporary as discard
    try:
        return discard(config_path(home), record)
    except ConfigConflict:
        return False


def restore(home, change, binding, temporary=None):
    """Undo our write only while the file is still our exact object and bytes; True when undone."""
    path = config_path(home)
    try:
        current, exists = read(home)
        if not exists or current != change.candidate:
            return False
        require_object_binding(path, binding)
        if change.existed:
            _atomic_write(path, change.source, MAX_CONFIG_BYTES, expected=change.candidate,
                          require_existing=True, max_mode=SHARED_MODE, preserve_mode=True, temporary=temporary)
            return True
        fd = _open_parent(path)
        try:
            again, still = _read_at(fd, CONFIG_NAME, MAX_CONFIG_BYTES, SHARED_MODE)
            if not still or again != change.candidate:
                return False
            os.unlink(CONFIG_NAME, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except (ConfigConflict, OSError, RuntimePathError, ValueError):
        return False
