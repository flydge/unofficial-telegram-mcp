"""Edits during a change and refusals after the broker stop (0.53 critic round 3), over disposable homes only.

A client file the user edits while the release is copied is merged: only a change of our own entry (or, when
adopting, of the adopted foreign entry) counts as a conflict. Any refusal after the broker stop and before the
commit first restores the previous service, byte for byte, through a journaled step: the old release keeps
serving and the same command finishes the transaction once the named object is put back. Round 4: the service
step is idempotent by state, so a kill anywhere in the repeat's stop still ends on one release; refusals are not
limited and the journal does not grow with them; a parked change ends by its repeat or by `uninstall`; our Codex
table changed around its write refuses with the documented code. launchctl, the claude CLI and the pending-send
probe are fakes; nothing real is read or written.
"""
import json
from pathlib import Path
import plistlib
import unittest
from unittest import mock

from claude_fake import entry
from test_adopt_legacy import AdoptionHarness, LEGACY_PYTHON
from test_bundle_change_safety import SafetyHarness
from test_bundle_upgrade import NEIGHBOUR_CODEX

WEATHER = b'\n[mcp_servers.weather]\ncommand = "/opt/w"\n'
PROFILE = b'\n[profiles.p]\nmodel = "x"\n'
OURS, CHANGED = b'tool_timeout_sec = 600', b'tool_timeout_sec = 601'
LEGACY_ARGS, LEGACY_CHANGED = b'"telegram_search_mcp.server"]', b'"telegram_search_mcp.server", "--changed"]'


class LateHarness(AdoptionHarness, SafetyHarness):

    @property
    def config(self):
        return self.codex / 'config.toml'

    def at_copy(self, edit):
        """A checkpoint that applies `edit(bytes) -> bytes` to the Codex config once the release is copied."""
        def checkpoint(stage):
            if stage == 'release':
                self.config.write_bytes(edit(self.config.read_bytes()))
        return checkpoint

    def refused(self, action):
        with self.assertRaises(self.module().InstallError) as caught:
            action()
        return str(caught.exception)

    def service_state(self):
        return self.plist.read_bytes() if self.plist.exists() else None, self.launchctl.loaded

    def metadata(self):
        return {n: (self.runtime / n).read_bytes() if (self.runtime / n).exists() else None
                for n in ('receipt.json', 'current.json')}

    def journal(self, name):
        return json.loads((self.runtime / 'transactions' / name / 'journal.json').read_text())

    def assertNeighbours(self, added):
        """Only our table is gone: the original neighbours first, the table added during the copy last."""
        data = self.config.read_bytes()
        self.assertTrue(data.startswith(NEIGHBOUR_CODEX) and data.endswith(added), data)
        self.assertNotIn(b'telegram_search', data)

    def assertOneRelease(self, target):
        """current.json, the runtime.toml pin, the plist and the loaded broker all name `target`."""
        launcher = str(Path(target) / 'launcher.py')
        version = json.loads((Path(target) / 'manifest/release.json').read_text())['package_version']
        self.assertEqual((self.pointer()['release_root'],
                          plistlib.loads(self.plist.read_bytes())['ProgramArguments'][4], self.launchctl.loaded[4]),
                         (str(target), launcher, launcher))
        self.assertIn(('expected_package_version = "%s"' % version).encode(), (self.session_root / 'runtime.toml').read_bytes())

    def assertPending(self, action, pending):
        with self.assertRaises(self.module().InstallError) as caught:
            action()
        self.assertEqual((str(caught.exception), caught.exception.pending_action), ('transaction_pending', pending))


class NeighbourMergeTests(LateHarness, unittest.TestCase):
    """The reviewer's repros: a neighbour added during the release copy is merged, every byte kept."""

    def test_a_codex_table_added_during_the_copy_is_kept_by_the_upgrade_then_rollback_and_uninstall(self):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        result = self.install(new, checkpoint=self.at_copy(lambda data: data + WEATHER))
        self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))
        new_root = self.runtime / 'releases' / new[3]
        self.assertUpgradedTo(new_root, old['release_root'])
        self.assertTrue(self.config.read_bytes().endswith(WEATHER))
        self.assertEqual(self.launchctl.loaded[4], str(new_root / 'launcher.py'))
        self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
        self.assertEqual(self.change('rollback')['status'], 'rolled_back')
        self.assertTrue(self.config.read_bytes().endswith(WEATHER))
        removed = self.change('uninstall')
        self.assertEqual((removed['status'], removed['codex']), ('uninstalled', 'removed'))
        self.assertNeighbours(WEATHER)

    def test_a_codex_table_added_during_the_copy_is_kept_by_a_rollback(self):
        _, old = self.full()
        self.install(self.build('b', launcher=b'# second release\n'))
        result = self.change('rollback', checkpoint=self.at_copy(lambda data: data + WEATHER))
        self.assertEqual((result['status'], result['service']), ('rolled_back', 'restarted'))
        self.assertEqual(self.pointer()['release_root'], old['release_root'])
        command, _ = self.commands(old['release_root'])
        self.assertIn(command.encode(), self.config.read_bytes())
        self.assertTrue(self.config.read_bytes().endswith(WEATHER))
        self.assertEqual(self.launchctl.loaded[4], str(Path(old['release_root']) / 'launcher.py'))
        self.assertEqual(self.change('uninstall')['codex'], 'removed')
        self.assertNeighbours(WEATHER)

    def test_a_codex_table_added_during_an_adopting_first_install_is_kept(self):
        self.legacy()
        packet = self.build('a')
        result = self.install(packet, adopt_legacy=True, checkpoint=self.at_copy(lambda data: data + PROFILE))
        self.assertEqual((result['status'], result['service'], result['adopted']),
                         ('activation_required', 'restarted', ['claude_code', 'claude_desktop', 'codex', 'service']))
        data = self.config.read_bytes()
        command, _ = self.commands(result['release_root'])
        self.assertTrue(data.startswith(NEIGHBOUR_CODEX) and data.endswith(PROFILE))
        self.assertIn(command.encode(), data)
        self.assertNotIn(LEGACY_PYTHON.encode(), data)
        self.assertEqual(self.launchctl.loaded[4], str(Path(result['release_root']) / 'launcher.py'))
        removed = self.change('uninstall')
        self.assertEqual((removed['status'], removed['codex']), ('uninstalled', 'removed'))
        self.assertNeighbours(PROFILE)

    def test_our_codex_table_removed_during_the_copy_is_added_back_like_at_the_start(self):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        result = self.install(new, checkpoint=self.at_copy(lambda data: NEIGHBOUR_CODEX))
        self.assertEqual(result['status'], 'upgraded')
        self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])

    def test_claude_desktop_and_claude_code_neighbours_added_during_the_copy_are_kept(self):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        calendar = {'command': '/opt/calendar', 'args': ['--port', '7']}

        def edit(stage):
            if stage == 'release':
                document = json.loads(self.desktop_config.read_text())
                document['mcpServers']['calendar'] = calendar
                document['theme'] = 'dark'
                self.desktop_present(json.dumps(document, indent=2) + '\n')
                claude = self.claude().document()
                claude['mcpServers']['calendar'] = entry('/opt/calendar', ['--port', '7'])
                claude['tipsShown'] = 4
                self.claude().write_document(claude)
        result = self.install(new, checkpoint=edit)
        self.assertEqual((result['claude_code'], result['claude_desktop']), ('registered', 'registered'))
        self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])
        document = json.loads(self.desktop_config.read_text())
        self.assertEqual((document['mcpServers']['calendar'], document['theme']), (calendar, 'dark'))
        claude = self.claude().document()
        self.assertEqual((claude['mcpServers']['calendar'], claude['tipsShown']), (entry('/opt/calendar', ['--port', '7']), 4))


    def test_a_claude_desktop_neighbour_edited_after_a_kill_in_its_write_ahead_window_is_kept(self):
        """Killed after the write-ahead record of our Claude Desktop write, before the write: a neighbour the
        user (or Claude Desktop itself) writes before the resume is kept, and our entry is written over it."""
        for kind in ('first_install', 'upgrade'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'upgrade':
                    _, old = self.full()
                    packet = self.build('b', launcher=b'# second release\n')
                else:
                    self.codex_present()
                    self.desktop_present()
                    packet = self.build('a')
                self.claude()
                self.die(lambda checkpoint: self.install(packet, checkpoint=checkpoint), at='recorded:claude_desktop_add')
                document = json.loads(self.desktop_config.read_text())
                document['theme'] = 'dark'
                self.desktop_present(json.dumps(document, indent=2) + '\n')
                result = self.install(packet)
                self.assertEqual(result['claude_desktop'], 'registered')
                document = json.loads(self.desktop_config.read_text())
                command, args = self.commands(result['release_root'])
                self.assertEqual((document['theme'], document['mcpServers']['weather']), ('dark', {'command': '/opt/w', 'args': []}))
                self.assertEqual(document['mcpServers']['telegram_search'], {'command': command, 'args': args})
                self.assertEqual(oct(self.desktop_config.stat().st_mode & 0o777), '0o644')
                self.assertEqual(self.temporaries(), [])


class LateRefusalTests(LateHarness, unittest.TestCase):
    """A refusal after the broker stop restores the previous definition and broker before it is raised."""

    def test_our_codex_entry_changed_during_the_copy_restores_the_service_then_the_repeat_finishes(self):
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n')
                if kind == 'rollback':
                    self.install(new)

                def action(**kwargs):
                    return self.install(new, **kwargs) if kind == 'upgrade' else self.change('rollback', **kwargs)
                service, metadata = self.service_state(), self.metadata()
                others = {k: v for k, v in self.client_files().items() if not k.endswith('config.toml')}
                boots = self.launchctl.verbs().count('bootout')
                self.assertEqual(self.refused(lambda: action(checkpoint=self.at_copy(
                    lambda data: data.replace(OURS, CHANGED)))), 'target_conflict')
                # The old release keeps serving: its exact definition is back and its broker running again.
                self.assertEqual(self.service_state(), service)
                self.assertEqual(self.launchctl.verbs()[-2:], ['print', 'bootstrap'])
                self.assertEqual(self.launchctl.verbs().count('bootout'), boots + 1)
                self.assertEqual(self.metadata(), metadata)
                self.assertEqual({k: v for k, v in self.client_files().items() if k in others}, others)
                self.assertEqual(self.temporaries(), [])
                # A repeat meets the same conflict and leaves the same service; another kit names the open change
                # (uninstall would go over it: `NoDeadEndTests`).
                self.assertEqual(self.refused(action), 'target_conflict')
                self.assertEqual(self.service_state(), service)
                self.assertPending(lambda: self.install(self.build('c', launcher=b'# third release\n')), kind)
                # Our entry put back: the same command stops the broker again and finishes.
                self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
                result = action()
                self.assertEqual((result['status'], result['service']),
                                 ('upgraded' if kind == 'upgrade' else 'rolled_back', 'restarted'))
                target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                self.assertEqual(self.launchctl.loaded[4], str(target / 'launcher.py'))
                self.assertEqual(self.change('uninstall')['status'], 'uninstalled')

    def test_the_adopted_codex_entry_changed_during_an_adopting_first_install_restores_the_legacy_broker(self):
        backups = self.legacy()
        packet = self.build('a')
        service = self.service_state()
        self.assertEqual(self.refused(lambda: self.install(packet, adopt_legacy=True, checkpoint=self.at_copy(
            lambda data: data.replace(LEGACY_ARGS, LEGACY_CHANGED)))), 'foreign_registration')
        self.assertEqual(self.service_state(), service)
        self.assertEqual(self.plist.read_bytes(), backups['service'])
        self.assertEqual(self.launchctl.verbs()[-2:], ['print', 'bootstrap'])
        self.assertFalse((self.runtime / 'current.json').exists())
        self.assertPending(lambda: self.change('uninstall'), 'install')
        self.assertEqual(self.refused(lambda: self.install(packet, adopt_legacy=True)), 'foreign_registration')
        self.assertEqual(self.service_state(), service)
        self.config.write_bytes(self.config.read_bytes().replace(LEGACY_CHANGED, LEGACY_ARGS))
        result = self.install(packet, adopt_legacy=True)
        self.assertEqual((result['status'], result['service']), ('activation_required', 'restarted'))
        self.assertEqual(self.launchctl.loaded[4], str(Path(result['release_root']) / 'launcher.py'))
        removed = self.change('uninstall')
        self.assertEqual(plistlib.loads((Path(removed['legacy_backups']) / (self.tx(packet).name + '-service')).read_bytes()),
                         plistlib.loads(backups['service']))

    def test_a_broker_that_was_not_running_gets_only_its_definition_back(self):
        self.full(loaded=False)
        new = self.build('b', launcher=b'# second release\n')
        service = self.service_state()
        self.assertEqual(self.refused(lambda: self.install(new, checkpoint=self.at_copy(
            lambda data: data.replace(OURS, CHANGED)))), 'target_conflict')
        self.assertEqual(self.service_state(), service)
        self.assertNotIn('bootstrap', self.launchctl.verbs())
        self.assertNotIn('bootout', self.launchctl.verbs())

    def test_a_later_step_refusal_also_restores_and_leaves_earlier_clients_on_the_new_release(self):
        """A skill copy or the runtime.toml version line changed during the copy refuses after the Codex write:
        Codex already names the new release, which starts only once the repeat commits."""
        for cause in ('skill', 'policy'):
            with self.subTest(cause=cause):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
                skill = self.skills / 'telegram-search'
                policy = self.session_root / 'runtime.toml'
                saved = policy.read_bytes()

                def edit(stage):
                    if stage != 'release':
                        return
                    if cause == 'skill':
                        (skill / 'SKILL.md').chmod(0o600)
                        (skill / 'SKILL.md').write_bytes(b'my own notes on the search skill')
                    else:
                        policy.write_bytes(saved.replace(b'0.52.9', b'9.9.9'))
                service, metadata = self.service_state(), self.metadata()
                self.assertEqual(self.refused(lambda: self.install(new, checkpoint=edit)),
                                 'skill_conflict' if cause == 'skill' else 'policy_conflict')
                self.assertEqual((self.service_state(), self.metadata()), (service, metadata))
                command, _ = self.commands(self.runtime / 'releases' / new[3])
                self.assertIn(command.encode(), self.config.read_bytes())
                self.assertPending(lambda: self.change('rollback'), 'upgrade')
                if cause == 'skill':
                    (skill / 'SKILL.md').unlink()
                    skill.rmdir()  # Removed: the repeat publishes its own copy.
                else:
                    policy.write_bytes(saved)
                result = self.install(new)
                self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))
                self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])

    def test_a_refusal_after_the_version_line_puts_it_back_before_the_broker_starts(self):
        """Refused at the receipt, after `runtime.toml` names the new version: the old broker would refuse that
        pin (`config_stale`), so the restore writes the previous line back first."""
        for kind in ('upgrade', 'adopting first install'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'upgrade':
                    self.full()
                    packet = self.build('b', launcher=b'# second release\n')
                    action = lambda **kwargs: self.install(packet, **kwargs)
                    code, receipt, stage, tx = 'metadata_conflict', self.runtime / 'receipt.json', 'policy', 'change_1'
                    saved = receipt.read_bytes()
                    tamper = lambda: receipt.write_bytes(saved + b' ')
                    untamper = lambda: receipt.write_bytes(saved)
                else:
                    self.legacy()
                    packet = self.build('a')
                    action = lambda **kwargs: self.install(packet, adopt_legacy=True, **kwargs)
                    code, receipt, stage = 'foreign_receipt', self.runtime / 'receipt.json', 'claude_desktop'
                    tx = self.tx(packet).name
                    tamper = lambda: (receipt.write_bytes(b'{}'), receipt.chmod(0o600))
                    untamper = receipt.unlink
                policy = self.session_root / 'runtime.toml'
                service, pin = self.service_state(), policy.read_bytes()

                def edit(value):
                    if value == stage:
                        tamper()
                self.assertEqual(self.refused(lambda: action(checkpoint=edit)), code)
                # The new pin was written (its previous value journaled first), then put back by the restore, whose
                # closing record drops the cycle's write records.
                publications = self.journal(tx)['publications']
                self.assertEqual((publications['policy_previous'], 'policy' in publications, 'policy_restore' in publications),
                                 ([{'version': pin.split(b'expected_package_version = "')[1].split(b'"')[0].decode()}],
                                  False, False))
                self.assertEqual((policy.read_bytes(), self.service_state()), (pin, service))
                self.assertEqual(self.temporaries(), [])
                untamper()
                result = action()
                self.assertEqual(result['service'], 'restarted')
                self.assertIn(b'expected_package_version = "0.54.0rc1"', policy.read_bytes())
                self.assertEqual(self.launchctl.loaded[4], str(Path(result['release_root']) / 'launcher.py'))

    def test_a_kill_inside_the_restore_is_finished_by_the_repeat_with_the_same_refusal(self):
        for stage in ('recorded:service_restore', 'recorded:plist_restore', 'published:plist_restore'):
            with self.subTest(stage=stage):
                self.setUp()
                self.full()
                new = self.build('b', launcher=b'# second release\n')
                service = self.service_state()
                edit = self.at_copy(lambda data: data.replace(OURS, CHANGED))

                def die(checkpoint):
                    def both(value):
                        edit(value)
                        checkpoint(value)
                    self.install(new, checkpoint=both)
                self.die(die, at=stage)
                self.assertEqual(self.journal('change_1')['publications']['service_cycle'],
                                 [{'cycle': 1, 'state': 'restoring', 'code': 'target_conflict'}])
                self.assertEqual(self.refused(lambda: self.install(new)), 'target_conflict')
                self.assertEqual(self.service_state(), service)
                self.assertEqual(self.temporaries(), [])
                journal = self.journal('change_1')['publications']
                self.assertEqual(journal['service_cycle'], [{'cycle': 1, 'state': 'restored'}])
                self.assertFalse({'plist', 'plist_restore'} & set(journal))  # A finished restore ends its cycle's records.

    def test_a_failed_bootout_leaves_the_broker_serving_and_the_repeat_finishes(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        service = self.service_state()
        self.launchctl.fail = {'bootout'}
        self.assertEqual(self.refused(lambda: self.install(new)), 'service_restart_failed')
        self.assertEqual(self.service_state(), service)
        self.assertNotIn('bootstrap', self.launchctl.verbs())
        self.launchctl.fail = set()
        result = self.install(new)
        self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))

    def test_a_definition_changed_after_the_stop_is_never_overwritten(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        foreign = plistlib.dumps({'Label': self.label, 'ProgramArguments': ['/foreign/python', 'x']})

        def replace(stage):
            if stage == 'stopped:service':
                self.plist.write_bytes(foreign)
        self.assertEqual(self.refused(lambda: self.install(new, checkpoint=replace)), 'service_conflict')
        self.assertEqual(self.plist.read_bytes(), foreign)
        self.assertNotIn('bootstrap', self.launchctl.verbs())

    def test_any_number_of_late_refusals_keeps_the_journal_bounded_and_the_repeat_finishes(self):
        """Each repeat is a separate user-initiated run: no cycle limit, and every cycle reuses the same journal
        records, so the journal does not grow with the number of refusals."""
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        service = self.service_state()
        edit = self.at_copy(lambda data: data.replace(OURS, CHANGED))
        shapes = []
        for _ in range(12):
            self.assertEqual(self.refused(lambda: self.install(new, checkpoint=edit)), 'target_conflict')
            self.assertEqual(self.service_state(), service)
            shapes.append({k: len(v) for k, v in self.journal('change_1')['publications'].items()})
        self.assertEqual(shapes[2:], [shapes[1]] * 10)
        self.assertEqual(self.journal('change_1')['publications']['service_cycle'], [{'cycle': 12, 'state': 'restored'}])
        self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
        result = self.install(new)
        self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))
        self.assertOneRelease(self.runtime / 'releases' / new[3])

    def test_a_pending_send_at_the_stop_restores_nothing_and_changes_nothing(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        before, verbs = self.client_files(), len(self.launchctl.verbs())
        self.answers = ['none', 'none', 'pending']
        self.assertEqual(self.refused(lambda: self.install(new)), 'pending_send_unresolved')
        self.assertEqual(self.client_files(), before)
        self.assertNotIn('bootstrap', self.launchctl.verbs()[verbs:])
        publications = self.journal('change_1')['publications']
        self.assertNotIn('service_cycle', publications)


class ParkedHarness(LateHarness):
    """A change (or adopting first install) parked by a late refusal: the old service restored, the change open."""

    def parked(self, kind, cause='codex'):
        """Park `kind` with a late refusal of `cause` and resolve its conflict unless `cause` stays; returns
        (action, target release root). `codex`: our Codex table changed during the copy (before any client
        write). `receipt`: receipt.json changed at the policy step (every client already names the target);
        it is put back. `skill`: our search skill copy edited during the copy (after the Codex write); it stays."""
        if kind == 'adopting first install':
            self.legacy()
            packet = self.build('a')
            action = lambda **kwargs: self.install(packet, adopt_legacy=True, **kwargs)
            target = self.runtime / 'releases' / packet[3]
            ours, changed, code = LEGACY_ARGS, LEGACY_CHANGED, 'foreign_registration'
        else:
            _, old = self.full()
            new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
            if kind == 'rollback':
                self.install(new)
            target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
            action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                lambda **kwargs: self.change('rollback', **kwargs))
            ours, changed, code = OURS, CHANGED, 'target_conflict'
        self.claude()
        self.served = self.service_state()
        if cause == 'codex':
            self.assertEqual(self.refused(lambda: action(checkpoint=self.at_copy(lambda d: d.replace(ours, changed)))), code)
            self.config.write_bytes(self.config.read_bytes().replace(changed, ours))
        elif cause == 'receipt':
            receipt = self.runtime / 'receipt.json'
            saved = receipt.read_bytes()

            def tamper(stage):
                if stage == 'policy':
                    receipt.write_bytes(saved + b' ')
            self.assertEqual(self.refused(lambda: action(checkpoint=tamper)), 'metadata_conflict')
            receipt.write_bytes(saved)
        else:
            skill = self.skills / 'telegram-search'

            def edit(stage):
                if stage == 'release' and (skill / 'SKILL.md').read_bytes() != b'mine':
                    (skill / 'SKILL.md').chmod(0o600)
                    (skill / 'SKILL.md').write_bytes(b'mine')
            self.assertEqual(self.refused(lambda: action(checkpoint=edit)), 'skill_conflict')
        self.assertEqual(self.service_state(), self.served)
        return action, target

    def assertRemoved(self, result, *, kept_skill=None):
        """Uninstall removed every owned object, the parked change's entries included; neighbours stay."""
        self.assertEqual((result['status'], result['service'], result['codex'], result['claude_code'], result['claude_desktop']),
                         ('uninstalled', 'removed', 'removed', 'removed', 'removed'))
        self.assertEqual({k for k, v in result['skills'].items() if v != 'removed'}, {kept_skill} - {None})
        self.assertFalse(self.runtime.exists() or self.plist.exists())
        self.assertIsNone(self.launchctl.loaded)
        self.assertTrue(self.config.read_bytes().startswith(NEIGHBOUR_CODEX))
        self.assertNotIn(b'telegram_search', self.config.read_bytes())
        self.assertNotIn('telegram_search', self.claude().document().get('mcpServers', {}))
        servers = json.loads(self.desktop_config.read_text())['mcpServers']
        self.assertEqual((servers.get('weather'), 'telegram_search' in servers), ({'command': '/opt/w', 'args': []}, False))
        self.assertEqual(self.temporaries(), [])


class RepeatStopTests(ParkedHarness, unittest.TestCase):
    """The service step is idempotent by state: until the commit, every run makes the loaded service the target's
    (stopped again while the old definition is loaded, the plist rewritten until it is the target's), so a kill
    anywhere in the repeat's stop still ends on one release."""

    KINDS = ('upgrade', 'rollback', 'adopting first install')

    def test_a_kill_in_the_repeats_stop_still_rewrites_the_plist(self):
        kills = [dict(at='stopped:service'), dict(at='recorded:plist'), dict(at='published:plist'),
                 dict(after='stopped:service', skip=0), dict(after='stopped:service', skip=1)]
        for kind in self.KINDS:
            for kill in kills:
                with self.subTest(kind=kind, kill=kill):
                    self.setUp()
                    action, target = self.parked(kind)
                    self.die(lambda checkpoint: action(checkpoint=checkpoint), **kill)
                    result = action()
                    self.assertEqual(result['service'], 'restarted')
                    self.assertOneRelease(target)
                    self.assertEqual(self.temporaries(), [])
                    self.assertEqual(self.change('uninstall')['service'], 'removed')
                    self.assertFalse(self.plist.exists())
                    self.assertIsNone(self.launchctl.loaded)

    def test_a_broker_loaded_again_after_the_stop_is_stopped_again(self):
        """A login after the kill loads the old definition still on disk: the resume gates it and boots it out."""
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.setUp()
                action, target = self.parked(kind)
                self.die(lambda checkpoint: action(checkpoint=checkpoint), at='stopped:service')
                self.assertIsNone(self.launchctl.loaded)
                self.launchctl.load()
                boots = self.launchctl.verbs().count('bootout')
                self.answers = ['pending']
                self.assertEqual(self.refused(action), 'pending_send_unresolved')
                self.assertEqual(self.launchctl.verbs().count('bootout'), boots)
                self.assertEqual(self.service_state(), self.served)  # Loaded again: nothing restored over it.
                result = action()
                self.assertEqual(self.launchctl.verbs().count('bootout'), boots + 1)
                self.assertEqual(result['service'], 'restarted')
                self.assertOneRelease(target)

    def test_restart_starts_only_the_target_definition(self):
        """After the commit only the target's plist is bootstrapped: another definition refuses, nothing started."""
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.die(lambda checkpoint: self.install(new, checkpoint=checkpoint), at='complete')
        self.service(old['release_root'], loaded=False)
        verbs = len(self.launchctl.verbs())
        self.assertEqual(self.refused(lambda: self.install(new)), 'service_conflict')
        self.assertNotIn('bootstrap', self.launchctl.verbs()[verbs:])
        self.assertIsNone(self.launchctl.loaded)


class NoDeadEndTests(ParkedHarness, unittest.TestCase):
    """From any parked late-refusal state the repeat finishes once the conflict is resolved, and uninstall
    removes everything the installation owns, the parked change's entries included (by journal ownership)."""

    def test_the_reviewers_late_refusals_after_client_writes_then_repeat_or_uninstall(self):
        """Nine `skill_conflict` refusals after the Codex write: no `recovery_limit`; rollback names the parked
        upgrade; the repeat finishes once the skill copy is removed, and uninstall removes Codex's new entry."""
        for follow in ('repeat', 'uninstall'):
            with self.subTest(follow=follow):
                self.setUp()
                action, target = self.parked('upgrade', cause='skill')
                for _ in range(8):
                    self.assertEqual(self.refused(action), 'skill_conflict')
                    self.assertEqual(self.service_state(), self.served)
                command, _ = self.commands(target)
                self.assertIn(command.encode(), self.config.read_bytes())
                self.assertNotEqual(self.pointer()['release_root'], str(target))
                self.assertPending(lambda: self.change('rollback'), 'upgrade')
                if follow == 'repeat':
                    skill = self.skills / 'telegram-search'
                    (skill / 'SKILL.md').unlink()
                    skill.rmdir()
                    self.assertEqual(action()['status'], 'upgraded')
                    self.assertOneRelease(target)
                    self.assertRemoved(self.change('uninstall'))
                else:
                    self.assertRemoved(self.change('uninstall'), kept_skill='telegram-search')
                    self.assertEqual((self.skills / 'telegram-search' / 'SKILL.md').read_bytes(), b'mine')

    def test_a_kill_during_a_restore_after_many_refusals_then_repeat_and_uninstall(self):
        for follow in ('repeat', 'uninstall'):
            with self.subTest(follow=follow):
                self.setUp()
                self.full()
                new = self.build('b', launcher=b'# second release\n')
                served = self.service_state()
                edit = self.at_copy(lambda data: data.replace(OURS, CHANGED))
                for _ in range(9):
                    self.assertEqual(self.refused(lambda: self.install(new, checkpoint=edit)), 'target_conflict')

                def die(checkpoint):
                    def both(value):
                        edit(value)
                        checkpoint(value)
                    self.install(new, checkpoint=both)
                self.die(die, at='recorded:plist_restore')
                self.assertIsNone(self.launchctl.loaded)
                self.assertPending(lambda: self.change('uninstall'), 'upgrade')  # Not parked until the restore ends.
                self.assertEqual(self.refused(lambda: self.install(new)), 'target_conflict')
                self.assertEqual(self.service_state(), served)
                if follow == 'repeat':
                    self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
                    self.assertEqual(self.install(new)['status'], 'upgraded')
                    self.assertOneRelease(self.runtime / 'releases' / new[3])
                result = self.change('uninstall')
                # Never resolved, our table is still the user's changed one: not ours, so it is kept.
                self.assertEqual((result['status'], result['service'], result['codex']),
                                 ('uninstalled', 'removed', 'removed' if follow == 'repeat' else 'kept'))
                self.assertFalse(self.runtime.exists() or self.plist.exists())
                self.assertIsNone(self.launchctl.loaded)

    def test_uninstall_over_a_parked_change_removes_what_the_change_wrote(self):
        """Parked after every client already names the target: uninstall removes those entries and skill copies
        as the change's own, the broker and plist the restore put back, and the root with both releases."""
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                _, target = self.parked(kind, cause='receipt')
                command, args = self.commands(target)
                self.assertEqual(self.claude().entry, {'type': 'stdio', 'command': command, 'args': args})
                self.assertEqual(json.loads(self.desktop_config.read_text())['mcpServers']['telegram_search'],
                                 {'command': command, 'args': args})
                self.assertIn(command.encode(), self.config.read_bytes())
                if kind == 'upgrade':
                    self.assertPending(lambda: self.change('rollback'), 'upgrade')
                self.assertRemoved(self.change('uninstall'))
                self.assertEqual(self.change('uninstall'), {'status': 'uninstalled', 'installation': 'absent'})

    def test_uninstall_over_a_change_refused_before_its_stop_and_never_over_a_running_one(self):
        """A pending send at the stop gate parks the change with nothing written: uninstall completes. A change
        interrupted with its broker stopped is not parked: uninstall names it, and its repeat finishes."""
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.answers = ['none', 'none', 'pending']
        self.assertEqual(self.refused(lambda: self.install(new)), 'pending_send_unresolved')
        self.assertRemoved(self.change('uninstall'))
        self.setUp()
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.die(lambda checkpoint: self.install(new, checkpoint=checkpoint), at='registration')
        self.assertPending(lambda: self.change('uninstall'), 'upgrade')
        self.assertEqual(self.install(new)['status'], 'upgraded')
        self.assertRemoved(self.change('uninstall'))

    def test_a_refusal_at_the_pointer_switch_is_left_by_the_repeat_or_by_uninstall(self):
        """A disk error while staging current.json comes after receipt.json names the new release: the service is
        restored and the change parked; the repeat switches the pointer, and uninstall accepts the change's own
        receipt.json beside the unchanged pointer."""
        for follow in ('repeat', 'uninstall'):
            with self.subTest(follow=follow):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n')
                served, pointer = self.service_state(), (self.runtime / 'current.json').read_bytes()

                def fail(stage):
                    if stage == 'created:packet:pointer':
                        raise OSError('disk full')
                self.assertEqual(self.refused(lambda: self.install(new, checkpoint=fail)), 'installation_refused')
                self.assertEqual((self.service_state(), (self.runtime / 'current.json').read_bytes()), (served, pointer))
                self.assertEqual(self.receipt()['release_root'], str(self.runtime / 'releases' / new[3]))
                if follow == 'repeat':
                    self.assertEqual(self.install(new)['status'], 'upgraded')
                    self.assertOneRelease(self.runtime / 'releases' / new[3])
                self.assertRemoved(self.change('uninstall'))

    def test_an_object_the_change_wrote_and_the_user_removed_is_written_again(self):
        """Parked after every client names the target, then the user removes an object the change already wrote:
        the repeat writes it again and finishes."""
        for removed in ('skill', 'codex'):
            with self.subTest(removed=removed):
                self.setUp()
                action, target = self.parked('upgrade', cause='receipt')
                if removed == 'skill':
                    skill = self.skills / 'telegram-search'
                    self.assertEqual((skill / 'SKILL.md').read_bytes(), b'search-skill-2')
                    (skill / 'SKILL.md').unlink()
                    skill.rmdir()
                else:
                    self.assertIn(self.commands(target)[0].encode(), self.config.read_bytes())
                    self.config.write_bytes(NEIGHBOUR_CODEX)
                self.assertEqual(action()['status'], 'upgraded')
                self.assertOneRelease(target)
                self.assertEqual((self.skills / 'telegram-search' / 'SKILL.md').read_bytes(), b'search-skill-2')
                self.assertIn(self.commands(target)[0].encode(), self.config.read_bytes())
                self.assertTrue(self.config.read_bytes().startswith(NEIGHBOUR_CODEX))

    def test_a_skill_copy_the_change_published_and_the_user_put_back_is_its_own_again(self):
        """Round 5 (a longer run of the seeded sequences, seed 863): killed after its skill step with our new search
        skill copy edited in place; the repeat refuses `skill_conflict` and stages a fresh copy. Put back exactly
        (same file, same bytes and mode), the copy is still the one an earlier attempt of the change published: the
        next repeat replaces it and finishes, instead of refusing for good. An edit in place that keeps the file's
        size and mode is not that copy: it still refuses."""
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
                if kind == 'rollback':
                    self.install(new)
                action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                    lambda **kwargs: self.change('rollback', **kwargs))
                target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                skill = self.skills / 'telegram-search' / 'SKILL.md'
                saved = self.base / 'saved-skill'

                def die(checkpoint):
                    def both(value):
                        if value == 'claude_code' and not saved.exists():
                            saved.write_text(json.dumps([skill.read_bytes().hex(), skill.stat().st_mode & 0o777]))
                            skill.chmod(0o600)
                            skill.write_bytes(b'mine')
                        checkpoint(value)
                    action(checkpoint=both)
                self.die(die, at='claude_code')
                self.assertEqual(self.refused(action), 'skill_conflict')
                data, mode = json.loads(saved.read_text())
                data = bytes.fromhex(data)
                skill.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # Same file, size and mode; other bytes.
                skill.chmod(mode)
                self.assertEqual(self.refused(action), 'skill_conflict')
                skill.chmod(0o600)
                skill.write_bytes(data)  # In place: the very file the change published.
                skill.chmod(mode)
                self.assertEqual(action()['service'], 'restarted')
                self.assertOneRelease(target)
                self.assertEqual(skill.read_bytes(), (target / 'skills/telegram-search/SKILL.md').read_bytes())
                self.assertRemoved(self.change('uninstall'))

    def test_a_skill_copy_touched_after_the_receipt_is_published_never_blocks(self):
        """0.53.0rc1 re-review (R5h, seeds 10315 and 11124): a change killed once receipt.json names the target
        (published, recorded, or committed and short of its restart), then our search skill copy edited, edited
        and put back, or removed. The skill copies are no longer written once the change's receipt is in place:
        the repeat finishes and leaves the copy as the user left it; uninstall then keeps an edited copy as the
        user's and removes the rest. Before, the repeat re-staged the copy and refused `metadata_conflict` for
        good, and after the commit uninstall refused `transaction_pending` too."""
        for kind in ('upgrade', 'rollback'):
            for at in ('published:receipt', 'receipt', 'complete'):
                for touch in ('edit', 'put back', 'remove'):
                    with self.subTest(kind=kind, at=at, touch=touch):
                        self.setUp()
                        _, old = self.full()
                        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
                        if kind == 'rollback':
                            self.install(new)
                        action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                            lambda **kwargs: self.change('rollback', **kwargs))
                        target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                        self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                        skill = self.skills / 'telegram-search' / 'SKILL.md'
                        written, mode = skill.read_bytes(), skill.stat().st_mode & 0o777
                        if touch == 'remove':
                            skill.unlink()
                            skill.parent.rmdir()
                        else:
                            skill.chmod(0o600)
                            skill.write_bytes(b'mine')
                            if touch == 'put back':
                                skill.write_bytes(written)
                                skill.chmod(mode)
                        result = action()
                        self.assertEqual(result['status'], 'upgraded' if kind == 'upgrade' else 'rolled_back')
                        self.assertOneRelease(target)
                        self.assertEqual(skill.read_bytes() if skill.exists() else None,
                                         {'edit': b'mine', 'put back': written, 'remove': None}[touch])
                        self.assertEqual(action()['installation'], 'reused')
                        removed = self.change('uninstall')
                        self.assertRemoved(removed, kept_skill='telegram-search' if touch != 'put back' else None)
                        self.assertEqual(removed['skills']['telegram-search'],
                                         {'edit': 'kept', 'put back': 'removed', 'remove': 'absent'}[touch])

    def test_a_first_install_with_a_skill_copy_touched_after_its_receipt_finishes(self):
        """The same for a first install: killed once its receipt.json is published (or finished), then our search
        skill copy edited or removed. Before, the repeat refused for good (`installation_refused` for an edit,
        `metadata_conflict` for a removal) and uninstall named the unfinished install."""
        for at in ('published:receipt', 'receipt', None):
            for touch in ('edit', 'remove'):
                with self.subTest(at=at, touch=touch):
                    self.setUp()
                    packet = self.build('a')
                    self.claude()
                    if at is None:
                        self.install(packet)
                    else:
                        self.die(lambda checkpoint: self.install(packet, checkpoint=checkpoint), at=at)
                    skill = self.skills / 'telegram-search' / 'SKILL.md'
                    if touch == 'remove':
                        skill.unlink()
                        skill.parent.rmdir()
                    else:
                        skill.chmod(0o600)
                        skill.write_bytes(b'mine')
                    result = self.install(packet)
                    self.assertEqual((result['status'], result['installation']),
                                     ('activation_required', 'reused' if at is None else 'installed'))
                    self.assertEqual(self.install(packet)['installation'], 'reused')
                    self.assertEqual(skill.read_bytes() if skill.exists() else None, b'mine' if touch == 'edit' else None)
                    removed = self.change('uninstall')
                    self.assertEqual((removed['status'], removed['skills']['telegram-search']),
                                     ('uninstalled', 'kept' if touch == 'edit' else 'absent'))
                    self.assertFalse(self.runtime.exists())

    def test_uninstall_over_a_parked_change_removes_a_client_entry_written_before_its_status(self):
        """0.53.0rc1 re-review (R5j, R5k, seed 10387): killed right after the change wrote the Claude Desktop entry
        (or ran `claude mcp add`) and before it recorded that client's outcome, then parked by a late refusal at
        the repeat (our skill copy edited, then put back). Ownership comes from the change's write-ahead record,
        so uninstall removes the entry while it is exactly what the change wrote (it was kept, naming the removed
        release). Killed before the write, the older entry is the installation's and is removed as before (for
        Claude Code the older entry was already removed then, write-ahead, so nothing is left: `absent`)."""
        for kind in ('upgrade', 'rollback'):
            for at in ('published:claude_desktop_add', 'recorded:claude_desktop_add', 'published:claude_add',
                       'recorded:claude_add'):
                with self.subTest(kind=kind, at=at):
                    self.setUp()
                    _, old = self.full()
                    new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
                    if kind == 'rollback':
                        self.install(new)
                    action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                        lambda **kwargs: self.change('rollback', **kwargs))
                    target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                    self.claude()
                    self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                    command, args = self.commands(target)
                    written = ({'command': command, 'args': args} if 'desktop' in at else
                               {'type': 'stdio', 'command': command, 'args': args})
                    entry = (json.loads(self.desktop_config.read_text())['mcpServers'].get('telegram_search')
                             if 'desktop' in at else self.claude().entry)
                    self.assertEqual(entry == written, at.startswith('published:'))
                    skill = self.skills / 'telegram-search' / 'SKILL.md'
                    saved, mode = skill.read_bytes(), skill.stat().st_mode & 0o777
                    skill.chmod(0o600)
                    skill.write_bytes(b'mine')
                    self.assertEqual(self.refused(action), 'skill_conflict')
                    skill.write_bytes(saved)
                    skill.chmod(mode)
                    removed = self.change('uninstall')
                    if at == 'recorded:claude_add':
                        # Our older entry was removed (write-ahead) before the kill, and nothing was added.
                        self.assertEqual(removed['claude_code'], 'absent')
                        removed = {**removed, 'claude_code': 'removed'}
                    self.assertRemoved(removed)

    def test_our_codex_table_changed_after_the_commit_never_blocks_the_repeat(self):
        """The same after the commit (killed short of its restart) for our Codex table: changed or removed, the
        repeat finishes and leaves it; uninstall keeps a changed table as the user's."""
        for kind in ('upgrade', 'rollback'):
            for touch in ('change', 'remove'):
                with self.subTest(kind=kind, touch=touch):
                    self.setUp()
                    _, old = self.full()
                    new = self.build('b', launcher=b'# second release\n')
                    if kind == 'rollback':
                        self.install(new)
                    action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                        lambda **kwargs: self.change('rollback', **kwargs))
                    target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                    self.die(lambda checkpoint: action(checkpoint=checkpoint), at='complete')
                    self.config.write_bytes(self.config.read_bytes().replace(OURS, CHANGED) if touch == 'change'
                                            else NEIGHBOUR_CODEX)
                    self.assertEqual(action()['service'], 'restarted')
                    self.assertOneRelease(target)
                    removed = self.change('uninstall')
                    self.assertEqual((removed['status'], removed['codex']),
                                     ('uninstalled', 'kept' if touch == 'change' else 'absent'))
                    self.assertFalse(self.runtime.exists() or self.plist.exists())

    def test_a_killed_uninstall_over_a_parked_change_is_finished_by_its_retry(self):
        for stage in ('stopped:service', 'registration', 'claude_code', 'claude_desktop', 'skills', 'retired'):
            with self.subTest(stage=stage):
                self.setUp()
                self.parked('upgrade', cause='receipt')
                self.die(lambda checkpoint: self.change('uninstall', checkpoint=checkpoint), at=stage)
                self.assertPending(lambda: self.install(self.build('c', launcher=b'# third\n')), 'uninstall')
                self.assertRemoved(self.change('uninstall'))


class CodexWriteWindowTests(ParkedHarness, unittest.TestCase):
    """Our Codex table changed between the write's record and its replace, or right after it: the documented code
    (`target_conflict`, `foreign_registration` when adopting), never `installation_refused`."""

    def test_our_codex_table_changed_around_its_write_refuses_with_the_documented_code(self):
        for kind in RepeatStopTests.KINDS:
            for at in ('recorded:registration', 'published:registration'):
                with self.subTest(kind=kind, at=at):
                    self.setUp()
                    if kind == 'adopting first install':
                        self.legacy()
                        packet = self.build('a')
                        action = lambda **kwargs: self.install(packet, adopt_legacy=True, **kwargs)
                        target = self.runtime / 'releases' / packet[3]
                        # Before the replace the file still holds the adopted entry; after it, ours.
                        mine = (LEGACY_ARGS, LEGACY_CHANGED) if at == 'recorded:registration' else (OURS, CHANGED)
                        code = 'foreign_registration'
                    else:
                        _, old = self.full()
                        new = self.build('b', launcher=b'# second release\n')
                        if kind == 'rollback':
                            self.install(new)
                        target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                        action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                            lambda **kwargs: self.change('rollback', **kwargs))
                        mine, code = (OURS, CHANGED), 'target_conflict'
                    served = self.service_state()
                    saved = []

                    def edit(stage):
                        if stage == at and not saved:
                            saved.append(self.config.read_bytes())
                            self.assertIn(mine[0], saved[0])
                            self.config.write_bytes(saved[0].replace(*mine))
                    self.assertEqual(self.refused(lambda: action(checkpoint=edit)), code)
                    self.assertEqual(self.service_state(), served)
                    self.config.write_bytes(saved[0])
                    self.assertEqual(action()['service'], 'restarted')
                    self.assertOneRelease(target)

    def test_refusals_in_the_codex_write_window_run_after_run_never_reach_recovery_limit(self):
        """0.53.0rc1 re-review (R5a): our table changed between the write's record and its replace, then put back,
        run after run. Every attempt stayed journaled, so from the ninth run on the repeat refused `recovery_limit`
        for good (only uninstall ended it). A finished restore now keeps only the attempt that proves the change's
        own write, so each run the user starts has fresh attempts: the plain repeat finishes."""
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n')
                if kind == 'rollback':
                    self.install(new)
                target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                    lambda **kwargs: self.change('rollback', **kwargs))

                def edit(stage):
                    if stage == 'recorded:registration':
                        self.config.write_bytes(self.config.read_bytes().replace(OURS, CHANGED))
                for _ in range(10):
                    self.assertEqual(self.refused(lambda: action(checkpoint=edit)), 'target_conflict')
                    self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
                name = max(self.changes(), key=lambda n: int(n[7:]))
                self.assertLessEqual(len(self.journal(name)['publications'].get('registration', [])), 1)
                self.assertEqual(action()['service'], 'restarted')
                self.assertOneRelease(target)
                self.assertEqual(self.change('uninstall')['codex'], 'removed')

    def test_a_neighbour_added_in_the_codex_write_window_is_merged(self):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')

        def edit(stage):
            if stage == 'recorded:registration' and WEATHER not in self.config.read_bytes():
                self.config.write_bytes(self.config.read_bytes() + WEATHER)
        result = self.install(new, checkpoint=edit)
        self.assertEqual((result['status'], result['service']), ('upgraded', 'restarted'))
        self.assertTrue(self.config.read_bytes().endswith(WEATHER))
        self.assertOneRelease(self.runtime / 'releases' / new[3])


class LoginRestoreTests(ParkedHarness, unittest.TestCase):
    """0.53 critic round 4: launchd loads the plist on disk at login, so after a kill past the plist rewrite a
    login starts the target release's broker. The restore is idempotent by state, like the stop: the target's
    broker it finds loaded is booted out before the previous plist is written back, and the previous broker is
    bootstrapped again when it was running before the change; a loaded broker that is neither ours stays and
    refuses `service_conflict`. Before the fix every later repeat refused `service_conflict` and uninstall
    `transaction_pending` (the reviewer's `R4`, `R4c` and `R4d`)."""

    def prepare(self, kind):
        """(action, target, edit, clear): `edit` is a checkpoint that makes the repeat refuse late (our Codex table
        changed at `service`; for an adopting first install a skill directory of the user's at `registration`)."""
        skill = self.skills / 'telegram-search'
        if kind == 'adopting first install':
            self.legacy()
            packet = self.build('a')
            action = lambda **kwargs: self.install(packet, adopt_legacy=True, **kwargs)
            target = self.runtime / 'releases' / packet[3]

            def edit(stage):
                if stage == 'registration' and not skill.exists():
                    skill.mkdir(mode=0o700)
                    (skill / 'SKILL.md').write_bytes(b'mine')

            def clear():
                (skill / 'SKILL.md').unlink()
                skill.rmdir()
            code = 'foreign_telegram-search'
        else:
            _, old = self.full()
            new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
            if kind == 'rollback':
                self.install(new)
            target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
            action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                lambda **kwargs: self.change('rollback', **kwargs))

            def edit(stage):
                if stage == 'service':
                    self.config.write_bytes(self.config.read_bytes().replace(OURS, CHANGED))

            def clear():
                self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
            code = 'target_conflict'
        self.claude()
        self.served = self.service_state()
        self.pin = (self.session_root / 'runtime.toml').read_bytes()
        return action, target, edit, clear, code

    def login(self, target):
        """launchd at login: the plist on disk, here the target's, is loaded."""
        self.assertIsNone(self.launchctl.loaded)
        self.launchctl.load()
        self.assertEqual(self.launchctl.loaded[4], str(target / 'launcher.py'))

    def assertServed(self):
        """The old service exactly as before the change: plist bytes, loaded broker and runtime.toml pin."""
        self.assertEqual((self.service_state(), (self.session_root / 'runtime.toml').read_bytes()), (self.served, self.pin))
        self.assertEqual(self.temporaries(), [])

    def assertFinishes(self, action, target):
        result = action()
        self.assertEqual(result['service'], 'restarted')
        self.assertOneRelease(target)
        self.assertEqual(self.change('uninstall')['service'], 'removed')
        self.assertFalse(self.runtime.exists() or self.plist.exists())
        self.assertIsNone(self.launchctl.loaded)

    def test_a_login_after_a_kill_past_the_plist_rewrite_then_a_late_refusal_restores_the_old_broker(self):
        for kind in ('upgrade', 'rollback'):
            for at in ('published:plist', 'service', 'registration', 'claude_code', 'policy'):
                with self.subTest(kind=kind, at=at):
                    self.setUp()
                    action, target, edit, clear, code = self.prepare(kind)
                    self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                    self.login(target)
                    boots = self.launchctl.verbs().count('bootout')
                    self.assertEqual(self.refused(lambda: action(checkpoint=edit)), code)
                    self.assertServed()
                    self.assertEqual(self.launchctl.verbs().count('bootout'), boots + 1)  # The target's broker.
                    self.assertEqual(self.launchctl.verbs()[-2:], ['print', 'bootstrap'])
                    # Parked: uninstall would go over it; another kit names it.
                    self.assertPending(lambda: self.install(self.build('c', launcher=b'# third release\n')), kind)
                    clear()
                    self.assertFinishes(action, target)

    def test_a_login_after_a_kill_inside_the_restore_is_finished_by_the_repeat(self):
        """Killed inside the restore, before or after the previous plist is back: the login loads the plist on
        disk, the target's or the previous one; the repeat finishes the restore with the same code."""
        for kind in ('upgrade', 'rollback'):
            for at in ('recorded:service_restore', 'recorded:plist_restore', 'published:plist_restore'):
                with self.subTest(kind=kind, at=at):
                    self.setUp()
                    action, target, edit, clear, code = self.prepare(kind)

                    def die(checkpoint):
                        def both(value):
                            edit(value)
                            checkpoint(value)
                        action(checkpoint=both)
                    self.die(die, at=at)
                    self.assertIsNone(self.launchctl.loaded)
                    self.launchctl.load()
                    clear()
                    self.assertPending(lambda: self.change('uninstall'), kind)  # Not parked until the restore ends.
                    self.assertEqual(self.refused(action), code)
                    self.assertServed()
                    self.assertFinishes(action, target)

    def test_an_adopting_first_install_puts_the_legacy_broker_back_not_the_new_one(self):
        for at in ('published:plist', 'registration'):
            with self.subTest(at=at):
                self.setUp()
                action, target, edit, clear, code = self.prepare('adopting first install')
                self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                self.login(target)
                self.assertEqual(self.refused(lambda: action(checkpoint=edit)), code)
                self.assertServed()  # The legacy plist with the legacy broker loaded.
                self.assertFalse((self.runtime / 'current.json').exists())
                clear()
                result = action()
                self.assertEqual((result['status'], result['service']), ('activation_required', 'restarted'))
                self.assertOneRelease(target)

    def test_an_old_broker_a_login_started_in_a_change_that_found_none_running_is_stopped_again(self):
        """The change began with no broker running; a login since loaded the plist on disk, the old release's
        (the change parked by a late refusal, or killed after its stop record). The next run gates that broker
        and stops it before the plist rewrite, and the commit starts none, as none ran before the change: the old
        broker never serves on the new pin, and the next login starts the new release's."""
        for how in ('parked', 'killed'):
            with self.subTest(how=how):
                self.setUp()
                _, old = self.full(loaded=False)
                new = self.build('b', launcher=b'# second release\n')
                target = self.runtime / 'releases' / new[3]
                if how == 'parked':
                    self.assertEqual(self.refused(lambda: self.install(new, checkpoint=self.at_copy(
                        lambda data: data.replace(OURS, CHANGED)))), 'target_conflict')
                    self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
                else:
                    self.die(lambda checkpoint: self.install(new, checkpoint=checkpoint), at='stopped:service')
                self.assertIsNone(self.launchctl.loaded)
                self.launchctl.load()
                self.assertEqual(self.launchctl.loaded[4], str(Path(old['release_root']) / 'launcher.py'))
                served = self.service_state()
                self.answers = ['pending']  # Gated before it is stopped: a pending send refuses, nothing changed.
                self.assertEqual(self.refused(lambda: self.install(new)), 'pending_send_unresolved')
                self.assertEqual(self.service_state(), served)
                result = self.install(new)
                self.assertEqual((result['status'], result['service']), ('upgraded', 'not_loaded'))
                self.assertIsNone(self.launchctl.loaded)
                self.launchctl.load()  # The next login.
                self.assertOneRelease(target)

    def test_the_previous_plist_bootstrapped_between_the_bootout_and_the_rewrite_is_stopped(self):
        """0.53.0rc1 re-review (R5d): a concurrent `connect` or a manual bootstrap loads the previous plist right
        after the stop's bootout, before the rewrite. The commit then found the previous broker loaded beside the
        target's plist and refused `service_conflict` for good (uninstall named the change). It is the previous
        definition the change kept, so it is gated and booted out like any other: the restart starts the target's
        when a broker ran before, and none otherwise."""
        for kind in ('upgrade', 'rollback'):
            for loaded in (True, False):
                with self.subTest(kind=kind, loaded=loaded):
                    self.setUp()
                    _, old = self.full(loaded=loaded)
                    new = self.build('b', launcher=b'# second release\n')
                    if kind == 'rollback':
                        self.install(new)
                        if not loaded:
                            self.launchctl.loaded = None
                    target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
                    action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                        lambda **kwargs: self.change('rollback', **kwargs))
                    previous = self.plist.read_bytes()

                    def bootstrap(stage):
                        if stage == 'stopped:service' and self.launchctl.loaded is None:
                            self.assertEqual(self.plist.read_bytes(), previous)
                            self.launchctl.load()
                    result = action(checkpoint=bootstrap)
                    self.assertEqual(result['service'], 'restarted' if loaded else 'not_loaded')
                    if not loaded:
                        self.assertIsNone(self.launchctl.loaded)
                        self.launchctl.load()  # The next login.
                    self.assertOneRelease(target)
                    self.assertEqual(self.change('uninstall')['service'], 'removed')
                    self.assertIsNone(self.launchctl.loaded)

    def test_with_no_broker_before_the_change_the_restart_reads_no_plist(self):
        """The previous-broker check after the commit never turns a change that found no broker running into a
        refusal: a plist changed by hand after a kill at `complete` is not read (`not_loaded`, as before)."""
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                _, old = self.full(loaded=False)
                new = self.build('b', launcher=b'# second release\n')
                if kind == 'rollback':
                    self.install(new)
                    self.launchctl.loaded = None
                action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
                    lambda **kwargs: self.change('rollback', **kwargs))
                self.claude()
                self.die(lambda checkpoint: action(checkpoint=checkpoint), at='complete')
                payload = plistlib.loads(self.plist.read_bytes())
                payload['ProcessType'] = 'Interactive'
                self.plist.write_bytes(plistlib.dumps(payload))
                self.assertEqual(action()['service'], 'not_loaded')
                self.assertIsNone(self.launchctl.loaded)

    def test_a_broker_neither_ours_stays_loaded_and_refuses(self):
        """Neither the target's nor the previous broker: never booted out, `service_conflict` until it is gone;
        then the repeat finishes the restore with the recorded code, and the next one the change."""
        action, target, edit, clear, code = self.prepare('upgrade')
        self.die(lambda checkpoint: action(checkpoint=checkpoint), at='published:plist')
        foreign = ['/foreign/python', 'x']
        self.launchctl.loaded = foreign
        boots = self.launchctl.verbs().count('bootout')
        self.assertEqual(self.refused(lambda: action(checkpoint=edit)), 'service_conflict')
        self.assertEqual((self.launchctl.loaded, self.launchctl.verbs().count('bootout')), (foreign, boots))
        self.assertEqual(self.refused(action), 'service_conflict')
        self.launchctl.loaded = None  # The user stops the foreign job.
        clear()
        self.assertEqual(self.refused(action), code)
        self.assertServed()
        self.assertFinishes(action, target)


class ParkedTemporaryTests(ParkedHarness, unittest.TestCase):
    """0.53 critic round 4: a kill during the Claude Desktop write leaves our temporary, a copy of the user's
    config, beside it; a repeat refused before the Desktop step parked the change with it, and uninstall removed
    the root but left the file for good (the reviewer's `R4e`). A late refusal's restore now removes every
    temporary the change's journal binds beside each client file it touches, and so does every finishing action:
    uninstall over a parked change (one an earlier build parked with it included) and the commit."""

    def killed_in_a_client_write(self, kind, where):
        """`kind` killed right after our temporary in `where` is created; returns (action, its directory)."""
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        if kind == 'rollback':
            self.install(new)
        action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
            lambda **kwargs: self.change('rollback', **kwargs))
        self.claude()
        directory = {'desktop': self.desktop_config.parent, 'session': self.session_root, 'plist': self.plist.parent}[where]
        self.die(lambda checkpoint: action(checkpoint=checkpoint), directory=directory)
        self.assertEqual([Path(p).parent for p in self.temporaries()], [directory.relative_to(self.base)])
        return action, directory

    def refuse_at_codex(self, action):
        def edit(stage):
            if stage == 'service':
                self.config.write_bytes(self.config.read_bytes().replace(OURS, CHANGED))
        self.assertEqual(self.refused(lambda: action(checkpoint=edit)), 'target_conflict')
        self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))

    def test_a_late_refusal_after_a_kill_in_a_client_write_leaves_no_temporary(self):
        for kind in ('upgrade', 'rollback'):
            for where in ('desktop', 'session', 'plist'):
                with self.subTest(kind=kind, where=where):
                    self.setUp()
                    action, _ = self.killed_in_a_client_write(kind, where)
                    self.refuse_at_codex(action)
                    self.assertEqual(self.temporaries(), [])
                    self.assertRemoved(self.change('uninstall'))

    def test_uninstall_over_a_change_parked_with_a_client_temporary_removes_it(self):
        """A change parked with the temporary (as the round-4 build's restore left it): uninstall removes it."""
        for kind in ('upgrade', 'rollback'):
            with self.subTest(kind=kind):
                self.setUp()
                action, directory = self.killed_in_a_client_write(kind, 'desktop')
                with mock.patch.object(self.module(), '_client_temporaries'):
                    self.refuse_at_codex(action)
                self.assertEqual([Path(p).parent for p in self.temporaries()], [directory.relative_to(self.base)])
                self.assertRemoved(self.change('uninstall'))

    def test_the_repeat_of_a_change_parked_with_a_client_temporary_removes_it(self):
        action, directory = self.killed_in_a_client_write('upgrade', 'desktop')
        with mock.patch.object(self.module(), '_client_temporaries'):
            self.refuse_at_codex(action)
        self.assertEqual(len(self.temporaries()), 1)
        self.assertEqual(action()['status'], 'upgraded')
        self.assertEqual(self.temporaries(), [])


class RemovedPlistTests(ParkedHarness, unittest.TestCase):
    """0.53.0rc1 re-review (R5f, R5g; reproduced on 05fee70): the broker plist removed by hand while a change is
    unfinished after its stop. Every repeat refused `service_restart_failed` (the stop expected the plist to exist,
    the restore bootstrapped a plist that was gone) and uninstall named the change, for good. A missing plist is
    now nothing to stop: the stop writes the target's definition anew; the restore brings the previous one back
    from its journaled copy (or, with none kept, starts nothing); after the commit the restart writes the target's
    again. A registration still loaded from the removed plist's path that runs one of the two releases is ours."""

    def kinds(self, kind, *, loaded=True):
        _, old = self.full(loaded=loaded)
        new = self.build('b', launcher=b'# second release\n')
        if kind == 'rollback':
            self.install(new)
        action = (lambda **kwargs: self.install(new, **kwargs)) if kind == 'upgrade' else (
            lambda **kwargs: self.change('rollback', **kwargs))
        target = self.runtime / 'releases' / new[3] if kind == 'upgrade' else Path(old['release_root'])
        previous = Path(old['release_root']) if kind == 'upgrade' else self.runtime / 'releases' / new[3]
        self.claude()
        return action, target, previous

    def late(self, value):
        if value == 'service':
            self.config.write_bytes(self.config.read_bytes().replace(OURS, CHANGED))

    def put_back(self):
        self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))

    def test_a_parked_change_whose_launchagent_was_removed_by_hand_finishes(self):
        for kind in ('upgrade', 'rollback'):
            for cleanup in ('bootout and remove', 'remove only'):
                for follow in ('repeat', 'uninstall'):
                    with self.subTest(kind=kind, cleanup=cleanup, follow=follow):
                        self.setUp()
                        action, target, _ = self.kinds(kind)
                        self.assertEqual(self.refused(lambda: action(checkpoint=self.late)), 'target_conflict')
                        self.put_back()
                        if cleanup == 'bootout and remove':
                            self.launchctl.loaded = None
                        self.plist.unlink()
                        if follow == 'repeat':
                            result = action()
                            self.assertEqual((result['status'], result['service']),
                                             ('upgraded' if kind == 'upgrade' else 'rolled_back', 'restarted'))
                            self.assertOneRelease(target)
                        removed = self.change('uninstall')
                        self.assertEqual(removed['status'], 'uninstalled')
                        self.assertEqual(removed['service'], 'removed' if follow == 'repeat' or
                                         cleanup == 'remove only' else 'absent')
                        self.assertFalse(self.runtime.exists() or self.plist.exists())
                        self.assertIsNone(self.launchctl.loaded)

    def test_a_plist_removed_after_a_kill_past_the_stop_is_written_anew_by_the_repeat(self):
        for kind in ('upgrade', 'rollback'):
            for at in ('stopped:service', 'service', 'policy', 'complete'):
                with self.subTest(kind=kind, at=at):
                    self.setUp()
                    action, target, _ = self.kinds(kind)
                    self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                    self.plist.unlink()
                    result = action()
                    self.assertEqual(result['service'], 'restarted')
                    self.assertOneRelease(target)
                    self.assertEqual(self.change('uninstall')['service'], 'removed')
                    self.assertFalse(self.runtime.exists() or self.plist.exists())

    def test_a_plist_removed_after_a_kill_then_a_late_refusal_restores_the_kept_copy(self):
        """Killed past the stop, the plist removed, then a repeat refused late: the restore writes the previous
        definition back from the transaction's copy when the stop had kept one (and starts its broker, which ran
        before), else it starts nothing; either way the change is parked, and its repeat or uninstall finishes."""
        for kind in ('upgrade', 'rollback'):
            for at in ('stopped:service', 'service', 'policy'):
                for follow in ('repeat', 'uninstall'):
                    with self.subTest(kind=kind, at=at, follow=follow):
                        self.setUp()
                        action, target, previous = self.kinds(kind)
                        before = self.plist.read_bytes()
                        self.die(lambda checkpoint: action(checkpoint=checkpoint), at=at)
                        kept = (self.runtime / 'transactions').glob('change_*/service.previous')
                        kept = [p for p in kept if p.read_bytes() == before]
                        self.plist.unlink()
                        self.assertEqual(self.refused(lambda: action(checkpoint=self.late)), 'target_conflict')
                        if kept:
                            self.assertEqual(self.plist.read_bytes(), before)
                            self.assertEqual(self.launchctl.loaded[4], str(previous / 'launcher.py'))
                        else:
                            self.assertFalse(self.plist.exists())
                            self.assertIsNone(self.launchctl.loaded)
                        self.put_back()
                        if follow == 'repeat':
                            self.assertEqual(action()['service'], 'restarted')
                            self.assertOneRelease(target)
                        removed = self.change('uninstall')
                        self.assertEqual((removed['status'], removed['service']),
                                         ('uninstalled', 'removed' if kept or follow == 'repeat' else 'absent'))
                        self.assertFalse(self.runtime.exists() or self.plist.exists())
                        self.assertIsNone(self.launchctl.loaded)


if __name__ == '__main__':
    unittest.main()
