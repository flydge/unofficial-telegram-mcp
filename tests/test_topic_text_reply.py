"""Exact topic and reply identity must survive one attempt.

Transport and owner are synthetic; models, broker, registry, native validation,
observations and SDK paths exercise the production implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_drafts import DraftOwner
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, ReplySource, source_from_message
from telegram_search_mcp.tdjson import TDLibClient, _validate_bounded_request

CLIENT = 'client_' + 'a' * 24
CHAT = -1007
TOPIC = {'kind': 'forum', 'id': 41}
NATIVE_TOPIC = {'@type': 'messageTopicForum', 'forum_topic_id': 41}
ANCHOR = {'chat_id': CHAT, 'message_id': 55}
BODY = 'Exact topic reply 🙂'
CAPS = ('send', 'reply_text_send', 'topic_text_send', 'reply_formatted_targets')
SEND_OPTIONS = {'@type': 'messageSendOptions', 'suggested_post_info': None,
  'disable_notification': False, 'from_background': False, 'protect_content': False,
  'allow_paid_broadcast': False, 'paid_message_star_count': 0,
  'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
  'effect_id': 0, 'only_preview': False}
LINK_OPTIONS = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
  'force_small_media': False, 'force_large_media': False, 'show_above_text': False}


def canonical(value):
  return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def expected_digest(preview):
  # Independent, literal approved v1/v2 projection; no production digest builder.
  facts = {'version': 2 if preview['draft'].get('topic') is not None else 1,
    'draft': preview['draft'], 'reply_target': preview['reply_target'],
    'topic_id': ({'@type': 'messageTopicForum', 'forum_topic_id': preview['draft']['topic']['forum_topic_id']}
      if preview['draft'].get('topic') is not None else None), 'outgoing_entities': [], 'send_options': SEND_OPTIONS,
    'link_preview_options': LINK_OPTIONS, 'clear_draft': False, 'reply_markup': None,
    'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': preview['reply_target']['anchor']['message_id'],
          'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}}
  return hashlib.sha256(canonical(facts).encode()).hexdigest()


def topic_metadata():
  return {'@type': 'forumTopic', 'info': {'@type': 'forumTopicInfo',
    'chat_id': CHAT, 'forum_topic_id': 41, 'name': 'Selected topic',
    'creation_date': 1700000000, 'creator_id': {'@type': 'messageSenderUser', 'user_id': 7},
    'is_general': False, 'is_outgoing': True, 'is_closed': False, 'is_hidden': False}}


def source_message(styled=False):
  entities = [{'@type': 'textEntity', 'offset': 0, 'length': 6,
        'type': {'@type': 'textEntityTypeBold'}}] if styled else []
  return {'@type': 'message', 'chat_id': CHAT, 'id': 55,
    'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
    'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
    'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
    'topic_id': copy.deepcopy(NATIVE_TOPIC),
    'content': {'@type': 'messageText', 'text': {'@type': 'formattedText',
          'text': 'Source full text', 'entities': entities}}}


def reply_metadata():
  return {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': 55,
      'checklist_task_id': 0, 'poll_option_id': '', 'origin_send_date': 0}


def outgoing(identifier, **changes):
  return {'@type': 'message', 'id': identifier, 'chat_id': CHAT, 'is_outgoing': True,
    'topic_id': copy.deepcopy(NATIVE_TOPIC), 'reply_to': reply_metadata(),
    'content': {'@type': 'messageText', 'text': {'@type': 'formattedText',
          'text': BODY, 'entities': []}}, **changes}


class Raw:
  """Same native topic metadata + full reply source, no credentials or network."""
  def __init__(self, styled=False):
    self.sent = []; self.events = deque(); self.source = source_message(styled)
    self.topic = topic_metadata(); self.account = 7; self.title = 'Synthetic forum'
    self.can_reply = True; self.mode = 'sent'; self.closed = False
    self.fail_receive = False; self.last_tempid = -9
    self.request_hook = None; self.final_hook = None
    self.preliminary_mutation = None; self.final_mutation = None

  def send(self, request):
    self.sent.append(copy.deepcopy(request))
    if self.request_hook is not None:
      self.request_hook(request)
    kind = request['@type']
    if kind == 'getAuthorizationState': value = {'@type': 'authorizationStateReady'}
    elif kind == 'getMe': value = {'@type': 'user', 'id': self.account}
    elif kind == 'getChat':
      value = {'@type': 'chat', 'id': request['chat_id'], 'title': self.title,
        'type': {'@type': 'chatTypeSupergroup', 'supergroup_id': 7, 'is_channel': False}}
    elif kind == 'getSupergroup': value = {'@type': 'supergroup', 'id': 7, 'is_forum': True}
    elif kind == 'getForumTopic': value = copy.deepcopy(self.topic)
    elif kind == 'getMessage': value = copy.deepcopy(self.source)
    elif kind == 'getMessageProperties':
      value = {'@type': 'messageProperties', 'can_be_replied': self.can_reply}
    elif kind == 'sendMessage':
      self.last_tempid -= 1
      pending = outgoing(self.last_tempid, sending_state={
        '@type': 'messageSendingStatePending', 'sending_id': request['options']['sending_id']})
      if self.preliminary_mutation is not None: self.preliminary_mutation(pending)
      if self.mode == 'lost_before_reply': self.fail_receive = True; return
      self.events.append({**pending, '@extra': request['@extra']})
      if self.mode == 'lost': self.fail_receive = True; return
      final = outgoing(701, sending_state=None)
      if self.final_mutation is not None: self.final_mutation(final)
      self.events.append({'@type': 'updateMessageSendSucceeded',
                'old_message_id': self.last_tempid, 'message': final})
      return
    else: raise AssertionError('unreviewed raw request: ' + str(kind))
    self.events.append({**value, '@extra': request['@extra']})

  def receive(self, timeout):
    if self.events:
      event = self.events.popleft()
      if event.get('@type') == 'updateMessageSendSucceeded' and self.final_hook is not None:
        self.final_hook()
      return event
    if self.fail_receive:
      self.fail_receive = False
      raise OSError('synthetic response loss')
    return None

  def close(self): self.closed = True


class Harness:
  """All hooks preserve the real registry and observation method side effects."""
  def __init__(self, *, styled=False, capabilities=CAPS):
    self.temporary = tempfile.TemporaryDirectory(); root = Path(self.temporary.name)
    self.raw = Raw(styled); self.provider = TDLibClient(raw=self.raw)
    self.providers = [self.provider]; self.approvals = []; self.decision = lambda: True
    self.thread = None; self.proxy = None; self.current_id = None
    self.register_count = 0; self.register_success_count = 0
    self.register_attempts = []; self.claim_count = 0; self.claim_success_count = 0; self.register_hook = None
    self.capabilities = tuple(capabilities); self.read_hooks = []
    self.broker = Broker(socket_path=root/'broker.sock', lock_path=root/'broker.lock',
      artifact_store=ArtifactStore(cache_dir=root/'cache'),
      client_factory=lambda: self.provider, policy=RuntimePolicy(enabled_capabilities=capabilities),
      approval_prompt=self.owner)
    self.instrument_provider(self.provider)
    claim = self.broker._drafts.claim
    def observed_claim(*args, **kwargs):
      self.claim_count += 1
      result = claim(*args, **kwargs)
      self.claim_success_count += 1
      return result
    self.broker._drafts.claim = observed_claim

  def instrument_provider(self, provider):
    register = provider._send_observations.register
    def observed_register(*args, **kwargs):
      self.register_count += 1; self.register_attempts.append(provider)
      result = register(*args, **kwargs)
      self.register_success_count += 1
      if self.register_hook is not None: self.register_hook()
      return result
    provider._send_observations.register = observed_register

  def __enter__(self): return self

  def __exit__(self, *exc):
    try:
      if self.proxy is not None: self.proxy.close()
      self.broker.shutdown()
      if self.thread is not None:
        self.thread.join(3)
        if self.thread.is_alive(): raise AssertionError('test broker thread did not close')
    finally:
      for provider in self.providers: provider.close()
      self.temporary.cleanup()

  def owner(self, **kwargs):
    self.approvals.append(copy.deepcopy(kwargs)); return self.decision()

  def policy_without(self, capability):
    self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in self.capabilities if c != capability))

  def dispatch(self, operation, payload):
    return self.broker._dispatch({'operation': operation, 'payload': payload,
      'client_id': CLIENT, 'deadline': time.monotonic()+10,
      'broker_generation': self.broker._generation})

  def prepare_result(self, **changes):
    payload = {'recipient': CHAT, 'text': BODY, 'reply_to': copy.deepcopy(ANCHOR),
         'topic': copy.deepcopy(TOPIC), **changes}
    return self.dispatch('prepare_reply_text_send', payload)

  def prepare(self):
    try: result = self.prepare_result()
    except ValidationError as error:
      raise AssertionError('approved optional topic reply preparation must be admitted') from error
    if result['status'] != 'prepared' or result.get('reply') is None:
      raise AssertionError('topic reply must prepare full evidence: ' + repr(result))
    self.current_id = result['reply']['draft']['draft_id']
    return result['reply']

  def send(self, identifier=None, approved=True):
    return self.dispatch('send_prepared_text', {'draft_id': identifier or self.current_id,
                         'approved': approved})

  def sends(self):
    return [r for provider in self.providers for r in provider._raw.sent
            if r['@type'] == 'sendMessage']

  def mutate(self, kind):
    if kind == 'source': self.raw.source['content']['text']['text'] = 'Edited source'
    elif kind == 'deleted': self.raw.source = {'@type': 'error', 'code': 404, 'message': 'private'}
    elif kind == 'topic': self.raw.topic['info']['name'] = 'Changed topic'
    elif kind == 'account': self.raw.account = 8
    elif kind == 'properties': self.raw.can_reply = False
    elif kind == 'epoch': self.provider._invalidate_send_observations()
    elif kind == 'provider':
      other = TDLibClient(raw=Raw()); self.providers.append(other)
      self.instrument_provider(other); self.broker._client = other
    elif kind == 'expiry':
      clock = self.broker._drafts._clock; self.broker._drafts._clock = lambda: clock()+1000
    elif kind in self.capabilities: self.policy_without(kind)
    else: raise AssertionError(kind)

  def arm_read(self, native_kind, action, *, occurrence=1, after_registration=False):
    ledger = {'kind': native_kind, 'seen': 0, 'fired': 0,
      'registration_attempts_at_fire': None, 'registration_successes_at_fire': None}
    def hook(request):
      if request['@type'] == native_kind:
        ledger['seen'] += 1
        phase_reached = not after_registration or self.register_success_count > 0
        if (ledger['fired'] == 0 and phase_reached
            and (after_registration or ledger['seen'] == occurrence)):
          ledger['fired'] += 1
          ledger['registration_attempts_at_fire'] = self.register_count
          ledger['registration_successes_at_fire'] = self.register_success_count
          action()
    self.raw.request_hook = hook; self.read_hooks.append(ledger)
    return ledger

  def start_ipc(self):
    from telegram_search_mcp.broker_client import BrokerClient
    self.thread = threading.Thread(target=self.broker.serve_forever)
    self.thread.start()
    if not self.broker.wait_until_ready(timeout=2): raise AssertionError('test broker not ready')
    self.proxy = BrokerClient(socket_path=self.broker._socket_path,
      policy=self.broker._policy, client_id=CLIENT, restart_callback=lambda: None)
    return self.proxy


class TopicReplyTests(unittest.TestCase):
  def test_prepare_binds_full_topic_source_and_only_the_combined_digest(self):
    with Harness() as h:
      from telegram_search_mcp import reply_drafts
      preview = h.prepare(); draft = preview['draft']; target = preview['reply_target']
      inner = getattr(reply_drafts, 'ReplyTextDraftPreview', None)
      self.assertIsNotNone(inner, 'the approved explicit closed reply-text content model must exist')
      self.assertIs(type(ReplyDraftPreview.model_validate(preview).draft), inner)
      self.assertEqual((draft['recipient'], draft['account_id']), (CHAT, 7))
      self.assertEqual(draft['topic']['forum_topic_id'], 41)
      self.assertEqual(target['forum_topic_id'], 41)
      self.assertEqual(target['anchor'], ANCHOR)
      self.assertEqual(target['text'], '[untrusted Telegram evidence] Source full text')
      self.assertNotIn('preview_sha256', draft)
      self.assertEqual(preview['preview_sha256'], expected_digest(preview))
      h.mutate('source'); h.mutate('topic')
      self.assertEqual(h.dispatch('get_reply_draft', {'draft_id': h.current_id})['reply'], preview)
      self.assertEqual((h.sends(), h.approvals, h.claim_count, h.register_count), ([], [], 0, 0))
      for field, value in [('forum_topic_id', 42), ('source_sha256', '0'*64)]:
        bad = copy.deepcopy(preview); bad['reply_target'][field] = value
        with self.subTest(field=field), self.assertRaises(ValidationError):
          ReplyDraftPreview.model_validate(bad)
      bad = copy.deepcopy(preview); bad['reply_target']['forum_topic_id'] = 42
      bad['preview_sha256'] = expected_digest(bad)
      with self.assertRaises(ValidationError): ReplyDraftPreview.model_validate(bad)

    mutations = [
      ('account', lambda p: p['draft'].update(account_id=8)),
      ('chat', lambda p: p['draft'].update(recipient=CHAT-1)),
      ('expiry', lambda p: p['draft'].update(expires_at='2040-01-01T00:00:00Z')),
      ('topic_id', lambda p: p['draft']['topic'].update(forum_topic_id=42)),
      ('topic_name', lambda p: p['draft']['topic'].update(name='[untrusted Telegram evidence] Other')),
      ('topic_digest', lambda p: p['draft']['topic'].update(source_sha256='0'*64)),
      ('extra', lambda p: p['draft']['topic'].update(extra=True)),
      ('bool', lambda p: p['reply_target'].update(forum_topic_id=True))]
    with Harness() as h:
      preview = h.prepare()
      for label, mutate in mutations:
        bad = copy.deepcopy(preview); mutate(bad)
        with self.subTest(fact=label, digest='old'), self.assertRaises(ValidationError):
          ReplyDraftPreview.model_validate(bad)
        self.assertNotEqual(expected_digest(bad), preview['preview_sha256'])
        if label in ('account', 'chat', 'topic_id', 'extra', 'bool'):
          bad['preview_sha256'] = expected_digest(bad)
          with self.subTest(fact=label, digest='recomputed'), self.assertRaises(ValidationError):
            ReplyDraftPreview.model_validate(bad)
      for extra in ({'topic': TOPIC}, {'reply_to': ANCHOR}, {'preview_sha256': '0'*64}):
        with self.subTest(apply=extra), self.assertRaises(ValidationError):
          h.dispatch('send_prepared_text', {'draft_id': h.current_id, 'approved': True, **extra})
      self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

  def test_canonical_source_keeps_native_topic_while_default_parser_refuses_it(self):
    with Harness() as h:
      with self.assertRaises(ValueError): source_from_message(h.raw.source, CHAT, 55)
      preview = h.prepare()
      source = h.broker._drafts._entries[h.current_id].draft.reply_source
      projection = json.loads(source.projection_json)
      self.assertEqual(projection['message']['topic_id'], NATIVE_TOPIC)
      self.assertEqual(projection['anchor'], ANCHOR)
      self.assertEqual(preview['reply_target']['source_sha256'],
              hashlib.sha256(source.projection_json.encode()).hexdigest())
      self.assertEqual(ReplySource(source.projection_json).source_sha256, source.source_sha256)
      h.raw.source['topic_id']['forum_topic_id'] = 42
      self.assertEqual(json.loads(source.projection_json)['message']['topic_id'], NATIVE_TOPIC)
      before = len(h.broker._drafts._entries)
      for native_topic in (None, {'@type': 'messageTopicForum', 'forum_topic_id': 42},
                {'@type': 'messageTopicForum', 'forum_topic_id': True},
                 {'@type': 'messageTopicForum', 'forum_topic_id': '41'},
                 {'@type': 'messageTopicForum', 'forum_topic_id': 41, 'extra': 1},
                 {'@type': 'messageTopicDirectMessages', 'forum_topic_id': 41}):
        h.raw.source['topic_id'] = native_topic
        with self.subTest(native_topic=native_topic):
          refusal = h.prepare_result()
          self.assertEqual((refusal['status'], refusal['reply']), ('unavailable', None))
          self.assertEqual(len(h.broker._drafts._entries), before)
          self.assertEqual((h.sends(), h.approvals, h.register_count), ([], [], 0))


    with Harness() as h:
      h.raw.source['chat_id'] = CHAT-1
      result = h.prepare_result()
      self.assertEqual((result['status'], result['reply']), ('unavailable', None))
      self.assertEqual((len(h.broker._drafts._entries), h.sends()), (0, []))

  def test_topic_reply_and_source_capabilities_form_one_current_union(self):
    from telegram_search_mcp.contract import CompatibilityError
    for capability in CAPS:
      with self.subTest(capability=capability), Harness(styled=True) as h:
        preview = h.prepare(); did = h.current_id
        ids = h.broker._drafts.known_ids(); entry = h.broker._drafts._entries[did]
        original_draft = entry.draft
        self.assertEqual(ids, {did}); self.assertEqual(entry.state, 'pending')
        required = h.broker._drafts.source_required_capabilities(did,
          owner=DraftOwner(CLIENT, 7), provider=h.provider,
          provider_epoch=h.provider.send_observation_epoch)
        self.assertTrue({'reply_text_send', 'topic_text_send', 'reply_formatted_targets'} <= set(required))
        h.policy_without(capability)
        for operation, extra in [('get_reply_draft', {}), ('update_reply_draft', {'text': 'Revision'}),
                    ('refresh_reply_draft', {})]:
          if capability in {'send', 'reply_text_send'}:
            with self.assertRaises(CompatibilityError) as caught:
              h.dispatch(operation, {'draft_id': did, **extra})
            self.assertEqual(caught.exception.code, 'capability_disabled')
          else:
            result = h.dispatch(operation, {'draft_id': did, **extra})
            self.assertEqual((result['status'], result.get('reply')), ('unavailable', None))
        if capability == 'send':
          with self.assertRaises(CompatibilityError) as caught:
            h.send()
          self.assertEqual(caught.exception.code, 'capability_disabled')
        else:
          denied = h.send()
          self.assertEqual((denied['status'], denied.get('recipient'), denied['message_id'], denied['detail']),
            ('expired', None, None, 'draft is unavailable or no longer valid'))
        self.assertEqual((h.sends(), h.approvals, h.register_count), ([], [], 0))
        # Creation must also fail; source-specific gates may hydrate first.
        if capability in {'send', 'reply_text_send'}:
          with self.assertRaises(CompatibilityError) as caught:
            h.prepare_result()
          self.assertEqual(caught.exception.code, 'capability_disabled')
        else:
          self.assertEqual(h.prepare_result()['status'], 'unavailable')
        self.assertEqual(h.broker._drafts.known_ids(), ids)
        self.assertIs(h.broker._drafts._entries[did], entry)
        self.assertEqual(entry.state, 'pending'); self.assertIs(entry.draft, original_draft)
        self.assertEqual((h.sends(), h.approvals, h.register_count), ([], [], 0))

  def test_preparation_provider_and_epoch_are_frozen_before_both_reads(self):
    for native_kind in ('getForumTopic', 'getMessage', 'getMessageProperties'):
      for change in ('provider', 'epoch', 'account', 'topic_text_send', 'reply_text_send'):
        with self.subTest(read=native_kind, change=change), Harness() as h:
          h.prepare(); count = len(h.broker._drafts._entries)
          hook = h.arm_read(native_kind, lambda: h.mutate(change))
          result = h.prepare_result()
          self.assertEqual((result['status'], result['reply']), ('unavailable', None))
          self.assertEqual(len(h.broker._drafts._entries), count)
          self.assertEqual(hook['fired'], 1, 'the selected preparation boundary must be reached')
          self.assertEqual((h.sends(), h.approvals, h.register_count), ([], [], 0))

  def test_stale_source_or_topic_cannot_be_shown_to_owner(self):
    for change in ('source', 'deleted', 'topic', 'properties', 'provider', 'epoch', 'account'):
      with self.subTest(change=change), Harness() as h:
        h.prepare(); h.mutate(change)
        result = h.send()
        self.assertNotEqual(result['status'], 'sent')
        self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))


  def test_each_authority_change_after_owner_refuses_before_registration(self):
    for change in ('source', 'topic', 'properties', 'account', 'provider', 'epoch',
           'reply_text_send', 'topic_text_send', 'reply_formatted_targets'):
      with self.subTest(change=change), Harness(styled=True) as h:
        h.prepare(); h.decision = lambda: h.mutate(change) or True
        result = h.send()
        self.assertNotEqual(result['status'], 'sent'); self.assertIsNone(result['message_id'])
        self.assertEqual((h.register_count, h.sends()), (0, []))

  def test_registration_mutation_is_rechecked_immediately_before_raw_send(self):
    for change in ('source', 'topic', 'properties', 'account', 'provider', 'epoch', 'expiry',
           'reply_text_send', 'topic_text_send', 'reply_formatted_targets'):
      with self.subTest(change=change), Harness(styled=True) as h:
        h.prepare(); h.register_hook = lambda: h.mutate(change)
        result = h.send()
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['message_id']); self.assertEqual(h.register_count, 1)
        self.assertEqual(h.sends(), [])
        status = h.dispatch('get_send_status', {'draft_id': h.current_id})
        self.assertIsNone(status['message_id'])
        self.assertNotEqual(h.send()['status'], 'sent'); self.assertEqual(h.sends(), [])

  def test_one_exact_native_send_and_one_combined_owner_preview(self):
    with Harness() as h:
      preview = h.prepare()
      self.assertEqual(h.send(approved=False)['status'], 'not_approved')
      self.assertEqual(h.approvals, [])
      h.decision = lambda: False
      self.assertEqual(h.send()['status'], 'not_approved')
      self.assertEqual((h.sends(), h.register_count, h.claim_count), ([], 0, 0))
      h.decision = lambda: True; result = h.send()
      self.assertEqual((result['status'], result['message_id']), ('sent', 701))
      wire = h.sends()[0]; bounded = copy.deepcopy(wire); bounded.pop('@extra')
      _validate_bounded_request(bounded)
      self.assertEqual(wire['chat_id'], CHAT); self.assertEqual(wire['topic_id'], NATIVE_TOPIC)
      self.assertEqual(wire['reply_to'], {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
        'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''})
      self.assertEqual(wire['input_message_content'], {'@type': 'inputMessageText',
        'text': {'@type': 'formattedText', 'text': BODY, 'entities': []},
        'link_preview_options': LINK_OPTIONS, 'clear_draft': False})
      self.assertIsNone(wire['reply_markup'])
      options = dict(wire['options']); sending_id = options.pop('sending_id')
      self.assertIs(type(sending_id), int); self.assertGreater(sending_id, 0)
      self.assertEqual(options, SEND_OPTIONS)
      self.assertEqual(h.approvals[-1]['reply_preview'], preview)
      self.assertNotIn('topic_preview', h.approvals[-1])
      self.assertEqual((h.claim_count, h.register_count, len(h.sends())), (1, 1, 1))
      self.assertEqual(h.dispatch('get_send_status', {'draft_id': h.current_id})['message_id'], 701)
      self.assertEqual(h.send()['message_id'], 701); self.assertEqual(len(h.sends()), 1)

      from telegram_search_mcp.tdjson import ForbiddenTDLibRequest
      variants = [(['topic_id', 'forum_topic_id'], True), (['topic_id', 'forum_topic_id'], '41'),
        (['topic_id', 'forum_topic_id'], 2**31), (['topic_id', 'extra'], 1),
        (['reply_to', 'message_id'], True), (['reply_to', 'quote'], {'text': 'Unreviewed'}),
        (['options', 'disable_notification'], 0), (['options', 'paid_message_star_count'], False),
        (['options', 'only_preview'], 0), (['options', 'protect_content'], True),
        (['input_message_content', 'link_preview_options', 'is_disabled'], 1),
        (['input_message_content', 'link_preview_options', 'url'], 'https://unreviewed.invalid'),
        (['input_message_content', 'clear_draft'], 0),
        (['input_message_content', 'text', 'entities'], [{'offset': 0, 'length': 1, 'type': {'@type': 'textEntityTypeBold'}}]),
        (['input_message_content', '@type'], 'inputMessagePhoto'), (['reply_markup'], {})]
      for path, value in variants:
        candidate = copy.deepcopy(bounded); node = candidate
        for key in path[:-1]: node = node[key]
        node[path[-1]] = value
        with self.subTest(native_path=path), self.assertRaises(ForbiddenTDLibRequest):
          _validate_bounded_request(candidate)
      # The existing two noncombined text shapes remain individually valid.
      ordinary = copy.deepcopy(bounded); ordinary['topic_id'] = None; _validate_bounded_request(ordinary)
      plain_topic = copy.deepcopy(bounded); plain_topic['reply_to'] = None; _validate_bounded_request(plain_topic)

  def test_preliminary_and_final_require_both_topic_and_reply_independently(self):
    mutations = [('wrong_topic', lambda m: m['topic_id'].update(forum_topic_id=42)),
      ('missing_topic', lambda m: m.pop('topic_id')),
      ('wrong_reply', lambda m: m['reply_to'].update(message_id=56)),
      ('missing_reply', lambda m: m.pop('reply_to')),
      ('foreign_reply_chat', lambda m: m['reply_to'].update(chat_id=CHAT-1)),
       ('extra_topic', lambda m: m['topic_id'].update(extra=True)),
       ('bool_topic', lambda m: m['topic_id'].update(forum_topic_id=True)),
       ('extra_reply', lambda m: m['reply_to'].update(extra=True)),
       ('bool_reply', lambda m: m['reply_to'].update(message_id=True)),
       ('wrong_reply_type', lambda m: m['reply_to'].update(**{'@type': 'messageReplyToStory'})),
       ('wrong_chat', lambda m: m.update(chat_id=CHAT-1)),
       ('wrong_text', lambda m: m['content']['text'].update(text='Other'))]
    for phase in ('preliminary', 'final'):
      for label, mutate in mutations:
        with self.subTest(phase=phase, mutation=label), Harness() as h:
          h.prepare()
          if phase == 'preliminary': h.raw.preliminary_mutation = mutate
          else: h.raw.final_mutation = mutate
          result = h.send()
          self.assertEqual((result['status'], result['message_id']), ('outcome_unknown', None))
          self.assertEqual(h.send()['status'], 'outcome_unknown')
          self.assertEqual(len(h.sends()), 1)


  def test_exact_late_success_keeps_union_disclosure_and_never_retries(self):
    with Harness(styled=True) as h:
      h.prepare(); h.raw.mode = 'lost'
      self.assertEqual(h.send()['status'], 'outcome_unknown')
      wrong = outgoing(702, sending_state=None); wrong['reply_to']['message_id'] = 56
      h.raw.events.append({'@type': 'updateMessageSendSucceeded',
        'old_message_id': h.raw.last_tempid, 'message': wrong})
      self.assertEqual(h.dispatch('get_send_status', {'draft_id': h.current_id})['status'], 'outcome_unknown')
      h.raw.events.append({'@type': 'updateMessageSendSucceeded',
        'old_message_id': h.raw.last_tempid, 'message': outgoing(703, sending_state=None)})
      status = h.dispatch('get_send_status', {'draft_id': h.current_id})
      self.assertEqual((status['status'], status['message_id']), ('sent', 703))
      for capability in ('reply_text_send', 'reply_formatted_targets'):
        h.policy_without(capability)
        hidden = h.dispatch('get_send_status', {'draft_id': h.current_id})
        self.assertEqual((hidden['status'], hidden['message_id']), ('outcome_unknown', None))
        replay = h.send(); self.assertNotEqual(replay['status'], 'sent')
        self.assertIsNone(replay['message_id']); self.assertEqual(len(h.sends()), 1)
        h.broker._policy = RuntimePolicy(enabled_capabilities=CAPS)
      self.assertEqual(h.send()['message_id'], 703); self.assertEqual(len(h.sends()), 1)


  def test_combined_owner_dialog_is_complete_closed_and_bound(self):
    from telegram_search_mcp.approval_prompt import confirm_approved_send
    with Harness() as h:
      preview = h.prepare(); h.decision = lambda: False
      self.assertEqual(h.send()['status'], 'not_approved'); kwargs = h.approvals[0]
      self.assertEqual(kwargs['reply_preview'], preview); self.assertNotIn('topic_preview', kwargs)
      completed = subprocess.CompletedProcess([], 0, b'APPROVED\n')
      with patch('telegram_search_mcp.approval_prompt.subprocess.run', return_value=completed) as run:
        self.assertTrue(confirm_approved_send(**kwargs))
        shown = run.call_args.args[0][-1]
        expires = datetime.fromisoformat(preview['draft']['expires_at'].replace('Z', '+00:00')).isoformat()
        topic = preview['draft']['topic']
        for evidence in (BODY, 'Account: 7', 'Topic ID: 41',
            f'Reply anchor: chat {CHAT}, message 55', expires,
            f"Topic type: {topic['chat_type']}; peer: {topic['peer_id']}",
            f"Created: {topic['creation_date']}; creator: {topic['creator_kind']} {topic['creator_id']}",
            f"General: {topic['is_general']}; outgoing: {topic['is_outgoing']}; closed: {topic['is_closed']}; hidden: {topic['is_hidden']}",
            preview['draft']['topic']['name'], preview['draft']['topic']['source_sha256'],
            preview['reply_target']['text'], preview['reply_target']['source_sha256'],
            preview['preview_sha256'], preview['draft']['sha256'], h.current_id):
          self.assertIn(evidence, shown)
        self.assertNotIn('no reply', shown.lower())
        self.assertEqual(shown.count('Preview SHA-256:'), 1)
      for key, value in [('recipient', CHAT-1), ('recipient_title', 'Other recipient'),
          ('text', 'Different'), ('sha256', '0'*64), ('size_bytes', 1)]:
        with self.subTest(kwarg=key), patch('telegram_search_mcp.approval_prompt.subprocess.run') as run:
          self.assertFalse(confirm_approved_send(**{**kwargs, key: value})); run.assert_not_called()
      for mutate in (lambda p: p['draft']['topic'].update(forum_topic_id=42),
          lambda p: p['reply_target']['anchor'].update(message_id=56),
          lambda p: p.update(extra=True), lambda p: p['draft'].update(preview_sha256='0'*64)):
        bad = copy.deepcopy(kwargs); mutate(bad['reply_preview'])
        with patch('telegram_search_mcp.approval_prompt.subprocess.run') as run:
          self.assertFalse(confirm_approved_send(**bad)); run.assert_not_called()
      self.assertEqual((h.claim_count, h.register_count, h.sends()), (0, 0, []))

  def test_combined_claim_lifecycle_and_aggregate_budget(self):
    from telegram_search_mcp.schemas import SendPreparedTextRequest
    for operation in ('cancel_draft', 'update_reply_draft', 'expiry'):
      with self.subTest(owner=operation), Harness() as h:
        h.prepare()
        def decide():
          if operation == 'expiry': h.mutate('expiry')
          else: h.dispatch(operation, {'draft_id': h.current_id, **({'text': 'Revision'} if operation == 'update_reply_draft' else {})})
          return True
        h.decision = decide; result = h.send()
        self.assertNotEqual(result['status'], 'sent'); self.assertIsNone(result['message_id'])
        self.assertIn(h.claim_count, (0, 1)); self.assertEqual(h.claim_success_count, 0)
        self.assertEqual((h.register_count, h.sends()), (0, []))
    for phase in ('properties', 'registration'):
      with self.subTest(deadline=phase), Harness() as h:
        h.prepare()
        clock = [time.monotonic()]; crossings = []
        def exhaust_budget():
          crossings.append(True); clock[0] += 20
        if phase == 'properties':
          hook = h.arm_read('getMessageProperties', exhaust_budget, after_registration=True)
        else: h.register_hook = exhaust_budget
        # Single-thread clock control reaches the actual boundary first; no sleep.
        # All consumers of this shared time module see the same monotonic value.
        with patch('telegram_search_mcp.tdjson.time.monotonic', side_effect=lambda: clock[0]):
          result = h.broker._send_prepared_text(SendPreparedTextRequest(draft_id=h.current_id, approved=True),
            client_id=CLIENT, deadline=clock[0]+10)
        self.assertEqual(crossings, [True])
        self.assertNotEqual(result.status, 'sent'); self.assertIsNone(result.message_id)
        self.assertEqual(h.sends(), [])
        self.assertIsNone(h.provider._send_observations.snapshot(h.current_id))
        self.assertEqual((h.claim_count, h.claim_success_count, h.register_count, h.register_success_count), (1, 1, 1, 1))
        if phase == 'properties':
          self.assertEqual((hook['fired'], hook['registration_attempts_at_fire'],
            hook['registration_successes_at_fire']), (1, 1, 1))
    with Harness() as h:
      h.prepare(); held = threading.Event(); release = threading.Event(); results = []; errors = []
      def hold():
        with h.provider._lock: held.set(); release.wait(2)
      holder = threading.Thread(target=hold); holder.start()
      try:
        self.assertTrue(held.wait(1))
        def call():
          try: results.append(h.broker._send_prepared_text(SendPreparedTextRequest(draft_id=h.current_id,
            approved=True), client_id=CLIENT, deadline=time.monotonic()+.02))
          except Exception as error: errors.append(error)
        caller = threading.Thread(target=call); caller.start(); caller.join(.2)
        finished = not caller.is_alive()
      finally:
        release.set(); holder.join(1)
        if 'caller' in locals(): caller.join(1)
      self.assertFalse(holder.is_alive()); self.assertFalse(caller.is_alive())
      self.assertTrue(finished, 'combined owner/provider lock must obey the aggregate deadline')
      self.assertEqual(errors, []); self.assertEqual(len(results), 1)
      self.assertNotEqual(results[0].status, 'sent')
      self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))
    with Harness() as h:
      h.prepare(); barrier = threading.Barrier(2)
      h.decision = lambda: barrier.wait(timeout=5) is not None
      with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [f.result(timeout=8) for f in [pool.submit(h.send) for _ in range(2)]]
      self.assertEqual(sum(r['status'] == 'sent' for r in outcomes), 1)
      self.assertIn(h.claim_count, (1, 2))
      self.assertEqual((len(h.approvals), h.claim_success_count, h.register_count, len(h.sends())), (2, 1, 1, 1))

  def test_combined_attempt_retention_and_final_disclosure(self):
    with Harness() as h:
      h.prepare(); fired = []
      def expire(): fired.append(True); h.mutate('expiry')
      h.raw.final_hook = expire
      self.assertEqual(h.send()['message_id'], 701); self.assertEqual(fired, [True])
      self.assertIsNotNone(h.provider._send_observations.snapshot(h.current_id))
      self.assertEqual(h.dispatch('get_send_status', {'draft_id': h.current_id})['message_id'], 701)
      self.assertEqual(h.send()['message_id'], 701); self.assertEqual(len(h.sends()), 1)
    for phase in ('final_receive', 'status_recheck'):
      for capability in ('reply_text_send', 'reply_formatted_targets'):
        with self.subTest(phase=phase, capability=capability), Harness(styled=True) as h:
          h.prepare(); fired = []
          def revoke(): fired.append(True); h.policy_without(capability)
          if phase == 'final_receive':
            h.raw.final_hook = revoke; hidden = h.send()
          else:
            self.assertEqual(h.send()['status'], 'sent')
            original = h.provider.get_send_observation
            def observe(*args, **kwargs):
              value = original(*args, **kwargs)
              if not fired: revoke()
              return value
            with patch.object(h.provider, 'get_send_observation', side_effect=observe):
              hidden = h.dispatch('get_send_status', {'draft_id': h.current_id})
          self.assertEqual(fired, [True]); self.assertIsNone(hidden['message_id'])
          self.assertNotEqual(hidden['status'], 'sent')
          self.assertIsNotNone(h.provider._send_observations.snapshot(h.current_id))
          self.assertIsNone(h.send()['message_id']); self.assertEqual(len(h.sends()), 1)
          h.broker._policy = RuntimePolicy(enabled_capabilities=h.capabilities)
          self.assertEqual(h.dispatch('get_send_status', {'draft_id': h.current_id})['message_id'], 701)
          self.assertEqual(h.send()['message_id'], 701); self.assertEqual(len(h.sends()), 1)

  def test_combined_lost_preliminary_requires_both_pending_and_final(self):
    cases = [('exact', lambda m: None), ('wrong_topic', lambda m: m['topic_id'].update(forum_topic_id=42)),
      ('missing_topic', lambda m: m.pop('topic_id')), ('wrong_reply', lambda m: m['reply_to'].update(message_id=56)),
      ('missing_reply', lambda m: m.pop('reply_to')), ('final_only', None)]
    for label, mutate in cases:
      with self.subTest(pending=label), Harness() as h:
        h.prepare(); h.raw.mode = 'lost_before_reply'
        self.assertEqual(h.send()['status'], 'outcome_unknown'); wire = h.sends()[0]
        if mutate is not None:
          pending = outgoing(h.raw.last_tempid, sending_state={'@type': 'messageSendingStatePending',
            'sending_id': wire['options']['sending_id']}); mutate(pending)
          h.raw.events.append({'@type': 'updateNewMessage', 'message': pending})
        h.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': h.raw.last_tempid,
          'message': outgoing(704, sending_state=None)})
        result = h.dispatch('get_send_status', {'draft_id': h.current_id})
        self.assertEqual((result['status'], result['message_id']),
          ('sent', 704) if label == 'exact' else ('outcome_unknown', None))
        self.assertEqual(h.send()['status'], result['status']); self.assertEqual(len(h.sends()), 1)
    with Harness() as h:
      h.prepare(); h.raw.mode = 'lost'; self.assertEqual(h.send()['status'], 'outcome_unknown')
      failed = outgoing(h.raw.last_tempid, sending_state={'@type': 'messageSendingStateFailed',
        'error': {'@type': 'error', 'code': 400, 'message': 'synthetic'}})
      h.raw.events.append({'@type': 'updateMessageSendFailed', 'old_message_id': h.raw.last_tempid,
        'message': failed, 'error': {'@type': 'error', 'code': 400, 'message': 'synthetic'}})
      terminal = h.dispatch('get_send_status', {'draft_id': h.current_id})
      self.assertEqual((terminal['status'], terminal['message_id']), ('failed', None))
      h.raw.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': h.raw.last_tempid,
        'message': outgoing(705, sending_state=None)})
      self.assertEqual(h.dispatch('get_send_status', {'draft_id': h.current_id})['status'], 'failed')
      self.assertEqual(h.send()['status'], 'failed'); self.assertEqual(len(h.sends()), 1)

  def test_combined_mixed_source_union_survives_each_boundary(self):
    from test_artifact_replies import content_fixture
    for source_kind in ('mixed', 'photo'):
      extras = ('reply_text_url_targets',) if source_kind == 'mixed' else ('reply_media_targets',)
      caps = CAPS+extras
      source_gates = ('reply_formatted_targets', 'reply_text_url_targets') if source_kind == 'mixed' else ('reply_media_targets',)
      for phase in ('owner', 'properties', 'registration', 'status'):
        for gate in source_gates:
          with self.subTest(source=source_kind, phase=phase, gate=gate), Harness(capabilities=caps) as h:
            if source_kind == 'photo': h.raw.source['content'] = content_fixture('photo', 'Source photo caption')
            else:
              h.raw.source['content']['text'] = {'@type': 'formattedText', 'text': 'Link bold', 'entities': [
                {'@type': 'textEntity', 'offset': 0, 'length': 4, 'type': {'@type': 'textEntityTypeTextUrl', 'url': 'OpaqueA'}},
                {'@type': 'textEntity', 'offset': 5, 'length': 4, 'type': {'@type': 'textEntityTypeBold'}}]}
            preview = h.prepare()
            required = h.broker._drafts.source_required_capabilities(h.current_id, owner=DraftOwner(CLIENT, 7),
              provider=h.provider, provider_epoch=h.provider.send_observation_epoch)
            self.assertTrue(set(source_gates)|{'reply_text_send', 'topic_text_send'} <= set(required))
            fired = []
            def revoke(): fired.append(True); h.policy_without(gate)
            if phase == 'owner': h.decision = lambda: revoke() or True
            elif phase == 'properties': hook = h.arm_read('getMessageProperties', revoke, after_registration=True)
            elif phase == 'registration': h.register_hook = revoke
            if phase == 'status':
              self.assertEqual(h.send()['status'], 'sent'); revoke()
              result = h.dispatch('get_send_status', {'draft_id': h.current_id})
            else: result = h.send()
            self.assertEqual(fired, [True]); self.assertNotEqual(result['status'], 'sent'); self.assertIsNone(result['message_id'])
            if phase == 'properties':
              self.assertEqual((hook['fired'], hook['registration_attempts_at_fire'],
                hook['registration_successes_at_fire']), (1, 1, 1))
              self.assertEqual((h.register_count, h.register_success_count), (1, 1))
              self.assertIsNone(h.provider._send_observations.snapshot(h.current_id))
            self.assertIsNone(h.send()['message_id']); self.assertEqual(len(h.sends()), 1 if phase == 'status' else 0)
            if phase == 'status':
              self.assertEqual(h.sends()[0]['input_message_content']['@type'], 'inputMessageText')
              self.assertEqual(h.sends()[0]['input_message_content']['text'], {'@type': 'formattedText', 'text': BODY, 'entities': []})
              h.broker._policy = RuntimePolicy(enabled_capabilities=caps)
              self.assertEqual(h.send()['message_id'], 701); self.assertEqual(len(h.sends()), 1)

  def test_combined_proxy_topic_presence_identity_and_revision_binding(self):
    from telegram_search_mcp.broker_client import BrokerClient
    from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest, PrepareReplyTextSendResponse, PREPARED
    with Harness() as h:
      preview = h.prepare(); did = h.current_id
      proxy = h.start_ipc()
      request = PrepareReplyTextSendRequest(recipient=CHAT, text=BODY, reply_to=ANCHOR, topic=TOPIC)
      bads = []
      for label, mutate in [('missing', lambda p: (p['draft'].pop('topic'), p['reply_target'].pop('forum_topic_id'))),
          ('topic', lambda p: (p['draft']['topic'].update(forum_topic_id=42), p['reply_target'].update(forum_topic_id=42))),
          ('chat', lambda p: (p['draft'].update(recipient=CHAT-1), p['reply_target']['anchor'].update(chat_id=CHAT-1),
            p['draft']['topic'].update(chat_id=CHAT-1))),
          ('anchor', lambda p: p['reply_target']['anchor'].update(message_id=56)),
          ('text', lambda p: p['draft'].update(text='Other', sha256=hashlib.sha256(b'Other').hexdigest(), size_bytes=5))]:
        bad = copy.deepcopy(preview); mutate(bad)
        if bad['draft'].get('topic') is not None:
          topic = bad['draft']['topic']
          topic['source_sha256'] = hashlib.sha256(canonical(
            {key: value for key, value in topic.items() if key != 'source_sha256'}).encode()).hexdigest()
        bad['preview_sha256'] = expected_digest(bad); bads.append((label, bad))
      ordinary = PrepareReplyTextSendRequest(recipient=CHAT, text=BODY, reply_to=ANCHOR)
      for label, bad in bads+[('unsolicited', preview)]:
        envelope = {'status': 'prepared', 'reply': bad, 'detail': PREPARED}
        with self.subTest(forgery=label):
          # A valid closed envelope must reach the actual request-binding check.
          validated = PrepareReplyTextSendResponse.model_validate(envelope)
          self.assertEqual(validated.model_dump(mode='json'), envelope)
          with patch.object(proxy, '_request', return_value=envelope) as transport:
            result = proxy.prepare_reply_text_send(ordinary if label == 'unsolicited' else request)
            self.assertEqual((result.status, result.reply), ('unavailable', None)); self.assertEqual(transport.call_count, 1)
      updated = h.dispatch('update_reply_draft', {'draft_id': did, 'text': 'Revision'})
      self.assertEqual(updated['status'], 'revised'); revised = updated['reply']; h.current_id = revised['draft']['draft_id']
      self.assertNotEqual(h.current_id, did); self.assertNotEqual(revised['preview_sha256'], preview['preview_sha256'])
      self.assertEqual(revised['preview_sha256'], expected_digest(revised))
      self.assertEqual(revised['draft']['topic'], preview['draft']['topic'])
      self.assertEqual(revised['reply_target'], preview['reply_target'])
      required = h.broker._drafts.source_required_capabilities(h.current_id, owner=DraftOwner(CLIENT, 7),
        provider=h.provider, provider_epoch=h.provider.send_observation_epoch)
      self.assertTrue({'topic_text_send', 'reply_text_send'} <= set(required))
      self.assertEqual(h.dispatch('get_reply_draft', {'draft_id': did})['status'], 'unavailable')
      self.assertNotEqual(h.send(did)['status'], 'sent')
      h.mutate('provider'); self.assertNotEqual(h.send()['status'], 'sent')
      self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

    # A drift at the second owner read must refuse before registry replacement.
    # Arm only after preparation so its getMe reads cannot trigger this hook.
    with Harness() as h:
      from telegram_search_mcp.reply_drafts import ReviseReplyDraftResponse, UNAVAILABLE
      preview = h.prepare(); did = h.current_id; owner = DraftOwner(CLIENT, 7)
      provider = h.provider; epoch = provider.send_observation_epoch
      account = h.raw.account; ids = h.broker._drafts.known_ids()
      self.assertEqual(ids, {did})
      old_entry = h.broker._drafts._entries[did]; old_draft = old_entry.draft
      self.assertEqual(old_entry.state, 'pending')
      hook = h.arm_read('getMe', lambda: h.mutate('epoch'), occurrence=2)
      updated = h.dispatch('update_reply_draft', {'draft_id': did, 'text': 'Revision'})
      typed = ReviseReplyDraftResponse.model_validate(updated)
      self.assertEqual((typed.status, typed.previous_draft_id, typed.reply, typed.detail),
        ('unavailable', None, None, UNAVAILABLE))
      self.assertGreaterEqual(hook['seen'], 2); self.assertEqual(hook['fired'], 1)
      self.assertIs(h.provider, provider); self.assertIs(h.broker._client, provider)
      self.assertEqual(h.raw.account, account); self.assertEqual(account, 7)
      self.assertIsNot(provider.send_observation_epoch, epoch)
      self.assertEqual(h.broker._drafts.known_ids(), ids)
      self.assertIs(h.broker._drafts._entries[did], old_entry)
      self.assertEqual(old_entry.state, 'pending'); self.assertIs(old_entry.draft, old_draft)
      self.assertIs(h.broker._drafts.peek(did, owner=owner), old_draft)
      self.assertEqual((h.approvals, h.claim_count, h.claim_success_count,
        h.register_count, h.register_success_count, h.sends()), ([], 0, 0, 0, 0, []))


class PublicTopicReplyTests(unittest.IsolatedAsyncioTestCase):
  async def test_public_selector_admission_and_real_sdk_proxy_broker_lifecycle(self):
    from mcp import Client
    from mcp.shared.exceptions import MCPError
    from telegram_search_mcp.server import build_server
    async def invalid(consumer, arguments):
      try: result = await consumer.call_tool('prepare_reply_text_send', arguments)
      except MCPError as error:
        self.assertTrue(any(word in str(error).lower() for word in ('validation', 'invalid', 'extra', 'integer')))
      else: self.assertTrue(result.is_error)
    with Harness() as h:
      proxy = h.start_ipc()
      async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
        tools = {t.name: t for t in (await consumer.list_tools()).tools}
        self.assertEqual(len(tools), 49)
        self.assertIn('topic', tools['prepare_reply_text_send'].input_schema['properties'])
        payload = {'recipient': CHAT, 'text': BODY, 'reply_to': ANCHOR, 'topic': TOPIC}
        for bad in ({'id': True}, {'id': None}, {'id': 41, 'reply_to': ANCHOR}, {'id': '41'}, {'id': 0}, {'id': -1}, {'id': 2**31},
              {'id': 41.0}, {'id': 41, 'extra': 1}, {'kind': 'other', 'id': 41}):
          with self.subTest(selector=bad): await invalid(consumer, {**payload, 'topic': bad})
        for extra in ({'topic_id': NATIVE_TOPIC}, {'outgoing_entities': None}, {'quote': 'Unreviewed'}):
          await invalid(consumer, {**payload, **extra})
        self.assertEqual((h.raw.sent, h.approvals, len(h.broker._drafts._entries)), ([], [], 0))
        prepared = (await consumer.call_tool('prepare_reply_text_send', payload)).structured_content['result']
        self.assertEqual(prepared['status'], 'prepared'); preview = prepared['reply']; did = preview['draft']['draft_id']
        got = (await consumer.call_tool('get_reply_draft', {'draft_id': did})).structured_content
        self.assertEqual(got['reply'], preview)
        refresh = (await consumer.call_tool('refresh_reply_draft', {'draft_id': did})).structured_content
        self.assertEqual((refresh['status'], refresh['reply']), ('unavailable', None))
        updated = (await consumer.call_tool('update_reply_draft', {'draft_id': did, 'text': BODY})).structured_content
        self.assertEqual(updated['status'], 'revised'); revised = updated['reply']; old_id = did
        did = revised['draft']['draft_id']; self.assertNotEqual(did, old_id)
        self.assertNotEqual(revised['preview_sha256'], preview['preview_sha256'])
        self.assertEqual((revised['draft']['topic'], revised['reply_target']), (preview['draft']['topic'], preview['reply_target']))
        self.assertEqual((await consumer.call_tool('get_reply_draft', {'draft_id': old_id})).structured_content['status'], 'unavailable')
        h.decision = lambda: False
        owner_denied = (await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True})).structured_content
        self.assertEqual(owner_denied['status'], 'not_approved'); self.assertEqual(len(h.approvals), 1)
        self.assertEqual((h.claim_count, h.register_count, h.sends()), (0, 0, [])); h.approvals.clear()
        h.decision = lambda: True
        denial = (await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': False})).structured_content
        self.assertEqual(denial['status'], 'not_approved'); self.assertEqual(h.approvals, [])
        sent = (await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True})).structured_content
        self.assertEqual((sent['status'], sent['message_id']), ('sent', 701))
        replay = (await consumer.call_tool('send_prepared_text', {'draft_id': did, 'approved': True})).structured_content
        self.assertEqual(replay['message_id'], 701); self.assertEqual(len(h.sends()), 1)
        status = (await consumer.call_tool('get_send_status', {'draft_id': did})).structured_content
        self.assertEqual((status['status'], status['message_id']), ('sent', 701))
    for missing in ('send', 'reply_text_send', 'topic_text_send'):
      with self.subTest(missing=missing), Harness(capabilities=tuple(c for c in CAPS if c != missing)) as h:
        proxy = h.start_ipc(); reached = []
        original = proxy.prepare_reply_text_send
        def observed_service(request):
          reached.append(request); return original(request)
        with patch.object(proxy, 'prepare_reply_text_send', side_effect=observed_service):
          async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
            result = await consumer.call_tool('prepare_reply_text_send',
              {'recipient': CHAT, 'text': BODY, 'reply_to': ANCHOR, 'topic': TOPIC})
            if missing in ('send', 'reply_text_send'):
              self.assertTrue(result.is_error); self.assertEqual(reached, [])
            elif result.is_error:
              diagnostic = ' '.join(getattr(block, 'text', '') for block in result.content).lower()
              self.assertIn('capability', diagnostic)
            else:
              self.assertEqual((result.structured_content['result']['status'], result.structured_content['result']['reply']), ('unavailable', None))
            self.assertEqual((h.raw.sent, h.approvals, h.register_count, len(h.broker._drafts._entries)), ([], [], 0, 0))



if __name__ == '__main__':
  unittest.main()
