"""Codex ownership is our table's own keys and values, never the comments or blank lines around it.

tomlkit attaches a comment written after our last key (above the next table, or at the end of the file) to our
table. Such a comment is the user's: it must not change the ownership hash, and an upgrade, a removal or an adoption
replaces only our table's own lines and keeps every other byte, that comment included, exactly. Disposable data.
"""
import json
from pathlib import Path
import unittest

from test_bundle_upgrade import ChangeHarness, NEIGHBOUR_CODEX
from telegram_search_mcp import codex_registration as cr


def registration(name):
    root = '/owned/releases/%s' % name
    return cr.Registration(root + '/python/bin/python3', ('-I', '-S', '-B', root + '/launcher.py', 'mcp'), {}, ('_manifest',))


BASE = b'# preserved neighbor\nmodel = "example"\n'
FIRST = cr.merge_registration(BASE, registration('a'))
OURS = FIRST.candidate[len(BASE) + 1:]  # Appended after one separating newline.
OWNER = cr.Receipt('install_test', 'first', None, FIRST.post_subtree_hash, None)
# The user's edits around our untouched table: (text before our table, text after it).
EDITS = {
    'a comment above a later table': (b'', b'\n# my weather server\n[mcp_servers.weather]\ncommand = "/opt/w"\n'),
    'a comment above an unrelated table': (b'', b'\n# note\n\n[profiles.x]\nmodel = "y"\n'),
    'a comment at the end of the file': (b'', b'# trailing comment\n'),
    'comments and blank lines between tables': (b'', b'\n\n# one\n\n# two\n[mcp_servers.other]\ncommand = "/o"  # inline\n'),
    'a comment above our table': (b'# about telegram\n', b''),
    'comments on both sides': (b'# about telegram\n', b'# after\n[profiles.q]\ny = 2\n'),
}


def edited(before, after, ours=OURS):
    return BASE + b'\n' + before + ours + after


class OwnershipHashTests(unittest.TestCase):

    def test_comments_around_our_table_keep_the_ownership_hash(self):
        for name, (before, after) in EDITS.items():
            with self.subTest(edit=name):
                source = edited(before, after)
                self.assertEqual(cr._digest(cr.owned_subtree(source)), FIRST.post_subtree_hash)

    def test_an_upgrade_replaces_only_our_lines(self):
        wanted = cr.merge_registration(BASE, registration('b')).candidate[len(BASE) + 1:]
        for name, (before, after) in EDITS.items():
            with self.subTest(edit=name):
                change = cr.merge_registration(edited(before, after), registration('b'), OWNER)
                self.assertEqual(change.candidate, edited(before, after, wanted))
                self.assertEqual(change.post_subtree_hash, cr._digest(cr.owned_subtree(change.candidate)))

    def test_a_removal_keeps_every_other_byte(self):
        for name, (before, after) in EDITS.items():
            with self.subTest(edit=name):
                change = cr.rollback_registration(edited(before, after), OWNER)
                self.assertEqual(change.candidate, BASE + b'\n' + before + after)
                self.assertIsNone(cr.owned_subtree(change.candidate))

    def test_an_adoption_keeps_the_comment_after_a_foreign_table(self):
        foreign = b'[mcp_servers.telegram_search]\ncommand = "/legacy/051"\n'
        wanted = cr.merge_registration(BASE, registration('a')).candidate[len(BASE) + 1:]
        for name, (before, after) in EDITS.items():
            with self.subTest(edit=name):
                change = cr.adopt_registration(edited(before, after, foreign), registration('a'))
                self.assertEqual(change.candidate, edited(before, after, wanted))

    def test_a_change_inside_our_table_still_counts(self):
        """Our own keys, values and the comments between or on them are ours: changing them is a change."""
        for changed in (OURS.replace(b'required = true', b'required = false'),
                        OURS.replace(b'enabled = true\n', b'enabled = true\n# mine\n'),
                        OURS.replace(b'required = true', b'required = true  # mine')):
            with self.subTest(changed=changed):
                self.assertNotEqual(cr._digest(cr.owned_subtree(edited(b'', b'', changed))), FIRST.post_subtree_hash)
                with self.assertRaises(cr.ConfigConflict):
                    cr.merge_registration(edited(b'', b'', changed), registration('b'), OWNER)


class InstallerTests(ChangeHarness, unittest.TestCase):
    """The critic's repro: a commented neighbour appended after install no longer blocks upgrade or uninstall."""

    def test_upgrade_and_uninstall_keep_the_users_comment_and_neighbours(self):
        for name, (_, after) in EDITS.items():
            if not after:
                continue
            with self.subTest(edit=name):
                self.setUp()
                self.full()
                config = self.codex / 'config.toml'
                config.write_bytes(config.read_bytes() + after)
                result = self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual(result['status'], 'upgraded')
                data = config.read_bytes()
                self.assertTrue(data.startswith(NEIGHBOUR_CODEX) and data.endswith(after), data)
                self.assertIn(str(self.runtime / 'releases').encode(), data)
                removed = self.change('uninstall')
                self.assertEqual(removed['codex'], 'removed')
                self.assertEqual(config.read_bytes(), NEIGHBOUR_CODEX + b'\n' + after)


if __name__ == '__main__':
    unittest.main()
