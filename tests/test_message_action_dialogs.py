"""0.57 criterion 6: the owner-dialog goldens of every own-message action on the one 0.54 renderer, with and without a
0.55 account registry.

Each action (a styled caption edit, a styled text edit, a pin, an unpin, a deletion) renders one complete document,
pinned here as logical lines for a realistic user id. Without a registry the account line carries the user id (the
0.54 line) and the dialog script is the 0.54 change script, or for a deletion the delete script with its Delete
button. With a registry the heading names the alias and the account line masks the user id, which then appears
nowhere in the dialog's argv; nothing else in the document changes. No real dialog runs: osascript is patched.
"""
from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_archive_edit as archive
import test_caption_edit as caption_suite
from approval_layout import logical, tail
from test_account_dialog import CHANGE_SCRIPT, SEND_SCRIPT
from test_archive_edit import CHAT, DID, EXPIRY, MID, chat, entity

from telegram_search_mcp import approval_prompt
from telegram_search_mcp.approval_prompt import decide_approved_send, render_approval
from telegram_search_mcp.caption_edit_drafts import CaptionEditDraftPreview, CaptionEditSnapshot, CaptionEditSpec
from telegram_search_mcp.delete_drafts import DeleteSnapshot, MessageDeleteDraftPreview, MessageDeleteSpec
from telegram_search_mcp.edit_drafts import EditChat, EditSnapshot, TextEditDraftPreview
from telegram_search_mcp.pin_drafts import PinChangeDraftPreview, PinChangeSpec, PinSnapshot

USER = 7401234561
OTHER = 8
ALIAS = 'work'
PLAIN = f'Account: {USER}'
MASKED = 'Account: work (Telegram account ••61)'
# The delete dialog script as 0.57 part 3 introduces it: the 0.54 template with the Delete button.
DELETE_SCRIPT = '8a51361224918b32608c476f1c58c084ff728f409f1dae8397b855df8adade7c'
YOU = {'@type': 'messageSenderUser', 'user_id': USER}
PROPERTIES = {**archive.PROPERTIES, 'can_be_pinned': True, 'can_be_deleted_only_for_self': False,
              'can_be_deleted_for_all_users': True}
EXPIRES = 'Expires: 2030-01-02T03:04:05+00:00'
EDIT_CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its edit permission, '
                'then make one {} attempt.')
PIN_CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its pin permission, '
               'then make one {} attempt and report the pin state it observes right after.')
DELETE_CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its permission to '
                  'delete it for everyone, then make one deletion attempt; a deletion is never retried.')


def edit_chat(kind='supergroup'):
    return EditChat(chat_id=CHAT, title='Team Chat', kind=kind)


def caption_preview():
    message = caption_suite.own_media('photo', 'Old caption', [entity('textEntityTypeBold', 0, 3)], sender_id=YOU)
    snapshot = CaptionEditSnapshot.create(chat(CHAT, 'Team Chat'), CHAT, message, MID, dict(PROPERTIES))
    spec = CaptionEditSpec(snapshot=snapshot, new_caption='New caption',
                           styles=((0, 3, 'textEntityTypeBold'), (4, 7, 'textEntityTypeItalic')))
    value = CaptionEditDraftPreview.create(draft_id=DID, account_id=USER, expires_at=EXPIRY, chat=edit_chat(),
                                           spec=spec)
    return value, dict(display_name='Caption edit', sha256=value.source_sha256, kind='caption_edit',
                       caption_edit_preview=value.model_dump(mode='json'))


def styled_text_preview():
    message = archive.own_message('Hello world', [entity('textEntityTypeItalic', 0, 5)], sender_id=YOU)
    snapshot = EditSnapshot.create(chat(CHAT, 'Team Chat'), CHAT, message, MID, dict(PROPERTIES))
    value = TextEditDraftPreview.create(draft_id=DID, account_id=USER, expires_at=EXPIRY, chat=edit_chat(),
                                        snapshot=snapshot, new_text='Hello there',
                                        styles=((0, 5, 'textEntityTypeBold'),))
    return value, dict(display_name='Text edit', sha256=value.source_sha256, kind='text_edit',
                       text_edit_preview=value.model_dump(mode='json'))


def pin_preview(action):
    if action == 'pin':  # another member's message
        message, kind = archive.own_message('Meeting at 10', is_pinned=False, is_outgoing=False,
                                            sender_id={'@type': 'messageSenderUser', 'user_id': OTHER}), 'supergroup'
    else:  # the owner's own channel post
        message, kind = archive.own_message('Announcement', is_pinned=True, sender_id=YOU), 'channel'
    snapshot = PinSnapshot.create(chat(CHAT, 'Team Chat', kind), CHAT, message, MID, dict(PROPERTIES))
    value = PinChangeDraftPreview.create(draft_id=DID, account_id=USER, expires_at=EXPIRY, chat=edit_chat(kind),
                                         spec=PinChangeSpec(snapshot=snapshot, action=action))
    return value, dict(display_name='Pin change', sha256=value.message.source_sha256, kind='pin_change',
                       pin_change_preview=value.model_dump(mode='json'))


def delete_preview():
    message = archive.own_message('See you at 10', sender_id=YOU)
    snapshot = DeleteSnapshot.create(chat(CHAT, 'Team Chat'), CHAT, message, MID, dict(PROPERTIES), USER)
    value = MessageDeleteDraftPreview.create(draft_id=DID, account_id=USER, expires_at=EXPIRY, chat=edit_chat(),
                                             spec=MessageDeleteSpec(snapshot=snapshot))
    return value, dict(display_name='Message deletion', sha256=value.message.source_sha256, kind='message_delete',
                       message_delete_preview=value.model_dump(mode='json'))


def shown(arguments, alias=None):
    """The one dialog's argv: (logical document, script, raw argv text)."""
    seen = []
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        decision = decide_approved_send(recipient_title='Team Chat', recipient=CHAT, size_bytes=1, caption='',
                                        account_alias=alias, **arguments)
    assert decision.outcome == 'declined'
    [argv] = seen
    return logical(argv[-1]), argv[argv.index('-e') + 1], '\n'.join(argv)


def document(heading, account, body, technical, closing):
    return '\n'.join([heading, account, EXPIRES, *body]) + '\n' + tail(*technical, closing=closing)


# (builder, heading, body after the expiry line, closing, verb) per action; the bodies are the goldens.
CASES = {
    'caption_edit': (caption_preview, f'Edit the caption of your photo message {MID} in «Team Chat» (chat -1001234, '
                     'supergroup)', [
        'Last edited: never', '',
        'Current caption [untrusted Telegram evidence] (exact):', '│ «Old caption»',
        'Current formatting, replaced by this edit (raw text UTF-16 code units):',
        'Bold: offset 0, length 3; covered «Old»',
        'New caption (exact):', '│ «New caption»',
        'New caption styles (final caption; UTF-16 code units):',
        'Bold: offset 0, length 3; covered «New»',
        'Italic: offset 4, length 7; covered «caption»',
        'Caption position: below the media (unchanged)', '',
        'The new caption carries only the styles listed here; Telegram may detect links in it again. Recipients see '
        'the message marked "edited".'], EDIT_CLOSING.format('caption edit'), 'change'),
    'styled_text_edit': (styled_text_preview, f'Edit your message {MID} in «Team Chat» (chat -1001234, supergroup)', [
        'Last edited: never', '',
        'Current text [untrusted Telegram evidence] (exact):', '│ «Hello world»',
        'Current formatting, replaced by this edit (raw text UTF-16 code units):',
        'Italic: offset 0, length 5; covered «Hello»',
        'New text (exact):', '│ «Hello there»',
        'New text styles (final text; UTF-16 code units):',
        'Bold: offset 0, length 5; covered «Hello»', '',
        'The new text carries only the styles listed here; Telegram may detect links in it and show a link preview. '
        'Link preview settings return to Telegram\'s default. Recipients see the message marked "edited".'],
        EDIT_CLOSING.format('edit'), 'change'),
    'pin': (lambda: pin_preview('pin'), f'Pin message {MID} in «Team Chat» (chat -1001234, supergroup)', [
        '',
        f'Message ID: {MID}', f'Sender [untrusted Telegram evidence]: user {OTHER}', 'Date: 2023-11-14T22:13:20+00:00',
        'Content: text', 'Text [untrusted Telegram evidence] (exact):', '│ «Meeting at 10»', '',
        'Telegram posts a visible service message about the pin that every participant of the chat sees.',
        'The message is pinned for everyone in the chat, not only for you, and silently: members get no notification.',
        'Messages pinned earlier stay pinned.'], PIN_CLOSING.format('pin'), 'change'),
    'unpin': (lambda: pin_preview('unpin'), f'Unpin message {MID} in «Team Chat» (chat -1001234, channel; visible to '
              'all subscribers)', [
        '',
        f'Message ID: {MID}', 'Sender [untrusted Telegram evidence]: you', 'Date: 2023-11-14T22:13:20+00:00',
        'Content: text', 'Text [untrusted Telegram evidence] (exact):', '│ «Announcement»', '',
        'The message is unpinned for everyone in the chat; no service message is posted.'],
        PIN_CLOSING.format('unpin'), 'change'),
    'delete': (delete_preview, f'Delete your message {MID} in «Team Chat» (chat -1001234, supergroup) for everyone', [
        '',
        f'Message ID: {MID}', 'Sender [untrusted Telegram evidence]: you', 'Date: 2023-11-14T22:13:20+00:00',
        'Content: text', 'Text [untrusted Telegram evidence] (exact):', '│ «See you at 10»', '',
        'The deletion is irreversible: nobody can restore the message, not even you.',
        'The message is removed for every participant of the chat, not only for you.',
        'If the message is pinned, deleting it also unpins it.'], DELETE_CLOSING, 'delete'),
}


def technical(value):
    source = value.message.source_sha256 if hasattr(value, 'message') else value.source_sha256
    return (f'Draft: {DID}', f'Source SHA-256: {source}', f'Preview SHA-256: {value.preview_sha256}')


class ActionDialogGoldenTests(unittest.TestCase):
    maxDiff = None

    def test_every_action_without_a_registry_is_the_054_document_with_the_user_id(self):
        for name, (builder, heading, body, closing, verb) in CASES.items():
            with self.subTest(action=name):
                value, arguments = builder()
                text, script, _ = shown(arguments)
                self.assertEqual(text, document(heading, PLAIN, body, technical(value), closing))
                expected = DELETE_SCRIPT if verb == 'delete' else CHANGE_SCRIPT
                self.assertEqual(hashlib.sha256(script.encode()).hexdigest(), expected)
                self.assertEqual(render_approval(recipient_title='Team Chat', recipient=CHAT, size_bytes=1,
                                                 caption='', **arguments)[1], verb)

    def test_every_action_with_a_registry_names_the_alias_and_masks_the_account(self):
        for name, (builder, heading, body, closing, verb) in CASES.items():
            with self.subTest(action=name):
                value, arguments = builder()
                text, script, argv = shown(arguments, ALIAS)
                self.assertEqual(text, document(heading, MASKED, body, technical(value), closing))
                self.assertEqual(text, shown(arguments)[0].replace(PLAIN, MASKED))  # only the account line differs
                self.assertEqual(script, approval_prompt._dialog_script(
                    approval_prompt._DELETE_SCRIPT if verb == 'delete' else approval_prompt._CHANGE_SCRIPT, ALIAS))
                self.assertIn(' for account \\u00abwork\\u00bb', script)
                self.assertNotIn(str(USER), argv)  # the user id reaches neither the document nor the script

    def test_the_buttons_name_the_action(self):
        for name, (builder, *_rest, verb) in CASES.items():
            for alias in (None, ALIAS):
                with self.subTest(action=name, alias=alias):
                    _, script, _ = shown(builder()[1], alias)
                    button, heading, effect = (("$('Delete')", 'approve deletion', 'nothing is deleted.')
                                               if verb == 'delete' else
                                               ("$('Apply')", 'approve change', 'nothing is changed.'))
                    self.assertIn(f'alert.addButtonWithTitle({button}).keyEquivalent = $(\'\');', script)
                    self.assertIn(heading, script)
                    self.assertIn(effect, script)
                    for other in {"$('Send')", "$('Apply')", "$('Delete')"} - {button}:
                        self.assertNotIn(other, script)

    def test_the_send_and_change_scripts_keep_their_054_bytes(self):
        self.assertEqual(hashlib.sha256(approval_prompt._dialog_script(approval_prompt._SCRIPT).encode()).hexdigest(),
                         SEND_SCRIPT)
        self.assertEqual(hashlib.sha256(
            approval_prompt._dialog_script(approval_prompt._CHANGE_SCRIPT).encode()).hexdigest(), CHANGE_SCRIPT)
        self.assertEqual(set(approval_prompt._SCRIPTS), {'send', 'change', 'delete'})

    def test_a_verb_outside_the_closed_set_shows_no_dialog(self):
        seen = []
        with patch('telegram_search_mcp.approval_prompt.subprocess.run',
                   side_effect=lambda argv, **options: seen.append(argv)):
            for verb in ('Delete', 'remove', '', True, None):
                with self.subTest(verb=verb):
                    self.assertEqual(approval_prompt._dialog('x', verb=verb, seconds=5).outcome, 'dialog_unavailable')
        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
