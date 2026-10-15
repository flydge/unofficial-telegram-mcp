"""Claude Code user-scope MCP registration through the `claude` CLI.

The CLI is found only at a fixed list of locations (no PATH search). Every call is an
argv list with no shell, a bounded timeout and a private working directory; output is
parsed for a few fixed fields and never returned, logged or placed in an exception.
The runner is injectable so tests never touch the real CLI or its configuration.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import subprocess

SERVER_NAME = 'telegram_search'
GET_TIMEOUT = 60  # `claude mcp get` also health-checks the server it describes.
WRITE_TIMEOUT = 30
_MAX_OUTPUT = 64 * 1024
_FIELD = re.compile(r'  ([A-Za-z][A-Za-z ]{0,31}): ?(.*)')


class ClaudeCliError(RuntimeError):
    """The CLI could not be run or refused; carries no command output."""
    code = 'claude_registration_failed'

    def __init__(self):
        super().__init__(self.code)


def candidate_paths(home):
    home = Path(home)
    return (home / '.local/bin/claude', home / '.claude/local/claude',
            Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude'))


def locate(candidates):
    """First candidate that resolves to a regular executable owned by the user or root, else None."""
    for path in candidates:
        try:
            metadata = Path(path).resolve(strict=True).stat()
            if (stat.S_ISREG(metadata.st_mode) and metadata.st_uid in (0, os.getuid())
                    and not metadata.st_mode & 0o022 and os.access(path, os.X_OK)):
                return Path(path)
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _run(runner, cli, home, arguments, timeout):
    try:
        return runner([str(cli), 'mcp', *arguments], capture_output=True, text=True, errors='replace',
                      check=False, timeout=timeout, cwd=str(home), stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ClaudeCliError() from None


def _matches(output, command, args):
    if type(output) is not str:
        return False
    text = output[:_MAX_OUTPUT]
    lines = text.splitlines()
    if not lines or lines[0].strip() != SERVER_NAME + ':':
        return False
    fields = {}
    for line in lines[1:]:
        found = _FIELD.fullmatch(line.rstrip())
        if found and found.group(1) not in fields:
            fields[found.group(1)] = found.group(2).strip()
    return (fields.get('Scope', '').startswith('User') and fields.get('Type') == 'stdio'
            and fields.get('Command') == command and fields.get('Args') == ' '.join(args)
            and 'Environment' not in fields and 'URL' not in fields)


def state(runner, cli, home, command, args):
    """'absent' (the CLI reports no such server), 'exact' (user scope, this command and args) or 'different'."""
    result = _run(runner, cli, home, ['get', SERVER_NAME], GET_TIMEOUT)
    if result.returncode != 0:
        return 'absent'  # A failing add (it never overwrites) and the confirming get decide the rest.
    return 'exact' if _matches(result.stdout, command, args) else 'different'


def add(runner, cli, home, command, args):
    payload = json.dumps({'type': 'stdio', 'command': command, 'args': list(args)}, separators=(',', ':'))
    if _run(runner, cli, home, ['add-json', '--scope', 'user', SERVER_NAME, payload], WRITE_TIMEOUT).returncode != 0:
        raise ClaudeCliError()


def remove(runner, cli, home):
    if _run(runner, cli, home, ['remove', '--scope', 'user', SERVER_NAME], WRITE_TIMEOUT).returncode != 0:
        raise ClaudeCliError()
