"""Behavioural stand-in for `claude mcp get|add-json|remove` over one MCP server name.

Never the real CLI: tests hand an instance to the installer as its runner and point the
fixed-location lookup at a harmless executable file. The text format mirrors the fields
the installer reads; everything else about the real CLI is out of scope here.
"""
import json
import subprocess

NAME = 'telegram_search'
SCOPES = {'user': 'User config (available in all your projects)',
          'local': 'Local config (private to you in this project)',
          'project': 'Project config (shared via .mcp.json)'}


def entry(command, args, *, scope='user', type='stdio', env=None, url=None):
    return dict(command=command, args=list(args), scope=scope, type=type, env=dict(env or {}), url=url)


class FakeClaude:
    def __init__(self, existing=None):
        self.entry = existing
        self.calls = []          # argv[1:] of every call, in order
        self.kwargs = []         # keyword arguments of every call
        self.fail = set()        # verbs that exit non-zero: 'get', 'add-json', 'remove'
        self.timeout = set()     # verbs that time out
        self.lose_add = False    # add-json exits zero but stores nothing
        self.mutate_after_add = None  # callable(self) run right after a successful add-json

    def verbs(self):
        return [call[1] for call in self.calls]

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv[1:]))
        self.kwargs.append(kwargs)
        assert argv[1] == 'mcp' and isinstance(argv, list), argv
        assert kwargs.get('check') is False and kwargs.get('timeout', 0) > 0 and 'shell' not in kwargs, kwargs
        verb = argv[2]
        if verb in self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 1, '', 'synthetic failure')
        if verb == 'get':
            assert argv[3:] == [NAME], argv
            if self.entry is None:
                return subprocess.CompletedProcess(argv, 1, '', 'No MCP server found with name: ' + NAME)
            return subprocess.CompletedProcess(argv, 0, self.render(), '')
        if verb == 'add-json':
            assert argv[3:5] == ['--scope', 'user'] and argv[5] == NAME and len(argv) == 7, argv
            if self.entry is not None and self.entry['scope'] == 'user':
                return subprocess.CompletedProcess(argv, 1, '', 'already exists')
            value = json.loads(argv[6])
            assert list(value) == ['type', 'command', 'args'] and value['type'] == 'stdio', value
            if not self.lose_add:
                self.entry = entry(value['command'], value['args'])
            if self.mutate_after_add is not None:
                self.mutate_after_add(self)
            return subprocess.CompletedProcess(argv, 0, 'added', '')
        if verb == 'remove':
            assert argv[3:] == ['--scope', 'user', NAME], argv
            if self.entry is None or self.entry['scope'] != 'user':
                return subprocess.CompletedProcess(argv, 1, '', 'not found')
            self.entry = None
            return subprocess.CompletedProcess(argv, 0, 'removed', '')
        raise AssertionError('unexpected claude subcommand: %r' % (argv,))

    def render(self):
        e = self.entry
        lines = [NAME + ':', '  Scope: ' + SCOPES[e['scope']], '  Status: ✗ Failed to connect', '  Type: ' + e['type']]
        if e['type'] == 'stdio':
            lines += ['  Command: ' + e['command'], '  Args: ' + ' '.join(e['args'])]
        else:
            lines.append('  URL: ' + str(e['url']))
        if e['env']:
            lines.append('  Environment:')
            lines += ['    %s=%s' % item for item in sorted(e['env'].items())]
        return '\n'.join(lines) + '\n\nTo remove this server, run: claude mcp remove "%s" -s %s\n' % (NAME, e['scope'])
