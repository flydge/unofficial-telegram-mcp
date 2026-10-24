"""0.57 work item 3: one owner-approved caption edit of an own media message (F17 rest, part 1).

Synthetic raw transport only (test_archive_edit's, answering editMessageCaption too): projections, drafts, broker,
proxy, server and TDLib reducers are production code. No live TDLib and no owner dialog: the prompt is a fake, and
the dialog goldens patch osascript.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_archive_edit as archive
from approval_layout import tail as dialog_tail
from test_archive_edit import (ACCOUNT, CHAT, CLIENT, DID, ERRORS, EXPIRY, FOREIGN, MARK, MID, PROPERTIES, UNKNOWN,
                               chat, digest, entity, escape, formatted)
from telegram_search_mcp import broker_client as broker_client_module
from telegram_search_mcp.approval_prompt import DETAILS, NOT_GIVEN, ApprovalDecision, confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.caption_edit_drafts import (ApplyPreparedCaptionEditRequest, ApplyPreparedCaptionEditResponse,
    CaptionEditDraftPreview, CaptionEditSnapshot, CaptionEditSpec, PrepareCaptionEditRequest,
    PrepareCaptionEditResponse, UNAVAILABLE as CAPTION_UNAVAILABLE, edited_caption_result)
from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, OPERATION_CAPABILITIES
from telegram_search_mcp.draft_kinds import DRAFT_KINDS
from telegram_search_mcp.edit_drafts import EditChat
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, TDLibClient,
    _ALLOWED_REQUEST_TYPES, _CALL_REFUSED_TYPES, _CHANGE_REQUEST_TYPES, _validate_bounded_request)

TOOLS = ('prepare_caption_edit', 'apply_prepared_caption_edit')
CAPTION = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_caption'))
STYLED = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_caption', 'edit_own_styled_text'))
STYLED_ONLY = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_styled_text'))
TEXT_EDIT = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
PREPARED = 'immutable caption edit draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for this caption edit; no draft was created'
WHITESPACE = 'new caption must not start or end with whitespace; Telegram trims it'
REWRITTEN = ('new caption changes under Telegram normalization (NFKC, line breaks, rewritten characters); send its '
             'stored form')
DISABLED = 'a styled caption needs capabilities the owner has not enabled: missing_capabilities'
BOLD = {'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeBold'}}
KINDS = ('photo', 'video', 'animation', 'document', 'audio', 'voice_note')
CONTENT_TYPES = {'photo': 'messagePhoto', 'video': 'messageVideo', 'animation': 'messageAnimation',
                 'document': 'messageDocument', 'audio': 'messageAudio', 'voice_note': 'messageVoiceNote'}
# The exact closed sets after this slice: parts 2 and 3 of 0.57 (pin, delete) extend them deliberately.
ALLOWED = {'getAuthorizationState', 'setTdlibParameters', 'checkDatabaseEncryptionKey', 'getMe', 'createPrivateChat',
           'searchPublicChat', 'searchChats', 'searchChatsOnServer', 'getChats', 'loadChats', 'getChat',
           'getChatListsToAddChat', 'getSupergroup', 'getForumTopics', 'getForumTopic', 'getForumTopicHistory',
           'searchMessages', 'searchChatMessages', 'getMessage', 'getMessageProperties', 'getChatHistory',
           'getMessageLink', 'getUser', 'downloadFile', 'getFile', 'cancelDownloadFile', 'sendMessage',
           'sendMessageAlbum', 'forwardMessages', 'addChatToList', 'editMessageText', 'editMessageCaption'}
CHANGES = {'addChatToList', 'editMessageText', 'editMessageCaption'}
CALL_REFUSED = CHANGES | {'forwardMessages', 'sendMessageAlbum'}
# Requests no tool may reach in this slice, whatever their shape.
UNREACHABLE = ('editMessageMedia', 'editMessageReplyMarkup', 'editMessageLiveLocation', 'editMessageChecklist',
               'editMessageSchedulingState', 'editInlineMessageCaption', 'deleteMessages', 'deleteChatHistory',
               'deleteChatMessagesBySender', 'deleteChatMessagesByDate', 'deleteChat', 'pinChatMessage',
               'unpinChatMessage', 'unpinAllChatMessages', 'setMessageReactions')


def remote_file(size=4096, unique='unique-1'):
    return {'@type': 'file', 'id': 11, 'size': size, 'expected_size': size,
            'local': {'@type': 'localFile', 'path': '', 'is_downloading_completed': False},
            'remote': {'@type': 'remoteFile', 'id': 'remote-id', 'unique_id': unique, 'is_uploading_active': False,
                       'is_uploading_completed': True, 'uploaded_size': size}}


def media(kind='photo', caption='Old caption', entities=(), above=False):
    text = formatted(caption, entities)
    visual = {'caption': text, 'show_caption_above_media': above, 'has_spoiler': False, 'is_secret': False}
    if kind == 'photo':
        return {'@type': 'messagePhoto', 'photo': {'@type': 'photo', 'has_stickers': False, 'minithumbnail': None,
                'sizes': [{'@type': 'photoSize', 'type': 'y', 'photo': remote_file(), 'width': 800, 'height': 600,
                           'progressive_sizes': []}]}, **visual}
    if kind == 'video':
        return {'@type': 'messageVideo', 'video': {'@type': 'video', 'duration': 5, 'width': 640, 'height': 360,
                'file_name': 'clip.mp4', 'mime_type': 'video/mp4', 'has_stickers': False, 'supports_streaming': True,
                'minithumbnail': None, 'thumbnail': None, 'video': remote_file()}, 'alternative_videos': [],
                'storyboards': [], 'cover': None, 'start_timestamp': 0, **visual}
    if kind == 'animation':
        return {'@type': 'messageAnimation', 'animation': {'@type': 'animation', 'duration': 2, 'width': 320,
                'height': 240, 'file_name': 'loop.mp4', 'mime_type': 'video/mp4', 'has_stickers': False,
                'minithumbnail': None, 'thumbnail': None, 'animation': remote_file()}, **visual}
    if kind == 'document':
        return {'@type': 'messageDocument', 'document': {'@type': 'document', 'file_name': 'report.pdf',
                'mime_type': 'application/pdf', 'minithumbnail': None, 'thumbnail': None,
                'document': remote_file()}, 'caption': text}
    if kind == 'audio':
        return {'@type': 'messageAudio', 'audio': {'@type': 'audio', 'duration': 30, 'title': 'Song',
                'performer': 'Band', 'file_name': 'song.mp3', 'mime_type': 'audio/mpeg',
                'album_cover_minithumbnail': None, 'album_cover_thumbnail': None, 'external_album_covers': [],
                'audio': remote_file()}, 'caption': text}
    return {'@type': 'messageVoiceNote', 'voice_note': {'@type': 'voiceNote', 'duration': 3, 'waveform': '',
            'mime_type': 'audio/ogg', 'speech_recognition_result': None, 'voice': remote_file()}, 'caption': text,
            'is_listened': False}


def own_media(kind='photo', caption='Old caption', entities=(), above=False, **changes):
    return archive.own_message(content=media(kind, caption, entities, above), **changes)


class Raw(archive.Raw):
    """test_archive_edit's transport with an own photo by default, answering editMessageCaption too."""

    def __init__(self):
        super().__init__()
        self.message = own_media()
        self.caption_mode = 'ok'

    def send(self, request):
        if request['@type'] != 'editMessageCaption':
            return super().send(request)
        self.sent.append(copy.deepcopy(request))
        mode = self.caption_mode
        if mode in ERRORS:
            if mode not in ('rejected', 'flood'):  # A 5xx/negative/codeless error may follow the edit.
                self.message['content']['caption'] = copy.deepcopy(request['caption'])
                self.message['edit_date'] = 1700000500
            error = {'@type': 'error', 'message': 'MESSAGE_NOT_MODIFIED PRIVATE', '@extra': request['@extra']}
            self.events.append(error if ERRORS[mode] is None else {**error, 'code': ERRORS[mode]})
            return
        if mode == 'lost':
            self.fail_receive = True
            return
        if mode == 'switch_account':
            self.account = 8
        value = copy.deepcopy(self.message)
        content, caption = value['content'], copy.deepcopy(request['caption'])
        if mode == 'auto_detected':  # Telegram detects the link in "see example.com" again.
            caption['entities'].append(entity('textEntityTypeUrl', caption['text'].index('example.com'), 11))
        if mode == 'wrong_caption':
            caption['text'] += '!'
        if mode == 'extra_style':
            caption['entities'].append(entity('textEntityTypeItalic', 0, 1))
        if mode == 'missing_style':
            caption['entities'] = []
        if mode == 'hidden_entity':
            caption['entities'].append(entity('textEntityTypeTextUrl', 0, 1, url='https://hidden.invalid'))
        content['caption'] = caption
        if 'show_caption_above_media' in content:  # TDLib drops the flag for kinds without a caption position.
            content['show_caption_above_media'] = request['show_caption_above_media'] != (mode == 'above_flipped')
        if mode == 'wrong_kind':
            content['@type'] = 'messageDocument' if content['@type'] != 'messageDocument' else 'messagePhoto'
        value['edit_date'] = {'zero_edit_date': 0, 'stale': self.message['edit_date'] - 1}.get(mode, 1700000500)
        if mode == 'wrong_id':
            value['id'] = MID + 1048576
        if mode == 'wrong_chat':
            value['chat_id'] = CHAT - 1
        if mode in ('ok', 'switch_account', 'auto_detected'):
            self.message = copy.deepcopy(value)
        self.events.append({'@type': 'updateMessageEdited', 'chat_id': CHAT, 'message_id': MID,
                            'edit_date': value['edit_date'], 'reply_markup': None})
        self.events.append({**value, '@extra': request['@extra']})


def snapshot(message=None, title='Team Chat', kind='supergroup', properties=None):
    return CaptionEditSnapshot.create(chat(CHAT, title, kind), CHAT, message or own_media(), MID,
                                      properties or dict(PROPERTIES))


def preview(new='New caption', styles=(), message=None, title='Team Chat', kind='supergroup'):
    spec = CaptionEditSpec(snapshot=snapshot(message, title, kind), new_caption=new, styles=styles)
    return CaptionEditDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
                                          chat=EditChat(chat_id=CHAT, title=title, kind=kind), spec=spec)


def shown(value, **kw):
    """The exact owner-dialog document (logical lines) and script; the DECLINED mock has no owner side effect."""
    from approval_layout import logical
    seen = []
    arguments = dict(recipient_title=value.chat.title, recipient=value.chat.chat_id, display_name='Caption edit',
                     size_bytes=1, sha256=value.source_sha256, caption='', kind='caption_edit',
                     caption_edit_preview=value.model_dump(mode='json'))
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        assert confirm_approved_send(**arguments) is False
    return [logical(argv[-1]) for argv in seen], [argv[argv.index('-e') + 1] for argv in seen]


class Fixture:
    policy = STYLED

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.raw = Raw()
        self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                             client_factory=lambda: self.client, policy=self.policy,
                             approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
                                      'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def requests(self, kind):
        return [request for request in self.raw.sent if request['@type'] == kind]

    def prepare_payload(self, new='New caption', entities=None):
        return {'chat': CHAT, 'message_id': MID, 'new_caption': new,
                **({'outgoing_entities': entities} if entities is not None else {})}

    def prepare(self, new='New caption', entities=None, client=CLIENT):
        result = self.dispatch('prepare_caption_edit', self.prepare_payload(new, entities), client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['caption_edit']

    def apply(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_caption_edit', {'draft_id': did, 'approved': approved}, client)

    def assert_no_attempt(self):
        self.assertEqual(self.requests('editMessageCaption'), [])
        self.assertEqual(self.requests('editMessageText'), [])


class DraftKindTests(unittest.TestCase):
    def test_the_caption_edit_kind_is_a_dedicated_unbacked_change_bound_to_edit_own_caption(self):
        row = DRAFT_KINDS['caption_edit']
        self.assertEqual((row.change, row.unbacked, row.dedicated, row.status, row.display_name),
                         (True, True, True, 'edit_own_caption', 'Caption edit'))
        self.assertIs(row.spec_type(), CaptionEditSpec)
        self.assertIn('prepare_caption_edit', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        self.assertNotIn('apply_prepared_caption_edit', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        for name in TOOLS:
            self.assertEqual(OPERATION_CAPABILITIES[name], 'edit_own_caption')
            self.assertIn(name, OPERATIONS)


class CapabilityBoundaryTests(unittest.TestCase):
    """Criterion 5: send + read + edit_own_caption at the broker and the proxy; a styled caption needs both edits."""

    def test_broker_refuses_before_any_provider_read_unless_every_capability(self):
        ops = (('prepare_caption_edit', {'chat': CHAT, 'message_id': MID, 'new_caption': 'x'}),
               ('apply_prepared_caption_edit', {'draft_id': DID, 'approved': True}))
        for enabled in ((), ('send', 'read'), ('edit_own_caption',), ('send', 'edit_own_caption'),
                        ('read', 'edit_own_caption'), ('send', 'read', 'edit_own_styled_text'),
                        ('send', 'read', 'edit_own_text', 'edit_own_styled_text', 'formatted_text_send')):
            with tempfile.TemporaryDirectory() as directory:
                opened = []
                broker = Broker(socket_path=Path(directory) / 'broker.sock',
                                artifact_store=ArtifactStore(cache_dir=Path(directory) / 'cache'),
                                client_factory=lambda: opened.append(True),
                                policy=RuntimePolicy(enabled_capabilities=enabled), verify_peer_uid=False)
                self.addCleanup(broker.shutdown)
                for op, payload in ops:
                    with self.subTest(enabled=enabled, op=op), self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation': op, 'payload': payload, 'client_id': CLIENT,
                                          'deadline': time.monotonic() + 10, 'broker_generation': broker._generation})
                self.assertEqual(opened, [])

    def test_proxy_refuses_before_connecting_unless_every_capability(self):
        for policy, enabled in ((RuntimePolicy(), False), (STYLED_ONLY, False),
                                (RuntimePolicy(enabled_capabilities=('send', 'edit_own_caption')), False),
                                (CAPTION, True)):
            connects = []

            def connector(path):
                connects.append(path)
                raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            with self.subTest(policy=policy.enabled_capabilities):
                prepared = proxy.prepare_caption_edit(PrepareCaptionEditRequest(chat=CHAT, message_id=MID,
                                                                                new_caption='x'))
                self.assertEqual(prepared.status, 'unavailable')
                applied = proxy.apply_prepared_caption_edit(ApplyPreparedCaptionEditRequest(draft_id=DID,
                                                                                            approved=True))
                if enabled:
                    self.assertTrue(connects)
                    self.assertEqual((applied.status, applied.next_action), ('outcome_unknown', 'check_send_status'))
                else:
                    self.assertEqual(connects, [])
                    self.assertEqual((applied.status, applied.detail),
                                     ('error', 'request rejected before dispatch: capability_disabled'))


class StyledConjunctionTests(Fixture, unittest.TestCase):
    policy = CAPTION

    def test_a_styled_caption_without_edit_own_styled_text_names_it_and_reads_nothing(self):
        result = self.dispatch('prepare_caption_edit', self.prepare_payload('New caption', [BOLD]))
        self.assertEqual(result, {'status': 'capability_disabled', 'caption_edit': None, 'refusal': None,
                                  'missing_capabilities': ['edit_own_styled_text'], 'detail': DISABLED,
                                  'next_action': 'enable_capability'})
        self.assertEqual((self.requests('getMessage'), self.requests('getChat')), ([], []))
        self.assertEqual(self.broker._drafts.known_ids(), set())
        # The plain caption needs no style capability.
        self.assertEqual(self.prepare('New caption')['outgoing_entities'], [])

    def test_a_styled_draft_stays_bound_to_both_edit_capabilities(self):
        self.broker._policy = STYLED
        did = self.prepare('New caption', [BOLD])['draft_id']
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_pending')
        self.broker._policy = CAPTION
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))  # content-free
        self.assertEqual(self.apply(did)['status'], 'expired')
        self.assertEqual(self.approvals, [])
        self.assert_no_attempt()
        self.broker._policy = STYLED
        self.assertEqual(self.apply(did)['status'], 'applied')
        self.broker._policy = CAPTION  # The receipt is not replayed without the style capability either.
        self.assertNotEqual(self.apply(did)['status'], 'applied')
        self.assertEqual(len(self.requests('editMessageCaption')), 1)


class PrepareTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: refusals and the closed preview."""

    def test_every_own_media_kind_prepares_a_bound_preview_without_any_change(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.setUp()
                self.raw.message = own_media(kind, 'Old caption', [entity('textEntityTypeBold', 0, 3)])
                value = self.prepare('Новая «подпись»\nnext' if kind == 'photo' else 'New caption')
                self.assertEqual(value['content_kind'], kind)
                self.assertEqual(value['chat'], {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'})
                self.assertEqual((value['message_id'], value['current_edit_date'], value['account_id']),
                                 (MID, 0, ACCOUNT))
                self.assertEqual(value['current_caption'], MARK + escape('Old caption'))
                self.assertEqual(value['current_entities'], [{'type': 'textEntityTypeBold', 'offset': 0, 'length': 3}])
                self.assertIs(value['show_caption_above_media'], False)
                self.assertEqual(CaptionEditDraftPreview.model_validate(value).model_dump(mode='json'), value)
                self.assertNotIn('unique-1', json.dumps(value))
                self.assert_no_attempt()
                self.assertEqual(self.approvals, [])

    def test_crlf_and_other_rewritten_input_is_refused_never_rewritten(self):
        for new, detail in (('a\r\nb', REWRITTEN), ('a\tb', REWRITTEN), ('a b', REWRITTEN), ('x̊', REWRITTEN),
                            ('Café', REWRITTEN), ('Ａ', REWRITTEN), (' New', WHITESPACE),
                            ('New ', WHITESPACE), ('New\n', WHITESPACE), (' New', WHITESPACE),
                            ('　New', WHITESPACE), ('  ', WHITESPACE), ('\tNew', WHITESPACE),
                            ('bad\u0007bell', REFUSED), ('‮new', REFUSED), ('x' * 1025, None)):
            with self.subTest(new=new[:12]):
                self.setUp()
                if detail is None:
                    with self.assertRaises(Exception):
                        self.dispatch('prepare_caption_edit', self.prepare_payload(new))
                    continue
                result = self.dispatch('prepare_caption_edit', self.prepare_payload(new))
                self.assertEqual(result, {'status': 'refused', 'caption_edit': None, 'refusal': 'invalid_input',
                                          'detail': detail})
                self.assertEqual(self.requests('getMessage'), [])  # Local input checks precede any read.
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_the_stored_form_of_a_caption_prepares_exactly(self):
        value = self.prepare('Новая «подпись»\nnext  line')
        self.assertEqual(value['new_caption'], MARK + escape('Новая «подпись»\nnext  line'))

    def test_ineligible_messages_and_captions_refuse_without_draft(self):
        forward_info = {'@type': 'messageForwardInfo', 'origin': {'@type': 'messageOriginUser', 'sender_user_id': 8},
                        'date': 1, 'source': None, 'public_service_announcement_type': ''}
        markup = {'@type': 'replyMarkupInlineKeyboard', 'rows': []}
        secret_photo = media('photo')
        secret_photo['is_secret'] = True
        cases = [
            ('not_outgoing', lambda raw: raw.message.update(is_outgoing=False), {}),
            ('unsupported_source_content', lambda raw: setattr(raw, 'message', archive.own_message('Text only')), {}),
            ('unsupported_source_content', lambda raw: raw.message.update(content={'@type': 'messageSticker'}), {}),
            ('unsupported_source_content', lambda raw: raw.message.update(
                content={'@type': 'messageVideoNote', 'video_note': {}, 'is_viewed': False, 'is_secret': False}), {}),
            ('source_is_forward', lambda raw: raw.message.update(forward_info=forward_info), {}),
            ('hidden_source_content', lambda raw: raw.message.update(reply_markup=markup), {}),
            ('not_editable', lambda raw: raw.properties.update(can_be_edited=False), {}),
            ('hidden_source_content', lambda raw: raw.message['content']['caption']['entities'].append(
                entity('textEntityTypeTextUrl', 0, 3, url='https://evil.invalid')), {}),
            ('hidden_source_content', lambda raw: raw.message['content']['caption']['entities'].append(
                entity('textEntityTypeCustomEmoji', 0, 1, custom_emoji_id='1')), {}),
            ('hidden_source_content', lambda raw: raw.message['content']['caption']['entities'].append(
                entity('textEntityTypeMentionName', 0, 3, user_id=9)), {}),
            ('unsupported_source_state', lambda raw: raw.message.update(
                sending_state={'@type': 'messageSendingStatePending', 'sending_id': 1}), {}),
            ('unsupported_source_state', lambda raw: raw.message.update(
                scheduling_state={'@type': 'messageSchedulingStateSendAtDate', 'send_date': 1}), {}),
            ('unsupported_source_state', lambda raw: raw.message.update(self_destruct_in=5.0), {}),
            ('unsupported_source_state', lambda raw: raw.message.update(content=secret_photo), {}),
            ('unchanged_caption', lambda raw: None, {'new_caption': 'Old caption'}),
            ('unchanged_caption', lambda raw: setattr(raw, 'message', own_media(
                'photo', 'Old caption', [entity('textEntityTypeBold', 0, 3)])),
             {'new_caption': 'Old caption', 'outgoing_entities': [BOLD]}),
            ('unchanged_caption', lambda raw: setattr(raw, 'message', own_media('document', '')), {'new_caption': ''}),
            ('invalid_input', lambda raw: None, {'new_caption': '', 'outgoing_entities': [BOLD]}),
            ('invalid_input', lambda raw: None, {'new_caption': 'New', 'outgoing_entities': [
                {'offset': 0, 'length': 9, 'type': {'@type': 'textEntityTypeBold'}}]}),
        ]
        for code, change, payload in cases:
            with self.subTest(code=code, payload=payload):
                self.setUp()
                change(self.raw)
                result = self.dispatch('prepare_caption_edit', {**self.prepare_payload(), **payload})
                self.assertEqual(result, {'status': 'refused', 'caption_edit': None, 'refusal': code,
                                          'detail': REFUSED})
                self.assertEqual(self.broker._drafts.known_ids(), set())
                if code == 'invalid_input':
                    self.assertEqual(self.requests('getMessage'), [])
        self.assert_no_attempt()

    def test_removing_or_restyling_a_caption_is_a_change(self):
        removed = self.prepare('')
        self.assertEqual((removed['new_caption'], removed['outgoing_entities']), (None, []))
        self.setUp()
        self.raw.message = own_media('photo', 'Old caption', [entity('textEntityTypeBold', 0, 3)])
        plain = self.prepare('Old caption')  # Dropping the bold changes the caption's formatting.
        self.assertEqual(plain['new_caption'], MARK + escape('Old caption'))
        self.setUp()
        styled = self.prepare('Old caption', [{'offset': 4, 'length': 7, 'type': {'@type': 'textEntityTypeItalic'}},
                                              BOLD])
        self.assertEqual(styled['outgoing_entities'], [BOLD, {'offset': 4, 'length': 7,
                                                              'type': {'@type': 'textEntityTypeItalic'}}])

    def test_every_visible_fieldless_current_entity_is_admitted(self):
        from telegram_search_mcp.forward_sources import VISIBLE_ENTITY_TYPES
        for kind in VISIBLE_ENTITY_TYPES:
            with self.subTest(kind=kind):
                self.setUp()
                self.raw.message = own_media('photo', 'Old caption', [entity(kind, 0, 3)])
                self.assertEqual(self.prepare()['current_entities'][0]['type'], kind)

    def test_missing_secret_and_malformed_messages_are_unavailable(self):
        for change in (lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'gone'}),
                       lambda raw: raw.message.pop('is_outgoing'),
                       lambda raw: raw.message.update(chat_id=CHAT + 1),
                       lambda raw: raw.properties.pop('can_be_edited'),
                       lambda raw: raw.message['content'].pop('caption'),
                       lambda raw: raw.chats.update({CHAT: chat(CHAT, 'Secret', 'secret')}),
                       lambda raw: raw.chats.pop(CHAT)):
            with self.subTest(change=change):
                self.setUp()
                change(self.raw)
                result = self.dispatch('prepare_caption_edit', self.prepare_payload())
                self.assertIn(result['status'], ('unavailable', 'refused'))
                if result['status'] == 'refused':  # a content shape the projection refuses
                    self.assertEqual(result['refusal'], 'unsupported_source_content')
                else:
                    self.assertEqual(result, {'status': 'unavailable', 'caption_edit': None, 'refusal': None,
                                              'detail': CAPTION_UNAVAILABLE})
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_generic_draft_tools_and_other_send_or_apply_tools_never_touch_caption_drafts(self):
        did = self.prepare()['draft_id']
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*STYLED.enabled_capabilities, 'artifacts',
                                                                  'reply_text_send', 'reply_artifact_send',
                                                                  'edit_own_text', 'archive_chats'))
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {}), ('update_reply_draft', {'text': 'x'}),
                          ('refresh_reply_draft', {}), ('get_reply_artifact_draft', {})):
            with self.subTest(op=op):
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        for op, detail in (('send_prepared_text', 'draft kind does not match send tool'),
                           ('send_prepared_artifact', 'draft kind does not match send tool'),
                           ('apply_prepared_archive', 'draft kind does not match apply tool'),
                           ('apply_prepared_text_edit', 'draft kind does not match apply tool')):
            result = self.dispatch(op, {'draft_id': did, 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', detail), op)
        self.assert_no_attempt()
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_the_caption_apply_tool_refuses_every_other_kind(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*STYLED.enabled_capabilities, 'edit_own_text'))
        self.raw.message = archive.own_message()
        edit = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'Hello there'})
        text = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': 'plain'})
        for did in (edit['text_edit']['draft_id'], text['draft_id']):
            result = self.apply(did)
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match apply tool'))
        self.assertEqual(self.approvals, [])
        self.assert_no_attempt()

    def test_reprepare_supersedes_only_the_same_owner_and_message(self):
        first = self.prepare('One')['draft_id']
        foreign = self.prepare('Foreign', client=FOREIGN)['draft_id']
        second = self.prepare('Two')['draft_id']
        self.assertEqual(self.apply(first)['status'], 'expired')
        self.assertEqual(self.broker._drafts._entries[foreign].state, 'pending')
        self.assertEqual(self.apply(second)['status'], 'applied')
        [request] = self.requests('editMessageCaption')
        self.assertEqual(request['caption']['text'], 'Two')

    def test_proxy_validates_preview_digest_and_request_scope(self):
        payload = self.prepare_payload('New caption', [BOLD])
        broker_result = self.dispatch('prepare_caption_edit', payload)
        request = PrepareCaptionEditRequest(**payload)

        def proxy(result, request=request):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=STYLED)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close)
            return client.prepare_caption_edit(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for key, value in (('new_caption', MARK + escape('Other')), ('new_caption', None),
                           ('current_caption', MARK + escape('Other')), ('message_id', MID + 1),
                           ('current_edit_date', 5), ('source_sha256', 'a' * 64), ('preview_sha256', 'f' * 64),
                           ('account_id', ACCOUNT + 1), ('outgoing_entities', []), ('content_kind', 'video'),
                           ('show_caption_above_media', True),
                           ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key, value=value):
                result = copy.deepcopy(broker_result)
                result['caption_edit'][key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        for other in ({'new_caption': 'Other', 'outgoing_entities': [BOLD]}, {'message_id': MID + 1},
                      {'outgoing_entities': []}, {'chat': CHAT - 1},
                      {'outgoing_entities': [{'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeItalic'}}]}):
            with self.subTest(other=other):
                self.assertEqual(proxy(broker_result, PrepareCaptionEditRequest(**{**payload, **other})).status,
                                 'unavailable')


class ApplyTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: revalidation, one call with the exact shape, result validation, no retry."""

    def test_one_exact_edit_message_caption_then_receipt_replay(self):
        self.raw.message = own_media('photo', 'Old caption', edit_date=1700000100)
        value = self.prepare('New caption')
        result = self.apply(value['draft_id'])
        self.assertEqual(result, {'status': 'applied', 'draft_id': value['draft_id'], 'chat_id': CHAT,
                                  'message_id': MID, 'edit_date': 1700000500,
                                  'detail': 'TDLib returned the edited message with the new caption'})
        [request] = self.requests('editMessageCaption')
        self.assertEqual(request, {'@type': 'editMessageCaption', 'chat_id': CHAT, 'message_id': MID,
                                   'reply_markup': None, 'caption': {'@type': 'formattedText', 'text': 'New caption',
                                                                     'entities': []},
                                   'show_caption_above_media': False, '@extra': request['@extra']})
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['caption_edit_preview'], dialog['sha256'], dialog['display_name']),
                         ('caption_edit', value, value['source_sha256'], 'Caption edit'))
        replay = self.apply(value['draft_id'])
        self.assertEqual((replay['status'], replay['edit_date']), ('applied', 1700000500))
        self.assertIn('no new change', replay['detail'])
        self.assertEqual((len(self.requests('editMessageCaption')), len(self.approvals)), (1, 1))

    def test_removal_sends_exactly_the_empty_formatted_text(self):
        did = self.prepare('')['draft_id']
        self.assertEqual(self.apply(did)['status'], 'applied')
        [request] = self.requests('editMessageCaption')
        self.assertEqual(request['caption'], {'@type': 'formattedText', 'text': '', 'entities': []})

    def test_styles_travel_natively_and_only_auto_detected_extras_are_tolerated(self):
        did = self.prepare('see example.com now', [BOLD])['draft_id']
        self.raw.caption_mode = 'auto_detected'
        self.assertEqual(self.apply(did)['status'], 'applied')
        [request] = self.requests('editMessageCaption')
        self.assertEqual(request['caption']['entities'], [{'@type': 'textEntity', 'offset': 0, 'length': 3,
                                                           'type': {'@type': 'textEntityTypeBold'}}])

    def test_the_reviewed_caption_position_is_sent_and_verified(self):
        for kind, above in (('photo', True), ('video', True), ('animation', False), ('document', False),
                            ('audio', False), ('voice_note', False)):
            with self.subTest(kind=kind):
                self.setUp()
                self.raw.message = own_media(kind, 'Old caption', above=above)
                value = self.prepare()
                self.assertIs(value['show_caption_above_media'], above)
                self.assertEqual(self.apply(value['draft_id'])['status'], 'applied')
                [request] = self.requests('editMessageCaption')
                self.assertIs(request['show_caption_above_media'], above)
        for kind in ('photo', 'video', 'animation'):
            with self.subTest(flipped=kind):
                self.setUp()
                self.raw.message = own_media(kind)
                self.raw.caption_mode = 'above_flipped'
                self.assertEqual(self.apply(self.prepare()['draft_id'])['status'], 'outcome_unknown')

    def test_result_validation_lost_and_rejected_outcomes_never_retry(self):
        # Only a 4xx error proves no change; 5xx, negative or codeless errors may follow an edit.
        for mode, expected, styled in (('wrong_caption', 'outcome_unknown', False), ('wrong_kind', 'outcome_unknown', False),
                                       ('extra_style', 'outcome_unknown', False), ('missing_style', 'outcome_unknown', True),
                                       ('hidden_entity', 'outcome_unknown', False),
                                       ('zero_edit_date', 'outcome_unknown', False), ('stale', 'outcome_unknown', False),
                                       ('wrong_id', 'outcome_unknown', False), ('wrong_chat', 'outcome_unknown', False),
                                       ('lost', 'outcome_unknown', False), ('server_error', 'outcome_unknown', False),
                                       ('negative', 'outcome_unknown', False), ('codeless', 'outcome_unknown', False),
                                       ('rejected', 'failed', False), ('flood', 'failed', False)):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.caption_mode = mode
                self.raw.message = own_media('photo', 'Old caption', edit_date=1700000100)
                did = self.prepare('New caption', [BOLD] if styled else None)['draft_id']
                result = self.apply(did)
                self.assertEqual(result['status'], expected)
                self.assertIsNone(result['edit_date'])
                self.assertEqual(self.apply(did)['status'], expected)
                self.assertEqual(len(self.requests('editMessageCaption')), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                if expected == 'failed':
                    self.assertEqual(result['detail'], f'TDLib rejected the change (code {ERRORS[mode]})')
                    self.assertNotIn('next_action', result)
                else:
                    self.assertIn('do not retry', result['detail'])
                    self.assertEqual(result['next_action'], 'check_send_status')

    def test_not_approved_and_declined_dialog_make_no_attempt(self):
        did = self.prepare()['draft_id']
        result = self.apply(did, approved=False)
        self.assertEqual((result['status'], result['detail']), ('declined', NOT_GIVEN))
        self.assertEqual(self.approvals, [])
        for answer, status in ((False, 'declined'), (ApprovalDecision('timeout'), 'timeout'),
                               (ApprovalDecision('dialog_unavailable'), 'dialog_unavailable'),
                               (object(), 'dialog_unavailable')):
            with self.subTest(answer=answer):
                self.broker._approval_prompt = lambda answer=answer, **kw: answer
                result = self.apply(did)
                self.assertEqual((result['status'], result['detail']), (status, DETAILS[status]))
                self.assertNotIn('next_action', result)
        self.assert_no_attempt()
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_revalidation_before_dialog_refuses_without_dialog_or_attempt(self):
        for detail, change in (
                ('message changed', lambda raw: raw.message.update(edit_date=1700000100)),
                ('message changed', lambda raw: raw.message['content']['caption'].update(text='Edited elsewhere')),
                ('message changed', lambda raw: raw.message['content'].update(show_caption_above_media=True)),
                ('message changed', lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'x'})),
                ('message changed', lambda raw: raw.message.update(reply_markup={'@type': 'replyMarkupInlineKeyboard',
                                                                                 'rows': []})),
                ('message changed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                ('message changed', lambda raw: raw.chats[CHAT]['type'].update(supergroup_id=99)),
                ('message can no longer be edited', lambda raw: raw.properties.update(can_be_edited=False))):
            with self.subTest(detail=detail, change=change):
                self.setUp()
                did = self.prepare()['draft_id']
                change(self.raw)
                result = self.apply(did)
                self.assertEqual((result['status'], result['chat_id'], result['message_id']),
                                 ('message_changed', CHAT, MID))
                self.assertTrue(result['detail'].startswith(detail), result['detail'])
                self.assertEqual(self.approvals, [])
                self.assert_no_attempt()

    def test_changes_during_the_dialog_are_local_failures_or_expire_without_attempt(self):
        for expected, change in (('local_failed', lambda raw: raw.message.update(edit_date=1700000100)),
                                 ('local_failed', lambda raw: raw.properties.update(can_be_edited=False)),
                                 ('local_failed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                                 ('expired', lambda raw: setattr(raw, 'account', 8))):
            with self.subTest(expected=expected):
                self.setUp()
                did = self.prepare()['draft_id']

                def approve(**kw):
                    change(self.raw)
                    return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.apply(did)['status'], expected)
                self.assert_no_attempt()
        for disabled in (TEXT_EDIT, CAPTION):
            with self.subTest(disabled=disabled.enabled_capabilities):
                self.setUp()
                did = self.prepare('New caption', [BOLD])['draft_id']

                def disable(**kw):
                    self.broker._policy = disabled
                    return True
                self.broker._approval_prompt = disable
                self.assertEqual(self.broker._apply_prepared_caption_edit(
                    ApplyPreparedCaptionEditRequest(draft_id=did, approved=True), client_id=CLIENT).status, 'expired')
                self.assert_no_attempt()

    def test_guard_failures_inside_the_provider_lock_are_local_failed_without_transport(self):
        # After the claim, the revalidation read inside the provider lock: edit_own_caption or (for a styled
        # caption) edit_own_styled_text turned off, or the claim made stale by expiry.
        for name, styled, change in (('capability', False, lambda: setattr(self.broker, '_policy', TEXT_EDIT)),
                                     ('styled_capability', True, lambda: setattr(self.broker, '_policy', CAPTION)),
                                     ('stale_claim', False, lambda: setattr(self.broker._drafts, '_clock',
                                                                            lambda: time.time() + 10**6))):
            with self.subTest(name=name):
                self.setUp()
                did = self.prepare('New caption', [BOLD] if styled else None)['draft_id']
                real = self.raw.send

                def send(request, real=real, change=change):
                    real(request)
                    if request['@type'] == 'getMessageProperties' and self.approvals:
                        change()
                self.raw.send = send
                result = self.broker._apply_prepared_caption_edit(
                    ApplyPreparedCaptionEditRequest(draft_id=did, approved=True), client_id=CLIENT)
                self.assertEqual((result.status, result.edit_date), ('local_failed', None))
                self.assertEqual(len(self.approvals), 1)
                self.assert_no_attempt()
                self.broker._policy = STYLED
                self.assertEqual(self.apply(did)['status'], 'local_failed')
                self.assert_no_attempt()

    def test_concurrent_applies_make_one_attempt(self):
        did = self.prepare()['draft_id']
        gate, results = threading.Event(), []

        def slow(**kw):
            gate.wait(5)
            self.approvals.append(kw)
            return True
        self.broker._approval_prompt = slow
        threads = [threading.Thread(target=lambda: results.append(self.apply(did))) for _ in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        gate.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(self.requests('editMessageCaption')), 1)
        self.assertIn('applied', [result['status'] for result in results])
        self.assertTrue(all(result['status'] in ('applied', 'outcome_unknown', 'dialog_unavailable')
                            for result in results), results)

    def test_account_switch_after_the_call_is_outcome_unknown_never_expired(self):
        self.raw.caption_mode = 'switch_account'
        did = self.prepare()['draft_id']
        self.assertEqual(self.apply(did), {'status': 'outcome_unknown', 'draft_id': did, 'chat_id': None,
                                           'message_id': None, 'edit_date': None, 'detail': UNKNOWN,
                                           'next_action': 'check_send_status'})
        self.assertEqual(self.broker._drafts._entries[did].receipt.status, 'applied')
        self.assertEqual(self.apply(did)['status'], 'outcome_unknown')
        self.assertEqual(self.apply(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(len(self.requests('editMessageCaption')), 1)

    def test_restart_foreign_owner_and_disabled_policy_are_content_free(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.apply(did, client=FOREIGN)['status'], 'expired')
        self.broker._policy = TEXT_EDIT
        with self.assertRaises(CompatibilityError):
            self.apply(did)
        self.broker._policy = STYLED
        self.broker._draft_recipient_titles.clear()
        self.assertEqual(self.apply(did)['status'], 'expired')
        self.assertEqual(self.approvals, [])
        self.assert_no_attempt()

    def test_status_is_bound_to_the_caption_capability_and_never_reports_sent(self):
        did = self.prepare()['draft_id']
        pending = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((pending['status'], pending['evidence']), ('pending', 'local_pending'))
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'none')
        self.broker._policy = STYLED
        self.assertEqual(self.apply(did)['status'], 'applied')
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))


class VerificationTests(unittest.TestCase):
    """Criterion 4: the result rule, apart from the transport."""

    def result(self, caption='New caption', entities=(), kind='messagePhoto', above=False, **changes):
        content = media({value: key for key, value in CONTENT_TYPES.items()}[kind], caption, entities, above)
        return {**own_media(edit_date=1700000500), 'content': content, **changes}

    def check(self, value, styles=(), content_type='messagePhoto', above=False, new='New caption'):
        return edited_caption_result(value, chat_id=CHAT, message_id=MID, content_type=content_type, new_caption=new,
                                     styles=styles, show_caption_above_media=above, previous_edit_date=0)

    def test_exact_caption_styles_and_auto_detected_extras(self):
        bold = ((0, 3, 'textEntityTypeBold'),)
        self.assertEqual(self.check(self.result('New caption', [entity('textEntityTypeBold', 0, 3)]), bold), 1700000500)
        from telegram_search_mcp.edit_drafts import AUTO_DETECTED
        for kind in sorted(AUTO_DETECTED):
            with self.subTest(kind=kind):
                self.assertEqual(self.check(self.result('New caption', [entity(kind, 4, 7)])), 1700000500)
        for entities, styles in (([], bold), ([entity('textEntityTypeBold', 0, 3)], ()),
                                 ([entity('textEntityTypeItalic', 0, 3)], bold),
                                 ([entity('textEntityTypeCode', 0, 3)], ()),
                                 ([entity('textEntityTypeTextUrl', 0, 3, url='https://x.invalid')], ()),
                                 ([entity('textEntityTypeUrl', 4, 7, extra=1)], ()),
                                 ([entity('textEntityTypeUrl', 4, 99)], ()), ([{'offset': 0}], ())):
            with self.subTest(entities=entities, styles=styles):
                self.assertIsNone(self.check(self.result('New caption', entities), styles))
        self.assertEqual(self.check(self.result(''), new=''), 1700000500)
        self.assertIsNone(self.check(self.result('', [entity('textEntityTypeUrl', 0, 1)]), new=''))

    def test_same_message_same_kind_and_position(self):
        self.assertIsNone(self.check(self.result(id=MID + 1)))
        self.assertIsNone(self.check(self.result(chat_id=CHAT - 1)))
        self.assertIsNone(self.check(self.result(kind='messageVideo')))
        self.assertIsNone(self.check(self.result(above=True)))
        self.assertEqual(self.check(self.result(above=True), above=True), 1700000500)
        self.assertEqual(self.check(self.result(kind='messageDocument'), content_type='messageDocument', above=False),
                         1700000500)
        self.assertIsNone(self.check(self.result(edit_date=0)))
        self.assertIsNone(self.check({'@type': 'error', 'code': 400}))
        self.assertIsNone(self.check(self.result('New caption!')))


class DialogTests(unittest.TestCase):
    """Criterion 6: the caption dialog goldens: old and new caption, or Remove caption, with every side effect."""
    maxDiff = None
    CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its edit permission, '
               'then make one caption edit attempt.')
    NOTE = ('The new caption carries only the styles listed here; Telegram may detect links in it again. Recipients '
            'see the message marked "edited".')

    def test_plain_photo_caption_golden(self):
        value = preview('New caption', message=own_media('photo', 'Old caption', [entity('textEntityTypeBold', 0, 3)]))
        texts, scripts = shown(value)
        self.assertEqual(texts, [
            f'Edit the caption of your photo message {MID} in «Team Chat» (chat -1001234, supergroup)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: never\n\n'
            'Current caption [untrusted Telegram evidence] (exact):\n│ «Old caption»\n'
            'Current formatting, replaced by this edit (raw text UTF-16 code units):\n'
            'Bold: offset 0, length 3; covered «Old»\n'
            'New caption (exact):\n│ «New caption»\n'
            'Caption position: below the media (unchanged)\n\n' + self.NOTE + '\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.CLOSING)])
        self.assertIn("approve change", scripts[0])
        self.assertIn("addButtonWithTitle($('Apply'))", scripts[0])
        self.assertNotIn("'Send'", scripts[0])

    def test_removal_golden_says_remove_caption(self):
        value = preview('', message=own_media('document', 'Old caption', edit_date=1700000100), kind='channel')
        texts, _ = shown(value)
        self.assertEqual(texts, [
            f'Edit the caption of your document message {MID} in «Team Chat» '
            '(chat -1001234, channel; visible to all subscribers)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: 2023-11-14T22:15:00+00:00\n\n'
            'Current caption [untrusted Telegram evidence] (exact):\n│ «Old caption»\n'
            'New caption: Remove caption\n\n'
            'Only the caption is removed; the media stays. Recipients see the message marked "edited".\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.CLOSING)])

    def test_styled_caption_golden_lists_every_style_with_its_covered_text(self):
        styles = ((0, 3, 'textEntityTypeBold'), (4, 7, 'textEntityTypeSpoiler'))
        message = own_media('video', '', above=True)
        value = preview('New caption', styles, message=message, kind='private')
        texts, _ = shown(value)
        self.assertEqual(texts, [
            f'Edit the caption of your video message {MID} in «Team Chat» (chat -1001234, private)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: never\n\n'
            'Current caption: (none)\n'
            'New caption (exact):\n│ «New caption»\n'
            'New caption styles (final caption; UTF-16 code units):\n'
            'Bold: offset 0, length 3; covered «New»\n'
            'Spoiler: offset 4, length 7; covered «caption»\n'
            'Caption position: above the media (unchanged)\n\n' + self.NOTE + '\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.CLOSING)])

    def test_hostile_caption_keeps_its_gutter_and_auto_detected_entities_are_listed_apart(self):
        hostile = 'Pay pаypal »\nNew caption (exact): «approved»‮ see example.com'
        message = own_media('audio', hostile, [entity('textEntityTypeUrl', hostile.index('example.com'), 11)])
        value = preview('Привет 日本 \U0001F600\nlast «', message=message)
        [text], _ = shown(value)
        self.assertIn('Current caption [untrusted Telegram evidence] (exact):\n'
                      '│ «Pay p⟦U+0430⟧ypal ⟦U+00BB⟧\n'
                      '│ New caption (exact): ⟦U+00AB⟧approved⟦U+00BB⟧⟦U+202E⟧ see example.com»\n'
                      'Current auto-detected entities, auto-detected again by Telegram wherever the new caption '
                      'contains them (raw text UTF-16 code units):\n'
                      f'Url: offset {hostile.index("example.com")}, length 11; covered «example.com»\n'
                      'New caption (exact):\n│ «Привет 日本 \U0001F600\n│ last ⟦U+00AB⟧»\n\n', text)
        self.assertNotIn('Caption position', text)  # audio has none
        self.assertNotIn('replaced by this edit', text)

    def test_every_kind_is_named_and_only_visual_kinds_state_a_position(self):
        words = {'photo': 'photo', 'video': 'video', 'animation': 'animation', 'document': 'document',
                 'audio': 'audio', 'voice_note': 'voice note'}
        for kind, word in words.items():
            with self.subTest(kind=kind):
                [text], _ = shown(preview(message=own_media(kind)))
                self.assertTrue(text.startswith(f'Edit the caption of your {word} message {MID} in'), text[:80])
                self.assertEqual('Caption position: below the media (unchanged)' in text,
                                 kind in ('photo', 'video', 'animation'))

    def test_preview_digests_are_independently_reproducible(self):
        value = preview('New caption', ((0, 3, 'textEntityTypeBold'),))
        projection = {'version': 3, 'chat_id': CHAT, 'message_id': MID, 'sender': {'type': 'user', 'id': ACCOUNT},
                      'author_signature': '', 'via_bot_user_id': 0, 'is_forward': False, 'date': 1700000000,
                      'edit_date': 0, 'content': {'kind': 'photo', 'caption': {'text': 'Old caption', 'entities': []},
                                                  'has_spoiler': False, 'show_caption_above_media': False,
                                                  'variants': [{'type': 'y', 'width': 800, 'height': 600,
                                                                'size': 4096, 'unique_id_sha256':
                                                                hashlib.sha256(b'unique-1').hexdigest()}]}}
        self.assertEqual(value.source_sha256, digest(projection))
        self.assertEqual(value.preview_sha256, digest({
            'domain': 'telegram-search-mcp.caption_edit', 'version': 1, 'draft_id': DID, 'account_id': ACCOUNT,
            'expires_at': '2030-01-02T03:04:05+00:00', 'approval_required': True,
            'chat': {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'}, 'message_id': MID,
            'content_kind': 'photo', 'current_caption': MARK + escape('Old caption'), 'current_entities': [],
            'show_caption_above_media': False, 'new_caption': MARK + escape('New caption'),
            'outgoing_entities': [BOLD], 'current_edit_date': 0, 'source_sha256': value.source_sha256,
            'request': {'@type': 'editMessageCaption', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                        'caption': {'@type': 'formattedText', 'text': 'New caption',
                                    'entities': [{'@type': 'textEntity', **BOLD}]},
                        'show_caption_above_media': False}}))

    def test_competing_mismatched_or_tampered_previews_refuse(self):
        value = preview()
        self.assertEqual(shown(value, text_edit_preview=archive.edit_preview().model_dump(mode='json'))[0], [])
        for kind in ('document', 'text_edit', 'archive', 'text'):
            self.assertEqual(shown(value, kind=kind)[0], [], kind)
        for key, changed in (('recipient', CHAT + 1), ('recipient_title', 'Other'), ('sha256', 'b' * 64),
                             ('text', 'smuggled'), ('caption', 'x'), ('duration_seconds', 5),
                             ('source_sha256', 'a' * 64)):
            with self.subTest(key=key):
                self.assertEqual(shown(value, **{key: changed})[0], [])
        for key, changed in (('new_caption', MARK + escape('Old caption')), ('new_caption', MARK + escape('a\tb')),
                             ('new_caption', MARK + escape(' x')), ('current_edit_date', 9),
                             ('show_caption_above_media', True), ('content_kind', 'document'),
                             ('outgoing_entities', [BOLD]),
                             ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key):
                data = value.model_dump(mode='json')
                data[key] = changed
                self.assertEqual(shown(value, caption_edit_preview=data)[0], [], key)


class TransportShapeTests(unittest.TestCase):
    """Criterion 3: the closed editMessageCaption shape and the exact allowlist, change and refusal sets."""

    @staticmethod
    def request(**changes):
        caption = changes.pop('caption', {'@type': 'formattedText', 'text': 'New caption', 'entities': []})
        return {'@type': 'editMessageCaption', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                'caption': caption, 'show_caption_above_media': False, **changes}

    def test_the_exact_closed_sets(self):
        self.assertEqual(set(_ALLOWED_REQUEST_TYPES), ALLOWED)
        self.assertEqual(set(_CHANGE_REQUEST_TYPES), CHANGES)
        self.assertEqual(set(_CALL_REFUSED_TYPES), CALL_REFUSED)
        self.assertFalse(set(UNREACHABLE) & ALLOWED)

    def test_edit_message_caption_has_one_closed_shape(self):
        bold = {'@type': 'textEntity', 'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeBold'}}
        for good in (self.request(), self.request(show_caption_above_media=True),
                     self.request(caption={'@type': 'formattedText', 'text': '', 'entities': []}),
                     self.request(caption={'@type': 'formattedText', 'text': 'New caption', 'entities': [bold]}),
                     self.request(caption={'@type': 'formattedText', 'text': 'x' * 1024, 'entities': []})):
            _validate_bounded_request(good)

        def caption(text, entities=()):
            return {'@type': 'formattedText', 'text': text, 'entities': list(entities)}
        url = {'@type': 'textEntity', 'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeUrl'}}
        text_url = {'@type': 'textEntity', 'offset': 0, 'length': 3,
                    'type': {'@type': 'textEntityTypeTextUrl', 'url': 'https://x.invalid'}}
        bad = [self.request(reply_markup={'@type': 'replyMarkupRemoveKeyboard', 'is_personal': False}),
               self.request(show_caption_above_media=None), self.request(show_caption_above_media=0),
               self.request(caption=None), self.request(caption=caption('', [bold])),
               self.request(caption=caption('x' * 1025)), self.request(caption=caption(' New')),
               self.request(caption=caption('New\n')), self.request(caption=caption('a\tb')),
               self.request(caption=caption('a\r\nb')), self.request(caption=caption('a b')),
               self.request(caption=caption('x̊')), self.request(caption=caption('Café')),
               self.request(caption=caption('bad\u0007')), self.request(caption=caption('New', [url])),
               self.request(caption=caption('New', [text_url])),
               self.request(caption=caption('New', [{**bold, 'length': 9}])),
               self.request(caption=caption('New', [bold, bold])),
               self.request(caption={**caption('New'), 'x': 1}), self.request(caption={'@type': 'formattedText',
                                                                                       'text': 'New'}),
               self.request(message_id=0), self.request(message_id=True), self.request(chat_id=0),
               self.request(extra=1)]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(request)
        missing = self.request()
        del missing['show_caption_above_media']
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request(missing)

    def test_the_generic_call_and_the_change_transport_refuse_every_unreachable_request(self):
        raw = Raw()
        client = TDLibClient(raw=raw)
        client._ready = True
        for kind in (*UNREACHABLE, 'editMessageCaption'):
            payload = self.request() if kind == 'editMessageCaption' else {'@type': kind, 'chat_id': CHAT,
                                                                            'message_ids': [MID], 'revoke': True}
            with self.subTest(kind=kind), self.assertRaises(ForbiddenTDLibRequest):
                client._call(payload)
            if kind != 'editMessageCaption':
                with self.subTest(kind=kind), self.assertRaises(MessageSendNotAttempted):
                    client._change_transport(payload, pre_transport_guard=lambda: True)
        with self.assertRaises(MessageSendNotAttempted):
            client._change_transport(self.request(), pre_transport_guard=lambda: False)
        self.assertEqual(raw.sent, [])


class RegistryTests(unittest.TestCase):
    def test_the_spec_refuses_unchanged_unnormalized_and_ineligible_captions(self):
        for new, styles, message, properties in (
                ('Old caption', (), None, None), ('New ', (), None, None), ('a\tb', (), None, None),
                ('', ((0, 1, 'textEntityTypeBold'),), None, None), ('New', ((0, 9, 'textEntityTypeBold'),), None, None),
                ('New', ((0, 3, 'textEntityTypeCode'),), None, None),
                ('New', ((0, 3, 'textEntityTypeBold'), (0, 3, 'textEntityTypeBold')), None, None),
                ('New', (), None, {**PROPERTIES, 'can_be_edited': False}),
                ('', (), own_media('photo', ''), None)):
            with self.subTest(new=new, styles=styles), self.assertRaises(ValueError):
                CaptionEditSpec(snapshot=snapshot(message, properties=properties), new_caption=new, styles=styles)

    def test_a_caption_draft_is_dedicated_unbacked_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(client_id=CLIENT, account_id=ACCOUNT)
            spec = CaptionEditSpec(snapshot=snapshot(), new_caption='New caption')
            draft = registry.prepare_change(owner=owner, chat_id=CHAT, chat_title='Team Chat', change=spec)
            self.assertEqual((draft.kind, draft.display_name, draft.caption_edit), ('caption_edit', 'Caption edit', spec))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            with self.assertRaises(Exception):
                registry.cancel(draft.draft_id, owner=owner)
            claim = registry.claim(draft.draft_id, owner=owner, approved=True)
            with self.assertRaises(ValueError):
                registry.finish(draft.draft_id, owner=owner, claim=claim, status='sent', message_id=5)
            receipt = registry.finish(draft.draft_id, owner=owner, claim=claim, status='applied', observed=1700000500)
            self.assertEqual((receipt.status, receipt.observed), ('applied', 1700000500))
            with self.assertRaises(Exception):  # Another chat than the snapshot's.
                registry.prepare_change(owner=owner, chat_id=CHAT - 1, chat_title='Team Chat', change=spec)


if __name__ == '__main__':
    unittest.main()
