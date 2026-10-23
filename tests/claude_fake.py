"""Behavioural stand-in for `claude mcp add-json|remove --scope user` over our MCP server names.

Never the real CLI: tests hand an instance to the installer as its runner and point the
fixed-location lookup at a harmless executable file. Like the real CLI it keeps user-scope
servers in the top-level `mcpServers` object of `<home>/.claude.json` (mode 0600, replaced
atomically, neighbouring keys preserved); an entry added without env is exactly
`{"type": "stdio", "command": ..., "args": [...]}`. Reads never start a process, so a `get`
(or any other verb) fails the test. The default's name is `telegram_search`; a named alias's
`telegram_search_<alias>` (0.55 spec 3D) is accepted too and read through `entry_of`.
"""
import json
import os
from pathlib import Path
import re
import subprocess

NAME = 'telegram_search'
NAMES = re.compile(r'telegram_search(?:_[a-z][a-z0-9]{0,11})?\Z')


def entry(command=None, args=(), *, type='stdio', env=None, url=None):
    """A `mcpServers` value in the CLI's on-disk shape; env/url only when given."""
    value = {'type': type}
    if command is not None:
        value.update(command=command, args=list(args))
    if env is not None:
        value['env'] = dict(env)
    if url is not None:
        value['url'] = url
    return value


class FakeClaude:
    def __init__(self, home):
        self.home = Path(home)
        self.calls = []          # argv[1:] of every call, in order
        self.argv0 = []          # argv[0] of every call
        self.kwargs = []         # keyword arguments of every call
        self.fail = set()        # verbs that exit non-zero: 'add-json', 'remove'
        self.returncode = 1      # exit status used for `fail` (127: an interpreter the CLI needs is missing)
        self.timeout = set()     # verbs that time out
        self.lose_add = False    # add-json exits zero but stores nothing
        self.mutate_after_add = None  # callable(self) run right after a successful add-json

    @property
    def path(self):
        return self.home / '.claude.json'

    def document(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def write_document(self, value):
        temporary = self.home / ('.claude.json.tmp-%d' % os.getpid())
        temporary.write_text(json.dumps(value, indent=2))
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    @property
    def entry(self):
        return self.entry_of(NAME)

    @entry.setter
    def entry(self, value):
        self.set_entry(NAME, value)

    def entry_of(self, name):
        document = self.document()
        return None if document is None else document.get('mcpServers', {}).get(name)

    def set_entry(self, name, value):
        document = self.document() or {'numStartups': 3, 'projects': {}}
        servers = dict(document.get('mcpServers', {}))
        if value is None:
            servers.pop(name, None)
        else:
            servers[name] = value
        self.write_document({**document, 'mcpServers': servers})

    def verbs(self):
        return [call[1] for call in self.calls]

    def __call__(self, argv, **kwargs):
        assert isinstance(argv, list), argv
        self.calls.append(list(argv[1:]))
        self.argv0.append(argv[0])
        self.kwargs.append(kwargs)
        assert argv[1] == 'mcp', argv
        assert kwargs.get('check') is False and kwargs.get('timeout', 0) > 0 and 'shell' not in kwargs, kwargs
        assert type(kwargs.get('env')) is dict, kwargs
        verb = argv[2]
        assert verb in ('add-json', 'remove'), 'the installer may only write through the CLI: %r' % (argv,)
        if verb in self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, self.returncode)
        if verb == 'add-json':
            assert argv[3:5] == ['--scope', 'user'] and NAMES.fullmatch(argv[5]) and len(argv) == 7, argv
            if self.entry_of(argv[5]) is not None:
                return subprocess.CompletedProcess(argv, 1)
            value = json.loads(argv[6])
            assert list(value) == ['type', 'command', 'args'] and value['type'] == 'stdio', value
            if not self.lose_add:
                self.set_entry(argv[5], entry(value['command'], value['args']))
            if self.mutate_after_add is not None:
                self.mutate_after_add(self)
            return subprocess.CompletedProcess(argv, 0)
        assert argv[3:5] == ['--scope', 'user'] and len(argv) == 6 and NAMES.fullmatch(argv[5]), argv
        if self.entry_of(argv[5]) is None:
            return subprocess.CompletedProcess(argv, 1)
        self.set_entry(argv[5], None)
        return subprocess.CompletedProcess(argv, 0)
