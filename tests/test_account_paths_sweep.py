"""0.55 phase A gate (spec criterion 3): no module outside config.py defaults to a path, label or socket.

A parameter default, or a module- or class-level value evaluated at import, that names one of
config's per-account constants binds that account for good; only config derives them. Call-time use
in a function body is how the threaded defaults resolve the default alias, so it is allowed.
"""
from __future__ import annotations

import ast
import textwrap
import unittest
from pathlib import Path

from telegram_search_mcp import config
from telegram_search_mcp.config import AccountPaths

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src' / 'telegram_search_mcp'
# Every Path or AccountPaths constant config exports, its label and service names, and the MCP server
# name (an AccountPaths name whose source of truth is product_identity).
LABELS = frozenset({'KEYCHAIN_SERVICE', 'LAUNCH_AGENT_LABEL', 'CONNECT_JOB_LABEL', 'MCP_SERVER_NAME'})
CONSTANTS = frozenset({name for name, value in vars(config).items()
                       if name.isupper() and isinstance(value, (Path, AccountPaths))}) | LABELS
# Calls that derive an account's paths; one evaluated at import binds an account just the same.
DERIVERS = frozenset({'_owner_paths', 'for_alias', '_derive', 'relocated', 'SetupPaths', 'from_account'})
# (module relative to the repository, bound name) -> why phase A cannot remove it.
ALLOWED = {
    ('src/telegram_search_mcp/codex_registration.py', 'SERVER_NAME'):
        'Registrations take an optional server name in phase D (spec 3D); today they register the default alias.',
    ('src/telegram_search_mcp/claude_registration.py', 'SERVER_NAME'):
        'Registrations take an optional server name in phase D (spec 3D); today they register the default alias.',
    ('src/telegram_search_mcp/claude_desktop_registration.py', 'SERVER_NAME'):
        'Registrations take an optional server name in phase D (spec 3D); today they register the default alias.',
}


def _names(node):
    """Identifiers and called derivers a value expression reaches when evaluated; a lambda's defaults are
    checked as parameters and its body runs at call time, so the walk stops there."""
    pending = [node]
    while pending:
        child = pending.pop()
        if isinstance(child, ast.Lambda):
            continue
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr
        if isinstance(child, ast.Call):
            func = child.func
            yield '()' + (func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else '')
        pending.extend(ast.iter_child_nodes(child))


def violations(source):
    """[(line, bound name or parameter, constant)] for import-time defaults naming an account constant."""
    tree = ast.parse(source)
    banned = set(CONSTANTS)
    for node in ast.walk(tree):  # `from .config import X as Y` makes Y the same constant.
        if isinstance(node, ast.ImportFrom):
            banned.update(alias.asname for alias in node.names if alias.name in CONSTANTS and alias.asname)

    def hit(value):
        for name in _names(value):
            if name in banned or (name.startswith('()') and name[2:] in DERIVERS):
                return name.removeprefix('()')
        return None

    found = []

    def defaults(function):
        arguments = function.args
        positional = arguments.posonlyargs + arguments.args
        pairs = list(zip(positional[len(positional) - len(arguments.defaults):], arguments.defaults))
        pairs += [(argument, value) for argument, value in zip(arguments.kwonlyargs, arguments.kw_defaults)
                  if value is not None]
        for argument, value in pairs:
            constant = hit(value)
            if constant is not None:
                found.append((value.lineno, argument.arg, constant))

    def import_time(body):
        """Statements run at import: module and class bodies, including their if/try/with blocks."""
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(statement, ast.ClassDef):
                import_time(statement.body)
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and statement.value is not None:
                constant = hit(statement.value)
                if constant is not None:
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name):
                                found.append((statement.lineno, name.id, constant))
                                banned.add(name.id)  # A derived module name is the same constant.
            for field in ('body', 'orelse', 'finalbody', 'handlers'):
                block = getattr(statement, field, None)
                if isinstance(block, list) and not isinstance(statement, ast.ClassDef):
                    import_time([item for item in block if isinstance(item, ast.stmt)]
                                + [inner for handler in block if isinstance(handler, ast.ExceptHandler)
                                   for inner in handler.body])

    import_time(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            defaults(node)
    return found


def sources():
    paths = sorted([*PACKAGE.glob('*.py'), *(ROOT / 'packaging').glob('*.py')])
    return [path for path in paths if path != PACKAGE / 'config.py']


class SweepTests(unittest.TestCase):
    def test_no_module_outside_config_defaults_to_an_account_path_label_or_socket(self):
        self.assertGreater(len(sources()), 80)
        found = {}
        for path in sources():
            relative = path.relative_to(ROOT).as_posix()
            for line, name, constant in violations(path.read_text(encoding='utf-8')):
                found.setdefault((relative, name), []).append(f'{line}:{constant}')
        self.assertEqual(sorted(set(found) - set(ALLOWED)), [], found)

    def test_every_allowlist_entry_is_still_needed_and_has_a_reason(self):
        for (relative, name), reason in ALLOWED.items():
            with self.subTest(module=relative, name=name):
                self.assertTrue(reason.strip())
                names = {found for _, found, _ in violations((ROOT / relative).read_text(encoding='utf-8'))}
                self.assertIn(name, names)

    def test_the_constant_set_covers_every_per_account_name(self):
        self.assertLessEqual({'DEFAULT_ACCOUNT_PATHS', 'APP_SUPPORT_ROOT', 'TDLIB_SESSION_DIRECTORY',
                              'BROKER_RUNTIME_DIRECTORY', 'BROKER_SOCKET_PATH', 'BROKER_LOCK_PATH',
                              'RUNTIME_POLICY_PATH', 'LAUNCH_AGENT_PLIST', 'CACHE_DIRECTORY', 'UPLOAD_ROOT',
                              'STAGING_ROOT', 'WHISPER_MODEL_PATH', 'KEYCHAIN_SERVICE', 'LAUNCH_AGENT_LABEL',
                              'CONNECT_JOB_LABEL', 'MCP_SERVER_NAME'}, CONSTANTS)

    def test_the_sweep_finds_each_kind_of_import_time_default(self):
        cases = {
            'def f(p=BROKER_SOCKET_PATH): pass': [(1, 'p', 'BROKER_SOCKET_PATH')],
            'def f(*, p=config.RUNTIME_POLICY_PATH): pass': [(1, 'p', 'RUNTIME_POLICY_PATH')],
            'f = lambda p=STAGING_ROOT / "x": p': [(1, 'p', 'STAGING_ROOT')],
            'ROOT = APP_SUPPORT_ROOT / "artifacts"': [(1, 'ROOT', 'APP_SUPPORT_ROOT')],
            'LABEL = f"{KEYCHAIN_SERVICE}.connect"': [(1, 'LABEL', 'KEYCHAIN_SERVICE')],
            'A, B = _owner_paths(Path.home())[:2]': [(1, 'A', '_owner_paths'), (1, 'B', '_owner_paths')],
            'class C:\n    root: Path = DEFAULT_ACCOUNT_PATHS.root': [(2, 'root', 'DEFAULT_ACCOUNT_PATHS')],
            'def f(paths=SetupPaths()): pass': [(1, 'paths', 'SetupPaths')],
            'from .config import UPLOAD_ROOT as U\ndef f(r=U): pass': [(2, 'r', 'U')],
            'NAME = identity.MCP_SERVER_NAME\ndef f(n=NAME): pass': [(1, 'NAME', 'MCP_SERVER_NAME'), (2, 'n', 'NAME')],
            'try:\n    X = CACHE_DIRECTORY\nexcept Exception:\n    X = None': [(2, 'X', 'CACHE_DIRECTORY')],
            'def f(p=None):\n    return BROKER_SOCKET_PATH if p is None else p': [],
            'def f(p=None):\n    X = APP_SUPPORT_ROOT\n    return X': [],
            'from .config import BROKER_SOCKET_PATH, CACHE_DIRECTORY': [],
            'def f(home):\n    return AccountPaths.for_alias("default", home)': [],
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(sorted(violations(textwrap.dedent(source))), sorted(expected))


if __name__ == '__main__':
    unittest.main()
