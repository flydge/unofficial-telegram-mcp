"""Synthetic broker ownership, nonconsumption and publication boundaries."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from functools import partial
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.local_upload import LocalUploadRegistry
from telegram_search_mcp.schemas import CreateLocalArtifactRequest, ReadAttachmentRequest
from xlsx_fixtures import write_xlsx
from pptx_fixtures import make_pptx

A = 'client_' + 'a' * 24
B = 'client_' + 'b' * 24
POLICY = RuntimePolicy(enabled_capabilities=('artifacts', 'read', 'attachment_pages',
    'spreadsheets', 'presentations', 'send', 'reply_artifact_send'))


class Provider:
    def __init__(self, source):
        self.source = source
        self.account = 17
        self.calls = []
        self.send_observation_epoch = object()
        self._request_context = threading.local()

    def ensure_ready(self): self.calls.append('ready')
    def get_account_id(self):
        self.calls.append('account')
        return self.account
    def resolve_target(self, target):
        self.calls.append('resolve')
        return {'id': target, 'title': 'Synthetic', 'type': {'@type': 'chatTypePrivate'}}
    def get_message(self, chat, message):
        self.calls.append('message')
        return {'chat_id': chat, 'id': message, 'content': {'@type': 'messageDocument',
            'document': {'file_name': 'private.txt', 'mime_type': 'text/plain',
                         'document': {'@type': 'file', 'id': 1, 'size': 3}}}}
    def download_file(self, file, **kwargs):
        self.calls.append('download')
        return self.source
    def request_budget(self, deadline): return nullcontext()
    def close(self): pass


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        # Retain canonical OS-temp fixtures for independent evidence review.
        self.root = Path(tempfile.mkdtemp(prefix='telegram-b2a-')).resolve()
        self.source = self.root / 'download' / 'private.txt'
        self.source.parent.mkdir()
        self.source.write_bytes(b'abc')
        self.provider = Provider(self.source)
        self.store = ArtifactStore(cache_dir=self.root / 'cache')
        self.now = [100.]
        with patch('telegram_search_mcp.broker.LocalUploadRegistry',
                   side_effect=partial(LocalUploadRegistry, root=self.root / 'uploads')):
            self.broker = Broker(socket_path=self.root / 'run' / 'broker.sock',
                artifact_store=self.store, download_source_root=self.source.parent,
                client_factory=lambda: self.provider, policy=POLICY,
                context_clock=lambda: self.now[0], verify_peer_uid=False)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.shutdown()
        for entry in self.broker._uploads._entries.values(): os.close(entry.fd)
        self.broker._uploads._entries.clear()

    def dispatch(self, op, payload, client=A, broker=None):
        broker = broker or self.broker
        return broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic() + 30, 'broker_generation': broker._generation})

    def create(self, name='private.txt', data=b'private evidence'):
        response = self.dispatch('create_local_artifact', {'file_name': name,
            'content_base64': base64.b64encode(data).decode()})
        self.assertEqual(response['status'], 'complete')
        return response

    def begin(self, data=b'abc'):
        result = self.dispatch('begin_local_upload', {'file_name': 'private.txt',
            'kind': 'document', 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.assertEqual(result['status'], 'ready')
        return result['upload_id']

    def append(self, token, data=b'abc', index=0, client=A):
        return self.dispatch('append_local_upload', {'upload_id': token, 'index': index,
            'content_base64': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest()}, client)

    def test_foreign_initial_readers_and_drafts_do_zero_content_work(self):
        word = self.create()
        xlsx = write_xlsx(self.root / 'book.xlsx')
        book = self.create(xlsx.name, xlsx.read_bytes())
        pptx = make_pptx(self.root / 'slides.pptx')
        slides = self.create(pptx.name, pptx.read_bytes())
        cases = [('read_attachment', {'artifact_id': word['artifact_id']}),
            ('analyze_media', {'artifact_id': word['artifact_id']}),
            ('read_attachment_page', {'artifact_id': word['artifact_id'], 'render_pages': False}),
            ('read_spreadsheet', {'artifact_id': book['artifact_id'], 'selections': [{'sheet_index': 1, 'range': 'A1'}]}),
            ('read_presentation', {'artifact_id': slides['artifact_id'], 'slides': [1]}),
            ('prepare_artifact_send', {'artifact_id': word['artifact_id'], 'recipient': 7,
                'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''}),
            ('prepare_reply_artifact_send', {'artifact_id': word['artifact_id'], 'recipient': 7,
                'reply_to': {'chat_id': 7, 'message_id': 8}, 'display_name': 'private.txt',
                'mime_type': 'text/plain', 'caption': ''})]
        self.provider.calls.clear()
        forbidden = AssertionError('foreign artifact reached content processing')
        with patch('telegram_search_mcp.broker.read_document', side_effect=forbidden), \
             patch('telegram_search_mcp.broker.analyze_media', side_effect=forbidden), \
             patch('telegram_search_mcp.attachment_pages.read_document_page', side_effect=forbidden), \
             patch('telegram_search_mcp.spreadsheets.read_xlsx', side_effect=forbidden), \
             patch('telegram_search_mcp.presentations.read_pptx', side_effect=forbidden), \
             patch('telegram_search_mcp.broker.prepare_voice_note', side_effect=forbidden):
            for op, payload in cases:
                with self.subTest(op=op):
                    response = self.dispatch(op, payload, B)
                    self.assertNotIn(response['status'], ('complete', 'prepared', 'page', 'catalog', 'unsupported'))
                    self.assertFalse(response.get('artifact_path'))
                    self.assertFalse(response.get('source_anchor'))
                    self.assertFalse(response.get('scope'))
                    self.assertFalse(response.get('reply'))
                    self.assertFalse(response.get('text'))
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': word['artifact_id']})['text'], 'private evidence')

    def test_foreign_upload_attempts_never_mutate_or_consume_owner_entry(self):
        token = self.begin(b'abcdef')
        entry = self.broker._uploads._entries[token]
        expiry = entry.expires_at
        for index in range(2):
            self.assertEqual(self.append(token, b'abc', index, B)['status'], 'error')
            self.assertEqual(self.dispatch('finish_local_upload', {'upload_id': token}, B)['status'], 'error')
            self.assertIn(token, self.broker._uploads._entries)
            self.assertEqual((entry.received, entry.next_index, entry.expires_at), (3 * index, index, expiry))
            self.assertEqual(entry.path.read_bytes(), b'abc' if index else b'')
            self.assertEqual(self.append(token, b'abc' if index == 0 else b'def', index)['status'], 'accepted')
        done = self.dispatch('finish_local_upload', {'upload_id': token})
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(done['file_name'], 'private.txt')
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': done['artifact_id']})['text'], 'abcdef')
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': done['artifact_id']}, B)['status'], 'expired')

    def test_local_only_release_reuse_does_not_resurrect_and_idle_does_not_expire(self):
        made = self.create()
        token = self.begin()
        self.assertEqual(self.broker._contexts, {})
        self.now[0] += 301
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['text'], 'private evidence')
        self.dispatch('release_client', {})
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['status'], 'expired')
        self.assertEqual(self.append(token)['status'], 'error')
        self.assertEqual(self.dispatch('finish_local_upload', {'upload_id': token})['status'], 'error')
        new = self.create(data=b'new lifecycle')
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': new['artifact_id']})['text'], 'new lifecycle')
        self.assertEqual(self.provider.calls, [])

    def test_restart_cannot_adopt_existing_cache_even_same_client_id(self):
        made = self.create()
        replacement = Broker(socket_path=self.root / 'other.sock',
            artifact_store=ArtifactStore(cache_dir=self.store.cache_dir), policy=POLICY,
            client_factory=lambda: self.provider)
        self.addCleanup(replacement.shutdown)
        result = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''}, broker=replacement)
        self.assertEqual(result['status'], 'error')
        self.assertTrue(Path(made['artifact_path']).exists())
        self.assertEqual(self.provider.calls, [])

    def test_release_during_local_parse_discards_content_and_reuse(self):
        made = self.create()
        entered = threading.Event(); resume = threading.Event(); results = []
        from telegram_search_mcp.document_reader import read_document
        def paused(*args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(3))
            return read_document(*args, **kwargs)
        with patch('telegram_search_mcp.broker.read_document', side_effect=paused):
            worker = threading.Thread(target=lambda: results.append(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})))
            worker.start(); self.assertTrue(entered.wait(2))
            self.dispatch('release_client', {})
            self.create(data=b'new')
            resume.set(); worker.join(3); self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'expired')
        self.assertFalse(results[0]['text']); self.assertFalse(results[0]['images'])
        self.assertIsNone(results[0]['source_anchor'])

    def test_download_observed_account_drift_never_publishes_or_resurrects(self):
        first = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
        self.assertEqual(first['status'], 'complete')
        self.provider.account = 18
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': first['artifact_id']})['status'], 'expired')
        self.provider.account = 17
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': first['artifact_id']})['status'], 'expired')
        original = self.provider.download_file
        def drift(*args, **kwargs):
            result = original(*args, **kwargs)
            self.provider.account = 18
            return result
        self.provider.download_file = drift
        response = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
        self.assertEqual(response['status'], 'error')
        self.assertIsNone(response['artifact_id']); self.assertIsNone(response['artifact_path'])
        self.assertIsNone(response['file_name']); self.assertIsNone(response['sha256'])

    def test_offline_first_page_spreadsheet_presentation_never_start_provider(self):
        word = self.create(data=b'offline')
        xlsx = write_xlsx(self.root / 'book.xlsx')
        book = self.create(xlsx.name, xlsx.read_bytes())
        pptx = make_pptx(self.root / 'slides.pptx')
        slides = self.create(pptx.name, pptx.read_bytes())
        self.assertEqual(self.dispatch('read_attachment_page', {'artifact_id': word['artifact_id'], 'render_pages': False})['text'], 'offline')
        self.assertEqual(self.dispatch('read_spreadsheet', {'artifact_id': book['artifact_id'], 'selections': [{'sheet_index': 1, 'range': 'A1'}]})['cells'][0]['value'], '42')
        self.assertEqual(self.dispatch('read_presentation', {'artifact_id': slides['artifact_id'], 'slides': [1]})['slides'][0]['text'], 'synthetic slide')
        self.assertIsNone(self.broker._client)
        self.assertEqual(self.provider.calls, [])

    def test_same_client_socket_reconnect_preserves_issued_artifact(self):
        thread = threading.Thread(target=self.broker.serve_forever); thread.start()
        self.assertTrue(self.broker.wait_until_ready(timeout=3))
        owner = BrokerClient(socket_path=self.broker._socket_path, policy=POLICY, client_id=A, restart_callback=lambda: None)
        reconnect = BrokerClient(socket_path=self.broker._socket_path, policy=POLICY, client_id=A, restart_callback=lambda: None)
        foreign = BrokerClient(socket_path=self.broker._socket_path, policy=POLICY, client_id=B, restart_callback=lambda: None)
        try:
            made = owner.create_local_artifact(CreateLocalArtifactRequest(file_name='private.txt', content='same client'))
            request = ReadAttachmentRequest(artifact_id=made.artifact_id)
            self.assertEqual(reconnect.read_attachment(request).text, 'same client')
            self.assertEqual(foreign.read_attachment(request).status, 'expired')
        finally:
            self.broker.shutdown(); thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_store_and_registry_redemption_cannot_bypass_dispatch_admission(self):
        made = self.create()
        payload = {'artifact_id': made['artifact_id'], 'recipient': 7,
            'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''}
        prepared = self.dispatch('prepare_artifact_send', payload)
        self.assertEqual(prepared['status'], 'prepared')
        # Exercise internal registry re-lookups under trusted dispatch scopes,
        # including an ID absent from the dispatch payload.
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        with self.broker._artifact_authority.scope(B, self.store):
            self.assertIsNone(self.store.lookup(made['artifact_id']))
            with self.assertRaises(DraftError):
                self.broker._drafts.prepare(owner=DraftOwner(B, 17), **payload)
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'pending')
        self.dispatch('release_client', {})
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        self.assertEqual(self.dispatch('send_prepared_artifact', {'draft_id': prepared['draft_id'], 'approved': True})['status'], 'expired')
        # New grants cannot revive a prior immutable draft binding.
        self.create(data=b'new')
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')

    def test_observed_drift_cannot_resurrect_pending_artifact_draft(self):
        made = self.create()
        prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
        self.assertEqual(prepared['status'], 'prepared')
        self.provider.account = 18
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')
        self.provider.account = 17
        self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['text'], 'private evidence')

    def test_release_during_download_and_upload_finish_never_publishes(self):
        for operation in ('get_attachment', 'finish_local_upload'):
            with self.subTest(operation=operation):
                entered = threading.Event(); resume = threading.Event(); results = []
                if operation == 'get_attachment':
                    original = self.provider.download_file
                    payload = {'anchor': {'chat_id': 7, 'message_id': 8}}
                    target, name = self.provider, 'download_file'
                else:
                    token = self.begin(); self.append(token)
                    original = self.store._store_bytes
                    payload = {'upload_id': token}
                    target, name = self.store, '_store_bytes'
                def paused(*args, **kwargs):
                    entered.set(); self.assertTrue(resume.wait(3))
                    return original(*args, **kwargs)
                with patch.object(target, name, side_effect=paused):
                    worker = threading.Thread(target=lambda: results.append(self.dispatch(operation, payload)))
                    worker.start(); self.assertTrue(entered.wait(2))
                    self.dispatch('release_client', {})
                    resume.set(); worker.join(3); self.assertFalse(worker.is_alive())
                self.assertEqual(results[0]['status'], 'error')
                for field in ('artifact_id', 'artifact_path', 'sha256', 'file_name'):
                    self.assertIsNone(results[0][field])
                if operation == 'finish_local_upload':
                    self.assertEqual(self.dispatch(operation, payload)['status'], 'error')
                    self.assertNotIn(token, self.broker._uploads._entries)
                self.assertEqual(self.broker._artifact_authority._grants, {})

    def test_fixed_artifact_and_upload_ttls_outlive_reader_idle(self):
        wall = [100.]
        self.store.clock = lambda: wall[0]
        self.broker._artifact_authority.clock = self.store.clock
        self.broker._uploads._clock = self.store.clock
        made = self.create(data=b'fixed expiry')
        token = self.begin()
        with self.broker._artifact_authority.scope(A, self.store):
            original_expiry = self.store.lookup(made['artifact_id']).expires_at
        self.assertEqual(original_expiry, 43300.)
        upload_expiry = self.broker._uploads._entries[token].expires_at
        self.assertEqual(upload_expiry, 1000.)
        wall[0] = 401.; self.now[0] += 301.
        self.assertEqual(self.append(token)['status'], 'accepted')
        with self.broker._artifact_authority.scope(A, self.store):
            self.assertEqual(self.store.lookup(made['artifact_id']).expires_at, original_expiry)
        self.assertEqual(self.broker._uploads._entries[token].expires_at, upload_expiry)
        wall[0] = 1000.
        self.assertEqual(self.dispatch('finish_local_upload', {'upload_id': token})['status'], 'error')
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['text'], 'fixed expiry')
        wall[0] = 43300.
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['status'], 'expired')

    def test_derived_image_frame_voice_inherit_owner_without_metadata(self):
        from telegram_search_mcp.document_reader import DocumentReadResult
        from telegram_search_mcp.media_analyzer import MediaAnalysis, MediaFrame
        from telegram_search_mcp.outgoing_voice import VoicePrepared
        derived = []
        made = self.create()
        image_root = self.root / 'rendered'; image_root.mkdir()
        image = image_root / 'page.png'; image.write_bytes(b'synthetic image bytes')
        with patch('telegram_search_mcp.broker.read_document', return_value=DocumentReadResult(
                status='complete', text='page', processed_bytes=16, total_bytes=16,
                processed_pages=1, total_pages=1, image_paths=(image,))):
            response = self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})
            derived.append(response['images'][0]['artifact_id'])
        video = self.create('clip.mp4', b'inert media')
        frame_root = self.root / 'frames'; frame_root.mkdir()
        frame = frame_root / 'frame.png'; frame.write_bytes(b'inert frame')
        with patch('telegram_search_mcp.broker.analyze_media', return_value=MediaAnalysis(
                status='complete', duration_seconds=1., window_start_seconds=0., window_end_seconds=1.,
                transcription_status='no_audio', frame_status='complete',
                frames=(MediaFrame(0., frame, 'time'),), temp_directory=frame_root)):
            response = self.dispatch('analyze_media', {'artifact_id': video['artifact_id']})
            derived.append(response['frames'][0]['artifact_id'])
        audio = self.create('input.wav', b'inert audio')
        voice = self.root / 'voice.ogg'; voice.write_bytes(b'OggSsynthetic')
        def convert(source, store, **kwargs):
            return VoicePrepared(store.store(voice, kind='media'), source.sha256, 1, 'AAAA', True)
        with patch('telegram_search_mcp.broker.prepare_voice_note', side_effect=convert):
            prepared = self.dispatch('prepare_artifact_send', {'artifact_id': audio['artifact_id'],
                'recipient': 7, 'kind': 'voice_note', 'display_name': 'input.wav', 'mime_type': 'audio/wav', 'caption': ''})
            self.assertEqual(prepared['status'], 'prepared')
            derived.append(self.broker._drafts._entries[prepared['draft_id']].draft.artifact_id)
        for token in derived:
            # Derived previews may now have metadata, but ownership precedes its use.
            with self.broker._artifact_authority.scope(B, self.store):
                self.assertIsNone(self.store.lookup(token))
            with self.broker._artifact_authority.scope(A, self.store):
                self.assertIsNotNone(self.store.lookup(token))
        self.dispatch('release_client', {})
        for token in derived:
            with self.broker._artifact_authority.scope(A, self.store):
                self.assertIsNone(self.store.lookup(token))

    def test_release_during_voice_conversion_does_not_publish_draft_or_derived_grant(self):
        from telegram_search_mcp.outgoing_voice import VoicePrepared
        made = self.create('input.wav', b'inert audio')
        output = self.root / 'output.ogg'; output.write_bytes(b'OggSsynthetic')
        entered = threading.Event(); resume = threading.Event(); results = []
        def convert(source, store, **kwargs):
            entered.set(); self.assertTrue(resume.wait(3))
            return VoicePrepared(store.store(output, kind='media'), source.sha256, 1, 'AAAA', True)
        with patch('telegram_search_mcp.broker.prepare_voice_note', side_effect=convert):
            payload = {'artifact_id': made['artifact_id'], 'recipient': 7, 'kind': 'voice_note',
                'display_name': 'input.wav', 'mime_type': 'audio/wav', 'caption': ''}
            worker = threading.Thread(target=lambda: results.append(self.dispatch('prepare_artifact_send', payload)))
            worker.start(); self.assertTrue(entered.wait(2))
            self.dispatch('release_client', {})
            resume.set(); worker.join(3); self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'error'); self.assertIsNone(results[0]['draft_id'])
        self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assertEqual(self.broker._artifact_authority._grants, {})

    def test_account_drift_terminal_draft_responses_remain_closed(self):
        for operation in ('get_send_status', 'get_reply_artifact_draft', 'refresh_reply_artifact_draft'):
            with self.subTest(operation=operation):
                self.provider.account = 17
                made = self.create()
                prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
                    'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
                calls = [0]
                def drift():
                    calls[0] += 1
                    return 17 if calls[0] == 1 else 18
                with patch.object(self.provider, 'get_account_id', side_effect=drift):
                    result = self.dispatch(operation, {'draft_id': prepared['draft_id']})
                self.assertEqual(result['status'], 'outcome_unknown' if operation == 'get_send_status' else 'unavailable')
                self.assertFalse(result.get('reply')); self.assertFalse(result.get('message_id'))

    def test_download_authorization_block_preserves_content_free_blocked_status(self):
        from telegram_search_mcp.tdjson import AuthorizationBlocked
        with patch.object(self.provider, 'ensure_ready', side_effect=AuthorizationBlocked('not ready')):
            result = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['file_name']); self.assertIsNone(result['artifact_id'])

    def test_observed_authorization_loss_revokes_provider_grants_without_resurrection(self):
        from telegram_search_mcp.tdjson import AuthorizationBlocked
        made = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
        self.assertEqual(made['status'], 'complete')
        with patch.object(self.provider, 'ensure_ready', side_effect=AuthorizationBlocked('not ready')):
            result = self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})
        self.assertEqual(result['status'], 'expired'); self.assertFalse(result['text'])
        self.assertEqual(self.dispatch('read_attachment', {'artifact_id': made['artifact_id']})['status'], 'expired')

    def test_new_framed_handshake_denies_prior_generation_bytes_and_upload_token(self):
        from telegram_search_mcp.schemas import (BeginLocalUploadRequest, AppendLocalUploadRequest,
            PrepareArtifactSendRequest)
        thread = threading.Thread(target=self.broker.serve_forever); thread.start()
        self.assertTrue(self.broker.wait_until_ready(timeout=3))
        proxy = BrokerClient(socket_path=self.broker._socket_path, policy=POLICY, client_id=A, restart_callback=lambda: None)
        made = proxy.create_local_artifact(CreateLocalArtifactRequest(file_name='private.txt', content='prior generation'))
        token = proxy.begin_local_upload(BeginLocalUploadRequest(file_name='private.txt', kind='document',
            size_bytes=3, sha256=hashlib.sha256(b'abc').hexdigest())).upload_id
        old_generation = self.broker._generation
        self.broker.shutdown(); thread.join(3); self.assertFalse(thread.is_alive())
        with patch('telegram_search_mcp.broker.LocalUploadRegistry', side_effect=partial(LocalUploadRegistry, root=self.root / 'new-uploads')):
            replacement = Broker(socket_path=self.broker._socket_path, policy=POLICY,
                artifact_store=ArtifactStore(cache_dir=self.store.cache_dir), verify_peer_uid=False,
                client_factory=lambda: self.provider)
        newer = threading.Thread(target=replacement.serve_forever); newer.start()
        try:
            self.assertTrue(replacement.wait_until_ready(timeout=3))
            self.assertNotEqual(old_generation, replacement._generation)
            reconnect = BrokerClient(socket_path=replacement._socket_path, policy=POLICY, client_id=A, restart_callback=lambda: None)
            self.assertEqual(reconnect.read_attachment(ReadAttachmentRequest(artifact_id=made.artifact_id)).status, 'expired')
            self.assertEqual(reconnect.prepare_artifact_send(PrepareArtifactSendRequest(artifact_id=made.artifact_id,
                recipient=7, display_name='private.txt', mime_type='text/plain', caption='')).status, 'error')
            self.assertEqual(reconnect.append_local_upload(AppendLocalUploadRequest(upload_id=token, index=0,
                content_base64='YWJj', sha256=hashlib.sha256(b'abc').hexdigest())).status, 'error')
            self.assertTrue(Path(made.artifact_path).exists())
            self.assertEqual(self.provider.calls, [])
        finally:
            replacement.shutdown(); newer.join(3); self.assertFalse(newer.is_alive())

    def test_draft_observed_auth_loss_revokes_downloaded_derived_and_draft_authority(self):
        from telegram_search_mcp.tdjson import AuthorizationBlocked, TDLibError
        from telegram_search_mcp.document_reader import DocumentReadResult
        for method, failure in [('ensure_ready', AuthorizationBlocked('not ready')),
                                ('get_account_id', TDLibError('lost identity'))]:
            with self.subTest(method=method):
                downloaded = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
                offline = self.create(data=b'offline grant')
                folder = self.root / method; folder.mkdir()
                image = folder / 'page.png'; image.write_bytes(b'inert page')
                with patch('telegram_search_mcp.broker.read_document', return_value=DocumentReadResult(
                        status='complete', image_paths=(image,), total_pages=1)):
                    derived = self.dispatch('read_attachment', {'artifact_id': downloaded['artifact_id']})['images'][0]['artifact_id']
                prepared = self.dispatch('prepare_artifact_send', {'artifact_id': offline['artifact_id'],
                    'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
                self.assertEqual(prepared['status'], 'prepared')
                with patch.object(self.provider, method, side_effect=failure):
                    refused = self.dispatch('get_draft', {'draft_id': prepared['draft_id']})
                self.assertEqual(refused['status'], 'unavailable')
                self.assertEqual(self.dispatch('read_attachment', {'artifact_id': downloaded['artifact_id']})['status'], 'expired')
                with self.broker._artifact_authority.scope(A, self.store):
                    self.assertIsNone(self.store.lookup(derived))
                self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'unavailable')
                self.assertEqual(self.dispatch('read_attachment', {'artifact_id': offline['artifact_id']})['text'], 'offline grant')

    def test_provider_account_work_never_runs_inside_registry_or_state_locks(self):
        made = self.create()
        prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
        downloaded = self.dispatch('get_attachment', {'anchor': {'chat_id': 7, 'message_id': 8}})
        original = self.provider.get_account_id
        def unlocked_account():
            self.assertFalse(self.broker._drafts._lock.locked(), 'provider account work holds registry lock')
            self.assertFalse(self.broker._state_lock._is_owned(), 'provider account work holds broker state lock')
            self.assertFalse(self.broker._artifact_authority.lock._is_owned(), 'provider account work holds authority lock')
            return original()
        with patch.object(self.provider, 'get_account_id', side_effect=unlocked_account):
            self.assertEqual(self.dispatch('get_draft', {'draft_id': prepared['draft_id']})['status'], 'pending')
            self.assertEqual(self.dispatch('read_attachment', {'artifact_id': downloaded['artifact_id']})['text'], 'abc')
            revised = self.dispatch('update_draft', {'draft_id': prepared['draft_id'], 'caption': 'new'})
            self.assertEqual(revised['status'], 'revised')

    def test_registry_peek_completes_while_another_thread_holds_broker_state(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner
        made = self.create()
        prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
        held = threading.Event(); release = threading.Event(); captured = []
        def hold_state():
            with self.broker._state_lock:
                held.set(); release.wait(3)
        holder = threading.Thread(target=hold_state); holder.start(); self.assertTrue(held.wait(2))
        original = self.broker._artifact_authority.account_lookup
        def forbid_inversion():
            acquired = self.broker._state_lock.acquire(blocking=False)
            if acquired: self.broker._state_lock.release()
            if self.broker._drafts._lock.locked() and not acquired:
                raise AssertionError('registry-to-state account lock inversion')
            return original()
        def peek():
            try:
                with self.broker._artifact_authority.scope(A, self.store) as scope:
                    self.broker._artifact_authority.observe(scope, 17)
                    captured.append(self.broker._drafts.peek(prepared['draft_id'], owner=DraftOwner(A, 17)))
            except Exception as error:
                captured.append(error)
        try:
            with patch.object(self.broker._artifact_authority, 'account_lookup', side_effect=forbid_inversion):
                worker = threading.Thread(target=peek); worker.start(); worker.join(2)
                self.assertFalse(worker.is_alive(), 'registry peek waited for held broker state')
                self.assertEqual(len(captured), 1)
                self.assertFalse(isinstance(captured[0], Exception), str(captured[0]))
                self.assertEqual(captured[0].caption, '')
        finally:
            release.set(); holder.join(3)

    def test_release_or_observed_drift_during_staging_blocks_unissued_send_and_records_claim(self):
        from telegram_search_mcp.outgoing_stage import stage_approved_document
        self.broker._approval_prompt = lambda **kwargs: True
        for revocation in ('release', 'drift'):
            with self.subTest(revocation=revocation):
                self.provider.account = 17
                made = self.create()
                prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
                    'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
                entered = threading.Event(); resume = threading.Event(); sent = []; results = []; errors = []
                def stage(claim):
                    entered.set(); self.assertTrue(resume.wait(3))
                    return stage_approved_document(claim, root=self.root / ('outgoing-' + revocation))
                def provider_send(recipient, path, caption, *, attempt_id=None, pre_send_fence=None):
                    with pre_send_fence() if pre_send_fence is not None else nullcontext():
                        sent.append((recipient, path.read_bytes(), caption))
                    return 701
                self.provider.send_document_message = provider_send
                def worker_body():
                    try:
                        results.append(self.dispatch('send_prepared_artifact', {'draft_id': prepared['draft_id'], 'approved': True}))
                    except Exception as error: errors.append(error)
                with patch('telegram_search_mcp.broker.stage_approved_document', side_effect=stage), \
                     patch('telegram_search_mcp.broker.retire_staged_document'):
                    worker = threading.Thread(target=worker_body); worker.start(); self.assertTrue(entered.wait(2))
                    if revocation == 'release': self.dispatch('release_client', {})
                    else:
                        self.provider.account = 18
                        self.dispatch('get_draft', {'draft_id': prepared['draft_id']})
                    resume.set(); worker.join(3); self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(sent, [], 'revocation acknowledged before dispatch still reached provider')
                # A consumed claim never reads as expired: its content-free local failure is replayed.
                local = {'status': 'failed', 'draft_id': prepared['draft_id'], 'recipient': None, 'message_id': None,
                         'detail': 'local preparation failed; no provider send was attempted'}
                self.assertEqual(results[0], local)
                entry = self.broker._drafts._entries[prepared['draft_id']]
                self.assertEqual(entry.state, 'finished')
                self.assertEqual((entry.receipt.status, entry.receipt.evidence), ('failed', 'local_failed'))
                self.provider.account = 17
                self.assertEqual(self.dispatch('get_send_status', {'draft_id': prepared['draft_id']})['status'], 'outcome_unknown')
                self.assertEqual(self.dispatch('send_prepared_artifact', {'draft_id': prepared['draft_id'], 'approved': True})['status'], 'expired')
                self.assertEqual(sent, [])

    def test_issued_send_retains_original_terminal_or_unknown_after_revocation_without_disclosure(self):
        from telegram_search_mcp.tdjson import MessageSendOutcomeUnknown
        from telegram_search_mcp.outgoing_stage import stage_approved_document
        self.broker._approval_prompt = lambda **kwargs: True
        for revocation, result_kind in [('release', 'sent'), ('drift', 'sent'), ('release', 'unknown')]:
            with self.subTest(revocation=revocation, result_kind=result_kind):
                self.provider.account = 17
                made = self.create()
                prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
                    'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
                issued = threading.Event(); confirm = threading.Event(); sent = []; results = []; errors = []; revoked = []
                def provider_send(recipient, path, caption, *, attempt_id=None, pre_send_fence=None):
                    with pre_send_fence() if pre_send_fence is not None else nullcontext():
                        sent.append((recipient, path.read_bytes(), caption))
                    issued.set(); self.assertTrue(confirm.wait(4))
                    if result_kind == 'unknown': raise MessageSendOutcomeUnknown('lost confirmation')
                    return 701
                self.provider.send_document_message = provider_send
                def worker_body():
                    try: results.append(self.dispatch('send_prepared_artifact', {'draft_id': prepared['draft_id'], 'approved': True}))
                    except Exception as error: errors.append(error)
                def revoke():
                    try:
                        if revocation == 'release': revoked.append(self.dispatch('release_client', {}))
                        else:
                            self.provider.account = 18
                            revoked.append(self.dispatch('get_draft', {'draft_id': prepared['draft_id']}))
                    except Exception as error: errors.append(error)
                with patch('telegram_search_mcp.broker.stage_approved_document',
                           side_effect=partial(stage_approved_document, root=self.root / 'issued-stage')), \
                     patch('telegram_search_mcp.broker.retire_staged_document'):
                    worker = threading.Thread(target=worker_body); worker.start(); self.assertTrue(issued.wait(2))
                    control = threading.Thread(target=revoke); control.start(); control.join(1)
                    try:
                        self.assertFalse(control.is_alive(), 'release waited for provider confirmation')
                        self.assertEqual(len(revoked), 1)
                    finally:
                        confirm.set(); worker.join(3); control.join(3)
                self.assertEqual(errors, []); self.assertFalse(worker.is_alive())
                self.assertEqual(len(sent), 1)
                entry = self.broker._drafts._entries[prepared['draft_id']]
                self.assertEqual(entry.state, 'finished', 'consumed claim lost original completion evidence')
                self.assertEqual(entry.receipt.status, 'sent' if result_kind == 'sent' else 'outcome_unknown')
                self.assertEqual(entry.receipt.message_id, 701 if result_kind == 'sent' else None)
                self.assertEqual(entry.owner.account_id, 17)
                # An issued attempt never reads as expired and discloses no provider evidence.
                unknown = {'status': 'outcome_unknown', 'draft_id': prepared['draft_id'], 'recipient': None,
                           'message_id': None, 'detail': 'current receipt authority is unavailable; do not retry automatically',
                           'next_action': 'check_send_status'}
                self.assertEqual(results[0], unknown)
                self.provider.account = 17
                self.assertEqual(self.dispatch('get_send_status', {'draft_id': prepared['draft_id']})['status'], 'outcome_unknown')
                self.assertEqual(self.dispatch('send_prepared_artifact', {'draft_id': prepared['draft_id'], 'approved': True})['status'], 'expired')
                self.assertEqual(len(sent), 1)

    def test_revoked_completion_requires_original_claim_identity(self):
        from dataclasses import replace
        from telegram_search_mcp.outgoing_drafts import DraftOwner, DraftError
        made = self.create()
        prepared = self.dispatch('prepare_artifact_send', {'artifact_id': made['artifact_id'],
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain', 'caption': ''})
        owner = DraftOwner(A, 17)
        with self.broker._artifact_authority.scope(A, self.store) as scope:
            self.broker._artifact_authority.observe(scope, 17)
            claim = self.broker._drafts.claim(prepared['draft_id'], owner=owner, approved=True,
                provider=self.provider, provider_epoch=self.provider.send_observation_epoch)
            self.broker._artifact_authority.release(A)
            with self.assertRaises(DraftError):
                self.broker._drafts.finish(prepared['draft_id'], owner=owner,
                    claim=replace(claim, claim_token=object()), status='sent', message_id=701)
            receipt = self.broker._drafts.finish(prepared['draft_id'], owner=owner,
                claim=claim, status='sent', message_id=701)
            self.assertEqual((receipt.status, receipt.message_id), ('sent', 701))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': prepared['draft_id']})['status'], 'outcome_unknown')

    def test_raw_unbound_fixture_is_not_adopted_by_prepare_or_fresh_issuance(self):
        source = self.root / 'unbound.txt'; source.write_bytes(b'unbound private bytes')
        unbound = self.store.store(source)
        payload = {'artifact_id': unbound.artifact_id, 'recipient': 7,
            'display_name': 'unbound.txt', 'mime_type': 'text/plain', 'caption': ''}
        self.provider.calls.clear()
        self.assertEqual(self.dispatch('prepare_artifact_send', payload)['status'], 'error')
        reply = self.dispatch('prepare_reply_artifact_send',
            dict(payload, reply_to={'chat_id': 7, 'message_id': 8}))
        self.assertEqual(reply['status'], 'unavailable'); self.assertIsNone(reply['reply'])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.broker._drafts.known_ids(), set())
        with self.broker._artifact_authority.scope(A, self.store):
            issued = self.store.store(source)
        self.assertNotEqual(issued.artifact_id, unbound.artifact_id)
        prepared = self.dispatch('prepare_artifact_send', dict(payload, artifact_id=issued.artifact_id))
        self.assertEqual(prepared['status'], 'prepared')
        self.assertEqual(prepared['sha256'], hashlib.sha256(b'unbound private bytes').hexdigest())
        self.assertEqual(self.dispatch('prepare_artifact_send', payload)['status'], 'error')
        self.assertNotIn(unbound.artifact_id, self.broker._artifact_authority._grants)
