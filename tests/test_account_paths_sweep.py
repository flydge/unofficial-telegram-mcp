"""0.55 phase A gate (spec criterion 3): no module outside config.py defaults to a path, label or socket.

A parameter default, or a module- or class-level value evaluated at import, that names one of
config's per-account constants binds that account for good; only config derives them. Call-time use
in a function body is how the threaded defaults resolve the default alias, so it is allowed.

Two sweeps. The AST sweep reads the source, so it sees names. The runtime sweep imports every module
and reads the values the AST cannot follow (a helper call, a name imported from a re-exporting module,
getattr, a rebuilt string, a decorator argument): every function's __defaults__, __kwdefaults__ and
closure cells, every dataclass field default, and every module or class value, against the default
alias's locations, labels and names.
"""
from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import pkgutil
import sys
import textwrap
import types
import unittest
from pathlib import Path, PurePath
from unittest import mock

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
# (module relative to the repository, bound name) -> why it cannot be removed. Empty since phase D: the client
# registrations take their server name at call time (spec 3D, criterion 3).
ALLOWED = {}


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


# Runtime sweep. The default alias's locations, and the text every label, plist name and location string
# contains (every label starts with the Keychain service; every location but the plist lies under root).
DEFAULT = config.DEFAULT_ACCOUNT_PATHS
LOCATIONS = frozenset(getattr(DEFAULT, field.name) for field in dataclasses.fields(DEFAULT)
                      if isinstance(getattr(DEFAULT, field.name), PurePath))
TEXTS = (DEFAULT.keychain_service, str(DEFAULT.root), str(DEFAULT.broker_plist))
CONFIG_MODULE = config.__name__


def binds_account(value, depth=0, seen=None):
    """Whether a value is, holds or spells a default-alias location, label, socket or server name."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, AccountPaths):
        return True
    if isinstance(value, PurePath):
        return value in LOCATIONS or value.is_relative_to(DEFAULT.root)
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'surrogateescape')
    if isinstance(value, str):
        return value == DEFAULT.server_name or any(text in value for text in TEXTS)
    if depth >= 4 or isinstance(value, (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
                                        types.MethodType)):
        return False
    if isinstance(value, dict):
        items = [*value.keys(), *value.values()]
    elif isinstance(value, (tuple, list, set, frozenset)):
        items = list(value)
    elif isinstance(value, functools.partial):
        items = [*value.args, *value.keywords.values()]
    elif dataclasses.is_dataclass(value):
        items = [getattr(value, field.name, None) for field in dataclasses.fields(value)]
    elif hasattr(value, '__dict__'):
        items = list(vars(value).values())
    else:
        return False
    return any(binds_account(item, depth + 1, seen) for item in items)


def wrapped_function(value, depth=0):
    """A plain function, or a cache wrapper (functools.lru_cache, functools.cache) around one, at any depth."""
    if isinstance(value, types.FunctionType):
        return True
    return (depth < 8 and callable(value) and not isinstance(value, type) and hasattr(value, 'cache_info')
            and wrapped_function(getattr(value, '__wrapped__', None), depth + 1))


def package_modules():
    """Every module of the package but config, imported."""
    import telegram_search_mcp
    names = [telegram_search_mcp.__name__] + [f'{telegram_search_mcp.__name__}.{info.name}'
                                              for info in pkgutil.iter_modules(telegram_search_mcp.__path__)]
    return [importlib.import_module(name) for name in names if name != CONFIG_MODULE]


def runtime_violations(module):
    """[(owner qualname or '<module>', name)]: a parameter default, a closure cell, a dataclass field default,
    or a module or class value that binds the default alias. A module or class value may only be config's own
    object (a re-export) or the product identity's server name; a default or a cell may not bind it at all."""
    from telegram_search_mcp import product_identity
    shared = {id(value) for value in vars(config).values()} | {id(product_identity.MCP_SERVER_NAME)}
    found, pending, visited = [], [], set()

    def value(owner, name, held):
        if id(held) not in shared and binds_account(held):
            found.append((owner, name))

    def attributes(owner, held):
        """A function's or wrapper's own attributes (`f.root = ...`); the dunder metadata wraps() copies is not."""
        for name, value in (getattr(held, '__dict__', None) or {}).items():
            if not (name.startswith('__') and name.endswith('__')) and binds_account(value):
                found.append((owner, name))

    def defaults(function):
        """Defaults, keyword defaults, closure cells (a decorator factory's argument) and attributes of a function,
        of every function it wraps and of every cache wrapper (functools.lru_cache, functools.cache) between."""
        while function is not None and id(function) not in visited:
            visited.add(id(function))
            if not isinstance(function, types.FunctionType):  # A cache or other wrapper: read it, then unwrap.
                if getattr(function, '__module__', None) != CONFIG_MODULE:
                    attributes(getattr(function, '__qualname__', type(function).__qualname__), function)
                function = getattr(function, '__wrapped__', None)
                continue
            if function.__module__ != CONFIG_MODULE:
                code = function.__code__
                given = function.__defaults__ or ()
                positional = code.co_varnames[:code.co_argcount][code.co_argcount - len(given):]
                cells = []
                for name, cell in zip(code.co_freevars, function.__closure__ or ()):
                    try:
                        cells.append((name, cell.cell_contents))
                    except ValueError:  # A cell not yet bound holds nothing.
                        pass
                for name, held in [*zip(positional, given), *(function.__kwdefaults__ or {}).items(), *cells]:
                    if binds_account(held):
                        found.append((function.__qualname__, name))
                attributes(function.__qualname__, function)
                pending.extend(held for _, held in cells if wrapped_function(held))
            function = getattr(function, '__wrapped__', None)

    def member(owner, name, held):
        if isinstance(held, (staticmethod, classmethod)):
            held = held.__func__
        if wrapped_function(held):
            defaults(held)
        elif isinstance(held, property):
            for accessor in (held.fget, held.fset, held.fdel):
                if isinstance(accessor, types.FunctionType):
                    defaults(accessor)
        elif isinstance(held, functools.cached_property):
            defaults(held.func)
        elif isinstance(held, type):
            if held.__module__ == module.__name__:
                pending.append(held)
        elif not (name.startswith('__') and name.endswith('__')):
            value(owner, name, held)

    for name, held in vars(module).items():
        member('<module>', name, held)
    while pending:
        cls = pending.pop()
        if wrapped_function(cls):  # A function a closure holds: the one a decorator wrapped.
            defaults(cls)
            continue
        if id(cls) in visited:
            continue
        visited.add(id(cls))
        for name, held in vars(cls).items():
            member(cls.__qualname__, name, held)
        if dataclasses.is_dataclass(cls):
            for field in dataclasses.fields(cls):
                if field.default is not dataclasses.MISSING and binds_account(field.default):
                    found.append((cls.__qualname__, field.name))
    return sorted(set(found))


class SweepTests(unittest.TestCase):
    def test_no_module_outside_config_defaults_to_an_account_path_label_or_socket(self):
        self.assertGreater(len(sources()), 80)
        found = {}
        for path in sources():
            relative = path.relative_to(ROOT).as_posix()
            for line, name, constant in violations(path.read_text(encoding='utf-8')):
                found.setdefault((relative, name), []).append(f'{line}:{constant}')
        self.assertEqual(sorted(set(found) - set(ALLOWED)), [], found)

    def test_the_allowlist_is_empty(self):
        self.assertEqual(ALLOWED, {})  # Criterion 3: no path, label or socket default outside config.py at all.

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


PROBE_PRELUDE = '''
import dataclasses
import functools
from pathlib import Path
from telegram_search_mcp import config
from telegram_search_mcp.config import CACHE_DIRECTORY


def _helper():
    return CACHE_DIRECTORY
'''


def probe(source):
    """runtime_violations of a synthetic package module built from the prelude and `source`."""
    name = 'telegram_search_mcp._runtime_sweep_probe'
    module = types.ModuleType(name)
    with mock.patch.dict(sys.modules, {name: module}):
        exec(compile(PROBE_PRELUDE + textwrap.dedent(source), name, 'exec'), vars(module))
        return set(runtime_violations(module))


class RuntimeSweepTests(unittest.TestCase):
    """The AST cannot follow a helper call, a name imported from a re-exporting module, getattr or a rebuilt
    string; once every module is imported, the values themselves show the default alias."""

    def test_no_imported_module_holds_the_default_alias_in_a_default_or_derived_value(self):
        modules = package_modules()
        self.assertGreater(len(modules), 80)
        names = {module.__name__ for module in modules}
        self.assertLessEqual({'telegram_search_mcp', 'telegram_search_mcp.product_identity',
                              'telegram_search_mcp.media_analyzer', 'telegram_search_mcp.launch_agent'}, names)
        self.assertNotIn('telegram_search_mcp.config', names)
        found = {module.__name__: runtime_violations(module) for module in modules}
        self.assertEqual({name: hits for name, hits in found.items() if hits}, {})

    def test_the_helper_call_and_renamed_import_bypasses_are_caught_at_runtime_only(self):
        helper = 'def f(root=_helper()): pass\n'
        renamed = ('from telegram_search_mcp.media_analyzer import DEFAULT_MODEL_PATH as MODEL\n'
                   'def f(model=MODEL): pass\n')
        self.assertEqual(violations(PROBE_PRELUDE + helper), [])
        self.assertEqual(violations(PROBE_PRELUDE + renamed), [])
        self.assertEqual(probe(helper), {('f', 'root')})
        self.assertEqual(probe(renamed), {('f', 'model')})

    def test_each_kind_of_runtime_default_is_found(self):
        cases = {
            'def f(*, socket=str(_helper().parent / "run" / "broker.sock")): pass': {('f', 'socket')},
            'def f(label="com." + Path.home().name + ".telegram-search-mcp.broker"): pass': {('f', 'label')},
            'def f(policy=getattr(config, "RUNTIME_" + "POLICY_PATH")): pass': {('f', 'policy')},
            'def f(roots=(Path("/elsewhere"), _helper()), paths=config.DEFAULT_ACCOUNT_PATHS): pass':
                {('f', 'roots'), ('f', 'paths')},
            'from telegram_search_mcp.enrollment import SetupPaths\ndef f(paths=SetupPaths()): pass': {('f', 'paths')},
            'pick = lambda p=_helper(): p': {('<lambda>', 'p')},
            'ROOT = _helper().parent / "elsewhere"': {('<module>', 'ROOT')},
            'class K:\n    ROOT = _helper() / "x"': {('K', 'ROOT')},
            '@dataclasses.dataclass(frozen=True)\nclass Box:\n    root: Path = _helper()':
                {('Box', 'root'), ('Box.__init__', 'root')},
            '''
            class Service:
                @staticmethod
                def s(p=_helper()): pass
                @classmethod
                def c(cls, p=_helper()): pass
                @property
                def r(self, p=_helper()): pass
            ''': {('Service.s', 'p'), ('Service.c', 'p'), ('Service.r', 'p')},
            '''
            def deco(function):
                @functools.wraps(function)
                def wrapper(*args, **kwargs):
                    return function(*args, **kwargs)
                return wrapper
            @deco
            def g(p=_helper()): pass
            ''': {('g', 'p')},
            '''
            def at(root):
                def deco(function):
                    def wrapper(*args):
                        return function(root, *args)
                    return wrapper
                return deco
            @at(_helper())
            def g(root): pass
            ''': {('at.<locals>.deco.<locals>.wrapper', 'root')},
            # Cache wrappers hide the function they wrap from a FunctionType check; attributes hide from defaults.
            '@functools.lru_cache\ndef g(p=_helper()): pass': {('g', 'p')},
            '@functools.lru_cache(maxsize=4)\ndef g(*, p=_helper()): pass': {('g', 'p')},
            '@functools.cache\ndef g(p=_helper()): pass': {('g', 'p')},
            '''
            @functools.cache
            @functools.lru_cache
            def g(p=_helper()): pass
            ''': {('g', 'p')},
            'class K:\n    @functools.lru_cache\n    def m(self, p=_helper()): pass': {('K.m', 'p')},
            'class K:\n    @staticmethod\n    @functools.cache\n    def s(p=_helper()): pass': {('K.s', 'p')},
            'def g(): pass\ng.root = _helper()': {('g', 'root')},
            'def g(): pass\ng.labels = {"broker": config.LAUNCH_AGENT_LABEL}': {('g', 'labels')},
            '@functools.cache\ndef g(): pass\ng.root = _helper()': {('g', 'root')},
            '''
            def at(root):
                def deco(function):
                    @functools.lru_cache
                    def wrapper(*args):
                        return function(root, *args)
                    return wrapper
                return deco
            @at(_helper())
            def g(root): pass
            ''': {('at.<locals>.deco.<locals>.wrapper', 'root')},
            # Allowed: call-time use, unrelated paths, a named alias's server name and a re-exported config object.
            '@functools.cache\ndef g(p=None):\n    return p or CACHE_DIRECTORY': set(),
            'def g(): pass\ng.name = "telegram_search_work"\ng.__doc__ = "unrelated"': set(),
            'def f(p=None, home=Path.home(), other=Path("/elsewhere"), name="telegram_search_work"):\n'
            '    return p or CACHE_DIRECTORY': set(),
            'from telegram_search_mcp.artifact_store import CACHE_DIRECTORY as C': set(),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(probe(source), expected)


if __name__ == '__main__':
    unittest.main()
