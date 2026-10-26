"""A first install over a kept default session (final acceptance; the final audit's open item 1, 0.53/0.54 behaviour).

An uninstall keeps the default's session and its runtime.toml. Before this fix a first install moved that file's
`expected_package_version` line only when it adopted a legacy broker, so after an uninstall and a first install at
another version the default broker met `config_stale` and the next upgrade `policy_conflict`. Now a first install over
a kept session moves the line onto the installed release from an equal or older value, journaled (`policy_previous`,
`policy`) as the alias repair moves an alias's (account-add, review fix 055d4), and refuses `policy_conflict` before
the transaction for a newer value, whose session an older release must not serve, or for a line it cannot rewrite
exactly. A fresh install with no kept session, or over a line already naming the release, is byte-identical to before.
Disposable homes and fakes only.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from test_account_default_goldens import JOURNALS_054
from test_bundle_upgrade import OLD_VERSION, ChangeHarness
from telegram_search_mcp import config

NEW = ChangeHarness.build.__kwdefaults__['version']


class KeptSessionTests(ChangeHarness, unittest.TestCase):
    def kept(self):
        """The older release installed with every client and a session pinning it, then uninstalled: the session stays."""
        self.full()
        self.claude()
        self.assertEqual(self.change('uninstall')['status'], 'uninstalled')
        self.assertFalse(self.runtime.exists())
        return (self.session_root / 'runtime.toml').read_bytes()

    def journal(self, packet):
        return json.loads((self.runtime / 'transactions' / ('install_' + packet[3][:24]) / 'journal.json').read_bytes())

    def policy(self):
        return (self.session_root / 'runtime.toml').read_bytes()

    def test_a_first_install_over_a_kept_older_session_moves_its_version_line_journaled(self):
        before = self.kept()
        hashes = self.session_hashes()
        packet = self.build('b', launcher=b'# second release\n')
        result = self.install(packet)
        self.assertEqual((result['status'], result['installation'], result['package_version']),
                         ('activation_required', 'installed', NEW))
        self.assertEqual(self.policy(), before.replace(b'"%s"' % OLD_VERSION.encode(), b'"%s"' % NEW.encode()))
        self.assertEqual(self.session_hashes(), hashes)  # enrollment.json and the TDLib session untouched
        records = self.journal(packet)['publications']
        self.assertEqual(records['policy_previous'], [{'version': OLD_VERSION}])
        self.assertEqual([sorted(attempt) for attempt in records['policy']], [['object', 'temporary']])
        self.assertEqual(set(records) - set(JOURNALS_054['install']['records']), {'policy', 'policy_previous'})
        # The default broker of the installed release accepts its pin (no config_stale) ...
        with mock.patch('telegram_search_mcp.__version__', NEW):
            config.load_runtime_policy(self.session_root / 'runtime.toml').require_current()
        # ... and the next upgrade moves it on (no policy_conflict).
        upgraded = self.install(self.build('c', launcher=b'# third release\n', search=b'search-skill-3'))
        self.assertEqual(upgraded['status'], 'upgraded')
        self.assertIn(b'expected_package_version = "%s"' % NEW.encode(), self.policy())

    def test_an_equal_line_and_a_fresh_install_without_a_session_stay_byte_identical(self):
        for kind in ('equal', 'fresh'):
            with self.subTest(kind=kind):
                self.setUp()
                self.codex_present()
                self.desktop_present()
                if kind == 'equal':
                    self.session(pin=NEW)
                    before = self.policy()
                packet = self.build('a')
                self.assertEqual(self.install(packet)['status'], 'activation_required')
                records = set(self.journal(packet)['publications'])
                self.assertFalse(records & {'policy', 'policy_previous', 'policy_restore'})
                if kind == 'equal':
                    self.assertEqual(self.policy(), before)
                else:
                    self.assertFalse(self.session_root.exists())

    def test_a_line_naming_the_release_in_another_valid_form_changes_nothing(self):
        """An owner's hand-edited line that TOML and the broker read as this release (trailing spaces, an inline
        comment) is left as it is: the exact installer form is required only of a line that must move."""
        for kind, tail in (('trailing_spaces', b'  '), ('inline_comment', b'  # pinned')):
            with self.subTest(kind=kind):
                self.setUp()
                before = self.kept()
                line = b'expected_package_version = "%s"' % OLD_VERSION.encode()
                self.assertEqual(before.count(line), 1)
                text = before.replace(line, b'expected_package_version = "%s"%s' % (NEW.encode(), tail))
                path = self.session_root / 'runtime.toml'
                path.write_bytes(text)
                with mock.patch('telegram_search_mcp.__version__', NEW):
                    config.load_runtime_policy(path).require_current()  # the broker accepts the file as it is
                packet = self.build('b', launcher=b'# second release\n')
                self.assertEqual(self.install(packet)['status'], 'activation_required')
                self.assertEqual(path.read_bytes(), text)
                self.assertFalse(set(self.journal(packet)['publications']) & {'policy', 'policy_previous',
                                                                               'policy_restore'})

    def test_a_newer_or_unrewritable_line_refuses_policy_conflict_before_the_transaction(self):
        for kind, text in (('newer', 'config_version = 1\nexpected_package_version = "9.9.9"\n'
                                     'expected_contract_version = 1\nenabled_capabilities = ["read", "artifacts"]\n'),
                           ('two_lines', 'config_version = 1\nexpected_package_version = "%s"\n'
                                         'expected_package_version = "%s"\n' % (OLD_VERSION, OLD_VERSION)),
                           ('unquoted', 'config_version = 1\nexpected_package_version = 0.52\n'),
                           # An older line that must move is rewritten only in the exact installer form.
                           ('older_with_comment', 'config_version = 1\nexpected_package_version = "%s"  # pinned\n'
                                                  'expected_contract_version = 1\n' % OLD_VERSION)):
            with self.subTest(kind=kind):
                self.setUp()
                self.kept()
                path = self.session_root / 'runtime.toml'
                path.write_text(text)
                watched = (path, self.codex / 'config.toml', self.desktop_config)
                snapshot = {p: p.read_bytes() for p in watched}
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual(str(caught.exception), 'policy_conflict')
                self.assertEqual({p: p.read_bytes() for p in watched}, snapshot)
                self.assertFalse(self.runtime.exists())
                self.assertFalse(self.plist.exists())
                self.assertEqual(self.claude().verbs()[-1:], ['remove'])  # the uninstall's; nothing since

    def test_a_resumed_first_install_moves_the_line_by_state_once(self):
        before = self.kept()
        packet = self.build('b', launcher=b'# second release\n')

        def interrupt(stage):
            if stage == 'receipt':
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.install(packet, checkpoint=interrupt)
        moved = before.replace(b'"%s"' % OLD_VERSION.encode(), b'"%s"' % NEW.encode())
        self.assertEqual(self.policy(), moved)
        self.assertEqual(self.install(packet)['status'], 'activation_required')
        self.assertEqual(self.policy(), moved)
        records = self.journal(packet)['publications']
        self.assertEqual((records['policy_previous'], len(records['policy'])), ([{'version': OLD_VERSION}], 1))
        self.assertEqual(hashlib.sha256(self.policy()).hexdigest(), hashlib.sha256(moved).hexdigest())


if __name__ == '__main__':
    unittest.main()
