"""Client entry ownership across upgrade, rollback and uninstall (CLIENTS-13).

The installation owns exactly the Claude Code and Claude Desktop entries its own transactions wrote: a rollback owns
what it wrote, and an upgrade whose client step ended otherwise (a CLI failure, no CLI, an unreadable config, a kill
in a write-ahead window) keeps owning the entry it left in place, so the next upgrade replaces it and uninstall
removes it. Disposable homes and fakes only.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from claude_fake import entry
from test_bundle_upgrade import ChangeHarness, OLD_VERSION
from telegram_search_mcp import claude_desktop_registration as desktop


class OwnershipTests(ChangeHarness, unittest.TestCase):

    def entries(self):
        document = json.loads(self.desktop_config.read_text()) if self.desktop_config.exists() else {}
        return self.claude().entry, document.get('mcpServers', {}).get('telegram_search')

    def at(self, release_root):
        command, args = self.commands(release_root)
        return entry(command, args), desktop.entry(command, args)

    def test_a_rollback_owns_the_entries_it_wrote_and_uninstall_removes_them(self):
        for v1 in (False, True):
            with self.subTest(v1=v1):
                self.setUp()
                self.codex_present()
                self.session()
                (self.home / '.claude').mkdir(mode=0o755)
                # A: no claude CLI and no Claude Desktop folder, so neither entry is the installer's.
                with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
                    first = self.install(self.build('a', version=OLD_VERSION), claude_candidates=())
                self.assertEqual((first['claude_code'], first['claude_desktop']), ('not_found', 'skipped'))
                if v1:
                    self.as_v1()
                self.desktop_present()
                b = self.build('b', launcher=b'# second release\n')
                up = self.install(b)
                self.assertEqual((up['claude_code'], up['claude_desktop']), ('registered', 'registered'))
                back = self.change('rollback')
                self.assertEqual((back['claude_code'], back['claude_desktop']), ('registered', 'registered'))
                self.assertEqual(self.entries(), self.at(first['release_root']))
                receipt = self.receipt()
                self.assertEqual((receipt['schema_version'], receipt['claude_code'], receipt['claude_desktop']),
                                 (1 if v1 else 2, 'registered', 'registered'))
                if not v1:
                    owned = receipt['claude_desktop_ownership']
                    self.assertEqual((owned['transaction_id'], owned['post_subtree_hash']),
                                     ('change_2', hashlib.sha256(desktop.subtree(*self.commands(first['release_root']))).hexdigest()))
                # The newer kit again moves both entries; back again; uninstall removes what the rollback wrote.
                again = self.install(b)
                self.assertEqual((again['claude_code'], again['claude_desktop']), ('registered', 'registered'))
                self.assertEqual(self.entries(), self.at(self.runtime / 'releases' / b[3]))
                self.change('rollback')
                removed = self.change('uninstall')
                self.assertEqual((removed['claude_code'], removed['claude_desktop']), ('removed', 'removed'))
                self.assertEqual(self.entries(), (None, None))

    def test_a_rollback_whose_step_ended_otherwise_keeps_owning_the_newer_entry(self):
        _, old = self.full()
        b = self.build('b', launcher=b'# second release\n')
        self.install(b)
        self.claude().fail = {'remove'}
        self.desktop_config.chmod(0o664)
        back = self.change('rollback')
        self.assertEqual((back['claude_code'], back['claude_desktop']), ('failed', 'unavailable'))
        self.assertEqual(self.receipt()['claude_code'], 'failed')
        self.claude().fail = set()
        self.desktop_config.chmod(0o644)
        self.assertEqual(self.entries(), self.at(self.runtime / 'releases' / b[3]))
        removed = self.change('uninstall')
        self.assertEqual((removed['claude_code'], removed['claude_desktop']), ('removed', 'removed'))
        self.assertEqual(self.entries(), (None, None))

    def broken_upgrade(self, case):
        """A full A install, then the upgrade to B with one client step not ending `registered`; A's entry stays."""
        _, old = self.full()
        b = self.build('b', launcher=b'# second release\n')
        fake = self.claude()
        if case == 'claude remove fails':
            fake.fail = {'remove'}
            result = self.install(b)
        elif case == 'no claude CLI':
            result = self.install(b, claude_candidates=())
        elif case == 'killed after the claude_remove record':
            def crash(value):
                if value == 'recorded:claude_remove': raise RuntimeError('simulated crash')
            with self.assertRaises(RuntimeError):
                self.install(b, checkpoint=crash)
            result = self.install(b)
        else:
            self.desktop_config.chmod(0o664)
            result = self.install(b)
        fake.fail = set()
        self.desktop_config.chmod(0o644)
        expected = {'claude remove fails': ('failed', 'registered'), 'no claude CLI': ('not_found', 'registered'),
                    'killed after the claude_remove record': ('failed', 'registered'),
                    'desktop config unreadable': ('registered', 'unavailable')}[case]
        self.assertEqual((result['status'], result['claude_code'], result['claude_desktop']), ('upgraded', *expected))
        left = self.at(old['release_root'])
        now = self.entries()
        if expected[0] != 'registered':
            self.assertEqual(now[0], left[0])
        if expected[1] != 'registered':
            self.assertEqual(now[1], left[1])
        return old, b

    CASES = ('claude remove fails', 'no claude CLI', 'killed after the claude_remove record', 'desktop config unreadable')

    def test_an_upgrade_keeps_owning_the_entry_its_step_left_and_the_next_upgrade_replaces_it(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.setUp()
                self.broken_upgrade(case)
                c = self.build('c', launcher=b'# third release\n')
                result = self.install(c)
                self.assertEqual((result['status'], result['claude_code'], result['claude_desktop']),
                                 ('upgraded', 'registered', 'registered'))
                self.assertEqual(self.entries(), self.at(self.runtime / 'releases' / c[3]))
                self.assertEqual(self.receipt()['claude_desktop_ownership']['transaction_id'], 'change_2')
                removed = self.change('uninstall')
                self.assertEqual((removed['claude_code'], removed['claude_desktop']), ('removed', 'removed'))
                self.assertEqual(self.entries(), (None, None))

    def test_uninstall_removes_the_entry_a_broken_upgrade_left(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.setUp()
                old, _ = self.broken_upgrade(case)
                removed = self.change('uninstall')
                self.assertEqual((removed['claude_code'], removed['claude_desktop']), ('removed', 'removed'))
                self.assertEqual(self.entries(), (None, None))
                self.assertFalse(Path(old['release_root']).exists())

    def test_an_entry_the_installer_never_wrote_stays_unowned(self):
        """A `reused` entry (already exact, not written by any transaction) is not adopted by a later failure."""
        self.codex_present()
        self.session()
        (self.home / '.claude').mkdir(mode=0o755)
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            a = self.build('a', version=OLD_VERSION)
            command, args = self.commands(self.runtime / 'releases' / a[3])
            self.claude().entry = entry(command, args)  # The user's own copy of the printed command.
            first = self.install(a)
        self.assertEqual(first['claude_code'], 'reused')
        removed = self.change('uninstall')
        self.assertEqual(removed['claude_code'], 'kept')
        self.assertEqual(self.claude().entry, entry(command, args))


if __name__ == '__main__':
    unittest.main()
