"""0.55 phase A: AccountPaths carries every per-account location and name; the default alias is 0.54.

The golden values are literals, so a changed default path, label or name fails here first.
"""
from __future__ import annotations

import ast
import dataclasses
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp import config
from telegram_search_mcp.config import (DEFAULT_ALIAS, AccountPaths, AccountPathsError, ConfigurationError,
                                        named_aliases_for_tests)
from telegram_search_mcp.enrollment import SetupPaths

HOME = Path('/fixture-home/sample-owner')
_ROOT = '/fixture-home/sample-owner/Library/Application Support/TelegramSearchMCP'
GOLDEN = {
    'alias': 'default',
    'root': Path(_ROOT),
    'session': Path(_ROOT + '/tdlib'),
    'database': Path(_ROOT + '/tdlib/db'),
    'files': Path(_ROOT + '/tdlib/files'),
    'run': Path(_ROOT + '/run'),
    'socket': Path(_ROOT + '/run/broker.sock'),
    'broker_lock': Path(_ROOT + '/run/broker.lock'),
    'connect_lock': Path(_ROOT + '/run/connect.lock'),
    'connect_status': Path(_ROOT + '/run/connect-status.json'),
    'connect_plist': Path(_ROOT + '/run/com.sample-owner.telegram-search-mcp.connect.plist'),
    'artifacts': Path(_ROOT + '/artifacts'),
    'artifacts_quarantine': Path(_ROOT + '/artifacts-quarantine'),
    'uploads': Path(_ROOT + '/incoming-uploads'),
    'uploads_quarantine': Path(_ROOT + '/incoming-uploads-quarantine'),
    'staging': Path(_ROOT + '/outgoing-staging'),
    'staging_quarantine': Path(_ROOT + '/outgoing-staging-quarantine'),
    'policy': Path(_ROOT + '/runtime.toml'),
    'enrollment_record': Path(_ROOT + '/enrollment.json'),
    'keychain_service': 'com.sample-owner.telegram-search-mcp',
    'broker_label': 'com.sample-owner.telegram-search-mcp.broker',
    'broker_plist': Path('/fixture-home/sample-owner/Library/LaunchAgents/com.sample-owner.telegram-search-mcp.broker.plist'),
    'connect_label': 'com.sample-owner.telegram-search-mcp.connect',
}
# Module constant -> AccountPaths field, for the default alias.
CONSTANTS = {
    'APP_SUPPORT_ROOT': 'root', 'KEYCHAIN_SERVICE': 'keychain_service', 'LAUNCH_AGENT_LABEL': 'broker_label',
    'LAUNCH_AGENT_PLIST': 'broker_plist', 'TDLIB_SESSION_DIRECTORY': 'session', 'BROKER_RUNTIME_DIRECTORY': 'run',
    'BROKER_SOCKET_PATH': 'socket', 'BROKER_LOCK_PATH': 'broker_lock', 'RUNTIME_POLICY_PATH': 'policy',
    'CONNECT_JOB_LABEL': 'connect_label', 'CACHE_DIRECTORY': 'artifacts', 'UPLOAD_ROOT': 'uploads',
    'STAGING_ROOT': 'staging',
}


def as_dict(paths):
    return {field.name: getattr(paths, field.name) for field in dataclasses.fields(paths)}


class DefaultGoldenTests(unittest.TestCase):
    def test_default_alias_is_the_literal_054_layout(self):
        paths = AccountPaths.for_alias(DEFAULT_ALIAS, HOME)
        self.assertEqual(as_dict(paths), GOLDEN)
        self.assertEqual(paths.server_name, 'telegram_search')
        self.assertEqual(DEFAULT_ALIAS, 'default')

    def test_owner_paths_keeps_its_tuple_and_derives_from_the_default_alias(self):
        self.assertEqual(config._owner_paths(HOME), (GOLDEN['root'], GOLDEN['keychain_service'],
                                                     GOLDEN['broker_label'], GOLDEN['broker_plist']))

    def test_module_constants_follow_the_home_layout_rule(self):
        # Re-executing config with an invented home is the import-time derivation every process runs.
        with patch.object(Path, 'home', return_value=HOME):
            namespace = runpy.run_path(config.__file__)
        for constant, field in CONSTANTS.items():
            with self.subTest(constant=constant):
                self.assertEqual(namespace[constant], GOLDEN[field])
        self.assertEqual(as_dict(namespace['DEFAULT_ACCOUNT_PATHS']), GOLDEN)
        self.assertEqual(namespace['WHISPER_MODEL_PATH'], Path(_ROOT + '/models/ggml-small.bin'))

    def test_module_constants_equal_the_default_alias_of_this_home(self):
        paths = AccountPaths.for_alias(DEFAULT_ALIAS, Path.home())
        self.assertEqual(config.DEFAULT_ACCOUNT_PATHS, paths)
        for constant, field in CONSTANTS.items():
            with self.subTest(constant=constant):
                self.assertEqual(getattr(config, constant), getattr(paths, field))
        self.assertEqual(config.load_runtime_policy.__defaults__, (paths.policy,))

    def test_server_name_is_read_through_the_product_identity(self):
        from telegram_search_mcp import product_identity
        paths = AccountPaths.for_alias(DEFAULT_ALIAS, HOME)
        with patch.object(product_identity, 'MCP_SERVER_NAME', 'renamed_server'), named_aliases_for_tests():
            self.assertEqual(paths.server_name, 'renamed_server')
            self.assertEqual(AccountPaths._derive('work', HOME).server_name, 'renamed_server_work')

    def test_frozen(self):
        paths = AccountPaths.for_alias(DEFAULT_ALIAS, HOME)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            paths.root = Path('/elsewhere')


class AliasRefusalTests(unittest.TestCase):
    def test_only_the_default_alias_is_constructible_and_the_refusal_is_fixed(self):
        for alias in ('work', 'Default', 'default ', '', '../default', 'a/b', None, 7, b'default'):
            with self.subTest(alias=alias), self.assertRaises(AccountPathsError) as caught:
                AccountPaths.for_alias(alias, HOME)
            self.assertEqual((caught.exception.code, str(caught.exception)), ('alias_unavailable',) * 2)
            self.assertIsInstance(caught.exception, ConfigurationError)

    def test_the_private_layout_takes_the_closed_grammar_and_reserves_default(self):
        refused = ('', 'Work', '1work', 'a/b', '../x', 'a.b', 'a-b', 'a b', None, 7, b'work', 'work\n', '\nwork',
                   'abcdefghijklm', 'a' * 13, 'w\u00f6rk', '\uff57ork', 'default')
        default = as_dict(AccountPaths.for_alias(DEFAULT_ALIAS, HOME))
        with named_aliases_for_tests():
            for alias in refused:
                with self.subTest(alias=alias), self.assertRaises(AccountPathsError) as caught:
                    AccountPaths._derive(alias, HOME)
                self.assertEqual((caught.exception.code, str(caught.exception)), ('alias_unavailable',) * 2)
                if alias != DEFAULT_ALIAS:  # The constructor takes the same grammar, even inside the hook.
                    with self.subTest(constructor=alias), self.assertRaises(AccountPathsError):
                        AccountPaths(**{**default, 'alias': alias})
            for alias in ('a', 'w0rk', 'abcdefghijkl', 'z' + '9' * 11):
                with self.subTest(alias=alias):
                    self.assertEqual(AccountPaths._derive(alias, HOME).alias, alias)
        self.assertEqual(config.ALIAS_GRAMMAR.pattern, '[a-z][a-z0-9]{0,11}')


class NamedAliasConstructionTests(unittest.TestCase):
    """Until the phase B registry exists, a named alias exists only inside the explicit test hook."""

    def test_every_way_to_build_a_named_alias_refuses_outside_the_hook(self):
        default = AccountPaths.for_alias(DEFAULT_ALIAS, HOME)
        with named_aliases_for_tests():
            work = AccountPaths._derive('work', HOME)
        builds = {
            'private layout': lambda: AccountPaths._derive('work', HOME),
            'constructor': lambda: AccountPaths(**{**as_dict(default), 'alias': 'work'}),
            'replace': lambda: dataclasses.replace(default, alias='work'),
            'relocated': lambda: work.relocated(Path('/synthetic/root')),
            'setup paths elsewhere': lambda: SetupPaths(Path('/synthetic/root'), work),
            'path-shaped constructor': lambda: AccountPaths(**{**as_dict(default), 'alias': '../x'}),
            'non-string alias': lambda: AccountPaths(**{**as_dict(default), 'alias': None}),
        }
        for name, build in builds.items():
            with self.subTest(build=name), self.assertRaises(AccountPathsError) as caught:
                build()
            self.assertEqual(caught.exception.code, 'alias_unavailable')

    def test_the_hook_admits_only_inside_its_block_even_when_the_block_raises(self):
        with self.assertRaises(RuntimeError), named_aliases_for_tests():
            self.assertEqual(AccountPaths._derive('work', HOME).alias, 'work')
            raise RuntimeError('leave the block')
        with self.assertRaises(AccountPathsError):
            AccountPaths._derive('work', HOME)
        with named_aliases_for_tests():
            with named_aliases_for_tests():
                pass
            self.assertEqual(AccountPaths._derive('home', HOME).alias, 'home')
        with self.assertRaises(AccountPathsError):
            AccountPaths._derive('home', HOME)

    def test_the_default_alias_and_an_admitted_account_stay_usable_outside_the_hook(self):
        default = AccountPaths.for_alias(DEFAULT_ALIAS, HOME)
        self.assertEqual(dataclasses.replace(default), default)
        self.assertEqual(default.relocated(Path('/synthetic/root')).root, Path('/synthetic/root'))
        with named_aliases_for_tests():
            work = AccountPaths._derive('work', HOME)
        self.assertEqual(SetupPaths.from_account(work).account, work)

    def test_nothing_outside_config_builds_an_account_or_enters_the_hook(self):
        root = Path(__file__).resolve().parents[1]
        package = root / 'src' / 'telegram_search_mcp'
        closed = {'AccountPaths._derive', 'AccountPaths._home_layout', 'AccountPaths._layout',
                  'named_aliases_for_tests', '_NAMED_ALIASES_ADMITTED', 'AccountPaths()'}
        sources = sorted([*package.glob('*.py'), *(root / 'packaging').glob('*.py')])
        self.assertGreater(len(sources), 80)
        found = []
        for path in sources:
            if path == package / 'config.py':
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = []
                if isinstance(node, ast.Attribute):
                    names += [node.attr, f'AccountPaths.{node.attr}']
                elif isinstance(node, ast.Name):
                    names.append(node.id)
                elif isinstance(node, ast.alias):
                    names.append(node.asname or node.name)
                if isinstance(node, ast.Call) and ast.unparse(node.func).split('.')[-1] == 'AccountPaths':
                    names.append('AccountPaths()')
                found += [f'{path.name}:{name}' for name in names if name in closed]
        self.assertEqual(found, [])


class AliasLayoutTests(unittest.TestCase):
    """The private constructor shows what phase B will admit; nothing public can build it yet."""

    def test_a_named_alias_is_a_sibling_of_the_legacy_tree_with_namespaced_names(self):
        with named_aliases_for_tests():
            work = AccountPaths._derive('work', HOME)
        root = Path('/fixture-home/sample-owner/Library/Application Support/TelegramSearchMCP-accounts/work')
        self.assertEqual((work.root, work.socket, work.policy), (root, root / 'run/broker.sock', root / 'runtime.toml'))
        self.assertEqual(work.broker_label, 'com.sample-owner.telegram-search-mcp.a.work.broker')
        self.assertEqual(work.connect_label, 'com.sample-owner.telegram-search-mcp.a.work.connect')
        self.assertEqual(work.broker_plist, HOME / 'Library/LaunchAgents/com.sample-owner.telegram-search-mcp.a.work.broker.plist')
        self.assertEqual(work.connect_plist, root / 'run/com.sample-owner.telegram-search-mcp.a.work.connect.plist')
        self.assertEqual(work.server_name, 'telegram_search_work')

    def test_every_field_but_the_shared_keychain_service_is_disjoint_between_accounts(self):
        with named_aliases_for_tests():
            accounts = [AccountPaths.for_alias(DEFAULT_ALIAS, HOME),
                        *(AccountPaths._derive(alias, HOME) for alias in ('work', 'home'))]
        for first in accounts:
            for second in accounts:
                if first is second:
                    continue
                with self.subTest(first=first.alias, second=second.alias):
                    self.assertEqual(first.keychain_service, second.keychain_service)  # spec decision 6
                    for field in dataclasses.fields(AccountPaths):
                        if field.name == 'keychain_service':
                            continue
                        self.assertNotEqual(getattr(first, field.name), getattr(second, field.name), field.name)
                    self.assertNotEqual(first.server_name, second.server_name)
                    for field in dataclasses.fields(AccountPaths):
                        value = getattr(first, field.name)
                        if isinstance(value, Path) and field.name != 'broker_plist':
                            self.assertFalse(value.is_relative_to(second.root), field.name)
                            self.assertTrue(value.is_relative_to(first.root), field.name)

    def test_relocation_keeps_the_names_and_moves_every_location(self):
        with named_aliases_for_tests():
            work = AccountPaths._derive('work', HOME)
            moved = work.relocated(Path('/synthetic/root'))
        for field in dataclasses.fields(AccountPaths):
            value, before = getattr(moved, field.name), getattr(work, field.name)
            if field.name in ('alias', 'keychain_service', 'broker_label', 'broker_plist', 'connect_label'):
                self.assertEqual(value, before, field.name)
            elif field.name == 'root':
                self.assertEqual(value, Path('/synthetic/root'))
            else:
                self.assertEqual(value, Path('/synthetic/root') / before.relative_to(work.root), field.name)
        self.assertEqual(AccountPaths.for_alias(DEFAULT_ALIAS, HOME).relocated(Path(_ROOT)),
                         AccountPaths.for_alias(DEFAULT_ALIAS, HOME))


if __name__ == '__main__':
    unittest.main()
