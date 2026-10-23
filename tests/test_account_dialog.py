"""0.55 phase C: the approval dialog names the alias and a masked account once a registry exists (spec 3C).

Without a registry the broker passes the 0.54 arguments and the dialog is the 0.54 bytes (the script hashes here,
the per-kind document goldens in their own suites). With one, every kind's heading names the alias and its
summary carries `Account: <alias> (Telegram account ••<last two digits>)` in place of the user id, which never
reaches the dialog's argv: the owner's own chat is "Saved Messages" and the owner as a sender or topic creator
"you". Two of these substitutions also hold without a registry, where 0.54 printed the user id: the own chat and
a forwarded own message's sender (goldens below). No real dialog runs: osascript is always patched.
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

from approval_layout import logical, tail
from test_approval_prompt import (DID, EXPIRES, RUN, captured, file_arguments, file_preview, text_arguments,
                                  text_preview)
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


SAVED = 'Saved Messages'


def saved_text(account, text='Hello'):
    """A text send to the owner's own chat (Saved Messages), whose chat id is the user id."""
    return text_arguments(text, recipient=account, recipient_title=SAVED,
                          draft_preview=text_preview(text, account_id=account, recipient=account, recipient_title=SAVED))


def own_forward(account, *, saved=False):
    """A forward of the owner's own message, from a group or (saved) from Saved Messages to Saved Messages."""
    from telegram_search_mcp.forward_drafts import ForwardChat, ForwardDraftPreview, ForwardSourcePreview
    from telegram_search_mcp.forward_sources import ForwardSnapshot, ForwardSource
    source_chat = account if saved else forward_suite.SOURCE_CHAT
    raw = forward_suite.source_message(None, chat_id=source_chat, is_outgoing=True,
                                       sender_id={'@type': 'messageSenderUser', 'user_id': account})
    meta = (forward_suite.chat(account, SAVED, 'private', native=account) if saved
            else forward_suite.chat(source_chat, 'Source Group'))
    snapshot = ForwardSnapshot.create(meta, source_chat, ForwardSource.from_message(raw, source_chat, 55),
                                      dict(forward_suite.PROPERTIES))
    recipient = ForwardChat(chat_id=account, title=SAVED) if saved else ForwardChat(chat_id=forward_suite.RECIPIENT,
                                                                                    title='Recipient')
    value = ForwardDraftPreview.create(draft_id=forward_suite.DID, account_id=account, expires_at=forward_suite.EXPIRY,
                                       mode='forward', remove_caption=False,
                                       source=ForwardSourcePreview.create(snapshot), recipient=recipient)
    return value, dict(recipient=recipient.chat_id, recipient_title=recipient.title)


class OwnIdGoldenTests(unittest.TestCase):
    """The owner's own chat is "Saved Messages" and the owner as a sender is "you", never a number (review fix).

    Without a registry 0.54 printed `(chat <user id>)` and `Sender: user <user id>` in these two cases; both take the
    same substitution there, deliberately, and nothing else changes (the account line keeps the 0.54 user id)."""

    def test_a_send_to_saved_messages_names_it_without_a_number(self):
        sha = hashlib.sha256(b'Hello').hexdigest()
        body = ('\n\nText (exact):\n│ «Hello»\n' +
                tail(f'Draft: {DID}', 'Bytes: 5', f'Text SHA-256: {sha}',
                     closing='Unofficial Telegram MCP will make one send attempt.'))
        _, calls = captured(saved_text(7))
        self.assertEqual(logical(calls[0][0][-1]), 'Send text to «Saved Messages» (Saved Messages)\nAccount: 7\n'
                                                   f'Expires: {EXPIRES}' + body)
        _, calls = captured(dict(saved_text(USER), account_alias='work'))
        self.assertEqual(logical(calls[0][0][-1]), 'Send text to «Saved Messages» (Saved Messages)\n'
                                                   f'{MASKED}\nExpires: {EXPIRES}' + body)
        self.assertFalse(any(str(USER) in part for part in calls[0][0]))

    def test_a_forwarded_own_message_names_its_sender_you(self):
        tail_of = lambda value: tail(f'Draft: {DID}', f'Source SHA-256: {value.source.source_sha256}',
                                     f'Preview SHA-256: {value.preview_sha256}',
                                     closing='Unofficial Telegram MCP will revalidate the source, its permissions and '
                                             'the recipient, then make one forward attempt.')
        content = ('Date: 2023-11-14T22:13:20+00:00\nContent: text\n'
                   'Text [untrusted Telegram evidence] (exact):\n│ «Source text»\n')
        value, where = own_forward(forward_suite.ACCOUNT)
        self.assertEqual(forward_suite.shown(value, **where), [
            'Forward a message to «Recipient» (chat 4242)\nMode: FORWARD — recipients see the original sender\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\nSource chat: «Source Group» (chat -1001234)\n'
            'Message ID: 55\nSender [untrusted Telegram evidence]: you\n' + content + tail_of(value)])
        value, where = own_forward(USER, saved=True)
        seen = []
        with patch(RUN, side_effect=lambda argv, **options: seen.append(argv) or
                   SimpleNamespace(returncode=0, stdout=b'DECLINED')):
            self.assertEqual(decide_approved_send(**forward_arguments(value, where), account_alias='work').outcome,
                             'declined')
        self.assertEqual(logical(seen[0][-1]),
            'Forward a message to «Saved Messages» (Saved Messages)\nMode: FORWARD — recipients see the original '
            f'sender\n{MASKED}\nExpires: 2030-01-02T03:04:05+00:00\n\nSource chat: «Saved Messages» (Saved Messages)\n'
            'Message ID: 55\nSender [untrusted Telegram evidence]: you\n' + content + tail_of(value))
        self.assertFalse(any(str(USER) in part for part in seen[0]))

    def test_another_users_chat_and_message_keep_their_numbers(self):
        self.assertIn('(chat 123)\nAccount: 7\n', render_approval(**text_arguments('Hello'))[0])
        self.assertIn('Sender [untrusted Telegram evidence]: user 8\n', forward_suite.shown(forward_suite.preview())[0])
        # A private chat with another user has that user's id as its chat id: still a number, never "Saved Messages".
        other = text_arguments('Hello', recipient=8, draft_preview=text_preview('Hello', recipient=8))
        self.assertIn('Send text to «Synthetic Recipient» (chat 8)\n', render_approval(**other)[0])

    def test_one_helper_names_every_chat(self):
        tree = ast.parse(SOURCE.read_text())
        owners = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  for child in ast.walk(node) if isinstance(child, ast.JoinedStr)
                  for part in child.values if isinstance(part, ast.Constant) and type(part.value) is str
                  and part.value.endswith(('(chat ', ' in chat '))}
        self.assertEqual(owners, set())
        self.assertIn("f'chat {chat_id}'", SOURCE.read_text())


def forward_arguments(value, where):
    return dict(display_name='Forwarded message', size_bytes=1, sha256=value.source.source_sha256, caption='',
                kind='forward', forward_preview=value.model_dump(mode='json'), **where)


class OwnIdEveryKindTests(unittest.TestCase):
    """With a registry no kind's dialog carries the user id: its own chat, its own messages, its own topics."""

    def assert_hidden(self, arguments):
        seen = []
        with patch(RUN, side_effect=lambda argv, **options: seen.append(argv) or
                   SimpleNamespace(returncode=0, stdout=b'DECLINED')):
            self.assertEqual(decide_approved_send(**arguments, account_alias='work').outcome, 'declined')
        self.assertEqual(len(seen), 1)
        self.assertFalse(any(str(USER) in part for part in seen[0]))
        self.assertIn(f'\n{MASKED}\n', logical(seen[0][-1]))
        return logical(seen[0][-1])

    def test_files_replies_and_formatted_text_to_saved_messages(self):
        document = dict(file_preview(caption='See'), account_id=USER, recipient=USER, recipient_title=SAVED)
        self.assertIn('(Saved Messages)\n', self.assert_hidden(file_arguments(document, recipient=USER,
                                                                              recipient_title=SAVED)))
        reply = saved_reply_artifact(document)
        shown = self.assert_hidden(file_arguments(document, draft_preview=None, reply_artifact_preview=reply,
                                                  recipient=USER, recipient_title=SAVED))
        self.assertIn('\nIn reply to message 55 in Saved Messages [untrusted Telegram evidence] (rendering):\n', shown)
        formatted = saved_formatted()
        data = formatted['text'].encode()
        self.assert_hidden(dict(recipient_title=SAVED, recipient=USER, display_name='Text message',
                                size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), caption='',
                                kind='text', text=formatted['text'], topic_preview=formatted))

    def test_a_topic_the_owner_created_names_its_creator_you(self):
        value = owner_topic_text()
        data = value['text'].encode()
        arguments = dict(recipient_title='Chat', recipient=value['recipient'], display_name='Text message',
                         size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), caption='', kind='text',
                         text=value['text'], topic_preview=value)
        self.assertIn('\nTopic created: 1700000000; creator: you\n', self.assert_hidden(arguments))
        # Without a registry the technical line is 0.54's (the account line prints the user id there anyway).
        self.assertIn(f'\nTopic created: 1700000000; creator: user {USER}\n', render_approval(**arguments)[0])

    def test_video_archive_and_edit_in_saved_messages(self):
        from telegram_search_mcp.archive_drafts import ArchiveChat, ArchiveDraftPreview
        from telegram_search_mcp.edit_drafts import EditChat, EditSnapshot, TextEditDraftPreview
        from telegram_search_mcp.video_drafts import VideoRecipient
        video = video_suite.preview(account_id=USER, recipient=VideoRecipient(chat_id=USER, title=SAVED))
        self.assert_hidden(dict(recipient_title=SAVED, recipient=USER, display_name=video.raw_display_name,
                                size_bytes=video.size_bytes, sha256=video.sha256, caption='', kind='video',
                                video_preview=video.model_dump(mode='json')))
        archive = ArchiveDraftPreview.create(draft_id=DID, account_id=USER, expires_at=archive_suite.EXPIRY,
                                             chat=ArchiveChat(chat_id=USER, title=SAVED, kind='private'),
                                             current_list='main', target_list='archive')
        shown = self.assert_hidden(dict(recipient_title=SAVED, recipient=USER, display_name='Chat list move',
                                        size_bytes=1, sha256=archive_suite.archive_sha(archive), caption='',
                                        kind='archive', archive_preview=archive.model_dump(mode='json')))
        self.assertTrue(shown.startswith('Move chat «Saved Messages» (Saved Messages, private) from Main to Archive\n'))
        message = archive_suite.own_message(chat_id=USER, sender_id={'@type': 'messageSenderUser', 'user_id': USER})
        snapshot = EditSnapshot.create(archive_suite.chat(USER, SAVED, 'private', native=USER), USER, message,
                                       archive_suite.MID, dict(archive_suite.PROPERTIES))
        edit = TextEditDraftPreview.create(draft_id=DID, account_id=USER, expires_at=archive_suite.EXPIRY,
                                           chat=EditChat(chat_id=USER, title=SAVED, kind='private'), snapshot=snapshot,
                                           new_text='Hello there')
        shown = self.assert_hidden(dict(recipient_title=SAVED, recipient=USER, display_name='Chat list move',
                                        size_bytes=1, sha256=edit.source_sha256, caption='', kind='text_edit',
                                        text_edit_preview=edit.model_dump(mode='json')))
        self.assertTrue(shown.startswith(f'Edit your message {archive_suite.MID} in «Saved Messages» '
                                         '(Saved Messages, private)\n'))


def saved_reply_artifact(draft):
    from telegram_search_mcp.draft_models import DraftPreview
    from telegram_search_mcp.reply_artifact_drafts import preview_facts
    from telegram_search_mcp.reply_drafts import ReplyTarget
    from telegram_search_mcp.reply_drafts import digest as reply_digest
    target = ReplyTarget(anchor={'chat_id': USER, 'message_id': 55}, text='[untrusted Telegram evidence] Note',
                         source_sha256='a' * 64, sanitized=False)
    value = DraftPreview.model_validate(draft)
    return {'draft': value.model_dump(mode='json'), 'reply_target': target.model_dump(mode='json'),
            'preview_sha256': reply_digest(preview_facts(value, target))}


def saved_formatted():
    import test_formatting_public_owner as formatting
    value = formatting.draft(formatting.ROWS)
    value.update(account_id=USER, recipient=USER, recipient_title=SAVED)
    value['preview_sha256'] = formatting.digest(formatting.nonreply_facts(value, formatting.ROWS))
    return value


def owner_topic_text():
    import test_formatting_public_owner as formatting
    topic = {key: item for key, item in formatting.topic().items() if key != 'source_sha256'}
    topic.update(account_id=USER, creator_id=USER)
    value = formatting.draft([], with_topic=True)
    value.update(account_id=USER, topic=dict(topic, source_sha256=formatting.digest(topic)))
    value['preview_sha256'] = formatting.digest(formatting.nonreply_facts(value, []))
    return value


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
