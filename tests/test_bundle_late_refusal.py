"""Edits during a change and refusals after the broker stop (0.53 critic round 3), over disposable homes only.

A client file the user edits while the release is copied is merged: only a change of our own entry (or, when
adopting, of the adopted foreign entry) counts as a conflict. Any refusal after the broker stop and before the
commit first restores the previous service, byte for byte, through a journaled step: the old release keeps
serving and the same command finishes the transaction once the named object is put back. launchctl, the claude
CLI and the pending-send probe are fakes; nothing real is read or written.
"""
import json
from pathlib import Path
import plistlib
import unittest

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
                # A repeat meets the same conflict and leaves the same service; other actions name the open change.
                self.assertEqual(self.refused(action), 'target_conflict')
                self.assertEqual(self.service_state(), service)
                self.assertPending(lambda: self.change('uninstall'), kind)
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
                self.assertEqual(len(self.journal(tx)['publications']['policy']), 1)  # The new pin was written.
                self.assertEqual((policy.read_bytes(), self.service_state()), (pin, service))
                self.assertEqual(self.temporaries(), [])
                untamper()
                result = action()
                self.assertEqual(result['service'], 'restarted')
                self.assertIn(b'expected_package_version = "0.53.0.dev0"', policy.read_bytes())
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
                self.assertEqual(self.refused(lambda: self.install(new)), 'target_conflict')
                self.assertEqual(self.service_state(), service)
                self.assertEqual(self.temporaries(), [])
                journal = self.journal('change_1')['publications']
                self.assertEqual((len(journal['service_restoring']), len(journal['service_restored'])), (1, 1))
                self.assertEqual(journal['service_restoring'][0]['code'], 'target_conflict')

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

    def test_late_refusals_are_bounded_and_the_last_refuses_before_any_stop(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        service = self.service_state()
        edit = self.at_copy(lambda data: data.replace(OURS, CHANGED))
        for _ in range(8):
            self.assertEqual(self.refused(lambda: self.install(new, checkpoint=edit)), 'target_conflict')
        boots = self.launchctl.verbs().count('bootout')
        self.assertEqual(self.refused(lambda: self.install(new)), 'recovery_limit')
        self.assertEqual(self.launchctl.verbs().count('bootout'), boots)
        self.assertEqual(self.service_state(), service)

    def test_a_pending_send_at_the_stop_restores_nothing_and_changes_nothing(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        before, verbs = self.client_files(), len(self.launchctl.verbs())
        self.answers = ['none', 'none', 'pending']
        self.assertEqual(self.refused(lambda: self.install(new)), 'pending_send_unresolved')
        self.assertEqual(self.client_files(), before)
        self.assertNotIn('bootstrap', self.launchctl.verbs()[verbs:])
        publications = self.journal('change_1')['publications']
        self.assertFalse({'service_restoring', 'service_restored', 'service_stopped'} & set(publications))


if __name__ == '__main__':
    unittest.main()
