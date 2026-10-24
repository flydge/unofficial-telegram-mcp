"""0.55 phase E: the isolation matrix (spec 3E, criterion 5).

Two real brokers serve on real sockets under one short synthetic home: the default alias and `work`, listed in
one alias registry, each with its own runtime policy, caches and fake TDLib session authorized as a different
user. Every handle kind (the 0.56 album draft id included) is minted on one broker (A) through its socket and presented
to the other (B) through B's socket, by the same client id: B answers exactly what it answers for an unknown handle of that kind (the
whole wire response, the handle masked), shows no dialog and makes no provider change, and A still honours its
own handle afterwards. Each matrix runs in both directions (default to work, work to default).

Also here: the socket-path and tool-name budgets, the duplicate account and pin mismatch through the socket, and
two aliases in one fake launchd domain with disjoint labels. Everything is synthetic: no Telegram, launchd,
Keychain or dialog.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_album_send as album_fixture
import test_archive_edit as archive_fixture
import test_caption_edit as caption_fixture
import test_styled_text_edit as styled_fixture
import test_pin_change as pin_fixture
import test_artifact_replies as artifact_reply_fixture
import test_forward_send as forward_fixture
import test_send_status as text_fixture
import test_text_replies as reply_fixture
import test_video_send as video_fixture
from test_account_jobs import FakeLaunchd
from test_chat_list_reader import Provider as ChatListProvider
from test_exact_search_reader import SearchProvider
from test_forum_reader import page as forum_page
from test_history_reader import HistoryProvider
from test_message_reader import message
from test_relogin_hardening import AccountClient as DiscoveryAccountClient
from test_selected_search import SelectedProvider
from test_topic_history_boundary import HistoryForumProvider
from test_verified_targets import TargetProvider
from xlsx_fixtures import worksheet, write_xlsx, xlsx_parts

from telegram_search_mcp import account_registry, launch_agent, tdjson, transfer_cli
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION,
                                                 receive_frame, send_frame)
from telegram_search_mcp.config import KNOWN_CAPABILITIES, AccountPaths, accounts_root, load_runtime_policy
from telegram_search_mcp.contract import contract_descriptor
from telegram_search_mcp.local_upload_models import UploadAuthorityEnvelope
from telegram_search_mcp.reply_drafts import source_from_message
from telegram_search_mcp.tdjson import TDLibClient

# One proxy client id on both brokers: the strictest case, since a binding to the client id alone would pass.
CLIENT = 'client_' + 'i' * 24
HELPER = 'client_' + 'h' * 24  # the transfer helper's own client (upload authority redemption)
LONGEST = account_registry.longest_tool_name()
MUTATING = frozenset({'sendMessage', 'forwardMessages', 'addChatToList', 'editMessageText', 'sendMessageAlbum',
                      'editMessageCaption', 'pinChatMessage', 'unpinChatMessage'})
POLICY_TOML = ('config_version = 1\nenabled_capabilities = [' +
               ', '.join(f'"{name}"' for name in sorted(KNOWN_CAPABILITIES)) + ']\n')


def forged(token):
    """An unknown handle of the same kind and shape: the same prefix, a fresh random identity."""
    if token.startswith('scan_'):
        return 'scan_' + secrets.token_urlsafe(24)
    if token.startswith('artifact_'):
        _, identity, sha256, size = token.split('_')
        return f'artifact_{secrets.token_hex(len(identity) // 2)}_{sha256}_{size}'
    prefix, _, suffix = token.rpartition('_')
    assert prefix and re.fullmatch(r'[0-9a-f]+', suffix) and len(suffix) % 2 == 0, token
    return f'{prefix}_{secrets.token_hex(len(suffix) // 2)}'


def masked(response, *tokens):
    """The wire response with each presented handle replaced, so a handle echoed back compares equal."""
    text = json.dumps(response, sort_keys=True)
    for token in tokens:
        text = text.replace(token, '<handle>')
    return json.loads(text)


def wire(account, operation, payload, client_id=CLIENT, generation=None):
    """One operation through the broker's socket: the handshake, then the request, as the proxy sends them (with
    the generation the handshake named, unless another is given)."""
    policy = load_runtime_policy(account.policy)
    deadline = time.monotonic() + 60

    def frame(name, body, generation, request_id):
        return {'version': PROTOCOL_VERSION, 'client_id': client_id, 'request_id': request_id, 'operation': name,
                'payload': body, 'deadline': deadline, 'broker_generation': generation}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(90)
        connection.connect(str(account.socket))
        send_frame(connection, frame('handshake', contract_descriptor(policy), None,
                                     'request_' + secrets.token_hex(12)), max_bytes=MAX_REQUEST_BYTES)
        hello = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        assert hello['ok'] is True, hello
        send_frame(connection, frame(operation, payload, generation or hello['result']['broker_generation'],
                                     'request_' + secrets.token_hex(12)), max_bytes=MAX_REQUEST_BYTES)
        response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
    return {key: value for key, value in response.items() if key in ('ok', 'result', 'error')}


class Side:
    """One served broker: its alias, paths, fake session and the owner dialogs it was asked to show."""

    def __init__(self, test, alias, account, client):
        self.alias, self.paths, self.client, self.approvals = alias, account, client, []
        self.broker = Broker(socket_path=account.socket, client_factory=lambda: client, account=account,
                             approval_prompt=lambda **facts: self.approvals.append(facts) or True)
        thread = threading.Thread(target=self.broker.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(thread.join, 10)
        test.addCleanup(self.broker.shutdown)  # Cleanups run last-in first: stop, then join.
        test.assertTrue(self.broker.wait_until_ready(timeout=10))

    def call(self, operation, payload, client_id=CLIENT, generation=None):
        return wire(self.paths, operation, payload, client_id, generation)

    def result(self, operation, payload, client_id=CLIENT):
        response = self.call(operation, payload, client_id)
        assert response.get('ok') is True, (operation, response)
        return response['result']

    def provider_changes(self):
        raw = getattr(self.client, '_raw', None)
        return [] if raw is None else [request['@type'] for request in raw.sent if request['@type'] in MUTATING]


class Pair(unittest.TestCase):
    """A short synthetic home whose registry lists `work` beside the default; MINTER names the alias of A."""

    MINTER = 'default'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='i')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        account_registry.add_alias('work', self.home, longest_tool=LONGEST)
        self.accounts = {alias: AccountPaths.for_alias(alias, self.home) for alias in ('default', 'work')}
        for account in self.accounts.values():
            account.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(account.root, 0o700)
            account.policy.write_text(POLICY_TOML)
            os.chmod(account.policy, 0o600)

    @property
    def presenter(self):
        return 'work' if self.MINTER == 'default' else 'default'

    def serve(self, minter_client, presenter_client):
        """A authorized as the fake's own user, B as another; each pins its own user at its first check."""
        a = Side(self, self.MINTER, self.accounts[self.MINTER], minter_client)
        b = Side(self, self.presenter, self.accounts[self.presenter], presenter_client)
        self.assertEqual(a.result('check', {}), {'status': 'ready'})
        self.assertEqual(b.result('check', {}), {'status': 'ready'})
        return a, b

    def pins(self):
        value = json.loads((accounts_root(self.home) / account_registry.REGISTRY_NAME).read_bytes())
        return {'default': value['default_user_id'], **{k: v['user_id'] for k, v in value['aliases'].items()}}

    def assert_refused_like_unknown(self, b, operation, payload, token, *, client_id=CLIENT):
        """B answers the foreign handle exactly as an unknown one of the same kind, before and after."""
        unknown, later = forged(token), forged(token)
        before = masked(b.call(operation, payload(unknown), client_id), unknown)
        foreign = masked(b.call(operation, payload(token), client_id), token)
        after = masked(b.call(operation, payload(later), client_id), later)
        self.assertEqual(foreign, before, f'{operation}: a handle of {self.MINTER} reached {self.presenter}')
        self.assertEqual(after, before, operation)
        return foreign

    def assert_untouched(self, b):
        self.assertEqual(b.approvals, [], 'the presenting broker showed an owner dialog')
        self.assertEqual(b.provider_changes(), [], 'the presenting broker changed provider state')


def draft_of(result):
    found = set(re.findall(r'draft_[0-9a-f]{32}', json.dumps(result)))
    assert len(found) == 1, result
    return found.pop()


# Every operation that takes a draft handle, with a payload valid in every other field.
DRAFT_OPERATIONS = (
    ('get_draft', lambda d: {'draft_id': d}),
    ('list_drafts', lambda d: {'after_draft_id': d}),
    ('update_draft', lambda d: {'draft_id': d, 'text': 'Changed text'}),
    ('update_draft', lambda d: {'draft_id': d, 'caption': 'Changed caption'}),
    ('refresh_draft', lambda d: {'draft_id': d}),
    ('get_send_status', lambda d: {'draft_id': d}),
    ('get_reply_draft', lambda d: {'draft_id': d}),
    ('update_reply_draft', lambda d: {'draft_id': d, 'text': 'Changed reply'}),
    ('refresh_reply_draft', lambda d: {'draft_id': d}),
    ('get_reply_artifact_draft', lambda d: {'draft_id': d}),
    ('update_reply_artifact_draft', lambda d: {'draft_id': d, 'caption': 'Changed caption'}),
    ('refresh_reply_artifact_draft', lambda d: {'draft_id': d}),
    ('send_prepared_text', lambda d: {'draft_id': d, 'approved': True}),
    ('send_prepared_artifact', lambda d: {'draft_id': d, 'approved': True}),
    ('send_prepared_forward', lambda d: {'draft_id': d, 'approved': True}),
    ('send_prepared_video', lambda d: {'draft_id': d, 'approved': True}),
    ('apply_prepared_archive', lambda d: {'draft_id': d, 'approved': True}),
    ('apply_prepared_text_edit', lambda d: {'draft_id': d, 'approved': True}),
    ('apply_prepared_caption_edit', lambda d: {'draft_id': d, 'approved': True}),
    ('apply_prepared_pin_change', lambda d: {'draft_id': d, 'approved': True}),
    ('send_prepared_album', lambda d: {'draft_id': d, 'approved': True}),
    ('get_album_send_status', lambda d: {'draft_id': d}),
    ('send_prepared_text', lambda d: {'draft_id': d, 'approved': False}),
    ('send_prepared_album', lambda d: {'draft_id': d, 'approved': False}),
    ('cancel_draft', lambda d: {'draft_id': d}),
)


OTHER = 4_000_000_901  # B's user; A keeps its fake's own user (the fakes' own messages are authored by it)


class PlainArtifactRaw(artifact_reply_fixture.ArtifactRaw):
    """The artifact-reply transport, answering a plain (non-reply) document send without a reply."""

    def send(self, request):
        start = len(self.events)
        super().send(request)
        if request['@type'] == 'sendMessage' and request.get('reply_to') is None:
            for event in list(self.events)[start:]:
                (event.get('message') or event).pop('reply_to', None)


class DraftMatrix:
    """One draft kind (a mixin over Pair): prepared on A, presented to B pending and again after A sent or applied
    it. B gets every draft operation, each with the foreign handle and with unknown ones. Photo and voice-note
    artifact drafts share the artifact kind's registry, operations and refusal; the document stands for them."""

    RAW = None    # the kind's synthetic TDLib transport (the production TDLibClient reduces it)
    SEND = None   # the operation that sends or applies this kind
    SENT = None   # A's status for it
    STATUS = 'get_send_status'  # the operation that reports its send status

    def client(self, user_id, account):
        raw = self.RAW()
        if hasattr(raw, 'mode'):
            raw.mode = 'sent'
        if user_id is not None:
            raw.account = user_id  # every kind's transport answers getMe with it
        client = TDLibClient(raw=raw, staging_root=account.staging)
        client.CHANGE_OBSERVATION_SECONDS, client.CHANGE_POLL_SECONDS = 0.05, 0.01  # archive and edit observation
        return client

    def prepare(self, a):
        raise NotImplementedError

    def local_artifact(self, a, name, data):
        made = a.result('create_local_artifact', {'file_name': name,
                                                  'content_base64': base64.b64encode(data).decode()})
        self.assertEqual(made['status'], 'complete', made)
        return made['artifact_id']

    def present_every_draft_operation(self, b, draft_id):
        for operation, payload in DRAFT_OPERATIONS:
            with self.subTest(operation=operation, payload=sorted(payload('x'))):
                self.assert_refused_like_unknown(b, operation, payload, draft_id)

    def test_a_draft_and_its_receipt_are_refused_like_unknown_ones(self):
        a, b = self.serve(self.client(None, self.accounts[self.MINTER]),
                          self.client(OTHER, self.accounts[self.presenter]))
        draft_id = self.prepare(a)
        pending = a.result(self.STATUS, {'draft_id': draft_id})
        self.assertEqual(pending['status'], 'pending', pending)  # A knows its own handle.
        self.present_every_draft_operation(b, draft_id)
        self.assertEqual(b.result('list_drafts', {})['drafts'], [])
        sent = a.result(self.SEND, {'draft_id': draft_id, 'approved': True})
        self.assertEqual(sent['status'], self.SENT, sent)
        self.assertEqual(len(a.approvals), 1)
        # A replays its own receipt (no second dialog or send); an unknown handle gets no receipt.
        receipt = a.result(self.SEND, {'draft_id': draft_id, 'approved': True})
        self.assertEqual(receipt['status'], self.SENT, receipt)
        unknown = forged(draft_id)
        self.assertNotEqual(a.result(self.SEND, {'draft_id': unknown, 'approved': True})['status'], self.SENT)
        status = a.result(self.STATUS, {'draft_id': draft_id})
        self.present_every_draft_operation(b, draft_id)  # The receipt (send status) is A's alone too.
        self.assert_untouched(b)
        self.assertEqual(a.result(self.SEND, {'draft_id': draft_id, 'approved': True}), receipt)
        self.assertEqual(a.result(self.STATUS, {'draft_id': draft_id}), status)
        self.assertEqual(len(a.approvals), 1)
        self.assertLessEqual(len(a.provider_changes()), 1)
        self.assertEqual(self.pins()[self.presenter], OTHER)
        self.assertNotIn(self.pins()[self.MINTER], (None, OTHER))


class TextDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = text_fixture.Raw, 'send_prepared_text', 'sent'

    def prepare(self, a):
        return draft_of(a.result('prepare_text_send', {'recipient': 123, 'text': 'PRIVATE BODY'}))


class ArtifactDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = PlainArtifactRaw, 'send_prepared_artifact', 'sent'

    def prepare(self, a):
        artifact = self.local_artifact(a, 'evidence.txt', b'bounded document')
        return draft_of(a.result('prepare_artifact_send', {'artifact_id': artifact, 'recipient': 123,
            'display_name': 'evidence.txt', 'mime_type': 'text/plain', 'caption': 'Caption'}))

    def test_a_document_staged_for_one_account_is_refused_by_the_others_transport(self):
        a, b = self.serve(self.client(None, self.accounts[self.MINTER]),
                          self.client(OTHER, self.accounts[self.presenter]))
        draft_id = self.prepare(a)
        self.assertEqual(a.result(self.SEND, {'draft_id': draft_id, 'approved': True})['status'], 'sent')
        [request] = [value for value in a.client._raw.sent if value['@type'] == 'sendMessage']
        payload = {key: value for key, value in request.items() if key != '@extra'}
        staged = Path(payload['input_message_content']['document']['document']['path'])
        self.assertEqual((staged.parent.parent, staged.parent.name), (a.paths.staging, draft_id))
        tdjson._validate_bounded_request(copy.deepcopy(payload), a.paths.staging)  # A's own transport admits it.
        with self.assertRaises(tdjson.ForbiddenTDLibRequest):
            tdjson._validate_bounded_request(copy.deepcopy(payload), b.paths.staging)
        self.assertEqual(b.client._staging_root, b.paths.staging)


class ReplyTextDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = reply_fixture.Raw, 'send_prepared_text', 'sent'

    def prepare(self, a):
        return draft_of(a.result('prepare_reply_text_send', {'recipient': 123, 'text': 'Final reply',
                                                             'reply_to': reply_fixture.ANCHOR}))


class ReplyArtifactDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = artifact_reply_fixture.ArtifactRaw, 'send_prepared_artifact', 'sent'

    def prepare(self, a):
        artifact = self.local_artifact(a, 'evidence.txt', b'bounded document')
        return draft_of(a.result('prepare_reply_artifact_send', {'artifact_id': artifact, 'recipient': 123,
            'display_name': 'evidence.txt', 'mime_type': 'text/plain', 'caption': 'Caption', 'kind': 'document',
            'reply_to': reply_fixture.ANCHOR}))


class ForwardDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = forward_fixture.Raw, 'send_prepared_forward', 'sent'

    def prepare(self, a):
        return draft_of(a.result('prepare_forward_send', {'source_chat': forward_fixture.SOURCE_CHAT,
            'source_message_id': 55, 'recipient': forward_fixture.RECIPIENT, 'mode': 'forward'}))


class VideoDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = video_fixture.Raw, 'send_prepared_video', 'sent'

    def prepare(self, a):
        artifact = self.local_artifact(a, 'clip.mp4', video_fixture.MOVIE)
        return draft_of(a.result('prepare_video_send', {'artifact_id': artifact,
                                                        'recipient': video_fixture.RECIPIENT}))


class ArchiveDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = archive_fixture.Raw, 'apply_prepared_archive', 'applied'

    def prepare(self, a):
        return draft_of(a.result('prepare_chat_archive', {'chat': archive_fixture.CHAT, 'target_list': 'archive'}))


class TextEditDraftTests(DraftMatrix, Pair):
    RAW, SEND, SENT = archive_fixture.Raw, 'apply_prepared_text_edit', 'applied'

    def prepare(self, a):
        return draft_of(a.result('prepare_text_edit', {'chat': archive_fixture.CHAT,
                                                       'message_id': archive_fixture.MID, 'new_text': 'Hello there'}))


class StyledTextEditDraftTests(DraftMatrix, Pair):
    """0.57 part 2: a styled text edit is a text edit draft bound to both edit capabilities, one more handle."""
    RAW, SEND, SENT = styled_fixture.Raw, 'apply_prepared_text_edit', 'applied'

    def prepare(self, a):
        return draft_of(a.result('prepare_text_edit', {
            'chat': archive_fixture.CHAT, 'message_id': archive_fixture.MID, 'new_text': 'PRIVATE TEXT',
            'outgoing_entities': [{'offset': 0, 'length': 7, 'type': {'@type': 'textEntityTypeBold'}}]}))


class CaptionEditDraftTests(DraftMatrix, Pair):
    """0.57 criterion 7: the caption edit draft id is one more handle kind (a styled caption, so both edit
    capabilities are exercised)."""
    RAW, SEND, SENT = caption_fixture.Raw, 'apply_prepared_caption_edit', 'applied'

    def prepare(self, a):
        return draft_of(a.result('prepare_caption_edit', {
            'chat': archive_fixture.CHAT, 'message_id': archive_fixture.MID, 'new_caption': 'PRIVATE CAPTION',
            'outgoing_entities': [{'offset': 0, 'length': 7, 'type': {'@type': 'textEntityTypeBold'}}]}))


class PinChangeDraftTests(DraftMatrix, Pair):
    """0.57 criterion 7 (part 2): the pin change draft id is one more handle kind; the pinned message is another
    member's, so its content is the minting alias's evidence alone."""
    RAW, SEND, SENT = pin_fixture.Raw, 'apply_prepared_pin_change', 'applied'

    def prepare(self, a):
        return draft_of(a.result('prepare_pin_change', {'chat': archive_fixture.CHAT,
                                                        'message_id': archive_fixture.MID, 'action': 'pin'}))


class AlbumDraftTests(DraftMatrix, Pair):
    """0.56 criterion 8: the album draft id is one more handle kind. Its item artifacts are account-bound like every
    artifact id, and its items are staged under the minting alias's own staging root."""
    RAW, SEND, SENT, STATUS = album_fixture.Raw, 'send_prepared_album', 'sent', 'get_album_send_status'

    def items(self, side, *names):
        data = {'one.png': album_fixture.PNG, 'two.jpg': album_fixture.JPEG, 'three.png': album_fixture.image('PNG', (30, 20))}
        return [self.local_artifact(side, name, data[name]) for name in names]

    def prepare(self, a):
        photo, jpeg = self.items(a, 'one.png', 'two.jpg')
        return draft_of(a.result('prepare_album_send', {'recipient': album_fixture.RECIPIENT, 'items': [
            {'artifact_id': photo, 'caption': 'PRIVATE CAPTION'}, {'artifact_id': jpeg}]}))

    def test_item_artifacts_of_one_account_are_refused_like_unknown_ones(self):
        a, b = self.serve(self.client(None, self.accounts[self.MINTER]),
                          self.client(OTHER, self.accounts[self.presenter]))
        foreign = self.items(a, 'one.png', 'two.jpg')
        own, other = self.items(b, 'one.png', 'three.png')  # B's own cache holds artifacts of the same kind.
        for position in (0, 1):
            for artifact_id in foreign:
                def payload(token, position=position):
                    items = [{'artifact_id': own}, {'artifact_id': token}]
                    return {'recipient': album_fixture.RECIPIENT, 'items': items if position else items[::-1]}
                with self.subTest(position=position, artifact=foreign.index(artifact_id)):
                    refused = self.assert_refused_like_unknown(b, 'prepare_album_send', payload, artifact_id)
                    self.assertEqual(refused['result']['status'], 'unavailable', refused)
        self.assert_untouched(b)
        self.assertEqual(b.result('list_drafts', {})['drafts'], [])
        # The refusal is the foreign item's: B prepares from its own two, and A still honours its own.
        own_album = b.result('prepare_album_send', {'recipient': album_fixture.RECIPIENT, 'items': [
            {'artifact_id': own}, {'artifact_id': other}]})
        self.assertEqual(own_album['status'], 'prepared', own_album)
        mine = a.result('prepare_album_send', {'recipient': album_fixture.RECIPIENT, 'items': [
            {'artifact_id': artifact_id} for artifact_id in foreign]})
        self.assertEqual(mine['status'], 'prepared', mine)
        self.assertEqual((a.approvals, b.approvals), ([], []))

    def test_an_album_staged_for_one_account_is_refused_by_the_others_transport(self):
        a, b = self.serve(self.client(None, self.accounts[self.MINTER]),
                          self.client(OTHER, self.accounts[self.presenter]))
        draft_id = self.prepare(a)
        self.assertEqual(a.result(self.SEND, {'draft_id': draft_id, 'approved': True})['status'], 'sent')
        [request] = [value for value in a.client._raw.sent if value['@type'] == 'sendMessageAlbum']
        payload = {key: value for key, value in request.items() if key != '@extra'}
        staged = [Path(content['photo']['photo']['path']) for content in payload['input_message_contents']]
        self.assertEqual({path.parent.parent for path in staged}, {a.paths.staging})
        self.assertEqual(len({path.parent for path in staged}), 2)  # One private directory per item.
        tdjson._validate_bounded_request(copy.deepcopy(payload), a.paths.staging)  # A's own transport admits it.
        with self.assertRaises(tdjson.ForbiddenTDLibRequest):
            tdjson._validate_bounded_request(copy.deepcopy(payload), b.paths.staging)


class TextDraftReverseTests(TextDraftTests):
    MINTER = 'work'


class ArtifactDraftReverseTests(ArtifactDraftTests):
    MINTER = 'work'


class ReplyTextDraftReverseTests(ReplyTextDraftTests):
    MINTER = 'work'


class ReplyArtifactDraftReverseTests(ReplyArtifactDraftTests):
    MINTER = 'work'


class ForwardDraftReverseTests(ForwardDraftTests):
    MINTER = 'work'


class VideoDraftReverseTests(VideoDraftTests):
    MINTER = 'work'


class ArchiveDraftReverseTests(ArchiveDraftTests):
    MINTER = 'work'


class TextEditDraftReverseTests(TextEditDraftTests):
    MINTER = 'work'


class StyledTextEditDraftReverseTests(StyledTextEditDraftTests):
    MINTER = 'work'


class CaptionEditDraftReverseTests(CaptionEditDraftTests):
    MINTER = 'work'


class PinChangeDraftReverseTests(PinChangeDraftTests):
    MINTER = 'work'


class AlbumDraftReverseTests(AlbumDraftTests):
    MINTER = 'work'


# Artifacts, uploads, selections and verified targets --------------------------------------------------------------

DOCUMENT = 30  # the one document message in chat 7


class Session(TargetProvider):
    """A fake TDLib session (a high-level double): text messages 1..21 (11 a reply target) and one document in
    chat 7, so every artifact, selection and reply call has a source A accepts."""

    def __init__(self, account, user_id=None):
        super().__init__()
        if user_id is not None:
            self.account = user_id
        self.send_observation_epoch = object()
        self._request_context = threading.local()
        self.source = account.files / 'documents' / 'private.txt'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b'abc')
        self.messages[11] = reply_fixture.target(chat_id=7, id=11, media_album_id=0,
                                                 sender_id={'@type': 'messageSenderUser', 'user_id': 17})
        self.messages[DOCUMENT] = message(DOCUMENT, content={'@type': 'messageDocument', 'document': {
            'file_name': 'private.txt', 'mime_type': 'text/plain',
            'document': {'@type': 'file', 'id': 1, 'size': 3, 'remote': {'unique_id': 'stable-document'}}}})

    def download_file(self, file_id, **kwargs):
        self.calls.append(('download', file_id))
        return self.source

    def read_reply_source(self, chat_id, message_id, **kwargs):
        self.calls.append(('reply_source', chat_id, message_id))
        return source_from_message(self.get_message(chat_id, message_id), chat_id, message_id)


ARTIFACT_OPERATIONS = (
    ('read_attachment', lambda t: {'artifact_id': t}),
    ('analyze_media', lambda t: {'artifact_id': t}),
    ('read_attachment_page', lambda t: {'artifact_id': t, 'render_pages': False}),
    ('read_spreadsheet', lambda t: {'artifact_id': t, 'selections': [{'sheet_index': 1, 'range': 'A1'}]}),
    ('read_presentation', lambda t: {'artifact_id': t, 'slides': [1]}),
    ('prepare_artifact_send', lambda t: {'artifact_id': t, 'recipient': 7, 'display_name': 'private.txt',
                                         'mime_type': 'text/plain', 'caption': ''}),
    ('prepare_reply_artifact_send', lambda t: {'artifact_id': t, 'recipient': 7, 'display_name': 'private.txt',
        'mime_type': 'text/plain', 'caption': '', 'reply_to': {'chat_id': 7, 'message_id': 11}}),
    ('prepare_video_send', lambda t: {'artifact_id': t, 'recipient': 7}),
    ('retain_selection', lambda t: {'source': {'kind': 'file', 'artifact_id': t}}),
)


def manifest(data, name='private.txt'):
    return {'file_name': name, 'kind': 'document', 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def chunk(token, data, index=0):
    return {'upload_id': token, 'index': index, 'content_base64': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest()}


class LocalHandles:
    """A mixin over Pair: both brokers on fake high-level sessions, each with its own document."""

    def serve_sessions(self):
        return self.serve(Session(self.accounts[self.MINTER]), Session(self.accounts[self.presenter], OTHER))

    def create(self, side, name='private.txt', data=b'private evidence'):
        made = side.result('create_local_artifact', {'file_name': name,
                                                     'content_base64': base64.b64encode(data).decode()})
        self.assertEqual(made['status'], 'complete', made)
        return made['artifact_id']

    def upload(self, side, data=b'uploaded evidence'):
        begun = side.result('begin_local_upload', manifest(data))
        self.assertEqual(begun['status'], 'ready', begun)
        self.assertEqual(side.result('append_local_upload', chunk(begun['upload_id'], data))['status'], 'accepted')
        done = side.result('finish_local_upload', {'upload_id': begun['upload_id']})
        self.assertEqual(done['status'], 'complete', done)
        return done['artifact_id']


class ArtifactHandleTests(LocalHandles, Pair):
    def test_artifact_ids_are_refused_like_unknown_ones(self):
        a, b = self.serve_sessions()
        telegram = a.result('get_attachment', {'anchor': {'chat_id': 7, 'message_id': DOCUMENT}})
        self.assertEqual(telegram['status'], 'complete', telegram)
        handles = {'telegram': telegram['artifact_id'], 'local': self.create(a), 'upload': self.upload(a)}
        own = self.create(b)  # B's own cache exists and holds the same kind of artifact.
        for source, artifact_id in handles.items():
            self.assertEqual(a.result('read_attachment', {'artifact_id': artifact_id})['status'], 'complete')
            for operation, payload in ARTIFACT_OPERATIONS:
                with self.subTest(source=source, operation=operation):
                    self.assert_refused_like_unknown(b, operation, payload, artifact_id)
        self.assert_untouched(b)
        self.assertEqual(b.result('read_attachment', {'artifact_id': own})['text'], 'private evidence')
        for artifact_id in handles.values():  # A still honours every one of them.
            self.assertEqual(a.result('read_attachment', {'artifact_id': artifact_id})['status'], 'complete')

    def test_a_download_from_another_accounts_session_files_is_refused(self):
        a, b = self.serve_sessions()
        own = b.client.source
        b.client.source = a.client.source  # B's TDLib names a file in A's session tree.
        anchor = {'anchor': {'chat_id': 7, 'message_id': DOCUMENT}}
        refused = b.result('get_attachment', anchor)
        self.assertEqual((refused['status'], refused['detail'], refused.get('artifact_id')),
                         ('error', 'TDLib file path is outside its private cache', None))
        b.client.source = own
        self.assertEqual(b.result('get_attachment', anchor)['status'], 'complete')
        self.assertEqual(a.result('get_attachment', anchor)['status'], 'complete')

    def download(self, side, artifact_id, destination):
        client = BrokerClient(socket_path=side.paths.socket, policy=load_runtime_policy(side.paths.policy),
                              restart_callback=lambda: None, account=side.paths)
        self.addCleanup(client.close)
        try:
            return transfer_cli.download_artifact(artifact_id=artifact_id, destination=str(destination),
                                                  client=client, account=side.paths)
        except transfer_cli.TransferError as error:
            return {'status': 'error', 'code': error.code}

    def test_the_transfer_download_refuses_another_accounts_artifact_like_an_unknown_one(self):
        a, b = self.serve_sessions()
        out = self.home / 'out'
        out.mkdir(mode=0o700)
        local, uploaded = self.create(a), self.upload(a)
        telegram = a.result('get_attachment', {'anchor': {'chat_id': 7, 'message_id': DOCUMENT}})['artifact_id']
        self.create(b)
        for index, artifact_id in enumerate((local, uploaded, telegram)):
            with self.subTest(artifact=index):
                foreign = self.download(b, artifact_id, out / f'foreign-{index}')
                unknown = self.download(b, forged(artifact_id), out / f'unknown-{index}')
                self.assertEqual(foreign, unknown)
                self.assertEqual(foreign, {'status': 'error', 'code': 'artifact_unverified'})
                self.assertFalse((out / f'foreign-{index}').exists())
                own = self.download(a, artifact_id, out / f'own-{index}')
                self.assertEqual(own['status'], 'complete', own)
        self.assertEqual((out / 'own-0').read_bytes(), b'private evidence')
        self.assertEqual(sorted(path.name for path in out.iterdir()), ['own-0', 'own-1', 'own-2'])

    def command(self, artifact_id, destination):
        """`telegram-search-transfer download` as B's owner runs it: `--account work`, or no flag for the default
        (whose module paths are pointed at this home's default account, as a default install reads them)."""
        arguments = ['download', '--artifact-id', artifact_id, '--destination', str(destination)]
        default = self.accounts['default']
        if self.presenter == 'work':
            arguments += ['--account', 'work']
            patches = (mock.patch.object(Path, 'home', return_value=self.home),)
        else:
            patches = (mock.patch.object(transfer_cli, 'BROKER_SOCKET_PATH', default.socket),
                       mock.patch.object(transfer_cli, 'CACHE_DIRECTORY', default.artifacts),
                       mock.patch.object(transfer_cli, 'load_runtime_policy',
                                         side_effect=lambda *_: load_runtime_policy(default.policy)))
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            code = transfer_cli.main(arguments)
        return code, json.loads(stderr.getvalue() or stdout.getvalue())

    def test_the_transfer_command_takes_only_its_own_aliases_cache(self):
        a, b = self.serve_sessions()
        out = self.home / 'out'
        out.mkdir(mode=0o700)
        local, own = self.create(a), self.create(b, data=b'own evidence')
        self.assertEqual(self.command(local, out / 'foreign'), self.command(forged(local), out / 'unknown'))
        self.assertEqual(self.command(local, out / 'foreign'), (1, {'status': 'error', 'code': 'artifact_unverified',
            'detail': transfer_cli.TransferError('artifact_unverified').args[0]}))
        self.assertEqual(list(out.iterdir()), [])
        code, receipt = self.command(own, out / 'own')  # The same command exports B's own artifact.
        self.assertEqual((code, receipt['status']), (0, 'complete'))
        self.assertEqual((out / 'own').read_bytes(), b'own evidence')


class UploadHandleTests(LocalHandles, Pair):
    def present_upload_session(self, b, token):
        for client_id in (CLIENT, HELPER):
            for operation, payload in (
                    ('append_local_upload', lambda t: chunk(t, b'abc')),
                    ('finish_local_upload', lambda t: {'upload_id': t}),
                    ('append_authorized_local_upload', lambda t: chunk(t, b'abc')),
                    ('finish_authorized_local_upload', lambda t: {'upload_id': t})):
                with self.subTest(operation=operation, client=client_id):
                    self.assert_refused_like_unknown(b, operation, payload, token, client_id=client_id)

    def present_result(self, b, result_ref):
        for operation in ('consume_local_upload_result', 'cancel_local_upload_authority'):
            with self.subTest(operation=operation):
                self.assert_refused_like_unknown(b, operation, lambda t: {'result_ref': t}, result_ref)

    def test_an_upload_session_is_refused_like_an_unknown_one(self):
        a, b = self.serve_sessions()
        data = b'abcdef'
        begun = a.result('begin_local_upload', manifest(data))
        self.present_upload_session(b, begun['upload_id'])
        self.assertEqual(a.result('append_local_upload', chunk(begun['upload_id'], data))['status'], 'accepted')
        self.present_upload_session(b, begun['upload_id'])  # Mid-upload, after A accepted a chunk.
        done = a.result('finish_local_upload', {'upload_id': begun['upload_id']})
        self.assertEqual(done['status'], 'complete', done)
        self.assertEqual(a.result('read_attachment', {'artifact_id': done['artifact_id']})['text'], 'abcdef')
        self.present_upload_session(b, begun['upload_id'])
        self.assert_untouched(b)

    def test_upload_authority_and_result_tokens_are_refused_like_unknown_ones(self):
        a, b = self.serve_sessions()
        data = b'authority evidence'
        issued = a.result('issue_local_upload_authority', {'manifest': manifest(data)})
        self.assertEqual(issued['status'], 'issued', issued)
        authority, result_ref = issued['upload_authority'], issued['result_ref']
        for client_id in (HELPER, CLIENT):
            with self.subTest(operation='redeem_local_upload_authority', client=client_id):
                self.assert_refused_like_unknown(b, 'redeem_local_upload_authority',
                    lambda t: {'upload_authority': t, 'manifest': manifest(data)}, authority, client_id=client_id)
        self.present_result(b, result_ref)  # Issued, not yet redeemed.
        redeemed = a.result('redeem_local_upload_authority', {'upload_authority': authority,
                                                              'manifest': manifest(data)}, client_id=HELPER)
        self.assertEqual(redeemed['status'], 'ready', redeemed)
        self.present_upload_session(b, redeemed['upload_id'])
        self.present_result(b, result_ref)  # Redeemed, pending.
        self.assertEqual(a.result('append_authorized_local_upload', chunk(redeemed['upload_id'], data),
                                  client_id=HELPER)['status'], 'accepted')
        finished = a.result('finish_authorized_local_upload', {'upload_id': redeemed['upload_id']}, client_id=HELPER)
        self.assertEqual(finished['status'], 'upload_complete', finished)
        self.present_result(b, result_ref)  # Supplied, not yet consumed.
        consumed = a.result('consume_local_upload_result', {'result_ref': result_ref})
        self.assertEqual(consumed['status'], 'complete', consumed)
        self.present_result(b, result_ref)  # Consumed.
        for operation, payload in ARTIFACT_OPERATIONS[:2]:
            with self.subTest(operation=operation, artifact='consumed upload'):
                self.assert_refused_like_unknown(b, operation, payload, consumed['artifact_id'])
        self.assertEqual(a.result('read_attachment', {'artifact_id': consumed['artifact_id']})['text'],
                         'authority evidence')
        self.assert_untouched(b)

    def test_the_transfer_helper_redeems_an_authority_only_at_its_own_aliases_broker(self):
        a, b = self.serve_sessions()
        source = self.home / 'source.txt'
        source.write_bytes(b'helper evidence')
        data = source.read_bytes()
        issued = a.result('issue_local_upload_authority', {'manifest': manifest(data, 'source.txt')})
        envelope = UploadAuthorityEnvelope.model_validate({'upload_authority': issued['upload_authority'],
                                                           'manifest': manifest(data, 'source.txt')})
        unknown = envelope.model_copy(update={'upload_authority': forged(issued['upload_authority'])})

        def helper(side, value):  # `telegram-search-transfer upload --upload-authority-stdin [--account]`
            try:
                return transfer_cli.upload_source(source=str(source), kind='document', client=None,
                                                  authority_envelope=value, account=side.paths)
            except transfer_cli.TransferError as error:
                return {'status': 'error', 'code': error.code}
        self.assertEqual(helper(b, envelope), helper(b, unknown))
        self.assertEqual(helper(b, envelope), {'status': 'error', 'code': 'upload_unverified'})
        self.present_result(b, issued['result_ref'])
        supplied = helper(a, envelope)
        self.assertEqual((supplied['status'], supplied['result_ref']), ('upload_complete', issued['result_ref']))
        self.present_result(b, issued['result_ref'])
        consumed = a.result('consume_local_upload_result', {'result_ref': issued['result_ref']})
        self.assertEqual(consumed['status'], 'complete', consumed)
        self.assertEqual(a.result('read_attachment', {'artifact_id': consumed['artifact_id']})['text'],
                         'helper evidence')
        self.assert_untouched(b)


# Every operation that takes a retained selection, with a payload valid in every other field.
SELECTED_OPERATIONS = (
    ('read_messages', lambda s: {'selection_id': s}),
    ('get_message_context', lambda s: {'selection_id': s}),
    ('read_reply_chain', lambda s: {'selection_id': s}),
    ('get_attachment', lambda s: {'selection_id': s}),
    ('read_attachment', lambda s: {'selection_id': s}),
    ('analyze_media', lambda s: {'selection_id': s}),
    ('read_attachment_page', lambda s: {'selection_id': s, 'render_pages': False}),
    ('read_spreadsheet', lambda s: {'selection_id': s}),
    ('read_presentation', lambda s: {'selection_id': s}),
    ('prepare_artifact_send', lambda s: {'selection_id': s, 'recipient': 7, 'display_name': 'private.txt',
                                         'mime_type': 'text/plain'}),
    ('prepare_reply_artifact_send', lambda s: {'selection_id': s, 'recipient': 7, 'display_name': 'private.txt',
        'mime_type': 'text/plain', 'reply_to': {'chat_id': 7, 'message_id': 11}}),
    ('prepare_reply_text_send', lambda s: {'reply_selection_id': s, 'recipient': 7, 'text': 'Reply text'}),
)


class SelectionHandleTests(LocalHandles, Pair):
    def test_retained_selections_are_refused_like_unknown_ones(self):
        a, b = self.serve_sessions()
        target = a.result('verify_target', {'target': 7})
        self.assertEqual(target['status'], 'verified', target)
        sources = {
            'messages': {'kind': 'messages', 'anchors': [{'chat_id': 7, 'message_id': 11}]},
            'file': {'kind': 'file', 'artifact_id': self.create(a)},
            'target_messages': {'kind': 'target_messages', 'target_handle': target['target_handle'],
                                'message_ids': [11]},
        }
        own = self.create(b)
        reply_artifact = ('prepare_reply_artifact_send', lambda s: {'artifact_id': own, 'reply_selection_id': s,
            'recipient': 7, 'display_name': 'private.txt', 'mime_type': 'text/plain'})
        for kind, source in sources.items():
            retained = a.result('retain_selection', {'source': source})
            self.assertEqual(retained['status'], 'retained', retained)
            selection = retained['selection_id']
            check = ('read_attachment' if kind == 'file' else 'read_messages', {'selection_id': selection})
            self.assertEqual(a.result(*check)['status'], 'complete')
            for operation, payload in (*SELECTED_OPERATIONS, reply_artifact):
                with self.subTest(kind=kind, operation=operation):
                    refused = self.assert_refused_like_unknown(b, operation, payload, selection)
                    self.assertEqual(refused['result']['status'], 'selection_unavailable')
            self.assertEqual(a.result(*check)['status'], 'complete')  # A still honours it.
        self.assertEqual(b.result('list_drafts', {})['drafts'], [])
        self.assert_untouched(b)

    def test_a_verified_target_is_refused_like_an_unknown_one(self):
        a, b = self.serve_sessions()
        handle = a.result('verify_target', {'target': 7})['target_handle']
        b.result('verify_target', {'target': 7})  # B holds a live handle of its own for the same chat.
        cases = (('read_target_messages', lambda t: {'target_handle': t, 'message_ids': [11]}),
                 ('retain_selection', lambda t: {'source': {'kind': 'target_messages', 'target_handle': t,
                                                            'message_ids': [11]}}))
        for operation, payload in cases:
            with self.subTest(operation=operation):
                self.assert_refused_like_unknown(b, operation, payload, handle)
        read = a.result('read_target_messages', {'target_handle': handle, 'message_ids': [11]})
        self.assertEqual(read['status'], 'complete', read)
        self.assert_untouched(b)


class GenerationTests(LocalHandles, Pair):
    def test_a_broker_generation_of_one_alias_is_stale_at_the_other(self):
        a, b = self.serve_sessions()
        cases = (('check', {}), ('resolve', {'target': 7}), ('read_messages', {'anchors': [{'chat_id': 7,
                 'message_id': 11}]}), ('create_local_artifact', {'file_name': 'x.txt', 'content': 'x'}),
                 ('issue_local_upload_authority', {'manifest': manifest(b'x')}))
        for operation, payload in cases:
            with self.subTest(operation=operation):
                foreign = b.call(operation, payload, generation=a.broker._generation)
                self.assertEqual(foreign, b.call(operation, payload, generation='broker_' + secrets.token_hex(16)))
                self.assertIn(foreign, ({'ok': False, 'error': 'generation_stale'},
                                        {'ok': True, 'result': {'status': 'unavailable',
                                                                'detail': 'local upload is unavailable'}}))
        self.assertEqual(b.result('check', {}), {'status': 'ready'})
        self.assert_untouched(b)


class ArtifactHandleReverseTests(ArtifactHandleTests):
    MINTER = 'work'


class UploadHandleReverseTests(UploadHandleTests):
    MINTER = 'work'


class SelectionHandleReverseTests(SelectionHandleTests):
    MINTER = 'work'


class GenerationReverseTests(GenerationTests):
    MINTER = 'work'


# Cursors ------------------------------------------------------------------------------------------------------------

class Closable:
    """The broker closes its client at shutdown; the reader fakes have nothing to close."""

    def close(self):
        pass


class History(Closable, HistoryProvider):
    def __init__(self, user_id=None):
        super().__init__({i: message(i) for i in (30, 20, 10)})
        self.account_id = self.account_id if user_id is None else user_id


class Searchable(Closable, SearchProvider):
    """Exact and boolean search: every branch query finds the same selected messages."""

    def __init__(self, user_id=None):
        super().__init__({i: message(i) for i in range(1, 46)})
        self.account_id = self.account_id if user_id is None else user_id

    def search_chat_messages(self, chat_id, query, *, from_message_id, limit, **filters):
        return super().search_chat_messages(chat_id, query, from_message_id=from_message_id, limit=limit)


class Forum(Closable, HistoryForumProvider):
    """Topics 1..3 in two native pages, and topic 2's history."""

    def __init__(self, user_id=None):
        super().__init__()
        self.account = self.account if user_id is None else user_id

    def get_forum_topics(self, chat, **kwargs):
        self.calls.append(('page', chat, kwargs))
        first = not kwargs.get('offset_forum_topic_id')
        return copy.deepcopy(forum_page([1, 2], (10, 100, 2)) if first else forum_page([3], (0, 0, 0)))


class Chats(Closable, ChatListProvider):
    def __init__(self, user_id=None):
        super().__init__(main=(9, 3, 7))
        self.account = self.account if user_id is None else user_id


class Selected(Closable, SelectedProvider):
    def __init__(self, user_id=None):
        super().__init__(count=3)
        self.account = self.account if user_id is None else user_id


class Discovery(Closable, DiscoveryAccountClient):
    def __init__(self, user_id=None):
        super().__init__(17 if user_id is None else user_id)
        self.send_observation_epoch = object()

    @contextlib.contextmanager
    def request_budget(self, deadline):
        yield


def workbook():
    cells = ''.join(f'<row r="{row}"><c r="A{row}"><v>{row}</v></c><c r="B{row}"><v>{row * 10}</v></c></row>'
                    for row in (1, 2))
    path = Path(tempfile.mkdtemp(dir='/tmp', prefix='x')) / 'book.xlsx'
    try:
        return write_xlsx(path, xlsx_parts([('Visible', 'visible', worksheet(cells))])).read_bytes()
    finally:
        path.unlink()
        path.parent.rmdir()


class CursorMatrix:
    """A mixin over Pair: a cursor issued on A, presented to B with the same arguments by the same client, before and
    after B holds a live scan of its own; B's own scan and A's cursor both still continue afterwards."""

    def assert_continues(self, side, operation, request, cursor):
        unknown = forged(cursor)
        refused = masked(side.result(operation, {**request, 'cursor': unknown}), unknown)
        continued = side.result(operation, {**request, 'cursor': cursor})
        self.assertNotEqual(masked(continued, cursor), refused, f'{side.alias} lost its own {operation} cursor')
        return continued

    def check(self, minter_client, presenter_client, operation, request, *, artifact=None):
        a, b = self.serve(minter_client, presenter_client)
        if artifact is not None:
            request = {**request, 'artifact_id': self.create_artifact(a, *artifact)}
        first = a.result(operation, request)
        cursor = first.get('next_cursor')
        self.assertTrue(cursor, first)
        payload = lambda value: {**request, 'cursor': value}
        self.assert_refused_like_unknown(b, operation, payload, cursor)
        own_request = request if artifact is None else {**request, 'artifact_id': self.create_artifact(b, *artifact)}
        own = b.result(operation, own_request)
        self.assertTrue(own.get('next_cursor'), own)
        self.assertNotEqual(own['next_cursor'], cursor)
        self.assert_refused_like_unknown(b, operation, payload, cursor)
        if artifact is not None:  # A's cursor against B's own artifact of the same bytes.
            self.assert_refused_like_unknown(b, operation, lambda value: {**own_request, 'cursor': value}, cursor)
        self.assert_continues(b, operation, own_request, own['next_cursor'])
        self.assert_continues(a, operation, request, cursor)
        self.assert_untouched(b)

    def create_artifact(self, side, name, data):
        made = side.result('create_local_artifact', {'file_name': name,
                                                     'content_base64': base64.b64encode(data).decode()})
        self.assertEqual(made['status'], 'complete', made)
        return made['artifact_id']

    def test_history_cursor(self):
        self.check(History(), History(OTHER), 'read_history', {'target': 7, 'mode': 'latest', 'limit': 1})

    def test_topics_cursor(self):
        self.check(Forum(), Forum(OTHER), 'list_topics', {'target': 7, 'limit': 1})

    def test_topic_history_cursor(self):
        self.check(Forum(), Forum(OTHER), 'read_topic_history',
                   {'target': 7, 'topic': {'kind': 'forum', 'id': 2}, 'mode': 'latest', 'limit': 1})

    def test_exact_search_cursor(self):
        self.check(Searchable(), Searchable(OTHER), 'search_messages',
                   {'target': 7, 'query': 'selected', 'mode': 'latest', 'limit': 1})

    def test_boolean_search_cursor(self):
        self.check(Searchable(), Searchable(OTHER), 'search_messages',
                   {'target': 7, 'query': {'any': ['selected', 'full']}, 'mode': 'latest', 'limit': 1})

    def test_chat_list_cursor(self):
        self.check(Chats(), Chats(OTHER), 'list_chats', {'scope': 'main', 'limit': 2})

    def test_selected_search_cursor(self):
        self.check(Selected(), Selected(OTHER), 'search_chats',
                   {'targets': [7, 8], 'query': 'Full', 'mode': 'latest', 'limit': 2})

    def test_attachment_page_cursor(self):
        self.check(Session(self.accounts[self.MINTER]), Session(self.accounts[self.presenter], OTHER),
                   'read_attachment_page', {'max_chars': 2, 'render_pages': False},
                   artifact=('sample.txt', b'abcdef'))

    def test_spreadsheet_cursor(self):
        self.check(Session(self.accounts[self.MINTER]), Session(self.accounts[self.presenter], OTHER),
                   'read_spreadsheet', {'selections': [{'sheet_index': 1, 'range': 'A1:B2'}], 'max_cells': 2},
                   artifact=('book.xlsx', workbook()))

    def test_discovery_scan_cursor(self):
        self.check(Discovery(), Discovery(OTHER), 'discover', {'hypotheses': ['garden', 'seeds'], 'scope': 'main'})


class CursorTests(CursorMatrix, Pair):
    pass


class CursorReverseTests(CursorMatrix, Pair):
    MINTER = 'work'


# The account pin through the socket -----------------------------------------------------------------------------

THIRD = 4_000_000_933


class PinTests(Pair):
    """The duplicate account and the pin mismatch as a served pair sees them: the fixed blocked or unavailable
    shapes, no user id on the wire, no content read, and the other broker unaffected."""

    def start(self, alias, client):
        return Side(self, alias, self.accounts[alias], client)

    def assert_blocked(self, side):
        self.assertEqual(side.result('check', {}), {'status': 'blocked'})
        calls = (('resolve', {'target': 7}), ('verify_target', {'target': 7}),
                 ('read_messages', {'anchors': [{'chat_id': 7, 'message_id': 11}]}),
                 ('read_history', {'target': 7, 'mode': 'latest', 'limit': 1}),
                 ('prepare_text_send', {'recipient': 7, 'text': 'Text'}),
                 ('get_attachment', {'anchor': {'chat_id': 7, 'message_id': DOCUMENT}}))
        for operation, payload in calls:
            with self.subTest(operation=operation):
                response = side.call(operation, payload)
                self.assertEqual(response, {'ok': False, 'error': 'unavailable'})  # the existing fixed shape
                for user_id in (17, OTHER, THIRD):
                    self.assertNotIn(str(user_id), json.dumps(response))
        self.assertLessEqual({call[0] for call in side.client.calls}, {'ready', 'account'})  # no content read

    def test_the_same_account_under_both_aliases_blocks_the_second(self):
        a = self.start(self.MINTER, Session(self.accounts[self.MINTER]))
        b = self.start(self.presenter, Session(self.accounts[self.presenter]))  # the same user, 17
        self.assertEqual(a.result('check', {}), {'status': 'ready'})
        b.client.calls.clear()
        self.assert_blocked(b)
        self.assertEqual(self.pins(), {self.MINTER: 17, self.presenter: None})
        handle = a.result('verify_target', {'target': 7})['target_handle']
        self.assert_refused_like_unknown(b, 'read_target_messages',
                                         lambda t: {'target_handle': t, 'message_ids': [11]}, handle)
        self.assertEqual(a.result('read_target_messages', {'target_handle': handle,
                                                           'message_ids': [11]})['status'], 'complete')
        self.assertEqual(self.pins(), {self.MINTER: 17, self.presenter: None})
        self.assert_untouched(b)

    def test_a_relogin_into_another_account_is_blocked_by_the_pin(self):
        a, b = self.serve(Session(self.accounts[self.MINTER]), Session(self.accounts[self.presenter], OTHER))
        pinned = self.pins()
        own = b.result('verify_target', {'target': 7})['target_handle']
        foreign = a.result('verify_target', {'target': 7})['target_handle']
        b.client.account, b.client.send_observation_epoch = THIRD, object()  # B's session now is a third user.
        b.client.calls.clear()
        self.assert_blocked(b)
        for handle in (own, foreign):  # Neither B's own handle nor A's reaches the third account.
            self.assert_refused_like_unknown(b, 'read_target_messages',
                                             lambda t: {'target_handle': t, 'message_ids': [11]}, handle)
        self.assertEqual(self.pins(), pinned)
        b.client.account = 17  # A's user under B's alias: a duplicate, not a re-pin.
        b.client.send_observation_epoch = object()
        self.assertEqual(b.result('check', {}), {'status': 'blocked'})
        self.assertEqual(self.pins(), pinned)
        b.client.account, b.client.send_observation_epoch = OTHER, object()
        self.assertEqual(b.result('check', {}), {'status': 'ready'})
        self.assertEqual(a.result('check', {}), {'status': 'ready'})
        self.assertEqual(self.pins(), pinned)
        self.assert_untouched(b)


class PinReverseTests(PinTests):
    MINTER = 'work'


# Budgets and launchd ----------------------------------------------------------------------------------------------

SOCKET_TAIL = '/Library/Application Support/TelegramSearchMCP-accounts/{}/run/broker.sock'


class BudgetTests(unittest.TestCase):
    """An alias is refused before anything exists when its socket path or tool names would not fit."""

    def home_of_length(self, length):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='b')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        home = base / ('h' * (length - len(str(base)) - 1))
        home.mkdir(mode=0o700)
        self.assertEqual(len(str(home)), length)
        return home

    def test_the_socket_path_budget_is_exact_and_refuses_before_creating_anything(self):
        alias = 'work'
        fits = account_registry.SOCKET_PATH_LIMIT - len(SOCKET_TAIL.format(alias))
        home = self.home_of_length(fits)
        account_registry.add_alias(alias, home, longest_tool=LONGEST)  # exactly 103 bytes
        self.assertEqual(len(os.fsencode(str(AccountPaths.for_alias(alias, home).socket))), 103)
        with self.assertRaises(account_registry.RegistryError) as caught:
            account_registry.add_alias('work1', home, longest_tool=LONGEST)  # 104 bytes
        self.assertEqual(caught.exception.code, 'alias_path_too_long')
        self.assertEqual(json.loads((accounts_root(home) / account_registry.REGISTRY_NAME).read_bytes())['aliases'],
                         {'work': {'user_id': None}})
        longer = self.home_of_length(fits + 1)
        with self.assertRaises(account_registry.RegistryError) as caught:
            account_registry.add_alias(alias, longer, longest_tool=LONGEST)
        self.assertEqual((caught.exception.code, str(caught.exception)), ('alias_path_too_long', 'alias_path_too_long'))
        self.assertEqual(list(longer.iterdir()), [])  # Nothing was created: no accounts root, no registry.
        # A hand-written entry whose socket would not fit is never admitted either: no paths, no broker.
        registry = accounts_root(home) / account_registry.REGISTRY_NAME
        aliases = {'work': {'user_id': None}, 'work1': {'user_id': None}}
        registry.write_bytes(json.dumps({'version': 1, 'aliases': aliases, 'default_user_id': None}).encode())
        self.assertEqual(sorted(account_registry.load(accounts_root(home)).aliases), ['work', 'work1'])
        with self.assertRaises(account_registry.RegistryError) as caught:
            AccountPaths.for_alias('work1', home)
        self.assertEqual(caught.exception.code, 'alias_path_too_long')
        self.assertEqual(AccountPaths.for_alias('work', home).alias, 'work')
        self.assertFalse((accounts_root(home) / 'work1').exists())

    def test_the_tool_name_budget_is_exact(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='b')
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)  # unresolved and short: every 12-character alias's socket fits
        self.assertLessEqual(len(str(home)) + len(SOCKET_TAIL.format('abcdefghijkl')),
                             account_registry.SOCKET_PATH_LIMIT)
        server = 'mcp__telegram_search_{}__'
        for alias in ('a', 'work', 'abcdefghijkl'):
            with self.subTest(alias=alias):
                room = account_registry.TOOL_NAME_LIMIT - len(server.format(alias))
                account_registry.check_budgets(alias, home, longest_tool='t' * room)
                with self.assertRaises(account_registry.RegistryError) as caught:
                    account_registry.check_budgets(alias, home, longest_tool='t' * (room + 1))
                self.assertEqual((caught.exception.code, caught.exception.args), ('alias_name_too_long',
                                                                                  ('alias_name_too_long',)))
        account_registry.check_budgets('abcdefghijkl', home)  # Every 12-character alias fits this release's tools.
        self.assertLessEqual(len(server.format('abcdefghijkl') + LONGEST), account_registry.TOOL_NAME_LIMIT)
        with self.assertRaises(account_registry.RegistryError) as caught:
            account_registry.add_alias('work', home, longest_tool='t' * 64)
        self.assertEqual(caught.exception.code, 'alias_name_too_long')
        self.assertEqual(list(home.iterdir()), [])


class LaunchdDomainTests(unittest.TestCase):
    """The default and named aliases in one fake gui domain: disjoint labels and plists, each job its own."""

    UID = 501

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='j')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        patcher = mock.patch.object(Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        account_registry.add_alias('work', self.home, longest_tool=LONGEST)
        self.default = AccountPaths.for_alias('default', self.home)
        self.work = AccountPaths.for_alias('work', self.home)
        self.python = Path(sys.executable)

    def start(self, launchd, account):
        launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd, uid=self.UID,
                                           account=account)
        self.assertEqual(launch_agent.start_connect_job(plist_path=account.connect_plist, python_executable=self.python,
                                                        runner=launchd, uid=self.UID, account=account), 'started')

    def remove(self, launchd, account):
        launch_agent.uninstall_launch_agent(runner=launchd, uid=self.UID, account=account)
        launch_agent.stop_connect_job(runner=launchd, uid=self.UID, account=account, sleeper=lambda _: None)

    def test_two_aliases_share_one_domain_without_naming_each_others_jobs(self):
        launchd = FakeLaunchd()
        for account in (self.default, self.work):
            self.start(launchd, account)
        jobs = {account.alias: (account.broker_label, account.connect_label) for account in (self.default, self.work)}
        self.assertEqual(set(launchd.loaded), {*jobs['default'], *jobs['work']})
        self.assertEqual(len({*jobs['default'], *jobs['work']}), 4)
        self.assertEqual(jobs['default'], (f'com.{self.home.name}.telegram-search-mcp.broker',
                                           f'com.{self.home.name}.telegram-search-mcp.connect'))
        self.assertEqual(jobs['work'], (f'com.{self.home.name}.telegram-search-mcp.a.work.broker',
                                        f'com.{self.home.name}.telegram-search-mcp.a.work.connect'))
        plists = [path for account in (self.default, self.work) for path in (account.broker_plist,
                                                                             account.connect_plist)]
        self.assertEqual(len(set(plists)), 4)
        self.assertTrue(all(path.exists() for path in plists[::2]))
        for label in jobs['default']:
            self.assertNotIn('--account', launchd.loaded[label][1])
        for label in jobs['work']:
            self.assertEqual(launchd.loaded[label][1][-2:], ['--account', 'work'])
        for account in (self.default, self.work):
            self.assertTrue(launch_agent.launch_agent_status(runner=launchd, uid=self.UID, account=account))
        default_bytes = self.default.broker_plist.read_bytes()
        launchd.calls.clear()
        self.remove(launchd, self.work)
        self.assertEqual(launchd.labels(), set(jobs['work']))
        self.assertEqual(set(launchd.loaded), set(jobs['default']))
        self.assertEqual(self.default.broker_plist.read_bytes(), default_bytes)
        self.assertFalse(self.work.broker_plist.exists())
        self.assertFalse(launch_agent.launch_agent_status(runner=launchd, uid=self.UID, account=self.work))
        self.assertTrue(launch_agent.launch_agent_status(runner=launchd, uid=self.UID, account=self.default))
        self.start(launchd, self.work)
        launchd.calls.clear()
        self.remove(launchd, self.default)  # And the other way round.
        self.assertEqual(launchd.labels(), set(jobs['default']))
        self.assertEqual(set(launchd.loaded), set(jobs['work']))

    def test_every_alias_of_a_full_registry_has_its_own_labels_and_paths(self):
        for alias in ('a', 'broker', 'connect', 'default1', 'a1', 'work2', 'z'):  # with work: the eight allowed
            account_registry.add_alias(alias, self.home, longest_tool=LONGEST)
        with self.assertRaises(account_registry.RegistryError) as caught:
            account_registry.add_alias('ninth', self.home, longest_tool=LONGEST)
        self.assertEqual(caught.exception.code, 'registry_full')
        registry = json.loads((accounts_root(self.home) / account_registry.REGISTRY_NAME).read_bytes())
        accounts = [self.default] + [AccountPaths.for_alias(alias, self.home) for alias in registry['aliases']]
        self.assertEqual(len(accounts), 9)
        for field in ('broker_label', 'connect_label', 'broker_plist', 'connect_plist', 'socket', 'root', 'artifacts',
                      'uploads', 'staging', 'session', 'policy', 'broker_lock', 'connect_lock', 'server_name'):
            with self.subTest(field=field):
                values = [getattr(account, field) for account in accounts]
                self.assertEqual(len(set(values)), len(values))
        for account in accounts[1:]:  # No named root lies inside another root or the legacy tree.
            for other in accounts:
                if other is not account:
                    self.assertFalse(account.root.is_relative_to(other.root))
        launchd = FakeLaunchd()
        for account in accounts:
            self.start(launchd, account)
        self.assertEqual(len(launchd.loaded), 18)
        launchd.calls.clear()
        self.remove(launchd, accounts[3])
        self.assertEqual(launchd.labels(), {accounts[3].broker_label, accounts[3].connect_label})
        self.assertEqual(len(launchd.loaded), 16)


if __name__ == '__main__':
    unittest.main()
