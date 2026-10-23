"""0.55 phase D part 2 (spec 3D, criterion 6): upgrade, rollback and uninstall over the default and every alias.

Synthetic installations over disposable homes: every release is a synthetic admitted tree, and launchctl, the
claude CLI, the default broker's and the alias brokers' pending-send probes are fakes. Nothing reaches the real
~/Library, a client configuration, the Keychain, launchd or Telegram. The socket-path budget is widened only
because a disposable home's path is long (tests/test_account_install.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import unittest
from unittest import mock

from test_account_install import AccountHarness, tree
from test_bundle_upgrade import NEIGHBOUR_CODEX, OLD_VERSION
from telegram_search_mcp import account_registry, bundle_install, config

NEW_VERSION = '0.55.0.dev0'


class AliasChangeHarness(AccountHarness):
    """AccountHarness plus the change actions with the alias brokers' probe, and an alias session whose
    runtime.toml pins the installed release's version (as the setup wizard writes it)."""

    def install(self, packet, **kwargs):
        kwargs.setdefault('alias_pending', self.alias_pending)
        return super().install(packet, **kwargs)

    def change(self, action, **kwargs):
        kwargs.setdefault('alias_pending', self.alias_pending)
        return super().change(action, **kwargs)

    def alias_session(self, alias=None, pin=OLD_VERSION):
        """What a login leaves for `alias`: its root, session and pinned policy, its exact broker plist loaded and its
        connect job loaded (the `work` alias is the one the fake launchd knows)."""
        assert alias in (None, self.ALIAS)
        super().alias_session()
        policy = self.jobs.root / 'runtime.toml'
        policy.write_bytes(('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                            'enabled_capabilities = ["read", "artifacts"]\n' % pin).encode())
        policy.chmod(0o600)
        (self.jobs.root / 'enrollment.json').write_bytes(b'{"synthetic": "alias enrollment"}\n')
        (self.jobs.root / 'enrollment.json').chmod(0o600)
        return tree(self.jobs.root)

    def change_snapshot(self):
        """Everything an upgrade, rollback or uninstall over the aliases could touch."""
        state = self.snapshot()
        state.update(receipt=(self.runtime / 'receipt.json').read_bytes(), pointer=(self.runtime / 'current.json').read_bytes(),
                     plist=self.plist.read_bytes() if self.plist.exists() else None,
                     alias_plist=self.jobs.broker_plist.read_bytes() if self.jobs.broker_plist.exists() else None,
                     transactions=sorted(os.listdir(self.runtime / 'transactions')),
                     alias_policy=(self.jobs.root / 'runtime.toml').read_bytes() if self.jobs.root.exists() else None)
        return state

    def alias_tree(self):
        """The alias's session tree but its runtime.toml (whose version line a change rewrites)."""
        return {k: v for k, v in tree(self.jobs.root).items() if k != 'runtime.toml'}

    def newer(self, name='b', version=NEW_VERSION):
        return self.build(name, launcher=('# release %s\n' % name).encode(), version=version)

    def entries(self, alias, release):
        """{client: the alias's entry is exactly `release`'s} for Codex, Claude Code and Claude Desktop."""
        command, arguments = self.commands(release)[0], self.commands(release)[1] + ['--account', alias]
        codex = self.codex_servers().get('telegram_search_' + alias)
        return {'codex': codex is not None and (codex['command'], list(codex['args'])) == (command, arguments),
                'claude': self.claude().entry_of('telegram_search_' + alias) == {'type': 'stdio', 'command': command,
                                                                                  'args': arguments},
                'desktop': self.desktop_servers().get('telegram_search_' + alias) == {'command': command,
                                                                                       'args': arguments}}

    def release_root(self, packet):
        return self.runtime / 'releases' / packet[3]


class UpgradeOverAliasesTests(AliasChangeHarness, unittest.TestCase):
    def test_an_upgrade_repoints_every_alias_and_republishes_its_record(self):
        self.added()
        self.account('account_add', 'play')  # Never logged in: no root, no broker.
        self.alias_session()
        session = self.alias_tree()
        old = self.release
        new = self.newer()
        result = self.install(new)
        new_root = self.release_root(new)
        self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))
        self.assertEqual(result['accounts'], {
            'play': {'service': 'not_loaded', 'codex': 'registered', 'claude_code': 'registered',
                     'claude_desktop': 'registered', 'claude_desktop_next_action': 'restart_claude_desktop'},
            'work': {'service': 'restarted', 'codex': 'registered', 'claude_code': 'registered',
                     'claude_desktop': 'registered', 'claude_desktop_next_action': 'restart_claude_desktop'}})
        for alias in ('work', 'play'):
            self.assertEqual(self.entries(alias, new_root), {'codex': True, 'claude': True, 'desktop': True})
        self.assertIs(self.codex_servers()['telegram_search_work']['required'], False)
        self.assertEqual(list(self.desktop_servers()), ['weather', 'telegram_search', 'telegram_search_work',
                                                        'telegram_search_play'])
        self.assertTrue((self.codex / 'config.toml').read_bytes().startswith(NEIGHBOUR_CODEX))
        # Its broker: the target's exact definition, booted out once and bootstrapped again after the commit.
        payload = bundle_install._service_payload(new_root, self.jobs.broker_label, 'work')
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()), payload)
        self.assertEqual(self.launchctl.job(self.jobs.broker_label), payload['ProgramArguments'])
        self.assertEqual(self.alias_probes[0], (old / 'python/bin/python3', old / 'app', self.jobs.socket,
                                                self.jobs.policy))
        # Its version line and nothing else of its session.
        self.assertIn(('expected_package_version = "%s"' % NEW_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())
        self.assertEqual(self.alias_tree(), session)
        self.assertFalse(config.alias_jobs('play', self.home).root.exists())
        side = self.sidecar()['accounts']
        for alias in ('work', 'play'):
            self.assertEqual((side[alias]['transaction'], side[alias]['release_root'], side[alias]['claude_code'],
                              side[alias]['claude_desktop']), ('change_1', str(new_root), 'registered', 'registered'))
            self.assertEqual(side[alias]['codex']['transaction_id'], 'change_1')
        journal = json.loads((self.runtime / 'transactions' / 'change_1' / 'journal.json').read_bytes())
        self.assertEqual(sorted(journal['accounts']), ['play', 'work'])
        self.assertNotIn('user_id', json.dumps(journal))
        # The account actions run over the re-pointed records.
        removed = self.account('account_remove')
        self.assertEqual((removed['service'], removed['codex'], removed['claude_code'], removed['claude_desktop']),
                         ('removed', 'removed', 'removed', 'removed'))
        self.assertEqual(self.account('account_add', 'office')['status'], 'account_added')


    def test_every_alias_refusal_comes_before_the_transaction(self):
        def edited_codex():
            path = self.codex / 'config.toml'
            data = path.read_bytes()
            start = data.index(b'[mcp_servers.telegram_search_work]')
            path.write_bytes(data[:start] + data[start:].replace(b'tool_timeout_sec = 600', b'tool_timeout_sec = 9', 1))
        cases = [
            ('pending_send_unresolved', lambda: self.alias_answers.extend(['pending'])),
            ('pending_send_unresolved', lambda: self.alias_answers.extend(['unknown'])),
            ('service_conflict', lambda: self.jobs.broker_plist.write_bytes(
                plistlib.dumps({**plistlib.loads(self.jobs.broker_plist.read_bytes()), 'KeepAlive': False}))),
            ('service_conflict', lambda: self.launchctl.set_job(self.jobs.broker_label, ['/foreign/python', 'x'])),
            ('registration_conflict', edited_codex),
            ('claude_registration_conflict', lambda: self.claude().set_entry(
                'telegram_search_work', {'type': 'stdio', 'command': '/x', 'args': []})),
            ('claude_desktop_registration_conflict', lambda: self.desktop_config.write_text(
                self.desktop_config.read_text().replace('"--account",\n        "work"', '"--account",\n        "w"'))),
            ('policy_conflict', lambda: (self.jobs.root / 'runtime.toml').write_bytes(
                (self.jobs.root / 'runtime.toml').read_bytes().replace(OLD_VERSION.encode(), b'9.9.9'))),
        ]
        for code, arrange in cases:
            with self.subTest(code=code):
                self.setUp()
                self.added()
                self.alias_session()
                arrange()
                before = self.change_snapshot()
                new = self.newer()
                with self.assertRaises(bundle_install.InstallError) as caught:
                    self.install(new)
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(self.change_snapshot(), before)
                self.assertFalse(self.release_root(new).exists())
                self.assertNotIn('bootout', self.launchctl.verbs())

    def test_a_late_pending_send_on_an_alias_restores_the_default_and_the_retry_resumes(self):
        self.added()
        self.alias_session()
        old, new = self.release, self.newer()
        self.alias_answers = ['none', 'none', 'pending']  # Before the transaction, its observation, then its stop.
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.install(new)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        # The default's broker, stopped first, is back on the old release; the alias's was never stopped.
        self.assertEqual(self.launchctl.loaded[4], str(old / 'launcher.py'))
        self.assertEqual(plistlib.loads(self.plist.read_bytes())['ProgramArguments'][4], str(old / 'launcher.py'))
        self.assertEqual(self.launchctl.job(self.jobs.broker_label)[4], str(old / 'launcher.py'))
        self.assertEqual(self.pointer()['release_root'], str(old))
        self.assertEqual(self.entries('work', old), {'codex': True, 'claude': True, 'desktop': True})
        journal = json.loads((self.runtime / 'transactions' / 'change_1' / 'journal.json').read_bytes())
        self.assertEqual(journal['publications']['service_cycle'], [{'cycle': 1, 'state': 'restored'}])
        self.assertTrue(bundle_install._parked(journal))
        error = self.refused('account_add', 'transaction_pending', 'play')
        self.assertEqual(error.pending_action, 'upgrade')
        result = self.install(new)
        self.assertEqual((result['status'], result['accounts']['work']['service']), ('upgraded', 'restarted'))
        self.assertEqual(self.entries('work', self.release_root(new)), {'codex': True, 'claude': True, 'desktop': True})

    def test_a_late_refusal_after_the_alias_entries_restores_every_broker_and_its_version_line(self):
        self.added()
        self.alias_session()
        old, new = self.release, self.newer()
        receipt = self.runtime / 'receipt.json'

        def changed(stage):
            if stage == 'policy':  # After every client step and both version lines: receipt.json edited by hand.
                receipt.write_bytes(receipt.read_bytes().replace(b'"status":"activation_required"',
                                                                 b'"status":"activation_required" '))
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.install(new, checkpoint=changed)
        self.assertEqual(str(caught.exception), 'metadata_conflict')
        for label, job in ((self.label, self.launchctl.loaded), (self.jobs.broker_label,
                                                                  self.launchctl.job(self.jobs.broker_label))):
            with self.subTest(label=label):
                self.assertEqual(job[4], str(old / 'launcher.py'))
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(old, self.jobs.broker_label, 'work'))
        self.assertIn(('expected_package_version = "%s"' % OLD_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())
        journal = json.loads((self.runtime / 'transactions' / 'change_1' / 'journal.json').read_bytes())
        self.assertEqual(journal['publications']['service_cycle:work'], [{'cycle': 1, 'state': 'restored'}])
        self.assertTrue(bundle_install._parked(journal))
        # The entries the parked change wrote stay; the repeat, once receipt.json is back, finishes it.
        self.assertEqual(self.entries('work', self.release_root(new)), {'codex': True, 'claude': True, 'desktop': True})
        receipt.write_bytes(receipt.read_bytes().replace(b'"status":"activation_required" ',
                                                         b'"status":"activation_required"'))
        result = self.install(new)
        self.assertEqual((result['status'], result['accounts']['work']['service']), ('upgraded', 'restarted'))
        self.assertIn(('expected_package_version = "%s"' % NEW_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())

    def test_an_alias_the_registry_lists_without_a_record_has_its_broker_rewritten_and_no_entry_touched(self):
        self.full()
        account_registry.add_alias('work', self.home, longest_tool='t')  # Registered outside account-add.
        self.alias_session()
        command, arguments = self.alias_entry()
        foreign = {'type': 'stdio', 'command': command, 'args': arguments}
        self.claude().set_entry('telegram_search_work', foreign)  # An entry no record owns: never ours.
        new = self.newer()
        result = self.install(new)
        self.assertEqual(result['accounts'], {'work': {'service': 'restarted'}})
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(self.release_root(new), self.jobs.broker_label, 'work'))
        self.assertEqual(self.claude().entry_of('telegram_search_work'), foreign)
        self.assertNotIn('telegram_search_work', self.codex_servers())
        self.assertIsNone(self.sidecar())

    def test_an_entry_the_upgrade_could_not_repoint_stays_owned_and_its_removal_still_finds_it(self):
        self.added()
        old = self.release
        new = self.newer()
        result = self.install(new, claude_candidates=())  # No claude CLI: neither entry is re-pointed.
        new_root = self.release_root(new)
        self.assertEqual((result['claude_code'], result['accounts']['work']['claude_code']), ('not_found', 'not_found'))
        self.assertEqual(result['accounts']['work']['claude_registration_command'][5], 'telegram_search_work')
        self.assertEqual(self.entries('work', old)['claude'], True)
        self.assertEqual(self.sidecar()['accounts']['work']['claude_code'], 'not_found')
        # The entry it left on the old release is still the installation's: account-remove removes it.
        removed = self.account('account_remove')
        self.assertEqual(removed['claude_code'], 'removed')
        self.assertIsNone(self.claude().entry_of('telegram_search_work'))
        self.assertEqual(self.entries('work', new_root), {'codex': False, 'claude': False, 'desktop': False})

    def test_plugin_mode_repoints_the_alias_user_entry_and_never_the_plugin(self):
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755)
        self.session()
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            self.install(self.build('a', version=OLD_VERSION), claude_code='plugin')
        self.claude()
        self.account('account_add')
        new = self.newer()
        result = self.install(new)
        self.assertEqual((result['claude_code'], result['accounts']['work']['claude_code']), ('plugin', 'registered'))
        self.assertIsNone(self.claude().entry)  # The default stays the plugin's.
        self.assertTrue(self.entries('work', self.release_root(new))['claude'])



# The installation root's names a 0.54 release (v0.54.0rc1, c6338a0) accepts: its `bundle_install._ROOT_NAMES`.
ROOT_NAMES_054 = frozenset({'transactions', 'releases', '.install.lock', 'receipt.json', 'current.json', 'adopted.json'})


class RollbackOverAliasesTests(AliasChangeHarness, unittest.TestCase):
    def upgraded_then(self, *aliases):
        """0.52.9 installed, then upgraded to 0.55.0.dev0, then `aliases` added on it (`work` logged in)."""
        self.full()
        old = self.release
        self.install(self.newer())
        self.claude()
        for alias in aliases:
            self.account('account_add', alias)
        if 'work' in aliases:
            self.alias_session(pin=NEW_VERSION)
        return old

    def readiness(self):
        """The default broker's readiness record, as a 0.55 broker writes it once a registry exists (spec 3C)."""
        run = self.session_root / 'run'
        run.mkdir(mode=0o700, exist_ok=True)
        path = run / 'readiness.json'
        path.write_bytes(b'{"authorized":true,"generation":"g","version":1}')
        path.chmod(0o600)
        return path

    def test_a_rollback_below_055_refuses_while_any_alias_exists_and_with_none_left_retires_the_registry(self):
        old = self.upgraded_then('work')
        record = self.readiness()
        before = self.change_snapshot()
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'accounts_present')
        self.assertEqual(self.change_snapshot(), before)
        self.account('account_remove')
        account_registry.add_alias('solo', self.home, longest_tool='t')  # The registry alone lists it: still an alias.
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'accounts_present')
        account_registry.remove_alias('solo', self.home)
        accounts = config.accounts_root(self.home)
        pinned = (accounts / 'registry.json').read_bytes()
        result = self.change('rollback')
        self.assertEqual((result['status'], result['release_root'], result['account_registry']),
                         ('rolled_back', str(old), 'retired'))
        self.assertNotIn('accounts', result)
        # Moved aside, never deleted: the registry beside itself, the readiness record into the transaction.
        self.assertFalse((accounts / 'registry.json').exists())
        retired = [p for p in accounts.iterdir() if p.name.startswith('retired-')]
        self.assertEqual(len(retired), 1)
        self.assertRegex(retired[0].name, r'\Aretired-[0-9a-f]{32}-registry\.json\Z')
        self.assertEqual(retired[0].read_bytes(), pinned)
        self.assertFalse(record.exists())
        self.assertEqual((self.runtime / 'transactions' / 'change_2' / 'readiness.retired').read_bytes(),
                         b'{"authorized":true,"generation":"g","version":1}')
        self.assertIsNone(account_registry.load(accounts))  # Every path behaves as 0.54 again.
        # What a 0.54 release reads: its root names, and a default tree with nothing 0.55 added.
        self.assertLessEqual(set(os.listdir(self.runtime)), ROOT_NAMES_054)
        self.assertFalse((self.session_root / 'run' / 'readiness.json').exists())
        # Repeated after it finished: its result, no step.
        again = self.change('rollback')
        self.assertEqual((again['status'], again['installation']), ('rolled_back', 'reused'))

    def test_a_busy_registry_at_the_retirement_keeps_the_older_broker_serving_and_the_retry_finishes(self):
        old = self.upgraded_then('work')
        self.account('account_remove')
        real = account_registry.retire

        def busy(*args, **kwargs):
            raise account_registry.RegistryError('registry_busy')
        with mock.patch.object(account_registry, 'retire', busy), self.assertRaises(bundle_install.InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'registry_busy')
        self.assertEqual((self.pointer()['release_root'], self.launchctl.loaded[4]), (str(old), str(old / 'launcher.py')))
        self.assertTrue((config.accounts_root(self.home) / 'registry.json').exists())
        with mock.patch.object(account_registry, 'retire', real):
            result = self.change('rollback')
        self.assertEqual((result['status'], result['installation'], result['account_registry']),
                         ('rolled_back', 'rolled_back', 'retired'))
        self.assertFalse((config.accounts_root(self.home) / 'registry.json').exists())

    def test_a_rollback_below_055_without_a_registry_retires_nothing(self):
        self.full()
        self.install(self.newer())
        result = self.change('rollback')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertNotIn('account_registry', result)
        journal = json.loads((self.runtime / 'transactions' / 'change_2' / 'journal.json').read_bytes())
        self.assertNotIn('retire', journal)
        self.assertNotIn('accounts', journal)

    def releases_that_know_aliases(self):
        """Release a at 0.55.0.dev0 with `work` added and logged in, then upgraded to b at the same version."""
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755)
        self.session(pin=NEW_VERSION)
        self.install(self.newer('a'))
        self.service(self.release)
        self.claude()
        self.account('account_add')
        self.alias_session(pin=NEW_VERSION)
        a = self.release
        self.install(self.newer('b'))
        return a, self.release

    def test_a_rollback_between_releases_that_know_aliases_repoints_every_alias(self):
        a, b = self.releases_that_know_aliases()
        self.assertEqual(self.entries('work', b), {'codex': True, 'claude': True, 'desktop': True})
        session = self.alias_tree()
        result = self.change('rollback')
        self.assertEqual((result['status'], result['release_root'], result['accounts']['work']['service']),
                         ('rolled_back', str(a), 'restarted'))
        self.assertEqual(self.entries('work', a), {'codex': True, 'claude': True, 'desktop': True})
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(a, self.jobs.broker_label, 'work'))
        self.assertEqual(self.launchctl.job(self.jobs.broker_label)[4], str(a / 'launcher.py'))
        record = self.sidecar()['accounts']['work']
        self.assertEqual((record['transaction'], record['release_root']), ('change_2', str(a)))
        self.assertEqual(self.alias_tree(), session)
        self.assertEqual(self.registry(), ('work',))
        self.assertEqual(self.account('account_remove')['codex'], 'removed')

    def test_a_rollback_refuses_newer_alias_state_and_tolerates_a_profile_switch(self):
        cases = ('added_since', 'enrollment', 'other_line', 'switch')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                self.releases_that_know_aliases()
                policy = self.jobs.root / 'runtime.toml'
                if case == 'added_since':  # A session the newer release wrote from the start.
                    self.account('account_add', 'play')
                elif case == 'enrollment':
                    (self.jobs.root / 'enrollment.json').write_bytes(b'{"written": "by the newer release"}\n')
                elif case == 'other_line':
                    policy.write_bytes(policy.read_bytes() + b'selection_ttl_seconds = 1200\n')
                else:
                    policy.write_bytes(policy.read_bytes().replace(b'["read", "artifacts"]', b'["read", "artifacts", "send"]'))
                before = self.change_snapshot()
                if case == 'switch':
                    self.assertEqual(self.change('rollback')['status'], 'rolled_back')
                    self.assertIn(b'"send"', policy.read_bytes())
                    continue
                with self.assertRaises(bundle_install.InstallError) as caught:
                    self.change('rollback')
                self.assertEqual(str(caught.exception), 'rollback_refused')
                self.assertEqual(self.change_snapshot(), before)


class UninstallOverAliasesTests(AliasChangeHarness, unittest.TestCase):
    def test_uninstall_retires_every_alias_broker_and_entry_and_never_its_session(self):
        self.added()
        self.account('account_add', 'play')
        session = self.alias_session()
        result = self.change('uninstall')
        self.assertEqual(result['accounts'], {
            'play': {'service': 'absent', 'connect_job': 'not_loaded', 'codex': 'removed', 'claude_code': 'removed',
                     'claude_desktop': 'removed'},
            'work': {'service': 'removed', 'connect_job': 'stopped', 'codex': 'removed', 'claude_code': 'removed',
                     'claude_desktop': 'removed'}})
        self.assertFalse(self.runtime.exists() or self.jobs.broker_plist.exists())
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertIsNone(self.launchctl.job(self.jobs.connect_label))
        self.assertEqual({n for n in self.codex_servers() if n.startswith('telegram_search')}, set())
        for alias in ('work', 'play'):
            self.assertIsNone(self.claude().entry_of('telegram_search_' + alias))
        self.assertEqual(list(self.desktop_servers()), ['weather'])
        # Never touched: the alias's root and session, and the registry (a later install re-adds the aliases).
        self.assertEqual(tree(self.jobs.root), session)
        self.assertEqual(self.registry(), ('play', 'work'))
        self.assertEqual(self.alias_probes[-1][2:], (self.jobs.socket, self.jobs.policy))

    def test_a_pending_send_on_an_alias_refuses_the_uninstall_before_anything(self):
        for answer in ('pending', 'unknown'):
            with self.subTest(answer=answer):
                self.setUp()
                self.added()
                self.alias_session()
                self.alias_answers = [answer]
                before = self.change_snapshot()
                with self.assertRaises(bundle_install.InstallError) as caught:
                    self.change('uninstall')
                self.assertEqual(str(caught.exception), 'pending_send_unresolved')
                self.assertEqual(self.change_snapshot(), before)

    def test_an_entry_no_record_owns_and_a_foreign_broker_definition_are_kept(self):
        self.full()
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.alias_session()
        foreign = plistlib.dumps({**plistlib.loads(self.jobs.broker_plist.read_bytes()), 'KeepAlive': False})
        self.jobs.broker_plist.write_bytes(foreign)
        command, arguments = self.alias_entry()
        self.claude().set_entry('telegram_search_work', {'type': 'stdio', 'command': command, 'args': arguments})
        result = self.change('uninstall')
        self.assertEqual(result['accounts']['work'], {'service': 'kept', 'connect_job': 'stopped', 'codex': 'absent',
                                                      'claude_code': 'kept', 'claude_desktop': 'absent'})
        self.assertEqual(self.jobs.broker_plist.read_bytes(), foreign)
        self.assertIsNotNone(self.claude().entry_of('telegram_search_work'))

    def test_an_uninstall_that_leaves_no_alias_retires_the_registry_and_never_touches_the_default_tree(self):
        self.added()
        self.account('account_remove')  # No alias left: the registry holds only the default's pin.
        run = self.session_root / 'run'
        run.mkdir(mode=0o700, exist_ok=True)
        (run / 'readiness.json').write_bytes(b'{"authorized":true,"generation":"g","version":1}')
        (run / 'readiness.json').chmod(0o600)
        default = tree(self.session_root)
        accounts = config.accounts_root(self.home)
        pinned = (accounts / 'registry.json').read_bytes()
        result = self.change('uninstall')
        self.assertEqual((result['status'], result['account_registry']), ('uninstalled', 'retired'))
        self.assertNotIn('accounts', result)
        self.assertFalse((accounts / 'registry.json').exists())
        retired = [p for p in accounts.iterdir() if p.name.startswith('retired-')]
        self.assertEqual([p.read_bytes() for p in retired], [pinned])
        self.assertEqual(tree(self.session_root), default)  # The default's session tree, readiness record included.
        self.assertIsNone(account_registry.load(accounts))

    def parked_upgrade(self, stage, edit, undo):
        """An upgrade parked by a late refusal at `stage` (after the alias entries were re-pointed)."""
        self.added()
        self.alias_session()
        new = self.newer()

        def checkpoint(value):
            if value == stage:
                edit()
        with self.assertRaises(bundle_install.InstallError):
            self.install(new, checkpoint=checkpoint)
        undo()
        journal = json.loads((self.runtime / 'transactions' / 'change_1' / 'journal.json').read_bytes())
        self.assertTrue(bundle_install._parked(journal))
        self.assertEqual(self.entries('work', self.release_root(new)), {'codex': True, 'claude': True, 'desktop': True})
        return new

    def test_uninstall_over_a_parked_upgrade_removes_the_alias_entries_it_rewrote(self):
        policy = lambda: self.session_root / 'runtime.toml'
        receipt = lambda: self.runtime / 'receipt.json'
        saved = {}

        def edit_policy():
            saved['policy'] = policy().read_bytes()
            policy().write_bytes(saved['policy'].replace(OLD_VERSION.encode(), b'9.9.9'))

        def edit_receipt():
            saved['receipt'] = receipt().read_bytes()
            receipt().write_bytes(saved['receipt'] + b' ')
        cases = {'before the sidecar': ('claude_desktop', edit_policy, lambda: policy().write_bytes(saved['policy'])),
                 'after the sidecar': ('policy', edit_receipt, lambda: receipt().write_bytes(saved['receipt']))}
        for case, (stage, edit, undo) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                saved.clear()
                self.parked_upgrade(stage, edit, undo)
                result = self.change('uninstall')
                self.assertEqual({k: result['accounts']['work'][k] for k in ('codex', 'claude_code', 'claude_desktop')},
                                 {'codex': 'removed', 'claude_code': 'removed', 'claude_desktop': 'removed'})
                self.assertFalse(self.runtime.exists())
                self.assertEqual({n for n in self.codex_servers() if n.startswith('telegram_search')}, set())
                self.assertIsNone(self.claude().entry_of('telegram_search_work'))
                self.assertEqual(list(self.desktop_servers()), ['weather'])
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))


class AccountsDirectoryWithoutRegistryTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d, low): without a registry the accounts directory is never judged, so a non-private one leaves
    an upgrade, a rollback and an uninstall as 0.54's; with a registry in it they still refuse `registry_unsafe`."""

    def loose_accounts_directory(self):
        accounts = config.accounts_root(self.home)
        accounts.mkdir(mode=0o755, exist_ok=True)
        accounts.chmod(0o755)
        return accounts

    def test_upgrade_rollback_and_uninstall_proceed_without_a_registry(self):
        self.full()
        accounts = self.loose_accounts_directory()
        result = self.install(self.newer())
        self.assertEqual(result['status'], 'upgraded')
        self.assertNotIn('accounts', result)
        self.assertEqual(self.change('rollback')['status'], 'rolled_back')
        result = self.change('uninstall')
        self.assertEqual(result['status'], 'uninstalled')
        self.assertNotIn('account_registry', result)
        self.assertEqual(os.listdir(accounts), [])  # Nothing created, nothing retired.

    def test_with_a_registry_a_loose_accounts_directory_still_refuses(self):
        self.full()
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.loose_accounts_directory()
        before = self.change_snapshot()
        new = self.newer()
        for action, run in (('upgrade', lambda: self.install(new)), ('uninstall', lambda: self.change('uninstall'))):
            with self.subTest(action=action):
                with self.assertRaises(bundle_install.InstallError) as caught:
                    run()
                self.assertEqual(str(caught.exception), 'registry_unsafe')
                self.assertEqual(self.change_snapshot(), before)


class ReinstallOverKeptAliasesTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d, high): an uninstall keeps the registry while accounts.json leaves with the retired root, so
    after a reinstall every alias the registry lists has no record. That is repairable, not inconsistent: each one's
    own account-add registers its entries again (its registry entry, pin and session kept) and its account-remove
    retires it, beside any other alias still in that state."""

    USER = 4_000_000_041  # A synthetic user id: never printed.

    def reinstalled(self, *aliases):
        packet, _ = self.full()
        for alias in aliases:
            self.account('account_add', alias)
        self.alias_session()
        account_registry.pin_account(config.accounts_root(self.home), 'work', self.USER)
        session = tree(self.jobs.root)
        self.change('uninstall')
        self.assertEqual((self.registry(), self.sidecar()), (tuple(sorted(aliases)), None))
        version = json.loads((packet[1] / 'manifest' / 'release.json').read_bytes())['package_version']
        with mock.patch('telegram_search_mcp.__version__', version):
            self.install(packet)
        self.service(self.release)
        return session

    def test_each_alias_an_uninstall_kept_is_added_and_removed_again_after_a_reinstall(self):
        for aliases in (('work',), ('play', 'work')):
            with self.subTest(aliases=aliases):
                self.setUp()
                session = self.reinstalled(*aliases)
                for alias in aliases:
                    self.assertEqual(self.account('account_add', alias)['status'], 'account_added')
                    self.assertEqual(self.entries(alias, self.release), {'codex': True, 'claude': True, 'desktop': True})
                self.assertEqual(self.registry(), tuple(sorted(aliases)))
                self.assertEqual(sorted(self.sidecar()['accounts']), sorted(aliases))
                registry = account_registry.load(config.accounts_root(self.home))
                self.assertEqual(dict(registry.entries)['work'], self.USER)  # Never added again: its pin stays.
                self.assertEqual(tree(self.jobs.root), session)
                for alias in aliases:
                    removed = self.account('account_remove', alias)
                    self.assertEqual((removed['status'], removed['codex']), ('account_removed', 'removed'))
                self.assertEqual((self.registry(), self.sidecar()), ((), None))
                self.assertFalse(self.jobs.root.exists())

    def test_a_kept_alias_is_removed_without_being_added_again_beside_another(self):
        self.reinstalled('play', 'work')
        self.account('account_add', 'work')
        removed = self.account('account_remove', 'play')  # The registry alone lists it.
        self.assertEqual((removed['status'], removed['codex'], removed['session']), ('account_removed', 'absent', 'absent'))
        self.assertEqual((self.registry(), sorted(self.sidecar()['accounts'])), (('work',), ['work']))
        removed = self.account('account_remove', 'work')
        self.assertEqual((removed['codex'], removed['session']), ('removed', 'moved'))
        self.assertEqual((self.registry(), self.sidecar()), ((), None))

    def test_a_record_without_a_registry_entry_still_refuses(self):
        self.full()
        self.account('account_add', 'work')
        self.account('account_add', 'play')
        account_registry.remove_alias('play', self.home)  # Its record stays: that is not ours to repair.
        before = self.snapshot()
        self.refused('account_add', 'accounts_inconsistent', 'office')
        self.refused('account_remove', 'accounts_inconsistent', 'work')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
