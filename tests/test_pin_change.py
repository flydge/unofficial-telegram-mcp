"""0.57 work item 2 of part 2: one owner-approved pin or unpin of one message (F17 rest).

Synthetic raw transport only (test_archive_edit's, answering pinChatMessage and unpinChatMessage too): projections,
drafts, broker, proxy, server and TDLib reducers are production code. No live TDLib and no owner dialog: the prompt is
a fake, and the dialog goldens patch osascript.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client
from mcp.shared.exceptions import MCPError

import test_archive_edit as archive
from approval_layout import tail as dialog_tail
from schema_changes import DIRECTORY as SCHEMA_CHANGES
from test_archive_edit import ACCOUNT, CHAT, CLIENT, DID, ERRORS, EXPIRY, FOREIGN, MARK, MID, UNKNOWN, chat, entity
from telegram_search_mcp import broker_client as broker_client_module
from telegram_search_mcp.approval_prompt import DETAILS, NOT_GIVEN, ApprovalDecision, confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import SEND_OPERATIONS, Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS, CompatibilityError
from telegram_search_mcp.draft_kinds import DRAFT_KINDS
from telegram_search_mcp.edit_drafts import EditChat
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.pin_drafts import (ApplyPreparedPinChangeRequest, ApplyPreparedPinChangeResponse,
    PinChangeDraftPreview, PinChangeSpec, PinSnapshot, PreparePinChangeRequest, PreparePinChangeResponse,
    UNAVAILABLE as PIN_UNAVAILABLE, pin_request)
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES, available_tools, build_server
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, TDLibClient,
                                        _validate_bounded_request)

TOOLS = ('prepare_pin_change', 'apply_prepared_pin_change')
PIN = RuntimePolicy(enabled_capabilities=('send', 'read', 'pin_messages'))
EDITS = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text', 'edit_own_caption',
                                            'edit_own_styled_text', 'archive_chats'))
PREPARED = 'immutable pin change draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for this pin change; no draft was created'
ALREADY_PINNED = 'message is already pinned; no draft was created'
NOT_PINNED = 'message is not pinned; no draft was created'
PROPERTIES = {**archive.PROPERTIES, 'can_be_pinned': True}
OTHER = 8


def message(text='Meeting at 10', pinned=False, **changes):
    """Another member's text message (a pin may target anyone's message)."""
    return archive.own_message(text, is_pinned=pinned, is_outgoing=False,
                               sender_id={'@type': 'messageSenderUser', 'user_id': OTHER}, **changes)


class Raw(archive.Raw):
    """test_archive_edit's transport answering pinChatMessage and unpinChatMessage; `pin_mode` shapes the answer."""

    def __init__(self):
        super().__init__()
        self.message = message()
        self.properties = dict(PROPERTIES)
        self.pin_mode = 'ok'
        self.late = None  # (reads left, state): getMessage reads before the pin state becomes visible
        self.readback = None  # the getMessage answer right after a rejection

    def send(self, request):
        kind = request['@type']
        if kind == 'getMessage' and self.late is not None:
            left, state = self.late
            self.late = (left - 1, state) if left > 1 else None
            if left <= 1:
                self.message['is_pinned'] = state
        if kind == 'getMessage' and self.readback is not None and any(
                r['@type'] in ('pinChatMessage', 'unpinChatMessage') for r in self.sent):
            self.sent.append(copy.deepcopy(request))
            answer, self.readback = self.readback, None
            self.events.append({**answer, '@extra': request['@extra']})
            return
        if kind not in ('pinChatMessage', 'unpinChatMessage'):
            return super().send(request)
        self.sent.append(copy.deepcopy(request))
        target, mode = kind == 'pinChatMessage', self.pin_mode
        if mode == 'lost':
            self.fail_receive = True
            return
        if mode == 'switch_account':
            self.account = 8
        if mode in ('ok', 'switch_account', 'server_error', 'codeless', 'negative', 'race', 'bad_result'):
            self.message['is_pinned'] = target  # a racing pin by someone else, or a change before a 5xx
            self.events.append({'@type': 'updateMessageIsPinned', 'chat_id': CHAT, 'message_id': MID,
                                'is_pinned': target})
        if mode == 'late':
            self.late = (3, target)
        if mode in ERRORS or mode == 'race':
            code = 400 if mode == 'race' else ERRORS[mode]
            error = {'@type': 'error', 'message': 'PINNED_MESSAGE_PRIVATE', '@extra': request['@extra']}
            self.events.append(error if code is None else {**error, 'code': code})
            return
        self.events.append({'@type': 'message' if mode == 'bad_result' else 'ok', '@extra': request['@extra']})


def snapshot(raw_message=None, kind='supergroup', properties=None):
    return PinSnapshot.create(chat(CHAT, 'Team Chat', kind), CHAT, raw_message or message(), MID,
                              properties or dict(PROPERTIES))


def preview(action='pin', raw_message=None, kind='supergroup'):
    raw_message = raw_message or message(pinned=action == 'unpin')
    spec = PinChangeSpec(snapshot=snapshot(raw_message, kind), action=action)
    return PinChangeDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
                                        chat=EditChat(chat_id=CHAT, title='Team Chat', kind=kind), spec=spec)


def shown(value, **kw):
    from approval_layout import logical
    seen = []
    arguments = dict(recipient_title=value.chat.title, recipient=value.chat.chat_id, display_name='Pin change',
                     size_bytes=1, sha256=value.message.source_sha256, caption='', kind='pin_change',
                     pin_change_preview=value.model_dump(mode='json'))
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        assert confirm_approved_send(**arguments) is False
    return [logical(argv[-1]) for argv in seen], [argv[argv.index('-e') + 1] for argv in seen]


class Fixture:
    policy = PIN

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.raw = Raw()
        self.client = TDLibClient(raw=self.raw)
        self.client.CHANGE_OBSERVATION_SECONDS, self.client.CHANGE_POLL_SECONDS = 0.3, 0.01
        self.approvals = []
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                             client_factory=lambda: self.client, policy=self.policy,
                             approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
                                      'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def changes(self):
        return [request for request in self.raw.sent if request['@type'] in ('pinChatMessage', 'unpinChatMessage')]

    def prepare(self, action='pin', client=CLIENT):
        result = self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': action}, client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['pin_change']

    def apply(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_pin_change', {'draft_id': did, 'approved': approved}, client)


class ContractTests(unittest.TestCase):
    def test_the_kind_row_the_operations_and_the_census(self):
        row = DRAFT_KINDS['pin_change']
        self.assertEqual((row.change, row.unbacked, row.dedicated, row.status, row.display_name),
                         (True, True, True, 'pin_messages', 'Pin change'))
        self.assertIs(row.spec_type(), PinChangeSpec)
        self.assertIn('prepare_pin_change', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        self.assertNotIn('apply_prepared_pin_change', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        self.assertIn('apply_prepared_pin_change', SEND_OPERATIONS)
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(OPERATION_CAPABILITIES[name], 'pin_messages')
                self.assertEqual(TOOL_OPERATIONS[name], name)
                self.assertIn(name, OPERATIONS)
                self.assertEqual(_TOOL_CAPABILITIES[name], ('pin_messages', 'send', 'read'))
        self.assertEqual(len(_TOOL_NAMES), 62)
        self.assertEqual(len(set(_TOOL_NAMES)), 62)
        self.assertEqual(tuple(_TOOL_NAMES[60:62]), TOOLS)
        listed = json.loads((SCHEMA_CHANGES / '0.57-pin-tools.json').read_text(encoding='utf-8'))
        self.assertEqual(set(listed), {'structure'})
        self.assertEqual(set(listed['structure']), set(TOOLS))

    def test_the_closed_requests(self):
        self.assertEqual(pin_request('pin', CHAT, MID), {'@type': 'pinChatMessage', 'chat_id': CHAT, 'message_id': MID,
                                                         'disable_notification': True, 'only_for_self': False})
        self.assertEqual(pin_request('unpin', CHAT, MID), {'@type': 'unpinChatMessage', 'chat_id': CHAT,
                                                           'message_id': MID})
        with self.assertRaises(ValueError):
            pin_request('unpin_all', CHAT, MID)


class CapabilityBoundaryTests(unittest.TestCase):
    """Criterion 5: send + read + pin_messages at the broker and the proxy; no edit capability grants a pin."""

    def test_broker_refuses_before_any_provider_read_unless_every_capability(self):
        ops = (('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'pin'}),
               ('apply_prepared_pin_change', {'draft_id': DID, 'approved': True}))
        for enabled in ((), ('send', 'read'), ('pin_messages',), ('send', 'pin_messages'), ('read', 'pin_messages'),
                        EDITS.enabled_capabilities, ('send', 'read', 'delete_own_message')):
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
        for policy, enabled in ((RuntimePolicy(), False), (EDITS, False),
                                (RuntimePolicy(enabled_capabilities=('send', 'pin_messages')), False), (PIN, True)):
            connects = []

            def connector(path):
                connects.append(path)
                raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            with self.subTest(policy=policy.enabled_capabilities):
                prepared = proxy.prepare_pin_change(PreparePinChangeRequest(chat=CHAT, message_id=MID, action='pin'))
                self.assertEqual(prepared.status, 'unavailable')
                applied = proxy.apply_prepared_pin_change(ApplyPreparedPinChangeRequest(draft_id=DID, approved=True))
                if enabled:
                    self.assertTrue(connects)
                    self.assertEqual((applied.status, applied.next_action), ('outcome_unknown', 'check_send_status'))
                else:
                    self.assertEqual(connects, [])
                    self.assertEqual((applied.status, applied.detail),
                                     ('error', 'request rejected before dispatch: capability_disabled'))


class PrepareTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: refusals, the already states and the closed preview."""

    def test_pin_prepares_a_bound_preview_of_another_members_message(self):
        value = self.prepare('pin')
        self.assertEqual((value['action'], value['account_id'], value['chat']),
                         ('pin', ACCOUNT, {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'}))
        self.assertEqual((value['message']['message_id'], value['message']['sender']),
                         (MID, {'kind': 'user', 'id': OTHER}))
        self.assertEqual(value['message']['text'], MARK + archive.escape('Meeting at 10'))
        self.assertEqual(PinChangeDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertEqual((self.changes(), self.approvals), ([], []))

    def test_unpin_prepares_for_a_pinned_message(self):
        self.raw.message = message(pinned=True)
        self.assertEqual(self.prepare('unpin')['action'], 'unpin')

    def test_already_pinned_and_not_pinned_create_no_draft(self):
        self.raw.message = message(pinned=True)
        self.assertEqual(self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'pin'}),
                         {'status': 'already_pinned', 'pin_change': None, 'refusal': None, 'detail': ALREADY_PINNED})
        self.raw.message = message(pinned=False)
        self.assertEqual(self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'unpin'}),
                         {'status': 'not_pinned', 'pin_change': None, 'refusal': None, 'detail': NOT_PINNED})
        self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_ineligible_messages_refuse_without_draft(self):
        markup = {'@type': 'replyMarkupInlineKeyboard', 'rows': []}
        cases = [
            ('not_pinnable', lambda raw: raw.properties.update(can_be_pinned=False)),
            ('unsupported_source_content', lambda raw: raw.message.update(content={'@type': 'messageSticker'})),
            ('unsupported_source_content', lambda raw: raw.message.update(
                content={'@type': 'messagePinMessage', 'message_id': 5})),
            ('hidden_source_content', lambda raw: raw.message.update(reply_markup=markup)),
            ('hidden_source_content', lambda raw: raw.message['content']['text']['entities'].append(
                entity('textEntityTypeTextUrl', 0, 3, url='https://evil.invalid'))),
            ('unsupported_source_state', lambda raw: raw.message.update(
                sending_state={'@type': 'messageSendingStatePending', 'sending_id': 1})),
            ('unsupported_source_state', lambda raw: raw.message.update(
                scheduling_state={'@type': 'messageSchedulingStateSendAtDate', 'send_date': 1})),
        ]
        for code, change in cases:
            with self.subTest(code=code, change=change):
                self.setUp()
                change(self.raw)
                result = self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'pin'})
                self.assertEqual(result, {'status': 'refused', 'pin_change': None, 'refusal': code, 'detail': REFUSED})
                self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assertEqual(self.changes(), [])

    def test_missing_secret_and_malformed_messages_are_unavailable(self):
        for change in (lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'gone'}),
                       lambda raw: raw.message.pop('is_pinned'),
                       lambda raw: raw.message.update(is_pinned=1),
                       lambda raw: raw.properties.pop('can_be_pinned'),
                       lambda raw: raw.message.update(chat_id=CHAT + 1),
                       lambda raw: raw.chats.update({CHAT: chat(CHAT, 'Secret', 'secret')}),
                       lambda raw: raw.chats.pop(CHAT)):
            with self.subTest(change=change):
                self.setUp()
                change(self.raw)
                result = self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'pin'})
                self.assertEqual(result, {'status': 'unavailable', 'pin_change': None, 'refusal': None,
                                          'detail': PIN_UNAVAILABLE})
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_generic_draft_tools_and_other_apply_tools_never_touch_pin_drafts(self):
        did = self.prepare()['draft_id']
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*PIN.enabled_capabilities, 'artifacts',
                                                                  'reply_text_send', 'edit_own_text', 'archive_chats',
                                                                  'edit_own_caption'))
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {}), ('update_reply_draft', {'text': 'x'})):
            with self.subTest(op=op):
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        for op, detail in (('send_prepared_text', 'draft kind does not match send tool'),
                           ('send_prepared_artifact', 'draft kind does not match send tool'),
                           ('apply_prepared_archive', 'draft kind does not match apply tool'),
                           ('apply_prepared_text_edit', 'draft kind does not match apply tool'),
                           ('apply_prepared_caption_edit', 'draft kind does not match apply tool')):
            result = self.dispatch(op, {'draft_id': did, 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', detail), op)
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_the_pin_apply_tool_refuses_every_other_kind(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*PIN.enabled_capabilities, 'edit_own_text'))
        self.raw.message = archive.own_message(is_pinned=False)
        edit = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'Hello there'})
        text = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': 'plain'})
        for did in (edit['text_edit']['draft_id'], text['draft_id']):
            result = self.apply(did)
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match apply tool'))
        self.assertEqual((self.approvals, self.changes()), ([], []))

    def test_reprepare_supersedes_only_the_same_owner_and_message(self):
        first = self.prepare()['draft_id']
        foreign = self.prepare(client=FOREIGN)['draft_id']
        second = self.prepare()['draft_id']
        self.assertEqual(self.apply(first)['status'], 'expired')
        self.assertEqual(self.broker._drafts._entries[foreign].state, 'pending')
        self.assertEqual(self.apply(second)['status'], 'applied')

    def test_proxy_validates_preview_digest_and_request_scope(self):
        payload = {'chat': CHAT, 'message_id': MID, 'action': 'pin'}
        broker_result = self.dispatch('prepare_pin_change', payload)

        def proxy(result, request=PreparePinChangeRequest(**payload)):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=PIN)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close)
            return client.prepare_pin_change(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for key, value in (('action', 'unpin'), ('preview_sha256', 'f' * 64), ('account_id', ACCOUNT + 1),
                           ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key):
                result = copy.deepcopy(broker_result)
                result['pin_change'][key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        for other in ({'action': 'unpin'}, {'message_id': MID + 1}, {'chat': CHAT - 1}):
            with self.subTest(other=other):
                self.assertEqual(proxy(broker_result, PreparePinChangeRequest(**{**payload, **other})).status,
                                 'unavailable')


class ApplyTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: revalidation, one closed call, the bounded observation, the 4xx read-back, no retry."""

    def test_one_exact_silent_pin_for_everyone_then_receipt_replay(self):
        value = self.prepare('pin')
        result = self.apply(value['draft_id'])
        self.assertEqual(result, {'status': 'applied', 'draft_id': value['draft_id'], 'chat_id': CHAT,
                                  'message_id': MID, 'is_pinned': True,
                                  'detail': 'TDLib applied the pin; the pinned state was observed right after the call'})
        [request] = self.changes()
        self.assertEqual(request, {'@type': 'pinChatMessage', 'chat_id': CHAT, 'message_id': MID,
                                   'disable_notification': True, 'only_for_self': False, '@extra': request['@extra']})
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['pin_change_preview'], dialog['sha256'], dialog['display_name']),
                         ('pin_change', value, value['message']['source_sha256'], 'Pin change'))
        replay = self.apply(value['draft_id'])
        self.assertEqual((replay['status'], replay['is_pinned']), ('applied', True))
        self.assertIn('no new change', replay['detail'])
        self.assertEqual((len(self.changes()), len(self.approvals)), (1, 1))

    def test_one_exact_unpin(self):
        self.raw.message = message(pinned=True)
        result = self.apply(self.prepare('unpin')['draft_id'])
        self.assertEqual((result['status'], result['is_pinned']), ('applied', False))
        self.assertEqual(result['detail'], 'TDLib applied the unpin; the unpinned state was observed right after the call')
        [request] = self.changes()
        self.assertEqual(request, {'@type': 'unpinChatMessage', 'chat_id': CHAT, 'message_id': MID,
                                   '@extra': request['@extra']})

    def test_the_final_state_is_a_bounded_get_message_poll(self):
        self.raw.pin_mode = 'late'  # visible only after a few reads
        self.assertEqual(self.apply(self.prepare()['draft_id'])['status'], 'applied')
        self.setUp()
        self.raw.pin_mode = 'no_update'  # ok, but the pin is never observed within the bound
        started = time.monotonic()
        result = self.apply(self.prepare()['draft_id'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result['status'], result['is_pinned'], result['next_action']),
                         ('outcome_unknown', None, 'check_send_status'))
        self.assertEqual(len(self.changes()), 1)

    def test_a_rejection_is_read_back_already_in_the_target_state_or_failed(self):
        # A racing pin's 4xx is not "not modified": one read-back classifies it.
        self.raw.pin_mode = 'race'
        did = self.prepare('pin')['draft_id']
        result = self.apply(did)
        self.assertEqual((result['status'], result['is_pinned']), ('already_pinned', True))
        self.assertEqual(result['detail'], 'TDLib rejected the change (code 400); read back right after, the message '
                                           'is pinned; this attempt changed nothing')
        self.assertNotIn('next_action', result)
        replay = self.apply(did)
        self.assertEqual((replay['status'], replay['is_pinned']), ('already_pinned', True))
        self.setUp()
        self.raw.message = message(pinned=True)
        self.raw.pin_mode = 'race'
        result = self.apply(self.prepare('unpin')['draft_id'])
        self.assertEqual((result['status'], result['is_pinned']), ('not_pinned', False))
        for mode, readback in (('rejected', None), ('flood', None),
                               ('rejected', {'@type': 'error', 'code': 404, 'message': 'gone'})):
            with self.subTest(mode=mode, readback=readback):
                self.setUp()
                self.raw.pin_mode, self.raw.readback = mode, readback
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['is_pinned']), ('failed', None))
                self.assertEqual(result['detail'], f'TDLib rejected the change (code {ERRORS[mode]})')
                self.assertEqual(self.apply(did)['status'], 'failed')
                self.assertEqual(len(self.changes()), 1)

    def test_unproven_errors_and_lost_or_malformed_results_are_unknown_and_never_retried(self):
        for mode in ('server_error', 'negative', 'codeless', 'lost', 'bad_result'):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.pin_mode = mode
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['is_pinned']), ('outcome_unknown', None))
                self.assertIn('do not retry', result['detail'])
                self.assertEqual(self.apply(did)['status'], 'outcome_unknown')
                self.assertEqual(len(self.changes()), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))

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
        self.assertEqual(self.changes(), [])
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_revalidation_before_the_dialog_refuses_without_dialog_or_attempt(self):
        for status, detail, change in (
                ('message_changed', 'message changed', lambda raw: raw.message.update(edit_date=1700000100)),
                ('message_changed', 'message changed', lambda raw: raw.message['content']['text'].update(text='Other')),
                ('message_changed', 'message changed', lambda raw: setattr(raw, 'message', {
                    '@type': 'error', 'code': 404, 'message': 'x'})),
                ('message_changed', 'message changed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                ('message_changed', 'message can no longer be pinned',
                 lambda raw: raw.properties.update(can_be_pinned=False)),
                ('already_pinned', 'message is already pinned; no change was attempted',
                 lambda raw: raw.message.update(is_pinned=True))):
            with self.subTest(detail=detail, change=change):
                self.setUp()
                did = self.prepare()['draft_id']
                change(self.raw)
                result = self.apply(did)
                self.assertEqual((result['status'], result['chat_id'], result['message_id']), (status, CHAT, MID))
                self.assertTrue(result['detail'].startswith(detail), result['detail'])
                self.assertEqual((self.approvals, self.changes()), ([], []))

    def test_changes_during_the_dialog_are_local_failures_or_expire_without_attempt(self):
        for expected, change in (('local_failed', lambda raw: raw.message.update(edit_date=1700000100)),
                                 ('local_failed', lambda raw: raw.message.update(is_pinned=True)),
                                 ('local_failed', lambda raw: raw.properties.update(can_be_pinned=False)),
                                 ('expired', lambda raw: setattr(raw, 'account', 9)),
                                 ('expired', lambda raw: setattr(self.broker, '_policy', EDITS))):
            with self.subTest(expected=expected):
                self.setUp()
                did = self.prepare()['draft_id']

                def approve(**kw):
                    change(self.raw)
                    return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.broker._apply_prepared_pin_change(
                    ApplyPreparedPinChangeRequest(draft_id=did, approved=True), client_id=CLIENT).status, expected)
                self.assertEqual(self.changes(), [])

    def test_guard_failures_inside_the_provider_lock_are_local_failed_without_transport(self):
        for name, change in (('capability', lambda: setattr(self.broker, '_policy', EDITS)),
                             ('stale_claim', lambda: setattr(self.broker._drafts, '_clock', lambda: time.time() + 10**6))):
            with self.subTest(name=name):
                self.setUp()
                did = self.prepare()['draft_id']
                real = self.raw.send

                def send(request, real=real, change=change):
                    real(request)
                    if request['@type'] == 'getMessageProperties' and self.approvals:
                        change()
                self.raw.send = send
                result = self.broker._apply_prepared_pin_change(
                    ApplyPreparedPinChangeRequest(draft_id=did, approved=True), client_id=CLIENT)
                self.assertEqual((result.status, result.is_pinned), ('local_failed', None))
                self.assertEqual(self.changes(), [])

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
        self.assertEqual(len(self.changes()), 1)
        self.assertIn('applied', [result['status'] for result in results])

    def test_account_switch_after_the_call_is_outcome_unknown_never_expired(self):
        self.raw.pin_mode = 'switch_account'
        did = self.prepare()['draft_id']
        self.assertEqual(self.apply(did), {'status': 'outcome_unknown', 'draft_id': did, 'chat_id': None,
                                           'message_id': None, 'is_pinned': None, 'detail': UNKNOWN,
                                           'next_action': 'check_send_status'})
        self.assertEqual(self.apply(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(len(self.changes()), 1)

    def test_status_is_bound_to_pin_messages_and_never_reports_sent(self):
        did = self.prepare()['draft_id']
        pending = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((pending['status'], pending['evidence']), ('pending', 'local_pending'))
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'none')
        self.broker._policy = PIN
        self.assertEqual(self.apply(did)['status'], 'applied')
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
        with self.assertRaises(CompatibilityError):
            self.apply(did)


class DialogTests(unittest.TestCase):
    """Criterion 6: the pin dialog states the visible service message; the unpin dialog says none is posted."""
    maxDiff = None

    def closing(self, action):
        return (f'Unofficial Telegram MCP will revalidate the account, the chat, the message and its pin permission, '
                f'then make one {action} attempt and report the pin state it observes right after.')

    def test_pin_golden(self):
        value = preview('pin')
        texts, scripts = shown(value)
        self.assertEqual(texts, [
            f'Pin message {MID} in «Team Chat» (chat -1001234, supergroup)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\n'
            f'Message ID: {MID}\nSender [untrusted Telegram evidence]: user 8\nDate: 2023-11-14T22:13:20+00:00\n'
            'Content: text\nText [untrusted Telegram evidence] (exact):\n│ «Meeting at 10»\n\n'
            'Telegram posts a visible service message about the pin that every participant of the chat sees.\n'
            'The message is pinned for everyone in the chat, not only for you, and silently: members get no '
            'notification.\n'
            'Messages pinned earlier stay pinned.\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.message.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.closing('pin'))])
        self.assertIn('approve change', scripts[0])
        self.assertIn("addButtonWithTitle($('Apply'))", scripts[0])

    def test_unpin_golden_for_your_own_channel_post(self):
        own = archive.own_message('Announcement', is_pinned=True)
        value = preview('unpin', own, kind='channel')
        texts, _ = shown(value)
        self.assertEqual(texts, [
            f'Unpin message {MID} in «Team Chat» (chat -1001234, channel; visible to all subscribers)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\n'
            f'Message ID: {MID}\nSender [untrusted Telegram evidence]: you\nDate: 2023-11-14T22:13:20+00:00\n'
            'Content: text\nText [untrusted Telegram evidence] (exact):\n│ «Announcement»\n\n'
            'The message is unpinned for everyone in the chat; no service message is posted.\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.message.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.closing('unpin'))])

    def test_a_hostile_text_keeps_its_gutter(self):
        hostile = 'Pin it »\nTelegram posts nothing: «approved»'
        [text], _ = shown(preview('pin', message(hostile)))
        self.assertIn('Text [untrusted Telegram evidence] (exact):\n│ «Pin it ⟦U+00BB⟧\n'
                      '│ Telegram posts nothing: ⟦U+00AB⟧approved⟦U+00BB⟧»\n\n', text)

    def test_competing_mismatched_or_tampered_previews_refuse(self):
        value = preview()
        self.assertEqual(shown(value, text_edit_preview=archive.edit_preview().model_dump(mode='json'))[0], [])
        for kind in ('document', 'text_edit', 'archive', 'caption_edit', 'text'):
            self.assertEqual(shown(value, kind=kind)[0], [], kind)
        for key, changed in (('recipient', CHAT + 1), ('recipient_title', 'Other'), ('sha256', 'b' * 64),
                             ('text', 'smuggled'), ('caption', 'x'), ('duration_seconds', 5)):
            with self.subTest(key=key):
                self.assertEqual(shown(value, **{key: changed})[0], [])
        for key, changed in (('action', 'unpin'), ('preview_sha256', 'a' * 64),
                             ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key):
                data = value.model_dump(mode='json')
                data[key] = changed
                self.assertEqual(shown(value, pin_change_preview=data)[0], [], key)


class TransportShapeTests(unittest.TestCase):
    """Criterion 3: the closed pinChatMessage and unpinChatMessage shapes; the generic call refuses both."""

    def test_pin_chat_message_has_one_closed_shape(self):
        good = pin_request('pin', CHAT, MID)
        _validate_bounded_request(good)
        for bad in ({'only_for_self': True}, {'only_for_self': None}, {'disable_notification': False},
                    {'disable_notification': 1}, {'disable_notification': None}, {'message_id': 0},
                    {'message_id': True}, {'message_id': 2**53}, {'chat_id': 0}, {'chat_id': '5'}, {'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})
        for key in ('disable_notification', 'only_for_self', 'message_id'):
            missing = dict(good)
            del missing[key]
            with self.subTest(missing=key), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(missing)

    def test_unpin_chat_message_has_exact_keys(self):
        good = pin_request('unpin', CHAT, MID)
        _validate_bounded_request(good)
        for bad in ({'disable_notification': True}, {'only_for_self': False}, {'message_id': 0}, {'chat_id': 0},
                    {'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request({'@type': 'unpinChatMessage', 'chat_id': CHAT})

    def test_the_generic_call_refuses_both_and_unpin_all_stays_unreachable(self):
        raw = Raw()
        client = TDLibClient(raw=raw)
        client._ready = True
        for payload in (pin_request('pin', CHAT, MID), pin_request('unpin', CHAT, MID),
                        {'@type': 'unpinAllChatMessages', 'chat_id': CHAT},
                        {'@type': 'unpinAllForumTopicMessages', 'chat_id': CHAT, 'forum_topic_id': 1}):
            with self.subTest(payload=payload['@type']), self.assertRaises(ForbiddenTDLibRequest):
                client._call(payload)
        for payload in ({'@type': 'unpinAllChatMessages', 'chat_id': CHAT},
                        {**pin_request('pin', CHAT, MID), 'only_for_self': True}):
            with self.subTest(change=payload), self.assertRaises(MessageSendNotAttempted):
                client._change_transport(payload, pre_transport_guard=lambda: True)
        with self.assertRaises(MessageSendNotAttempted):
            client._change_transport(pin_request('pin', CHAT, MID), pre_transport_guard=lambda: False)
        self.assertEqual(raw.sent, [])


class RegistryTests(unittest.TestCase):
    def test_the_spec_needs_a_pinnable_message_in_the_other_state(self):
        PinChangeSpec(snapshot=snapshot(message(pinned=False)), action='pin')
        PinChangeSpec(snapshot=snapshot(message(pinned=True)), action='unpin')
        for raw_message, action, properties in ((message(pinned=True), 'pin', None),
                                                (message(pinned=False), 'unpin', None),
                                                (message(), 'pin', {**PROPERTIES, 'can_be_pinned': False}),
                                                (message(), 'unpin_all', None)):
            with self.subTest(action=action), self.assertRaises(ValueError):
                PinChangeSpec(snapshot=snapshot(raw_message, properties=properties), action=action)

    def test_a_pin_draft_is_dedicated_unbacked_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(client_id=CLIENT, account_id=ACCOUNT)
            spec = PinChangeSpec(snapshot=snapshot(), action='pin')
            draft = registry.prepare_change(owner=owner, chat_id=CHAT, chat_title='Team Chat', change=spec)
            self.assertEqual((draft.kind, draft.display_name, draft.pin_change), ('pin_change', 'Pin change', spec))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            self.assertEqual(registry.extra_capabilities(draft.draft_id, owner=owner), ())
            claim = registry.claim(draft.draft_id, owner=owner, approved=True)
            receipt = registry.finish(draft.draft_id, owner=owner, claim=claim, status='applied', observed=True)
            self.assertEqual((receipt.status, receipt.observed), ('applied', True))


class _Service:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def prepare_pin_change(self, request):
        self.calls.append(request)
        return PreparePinChangeResponse(status='unavailable', detail=PIN_UNAVAILABLE)

    def apply_prepared_pin_change(self, request):
        self.calls.append(request)
        return ApplyPreparedPinChangeResponse(status='dialog_unavailable', draft_id=request.draft_id,
                                              detail='synthetic')


GOOD = {'prepare_pin_change': {'chat': CHAT, 'message_id': MID, 'action': 'pin'},
        'apply_prepared_pin_change': {'draft_id': DID, 'approved': True}}


class SdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemas_titles_and_annotations(self):
        from tool_listing import full_listing
        tools = await full_listing()
        prepare, apply = (tools[name] for name in TOOLS)
        self.assertEqual(set(prepare.input_schema['properties']), {'chat', 'message_id', 'action'})
        self.assertEqual(sorted(prepare.input_schema['required']), ['action', 'chat', 'message_id'])
        self.assertEqual(set(apply.input_schema['properties']), {'draft_id', 'approved'})
        for code in ('already_pinned', 'not_pinned', 'not_pinnable', 'hidden_source_content'):
            self.assertIn(f'"{code}"', json.dumps(prepare.output_schema))
        for status in ('already_pinned', 'not_pinned', 'declined', 'timeout', 'dialog_unavailable',
                       'check_send_status'):
            self.assertIn(f'"{status}"', json.dumps(apply.output_schema))
        for tool in (prepare, apply):
            self.assertIs(tool.input_schema['additionalProperties'], False)
            self.assertTrue(tool.title)
        self.assertEqual((prepare.annotations.read_only_hint, prepare.annotations.destructive_hint), (True, False))
        self.assertEqual(apply.annotations, tools['apply_prepared_archive'].annotations)  # reversible: not destructive

    async def test_listed_only_with_every_capability_and_refused_at_the_guard_otherwise(self):
        cases = ((RuntimePolicy(enabled_capabilities=()), 'pin_messages, send, read'),
                 (RuntimePolicy(), 'pin_messages'), (EDITS, 'pin_messages'),
                 (RuntimePolicy(enabled_capabilities=('send', 'pin_messages')), 'read'), (PIN, None))
        for policy, missing in cases:
            service = _Service()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                listed = {tool.name for tool in (await consumer.list_tools()).tools}
                self.assertEqual(listed, set(available_tools(policy.enabled_capabilities)))
                self.assertEqual(set(TOOLS) <= listed, missing is None)
                for name in TOOLS:
                    with self.subTest(policy=policy.enabled_capabilities, tool=name):
                        result = await consumer.call_tool(name, GOOD[name])
                        if missing is None:
                            self.assertFalse(result.is_error, result.content)
                            continue
                        self.assertTrue(result.is_error)
                        self.assertIn(f'capability_disabled: {missing} (', result.content[0].text)
            self.assertEqual(service.calls, [] if missing else [PreparePinChangeRequest(**GOOD[TOOLS[0]]),
                                                                ApplyPreparedPinChangeRequest(**GOOD[TOOLS[1]])])

    async def test_closed_inputs_never_reach_the_service(self):
        service = _Service()
        bad = {'prepare_pin_change': ({'action': 'unpin_all'}, {'action': 'Pin'}, {'action': None},
                                      {'message_id': 0}, {'chat': 0}, {'only_for_self': True},
                                      {'disable_notification': False}),
               'apply_prepared_pin_change': ({'approved': 'true'}, {'draft_id': 'draft_bad'}, {'extra': 1})}
        async with Client(build_server(service_factory=lambda: service, policy=PIN)) as consumer:
            for name, cases in bad.items():
                for change in cases:
                    with self.subTest(tool=name, change=change):
                        try:
                            result = await consumer.call_tool(name, GOOD[name] | change)
                        except MCPError:
                            continue
                        self.assertTrue(result.is_error)
                        self.assertIn('invalid_arguments', result.content[0].text)
        self.assertEqual(service.calls, [])

    async def test_prepare_apply_replay_and_status_through_the_official_client(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='p')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        raw, approvals = Raw(), []
        client = TDLibClient(raw=raw)
        client.CHANGE_OBSERVATION_SECONDS, client.CHANGE_POLL_SECONDS = 0.3, 0.01
        broker = Broker(socket_path=root / 'b.sock', lock_path=root / 'b.lock',
                        artifact_store=ArtifactStore(cache_dir=root / 'cache'), client_factory=lambda: client,
                        policy=PIN, approval_prompt=lambda **facts: approvals.append(facts) or True)
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(broker.shutdown)
        self.assertTrue(broker.wait_until_ready(timeout=10))
        proxy = BrokerClient(socket_path=root / 'b.sock', policy=PIN, restart_callback=lambda: None)
        self.addCleanup(proxy.close)
        async with Client(build_server(service_factory=lambda: proxy, policy=PIN)) as consumer:
            async def call(name, arguments):
                result = await consumer.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.content)
                return result.structured_content
            prepared = await call('prepare_pin_change', GOOD['prepare_pin_change'])
            self.assertEqual(prepared['status'], 'prepared', prepared)
            did = prepared['pin_change']['draft_id']
            self.assertEqual((await call('apply_prepared_pin_change', {'draft_id': did, 'approved': False}))['status'],
                             'declined')
            applied = await call('apply_prepared_pin_change', {'draft_id': did, 'approved': True})
            self.assertEqual((applied['status'], applied['is_pinned']), ('applied', True))
            replay = await call('apply_prepared_pin_change', {'draft_id': did, 'approved': True})
            self.assertEqual(replay['status'], 'applied')
            again = await call('prepare_pin_change', GOOD['prepare_pin_change'])
            self.assertEqual(again['status'], 'already_pinned')
        self.assertEqual((len([r for r in raw.sent if r['@type'] == 'pinChatMessage']), len(approvals)), (1, 1))


if __name__ == '__main__':
    unittest.main()
