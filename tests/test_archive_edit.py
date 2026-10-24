"""F17a: one owner-approved Main/Archive move and one plain-text edit of an own text message.

Synthetic raw transport only: projections, drafts, broker, proxy and TDLib reducers are
production code. No live TDLib or owner dialog is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest

from schema_changes import changed_since_0_52, legacy_0_52
from telegram_search_mcp import broker_client as broker_client_module
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_search_mcp.approval_prompt import confirm_approved_send
from telegram_search_mcp.archive_drafts import (ArchiveChat, ArchiveDraftPreview, ArchiveSnapshot, ArchiveSpec,
    current_list,
    ApplyPreparedArchiveRequest, ApplyPreparedArchiveResponse, PrepareChatArchiveRequest, PrepareChatArchiveResponse,
    UNAVAILABLE as ARCHIVE_UNAVAILABLE)
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import KNOWN_CAPABILITIES, LEGACY_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import (OPERATION_CAPABILITIES, SEND_DEPENDENT_CAPABILITIES, TOOL_OPERATIONS,
    CompatibilityError)
from telegram_search_mcp.edit_drafts import (EditChat, EditSnapshot, TextEditDraftPreview, TextEditSpec,
    ApplyPreparedTextEditRequest, ApplyPreparedTextEditResponse, PrepareTextEditRequest, PrepareTextEditResponse,
    UNAVAILABLE as EDIT_UNAVAILABLE)
from approval_layout import RULE as LAYOUT_RULE, logical, payload_under, tail as dialog_tail
from telegram_search_mcp.approval_prompt import DETAILS, NOT_GIVEN, ApprovalDecision
from telegram_search_mcp.forward_drafts import READABLE_RULE
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.send_status_models import GetSendStatusRequest
from telegram_search_mcp.tdjson import (ForbiddenTDLibRequest, MessageSendNotAttempted, TDLibClient, TDLibError,
    _ALLOWED_REQUEST_TYPES, _validate_bounded_request)

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
CHAT = -1001234
SECRET = 9000
ACCOUNT = 7
MID = 1048576
ARCHIVE = RuntimePolicy(enabled_capabilities=('send', 'read', 'archive_chats'))
EDIT = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
BOTH = RuntimePolicy(enabled_capabilities=('send', 'read', 'archive_chats', 'edit_own_text'))
NO_READ = RuntimePolicy(enabled_capabilities=('send', 'archive_chats', 'edit_own_text'))  # Owner decision 20.10.2026.
DID = 'draft_' + '1' * 32
EXPIRY = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
MARK = '[untrusted Telegram evidence] '
ORDER = '9221294780217032704'
LISTS = {'main': 'chatListMain', 'archive': 'chatListArchive'}
PROPERTIES = {'@type': 'messageProperties', 'can_be_edited': True, 'can_be_forwarded': True, 'can_be_copied': True,
    'can_be_replied': True, 'can_be_saved': True, 'has_protected_content_by_current_user': False,
    'has_protected_content_by_other_user': False}
UNKNOWN = 'current receipt authority is unavailable; do not retry automatically'
WHITESPACE = 'new text must not start or end with whitespace; Telegram trims it'
REWRITTEN = 'new text contains characters Telegram rewrites (tab, U+2028–U+202E, U+030A, U+0333, U+033F)'
# Provider error codes the fake returns after the request; None omits the code. Only 4xx proves no change.
ERRORS = {'rejected': 400, 'flood': 420, 'server_error': 500, 'negative': -1, 'codeless': None}
MTPROXY = {'@type': 'chatSourceMtprotoProxy'}
PSA = {'@type': 'chatSourcePublicServiceAnnouncement', 'type': 'covid', 'text': 'PRIVATE PSA TEXT'}
# Independent literal of the shared dialog rule; a silent change fails the goldens.
RULE = ('⟦U+XXXX⟧ marks control, format, private-use, unassigned and default-ignorable characters '
        '(except one U+FE0E/U+FE0F right after a non-selector character), line, paragraph and non-ASCII '
        'spaces, every repeated space, U+2800, characters NFKC changes, combining marks after the second '
        'in a row, « » ⟦ 〚 〛, and Cyrillic, Greek, Armenian, Cherokee, Lisu or undecomposed non-ASCII '
        'Latin letters in a word that also has ASCII letters; look-alike words in one script are not marked')
NOTE = ('Telegram may move an archived chat whose notifications are enabled back to Main when a new message '
        'arrives')
# Digest of the 51 pre-F17a tool schema entries (name, input/output schema, annotations) at 1b81cfe.
EXISTING_SCHEMAS_SHA256 = '6f13303fe340bf0630c537c01926984fea4890d40f2cfea8cd12dc1def980f56'


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


def positions(*lists, extra=(), source=None):
    return [{'@type': 'chatPosition', 'list': {'@type': LISTS[name]}, 'order': ORDER, 'is_pinned': False,
             'source': source} for name in lists] + list(extra)


def folder_position():
    return {'@type': 'chatPosition', 'list': {'@type': 'chatListFolder', 'chat_folder_id': 3}, 'order': ORDER,
            'is_pinned': False, 'source': None}


def chat(chat_id, title, kind='supergroup', lists=('main',), native=1234):
    types = {'supergroup': {'@type': 'chatTypeSupergroup', 'supergroup_id': native, 'is_channel': False},
             'channel': {'@type': 'chatTypeSupergroup', 'supergroup_id': native, 'is_channel': True},
             'basic_group': {'@type': 'chatTypeBasicGroup', 'basic_group_id': native},
             'private': {'@type': 'chatTypePrivate', 'user_id': native},
             'secret': {'@type': 'chatTypeSecret', 'secret_chat_id': 9, 'user_id': native}}
    return {'@type': 'chat', 'id': chat_id, 'title': title, 'type': types[kind], 'has_protected_content': False,
            'positions': positions(*lists)}


def entity(kind, offset=0, length=1, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': kind, **fields}}


def formatted(text, entities=()):
    return {'@type': 'formattedText', 'text': text, 'entities': [dict(e) for e in entities]}


def own_message(text='Hello world', entities=(), **changes):
    return {'@type': 'message', 'chat_id': CHAT, 'id': MID,
            'sender_id': {'@type': 'messageSenderUser', 'user_id': ACCOUNT}, 'is_outgoing': True,
            'author_signature': '', 'via_bot_user_id': 0, 'date': 1700000000, 'edit_date': 0,
            'self_destruct_in': 0.0, 'auto_delete_in': 0.0, 'ephemeral_message_id': 0,
            'interaction_info': {'@type': 'messageInteractionInfo', 'view_count': 3},
            'content': {'@type': 'messageText', 'text': formatted(text, entities)}, **changes}


class Raw:
    def __init__(self):
        self.sent = []; self.events = deque(); self.account = ACCOUNT; self.fail_receive = False
        self.archive_mode = 'ok'; self.edit_mode = 'ok'
        # A fresh session: `unloaded` chats answer 400 until loadChats ('ok', or 404 'end', loads them).
        self.unloaded = {}; self.load_mode = 'ok'; self.chat_errors = {}
        # getChatListsToAddChat: per-chat offered lists, or one verbatim response for every chat.
        self.offered = {}; self.lists_response = None; self.on_lists = None
        self.chats = {CHAT: chat(CHAT, 'Team Chat'), SECRET: chat(SECRET, 'Secret', 'secret', native=55)}
        self.message = own_message(); self.properties = dict(PROPERTIES)

    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind = request['@type']
        if kind == 'getAuthorizationState':
            value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            missing = {'@type': 'error', 'code': self.chat_errors.get(request['chat_id'], 400), 'message': 'x'}
            value = copy.deepcopy(missing if request['chat_id'] in self.chat_errors
                                  else self.chats.get(request['chat_id'], missing))
        elif kind == 'loadChats':
            if self.load_mode == 'fail':
                self.events.append({'@type': 'error', 'code': 500, 'message': 'x', '@extra': request['@extra']}); return
            self.chats.update(self.unloaded); self.unloaded = {}
            if self.load_mode == 'end':  # TDLib's 404: every chat of the list is already loaded.
                self.events.append({'@type': 'error', 'code': 404, 'message': 'x', '@extra': request['@extra']}); return
            value = {'@type': 'ok'}
        elif kind == 'getChatListsToAddChat':
            if self.on_lists is not None:
                self.on_lists()
            both = [{'@type': 'chatListMain'}, {'@type': 'chatListArchive'}, {'@type': 'chatListFolder', 'chat_folder_id': 3}]
            value = copy.deepcopy(self.lists_response if self.lists_response is not None else
                                  {'@type': 'chatLists', 'chat_lists': self.offered.get(request['chat_id'], both)})
        elif kind == 'getMessage':
            value = copy.deepcopy(self.message)
        elif kind == 'getMessageProperties':
            value = copy.deepcopy(self.properties)
        elif kind == 'addChatToList':
            mode = self.archive_mode
            if mode == 'lost':
                self.fail_receive = True; return
            if mode == 'switch_account':
                self.account = 8
            target = {value: key for key, value in LISTS.items()}[request['chat_list']['@type']]
            if mode not in ('no_move', 'rejected', 'flood'):  # A 5xx/negative/codeless error may follow the move.
                moved = positions(target, source=MTPROXY if mode == 'sponsored' else None)
                self.chats[request['chat_id']]['positions'] = moved
                self.events.append({'@type': 'updateChatPosition', 'chat_id': request['chat_id'], 'position': moved[0]})
            if mode in ERRORS:
                error = {'@type': 'error', 'message': 'PRIVATE PROVIDER TEXT', '@extra': request['@extra']}
                self.events.append(error if ERRORS[mode] is None else {**error, 'code': ERRORS[mode]}); return
            value = {'@type': 'chat'} if mode == 'bad_result' else {'@type': 'ok'}
        elif kind == 'editMessageText':
            mode = self.edit_mode
            if mode in ERRORS:
                if mode not in ('rejected', 'flood'):  # A 5xx/negative/codeless error may follow the edit.
                    self.message['content'] = {'@type': 'messageText',
                                               'text': formatted(request['input_message_content']['text']['text'])}
                    self.message['edit_date'] = 1700000500
                error = {'@type': 'error', 'message': 'MESSAGE_EDIT_TIME_EXPIRED PRIVATE', '@extra': request['@extra']}
                self.events.append(error if ERRORS[mode] is None else {**error, 'code': ERRORS[mode]}); return
            if mode == 'lost':
                self.fail_receive = True; return
            if mode == 'switch_account':
                self.account = 8
            new = request['input_message_content']['text']['text']
            value = copy.deepcopy(self.message)
            value['content'] = {'@type': 'messageText', 'text': formatted(new + ('!' if mode == 'wrong_text' else ''))}
            value['edit_date'] = {'zero_edit_date': 0, 'stale': self.message['edit_date'] - 1}.get(mode, 1700000500)
            if mode == 'wrong_id':
                value['id'] = MID + 1048576
            if mode == 'wrong_chat':
                value['chat_id'] = CHAT - 1
            if mode in ('ok', 'switch_account'):
                self.message = copy.deepcopy(value)
            self.events.append({'@type': 'updateMessageEdited', 'chat_id': CHAT, 'message_id': MID,
                                'edit_date': value['edit_date'], 'reply_markup': None})
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


def shown(archive=None, text_edit=None, **kw):
    """Capture the exact owner-dialog document (logical lines) and script; the DECLINED mock has no owner side effect."""
    seen = []
    value = archive if archive is not None else text_edit
    arguments = dict(recipient_title=value.chat.title, recipient=value.chat.chat_id, display_name='Chat list move',
        size_bytes=1, sha256=archive_sha(archive) if archive is not None else text_edit.source_sha256,
        caption='', kind='archive' if archive is not None else 'text_edit',
        archive_preview=archive.model_dump(mode='json') if archive is not None else None,
        text_edit_preview=text_edit.model_dump(mode='json') if text_edit is not None else None)
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        assert confirm_approved_send(**arguments) is False
    return [logical(argv[-1]) for argv in seen], [argv[argv.index('-e') + 1] for argv in seen]


def archive_sha(value):
    return digest({'domain': 'telegram-search-mcp.archive-draft', 'chat_id': value.chat.chat_id,
                   'kind': value.chat.kind, 'current_list': value.current_list, 'target_list': value.target_list})


def archive_preview(current='main', target='archive', title='Team Chat', kind='supergroup'):
    return ArchiveDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
        chat=ArchiveChat(chat_id=CHAT, title=title, kind=kind), current_list=current, target_list=target)


def edit_snapshot(message=None, title='Team Chat', properties=None, kind='supergroup'):
    return EditSnapshot.create(chat(CHAT, title, kind), CHAT, message or own_message(), MID,
                               properties or dict(PROPERTIES))


def edit_preview(new='Hello there', message=None, title='Team Chat', kind='supergroup'):
    return TextEditDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
        chat=EditChat(chat_id=CHAT, title=title, kind=kind), snapshot=edit_snapshot(message, title, kind=kind),
        new_text=new)


class Fixture:
    policy = BOTH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.raw = Raw(); self.client = TDLibClient(raw=self.raw)
        self.client.CHANGE_OBSERVATION_SECONDS = 0.05; self.client.CHANGE_POLL_SECONDS = 0.01
        self.approvals = []
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
            client_factory=lambda: self.client, policy=self.policy,
            approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def requests(self, kind):
        return [request for request in self.raw.sent if request['@type'] == kind]

    def prepare_archive(self, target='archive', chat_id=CHAT):
        result = self.dispatch('prepare_chat_archive', {'chat': chat_id, 'target_list': target})
        self.assertEqual(result['status'], 'prepared', result)
        return result['archive']

    def apply_archive(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_archive', {'draft_id': did, 'approved': approved}, client)

    def prepare_edit(self, new='Hello there'):
        result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': new})
        self.assertEqual(result['status'], 'prepared', result)
        return result['text_edit']

    def apply_edit(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_text_edit', {'draft_id': did, 'approved': approved}, client)

    def assert_no_attempt(self):
        self.assertEqual(self.requests('addChatToList'), []); self.assertEqual(self.requests('editMessageText'), [])


class CapabilityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Criteria 1 and 2: registration, default off, send alone insufficient, each independent."""

    def test_default_off_capabilities_are_registered_everywhere(self):
        for capability, names in (('archive_chats', ('prepare_chat_archive', 'apply_prepared_archive')),
                                  ('edit_own_text', ('prepare_text_edit', 'apply_prepared_text_edit'))):
            self.assertIn(capability, KNOWN_CAPABILITIES)
            self.assertIn(capability, SEND_DEPENDENT_CAPABILITIES)
            self.assertNotIn(capability, LEGACY_CAPABILITIES)
            self.assertNotIn(capability, RuntimePolicy().enabled_capabilities)
            for name in names:
                self.assertEqual(OPERATION_CAPABILITIES[name], capability)
                self.assertEqual(TOOL_OPERATIONS[name], name)
                self.assertIn(name, OPERATIONS)
        self.assertTrue({'addChatToList', 'editMessageText'} <= _ALLOWED_REQUEST_TYPES)

    def test_existing_tool_schemas_are_byte_identical(self):
        from telegram_search_mcp.contract import schema_document
        from telegram_search_mcp.server import _TOOL_NAMES, build_server
        tools = {tool['name']: tool for tool in schema_document(build_server(policy=RuntimePolicy()))['tools']}
        self.assertEqual(len(tools), len(_TOOL_NAMES))  # the registration, independent of the listing policy
        new = {'prepare_chat_archive', 'apply_prepared_archive', 'prepare_text_edit', 'apply_prepared_text_edit'}
        later = {'prepare_album_send', 'send_prepared_album', 'get_album_send_status',  # 0.56 albums, after this slice
                 'prepare_caption_edit', 'apply_prepared_caption_edit'}  # 0.57 caption edits
        self.assertTrue(new <= set(tools))
        existing = [tools[name] for name in sorted(set(tools) - new - later)]
        self.assertEqual(len(existing), 51)
        changed = changed_since_0_52()
        if not changed:
            self.assertEqual(digest(existing), EXISTING_SCHEMAS_SHA256)
        legacy = legacy_0_52()  # later slices change only listed tools
        for tool in existing:
            if tool['name'] not in changed:
                self.assertEqual(digest(tool), legacy[tool['name']], tool['name'])

    async def test_sdk_lists_closed_tools_but_refuses_unless_both_capabilities(self):
        from mcp import Client
        from mcp.shared.exceptions import MCPError
        from telegram_search_mcp.server import build_server
        archive_good = {'chat': CHAT, 'target_list': 'archive'}
        edit_good = {'chat': CHAT, 'message_id': MID, 'new_text': 'New'}
        apply_good = {'draft_id': DID, 'approved': True}
        from telegram_search_mcp.server import _TOOL_NAMES, available_tools
        full = RuntimePolicy(enabled_capabilities=tuple(sorted(KNOWN_CAPABILITIES)))
        async with Client(build_server(service_factory=lambda: self.fail('provider opened'), policy=full)) as consumer:
            tools = {t.name: t for t in (await consumer.list_tools()).tools}
        self.assertEqual(len(tools), len(_TOOL_NAMES))
        # The 0.56 album tools and the 0.57 caption tools follow these four in registration order.
        ours = ['prepare_chat_archive', 'apply_prepared_archive', 'prepare_text_edit', 'apply_prepared_text_edit']
        self.assertEqual(list(tools)[51:55], ours)
        self.assertEqual(set(tools['prepare_chat_archive'].input_schema['properties']), {'chat', 'target_list'})
        self.assertEqual(set(tools['prepare_text_edit'].input_schema['properties']),
                         {'chat', 'message_id', 'new_text'})
        for name in ours:
            self.assertFalse(tools[name].input_schema['additionalProperties'])
            # 0.54: preparation creates local draft state only, so it is read-only.
            self.assertEqual(tools[name].annotations.read_only_hint, name.startswith('prepare_'))
        self.assertEqual([name for name in ours if tools[name].annotations.destructive_hint],
                         ['apply_prepared_text_edit'])
        for policy, archive_on, edit_on in ((RuntimePolicy(), False, False),
                                            (RuntimePolicy(enabled_capabilities=('send',)), False, False),
                                            (RuntimePolicy(enabled_capabilities=('archive_chats',)), False, False),
                                            (RuntimePolicy(enabled_capabilities=('edit_own_text',)), False, False),
                                            (RuntimePolicy(enabled_capabilities=('archive_chats', 'edit_own_text')), False, False),
                                            (NO_READ, False, False),
                                            (ARCHIVE, True, False), (EDIT, False, True), (BOTH, True, True)):
            calls = []
            class Service:
                def close(self): pass
                def prepare_chat_archive(self, request):
                    calls.append(request); return PrepareChatArchiveResponse(status='unavailable', detail=ARCHIVE_UNAVAILABLE)
                def apply_prepared_archive(self, request):
                    calls.append(request)
                    return ApplyPreparedArchiveResponse(status='dialog_unavailable', draft_id=request.draft_id, detail='synthetic')
                def prepare_text_edit(self, request):
                    calls.append(request); return PrepareTextEditResponse(status='unavailable', detail=EDIT_UNAVAILABLE)
                def apply_prepared_text_edit(self, request):
                    calls.append(request)
                    return ApplyPreparedTextEditResponse(status='dialog_unavailable', draft_id=request.draft_id, detail='synthetic')
            async with Client(build_server(service_factory=Service, policy=policy)) as consumer:
                listed = {t.name for t in (await consumer.list_tools()).tools}
                self.assertEqual(listed, set(available_tools(policy.enabled_capabilities)))
                self.assertEqual({'prepare_chat_archive', 'apply_prepared_archive'} <= listed, archive_on)
                self.assertEqual({'prepare_text_edit', 'apply_prepared_text_edit'} <= listed, edit_on)
                for prepare, apply, good, enabled, request_model in (
                        ('prepare_chat_archive', 'apply_prepared_archive', archive_good, archive_on, PrepareChatArchiveRequest),
                        ('prepare_text_edit', 'apply_prepared_text_edit', edit_good, edit_on, PrepareTextEditRequest)):
                    result = await consumer.call_tool(prepare, good)
                    applied = await consumer.call_tool(apply, apply_good)
                    if not enabled:
                        self.assertTrue(result.is_error); self.assertTrue(applied.is_error); self.assertEqual(calls, [])
                        continue
                    self.assertEqual(result.structured_content['status'], 'unavailable')
                    self.assertEqual(applied.structured_content['status'], 'dialog_unavailable')
                    self.assertEqual(calls[0], request_model(**good)); calls.clear()
                    bad_inputs = ([{'target_list': 'folder'}, {'target_list': 'Archive'}, {'chat': 0}, {'chat': '1'},
                                   {'chat': True}, {'chat_list': 'archive'}, {'extra': 1}] if prepare == 'prepare_chat_archive'
                                  else [{'message_id': 0}, {'message_id': 2**53}, {'message_id': '5'}, {'new_text': ''},
                                        {'new_text': 'x' * 4097}, {'new_text': 5}, {'entities': []},
                                        {'reply_markup': None}, {'chat': 0}])
                    for bad in bad_inputs:
                        try:
                            failed = await consumer.call_tool(prepare, good | bad)
                        except MCPError:
                            continue
                        self.assertTrue(failed.is_error, bad)
                    for bad in ({'approved': 'true'}, {'approved': 1}, {'draft_id': 'draft_bad'}, {'extra': 1}):
                        try:
                            failed = await consumer.call_tool(apply, apply_good | bad)
                        except MCPError:
                            continue
                        self.assertTrue(failed.is_error, bad)
                    self.assertEqual(calls, [])

    def test_proxy_refuses_before_connecting_unless_both_capabilities(self):
        for policy, archive_on, edit_on in ((RuntimePolicy(), False, False),
                                            (RuntimePolicy(enabled_capabilities=('send',)), False, False),
                                            (RuntimePolicy(enabled_capabilities=('archive_chats', 'edit_own_text')), False, False),
                                            (NO_READ, False, False),
                                            (ARCHIVE, True, False), (EDIT, False, True)):
            connects = []
            def connector(path):
                connects.append(path); raise OSError('synthetic no broker')
            proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                                 restart_callback=lambda: None, sleeper=lambda _: None)
            for enabled, prepare, apply in (
                    (archive_on, lambda: proxy.prepare_chat_archive(PrepareChatArchiveRequest(chat=CHAT, target_list='archive')),
                     lambda: proxy.apply_prepared_archive(ApplyPreparedArchiveRequest(draft_id=DID, approved=True))),
                    (edit_on, lambda: proxy.prepare_text_edit(PrepareTextEditRequest(chat=CHAT, message_id=MID, new_text='x')),
                     lambda: proxy.apply_prepared_text_edit(ApplyPreparedTextEditRequest(draft_id=DID, approved=True)))):
                connects.clear()
                with self.subTest(policy=policy.enabled_capabilities, enabled=enabled):
                    self.assertEqual(prepare().status, 'unavailable')
                    applied = apply()
                    if enabled:
                        self.assertEqual(len(connects), 2 * (1 + int(broker_client_module.BROKER_STARTUP_WAIT_SECONDS / 0.25))); self.assertEqual(applied.status, 'outcome_unknown')
                    else:
                        self.assertEqual(connects, []); self.assertEqual(applied.status, 'error')
                        self.assertIn('capability_disabled', applied.detail)

    def test_broker_refuses_before_provider_unless_both_capabilities(self):
        archive_ops = (('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'}),
                       ('apply_prepared_archive', {'draft_id': DID, 'approved': True}))
        edit_ops = (('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'x'}),
                    ('apply_prepared_text_edit', {'draft_id': DID, 'approved': True}))
        for policy, ops in ((RuntimePolicy(), archive_ops + edit_ops),
                            (RuntimePolicy(enabled_capabilities=('send',)), archive_ops + edit_ops),
                            (RuntimePolicy(enabled_capabilities=('archive_chats', 'edit_own_text')), archive_ops + edit_ops),
                            (NO_READ, archive_ops + edit_ops),
                            (EDIT, archive_ops), (ARCHIVE, edit_ops)):
            with tempfile.TemporaryDirectory() as directory:
                opened = []
                broker = Broker(socket_path=Path(directory) / 'broker.sock',
                    artifact_store=ArtifactStore(cache_dir=Path(directory) / 'cache'),
                    client_factory=lambda: opened.append(True), policy=policy, verify_peer_uid=False)
                self.addCleanup(broker.shutdown)
                for op, payload in ops:
                    with self.subTest(policy=policy.enabled_capabilities, op=op), self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation': op, 'payload': payload, 'client_id': CLIENT,
                            'deadline': time.monotonic() + 10, 'broker_generation': broker._generation})
                self.assertEqual(opened, [])


class ArchivePrepareTests(Fixture, unittest.TestCase):
    """Criterion 3: refusals and the closed preview."""

    def test_prepare_archive_and_unarchive_previews_without_any_change(self):
        preview = self.prepare_archive()
        self.assertEqual(preview['chat'], {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'})
        self.assertEqual((preview['current_list'], preview['target_list'], preview['note']), ('main', 'archive', NOTE))
        self.assertEqual(preview['account_id'], ACCOUNT)
        self.assertEqual(ArchiveDraftPreview.model_validate(preview).model_dump(mode='json'), preview)
        self.raw.chats[CHAT]['positions'] = positions('archive', extra=[folder_position()])
        back = self.prepare_archive('main')
        self.assertEqual((back['current_list'], back['target_list'], back['note']), ('archive', 'main', None))
        self.assert_no_attempt(); self.assertEqual(self.approvals, [])

    def test_secret_chat_neither_list_and_already_in_target_refuse_without_draft(self):
        for chat_id, chat_positions, target, expected in (
                (SECRET, None, 'archive', {'status': 'refused', 'archive': None, 'refusal': 'unsupported_chat',
                                           'detail': 'chat is not eligible; no draft was created'}),
                (CHAT, [], 'archive', {'status': 'refused', 'archive': None, 'refusal': 'unsupported_chat',
                                       'detail': 'chat is not eligible; no draft was created'}),
                (CHAT, [folder_position()], 'main', {'status': 'refused', 'archive': None, 'refusal': 'unsupported_chat',
                                                     'detail': 'chat is not eligible; no draft was created'}),
                (CHAT, positions('main'), 'main', {'status': 'already_in_target', 'archive': None, 'refusal': None,
                    'detail': 'chat is already in the target list; no draft was created'}),
                (CHAT, positions('archive'), 'archive', {'status': 'already_in_target', 'archive': None, 'refusal': None,
                    'detail': 'chat is already in the target list; no draft was created'})):
            with self.subTest(chat_id=chat_id, positions=chat_positions, target=target):
                if chat_positions is not None:
                    self.raw.chats[CHAT]['positions'] = chat_positions
                result = self.dispatch('prepare_chat_archive', {'chat': chat_id, 'target_list': target})
                self.assertEqual(result, expected)
                self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assert_no_attempt()

    def test_sponsored_or_psa_positions_are_not_list_membership(self):
        # Decision 3: only a source-null position is membership; TDLib JSON omits a null source.
        organic = positions('main'); del organic[0]['source']
        for chat_positions, expected in ((positions('main', source=MTPROXY), 'none'),
                                         (positions('archive', source=PSA), 'none'),
                                         (positions('main', source=PSA) + positions('archive'), 'archive'),
                                         (positions('archive', source=MTPROXY) + positions('main'), 'main'),
                                         (organic, 'main'), (positions('main') + [folder_position()], 'main')):
            with self.subTest(positions=chat_positions):
                self.assertEqual(current_list(chat_positions), expected)
        for source in ('chatSourceMtprotoProxy', {'@type': 'chatListMain'}, {}, {'@type': 5}, []):
            with self.subTest(source=source), self.assertRaises(ValueError):
                current_list(positions('main', source=source))
        with self.assertRaises(ValueError):  # Still one position per list, sponsored or not.
            current_list(positions('main', source=MTPROXY) + positions('main'))
        for target in ('archive', 'main'):
            for sponsored in (positions('main', source=MTPROXY), positions('main', source=PSA),
                              positions('archive', source=PSA)):
                with self.subTest(target=target, sponsored=sponsored):
                    self.setUp(); self.raw.chats[CHAT]['positions'] = sponsored
                    result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': target})
                    self.assertEqual(result, {'status': 'refused', 'archive': None, 'refusal': 'unsupported_chat',
                                              'detail': 'chat is not eligible; no draft was created'})
                    self.assertEqual(self.broker._drafts.known_ids(), set()); self.assert_no_attempt()
                    self.assertNotIn('PRIVATE', json.dumps(result))

    def test_malformed_missing_or_ambiguous_chats_are_unavailable(self):
        for change in (lambda raw: raw.chats[CHAT].update(positions=positions('main', 'archive')),
                       lambda raw: raw.chats[CHAT].update(positions=positions('main', 'main')),
                       lambda raw: raw.chats[CHAT]['positions'][0].update(order='0'),
                       lambda raw: raw.chats[CHAT]['positions'][0].update(order=12),
                       lambda raw: raw.chats[CHAT]['positions'][0]['list'].update(extra=1),
                       lambda raw: raw.chats[CHAT].pop('positions'),
                       lambda raw: raw.chats[CHAT].update(title=''),
                       lambda raw: raw.chats.pop(CHAT)):
            with self.subTest(change=change):
                self.setUp(); change(self.raw)
                result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})
                self.assertEqual(result, {'status': 'unavailable', 'archive': None, 'refusal': None,
                                          'detail': 'archive draft is unavailable'})
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def lists_requests(self):
        return [{key: value for key, value in request.items() if key != '@extra'}
                for request in self.requests('getChatListsToAddChat')]

    def test_target_list_not_offered_by_telegram_refuses_without_draft_dialog_or_attempt(self):
        # Amendment 20.10.2026: the service chat 777000 can be moved to neither list on request; Telegram's
        # own getChatListsToAddChat decides at prepare, so the owner is never asked to approve it.
        service = 777000
        self.raw.chats[service] = chat(service, 'Telegram', 'private', native=service)
        main, archive, folder = ({'@type': 'chatListMain'}, {'@type': 'chatListArchive'},
                                 {'@type': 'chatListFolder', 'chat_folder_id': 3})
        refused = {'status': 'refused', 'archive': None, 'refusal': 'unsupported_chat',
                   'detail': 'chat is not eligible; no draft was created'}
        for chat_id, current, target, offered in ((service, 'main', 'archive', [main]),
                                                  (service, 'main', 'archive', []),
                                                  (CHAT, 'main', 'archive', [main, folder]),
                                                  (CHAT, 'main', 'archive', [folder]),
                                                  (CHAT, 'archive', 'main', [archive, folder])):
            with self.subTest(chat_id=chat_id, target=target, offered=offered):
                self.raw.chats[chat_id]['positions'] = positions(current)
                self.raw.offered[chat_id] = offered
                self.assertEqual(self.dispatch('prepare_chat_archive', {'chat': chat_id, 'target_list': target}), refused)
                self.assertEqual(self.broker._drafts.known_ids(), set())
        self.assert_no_attempt(); self.assertEqual(self.approvals, [])
        self.assertEqual(self.lists_requests()[0], {'@type': 'getChatListsToAddChat', 'chat_id': service})
        # An offered target (folders ignored) prepares; the answer is read once per prepare.
        self.raw.sent.clear(); self.raw.chats[service]['positions'] = positions('archive')
        self.raw.offered[service] = [main, folder]
        preview = self.prepare_archive('main', chat_id=service)
        self.assertEqual((preview['current_list'], preview['target_list']), ('archive', 'main'))
        self.assertEqual(self.lists_requests(), [{'@type': 'getChatListsToAddChat', 'chat_id': service}])
        self.assert_no_attempt(); self.assertEqual(self.approvals, [])

    def test_lists_to_add_is_asked_only_after_the_existing_prepare_checks(self):
        # Existing order is kept up to already_in_target: refusals and "already there" never ask Telegram.
        self.raw.offered[CHAT] = []
        for chat_id, chat_positions, target in ((SECRET, None, 'archive'), (CHAT, [], 'archive'),
                                                (CHAT, positions('main'), 'main'), (CHAT, positions('archive'), 'archive')):
            if chat_positions is not None:
                self.raw.chats[CHAT]['positions'] = chat_positions
            self.dispatch('prepare_chat_archive', {'chat': chat_id, 'target_list': target})
        self.assertEqual(self.requests('getChatListsToAddChat'), [])
        self.raw.chats[CHAT]['positions'] = positions('main')
        self.assertEqual(self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})['status'],
                         'refused')
        self.assertEqual(len(self.requests('getChatListsToAddChat')), 1)

    def test_account_switch_while_asking_telegram_for_the_lists_is_unavailable(self):
        self.raw.on_lists = lambda: setattr(self.raw, 'account', 8)
        result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})
        self.assertEqual(result, {'status': 'unavailable', 'archive': None, 'refusal': None,
                                  'detail': 'archive draft is unavailable'})
        self.assertEqual(len(self.requests('getChatListsToAddChat')), 1)
        self.assertEqual(self.broker._drafts.known_ids(), set()); self.assert_no_attempt()

    def test_malformed_chat_lists_answers_are_unavailable(self):
        main, archive = {'@type': 'chatListMain'}, {'@type': 'chatListArchive'}
        for response in ({'@type': 'ok'}, {'@type': 'chatLists'}, {'@type': 'chatLists', 'chat_lists': None},
                         {'@type': 'chatLists', 'chat_lists': 'chatListArchive'},
                         {'@type': 'chatLists', 'chat_lists': {'@type': 'chatListArchive'}},
                         {'@type': 'chatLists', 'chat_lists': ['chatListArchive']},
                         {'@type': 'chatLists', 'chat_lists': [archive, None]},
                         {'@type': 'chatLists', 'chat_lists': [archive, {}]},
                         {'@type': 'chatLists', 'chat_lists': [archive, {'@type': 5}]},
                         {'@type': 'chatLists', 'chat_lists': [archive, {'@type': 'chatListOther'}]},
                         {'@type': 'chatLists', 'chat_lists': [{'@type': 'chatListArchive', 'x': 1}]},
                         {'@type': 'chatLists', 'chat_lists': [archive], 'x': 1},
                         {'@type': 'chatLists', 'chat_lists': [archive] + [main] * 64}):
            with self.subTest(response=str(response)[:60]):
                self.setUp(); self.raw.lists_response = response
                result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})
                self.assertEqual(result, {'status': 'unavailable', 'archive': None, 'refusal': None,
                                          'detail': 'archive draft is unavailable'})
                self.assertEqual(self.broker._drafts.known_ids(), set())
                self.assert_no_attempt(); self.assertEqual(self.approvals, [])
        for code in (400, 500):  # A failing getChatListsToAddChat is unavailable, never refused, never retried.
            with self.subTest(code=code):
                self.setUp(); self.raw.lists_response = {'@type': 'error', 'code': code, 'message': 'PRIVATE'}
                result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})
                self.assertEqual((result['status'], len(self.requests('getChatListsToAddChat'))), ('unavailable', 1))
                self.assertEqual(self.broker._drafts.known_ids(), set()); self.assertEqual(self.approvals, [])

    def test_chat_lists_to_add_returns_only_main_and_archive(self):
        client = TDLibClient(raw=self.raw); client._ready = True
        main, archive, folder = ({'@type': 'chatListMain'}, {'@type': 'chatListArchive'},
                                 {'@type': 'chatListFolder', 'chat_folder_id': 3})
        for offered, expected in (([], set()), ([folder], set()), ([main], {'main'}), ([archive, folder], {'archive'}),
                                  ([folder, main, archive, folder], {'main', 'archive'}), ([main, main], {'main'})):
            with self.subTest(offered=offered):
                self.raw.offered[CHAT] = offered
                answer = client.chat_lists_to_add(CHAT)
                self.assertEqual((type(answer), answer), (frozenset, frozenset(expected)))
        for bad in (0, True, 2**53, '5', None, 1.0):
            with self.subTest(chat_id=bad), self.assertRaises(ValueError):
                client.chat_lists_to_add(bad)

    def test_unknown_chat_is_loaded_for_both_lists_then_retried_once(self):
        # Amendment 20.10.2026: a fresh session answers getChat 400 until chats are loaded.
        for load_mode in ('ok', 'end'):  # 'end' = TDLib's 404 "no more chats", which is fine.
            with self.subTest(load_mode=load_mode):
                self.setUp(); self.raw.load_mode = load_mode
                self.raw.unloaded = {CHAT: self.raw.chats.pop(CHAT)}
                self.assertEqual(self.prepare_archive()['chat']['title'], 'Team Chat')
                sent = [{key: value for key, value in request.items() if key != '@extra'}
                        for request in self.raw.sent if request['@type'] not in ('getAuthorizationState', 'getMe')]
                self.assertEqual(sent, [
                    {'@type': 'getChat', 'chat_id': CHAT},
                    {'@type': 'loadChats', 'chat_list': {'@type': 'chatListMain'}, 'limit': 100},
                    {'@type': 'loadChats', 'chat_list': {'@type': 'chatListArchive'}, 'limit': 100},
                    {'@type': 'getChat', 'chat_id': CHAT},
                    {'@type': 'getChatListsToAddChat', 'chat_id': CHAT}])
                self.assert_no_attempt(); self.assertEqual(self.approvals, [])
        # A chat TDLib already knows is never loaded.
        self.setUp(); self.prepare_archive()
        self.assertEqual((len(self.requests('getChat')), self.requests('loadChats')), (1, []))

    def test_still_unknown_after_loading_or_any_other_failure_is_unavailable(self):
        unavailable = {'status': 'unavailable', 'archive': None, 'refusal': None, 'detail': 'archive draft is unavailable'}
        self.raw.chats.pop(CHAT)  # Second 400 after loading: exactly one retry, then unavailable.
        self.assertEqual(self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'}), unavailable)
        self.assertEqual(len(self.requests('getChat')), 2)
        self.assertEqual([r['chat_list']['@type'] for r in self.requests('loadChats')],
                         ['chatListMain', 'chatListArchive'])
        self.assertEqual(self.requests('getChatListsToAddChat'), [])
        self.assertEqual(self.broker._drafts.known_ids(), set()); self.assert_no_attempt()
        for code in (404, 420, 500):  # Only a 400 loads; any other getChat failure is final.
            with self.subTest(getchat_code=code):
                self.setUp(); self.raw.chat_errors = {CHAT: code}
                self.assertEqual(self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'}),
                                 unavailable)
                self.assertEqual((len(self.requests('getChat')), self.requests('loadChats')), (1, []))
        self.setUp(); self.raw.load_mode = 'fail'  # A failing load is not retried or ignored.
        self.raw.unloaded = {CHAT: self.raw.chats.pop(CHAT)}
        self.assertEqual(self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'}), unavailable)
        self.assertEqual((len(self.requests('getChat')), len(self.requests('loadChats'))), (1, 1))
        self.assert_no_attempt(); self.assertEqual(self.approvals, [])

    def test_only_read_chat_list_state_loads_chats(self):
        # resolve_target (every other read path) keeps failing on 400 without loading anything.
        client = TDLibClient(raw=self.raw); client._ready = True
        self.raw.chats.pop(CHAT)
        with self.assertRaises(TDLibError) as caught:
            client.resolve_target(CHAT)
        self.assertEqual(caught.exception.provider_code, 400)
        self.assertEqual(self.requests('loadChats'), [])
        with self.assertRaises(TDLibError):
            client.read_chat_list_state(CHAT)
        self.assertEqual(len(self.requests('loadChats')), 2)

    def test_generic_draft_tools_and_other_send_tools_never_touch_archive_drafts(self):
        did = self.prepare_archive()['draft_id']
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {}), ('update_reply_draft', {'text': 'x'}),
                          ('refresh_reply_draft', {}), ('get_reply_artifact_draft', {})):
            with self.subTest(op=op):
                self.broker._policy = RuntimePolicy(enabled_capabilities=(*BOTH.enabled_capabilities, 'artifacts',
                                                                          'reply_text_send', 'reply_artifact_send'))
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        self.broker._policy = BOTH
        for op in ('send_prepared_text', 'send_prepared_artifact', 'apply_prepared_text_edit'):
            self.assertEqual(self.dispatch(op, {'draft_id': did, 'approved': True})['status'], 'error', op)
        self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'archive_chats', 'forward_send'))
        self.assertEqual(self.dispatch('send_prepared_forward', {'draft_id': did, 'approved': True})['status'], 'error')
        self.broker._policy = BOTH
        self.assert_no_attempt()
        self.assertEqual(self.apply_archive(did)['status'], 'applied')
        for op in ('send_prepared_text', 'send_prepared_artifact'):  # Never a replay of the change receipt.
            result = self.dispatch(op, {'draft_id': did, 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match send tool'), op)

    def test_reprepare_supersedes_only_the_same_owner_and_chat(self):
        first = self.prepare_archive()['draft_id']
        self.raw.chats[4242] = chat(4242, 'Other', 'private', native=4242)
        other = self.prepare_archive(chat_id=4242)['draft_id']
        foreign = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'}, FOREIGN)['archive']['draft_id']
        second = self.prepare_archive()['draft_id']
        self.assertEqual(self.apply_archive(first)['status'], 'expired')
        self.assertEqual(self.broker._drafts._entries[other].state, 'pending')
        self.assertEqual(self.broker._drafts._entries[foreign].state, 'pending')
        self.assertEqual(self.apply_archive(second)['status'], 'applied')
        self.assertEqual(len(self.requests('addChatToList')), 1)

    def test_proxy_validates_preview_digest_and_request_scope(self):
        broker_result = self.dispatch('prepare_chat_archive', {'chat': CHAT, 'target_list': 'archive'})
        request = PrepareChatArchiveRequest(chat=CHAT, target_list='archive')
        def proxy(result, request=request):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=ARCHIVE)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close); return client.prepare_chat_archive(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for where, key, value in (('chat', 'title', 'Other'), ('chat', 'kind', 'channel'), ('chat', 'chat_id', 5),
                                  ('archive', 'current_list', 'archive'), ('archive', 'target_list', 'main'),
                                  ('archive', 'note', None), ('archive', 'account_id', ACCOUNT + 1),
                                  ('archive', 'preview_sha256', 'f' * 64)):
            with self.subTest(where=where, key=key):
                result = copy.deepcopy(broker_result)
                target = result['archive'] if where == 'archive' else result['archive'][where]
                target[key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        self.assertEqual(proxy(broker_result, PrepareChatArchiveRequest(chat=CHAT, target_list='main')).status,
                         'unavailable')
        self.assertEqual(proxy(broker_result, PrepareChatArchiveRequest(chat=CHAT + 1, target_list='archive')).status,
                         'unavailable')


class ArchiveApplyTests(Fixture, unittest.TestCase):
    """Criterion 3: revalidation, one exact call, applied only after the observed position."""

    def test_one_exact_add_chat_to_list_then_observed_receipt_replay(self):
        for current, target, chat_list in (('main', 'archive', 'chatListArchive'), ('archive', 'main', 'chatListMain')):
            with self.subTest(target=target):
                self.setUp(); self.raw.chats[CHAT]['positions'] = positions(current)
                preview = self.prepare_archive(target)
                result = self.apply_archive(preview['draft_id'])
                self.assertEqual(result, {'status': 'applied', 'draft_id': preview['draft_id'], 'chat_id': CHAT,
                    'observed_list': target, 'detail': 'TDLib applied the move; list observed right after the call'})
                [request] = self.requests('addChatToList')
                self.assertEqual(request, {'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': chat_list},
                                           '@extra': request['@extra']})
                self.assertEqual(len(self.approvals), 1)
                self.assertEqual(self.approvals[0]['kind'], 'archive')
                self.assertEqual(self.approvals[0]['archive_preview'], preview)
                # The observation read happens after the call.
                kinds = [r['@type'] for r in self.raw.sent]
                self.assertIn('getChat', kinds[kinds.index('addChatToList') + 1:])
                replay = self.apply_archive(preview['draft_id'])
                self.assertEqual((replay['status'], replay['observed_list']), ('applied', target))
                self.assertIn('no new change', replay['detail'])
                self.assertEqual(len(self.requests('addChatToList')), 1); self.assertEqual(len(self.approvals), 1)

    def test_unobserved_lost_malformed_and_rejected_outcomes_never_retry(self):
        # Decision 2: only a 4xx error proves no change; 5xx, negative or codeless errors may follow a move.
        for mode, expected in (('no_move', 'outcome_unknown'), ('lost', 'outcome_unknown'),
                               ('bad_result', 'outcome_unknown'), ('sponsored', 'outcome_unknown'),
                               ('server_error', 'outcome_unknown'), ('negative', 'outcome_unknown'),
                               ('codeless', 'outcome_unknown'), ('rejected', 'failed'), ('flood', 'failed')):
            with self.subTest(mode=mode):
                self.setUp(); self.raw.archive_mode = mode
                did = self.prepare_archive()['draft_id']
                result = self.apply_archive(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['observed_list'])
                self.assertEqual(self.apply_archive(did)['status'], expected)
                self.assertEqual(len(self.requests('addChatToList')), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                if expected == 'failed':
                    self.assertEqual(result['detail'], f'TDLib rejected the change (code {ERRORS[mode]})')
                else:
                    self.assertIn('do not retry', result['detail'])

    def test_unobserved_move_polls_bounded_then_stays_unknown(self):
        self.raw.archive_mode = 'no_move'
        did = self.prepare_archive()['draft_id']
        self.client.CHANGE_OBSERVATION_SECONDS = 0.2
        started = time.monotonic()
        self.assertEqual(self.apply_archive(did)['status'], 'outcome_unknown')
        self.assertLess(time.monotonic() - started, 5)
        kinds = [r['@type'] for r in self.raw.sent]
        polls = kinds[kinds.index('addChatToList') + 1:].count('getChat')
        self.assertTrue(2 <= polls <= 64, polls)
        self.assertEqual(TDLibClient.CHANGE_OBSERVATION_SECONDS, 10.0)

    def test_not_approved_and_declined_dialog_make_no_attempt(self):
        did = self.prepare_archive()['draft_id']
        result = self.apply_archive(did, approved=False)
        self.assertEqual((result['status'], result['detail']), ('declined', NOT_GIVEN))
        self.assertEqual(self.approvals, [])
        for answer, status in ((False, 'declined'), (ApprovalDecision('timeout'), 'timeout'),
                               (ApprovalDecision('dialog_unavailable'), 'dialog_unavailable'),
                               (object(), 'dialog_unavailable')):
            with self.subTest(answer=answer):
                self.broker._approval_prompt = lambda answer=answer, **kw: answer
                result = self.apply_archive(did)
                self.assertEqual((result['status'], result['detail']), (status, DETAILS[status]))
        self.assert_no_attempt()
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.apply_archive(did)['status'], 'applied')

    def test_revalidation_before_dialog_refuses_without_dialog_or_attempt(self):
        for expected, change in (
                ('chat_changed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                ('chat_changed', lambda raw: raw.chats[CHAT]['type'].update(supergroup_id=99)),
                ('chat_changed', lambda raw: raw.chats[CHAT]['type'].update(is_channel=True)),
                ('chat_changed', lambda raw: raw.chats[CHAT].update(positions=[folder_position()])),
                ('chat_changed', lambda raw: raw.chats[CHAT].update(positions=positions('main', 'archive'))),
                ('chat_changed', lambda raw: raw.chats[CHAT].update(positions=positions('main', source=MTPROXY))),
                ('chat_changed', lambda raw: raw.chats[CHAT].update(positions=positions('main', source=PSA))),
                ('chat_changed', lambda raw: raw.chats[CHAT].update(positions=positions('archive', source=PSA))),
                ('already_in_target', lambda raw: raw.chats[CHAT].update(positions=positions('archive')))):
            with self.subTest(expected=expected, change=change):
                self.setUp(); did = self.prepare_archive()['draft_id']; change(self.raw)
                result = self.apply_archive(did)
                self.assertEqual((result['status'], result['chat_id'], result['observed_list']), (expected, CHAT, None))
                self.assertEqual(self.approvals, []); self.assert_no_attempt()

    def test_changes_during_the_dialog_are_local_failures_or_expire_without_attempt(self):
        for expected, change in (('local_failed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                                 ('local_failed', lambda raw: raw.chats[CHAT].update(positions=positions('archive'))),
                                 ('expired', lambda raw: setattr(raw, 'account', 8))):
            with self.subTest(expected=expected):
                self.setUp(); did = self.prepare_archive()['draft_id']
                def approve(**kw):
                    change(self.raw); return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.apply_archive(did)['status'], expected)
                self.assert_no_attempt()
                if expected == 'local_failed':
                    self.assertEqual(self.apply_archive(did)['status'], 'local_failed')
        self.setUp(); did = self.prepare_archive()['draft_id']
        def disable(**kw):
            self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'edit_own_text')); return True
        self.broker._approval_prompt = disable
        self.assertEqual(self.broker._apply_prepared_archive(ApplyPreparedArchiveRequest(draft_id=did, approved=True),
                                                             client_id=CLIENT).status, 'expired')
        self.assert_no_attempt()

    def test_guard_failure_inside_the_provider_lock_is_local_failed_without_transport(self):
        did = self.prepare_archive()['draft_id']
        real = self.raw.send
        def send(request):
            real(request)
            if request['@type'] == 'getChat' and self.approvals:  # The revalidation read inside the lock.
                self.broker._policy = RuntimePolicy(enabled_capabilities=('send', 'edit_own_text'))
        self.raw.send = send
        result = self.broker._apply_prepared_archive(ApplyPreparedArchiveRequest(draft_id=did, approved=True),
                                                     client_id=CLIENT)
        self.assertEqual(result.status, 'local_failed'); self.assert_no_attempt()

    def test_claim_made_stale_inside_the_provider_lock_is_local_failed_without_transport(self):
        # The claim expires between the claim and the raw send: archive_guard's claim check refuses.
        did = self.prepare_archive()['draft_id']
        real = self.raw.send
        def send(request):
            real(request)
            if request['@type'] == 'getChat' and self.approvals:  # The revalidation read after the claim.
                self.broker._drafts._clock = lambda: time.time() + 10**6
        self.raw.send = send
        self.assertEqual(self.apply_archive(did)['status'], 'local_failed'); self.assert_no_attempt()
        self.assertEqual(self.apply_archive(did)['status'], 'local_failed'); self.assert_no_attempt()

    def test_concurrent_applies_make_one_attempt(self):
        # Recorded pattern (spec §8 amendments, shared with F14/F15): the second caller made no
        # attempt but reads outcome_unknown, never applied or expired.
        did = self.prepare_archive()['draft_id']
        gate = threading.Event(); results = []
        def prompt(**kw):
            self.approvals.append(kw); gate.wait(5); return True
        self.broker._approval_prompt = prompt
        # The broker shows one dialog at a time (test_approval_serialization); bypass that gate to drive the claim race.
        self.broker._owner_dialog = lambda **facts: self.broker._approval_prompt(**facts)
        threads = [threading.Thread(target=lambda: results.append(self.apply_archive(did)['status'])) for _ in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 5
        while len(self.approvals) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        gate.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(results), ['applied', 'outcome_unknown'])
        self.assertEqual(len(self.requests('addChatToList')), 1)

    def test_restart_foreign_owner_and_disabled_policy_are_content_free(self):
        did = self.prepare_archive()['draft_id']
        self.assertEqual(self.apply_archive(did, client=FOREIGN)['status'], 'expired')
        self.broker._policy = EDIT
        with self.assertRaises(CompatibilityError):
            self.apply_archive(did)
        self.broker._policy = BOTH
        titles = dict(self.broker._draft_recipient_titles); self.broker._draft_recipient_titles.clear()
        self.assertEqual(self.apply_archive(did)['status'], 'expired')
        self.broker._draft_recipient_titles.update(titles)
        self.broker._drafts = OutgoingDraftRegistry(self.broker._artifact_store)
        self.assertEqual(self.apply_archive(did)['status'], 'expired')
        self.assertEqual(self.approvals, []); self.assert_no_attempt()

    def test_account_switch_after_the_call_is_outcome_unknown_never_expired(self):
        self.raw.archive_mode = 'switch_account'
        did = self.prepare_archive()['draft_id']
        self.assertEqual(self.apply_archive(did), {'status': 'outcome_unknown', 'draft_id': did, 'chat_id': None,
                                                   'observed_list': None, 'detail': UNKNOWN,
                                                   'next_action': 'check_send_status'})
        self.assertEqual(self.broker._drafts._entries[did].receipt.status, 'applied')  # Kept, never disclosed here.
        self.assertEqual(self.apply_archive(did)['status'], 'outcome_unknown')
        self.assertEqual(self.apply_archive(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(len(self.requests('addChatToList')), 1); self.assertEqual(len(self.approvals), 1)

    def test_status_is_bound_to_the_change_capability_and_never_reports_sent(self):
        # A capability refusal is the content-free default; pending and local-failure statuses differ
        # from it, so they prove the binding. An applied change reads content-free, never sent.
        refused = ('outcome_unknown', 'none', None)
        for kind, other in (('archive', RuntimePolicy(enabled_capabilities=('send', 'edit_own_text'))),
                            ('text_edit', RuntimePolicy(enabled_capabilities=('send', 'archive_chats')))):
            def status(did, policy):
                self.broker._policy = policy
                try:
                    value = self.broker._get_send_status(GetSendStatusRequest(draft_id=did), client_id=CLIENT)
                finally:
                    self.broker._policy = BOTH
                return value.status, value.evidence, value.message_id
            def prepare():
                return self.prepare_archive()['draft_id'] if kind == 'archive' else self.prepare_edit()['draft_id']
            def apply(did):
                return self.apply_archive(did) if kind == 'archive' else self.apply_edit(did)
            with self.subTest(kind=kind):
                self.setUp(); did = prepare()
                self.assertEqual(status(did, BOTH), ('pending', 'local_pending', None))
                self.assertEqual(status(did, other), refused)
                def rename(**kw):
                    self.raw.chats[CHAT]['title'] = 'Renamed'; return True
                self.broker._approval_prompt = rename
                self.assertEqual(apply(did)['status'], 'local_failed')
                self.assertEqual(status(did, BOTH), ('failed', 'local_failed', None))
                self.assertEqual(status(did, other), refused)
                self.setUp(); did = prepare()
                self.assertEqual(apply(did)['status'], 'applied')
                self.assertEqual(status(did, BOTH), refused)  # Applied: content-free, never 'sent'.
                self.assertEqual(status(did, other), refused)


class EditPrepareTests(Fixture, unittest.TestCase):
    """Criterion 4: refusals and the closed preview."""

    def test_prepare_binds_projection_and_reversible_escapes(self):
        self.raw.message = own_message('Hello world', [entity('textEntityTypeBold', 0, 5)])
        preview = self.prepare_edit('Привет, «мир»\r\nnext')
        self.assertEqual(preview['chat'], {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'})
        self.assertEqual((preview['message_id'], preview['current_edit_date'], preview['account_id']), (MID, 0, ACCOUNT))
        self.assertEqual(preview['current_text'], MARK + escape('Hello world'))
        self.assertEqual(preview['new_text'], MARK + escape('Привет, «мир»\nnext'))  # Normalized like a text send.
        self.assertEqual(preview['current_entities'], [{'type': 'textEntityTypeBold', 'offset': 0, 'length': 5}])
        projection = {'version': 3, 'chat_id': CHAT, 'message_id': MID, 'sender': {'type': 'user', 'id': ACCOUNT},
            'author_signature': '', 'via_bot_user_id': 0, 'is_forward': False, 'date': 1700000000, 'edit_date': 0,
            'content': {'kind': 'text', 'text': {'text': 'Hello world',
                        'entities': [{'type': 'textEntityTypeBold', 'offset': 0, 'length': 5}]},
                        'link_preview_options': None, 'link_preview_url': None}}
        self.assertEqual(preview['source_sha256'], digest(projection))
        self.assertNotIn('view_count', json.dumps(preview))
        self.assertEqual(TextEditDraftPreview.model_validate(preview).model_dump(mode='json'), preview)
        self.assert_no_attempt(); self.assertEqual(self.approvals, [])

    def test_ineligible_messages_and_texts_refuse_without_draft(self):
        photo = {'@type': 'messagePhoto', 'photo': {'@type': 'photo', 'has_stickers': False, 'sizes': []},
                 'caption': formatted('cap'), 'show_caption_above_media': False, 'has_spoiler': False, 'is_secret': False}
        forward_info = {'@type': 'messageForwardInfo', 'origin': {'@type': 'messageOriginUser', 'sender_user_id': 8},
                        'date': 1, 'source': None, 'public_service_announcement_type': ''}
        markup = {'@type': 'replyMarkupInlineKeyboard', 'rows': []}
        cases = [
            ('not_outgoing', lambda raw: raw.message.update(is_outgoing=False), 'New'),
            ('unsupported_source_content', lambda raw: raw.message.update(content=photo), 'New'),
            ('unsupported_source_content', lambda raw: raw.message.update(content={'@type': 'messageSticker'}), 'New'),
            ('source_is_forward', lambda raw: raw.message.update(forward_info=forward_info), 'New'),
            ('hidden_source_content', lambda raw: raw.message.update(reply_markup=markup), 'New'),
            ('not_editable', lambda raw: raw.properties.update(can_be_edited=False), 'New'),
            ('hidden_source_content', lambda raw: raw.message['content']['text']['entities'].append(
                entity('textEntityTypeTextUrl', 0, 5, url='https://evil.invalid')), 'New'),
            ('hidden_source_content', lambda raw: raw.message['content']['text']['entities'].append(
                entity('textEntityTypeCustomEmoji', 0, 1, custom_emoji_id='1')), 'New'),
            ('hidden_source_content', lambda raw: raw.message['content'].update(link_preview_options={
                '@type': 'linkPreviewOptions', 'is_disabled': False, 'url': 'https://hidden.invalid',
                'force_small_media': False, 'force_large_media': False, 'show_above_text': False}), 'New'),
            ('unsupported_source_state', lambda raw: raw.message.update(
                sending_state={'@type': 'messageSendingStatePending', 'sending_id': 1}), 'New'),
            ('unsupported_source_state', lambda raw: raw.message.update(
                scheduling_state={'@type': 'messageSchedulingStateSendAtDate', 'send_date': 1}), 'New'),
            ('unsupported_source_state', lambda raw: raw.message.update(self_destruct_in=5.0), 'New'),
            ('unsupported_source_state', lambda raw: raw.message.update(
                self_destruct_type={'@type': 'messageSelfDestructTypeTimer', 'self_destruct_time': 5}), 'New'),
            ('unchanged_text', lambda raw: None, 'Hello world'),
            ('unchanged_text', lambda raw: raw.message['content']['text'].update(text='Café'), 'Café'),
            ('unchanged_text', lambda raw: raw.message['content']['text'].update(text='a\nb'), 'a\r\nb'),
            ('invalid_input', lambda raw: None, '   \n '),
            ('invalid_input', lambda raw: None, 'bad\u0007bell'),
        ]
        for code, change, new in cases:
            with self.subTest(code=code, new=new):
                self.setUp(); change(self.raw)
                result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': new})
                self.assertEqual(result, {'status': 'refused', 'text_edit': None, 'refusal': code,
                    'detail': 'message is not eligible for an own text edit; no draft was created'})
                self.assertEqual(self.broker._drafts.known_ids(), set())
                if code == 'invalid_input':
                    self.assertEqual(self.requests('getMessage'), [])  # Local normalisation precedes any read.
        self.assert_no_attempt()

    def test_new_text_edge_whitespace_is_trimmed_in_the_preview(self):
        # Owner decision 20.10.2026: Telegram trims edge whitespace, so the shared normalisation trims it before the
        # preview and the dialog shows the stored text. NBSP, U+3000 and U+00A8 become a space first (NFKC).
        for new, stored in ((' Hello', 'Hello'), ('Hello ', 'Hello'), ('Hello\n', 'Hello'), ('\nHello', 'Hello'),
                            ('\tHello', 'Hello'), ('Hello\t', 'Hello'), ('Hello\r\n', 'Hello'), ('Hello\r', 'Hello'),
                            ('Hello\u2028', 'Hello'), ('\u2029Hello', 'Hello'), ('\u1680Hello', '\u1680Hello'),
                            ('\u3000Hello', 'Hello'), ('Hello\u00a0', 'Hello'), ('Hello\u2003', 'Hello'),
                            ('\u00a8Hello', '\u0308Hello'), ('Hello there \n', 'Hello there')):
            with self.subTest(new=new):
                self.setUp()
                self.assertEqual(self.prepare_edit(new)['new_text'], MARK + escape(stored))
        self.setUp()
        preview = self.prepare_edit('Hello  there\nnext')  # Interior spaces and line breaks are kept.
        self.assertEqual(preview['new_text'], MARK + escape('Hello  there\nnext'))
        for new in ('   ', '\n\t\u2028'):  # Nothing is left after the rewrite: refused like an empty text.
            with self.subTest(new=new):
                self.setUp()
                result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': new})
                self.assertEqual((result['status'], result['refusal']), ('refused', 'invalid_input'))
                self.assertEqual(self.requests('getMessage'), [])
        self.assert_no_attempt()
        for value in (' New', 'New\n', 'New\u2028'):  # The models still refuse text that skipped normalisation.
            with self.subTest(value=value), self.assertRaises(ValueError):
                TextEditSpec(snapshot=edit_snapshot(), new_text=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                edit_preview(value)
        for status, refusal, detail in (('refused', 'not_editable', WHITESPACE), ('prepared', None, WHITESPACE),
                                        ('unavailable', None, WHITESPACE)):
            with self.subTest(status=status, refusal=refusal), self.assertRaises(ValueError):
                PrepareTextEditResponse(status=status, refusal=refusal, detail=detail)

    def test_characters_telegram_rewrites_are_rewritten_in_the_preview_and_sent_exactly(self):
        # Owner decision 20.10.2026: a tab becomes a space and U+2028, U+2029, U+030A, U+0333 and U+033F are removed
        # before the preview, as the pinned TDLib would, so the dialog shows the stored text. After NFKC, U+02DA and
        # U+2017 are a space plus U+030A or U+0333; a + U+030A composes to U+00E5 and is kept.
        for new, stored in (('a\tb', 'a b'), ('Hello\u2028there', 'Hellothere'), ('Hello\u2029there', 'Hellothere'),
                            ('x\u030a', 'x'), ('a\u0333b', 'ab'), ('a\u033fb', 'ab'), ('Temp 5\u02da', 'Temp 5'),
                            ('x\u2017y', 'x y'), ('tab\tinside and \u2028 too', 'tab inside and  too'),
                            ('Ra\u030ad', 'R\u00e5d')):
            with self.subTest(new=new):
                self.setUp()
                self.assertEqual(self.prepare_edit(new)['new_text'], MARK + escape(stored))
        self.setUp()
        preview = self.prepare_edit('one\ttwo')
        self.assertEqual(self.apply_edit(preview['draft_id'])['status'], 'applied')
        [request] = self.requests('editMessageText')
        self.assertEqual(request['input_message_content']['text']['text'], 'one two')
        self.setUp()  # Format characters still fail normalisation.
        result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': '\u202ex'})
        self.assertEqual((result['status'], result['refusal']), ('refused', 'invalid_input'))
        for value in ('a\tb', 'a\u2029b', 'x\u030a', 'a\u0333b', 'a\u033fb'):  # Models refuse unnormalised text.
            with self.subTest(value=value), self.assertRaises(ValueError):
                TextEditSpec(snapshot=edit_snapshot(), new_text=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                edit_preview(value)
        for status, refusal in (('refused', 'not_editable'), ('prepared', None), ('unavailable', None)):
            with self.subTest(status=status, refusal=refusal), self.assertRaises(ValueError):
                PrepareTextEditResponse(status=status, refusal=refusal, detail=REWRITTEN)

    def test_every_visible_fieldless_entity_is_admitted(self):
        from telegram_search_mcp.forward_sources import VISIBLE_ENTITY_TYPES
        for kind in VISIBLE_ENTITY_TYPES:
            with self.subTest(kind=kind):
                self.setUp(); self.raw.message = own_message('Hello world', [entity(kind, 0, 5)])
                self.assertEqual(self.prepare_edit()['current_entities'][0]['type'], kind)

    def test_missing_secret_and_malformed_messages_are_unavailable(self):
        for change in (lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'gone'}),
                       lambda raw: raw.message.pop('is_outgoing'),
                       lambda raw: raw.message.update(chat_id=CHAT + 1),
                       lambda raw: raw.properties.pop('can_be_edited'),
                       lambda raw: raw.chats.update({CHAT: chat(CHAT, 'Secret', 'secret')}),
                       lambda raw: raw.chats.pop(CHAT)):
            with self.subTest(change=change):
                self.setUp(); change(self.raw)
                result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'New'})
                self.assertEqual(result, {'status': 'unavailable', 'text_edit': None, 'refusal': None,
                                          'detail': 'text edit draft is unavailable'})
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_generic_draft_tools_and_other_send_tools_never_touch_text_edit_drafts(self):
        did = self.prepare_edit()['draft_id']
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        for op, extra in (('get_draft', {}), ('update_draft', {'text': 'x'}), ('refresh_draft', {}),
                          ('cancel_draft', {}), ('get_reply_draft', {}), ('update_reply_draft', {'text': 'x'}),
                          ('refresh_reply_draft', {}), ('get_reply_artifact_draft', {})):
            with self.subTest(op=op):
                self.broker._policy = RuntimePolicy(enabled_capabilities=(*BOTH.enabled_capabilities, 'artifacts',
                                                                          'reply_text_send', 'reply_artifact_send'))
                self.assertEqual(self.dispatch(op, {'draft_id': did, **extra})['status'], 'unavailable')
        self.broker._policy = BOTH
        for op in ('send_prepared_text', 'send_prepared_artifact', 'apply_prepared_archive'):
            self.assertEqual(self.dispatch(op, {'draft_id': did, 'approved': True})['status'], 'error', op)
        self.assert_no_attempt()
        self.assertEqual(self.apply_edit(did)['status'], 'applied')
        for op in ('send_prepared_text', 'send_prepared_artifact'):  # Never a replay of the change receipt.
            result = self.dispatch(op, {'draft_id': did, 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match send tool'), op)

    def test_apply_tools_refuse_plain_text_drafts(self):
        text = self.dispatch('prepare_text_send', {'recipient': CHAT, 'text': 'plain'})
        self.assertEqual(text['status'], 'prepared', text)
        for op in ('apply_prepared_archive', 'apply_prepared_text_edit'):
            result = self.dispatch(op, {'draft_id': text['draft_id'], 'approved': True})
            self.assertEqual((result['status'], result['detail']), ('error', 'draft kind does not match apply tool'), op)
        self.assertEqual(self.approvals, []); self.assert_no_attempt()
        self.assertEqual(self.requests('sendMessage'), [])

    def test_reprepare_supersedes_only_the_same_owner_and_message(self):
        first = self.prepare_edit('One')['draft_id']
        foreign = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'F'},
                                FOREIGN)['text_edit']['draft_id']
        archive = self.prepare_archive()['draft_id']
        second = self.prepare_edit('Two')['draft_id']
        self.assertEqual(self.apply_edit(first)['status'], 'expired')
        self.assertEqual(self.broker._drafts._entries[foreign].state, 'pending')
        self.assertEqual(self.broker._drafts._entries[archive].state, 'pending')
        self.assertEqual(self.apply_edit(second)['status'], 'applied')
        [request] = self.requests('editMessageText')
        self.assertEqual(request['input_message_content']['text']['text'], 'Two')

    def test_proxy_validates_preview_digest_and_request_scope(self):
        broker_result = self.dispatch('prepare_text_edit', {'chat': CHAT, 'message_id': MID, 'new_text': 'New\r\ntext'})
        request = PrepareTextEditRequest(chat=CHAT, message_id=MID, new_text='New\r\ntext')
        def proxy(result, request=request):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=EDIT)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close); return client.prepare_text_edit(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for key, value in (('new_text', MARK + escape('Other')), ('new_text', MARK + '"New\\ntext"'),
                           ('current_text', MARK + escape('Other')), ('message_id', MID + 1),
                           ('current_edit_date', 5), ('source_sha256', 'a' * 64), ('preview_sha256', 'f' * 64),
                           ('account_id', ACCOUNT + 1), ('new_text', 'unmarked'),
                           ('current_entities', [{'type': 'textEntityTypeBold', 'offset': 0, 'length': 99}]),
                           ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'}),
                           ('chat', {'chat_id': CHAT, 'title': 'Team Chat'})):
            with self.subTest(key=key, value=value):
                result = copy.deepcopy(broker_result); result['text_edit'][key] = value
                self.assertEqual(proxy(result).status, 'unavailable')
        whitespace = copy.deepcopy(broker_result)
        whitespace['text_edit']['new_text'] = MARK + escape('New\ntext ')
        self.assertEqual(proxy(whitespace, PrepareTextEditRequest(chat=CHAT, message_id=MID,
                                                                  new_text='New\ntext ')).status, 'unavailable')
        self.assertEqual(proxy(broker_result, PrepareTextEditRequest(chat=CHAT, message_id=MID, new_text='Other')).status,
                         'unavailable')
        self.assertEqual(proxy(broker_result, PrepareTextEditRequest(chat=CHAT, message_id=MID + 1,
                                                                     new_text='New\r\ntext')).status, 'unavailable')


class EditApplyTests(Fixture, unittest.TestCase):
    """Criterion 4: revalidation, one call with the exact shape, result validation, no retry."""

    def test_one_exact_edit_message_text_then_receipt_replay(self):
        self.raw.message = own_message('Hello world', edit_date=1700000100)
        preview = self.prepare_edit('Hello there')
        result = self.apply_edit(preview['draft_id'])
        self.assertEqual(result, {'status': 'applied', 'draft_id': preview['draft_id'], 'chat_id': CHAT,
            'message_id': MID, 'edit_date': 1700000500,
            'detail': 'TDLib returned the edited message with the new text'})
        [request] = self.requests('editMessageText')
        self.assertEqual(request, {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
            'input_message_content': {'@type': 'inputMessageText',
                'text': {'@type': 'formattedText', 'text': 'Hello there', 'entities': []},
                'link_preview_options': None, 'clear_draft': False}, '@extra': request['@extra']})
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(self.approvals[0]['kind'], 'text_edit')
        self.assertEqual(self.approvals[0]['text_edit_preview'], preview)
        self.assertEqual(self.approvals[0]['sha256'], preview['source_sha256'])
        replay = self.apply_edit(preview['draft_id'])
        self.assertEqual((replay['status'], replay['edit_date']), ('applied', 1700000500))
        self.assertIn('no new change', replay['detail'])
        self.assertEqual(len(self.requests('editMessageText')), 1); self.assertEqual(len(self.approvals), 1)

    def test_result_validation_lost_and_rejected_outcomes_never_retry(self):
        # Decision 2: only a 4xx error proves no change; 5xx, negative or codeless errors may follow an edit.
        for mode, expected in (('wrong_text', 'outcome_unknown'), ('zero_edit_date', 'outcome_unknown'),
                               ('stale', 'outcome_unknown'), ('wrong_id', 'outcome_unknown'),
                               ('wrong_chat', 'outcome_unknown'), ('lost', 'outcome_unknown'),
                               ('server_error', 'outcome_unknown'), ('negative', 'outcome_unknown'),
                               ('codeless', 'outcome_unknown'), ('rejected', 'failed'), ('flood', 'failed')):
            with self.subTest(mode=mode):
                self.setUp(); self.raw.edit_mode = mode
                self.raw.message = own_message('Hello world', edit_date=1700000100)
                did = self.prepare_edit()['draft_id']
                result = self.apply_edit(did)
                self.assertEqual(result['status'], expected); self.assertIsNone(result['edit_date'])
                self.assertEqual(self.apply_edit(did)['status'], expected)
                self.assertEqual(len(self.requests('editMessageText')), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                if expected == 'failed':
                    self.assertEqual(result['detail'], f'TDLib rejected the change (code {ERRORS[mode]})')
                else:
                    self.assertIn('do not retry', result['detail'])

    def test_first_edit_accepts_any_non_zero_edit_date(self):
        did = self.prepare_edit()['draft_id']
        self.assertEqual(self.apply_edit(did)['edit_date'], 1700000500)

    def test_not_approved_and_declined_dialog_make_no_attempt(self):
        did = self.prepare_edit()['draft_id']
        result = self.apply_edit(did, approved=False)
        self.assertEqual((result['status'], result['detail']), ('declined', NOT_GIVEN))
        self.assertEqual(self.approvals, [])
        for answer, status in ((False, 'declined'), (ApprovalDecision('timeout'), 'timeout'),
                               (ApprovalDecision('dialog_unavailable'), 'dialog_unavailable'),
                               (object(), 'dialog_unavailable')):
            with self.subTest(answer=answer):
                self.broker._approval_prompt = lambda answer=answer, **kw: answer
                result = self.apply_edit(did)
                self.assertEqual((result['status'], result['detail']), (status, DETAILS[status]))
        self.assert_no_attempt()
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.apply_edit(did)['status'], 'applied')

    def test_revalidation_before_dialog_refuses_without_dialog_or_attempt(self):
        for detail, change in (
                ('message changed', lambda raw: raw.message.update(edit_date=1700000100)),
                ('message changed', lambda raw: raw.message['content']['text'].update(text='Edited elsewhere')),
                ('message changed', lambda raw: setattr(raw, 'message', {'@type': 'error', 'code': 404, 'message': 'x'})),
                ('message changed', lambda raw: raw.message.update(reply_markup={'@type': 'replyMarkupInlineKeyboard',
                                                                                 'rows': []})),
                ('message changed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                ('message changed', lambda raw: raw.chats[CHAT]['type'].update(supergroup_id=99)),
                ('message can no longer be edited', lambda raw: raw.properties.update(can_be_edited=False))):
            with self.subTest(detail=detail, change=change):
                self.setUp(); did = self.prepare_edit()['draft_id']; change(self.raw)
                result = self.apply_edit(did)
                self.assertEqual((result['status'], result['chat_id'], result['message_id']), ('message_changed', CHAT, MID))
                self.assertTrue(result['detail'].startswith(detail), result['detail'])
                self.assertEqual(self.approvals, []); self.assert_no_attempt()

    def test_changes_during_the_dialog_are_local_failures_or_expire_without_attempt(self):
        for expected, change in (('local_failed', lambda raw: raw.message.update(edit_date=1700000100)),
                                 ('local_failed', lambda raw: raw.properties.update(can_be_edited=False)),
                                 ('local_failed', lambda raw: raw.chats[CHAT].update(title='Renamed')),
                                 ('expired', lambda raw: setattr(raw, 'account', 8))):
            with self.subTest(expected=expected):
                self.setUp(); did = self.prepare_edit()['draft_id']
                def approve(**kw):
                    change(self.raw); return True
                self.broker._approval_prompt = approve
                self.assertEqual(self.apply_edit(did)['status'], expected)
                self.assert_no_attempt()
        self.setUp(); did = self.prepare_edit()['draft_id']
        def disable(**kw):
            self.broker._policy = ARCHIVE; return True
        self.broker._approval_prompt = disable
        self.assertEqual(self.broker._apply_prepared_text_edit(ApplyPreparedTextEditRequest(draft_id=did, approved=True),
                                                               client_id=CLIENT).status, 'expired')
        self.assert_no_attempt()

    def test_guard_failures_inside_the_provider_lock_are_local_failed_without_transport(self):
        # After the claim, the revalidation read inside the provider lock: edit_own_text turned off
        # (edit_guard's capability check) or the claim made stale by expiry (its claim check).
        for name, change in (('capability', lambda: setattr(self.broker, '_policy', ARCHIVE)),
                             ('stale_claim', lambda: setattr(self.broker._drafts, '_clock',
                                                             lambda: time.time() + 10**6))):
            with self.subTest(name=name):
                self.setUp(); did = self.prepare_edit()['draft_id']
                real = self.raw.send
                def send(request, real=real, change=change):
                    real(request)
                    if request['@type'] == 'getMessageProperties' and self.approvals:
                        change()
                self.raw.send = send
                result = self.broker._apply_prepared_text_edit(
                    ApplyPreparedTextEditRequest(draft_id=did, approved=True), client_id=CLIENT)
                self.assertEqual((result.status, result.edit_date), ('local_failed', None))
                self.assertEqual(len(self.approvals), 1); self.assert_no_attempt()
                self.broker._policy = BOTH
                self.assertEqual(self.apply_edit(did)['status'], 'local_failed'); self.assert_no_attempt()

    def test_account_switch_after_the_call_is_outcome_unknown_never_expired(self):
        self.raw.edit_mode = 'switch_account'
        did = self.prepare_edit()['draft_id']
        self.assertEqual(self.apply_edit(did), {'status': 'outcome_unknown', 'draft_id': did, 'chat_id': None,
                                                'message_id': None, 'edit_date': None, 'detail': UNKNOWN,
                                                'next_action': 'check_send_status'})
        self.assertEqual(self.broker._drafts._entries[did].receipt.status, 'applied')
        self.assertEqual(self.apply_edit(did)['status'], 'outcome_unknown')
        self.assertEqual(self.apply_edit(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(len(self.requests('editMessageText')), 1)

    def test_restart_foreign_owner_and_disabled_policy_are_content_free(self):
        did = self.prepare_edit()['draft_id']
        self.assertEqual(self.apply_edit(did, client=FOREIGN)['status'], 'expired')
        self.broker._policy = ARCHIVE
        with self.assertRaises(CompatibilityError):
            self.apply_edit(did)
        self.broker._policy = BOTH
        self.broker._draft_recipient_titles.clear()
        self.assertEqual(self.apply_edit(did)['status'], 'expired')
        self.assertEqual(self.approvals, []); self.assert_no_attempt()


class DialogTests(unittest.TestCase):
    """Criterion 5: dedicated dialogs with « » spans under the shared readable rule."""
    maxDiff = None

    def test_archive_and_unarchive_goldens(self):
        # Decision 4: the move unpins the chat in the list it leaves; folders excluding archived chats hide it.
        self.assertEqual(READABLE_RULE, RULE)
        self.assertEqual(RULE, LAYOUT_RULE)
        closing = ('Unofficial Telegram MCP will revalidate the account, the chat and its current list, then make one '
                   'move attempt and report the list it observes right after.')
        value = archive_preview()
        texts, scripts = shown(value)
        self.assertEqual(texts, [
            'Move chat «Team Chat» (chat -1001234, supergroup) from Main to Archive\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\n'
            'Current list: Main\nTarget list: Archive\n'
            'Note: the chat leaves Main and loses any pin there; it is not pinned in Archive (Telegram behaviour).\n'
            'Note: chat folders that exclude archived chats will hide it (Telegram behaviour).\n'
            f'Note: {NOTE} (Telegram behaviour).\n' +
            dialog_tail(f'Draft: {DID}', f'Preview SHA-256: {value.preview_sha256}', closing=closing)])
        # A change dialog: Apply, worded as a change, Cancel added first (the default button).
        self.assertIn("approve change", scripts[0]); self.assertIn("nothing is changed", scripts[0])
        self.assertLess(scripts[0].index("addButtonWithTitle($('Cancel'))"),
                        scripts[0].index("addButtonWithTitle($('Apply'))"))
        self.assertNotIn("'Send'", scripts[0])
        back = archive_preview('archive', 'main', title='Тим «x» ⟦1⟧', kind='private')
        texts, _ = shown(back)
        self.assertEqual(texts, [
            'Move chat «Тим ⟦U+00AB⟧x⟦U+00BB⟧ ⟦U+27E6⟧1⟧» (chat -1001234, private) from Archive to Main\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\n'
            'Current list: Archive\nTarget list: Main\n'
            'Note: the chat leaves Archive and loses any pin there; it is not pinned in Main (Telegram behaviour).\n' +
            dialog_tail(f'Draft: {DID}', f'Preview SHA-256: {back.preview_sha256}', closing=closing)])

    def test_edit_plain_and_hostile_goldens(self):
        message = own_message('Hello world', [entity('textEntityTypeBold', 0, 5)])
        value = edit_preview('Hello there', message)
        texts, scripts = shown(text_edit=value)
        note = ('\n\nThe new text is plain text without formatting; Telegram may detect links in it and show a link '
                'preview. Link preview settings return to Telegram\'s default. Recipients see the message marked '
                '"edited".\n')
        closing = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its edit permission, '
                   'then make one edit attempt.')
        self.assertEqual(texts, [
            f'Edit your message {MID} in «Team Chat» (chat -1001234, supergroup)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: never\n\n'
            'Current text [untrusted Telegram evidence] (exact):\n│ «Hello world»\n'
            'Current formatting, removed by this plain-text edit (raw text UTF-16 code units):\n'
            'Bold: offset 0, length 5; covered «Hello»\n'
            'New text (exact):\n│ «Hello there»' + note +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=closing)])
        self.assertIn("addButtonWithTitle($('Apply'))", scripts[0])
        hostile_current = 'Pay pаypal »\nNew text (exact): «approved»‮'
        # A real line break keeps its payload gutter, so it cannot start a fake dialog line (an interior tab
        # is refused at prepare).
        hostile = edit_preview('Привет 日本 \U0001F600\nlast «', own_message(hostile_current, edit_date=1700000100))
        texts, _ = shown(text_edit=hostile)
        self.assertEqual(texts, [
            f'Edit your message {MID} in «Team Chat» (chat -1001234, supergroup)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: 2023-11-14T22:15:00+00:00\n\n'
            'Current text [untrusted Telegram evidence] (exact):\n'
            '│ «Pay p⟦U+0430⟧ypal ⟦U+00BB⟧\n│ New text (exact): ⟦U+00AB⟧approved⟦U+00BB⟧⟦U+202E⟧»\n'
            'New text (exact):\n│ «Привет 日本 \U0001F600\n│ last ⟦U+00AB⟧»' + note +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {hostile.source_sha256}',
                        f'Preview SHA-256: {hostile.preview_sha256}', closing=closing)])
        self.assertEqual(re.sub('⟦U\\+([0-9A-F]{4,6})⟧', lambda m: chr(int(m.group(1), 16)),
                                payload_under(texts[0], 'Current text [untrusted Telegram evidence] (exact):')),
                         hostile_current)

    def test_edit_dialog_shows_chat_kind_and_only_undetected_formatting_as_removed(self):
        # Decision 4: kind (channel edits reach every subscriber), link preview default, and auto-detected
        # entity kinds listed apart from the formatting this plain-text edit removes.
        text = 'Hi @team see example.com #tag $USD /start mail@x.io +12025550123 4111111111111111 bold'
        def at(word):
            return text.index(word)
        entities = [entity('textEntityTypeMention', at('@team'), 5), entity('textEntityTypeUrl', at('example'), 11),
                    entity('textEntityTypeHashtag', at('#tag'), 4), entity('textEntityTypeCashtag', at('$USD'), 4),
                    entity('textEntityTypeBotCommand', at('/start'), 6),
                    entity('textEntityTypeEmailAddress', at('mail@'), 9),
                    entity('textEntityTypePhoneNumber', at('+1'), 12),
                    entity('textEntityTypeBankCardNumber', at('4111'), 16),
                    entity('textEntityTypeBold', at('bold'), 4), entity('textEntityTypeItalic', 0, 2)]
        for kind, label in (('private', 'private'), ('basic_group', 'group'), ('supergroup', 'supergroup'),
                            ('channel', 'channel; visible to all subscribers')):
            with self.subTest(kind=kind):
                value = edit_preview('Hello there', own_message(text, entities), kind=kind)
                self.assertEqual(value.chat.kind, kind)
                [shown_text], _ = shown(text_edit=value)
                self.assertTrue(shown_text.startswith(
                    f'Edit your message {MID} in «Team Chat» (chat -1001234, {label})\n'), shown_text)
                self.assertIn(
                    f'Current text [untrusted Telegram evidence] (exact):\n│ «{text}»\n'
                    'Current formatting, removed by this plain-text edit (raw text UTF-16 code units):\n'
                    f'Bold: offset {at("bold")}, length 4; covered «bold»\n'
                    'Italic: offset 0, length 2; covered «Hi»\n'
                    'Current auto-detected entities, auto-detected again by Telegram wherever the new text '
                    'contains them (raw text UTF-16 code units):\n'
                    'Mention: offset 3, length 5; covered «@team»\n'
                    f'Url: offset {at("example")}, length 11; covered «example.com»\n'
                    f'Hashtag: offset {at("#tag")}, length 4; covered «#tag»\n'
                    f'Cashtag: offset {at("$USD")}, length 4; covered «$USD»\n'
                    f'BotCommand: offset {at("/start")}, length 6; covered «/start»\n'
                    f'EmailAddress: offset {at("mail@")}, length 9; covered «mail@x.io»\n'
                    f'PhoneNumber: offset {at("+1")}, length 12; covered «+12025550123»\n'
                    f'BankCardNumber: offset {at("4111")}, length 16; covered «4111111111111111»\n'
                    'New text (exact):\n│ «Hello there»\n', shown_text)
                self.assertIn("Link preview settings return to Telegram's default.", shown_text)
        only_detected = edit_preview('Hello there',
                                     own_message('see example.com', [entity('textEntityTypeUrl', 4, 11)]))
        [shown_text], _ = shown(text_edit=only_detected)
        self.assertNotIn('removed by this plain-text edit', shown_text)
        self.assertIn('auto-detected again by Telegram', shown_text)
        with self.assertRaises(ValueError):  # The preview kind must be the snapshot's kind.
            TextEditDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
                chat=EditChat(chat_id=CHAT, title='Team Chat', kind='channel'), snapshot=edit_snapshot(),
                new_text='New')

    def test_preview_digests_are_independently_reproducible(self):
        value = archive_preview()
        self.assertEqual(value.preview_sha256, digest({'domain': 'telegram-search-mcp.archive', 'version': 1,
            'draft_id': DID, 'account_id': ACCOUNT, 'expires_at': '2030-01-02T03:04:05+00:00',
            'approval_required': True, 'chat': {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'},
            'current_list': 'main', 'target_list': 'archive', 'note': NOTE,
            'request': {'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': 'chatListArchive'}}}))
        edit = edit_preview('New')
        self.assertEqual(edit.preview_sha256, digest({'domain': 'telegram-search-mcp.text_edit', 'version': 1,
            'draft_id': DID, 'account_id': ACCOUNT, 'expires_at': '2030-01-02T03:04:05+00:00',
            'approval_required': True, 'chat': {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'supergroup'},
            'message_id': MID,
            'current_text': MARK + escape('Hello world'), 'current_entities': [], 'new_text': MARK + escape('New'),
            'current_edit_date': 0, 'source_sha256': edit.source_sha256,
            'request': {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                'input_message_content': {'@type': 'inputMessageText',
                    'text': {'@type': 'formattedText', 'text': 'New', 'entities': []},
                    'link_preview_options': None, 'clear_draft': False}}}))

    def test_competing_mismatched_or_tampered_previews_refuse(self):
        archive, edit = archive_preview(), edit_preview()
        self.assertEqual(shown(archive, text_edit_preview=edit.model_dump(mode='json'))[0], [])
        for kind in ('document', 'forward', 'text', 'video', 'text_edit'):
            self.assertEqual(shown(archive, kind=kind)[0], [], kind)
        for kind in ('document', 'archive'):
            self.assertEqual(shown(text_edit=edit, kind=kind)[0], [], kind)
        for value, key, changed in ((archive, 'recipient', CHAT + 1), (archive, 'recipient_title', 'Other'),
                                    (archive, 'sha256', 'b' * 64), (archive, 'text', 'smuggled'),
                                    (archive, 'caption', 'x'), (edit, 'recipient', CHAT + 1),
                                    (edit, 'recipient_title', 'Other'), (edit, 'sha256', 'b' * 64),
                                    (edit, 'duration_seconds', 5), (edit, 'source_sha256', 'a' * 64)):
            with self.subTest(key=key):
                self.assertEqual((shown(archive, **{key: changed}) if value is archive else
                                  shown(text_edit=value, **{key: changed}))[0], [])
        for key, changed in (('current_list', 'archive'), ('note', None), ('target_list', 'main'),
                             ('account_id', ACCOUNT + 1)):
            data = archive.model_dump(mode='json'); data[key] = changed
            self.assertEqual(shown(archive, archive_preview=data)[0], [], key)
        for key, changed in (('new_text', MARK + escape('Hello world')), ('new_text', MARK + escape('a\tb\u0007')),
                             ('current_edit_date', 9), ('new_text', MARK + '"N\\u0065w"'),
                             ('chat', {'chat_id': CHAT, 'title': 'Team Chat', 'kind': 'channel'})):
            data = edit.model_dump(mode='json'); data[key] = changed
            self.assertEqual(shown(text_edit=edit, text_edit_preview=data)[0], [], key)


class TransportShapeTests(unittest.TestCase):
    """Closed TDLib shapes for addChatToList and editMessageText."""

    def test_add_chat_to_list_has_one_closed_shape(self):
        for chat_list in ('chatListMain', 'chatListArchive'):
            _validate_bounded_request({'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': chat_list}})
        good = {'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': 'chatListArchive'}}
        for bad in ({'chat_list': {'@type': 'chatListFolder', 'chat_folder_id': 1}},
                    {'chat_list': {'@type': 'chatListArchive', 'x': 1}}, {'chat_list': 'chatListArchive'},
                    {'chat_list': None}, {'chat_id': 0}, {'chat_id': True}, {'chat_id': 2**53}, {'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request({'@type': 'addChatToList', 'chat_id': CHAT})

    def test_get_chat_lists_to_add_chat_has_one_closed_shape(self):
        self.assertIn('getChatListsToAddChat', _ALLOWED_REQUEST_TYPES)
        for chat_id in (CHAT, -1, 777000, 2**53 - 1, -(2**53 - 1)):
            _validate_bounded_request({'@type': 'getChatListsToAddChat', 'chat_id': chat_id})
        good = {'@type': 'getChatListsToAddChat', 'chat_id': CHAT}
        for bad in ({'extra': 1}, {'chat_list': {'@type': 'chatListArchive'}}, {'chat_id': 0}, {'chat_id': True},
                    {'chat_id': 2**53}, {'chat_id': -(2**53)}, {'chat_id': '5'}, {'chat_id': 1.0}, {'chat_id': None}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request({'@type': 'getChatListsToAddChat'})

    def test_load_chats_admits_only_the_catalog_and_the_one_shot_limit(self):
        for limit in (50, 100):
            for chat_list in ('chatListMain', 'chatListArchive'):
                _validate_bounded_request({'@type': 'loadChats', 'chat_list': {'@type': chat_list}, 'limit': limit})
        good = {'@type': 'loadChats', 'chat_list': {'@type': 'chatListMain'}, 'limit': 100}
        for bad in ({'limit': 0}, {'limit': 1}, {'limit': 99}, {'limit': 101}, {'limit': 200}, {'limit': True},
                    {'limit': 100.0}, {'limit': '100'}, {'limit': None}, {'extra': 1},
                    {'chat_list': {'@type': 'chatListFolder', 'chat_folder_id': 1}}, {'chat_list': None}):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request({**good, **bad})

    def test_edit_message_text_has_one_closed_shape(self):
        def request(**changes):
            content = {'@type': 'inputMessageText', 'text': {'@type': 'formattedText', 'text': 'New', 'entities': []},
                       'link_preview_options': None, 'clear_draft': False}
            text = changes.pop('text', None)
            if text is not None:
                content['text'] = text
            content.update(changes.pop('content', {}))
            return {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                    'input_message_content': content, **changes}
        _validate_bounded_request(request())
        bold = {'@type': 'textEntity', 'offset': 0, 'length': 1, 'type': {'@type': 'textEntityTypeBold'}}
        for bad in (request(reply_markup={'@type': 'replyMarkupRemoveKeyboard', 'is_personal': False}),
                    request(content={'link_preview_options': {'@type': 'linkPreviewOptions', 'is_disabled': True,
                        'url': '', 'force_small_media': False, 'force_large_media': False, 'show_above_text': False}}),
                    request(content={'clear_draft': True}), request(content={'clear_draft': 0}),
                    request(content={'@type': 'inputMessagePhoto'}), request(content={'extra': 1}),
                    request(text={'@type': 'formattedText', 'text': 'New', 'entities': [bold]}),
                    request(text={'@type': 'formattedText', 'text': '', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'x' * 4097, 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\r\nb', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': ' New', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'New\n', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'New\t', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': '\u2028New', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\tb', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\u2028b', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\u2029b', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'x\u030a', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\u0333b', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'a\u033fb', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'Café', 'entities': []}),
                    request(text={'@type': 'formattedText', 'text': 'New', 'entities': [], 'x': 1}),
                    request(message_id=0), request(message_id=True), request(chat_id=0), request(extra=1)):
            with self.subTest(bad=bad), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(bad)
        broken = request(); del broken['reply_markup']
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request(broken)

    def test_change_transport_refuses_other_requests_before_any_raw_send(self):
        raw = Raw(); client = TDLibClient(raw=raw); client._ready = True
        for payload in ({'@type': 'getChat', 'chat_id': CHAT}, {'@type': 'getChatListsToAddChat', 'chat_id': CHAT},
                        {'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': 'chatListFolder'}}):
            with self.subTest(payload=payload), self.assertRaises(MessageSendNotAttempted):
                client._change_transport(payload, pre_transport_guard=lambda: True)
        with self.assertRaises(MessageSendNotAttempted):
            client._change_transport({'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': 'chatListMain'}},
                                     pre_transport_guard=lambda: False)
        # The generic call path never carries a state change, even in the exact closed shape.
        for payload in ({'@type': 'addChatToList', 'chat_id': CHAT, 'chat_list': {'@type': 'chatListMain'}},
                        {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                         'input_message_content': {'@type': 'inputMessageText', 'text': {'@type': 'formattedText',
                             'text': 'x', 'entities': []}, 'link_preview_options': None, 'clear_draft': False}}):
            with self.subTest(payload=payload['@type']), self.assertRaises(ForbiddenTDLibRequest):
                client._call(payload)
        self.assertEqual(raw.sent, [])

    def test_registry_change_kinds_are_dedicated_unbacked_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(client_id=CLIENT, account_id=ACCOUNT)
            snapshot = ArchiveSnapshot.create(chat(CHAT, 'Team Chat'), CHAT)
            draft = registry.prepare_change(owner=owner, chat_id=CHAT, chat_title='Team Chat',
                                            change=ArchiveSpec(snapshot=snapshot, target_list='archive'))
            self.assertEqual(registry.list_pending(owner=owner), ([], False))
            for call in (lambda: registry.cancel(draft.draft_id, owner=owner),
                         lambda: registry.revise(draft.draft_id, owner=owner, recipient_title='x', text='y')):
                with self.assertRaises(Exception):
                    call()
            claim = registry.claim(draft.draft_id, owner=owner, approved=True)
            with self.assertRaises(ValueError):
                registry.finish(draft.draft_id, owner=owner, claim=claim, status='sent', message_id=5)
            with self.assertRaises(ValueError):
                registry.finish(draft.draft_id, owner=owner, claim=claim, status='applied')
            receipt = registry.finish(draft.draft_id, owner=owner, claim=claim, status='applied', observed='archive')
            self.assertEqual((receipt.status, receipt.observed, receipt.message_id), ('applied', 'archive', None))
            with self.assertRaises(ValueError):
                ArchiveSpec(snapshot=snapshot, target_list='main')  # Already in the target list.
            with self.assertRaises(ValueError):
                TextEditSpec(snapshot=edit_snapshot(), new_text='Hello world')
            with self.assertRaises(ValueError):
                TextEditSpec(snapshot=edit_snapshot(properties={**PROPERTIES, 'can_be_edited': False}), new_text='New')
            text = registry.prepare_text(owner=owner, recipient=CHAT, text='plain')
            with self.assertRaises(ValueError):
                registry.finish(text.draft_id, owner=owner, status='applied', observed=1)


if __name__ == '__main__':
    unittest.main()
