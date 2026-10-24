"""0.57 part 3: one owner-approved deletion of one own message for everyone (F17 rest).

Synthetic raw transport only: test_archive_edit's, answering deleteMessages the way the pinned TDLib does. The local
copy goes first (an updateDeleteMessages before any answer) and a later getMessage answers 404 locally; a message
TDLib does not hold is answered ok without any server request; after a server failure the message is fetched again
from the server. Projections, drafts, broker, proxy, server and TDLib reducers are production code. No live TDLib and
no owner dialog: the prompt is a fake, and the dialog goldens patch osascript.
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
import test_caption_edit as caption_suite
from approval_layout import tail as dialog_tail
from schema_changes import DIRECTORY as SCHEMA_CHANGES
from test_archive_edit import ACCOUNT, CHAT, CLIENT, DID, ERRORS, EXPIRY, FOREIGN, MARK, MID, chat, entity
from telegram_search_mcp import broker_client as broker_client_module
from telegram_search_mcp.approval_prompt import DETAILS, NOT_GIVEN, ApprovalDecision, confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import SEND_OPERATIONS, Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS, CompatibilityError
from telegram_search_mcp.delete_drafts import (ApplyPreparedMessageDeleteRequest, ApplyPreparedMessageDeleteResponse,
    DeleteSnapshot, MessageDeleteDraftPreview, MessageDeleteSpec, PrepareMessageDeleteRequest,
    PrepareMessageDeleteResponse, UNAVAILABLE as DELETE_UNAVAILABLE, delete_request)
from telegram_search_mcp.draft_kinds import DRAFT_KINDS
from telegram_search_mcp.edit_drafts import EditChat
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES, available_tools, build_server
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, TDLibClient,
                                        _validate_bounded_request)

TOOLS = ('prepare_message_delete', 'apply_prepared_message_delete')
DELETE = RuntimePolicy(enabled_capabilities=('send', 'read', 'delete_own_message'))
OTHERS = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text', 'edit_own_caption',
                                             'edit_own_styled_text', 'archive_chats', 'pin_messages'))
PREPARED = 'immutable message deletion draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for deletion for everyone; no draft was created'
APPLIED = 'TDLib deleted the message for everyone; the server confirmed the deletion'
# The disclosure of TDLib's logged deletion, replayed after a restart: every unknown delete outcome names it.
UNKNOWN = ('the deletion was not confirmed; TDLib may still complete a deletion it logged, also after a restart, so '
           'the message may disappear later; do not retry automatically')
REPLAYED = 'previous provider attempt receipt; no new deletion was attempted'
REPLAYED_UNKNOWN = ('previous provider attempt receipt; the deletion was not confirmed and TDLib may still complete '
                    'it, also after a restart; do not retry automatically')
AUTHORITY_UNKNOWN = ('current receipt authority is unavailable; TDLib may still complete a logged deletion, also '
                     'after a restart; do not retry automatically')
PROPERTIES = {**archive.PROPERTIES, 'can_be_deleted_only_for_self': False, 'can_be_deleted_for_all_users': True}
FAILED = 'TDLib rejected the deletion (code {}); read back right after, the message is still in the chat'


def own(text='Hello world', **changes):
    return archive.own_message(text, **changes)


class Raw(archive.Raw):
    """test_archive_edit's transport answering deleteMessages like the pinned TDLib; `delete_mode` shapes the answer.

    Only a message TDLib holds (any successful getMessage loads it) is deleted on the server; one it does not hold is
    answered ok with nothing sent. A deletion is local first: updateDeleteMessages precedes every answer, and a
    getMessage afterwards answers 404 from the local deleted set, unless a server failure made TDLib fetch the
    message again (`on_server` decides what that fetch finds; `readback` answers it verbatim)."""

    def __init__(self):
        super().__init__()
        self.properties = dict(PROPERTIES)
        self.delete_mode = 'ok'
        self.loaded = False
        self.local_deleted = False
        self.refetched = False
        self.on_server = True
        self.readback = None

    def send(self, request):
        kind = request['@type']
        if kind == 'getMessage' and self.local_deleted:
            self.sent.append(copy.deepcopy(request))
            if self.refetched and self.readback is not None:
                answer = self.readback
            elif self.refetched and self.on_server:
                answer = copy.deepcopy(self.message)
            else:
                answer = {'@type': 'error', 'code': 404, 'message': 'Not Found'}
            self.events.append({**answer, '@extra': request['@extra']})
            return
        if kind != 'deleteMessages':
            if kind == 'getMessage' and self.message.get('@type') == 'message':
                self.loaded = True
            return super().send(request)
        self.sent.append(copy.deepcopy(request))
        if not self.loaded:  # TDLib: nothing to delete on the server, so ok without any server request
            self.events.append({'@type': 'ok', '@extra': request['@extra']})
            return
        mode = self.delete_mode
        self.local_deleted = True
        self.events.append({'@type': 'updateDeleteMessages', 'chat_id': request['chat_id'],
                            'message_ids': list(request['message_ids']), 'is_permanent': True, 'from_cache': False})
        if mode == 'lost':
            self.fail_receive = True
            return
        if mode == 'switch_account':
            self.account = 8
        if mode == 'auth_closed':
            self.events.append({'@type': 'updateAuthorizationState',
                                'authorization_state': {'@type': 'authorizationStateClosed'}})
            return
        if mode in ERRORS:
            self.refetched = True  # on_failed_message_deletion: fetched again from the server
            error = {'@type': 'error', 'message': 'MESSAGE_DELETE_FORBIDDEN PRIVATE', '@extra': request['@extra']}
            self.events.append(error if ERRORS[mode] is None else {**error, 'code': ERRORS[mode]})
            return
        if mode in ('ok', 'switch_account'):
            self.on_server = False
        answer = {'bad_result': {'@type': 'message'}, 'ok_extra': {'@type': 'ok', 'count': 1}}.get(mode, {'@type': 'ok'})
        self.events.append({**answer, '@extra': request['@extra']})


def snapshot(raw_message=None, kind='supergroup', properties=None, chat_id=CHAT, title='Team Chat'):
    return DeleteSnapshot.create(chat(chat_id, title, kind), chat_id, raw_message or own(), MID,
                                 properties or dict(PROPERTIES), ACCOUNT)


def preview(raw_message=None, kind='supergroup', title='Team Chat'):
    spec = MessageDeleteSpec(snapshot=snapshot(raw_message, kind, title=title))
    return MessageDeleteDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
                                            chat=EditChat(chat_id=CHAT, title=title, kind=kind), spec=spec)


def shown(value, **kw):
    from approval_layout import logical
    seen = []
    arguments = dict(recipient_title=value.chat.title, recipient=value.chat.chat_id, display_name='Message deletion',
                     size_bytes=1, sha256=value.message.source_sha256, caption='', kind='message_delete',
                     message_delete_preview=value.model_dump(mode='json'))
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        assert confirm_approved_send(**arguments) is False
    return [logical(argv[-1]) for argv in seen], [argv[argv.index('-e') + 1] for argv in seen]


class Fixture:
    policy = DELETE

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

    def deletions(self):
        return [request for request in self.raw.sent if request['@type'] == 'deleteMessages']

    def after_deletion(self):
        """Every provider request after the one deletion but the broker's account checks."""
        types = [request['@type'] for request in self.raw.sent]
        return [kind for kind in types[types.index('deleteMessages') + 1:]
                if kind not in ('getAuthorizationState', 'getMe')]

    def prepare(self, client=CLIENT):
        result = self.dispatch('prepare_message_delete', {'chat': CHAT, 'message_id': MID}, client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['message_delete']

    def apply(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_message_delete', {'draft_id': did, 'approved': approved}, client)


class ContractTests(unittest.TestCase):
    def test_the_kind_row_the_operations_and_the_census(self):
        row = DRAFT_KINDS['message_delete']
        self.assertEqual((row.change, row.unbacked, row.dedicated, row.status, row.display_name),
                         (True, True, True, 'delete_own_message', 'Message deletion'))
        self.assertIs(row.spec_type(), MessageDeleteSpec)
        self.assertIn('prepare_message_delete', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        self.assertNotIn('apply_prepared_message_delete', broker_client_module.SHORT_DEADLINE_OPERATIONS)
        self.assertIn('apply_prepared_message_delete', SEND_OPERATIONS)
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(OPERATION_CAPABILITIES[name], 'delete_own_message')
                self.assertEqual(TOOL_OPERATIONS[name], name)
                self.assertIn(name, OPERATIONS)
                self.assertEqual(_TOOL_CAPABILITIES[name], ('delete_own_message', 'send', 'read'))
        self.assertEqual(len(_TOOL_NAMES), 64)
        self.assertEqual(len(set(_TOOL_NAMES)), 64)
        self.assertEqual(tuple(_TOOL_NAMES[62:64]), TOOLS)
        listed = json.loads((SCHEMA_CHANGES / '0.57-delete-tools.json').read_text(encoding='utf-8'))
        self.assertEqual(set(listed), {'structure'})
        self.assertEqual(set(listed['structure']), set(TOOLS))

    def test_the_one_closed_request(self):
        self.assertEqual(delete_request(CHAT, MID), {'@type': 'deleteMessages', 'chat_id': CHAT, 'message_ids': [MID],
                                                     'revoke': True})


class CapabilityBoundaryTests(unittest.TestCase):
    """Criterion 5: send + read + delete_own_message at the broker and the proxy; no other capability grants it."""

    def test_broker_refuses_before_any_provider_read_unless_every_capability(self):
        ops = (('prepare_message_delete', {'chat': CHAT, 'message_id': MID}),
               ('apply_prepared_message_delete', {'draft_id': DID, 'approved': True}))
        for enabled in ((), ('send', 'read'), ('delete_own_message',), ('send', 'delete_own_message'),
                        ('read', 'delete_own_message'), OTHERS.enabled_capabilities):
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
        for policy, enabled in ((RuntimePolicy(), False), (OTHERS, False),
                                (RuntimePolicy(enabled_capabilities=('send', 'delete_own_message')), False),
                                (DELETE, True)):
            connects = []

            def connector(path):
                connects.append(path)
                raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            with self.subTest(policy=policy.enabled_capabilities):
                prepared = proxy.prepare_message_delete(PrepareMessageDeleteRequest(chat=CHAT, message_id=MID))
                self.assertEqual(prepared.status, 'unavailable')
                applied = proxy.apply_prepared_message_delete(
                    ApplyPreparedMessageDeleteRequest(draft_id=DID, approved=True))
                if enabled:
                    self.assertTrue(connects)
                    self.assertEqual((applied.status, applied.next_action), ('outcome_unknown', 'check_send_status'))
                    self.assertEqual(applied.detail, 'broker response is unavailable; TDLib may still complete a '
                                                     'logged deletion, also after a restart; do not retry automatically')
                else:
                    self.assertEqual(connects, [])
                    self.assertEqual((applied.status, applied.detail),
                                     ('error', 'request rejected before dispatch: capability_disabled'))


class PrepareTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: refusals and the closed preview."""

    def test_prepares_a_bound_preview_of_an_own_message(self):
        value = self.prepare()
        self.assertEqual((value['account_id'], value['chat']),
                         (ACCOUNT, {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'}))
        self.assertEqual((value['message']['message_id'], value['message']['sender']),
                         (MID, {'kind': 'user', 'id': ACCOUNT}))
        self.assertEqual(value['message']['text'], MARK + archive.escape('Hello world'))
        self.assertEqual(MessageDeleteDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertEqual((self.deletions(), self.approvals), ([], []))

    def test_an_own_forward_and_an_own_media_message_prepare(self):
        self.raw.message = own(forward_info={'@type': 'messageForwardInfo', 'date': 1})
        self.assertTrue(self.prepare()['message']['is_forward'])
        self.raw.message = caption_suite.own_media('document', 'Report')
        self.assertEqual(self.prepare()['message']['content_kind'], 'document')

    def test_ineligible_messages_refuse_without_draft(self):
        markup = {'@type': 'replyMarkupInlineKeyboard', 'rows': []}

        def saved(raw):
            raw.chats[ACCOUNT] = chat(ACCOUNT, 'Saved Messages', 'private', native=ACCOUNT)
            raw.message = own(chat_id=ACCOUNT)
            raw.properties.update(can_be_deleted_for_all_users=False, can_be_deleted_only_for_self=True)
            return ACCOUNT
        cases = [
            ('not_outgoing', lambda raw: raw.message.update(
                is_outgoing=False, sender_id={'@type': 'messageSenderUser', 'user_id': 8})),
            ('saved_messages', saved),
            ('not_deletable_for_all', lambda raw: raw.properties.update(can_be_deleted_for_all_users=False,
                                                                        can_be_deleted_only_for_self=True)),
            ('not_deletable_for_all', lambda raw: raw.properties.update(can_be_deleted_for_all_users=False)),
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
                chat_id = change(self.raw) or CHAT
                result = self.dispatch('prepare_message_delete', {'chat': chat_id, 'message_id': MID})
                self.assertEqual(result, {'status': 'refused', 'message_delete': None, 'refusal': code,
                                          'detail': REFUSED})
                self.assertEqual(self.broker._drafts.known_ids(), set())
                self.assertEqual(self.deletions(), [])

    def test_missing_secret_and_malformed_messages_are_unavailable(self):
        for change in (lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'gone'}),
                       lambda raw: raw.message.pop('is_outgoing'),
                       lambda raw: raw.properties.pop('can_be_deleted_for_all_users'),
                       lambda raw: raw.properties.update(can_be_deleted_for_all_users=1),
                       lambda raw: raw.properties.update(can_be_deleted_only_for_self=None),
                       lambda raw: raw.message.update(chat_id=CHAT + 1),
                       lambda raw: raw.message.update(id=MID + 1),
                       lambda raw: raw.chats.update({CHAT: chat(CHAT, 'Secret', 'secret')}),
                       lambda raw: raw.chats.pop(CHAT)):
            with self.subTest(change=change):
                self.setUp()
                change(self.raw)
                result = self.dispatch('prepare_message_delete', {'chat': CHAT, 'message_id': MID})
                self.assertEqual(result, {'status': 'unavailable', 'message_delete': None, 'refusal': None,
                                          'detail': DELETE_UNAVAILABLE})
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_generic_draft_tools_and_other_apply_tools_never_touch_delete_drafts(self):
        did = self.prepare()['draft_id']
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*DELETE.enabled_capabilities, 'artifacts',
                                                                  'reply_text_send', 'edit_own_text', 'archive_chats',
                                                                  'edit_own_caption', 'pin_messages'))
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {}), ('update_reply_draft', {'text': 'x'})):
            with self.subTest(op=op):
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        for op, detail in (('send_prepared_text', 'draft kind does not match send tool'),
                           ('send_prepared_artifact', 'draft kind does not match send tool'),
                           ('apply_prepared_archive', 'draft kind does not match apply tool'),
                           ('apply_prepared_text_edit', 'draft kind does not match apply tool'),
                           ('apply_prepared_caption_edit', 'draft kind does not match apply tool'),
                           ('apply_prepared_pin_change', 'draft kind does not match apply tool')):
            result = self.dispatch(op, {'draft_id': did, 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', detail), op)
        self.assertEqual((self.deletions(), self.approvals), ([], []))
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_the_delete_apply_tool_refuses_every_other_kind(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*DELETE.enabled_capabilities, 'edit_own_text',
                                                                  'pin_messages'))
        edit = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'Hello there'})
        self.raw.properties['can_be_pinned'], self.raw.message['is_pinned'] = True, False
        pin = self.dispatch('prepare_pin_change', {'chat': CHAT, 'message_id': MID, 'action': 'pin'})
        text = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': 'plain'})
        for did in (edit['text_edit']['draft_id'], pin['pin_change']['draft_id'], text['draft_id']):
            result = self.apply(did)
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match apply tool'))
        self.assertEqual((self.approvals, self.deletions()), ([], []))

    def test_reprepare_supersedes_only_the_same_owner_and_message(self):
        first = self.prepare()['draft_id']
        foreign = self.prepare(client=FOREIGN)['draft_id']
        second = self.prepare()['draft_id']
        self.assertEqual(self.apply(first)['status'], 'expired')
        self.assertEqual(self.broker._drafts._entries[foreign].state, 'pending')
        self.assertEqual(self.apply(second)['status'], 'applied')

    def test_proxy_validates_preview_digest_and_request_scope(self):
        payload = {'chat': CHAT, 'message_id': MID}
        broker_result = self.dispatch('prepare_message_delete', payload)

        def proxy(result, request=PrepareMessageDeleteRequest(**payload)):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=DELETE)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close)
            return client.prepare_message_delete(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for key, value in (('preview_sha256', 'f' * 64), ('account_id', ACCOUNT + 1),
                           ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key):
                result = copy.deepcopy(broker_result)
                result['message_delete'][key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        for other in ({'message_id': MID + 1}, {'chat': CHAT - 1}):
            with self.subTest(other=other):
                self.assertEqual(proxy(broker_result, PrepareMessageDeleteRequest(**{**payload, **other})).status,
                                 'unavailable')


class ApplyTests(Fixture, unittest.TestCase):
    """Criteria 3 and 4: revalidation, one closed call after a locked read, the evidence order, no retry."""

    def test_one_exact_delete_for_everyone_after_a_locked_read_then_receipt_replay(self):
        value = self.prepare()
        did = value['draft_id']
        self.raw.sent.clear()
        result = self.apply(did)
        self.assertEqual(result, {'status': 'applied', 'draft_id': did, 'chat_id': CHAT, 'message_id': MID,
                                  'detail': APPLIED})
        [request] = self.deletions()
        self.assertEqual(request, {'@type': 'deleteMessages', 'chat_id': CHAT, 'message_ids': [MID], 'revoke': True,
                                   '@extra': request['@extra']})
        # ok counts only after a getMessage that succeeded under the same provider lock right before the deletion: the
        # locked revalidation (chat, message, eligibility, account), then the message itself as the last request.
        types = [request['@type'] for request in self.raw.sent]
        locked = types[:types.index('deleteMessages')][-7:]
        self.assertEqual(locked, ['getMe', 'getChat', 'getMessage', 'getMessageProperties', 'getMe', 'getMe',
                                  'getMessage'])
        self.assertEqual(self.raw.sent[types.index('deleteMessages') - 1]['message_id'], MID)
        self.assertEqual(self.after_deletion(), [])  # nothing reads after the ok
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['message_delete_preview'], dialog['sha256'], dialog['display_name']),
                         ('message_delete', value, value['message']['source_sha256'], 'Message deletion'))
        replay = self.apply(did)
        self.assertEqual(replay, {'status': 'applied', 'draft_id': did, 'chat_id': None, 'message_id': None,
                                  'detail': REPLAYED})
        self.assertEqual((len(self.deletions()), len(self.approvals)), (1, 1))

    def test_tdlibs_local_first_deletion_is_never_evidence(self):
        # The updateDeleteMessages that precedes every answer proves nothing: a lost answer or a closed session is
        # unknown, and nothing is read afterwards, since a getMessage would answer 404 from the local deleted set.
        for mode in ('lost', 'auth_closed'):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.delete_mode = mode
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['next_action']), ('outcome_unknown', 'check_send_status'))
                self.assertIn(result['detail'], (UNKNOWN, AUTHORITY_UNKNOWN))
                self.assertEqual(self.after_deletion(), [])
                self.assertEqual(len(self.deletions()), 1)

    def test_a_rejection_read_back_on_the_server_is_failed(self):
        for mode in ('rejected', 'flood'):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.delete_mode = mode
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual(result, {'status': 'failed', 'draft_id': did, 'chat_id': CHAT, 'message_id': MID,
                                          'detail': FAILED.format(ERRORS[mode])})
                self.assertEqual(self.after_deletion(), ['getMessage'])  # one read-back, fetched from the server
                replay = self.apply(did)
                self.assertEqual((replay['status'], replay['detail']), ('failed', REPLAYED))
                self.assertEqual(len(self.deletions()), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_a_rejection_without_the_message_found_again_is_unknown(self):
        # The generic "4xx proves no change" rule is not used: TDLib already deleted the local copy.
        cases = (('gone', None), ('other_id', {**own(), 'id': MID + 1}), ('other_chat', {**own(), 'chat_id': CHAT - 1}),
                 ('not_a_message', {'@type': 'messages'}), ('server_error', {'@type': 'error', 'code': 500,
                                                                             'message': 'x'}))
        for name, readback in cases:
            with self.subTest(case=name):
                self.setUp()
                self.raw.delete_mode = 'rejected'
                if readback is None:
                    self.raw.on_server = False
                self.raw.readback = readback
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['detail'], result['next_action']),
                                 ('outcome_unknown', UNKNOWN, 'check_send_status'))
                self.assertEqual(self.after_deletion(), ['getMessage'])
                replay = self.apply(did)
                self.assertEqual((replay['status'], replay['detail']), ('outcome_unknown', REPLAYED_UNKNOWN))
                self.assertEqual(len(self.deletions()), 1)

    def test_unproven_errors_and_lost_or_malformed_results_are_unknown_and_never_retried(self):
        for mode in ('server_error', 'negative', 'codeless', 'lost', 'bad_result', 'ok_extra'):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.delete_mode = mode
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['detail']), ('outcome_unknown', UNKNOWN))
                self.assertEqual(self.after_deletion(), [])  # only a 4xx is read back
                self.assertEqual(self.apply(did)['detail'], REPLAYED_UNKNOWN)
                self.assertEqual(len(self.deletions()), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_an_ok_for_a_message_tdlib_does_not_hold_is_never_reached(self):
        # TDLib answers ok without any server request for a message it does not hold, so the transport alone cannot
        # tell a deletion: the provider makes the deletion only right after the locked getMessage succeeded.
        raw = Raw()
        client = TDLibClient(raw=raw)
        client._ready = True
        self.assertEqual(client._delete_transport(delete_request(CHAT, MID), pre_transport_guard=lambda: True),
                         {'@type': 'ok', '@extra': raw.sent[-1]['@extra']})
        self.assertTrue(raw.on_server)  # nothing was deleted on the server
        did = self.prepare()['draft_id']
        real = self.raw.send
        reads = []

        def send(request, fail_at):
            if request['@type'] == 'getMessage' and self.approvals:
                reads.append(request)
                if len(reads) == fail_at:  # the message left TDLib's memory and the server read fails
                    self.raw.loaded = False
                    self.raw.sent.append(copy.deepcopy(request))
                    self.raw.events.append({'@type': 'error', 'code': 404, 'message': 'x',
                                            '@extra': request['@extra']})
                    return
            real(request)
        for fail_at in (1, 2):  # the locked revalidation's read, or the read right before the attempt
            with self.subTest(fail_at=fail_at):
                reads.clear()
                self.raw.send = lambda request, fail_at=fail_at: send(request, fail_at)
                result = self.apply(did)
                self.assertEqual((result['status'], result['detail']),
                                 ('local_failed', 'local revalidation failed; no provider change was attempted'))
                self.assertEqual(self.deletions(), [])
                self.setUp()
                did = self.prepare()['draft_id']
                real = self.raw.send

    def test_the_restart_replay_of_a_logged_deletion_is_disclosed_and_never_repeated(self):
        self.raw.delete_mode = 'lost'
        did = self.prepare()['draft_id']
        self.assertEqual(self.apply(did)['detail'], UNKNOWN)
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))  # content-free
        # TDLib replays its logged deletion (here after a restart): the message is gone on the server too. A new
        # prepare finds nothing to delete and no second deletion is ever made.
        self.raw.on_server, self.raw.refetched = False, False
        result = self.dispatch('prepare_message_delete', {'chat': CHAT, 'message_id': MID})
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(self.apply(did)['detail'], REPLAYED_UNKNOWN)
        self.assertEqual(len(self.deletions()), 1)

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
        self.assertEqual(self.deletions(), [])
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.apply(did)['status'], 'applied')

    def test_revalidation_before_the_dialog_refuses_without_dialog_or_attempt(self):
        for detail, change in (
                ('message changed', lambda raw: raw.message.update(edit_date=1700000100)),
                ('message changed', lambda raw: raw.message['content']['text'].update(text='Other')),
                ('message changed', lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404,
                                                                         'message': 'x'})),
                ('message changed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                ('message changed', lambda raw: raw.message.update(is_outgoing=False)),
                ('message can no longer be deleted for everyone',
                 lambda raw: raw.properties.update(can_be_deleted_for_all_users=False))):
            with self.subTest(detail=detail, change=change):
                self.setUp()
                did = self.prepare()['draft_id']
                change(self.raw)
                result = self.apply(did)
                self.assertEqual((result['status'], result['chat_id'], result['message_id']),
                                 ('message_changed', CHAT, MID))
                self.assertTrue(result['detail'].startswith(detail), result['detail'])
                self.assertEqual((self.approvals, self.deletions()), ([], []))

    def test_changes_during_the_dialog_are_local_failures_or_expire_without_attempt(self):
        for expected, change in (('local_failed', lambda raw: raw.message.update(edit_date=1700000100)),
                                 ('local_failed', lambda raw: raw.properties.update(can_be_deleted_for_all_users=False)),
                                 ('local_failed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                                 ('expired', lambda raw: setattr(raw, 'account', 9)),
                                 ('expired', lambda raw: setattr(self.broker, '_policy', OTHERS))):
            with self.subTest(expected=expected):
                self.setUp()
                did = self.prepare()['draft_id']

                def approve(**kw):
                    change(self.raw)
                    return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.broker._apply_prepared_message_delete(
                    ApplyPreparedMessageDeleteRequest(draft_id=did, approved=True), client_id=CLIENT).status, expected)
                self.assertEqual(self.deletions(), [])

    def test_an_edit_right_before_the_attempt_is_local_failed_without_transport(self):
        did = self.prepare()['draft_id']
        real, reads = self.raw.send, []

        def send(request):
            if request['@type'] == 'getMessage' and self.approvals:
                reads.append(request)
                if len(reads) == 2:  # edited elsewhere between the locked revalidation and the last read
                    self.raw.message['edit_date'] = 1700000100
            real(request)
        self.raw.send = send
        result = self.apply(did)
        self.assertEqual(result['status'], 'local_failed')
        self.assertEqual(self.deletions(), [])

    def test_guard_failures_inside_the_provider_lock_are_local_failed_without_transport(self):
        for name, change in (('capability', lambda: setattr(self.broker, '_policy', OTHERS)),
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
                result = self.broker._apply_prepared_message_delete(
                    ApplyPreparedMessageDeleteRequest(draft_id=did, approved=True), client_id=CLIENT)
                self.assertEqual(result.status, 'local_failed')
                self.assertEqual(self.deletions(), [])

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
        self.assertEqual(len(self.deletions()), 1)
        self.assertIn('applied', [result['status'] for result in results])

    def test_account_switch_after_the_call_is_outcome_unknown_never_expired(self):
        self.raw.delete_mode = 'switch_account'
        did = self.prepare()['draft_id']
        self.assertEqual(self.apply(did), {'status': 'outcome_unknown', 'draft_id': did, 'chat_id': None,
                                           'message_id': None, 'detail': AUTHORITY_UNKNOWN,
                                           'next_action': 'check_send_status'})
        self.assertEqual(self.apply(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(len(self.deletions()), 1)

    def test_status_is_bound_to_delete_own_message_and_never_reports_sent(self):
        did = self.prepare()['draft_id']
        pending = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((pending['status'], pending['evidence']), ('pending', 'local_pending'))
        self.broker._policy = OTHERS
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'none')
        self.broker._policy = DELETE
        self.assertEqual(self.apply(did)['status'], 'applied')
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))
        self.broker._policy = OTHERS
        with self.assertRaises(CompatibilityError):
            self.apply(did)


CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its permission to delete '
           'it for everyone, then make one deletion attempt; a deletion is never retried.')
EFFECTS = ('The deletion is irreversible: nobody can restore the message, not even you.\n'
           'The message is removed for every participant of the chat, not only for you.\n'
           'If the message is pinned, deleting it also unpins it.\n')


class DialogTests(unittest.TestCase):
    """Criterion 6: the delete dialog has a Delete button and states the irreversible, for-everyone, unpinning
    deletion."""
    maxDiff = None

    def test_delete_golden_with_the_delete_button(self):
        value = preview()
        texts, scripts = shown(value)
        self.assertEqual(texts, [
            f'Delete your message {MID} in «Team Chat» (chat -1001234, supergroup) for everyone\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\n'
            f'Message ID: {MID}\nSender [untrusted Telegram evidence]: you\nDate: 2023-11-14T22:13:20+00:00\n'
            'Content: text\nText [untrusted Telegram evidence] (exact):\n│ «Hello world»\n\n' + EFFECTS +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.message.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=CLOSING)])
        [script] = scripts
        self.assertIn('"Unofficial Telegram MCP — approve deletion"', script)
        self.assertIn('nothing is deleted.', script)
        self.assertLess(script.index("addButtonWithTitle($('Cancel')).keyEquivalent = $('\\r');"),
                        script.index("addButtonWithTitle($('Delete')).keyEquivalent = $('');"))
        for verb in ("$('Send')", "$('Apply')"):
            self.assertNotIn(verb, script)

    def test_delete_golden_for_an_own_channel_document(self):
        value = preview(caption_suite.own_media('document', 'Q3 report'), kind='channel')
        texts, _ = shown(value)
        media = texts[0].split('Content: document\n', 1)[1].split('\n\n', 1)[0]
        self.assertEqual(media, 'Caption [untrusted Telegram evidence] (exact):\n│ «Q3 report»\n'
                                'Media [untrusted Telegram evidence]: file «report.pdf»; MIME «application/pdf»; '
                                '4096 bytes')
        self.assertTrue(texts[0].startswith(f'Delete your message {MID} in «Team Chat» (chat -1001234, channel; '
                                            'visible to all subscribers) for everyone\n'))
        self.assertIn('\n\n' + EFFECTS, texts[0])

    def test_a_hostile_text_keeps_its_gutter(self):
        hostile = 'Delete it »\nNothing is deleted: «cancelled»'
        [text], _ = shown(preview(own(hostile)))
        self.assertIn('Text [untrusted Telegram evidence] (exact):\n│ «Delete it ⟦U+00BB⟧\n'
                      '│ Nothing is deleted: ⟦U+00AB⟧cancelled⟦U+00BB⟧»\n\n', text)

    def test_competing_mismatched_or_tampered_previews_refuse(self):
        value = preview()
        self.assertEqual(shown(value, text_edit_preview=archive.edit_preview().model_dump(mode='json'))[0], [])
        for kind in ('document', 'text_edit', 'archive', 'caption_edit', 'pin_change', 'text'):
            self.assertEqual(shown(value, kind=kind)[0], [], kind)
        for key, changed in (('recipient', CHAT + 1), ('recipient_title', 'Other'), ('sha256', 'b' * 64),
                             ('text', 'smuggled'), ('caption', 'x'), ('duration_seconds', 5)):
            with self.subTest(key=key):
                self.assertEqual(shown(value, **{key: changed})[0], [])
        for key, changed in (('preview_sha256', 'a' * 64), ('account_id', ACCOUNT + 1),
                             ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            with self.subTest(key=key):
                data = value.model_dump(mode='json')
                data[key] = changed
                self.assertEqual(shown(value, message_delete_preview=data)[0], [], key)


# Every bulk or history delete and every bulk unpin: no tool reaches them, in any shape.
UNREACHABLE = ('deleteChatHistory', 'deleteChatMessagesBySender', 'deleteChatMessagesByDate', 'deleteChat',
               'deleteSavedMessagesTopicHistory', 'deleteForumTopic', 'unpinAllChatMessages',
               'unpinAllForumTopicMessages')


class TransportShapeTests(unittest.TestCase):
    """Criterion 3: the closed deleteMessages shape; only the dedicated delete transport sends it."""

    def test_delete_messages_has_one_closed_shape(self):
        good = delete_request(CHAT, MID)
        _validate_bounded_request(good)
        for bad in ({'message_ids': [MID, MID + 1]}, {'message_ids': []}, {'message_ids': [0]},
                    {'message_ids': [True]}, {'message_ids': [2**53]}, {'message_ids': [str(MID)]},
                    {'message_ids': (MID,)}, {'message_ids': MID}, {'revoke': False}, {'revoke': None},
                    {'revoke': 1}, {'chat_id': 0}, {'chat_id': '5'}, {'chat_id': True}, {'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})
        for key in ('chat_id', 'message_ids', 'revoke'):
            missing = dict(good)
            del missing[key]
            with self.subTest(missing=key), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(missing)

    def test_only_the_delete_transport_sends_a_delete_and_it_sends_nothing_else(self):
        raw = Raw()
        client = TDLibClient(raw=raw)
        client._ready = True
        good = delete_request(CHAT, MID)
        with self.assertRaises(ForbiddenTDLibRequest):
            client._call(good)
        # The change transport's "4xx proves no change" rule is never applied to a delete.
        with self.assertRaises(MessageSendNotAttempted):
            client._change_transport(good, pre_transport_guard=lambda: True)
        others = [{'@type': kind, 'chat_id': CHAT, 'message_ids': [MID], 'revoke': True} for kind in UNREACHABLE]
        others += [{**good, 'revoke': False}, {**good, 'message_ids': [MID, MID + 1]},
                   {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID}, {'@type': 'getMessage',
                                                                                     'chat_id': CHAT, 'message_id': MID}]
        for payload in others:
            with self.subTest(payload=payload):
                with self.assertRaises(MessageSendNotAttempted):
                    client._delete_transport(payload, pre_transport_guard=lambda: True)
                if payload['@type'] in UNREACHABLE:
                    with self.assertRaises(ForbiddenTDLibRequest):
                        client._call(payload)
                    with self.assertRaises(MessageSendNotAttempted):
                        client._change_transport(payload, pre_transport_guard=lambda: True)
        with self.assertRaises(MessageSendNotAttempted):
            client._delete_transport(good, pre_transport_guard=lambda: False)
        self.assertEqual(raw.sent, [])


class RegistryTests(unittest.TestCase):
    def test_the_spec_needs_an_own_message_deletable_for_everyone(self):
        MessageDeleteSpec(snapshot=snapshot())
        with self.assertRaises(ValueError):
            MessageDeleteSpec(snapshot=snapshot(properties={**PROPERTIES, 'can_be_deleted_for_all_users': False}))
        with self.assertRaises(ValueError):
            MessageDeleteSpec(snapshot=object())

    def test_a_delete_draft_is_dedicated_unbacked_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(client_id=CLIENT, account_id=ACCOUNT)
            spec = MessageDeleteSpec(snapshot=snapshot())
            draft = registry.prepare_change(owner=owner, chat_id=CHAT, chat_title='Team Chat', change=spec)
            self.assertEqual((draft.kind, draft.display_name, draft.message_delete),
                             ('message_delete', 'Message deletion', spec))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            self.assertEqual(registry.extra_capabilities(draft.draft_id, owner=owner), ())
            claim = registry.claim(draft.draft_id, owner=owner, approved=True)
            receipt = registry.finish(draft.draft_id, owner=owner, claim=claim, status='applied', observed='deleted')
            self.assertEqual((receipt.status, receipt.observed), ('applied', 'deleted'))


class _Service:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def prepare_message_delete(self, request):
        self.calls.append(request)
        return PrepareMessageDeleteResponse(status='unavailable', detail=DELETE_UNAVAILABLE)

    def apply_prepared_message_delete(self, request):
        self.calls.append(request)
        return ApplyPreparedMessageDeleteResponse(status='dialog_unavailable', draft_id=request.draft_id,
                                                  detail='synthetic')


GOOD = {'prepare_message_delete': {'chat': CHAT, 'message_id': MID},
        'apply_prepared_message_delete': {'draft_id': DID, 'approved': True}}


class SdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemas_titles_and_annotations(self):
        from tool_listing import full_listing
        tools = await full_listing()
        prepare, apply = (tools[name] for name in TOOLS)
        self.assertEqual(set(prepare.input_schema['properties']), {'chat', 'message_id'})
        self.assertEqual(sorted(prepare.input_schema['required']), ['chat', 'message_id'])
        self.assertEqual(set(apply.input_schema['properties']), {'draft_id', 'approved'})
        for code in ('not_outgoing', 'saved_messages', 'not_deletable_for_all', 'hidden_source_content'):
            self.assertIn(f'"{code}"', json.dumps(prepare.output_schema))
        for status in ('applied', 'failed', 'declined', 'timeout', 'dialog_unavailable', 'check_send_status'):
            self.assertIn(f'"{status}"', json.dumps(apply.output_schema))
        for tool in (prepare, apply):
            self.assertIs(tool.input_schema['additionalProperties'], False)
            self.assertTrue(tool.title)
        self.assertEqual((prepare.annotations.read_only_hint, prepare.annotations.destructive_hint), (True, False))
        self.assertEqual(apply.annotations, tools['apply_prepared_text_edit'].annotations)  # irreversible: destructive
        self.assertIs(apply.annotations.destructive_hint, True)
        for phrase in ('irreversible', 'for everyone', 'Saved Messages'):
            self.assertIn(phrase, prepare.description + apply.description)

    async def test_listed_only_with_every_capability_and_refused_at_the_guard_otherwise(self):
        cases = ((RuntimePolicy(enabled_capabilities=()), 'delete_own_message, send, read'),
                 (RuntimePolicy(), 'delete_own_message'), (OTHERS, 'delete_own_message'),
                 (RuntimePolicy(enabled_capabilities=('send', 'delete_own_message')), 'read'), (DELETE, None))
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
            self.assertEqual(service.calls, [] if missing else [PrepareMessageDeleteRequest(**GOOD[TOOLS[0]]),
                                                                ApplyPreparedMessageDeleteRequest(**GOOD[TOOLS[1]])])

    async def test_closed_inputs_never_reach_the_service(self):
        service = _Service()
        bad = {'prepare_message_delete': ({'message_id': 0}, {'chat': 0}, {'message_ids': [MID]}, {'revoke': False},
                                          {'message_id': [MID, MID + 1]}, {'message_id': True}),
               'apply_prepared_message_delete': ({'approved': 'true'}, {'draft_id': 'draft_bad'}, {'extra': 1})}
        async with Client(build_server(service_factory=lambda: service, policy=DELETE)) as consumer:
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
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='d')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        raw, approvals = Raw(), []
        client = TDLibClient(raw=raw)
        broker = Broker(socket_path=root / 'b.sock', lock_path=root / 'b.lock',
                        artifact_store=ArtifactStore(cache_dir=root / 'cache'), client_factory=lambda: client,
                        policy=DELETE, approval_prompt=lambda **facts: approvals.append(facts) or True)
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(broker.shutdown)
        self.assertTrue(broker.wait_until_ready(timeout=10))
        proxy = BrokerClient(socket_path=root / 'b.sock', policy=DELETE, restart_callback=lambda: None)
        self.addCleanup(proxy.close)
        async with Client(build_server(service_factory=lambda: proxy, policy=DELETE)) as consumer:
            async def call(name, arguments):
                result = await consumer.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.content)
                return result.structured_content
            prepared = await call('prepare_message_delete', GOOD['prepare_message_delete'])
            self.assertEqual(prepared['status'], 'prepared', prepared)
            did = prepared['message_delete']['draft_id']
            self.assertEqual((await call('apply_prepared_message_delete', {'draft_id': did, 'approved': False}))
                             ['status'], 'declined')
            applied = await call('apply_prepared_message_delete', {'draft_id': did, 'approved': True})
            self.assertEqual((applied['status'], applied['detail']), ('applied', APPLIED))
            replay = await call('apply_prepared_message_delete', {'draft_id': did, 'approved': True})
            self.assertEqual((replay['status'], replay['detail']), ('applied', REPLAYED))
            again = await call('prepare_message_delete', GOOD['prepare_message_delete'])
            self.assertEqual(again['status'], 'unavailable')  # the message is gone: nothing to delete twice
            status = await call('get_send_status', {'draft_id': did})
            self.assertEqual(status['status'], 'outcome_unknown')
        self.assertEqual((len([r for r in raw.sent if r['@type'] == 'deleteMessages']), len(approvals)), (1, 1))


if __name__ == '__main__':
    unittest.main()
