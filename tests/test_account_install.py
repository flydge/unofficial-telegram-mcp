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
import unittest
from unittest import mock

import tomlkit

from test_bundle_upgrade import ChangeHarness, FakeLaunchctl, NEIGHBOUR_CODEX
from telegram_search_mcp import account_registry, bundle_install, config


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
    broker and connect jobs in the same fake launchd domain."""

    ALIAS = 'work'

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
        kwargs.setdefault('default_check' if action == 'account_add' else 'alias_pending',
                          self.default_check if action == 'account_add' else self.alias_pending)
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return getattr(module, action)(self.runtime, alias=alias or self.ALIAS, user_home=self.home, **kwargs)

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
        """What a login leaves for the alias: its root with a session, policy and run directory, its broker plist
        (the release's exact alias definition) loaded, and its connect job loaded."""
        for path in (self.jobs.root, self.jobs.root / 'tdlib', self.jobs.root / 'run'):
            path.mkdir(mode=0o700, exist_ok=True)
        (self.jobs.root / 'tdlib' / 'td.binlog').write_bytes(b'alias session bytes')
        (self.jobs.root / 'runtime.toml').write_bytes(b'config_version = 1\nenabled_capabilities = ["read"]\n')
        for path in (self.jobs.root / 'tdlib' / 'td.binlog', self.jobs.root / 'runtime.toml'):
            path.chmod(0o600)
        payload = self.module()._service_payload(self.release, self.jobs.broker_label, self.ALIAS)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        self.launchctl.load(self.jobs.broker_label)
        self.connect_plist.write_bytes(plistlib.dumps({'Label': self.jobs.connect_label,
                                                       'ProgramArguments': [payload['ProgramArguments'][0], 'connect']}))
        self.connect_plist.chmod(0o600)
        self.launchctl.load(self.jobs.connect_label)
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
        self.assertFalse(self.jobs.root.exists() or self.jobs.broker_plist.exists())  # No login, no directory.
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
                with mock.patch('telegram_search_mcp.__version__', '0.52.9'):
                    self.install(self.build('a', version='0.52.9'), claude_code=mode)
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
        self.assertFalse(self.jobs.root.exists())
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

    def test_an_alias_never_logged_in_has_nothing_to_stop_or_move(self):
        self.added()
        result = self.account('account_remove')
        self.assertEqual((result['service'], result['connect_job'], result['session']), ('absent', 'not_loaded', 'absent'))
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

    def test_a_broker_or_login_holding_the_aliases_locks_outside_launchd_is_never_moved_under(self):
        from telegram_search_mcp.session_owner import acquire_owner_lock
        for name, loaded in (('broker.lock', self.jobs.broker_label), ('connect.lock', self.jobs.connect_label)):
            with self.subTest(lock=name):
                self.setUp()
                self.added()
                self.alias_session()
                self.launchctl.set_job(loaded, None)  # Not a launchd job: a process the owner started by hand.
                held = acquire_owner_lock(self.jobs.root / 'run' / name)
                try:
                    before, session = self.snapshot(), tree(self.jobs.root)
                    self.refused('account_remove', 'service_conflict')
                    self.assertEqual((self.snapshot(), tree(self.jobs.root)), (before, session))
                    # Started after the precheck: the move refuses, the transaction stays for the retry.
                    os.close(held)
                    held = None

                    def start(stage, name=name):
                        nonlocal held
                        if stage == 'registry':
                            held = acquire_owner_lock(self.jobs.root / 'run' / name)
                    self.refused('account_remove', 'service_conflict', checkpoint=start)
                    self.assertTrue(self.jobs.root.exists())
                    self.assertEqual(self.registry(), ())
                finally:
                    if held is not None:
                        os.close(held)
                self.assertEqual(self.account('account_remove')['session'], 'moved')

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
    def test_a_disagreement_on_another_alias_refuses_and_the_disagreeing_aliases_own_action_repairs_it(self):
        self.added()
        account_registry.add_alias('play', self.home, longest_tool='t')  # Registered outside account-add.
        before = self.snapshot()
        self.refused('account_add', 'accounts_inconsistent', 'office')
        self.refused('account_remove', 'accounts_inconsistent', 'work')
        self.assertEqual(self.snapshot(), before)
        # account-add of the registry-only alias registers its entries; its registry entry stays as it was.
        result = self.account('account_add', 'play')
        self.assertEqual(result['status'], 'account_added')
        self.assertEqual(sorted(self.sidecar()['accounts']), ['play', 'work'])
        account_registry.add_alias('gone', self.home, longest_tool='t')
        self.assertEqual(self.account('account_remove', 'gone')['codex'], 'absent')
        self.assertEqual(self.registry(), ('play', 'work'))
        # A sidecar record whose registry entry was dropped by hand: account-remove retires the rest of it.
        account_registry.remove_alias('play', self.home)
        self.refused('account_add', 'accounts_inconsistent', 'office')
        self.refused('account_add', 'accounts_inconsistent', 'play')
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
