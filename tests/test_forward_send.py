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
from telegram_search_mcp.forward_sources import VISIBLE_ENTITY_TYPES, ForwardSnapshot, ForwardSource
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
MARK = '[untrusted Telegram evidence] '
ACCOUNT = 7


def digest(value):
    # Independent expected adapter; never imports the implementation helper.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def escape(value):
    # Independent reversible escape: only ASCII letters, digits, _ and - stay literal.
    out = '"'
    for char in value:
        if char.isascii() and (char.isalnum() or char in '_-'):
            out += char
        else:
            data = char.encode('utf-16-be')
            out += ''.join('\\u%04x' % int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2))
    return out + '"'


def entity(kind, offset=0, length=1, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': kind, **fields}}


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
            'author_signature': '', 'via_bot_user_id': 0,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
            'ephemeral_message_id': 0, 'interaction_info': {'@type': 'messageInteractionInfo', 'view_count': 3},
            'content': copy.deepcopy(content) if content is not None else text_content(), **changes}


def outgoing(identifier, kind='messageText', chat_id=RECIPIENT, **changes):
    return {'@type': 'message', 'id': identifier, 'chat_id': chat_id, 'is_outgoing': True,
            'content': {'@type': kind}, **changes}


class Raw:
    def __init__(self):
        self.sent = []; self.events = deque(); self.account = ACCOUNT; self.mode = 'sent'; self.fail_receive = False
        self.source = source_message(); self.properties = dict(PROPERTIES); self.result_outgoing = True
        self.chats = {SOURCE_CHAT: chat(SOURCE_CHAT, 'Source Group'),
                      RECIPIENT: chat(RECIPIENT, 'Recipient', 'private', native=RECIPIENT),
                      ACCOUNT: chat(ACCOUNT, 'Saved Messages', 'private', native=ACCOUNT)}

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
            sending_id = request['options']['sending_id']
            if self.mode == 'foreign_pending':
                sending_id = sending_id % (2**31 - 1) + 1  # Another send's pending message in the same chat.
            target = 999 if self.mode == 'wrong_chat' else request['chat_id']
            def message(identifier, **changes):
                # Realistic provider copies: a forward carries forward_info, a copy carries none.
                value = outgoing(identifier, content_type, chat_id=target, is_outgoing=self.result_outgoing,
                                 sender_id={'@type': 'messageSenderUser', 'user_id': self.account}, **changes)
                if not request['send_copy']:
                    value['forward_info'] = {'@type': 'messageForwardInfo', 'origin': {'@type': 'messageOriginUser',
                        'sender_user_id': 8}, 'date': self.source['date'], 'source': None,
                        'public_service_announcement_type': ''}
                return value
            pending = message(-10, sending_state={'@type': 'messageSendingStatePending', 'sending_id': sending_id})
            if self.mode == 'rejected':
                self.events.append({'@type': 'error', 'code': 403, 'message': 'PRIVATE PROVIDER TEXT',
                                    '@extra': request['@extra']}); return
            self.events.append({'@type': 'updateNewMessage', 'message': copy.deepcopy(pending)})
            if self.mode == 'lost_before_result':
                self.fail_receive = True; return
            result = {'@type': 'messages', 'total_count': 1, 'messages': [copy.deepcopy(pending)]}
            if self.mode in ('malformed', 'early_malformed'):
                result = {'@type': 'messages', 'total_count': 2, 'messages': [pending, copy.deepcopy(pending)]}
            success = {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                       'message': message(701, sending_state=None)}
            if self.mode.startswith('early_'):
                self.events.append(success)  # The final update overtakes the forwardMessages result.
                if self.mode == 'early_lost':
                    self.fail_receive = True; return
                self.events.append({**result, '@extra': request['@extra']}); return
            self.events.append({**result, '@extra': request['@extra']})
            if self.mode in ('sent', 'foreign_pending'):
                self.events.append(success)
            elif self.mode == 'failed':
                self.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': -10,
                    'message': message(-10, sending_state={'@type': 'messageSendingStateFailed',
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

    def assert_refused(self, code, mode='forward', **extra):
        before = self.broker._drafts.known_ids()
        result = self.dispatch('prepare_forward_send', self.request(mode, **extra))
        self.assertEqual(result, {'status': 'refused', 'forward': None, 'refusal': code,
                                  'detail': 'source message is not eligible; no draft was created'})
        self.assertEqual(self.broker._drafts.known_ids(), before)

    def assert_unavailable(self, mode='copy'):
        result = self.dispatch('prepare_forward_send', self.request(mode))
        self.assertEqual(result, {'status': 'unavailable', 'forward': None, 'refusal': None,
                                  'detail': 'forward draft is unavailable'})

    def dialog(self, approval):
        [text] = shown(**approval)
        return text


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
        for kind, content in [('text', text_content('Hello', [entity('textEntityTypeBold', 0, 5)])), *CONTENTS.items()]:
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
                for secret in (SECRET_FILE_ID, SECRET_PATH, 'UNIQUE-', 'view_count'):
                    self.assertNotIn(secret, wire)
                expected = ForwardSource.from_message(self.raw.source, SOURCE_CHAT, 55)
                self.assertEqual(source['source_sha256'], hashlib.sha256(expected.projection_json.encode()).hexdigest())
                self.assertEqual((source['media'] is None), kind == 'text')
        self.assertEqual(self.forwards(), []); self.assertEqual(self.approvals, [])

    def test_media_identity_and_marked_sanitized_evidence(self):
        self.raw.source = source_message(photo_content('Look ‮evil‬ "here"'))
        source = self.prepare()['source']
        self.assertEqual(source['text'], MARK + r'"Look\u0020\u202eevil\u202c\u0020\u0022here\u0022"')
        self.assertEqual(source['media'], {'file_name': None, 'mime_type': None, 'title': None, 'performer': None,
            'duration_seconds': None, 'width': 1280, 'height': 960, 'start_timestamp_seconds': None,
            'has_spoiler': False, 'caption_above_media': False, 'size_bytes': 50000,
            'unique_id_sha256': hashlib.sha256(b'UNIQUE-X').hexdigest(), 'variants': 2})
        self.raw.source = source_message(CONTENTS['document'])
        source = self.prepare()['source']
        self.assertIsNone(source['text'])
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
        self.raw.source = source_message(photo_content())
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
        self.raw.source = source_message(photo_content())
        broker_result = self.dispatch('prepare_forward_send', self.request('copy', remove_caption=True))
        request = PrepareForwardSendRequest(**self.request('copy', remove_caption=True))
        def proxy(result):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=POLICY)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close); return client.prepare_forward_send(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        tampers = [('source', 'text', '[untrusted Telegram evidence] other'), ('source', 'message_id', 56),
                   ('recipient', 'chat_id', 5), ('forward', 'mode', 'forward'), ('forward', 'remove_caption', False),
                   ('forward', 'preview_sha256', 'f' * 64), ('source', 'text', 'unmarked'),
                   ('source', 'text', MARK + '"Photo caption"'), ('forward', 'account_id', ACCOUNT + 1),
                   ('source', 'is_forward', True)]
        for where, key, value in tampers:
            with self.subTest(where=where, key=key):
                result = copy.deepcopy(broker_result)
                target = result['forward'] if where == 'forward' else result['forward'][where]
                target[key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        # A coherent re-signed preview for another message/mode is still refused by scope binding.
        forged = ForwardDraftPreview.model_validate(broker_result['forward'])
        coherent = ForwardDraftPreview.create(draft_id=forged.draft_id, account_id=forged.account_id,
            expires_at=forged.expires_at, mode='copy',
            remove_caption=False, source=forged.source, recipient=forged.recipient)
        result = dict(broker_result, forward=coherent.model_dump(mode='json'))
        self.assertEqual(proxy(result).status, 'unavailable')


class SendTests(ForwardFixture, unittest.TestCase):
    def test_one_exact_closed_forward_attempt_then_receipt_replay(self):
        for mode, remove in (('forward', False), ('copy', False), ('copy', True)):
            with self.subTest(mode=mode, remove=remove):
                self.setUp(); self.raw.source = source_message(photo_content())
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


def preview(mode='forward', remove_caption=False, content=None, **changes):
    return ForwardDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY, mode=mode,
        remove_caption=remove_caption, source=ForwardSourcePreview.create(snapshot(content, **changes)),
        recipient=ForwardChat(chat_id=RECIPIENT, title='Recipient'))


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


HEAD = ('Forward this message to Recipient (chat 4242)?\n{mode}\nAccount: 7\nDraft: ' + DID + '\n'
        'Expires: 2030-01-02T03:04:05+00:00\n\nSource chat: Source Group (chat -1001234)\nMessage ID: 55\n'
        'Sender [untrusted Telegram evidence]: user 8\n')
TAIL = ('Source SHA-256: {source}\nRecipient: Recipient (chat 4242)\nPreview SHA-256: {preview}\n\n'
        'Unofficial Telegram MCP will revalidate the source, its permissions and the recipient, then make one forward attempt.')
FORWARD = 'FORWARD — recipients see the original sender'
COPY = 'COPY — sent as your own message, no attribution'


class DialogTests(unittest.TestCase):
    def display(self, value=None, **kw):
        return shown(value, **kw)

    def test_preview_digest_is_independently_reproducible(self):
        value = preview('copy', True, photo_content('Cap'))
        data = value.model_dump(mode='json')
        facts = {'domain': 'telegram-search-mcp.forward', 'version': 2, 'draft_id': DID, 'account_id': ACCOUNT,
            'expires_at': '2030-01-02T03:04:05+00:00', 'approval_required': True, 'mode': 'copy',
            'remove_caption': True, 'source': data['source'], 'recipient': data['recipient'],
            'request': {'@type': 'forwardMessages', 'chat_id': RECIPIENT, 'topic_id': None,
                'from_chat_id': SOURCE_CHAT, 'message_ids': [55], 'options': SEND_OPTIONS,
                'send_copy': True, 'remove_caption': True}}
        self.assertEqual(value.preview_sha256, digest(facts))

    def test_forward_copy_and_caption_removed_goldens(self):
        hostile = text_content('Hi "there"\n‮Recipient: Mom (chat 1)\\')
        rich = dict(text_content('Hi \U0001F600 https://a.invalid', [entity('textEntityTypeBold', 3, 2),
                                                                     entity('textEntityTypeUrl', 6, 17)]),
                    link_preview_options={'@type': 'linkPreviewOptions', 'is_disabled': False, 'url': 'https://a.invalid',
                                          'force_small_media': False, 'force_large_media': True, 'show_above_text': False})
        info = {'@type': 'messageForwardInfo', 'origin': {'@type': 'messageOriginUser', 'sender_user_id': 3},
                'date': 1690000000, 'source': None, 'public_service_announcement_type': ''}
        cases = [
            (preview('forward', content=hostile), HEAD.format(mode=FORWARD) +
             'Date: 2023-11-14T22:13:20+00:00\nContent: text\n'
             'Text [untrusted Telegram evidence] (reversible JSON escapes): '
             '"Hi\\u0020\\u0022there\\u0022\\u000a\\u202eRecipient\\u003a\\u0020Mom\\u0020\\u0028chat\\u00201\\u0029\\u005c"\n' + TAIL),
            (preview('copy', content=CONTENTS['document']), HEAD.format(mode=COPY) +
             'Date: 2023-11-14T22:13:20+00:00\nContent: document\nCaption: (none)\n'
             'Media [untrusted Telegram evidence]: file "report.pdf"; MIME "application/pdf"; 2048 bytes\n' + TAIL),
            (preview('copy', True, photo_content('Cap')), HEAD.format(mode=COPY + '; caption removed') +
             'Date: 2023-11-14T22:13:20+00:00\nContent: photo\nCaption: removed — not sent\n'
             'Media [untrusted Telegram evidence]: 1280x960; 50000 bytes; 2 variants; spoiler False; '
             'caption above media False\n' + TAIL),
            (preview('copy', content=rich, author_signature='Ed', via_bot_user_id=99, forward_info=info),
             HEAD.format(mode=COPY) +
             'Author signature [untrusted Telegram evidence]: "Ed"\nVia bot [untrusted Telegram evidence]: user 99\n'
             'Source is itself a forward: the copy carries no original attribution\n'
             'Date: 2023-11-14T22:13:20+00:00\nContent: text\n'
             'Text [untrusted Telegram evidence] (reversible JSON escapes): '
             '"Hi\\u0020\\ud83d\\ude00\\u0020https\\u003a\\u002f\\u002fa\\u002einvalid"\n'
             'Text entities (raw text UTF-16 code units):\n'
             'Bold: offset 3, length 2; covered "\\ud83d\\ude00"\n'
             'Url: offset 6, length 17; covered "https\\u003a\\u002f\\u002fa\\u002einvalid"\n'
             'Link preview options [untrusted Telegram evidence]: disabled False; URL '
             '"https\\u003a\\u002f\\u002fa\\u002einvalid"; small media False; large media True; above text False\n' + TAIL),
            (preview('forward', content=CONTENTS['audio']), HEAD.format(mode=FORWARD) +
             'Date: 2023-11-14T22:13:20+00:00\nContent: audio\nCaption: (none)\n'
             'Media [untrusted Telegram evidence]: file "song.mp3"; MIME "audio/mpeg"; title "T"; performer "P"; '
             '200 seconds; 4000000 bytes\n' + TAIL),
        ]
        for value, expected in cases:
            with self.subTest(mode=value.mode, remove=value.remove_caption, kind=value.source.content_kind):
                self.assertEqual(self.display(value), [expected.replace('{source}', value.source.source_sha256)
                                                       .replace('{preview}', value.preview_sha256)])

    def test_pinned_digests_for_golden_sources(self):
        # Independent literal: stable identity only, never file IDs, paths or raw unique IDs.
        projection = {'version': 2, 'chat_id': SOURCE_CHAT, 'message_id': 55, 'sender': {'type': 'user', 'id': 8},
            'author_signature': '', 'via_bot_user_id': 0, 'is_forward': False,
            'date': 1700000000, 'edit_date': 0, 'content': {'kind': 'photo', 'caption': {'text': 'Cap', 'entities': []},
            'has_spoiler': False, 'show_caption_above_media': False,
            'variants': [{'type': 's', 'width': 90, 'height': 60, 'size': 1000,
                          'unique_id_sha256': hashlib.sha256(b'UNIQUE-S').hexdigest()},
                         {'type': 'x', 'width': 1280, 'height': 960, 'size': 50000,
                          'unique_id_sha256': hashlib.sha256(b'UNIQUE-X').hexdigest()}]}}
        value = preview('copy', True, photo_content('Cap'))
        self.assertEqual(value.source.source_sha256, digest(projection))
        self.assertEqual(value.source.source_sha256, '3317d95b3053cbaba454d981c64cd3fc6794b76a9544bb2aed5488395d2ad64e')
        self.assertEqual(value.preview_sha256, 'f51120102abc8dd76f720678afce4135a8aabc03a381e51e7e83a31b00364c6b')

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
        for key, changed in (('text', MARK + 'Other'), ('text', MARK + '"Source text"'), ('text', 'Source text')):
            data = value.model_dump(mode='json'); data['source'][key] = changed
            self.assertEqual(self.display(value, forward_preview=data), [])
        for key, changed in (('mode', 'copy'), ('account_id', ACCOUNT + 1), ('remove_caption', True)):
            data = value.model_dump(mode='json'); data[key] = changed
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
        for kwargs in ({'forward_self': True}, {'forward_self': 1}):
            with self.assertRaises(ObservationUnavailable):
                observations.register('a', 'e', 1, RECIPIENT, 'messageText', **kwargs)
        with self.assertRaises(ObservationUnavailable):
            observations.register('a', 'e', 1, -100, 'messageText', forward=True, forward_self=True)
        pending = outgoing(-10, 'messagePhoto', sending_state={'@type': 'messageSendingStatePending', 'sending_id': 5})
        for event in ({'@type': 'messages', 'total_count': 1, 'messages': [dict(pending, chat_id=1)]},
                      {'@type': 'messages', 'total_count': 2, 'messages': [pending, pending]},
                      {'@type': 'messages', 'total_count': 1, 'messages': [None]},
                      {'@type': 'messages', 'total_count': True, 'messages': [pending]},
                      {'@type': 'messages', 'total_count': 1, 'messages': [dict(pending, is_outgoing=False)]},
                      dict(pending), {'@type': 'messages', 'total_count': 1, 'messages': [dict(pending, sending_state=None)]}):
            # A malformed or mismatched forwardMessages result is a terminal conflict, never later `sent`.
            conflicted = SendObservations()
            conflicted.register('a', 'e', 5, RECIPIENT, 'messagePhoto', forward=True)
            conflicted.reduce({**event, '@extra': 'e'})
            self.assertEqual(conflicted.snapshot('a').status, 'conflict')
            conflicted.reduce({'@type': 'updateNewMessage', 'message': pending})
            conflicted.reduce({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                               'message': outgoing(9, 'messagePhoto', sending_state=None)})
            self.assertEqual((conflicted.snapshot('a').status, conflicted.snapshot('a').message_id), ('conflict', None))
        observations.register('a', 'e', 5, RECIPIENT, 'messagePhoto', forward=True)
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


HIDDEN_ENTITIES = (
    entity('textEntityTypeTextUrl', 0, 5, url='https://evil.invalid/'),
    entity('textEntityTypeMentionName', 0, 5, user_id=9),
    entity('textEntityTypeCustomEmoji', 0, 5, custom_emoji_id='5368324170671202286'),
    entity('textEntityTypePreCode', 0, 5, language='evil'),
    entity('textEntityTypeMediaTimestamp', 0, 5, media_timestamp=30),
    entity('textEntityTypeDateTime', 0, 5, unix_time=1700000000, formatting_type=None),
    entity('textEntityTypeFutureEvil', 0, 5),
    entity('textEntityTypeBold', 0, 5, url='https://evil.invalid/'),
)


class CriticReviewTests(ForwardFixture, unittest.TestCase):
    """Amendments 16.10.2026: one test per lead decision; each failed on 34e6a16."""

    def test_h1_hidden_or_parameterized_entities_refuse_content_free(self):
        for hidden in HIDDEN_ENTITIES:
            for content in (text_content('Hello world', [hidden]),
                            dict(photo_content(), caption=formatted('Hello world', [hidden]))):
                with self.subTest(type=hidden['type']['@type'], kind=content['@type']):
                    self.raw.source = source_message(content)
                    result = self.dispatch('prepare_forward_send', self.request('copy'))
                    self.assertEqual(result, {'status': 'refused', 'forward': None, 'refusal': 'hidden_source_content',
                                              'detail': 'source message is not eligible; no draft was created'})
        self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assertEqual(self.forwards(), []); self.assertEqual(self.approvals, [])

    def test_h1_every_visible_fieldless_entity_is_admitted(self):
        rows = [entity(kind, index, 1) for index, kind in enumerate(VISIBLE_ENTITY_TYPES)]
        self.assertEqual(len(rows), 17)
        self.raw.source = source_message(text_content('abcdefghijklmnopq', rows))
        source = self.prepare('copy')['source']
        self.assertEqual(source['entities'], [{'type': kind, 'offset': index, 'length': 1}
                                              for index, kind in enumerate(VISIBLE_ENTITY_TYPES)])

    def test_h1_reply_markup_and_hidden_link_preview_url_refuse(self):
        markup = {'@type': 'replyMarkupInlineKeyboard', 'rows': [[{'@type': 'inlineKeyboardButton', 'text': 'Open',
                  'type': {'@type': 'inlineKeyboardButtonTypeUrl', 'url': 'https://evil.invalid'}}]]}
        self.raw.source = source_message(reply_markup=markup)
        self.assert_refused('hidden_source_content', 'copy')
        def options(url, **changes):
            return {'@type': 'linkPreviewOptions', 'is_disabled': False, 'url': url, 'force_small_media': False,
                    'force_large_media': True, 'show_above_text': True, **changes}
        self.raw.source = source_message(dict(text_content('see https://a.invalid'),
                                              link_preview_options=options('https://evil.invalid/')))
        self.assert_refused('hidden_source_content', 'copy')
        self.raw.source = source_message(dict(text_content('see https://a.invalid'), link_preview_options=dict(
            options(''), is_disabled=1)))
        self.assert_refused('unsupported_source_content', 'copy')
        for url, shown_url in (('https://a.invalid', MARK + escape('https://a.invalid')), ('', None)):
            with self.subTest(url=url):
                self.raw.source = source_message(dict(text_content('see https://a.invalid'), link_preview_options=options(url)))
                preview = self.prepare('copy')
                self.assertEqual(preview['source']['link_preview'], {'url': shown_url, 'is_disabled': False,
                    'force_small_media': False, 'force_large_media': True, 'show_above_text': True})
        did = preview['draft_id']
        self.raw.source['content']['link_preview_options']['show_above_text'] = False
        self.assertEqual(self.send(did)['status'], 'source_changed')
        self.assertEqual(self.forwards(), [])

    def test_h1_hidden_target_swap_after_prepare_is_source_changed(self):
        self.raw.source = source_message(text_content('Click here', [entity('textEntityTypeBold', 0, 10)]),
                                         edit_date=1700000500)
        did = self.prepare('copy')['draft_id']
        # A same-second edit keeps edit_date; the projection still binds every entity type.
        self.raw.source['content']['text']['entities'][0]['type'] = {'@type': 'textEntityTypeTextUrl',
                                                                     'url': 'https://evil.invalid/'}
        self.assertEqual(self.send(did)['status'], 'source_changed')
        self.assertEqual(self.approvals, []); self.assertEqual(self.forwards(), [])

    def test_m1_forward_of_a_forward_refuses_only_in_forward_mode(self):
        info = {'@type': 'messageForwardInfo', 'origin': {'@type': 'messageOriginHiddenUser',
                'sender_name': 'Telegram Support'}, 'date': 1690000000, 'source': None,
                'public_service_announcement_type': ''}
        self.raw.source = source_message(forward_info=info)
        self.assert_refused('source_is_forward', 'forward')
        preview = self.prepare('copy')
        self.assertIs(preview['source']['is_forward'], True)
        self.assertNotIn('Telegram Support', json.dumps(preview))
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertIn('Source is itself a forward: the copy carries no original attribution\n',
                      self.dialog(self.approvals[0]))
        self.assertIs(self.forwards()[0]['send_copy'], True)

    def test_m1_author_signature_and_via_bot_are_bound_and_displayed(self):
        self.raw.source = source_message(author_signature='Editor ‮X', via_bot_user_id=99)
        preview = self.prepare('forward')
        self.assertEqual((preview['source']['author_signature'], preview['source']['via_bot_user_id']),
                         (MARK + 'Editor X', 99))
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or False
        self.assertEqual(self.send(preview['draft_id'])['status'], 'not_approved')
        text = self.dialog(self.approvals[0])
        self.assertIn('Sender [untrusted Telegram evidence]: user 8\nAuthor signature [untrusted Telegram evidence]: '
                      '"Editor X"\nVia bot [untrusted Telegram evidence]: user 99\n', text)
        self.broker._approval_prompt = lambda **kw: True
        self.raw.source['author_signature'] = 'Someone else'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'source_changed')
        for missing in ('author_signature', 'via_bot_user_id'):
            with self.subTest(missing=missing):
                self.raw.source = source_message(); self.raw.source.pop(missing)
                self.assert_unavailable()
        self.raw.source = source_message(author_signature='​⁦')
        self.assert_refused('hidden_source_content', 'copy')
        self.assertEqual(self.forwards(), [])

    def test_m2_text_and_caption_use_reversible_escapes_with_raw_offsets(self):
        raw = 'Pay  pаypal​.com\n\t‮ok \U0001F600 end'
        units = len(raw[:raw.index('\U0001F600')].encode('utf-16-le')) // 2
        rows = [entity('textEntityTypeSpoiler', units, 2), entity('textEntityTypeBold', units + 3, 3)]
        for content in (text_content(raw, rows), dict(photo_content(), caption=formatted(raw, rows))):
            with self.subTest(kind=content['@type']):
                self.setUp(); self.raw.source = source_message(content)
                preview = self.prepare('copy')
                text = preview['source']['text']
                self.assertEqual(text, MARK + escape(raw))
                self.assertEqual(json.loads(text[len(MARK):]), raw)
                for visible in ('\\u00a0\\u0020', '\\u0430', '\\u200b', '\\u000a', '\\u0009', '\\u202e'):
                    self.assertIn(visible, text)
                self.assertEqual(preview['source']['entities'], [{'type': 'textEntityTypeSpoiler', 'offset': units,
                    'length': 2}, {'type': 'textEntityTypeBold', 'offset': units + 3, 'length': 3}])
                self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
                dialog = self.dialog(self.approvals[0])
                label = 'Text' if content['@type'] == 'messageText' else 'Caption'
                self.assertIn(f'{label} [untrusted Telegram evidence] (reversible JSON escapes): {escape(raw)}\n'
                              f'{label} entities (raw text UTF-16 code units):\n'
                              f'Spoiler: offset {units}, length 2; covered "\\ud83d\\ude00"\n'
                              f'Bold: offset {units + 3}, length 3; covered "end"\n', dialog)
                self.assertNotIn('paypal', dialog)
        self.raw.source = source_message(text_content(raw, [entity('textEntityTypeBold', units + 1, 1)]))
        self.assert_refused('unsupported_source_content', 'copy')  # Split surrogate pair.

    def test_m3_reprepare_supersedes_only_the_same_owner_and_key(self):
        first = self.prepare('forward')['draft_id']
        other_mode = self.prepare('copy')['draft_id']
        foreign = self.dispatch('prepare_forward_send', self.request('forward'), client=FOREIGN)['forward']['draft_id']
        second = self.prepare('forward')['draft_id']
        self.assertEqual(self.send(first)['status'], 'expired')
        self.assertEqual(self.status(first)['evidence'], 'none')
        self.assertEqual((self.forwards(), self.approvals), ([], []))
        self.assertEqual(self.send(second)['status'], 'sent')
        self.assertEqual(self.send(other_mode)['status'], 'sent')
        self.assertEqual(self.send(foreign, client=FOREIGN)['status'], 'sent')
        self.assertEqual(len(self.forwards()), 3)

    async def _descriptions(self):
        from mcp import Client
        from telegram_search_mcp.server import build_server
        class Service:
            def close(self): pass
        async with Client(build_server(service_factory=Service, policy=POLICY)) as consumer:
            return {t.name: t.description for t in (await consumer.list_tools()).tools}

    def test_m3_tool_description_states_supersede_and_no_cancel(self):
        import asyncio
        description = asyncio.run(self._descriptions())['prepare_forward_send']
        for phrase in ('supersedes the earlier pending forward draft, which can then no longer be sent',
                       'cannot be listed, updated, refreshed or cancelled through the generic draft tools',
                       'reversible JSON escapes', 'itself a forward', 'auto-deleting'):
            self.assertIn(phrase, description)

    def test_l1_saved_messages_forward_accepts_the_provider_incoming_flag(self):
        self.raw.result_outgoing = False
        did = self.prepare('forward', recipient=ACCOUNT)['draft_id']
        result = self.send(did)
        self.assertEqual((result['status'], result['message_id'], result['recipient']), ('sent', 701, ACCOUNT))
        self.assertEqual(self.status(did)['status'], 'sent')
        self.setUp(); self.raw.result_outgoing = False
        did = self.prepare('forward')['draft_id']  # The same incoming flag elsewhere is not our message.
        result = self.send(did)
        self.assertEqual((result['status'], result['message_id']), ('outcome_unknown', None))
        self.assertEqual(self.status(did)['status'], 'outcome_unknown')
        self.assertEqual(len(self.forwards()), 1)

    def test_l2_early_success_waits_for_and_validates_the_result(self):
        for mode, expected in (('early_valid', 'sent'), ('early_malformed', 'outcome_unknown'),
                               ('early_lost', 'outcome_unknown')):
            with self.subTest(mode=mode):
                self.setUp(); self.raw.mode = mode
                did = self.prepare()['draft_id']
                result = self.send(did)
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['message_id'], 701 if expected == 'sent' else None)
                if mode == 'early_malformed':
                    self.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                                            'message': outgoing(701, sending_state=None)})
                    self.assertEqual(self.status(did)['status'], 'outcome_unknown')
                self.assertEqual(len(self.forwards()), 1)

    def test_l2_malformed_or_mismatched_result_never_reconciles_to_sent(self):
        for mode in ('malformed', 'wrong_chat', 'foreign_pending'):
            with self.subTest(mode=mode):
                self.setUp(); self.raw.mode = mode
                did = self.prepare()['draft_id']
                self.assertEqual(self.send(did)['status'], 'outcome_unknown')
                self.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                                        'message': outgoing(702, sending_state=None)})
                status = self.status(did)
                self.assertEqual((status['status'], status['message_id']), ('outcome_unknown', None))
                self.assertEqual(self.send(did)['status'], 'outcome_unknown')
                self.assertEqual(len(self.forwards()), 1)

    def test_l3_audio_names_and_media_presentation_flags_are_bound(self):
        self.raw.source = source_message(CONTENTS['audio'])
        preview = self.prepare('copy')
        self.assertEqual((preview['source']['media']['title'], preview['source']['media']['performer']),
                         (MARK + 'T', MARK + 'P'))
        self.raw.source['content']['audio']['title'] = 'OFFENSIVE TITLE'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'source_changed')
        self.setUp()
        self.raw.source = source_message(photo_content(has_spoiler=True, show_caption_above_media=True))
        preview = self.prepare('copy')
        self.assertEqual((preview['source']['media']['has_spoiler'], preview['source']['media']['caption_above_media']),
                         (True, True))
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or False
        self.send(preview['draft_id'])
        self.assertIn('; spoiler True; caption above media True\n', self.dialog(self.approvals[0]))
        self.broker._approval_prompt = lambda **kw: True
        self.raw.source['content']['has_spoiler'] = False
        self.assertEqual(self.send(preview['draft_id'])['status'], 'source_changed')
        self.raw.source = source_message(dict(CONTENTS['video'], start_timestamp=5))
        preview = self.prepare('copy')
        self.assertEqual(preview['source']['media']['start_timestamp_seconds'], 5)
        self.broker._approval_prompt = lambda **kw: self.approvals.append(kw) or False
        self.send(preview['draft_id'])
        self.assertIn('file "clip.mp4"; MIME "video/mp4"; 12 seconds; 640x360; starts at 5 seconds; 99999 bytes; '
                      'spoiler False; caption above media False\n', self.dialog(self.approvals[-1]))
        for content in (dict(CONTENTS['video'], start_timestamp=None), dict(photo_content(), has_spoiler=None),
                        dict(CONTENTS['audio'], audio=dict(CONTENTS['audio']['audio'], performer=None))):
            self.raw.source = source_message(content)
            self.assert_refused('unsupported_source_content', 'copy')
        self.assertEqual(self.forwards(), [])

    def test_l4_auto_deleting_source_refuses(self):
        self.raw.source = source_message(auto_delete_in=30.0)
        self.assert_refused('unsupported_source_state', 'copy')
        self.raw.source = source_message(); self.raw.source.pop('auto_delete_in')
        self.assert_unavailable()
        self.raw.source = source_message()
        did = self.prepare('copy')['draft_id']
        self.raw.source['auto_delete_in'] = 86400.0
        self.assertEqual(self.send(did)['status'], 'source_changed')
        self.assertEqual(self.forwards(), [])

    def test_l5_remove_caption_requires_a_media_source_and_dialog_marks_it_removed(self):
        self.assert_refused('invalid_input', 'copy', remove_caption=True)
        self.raw.source = source_message(photo_content('Secret caption'))
        preview = self.prepare('copy', remove_caption=True)
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        dialog = self.dialog(self.approvals[0])
        self.assertIn('\nCaption: removed — not sent\n', dialog)
        self.assertNotIn('Secret', dialog); self.assertNotIn(escape('Secret caption'), dialog)
        self.assertIs(self.forwards()[0]['remove_caption'], True)

    def test_info_dialog_identifies_account_draft_and_expiry(self):
        preview = self.prepare('copy')
        self.assertEqual(preview['account_id'], ACCOUNT)
        self.send(preview['draft_id'])
        self.assertIn(f"\nAccount: {ACCOUNT}\nDraft: {preview['draft_id']}\n"
                      f"Expires: {datetime.fromisoformat(preview['expires_at']).isoformat()}\n\n",
                      self.dialog(self.approvals[0]))

    def test_info_local_failure_replay_says_no_provider_attempt(self):
        did = self.prepare()['draft_id']
        def approve(**kw):
            self.raw.source['edit_date'] = 1700000100; return True
        self.broker._approval_prompt = approve
        self.assertEqual(self.send(did)['status'], 'local_failed')
        replay = self.send(did)
        self.assertEqual(replay['status'], 'local_failed')
        self.assertIn('no provider attempt was made', replay['detail'])
        self.assertNotIn('previous provider attempt', replay['detail'])
        self.assertEqual(self.forwards(), [])

    def test_foreign_sending_id_with_matching_chat_and_type_is_not_matched(self):
        self.raw.mode = 'foreign_pending'
        did = self.prepare()['draft_id']
        result = self.send(did)
        self.assertEqual((result['status'], result['message_id']), ('outcome_unknown', None))
        self.assertNotEqual(self.status(did)['status'], 'sent')

    def test_guard_failure_after_registration_is_local_failed_without_transport(self):
        did = self.prepare()['draft_id']
        observations = self.client._send_observations
        register = observations.register
        def flip(*args, **kwargs):
            register(*args, **kwargs)
            self.broker._policy = RuntimePolicy(enabled_capabilities=('send',))
        observations.register = flip
        result = self.broker._send_prepared_forward(SendPreparedForwardRequest(draft_id=did, approved=True),
                                                    client_id=CLIENT)
        self.broker._policy = POLICY
        self.assertEqual(result.status, 'local_failed'); self.assertEqual(self.forwards(), [])
        self.assertIsNone(observations.snapshot(did))
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('failed', 'local_failed'))

    def test_realistic_forward_results_with_forward_info_confirm(self):
        for mode in ('forward', 'copy'):
            with self.subTest(mode=mode):
                self.setUp()
                self.assertEqual(self.send(self.prepare(mode)['draft_id'])['status'], 'sent')
                self.assertEqual(len(self.forwards()), 1)


if __name__ == '__main__':
    unittest.main()
