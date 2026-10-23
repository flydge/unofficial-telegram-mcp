"""0.55 phase D (spec 3D, criterion 6): `account-add` and `account-remove`, small journaled installer transactions.

Synthetic installations over disposable homes: every release is a synthetic admitted tree, and launchctl, the
claude CLI, the default broker's check and the alias broker's pending-send probe are fakes. Nothing reaches the
real ~/Library, a client configuration, the Keychain, launchd or Telegram. The socket-path budget is the
registry's (tests/test_account_registry.py); here it is widened only because a disposable home's path is long, and
one test applies it unchanged.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import threading
import time
import unittest
from unittest import mock

import tomlkit

from test_bundle_upgrade import ChangeHarness, FakeLaunchctl, NEIGHBOUR_CODEX
from telegram_search_mcp import account_registry, bundle_install, config


# What account-add leaves at a new alias's place: its root, owner-only and empty but for its run directory (review fix
# 055d3: a login takes its connect.lock only inside an existing root and never creates one).
ADDED_ROOT = {'run': 'dir'}
# What a later installer transaction leaves at the root of an alias that never logged in: also the empty connect.lock it
# held for its run, as a login creates it first (review fix 055d2, `_LoginLocks`); nothing of a session.
LOCK_ONLY = {'run': 'dir', 'run/connect.lock': hashlib.sha256(b'').hexdigest()}


def tree(path):
    """{relative path: sha256 or 'dir'} of everything under `path`."""
    out = {}
    for parent, dirs, files in os.walk(path):
        for name in dirs:
            out[str((Path(parent) / name).relative_to(path))] = 'dir'
        for name in files:
            child = Path(parent) / name
            out[str(child.relative_to(path))] = hashlib.sha256(child.read_bytes()).hexdigest()
    return out


class AccountHarness(ChangeHarness):
    """An installed release with every client present, a session and a loaded default broker, plus a `work` alias's
    broker and connect jobs in the same fake launchd domain.

    The installed release is one that knows aliases (`BASE_VERSION`), since account-add and account-remove run only
    from a current release of 0.55 or newer (review fix 055d); a test that needs an older one sets its own."""

    ALIAS = 'work'
    BASE_VERSION = '0.55.0.dev0'

    def setUp(self):
        super().setUp()
        widened = mock.patch.object(account_registry, 'SOCKET_PATH_LIMIT', 4096)
        widened.start()
        self.addCleanup(widened.stop)
        self.jobs = config.alias_jobs(self.ALIAS, self.home)
        self.connect_plist = self.jobs.root / 'run' / (self.jobs.connect_label + '.plist')
        self.launchctl = FakeLaunchctl(self.plist, self.base / 'launchd-state.json',
                                       others={self.jobs.broker_label: self.jobs.broker_plist,
                                               self.jobs.connect_label: self.connect_plist})
        self.checks, self.check_answers = [], []
        self.alias_probes, self.alias_answers = [], []

    def old(self):
        """The installed release, built and installed as `BASE_VERSION`."""
        with mock.patch('telegram_search_mcp.__version__', self.BASE_VERSION):
            packet = self.build('a', version=self.BASE_VERSION)
            return packet, self.install(packet)

    def session(self, pin=None, capabilities='["read", "artifacts", "send"]'):
        """The default's session, its runtime.toml pinning the installed release (`BASE_VERSION`) unless told."""
        return super().session(self.BASE_VERSION if pin is None else pin, capabilities)

    def default_check(self, home, python, app):
        self.checks.append((python, app))
        return self.check_answers.pop(0) if self.check_answers else 'ready'

    def alias_pending(self, home, python, app, socket, policy):
        self.alias_probes.append((python, app, socket, policy))
        return self.alias_answers.pop(0) if self.alias_answers else 'none'

    def account(self, action, alias=None, **kwargs):
        module = self.module()
        self.claude()
        kwargs.setdefault('claude_candidates', (self.claude_cli,))
        kwargs.setdefault('claude_runner', self.claude())
        kwargs.setdefault('launchctl', self.launchctl)
        if action == 'account_add':
            kwargs.setdefault('default_check', self.default_check)
        kwargs.setdefault('alias_pending', self.alias_pending)
        if 'release_root' not in kwargs:
            kwargs['release_root'] = self.running_release()
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return getattr(module, action)(self.runtime, alias=alias or self.ALIAS, user_home=self.home, **kwargs)

    def running_release(self):
        """The release an account action runs from, as the launcher names it: the current one (None, an in-process
        call, while current.json cannot be read)."""
        try:
            return Path(json.loads((self.runtime / 'current.json').read_bytes())['release_root'])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def refused(self, action, code, alias=None, **kwargs):
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.account(action, alias, **kwargs)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    # --- state -------------------------------------------------------------------------------------------------

    @property
    def release(self):
        return Path(self.pointer()['release_root'])

    def alias_entry(self, alias=None):
        alias = alias or self.ALIAS
        return self.commands(self.release)[0], self.commands(self.release)[1] + ['--account', alias]

    def registry(self):
        value = account_registry.load(config.accounts_root(self.home))
        return None if value is None else value.aliases

    def sidecar(self):
        path = self.runtime / 'accounts.json'
        return json.loads(path.read_bytes()) if path.exists() else None

    def codex_servers(self):
        return tomlkit.parse((self.codex / 'config.toml').read_text()).get('mcp_servers', {})

    def desktop_servers(self):
        return json.loads(self.desktop_config.read_text())['mcpServers']

    def installer_files(self):
        """receipt.json, current.json and the default's plist and entries: never changed by an account action."""
        return {'receipt': (self.runtime / 'receipt.json').read_bytes(),
                'pointer': (self.runtime / 'current.json').read_bytes(), 'plist': self.plist.read_bytes(),
                'loaded': self.launchctl.loaded, 'codex': self.codex_servers().get('telegram_search'),
                'claude': self.claude().entry, 'desktop': self.desktop_servers().get('telegram_search')}

    def normal(self, value):
        """`value` as JSON with this fixture's base and the hashes that bind its paths masked."""
        import re
        text = json.dumps(value, sort_keys=True, default=str).replace(str(self.base), '<base>')
        return re.sub(r'"[0-9a-f]{64}"', '"<sha256>"', text)

    def account_journals(self):
        return sorted(self.runtime.glob('transactions/account_*/journal.json'))

    def snapshot(self):
        """Everything an account action could touch, for 'nothing changed' checks."""
        state = {'codex': (self.codex / 'config.toml').read_bytes(), 'desktop': self.desktop_config.read_bytes(),
                 'claude': self.claude().path.read_bytes() if self.claude().path.exists() else None,
                 'launchd': self.launchctl.loaded, 'journals': self.account_journals(),
                 'accounts': tree(config.accounts_root(self.home)) if config.accounts_root(self.home).exists() else None,
                 'sidecar': self.sidecar()}
        return state

    def alias_session(self):
        """What a finished login leaves for the alias: its root with a session, policy and run directory (with the
        connect.lock every login holds for its run), its broker plist (the release's exact alias definition) loaded,
        and its connect job still registered but idle, its helper exited."""
        for path in (self.jobs.root, self.jobs.root / 'tdlib', self.jobs.root / 'run'):
            path.mkdir(mode=0o700, exist_ok=True)
        (self.jobs.root / 'tdlib' / 'td.binlog').write_bytes(b'alias session bytes')
        (self.jobs.root / 'runtime.toml').write_bytes(b'config_version = 1\nenabled_capabilities = ["read"]\n')
        (self.jobs.root / 'run' / 'connect.lock').write_bytes(b'')
        for path in (self.jobs.root / 'tdlib' / 'td.binlog', self.jobs.root / 'runtime.toml',
                     self.jobs.root / 'run' / 'connect.lock'):
            path.chmod(0o600)
        payload = self.module()._service_payload(self.release, self.jobs.broker_label, self.ALIAS)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        self.launchctl.load(self.jobs.broker_label)
        self.connect_plist.write_bytes(plistlib.dumps({'Label': self.jobs.connect_label,
                                                       'ProgramArguments': [payload['ProgramArguments'][0], 'connect']}))
        self.connect_plist.chmod(0o600)
        self.launchctl.load(self.jobs.connect_label)
        self.launchctl.idle(self.jobs.connect_label)
        return tree(self.jobs.root)

    def foreign_root(self):
        """A directory at the alias's root that no account-add created (the accounts tree itself owner-only)."""
        for path in reversed([self.jobs.root, *self.jobs.root.parents]):
            if path.is_relative_to(self.home) and not path.exists():
                path.mkdir(mode=0o700)

    def added(self, **kwargs):
        self.full()
        self.claude()
        return self.account('account_add', **kwargs)


class AccountAddTests(AccountHarness, unittest.TestCase):
    def test_add_registers_the_alias_and_writes_only_its_own_entries(self):
        self.full()
        before = self.installer_files()
        result = self.account('account_add')
        command, arguments = self.alias_entry()
        self.assertEqual({k: result[k] for k in ('status', 'account', 'server_name', 'codex', 'claude_code',
                                                 'claude_desktop', 'default_check', 'next_action')},
                         {'status': 'account_added', 'account': 'work', 'server_name': 'telegram_search_work',
                          'codex': 'registered', 'claude_code': 'registered', 'claude_desktop': 'registered',
                          'default_check': 'ready', 'next_action': 'run_activation_then_wait'})
        self.assertEqual(result['activation_arguments'][-3:], ['connect', '--account', 'work'])
        self.assertEqual(self.registry(), ('work',))
        codex = self.codex_servers()['telegram_search_work']
        self.assertEqual((codex['command'], codex['args'], codex['required']), (command, arguments, False))
        self.assertEqual(self.claude().entry_of('telegram_search_work'),
                         {'type': 'stdio', 'command': command, 'args': arguments})
        self.assertEqual(self.claude().calls[-1][:5], ['mcp', 'add-json', '--scope', 'user', 'telegram_search_work'])
        self.assertEqual(list(self.desktop_servers()), ['weather', 'telegram_search', 'telegram_search_work'])
        self.assertEqual(self.desktop_servers()['telegram_search_work'], {'command': command, 'args': arguments})
        self.assertTrue((self.codex / 'config.toml').read_bytes().startswith(NEIGHBOUR_CODEX))
        self.assertEqual(self.installer_files(), before)  # receipt.json, current.json and the default's entries.
        # No login: its root as account-add creates it (review fix 055d3), owner-only, and no broker definition.
        self.assertEqual(tree(self.jobs.root), ADDED_ROOT)
        self.assertEqual(oct(self.jobs.root.stat().st_mode & 0o777), '0o700')
        self.assertFalse(self.jobs.broker_plist.exists())
        self.assertEqual(self.checks, [(self.release / 'python/bin/python3', self.release / 'app')])
        side = self.sidecar()
        self.assertEqual(set(side), {'schema_version', 'accounts'})
        record = side['accounts']['work']
        self.assertEqual((record['transaction'], record['release_root'], record['claude_code'],
                          record['claude_desktop']), ('account_1', str(self.release), 'registered', 'registered'))
        self.assertEqual(record['codex']['post_subtree_hash'],
                         hashlib.sha256(bundle_install.owned_subtree((self.codex / 'config.toml').read_bytes(),
                                                                     'telegram_search_work')).hexdigest())
        self.assertEqual(record['claude_desktop_ownership']['client'], 'claude-desktop')
        self.assertEqual(oct((self.runtime / 'accounts.json').stat().st_mode & 0o777), '0o600')
        # A retry after it finished returns its result without another step.
        calls = len(self.claude().calls)
        self.assertEqual(self.account('account_add'), result)
        self.assertEqual((len(self.claude().calls), len(self.account_journals())), (calls, 1))

    def test_a_second_alias_keeps_the_first_and_the_sidecar_lists_both(self):
        self.added()
        self.account('account_add', 'play')
        self.assertEqual(self.registry(), ('play', 'work'))
        self.assertEqual(sorted(self.sidecar()['accounts']), ['play', 'work'])
        self.assertEqual({'telegram_search', 'telegram_search_work', 'telegram_search_play'} - set(self.codex_servers()),
                         set())
        self.assertEqual(list(self.desktop_servers()),
                         ['weather', 'telegram_search', 'telegram_search_work', 'telegram_search_play'])

    def test_plugin_mode_writes_a_user_scope_entry_with_its_own_name_and_skip_mode_none(self):
        for mode in ('plugin', 'skip'):
            with self.subTest(mode=mode):
                self.setUp()
                self.codex_present()
                self.desktop_present()
                (self.home / '.claude').mkdir(mode=0o755)
                self.session()
                with mock.patch('telegram_search_mcp.__version__', self.BASE_VERSION):
                    self.install(self.build('a', version=self.BASE_VERSION), claude_code=mode)
                result = self.account('account_add')
                entry = self.claude().entry_of('telegram_search_work')
                self.assertIsNone(self.claude().entry)  # The plugin stays the default's; no default user entry.
                if mode == 'plugin':
                    self.assertEqual((result['claude_code'], entry['args'][-2:]), ('registered', ['--account', 'work']))
                else:
                    self.assertEqual((result['claude_code'], entry), ('skipped', None))
                self.assertNotIn('claude_code_user_entry', result)

    def test_every_refusal_comes_before_anything_is_written(self):
        self.full()
        cases = [
            ('Work', 'alias_invalid', None), ('default', 'alias_invalid', None), ('a' * 13, 'alias_invalid', None),
            ('work', 'registration_conflict',
             lambda: self.codex_present(NEIGHBOUR_CODEX + b'\n[mcp_servers.telegram_search_work]\ncommand = "/x"\n')),
            ('work', 'claude_registration_conflict',
             lambda: self.claude().set_entry('telegram_search_work', {'type': 'stdio', 'command': '/x', 'args': []})),
            ('work', 'claude_desktop_registration_conflict', lambda: self.desktop_present(
                '{"mcpServers": {"telegram_search_work": {"command": "/x", "args": []}}}\n')),
            ('work', 'claude_desktop_config_unsupported', lambda: self.desktop_present('{"zoom": 1.50}\n')),
            ('work', 'runtime_conflict', self.foreign_root),
            ('work', 'service_conflict', lambda: self.jobs.broker_plist.write_bytes(b'foreign')),
            ('work', 'service_conflict', lambda: self.plist.write_bytes(
                plistlib.dumps({**plistlib.loads(self.plist.read_bytes()), 'RunAtLoad': False}))),
        ]
        for alias, code, arrange in cases:
            with self.subTest(alias=alias, code=code):
                self.setUp()
                self.full()
                self.claude()
                if arrange is not None:
                    arrange()
                before = self.snapshot()
                self.refused('account_add', code, alias)
                self.assertEqual(self.snapshot(), before)
                self.assertIsNone(self.registry())
                self.assertEqual(self.checks, [])

    def test_the_registry_budget_refuses_before_any_directory_exists(self):
        self.full()
        before = self.snapshot()
        with mock.patch.object(account_registry, 'SOCKET_PATH_LIMIT', 103):
            self.refused('account_add', 'alias_path_too_long', 'abcdefghijkl')
        self.assertFalse(config.accounts_root(self.home).exists())
        self.assertEqual(self.snapshot(), before)

    def test_an_existing_alias_and_a_full_registry_refuse(self):
        self.added()
        self.account('account_add', 'play')
        before = self.snapshot()
        self.refused('account_add', 'alias_exists')
        self.assertEqual(self.snapshot(), before)
        for alias in ('a', 'b', 'c', 'd', 'e', 'f'):
            self.account('account_add', alias)
        self.refused('account_add', 'registry_full', 'h')

    def test_the_default_check_runs_only_on_a_loaded_default_and_an_unanswered_one_resumes(self):
        self.full(loaded=False)
        self.assertEqual(self.account('account_add')['default_check'], 'not_loaded')
        self.assertEqual(self.checks, [])
        self.setUp()
        self.full()
        self.check_answers = ['unknown']
        self.refused('account_add', 'default_check_failed')
        self.assertEqual(self.registry(), ('work',))  # Registered first, so the check pins the default.
        self.assertIsNone(self.sidecar())
        error = self.refused('account_remove', 'transaction_pending')
        self.assertEqual((error.pending_action, error.pending_account), ('account-add', 'work'))
        self.check_answers = ['blocked']
        self.assertEqual(self.account('account_add')['default_check'], 'blocked')
        self.assertEqual(len(self.checks), 2)
        self.assertEqual(len(self.account_journals()), 1)

    def test_an_unfinished_change_blocks_an_account_action_and_an_unfinished_account_action_blocks_a_change(self):
        self.full()
        newer = self.build('b', launcher=b'# second release\n')

        def crash(stage):
            if stage == 'registration':
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.install(newer, checkpoint=crash)
        error = self.refused('account_add', 'transaction_pending')
        self.assertEqual((error.pending_action, error.pending_account), ('upgrade', None))
        self.install(newer)
        with self.assertRaises(KeyboardInterrupt):
            self.account('account_add', checkpoint=crash)
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('rollback')
        self.assertEqual((str(caught.exception), caught.exception.pending_action, caught.exception.pending_account),
                         ('transaction_pending', 'account-add', 'work'))
        error = self.refused('account_add', 'transaction_pending', 'play')
        self.assertEqual((error.pending_action, error.pending_account), ('account-add', 'work'))
        self.assertEqual(self.account('account_add')['status'], 'account_added')

    def test_a_kill_at_every_checkpoint_resumes_without_double_effects(self):
        reference = None
        stages = ['prepared', 'registry', 'default_check', 'registration', 'recorded:claude_add',
                  'published:claude_add', 'claude_code', 'recorded:claude_desktop_add', 'published:claude_desktop_add',
                  'claude_desktop', 'created:packet:accounts_sidecar', 'recorded:accounts_sidecar',
                  'published:accounts_sidecar', 'sidecar']
        for stage in [None, *stages]:
            with self.subTest(stage=stage):
                self.setUp()
                self.full()
                self.claude()
                if stage is not None:
                    def crash(value, stage=stage):
                        if value == stage:
                            raise KeyboardInterrupt
                    with self.assertRaises(KeyboardInterrupt):
                        self.account('account_add', checkpoint=crash)
                result = self.account('account_add')
                state = self.normal((self.registry(), self.sidecar(), self.codex_servers().unwrap(),
                                     self.desktop_servers(), len(self.account_journals())))
                window = stage == 'recorded:claude_add'  # Write-ahead, never retried: `failed`, entry absent.
                expected = 'failed' if window else 'registered'
                self.assertEqual(result['claude_code'], expected)
                self.assertEqual(self.claude().calls.count(['mcp', 'add-json', '--scope', 'user', 'telegram_search_work',
                                                            self.claude().calls[-1][-1]]) if not window else
                                 [c[4] for c in self.claude().calls].count('telegram_search_work'), 0 if window else 1)
                if window:  # The single accepted difference: that outcome.
                    state = state.replace('"claude_code": "failed"', '"claude_code": "registered"')
                if reference is None:
                    reference = state
                self.assertEqual(state, reference)
                self.assertEqual(list(self.runtime.glob('transactions/account_1/*-packet-*')),
                                 list(self.runtime.glob('transactions/account_1/metadata-packet-accounts_sidecar-*')))


class AccountRemoveTests(AccountHarness, unittest.TestCase):
    def test_remove_retires_the_alias_and_moves_its_session_aside(self):
        self.added()
        session = self.alias_session()
        before = self.installer_files()
        result = self.account('account_remove')
        moved = Path(result.pop('session_moved_to'))
        self.assertEqual(result, {'status': 'account_removed', 'account': 'work', 'service': 'removed',
                                  'connect_job': 'stopped', 'codex': 'removed', 'claude_code': 'removed',
                                  'claude_desktop': 'removed', 'session': 'moved', 'next_action': 'restart_mcp_clients'})
        self.assertEqual(moved.parent, config.accounts_root(self.home))
        self.assertRegex(moved.name, r'\Aremoved-work-[0-9a-f]{32}\Z')
        self.assertEqual(tree(moved), session)  # Never deleted: the whole root, byte for byte.
        self.assertFalse(self.jobs.root.exists() or self.jobs.broker_plist.exists())
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertIsNone(self.launchctl.job(self.jobs.connect_label))
        self.assertEqual(self.alias_probes, [(self.release / 'python/bin/python3', self.release / 'app',
                                              self.jobs.socket, self.jobs.policy)] * 2)
        self.assertEqual(self.registry(), ())
        self.assertNotIn('telegram_search_work', self.codex_servers())
        self.assertIsNone(self.claude().entry_of('telegram_search_work'))
        self.assertEqual(list(self.desktop_servers()), ['weather', 'telegram_search'])
        self.assertIsNone(self.sidecar())  # The last alias's sidecar is retired into its transaction, not deleted.
        retired = self.runtime / 'transactions' / 'account_2' / 'accounts.retired'
        self.assertEqual(sorted(json.loads(retired.read_bytes())['accounts']), ['work'])
        self.assertEqual(self.installer_files(), before)
        # A retry after it finished returns its result; a later add of the same alias starts afresh.
        self.assertEqual({k: v for k, v in self.account('account_remove').items() if k != 'session_moved_to'}, result)
        self.assertEqual(self.account('account_add')['status'], 'account_added')
        self.assertEqual(tree(self.jobs.root), ADDED_ROOT)  # A new, empty root; the moved session stays as it was.
        self.assertEqual(tree(moved), session)

    def test_rename_is_a_remove_and_an_add(self):
        self.added()
        self.account('account_remove')
        self.account('account_add', 'office')
        self.assertEqual(self.registry(), ('office',))
        self.assertEqual(sorted(self.sidecar()['accounts']), ['office'])
        self.assertEqual({n for n in self.codex_servers() if n.startswith('telegram_search')},
                         {'telegram_search', 'telegram_search_office'})

    def test_removing_one_alias_keeps_the_other(self):
        self.added()
        self.account('account_add', 'play')
        self.account('account_remove')
        self.assertEqual(self.registry(), ('play',))
        self.assertEqual(sorted(self.sidecar()['accounts']), ['play'])
        self.assertIn('telegram_search_play', self.codex_servers())
        self.assertEqual(list(self.desktop_servers()), ['weather', 'telegram_search', 'telegram_search_play'])

    def test_an_alias_never_logged_in_has_nothing_to_stop_and_only_its_lock_to_move(self):
        self.added()
        result = self.account('account_remove')
        # Review fix 055d2: the transaction held the alias's connect.lock (created as a login creates it first), so the
        # root it moves aside holds only that, never a session.
        self.assertEqual((result['service'], result['connect_job'], result['session']),
                         ('absent', 'not_loaded', 'moved'))
        self.assertEqual(tree(Path(result['session_moved_to'])), LOCK_ONLY)
        self.assertFalse(self.jobs.root.exists())
        self.assertEqual(self.alias_probes, [])

    def test_every_refusal_comes_before_anything_changes(self):
        def edited_codex():
            config_path = self.codex / 'config.toml'
            config_path.write_bytes(config_path.read_bytes().replace(b'tool_timeout_sec = 600\nenabled_tools',
                                                                     b'tool_timeout_sec = 601\nenabled_tools', 2))
        cases = [
            ('play', 'alias_unknown', None),
            ('work', 'pending_send_unresolved', lambda: self.alias_answers.extend(['pending'])),
            ('work', 'pending_send_unresolved', lambda: self.alias_answers.extend(['unknown'])),
            ('work', 'registration_conflict', edited_codex),
            ('work', 'claude_registration_conflict', lambda: self.claude().set_entry(
                'telegram_search_work', {'type': 'stdio', 'command': '/x', 'args': []})),
            ('work', 'claude_desktop_registration_conflict', lambda: self.desktop_config.write_text(
                self.desktop_config.read_text().replace('"--account",\n        "work"', '"--account",\n        "w"'))),
            ('work', 'service_conflict', lambda: self.jobs.broker_plist.write_bytes(
                plistlib.dumps({**plistlib.loads(self.jobs.broker_plist.read_bytes()), 'KeepAlive': False}))),
        ]
        for alias, code, arrange in cases:
            with self.subTest(alias=alias, code=code):
                self.setUp()
                self.added()
                self.alias_session()
                if arrange is not None:
                    arrange()
                before, session = self.snapshot(), tree(self.jobs.root)
                self.refused('account_remove', code, alias)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(tree(self.jobs.root), session)
                self.assertEqual(self.launchctl.job(self.jobs.broker_label)[-2:], ['--account', 'work'])

    def test_a_broker_holding_the_aliases_lock_outside_launchd_is_never_moved_under(self):
        from telegram_search_mcp.session_owner import acquire_owner_lock
        self.added()
        self.alias_session()
        self.launchctl.set_job(self.jobs.broker_label, None)  # Not a launchd job: a process the owner started by hand.
        held = acquire_owner_lock(self.jobs.root / 'run' / 'broker.lock')
        try:
            before, session = self.snapshot(), tree(self.jobs.root)
            self.refused('account_remove', 'service_conflict')
            self.assertEqual((self.snapshot(), tree(self.jobs.root)), (before, session))
            # Started after the precheck: the move refuses, the transaction stays for the retry.
            os.close(held)
            held = None

            def start(stage):
                nonlocal held
                if stage == 'registry':
                    held = acquire_owner_lock(self.jobs.root / 'run' / 'broker.lock')
            self.refused('account_remove', 'service_conflict', checkpoint=start)
            self.assertTrue(self.jobs.root.exists())
            self.assertEqual(self.registry(), ())
        finally:
            if held is not None:
                os.close(held)
        self.assertEqual(self.account('account_remove')['session'], 'moved')

    def test_a_login_holding_the_connect_lock_refuses_and_none_starts_during_a_run(self):
        """Review fix (055d2, lead decision A): a login's connect.lock (an inline `connect` or `setup --account` in the
        owner's Terminal, no launchd job) refuses `login_in_progress` with nothing changed; during a run the transaction
        holds it, so a login started then cannot take it, at any checkpoint, fresh or resumed."""
        from telegram_search_mcp.session_owner import OwnerLeaseError, acquire_owner_lock
        self.added()
        self.alias_session()
        self.launchctl.set_job(self.jobs.connect_label, None)
        held = acquire_owner_lock(self.jobs.root / 'run' / 'connect.lock')
        try:
            before, session = self.snapshot(), tree(self.jobs.root)
            self.refused('account_remove', 'login_in_progress')
            self.assertEqual((self.snapshot(), tree(self.jobs.root)), (before, session))
        finally:
            os.close(held)
        refused = []

        def login(stage):
            if not self.jobs.root.exists():
                return  # Moved aside (after its registry entry went, so no login of it is admitted any more).
            try:
                os.close(acquire_owner_lock(self.jobs.root / 'run' / 'connect.lock'))
            except OwnerLeaseError as error:
                refused.append(error.code)
                if stage == 'registry':
                    raise KeyboardInterrupt  # Killed here: the retry holds it again from its start.
            else:
                refused.append('taken at ' + stage)
        with self.assertRaises(KeyboardInterrupt):
            self.account('account_remove', checkpoint=login)
        count = len(refused)
        result = self.account('account_remove', checkpoint=lambda stage: login(stage) if stage != 'registry' else None)
        self.assertEqual(result['session'], 'moved')
        self.assertGreater(count, 3)
        self.assertGreater(len(refused), count)
        self.assertEqual(set(refused), {'owner_busy'})

    def test_a_late_pending_send_refuses_before_the_bootout_and_the_retry_resumes(self):
        self.added()
        self.alias_session()
        self.alias_answers = ['none', 'pending']
        self.refused('account_remove', 'pending_send_unresolved')
        self.assertEqual(self.launchctl.job(self.jobs.broker_label)[-2:], ['--account', 'work'])
        self.assertIn('telegram_search_work', self.codex_servers())
        error = self.refused('account_add', 'transaction_pending', 'play')
        self.assertEqual((error.pending_action, error.pending_account), ('account-remove', 'work'))
        self.assertEqual(self.account('account_remove')['service'], 'removed')

    def test_an_entry_edited_after_the_precheck_refuses_and_the_retry_resumes_once_it_is_put_back(self):
        self.added()
        config_path = self.codex / 'config.toml'
        saved = config_path.read_bytes()

        def edit(stage):
            if stage == 'connect':
                config_path.write_bytes(saved.replace(b'tool_timeout_sec = 600\nenabled_tools = ["_manifest", '
                                                      b'"search_correspondence"]\n', b'tool_timeout_sec = 9\n'
                                                      b'enabled_tools = ["_manifest", "search_correspondence"]\n', 2))
        self.refused('account_remove', 'registration_conflict', checkpoint=edit)
        config_path.write_bytes(saved)
        self.assertEqual(self.account('account_remove')['codex'], 'removed')

    def test_a_kill_at_every_checkpoint_resumes_without_double_effects(self):
        stages = ['prepared', 'stopped:service', 'service', 'connect', 'recorded:codex', 'published:codex',
                  'registration', 'recorded:claude_remove', 'published:claude_remove', 'claude_code',
                  'recorded:claude_desktop_add', 'recorded:claude_desktop_write', 'published:claude_desktop_add',
                  'claude_desktop', 'registry', 'published:session', 'session', 'published:accounts_retired', 'sidecar']
        reference = None
        for stage in [None, *stages]:
            with self.subTest(stage=stage):
                self.setUp()
                self.added()
                session = self.alias_session()
                if stage is not None:
                    def crash(value, stage=stage):
                        if value == stage:
                            raise KeyboardInterrupt
                    with self.assertRaises(KeyboardInterrupt):
                        self.account('account_remove', checkpoint=crash)
                result = self.account('account_remove')
                moved = Path(result.pop('session_moved_to'))
                self.assertEqual(tree(moved), session)
                state = self.normal((result, self.registry(), self.sidecar(), self.launchctl.job(self.jobs.broker_label),
                                     self.codex_servers().unwrap(), self.claude().entry_of('telegram_search_work'),
                                     list(self.desktop_servers()), self.jobs.root.exists(),
                                     self.jobs.broker_plist.exists()))
                if reference is None:
                    reference = state
                self.assertEqual(state, reference)


class LoginRootTests(AccountHarness, unittest.TestCase):
    """Review fix (055d3, medium, R1): a login polling for the alias's connect.lock while account-remove held it (an
    earlier helper's terminal status in place, so `connect` waits out its linger) took it right after the move by
    creating `TelegramSearchMCP-accounts/<alias>/run/connect.lock` again (`acquire_owner_lock` created every missing
    parent), so account-add then refused `runtime_conflict` for good. A login now takes the lock only inside an existing
    root, reads the registry before and once it holds the lock, and refuses `alias_unavailable` with nothing created;
    account-add creates the root (owner-only, empty but for run/) right after its registry step."""

    def logged_in_new(self):
        """A logged-in alias's root, broker and connect job (`alias_session`) with an empty session directory, so a login
        of it is admitted (`setup_status`: new) and reaches the connect.lock."""
        self.added()
        self.alias_session()
        (self.jobs.root / 'tdlib' / 'td.binlog').unlink()

    def stages_before_registry(self):
        """Every checkpoint account-remove passes before its registry step, traced from one run."""
        self.logged_in_new()
        seen = []
        self.account('account_remove', checkpoint=seen.append)
        self.assertIn('stopped:service', seen)
        return seen[:seen.index('registry')]

    def login_paths(self):
        from telegram_search_mcp.enrollment import SetupPaths
        return SetupPaths.from_account(config.AccountPaths.for_alias(self.ALIAS, self.home))

    def polling_connect(self):
        """`connect --account` (the agent's), admitted now, while account-remove holds the alias's connect.lock: an
        earlier helper's terminal status makes `_enter` poll through the linger. Its sleeper blocks until `finish()`, so
        the transaction runs to its end between two polls; `finish()` returns connect's (value, code)."""
        from telegram_search_mcp import connect_cli
        from telegram_search_mcp.connect_flow import write_connect_status
        paths, now = self.login_paths(), time.time()
        write_connect_status(paths, 'failed', 'cancelled', updated_at=now, expires_at=now + 600)
        polling, released, out = threading.Event(), threading.Event(), []

        def sleeper(_):
            polling.set()
            released.wait(30)

        class Agent:  # A captured agent shell, not native Terminal: connect would start the launchd helper.
            def require_secure(self):
                raise RuntimeError('captured')

        def never(*_args, **_kwargs):
            raise AssertionError('no broker probe and no inline run')
        thread = threading.Thread(target=lambda: out.append(connect_cli.connect(
            paths, client_factory=never, terminal=Agent(), inline=never, runner=self.launchctl, uid=os.getuid(),
            sleeper=sleeper)))
        thread.start()
        self.assertTrue(polling.wait(10))  # Waiting for the lock the transaction holds.

        def finish():
            released.set()
            thread.join(30)
            self.assertFalse(thread.is_alive())
            return out[0]
        return finish

    def test_a_login_polling_through_account_remove_never_creates_its_root_again(self):
        for stage in self.stages_before_registry():
            with self.subTest(stage=stage):
                self.setUp()
                self.logged_in_new()
                finish = []

                def start(value, stage=stage):
                    if value == stage and not finish:
                        finish.append(self.polling_connect())
                self.assertEqual(self.account('account_remove', checkpoint=start)['session'], 'moved')
                self.assertEqual(finish[0](), ({'status': 'refused', 'code': 'alias_unavailable',
                                                'next_action': 'stop_and_report'}, 2))
                self.assertFalse(self.jobs.root.exists())
                self.assertIsNone(self.launchctl.job(self.jobs.connect_label))
                # The alias name can be added again: a new, empty root.
                self.assertEqual(self.account('account_add')['status'], 'account_added')
                self.assertEqual(tree(self.jobs.root), ADDED_ROOT)

    def test_setup_admitted_during_account_remove_never_creates_its_root_again(self):
        """The inline variant: `setup --account` in the owner's Terminal, admitted (the launcher's registry check) at a
        checkpoint before the registry step, reaching the connect.lock only after the move."""
        from test_onboarding import MemoryStore, Terminal
        from telegram_search_mcp.setup_wizard import run_setup
        for stage in self.stages_before_registry():
            for resume in (False, True):
                with self.subTest(stage=stage, resume=resume):
                    self.setUp()
                    self.logged_in_new()
                    admitted = []

                    def admit(value, stage=stage):
                        if value == stage and not admitted:
                            admitted.append(self.login_paths())
                    self.assertEqual(self.account('account_remove', checkpoint=admit)['session'], 'moved')

                    def never(*_args, **_kwargs):
                        raise AssertionError('no provider and no handoff')
                    terminal = Terminal()
                    result = run_setup(admitted[0], terminal, MemoryStore(), never, handoff=never, resume=resume,
                                       bundled_loader=lambda: None)
                    self.assertEqual((result.status, result.code, result.to_dict()['next_action'], terminal.prompts),
                                     ('pending', 'alias_unavailable', 'stop_and_report', []))
                    self.assertFalse(self.jobs.root.exists())
                    self.assertEqual(self.account('account_add')['status'], 'account_added')
                    self.assertEqual(tree(self.jobs.root), ADDED_ROOT)

    def test_account_add_creates_the_root_right_after_its_registry_step_and_a_kill_resumes(self):
        for stage in ('registry', 'root'):
            with self.subTest(stage=stage):
                self.setUp()
                self.full()
                self.claude()

                def die(value, stage=stage):
                    if value == stage:
                        raise KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.account('account_add', checkpoint=die)
                self.assertEqual(self.registry(), ('work',))
                self.assertEqual(self.jobs.root.exists(), stage == 'root')
                self.assertEqual(self.account('account_add')['status'], 'account_added')
                self.assertEqual(tree(self.jobs.root), ADDED_ROOT)

    def test_installer_locks_never_create_a_missing_root(self):
        """An alias the registry admits whose root is absent (moved by hand): no transaction creates it to hold its
        lock (`_LoginLocks`), since no login of it can start without it either."""
        self.added()
        os.rename(self.jobs.root, self.jobs.root.with_name('moved-by-hand'))
        locks = bundle_install._LoginLocks(self.launchctl, self.home, ['work']).__enter__()
        try:
            locks.complete()
            self.assertEqual(locks.held, set())
        finally:
            locks.__exit__()
        self.assertFalse(self.jobs.root.exists())


class LoadedAgainTests(AccountHarness, unittest.TestCase):
    """Review fix (055d, medium): a broker loaded again after account-remove's broker step (a still-loaded connect
    job's handoff, or a concurrent `setup --account`) is gated and stopped again, by every attempt until the root
    moves and once more right before the move, and its exact plist unlinked; a definition that is not the release's
    exact one is never removed. The connect job goes first, since its handoff loads the broker."""

    def setUp(self):
        super().setUp()
        from telegram_search_mcp.session_owner import acquire_owner_lock
        jobs, held = self.jobs, {}

        class Locking(FakeLaunchctl):
            """launchd whose loaded alias broker holds its broker.lock, as the running broker does, until booted out."""

            def set_job(self, label, value):
                super().set_job(label, value)
                if label == jobs.broker_label and value is None and 'fd' in held:
                    os.close(held.pop('fd'))
                elif label == jobs.broker_label and value is not None and 'fd' not in held:
                    held['fd'] = acquire_owner_lock(jobs.root / 'run' / 'broker.lock')
        self.launchctl = Locking(self.plist, self.base / 'launchd-state.json',
                                 others={self.jobs.broker_label: self.jobs.broker_plist,
                                         self.jobs.connect_label: self.connect_plist})
        self.addCleanup(lambda: os.close(held.pop('fd')) if 'fd' in held else None)

    def loaded_again(self, payload=None, *, load=True):
        """What a connect job's handoff does: the alias's broker definition written and loaded again."""
        payload = payload or self.module()._service_payload(self.release, self.jobs.broker_label, self.ALIAS)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        if load:
            self.launchctl.load(self.jobs.broker_label)

    def assertRetired(self, result, session):
        self.assertEqual((result['status'], result['service'], result['connect_job'], result['session']),
                         ('account_removed', 'removed', 'stopped', 'moved'))
        self.assertEqual(tree(Path(result['session_moved_to'])), session)
        self.assertFalse(self.jobs.root.exists() or self.jobs.broker_plist.exists())
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertEqual((self.registry(), self.sidecar()), ((), None))

    def test_the_connect_job_goes_before_the_broker(self):
        self.added()
        self.alias_session()
        self.account('account_remove')
        bootouts = [call[1].rpartition('/')[2] for call in self.launchctl.calls if call[0] == 'bootout']
        self.assertEqual(bootouts, [self.jobs.connect_label, self.jobs.broker_label])

    def test_a_broker_loaded_again_after_its_step_is_stopped_before_the_move(self):
        self.added()
        session = self.alias_session()

        def handoff(stage):
            if stage == 'service':
                self.loaded_again()
        self.assertRetired(self.account('account_remove', checkpoint=handoff), session)
        # Gated right before each bootout: the precheck, the broker step and the move's.
        self.assertEqual(len(self.alias_probes), 3)

    def test_a_retry_stops_a_broker_loaded_again_and_leaves_no_plist(self):
        from telegram_search_mcp.session_owner import acquire_owner_lock
        for case in ('loaded', 'booted out by hand'):
            with self.subTest(case=case):
                self.setUp()
                self.added()
                session = self.alias_session()
                stray = {}

                def started(stage):  # A broker started by hand holds its lock: the move refuses, the retry follows.
                    if stage == 'registry':
                        stray['fd'] = acquire_owner_lock(self.jobs.root / 'run' / 'broker.lock')
                self.refused('account_remove', 'service_conflict', checkpoint=started)
                os.close(stray.pop('fd'))
                self.assertEqual(self.registry(), ())
                self.loaded_again()  # A connect job still running hands off again.
                if case == 'booted out by hand':
                    self.launchctl.set_job(self.jobs.broker_label, None)
                self.assertRetired(self.account('account_remove'), session)

    def test_a_definition_that_is_not_the_releases_own_is_never_removed(self):
        self.added()
        self.alias_session()
        payload = self.module()._service_payload(self.release, self.jobs.broker_label, self.ALIAS)
        foreign = {**payload, 'KeepAlive': False}

        def written(stage):
            if stage == 'service':
                self.loaded_again(foreign, load=False)
        self.refused('account_remove', 'service_conflict', checkpoint=written)
        self.refused('account_remove', 'service_conflict')
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()), foreign)
        self.assertTrue(self.jobs.root.exists())


class AliasJobsTests(unittest.TestCase):
    """config.alias_jobs: what account-remove retires, by the alias's AccountPaths layout, without the registry."""

    def test_the_jobs_are_the_accounts_own_and_need_no_admission(self):
        home = Path('/fixture-home/sample-owner')
        with config.named_aliases_for_tests():
            work = config.AccountPaths._derive('work', home)
        jobs = config.alias_jobs('work', home)
        self.assertEqual((jobs.alias, jobs.root, jobs.socket, jobs.policy, jobs.broker_label, jobs.broker_plist,
                          jobs.connect_label),
                         (work.alias, work.root, work.socket, work.policy, work.broker_label, work.broker_plist,
                          work.connect_label))
        self.assertIsNone(account_registry.load(config.accounts_root(home)))  # Built with no registry at all.

    def test_only_a_named_alias_and_only_alias_jobs_build_one(self):
        for alias in ('default', 'Work', '', 'a' * 13, None):
            with self.subTest(alias=alias), self.assertRaises(config.AccountPathsError):
                config.alias_jobs(alias, Path('/fixture-home/sample-owner'))
        jobs = config.alias_jobs('work', Path('/fixture-home/sample-owner'))
        with self.assertRaises(config.AccountPathsError):
            config.AliasJobs(*(getattr(jobs, f) for f in ('alias', 'root', 'socket', 'policy', 'broker_label',
                                                         'broker_plist', 'connect_label')))
        self.assertNotIsInstance(jobs, config.AccountPaths)


class ConsistencyTests(AccountHarness, unittest.TestCase):
    def test_a_record_the_registry_does_not_list_refuses_and_each_aliases_own_action_repairs_it(self):
        self.added()
        account_registry.add_alias('play', self.home, longest_tool='t')  # Registered outside account-add.
        # Review fix (055d, high): an alias the registry lists alone (as an uninstall leaves them) is repairable, not
        # inconsistent: another alias's actions proceed beside it.
        self.assertEqual(self.account('account_add', 'office')['status'], 'account_added')
        self.assertEqual(self.account('account_remove', 'office')['status'], 'account_removed')
        # account-add of the registry-only alias registers its entries; its registry entry stays as it was.
        result = self.account('account_add', 'play')
        self.assertEqual(result['status'], 'account_added')
        self.assertEqual(sorted(self.sidecar()['accounts']), ['play', 'work'])
        account_registry.add_alias('gone', self.home, longest_tool='t')
        self.assertEqual(self.account('account_remove', 'gone')['codex'], 'absent')
        self.assertEqual(self.registry(), ('play', 'work'))
        # A sidecar record whose registry entry was dropped by hand refuses every other action, nothing changed;
        # its own account-remove retires the rest of it.
        account_registry.remove_alias('play', self.home)
        before = self.snapshot()
        self.refused('account_add', 'accounts_inconsistent', 'office')
        self.refused('account_remove', 'accounts_inconsistent', 'work')
        self.refused('account_add', 'accounts_inconsistent', 'play')
        self.assertEqual(self.snapshot(), before)
        result = self.account('account_remove', 'play')
        self.assertEqual((result['codex'], result['claude_code'], result['claude_desktop']),
                         ('removed', 'removed', 'removed'))
        self.assertEqual(sorted(self.sidecar()['accounts']), ['work'])

    def test_an_entry_without_a_record_is_never_taken_as_ours(self):
        self.full()
        account_registry.add_alias('work', self.home, longest_tool='t')
        command, arguments = self.alias_entry()
        self.claude().set_entry('telegram_search_work', {'type': 'stdio', 'command': command, 'args': arguments})
        self.refused('account_remove', 'claude_registration_conflict')

    def test_the_sidecar_shape_is_closed(self):
        self.added()
        path = self.runtime / 'accounts.json'
        good = path.read_bytes()
        value = json.loads(good)
        user = dict(value['accounts']['work'], user_id=7)
        for bad in ({**value, 'extra': 1}, {**value, 'schema_version': 2}, {**value, 'accounts': {}},
                    {**value, 'accounts': {'Work': value['accounts']['work']}},
                    {**value, 'accounts': {'work': user}},
                    {**value, 'accounts': {'work': dict(value['accounts']['work'], claude_code='plugin')}},
                    {**value, 'accounts': {'work': dict(value['accounts']['work'], release_root='/elsewhere')}}):
            with self.subTest(bad=sorted(bad)):
                path.write_bytes(json.dumps(bad).encode())
                self.refused('account_add', 'metadata_conflict', 'play')
        path.write_bytes(good)
        self.assertEqual(self.account('account_add', 'play')['status'], 'account_added')


if __name__ == '__main__':
    unittest.main()
