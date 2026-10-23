"""0.55 phase C: the approval dialog names the alias and a masked account once a registry exists (spec 3C).

Without a registry the broker passes the 0.54 arguments and the dialog is the 0.54 bytes (the script hashes here,
the per-kind document goldens in their own suites). With one, every kind's heading names the alias and its
summary carries `Account: <alias> (Telegram account ••<last two digits>)` in place of the user id, which never
reaches the dialog's argv. No real dialog runs: osascript is always patched.
"""
from __future__ import annotations

import ast
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from approval_layout import logical
from test_approval_prompt import captured, file_arguments, file_preview, text_arguments, text_preview
import test_archive_edit as archive_suite
import test_forward_send as forward_suite
import test_video_send as video_suite

from telegram_search_mcp import account_registry, approval_prompt
from telegram_search_mcp.approval_prompt import decide_approved_send, render_approval

USER = 7401234561
MASKED = 'Account: work (Telegram account ••61)'
# The two dialog scripts as 0.55 phase B (3d251c9) runs them; without a registry they never change.
SEND_SCRIPT = 'e3e30de874ef7dbf0c0b3c4a961a14b35ac2bbc45615bc3fdb2cd5ebcf106eac'
CHANGE_SCRIPT = '03b971783aeb064b32e11ffedcede20ef48b4e9558aecac47464111438a744d7'
SOURCE = Path(approval_prompt.__file__)


def script_of(argv):
    return argv[argv.index('-e') + 1]


def big_text_arguments(text='Hello'):
    return text_arguments(text, draft_preview=text_preview(text, account_id=USER))


class NoRegistryTests(unittest.TestCase):
    def test_the_scripts_are_the_054_bytes(self):
        for template, expected in ((approval_prompt._SCRIPT, SEND_SCRIPT), (approval_prompt._CHANGE_SCRIPT, CHANGE_SCRIPT)):
            self.assertEqual(hashlib.sha256(approval_prompt._dialog_script(template).encode()).hexdigest(), expected)
        _, calls = captured(text_arguments('Hello'))
        self.assertEqual(hashlib.sha256(script_of(calls[0][0]).encode()).hexdigest(), SEND_SCRIPT)
        texts, scripts = archive_suite.shown(archive_suite.archive_preview())
        self.assertEqual(hashlib.sha256(scripts[0].encode()).hexdigest(), CHANGE_SCRIPT)

    def test_no_alias_renders_exactly_as_before(self):
        for arguments in (text_arguments('Hello'), file_arguments(file_preview())):
            self.assertEqual(render_approval(**arguments, account_alias=None), render_approval(**arguments))
            self.assertIn('\nAccount: 7\n', render_approval(**arguments)[0])

    def test_one_helper_writes_every_account_line(self):
        tree = ast.parse(SOURCE.read_text())
        owners = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  for child in ast.walk(node) if isinstance(child, ast.Constant) and type(child.value) is str
                  and 'Account: ' in child.value]
        self.assertEqual(set(owners), {'_account_line'})


class AliasDialogTests(unittest.TestCase):
    def assert_named(self, documents, scripts, masked=MASKED, user=USER):
        self.assertEqual(len(documents), 1)
        rows = documents[0].split('\n')
        self.assertEqual([row for row in rows if row.startswith('Account:')], [masked])
        self.assertNotIn(str(user), documents[0])
        self.assertIn(' for account \\u00abwork\\u00bb', scripts[0])
        self.assertNotIn(str(user), scripts[0])

    def test_text_names_the_alias_and_masks_the_account(self):
        decision, calls = captured(dict(big_text_arguments(), account_alias='work'))
        self.assertEqual(decision.outcome, 'declined')
        self.assert_named([logical(calls[0][0][-1])], [script_of(calls[0][0])])
        plain = render_approval(**big_text_arguments())[0]
        named = render_approval(**big_text_arguments(), account_alias='work')[0]
        self.assertEqual(named, plain.replace(f'Account: {USER}', MASKED))  # Only the account line changes.

    def test_every_kind_names_the_alias(self):
        masked7 = 'Account: work (Telegram account ••7)'
        document = file_preview(caption='See')
        voice = file_preview('voice_note', b'OggS synthetic', 'memo.ogg', 'audio/ogg', duration_seconds=3,
                             waveform_base64='AAAA', source_sha256='b' * 64, source_display_name='memo.m4a',
                             converted=True)
        for name, arguments in (('document', file_arguments(document)), ('voice', file_arguments(voice)),
                                ('photo', file_arguments(file_preview('photo', b'\x89PNG x', 'a.png', 'image/png')))):
            with self.subTest(kind=name):
                _, calls = captured(dict(arguments, account_alias='work'))
                self.assert_named([logical(calls[0][0][-1])], [script_of(calls[0][0])], masked7, 'Account: 7')
        reply = __import__('test_approval_prompt').LayoutTests('test_reply_artifact_goldens').reply_artifact(document)
        _, calls = captured(dict(file_arguments(document, draft_preview=None, reply_artifact_preview=reply),
                                 account_alias='work'))
        self.assert_named([logical(calls[0][0][-1])], [script_of(calls[0][0])], masked7, 'Account: 7')
        for suite in (forward_suite, video_suite):
            with self.subTest(kind=suite.__name__):
                documents = suite.shown(suite.preview(), account_alias='work')
                self.assertEqual([row for row in documents[0].split('\n') if row.startswith('Account:')], [masked7])
        for builder, key in ((archive_suite.archive_preview, 'archive'), (archive_suite.edit_preview, 'text_edit')):
            with self.subTest(kind=key):
                texts, scripts = archive_suite.shown(**{key: builder()}, account_alias='work')
                self.assert_named(texts, scripts, masked7, 'Account: 7')

    def test_the_default_alias_is_named_too(self):
        _, calls = captured(dict(big_text_arguments(), account_alias='default'))
        self.assertIn('\nAccount: default (Telegram account ••61)\n', logical(calls[0][0][-1]))
        self.assertIn(' for account \\u00abdefault\\u00bb', script_of(calls[0][0]))

    def test_an_alias_outside_the_grammar_shows_no_dialog(self):
        for alias in ('Work', 'a' * 13, '', 'wo rk', '«x»', 7, True):
            with self.subTest(alias=alias):
                decision, calls = captured(dict(big_text_arguments(), account_alias=alias))
                self.assertEqual((decision.outcome, calls), ('dialog_unavailable', []))


class BrokerDialogTests(archive_suite.Fixture, unittest.TestCase):
    """The broker passes the alias to the dialog only once a registry exists."""

    def registry(self, alias):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='r')
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        os.chmod(home, 0o700)
        account_registry.add_alias('work', home, longest_tool='t')
        self.broker._accounts_root = account_registry.accounts_root(home)
        self.broker._alias = alias
        return self.broker._accounts_root

    def test_without_a_registry_the_dialog_gets_the_054_arguments(self):
        did = self.prepare_archive()['draft_id']
        self.assertEqual(self.apply_archive(did)['status'], 'applied')
        self.assertEqual(len(self.approvals), 1)
        self.assertNotIn('account_alias', self.approvals[0])
        self.assertIn('\nAccount: 7\n', render_approval(**self.approvals[0])[0])

    def check_named(self, alias):
        accounts = self.registry(alias)
        did = self.prepare_archive()['draft_id']
        self.assertEqual(self.apply_archive(did)['status'], 'applied')
        self.assertEqual(self.approvals[0]['account_alias'], alias)
        document = render_approval(**self.approvals[0])[0]
        self.assertIn(f'\nAccount: {alias} (Telegram account ••7)\n', document)
        self.assertNotIn('\nAccount: 7\n', document)
        registry = account_registry.load(accounts)  # The broker's own pin ran before the dialog.
        self.assertEqual(dict(registry.entries)['work'] if alias == 'work' else registry.default_user_id,
                         archive_suite.ACCOUNT)

    def test_with_a_registry_the_dialog_names_a_named_brokers_alias(self):
        self.check_named('work')

    def test_with_a_registry_the_dialog_names_the_default_brokers_alias(self):
        self.check_named('default')

if __name__ == '__main__':
    unittest.main()
