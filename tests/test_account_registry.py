"""0.55 phase B: the alias registry is closed, owner-only and bounded; for_alias admits only registered aliases.

Spec criterion 4: the alias grammar, the registry schema, the socket and tool-name budgets and the pin refusals
are closed with fixed codes. Homes are short directories under /tmp, so a socket path fits the macOS limit.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_search_mcp import account_registry as registry
from telegram_search_mcp import config
from telegram_search_mcp.account_registry import RegistryError
from telegram_search_mcp.config import AccountPaths, AccountPathsError, ConfigurationError

USER = 4_000_000_017  # A synthetic user id; it must never appear in any output.
OTHER = 4_000_000_029
SENTINEL = 'SYNTHETIC_ALIAS_SENTINEL'


def short_home(case: unittest.TestCase) -> Path:
    """A home whose alias socket paths fit the 103-byte budget: /tmp/<8 characters>, not resolved."""
    temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='r')
    case.addCleanup(temporary.cleanup)
    return Path(temporary.name)


def document(aliases=None, default=None, **extra):
    return {'version': 1, 'aliases': {} if aliases is None else aliases, 'default_user_id': default, **extra}


def encoded(value) -> bytes:
    return json.dumps(value).encode()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = short_home(self)
        self.root = config.accounts_root(self.home)

    def write(self, data: bytes, mode=0o600, root_mode=0o700):
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(root_mode)
        path = self.root / registry.REGISTRY_NAME
        if path.exists():
            path.unlink()
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def snapshot(self):
        return sorted(str(path.relative_to(self.home)) for path in self.home.rglob('*'))

    def refused(self, code, call):
        with self.assertRaises(RegistryError) as caught:
            call()
        self.assertEqual((caught.exception.code, str(caught.exception)), (code, code))
        self.assertIsInstance(caught.exception, ConfigurationError)
        for secret in (str(USER), str(OTHER), SENTINEL):
            self.assertNotIn(secret, repr(caught.exception.args))
        return caught.exception


class ClosedParserTests(Fixture):
    def test_the_closed_schema_parses(self):
        parsed = registry.parse(encoded(document({'work': {'user_id': None}, 'home': {'user_id': USER}}, OTHER)))
        self.assertEqual(parsed.aliases, ('home', 'work'))
        self.assertEqual(parsed.default_user_id, OTHER)
        self.assertEqual(registry.parse(encoded(document())), registry.EMPTY)
        eight = {f'a{n}': {'user_id': None} for n in range(8)}
        self.assertEqual(len(registry.parse(encoded(document(eight))).aliases), 8)

    def test_anything_else_refuses_with_one_fixed_code(self):
        nine = {f'a{n}': {'user_id': None} for n in range(9)}
        cases = {
            'version 2': encoded({**document(), 'version': 2}),
            'version string': encoded({**document(), 'version': '1'}),
            'version bool': encoded({**document(), 'version': True}),
            'version float': b'{"version": 1.0, "aliases": {}, "default_user_id": null}',
            'extra key': encoded(document(extra=SENTINEL)),
            'missing key': encoded({'version': 1, 'aliases': {}}),
            'aliases list': encoded({**document(), 'aliases': []}),
            'nine aliases': encoded(document(nine)),
            'reserved default': encoded(document({'default': {'user_id': None}})),
            'upper case': encoded(document({'Work': {'user_id': None}})),
            'leading digit': encoded(document({'1work': {'user_id': None}})),
            'thirteen characters': encoded(document({'a' * 13: {'user_id': None}})),
            'hyphen': encoded(document({'a-b': {'user_id': None}})),
            'empty alias': encoded(document({'': {'user_id': None}})),
            'non-ascii': encoded(document({'wörk': {'user_id': None}})),
            'path alias': encoded(document({'../x': {'user_id': None}})),
            'sentinel alias': encoded(document({SENTINEL: {'user_id': None}})),
            'entry extra key': encoded(document({'work': {'user_id': None, 'name': SENTINEL}})),
            'entry not object': encoded(document({'work': None})),
            'user id bool': encoded(document({'work': {'user_id': True}})),
            'user id string': encoded(document({'work': {'user_id': str(USER)}})),
            'user id float': b'{"version": 1, "aliases": {"work": {"user_id": 7.0}}, "default_user_id": null}',
            'user id zero': encoded(document({'work': {'user_id': 0}})),
            'user id negative': encoded(document({'work': {'user_id': -USER}})),
            'user id bound': encoded(document({'work': {'user_id': 2**53}})),
            'default bool': encoded(document(default=False)),
            'duplicate pin': encoded(document({'work': {'user_id': USER}, 'home': {'user_id': USER}})),
            'duplicate with default': encoded(document({'work': {'user_id': USER}}, USER)),
            'duplicate key': b'{"version": 1, "version": 1, "aliases": {}, "default_user_id": null}',
            'duplicate alias': b'{"version": 1, "aliases": {"work": {"user_id": null}, "work": {"user_id": null}}, '
                               b'"default_user_id": null}',
            'nan': b'{"version": 1, "aliases": {}, "default_user_id": NaN}',
            'infinity': b'{"version": 1, "aliases": {}, "default_user_id": Infinity}',
            'not utf-8': b'{"version": 1, "aliases": {"\xff": {"user_id": null}}, "default_user_id": null}',
            'trailing data': encoded(document()) + b' []',
            'top-level list': b'[]',
            'top-level null': b'null',
            'empty': b'',
            'oversized': encoded(document()) + b' ' * registry.MAX_REGISTRY_BYTES,
            'deep nesting': b'[' * 100000 + b']' * 100000,
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                error = self.refused('registry_invalid', lambda: registry.parse(data))
                self.assertNotIn(SENTINEL, str(error))

    def test_a_registry_never_shows_its_pins(self):
        parsed = registry.parse(encoded(document({'work': {'user_id': USER}}, OTHER)))
        for text in (repr(parsed), str(parsed)):
            self.assertNotIn(str(USER), text)
            self.assertNotIn(str(OTHER), text)

    def test_the_encoding_round_trips_through_the_closed_parser(self):
        value = registry.EMPTY.with_alias('work').with_alias('home').with_pin('work', USER).with_pin('default', OTHER)
        self.assertEqual(registry.parse(value.encode()), value)
        self.assertEqual(json.loads(value.encode()), document({'home': {'user_id': None}, 'work': {'user_id': USER}},
                                                               OTHER))
        self.assertEqual(value.without_alias('work').aliases, ('home',))


class FileSafetyTests(Fixture):
    def test_absent_registry_reads_as_none_and_creates_nothing(self):
        self.assertIsNone(registry.load(self.root))
        self.assertFalse(registry.present(self.root))
        self.root.mkdir(parents=True, mode=0o700)
        before = self.snapshot()
        self.assertIsNone(registry.load(self.root))
        self.assertFalse(registry.present(self.root))
        self.assertEqual(self.snapshot(), before)

    def test_without_a_registry_the_directory_is_never_judged(self):
        """Review fix (055d, low): no registry is 0.54, whatever the accounts directory's mode; with one, unsafe stays
        refused."""
        self.root.mkdir(parents=True, mode=0o755)
        self.root.chmod(0o755)
        before = self.snapshot()
        self.assertIsNone(registry.load(self.root))
        self.assertFalse(registry.admits('work', self.home))
        self.assertEqual(self.snapshot(), before)
        self.write(encoded(document({'work': {'user_id': None}})), root_mode=0o755)
        self.refused('registry_unsafe', lambda: registry.load(self.root))

    def test_an_owner_only_regular_file_loads(self):
        self.write(encoded(document({'work': {'user_id': None}})))
        self.assertTrue(registry.present(self.root))
        self.assertEqual(registry.load(self.root).aliases, ('work',))

    def test_unsafe_files_and_directories_refuse(self):
        data = encoded(document({'work': {'user_id': None}}))
        for mode in (0o644, 0o640, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.write(data, mode=mode)
                self.refused('registry_unsafe', lambda: registry.load(self.root))
        self.write(data, root_mode=0o755)
        self.refused('registry_unsafe', lambda: registry.load(self.root))
        self.root.chmod(0o700)
        path = self.root / registry.REGISTRY_NAME
        os.link(path, self.root / 'second-link')
        self.refused('registry_unsafe', lambda: registry.load(self.root))
        os.unlink(self.root / 'second-link')
        elsewhere = self.home / 'elsewhere.json'
        elsewhere.write_bytes(data)
        elsewhere.chmod(0o600)
        path.unlink()
        path.symlink_to(elsewhere)
        self.refused('registry_unsafe', lambda: registry.load(self.root))
        path.unlink()
        path.mkdir(mode=0o700)
        self.refused('registry_unsafe', lambda: registry.load(self.root))

    def test_a_symlinked_accounts_root_refuses(self):
        real = self.home / 'real'
        real.mkdir(mode=0o700)
        (real / registry.REGISTRY_NAME).write_bytes(encoded(document()))
        (real / registry.REGISTRY_NAME).chmod(0o600)
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(real)
        self.refused('registry_unsafe', lambda: registry.load(self.root))

    def test_an_oversized_file_refuses_before_parsing(self):
        self.write(encoded(document()) + b' ' * registry.MAX_REGISTRY_BYTES)
        self.refused('registry_invalid', lambda: registry.load(self.root))


class AliasGrammarAndBudgetTests(Fixture):
    def test_the_grammar_is_closed_and_default_is_reserved(self):
        for alias in ('default', '', 'Work', '1work', 'a' * 13, 'a-b', 'a.b', 'a b', 'wörk', 'ｗork',
                      'work\n', '../x', None, 7, b'work', SENTINEL):
            with self.subTest(alias=alias):
                self.refused('alias_invalid', lambda: registry.add_alias(alias, self.home, longest_tool='t'))
        self.assertEqual(self.snapshot(), [])

    def test_a_socket_path_beyond_the_macos_limit_refuses_before_anything_exists(self):
        deep = self.home / ('d' * 40)
        self.refused('alias_path_too_long', lambda: registry.add_alias('work', deep, longest_tool='t'))
        self.assertEqual(self.snapshot(), [])
        socket = config.alias_socket_path('a' * 12, self.home)
        self.assertLessEqual(len(os.fsencode(str(socket))), registry.SOCKET_PATH_LIMIT)
        self.assertEqual(socket, self.root / ('a' * 12) / 'run' / 'broker.sock')
        layout = len(str(config.alias_socket_path('w', Path('/'))))  # '/Library/.../w/run/broker.sock'
        exact = Path('/' + 'h' * (registry.SOCKET_PATH_LIMIT - 1 - layout))
        self.assertEqual(len(str(config.alias_socket_path('w', exact))), registry.SOCKET_PATH_LIMIT)
        registry.check_budgets('w', exact, longest_tool='t')  # Exactly at the limit: admitted.
        self.refused('alias_path_too_long', lambda: registry.check_budgets('w', Path(str(exact) + 'h'),
                                                                             longest_tool='t'))

    def test_a_tool_name_beyond_the_client_limit_refuses_before_anything_exists(self):
        prefix = len('mcp__telegram_search_work__')
        registry.check_budgets('work', self.home, longest_tool='t' * (registry.TOOL_NAME_LIMIT - prefix))
        self.refused('alias_name_too_long', lambda: registry.add_alias(
            'work', self.home, longest_tool='t' * (registry.TOOL_NAME_LIMIT - prefix + 1)))
        self.assertEqual(self.snapshot(), [])

    def test_the_budget_reads_this_releases_longest_tool_and_every_alias_fits_today(self):
        from telegram_search_mcp.contract import TOOL_OPERATIONS
        longest = registry.longest_tool_name()
        self.assertGreaterEqual(len(longest), max(len(name) for name in TOOL_OPERATIONS))
        self.assertLessEqual(len(f'mcp__telegram_search_{"a" * 12}__{longest}'), registry.TOOL_NAME_LIMIT)
        registry.check_budgets('a' * 12, self.home)

    def test_the_socket_budget_follows_the_alias_layout(self):
        with config.named_aliases_for_tests():
            self.assertEqual(config.alias_socket_path('work', self.home), AccountPaths._derive('work', self.home).socket)
        for alias in ('default', 'Work', None):
            with self.subTest(alias=alias), self.assertRaises(AccountPathsError):
                config.alias_socket_path(alias, self.home)


class MutationTests(Fixture):
    def test_the_first_add_creates_the_owner_only_root_and_registry(self):
        registry.add_alias('work', self.home, longest_tool='t')
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        path = self.root / registry.REGISTRY_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_bytes()), document({'work': {'user_id': None}}))
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()),
                         sorted([registry.LEASE_NAME, registry.REGISTRY_NAME]))  # No temporary is left behind.
        self.assertFalse((self.root / 'work').exists())  # The alias root is the installer's (phase D).

    def test_aliases_are_unique_and_bounded(self):
        for n in range(registry.MAX_ALIASES):
            registry.add_alias(f'a{n}', self.home, longest_tool='t')
        self.refused('alias_exists', lambda: registry.add_alias('a0', self.home, longest_tool='t'))
        self.refused('registry_full', lambda: registry.add_alias('extra', self.home, longest_tool='t'))
        self.assertEqual(len(registry.load(self.root).aliases), registry.MAX_ALIASES)

    def test_removal_drops_the_alias_and_its_pin_and_keeps_the_registry(self):
        registry.add_alias('work', self.home, longest_tool='t')
        registry.pin_account(self.root, 'work', USER)
        registry.remove_alias('work', self.home)
        self.assertEqual(registry.load(self.root), registry.EMPTY)
        self.refused('alias_unknown', lambda: registry.remove_alias('work', self.home))
        self.refused('alias_invalid', lambda: registry.remove_alias('default', self.home))
        other = short_home(self)
        self.refused('alias_unknown', lambda: registry.remove_alias('work', other))
        self.assertEqual(list(other.iterdir()), [])

    def test_a_held_lease_bounds_the_wait_then_refuses_busy(self):
        registry.add_alias('work', self.home, longest_tool='t')
        before = (self.root / registry.REGISTRY_NAME).read_bytes()
        descriptor = os.open(self.root / registry.LEASE_NAME, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        waits = []
        self.refused('registry_busy', lambda: registry.add_alias('home', self.home, longest_tool='t',
                                                                 sleeper=waits.append))
        self.assertEqual(len(waits), registry.LEASE_ATTEMPTS - 1)
        self.assertEqual((self.root / registry.REGISTRY_NAME).read_bytes(), before)

    def test_a_failed_write_keeps_the_previous_registry_and_leaves_no_temporary(self):
        registry.add_alias('work', self.home, longest_tool='t')
        before = (self.root / registry.REGISTRY_NAME).read_bytes()
        with mock.patch.object(registry.os, 'rename', side_effect=OSError('injected')):
            self.refused('registry_unsafe', lambda: registry.add_alias('home', self.home, longest_tool='t'))
        self.assertEqual((self.root / registry.REGISTRY_NAME).read_bytes(), before)
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()),
                         sorted([registry.LEASE_NAME, registry.REGISTRY_NAME]))

    def test_a_temporary_a_killed_write_left_is_removed_by_the_next_write_and_a_lookalike_stays(self):
        """0.55 phase D (account-add and account-remove resume after a kill anywhere): under the lease no writer holds
        a temporary, so the next write removes our exact random name; anything else in the directory stays."""
        registry.add_alias('work', self.home, longest_tool='t')
        left = self.root / ('.registry.json.' + 'a' * 32 + '.tmp')
        left.write_bytes(b'{"version": 1}')
        left.chmod(0o600)
        lookalikes = {'.registry.json.short.tmp': 0o600, '.registry.json.' + 'b' * 32 + '.tmp.keep': 0o600,
                      '.registry.json.' + 'c' * 32 + '.tmp': 0o644}
        for name, mode in lookalikes.items():
            (self.root / name).write_bytes(b'x')
            (self.root / name).chmod(mode)
        registry.add_alias('home', self.home, longest_tool='t')
        self.assertFalse(left.exists())
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()),
                         sorted([registry.LEASE_NAME, registry.REGISTRY_NAME, *lookalikes]))

    def test_an_unsafe_registry_is_never_overwritten(self):
        self.write(encoded(document({'work': {'user_id': None}})), mode=0o644)
        self.refused('registry_unsafe', lambda: registry.add_alias('home', self.home, longest_tool='t'))
        self.assertEqual(stat.S_IMODE((self.root / registry.REGISTRY_NAME).stat().st_mode), 0o644)


class PinTests(Fixture):
    def pins(self):
        value = json.loads((self.root / registry.REGISTRY_NAME).read_bytes())
        return {alias: entry['user_id'] for alias, entry in value['aliases'].items()}, value['default_user_id']

    def test_without_a_registry_nothing_is_pinned_or_created(self):
        registry.pin_account(self.root, 'default', USER)
        with self.assertRaises(registry.RegistryError) as caught:  # a named alias exists only in the registry
            registry.pin_account(self.root, 'work', USER)
        self.assertEqual(caught.exception.code, 'alias_unavailable')
        self.assertEqual(self.snapshot(), [])

    def test_the_first_authorized_user_is_pinned_and_then_required(self):
        registry.add_alias('work', self.home, longest_tool='t')
        registry.pin_account(self.root, 'default', OTHER)
        registry.pin_account(self.root, 'work', USER)
        self.assertEqual(self.pins(), ({'work': USER}, OTHER))
        before = (self.root / registry.REGISTRY_NAME).read_bytes()
        registry.pin_account(self.root, 'work', USER)  # The same user again: nothing is written.
        registry.pin_account(self.root, 'default', OTHER)
        self.assertEqual((self.root / registry.REGISTRY_NAME).read_bytes(), before)
        self.refused('account_mismatch', lambda: registry.pin_account(self.root, 'work', USER + 2))
        self.refused('account_mismatch', lambda: registry.pin_account(self.root, 'default', USER + 2))
        self.assertEqual((self.root / registry.REGISTRY_NAME).read_bytes(), before)

    def test_one_user_under_two_aliases_refuses_the_default_included(self):
        registry.add_alias('work', self.home, longest_tool='t')
        registry.add_alias('home', self.home, longest_tool='t')
        registry.pin_account(self.root, 'default', USER)
        self.refused('account_duplicate', lambda: registry.pin_account(self.root, 'work', USER))
        registry.pin_account(self.root, 'work', OTHER)
        self.refused('account_duplicate', lambda: registry.pin_account(self.root, 'home', OTHER))
        self.refused('account_duplicate', lambda: registry.pin_account(self.root, 'home', USER))
        self.assertEqual(self.pins(), ({'home': None, 'work': OTHER}, USER))

    def test_an_alias_pinned_first_blocks_the_unpinned_default_with_the_same_user(self):
        registry.add_alias('work', self.home, longest_tool='t')
        registry.pin_account(self.root, 'work', USER)
        self.refused('account_duplicate', lambda: registry.pin_account(self.root, 'default', USER))
        self.assertEqual(self.pins(), ({'work': USER}, None))

    def test_an_unregistered_alias_and_an_invalid_user_id_refuse(self):
        registry.add_alias('work', self.home, longest_tool='t')
        before = (self.root / registry.REGISTRY_NAME).read_bytes()
        self.refused('alias_unavailable', lambda: registry.pin_account(self.root, 'home', USER))
        self.refused('alias_unavailable', lambda: registry.pin_account(self.root, 'Work', USER))
        for user_id in (0, -1, 2**53, True, str(USER), None, 1.5):
            with self.subTest(user_id=user_id):
                self.refused('account_mismatch', lambda: registry.pin_account(self.root, 'work', user_id))
        self.assertEqual((self.root / registry.REGISTRY_NAME).read_bytes(), before)

    def test_a_pin_rechecks_under_the_lease(self):
        registry.add_alias('work', self.home, longest_tool='t')
        loaded = registry.load(self.root)
        # Another writer pinned the same user under the default between this check and the lease.
        registry.pin_account(self.root, 'default', USER)
        with mock.patch.object(registry, 'load', return_value=loaded):
            self.refused('account_duplicate', lambda: registry.pin_account(self.root, 'work', USER))
        self.assertEqual(self.pins(), ({'work': None}, USER))


class ForAliasTests(Fixture):
    def test_a_registered_alias_is_admitted_with_its_own_layout(self):
        registry.add_alias('work', self.home, longest_tool='t')
        before = self.snapshot()
        work = AccountPaths.for_alias('work', self.home)
        self.assertEqual(self.snapshot(), before)  # Admission reads the registry and creates nothing.
        with config.named_aliases_for_tests():
            self.assertEqual(work, AccountPaths._derive('work', self.home))
        self.assertEqual(work.root, self.root / 'work')
        self.assertEqual(work.accounts_root, self.root)
        self.assertEqual(AccountPaths.for_alias('default', self.home).accounts_root, self.root)

    def test_an_unregistered_alias_refuses_and_creates_nothing(self):
        for setup in (lambda: None, lambda: registry.add_alias('home', self.home, longest_tool='t')):
            setup()
            before = self.snapshot()
            with self.assertRaises(AccountPathsError) as caught:
                AccountPaths.for_alias('work', self.home)
            self.assertEqual(caught.exception.code, 'alias_unavailable')
            self.assertEqual(self.snapshot(), before)

    def test_an_invalid_registry_refuses_with_its_own_code(self):
        self.write(b'{"version": 2}')
        self.refused('registry_invalid', lambda: AccountPaths.for_alias('work', self.home))
        AccountPaths.for_alias('default', self.home)  # The default never reads the registry.

    def test_a_registered_alias_beyond_the_socket_budget_refuses(self):
        deep = self.home / ('d' * 40)
        root = config.accounts_root(deep)
        root.mkdir(parents=True, mode=0o700)
        (root / registry.REGISTRY_NAME).write_bytes(encoded(document({'work': {'user_id': None}})))
        (root / registry.REGISTRY_NAME).chmod(0o600)
        self.refused('alias_path_too_long', lambda: AccountPaths.for_alias('work', deep))

    def test_admission_lasts_only_for_its_own_construction(self):
        registry.add_alias('work', self.home, longest_tool='t')
        work = AccountPaths.for_alias('work', self.home)
        for build in (lambda: AccountPaths._derive('work', self.home), lambda: work.relocated(self.home / 'x'),
                      lambda: dataclasses.replace(work), lambda: AccountPaths(**dataclasses.asdict(work))):
            with self.assertRaises(AccountPathsError):
                build()


if __name__ == '__main__':
    unittest.main()
