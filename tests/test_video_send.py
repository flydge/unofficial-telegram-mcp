"""F15a: one owner-approved native video with probed metadata, revalidation and a receipt.

Synthetic ISO-BMFF bytes and a synthetic raw transport only: the probe, drafts, broker,
proxy, staging and TDLib reducers are production code. No live TDLib or owner dialog.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest

from telegram_search_mcp import broker_client as broker_client_module
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

import bmff_fixtures as bmff
from telegram_search_mcp.approval_prompt import confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS, BrokerProtocolError
from telegram_search_mcp.config import KNOWN_CAPABILITIES, LEGACY_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS, CompatibilityError
from telegram_search_mcp.forward_drafts import READABLE_RULE, readable
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry, _safe_display_name
from telegram_search_mcp.outgoing_stage import retire_staged_document, stage_approved_document
from telegram_search_mcp.send_observations import ObservationUnavailable, SendObservations
from telegram_search_mcp.send_status_models import GetSendStatusRequest
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, MessageSendOutcomeUnknown,
                                        TDLibClient, _validate_bounded_request)
from telegram_search_mcp.video_drafts import (PrepareVideoSendRequest, PrepareVideoSendResponse,
    SendPreparedVideoRequest, SendPreparedVideoResponse, VideoDraftPreview, VideoRecipient, VideoSpec, UNAVAILABLE)
from telegram_search_mcp.video_probe import VideoFacts, probe_video

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
RECIPIENT = 4242
ACCOUNT = 7
POLICY = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send'))
SEND_OPTIONS = {'@type': 'messageSendOptions', 'suggested_post_info': None, 'disable_notification': False,
    'from_background': False, 'protect_content': False, 'allow_paid_broadcast': False,
    'paid_message_star_count': 0, 'update_order_of_installed_sticker_sets': False,
    'scheduling_state': None, 'effect_id': 0, 'only_preview': False}
MARK = '[untrusted Telegram evidence] '
DID = 'draft_' + '1' * 32
EXPIRY = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
MOVIE = bmff.movie()  # ftyp + moov(mvhd 5 s, one avc1 1280x720 track) + mdat: faststart


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


def chat(chat_id, title, kind='private', native=None):
    types = {'private': {'@type': 'chatTypePrivate', 'user_id': native or chat_id},
             'secret': {'@type': 'chatTypeSecret', 'secret_chat_id': 9, 'user_id': native or chat_id}}
    return {'@type': 'chat', 'id': chat_id, 'title': title, 'type': types[kind], 'has_protected_content': False}


def file(size):
    return {'@type': 'file', 'id': 15, 'size': size, 'expected_size': size,
        'local': {'@type': 'localFile', 'path': '/private/stage', 'can_be_downloaded': True, 'can_be_deleted': True,
                  'is_downloading_active': False, 'is_downloading_completed': True, 'download_offset': 0,
                  'downloaded_prefix_size': size, 'downloaded_size': size},
        'remote': {'@type': 'remoteFile', 'id': 'REMOTE', 'unique_id': 'UNIQUE', 'is_uploading_active': False,
                   'is_uploading_completed': True, 'uploaded_size': size}}


def video_content(caption, duration, width, height, size, streaming):
    return {'@type': 'messageVideo', 'video': {'@type': 'video', 'duration': duration, 'width': width,
        'height': height, 'file_name': 'clip.mp4', 'mime_type': 'video/mp4', 'has_stickers': False,
        'supports_streaming': streaming, 'minithumbnail': None, 'thumbnail': None, 'video': file(size)},
        'alternative_videos': [], 'storyboards': [], 'cover': None, 'start_timestamp': 0,
        'caption': {'@type': 'formattedText', 'text': caption, 'entities': []},
        'show_caption_above_media': False, 'has_spoiler': False, 'is_secret': False}


def outgoing(identifier, content, chat_id=RECIPIENT, **changes):
    return {'@type': 'message', 'id': identifier, 'chat_id': chat_id, 'is_outgoing': True,
            'content': copy.deepcopy(content), **changes}


class Raw:
    def __init__(self):
        self.sent = []; self.events = deque(); self.account = ACCOUNT; self.mode = 'sent'; self.fail_receive = False
        self.final = {}  # Overrides applied to the final video object.
        self.chats = {RECIPIENT: chat(RECIPIENT, 'Recipient'), ACCOUNT: chat(ACCOUNT, 'Saved Messages')}

    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind = request['@type']
        if kind == 'getAuthorizationState':
            value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            value = copy.deepcopy(self.chats.get(request['chat_id'], {'@type': 'error', 'code': 400, 'message': 'x'}))
        elif kind == 'sendMessage':
            sent = request['input_message_content']; video = sent['video']
            size = Path(video['video']['path']).stat().st_size
            content = video_content(sent['caption']['text'], video['duration'], video['width'], video['height'],
                                    size, video['supports_streaming'])
            target = request['chat_id']
            pending = outgoing(-10, content, chat_id=target, sending_state={'@type': 'messageSendingStatePending',
                                                                            'sending_id': request['options']['sending_id']})
            if self.mode == 'rejected':
                self.events.append({'@type': 'error', 'code': 400, 'message': 'PRIVATE PROVIDER TEXT',
                                    '@extra': request['@extra']}); return
            self.events.append({**pending, '@extra': request['@extra']})
            if self.mode in ('lost', 'silent'):
                self.fail_receive = self.mode == 'lost'; return
            if self.mode == 'failed':
                self.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': -10,
                    'message': outgoing(-10, content, chat_id=target, sending_state={'@type': 'messageSendingStateFailed',
                                                                                     'error': {'@type': 'error', 'code': 403}}),
                    'error': {'@type': 'error', 'code': 403, 'message': 'PRIVATE'}}); return
            final = outgoing(701, content, chat_id=target, sending_state=None)
            final['content']['video'].update(self.final)
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': final})
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


class VideoFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.raw = Raw(); self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=self.root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=self.root / 'cache'),
            client_factory=lambda: self.client, policy=POLICY, approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)
        stage = self.root / 'stage'
        for target, replacement in (
                ('telegram_search_mcp.tdjson.STAGING_ROOT', stage),
                ('telegram_search_mcp.broker.stage_approved_document', lambda claim: stage_approved_document(claim, root=stage)),
                ('telegram_search_mcp.broker.retire_staged_document', lambda path: retire_staged_document(path, root=stage))):
            manager = patch(target, replacement); manager.start(); self.addCleanup(manager.stop)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def upload(self, data=MOVIE, name='clip.mp4', client=CLIENT):
        """The production local-artifact path: owner grant plus recorded file name."""
        result = self.dispatch('create_local_artifact', {'file_name': name,
            'content_base64': base64.b64encode(data).decode()}, client)
        self.assertEqual(result['status'], 'complete', result)
        return result['artifact_id']

    def store(self, data=MOVIE, name='clip.mp4'):
        """A granted artifact whose recorded file name came from Telegram (untrusted)."""
        path = self.root / 'source.bin'; path.write_bytes(data)
        with self.broker._artifact_authority.scope(CLIENT, self.broker._artifact_store):
            artifact = self.broker._artifact_store.store(path, kind='media')
        self.broker._artifact_metadata[artifact.artifact_id] = (None, name, 'video/mp4', 'video')
        return artifact.artifact_id

    def request(self, artifact_id, **extra):
        return {'artifact_id': artifact_id, 'recipient': RECIPIENT, **extra}

    def prepare(self, artifact_id=None, client=CLIENT, **extra):
        result = self.dispatch('prepare_video_send', self.request(artifact_id or self.upload(), **extra), client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['video']

    def send(self, did, approved=True, client=CLIENT):
        return self.dispatch('send_prepared_video', {'draft_id': did, 'approved': approved}, client)

    def status(self, did, client=CLIENT):
        return self.dispatch('get_send_status', {'draft_id': did}, client)

    def sends(self):
        return [x for x in self.raw.sent if x['@type'] == 'sendMessage']

    def assert_refused(self, artifact_id, code):
        before = self.broker._drafts.known_ids()
        result = self.dispatch('prepare_video_send', self.request(artifact_id))
        self.assertEqual(result, {'status': 'refused', 'video': None, 'refusal': code,
                                  'detail': 'artifact is not an eligible native video; no draft was created'})
        self.assertEqual(self.broker._drafts.known_ids(), before)

    def assert_unavailable(self, payload, client=CLIENT):
        before = self.broker._drafts.known_ids()
        self.assertEqual(self.dispatch('prepare_video_send', payload, client),
                         {'status': 'unavailable', 'video': None, 'refusal': None, 'detail': UNAVAILABLE})
        self.assertEqual(self.broker._drafts.known_ids(), before)

    def artifact_file(self, artifact_id):
        return self.broker._artifact_store.cache_dir / artifact_id


def preview(name='clip.mp4', caption='', facts=None, **changes):
    values = dict(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
        recipient=VideoRecipient(chat_id=RECIPIENT, title='Recipient'),
        artifact_id='artifact_' + 'c' * 32 + '_' + hashlib.sha256(MOVIE).hexdigest() + f'_{len(MOVIE)}',
        display_name=name, size_bytes=len(MOVIE), sha256=hashlib.sha256(MOVIE).hexdigest(),
        facts=facts or VideoFacts(5, 1280, 720, 'h264', False, True), caption=caption)
    values.update(changes)
    return VideoDraftPreview.create(**values)


def shown(value=None, **kw):
    """Capture the exact owner-dialog bytes; the DENIED mock has no owner side effect."""
    seen = []
    arguments = dict(recipient_title='Recipient', recipient=RECIPIENT,
        display_name=value.raw_display_name if value is not None else 'clip.mp4',
        size_bytes=value.size_bytes if value is not None else len(MOVIE),
        sha256=value.sha256 if value is not None else 'a' * 64,
        caption=value.raw_caption if value is not None else '', kind='video',
        video_preview=value.model_dump(mode='json') if value is not None else None)
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv[-1]) or SimpleNamespace(returncode=0, stdout=b'DENIED')):
        assert confirm_approved_send(**arguments) is False
    return seen


class CapabilityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Criteria 1 and 2: registration everywhere, default off, send alone insufficient."""

    def test_default_off_capability_is_registered_everywhere(self):
        self.assertIn('video_send', KNOWN_CAPABILITIES)
        self.assertNotIn('video_send', LEGACY_CAPABILITIES)
        self.assertNotIn('video_send', RuntimePolicy().enabled_capabilities)
        for name in ('prepare_video_send', 'send_prepared_video'):
            self.assertEqual(OPERATION_CAPABILITIES[name], 'video_send')
            self.assertEqual(TOOL_OPERATIONS[name], name)
            self.assertIn(name, OPERATIONS)

    async def test_sdk_lists_closed_tools_but_refuses_without_video_send_and_send(self):
        from mcp import Client
        from mcp.shared.exceptions import MCPError
        from telegram_search_mcp.server import build_server
        good = {'artifact_id': 'artifact_' + 'c' * 32 + '_' + 'd' * 64 + '_10', 'recipient': RECIPIENT}
        for policy, enabled in ((RuntimePolicy(), False), (RuntimePolicy(enabled_capabilities=('send',)), False),
                                (RuntimePolicy(enabled_capabilities=('video_send',)), False), (POLICY, True)):
            calls = []
            class Service:
                def close(self): pass
                def prepare_video_send(self, request):
                    calls.append(request); return PrepareVideoSendResponse(status='unavailable', detail=UNAVAILABLE)
                def send_prepared_video(self, request):
                    calls.append(request)
                    return SendPreparedVideoResponse(status='not_approved', draft_id=request.draft_id, detail='synthetic')
            async with Client(build_server(service_factory=Service, policy=policy)) as consumer:
                tools = {t.name: t for t in (await consumer.list_tools()).tools}
                self.assertEqual(len(tools), 55)
                self.assertEqual(list(tools)[-6:], ['prepare_video_send', 'send_prepared_video', 'prepare_chat_archive',
                                                    'apply_prepared_archive', 'prepare_text_edit', 'apply_prepared_text_edit'])
                self.assertEqual(set(tools['prepare_video_send'].input_schema['properties']),
                                 {'artifact_id', 'recipient', 'caption'})
                self.assertEqual(set(tools['send_prepared_video'].input_schema['properties']), {'draft_id', 'approved'})
                for name in ('prepare_video_send', 'send_prepared_video'):
                    self.assertFalse(tools[name].input_schema['additionalProperties'])
                    self.assertFalse(tools[name].annotations.read_only_hint)
                result = await consumer.call_tool('prepare_video_send', good)
                sent = await consumer.call_tool('send_prepared_video', {'draft_id': DID, 'approved': True})
                if not enabled:
                    self.assertTrue(result.is_error); self.assertTrue(sent.is_error); self.assertEqual(calls, [])
                    continue
                self.assertEqual(result.structured_content['status'], 'unavailable')
                self.assertEqual(sent.structured_content['status'], 'not_approved')
                self.assertEqual(calls[0], PrepareVideoSendRequest(**good)); calls.clear()
                for bad in ({'artifact_id': 'artifact_bad'}, {'recipient': 0}, {'recipient': True},
                            {'recipient': '4242'}, {'caption': 'x' * 1025}, {'caption': 1}, {'selection_id': 'x'},
                            {'display_name': 'clip.mp4'}, {'thumbnail': {}}, {'has_spoiler': True}, {'topic_id': 1}):
                    try:
                        failed = await consumer.call_tool('prepare_video_send', good | bad)
                    except MCPError:
                        continue
                    self.assertTrue(failed.is_error, bad)
                for bad in ({'approved': 'true'}, {'approved': 1}, {'draft_id': 'draft_bad'}, {'extra': 1}):
                    try:
                        failed = await consumer.call_tool('send_prepared_video', {'draft_id': DID, 'approved': True} | bad)
                    except MCPError:
                        continue
                    self.assertTrue(failed.is_error, bad)
                self.assertEqual(calls, [])

    def test_proxy_refuses_before_connecting_unless_both_capabilities(self):
        for policy, enabled in ((RuntimePolicy(), False), (RuntimePolicy(enabled_capabilities=('send',)), False),
                                (RuntimePolicy(enabled_capabilities=('video_send',)), False), (POLICY, True)):
            connects = []
            def connector(path):
                connects.append(path); raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            prepared = proxy.prepare_video_send(PrepareVideoSendRequest(
                artifact_id='artifact_' + 'c' * 32 + '_' + 'd' * 64 + '_10', recipient=RECIPIENT))
            sent = proxy.send_prepared_video(SendPreparedVideoRequest(draft_id=DID, approved=True))
            self.assertEqual(prepared.status, 'unavailable')
            if enabled:
                self.assertEqual(len(connects), 2 * (1 + int(broker_client_module.BROKER_STARTUP_WAIT_SECONDS / 0.25))); self.assertEqual(sent.status, 'outcome_unknown')
            else:
                self.assertEqual(connects, []); self.assertEqual(sent.status, 'error')
                self.assertIn('capability_disabled', sent.detail)

    def test_broker_refuses_before_provider_or_artifact_unless_both_capabilities(self):
        for policy in (RuntimePolicy(), RuntimePolicy(enabled_capabilities=('send', 'artifacts')),
                       RuntimePolicy(enabled_capabilities=('video_send', 'artifacts'))):
            with tempfile.TemporaryDirectory() as directory:
                opened = []
                broker = Broker(socket_path=Path(directory) / 'broker.sock',
                    artifact_store=ArtifactStore(cache_dir=Path(directory) / 'cache'),
                    client_factory=lambda: opened.append(True), policy=policy, verify_peer_uid=False)
                self.addCleanup(broker.shutdown)
                for op, payload in (('prepare_video_send', {'artifact_id': 'artifact_' + 'c' * 32 + '_' + 'd' * 64 + '_10',
                                                            'recipient': RECIPIENT}),
                                    ('send_prepared_video', {'draft_id': DID, 'approved': True})):
                    with self.subTest(policy=policy.enabled_capabilities, op=op), self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation': op, 'payload': payload, 'client_id': CLIENT,
                            'deadline': time.monotonic() + 10, 'broker_generation': broker._generation})
                self.assertEqual(opened, [])


class PrepareTests(VideoFixture, unittest.TestCase):
    def test_prepare_binds_probed_facts_bytes_and_marked_escaped_evidence(self):
        artifact_id = self.upload()
        video = self.prepare(artifact_id, caption='Hello\nworld')
        self.assertEqual({key: video[key] for key in ('account_id', 'artifact_id', 'size_bytes', 'sha256',
                          'duration_seconds', 'width', 'height', 'codec', 'has_audio', 'supports_streaming',
                          'approval_required')},
                         {'account_id': ACCOUNT, 'artifact_id': artifact_id, 'size_bytes': len(MOVIE),
                          'sha256': hashlib.sha256(MOVIE).hexdigest(), 'duration_seconds': 5, 'width': 1280,
                          'height': 720, 'codec': 'h264', 'has_audio': False, 'supports_streaming': True,
                          'approval_required': True})
        self.assertEqual(video['recipient'], {'chat_id': RECIPIENT, 'title': 'Recipient'})
        self.assertEqual(video['display_name'], MARK + escape('clip.mp4'))
        self.assertEqual(video['caption'], MARK + escape('Hello\nworld'))
        self.assertEqual(json.loads(video['caption'][len(MARK):]), 'Hello\nworld')
        self.assertEqual(self.sends(), []); self.assertEqual(self.approvals, [])
        self.assertIsNone(self.prepare(artifact_id)['caption'])

    def test_hevc_mov_rotation_audio_and_non_faststart_layout_flow_into_the_preview(self):
        data = bmff.movie(bmff.mvhd(600, 1201), bmff.video(b'hvc1', width=1920, height=1080, rotation=90),
                          bmff.audio(b'mp4a'), faststart=False, brand=(b'qt  ', (b'qt  ',)))
        video = self.prepare(self.upload(data, 'turned.mov'))
        self.assertEqual((video['codec'], video['width'], video['height'], video['duration_seconds'],
                          video['has_audio'], video['supports_streaming']), ('hevc', 1080, 1920, 3, True, False))
        self.assertEqual(self.broker._drafts.peek(video['draft_id'], owner=DraftOwner(CLIENT, ACCOUNT)).mime_type,
                         'video/quicktime')

    def test_unsupported_and_oversized_artifacts_refuse_without_draft(self):
        cases = [bmff.ftyp() + bmff.moov(bmff.mvhd(), bmff.video(), bmff.box(b'mvex')) + bmff.mdat(),  # fragmented
                 bmff.movie(bmff.mvhd(), bmff.audio()),                                               # no video
                 bmff.movie(bmff.mvhd(), bmff.video(), bmff.video()),                                  # two video tracks
                 bmff.movie(bmff.mvhd(), bmff.video(b'vp09')),                                         # unknown codec
                 bmff.movie(bmff.mvhd(), bmff.video(width=8, height=8)),                               # bad size
                 MOVIE[:-10],                                                                          # truncated
                 b'plain text, not a movie']
        for data in cases:
            with self.subTest(data=data[:24]):
                self.assert_refused(self.upload(data), 'unsupported_video')
        artifact_id = self.upload()
        with patch('telegram_search_mcp.broker.MAX_VIDEO_BYTES', len(MOVIE) - 1):
            self.assert_refused(artifact_id, 'video_too_large')
        self.assertEqual(self.sends(), []); self.assertEqual(self.approvals, [])

    def test_more_than_64_mib_refuses_as_too_large_before_probing(self):
        path = self.root / 'big.bin'
        with path.open('wb') as out:
            out.write(MOVIE); out.truncate(64 * 1024 * 1024 + 1)
        with self.broker._artifact_authority.scope(CLIENT, self.broker._artifact_store):
            artifact = self.broker._artifact_store.store(path, kind='media')
        with patch('telegram_search_mcp.broker.probe_video', side_effect=AssertionError('probed')):
            self.assert_refused(artifact.artifact_id, 'video_too_large')

    def test_secret_missing_foreign_and_unreadable_recipients_are_unavailable(self):
        artifact_id = self.upload()
        self.raw.chats[RECIPIENT] = chat(RECIPIENT, 'Secret', 'secret')
        self.assert_unavailable(self.request(artifact_id))
        self.raw.chats.pop(RECIPIENT)
        self.assert_unavailable(self.request(artifact_id))
        self.raw.chats[RECIPIENT] = chat(RECIPIENT, 'Recipient')
        self.assert_unavailable(self.request(artifact_id), client=FOREIGN)  # No grant for another client.
        self.assert_unavailable(self.request('artifact_' + 'c' * 32 + '_' + 'd' * 64 + '_10'))
        with self.assertRaises(ValidationError):
            self.dispatch('prepare_video_send', self.request(artifact_id, caption='x' * 1025))
        with self.assertRaises(ValidationError):
            self.dispatch('prepare_video_send', self.request(artifact_id, display_name='x.mp4'))
        # A selection token on a video operation is one fixed malformed-request refusal under
        # every policy; it never reaches the selection, artifact or provider path.
        token = 'selection_' + 'a' * 64
        before = len(self.raw.sent)
        for policy in (POLICY, RuntimePolicy(enabled_capabilities=(*POLICY.enabled_capabilities, 'retained_selections'))):
            self.broker._policy = policy
            for op, payload in (('prepare_video_send', self.request(artifact_id, selection_id=token)),
                                ('prepare_video_send', self.request(artifact_id, reply_selection_id=token)),
                                ('send_prepared_video', {'draft_id': DID, 'approved': True, 'selection_id': token})):
                with self.subTest(policy=policy.enabled_capabilities, op=op, keys=sorted(payload)):
                    with self.assertRaises(BrokerProtocolError) as caught:
                        self.dispatch(op, payload)
                    self.assertEqual(Broker._error_code(caught.exception), 'invalid_request')
        self.broker._policy = POLICY
        self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assertEqual(len(self.raw.sent), before)

    def test_untrusted_file_name_and_caption_are_normalized_then_escaped(self):
        artifact_id = self.store(name='../p\u0430ypal\u202e4pm.exe\u200b.mp4')
        video = self.prepare(artifact_id, caption='Pay \u202eNOW\u200b\r\n\tok \U0001F600 \ufb01')
        self.assertEqual(video['display_name'], MARK + escape('p\u0430ypal4pm.exe.mp4'))
        self.assertEqual(video['caption'], MARK + escape('Pay NOW\n\tok \U0001F600 fi'))
        for raw in (video['display_name'], video['caption']):
            self.assertTrue(raw[len(MARK):].isascii())

    def test_preview_refusal_leaves_no_orphan_and_supersedes_nothing(self):
        # L1: the preview is built and validated before the draft is inserted.
        artifact_id = self.upload()
        first = self.prepare(artifact_id)['draft_id']
        with patch('telegram_search_mcp.broker.VideoDraftPreview.create', side_effect=ValueError('synthetic refusal')):
            self.assert_unavailable(self.request(artifact_id, caption='changed'))
        self.assertEqual(self.broker._drafts.known_ids(), {first})
        self.assertEqual(self.broker._drafts._entries[first].state, 'pending')
        self.assertEqual(self.send(first)['status'], 'sent')

    def test_compatibility_solidus_is_normalized_before_the_path_split(self):
        # L1: NFKC first, so U+FF0F / U+FF3C / U+FE68 cannot turn into a separator after the split.
        for name in ('a\uff0fb.mp4', 'dir\uff3cb.mp4', 'x/y\uff0fb.mp4', 'a\ufe68b.mp4'):
            with self.subTest(name=name):
                self.assertEqual(_safe_display_name(name), 'b.mp4')
                before = self.broker._drafts.known_ids()
                video = self.prepare(self.store(name=name))
                self.assertEqual(video['display_name'], MARK + escape('b.mp4'))
                self.assertEqual(self.broker._drafts.known_ids() - before, {video['draft_id']})
        # The shared helper also fixes document drafts, which used to keep 'a/b.txt' and fail at staging.
        document = self.dispatch('prepare_artifact_send', {'artifact_id': self.upload(), 'recipient': RECIPIENT,
            'display_name': 'a\uff0fb.txt', 'mime_type': 'text/plain'})
        self.assertEqual((document['status'], document['display_name']), ('prepared', 'b.txt'))

    def test_generic_draft_tools_and_other_send_tools_never_touch_video_drafts(self):
        did = self.prepare()['draft_id']
        listed = self.dispatch('list_drafts', {})
        self.assertEqual((listed['status'], listed['drafts']), ('listed', []))
        for op, extra in (('get_draft', {}), ('update_draft', {'caption': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_artifact_draft', {})):
            with self.subTest(op=op):
                self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send',
                                                                          'reply_artifact_send'))
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'video_send', 'forward_send'))
        for op in ('send_prepared_text', 'send_prepared_artifact', 'send_prepared_forward'):
            self.assertEqual(self.dispatch(op, {'draft_id': did, 'approved': True})['status'], 'error')
        self.assertEqual((self.sends(), self.approvals), ([], []))
        self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_send_prepared_video_refuses_other_draft_kinds(self):
        artifact_id = self.upload()
        document = self.dispatch('prepare_artifact_send', {'artifact_id': artifact_id, 'recipient': RECIPIENT,
            'display_name': 'clip.mp4', 'mime_type': 'video/mp4'})
        self.assertEqual(document['status'], 'prepared')
        result = self.send(document['draft_id'])
        self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match send tool'))
        self.assertEqual((self.sends(), self.approvals), ([], []))

    def test_reprepare_supersedes_only_the_same_owner_artifact_and_recipient(self):
        artifact_id = self.upload()
        first = self.prepare(artifact_id)['draft_id']
        self.raw.chats[ACCOUNT] = chat(ACCOUNT, 'Saved Messages')
        other_recipient = self.dispatch('prepare_video_send', {'artifact_id': artifact_id, 'recipient': ACCOUNT})
        foreign_artifact = self.upload(client=FOREIGN)
        foreign = self.prepare(foreign_artifact, client=FOREIGN)['draft_id']
        second = self.prepare(artifact_id, caption='changed')['draft_id']
        self.assertEqual(self.send(first)['status'], 'expired')
        self.assertEqual(self.status(first)['evidence'], 'none')
        self.assertEqual((self.sends(), self.approvals), ([], []))
        self.assertEqual(self.send(second)['status'], 'sent')
        self.assertEqual(self.send(other_recipient['video']['draft_id'])['status'], 'sent')
        self.assertEqual(self.send(foreign, client=FOREIGN)['status'], 'sent')
        self.assertEqual(len(self.sends()), 3)

    def test_proxy_validates_preview_digest_and_request_scope(self):
        artifact_id = self.upload()
        broker_result = self.dispatch('prepare_video_send', self.request(artifact_id, caption='Cap'))
        request = PrepareVideoSendRequest(**self.request(artifact_id, caption='Cap'))
        def proxy(result, scoped=request):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=POLICY)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close); return client.prepare_video_send(scoped)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        tampers = [('width', 1281), ('duration_seconds', 6), ('codec', 'hevc'), ('has_audio', True),
                   ('supports_streaming', False), ('size_bytes', len(MOVIE) + 1), ('sha256', 'f' * 64),
                   ('preview_sha256', 'f' * 64), ('display_name', MARK + escape('other.mp4')),
                   ('display_name', 'clip.mp4'), ('caption', MARK + '"C\\u0061p"'), ('caption', MARK + escape('Cap\u200b')),
                   ('account_id', ACCOUNT + 1), ('approval_required', False)]
        for key, value in tampers:
            with self.subTest(key=key, value=value):
                result = copy.deepcopy(broker_result); result['video'][key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        result = copy.deepcopy(broker_result); result['video']['recipient']['chat_id'] = 5
        self.assertEqual(proxy(result).status, 'unavailable')
        # A coherent re-signed preview for another artifact, recipient or caption is refused by scope binding.
        forged = VideoDraftPreview.model_validate(broker_result['video'])
        for changes in ({'recipient': VideoRecipient(chat_id=5, title='Other')}, {'caption': 'Other'}):
            values = dict(draft_id=forged.draft_id, account_id=forged.account_id, expires_at=forged.expires_at,
                recipient=forged.recipient, artifact_id=forged.artifact_id, display_name=forged.raw_display_name,
                size_bytes=forged.size_bytes, sha256=forged.sha256, caption=forged.raw_caption,
                facts=VideoFacts(forged.duration_seconds, forged.width, forged.height, forged.codec,
                                 forged.has_audio, forged.supports_streaming))
            values.update(changes)
            coherent = VideoDraftPreview.create(**values)
            self.assertEqual(proxy(dict(broker_result, video=coherent.model_dump(mode='json'))).status, 'unavailable')
        other = PrepareVideoSendRequest(**self.request(self.upload(bmff.movie(faststart=False)), caption='Cap'))
        self.assertEqual(proxy(broker_result, other).status, 'unavailable')


class SendTests(VideoFixture, unittest.TestCase):
    """Criteria 4 and 5: revalidation, one closed attempt, receipt replay, never retry."""

    def test_one_exact_closed_send_message_attempt_then_receipt_replay(self):
        video = self.prepare(caption='Look')
        result = self.send(video['draft_id'])
        self.assertEqual((result['status'], result['message_id'], result['recipient']), ('sent', 701, RECIPIENT))
        [request] = self.sends()
        sending_id = request['options']['sending_id']
        self.assertIs(type(sending_id), int); self.assertTrue(0 < sending_id < 2**31)
        path = request['input_message_content']['video']['video']['path']
        self.assertEqual(Path(path), self.root / 'stage' / video['draft_id'] / 'clip.mp4')
        self.assertEqual(request, {'@type': 'sendMessage', 'chat_id': RECIPIENT, 'topic_id': None, 'reply_to': None,
            'reply_markup': None, 'options': {**SEND_OPTIONS, 'sending_id': sending_id},
            'input_message_content': {'@type': 'inputMessageVideo', 'video': {'@type': 'inputVideo',
                'video': {'@type': 'inputFileLocal', 'path': path}, 'thumbnail': None, 'cover': None,
                'start_timestamp': 0, 'added_sticker_file_ids': [], 'duration': 5, 'width': 1280, 'height': 720,
                'supports_streaming': True},
                'caption': {'@type': 'formattedText', 'text': 'Look', 'entities': []},
                'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False},
            '@extra': request['@extra']})
        self.assertFalse(Path(path).exists())  # The confirmed staged copy is retired.
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(self.approvals[0]['video_preview'], video)
        self.assertEqual(self.approvals[0]['kind'], 'video')
        replay = self.send(video['draft_id'])
        self.assertEqual((replay['status'], replay['message_id']), ('sent', 701))
        self.assertIn('no new send', replay['detail'])
        self.assertEqual(len(self.sends()), 1); self.assertEqual(len(self.approvals), 1)

    def test_staged_bytes_are_exactly_the_reviewed_artifact(self):
        staged = []
        real = stage_approved_document
        def capture(claim):
            path = real(claim, root=self.root / 'stage'); staged.append(path.read_bytes()); return path
        with patch('telegram_search_mcp.broker.stage_approved_document', side_effect=capture):
            self.assertEqual(self.send(self.prepare()['draft_id'])['status'], 'sent')
        self.assertEqual(staged, [MOVIE])

    def test_lost_invalid_and_rejected_outcomes_never_retry(self):
        for mode, final, expected in (('lost', {}, 'outcome_unknown'), ('sent', {'@type': 'animation'}, 'outcome_unknown'),
                                      ('rejected', {}, 'failed'), ('failed', {}, 'failed')):
            with self.subTest(mode=mode, final=final):
                self.setUp(); self.raw.mode = mode; self.raw.final = final
                did = self.prepare()['draft_id']
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['message_id'])
                self.assertEqual(self.send(did)['status'], expected)
                self.assertEqual(len(self.sends()), 1); self.assertEqual(len(self.approvals), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                if mode == 'rejected':
                    self.assertIn('code 400', result['detail'])

    def test_reencoded_final_facts_still_confirm_by_correlation(self):
        # I1: like document and photo sends, correlation and a final messageVideo decide.
        for final in ({'duration': 6}, {'width': 1281, 'height': 719}, {'video': file(len(MOVIE) + 1)},
                      {'duration': 1, 'width': 16, 'height': 16, 'video': file(1)}):
            with self.subTest(final=final):
                self.setUp(); self.raw.final = final
                did = self.prepare()['draft_id']
                result = self.send(did)
                self.assertEqual((result['status'], result['message_id']), ('sent', 701))
                self.assertEqual(len(self.sends()), 1)
                status = self.status(did)
                self.assertEqual((status['status'], status['message_id']), ('sent', 701))

    def test_final_message_without_reported_size_or_with_unknown_size_still_confirms(self):
        for final in ({'video': dict(file(0))}, {'minithumbnail': None}):
            with self.subTest(final=final):
                self.setUp(); self.raw.final = final
                self.assertEqual(self.send(self.prepare()['draft_id'])['status'], 'sent')

    def test_not_approved_and_declined_dialog_make_no_attempt(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, approved=False)['status'], 'not_approved')
        self.assertEqual(self.approvals, [])
        self.broker._approval_prompt = lambda **kw: False
        self.assertEqual(self.send(did)['status'], 'not_approved')
        self.assertEqual(self.sends(), [])
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.send(did)['status'], 'sent')

    def test_revalidation_before_dialog_refuses_without_dialog_or_attempt(self):
        cases = [
            ('recipient_changed', lambda: self.raw.chats[RECIPIENT].update(title='Renamed')),
            ('recipient_changed', lambda: self.raw.chats[RECIPIENT]['type'].update(user_id=9999)),
            ('expired', lambda: self.raw.chats.update({RECIPIENT: chat(RECIPIENT, 'Recipient', 'secret')})),
            ('expired', lambda: self.artifact_file(self.artifact_id).write_bytes(MOVIE[:-1] + b'X')),
            ('expired', lambda: self.artifact_file(self.artifact_id).unlink()),
        ]
        for expected, change in cases:
            with self.subTest(expected=expected):
                self.setUp(); self.artifact_id = self.upload(); did = self.prepare(self.artifact_id)['draft_id']
                change()
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['message_id'])
                self.assertEqual(self.approvals, []); self.assertEqual(self.sends(), [])

    def test_restart_foreign_owner_and_disabled_policy_are_content_free(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, client=FOREIGN)['status'], 'expired')
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send'))
        self.assertEqual(self.broker._send_prepared_video(SendPreparedVideoRequest(draft_id=did, approved=True),
                                                          client_id=CLIENT).status, 'expired')
        with self.assertRaises(CompatibilityError):
            self.send(did)
        self.assertEqual(self.status(did)['evidence'], 'none')
        self.broker._policy = POLICY
        titles = dict(self.broker._draft_recipient_titles); self.broker._draft_recipient_titles.clear()
        self.assertEqual(self.send(did)['status'], 'expired')
        self.broker._draft_recipient_titles.update(titles)
        self.broker._drafts = OutgoingDraftRegistry(self.broker._artifact_store)
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(self.approvals, []); self.assertEqual(self.sends(), [])

    def test_changes_during_the_dialog_never_send(self):
        def disable():
            self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send'))
        for expected, change in (('expired', disable),
                                 ('expired', lambda: setattr(self.raw, 'account', 8)),
                                 ('expired', lambda: self.artifact_file(self.artifact_id).write_bytes(MOVIE[:-1] + b'X')),
                                 ('local_failed', lambda: self.raw.chats[RECIPIENT].update(title='Renamed')),
                                 ('local_failed', lambda: self.raw.chats[RECIPIENT]['type'].update(user_id=9999))):
            with self.subTest(expected=expected, change=change):
                self.setUp(); self.artifact_id = self.upload(); did = self.prepare(self.artifact_id)['draft_id']
                def approve(**kw):
                    change(); return True
                self.broker._approval_prompt = approve
                result = self.send(did)
                self.assertEqual(result['status'], expected); self.assertEqual(self.sends(), [])
                if expected == 'local_failed':
                    replay = self.send(did)
                    self.assertEqual(replay['status'], 'local_failed')
                    self.assertIn('no provider attempt was made', replay['detail'])
                    status = self.status(did)
                    self.assertEqual((status['status'], status['evidence']), ('failed', 'local_failed'))

    def test_artifact_change_after_claim_is_a_local_staging_failure(self):
        self.artifact_id = self.upload(); did = self.prepare(self.artifact_id)['draft_id']
        real = stage_approved_document
        def tamper(claim):
            claim.path.write_bytes(MOVIE[:-1] + b'X'); return real(claim, root=self.root / 'stage')
        with patch('telegram_search_mcp.broker.stage_approved_document', side_effect=tamper):
            result = self.send(did)
        self.assertEqual(result['status'], 'local_failed'); self.assertEqual(self.sends(), [])
        self.assertEqual(list((self.root / 'stage').iterdir()), [])

    def test_guard_failure_after_registration_is_local_failed_without_transport(self):
        did = self.prepare()['draft_id']
        observations = self.client._send_observations
        register = observations.register
        def flip(*args, **kwargs):
            register(*args, **kwargs)
            self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send'))
        observations.register = flip
        result = self.broker._send_prepared_video(SendPreparedVideoRequest(draft_id=did, approved=True),
                                                  client_id=CLIENT)
        self.broker._policy = POLICY
        self.assertEqual(result.status, 'local_failed'); self.assertEqual(self.sends(), [])
        self.assertIsNone(observations.snapshot(did))
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('failed', 'local_failed'))

    def test_video_policy_is_rechecked_inside_the_raw_dispatch_fence(self):
        did = self.prepare()['draft_id']
        fence = self.broker._artifact_send_fence
        calls = []
        def late_disable(*args):
            calls.append(args)
            if len(calls) == 2:  # The provider's raw-dispatch fence, after every guard has passed.
                self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send'))
            return fence(*args)
        with patch.object(self.broker, '_artifact_send_fence', side_effect=late_disable):
            result = self.send(did)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'local_failed'); self.assertEqual(self.sends(), [])


UNKNOWN = 'current receipt authority is unavailable; do not retry automatically'


class AttemptedSendTests(VideoFixture, unittest.TestCase):
    """M1: the call that consumed the claim never reads as expired after a lost authority."""

    def after_raw_send(self, action):
        real = self.raw.send
        def send(request):
            real(request)
            if request['@type'] == 'sendMessage':
                action()
        self.raw.send = send

    def assert_unknown(self, result, did):
        self.assertEqual(result, {'status': 'outcome_unknown', 'draft_id': did, 'recipient': None,
                                  'message_id': None, 'detail': UNKNOWN})

    def test_release_during_the_confirmation_wait_is_outcome_unknown_never_expired(self):
        did = self.prepare()['draft_id']
        self.after_raw_send(lambda: self.broker._artifact_authority.release(CLIENT))
        self.assert_unknown(self.send(did), did)
        self.assertEqual(len(self.sends()), 1)
        receipt = self.broker._drafts._entries[did].receipt
        self.assertEqual((receipt.status, receipt.message_id), ('sent', 701))  # Kept, never disclosed here.
        # A later call under a fresh lifecycle keeps the content-free replay refusal and sends nothing.
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(len(self.sends()), 1); self.assertEqual(len(self.approvals), 1)
        # Content-free routing only: another client or the other send tool learns nothing.
        self.assertEqual(self.send(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(self.broker._artifact_unavailable('send_prepared_artifact', {'draft_id': did}, CLIENT)['status'],
                         'expired')
        self.assertEqual(self.broker._artifact_unavailable('send_prepared_video', {'draft_id': did}, FOREIGN)['status'],
                         'expired')

    def test_account_switch_right_after_the_raw_send_is_outcome_unknown(self):
        did = self.prepare()['draft_id']
        self.after_raw_send(lambda: setattr(self.raw, 'account', 8))
        self.assert_unknown(self.send(did), did)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.broker._drafts._entries[did].receipt.status, 'sent')
        # Later calls without the original account epoch keep the replay refusal and never resend.
        self.assertEqual(self.send(did)['status'], 'expired')
        self.raw.account = ACCOUNT  # Switching back does not revive the old account epoch.
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(len(self.sends()), 1); self.assertEqual(len(self.approvals), 1)

    def test_local_failure_with_lost_publication_replays_local_failed(self):
        did = self.prepare()['draft_id']
        def stage(claim):
            self.broker._artifact_authority.release(CLIENT)
            raise OSError('synthetic staging failure')
        with patch('telegram_search_mcp.broker.stage_approved_document', side_effect=stage):
            result = self.send(did)
        self.assertEqual(result, {'status': 'local_failed', 'draft_id': did, 'recipient': None, 'message_id': None,
                                  'detail': 'local revalidation failed; no provider send was attempted'})
        self.assertEqual(self.send(did)['status'], 'expired')  # Later call: fresh lifecycle, replay refusal.
        self.assertEqual(self.sends(), [])

    def test_release_before_the_claim_stays_expired_without_attempt(self):
        did = self.prepare()['draft_id']
        def approve(**kw):
            self.broker._artifact_authority.release(CLIENT); return True
        self.broker._approval_prompt = approve
        result = self.send(did)
        self.assertEqual((result['status'], result['detail']), ('expired', 'draft is unavailable'))
        self.assertEqual(self.sends(), [])
        self.assertIsNone(self.broker._drafts.attempt_outcome(did, client_id=CLIENT, kinds=frozenset({'video'})))

    def test_document_artifact_released_after_the_raw_send_is_outcome_unknown(self):
        artifact_id = self.upload()
        did = self.dispatch('prepare_artifact_send', {'artifact_id': artifact_id, 'recipient': RECIPIENT,
            'display_name': 'clip.mp4', 'mime_type': 'video/mp4'})['draft_id']
        real = self.raw.send
        def send(request):
            if request['@type'] != 'sendMessage':
                return real(request)
            self.raw.sent.append(copy.deepcopy(request))
            content = {'@type': 'messageDocument', 'document': {},
                       'caption': {'@type': 'formattedText', 'text': '', 'entities': []}}
            self.raw.events.append({**outgoing(-10, content, sending_state={'@type': 'messageSendingStatePending',
                'sending_id': request['options']['sending_id']}), '@extra': request['@extra']})
            self.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                                    'message': outgoing(801, content, sending_state=None)})
            self.broker._artifact_authority.release(CLIENT)
        self.raw.send = send
        payload = {'draft_id': did, 'approved': True}
        self.assert_unknown(self.dispatch('send_prepared_artifact', payload), did)
        self.assertEqual(self.broker._drafts._entries[did].receipt.status, 'sent')
        self.assertEqual(self.dispatch('send_prepared_artifact', payload)['status'], 'expired')  # Later call.
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.send(did)['status'], 'expired')  # The video tool learns nothing about a document.


class StatusTests(VideoFixture, unittest.TestCase):
    """Criterion 7: pending, then sent or failed only with a matching messageVideo."""

    def test_pending_then_late_correlated_messagevideo_reconciles_without_retry(self):
        self.raw.mode = 'lost'
        did = self.prepare()['draft_id']
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('pending', 'local_pending'))
        self.assertEqual(self.send(did)['status'], 'outcome_unknown')
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'provider_pending'))
        good = video_content('', 5, 1280, 720, len(MOVIE), True)
        def changed(**fields):
            value = copy.deepcopy(good); value['video'].update(fields); return value
        # Another chat, another content type or a missing video object never reconciles; a
        # correlated messageVideo does, whatever facts the provider re-encoded (I1).
        self.raw.events.extend([
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(703, good, chat_id=999)},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
             'message': outgoing(707, {'@type': 'messageDocument', 'document': {}})},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10, 'message': outgoing(708, dict(good, video=None))},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
             'message': outgoing(702, changed(duration=4, height=721, video=file(len(MOVIE) - 1)))}])
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence'], status['message_id']), ('sent', 'provider_confirmed', 702))
        self.assertEqual((self.send(did)['status'], self.send(did)['message_id']), ('sent', 702))
        self.assertEqual(len(self.sends()), 1)

    def test_late_exact_failure_and_policy_bound_status(self):
        self.raw.mode = 'lost'
        did = self.prepare()['draft_id']; self.send(did)
        content = video_content('', 5, 1280, 720, len(MOVIE), True)
        self.raw.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': -10,
            'message': outgoing(-10, content, sending_state={'@type': 'messageSendingStateFailed',
                                                             'error': {'@type': 'error', 'code': 400}}),
            'error': {'@type': 'error', 'code': 400, 'message': 'PRIVATE'}})
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('failed', 'provider_failed'))
        self.assertNotIn('PRIVATE', json.dumps(status))
        self.assertEqual(self.status(did, FOREIGN)['evidence'], 'none')
        self.broker._policy = RuntimePolicy(enabled_capabilities=('artifacts', 'send'))
        self.assertEqual(self.broker._get_send_status(GetSendStatusRequest(draft_id=did), client_id=CLIENT).evidence,
                         'none')

    def test_confirmed_send_status(self):
        did = self.prepare()['draft_id']; self.send(did)
        status = self.status(did)
        self.assertEqual((status['status'], status['message_id']), ('sent', 701))


GOLDEN = ('Send this native video to Recipient (chat 4242)?\nAccount: 7\nDraft: ' + DID + '\n'
          'Expires: 2030-01-02T03:04:05+00:00\n\n'
          'Untrusted spans in « » below are exact; ' + READABLE_RULE + '.\n'
          'File name [untrusted] (exact): «{name}»\n'
          'Bytes: {size}\nSHA-256: {sha}\nDeclared by the file (not decoded):\nDuration: 5 seconds\n'
          'Dimensions: 1280x720\nCodec: h264\nAudio: no\nStreaming: yes\n{caption}'
          'Recipient: Recipient (chat 4242)\nPreview SHA-256: {preview}\n\n'
          'Unofficial Telegram MCP will revalidate the account, the recipient and the file bytes, '
          'then make one upload/send attempt as a native video.')


class DialogTests(unittest.TestCase):
    """Criterion 6: dedicated video dialog golden; untrusted name and caption marked."""

    def golden(self, value, name, caption):
        return GOLDEN.format(name=name, size=len(MOVIE), sha=hashlib.sha256(MOVIE).hexdigest(),
                             caption=caption, preview=value.preview_sha256)

    def test_plain_and_hostile_goldens(self):
        # L4/L5: name and caption in « » under the forward dialog's legend; facts labelled as declared.
        self.assertNotIn('{', READABLE_RULE)
        plain = preview()
        self.assertEqual(shown(plain), [self.golden(plain, 'clip.mp4', 'Caption: (none)\n')])
        caption = 'Hi "there"\nRecipient: Mom (chat 1)\tp\u0430ypal ⟦x Привет 日本 \U0001F600 »\nCodec: hevc «'
        hostile = preview(name='p\u0430ypal ⟦1⟧ ».mp4', caption=caption)
        self.assertEqual(shown(hostile), [self.golden(hostile, 'p⟦U+0430⟧ypal ⟦U+27E6⟧1⟧ ⟦U+00BB⟧.mp4',
            'Caption [untrusted] (exact): «Hi "there"⟦U+000A⟧Recipient: Mom (chat 1)⟦U+0009⟧'
            'p⟦U+0430⟧ypal ⟦U+27E6⟧x Привет 日本 \U0001F600 ⟦U+00BB⟧⟦U+000A⟧Codec: hevc ⟦U+00AB⟧»\n')])
        self.assertEqual(readable(caption).count('\n'), 0)
        self.assertEqual(sum(readable(value).count(mark) for value in (caption, hostile.raw_display_name)
                             for mark in '«»'), 0)

    def test_preview_description_labels_facts_declared_not_decoded(self):
        self.assertTrue(VideoDraftPreview.model_json_schema()['description'].startswith(
            'Facts declared by the file (not decoded)'))

    def test_preview_digest_is_independently_reproducible(self):
        value = preview(caption='Cap', facts=VideoFacts(5, 1280, 720, 'hevc', True, False))
        data = value.model_dump(mode='json')
        facts = {'domain': 'telegram-search-mcp.video', 'version': 1, 'draft_id': DID, 'account_id': ACCOUNT,
            'expires_at': '2030-01-02T03:04:05+00:00', 'approval_required': True,
            'recipient': {'chat_id': RECIPIENT, 'title': 'Recipient'}, 'artifact_id': data['artifact_id'],
            'display_name': MARK + escape('clip.mp4'), 'size_bytes': len(MOVIE),
            'sha256': hashlib.sha256(MOVIE).hexdigest(),
            'video': {'duration_seconds': 5, 'width': 1280, 'height': 720, 'codec': 'hevc', 'has_audio': True,
                      'supports_streaming': False},
            'caption': MARK + escape('Cap'),
            'request': {'@type': 'sendMessage', 'chat_id': RECIPIENT, 'topic_id': None, 'reply_to': None,
                'reply_markup': None, 'options': SEND_OPTIONS, 'input_message_content': {
                    '@type': 'inputMessageVideo', 'video': {'@type': 'inputVideo', 'thumbnail': None, 'cover': None,
                        'start_timestamp': 0, 'added_sticker_file_ids': [], 'duration': 5, 'width': 1280,
                        'height': 720, 'supports_streaming': False},
                    'caption': {'@type': 'formattedText', 'text': 'Cap', 'entities': []},
                    'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}}}
        self.assertEqual(value.preview_sha256, digest(facts))
        self.assertEqual(VideoDraftPreview.model_validate(data), value)

    def test_competing_mismatched_or_tampered_video_previews_refuse(self):
        value = preview(caption='Cap')
        from test_forward_send import preview as forward_preview
        self.assertEqual(shown(value, forward_preview=forward_preview().model_dump(mode='json')), [])
        for kind in ('document', 'forward', 'text'):
            self.assertEqual(shown(value, kind=kind), [])
        self.assertEqual(shown(None), [])
        for key, changed in (('recipient', RECIPIENT + 1), ('recipient_title', 'Other'), ('sha256', 'b' * 64),
                             ('size_bytes', 1), ('display_name', 'other.mp4'), ('caption', 'Other'), ('caption', ''),
                             ('text', 'smuggled'), ('duration_seconds', 5), ('source_sha256', 'a' * 64)):
            with self.subTest(key=key):
                self.assertEqual(shown(value, **{key: changed}), [])
        for key, changed in (('codec', 'hevc'), ('account_id', ACCOUNT + 1), ('caption', MARK + '"C\\u0061p"'),
                             ('display_name', 'clip.mp4'), ('width', 1281), ('width', 8193)):
            data = value.model_dump(mode='json'); data[key] = changed
            self.assertEqual(shown(value, video_preview=data), [], key)

    def test_preview_model_refuses_incoherent_facts(self):
        for facts in (VideoFacts(5, 1280, 60, 'h264', False, True), VideoFacts(0, 1280, 720, 'h264', False, True),
                      VideoFacts(5, 1280, 720, 'vp9', False, True)):
            with self.subTest(facts=facts), self.assertRaises(ValueError):
                preview(facts=facts)
        with self.assertRaises(ValueError):
            preview(size_bytes=len(MOVIE) + 1)  # Disagrees with the artifact ID.
        with self.assertRaises(ValueError):
            preview(caption='x\u200b')  # Not normalized caption evidence.


class TransportShapeTests(unittest.TestCase):
    def valid(self, root, content=None, **changes):
        video = {'@type': 'inputVideo', 'video': {'@type': 'inputFileLocal', 'path': str(root / ('draft_' + 'a' * 32) / 'clip.mp4')},
                 'thumbnail': None, 'cover': None, 'start_timestamp': 0, 'added_sticker_file_ids': [],
                 'duration': 5, 'width': 1280, 'height': 720, 'supports_streaming': True}
        message = {'@type': 'inputMessageVideo', 'video': video,
                   'caption': {'@type': 'formattedText', 'text': 'Cap', 'entities': []},
                   'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}
        for key, value in (content or {}).items():
            (video if key in video and key not in ('@type',) else message)[key] = value
        return {'@type': 'sendMessage', 'chat_id': RECIPIENT, 'topic_id': None, 'reply_to': None,
                'reply_markup': None, 'options': {**SEND_OPTIONS, 'sending_id': 77},
                'input_message_content': message, **changes}

    def test_input_message_video_has_one_closed_shape(self):
        root = Path(tempfile.mkdtemp())
        with patch('telegram_search_mcp.tdjson.STAGING_ROOT', root):
            _validate_bounded_request(self.valid(root))
            _validate_bounded_request(self.valid(root, {'supports_streaming': False, 'duration': 10800,
                                                        'width': 16, 'height': 320}))
            bad = [{'thumbnail': {'@type': 'inputThumbnail'}}, {'cover': {'@type': 'inputFileLocal', 'path': '/x'}},
                   {'start_timestamp': 1}, {'start_timestamp': False}, {'added_sticker_file_ids': [1]},
                   {'added_sticker_file_ids': ()}, {'duration': 0}, {'duration': True}, {'duration': 10801},
                   {'width': 15}, {'height': 8193}, {'width': 2000, 'height': 99}, {'supports_streaming': 1},
                   {'show_caption_above_media': True}, {'self_destruct_type': {'@type': 'messageSelfDestructTypeTimer'}},
                   {'has_spoiler': True}, {'caption': {'@type': 'formattedText', 'text': 'x', 'entities': [{}]}},
                   {'caption': {'@type': 'formattedText', 'text': 'x' * 1025, 'entities': []}},
                   {'video': {'@type': 'inputFileLocal', 'path': '/tmp/unapproved.mp4'}},
                   {'video': {'@type': 'inputFileRemote', 'id': 'X'}}, {'extra': 1}]
            for changes in bad:
                with self.subTest(changes=repr(changes)[:80]), self.assertRaises(ForbiddenTDLibRequest):
                    _validate_bounded_request(self.valid(root, changes))
            for changes in ({'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 5, 'quote': None,
                                          'checklist_task_id': 0, 'poll_option_id': ''}},
                            {'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 1}},
                            {'options': {**SEND_OPTIONS, 'sending_id': 77, 'protect_content': True}},
                            {'reply_markup': {'@type': 'replyMarkupRemoveKeyboard'}}):
                with self.subTest(changes=repr(changes)[:80]), self.assertRaises(ForbiddenTDLibRequest):
                    _validate_bounded_request(self.valid(root, **changes))
            message = self.valid(root)
            message['input_message_content']['video'].pop('cover')
            with self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(message)

    def test_video_observation_registration_is_closed_and_final_correlates_by_type(self):
        observations = SendObservations()
        for kwargs in ({'video': (5, 1280, 720)}, {'video': [5, 1280, 720, 9]}, {'video': (0, 1280, 720, 9)},
                       {'video': (5, 1280, 720, 9), 'caption_sha256': 'a' * 64},
                       {'video': (5, 1280, 720, 9), 'reply_anchor': (RECIPIENT, 1)},
                       {'video': (5, 1280, 720, 9), 'forward': True}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ObservationUnavailable):
                observations.register('a', 'e', 1, RECIPIENT, 'messageVideo', **kwargs)
        with self.assertRaises(ObservationUnavailable):
            observations.register('a', 'e', 1, RECIPIENT, 'messageDocument', video=(5, 1280, 720, 9))
        content = video_content('', 5, 1280, 720, 9, True)
        pending = outgoing(-10, content, sending_state={'@type': 'messageSendingStatePending', 'sending_id': 5})
        for final, expected in ((content, 'sent'), (dict(content, video=dict(content['video'], duration=6)), 'sent'),
                                (dict(content, video=dict(content['video'], video=file(10))), 'sent'),
                                (dict(content, video=dict(content['video'], width=17, height=9000)), 'sent'),
                                (dict(content, video=None), 'pending'),
                                (dict(content, video=dict(content['video'], **{'@type': 'animation'})), 'pending'),
                                (dict(content, **{'@type': 'messageDocument'}), 'pending'),
                                (dict(content, video=dict(content['video'], video=file(0))), 'sent')):
            observed = SendObservations()
            observed.register('a', 'e', 5, RECIPIENT, 'messageVideo', video=(5, 1280, 720, 9))
            observed.reduce({**pending, '@extra': 'e'})
            self.assertEqual(observed.snapshot('a').status, 'pending')
            observed.reduce({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                             'message': outgoing(9, final, sending_state=None)})
            self.assertEqual(observed.snapshot('a').status, expected)

    def test_registry_binds_probed_bytes_and_never_lists_cancels_or_revises_video_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(cache_dir=Path(directory) / 'cache')
            source = Path(directory) / 'clip.mp4'; source.write_bytes(MOVIE)
            artifact = store.store(source, kind='media')
            registry = OutgoingDraftRegistry(store)
            owner = DraftOwner(CLIENT, ACCOUNT)
            facts = probe_video(artifact.path, size_bytes=artifact.size_bytes)
            spec = VideoSpec(facts=facts, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
                             recipient_identity=('chatTypePrivate', RECIPIENT))
            draft = registry.prepare_video(owner=owner, artifact_id=artifact.artifact_id, recipient=RECIPIENT,
                                           recipient_title='Recipient', display_name='a/b\\clip.mp4', caption='c',
                                           video=spec)
            self.assertEqual((draft.kind, draft.sha256, draft.display_name, draft.video), ('video', artifact.sha256,
                                                                                           'clip.mp4', spec))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            with self.assertRaises(DraftError):
                registry.cancel(draft.draft_id, owner=owner)
            with self.assertRaises(DraftError):
                registry.revise(draft.draft_id, owner=owner, recipient_title='Recipient', caption='x')
            with self.assertRaises(DraftError):
                registry.prepare_video(owner=owner, artifact_id=artifact.artifact_id, recipient=RECIPIENT,
                                       recipient_title='Recipient', display_name='x.mp4', caption='', video=object())
            stale = VideoSpec(facts=facts, sha256='0' * 64, size_bytes=artifact.size_bytes,
                              recipient_identity=('chatTypePrivate', RECIPIENT))
            with self.assertRaises(DraftError):
                registry.prepare_video(owner=owner, artifact_id=artifact.artifact_id, recipient=RECIPIENT,
                                       recipient_title='Recipient', display_name='x.mp4', caption='', video=stale)
            for bad in (dict(recipient_identity=()), dict(size_bytes=64 * 1024 * 1024 + 1),
                        dict(facts=VideoFacts(5, 1280, 720, 'vp9', False, True))):
                with self.assertRaises(ValueError):
                    VideoSpec(**{**dict(facts=facts, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
                                        recipient_identity=('chatTypePrivate', RECIPIENT)), **bad})
            self.assertEqual(registry.peek(draft.draft_id, owner=owner), draft)

    def test_video_confirmation_waits_up_to_120_seconds_then_stays_unknown(self):
        clock = [1000.0]
        root = Path(tempfile.mkdtemp())
        staged = root / ('draft_' + 'a' * 32) / 'clip.mp4'; staged.parent.mkdir(); staged.write_bytes(MOVIE)
        waits = {}
        for kind in ('video', 'document'):
            raw = Raw(); raw.mode = 'silent'
            def receive(timeout, raw=raw):
                if raw.events:
                    return raw.events.popleft()
                clock[0] += timeout
                return None
            raw.receive = receive
            if kind == 'document':
                raw.send = lambda request, raw=raw: (raw.sent.append(request) or raw.events.append(
                    {**outgoing(-10, {'@type': 'messageDocument'}, sending_state={'@type': 'messageSendingStatePending',
                     'sending_id': request['options']['sending_id']}), '@extra': request['@extra']}))
            client = TDLibClient(raw=raw); client._ready = True
            with patch('telegram_search_mcp.tdjson.STAGING_ROOT', root), patch('time.monotonic', lambda: clock[0]):
                start = clock[0]
                with self.assertRaises(MessageSendOutcomeUnknown):
                    if kind == 'video':
                        client.send_video_message(RECIPIENT, staged, '', duration_seconds=5, width=1280, height=720,
                            supports_streaming=True, size_bytes=len(MOVIE), expected_account_id=ACCOUNT,
                            expected_recipient_title='Recipient', expected_recipient_identity=('chatTypePrivate', RECIPIENT),
                            attempt_id='attempt', pre_send_guard=lambda: True)
                    else:
                        client.send_document_message(RECIPIENT, staged, '', attempt_id='attempt')
                waits[kind] = clock[0] - start
            self.assertEqual(len([x for x in raw.sent if x['@type'] == 'sendMessage']), 1)
        self.assertAlmostEqual(waits['video'], 120, delta=1)
        self.assertAlmostEqual(waits['document'], 30, delta=1)

    def test_sender_refusals_before_registration_are_no_attempt(self):
        root = Path(tempfile.mkdtemp())
        staged = root / ('draft_' + 'a' * 32) / 'clip.mp4'; staged.parent.mkdir(); staged.write_bytes(MOVIE)
        arguments = dict(duration_seconds=5, width=1280, height=720, supports_streaming=True, size_bytes=len(MOVIE),
                         expected_account_id=ACCOUNT, expected_recipient_title='Recipient',
                         expected_recipient_identity=('chatTypePrivate', RECIPIENT), attempt_id='attempt',
                         pre_send_guard=lambda: True)
        for changes, path in (({'expected_account_id': 8}, staged), ({'expected_recipient_title': 'Other'}, staged),
                              ({'expected_recipient_identity': ('chatTypePrivate', 1)}, staged),
                              ({'pre_send_guard': lambda: False}, staged), ({'width': 15}, staged),
                              ({}, Path('/tmp/unapproved.mp4'))):
            with self.subTest(changes=changes, path=path):
                raw = Raw(); client = TDLibClient(raw=raw); client._ready = True
                with patch('telegram_search_mcp.tdjson.STAGING_ROOT', root), self.assertRaises(MessageSendNotAttempted):
                    client.send_video_message(RECIPIENT, path, '', **{**arguments, **changes})
                self.assertEqual([x for x in raw.sent if x['@type'] == 'sendMessage'], [])
                self.assertIsNone(client._send_observations.snapshot('attempt'))


class FinalReviewTests(VideoFixture, unittest.TestCase):
    """Whole-branch review: the caller that lost the claim reads outcome_unknown (spec 0.54 §8)."""

    def assert_unknown(self, result, did):
        self.assertEqual(result, {'status': 'outcome_unknown', 'draft_id': did, 'recipient': None,
                                  'message_id': None, 'detail': UNKNOWN})

    def test_concurrent_sends_make_one_attempt(self):
        # The second caller made no attempt but reads outcome_unknown, never sent or expired.
        did = self.prepare()['draft_id']
        gate = threading.Event(); results = []
        def prompt(**kw):
            self.approvals.append(kw); gate.wait(5); return True
        self.broker._approval_prompt = prompt
        threads = [threading.Thread(target=lambda: results.append(self.send(did))) for _ in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 5
        while len(self.approvals) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        gate.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(result['status'] for result in results), ['outcome_unknown', 'sent'])
        self.assert_unknown(next(result for result in results if result['status'] != 'sent'), did)
        self.assertEqual(len(self.sends()), 1)

    def test_dialog_that_outlives_the_winning_send_reads_outcome_unknown(self):
        did = self.prepare()['draft_id']
        opened, release, calls, results = threading.Event(), threading.Event(), [], {}
        def prompt(**kw):
            calls.append(kw)
            if len(calls) == 1:
                opened.set(); release.wait(10)
            return True
        self.broker._approval_prompt = prompt
        loser = threading.Thread(target=lambda: results.setdefault('loser', self.send(did)))
        loser.start(); self.assertTrue(opened.wait(10))
        winner = self.send(did)
        release.set(); loser.join(10)
        self.assertEqual((winner['status'], winner['message_id']), ('sent', 701))
        self.assert_unknown(results['loser'], did)
        self.assertEqual((self.send(did)['status'], len(self.sends())), ('sent', 1))  # Receipt replay.

    def test_repeated_call_during_an_inflight_claim_reads_outcome_unknown_without_dialog(self):
        did = self.prepare()['draft_id']
        self.broker._drafts.claim(did, owner=DraftOwner(CLIENT, ACCOUNT), approved=True, provider=self.client,
                                  provider_epoch=self.client.send_observation_epoch)
        self.assert_unknown(self.send(did), did)
        self.assertEqual(self.send(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual((self.approvals, self.sends()), ([], []))


if __name__ == '__main__':
    unittest.main()
