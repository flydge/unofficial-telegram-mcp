"""Claude Code user-scope MCP registration.

State is read only from the user-scope `mcpServers` object of `<home>/.claude.json`: a
bounded, no-follow, owner-checked, strict-JSON read that never starts a process. The
`claude` CLI is used only to write (`mcp add-json|remove --scope user`). It is found only at
a fixed list of locations (no PATH search); every path component and link hop is checked
without following links and the resolved target itself is executed with an explicit
minimal environment. Calls are argv lists with no shell, a bounded timeout and a private
working directory; output is discarded, never parsed, returned, logged or raised.
The runner is injectable so tests never touch the real CLI or its configuration.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import subprocess

from . import product_identity as identity
from .bundle_paths import (MissingRuntimePath, RuntimePathError, open_owned_file, file_metadata_identity,
                           _safe_metadata, _validate_descriptor_acl, _path_binding_identity)

SERVER_NAME = identity.MCP_SERVER_NAME
WRITE_TIMEOUT = 30
MAX_CONFIG_BYTES = 64 * 1024 * 1024
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
_MAX_LINKS = 16
_MAX_STEPS = 256
_EXACT_KEYS = {'type', 'command', 'args', 'env'}


class ClaudeCliError(RuntimeError):
    """The CLI could not be run or refused; carries no command output."""
    code = 'claude_registration_failed'

    def __init__(self):
        super().__init__(self.code)


def candidate_paths(home):
    home = Path(home)
    return (home / '.local/bin/claude', home / '.claude/local/claude',
            Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude'))


def config_path(home):
    return Path(home) / '.claude.json'


def payload(command, args):
    return json.dumps({'type': 'stdio', 'command': command, 'args': list(args)}, separators=(',', ':'))


def manual_command(command, args):
    """Nonsecret argv the user can run themselves to register this server."""
    return ['claude', 'mcp', 'add-json', '--scope', 'user', SERVER_NAME, payload(command, args)]


# --- reads: ~/.claude.json only ------------------------------------------------------------

def _strict_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('duplicate')
            out[key] = value
        return out
    def constant(_): raise ValueError('constant')
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    if type(value) is not dict: raise ValueError('shape')
    return value


def _servers(home):
    """The user-scope `mcpServers` dict ({} when the file or key is absent), else None (unavailable)."""
    try:
        with open_owned_file(config_path(home)) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_CONFIG_BYTES: return None
            data = stream.read(MAX_CONFIG_BYTES + 1)
            if len(data) != before.st_size or file_metadata_identity(before) != file_metadata_identity(os.fstat(stream.fileno())):
                return None
    except MissingRuntimePath:
        return {}
    except (OSError, RuntimePathError, ValueError):
        return None
    try:
        servers = _strict_json(data).get('mcpServers', {})
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None
    return servers if type(servers) is dict else None


def _exact(value, command, args):
    # `args` is compared as a JSON list, so a joined or re-split lookalike never matches.
    return (type(value) is dict and set(value) <= _EXACT_KEYS and value.get('type', 'stdio') == 'stdio'
            and type(value.get('command')) is str and value['command'] == command
            and type(value.get('args')) is list and value['args'] == list(args)
            and value.get('env', {}) == {} and type(value.get('env', {})) is dict)


def state(home, command, args):
    """'absent', 'exact' (stdio, this command and argument list, no env or url), 'different' or 'unavailable'."""
    servers = _servers(home)
    if servers is None: return 'unavailable'
    if SERVER_NAME not in servers: return 'absent'
    return 'exact' if _exact(servers[SERVER_NAME], command, args) else 'different'


# --- locating the CLI ----------------------------------------------------------------------

def _admit(path, metadata, *, leaf):
    """Owner root or the user, not group/world-writable, deny-only ACL, bound to an opened descriptor."""
    if leaf:
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise RuntimePathError('unsafe executable')
    else:
        _safe_metadata(path, metadata)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if leaf else os.O_DIRECTORY)
    descriptor = os.open(path, flags)
    try:
        if _path_binding_identity(os.fstat(descriptor)) != _path_binding_identity(metadata):
            raise RuntimePathError('binding changed')
        _validate_descriptor_acl(descriptor)
    finally:
        os.close(descriptor)


def _resolve(candidate):
    """Resolve link by link from `/`, admitting every directory, link and the final file without following links."""
    path = Path(candidate)
    if not path.is_absolute() or '\x00' in str(path):
        raise RuntimePathError('candidate must be absolute')
    current = Path('/')
    _admit(current, current.lstat(), leaf=False)
    pending = list(reversed(path.parts[1:]))
    links = steps = 0
    while pending:
        steps += 1
        if steps > _MAX_STEPS: raise RuntimePathError('too many components')
        part = pending.pop()
        if part == '..':
            current = current.parent  # `current` is an admitted real directory.
            continue
        child = current / part
        metadata = child.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            links += 1
            if links > _MAX_LINKS or metadata.st_uid not in (0, os.getuid()):
                raise RuntimePathError('unsafe link')
            target = os.readlink(child)
            after = child.lstat()
            if (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino) or not target or '\x00' in target:
                raise RuntimePathError('link changed')
            target = Path(target)
            if target.is_absolute():
                current = Path('/')
                pending.extend(reversed(target.parts[1:]))
            else:
                pending.extend(reversed(target.parts))
            continue
        if pending:
            _admit(child, metadata, leaf=False)
        current = child
    _admit(current, current.lstat(), leaf=True)
    if not os.access(current, os.X_OK):
        raise RuntimePathError('not executable')
    return current


def locate(candidates):
    """Resolved target of the first safe executable candidate, else None. PATH is never searched."""
    for candidate in candidates:
        try:
            return _resolve(candidate)
        except (OSError, RuntimePathError, RuntimeError, ValueError):
            continue
    return None


# --- writes: the CLI -----------------------------------------------------------------------

def environment(cli, home):
    return {'HOME': str(home), 'PATH': str(Path(cli).parent) + ':' + SYSTEM_PATH, 'LC_ALL': 'C'}


def _run(runner, cli, home, arguments):
    try:
        result = runner([str(cli), 'mcp', *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False, timeout=WRITE_TIMEOUT, cwd=str(home),
                        env=environment(cli, home))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ClaudeCliError() from None
    if getattr(result, 'returncode', None) != 0:
        raise ClaudeCliError()


def add(runner, cli, home, command, args):
    _run(runner, cli, home, ['add-json', '--scope', 'user', SERVER_NAME, payload(command, args)])


def remove(runner, cli, home):
    _run(runner, cli, home, ['remove', '--scope', 'user', SERVER_NAME])
