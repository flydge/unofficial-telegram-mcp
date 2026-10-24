"""0.55 phase D (spec 3D): the launcher's account transactions, `status` over every account and the per-alias
capability switch.

`account-add <alias>` and `account-remove <alias>` run from the installed release by the rollback/uninstall
admission path (its own pinned tree); `status` reports the default and every alias (loaded, authorized,
registered, never the account) once a registry exists and keeps 0.54's keys without one; `capabilities ...
--account <alias>` edits only that alias's runtime.toml through its own broker and label. Synthetic homes, fake
launchd and fake installers only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_account_install import AccountHarness
from telegram_search_mcp import account_registry, bundle_install, bundle_runtime, capability_profiles as profiles
from telegram_search_mcp import config, setup_cli
from telegram_search_mcp.config import AccountPaths, CAPABILITY_PROFILES

LAUNCHER = Path(__file__).resolve().parents[1] / 'packaging' / 'launcher.py'
SENTINEL = 'SYNTHETICSENTINEL'
POLICY = (b'config_version = 1\nenabled_capabilities = ["read", "artifacts"]\n'
          b'expected_package_version = "0.55.0.dev0"\n')


def run(main, args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            code = main(args)
        except SystemExit as exit:
            code = exit.code
    return code, out.getvalue(), err.getvalue()


def launcher_at(release):
    release.mkdir(parents=True)
    shutil.copy(LAUNCHER, release / 'launcher.py')
    spec = importlib.util.spec_from_file_location('account_launcher_' + release.name[:8], release / 'launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AccountChangeGrammarTests(unittest.TestCase):
    """`account-add <alias>` / `account-remove <alias>`: the installed release's own admission, then the installer."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.release = Path(temporary.name).resolve() / 'TelegramSearch' / 'releases' / ('b' * 64)
        self.module = launcher_at(self.release)

    def dispatch(self, args, error=None):
        saved = list(sys.path)
        seen = {}

        def installer(name):
            def call(root, **kwargs):
                seen.update(action=name, root=root, **kwargs)
                if error is not None:
                    raise error
                return {'status': 'ok'}
            return call
        try:
            with mock.patch.object(self.module, 'admit') as admit, \
                    mock.patch.object(bundle_install, 'account_add', installer('account_add')), \
                    mock.patch.object(bundle_install, 'account_remove', installer('account_remove')):
                code, out, err = run(self.module.main, args)
        finally:
            sys.path[:] = saved
        return code, out, err, admit, seen

    def test_each_account_change_reaches_its_installer_with_the_alias(self):
        for action in ('account-add', 'account-remove'):
            with self.subTest(action=action):
                code, out, err, admit, seen = self.dispatch([action, 'work'])
                self.assertEqual((code, json.loads(out), err), (0, {'status': 'ok'}, ''))
                admit.assert_called_once_with(self.release, self.release / 'bundle-index.json', 'b' * 64, True)
                self.assertEqual(seen, {'action': action.replace('-', '_'), 'root': self.release.parent.parent,
                                        'alias': 'work', 'user_home': Path.home(), 'release_root': self.release})
        self.assertEqual(self.module.ACCOUNT_CHANGES, ('account-add', 'account-remove'))

    def test_the_grammar_is_closed_and_refuses_before_admission_without_echo(self):
        generic = json.dumps({'status': 'refused', 'code': 'bundle_admission_or_install_failed'}) + '\n'
        for args in (['account-add'], ['account-add', 'Work'], ['account-add', 'default'], ['account-add', 'a' * 13],
                     ['account-add', SENTINEL], ['account-add', 'work', 'play'], ['account-add', '--account', 'work'],
                     ['account-remove', '1work'], ['account-remove', ''], ['account-rename', 'work']):
            with self.subTest(args=args):
                code, out, err, admit, seen = self.dispatch(args)
                admit.assert_not_called()
                self.assertEqual(seen, {})
                self.assertEqual(code, 2)
                self.assertNotIn(SENTINEL, out + err)
                if args[0] != 'account-rename':
                    self.assertEqual(err, generic)

    def test_installer_codes_and_the_pending_account_reach_the_output(self):
        error = bundle_install.InstallError
        cases = {'accounts_inconsistent': error('accounts_inconsistent'), 'alias_exists': error('alias_exists'),
                 'account_remove_incomplete': error('account_remove_incomplete'),
                 'alias_path_too_long': error('alias_path_too_long')}
        for code, raised in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self.dispatch(['account-add', 'work'], raised)[2],
                                 json.dumps({'status': 'refused', 'code': code}) + '\n')
        pending = error('transaction_pending', pending_action='account-remove', pending_account='play')
        self.assertEqual(json.loads(self.dispatch(['account-add', 'work'], pending)[2]),
                         {'status': 'refused', 'code': 'transaction_pending', 'pending_action': 'account-remove',
                          'pending_account': 'play'})
        forged = error('transaction_pending', pending_action='account-add', pending_account='play')
        forged.pending_account = '/Users/' + SENTINEL
        self.assertEqual(json.loads(self.dispatch(['account-add', 'work'], forged)[2]),
                         {'status': 'refused', 'code': 'transaction_pending', 'pending_action': 'account-add'})
        self.assertIsNone(error('transaction_pending', pending_action='upgrade', pending_account='play').pending_account)
        self.assertIsNone(error('transaction_pending', pending_action='account-add', pending_account='Play').pending_account)

    def test_an_unreadable_leftover_broker_names_the_manual_bootout_of_its_exact_label(self):
        """Review fix (055d3, R4): `alias_broker_unreadable` carries the alias and the bootout of that alias's exact
        broker label as the owner's next step; a forged account is dropped with the step."""
        from telegram_search_mcp import config
        error = bundle_install.InstallError
        label = config.alias_jobs('work', Path.home()).broker_label
        for action in ('account-add', 'account-remove'):
            with self.subTest(action=action):
                raised = error('alias_broker_unreadable', account='work')
                refusal = json.loads(self.dispatch([action, 'play'], raised)[2])
                self.assertEqual(refusal, {'status': 'refused', 'code': 'alias_broker_unreadable', 'account': 'work',
                                           'next_action': 'boot_out_alias_broker_then_retry',
                                           'bootout_command': ['launchctl', 'bootout',
                                                               'gui/%d/%s' % (os.getuid(), label)]})
        forged = error('alias_broker_unreadable', account='work')
        forged.account = '/Users/' + SENTINEL
        self.assertEqual(json.loads(self.dispatch(['account-remove', 'work'], forged)[2]),
                         {'status': 'refused', 'code': 'alias_broker_unreadable'})
        self.assertIsNone(error('pending_send_unresolved', account='work').account)
        self.assertIsNone(error('alias_broker_unreadable', account='Work').account)

    def test_login_in_progress_and_an_unsafe_connect_lock_name_the_alias(self):
        """Critic fix (055f, low): `login_in_progress` and `connect_lock_unsafe` carry the alias (`account`, a closed
        token) whose login or lock refused, as `alias_broker_unreadable` does; a forged account is dropped."""
        error = bundle_install.InstallError
        for code in ('login_in_progress', 'connect_lock_unsafe'):
            for action in ('account-add', 'account-remove'):
                with self.subTest(code=code, action=action):
                    refusal = json.loads(self.dispatch([action, 'play'], error(code, account='work'))[2])
                    self.assertEqual(refusal, {'status': 'refused', 'code': code, 'account': 'work'})
            forged = error(code, account='work')
            forged.account = '/Users/' + SENTINEL
            self.assertEqual(json.loads(self.dispatch(['account-remove', 'work'], forged)[2]),
                             {'status': 'refused', 'code': code})
            self.assertEqual(json.loads(self.dispatch(['account-remove', 'work'], error(code))[2]),
                             {'status': 'refused', 'code': code})
            self.assertIsNone(error(code, account='Work').account)

    def test_every_code_an_account_transaction_can_pass_through_is_in_the_closed_set(self):
        pin_only = {'alias_unavailable', 'account_mismatch', 'account_unverified', 'account_duplicate'}
        self.assertLessEqual(account_registry.CODES - pin_only, self.module.INSTALL_CODES)
        self.assertLessEqual({'accounts_inconsistent', 'account_remove_incomplete',
                              'accounts_present', 'release_not_current', 'login_in_progress',
                              'connect_lock_unsafe', 'alias_broker_unreadable'},
                             self.module.INSTALL_CODES)
        self.assertEqual(self.module.PENDING_ACTIONS, bundle_install.PENDING_ACTIONS)
        # Critic fix (055f): an unanswered default check is reported in the result, never a refusal any more.
        self.assertNotIn('default_check_failed', self.module.INSTALL_CODES)

    def test_help_names_the_account_actions_and_the_capabilities_flag(self):
        code, out, _ = run(self.module.main, ['--help'])
        self.assertEqual(code, 0)
        for text in ('account-add <alias>', 'account-remove <alias>', 'capabilities take a trailing --account <alias>',
                     'without logging in', 'never deleting it'):
            self.assertIn(text, out)


class CapabilitiesGrammarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='c')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        account_registry.add_alias('work', self.home, longest_tool='t')
        patcher = mock.patch.object(Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = AccountPaths.for_alias('work', self.home)
        self.release = self.home / 'TelegramSearch' / 'releases' / ('b' * 64)
        self.module = launcher_at(self.release)

    def dispatch(self, args):
        receipt = json.dumps({'release_root': str(self.release), 'index_sha256': 'b' * 64}).encode()
        saved = list(sys.path)
        try:
            with mock.patch.object(self.module, '_read', return_value=receipt), mock.patch.object(self.module, 'admit'), \
                    mock.patch.object(bundle_runtime, 'activate'), \
                    mock.patch.object(self.module, 'capabilities', return_value=7) as capabilities:
                code, out, err = run(self.module.main, args)
        finally:
            sys.path[:] = saved
        return code, err, capabilities

    def test_the_flag_names_the_aliases_switch_and_without_it_the_call_is_unchanged(self):
        for args, expected in ((['capabilities', 'show', '--account', 'work'], ['show']),
                               (['capabilities', 'enable', 'video', '--account', 'work'], ['enable', 'video']),
                               (['capabilities', 'disable', 'send', '--account', 'work'], ['disable', 'send'])):
            with self.subTest(args=args):
                code, _, capabilities = self.dispatch(args)
                self.assertEqual(code, 7)
                capabilities.assert_called_once_with(self.release, expected, self.work)
        code, _, capabilities = self.dispatch(['capabilities', 'show'])
        capabilities.assert_called_once_with(self.release, ['show'])

    def test_an_unregistered_alias_and_grammar_violations_refuse_with_fixed_codes(self):
        account_registry.remove_alias('work', self.home)
        code, err, capabilities = self.dispatch(['capabilities', 'show', '--account', 'work'])
        self.assertEqual((code, json.loads(err)), (2, {'status': 'refused', 'code': 'alias_unavailable'}))
        capabilities.assert_not_called()
        for args in (['capabilities', 'show', '--account'], ['capabilities', 'show', '--account', 'default'],
                     ['capabilities', 'enable', 'everything', '--account', 'work'],
                     ['capabilities', 'show', 'extra', '--account', 'work'], ['capabilities', '--account', 'work']):
            with self.subTest(args=args):
                code, err, capabilities = self.dispatch(args)
                self.assertEqual((code, json.loads(err)), (2, {'status': 'refused', 'code': 'bundle_admission_failed'}))
                capabilities.assert_not_called()

    def test_the_aliases_switch_gates_and_restarts_its_own_broker(self):
        seen = {}

        def apply(home, action, profile, *, pending, restartable, restart, account=None):
            seen.update(home=home, action=action, profile=profile, account=account)
            seen.update(pending=pending(), restartable=restartable(), restart=restart())
            return {'status': 'changed'}
        with mock.patch.object(profiles, 'apply', apply), \
                mock.patch.object(bundle_install, 'account_pending_sends', return_value='none') as gate, \
                mock.patch.object(bundle_install, 'restart_receipt_broker', return_value='restarted') as restart:
            code, out, _ = run(lambda _: self.module.capabilities(self.release, ['enable', 'video'], self.work), None)
        self.assertEqual((code, json.loads(out)), (0, {'status': 'changed'}))
        self.assertEqual((seen['action'], seen['profile'], seen['account']), ('enable', 'video', self.work))
        gate.assert_called_once_with(self.home, Path(sys.executable), self.release / 'app', self.work.socket,
                                     self.work.policy)
        self.assertEqual(restart.call_args_list, [mock.call(self.release, self.home, kickstart=False, account=self.work),
                                                  mock.call(self.release, self.home, account=self.work)])


class AliasProfileTests(unittest.TestCase):
    """capability_profiles with a named alias: its own runtime.toml and label, the default's untouched."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='p')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.work = AccountPaths.for_alias('work', self.home)
        self.default = AccountPaths.for_alias('default', self.home)
        for account in (self.default, self.work):
            account.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            account.root.chmod(0o700)
            account.policy.write_bytes(POLICY)
            account.policy.chmod(0o600)

    def test_show_and_apply_edit_only_the_aliases_policy(self):
        self.assertEqual(profiles.show(self.home, self.work)['enabled_capabilities'], ['artifacts', 'read'])
        result = profiles.apply(self.home, 'enable', 'video', pending=lambda: 'none', restartable=lambda: 'loaded',
                                restart=lambda: 'restarted', account=self.work)
        self.assertEqual((result['status'], result['broker']), ('changed', 'restarted'))
        self.assertIn(b'"video_send"', self.work.policy.read_bytes())
        self.assertEqual(self.default.policy.read_bytes(), POLICY)
        self.assertEqual(profiles.show(self.home)['enabled_capabilities'], ['artifacts', 'read'])

    def test_its_refusals_come_before_the_write_and_a_failed_restart_names_its_own_label(self):
        with self.assertRaises(profiles.ProfileError) as caught:
            profiles.apply(self.home, 'enable', 'video', pending=lambda: 'pending', restartable=lambda: 'loaded',
                           restart=lambda: 'restarted', account=self.work)
        self.assertEqual(caught.exception.code, 'pending_send_unresolved')
        self.assertEqual(self.work.policy.read_bytes(), POLICY)
        result = profiles.apply(self.home, 'enable', 'video', pending=lambda: 'none', restartable=lambda: 'not_running',
                                restart=lambda: 'boom', account=self.work)
        self.assertEqual(result['restart_command'][-1], 'gui/%d/%s' % (os.getuid(), self.work.broker_label))
        self.assertEqual(profiles.restart_command(self.home)[-1], 'gui/%d/%s' % (os.getuid(), self.default.broker_label))


class AliasRestartTests(AccountHarness, unittest.TestCase):
    """restart_receipt_broker for a named alias: only its own label and its exact definition of the release."""

    def test_only_the_aliases_exact_definition_is_kickstarted(self):
        self.added()
        self.alias_session()
        work = AccountPaths.for_alias('work', self.home)
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv[1:3])
            if argv[1] == 'kickstart':
                return subprocess.CompletedProcess(argv, 0, '', '')
            return self.launchctl(argv, **kwargs)
        release = self.release
        self.assertEqual(bundle_install.restart_receipt_broker(release, self.home, runner, kickstart=False,
                                                               account=work), 'loaded')
        self.assertEqual(bundle_install.restart_receipt_broker(release, self.home, runner, account=work), 'restarted')
        self.assertEqual(calls[-1], ['kickstart', '-k'])
        self.assertTrue(all(work.broker_label in call[1] for call in calls if len(call) > 1 and '/' in call[1]))
        self.jobs.broker_plist.write_bytes(plistlib.dumps({**plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                                          'KeepAlive': False}))
        with self.assertRaises(bundle_install.InstallError) as caught:
            bundle_install.restart_receipt_broker(release, self.home, runner, account=work)
        self.assertEqual(str(caught.exception), 'service_conflict')
        self.launchctl.set_job(self.jobs.broker_label, None)
        self.assertEqual(bundle_install.restart_receipt_broker(release, self.home, runner, account=work), 'not_running')


class AccountsStatusTests(AccountHarness, unittest.TestCase):
    """`status` over every account: loaded, authorized and registered for the default and each alias."""

    def facts(self, paths, broker):
        return {'alias': paths.account.alias, 'loaded': paths.account.alias == 'work', 'authorized': None}

    def report(self):
        with mock.patch.object(account_registry, 'SOCKET_PATH_LIMIT', 4096), \
                mock.patch.object(setup_cli, 'existing_broker_status', return_value='not_running'), \
                mock.patch.object(setup_cli, 'account_status', side_effect=self.facts):
            return bundle_install.accounts_status(self.runtime, self.home)

    def test_without_a_registry_there_is_no_report(self):
        self.full()
        self.assertIsNone(self.report())
        self.assertEqual(bundle_install.account_registrations(self.runtime, self.home), {'default': True})

    def test_the_default_and_every_alias_are_reported_never_the_account(self):
        self.added()
        self.account('account_add', 'play')
        report = self.report()
        self.assertEqual(report, {'default': {'loaded': False, 'authorized': None, 'registered': True},
                                  'play': {'loaded': False, 'authorized': None, 'registered': True},
                                  'work': {'loaded': True, 'authorized': None, 'registered': True}})
        # An entry removed by hand reads unregistered; an alias the registry lists without a record too.
        self.claude().set_entry('telegram_search_play', None)
        account_registry.add_alias('gone', self.home, longest_tool='t')
        report = self.report()
        self.assertEqual((report['play']['registered'], report['gone']['registered'], report['work']['registered']),
                         (False, False, True))
        self.assertNotIn('user_id', json.dumps(report))

    def test_a_manual_outcome_reads_registered_once_the_printed_command_ran(self):
        """Review fix (055d2, low): a recorded manual outcome (failed, not_found, unavailable) was read as unregistered
        without looking, so `registered` stayed false after the owner ran the printed command; now its entry is observed
        like a registered one: exact counts, missing or different does not. For an alias and for the default."""
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755, exist_ok=True)
        self.session()
        self.claude()
        missing = (self.base / 'no-claude',)  # No Claude Code CLI found: the manual command is printed.
        with mock.patch('telegram_search_mcp.__version__', self.BASE_VERSION):
            installed = self.install(self.build('a', version=self.BASE_VERSION), claude_candidates=missing)
        self.service(installed['release_root'])
        added = self.account('account_add', claude_candidates=missing)
        self.assertEqual((installed['claude_code'], added['claude_code']), ('not_found', 'not_found'))
        self.assertEqual(self.report()['default']['registered'], False)
        self.assertEqual(self.report()['work']['registered'], False)
        # The owner runs the printed commands: the exact entries.
        command, arguments = self.commands(self.release)
        self.claude().set_entry('telegram_search', {'type': 'stdio', 'command': command, 'args': arguments})
        self.claude().set_entry('telegram_search_work', {'type': 'stdio', 'command': command,
                                                         'args': arguments + ['--account', 'work']})
        report = self.report()
        self.assertEqual((report['default']['registered'], report['work']['registered']), (True, True))
        # A different entry is not ours.
        self.claude().set_entry('telegram_search', {'type': 'stdio', 'command': '/x', 'args': []})
        self.claude().set_entry('telegram_search_work', {'type': 'stdio', 'command': '/x', 'args': []})
        report = self.report()
        self.assertEqual((report['default']['registered'], report['work']['registered']), (False, False))

    def test_an_unreadable_registry_reports_unavailable(self):
        self.added()
        (config.accounts_root(self.home) / 'registry.json').chmod(0o644)
        self.assertEqual(self.report(), 'unavailable')


class LauncherStatusTests(unittest.TestCase):
    def test_the_default_keeps_its_054_keys_without_a_registry_and_gains_the_report_with_one(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        module = launcher_at(Path(temporary.name).resolve() / 'TelegramSearch' / 'releases' / ('b' * 64))
        root = Path(temporary.name).resolve() / 'TelegramSearch' / 'releases' / ('b' * 64)
        for report, expected in ((None, {'state': 'new', 'clients': {'claude_desktop': 'ok'}}),
                                 ({'default': {'loaded': True, 'authorized': True, 'registered': True}},
                                  {'state': 'new', 'clients': {'claude_desktop': 'ok'},
                                   'accounts': {'default': {'loaded': True, 'authorized': True, 'registered': True}}})):
            with self.subTest(report=report), \
                    mock.patch.object(setup_cli, 'main', side_effect=lambda args: print('{"state": "new"}') or 0), \
                    mock.patch.object(bundle_install, 'client_status', return_value={'claude_desktop': 'ok'}), \
                    mock.patch.object(bundle_install, 'accounts_status', return_value=report) as accounts:
                code, out, _ = run(lambda _: module.status(root), None)
            self.assertEqual((code, json.loads(out)), (0, expected))
            accounts.assert_called_once_with(root.parent.parent, Path.home())


if __name__ == '__main__':
    unittest.main()
