"""0.58 formatted captions (criterion 5): gate, preview digest, closed transport, exact read-back.

The five caller styles may now style a document, photo, voice-note or video caption, behind the default-off
`formatted_caption_send` (send-dependent, so `required_capabilities()` stays the single source). A styled caption must
already be the text Telegram stores, so the spans address it as given. The artifact preview gains a digest over the
caption and its styles, reply-artifact facts move to v2 for a styled caption, the tdjson guards admit only canonical
frozen styles (album items stay plain) and the read-back is exact. Raw transports and owner decisions are synthetic;
drafts, broker, proxy contracts and reducers are production code.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from telegram_search_mcp.config import CAPABILITY_PROFILES, KNOWN_CAPABILITIES, RuntimePolicy, profile_of
from telegram_search_mcp.contract import (OPERATION_CAPABILITIES, READ_DEPENDENT_CAPABILITIES,
                                          SEND_DEPENDENT_CAPABILITIES, required_capabilities)
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, TDLibClient,
                                        _validate_bounded_request)
from telegram_search_mcp.approval_prompt import render_approval
from telegram_search_mcp.reply_drafts import SEND_OPTIONS

CAPTION = 'Report 😀 final'
STYLES = [{'offset': 0, 'length': 6, 'type': {'@type': 'textEntityTypeBold'}},
          {'offset': 7, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}}]
NATIVE = [{'@type': 'textEntity', **row} for row in STYLES]
FROZEN = ((0, 6, 'textEntityTypeBold'), (7, 2, 'textEntityTypeSpoiler'))
# TDLib's form of NATIVE as it returns it (split_entities cuts nothing here, but adds the link it detects).
STAGE = Path.home() / 'Library/Application Support/TelegramSearchMCP/outgoing-staging' / ('draft_' + 'a' * 32)


def canonical(value):
    """An independent canonical JSON digest; never the implementation helper."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


class CapabilityTests(unittest.TestCase):
    def test_the_gate_is_one_default_off_send_dependent_capability(self):
        self.assertIn('formatted_caption_send', KNOWN_CAPABILITIES)
        self.assertIn('formatted_caption_send', SEND_DEPENDENT_CAPABILITIES)
        self.assertNotIn('formatted_caption_send', READ_DEPENDENT_CAPABILITIES)
        self.assertEqual(required_capabilities('formatted_caption_send'), ('formatted_caption_send', 'send'))
        self.assertNotIn('formatted_caption_send', OPERATION_CAPABILITIES.values())
        self.assertEqual(profile_of('formatted_caption_send'), 'send')
        self.assertEqual(CAPABILITY_PROFILES['send'], ('send', 'formatted_text_send', 'topic_text_send',
                                                       'formatted_caption_send'))
        self.assertNotIn('formatted_caption_send', RuntimePolicy().enabled_capabilities)


class RequestContractTests(unittest.TestCase):
    def requests(self):
        from telegram_search_mcp.draft_models import UpdateDraftRequest
        from telegram_search_mcp.reply_artifact_drafts import (PrepareReplyArtifactSendRequest,
                                                               UpdateReplyArtifactDraftRequest)
        from telegram_search_mcp.schemas import PrepareArtifactSendRequest
        from telegram_search_mcp.video_drafts import PrepareVideoSendRequest
        artifact = dict(artifact_id='artifact_' + 'c' * 32 + '_' + 'a' * 64 + '_10', recipient=123,
                        display_name='report.txt', mime_type='text/plain')
        return [(PrepareArtifactSendRequest, artifact),
                (PrepareReplyArtifactSendRequest, dict(artifact, reply_to={'chat_id': 123, 'message_id': 55})),
                (UpdateReplyArtifactDraftRequest, dict(draft_id='draft_' + '1' * 32)),
                (UpdateDraftRequest, dict(draft_id='draft_' + '1' * 32)),
                (PrepareVideoSendRequest, dict(artifact_id=artifact['artifact_id'], recipient=123))]

    def test_styles_address_a_caption_already_in_its_stored_form(self):
        for model, base in self.requests():
            with self.subTest(model=model.__name__):
                value = model(**base, caption=CAPTION, outgoing_entities=list(reversed(STYLES)))
                self.assertEqual(value.model_dump(mode='json')['outgoing_entities'], STYLES)
                plain = model(**base, caption=' Plain\r\ncaption ')
                self.assertNotIn('outgoing_entities', plain.model_dump(mode='json'))
                for caption, entities in [('', STYLES[:1]), (' ' + CAPTION, STYLES), (CAPTION + '\n', STYLES),
                                          ('Ｒeport 😀 final', STYLES), ('Report\r\nend', STYLES[:1]),
                                          ('Report​😀 final', STYLES[:1]), ('Report\tx', STYLES[:1]),
                                          (CAPTION, [{'offset': 0, 'length': 6,
                                                      'type': {'@type': 'textEntityTypeCode'}}]),
                                          (CAPTION, [{'offset': 0, 'length': 8,
                                                      'type': {'@type': 'textEntityTypeBold'}}]),
                                          (CAPTION, [STYLES[0], STYLES[0]]),
                                          (CAPTION, [dict(STYLES[0], extra=1)])]:
                    with self.subTest(caption=caption, entities=entities), self.assertRaises(ValidationError):
                        model(**base, caption=caption, outgoing_entities=entities)

    def test_text_styles_keep_their_own_update_rule(self):
        from telegram_search_mcp.draft_models import UpdateDraftRequest
        value = UpdateDraftRequest(draft_id='draft_' + '1' * 32, text='Hello world',
                                   outgoing_entities=[STYLES[0]])
        self.assertEqual(value.outgoing_entities[0].offset, 0)
        with self.assertRaises(ValidationError):
            UpdateDraftRequest(draft_id='draft_' + '1' * 32, outgoing_entities=[STYLES[0]])


def artifact_preview(caption=CAPTION, entities=STYLES, kind='document', **changes):
    data = b'synthetic file'
    sha = hashlib.sha256(data).hexdigest()
    value = dict(draft_id='draft_' + '1' * 32, account_id=7, recipient=123, recipient_title='Recipient', kind=kind,
                 expires_at='2030-01-02T03:04:05+00:00', sha256=sha, size_bytes=len(data),
                 artifact_id=f'artifact_{"c" * 32}_{sha}_{len(data)}', display_name='report.txt',
                 mime_type='text/plain', caption=caption, outgoing_entities=copy.deepcopy(entities))
    value.update(changes)
    return value


FACTS = ('draft_id', 'account_id', 'recipient', 'recipient_title', 'expires_at', 'kind', 'artifact_id', 'sha256',
         'size_bytes', 'display_name', 'mime_type', 'caption', 'outgoing_entities')
VOICE = ('duration_seconds', 'waveform_base64', 'source_sha256', 'source_display_name', 'converted')


def expected_artifact_digest(value):
    """An independent rebuild of the artifact preview digest: every shown fact, the caption with its native styles
    and the fixed request shape of the one sendMessage the draft makes."""
    kind = value['kind']
    expires = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
    content = ({'thumbnail': None, 'disable_content_type_detection': True} if kind == 'document' else
               {'thumbnail': None, 'video': None, 'added_sticker_file_ids': [], 'width': 0, 'height': 0,
                'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}
               if kind == 'photo' else
               {'duration': value['duration_seconds'], 'waveform': value['waveform_base64'],
                'self_destruct_type': None})
    return canonical({'domain': 'telegram-search-mcp.artifact', 'version': 1,
                      **{key: value[key] for key in FACTS if key not in ('expires_at', 'caption',
                                                                         'outgoing_entities')},
                      'expires_at': expires.isoformat(), 'approval_required': True,
                      'caption': {'@type': 'formattedText', 'text': value['caption'],
                                  'entities': [{'@type': 'textEntity', **row} for row in value['outgoing_entities']]},
                      'voice': {key: value.get(key) for key in VOICE} if kind == 'voice_note' else None,
                      'topic_id': None, 'reply_to': None, 'reply_markup': None, 'send_options': dict(SEND_OPTIONS),
                      'content_options': content})


def digest_of(value):
    from telegram_search_mcp.draft_models import artifact_preview_digest
    return artifact_preview_digest(**{key: value[key] for key in FACTS},
                                   **{key: value[key] for key in VOICE if key in value})


class PreviewDigestTests(unittest.TestCase):
    def test_a_styled_artifact_preview_binds_every_fact_through_its_digest(self):
        from telegram_search_mcp.draft_models import DraftPreview
        base = artifact_preview(entities=list(reversed(STYLES)))
        value = DraftPreview.model_validate({**base, 'preview_sha256': digest_of(base)})
        dumped = value.model_dump(mode='json')
        self.assertEqual(dumped['outgoing_entities'], STYLES)
        self.assertEqual(dumped['preview_sha256'], expected_artifact_digest(dumped))
        for key, changed in [('caption', 'Report 😀 other'), ('display_name', 'other.txt'), ('recipient', 124),
                             ('recipient_title', 'Other'), ('mime_type', 'text/csv'), ('account_id', 8),
                             ('expires_at', '2031-01-02T03:04:05+00:00'),
                             ('outgoing_entities', STYLES[:1])]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                DraftPreview.model_validate({**dumped, key: changed})
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate({**base})
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate({**base, 'caption': ' ' + CAPTION, 'preview_sha256': 'a' * 64})
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate({**artifact_preview(caption=''), 'preview_sha256': 'a' * 64})

    def test_a_plain_artifact_preview_keeps_its_wire(self):
        from telegram_search_mcp.draft_models import DraftPreview
        plain = artifact_preview(entities=[])
        dumped = DraftPreview.model_validate(plain).model_dump(mode='json')
        self.assertNotIn('outgoing_entities', dumped)
        self.assertNotIn('preview_sha256', dumped)
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate({**plain, 'preview_sha256': 'a' * 64})

    def test_reply_artifact_facts_move_to_v2_only_for_a_styled_caption(self):
        from telegram_search_mcp.draft_models import DraftPreview
        from telegram_search_mcp.reply_artifact_drafts import preview_facts
        from telegram_search_mcp.reply_drafts import ReplyTarget
        target = ReplyTarget(anchor={'chat_id': 123, 'message_id': 55}, text='[untrusted Telegram evidence] x',
                             source_sha256='a' * 64, sanitized=False)
        base = artifact_preview()
        styled = DraftPreview.model_validate({**base, 'preview_sha256': digest_of(base)})
        facts = preview_facts(styled, target)
        self.assertEqual(facts['version'], 2)
        self.assertEqual(facts['outgoing_entities'], NATIVE)
        plain = preview_facts(DraftPreview.model_validate(artifact_preview(entities=[])), target)
        self.assertEqual((plain['version'], plain['outgoing_entities']), (1, []))


def media_content(kind, caption=CAPTION, entities=None):
    local = {'@type': 'inputFileLocal', 'path': str(STAGE / 'file.bin')}
    formatted = {'@type': 'formattedText', 'text': caption, 'entities': copy.deepcopy(NATIVE if entities is None
                                                                                       else entities)}
    if kind == 'document':
        return {'@type': 'inputMessageDocument', 'document': {'@type': 'inputDocument', 'document': local,
                'thumbnail': None, 'disable_content_type_detection': True}, 'caption': formatted}
    if kind == 'photo':
        return {'@type': 'inputMessagePhoto', 'photo': {'@type': 'inputPhoto', 'photo': local, 'thumbnail': None,
                'video': None, 'added_sticker_file_ids': [], 'width': 0, 'height': 0}, 'caption': formatted,
                'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}
    if kind == 'voice_note':
        return {'@type': 'inputMessageVoiceNote', 'voice_note': {'@type': 'inputVoiceNote', 'voice_note': local,
                'duration': 2, 'waveform': 'A' * 84}, 'caption': formatted, 'self_destruct_type': None}
    return {'@type': 'inputMessageVideo', 'video': {'@type': 'inputVideo', 'video': local, 'thumbnail': None,
            'cover': None, 'start_timestamp': 0, 'added_sticker_file_ids': [], 'duration': 5, 'width': 1280,
            'height': 720, 'supports_streaming': True}, 'caption': formatted, 'show_caption_above_media': False,
            'self_destruct_type': None, 'has_spoiler': False}


def send_request(content):
    return {'@type': 'sendMessage', 'chat_id': 123, 'topic_id': None, 'reply_to': None, 'reply_markup': None,
            'options': {**SEND_OPTIONS, 'sending_id': 12}, 'input_message_content': content}


class GuardTests(unittest.TestCase):
    def test_a_send_admits_only_canonical_frozen_caption_styles(self):
        for kind in ('document', 'photo', 'voice_note', 'video'):
            with self.subTest(kind=kind):
                _validate_bounded_request(send_request(media_content(kind)))
                _validate_bounded_request(send_request(media_content(kind, entities=[])))
                for caption, entities in [
                        (CAPTION, list(reversed(NATIVE))), (CAPTION, [dict(NATIVE[0], extra=1)]),
                        (CAPTION, [STYLES[0]]), (CAPTION, [dict(NATIVE[0], type={'@type': 'textEntityTypeCode'})]),
                        (CAPTION, [dict(NATIVE[0], type={'@type': 'textEntityTypeTextUrl', 'url': 'x'})]),
                        (CAPTION, [dict(NATIVE[0], length=8)]), (' ' + CAPTION, NATIVE),
                        ('Report\t😀 final', NATIVE), ('', NATIVE[:1]), (CAPTION, NATIVE * 17)]:
                    with self.subTest(caption=caption, entities=entities), self.assertRaises(ForbiddenTDLibRequest):
                        _validate_bounded_request(send_request(media_content(kind, caption, entities)))

    def test_album_items_stay_plain(self):
        stage = STAGE.parent
        items = [media_content('photo', entities=[]), media_content('video', entities=[])]
        for index, item in enumerate(items):
            item['photo' if index == 0 else 'video']['photo' if index == 0 else 'video']['path'] = str(
                stage / ('draft_' + str(index) * 32) / 'file.bin')
        album = {'@type': 'sendMessageAlbum', 'chat_id': 123, 'topic_id': None, 'reply_to': None,
                 'options': {**SEND_OPTIONS, 'sending_id': 12}, 'input_message_contents': items}
        _validate_bounded_request(album)
        styled = copy.deepcopy(album)
        styled['input_message_contents'][0]['caption']['entities'] = copy.deepcopy(NATIVE)
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request(styled)


class StyledRaw:
    """Answers chat, account and one sendMessage; the returned caption carries the sent styles (or `returned`)."""

    def __init__(self, returned=None):
        self.sent, self.events, self.account, self.title, self.returned = [], [], 7, 'Recipient', returned

    def send(self, request):
        self.sent.append(copy.deepcopy(request))
        kind = request['@type']
        if kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            value = {'@type': 'chat', 'id': request['chat_id'], 'title': self.title,
                     'type': {'@type': 'chatTypePrivate', 'user_id': request['chat_id']}}
        elif kind == 'sendMessage':
            sent = request['input_message_content']
            entities = sent['caption']['entities'] if self.returned is None else self.returned
            content = {'@type': {'inputMessageDocument': 'messageDocument', 'inputMessagePhoto': 'messagePhoto',
                                 'inputMessageVoiceNote': 'messageVoiceNote'}[sent['@type']],
                       'caption': {'@type': 'formattedText', 'text': sent['caption']['text'],
                                   'entities': copy.deepcopy(entities)}}
            if content['@type'] == 'messageVoiceNote':
                content['voice_note'] = {'@type': 'voiceNote', 'duration': 2}
            pending = {'@type': 'message', 'id': -10, 'chat_id': 123, 'is_outgoing': True, 'content': content,
                       'sending_state': {'@type': 'messageSendingStatePending',
                                         'sending_id': request['options']['sending_id']}}
            self.events.append({**pending, '@extra': request['@extra']})
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                                'message': dict(pending, id=701, sending_state=None)})
            return
        else:
            raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        return self.events.pop(0) if self.events else None

    def close(self):
        pass


class TransportTests(unittest.TestCase):
    def client(self, raw):
        client = TDLibClient(raw=raw)
        client._ready = True
        return client

    def send(self, client, kind, **changes):
        values = dict(attempt_id='attempt', outgoing_entities=FROZEN, expected_account_id=7,
                      expected_recipient_title='Recipient', pre_send_guard=lambda: True)
        values.update(changes)
        if kind == 'document':
            return client.send_document_message(123, STAGE / 'report.txt', CAPTION, **values)
        if kind == 'photo':
            return client.send_photo_message(123, STAGE / 'picture.png', CAPTION, **values)
        return client.send_voice_note_message(123, STAGE / 'speech.ogg', CAPTION, 2, 'A' * 84, **values)

    def test_a_styled_caption_is_one_exact_attempt_read_back_with_its_styles(self):
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                raw = StyledRaw()
                client = self.client(raw)
                self.assertEqual(self.send(client, kind), 701)
                sends = [request for request in raw.sent if request['@type'] == 'sendMessage']
                self.assertEqual(len(sends), 1)
                self.assertEqual(sends[0]['input_message_content']['caption'],
                                 {'@type': 'formattedText', 'text': CAPTION, 'entities': NATIVE})
                observation = client._send_observations.snapshot('attempt')
                self.assertEqual((observation.outgoing_entities, observation.caption_sha256),
                                 (FROZEN, hashlib.sha256(CAPTION.encode()).hexdigest()))

    def test_tdlibs_split_form_and_detected_links_confirm_but_another_style_does_not(self):
        caption_rows = [{'@type': 'textEntity', 'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeBold'}},
                        {'@type': 'textEntity', 'offset': 3, 'length': 3, 'type': {'@type': 'textEntityTypeBold'}},
                        NATIVE[1]]
        from telegram_search_mcp.tdjson import MessageSendOutcomeUnknown
        for returned, confirmed in [(caption_rows, True), (NATIVE[:1], False),
                                    ([*NATIVE, dict(NATIVE[0], type={'@type': 'textEntityTypeItalic'})], False)]:
            with self.subTest(returned=returned):
                raw = StyledRaw(returned)
                client = self.client(raw)
                if confirmed:
                    self.assertEqual(self.send(client, 'document'), 701)
                else:
                    with self.assertRaises(MessageSendOutcomeUnknown):
                        self.send(client, 'document')
                self.assertEqual(len([r for r in raw.sent if r['@type'] == 'sendMessage']), 1)

    def test_a_styled_caption_needs_reviewed_current_authority(self):
        for kind in ('document', 'photo', 'voice_note'):
            for field, value in [('expected_account_id', None), ('expected_account_id', 8),
                                 ('expected_recipient_title', 'Other'), ('pre_send_guard', None),
                                 ('pre_send_guard', lambda: False), ('attempt_id', None),
                                 ('outgoing_entities', tuple(reversed(FROZEN))),
                                 ('outgoing_entities', ((0, 6, 'textEntityTypeCode'),))]:
                with self.subTest(kind=kind, field=field, value=value):
                    raw = StyledRaw()
                    client = self.client(raw)
                    with self.assertRaises(MessageSendNotAttempted):
                        self.send(client, kind, **{field: value})
                    self.assertFalse(any(request['@type'] == 'sendMessage' for request in raw.sent))
                    self.assertIsNone(client._send_observations.snapshot('attempt'))

    def test_a_plain_caption_send_keeps_its_wire(self):
        raw = StyledRaw()
        client = self.client(raw)
        self.assertEqual(client.send_document_message(123, STAGE / 'report.txt', CAPTION, attempt_id='attempt'), 701)
        sends = [request for request in raw.sent if request['@type'] == 'sendMessage']
        self.assertEqual(sends[0]['input_message_content']['caption']['entities'], [])
        self.assertEqual([request['@type'] for request in raw.sent], ['sendMessage'])


from test_artifact_replies import ArtifactRaw, ArtifactSendFixture, content_fixture  # noqa: E402
from test_text_replies import ANCHOR, CLIENT, outgoing  # noqa: E402

CAPS = ('send', 'reply_text_send', 'reply_artifact_send')


class EchoRaw(ArtifactRaw):
    """The artifact raw transport, returning the caption entities that were sent (or `returned`)."""
    returned = None

    def send(self, request):
        if request['@type'] != 'sendMessage':
            return super().send(request)
        before = len(self.events)
        super().send(request)
        entities = request['input_message_content']['caption']['entities'] if self.returned is None else self.returned
        for event in list(self.events)[before:]:
            message = event.get('message', event)
            message['content']['caption']['entities'] = copy.deepcopy(entities)
            if request['reply_to'] is None:
                message.pop('reply_to', None)


class ArtifactLifecycleTests(ArtifactSendFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*CAPS, 'formatted_caption_send'))
        self.raw = EchoRaw()
        self.raw.events.clear()
        self.client._raw = self.raw

    def payload(self, **changes):
        value = dict(artifact_id=self.artifact.artifact_id, recipient=123, display_name='evidence.txt',
                     mime_type='text/plain', caption=CAPTION, kind='document', outgoing_entities=copy.deepcopy(STYLES))
        value.update(changes)
        return value

    def disable(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=CAPS)

    def test_disabled_gate_refuses_before_any_draft_or_provider_work(self):
        self.disable()
        before = self.broker._drafts.known_ids()
        result = self.dispatch('prepare_artifact_send', self.payload())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.broker._drafts.known_ids(), before)
        self.assertFalse([r for r in self.raw.sent if r['@type'] in ('getChat', 'getMessage', 'sendMessage')])
        reply = self.dispatch('prepare_reply_artifact_send', self.payload(reply_to=ANCHOR))
        self.assertEqual((reply['status'], reply['missing_capabilities']), ('capability_disabled',
                                                                            ['formatted_caption_send']))
        self.assertEqual(self.sends(), [])

    def test_prepare_inspect_and_send_one_exact_styled_document(self):
        prepared = self.dispatch('prepare_artifact_send', self.payload(outgoing_entities=list(reversed(STYLES))))
        self.assertEqual(prepared['status'], 'prepared', prepared)
        self.assertEqual(prepared['outgoing_entities'], STYLES)
        draft = self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['draft']
        self.assertEqual((draft['caption'], draft['outgoing_entities']), (CAPTION, STYLES))
        self.assertEqual(draft['preview_sha256'], expected_artifact_digest(draft))
        self.assertEqual(prepared['preview_sha256'], draft['preview_sha256'])
        sent = self.send(prepared['draft_id'])
        self.assertEqual((sent['status'], sent['message_id']), ('sent', 701), sent)
        self.assertEqual(self.approvals[-1]['draft_preview'], draft)
        shown = render_approval(**self.approvals[-1])  # the broker's own dialog arguments render (criterion 6)
        self.assertIn('Caption styles (exact caption; UTF-16 code units):', shown[0])
        wire = self.sends()[0]
        self.assertEqual(wire['input_message_content']['caption'],
                         {'@type': 'formattedText', 'text': CAPTION, 'entities': NATIVE})
        self.assertIsNone(wire['reply_to'])
        self.assertEqual(self.send(prepared['draft_id'])['message_id'], 701)
        self.assertEqual(len(self.sends()), 1)
        status = self.dispatch('get_send_status', {'draft_id': prepared['draft_id']})
        self.assertEqual((status['status'], status['message_id']), ('sent', 701))

    def test_a_plain_document_keeps_its_prepare_preview_and_wire(self):
        prepared = self.dispatch('prepare_artifact_send', self.payload(outgoing_entities=[]))
        self.assertNotIn('outgoing_entities', prepared)
        self.assertNotIn('preview_sha256', prepared)
        draft = self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['draft']
        self.assertNotIn('preview_sha256', draft)
        self.disable()
        self.assertEqual(self.send(prepared['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends()[0]['input_message_content']['caption']['entities'], [])

    def test_revoking_the_gate_or_changing_the_provider_refuses_without_an_attempt(self):
        prepared = self.dispatch('prepare_artifact_send', self.payload())
        self.disable()
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')
        self.assertNotEqual(self.send(prepared['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends(), [])
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*CAPS, 'formatted_caption_send'))
        prepared = self.dispatch('prepare_artifact_send', self.payload())
        self.client._invalidate_send_observations()  # a new provider epoch: the reviewed authority is gone
        self.assertNotEqual(self.send(prepared['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends(), [])

    def test_owner_revocation_at_the_last_guard_leaves_no_attempt(self):
        prepared = self.dispatch('prepare_artifact_send', self.payload())
        def revoke(**facts):
            self.approvals.append(facts)
            self.disable()
            return True
        self.broker._approval_prompt = revoke
        result = self.send(prepared['draft_id'])
        self.assertNotEqual(result['status'], 'sent')
        self.assertEqual(self.sends(), [])

    def test_update_replaces_or_clears_styles_and_refresh_keeps_them(self):
        prepared = self.dispatch('prepare_artifact_send', self.payload())
        restyled = self.dispatch('update_draft', {'draft_id': prepared['draft_id'], 'caption': 'New caption',
                                                  'outgoing_entities': [STYLES[0]]})
        self.assertEqual(restyled['status'], 'revised', restyled)
        self.assertEqual((restyled['draft']['caption'], restyled['draft']['outgoing_entities']),
                         ('New caption', [STYLES[0]]))
        refreshed = self.dispatch('refresh_draft', {'draft_id': restyled['draft']['draft_id']})
        self.assertEqual(refreshed['draft']['outgoing_entities'], [STYLES[0]])
        cleared = self.dispatch('update_draft', {'draft_id': refreshed['draft']['draft_id'], 'caption': ' Plain '})
        self.assertEqual(cleared['draft']['caption'], 'Plain')
        self.assertNotIn('outgoing_entities', cleared['draft'])
        self.assertNotIn('preview_sha256', cleared['draft'])
        self.disable()
        refused = self.dispatch('update_draft', {'draft_id': cleared['draft']['draft_id'], 'caption': CAPTION,
                                                 'outgoing_entities': STYLES})
        # Named as prepare_text_send names a disabled option (final acceptance, closing pass); the draft stays.
        self.assertEqual({key: refused.get(key) for key in ('status', 'missing_capabilities', 'detail', 'next_action')},
                         {'status': 'capability_disabled', 'missing_capabilities': ['formatted_caption_send'],
                          'detail': 'draft revision needs capabilities the owner has not enabled: '
                                    'missing_capabilities', 'next_action': 'enable_capability'})
        self.assertEqual(self.dispatch('get_draft', {'draft_id': cleared['draft']['draft_id']})['status'], 'pending')

    def test_a_styled_reply_moves_its_facts_to_v2_and_sends_exactly(self):
        prepared = self.dispatch('prepare_reply_artifact_send', self.payload(reply_to=ANCHOR))
        self.assertEqual(prepared['status'], 'prepared', prepared)
        reply = prepared['reply']
        self.assertEqual(reply['draft']['outgoing_entities'], STYLES)
        facts = {'domain': 'telegram-search-mcp.reply-artifact', 'version': 2, 'kind': 'document',
                 'draft': reply['draft'], 'reply_target': reply['reply_target'], 'topic_id': None,
                 'outgoing_entities': NATIVE, 'send_options': dict(SEND_OPTIONS), 'reply_markup': None,
                 'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55, 'quote': None,
                              'checklist_task_id': 0, 'poll_option_id': ''},
                 'content_options': {'thumbnail': None, 'disable_content_type_detection': True}}
        self.assertEqual(reply['preview_sha256'], canonical(facts))
        self.assertEqual(self.dispatch('get_reply_artifact_draft', {'draft_id': reply['draft']['draft_id']})['reply'],
                         reply)
        sent = self.send(reply['draft']['draft_id'])
        self.assertEqual((sent['status'], sent['message_id']), ('sent', 701), sent)
        self.assertEqual(self.approvals[-1]['reply_artifact_preview'], reply)
        from telegram_search_mcp.reply_drafts import source_from_message
        self.assertEqual(self.approvals[-1]['reply_source'],
                         source_from_message(self.raw.source, 123, 55).projection_json)
        shown = render_approval(**self.approvals[-1])  # the stored projection renders the reviewed target
        self.assertIn('Caption styles (exact caption; UTF-16 code units):', shown[0])
        wire = self.sends()[0]
        self.assertEqual(wire['input_message_content']['caption']['entities'], NATIVE)
        self.assertEqual(wire['reply_to']['message_id'], 55)

    def test_a_styled_reply_names_its_option_after_the_tool_and_send(self):
        from telegram_search_mcp.contract import CapabilityDisabled
        from telegram_search_mcp.reply_artifact_drafts import PrepareReplyArtifactSendRequest
        self.broker._policy = RuntimePolicy(enabled_capabilities=('reply_text_send',))
        request = PrepareReplyArtifactSendRequest(**self.payload(reply_to=ANCHOR))
        with patch.object(self.broker, '_reply_artifact_owner', side_effect=CapabilityDisabled(
                ('reply_artifact_send', 'send', 'formatted_caption_send'))) as owner:
            self.broker._prepare_reply_artifact_send(request, client_id=CLIENT)
        owner.assert_not_called()  # the request's own option is checked before the draft owner or Telegram
        result = self.broker._prepare_reply_artifact_send(request, client_id=CLIENT)
        self.assertEqual((result.status, result.missing_capabilities),
                         ('capability_disabled', ['reply_artifact_send', 'send', 'formatted_caption_send']))
        self.broker._policy = RuntimePolicy(enabled_capabilities=CAPS)
        prepared = self.dispatch('prepare_reply_artifact_send', self.payload(reply_to=ANCHOR, outgoing_entities=[]))
        self.assertEqual(prepared['status'], 'prepared')
        self.assertNotIn('outgoing_entities', prepared['reply']['draft'])
        update = self.dispatch('update_reply_artifact_draft', {'draft_id': prepared['reply']['draft']['draft_id'],
                                                               'caption': CAPTION, 'outgoing_entities': STYLES})
        self.assertEqual((update['status'], update['missing_capabilities']),
                         ('capability_disabled', ['formatted_caption_send']))

    def test_a_styled_reply_update_restyles_and_refresh_keeps_styles(self):
        prepared = self.dispatch('prepare_reply_artifact_send', self.payload(reply_to=ANCHOR))['reply']
        update = self.dispatch('update_reply_artifact_draft', {'draft_id': prepared['draft']['draft_id'],
                                                               'caption': 'Another caption',
                                                               'outgoing_entities': [STYLES[0]]})
        self.assertEqual(update['status'], 'revised', update)
        self.assertEqual(update['reply']['draft']['outgoing_entities'], [STYLES[0]])
        refreshed = self.dispatch('refresh_reply_artifact_draft', {'draft_id': update['reply']['draft']['draft_id']})
        self.assertEqual(refreshed['reply']['draft']['outgoing_entities'], [STYLES[0]])
        plain = self.dispatch('update_reply_artifact_draft', {'draft_id': refreshed['reply']['draft']['draft_id'],
                                                              'caption': 'Plain caption'})
        self.assertNotIn('outgoing_entities', plain['reply']['draft'])
        self.assertEqual(self.sends(), [])


from test_video_send import MOVIE, Raw as VideoRaw, VideoFixture  # noqa: E402


class StyledVideoRaw(VideoRaw):
    def send(self, request):
        before = len(self.events)
        super().send(request)
        if request['@type'] == 'sendMessage':
            for event in list(self.events)[before:]:
                message = event.get('message', event)
                if isinstance(message.get('content'), dict):
                    message['content']['caption']['entities'] = copy.deepcopy(
                        request['input_message_content']['caption']['entities'])


class VideoTests(VideoFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.raw = StyledVideoRaw()
        self.client._raw = self.raw
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send',
                                                                  'formatted_caption_send'))

    def test_a_styled_video_caption_is_previewed_digested_and_sent_exactly(self):
        video = self.prepare(caption=CAPTION, outgoing_entities=STYLES)
        self.assertEqual(video['outgoing_entities'], STYLES)
        sent = self.send(video['draft_id'])
        self.assertEqual((sent['status'], sent['message_id']), ('sent', 701), sent)
        self.assertEqual(self.approvals[-1]['video_preview'], video)
        self.assertIn('Caption styles (exact caption; UTF-16 code units):', render_approval(**self.approvals[-1])[0])
        self.assertEqual(self.sends()[0]['input_message_content']['caption'],
                         {'@type': 'formattedText', 'text': CAPTION, 'entities': NATIVE})

    def test_a_plain_video_keeps_its_preview_and_a_disabled_gate_refuses_styles(self):
        plain = self.prepare(caption=CAPTION)
        self.assertNotIn('outgoing_entities', plain)
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send'))
        before = self.broker._drafts.known_ids()
        refused = self.dispatch('prepare_video_send', self.request(self.upload(), caption=CAPTION,
                                                                   outgoing_entities=STYLES))
        self.assertEqual(refused['status'], 'unavailable')
        self.assertEqual(self.broker._drafts.known_ids(), before)

    # Review fixes (058, low): a styled video answers under the stored formatting authority, as a styled document does.

    def test_a_styled_video_receipt_replays_only_under_its_formatting_provider_and_epoch(self):
        styled = self.prepare(caption=CAPTION, outgoing_entities=STYLES)
        self.assertEqual(self.send(styled['draft_id'])['message_id'], 701)
        plain = self.prepare(caption=CAPTION)
        self.assertEqual(self.send(plain['draft_id'])['message_id'], 701)
        self.client._invalidate_send_observations()  # a new provider epoch: the reviewed authority is gone
        replay = self.send(styled['draft_id'])
        self.assertEqual((replay['status'], replay['message_id'], replay['recipient']),
                         ('outcome_unknown', None, None), replay)
        self.assertEqual(self.status(styled['draft_id'])['status'], 'outcome_unknown')
        kept = self.send(plain['draft_id'])  # a plain video keeps its pre-0.58 replay
        self.assertEqual((kept['status'], kept['message_id']), ('sent', 701), kept)
        self.assertEqual(len(self.sends()), 2)

    def revoke_after_send(self, *capabilities):
        raw_send = self.raw.send
        def send(request):
            raw_send(request)
            if request['@type'] == 'sendMessage':
                self.broker._policy = RuntimePolicy(enabled_capabilities=capabilities)
        self.raw.send = send

    def test_a_styled_video_receipt_is_withheld_when_its_option_is_revoked_after_the_send(self):
        styled = self.prepare(caption=CAPTION, outgoing_entities=STYLES)
        self.revoke_after_send('artifacts', 'send', 'video_send')
        result = self.send(styled['draft_id'])
        self.assertEqual((result['status'], result['message_id'], result['recipient']),
                         ('outcome_unknown', None, None), result)
        self.assertEqual(len(self.sends()), 1)
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send',
                                                                  'formatted_caption_send'))
        plain = self.prepare(caption=CAPTION)  # a plain video never needed the option
        result = self.send(plain['draft_id'])
        self.assertEqual((result['status'], result['message_id']), ('sent', 701), result)

    def test_a_styled_video_whose_formatting_epoch_changed_refuses_without_a_dialog(self):
        styled = self.prepare(caption=CAPTION, outgoing_entities=STYLES)
        self.client._invalidate_send_observations()
        for _ in range(2):  # every retry refuses before asking the owner
            result = self.send(styled['draft_id'])
            self.assertEqual(result['status'], 'expired', result)
        self.assertEqual((self.approvals, self.sends()), ([], []))
        plain = self.prepare(caption=CAPTION)
        self.assertEqual(self.send(plain['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.approvals), 1)

    def test_an_epoch_change_after_the_replay_check_still_refuses_before_the_dialog(self):
        styled = self.prepare(caption=CAPTION, outgoing_entities=STYLES)
        peek = self.broker._drafts.peek
        changed = []
        def peek_then_change(*args, **kwargs):
            entry = peek(*args, **kwargs)
            if not changed:  # the first peek follows the receipt check and precedes the send's epoch capture
                changed.append(self.client._invalidate_send_observations())
            return entry
        with patch.object(self.broker._drafts, 'peek', peek_then_change):
            result = self.send(styled['draft_id'])
        self.assertEqual((result['status'], len(changed)), ('expired', 1), result)
        self.assertEqual((self.approvals, self.sends()), ([], []))


if __name__ == '__main__':
    unittest.main()
