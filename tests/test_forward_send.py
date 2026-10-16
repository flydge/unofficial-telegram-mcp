"""F14: one exact owner-approved forward/copy with source revalidation and a receipt.

Synthetic raw transport only: projection, drafts, broker, proxy and TDLib
reducers are production code. No live TDLib or owner dialog is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from telegram_search_mcp.approval_prompt import confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import KNOWN_CAPABILITIES, LEGACY_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS, CompatibilityError
from telegram_search_mcp.forward_drafts import (ForwardChat, ForwardDraftPreview, ForwardSourcePreview,
    PrepareForwardSendRequest, PrepareForwardSendResponse, SendPreparedForwardRequest,
    SendPreparedForwardResponse, UNAVAILABLE)
from telegram_search_mcp.forward_sources import ForwardSnapshot, ForwardSource
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.send_observations import ObservationUnavailable, SendObservations
from telegram_search_mcp.send_status_models import GetSendStatusRequest
from telegram_search_mcp.tdjson import ForbiddenTDLibRequest, TDLibClient, _ALLOWED_REQUEST_TYPES, _validate_bounded_request

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
SOURCE_CHAT = -1001234
RECIPIENT = 4242
POLICY = RuntimePolicy(enabled_capabilities=('send', 'forward_send'))
SEND_OPTIONS = {'@type': 'messageSendOptions', 'suggested_post_info': None, 'disable_notification': False,
    'from_background': False, 'protect_content': False, 'allow_paid_broadcast': False,
    'paid_message_star_count': 0, 'update_order_of_installed_sticker_sets': False,
    'scheduling_state': None, 'effect_id': 0, 'only_preview': False}
PROPERTIES = {'@type': 'messageProperties', 'can_be_forwarded': True, 'can_be_copied': True,
    'has_protected_content_by_current_user': False, 'has_protected_content_by_other_user': False,
    'can_be_replied': True, 'can_be_saved': True}
DID = 'draft_' + '1' * 32
EXPIRY = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SECRET_FILE_ID = 'REMOTE-FILE-ID-SECRET'
SECRET_PATH = '/private/should-not-leak'


def digest(value):
    # Independent expected adapter; never imports the implementation helper.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def chat(chat_id, title, kind='supergroup', protected=False, native=1234):
    types = {'supergroup': {'@type': 'chatTypeSupergroup', 'supergroup_id': native, 'is_channel': False},
             'private': {'@type': 'chatTypePrivate', 'user_id': native},
             'secret': {'@type': 'chatTypeSecret', 'secret_chat_id': 9, 'user_id': native}}
    return {'@type': 'chat', 'id': chat_id, 'title': title, 'type': types[kind], 'has_protected_content': protected}


def file(identifier, size, unique):
    return {'@type': 'file', 'id': identifier, 'size': size, 'expected_size': size,
        'local': {'@type': 'localFile', 'path': SECRET_PATH, 'can_be_downloaded': True, 'can_be_deleted': True,
                  'is_downloading_active': False, 'is_downloading_completed': True, 'download_offset': 0,
                  'downloaded_prefix_size': size, 'downloaded_size': size},
        'remote': {'@type': 'remoteFile', 'id': SECRET_FILE_ID, 'unique_id': unique,
                   'is_uploading_active': False, 'is_uploading_completed': True, 'uploaded_size': size}}


def formatted(text, entities=()):
    return {'@type': 'formattedText', 'text': text, 'entities': [dict(e) for e in entities]}


def text_content(text='Source text', entities=()):
    return {'@type': 'messageText', 'text': formatted(text, entities)}


def photo_content(caption='Photo caption', **changes):
    sizes = [{'@type': 'photoSize', 'type': 's', 'photo': file(11, 1000, 'UNIQUE-S'), 'width': 90, 'height': 60,
              'progressive_sizes': []},
             {'@type': 'photoSize', 'type': 'x', 'photo': file(12, 50000, 'UNIQUE-X'), 'width': 1280, 'height': 960,
              'progressive_sizes': [1000, 20000]}]
    return {'@type': 'messagePhoto', 'photo': {'@type': 'photo', 'has_stickers': False, 'sizes': sizes},
            'caption': formatted(caption), 'show_caption_above_media': False, 'has_spoiler': False,
            'is_secret': False, **changes}


CONTENTS = {
    'photo': photo_content(),
    'document': {'@type': 'messageDocument', 'document': {'@type': 'document', 'file_name': 'report.pdf',
        'mime_type': 'application/pdf', 'document': file(13, 2048, 'UNIQUE-D')}, 'caption': formatted('')},
    'voice_note': {'@type': 'messageVoiceNote', 'voice_note': {'@type': 'voiceNote', 'duration': 4,
        'waveform': 'AAAA', 'mime_type': 'audio/ogg', 'voice': file(14, 900, 'UNIQUE-V')},
        'caption': formatted(''), 'is_listened': False},
    'video': {'@type': 'messageVideo', 'video': {'@type': 'video', 'duration': 12, 'width': 640, 'height': 360,
        'file_name': 'clip.mp4', 'mime_type': 'video/mp4', 'has_stickers': False, 'supports_streaming': True,
        'video': file(15, 99999, 'UNIQUE-VID')}, 'alternative_videos': [], 'storyboards': [], 'cover': None,
        'start_timestamp': 0, 'caption': formatted('clip'), 'show_caption_above_media': False,
        'has_spoiler': False, 'is_secret': False},
    'audio': {'@type': 'messageAudio', 'audio': {'@type': 'audio', 'duration': 200, 'title': 'T', 'performer': 'P',
        'file_name': 'song.mp3', 'mime_type': 'audio/mpeg', 'album_cover_minithumbnail': None,
        'album_cover_thumbnail': None, 'external_album_covers': [], 'audio': file(16, 4000000, 'UNIQUE-A')},
        'caption': formatted('')},
    'animation': {'@type': 'messageAnimation', 'animation': {'@type': 'animation', 'duration': 3, 'width': 320,
        'height': 240, 'file_name': 'a.mp4', 'mime_type': 'video/mp4', 'has_stickers': False,
        'animation': file(17, 30000, 'UNIQUE-G')}, 'caption': formatted(''), 'show_caption_above_media': False,
        'has_spoiler': False, 'is_secret': False},
}


def source_message(content=None, **changes):
    return {'@type': 'message', 'chat_id': SOURCE_CHAT, 'id': 55,
            'sender_id': {'@type': 'messageSenderUser', 'user_id': 8}, 'is_outgoing': False,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
            'ephemeral_message_id': 0, 'interaction_info': {'@type': 'messageInteractionInfo', 'view_count': 3},
            'content': copy.deepcopy(content) if content is not None else text_content(), **changes}


def outgoing(identifier, kind='messageText', chat_id=RECIPIENT, **changes):
    return {'@type': 'message', 'id': identifier, 'chat_id': chat_id, 'is_outgoing': True,
            'content': {'@type': kind}, **changes}


class Raw:
    def __init__(self):
        self.sent = []; self.events = deque(); self.account = 7; self.mode = 'sent'; self.fail_receive = False
        self.source = source_message(); self.properties = dict(PROPERTIES)
        self.chats = {SOURCE_CHAT: chat(SOURCE_CHAT, 'Source Group'),
                      RECIPIENT: chat(RECIPIENT, 'Recipient', 'private', native=RECIPIENT)}

    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind = request['@type']
        if kind == 'getAuthorizationState':
            value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            value = copy.deepcopy(self.chats.get(request['chat_id'], {'@type': 'error', 'code': 400, 'message': 'x'}))
        elif kind == 'getMessage':
            value = copy.deepcopy(self.source)
        elif kind == 'getMessageProperties':
            value = copy.deepcopy(self.properties)
        elif kind == 'forwardMessages':
            content_type = self.source['content']['@type']
            pending = outgoing(-10, content_type, chat_id=999 if self.mode == 'wrong_chat' else RECIPIENT,
                sending_state={'@type': 'messageSendingStatePending', 'sending_id': request['options']['sending_id']})
            if self.mode == 'rejected':
                self.events.append({'@type': 'error', 'code': 403, 'message': 'PRIVATE PROVIDER TEXT',
                                    '@extra': request['@extra']}); return
            self.events.append({'@type': 'updateNewMessage', 'message': copy.deepcopy(pending)})
            if self.mode == 'lost_before_result':
                self.fail_receive = True; return
            result = {'@type': 'messages', 'total_count': 1, 'messages': [pending]}
            if self.mode == 'malformed':
                result = {'@type': 'messages', 'total_count': 2, 'messages': [pending, copy.deepcopy(pending)]}
            self.events.append({**result, '@extra': request['@extra']})
            if self.mode == 'sent':
                self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                                    'message': outgoing(701, content_type, sending_state=None)})
            elif self.mode == 'failed':
                self.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': -10,
                    'message': outgoing(-10, content_type, sending_state={'@type': 'messageSendingStateFailed',
                        'error': {'@type': 'error', 'code': 403}}),
                    'error': {'@type': 'error', 'code': 403, 'message': 'PRIVATE'}})
            else:
                self.fail_receive = True
            return
        else:
            raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        if self.events:
            return self.events.popleft()
        if self.fail_receive:
            self.fail_receive = False
            raise OSError('synthetic loss')
        return None

    def close(self):
        pass


class ForwardFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.raw = Raw(); self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
            client_factory=lambda: self.client, policy=POLICY, approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def request(self, mode='forward', **extra):
        return {'source_chat': SOURCE_CHAT, 'source_message_id': 55, 'recipient': RECIPIENT, 'mode': mode, **extra}

    def prepare(self, mode='forward', **extra):
        result = self.dispatch('prepare_forward_send', self.request(mode, **extra))
        self.assertEqual(result['status'], 'prepared', result)
        return result['forward']

    def send(self, did, approved=True, client=CLIENT):
        return self.dispatch('send_prepared_forward', {'draft_id': did, 'approved': approved}, client)

    def status(self, did, client=CLIENT):
        return self.dispatch('get_send_status', {'draft_id': did}, client)

    def forwards(self):
        return [x for x in self.raw.sent if x['@type'] == 'forwardMessages']


class CapabilityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_default_off_capability_is_registered_everywhere(self):
        self.assertIn('forward_send', KNOWN_CAPABILITIES)
        self.assertNotIn('forward_send', LEGACY_CAPABILITIES)
        self.assertNotIn('forward_send', RuntimePolicy().enabled_capabilities)
        for name in ('prepare_forward_send', 'send_prepared_forward'):
            self.assertEqual(OPERATION_CAPABILITIES[name], 'forward_send')
            self.assertEqual(TOOL_OPERATIONS[name], name)
            self.assertIn(name, OPERATIONS)

    async def test_sdk_lists_tools_but_refuses_without_forward_send_and_send(self):
        from mcp import Client
        from mcp.shared.exceptions import MCPError
        from telegram_search_mcp.server import build_server
        good = {'source_chat': SOURCE_CHAT, 'source_message_id': 55, 'recipient': RECIPIENT, 'mode': 'copy'}
        for policy, enabled in ((RuntimePolicy(), False), (RuntimePolicy(enabled_capabilities=('send',)), False),
                                (RuntimePolicy(enabled_capabilities=('forward_send',)), False), (POLICY, True)):
            calls = []
            class Service:
                def close(self): pass
                def prepare_forward_send(self, request):
                    calls.append(request); return PrepareForwardSendResponse(status='unavailable', detail=UNAVAILABLE)
                def send_prepared_forward(self, request):
                    calls.append(request)
                    return SendPreparedForwardResponse(status='not_approved', draft_id=request.draft_id, detail='synthetic')
            async with Client(build_server(service_factory=Service, policy=policy)) as consumer:
                tools = {t.name: t for t in (await consumer.list_tools()).tools}
                self.assertEqual(len(tools), 49)
                for name in ('prepare_forward_send', 'send_prepared_forward'):
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                    self.assertFalse(tools[name].annotations.read_only_hint)
                result = await consumer.call_tool('prepare_forward_send', good)
                sent = await consumer.call_tool('send_prepared_forward', {'draft_id': DID, 'approved': True})
                if not enabled:
                    self.assertTrue(result.is_error); self.assertTrue(sent.is_error); self.assertEqual(calls, [])
                    continue
                self.assertEqual(result.structured_content['status'], 'unavailable')
                self.assertEqual(sent.structured_content['status'], 'not_approved')
                self.assertEqual(calls[0], PrepareForwardSendRequest(**good)); calls.clear()
                for bad in ({'mode': 'move'}, {'source_message_id': '55'}, {'source_message_id': 0},
                            {'source_message_id': 2**53}, {'source_chat': 0}, {'source_chat': True},
                            {'remove_caption': 'true'}, {'remove_caption': 1}, {'mode': 'forward', 'remove_caption': True},
                            {'topic_id': 1}, {'message_ids': [55, 56]}):
                    try:
                        failed = await consumer.call_tool('prepare_forward_send', good | bad)
                    except MCPError:
                        continue
                    self.assertTrue(failed.is_error, bad)
                for bad in ({'approved': 'true'}, {'approved': 1}, {'draft_id': 'draft_bad'}, {'extra': 1}):
                    try:
                        failed = await consumer.call_tool('send_prepared_forward', {'draft_id': DID, 'approved': True} | bad)
                    except MCPError:
                        continue
                    self.assertTrue(failed.is_error, bad)
                self.assertEqual(calls, [])

    def test_proxy_refuses_before_connecting_unless_both_capabilities(self):
        for policy, enabled in ((RuntimePolicy(), False), (RuntimePolicy(enabled_capabilities=('send',)), False),
                                (RuntimePolicy(enabled_capabilities=('forward_send',)), False), (POLICY, True)):
            connects = []
            def connector(path):
                connects.append(path); raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            prepared = proxy.prepare_forward_send(PrepareForwardSendRequest(source_chat=SOURCE_CHAT,
                source_message_id=55, recipient=RECIPIENT, mode='forward'))
            sent = proxy.send_prepared_forward(SendPreparedForwardRequest(draft_id=DID, approved=True))
            self.assertEqual(prepared.status, 'unavailable')
            if enabled:
                self.assertEqual(len(connects), 4); self.assertEqual(sent.status, 'outcome_unknown')
            else:
                self.assertEqual(connects, []); self.assertEqual(sent.status, 'error')
                self.assertIn('capability_disabled', sent.detail)

    def test_broker_refuses_before_provider_unless_both_capabilities(self):
        for policy in (RuntimePolicy(), RuntimePolicy(enabled_capabilities=('send',)),
                       RuntimePolicy(enabled_capabilities=('forward_send',))):
            with tempfile.TemporaryDirectory() as directory:
                opened = []
                broker = Broker(socket_path=Path(directory) / 'broker.sock',
                    artifact_store=ArtifactStore(cache_dir=Path(directory) / 'cache'),
                    client_factory=lambda: opened.append(True), policy=policy, verify_peer_uid=False)
                self.addCleanup(broker.shutdown)
                for op, payload in (('prepare_forward_send', {'source_chat': SOURCE_CHAT, 'source_message_id': 55,
                                     'recipient': RECIPIENT, 'mode': 'forward'}),
                                    ('send_prepared_forward', {'draft_id': DID, 'approved': True})):
                    with self.subTest(policy=policy.enabled_capabilities, op=op), self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation': op, 'payload': payload, 'client_id': CLIENT,
                            'deadline': time.monotonic() + 10, 'broker_generation': broker._generation})
                self.assertEqual(opened, [])


class PrepareTests(ForwardFixture, unittest.TestCase):
    def test_prepare_projects_every_supported_kind_without_file_ids_or_paths(self):
        for kind, content in [('text', text_content('Hello', [{'@type': 'textEntity', 'offset': 0, 'length': 5,
                'type': {'@type': 'textEntityTypeTextUrl', 'url': 'https://example.invalid/'}}])), *CONTENTS.items()]:
            with self.subTest(kind=kind):
                self.raw.source = source_message(content)
                preview = self.prepare('copy')
                source = preview['source']
                self.assertEqual(source['content_kind'], kind)
                self.assertEqual(source['chat'], {'chat_id': SOURCE_CHAT, 'title': 'Source Group'})
                self.assertEqual(source['sender'], {'kind': 'user', 'id': 8})
                self.assertEqual(source['date'], '2023-11-14T22:13:20Z')
                self.assertEqual(preview['recipient'], {'chat_id': RECIPIENT, 'title': 'Recipient'})
                wire = json.dumps(preview)
                for secret in (SECRET_FILE_ID, SECRET_PATH, 'UNIQUE-', 'example.invalid', 'view_count'):
                    self.assertNotIn(secret, wire)
                expected = ForwardSource.from_message(self.raw.source, SOURCE_CHAT, 55)
                self.assertEqual(source['source_sha256'], hashlib.sha256(expected.projection_json.encode()).hexdigest())
                self.assertEqual((source['media'] is None), kind == 'text')
        self.assertEqual(self.forwards(), []); self.assertEqual(self.approvals, [])

    def test_media_identity_and_marked_sanitized_evidence(self):
        self.raw.source = source_message(photo_content('Look ‮evil‬ "here"'))
        source = self.prepare()['source']
        self.assertEqual(source['text'], '[untrusted Telegram evidence] Look evil "here"')
        self.assertTrue(source['text_sanitized'])
        self.assertEqual(source['media'], {'file_name': None, 'mime_type': None, 'duration_seconds': None,
            'width': 1280, 'height': 960, 'size_bytes': 50000,
            'unique_id_sha256': hashlib.sha256(b'UNIQUE-X').hexdigest(), 'variants': 2})
        self.raw.source = source_message(CONTENTS['document'])
        source = self.prepare()['source']
        self.assertIsNone(source['text']); self.assertFalse(source['text_sanitized'])
        self.assertEqual((source['media']['file_name'], source['media']['mime_type'], source['media']['size_bytes']),
                         ('[untrusted Telegram evidence] report.pdf', '[untrusted Telegram evidence] application/pdf', 2048))
        self.raw.source = source_message(dict(CONTENTS['document'], document=dict(CONTENTS['document']['document'],
            file_name='a\u202egnp.exe')))
        self.assertEqual(self.prepare()['source']['media']['file_name'], '[untrusted Telegram evidence] agnp.exe')

    def test_volatile_fields_do_not_change_source_digest_but_edits_do(self):
        first = self.prepare()['source']['source_sha256']
        self.raw.source['interaction_info'] = {'@type': 'messageInteractionInfo', 'view_count': 99}
        self.raw.source['content']['link_preview'] = {'@type': 'linkPreview'}
        self.assertEqual(self.prepare()['source']['source_sha256'], first)
        self.raw.source['edit_date'] = 1700000100
        self.assertNotEqual(self.prepare()['source']['source_sha256'], first)

    def assert_refused(self, code, mode='forward'):
        before = self.broker._drafts.known_ids()
        result = self.dispatch('prepare_forward_send', self.request(mode))
        self.assertEqual(result, {'status': 'refused', 'forward': None, 'refusal': code,
                                  'detail': 'source message is not eligible; no draft was created'})
        self.assertEqual(self.broker._drafts.known_ids(), before)

    def test_unsupported_content_and_state_refuse_without_draft(self):
        for content in ({'@type': 'messageSticker'}, {'@type': 'messagePoll'}, {'@type': 'messageLocation'},
                        photo_content(video={'@type': 'video'}), text_content('x' * 4097),
                        text_content('bad', [{'@type': 'textEntity', 'offset': 0, 'length': 9,
                                              'type': {'@type': 'textEntityTypeBold'}}])):
            with self.subTest(content=repr(content)[:60]):
                self.raw.source = source_message(content); self.assert_refused('unsupported_source_content')
        for changes in ({'self_destruct_type': {'@type': 'messageSelfDestructTypeTimer'}}, {'self_destruct_in': 5.0},
                        {'ephemeral_content': {'@type': 'ephemeralMessageContent'}}, {'ephemeral_message_id': 3},
                        {'scheduling_state': {'@type': 'messageSchedulingStateSendWhenOnline'}},
                        {'sending_state': {'@type': 'messageSendingStatePending', 'sending_id': 1}}, {'date': 0},
                        {'content': photo_content(is_secret=True)}):
            with self.subTest(changes=repr(changes)[:60]):
                self.raw.source = source_message(**changes); self.assert_refused('unsupported_source_state')
        self.assertEqual(self.forwards(), []); self.assertEqual(self.approvals, [])

    def test_forwardability_and_protected_content_refuse(self):
        self.raw.properties['can_be_forwarded'] = False
        self.assert_refused('source_not_forwardable')
        self.assertEqual(self.prepare('copy')['mode'], 'copy')
        self.raw.properties.update(can_be_forwarded=True, can_be_copied=False)
        self.assert_refused('source_not_forwardable', 'copy')
        self.raw.properties['can_be_copied'] = True
        for key in ('has_protected_content_by_current_user', 'has_protected_content_by_other_user'):
            with self.subTest(key=key):
                self.raw.properties[key] = True
                self.assert_refused('protected_content'); self.assert_refused('protected_content', 'copy')
                self.raw.properties[key] = False
        self.raw.chats[SOURCE_CHAT]['has_protected_content'] = True
        self.assert_refused('protected_content', 'copy')
        self.assertEqual(self.forwards(), [])

    def test_secret_missing_and_malformed_sources_are_unavailable(self):
        cases = [('chats', SOURCE_CHAT, chat(SOURCE_CHAT, 'S', 'secret')), ('chats', RECIPIENT, chat(RECIPIENT, 'R', 'secret')),
                 ('source', None, {'@type': 'error', 'code': 404, 'message': 'SECRET'}),
                 ('source', None, source_message(id=56)), ('source', None, source_message(sender_id={'@type': 'messageSenderUser', 'user_id': True})),
                 ('properties', None, {'@type': 'messageProperties', 'can_be_forwarded': 1, 'can_be_copied': True,
                     'has_protected_content_by_current_user': False, 'has_protected_content_by_other_user': False}),
                 ('properties', None, {'@type': 'messageProperties', 'can_be_forwarded': True})]
        for field, key, value in cases:
            with self.subTest(field=field, value=repr(value)[:60]):
                self.setUp()
                if field == 'chats':
                    self.raw.chats[key] = value
                else:
                    setattr(self.raw, field, value)
                result = self.dispatch('prepare_forward_send', self.request('copy'))
                self.assertEqual(result, {'status': 'unavailable', 'forward': None, 'refusal': None,
                                          'detail': 'forward draft is unavailable'})
                self.assertEqual(self.broker._drafts.known_ids(), set())
        self.setUp()
        with self.assertRaises(ValidationError):
            PrepareForwardSendRequest(source_chat=SOURCE_CHAT, source_message_id=55, recipient=RECIPIENT,
                                      mode='forward', remove_caption=True)
        with self.assertRaises(ValidationError):
            self.dispatch('prepare_forward_send', self.request('forward', remove_caption=True))
        self.assertEqual(self.prepare('copy', remove_caption=True)['remove_caption'], True)

    def test_generic_draft_tools_never_expose_or_mutate_forward_drafts(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {})):
            with self.subTest(op=op):
                self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'forward_send', 'reply_text_send'))
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        self.broker._policy = POLICY
        for op in ('send_prepared_text', 'send_prepared_artifact'):
            self.assertEqual(self.dispatch(op, {'draft_id': did, 'approved': True})['status'], 'error')
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual(len(self.forwards()), 1)

    def test_proxy_validates_preview_digest_and_request_scope(self):
        broker_result = self.dispatch('prepare_forward_send', self.request('copy', remove_caption=True))
        request = PrepareForwardSendRequest(**self.request('copy', remove_caption=True))
        def proxy(result):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=POLICY)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close); return client.prepare_forward_send(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        tampers = [('source', 'text', '[untrusted Telegram evidence] other'), ('source', 'message_id', 56),
                   ('recipient', 'chat_id', 5), ('forward', 'mode', 'forward'), ('forward', 'remove_caption', False),
                   ('forward', 'preview_sha256', 'f' * 64), ('source', 'text', 'unmarked')]
        for where, key, value in tampers:
            with self.subTest(where=where, key=key):
                result = copy.deepcopy(broker_result)
                target = result['forward'] if where == 'forward' else result['forward'][where]
                target[key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        # A coherent re-signed preview for another message/mode is still refused by scope binding.
        forged = ForwardDraftPreview.model_validate(broker_result['forward'])
        coherent = ForwardDraftPreview.create(draft_id=forged.draft_id, expires_at=forged.expires_at, mode='copy',
            remove_caption=False, source=forged.source, recipient=forged.recipient)
        result = dict(broker_result, forward=coherent.model_dump(mode='json'))
        self.assertEqual(proxy(result).status, 'unavailable')


class SendTests(ForwardFixture, unittest.TestCase):
    def test_one_exact_closed_forward_attempt_then_receipt_replay(self):
        for mode, remove in (('forward', False), ('copy', False), ('copy', True)):
            with self.subTest(mode=mode, remove=remove):
                self.setUp()
                preview = self.prepare(mode, **({'remove_caption': True} if remove else {}))
                result = self.send(preview['draft_id'])
                self.assertEqual((result['status'], result['message_id'], result['recipient']), ('sent', 701, RECIPIENT))
                [request] = self.forwards()
                sending_id = request['options']['sending_id']
                self.assertIs(type(sending_id), int); self.assertTrue(0 < sending_id < 2**31)
                self.assertEqual(request, {'@type': 'forwardMessages', 'chat_id': RECIPIENT, 'topic_id': None,
                    'from_chat_id': SOURCE_CHAT, 'message_ids': [55], 'options': {**SEND_OPTIONS, 'sending_id': sending_id},
                    'send_copy': mode == 'copy', 'remove_caption': remove, '@extra': request['@extra']})
                self.assertEqual(len(self.approvals), 1)
                self.assertEqual(self.approvals[0]['forward_preview'], preview)
                self.assertEqual(self.approvals[0]['kind'], 'forward')
                replay = self.send(preview['draft_id'])
                self.assertEqual((replay['status'], replay['message_id']), ('sent', 701))
                self.assertIn('no new send', replay['detail'])
                self.assertEqual(len(self.forwards()), 1); self.assertEqual(len(self.approvals), 1)

    def test_lost_malformed_and_rejected_results_never_retry(self):
        for mode, expected in (('lost', 'outcome_unknown'), ('lost_before_result', 'outcome_unknown'),
                               ('malformed', 'outcome_unknown'), ('wrong_chat', 'outcome_unknown'),
                               ('rejected', 'failed'), ('failed', 'failed')):
            with self.subTest(mode=mode):
                self.setUp(); self.raw.mode = mode
                did = self.prepare()['draft_id']
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['message_id'])
                self.assertEqual(self.send(did)['status'], expected)
                self.assertEqual(len(self.forwards()), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                if expected == 'failed':
                    self.assertIn('code 403', result['detail'])

    def test_not_approved_and_declined_dialog_make_no_attempt(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, approved=False)['status'], 'not_approved')
        self.assertEqual(self.approvals, [])
        self.broker._approval_prompt = lambda **kw: False
        self.assertEqual(self.send(did)['status'], 'not_approved')
        self.assertEqual(self.forwards(), [])
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.send(did)['status'], 'sent')

    def test_revalidation_before_dialog_refuses_without_dialog_or_attempt(self):
        cases = [
            ('recipient_changed', lambda raw: raw.chats[RECIPIENT].update(title='Renamed')),
            ('recipient_changed', lambda raw: raw.chats[RECIPIENT]['type'].update(user_id=9999)),
            ('source_changed', lambda raw: raw.source.update(edit_date=1700000100)),
            ('source_changed', lambda raw: raw.source['content']['text'].update(text='Edited')),
            ('source_changed', lambda raw: raw.source.update(content={'@type': 'messageSticker'})),
            ('source_changed', lambda raw: setattr(raw, 'source', {'@type': 'error', 'code': 404, 'message': 'gone'})),
            ('source_changed', lambda raw: raw.chats[SOURCE_CHAT].update(title='Renamed source')),
            ('source_not_forwardable', lambda raw: raw.properties.update(can_be_forwarded=False)),
            ('protected_content', lambda raw: raw.properties.update(has_protected_content_by_other_user=True)),
            ('protected_content', lambda raw: raw.chats[SOURCE_CHAT].update(has_protected_content=True)),
        ]
        for expected, change in cases:
            with self.subTest(expected=expected):
                self.setUp(); did = self.prepare()['draft_id']; change(self.raw)
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['message_id'])
                self.assertEqual(self.approvals, []); self.assertEqual(self.forwards(), [])

    def test_restart_foreign_owner_and_disabled_policy_are_content_free(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, client=FOREIGN)['status'], 'expired')
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send',))
        self.assertEqual(self.broker._send_prepared_forward(SendPreparedForwardRequest(draft_id=did, approved=True),
                                                            client_id=CLIENT).status, 'expired')
        with self.assertRaises(CompatibilityError):
            self.send(did)
        self.broker._policy = POLICY
        titles = dict(self.broker._draft_recipient_titles); self.broker._draft_recipient_titles.clear()
        self.assertEqual(self.send(did)['status'], 'expired')
        self.broker._draft_recipient_titles.update(titles)
        self.broker._drafts = OutgoingDraftRegistry(self.broker._artifact_store)
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(self.approvals, []); self.assertEqual(self.forwards(), [])

    def test_policy_change_during_dialog_or_post_approval_drift_never_sends(self):
        did = self.prepare()['draft_id']
        def disable(**kw):
            self.broker._policy = RuntimePolicy(enabled_capabilities=('send',)); return True
        self.broker._approval_prompt = disable
        self.assertEqual(self.broker._send_prepared_forward(SendPreparedForwardRequest(draft_id=did, approved=True),
                                                            client_id=CLIENT).status, 'expired')
        self.assertEqual(self.forwards(), [])
        for expected, change in (('local_failed', lambda raw: raw.source.update(edit_date=1700000100)),
                                 ('local_failed', lambda raw: raw.properties.update(can_be_forwarded=False)),
                                 ('local_failed', lambda raw: raw.chats[SOURCE_CHAT].update(has_protected_content=True)),
                                 ('local_failed', lambda raw: raw.chats[RECIPIENT].update(title='Renamed')),
                                 ('expired', lambda raw: setattr(raw, 'account', 8))):
            with self.subTest(expected=expected, change=change):
                self.setUp(); did = self.prepare()['draft_id']
                def approve(**kw):
                    change(self.raw); return True
                self.broker._approval_prompt = approve
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertEqual(self.forwards(), [])
                if expected == 'local_failed':
                    self.assertEqual(self.send(did)['status'], 'local_failed')
                    status = self.status(did)
                    self.assertEqual((status['status'], status['evidence']), ('failed', 'local_failed'))

    def test_post_approval_source_edit_is_local_failure(self):
        did = self.prepare()['draft_id']
        def approve(**kw):
            self.raw.source['edit_date'] = 1700000100; return True
        self.broker._approval_prompt = approve
        self.assertEqual(self.send(did)['status'], 'local_failed')
        self.assertEqual(self.forwards(), [])


    def test_broker_dialog_shows_the_exact_proxy_validated_preview(self):
        prepared = self.dispatch('prepare_forward_send', self.request('copy'))
        validated = PrepareForwardSendResponse.model_validate(prepared).forward
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or False
        self.assertEqual(self.send(validated.draft_id)['status'], 'not_approved')
        self.assertEqual(self.approvals[0]['forward_preview'], validated.model_dump(mode='json'))
        [text] = shown(**self.approvals[0])
        self.assertIn(f'Preview SHA-256: {validated.preview_sha256}\n', text)
        self.assertIn(f'Source SHA-256: {validated.source.source_sha256}\n', text)
        self.assertIn('COPY — sent as your own message, no attribution\n', text)
        self.assertEqual(self.forwards(), [])


class StatusTests(ForwardFixture, unittest.TestCase):
    def test_pending_then_late_exact_success_reconciles_without_retry(self):
        self.raw.mode = 'lost'
        did = self.prepare()['draft_id']
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('pending', 'local_pending'))
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'provider_pending'))
        self.raw.events.extend([
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(702, chat_id=999)},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(702, 'messagePhoto')},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(702, is_outgoing=False)},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(702, sending_state=None)}])
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence'], status['message_id']), ('sent', 'provider_confirmed', 702))
        self.assertEqual((self.send(did)['status'], self.send(did)['message_id']), ('sent', 702))
        self.assertEqual(len(self.forwards()), 1)

    def test_late_exact_failure_and_policy_bound_status(self):
        self.raw.mode = 'lost'
        did = self.prepare()['draft_id']; self.send(did)
        self.raw.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': -10,
            'message': outgoing(-10, sending_state={'@type': 'messageSendingStateFailed', 'error': {'@type': 'error', 'code': 400}}),
            'error': {'@type': 'error', 'code': 400, 'message': 'PRIVATE'}})
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('failed', 'provider_failed'))
        self.assertNotIn('PRIVATE', json.dumps(status))
        self.assertEqual(self.status(did, FOREIGN)['evidence'], 'none')
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send',))
        self.assertEqual(self.broker._get_send_status(GetSendStatusRequest(draft_id=did), client_id=CLIENT).evidence, 'none')

    def test_confirmed_send_status(self):
        did = self.prepare()['draft_id']; self.send(did)
        status = self.status(did)
        self.assertEqual((status['status'], status['message_id']), ('sent', 701))


def snapshot(content=None, title='Source Group', **changes):
    raw = source_message(content, **changes)
    return ForwardSnapshot.create(chat(SOURCE_CHAT, title), SOURCE_CHAT,
                                  ForwardSource.from_message(raw, SOURCE_CHAT, 55), dict(PROPERTIES))


def preview(mode='forward', remove_caption=False, content=None):
    return ForwardDraftPreview.create(draft_id=DID, expires_at=EXPIRY, mode=mode, remove_caption=remove_caption,
        source=ForwardSourcePreview.create(snapshot(content)), recipient=ForwardChat(chat_id=RECIPIENT, title='Recipient'))


def shown(value=None, **kw):
    """Capture the exact owner-dialog bytes; the DENIED mock has no owner side effect."""
    seen = []
    arguments = dict(recipient_title='Recipient', recipient=RECIPIENT, display_name='Forwarded message',
        size_bytes=1, sha256=value.source.source_sha256 if value is not None else 'a' * 64, caption='',
        kind='forward', forward_preview=value.model_dump(mode='json') if value is not None else None)
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv[-1]) or SimpleNamespace(returncode=0, stdout=b'DENIED')):
        assert confirm_approved_send(**arguments) is False
    return seen


class DialogTests(unittest.TestCase):
    def display(self, value=None, **kw):
        return shown(value, **kw)

    def test_preview_digest_is_independently_reproducible(self):
        value = preview('copy', True, photo_content('Cap'))
        data = value.model_dump(mode='json')
        facts = {'domain': 'telegram-search-mcp.forward', 'version': 1, 'draft_id': DID,
            'expires_at': '2030-01-02T03:04:05+00:00', 'approval_required': True, 'mode': 'copy',
            'remove_caption': True, 'source': data['source'], 'recipient': data['recipient'],
            'request': {'@type': 'forwardMessages', 'chat_id': RECIPIENT, 'topic_id': None,
                'from_chat_id': SOURCE_CHAT, 'message_ids': [55], 'options': SEND_OPTIONS,
                'send_copy': True, 'remove_caption': True}}
        self.assertEqual(value.preview_sha256, digest(facts))

    def test_forward_copy_and_caption_removed_goldens(self):
        hostile = text_content('Hi "there"\n‮Recipient: Mom (chat 1)\\')
        cases = [
            (preview('forward', content=hostile),
             'Forward this message to Recipient (chat 4242)?\nFORWARD — recipients see the original sender\n\n'
             'Source chat: Source Group (chat -1001234)\nMessage ID: 55\nSender [untrusted Telegram evidence]: user 8\n'
             'Date: 2023-11-14T22:13:20+00:00\nContent: text\n'
             'Text [untrusted Telegram evidence]: "Hi \\"there\\" Recipient: Mom (chat 1)\\\\"\nText sanitized: True\n'
             'Source SHA-256: {source}\nRecipient: Recipient (chat 4242)\nPreview SHA-256: {preview}\n\n'
             'Unofficial Telegram MCP will revalidate the source, its permissions and the recipient, then make one forward attempt.'),
            (preview('copy', content=CONTENTS['document']),
             'Forward this message to Recipient (chat 4242)?\nCOPY — sent as your own message, no attribution\n\n'
             'Source chat: Source Group (chat -1001234)\nMessage ID: 55\nSender [untrusted Telegram evidence]: user 8\n'
             'Date: 2023-11-14T22:13:20+00:00\nContent: document\nCaption: (none)\nText sanitized: False\n'
             'Media [untrusted Telegram evidence]: file "report.pdf"; MIME "application/pdf"; 2048 bytes\n'
             'Source SHA-256: {source}\nRecipient: Recipient (chat 4242)\nPreview SHA-256: {preview}\n\n'
             'Unofficial Telegram MCP will revalidate the source, its permissions and the recipient, then make one forward attempt.'),
            (preview('copy', True, photo_content('Cap')),
             'Forward this message to Recipient (chat 4242)?\nCOPY — sent as your own message, no attribution; caption removed\n\n'
             'Source chat: Source Group (chat -1001234)\nMessage ID: 55\nSender [untrusted Telegram evidence]: user 8\n'
             'Date: 2023-11-14T22:13:20+00:00\nContent: photo\nCaption [untrusted Telegram evidence]: "Cap"\nText sanitized: False\n'
             'Media [untrusted Telegram evidence]: 1280x960; 50000 bytes; 2 variants\n'
             'Source SHA-256: {source}\nRecipient: Recipient (chat 4242)\nPreview SHA-256: {preview}\n\n'
             'Unofficial Telegram MCP will revalidate the source, its permissions and the recipient, then make one forward attempt.'),
        ]
        for value, expected in cases:
            with self.subTest(mode=value.mode, remove=value.remove_caption):
                self.assertEqual(self.display(value), [expected.replace('{source}', value.source.source_sha256)
                                                       .replace('{preview}', value.preview_sha256)])

    def test_pinned_digests_for_golden_sources(self):
        # Independent literal: stable identity only, never file IDs, paths or raw unique IDs.
        projection = {'version': 1, 'chat_id': SOURCE_CHAT, 'message_id': 55, 'sender': {'type': 'user', 'id': 8},
            'date': 1700000000, 'edit_date': 0, 'content': {'kind': 'photo', 'caption': {'text': 'Cap', 'entities': []},
            'variants': [{'type': 's', 'width': 90, 'height': 60, 'size': 1000,
                          'unique_id_sha256': hashlib.sha256(b'UNIQUE-S').hexdigest()},
                         {'type': 'x', 'width': 1280, 'height': 960, 'size': 50000,
                          'unique_id_sha256': hashlib.sha256(b'UNIQUE-X').hexdigest()}]}}
        value = preview('copy', True, photo_content('Cap'))
        self.assertEqual(value.source.source_sha256, digest(projection))
        self.assertEqual(value.source.source_sha256, '5fe403fdc3b471ee447a71ae3e314b635d115e0a15d852c23f966f59a45ae050')
        self.assertEqual(value.preview_sha256, 'e778c6f001aa29034ffd52fa27fdd4658c8be2178cbb829a43d7799ec2fbdf43')

    def test_competing_mismatched_or_tampered_forward_previews_refuse(self):
        value = preview()
        from test_formatting_public_owner import reply
        self.assertEqual(self.display(value, reply_preview=reply()), [])
        self.assertEqual(self.display(value, kind='text'), [])
        self.assertEqual(self.display(None), [])
        self.assertEqual(self.display(value, recipient=RECIPIENT + 1), [])
        self.assertEqual(self.display(value, recipient_title='Other'), [])
        self.assertEqual(self.display(value, sha256='b' * 64), [])
        self.assertEqual(self.display(value, text='smuggled'), [])
        data = value.model_dump(mode='json'); data['source']['text'] = '[untrusted Telegram evidence] Other'
        self.assertEqual(self.display(value, forward_preview=data), [])
        data = value.model_dump(mode='json'); data['mode'] = 'copy'
        self.assertEqual(self.display(value, forward_preview=data), [])



class TransportShapeTests(unittest.TestCase):
    def valid(self, **changes):
        return {'@type': 'forwardMessages', 'chat_id': RECIPIENT, 'topic_id': None, 'from_chat_id': SOURCE_CHAT,
                'message_ids': [55], 'options': {**SEND_OPTIONS, 'sending_id': 77}, 'send_copy': True,
                'remove_caption': False, **changes}

    def test_forward_messages_allowlist_has_one_closed_shape(self):
        self.assertIn('forwardMessages', _ALLOWED_REQUEST_TYPES)
        _validate_bounded_request(self.valid())
        _validate_bounded_request(self.valid(remove_caption=True))
        bad_options = [{**SEND_OPTIONS, 'sending_id': 0}, {**SEND_OPTIONS, 'sending_id': 2**31},
                       {**SEND_OPTIONS, 'sending_id': True}, {**SEND_OPTIONS, 'sending_id': 7, 'protect_content': True},
                       {**SEND_OPTIONS, 'sending_id': 7, 'scheduling_state': {'@type': 'messageSchedulingStateSendWhenOnline'}},
                       {**SEND_OPTIONS, 'sending_id': 7, 'paid_message_star_count': 1},
                       {**SEND_OPTIONS, 'sending_id': 7, 'disable_notification': 0},
                       {**SEND_OPTIONS, 'sending_id': 7, 'extra': 1}, dict(SEND_OPTIONS)]
        for changes in ([{'options': value} for value in bad_options] +
                        [{'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 1}}, {'message_ids': [55, 56]},
                         {'message_ids': []}, {'message_ids': [0]}, {'message_ids': [True]}, {'chat_id': 0},
                         {'from_chat_id': True}, {'send_copy': 1}, {'send_copy': False, 'remove_caption': True},
                         {'remove_caption': None}, {'extra': 1}]):
            with self.subTest(changes=repr(changes)[:80]), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(self.valid(**changes))
        missing = self.valid(); missing.pop('topic_id')
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request(missing)

    def test_forward_observation_is_correlation_only_and_exact(self):
        observations = SendObservations()
        for kwargs in ({'text_sha256': 'a' * 64}, {'reply_anchor': (RECIPIENT, 1)}, {'caption_sha256': 'a' * 64}):
            with self.assertRaises(ObservationUnavailable):
                observations.register('a', 'e', 1, RECIPIENT, 'messageText', forward=True, **kwargs)
        with self.assertRaises(ObservationUnavailable):
            observations.register('a', 'e', 1, RECIPIENT, 'messageSticker', forward=True)
        observations.register('a', 'e', 5, RECIPIENT, 'messagePhoto', forward=True)
        pending = outgoing(-10, 'messagePhoto', sending_state={'@type': 'messageSendingStatePending', 'sending_id': 5})
        for event in ({'@type': 'messages', 'total_count': 1, 'messages': [dict(pending, chat_id=1)]},
                      {'@type': 'messages', 'total_count': 2, 'messages': [pending, pending]},
                      {'@type': 'messages', 'total_count': 1, 'messages': [None]},
                      {'@type': 'messages', 'total_count': True, 'messages': [pending]},
                      dict(pending), {'@type': 'messages', 'total_count': 1, 'messages': [dict(pending, sending_state=None)]}):
            observations.reduce({**event, '@extra': 'e'})
            self.assertEqual(observations.snapshot('a').status, 'registered')
        observations.reduce({'@type': 'messages', 'total_count': 1, 'messages': [pending], '@extra': 'e'})
        self.assertEqual((observations.snapshot('a').status, observations.snapshot('a').temporary_id), ('pending', -10))
        observations.reduce({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                             'message': outgoing(9, 'messagePhoto', sending_state=None)})
        self.assertEqual((observations.snapshot('a').status, observations.snapshot('a').message_id), ('sent', 9))

    def test_registry_never_lists_cancels_or_revises_forward_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            from telegram_search_mcp.forward_drafts import ForwardSpec
            from telegram_search_mcp.outgoing_drafts import DraftError
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(CLIENT, 7); snap = snapshot()
            spec = ForwardSpec(snapshot=snap, source=ForwardSourcePreview.create(snap), mode='forward',
                               remove_caption=False, recipient_identity=('chatTypePrivate', RECIPIENT))
            draft = registry.prepare_forward(owner=owner, recipient=RECIPIENT, recipient_title='Recipient', forward=spec)
            self.assertEqual((draft.kind, draft.sha256), ('forward', snap.source.source_sha256))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            with self.assertRaises(DraftError):
                registry.cancel(draft.draft_id, owner=owner)
            with self.assertRaises(DraftError):
                registry.revise(draft.draft_id, owner=owner, recipient_title='Recipient', caption='x')
            with self.assertRaises(DraftError):
                registry.prepare_forward(owner=owner, recipient=RECIPIENT, recipient_title='Recipient', forward=object())
            with self.assertRaises(ValueError):
                ForwardSpec(snapshot=snap, source=ForwardSourcePreview.create(snap), mode='forward',
                            remove_caption=True, recipient_identity=())
            self.assertEqual(registry.peek(draft.draft_id, owner=owner), draft)


if __name__ == '__main__':
    unittest.main()
