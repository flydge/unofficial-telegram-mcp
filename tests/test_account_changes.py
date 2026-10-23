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

from test_account_install import LOCK_ONLY, AccountHarness, tree
from test_bundle_upgrade import NEIGHBOUR_CODEX, OLD_VERSION as LEGACY_VERSION
from telegram_search_mcp import account_registry, bundle_install, config

# The installed release the alias tests start from (one that knows aliases: account-add runs only from such a current
# release), the kit an upgrade goes to, and the 0.52.9 release a rollback below 0.55 goes back to.
OLD_VERSION = AccountHarness.BASE_VERSION
NEW_VERSION = '0.55.0.dev1'


class AliasChangeHarness(AccountHarness):
    """AccountHarness plus the change actions with the alias brokers' probe, and an alias session whose
    runtime.toml pins the installed release's version (as the setup wizard writes it)."""

    def install(self, packet, **kwargs):
        """The kit installed as its own package version (a synthetic release names it; the installer admits a tree of
        its own version only)."""
        kwargs.setdefault('alias_pending', self.alias_pending)
        version = json.loads((packet[1] / 'manifest' / 'release.json').read_bytes())['package_version']
        with mock.patch('telegram_search_mcp.__version__', version):
            return super().install(packet, **kwargs)

    def change(self, action, **kwargs):
        kwargs.setdefault('alias_pending', self.alias_pending)
        return super().change(action, **kwargs)

    def alias_session(self, alias=None, pin=None):
        """What a finished login leaves for `alias`: its root, session and policy pinning the installed release (unless
        told), its exact broker plist loaded, and its connect job still registered but idle, its helper exited (the
        `work` alias is the one the fake launchd knows)."""
        assert alias in (None, self.ALIAS)
        pin = self.receipt()['package_version'] if pin is None else pin
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
                     alias_policy=(self.jobs.root / 'runtime.toml').read_bytes()
                     if (self.jobs.root / 'runtime.toml').exists() else None)
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
        # Never logged in: its root holds only the run directory and connect.lock the upgrade held (review fix 055d2).
        self.assertEqual(tree(config.alias_jobs('play', self.home).root), LOCK_ONLY)
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
        """0.52.9 installed, then upgraded to 0.55.0.dev1, then `aliases` added on it (`work` logged in)."""
        self.BASE_VERSION = LEGACY_VERSION
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
        self.BASE_VERSION = LEGACY_VERSION
        self.full()
        self.install(self.newer())
        result = self.change('rollback')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertNotIn('account_registry', result)
        journal = json.loads((self.runtime / 'transactions' / 'change_2' / 'journal.json').read_bytes())
        self.assertNotIn('retire', journal)
        self.assertNotIn('accounts', journal)

    def releases_that_know_aliases(self):
        """Release a at 0.55.0.dev1 with `work` added and logged in, then upgraded to b at the same version."""
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

    def test_every_broker_is_gated_again_in_one_pass_before_any_bootout(self):
        """Review fix (055d2, low; it replaces 055d's gate right before each alias's bootout): inside the transaction,
        on every attempt, every alias broker (and the default's on a resume) is gated in one pass before any bootout,
        so a send pending on an alias refuses with every broker loaded and every plist in place, the default's too."""
        self.added()
        self.alias_session()
        self.alias_answers = ['none', 'pending']  # Before the transaction; the pass inside it.
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('uninstall')
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertIsNotNone(self.launchctl.loaded)
        self.assertTrue(self.plist.exists())
        self.assertEqual(self.launchctl.job(self.jobs.broker_label)[-2:], ['--account', 'work'])
        self.assertTrue(self.jobs.broker_plist.exists())
        self.assertEqual(len(self.alias_probes), 2)
        # Resumed, the default's broker is gated in the same pass: still none booted out while the alias's is pending.
        self.alias_answers, probes = ['pending'], len(self.probes)
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('uninstall')
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual(len(self.probes), probes + 1)
        self.assertNotIn('bootout', self.launchctl.verbs())
        result = self.change('uninstall')  # Once the send settled, the retry finishes.
        self.assertEqual((result['status'], result['service'], result['accounts']['work']['service']),
                         ('uninstalled', 'removed', 'removed'))
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertIsNone(self.launchctl.loaded)

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
        # Never logged in: the root it moves aside holds only the connect.lock a transaction held (review fix 055d2).
        self.assertEqual((removed['status'], removed['codex'], removed['session']),
                         ('account_removed', 'absent', 'moved'))
        self.assertEqual(tree(Path(removed['session_moved_to'])), LOCK_ONLY)
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


class AccountReleaseTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d, medium): account-add and account-remove run only from the receipt's current release, and only
    while that release knows aliases (0.55 or newer): every other run refuses `release_not_current` before anything,
    so no alias entry ever names a launcher without the `--account` grammar and no accounts.json appears beside a
    release that refuses it."""

    def installed(self, version):
        """`version` installed with every client present, a session pinning it and its broker loaded."""
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755, exist_ok=True)
        self.session(pin=version)
        with mock.patch('telegram_search_mcp.__version__', version):
            self.install(self.build('a', version=version))
        self.service(self.release)
        self.claude()
        return self.release

    def test_after_a_rollback_to_a_release_that_knows_no_aliases_the_account_actions_refuse(self):
        old = self.installed('0.54.0')
        self.install(self.newer())
        new = self.release
        self.assertEqual(self.change('rollback')['release_root'], str(old))
        before = self.change_snapshot()
        for action in ('account_add', 'account_remove'):
            # The 0.55 tree still in releases/ (the launcher passes its own), the current one, an in-process call.
            for release in ('new', 'old', None):
                with self.subTest(action=action, release=release):
                    self.refused(action, 'release_not_current', release_root={'new': new, 'old': old, None: None}[release])
                    self.assertEqual(self.change_snapshot(), before)
        self.assertEqual((self.registry(), self.sidecar()), (None, None))
        self.assertFalse(config.accounts_root(self.home).exists())

    def test_the_account_actions_run_only_from_the_current_release(self):
        a = self.installed(NEW_VERSION)
        self.install(self.newer())
        b = self.release
        self.change('rollback')
        before = self.change_snapshot()
        for action in ('account_add', 'account_remove'):
            with self.subTest(action=action):
                self.refused(action, 'release_not_current', release_root=b)
                self.assertEqual(self.change_snapshot(), before)
        self.assertEqual(self.account('account_add', release_root=a)['status'], 'account_added')
        self.assertEqual(self.account('account_remove', release_root=a)['status'], 'account_removed')


class LoginDuringChangeTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d, medium): an upgrade or rollback never starts beside an alias's running login, and a login
    that finished during one never leaves its alias on a release current.json no longer names."""

    def test_a_running_login_refuses_the_change_before_the_transaction(self):
        from telegram_search_mcp.session_owner import acquire_owner_lock
        for case in ('connect job running', 'connect lock held'):
            for action in ('upgrade', 'rollback'):
                with self.subTest(case=case, action=action):
                    self.setUp()
                    self.added()
                    self.alias_session()
                    new = self.newer()
                    if action == 'rollback':
                        self.install(new)
                    held = None
                    if case == 'connect job running':
                        self.launchctl.load(self.jobs.connect_label)
                    else:  # An inline `setup --account` in the owner's Terminal: no launchd job, its lock held.
                        self.launchctl.set_job(self.jobs.connect_label, None)
                        held = acquire_owner_lock(self.jobs.root / 'run' / 'connect.lock')
                    try:
                        before, calls = self.change_snapshot(), len(self.launchctl.calls)
                        with self.assertRaises(bundle_install.InstallError) as caught:
                            self.install(new) if action == 'upgrade' else self.change('rollback')
                        self.assertEqual(str(caught.exception), 'login_in_progress')
                        self.assertEqual(self.change_snapshot(), before)
                        self.assertNotIn('bootout', [c[0] for c in self.launchctl.calls[calls:]])
                    finally:
                        if held is not None:
                            os.close(held)
                    # Once the login is over (its job idle, or no lock held), the change proceeds.
                    if case == 'connect job running':
                        self.launchctl.idle(self.jobs.connect_label)
                    result = self.install(new) if action == 'upgrade' else self.change('rollback')
                    self.assertIn(result['status'], ('upgraded', 'rolled_back'))

    def first_login(self, release, pin):
        """What a first login run by `release` leaves for `work`: its root, policy pinning `pin`, and that release's
        exact broker definition written and loaded."""
        for path in (self.jobs.root, self.jobs.root / 'run'):
            path.mkdir(mode=0o700, exist_ok=True)
        policy = self.jobs.root / 'runtime.toml'
        policy.write_bytes(('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                            'enabled_capabilities = ["read", "artifacts"]\n' % pin).encode())
        policy.chmod(0o600)
        payload = bundle_install._service_payload(release, self.jobs.broker_label, self.ALIAS)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        self.launchctl.load(self.jobs.broker_label)

    def test_a_login_that_finished_during_the_change_leaves_its_alias_on_the_target(self):
        for stage in ('service', 'complete'):  # After the broker stops; after the commit, before the restart.
            with self.subTest(stage=stage):
                self.setUp()
                self.added()  # Never logged in: no root, no plist at the observation.
                old, new = self.release, self.newer()

                def login(value, stage=stage):
                    if value == stage and not self.jobs.broker_plist.exists():
                        self.first_login(old, OLD_VERSION)
                result = self.install(new, checkpoint=login)
                new_root = self.release_root(new)
                self.assertEqual((result['status'], result['accounts']['work']['service']), ('upgraded', 'not_loaded'))
                # Its definition and version line are the target's; the old broker is stopped, none started (none ran
                # when the change began): the next start runs the target.
                self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                 bundle_install._service_payload(new_root, self.jobs.broker_label, 'work'))
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                self.assertIn(('expected_package_version = "%s"' % NEW_VERSION).encode(),
                              (self.jobs.root / 'runtime.toml').read_bytes())
                self.assertEqual(self.pointer()['release_root'], str(new_root))

    def test_a_kill_while_the_definition_that_appeared_is_moved_resumes_to_the_same_state(self):
        for stage in ('recorded:plist:work', 'published:plist:work'):
            with self.subTest(stage=stage):
                self.setUp()
                self.added()
                old, new = self.release, self.newer()

                def login(value, stage=stage):
                    if value == 'complete' and not self.jobs.broker_plist.exists():
                        self.first_login(old, OLD_VERSION)
                    if value == stage:
                        raise KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.install(new, checkpoint=login)
                self.assertEqual(self.install(new)['accounts']['work']['service'], 'not_loaded')
                self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                 bundle_install._service_payload(self.release_root(new), self.jobs.broker_label, 'work'))
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                self.assertEqual([p.name for p in self.jobs.broker_plist.parent.iterdir()
                                  if p.name != self.plist.name], [self.jobs.broker_plist.name])  # No temporary left.

    def test_a_pending_send_on_a_broker_that_appeared_refuses_before_anything_changes(self):
        self.added()
        old, new = self.release, self.newer()

        def login(value):
            if value == 'complete' and not self.jobs.broker_plist.exists():
                self.first_login(old, OLD_VERSION)
                self.alias_answers.append('pending')
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.install(new, checkpoint=login)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(old, self.jobs.broker_label, 'work'))
        self.assertEqual(self.launchctl.job(self.jobs.broker_label)[4], str(old / 'launcher.py'))
        self.assertIn(('expected_package_version = "%s"' % OLD_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())
        self.assertEqual(self.install(new)['accounts']['work']['service'], 'not_loaded')  # Once the send settled.
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(self.release_root(new), self.jobs.broker_label, 'work'))
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertIn(('expected_package_version = "%s"' % NEW_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())

    def test_a_foreign_definition_that_appeared_is_never_overwritten(self):
        self.added()
        new = self.newer()
        foreign = plistlib.dumps({'Label': self.jobs.broker_label, 'ProgramArguments': ['/foreign/python', 'x']})

        def written(value):
            if value == 'complete' and not self.jobs.broker_plist.exists():
                self.jobs.broker_plist.write_bytes(foreign)
                self.jobs.broker_plist.chmod(0o600)
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.install(new, checkpoint=written)
        self.assertEqual(str(caught.exception), 'service_conflict')
        self.assertEqual(self.jobs.broker_plist.read_bytes(), foreign)
        self.assertEqual(self.pointer()['release_root'], str(self.release_root(new)))  # Committed: the retry finishes.
        self.jobs.broker_plist.unlink()
        self.assertEqual(self.install(new)['accounts']['work']['service'], 'not_loaded')
        self.assertFalse(self.jobs.broker_plist.exists())


class LoginLockTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d2, lead decision A): every installer transaction over the aliases holds every alias's
    connect.lock for each whole run, fresh or resumed, so no login runs during one; a login still holding it, or a
    connect job still running, refuses `login_in_progress` before anything changes and the transaction stays
    resumable."""

    def setUp(self):
        super().setUp()
        self.attempts = []

    def attempt_logins(self, stage, aliases=('play', 'work')):
        """At `stage`, the first thing a login of each alias does: take its connect.lock (never created here)."""
        from telegram_search_mcp.session_owner import OwnerLeaseError, acquire_owner_lock
        for alias in aliases:
            lock = config.alias_jobs(alias, self.home).root / 'run' / 'connect.lock'
            if not lock.exists():
                self.attempts.append((stage, alias, 'absent'))
                continue
            try:
                os.close(acquire_owner_lock(lock))
            except OwnerLeaseError as error:
                self.attempts.append((stage, alias, error.code))
            else:
                self.attempts.append((stage, alias, 'taken'))

    def test_a_parked_change_repeated_holds_every_alias_lock_at_every_checkpoint(self):
        """Review fix (055d2, medium): the login check ran on a fresh run only, so a repeated parked change let a login
        finish beside it and leave its alias on the old release."""
        from telegram_search_mcp.session_owner import acquire_owner_lock
        for action in ('upgrade', 'rollback'):
            with self.subTest(action=action):
                self.setUp()
                self.added()
                self.account('account_add', 'play')  # Never logged in: its lock is created as a login would.
                self.alias_session()
                new = self.newer()
                target = self.release_root(new) if action == 'upgrade' else self.release
                if action == 'rollback':
                    self.install(new)

                def run(**kwargs):
                    return self.install(new, **kwargs) if action == 'upgrade' else self.change('rollback', **kwargs)
                policy = self.session_root / 'runtime.toml'
                saved, version = policy.read_bytes(), self.receipt()['package_version'].encode()

                def park(stage):
                    self.attempt_logins(stage)
                    if stage == 'claude_desktop':  # A late refusal: every broker is restored, the change parks.
                        policy.write_bytes(saved.replace(version, b'9.9.9'))
                with self.assertRaises(bundle_install.InstallError) as caught:
                    run(checkpoint=park)
                self.assertEqual(str(caught.exception), 'policy_conflict')
                policy.write_bytes(saved)
                name = sorted(self.runtime.glob('transactions/change_*'), key=lambda p: int(p.name[7:]))[-1]
                self.assertTrue(bundle_install._parked(json.loads((name / 'journal.json').read_bytes())))
                first = len(self.attempts)
                # Repeated while a login holds its lock (inline, no job), or while its connect job still runs: refused,
                # nothing changed, the change still parked.
                for case in ('lock', 'job'):
                    held = None
                    if case == 'lock':
                        held = acquire_owner_lock(self.jobs.root / 'run' / 'connect.lock')
                    else:
                        self.launchctl.load(self.jobs.connect_label)
                    try:
                        before = self.change_snapshot()
                        with self.assertRaises(bundle_install.InstallError) as caught:
                            run()
                        self.assertEqual(str(caught.exception), 'login_in_progress')
                        self.assertEqual(self.change_snapshot(), before)
                    finally:
                        if held is not None:
                            os.close(held)
                    if case == 'job':
                        self.launchctl.idle(self.jobs.connect_label)
                result = run(checkpoint=self.attempt_logins)
                self.assertEqual(result['status'], 'upgraded' if action == 'upgrade' else 'rolled_back')
                self.assertGreater(first, 10)
                self.assertGreater(len(self.attempts), first + 10)
                self.assertEqual({outcome for _, _, outcome in self.attempts}, {'owner_busy'})
                # No alias on a mixed release: its definition and version line name the release current.json names.
                self.assertEqual(self.pointer()['release_root'], str(target))
                self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                 bundle_install._service_payload(target, self.jobs.broker_label, 'work'))
                version = json.loads((target / 'manifest' / 'release.json').read_bytes())['package_version']
                self.assertIn(('expected_package_version = "%s"' % version).encode(),
                              (self.jobs.root / 'runtime.toml').read_bytes())
                self.assertEqual(tree(config.alias_jobs('play', self.home).root), LOCK_ONLY)

    def test_a_running_login_refuses_the_uninstall_before_anything(self):
        """Review fix (055d2, low): uninstall never checked for a running alias login."""
        from telegram_search_mcp.session_owner import acquire_owner_lock
        for case in ('inline setup holding connect.lock', 'connect job running'):
            with self.subTest(case=case):
                self.setUp()
                self.added()
                self.alias_session()
                held = None
                if case == 'connect job running':
                    self.launchctl.load(self.jobs.connect_label)
                else:
                    self.launchctl.set_job(self.jobs.connect_label, None)
                    held = acquire_owner_lock(self.jobs.root / 'run' / 'connect.lock')
                try:
                    before, calls = self.change_snapshot(), len(self.launchctl.calls)
                    with self.assertRaises(bundle_install.InstallError) as caught:
                        self.change('uninstall')
                    self.assertEqual(str(caught.exception), 'login_in_progress')
                    self.assertEqual(self.change_snapshot(), before)
                    self.assertNotIn('bootout', [c[0] for c in self.launchctl.calls[calls:]])
                finally:
                    if held is not None:
                        os.close(held)
                if case == 'connect job running':
                    self.launchctl.idle(self.jobs.connect_label)
                result = self.change('uninstall', checkpoint=lambda stage: self.attempt_logins(stage, ('work',)))
                self.assertEqual((result['status'], result['accounts']['work']['service']), ('uninstalled', 'removed'))
                self.assertTrue(self.attempts)
                self.assertEqual({outcome for _, _, outcome in self.attempts}, {'owner_busy'})


class PendingTransactionLoginTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d3, R2): between two runs of a parked transaction no lock is held, so a login could hand a broker
    off under it. Every alias login entry point run from an installed release (`connect`, the agent's and inline, the
    connect helper before anything and again before its handoff, `setup`, `resume`) now refuses `transaction_pending`
    once it holds the alias's connect.lock, while an upgrade, rollback, uninstall, account-add or account-remove is
    unfinished, and changes nothing (the helper writes only its own terminal status, so `wait` reports the code). The
    default's login and source mode (no installation) keep 0.54's behaviour."""

    def logins(self, alias):
        """(name, run) of each login entry point of `alias`, admitted as the launcher admits it."""
        from test_onboarding import MemoryStore, Terminal
        from telegram_search_mcp import connect_cli
        from telegram_search_mcp.enrollment import SetupPaths
        from telegram_search_mcp.setup_wizard import run_setup
        paths = SetupPaths.from_account(config.AccountPaths.for_alias(alias, self.home))

        def never(*_args, **_kwargs):
            raise AssertionError('nothing may run')

        class Agent:
            def require_secure(self):
                raise RuntimeError('captured')

        def connect(terminal):
            value, code = connect_cli.connect(paths, client_factory=never, terminal=terminal, inline=never,
                                              runner=self.launchctl, uid=os.getuid(), sleeper=lambda _: None)
            return value['code'], value['next_action']

        def helper():
            log = []
            result = connect_cli.connect_helper(
                paths, store=None, provider_factory=None, handoff=never, run=never, ui_factory=never,
                server_factory=never, opener=never, sleeper=lambda _: None, restore_broker=lambda: log.append(1))
            self.assertEqual(log, [])  # A pending transaction owns the services: nothing is put back either.
            self.assertEqual(connect_cli.read_connect_status(paths), ('failed', result.code))
            paths.account.connect_status.unlink()  # Its own terminal status, for `wait`: the one thing it writes.
            return result.code, result.to_dict()['next_action']

        def setup(resume):
            terminal = Terminal()
            result = run_setup(paths, terminal, MemoryStore(), never, handoff=never, resume=resume,
                               bundled_loader=lambda: None)
            self.assertEqual(terminal.prompts, [])
            return result.code, result.to_dict()['next_action']
        return [('connect', lambda: connect(Agent())), ('connect inline', lambda: connect(Terminal())),
                ('connect helper', helper), ('setup', lambda: setup(False)), ('resume', lambda: setup(True))]

    def installed(self):
        from telegram_search_mcp import bundle_runtime
        return mock.patch.object(bundle_runtime, '_SELECTION', bundle_runtime.Selection(self.release, 'x' * 64))

    def pending(self, kind):
        """`work` and `play` added (never logged in: their roots as account-add leaves them), then `kind` left
        unfinished; returns the aliases whose logins are admitted."""
        self.added()
        if kind == 'account-add':
            def die(stage):
                if stage == 'default_check':  # After its registry entry and root: `play` is admitted.
                    raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                self.account('account_add', 'play', checkpoint=die)
            return ('play', 'work')
        self.account('account_add', 'play')
        if kind in ('upgrade', 'rollback'):
            new = self.kit = self.newer()
            if kind == 'rollback':
                self.install(new)
            policy = self.session_root / 'runtime.toml'
            saved, version = policy.read_bytes(), self.receipt()['package_version'].encode()

            def park(stage):
                if stage == 'claude_desktop':  # A late refusal: every broker restored, the change parked.
                    policy.write_bytes(saved.replace(version, b'9.9.9'))
            with self.assertRaises(bundle_install.InstallError):
                self.install(new, checkpoint=park) if kind == 'upgrade' else self.change('rollback', checkpoint=park)
            policy.write_bytes(saved)
            return ('play', 'work')
        stage, action = {'uninstall': ('connect:work', lambda **kw: self.change('uninstall', **kw)),
                         'account-remove': ('claude_code', lambda **kw: self.account('account_remove', **kw))}[kind]

        def die(value):
            if value == stage:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            action(checkpoint=die)
        return ('play', 'work')

    def test_every_alias_login_refuses_while_an_installer_transaction_is_pending(self):
        from telegram_search_mcp import account_registry, bundle_runtime
        refused = ('transaction_pending', 'finish_pending_installer_action_then_retry')
        for kind in ('upgrade', 'rollback', 'uninstall', 'account-add', 'account-remove'):
            with self.subTest(kind=kind):
                self.setUp()
                aliases = self.pending(kind)
                with self.installed():
                    for alias in aliases:
                        for name, login in self.logins(alias):
                            with self.subTest(alias=alias, login=name):
                                # The helper takes the lock before it asks (it reports through the status file
                                # only its lock holder writes), creating the empty connect.lock when absent.
                                lock = config.alias_jobs(alias, self.home).root / 'run' / 'connect.lock'
                                if name == 'connect helper' and not lock.exists():
                                    lock.touch(mode=0o600)
                                before, calls = self.change_snapshot(), len(self.launchctl.calls)
                                roots = {a: tree(config.alias_jobs(a, self.home).root) for a in aliases}
                                self.assertEqual(login(), refused)
                                self.assertEqual(self.change_snapshot(), before)
                                self.assertEqual({a: tree(config.alias_jobs(a, self.home).root) for a in aliases},
                                                 roots)
                                self.assertLessEqual({c[0] for c in self.launchctl.calls[calls:]}, {'print'})
                    self.assertTrue(bundle_runtime.installer_pending())
                    # The default's login keeps 0.54's behaviour (lead decision): it reads no installer state.
                    default = config.AccountPaths.for_alias('default', self.home)
                    self.assertIsNone(account_registry.login_admission(default))
                # Source mode (no installation this process runs from): unchanged.
                self.assertIsNone(account_registry.login_admission(config.AccountPaths.for_alias('play', self.home)))

    def test_a_login_is_admitted_again_once_the_transaction_finished(self):
        from telegram_search_mcp import account_registry
        for kind in ('upgrade', 'account-add', 'account-remove'):
            with self.subTest(kind=kind):
                self.setUp()
                self.pending(kind)
                if kind == 'upgrade':
                    self.install(self.kit)
                else:
                    self.account(kind.replace('-', '_'), 'play' if kind == 'account-add' else None)
                with self.installed():
                    self.assertIsNone(account_registry.login_admission(config.AccountPaths.for_alias('play', self.home)))

    def test_the_helper_asks_again_right_before_its_handoff(self):
        """A transaction found pending at the handoff (none can start while the helper holds the lock: this is the
        backstop): the run ends `transaction_pending` and no broker is started."""
        from telegram_search_mcp import connect_cli
        from telegram_search_mcp.enrollment import SetupPaths
        self.added()
        paths = SetupPaths.from_account(config.AccountPaths.for_alias('work', self.home))
        handoffs, seen = [], []

        def run(paths, store, provider_factory, ui, *, handoff, expires_at):
            with self.installed(), mock.patch('telegram_search_mcp.bundle_runtime.installer_pending', return_value=True):
                seen.append(handoff())
            from telegram_search_mcp.connect_flow import ConnectResult
            return ConnectResult('failed', seen[-1], True, True)

        class Server:
            def __init__(self, ui, expires_at, **options):
                pass

            def start(self):
                return 'http://127.0.0.1:1/#t=' + 'x' * 43

            def close(self):
                pass
        result = connect_cli.connect_helper(paths, store=None, provider_factory=None,
                                            handoff=lambda **_: handoffs.append(1) or 'ready', run=run,
                                            ui_factory=lambda: mock.Mock(), server_factory=Server,
                                            opener=lambda _: None, sleeper=lambda _: None,
                                            restore_broker=lambda: None)
        self.assertEqual((seen, handoffs, result.code), (['transaction_pending'], [], 'transaction_pending'))


class NewerStateRollbackTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d2, low): a login between two runs of a parked rollback gave a never logged in alias a session the
    older release must not take over; the restart moved its definition onto the older release anyway, skipping the
    per-alias state check. Now the check runs again first, and the alias stays whole on the release its session was
    made with (`newer_state_kept`); the next change moves it on."""

    def login_from(self, release, pin):
        """What a first login run by `release` leaves for `work`: its root, enrollment and policy pinning `pin`, and
        that release's exact broker definition written and loaded (its connect job idle)."""
        for path in (self.jobs.root, self.jobs.root / 'run'):
            path.mkdir(mode=0o700, exist_ok=True)
        policy = ('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                  'enabled_capabilities = ["read", "artifacts"]\n' % pin).encode()
        for name, data in (('enrollment.json', b'{"synthetic": "alias enrollment"}\n'), ('runtime.toml', policy)):
            (self.jobs.root / name).write_bytes(data)
            (self.jobs.root / name).chmod(0o600)
        payload = bundle_install._service_payload(release, self.jobs.broker_label, self.ALIAS)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        self.launchctl.load(self.jobs.broker_label)

    def test_an_alias_a_login_gave_a_session_between_the_runs_stays_on_that_release(self):
        self.added()  # Never logged in: no root, no plist at the upgrade or at the rollback's observation.
        old = self.release
        new = self.newer()
        self.install(new)
        new_root = self.release
        policy = self.session_root / 'runtime.toml'
        saved = policy.read_bytes()

        def park(stage):
            if stage == 'claude_desktop':
                policy.write_bytes(saved.replace(NEW_VERSION.encode(), b'9.9.9'))
        with self.assertRaises(bundle_install.InstallError):
            self.change('rollback', checkpoint=park)
        policy.write_bytes(saved)
        self.login_from(new_root, NEW_VERSION)  # Between the runs: the release current.json still names.
        kills = []

        def kill_once(stage):
            if stage == 'recorded:appeared:work' and not kills:
                kills.append(stage)
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.change('rollback', checkpoint=kill_once)
        result = self.change('rollback')
        self.assertEqual((result['status'], result['accounts']['work']['service']), ('rolled_back', 'newer_state_kept'))
        self.assertEqual(self.pointer()['release_root'], str(old))
        # Whole on the release its session was made with: that release's definition and version line, no broker of the
        # older release; the one the login started was stopped by the repeat and none is started.
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(new_root, self.jobs.broker_label, 'work'))
        self.assertIn(('expected_package_version = "%s"' % NEW_VERSION).encode(),
                      (self.jobs.root / 'runtime.toml').read_bytes())
        self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
        self.assertEqual([p.name for p in self.jobs.root.iterdir() if p.name.startswith('.')], [])  # No temporary.
        # The next upgrade, to a third release, moves it on whole: the definition is of this installation's own shape
        # and the version line names one of its releases.
        third = self.newer('c', '0.55.0.dev2')
        result = self.install(third)
        third_root = self.release_root(third)
        self.assertEqual((result['status'], result['accounts']['work']['service']), ('upgraded', 'not_loaded'))
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                         bundle_install._service_payload(third_root, self.jobs.broker_label, 'work'))
        self.assertIn(b'expected_package_version = "0.55.0.dev2"', (self.jobs.root / 'runtime.toml').read_bytes())
        self.assertEqual(self.entries('work', third_root), {'codex': True, 'claude': True, 'desktop': True})


class UninstallLoginGapTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d2, medium): uninstall ran an alias's broker step only while its outcome was unrecorded, so a
    broker a login loaded again between two attempts stayed, crash-looping on the removed release, and the alias could
    not be repaired. Every attempt now runs each alias's connect job bootout and broker step again until the root
    retires."""

    def test_a_login_between_two_attempts_leaves_no_broker_and_the_documented_commands_end_clean(self):
        for stage in ('connect:work', 'service:work'):
            with self.subTest(stage=stage):
                self.setUp()
                packet, _ = self.full()
                self.account('account_add')
                self.alias_session()

                def die(value, stage=stage):
                    if value == stage:
                        raise KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.change('uninstall', checkpoint=die)
                # In the gap, a login run by the release being removed hands its broker off again.
                payload = bundle_install._service_payload(self.release, self.jobs.broker_label, 'work')
                self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
                self.jobs.broker_plist.chmod(0o600)
                self.launchctl.load(self.jobs.broker_label)
                result = self.change('uninstall')
                self.assertEqual((result['status'], result['accounts']['work']['service']), ('uninstalled', 'removed'))
                self.assertFalse(self.jobs.broker_plist.exists())
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                self.assertIsNone(self.launchctl.job(self.jobs.connect_label))
                # A reinstall, then the documented commands only.
                version = json.loads((packet[1] / 'manifest' / 'release.json').read_bytes())['package_version']
                with mock.patch('telegram_search_mcp.__version__', version):
                    self.install(packet)
                self.service(self.release)
                self.assertEqual(self.account('account_add')['status'], 'account_added')
                upgraded = self.install(self.newer())
                self.assertEqual((upgraded['status'], upgraded['accounts']['work']['service']),
                                 ('upgraded', 'not_loaded'))
                removed = self.account('account_remove')
                self.assertEqual((removed['status'], removed['codex'], removed['session']),
                                 ('account_removed', 'removed', 'moved'))
                self.assertFalse(self.jobs.broker_plist.exists() or self.jobs.root.exists())
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                self.assertEqual((self.registry(), self.sidecar()), ((), None))
                self.assertEqual({n for n in self.codex_servers() if n.startswith('telegram_search_')}, set())
                self.assertIsNone(self.claude().entry_of('telegram_search_work'))
                self.assertNotIn('telegram_search_work', self.desktop_servers())


class LeftoverDefinitionTests(AliasChangeHarness, unittest.TestCase):
    """Review fix (055d2, medium): an alias plist exactly of this installation's own shape, naming any release of it
    (`<installation root>/releases/<id>`, present or not), is the installer's: account-remove and uninstall remove it,
    an upgrade rewrites it to the target, account-add's repair path rewrites it to the current release; anything else
    stays foreign, refused `service_conflict` and never removed."""

    GONE = 'e' * 64  # A release id this installation no longer holds.

    def leftover(self, *, loaded=False, release=None):
        release = self.runtime / 'releases' / (release or self.GONE)
        payload = bundle_install._service_payload(release, self.jobs.broker_label, self.ALIAS, present=False)
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        self.jobs.broker_plist.chmod(0o600)
        self.launchctl.set_job(self.jobs.broker_label, payload['ProgramArguments'] if loaded else None)
        return payload

    def foreign(self):
        payload = {**self.leftover(), 'KeepAlive': False}
        self.jobs.broker_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))
        return self.jobs.broker_plist.read_bytes()

    def test_account_remove_and_uninstall_remove_it(self):
        for action in ('account_remove', 'uninstall'):
            for loaded in (False, True):
                with self.subTest(action=action, loaded=loaded):
                    self.setUp()
                    self.added()
                    self.alias_session()
                    payload = self.leftover(loaded=loaded)
                    result = self.account(action) if action == 'account_remove' else self.change('uninstall')
                    service = result['service'] if action == 'account_remove' else result['accounts']['work']['service']
                    self.assertEqual(service, 'removed')
                    self.assertFalse(self.jobs.broker_plist.exists())
                    self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                    if loaded:  # Gated under the interpreter of the definition it runs.
                        self.assertIn(Path(payload['ProgramArguments'][0]), [p[0] for p in self.alias_probes])

    def test_an_upgrade_rewrites_it_to_the_target(self):
        self.added()
        self.alias_session()
        self.leftover(loaded=True)
        new = self.newer()
        result = self.install(new)
        target = self.release_root(new)
        self.assertEqual((result['status'], result['accounts']['work']['service']), ('upgraded', 'restarted'))
        payload = bundle_install._service_payload(target, self.jobs.broker_label, 'work')
        self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()), payload)
        self.assertEqual(self.launchctl.job(self.jobs.broker_label), payload['ProgramArguments'])

    def kept_by_an_uninstall(self):
        """`work` added and logged in, uninstalled (the registry keeps it, accounts.json leaves) and the same kit
        installed again: account-add's repair path."""
        packet, _ = self.full()
        self.account('account_add')
        self.alias_session()
        self.change('uninstall')
        version = json.loads((packet[1] / 'manifest' / 'release.json').read_bytes())['package_version']
        with mock.patch('telegram_search_mcp.__version__', version):
            self.install(packet)
        self.service(self.release)
        self.assertEqual((self.registry(), self.sidecar()), (('work',), None))

    def test_account_add_rewrites_it_on_the_repair_path(self):
        for loaded in (False, True):
            with self.subTest(loaded=loaded):
                self.setUp()
                self.kept_by_an_uninstall()
                payload = self.leftover(loaded=loaded)
                probes = len(self.alias_probes)
                result = self.account('account_add')
                self.assertEqual((result['status'], result['service']), ('account_added', 'rewritten'))
                self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                 bundle_install._service_payload(self.release, self.jobs.broker_label, 'work'))
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))  # None started: its connect starts it.
                # Gated before the transaction and again right before its bootout.
                self.assertEqual([p[0] for p in self.alias_probes[probes:]], [Path(payload['ProgramArguments'][0])] * 2
                                 if loaded else [])
                # A retry after it finished returns the same result, and the alias is removable as usual.
                self.assertEqual(self.account('account_add')['service'], 'rewritten')
                self.assertEqual(self.account('account_remove')['service'], 'removed')
                self.assertFalse(self.jobs.broker_plist.exists())

    def test_a_kill_inside_the_rewrite_resumes_to_the_same_state(self):
        for stage in ('recorded:service_rewrite', 'recorded:plist', 'published:plist'):
            with self.subTest(stage=stage):
                self.setUp()
                self.kept_by_an_uninstall()
                self.leftover(loaded=True)

                def die(value, stage=stage):
                    if value == stage:
                        raise KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.account('account_add', checkpoint=die)
                self.assertEqual(self.account('account_add')['service'], 'rewritten')
                self.assertEqual(plistlib.loads(self.jobs.broker_plist.read_bytes()),
                                 bundle_install._service_payload(self.release, self.jobs.broker_label, 'work'))
                self.assertIsNone(self.launchctl.job(self.jobs.broker_label))
                self.assertEqual([p.name for p in self.jobs.broker_plist.parent.iterdir()
                                  if p.name != self.plist.name], [self.jobs.broker_plist.name])  # No temporary left.

    def test_a_foreign_definition_is_refused_and_never_removed(self):
        for action in ('account_remove', 'upgrade', 'account_add', 'uninstall'):
            with self.subTest(action=action):
                self.setUp()
                if action == 'account_add':
                    self.kept_by_an_uninstall()
                else:
                    self.added()
                    self.alias_session()
                data = self.foreign()
                if action == 'uninstall':
                    self.assertEqual(self.change('uninstall')['accounts']['work']['service'], 'kept')
                else:
                    before = self.change_snapshot()
                    with self.assertRaises(bundle_install.InstallError) as caught:
                        self.install(self.newer()) if action == 'upgrade' else self.account(action)
                    self.assertEqual(str(caught.exception), 'service_conflict')
                    self.assertEqual(self.change_snapshot(), before)
                self.assertEqual(self.jobs.broker_plist.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
