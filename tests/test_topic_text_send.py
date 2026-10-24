"""Topic authority through production draft/broker/provider/confirmation paths."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
import subprocess
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.schemas import PrepareTextSendRequest, PrepareTextSendResponse
from telegram_search_mcp.tdjson import TDLibClient, _validate_bounded_request, ForbiddenTDLibRequest

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
CHAT = -1007
TOPIC = {'kind': 'forum', 'id': 41}
BODY = 'Exact topic text 🙂'
POLICY = RuntimePolicy(enabled_capabilities=('send', 'topic_text_send'))


def metadata(**changes):
    return {'@type': 'forumTopic', 'info': {
        '@type': 'forumTopicInfo', 'chat_id': CHAT, 'forum_topic_id': 41,
        'name': 'Selected topic', 'creation_date': 1700000000,
        'creator_id': {'@type': 'messageSenderUser', 'user_id': 7},
        'is_general': False, 'is_outgoing': True, 'is_closed': False,
        'is_hidden': False, **changes}}


def outgoing(identifier=-10, **changes):
    return {'@type': 'message', 'id': identifier, 'chat_id': CHAT,
        'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41},
        'is_outgoing': True, 'reply_to': None,
        'content': {'@type': 'messageText', 'text': {
            '@type': 'formattedText', 'text': BODY, 'entities': []}}, **changes}


class Raw:
    """Only transport is synthetic; safety code and reducer remain real."""
    def __init__(self):
        self.events = deque()
        self.sent = []
        self.account = 7
        self.chat_id = CHAT
        self.chat_type = {'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': False}
        self.has_topics = True
        self.topic = metadata()
        self.title = 'Synthetic forum'
        self.forum = True
        self.mode = 'sent'
        self.final_topic = {'@type': 'messageTopicForum', 'forum_topic_id': 41}
        self.fail_receive = False
        self.last_tempid = -9

    def send(self, request):
        self.sent.append(copy.deepcopy(request))
        kind = request['@type']
        if kind == 'getAuthorizationState':
            value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            value = {'@type': 'chat', 'id': request['chat_id'], 'title': self.title,
                'type': copy.deepcopy(self.chat_type)}
        elif kind == 'getSupergroup':
            value = {'@type': 'supergroup', 'id': 7, 'is_forum': self.forum}
        elif kind == 'getUser':
            value = {'@type': 'user', 'id': request['user_id'],
                     'type': {'@type': 'userTypeBot', 'has_topics': self.has_topics}}
        elif kind == 'getForumTopic':
            value = copy.deepcopy(self.topic)
        elif kind == 'sendMessage':
            self.last_tempid -= 1
            preliminary = outgoing(self.last_tempid, chat_id=self.chat_id, sending_state={'@type': 'messageSendingStatePending',
                'sending_id': request['options']['sending_id']})
            if self.mode == 'wrong_preliminary':
                preliminary['topic_id']['forum_topic_id'] = 42
            if self.mode == 'lost_before_reply':
                self.fail_receive = True
                return
            self.events.append({**preliminary, '@extra': request['@extra']})
            if self.mode == 'lost':
                self.fail_receive = True
                return
            final = outgoing(701, chat_id=self.chat_id, topic_id=copy.deepcopy(self.final_topic), sending_state=None)
            if self.mode == 'unexpected_reply':
                final['reply_to'] = {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': 9}
            elif self.mode == 'unexpected_entities':
                final['content']['text']['entities'] = [{'offset': 0, 'length': 1,
                    'type': {'@type': 'textEntityTypeBold'}}]
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': self.last_tempid, 'message': final})
            return
        else:
            raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        if self.events:
            return self.events.popleft()
        if self.fail_receive:
            self.fail_receive = False
            raise OSError('synthetic response loss')
        return None

    def close(self):
        pass


class TopicTextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.raw = Raw()
        self.provider = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=root/'broker.sock',
            artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=POLICY,
            client_factory=lambda: self.provider,
            approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload,
            'client_id': client, 'deadline': time.monotonic()+10,
            'broker_generation': self.broker._generation})

    def prepare(self):
        result = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})
        self.assertEqual(result['status'], 'prepared', result)
        return result

    def send(self, draft_id, approved=True):
        return self.dispatch('send_prepared_text', {'draft_id': draft_id, 'approved': approved})

    def sends(self):
        return [r for r in self.raw.sent if r['@type'] == 'sendMessage']

    def test_topic_selector_is_strict_and_capability_is_default_off(self):
        self.assertIn('topic_text_send', KNOWN_CAPABILITIES)
        self.assertNotIn('topic_text_send', RuntimePolicy().enabled_capabilities)
        request = PrepareTextSendRequest(recipient=CHAT, text=BODY, topic=TOPIC)
        self.assertEqual(request.topic.id, 41)
        for value in (True, 0, -1, 2**31, '41', 1.0):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PrepareTextSendRequest(recipient=CHAT, text=BODY, topic={'kind': 'forum', 'id': value})
        for value in ({'kind': 'other', 'id': 41}, {**TOPIC, 'extra': 1}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PrepareTextSendRequest(recipient=CHAT, text=BODY, topic=value)

    def test_prepare_freezes_target_and_full_preview_without_sending(self):
        preview = self.prepare()
        self.assertEqual(preview['account_id'], 7)
        self.assertEqual(preview['topic']['forum_topic_id'], 41)
        self.assertEqual(preview['topic']['chat_id'], CHAT)
        self.assertIn('[untrusted Telegram evidence]', preview['topic']['name'])
        self.assertEqual(preview['sha256'], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(len(preview['preview_sha256']), 64)
        self.raw.topic = metadata(name='Changed later')
        got = self.dispatch('get_draft', {'draft_id': preview['draft_id']})
        self.assertEqual(got['draft']['topic'], preview['topic'])
        self.assertEqual(got['draft']['preview_sha256'], preview['preview_sha256'])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.approvals, [])

    def test_complete_preview_rejects_cross_field_tampering(self):
        preview = self.prepare()
        for key, value in [('account_id', 8), ('recipient', -1008),
                           ('text', 'Other text'), ('preview_sha256', '0'*64)]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                PrepareTextSendResponse.model_validate({**preview, key: value})
        for path, value in [('forum_topic_id', 42), ('name', '[untrusted Telegram evidence] Other')]:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                PrepareTextSendResponse.model_validate({**preview, 'topic': {**preview['topic'], path: value}})
        for key, value in [('recipient_title', 'Changed chat'), ('expires_at', '2030-01-01T00:00:00Z')]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                PrepareTextSendResponse.model_validate({**preview, key: value})

    def test_topic_response_rejects_coercion_and_unknown_fields(self):
        preview = self.prepare()
        for change in ({'recipient': str(CHAT)}, {'approval_required': 1},
                {'approval_required': 'true'}, {'account_id': '7'}, {'unreviewed': True}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                PrepareTextSendResponse.model_validate({**preview, **change})

    def test_owner_denial_does_not_disclose_stale_topic_recipient(self):
        preview = self.prepare()
        def decline(**kwargs):
            self.raw.account = 8
            self.broker._policy = RuntimePolicy()
            return False
        self.broker._approval_prompt = decline
        result = self.send(preview['draft_id'])
        self.assertEqual(result['status'], 'declined')
        self.assertIsNone(result['recipient'])
        self.assertEqual(self.sends(), [])

    def test_renamed_recipient_during_authority_drift_is_content_free(self):
        for change in ('unchanged', 'policy', 'account', 'provider', 'epoch'):
            with self.subTest(change=change):
                self.broker._client = self.provider
                self.broker._policy = POLICY
                self.raw.account = 7
                preview = self.prepare()
                receive = self.raw.receive
                other = TDLibClient(raw=Raw())
                self.addCleanup(other.close)
                renamed = []
                def drift(timeout):
                    event = receive(timeout)
                    if event and event.get('@type') == 'chat':
                        renamed.append(True)
                        event = {**event, 'title': 'Renamed forum'}
                        if change == 'policy':
                            self.broker._policy = RuntimePolicy(enabled_capabilities=('send',))
                        elif change == 'account':
                            self.raw.account = 8
                        elif change == 'provider':
                            self.broker._client = other
                        elif change == 'epoch':
                            self.provider._invalidate_send_observations()
                    return event
                with patch.object(self.raw, 'receive', side_effect=drift), \
                        patch.object(self.broker._drafts, 'claim', wraps=self.broker._drafts.claim) as claim, \
                        patch.object(self.provider._send_observations, 'register',
                                     wraps=self.provider._send_observations.register) as register:
                    result = self.send(preview['draft_id'])
                self.assertEqual(renamed, [True])
                self.assertEqual(result['status'], 'expired')
                self.assertEqual(result['detail'], 'draft is unavailable or no longer valid')
                self.assertIsNone(result['recipient'])
                self.assertIsNone(result['message_id'])
                self.assertEqual(self.approvals, [])
                claim.assert_not_called()
                register.assert_not_called()
                self.assertEqual(self.sends(), [])
        self.broker._client = self.provider
        self.broker._policy = POLICY
        self.raw.account = 7
        ordinary = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY})
        self.assertEqual(ordinary['status'], 'prepared')
        self.raw.title = 'Renamed forum'
        result = self.send(ordinary['draft_id'])
        self.assertEqual(result['status'], 'recipient_changed')
        self.assertEqual(result['recipient'], CHAT)
        self.assertEqual(self.approvals, [])
        self.assertEqual(self.sends(), [])

    def test_digest_binds_the_complete_reviewed_wire_contract(self):
        preview = self.prepare()
        options = {'@type': 'messageSendOptions', 'suggested_post_info': None,
            'disable_notification': False, 'from_background': False, 'protect_content': False,
            'allow_paid_broadcast': False, 'paid_message_star_count': 0,
            'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
            'effect_id': 0, 'only_preview': False}
        link = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
            'force_small_media': False, 'force_large_media': False, 'show_above_text': False}
        value = {'version': 1, 'draft_id': preview['draft_id'], 'account_id': 7,
            'recipient': CHAT, 'recipient_title': preview['recipient_title'],
            'expires_at': datetime.fromisoformat(preview['expires_at']).astimezone(timezone.utc).isoformat(),
            'kind': 'text', 'approval_required': True, 'topic': preview['topic'],
            'text': BODY, 'sha256': hashlib.sha256(BODY.encode()).hexdigest(),
            'size_bytes': len(BODY.encode()), 'entities': [], 'reply_to': None,
            'link_preview_options': link, 'send_options': options, 'reply_markup': None, 'clear_draft': False}
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(preview['preview_sha256'], hashlib.sha256(encoded).hexdigest())

    def test_equivalent_display_names_keep_distinct_raw_authority(self):
        self.raw.topic = metadata(name='Ａ  B')
        a = self.prepare()['topic']
        self.raw.topic = metadata(name='A B')
        b = self.prepare()['topic']
        self.assertEqual(a['name'], b['name'])
        self.assertNotEqual(a['source_sha256'], b['source_sha256'])

    def test_metadata_refusals_never_prompt_or_send(self):
        variants = [dict(is_closed=True), dict(is_hidden=True), dict(is_closed=1),
            dict(is_general='false'), dict(chat_id=-1008), dict(forum_topic_id=42),
            dict(creation_date=0), dict(name=''), dict(creator_id=None)]
        for changes in variants:
            with self.subTest(changes=changes):
                self.raw.topic = metadata(**changes)
                result = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})
                self.assertEqual(result['status'], 'error')
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.approvals, [])

    def test_strict_metadata_unavailable_and_native_scope_matrix(self):
        variants = [dict(forum_topic_id=True), dict(chat_id=str(CHAT)), dict(creation_date=True),
            dict(creation_date='1700000000'), dict(creation_date=2**31), dict(is_outgoing=None),
            dict(name='x'*257), dict(creator_id={'@type': 'messageSenderUser', 'user_id': True})]
        raw_values = [metadata(**v) for v in variants]
        missing = metadata(); missing['info'].pop('is_general'); raw_values.append(missing)
        raw_values.extend([{'@type': 'forumTopic', 'info': None}, {'@type': 'other'},
            {'@type': 'error', 'code': 404, 'message': 'synthetic unavailable'}])
        for raw in raw_values:
            with self.subTest(raw=raw):
                self.raw.topic = raw
                result = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})
                self.assertEqual(result['status'], 'error')
        self.raw.topic = metadata()
        self.raw.forum = False
        self.assertEqual(self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})['status'], 'error')
        self.raw.forum = True
        for kind in ({'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': True},
                     {'@type': 'chatTypeBasicGroup', 'basic_group_id': 7},
                     {'@type': 'chatTypeSecret', 'secret_chat_id': 7}):
            with self.subTest(kind=kind):
                self.raw.chat_type = kind
                self.assertEqual(self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})['status'], 'error')
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.approvals, [])

    def test_topics_enabled_bot_uses_same_exact_topic_contract(self):
        self.raw.chat_id = 19
        self.raw.chat_type = {'@type': 'chatTypePrivate', 'user_id': 19}
        self.raw.topic = metadata(chat_id=19)
        result = self.dispatch('prepare_text_send', {'recipient': 19, 'text': BODY, 'topic': TOPIC})
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(result['topic']['chat_type'], 'topics_bot')
        self.assertEqual(self.send(result['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends()[0]['chat_id'], 19)
        self.raw.has_topics = False
        self.assertEqual(self.dispatch('prepare_text_send', {'recipient': 19, 'text': BODY, 'topic': TOPIC})['status'], 'error')

    def test_denied_capability_never_hydrates_or_prompts(self):
        self.broker._policy = RuntimePolicy()
        result = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(any(r['@type'] == 'getForumTopic' for r in self.raw.sent))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.approvals, [])

    def test_both_approval_layers_are_required(self):
        preview = self.prepare()
        from telegram_search_mcp.approval_prompt import ApprovalDecision
        self.assertEqual(self.send(preview['draft_id'], False)['status'], 'declined')
        self.assertEqual(self.approvals, [])
        self.broker._approval_prompt = lambda **kw: False
        self.assertEqual(self.send(preview['draft_id'])['status'], 'declined')
        for outcome in ('timeout', 'dialog_unavailable'):
            self.broker._approval_prompt = lambda outcome=outcome, **kw: ApprovalDecision(outcome)
            result = self.send(preview['draft_id'])
            self.assertEqual((result['status'], result['recipient']), (outcome, None))
        self.assertEqual(self.sends(), [])

    def test_exact_wire_and_owner_evidence_and_receipt_replay(self):
        preview = self.prepare()
        result = self.send(preview['draft_id'])
        self.assertEqual(result['status'], 'sent', result)
        self.assertEqual(result['message_id'], 701)
        wire = self.sends()[0]
        self.assertEqual(wire['topic_id'], {'@type': 'messageTopicForum', 'forum_topic_id': 41})
        self.assertIsNone(wire['reply_to'])
        self.assertIsNone(wire['reply_markup'])
        self.assertEqual(wire['input_message_content']['text']['entities'], [])
        self.assertTrue(wire['input_message_content']['link_preview_options']['is_disabled'])
        self.assertEqual(self.approvals[0]['topic_preview']['preview_sha256'], preview['preview_sha256'])
        self.assertEqual(self.approvals[0]['topic_preview']['topic'], preview['topic'])
        self.assertEqual(self.approvals[0]['topic_preview']['account_id'], 7)
        self.assertEqual(self.approvals[0]['topic_preview']['text'], BODY)
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_changed_target_before_owner_review_refuses(self):
        preview = self.prepare()
        self.raw.topic = metadata(name='Different topic name')
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.approvals, [])
        self.assertEqual(self.sends(), [])

    def test_topic_drift_during_owner_prompt_refuses(self):
        preview = self.prepare()
        def approve(**kwargs):
            self.raw.topic = metadata(is_closed=True)
            return True
        self.broker._approval_prompt = approve
        self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
        self.assertEqual(self.sends(), [])

    def test_preparation_provider_epoch_and_instance_are_bound(self):
        preview = self.prepare()
        self.provider._invalidate_send_observations()
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.approvals, [])
        self.assertEqual(self.sends(), [])
        preview = self.prepare()
        other_raw = Raw()
        other_provider = TDLibClient(raw=other_raw)
        self.addCleanup(other_provider.close)
        self.broker._client = other_provider
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.approvals, [])
        self.assertFalse(any(r['@type'] == 'sendMessage' for r in other_raw.sent))

    def test_epoch_changes_during_prompt_and_registration_prevent_send(self):
        preview = self.prepare()
        def approve(**kwargs):
            self.provider._invalidate_send_observations()
            return True
        self.broker._approval_prompt = approve
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.broker._approval_prompt = lambda **kw: True
        preview = self.prepare()
        register = self.provider._send_observations.register
        def reset(*args, **kwargs):
            value = register(*args, **kwargs)
            self.provider._invalidate_send_observations()
            return value
        with patch.object(self.provider._send_observations, 'register', side_effect=reset):
            self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
        self.assertEqual(self.sends(), [])

    def test_cancellation_and_revision_during_prompt_prevent_send(self):
        for operation, extras in [('cancel_draft', {}), ('update_draft', {'text': 'Replacement text'})]:
            with self.subTest(operation=operation):
                preview = self.prepare()
                def approve(**kwargs):
                    self.dispatch(operation, {'draft_id': preview['draft_id'], **extras})
                    return True
                self.broker._approval_prompt = approve
                self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends(), [])

    def test_expiry_before_prompt_and_after_registration_prevents_transport(self):
        preview = self.prepare()
        clock = self.broker._drafts._clock
        self.broker._drafts._clock = lambda: clock()+1000
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.approvals, [])
        self.broker._drafts._clock = clock
        preview = self.prepare()
        register = self.provider._send_observations.register
        def expire(*args, **kwargs):
            value = register(*args, **kwargs)
            self.broker._drafts._clock = lambda: clock()+1000
            return value
        with patch.object(self.provider._send_observations, 'register', side_effect=expire):
            self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
        self.assertEqual(self.sends(), [])

    def test_two_concurrent_sends_show_one_dialog_and_make_one_transport_attempt(self):
        # Review fix (057, low): two owner approvals of one draft can no longer happen; the first send holds the draft
        # from just before its dialog until its claim, even past the one-dialog gate bypassed here.
        preview = self.prepare()
        gate, dialogs = threading.Event(), []
        def approve(**kwargs):
            dialogs.append(kwargs)
            gate.wait(timeout=5)
            return True
        self.broker._approval_prompt = approve
        self.broker._owner_dialog = lambda **facts: self.broker._approval_prompt(**facts)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.send, preview['draft_id']) for _ in range(2)]
            refused = next(as_completed(futures, timeout=8)).result()  # Returns while the dialog is open.
            gate.set()
            outcomes = sorted(f.result(timeout=8)['status'] for f in futures)
        self.assertEqual((refused['status'], outcomes), ('dialog_unavailable', ['dialog_unavailable', 'sent']))
        self.assertEqual((len(dialogs), len(self.sends())), (1, 1))

    def test_revocation_after_registration_refuses_transport(self):
        preview = self.prepare()
        register = self.provider._send_observations.register
        def revoke(*args, **kwargs):
            value = register(*args, **kwargs)
            self.broker._policy = RuntimePolicy()
            return value
        with patch.object(self.provider._send_observations, 'register', side_effect=revoke):
            self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
        self.assertEqual(self.sends(), [])
        self.assertIsNone(self.provider._send_observations.snapshot(preview['draft_id']))

    def test_revision_preserves_topic_and_invalidates_prior_authority(self):
        preview = self.prepare()
        updated = self.dispatch('update_draft', {'draft_id': preview['draft_id'], 'text': 'New exact text'})
        self.assertEqual(updated['status'], 'revised', updated)
        self.assertEqual(updated['draft']['topic'], preview['topic'])
        self.assertNotEqual(updated['draft']['preview_sha256'], preview['preview_sha256'])
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.dispatch('refresh_draft', {'draft_id': updated['draft']['draft_id']})['status'], 'unavailable')

    def test_cancel_foreign_owner_and_capability_disclosure(self):
        preview = self.prepare()
        self.assertEqual(self.dispatch('get_draft', {'draft_id': preview['draft_id']}, FOREIGN)['status'], 'unavailable')
        self.broker._policy = RuntimePolicy()
        for op in ('get_draft', 'cancel_draft', 'refresh_draft'):
            with self.subTest(op=op):
                self.assertEqual(self.dispatch(op, {'draft_id': preview['draft_id']})['status'], 'unavailable')
        self.assertEqual(self.dispatch('list_drafts', {})['status'], 'unavailable')
        self.broker._policy = POLICY
        self.assertEqual(self.dispatch('cancel_draft', {'draft_id': preview['draft_id']})['status'], 'cancelled')
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(self.sends(), [])

    def test_final_wrong_topic_cannot_confirm_or_retry(self):
        for value in (None, {'@type': 'messageTopicForum', 'forum_topic_id': 42},
                      {'@type': 'messageTopicForum', 'forum_topic_id': True},
                      {'@type': 'messageTopicForum', 'forum_topic_id': 41, 'extra': 1}):
            with self.subTest(value=value):
                preview = self.prepare()
                self.raw.final_topic = value
                before = len(self.sends())
                self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
                self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
                self.assertEqual(len(self.sends()), before+1)

    def test_wrong_topic_preliminary_and_recovery_do_not_gain_authority(self):
        preview = self.prepare()
        self.raw.mode = 'wrong_preliminary'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
        self.assertEqual(len(self.sends()), 1)
        preview = self.prepare()
        self.raw.mode = 'lost_before_reply'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
        sending_id = self.sends()[-1]['options']['sending_id']
        wrong = outgoing(self.raw.last_tempid, topic_id={'@type': 'messageTopicForum', 'forum_topic_id': 42},
            sending_state={'@type': 'messageSendingStatePending', 'sending_id': sending_id})
        self.raw.events.append({'@type': 'updateNewMessage', 'message': wrong})
        self.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': self.raw.last_tempid,
            'message': outgoing(703, sending_state=None)})
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': preview['draft_id']})['status'], 'outcome_unknown')
        self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
        self.assertEqual(len(self.sends()), 2)

    def test_topic_policy_revocation_hides_status_and_terminal_replay(self):
        preview = self.prepare()
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.broker._policy = RuntimePolicy()
        result = self.dispatch('get_send_status', {'draft_id': preview['draft_id']})
        self.assertEqual((result['status'], result['message_id']), ('outcome_unknown', None))
        self.assertNotEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_plain_topic_does_not_confirm_unexpected_reply_or_entities(self):
        for mode in ('unexpected_reply', 'unexpected_entities'):
            with self.subTest(mode=mode):
                preview = self.prepare()
                self.raw.mode = mode
                self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')

    def test_lost_response_late_exact_success_does_not_retry(self):
        preview = self.prepare()
        self.raw.mode = 'lost'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
        self.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
            'message': outgoing(702, sending_state=None)})
        result = self.dispatch('get_send_status', {'draft_id': preview['draft_id']})
        self.assertEqual((result['status'], result['message_id']), ('sent', 702))
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_lost_preliminary_recovers_only_from_exact_pending_and_final(self):
        preview = self.prepare()
        self.raw.mode = 'lost_before_reply'
        self.assertEqual(self.send(preview['draft_id'])['status'], 'outcome_unknown')
        sending_id = self.sends()[0]['options']['sending_id']
        self.raw.events.extend([
            {'@type': 'updateNewMessage', 'message': outgoing(sending_state={
                '@type': 'messageSendingStatePending', 'sending_id': sending_id})},
            {'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                'message': outgoing(704, sending_state=None)}])
        result = self.dispatch('get_send_status', {'draft_id': preview['draft_id']})
        self.assertEqual((result['status'], result['message_id']), ('sent', 704))
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_apply_cannot_replace_frozen_topic_or_digest(self):
        preview = self.prepare()
        for extras in ({'topic': {'kind': 'forum', 'id': 42}}, {'preview_sha256': '0'*64},
                       {'reply_to': {'chat_id': CHAT, 'message_id': 1}}):
            with self.subTest(extras=extras), self.assertRaises(ValidationError):
                self.dispatch('send_prepared_text', {'draft_id': preview['draft_id'], 'approved': True, **extras})
        self.assertEqual(self.sends(), [])

    def test_topic_and_account_drift_after_registration_prevent_transport(self):
        register = self.provider._send_observations.register
        for account_drift in (False, True):
            with self.subTest(account_drift=account_drift):
                self.raw.account = 7
                self.raw.topic = metadata()
                preview = self.prepare()
                def drift(*args, **kwargs):
                    value = register(*args, **kwargs)
                    if account_drift:
                        self.raw.account = 8
                    else:
                        self.raw.topic = metadata(is_closed=True)
                    return value
                with patch.object(self.provider._send_observations, 'register', side_effect=drift):
                    self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
                self.assertIsNone(self.provider._send_observations.snapshot(preview['draft_id']))
        self.assertEqual(self.sends(), [])

    def test_deadline_after_registration_prevents_transport(self):
        preview = self.prepare()
        register = self.provider._send_observations.register
        original = time.monotonic
        def expire(*args, **kwargs):
            value = register(*args, **kwargs)
            self.provider._request_context.deadline = original()-1
            return value
        with patch.object(self.provider._send_observations, 'register', side_effect=expire):
            self.assertEqual(self.send(preview['draft_id'])['status'], 'failed')
        self.assertEqual(self.sends(), [])

    def test_topic_wire_rejects_implicit_conversion_and_unreviewed_options(self):
        preview = self.prepare()
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        wire = {key: value for key, value in self.sends()[0].items() if key != '@extra'}
        _validate_bounded_request(wire)
        # The approved topic-reply route now shares this strict wire boundary.
        reply_wire = copy.deepcopy(wire)
        reply_wire['reply_to'] = {'@type': 'inputMessageReplyToMessage', 'message_id': 9,
            'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}
        _validate_bounded_request(reply_wire)
        variants = [(['topic_id', 'forum_topic_id'], True),
            (['topic_id', 'forum_topic_id'], '41'), (['topic_id', 'forum_topic_id'], 2**31),
            (['chat_id'], True), (['chat_id'], 2**53), (['options', 'disable_notification'], 0),
            (['options', 'paid_message_star_count'], False), (['options', 'only_preview'], 0),
            (['input_message_content', 'link_preview_options', 'is_disabled'], 1),
            (['input_message_content', 'clear_draft'], 0),
            (['input_message_content', 'text', 'entities'], [{'offset': 0, 'length': 1, 'type': {'@type': 'textEntityTypeBold'}}]),
            (['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': True,
                'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}),
            (['reply_markup'], {}), (['topic_id', 'extra'], 1)]
        for path, value in variants:
            candidate = copy.deepcopy(wire)
            node = candidate
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(candidate)

    def test_owner_dialog_renders_full_bound_topic_and_denies_tampering(self):
        from telegram_search_mcp.approval_prompt import confirm_approved_send
        preview = self.prepare()
        captured = []
        self.broker._approval_prompt = lambda **kwargs: captured.append(kwargs) or False
        self.assertEqual(self.send(preview['draft_id'])['status'], 'declined')
        kwargs = captured[0]
        completed = subprocess.CompletedProcess([], 0, b'APPROVED\n')
        name = preview['topic']['name'].removeprefix('[untrusted Telegram evidence] ')
        with patch('telegram_search_mcp.approval_prompt.subprocess.run', return_value=completed) as run:
            self.assertTrue(confirm_approved_send(**kwargs))
            shown = run.call_args.args[0][-1]
            for evidence in (f'\n│ «{BODY}»\n', 'Account: 7', 'normal notifications',
                    f'\nTopic [untrusted Telegram evidence]: «{name}» (topic 41)\n', preview['topic']['source_sha256'],
                    preview['preview_sha256'], preview['draft_id'], preview['sha256']):
                self.assertIn(evidence, shown)
        changed = copy.deepcopy(kwargs)
        changed['topic_preview']['topic']['forum_topic_id'] = 42
        with patch('telegram_search_mcp.approval_prompt.subprocess.run') as run:
            self.assertFalse(confirm_approved_send(**changed))
            self.assertFalse(confirm_approved_send(**{**kwargs, 'text': 'Other text'}))
            run.assert_not_called()

    def test_broker_client_binds_requested_topic_text_and_recipient(self):
        from telegram_search_mcp.broker_client import BrokerClient
        preview = self.prepare()
        proxy = BrokerClient(socket_path=Path(self.temporary.name)/'unused.sock', policy=POLICY, restart_callback=lambda: None)
        self.addCleanup(proxy.close)
        request = PrepareTextSendRequest(recipient=CHAT, text=BODY, topic=TOPIC)
        with patch.object(proxy, '_request', return_value=preview):
            self.assertEqual(proxy.prepare_text_send(request).status, 'prepared')
            for foreign_request in (PrepareTextSendRequest(recipient=CHAT, text=BODY),
                    PrepareTextSendRequest(recipient=CHAT, text='Other', topic=TOPIC),
                    PrepareTextSendRequest(recipient=CHAT-1, text=BODY, topic=TOPIC),
                    PrepareTextSendRequest(recipient=CHAT, text=BODY, topic={'id': 42})):
                self.assertEqual(proxy.prepare_text_send(foreign_request).status, 'error')
        disabled = BrokerClient(socket_path=Path(self.temporary.name)/'unused.sock', policy=RuntimePolicy(), restart_callback=lambda: None)
        self.addCleanup(disabled.close)
        with patch.object(disabled, '_request') as request_mock:
            self.assertEqual(disabled.prepare_text_send(request).status, 'error')
            request_mock.assert_not_called()

    def test_final_receive_authority_change_hides_receipt_without_retry(self):
        for change in ('policy', 'account', 'provider', 'epoch'):
            with self.subTest(change=change):
                self.broker._client = self.provider
                self.broker._policy = POLICY
                self.raw.account = 7
                preview = self.prepare()
                receive = self.raw.receive
                other = TDLibClient(raw=Raw())
                self.addCleanup(other.close)
                def drift(timeout):
                    event = receive(timeout)
                    if event and event.get('@type') == 'updateMessageSendSucceeded':
                        if change == 'policy':
                            self.broker._policy = RuntimePolicy()
                        elif change == 'account':
                            self.raw.account = 8
                        elif change == 'epoch':
                            self.provider._invalidate_send_observations()
                            self.raw.fail_receive = True
                        else:
                            self.broker._client = other
                    return event
                with patch.object(self.raw, 'receive', side_effect=drift):
                    result = self.send(preview['draft_id'])
                self.assertNotEqual(result['status'], 'sent')
                self.assertIsNone(result['message_id'])
                before = len(self.sends())
                self.send(preview['draft_id'])
                self.assertEqual(len(self.sends()), before)

    def test_valid_final_after_preparation_expiry_keeps_retained_attempt_evidence(self):
        preview = self.prepare()
        receive = self.raw.receive
        draft_clock = self.broker._drafts._clock
        def late(timeout):
            event = receive(timeout)
            if event and event.get('@type') == 'updateMessageSendSucceeded':
                self.broker._drafts._clock = lambda: draft_clock()+1000
            return event
        with patch.object(self.raw, 'receive', side_effect=late):
            result = self.send(preview['draft_id'])
        self.assertEqual((result['status'], result['message_id']), ('sent', 701))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': preview['draft_id']})['status'], 'sent')
        self.assertEqual(self.send(preview['draft_id'])['status'], 'sent')
        self.assertEqual(len(self.sends()), 1)

    def test_topic_snapshot_lock_wait_respects_aggregate_deadline(self):
        from threading import Event, Thread
        from telegram_search_mcp.tdjson import TDLibDeadlineExceeded
        held, release = Event(), Event()
        def hold():
            with self.provider._serialized_provider_call():
                held.set()
                release.wait(0.8)
        worker = Thread(target=hold)
        worker.start()
        self.assertTrue(held.wait(1))
        started = time.monotonic()
        try:
            with self.provider.request_budget(started+0.03):
                with self.assertRaises(TDLibDeadlineExceeded):
                    self.provider.read_topic_snapshot(CHAT, 41,
                        expected_account_id=7, expected_recipient_title='Synthetic forum')
            self.assertLess(time.monotonic()-started, 0.5)
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.approvals, [])


class PublicTopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_proxy_broker_topic_lifecycle_and_strict_errors(self):
        from mcp import Client
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.server import build_server
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = Raw()
            provider = TDLibClient(raw=raw)
            confirmations = []
            broker = Broker(socket_path=root/'broker.sock', lock_path=root/'broker.lock',
                artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=POLICY,
                client_factory=lambda: provider,
                approval_prompt=lambda **kwargs: confirmations.append(kwargs) or True)
            thread = threading.Thread(target=broker.serve_forever)
            thread.start()
            proxy = BrokerClient(socket_path=root/'broker.sock', policy=POLICY,
                client_id=CLIENT, restart_callback=lambda: None)
            try:
                self.assertTrue(broker.wait_until_ready(timeout=2))
                async with Client(build_server(service_factory=lambda: proxy, policy=POLICY)) as consumer:
                    listed = {t.name: t for t in (await consumer.list_tools()).tools}
                    self.assertIn('topic', listed['prepare_text_send'].input_schema['properties'])
                    for topic in ({'id': True}, {'id': '41'}, {'id': 0}, {'id': 41, 'extra': 1}):
                        result = await consumer.call_tool('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': topic})
                        self.assertTrue(result.is_error)
                    self.assertEqual(raw.sent, [])
                    result = await consumer.call_tool('prepare_text_send', {'recipient': CHAT, 'text': BODY, 'topic': TOPIC})
                    prepared = result.structured_content
                    self.assertEqual(prepared['status'], 'prepared')
                    did = prepared['draft_id']
                    got = (await consumer.call_tool('get_draft', {'draft_id': did})).structured_content
                    self.assertEqual(got['draft']['preview_sha256'], prepared['preview_sha256'])
                    self.assertEqual((await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': False})).structured_content['status'], 'declined')
                    self.assertEqual(confirmations, [])
                    self.assertTrue((await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True, 'topic': TOPIC})).is_error)
                    self.assertEqual((await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True})).structured_content['status'], 'sent')
                    self.assertEqual((await consumer.call_tool('get_send_status', {'draft_id': did})).structured_content['status'], 'sent')
                    self.assertEqual((await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True})).structured_content['status'], 'sent')
                    self.assertEqual(len([r for r in raw.sent if r['@type'] == 'sendMessage']), 1)
            finally:
                proxy.close()
                broker.shutdown()
                thread.join(3)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
