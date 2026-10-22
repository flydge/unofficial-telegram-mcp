"""Absolute captions through real Broker, registry, staging and TDJSON.

Only raw provider events and owner callback are synthetic. Fixtures are local
DATA; no historical acceptance helper, native library or live Telegram runs.
"""
import copy
import functools
import json
import tempfile
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_artifact_drafts import (
    GetReplyArtifactDraftRequest, PrepareReplyArtifactSendRequest,
    RefreshReplyArtifactDraftRequest, UpdateReplyArtifactDraftRequest,
)
from telegram_search_mcp.reply_drafts import (
    GetReplyDraftRequest, PrepareReplyTextSendRequest, RefreshReplyDraftRequest,
    UpdateReplyDraftRequest, source_from_message,
)
from telegram_search_mcp.schemas import SendPreparedArtifactRequest, SendPreparedTextRequest
from telegram_search_mcp.tdjson import TDLibClient
from test_reply_datetime_media import caption, entity, provider_content
from test_text_replies import disabled
from test_reply_absolute_datetime_media import (
    CHAT, MESSAGE, KINDS, PRECISIONS, BASE_CAPS, RELATIVE, absolute, span, provider, file_of,
)

CLIENT = 'client_' + 'a' * 32
CAPABILITIES = ('send', 'reply_text_send', 'reply_artifact_send', *BASE_CAPS,
                'reply_lexical_targets', 'reply_identity_targets')
SOURCE_CAPABILITIES = CAPABILITIES[3:]


def mixed(kind):
    return provider(kind, caption('soon#tag@user', [span(0, 4),
        entity(4, 4, 'textEntityTypeHashtag'), entity(8, 5, 'textEntityTypeMentionName', user_id=53)]))


class ClosedRaw:
    def __init__(self):
        self.source = mixed('document')
        self.requests = []
        self.events = deque()
        self.allowed_send = False
        self.staging = None
        self.staged_bytes = []

    def send(self, request):
        self.requests.append(copy.deepcopy(request))
        kind = request['@type']
        if kind == 'getAuthorizationState': result = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe': result = {'@type': 'user', 'id': 61}
        elif kind == 'getChat':
            if request['chat_id'] != CHAT: raise AssertionError('unexpected chat')
            result = {'@type': 'chat', 'id': CHAT, 'title': 'Synthetic Absolute media room',
                      'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 73, 'is_channel': False}}
        elif kind == 'getMessage':
            if (request['chat_id'], request['message_id']) != (CHAT, MESSAGE): raise AssertionError('unexpected source')
            result = copy.deepcopy(self.source)
        elif kind == 'getMessageProperties': result = {'@type': 'messageProperties', 'can_be_replied': True}
        elif kind == 'sendMessage' and self.allowed_send:
            outgoing = request['input_message_content']
            artifact = outgoing['@type'] == 'inputMessageDocument'
            if artifact:
                path = Path(outgoing['document']['document']['path'])
                if not path.is_relative_to(self.staging): raise AssertionError('send escaped staging')
                self.staged_bytes.append(path.read_bytes())
                content = provider_content('document', outgoing['caption'])
            else:
                content = {'@type': 'messageText', 'text': copy.deepcopy(outgoing['text']),
                           'link_preview': None, 'link_preview_options': copy.deepcopy(outgoing['link_preview_options'])}
            msg = {'@type': 'message', 'chat_id': CHAT, 'id': -991, 'is_outgoing': True,
                'topic_id': None, 'content': content, 'sending_state': {'@type': 'messageSendingStatePending',
                    'sending_id': request['options']['sending_id']},
                'reply_to': {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': MESSAGE,
                    'quote': None, 'checklist_task_id': 0, 'poll_option_id': '',
                    'origin_send_date': 0, 'origin': None, 'content': None}}
            self.events.append({**msg, '@extra': request['@extra']})
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -991,
                                'message': {**copy.deepcopy(msg), 'id': 4096, 'sending_state': None}})
            return
        else: raise AssertionError('raw effect forbidden: ' + kind)
        self.events.append({**result, '@extra': request['@extra']})

    def receive(self, timeout):
        if self.events: return self.events.popleft()
        if timeout != 0: raise AssertionError('no synthetic provider wait')
        return None

    def close(self): pass


class AbsoluteCaptionBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='absolute-caption-')
        self.root = Path(self.temporary.name)
        self.raw = ClosedRaw()
        self.provider = TDLibClient(raw=self.raw, session_directory=self.root / 'unused-session',
            credential_loader=lambda: self.fail('credentials forbidden'))
        self.provider._ready = True
        self.dialogs = []; self.decision = lambda: True
        def owner(**kwargs):
            self.dialogs.append(copy.deepcopy(kwargs)); return self.decision()
        self.store = ArtifactStore(cache_dir=self.root / 'artifacts')
        payload = self.root / 'fixture.txt'; payload.write_bytes(b'data')
        self.artifact = self.store.store(payload)
        self.broker = Broker(socket_path=self.root / 'unused.sock', client_factory=lambda: self.provider,
            artifact_store=self.store, approval_prompt=owner, policy=RuntimePolicy(enabled_capabilities=CAPABILITIES))
        self.stack = ExitStack(); self.staging = self.root / 'staging'; self.raw.staging = self.staging
        self.stack.enter_context(patch('telegram_search_mcp.tdjson.STAGING_ROOT', self.staging))
        self.stack.enter_context(patch('telegram_search_mcp.broker.stage_approved_document',
            functools.partial(stage_approved_document, root=self.staging)))
        self.stack.enter_context(patch('telegram_search_mcp.broker.retire_staged_document',
            functools.partial(retire_staged_document, root=self.staging)))

    def tearDown(self):
        self.stack.close(); self.broker.shutdown(); self.broker._executor.shutdown(wait=True)
        self.provider.close(); self.temporary.cleanup()

    def prepare(self, family):
        anchor = {'chat_id': CHAT, 'message_id': MESSAGE}
        if family == 'text': return self.broker._prepare_reply_text_send(PrepareReplyTextSendRequest(
            recipient=CHAT, text='Plain', reply_to=anchor), client_id=CLIENT)
        return self.broker._prepare_reply_artifact_send(PrepareReplyArtifactSendRequest(recipient=CHAT,
            artifact_id=self.artifact.artifact_id, display_name='fixture.txt', mime_type='text/plain',
            kind='document', caption='Plain', reply_to=anchor), client_id=CLIENT)

    def inspect(self, draft_id, family):
        if family == 'text': return self.broker._get_reply_draft(GetReplyDraftRequest(draft_id=draft_id), client_id=CLIENT)
        return self.broker._get_reply_artifact_draft(GetReplyArtifactDraftRequest(draft_id=draft_id), client_id=CLIENT)

    def send(self, draft_id, family, approved=True):
        if family == 'text': return self.broker._send_prepared_text(SendPreparedTextRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)
        return self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)

    def revise(self, draft_id, family, refresh=False):
        if family == 'text':
            request = RefreshReplyDraftRequest(draft_id=draft_id) if refresh else UpdateReplyDraftRequest(draft_id=draft_id, text='Changed')
            return self.broker._revise_reply_draft(request, client_id=CLIENT)
        request = RefreshReplyArtifactDraftRequest(draft_id=draft_id) if refresh else UpdateReplyArtifactDraftRequest(draft_id=draft_id, caption='Changed')
        return self.broker._revise_reply_artifact_draft(request, client_id=CLIENT)

    def prepared(self, family):
        result = self.prepare(family)
        self.assertEqual(result.status, 'prepared', 'complete Absolute captions must reach immutable broker draft')
        self.assertIn('"show_day_of_week":false', result.reply.reply_target.text)
        return result

    def no_effects(self):
        self.assertFalse(any(r['@type'] == 'sendMessage' for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def reads(self): return sum(r['@type'] == 'getMessage' for r in self.raw.requests)

    def test_all_five_source_capabilities_missing_or_revoked_refuse_both_reply_families(self):
        # Break: missing conjunction at prepare/inspect/update/refresh/send/owner approval.
        for source_kind in KINDS:
            self.raw.source = mixed(source_kind)
            for family in ('text', 'artifact'):
                for missing in SOURCE_CAPABILITIES:
                    denied = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                    self.broker._policy = denied
                    self.assertEqual(disabled(self.prepare(family)),('capability_disabled',[missing]))
                    self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                    draft = self.prepared(family).reply.draft.draft_id
                    self.broker._policy = denied; reads = self.reads(); dialogs = len(self.dialogs)
                    with self.subTest(source=source_kind, family=family, missing=missing):
                        self.assertEqual(disabled(self.inspect(draft, family)),('capability_disabled',[missing]))
                        self.assertEqual(disabled(self.revise(draft, family)),('capability_disabled',[missing]))
                        self.assertEqual(self.reads(), reads)
                        # A refresh reads the source once and names what that newly read source needs (0.54).
                        self.assertEqual(disabled(self.revise(draft, family, True)),('capability_disabled',[missing]))
                        self.assertEqual(self.send(draft, family).status, 'expired')
                        self.assertEqual(self.reads(), reads + 1); self.assertEqual(len(self.dialogs), dialogs)
                    self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                    draft = self.prepared(family).reply.draft.draft_id
                    def revoke(): self.broker._policy = denied; return True
                    self.decision = revoke
                    with self.subTest(source=source_kind, family=family, owner_revoked=missing):
                        self.assertEqual(self.send(draft, family).status, 'expired')
        self.no_effects()

    def test_precision_weekday_timestamp_rawtext_stablemedia_drift_terminal_before_send(self):
        # Break: approval digest ignores a nested format/media fact before TDJSON send.
        for source_kind in KINDS:
            for family in ('text', 'artifact'):
                for mutation in ('time_precision', 'date_precision', 'show_day_of_week', 'timestamp', 'text', 'stable', 'open', 'relative'):
                    self.raw.source = mixed(source_kind); self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                    prepared = self.prepared(family); draft = prepared.reply.draft.draft_id
                    def drift():
                        typ = self.raw.source['content']['caption']['entities'][0]['type']; fmt = typ['formatting_type']
                        if mutation in ('time_precision', 'date_precision'): fmt[mutation]['@type'] = PRECISIONS[0]
                        elif mutation == 'show_day_of_week': fmt[mutation] = True
                        elif mutation == 'timestamp': typ['unix_time'] += 1
                        elif mutation == 'text': self.raw.source['content']['caption']['text'] = 'late#tag@user'
                        elif mutation == 'stable': file_of(self.raw.source, source_kind)['remote']['unique_id'] = 'changed'
                        elif mutation == 'open': fmt['date_precision']['extra'] = None
                        else: typ['formatting_type'] = copy.deepcopy(RELATIVE)
                        return True
                    self.decision = drift
                    with self.subTest(source=source_kind, family=family, mutation=mutation):
                        self.assertEqual(self.send(draft, family).status, 'failed')
                        key = 'reply_preview' if family == 'text' else 'reply_artifact_preview'
                        self.assertEqual(self.dialogs[-1][key], prepared.reply.model_dump(mode='json'))
                        self.assertEqual(self.send(draft, family).status, 'failed')
                        self.assertEqual(self.inspect(draft, family).status, 'unavailable')
        self.no_effects()

    def test_update_no_reread_refresh_v14_v12_v10_and_denials_preserve_binding(self):
        # Break: update rereads source, refresh retains approval, callerFalse/ownerdeny cause effects.
        for source_kind in KINDS:
            for family in ('text', 'artifact'):
                self.raw.source = mixed(source_kind); original = self.prepared(family)
                old = original.reply.draft.draft_id
                self.raw.source['content']['caption']['entities'][0]['type']['formatting_type'] = copy.deepcopy(RELATIVE)
                reads = self.reads(); updated = self.revise(old, family)
                self.assertEqual(updated.status, 'revised'); self.assertEqual(updated.reply.reply_target, original.reply.reply_target)
                self.assertNotEqual(updated.reply.preview_sha256, original.reply.preview_sha256)
                self.assertEqual(self.reads(), reads); self.assertEqual(self.send(old, family).status, 'expired')
                live = updated.reply
                for fmt, version, visible in ((RELATIVE, 12, '"dateTimeFormattingTypeRelative"'),
                    (None, 10, '"formatting_type":null'), (absolute(weekday=True), 14, '"show_day_of_week":true')):
                    self.raw.source['content']['caption']['entities'][0]['type']['formatting_type'] = copy.deepcopy(fmt)
                    self.assertEqual(json.loads(source_from_message(self.raw.source, CHAT, MESSAGE).projection_json)['version'], version)
                    reads = self.reads(); refreshed = self.revise(live.draft.draft_id, family, True)
                    self.assertEqual(refreshed.status, 'revised'); self.assertEqual(self.reads(), reads + 1)
                    self.assertIn(visible, refreshed.reply.reply_target.text)
                    self.assertNotEqual(refreshed.reply.reply_target.source_sha256, live.reply_target.source_sha256)
                    self.assertNotEqual(refreshed.reply.preview_sha256, live.preview_sha256)
                    self.assertEqual(self.send(live.draft.draft_id, family).status, 'expired'); live = refreshed.reply
                dialogs = len(self.dialogs)
                self.assertEqual(self.send(live.draft.draft_id, family, False).status, 'declined')
                self.assertEqual(len(self.dialogs), dialogs)
                self.decision = lambda: False
                self.assertEqual(self.send(live.draft.draft_id, family).status, 'declined')
                self.assertEqual(self.inspect(live.draft.draft_id, family).reply, live)
        self.no_effects()

    def test_exact_plain_send_at_most_once_staging_retired_and_minimal_authority(self):
        # Break: source entities emitted, extra authority required, replay or staged artifact survives.
        self.raw.allowed_send = True
        for source_kind in KINDS:
            for family in ('text', 'artifact'):
                self.raw.source = provider(source_kind)
                self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES[:6])
                self.decision = lambda: True
                prepared = self.prepared(family); draft = prepared.reply.draft.draft_id
                before = len([r for r in self.raw.requests if r['@type'] == 'sendMessage'])
                result = self.send(draft, family)
                with self.subTest(source=source_kind, family=family):
                    self.assertEqual(result.status, 'sent')
                    requests = [r for r in self.raw.requests if r['@type'] == 'sendMessage']
                    self.assertEqual(len(requests), before + 1)
                    request = requests[-1]; content = request['input_message_content']
                    self.assertEqual(request['chat_id'], CHAT); self.assertIsNone(request['topic_id'])
                    self.assertEqual(request['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': MESSAGE,
                        'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
                    self.assertEqual(content['text' if family == 'text' else 'caption'],
                        {'@type': 'formattedText', 'text': 'Plain', 'entities': []})
                    self.assertEqual(self.send(draft, family).status, 'sent')
                    self.assertEqual(len([r for r in self.raw.requests if r['@type'] == 'sendMessage']), before + 1)
                    self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))
                    key = 'reply_preview' if family == 'text' else 'reply_artifact_preview'
                    self.assertEqual(self.dialogs[-1][key], prepared.reply.model_dump(mode='json'))
        self.assertEqual(self.raw.staged_bytes, [b'data'] * 3)
