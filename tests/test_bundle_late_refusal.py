"""Edits during a change (0.53 critic round 3), over disposable homes only.

A client file the user edits while the release is copied is merged: only a change of our own entry (or, when
adopting, of the adopted foreign entry) counts as a conflict. launchctl, the claude CLI and the pending-send
probe are fakes; nothing real is read or written.
"""
import json
import os
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


if __name__ == '__main__':
    unittest.main()
