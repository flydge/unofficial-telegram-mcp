"""0.56 F15b, part 2 (surface): the three album tools, their guards, the proxy and the census.

The official MCP SDK client talks to the production server; the proxy is the production BrokerClient, against an
absent socket or a production broker serving a private socket over the synthetic album transport of
tests/test_album_send.py. No Telegram and no owner dialog: the approval prompt is a fake.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from mcp import Client
from mcp.shared.exceptions import MCPError

import test_album_send as album
from schema_changes import DIRECTORY as SCHEMA_CHANGES
from telegram_search_mcp import broker_client as broker_client_module
from telegram_search_mcp import server as server_module
from telegram_search_mcp.album_drafts import (GetAlbumSendStatusRequest, GetAlbumSendStatusResponse,
    PrepareAlbumSendRequest, PrepareAlbumSendResponse, SendPreparedAlbumRequest, SendPreparedAlbumResponse,
    album_status)
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS, fingerprint, schema_document
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES, available_tools, build_server
from test_guard_refusals import profile
from tool_listing import full_listing

TOOLS = ('prepare_album_send', 'send_prepared_album', 'get_album_send_status')
DID = 'draft_' + '1' * 32
ART = ['artifact_' + str(index) * 32 + '_' + 'd' * 64 + '_10' for index in range(1, 4)]
GOOD = {'prepare_album_send': {'recipient': album.RECIPIENT, 'items': [{'artifact_id': ART[0], 'caption': 'One'},
                                                                      {'artifact_id': ART[1]}]},
        'send_prepared_album': {'draft_id': DID, 'approved': True},
        'get_album_send_status': {'draft_id': DID}}
# The frozen get_send_status row (input, output and annotations) as 0.56 part 1 (37b25bf) publishes it.
GET_SEND_STATUS_SHA256 = '33eb3a1e1a565dd95a31be4f73ce1fdf64f88533ce95f117e12ada95e0663f64'
# Every one of the 55 earlier rows at 37b25bf, while the schema-change fixtures are the ones listed here: this slice
# adds tools and changes no existing schema. A later slice that adds a fixture is no longer pinned by it.
EARLIER_ROWS_SHA256 = 'e3e8c7019e236e29cb0983b414b39504a6796db2cceda0015dd56a8fa7cd595b'
FIXTURES_AT_THIS_SLICE = {'0.54-approvals', '0.54-fixes', '0.54-inputs', '0.54-recovery', '0.54-surface',
                          '0.56-albums', '0.56-album-tools'}
ALBUM_POLICY = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'album_send'))
VIDEO_POLICY = RuntimePolicy(enabled_capabilities=('artifacts', 'send', 'album_send', 'video_send'))


class ContractTests(unittest.TestCase):
    def test_each_operation_is_a_capability_row_an_ipc_operation_and_a_tool(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(OPERATION_CAPABILITIES[name], 'album_send')
                self.assertEqual(TOOL_OPERATIONS[name], name)
                self.assertIn(name, OPERATIONS)
                self.assertEqual(_TOOL_CAPABILITIES[name], ('album_send', 'send'))

    def test_the_census_grows_from_55_to_58(self):
        self.assertEqual(len(_TOOL_NAMES), 58)
        self.assertEqual(len(set(_TOOL_NAMES)), 58)
        self.assertEqual(tuple(_TOOL_NAMES[-3:]), TOOLS)

    def test_the_frozen_get_send_status_schema_and_every_earlier_row_are_untouched(self):
        rows = {row['name']: row for row in schema_document(build_server())['tools']}
        self.assertEqual(fingerprint(rows['get_send_status']), GET_SEND_STATUS_SHA256)
        if {path.stem for path in SCHEMA_CHANGES.glob('*.json')} == FIXTURES_AT_THIS_SLICE:
            earlier = sorted((row for name, row in rows.items() if name not in TOOLS), key=lambda row: row['name'])
            self.assertEqual(len(earlier), 55)
            self.assertEqual(fingerprint(earlier), EARLIER_ROWS_SHA256)

    def test_the_slice_fixture_lists_exactly_the_three_new_tools(self):
        document = json.loads((SCHEMA_CHANGES / '0.56-album-tools.json').read_text(encoding='utf-8'))
        self.assertEqual(set(document), {'structure'})
        self.assertEqual(set(document['structure']), set(TOOLS))

    def test_the_proxy_deadlines_cover_the_broker_budgets(self):
        # Preparation and status get the short request deadline; the send keeps the whole request timeout, which
        # covers the broker's 330-second send budget (dialog, staging and the 120-second confirmation wait).
        short = broker_client_module.SHORT_DEADLINE_OPERATIONS
        self.assertTrue({'prepare_album_send', 'get_album_send_status'} <= short)
        self.assertNotIn('send_prepared_album', short)
        self.assertEqual(short & {'send_prepared_text', 'send_prepared_artifact', 'send_prepared_video'}, set())
        proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=ALBUM_POLICY,
                             restart_callback=lambda: None)
        self.assertGreaterEqual(proxy._request_timeout, 330 + 30)


class DocCensusTests(unittest.TestCase):
    """056 re-review fix: every statement of the current candidate's tool count in the public docs is the census."""

    def test_the_docs_state_the_current_tool_count(self):
        root = Path(__file__).resolve().parents[1]
        ones = ('', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')
        tens = {5: 'fifty', 6: 'sixty'}
        count = len(_TOOL_NAMES)
        word = tens[count // 10] + ('-' + ones[count % 10] if count % 10 else '')
        found = []
        for name in ('reference.md', 'security.md', 'install.md'):
            text = (root / 'docs' / name).read_text(encoding='utf-8')
            found += re.findall(r'([a-z]+(?:-[a-z]+)?) tools of the current candidate', text)
            found += re.findall(r'\(([a-z]+(?:-[a-z]+)?) in the current candidate', text)
            found += [w for w in re.findall(r'The candidate exposes (\d+) tools', text)]
        self.assertGreaterEqual(len(found), 4)
        self.assertEqual(set(found) - {word, str(count)}, set())


class _Service:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def prepare_album_send(self, request):
        self.calls.append(request)
        return PrepareAlbumSendResponse(status='unavailable', detail='album draft is unavailable')

    def send_prepared_album(self, request):
        self.calls.append(request)
        return SendPreparedAlbumResponse(status='timeout', draft_id=request.draft_id, detail='synthetic')

    def get_album_send_status(self, request):
        self.calls.append(request)
        return album_status(request.draft_id)


class SdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemas_titles_and_annotations(self):
        tools = await full_listing()
        self.assertEqual(list(tools)[-3:], list(TOOLS))
        prepare, send, status = (tools[name] for name in TOOLS)
        self.assertEqual(set(prepare.input_schema['properties']), {'recipient', 'items', 'group'})
        self.assertEqual(sorted(prepare.input_schema['required']), ['items', 'recipient'])
        items = prepare.input_schema['properties']['items']
        self.assertEqual((items['minItems'], items['maxItems']), (2, 10))
        item = prepare.input_schema['$defs'][items['items']['$ref'].rsplit('/', 1)[1]]
        self.assertEqual((set(item['properties']), item['required'], item['additionalProperties']),
                         ({'artifact_id', 'caption', 'display_name'}, ['artifact_id'], False))
        self.assertEqual(item['properties']['caption']['maxLength'], 1024)
        # An optional display_name with exactly the published grammar of prepare_artifact_send's.
        grammar = lambda schema: {key: value for key, value in schema.items() if key not in ('title', 'description')}
        single = tools['prepare_artifact_send'].input_schema['properties']['display_name']
        branches = item['properties']['display_name']['anyOf']
        self.assertEqual([grammar(branch) for branch in branches], [grammar(single), {'type': 'null'}])
        refusals = json.dumps(prepare.output_schema)
        for code in ('unsupported_item', 'item_too_large', 'album_too_large', 'display_name_required'):
            self.assertIn(f'"{code}"', refusals)
        self.assertEqual(prepare.input_schema['properties']['group']['enum'], ['media', 'documents'])
        self.assertEqual(set(send.input_schema['properties']), {'draft_id', 'approved'})
        self.assertEqual(set(status.input_schema['properties']), {'draft_id'})
        for tool in (prepare, send, status):
            with self.subTest(tool=tool.name):
                self.assertIs(tool.input_schema['additionalProperties'], False)
                self.assertTrue(tool.title)
                self.assertTrue(tool.annotations.open_world_hint)
                self.assertFalse(tool.annotations.destructive_hint)
        self.assertEqual((prepare.annotations.read_only_hint, prepare.annotations.idempotent_hint), (True, False))
        self.assertEqual((send.annotations.read_only_hint, send.annotations.idempotent_hint), (False, False))
        self.assertEqual((status.annotations.read_only_hint, status.annotations.idempotent_hint), (True, True))
        # The album status has its own closed model; get_send_status says nothing about albums.
        self.assertIn('partial', json.dumps(status.output_schema))
        self.assertNotIn('partial', json.dumps(tools['get_send_status'].output_schema))

    async def test_listed_only_with_album_send_and_send_and_refused_at_the_guard_otherwise(self):
        cases = ((RuntimePolicy(enabled_capabilities=()), 'album_send, send', profile('albums')),
                 (RuntimePolicy(), 'album_send', profile('albums')),
                 (RuntimePolicy(enabled_capabilities=('send', 'artifacts', 'video_send')), 'album_send',
                  profile('albums')),
                 (RuntimePolicy(enabled_capabilities=('album_send', 'artifacts')), 'send', profile('send')),
                 (ALBUM_POLICY, None, None), (VIDEO_POLICY, None, None))
        for policy, missing, hint in cases:
            service = _Service()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                listed = {tool.name for tool in (await consumer.list_tools()).tools}
                self.assertEqual(listed, set(available_tools(policy.enabled_capabilities)))
                self.assertEqual(set(TOOLS) <= listed, missing is None)
                self.assertEqual(set(TOOLS) & listed, set() if missing else set(TOOLS))
                for name in TOOLS:
                    with self.subTest(policy=policy.enabled_capabilities, tool=name):
                        result = await consumer.call_tool(name, GOOD[name])
                        if missing is None:
                            self.assertFalse(result.is_error, result.content)
                            continue
                        self.assertTrue(result.is_error)
                        self.assertIsNone(result.structured_content)
                        self.assertEqual(result.content[0].text,
                                         f'Error executing tool {name}: capability_disabled: {missing}{hint}')
            if missing is None:
                self.assertEqual(service.calls, [PrepareAlbumSendRequest(**GOOD['prepare_album_send']),
                                                 SendPreparedAlbumRequest(**GOOD['send_prepared_album']),
                                                 GetAlbumSendStatusRequest(**GOOD['get_album_send_status'])])
            else:
                self.assertEqual(service.calls, [])

    async def test_closed_inputs_never_reach_the_service(self):
        service = _Service()
        one = {'artifact_id': ART[0]}
        bad = {'prepare_album_send': (
                   {'items': [one]}, {'items': [one] * 2}, {'items': [{'artifact_id': ART[index % 3]} for index in range(11)]},
                   {'items': [one, {'artifact_id': ART[1], 'caption': 'x' * 1025}]},
                   {'items': [one, {'artifact_id': ART[1], 'kind': 'photo'}]},
                   {'items': [one, {'artifact_id': 'artifact_bad'}]}, {'group': 'audio'}, {'group': 'Media'},
                   {'recipient': '4242'}, {'recipient': True}, {'reply_to': {'chat_id': 1, 'message_id': 2}},
                   {'topic': {'kind': 'forum', 'id': 3}}, {'selection_id': 'selection_' + 'a' * 64},
                   {'items': 'artifact'}, {'chat': album.RECIPIENT}),
               'send_prepared_album': ({'approved': 'true'}, {'approved': 1}, {'draft_id': 'draft_bad'}, {'extra': 1}),
               'get_album_send_status': ({'draft_id': 'draft_bad'}, {'draft_id': DID.upper()}, {'approved': True})}
        async with Client(build_server(service_factory=lambda: service, policy=ALBUM_POLICY)) as consumer:
            for name, cases in bad.items():
                for change in cases:
                    with self.subTest(tool=name, change=change):
                        try:
                            result = await consumer.call_tool(name, GOOD[name] | change)
                        except MCPError:
                            continue
                        self.assertTrue(result.is_error)
                        self.assertIn('invalid_arguments', result.content[0].text)
                        self.assertNotIn('x' * 20, result.content[0].text)
        self.assertEqual(service.calls, [])

    async def test_item_and_group_errors_name_the_parameter_and_undeclared_keys_are_only_counted(self):
        """As docs/reference.md states it: counts, duplicates and any error inside an item name `items`, another
        group names `group`, and an undeclared top-level key (a selection, a reply, a topic) is only counted."""
        one, two = {'artifact_id': ART[0]}, {'artifact_id': ART[1]}
        items, group = 'invalid_arguments: invalid items', 'invalid_arguments: invalid group'
        counted = 'invalid_arguments: 1 unexpected argument(s)'
        cases = (({'items': [one]}, items), ({'items': [{'artifact_id': ART[i % 3]} for i in range(11)]}, items),
                 ({'items': [one, one]}, items), ({'items': [one, two | {'kind': 'photo'}]}, items),
                 ({'items': [one, two | {'caption': 'x' * 1025}]}, items),
                 ({'items': [one, two | {'display_name': ''}]}, items),
                 ({'items': [one, two | {'display_name': 'x' * 256}]}, items), ({'group': 'audio'}, group),
                 ({'selection_id': 'selection_' + 'a' * 64}, counted), ({'reply_to': {'chat_id': 1, 'message_id': 2}},
                                                                         counted),
                 ({'topic': {'kind': 'forum', 'id': 3}}, counted), ({'chat': 7}, counted))
        async with Client(build_server(service_factory=_Service, policy=ALBUM_POLICY)) as consumer:
            for change, expected in cases:
                with self.subTest(change=repr(change)[:60]):
                    result = await consumer.call_tool('prepare_album_send',
                                                      {'recipient': 7, 'items': [one, two]} | change)
                    self.assertEqual(result.content[0].text, f'Error executing tool prepare_album_send: {expected}')

    async def test_duplicate_items_name_the_items_parameter(self):
        async with Client(build_server(service_factory=_Service, policy=ALBUM_POLICY)) as consumer:
            result = await consumer.call_tool('prepare_album_send', {'recipient': 7, 'items': [{'artifact_id': ART[0]}] * 2})
        self.assertEqual(result.content[0].text,
                         'Error executing tool prepare_album_send: invalid_arguments: invalid items')


class ProxyTests(album.Store, unittest.TestCase):
    def proxy(self, policy, connects):
        def connector(path):
            connects.append(path)
            raise OSError('synthetic no broker')
        return BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=policy, connector=connector,
                            restart_callback=lambda: None, sleeper=lambda _: None)

    def calls(self, proxy):
        return (proxy.prepare_album_send(PrepareAlbumSendRequest(**GOOD['prepare_album_send'])),
                proxy.send_prepared_album(SendPreparedAlbumRequest(**GOOD['send_prepared_album'])),
                proxy.get_album_send_status(GetAlbumSendStatusRequest(**GOOD['get_album_send_status'])))

    def test_refuses_before_connecting_unless_album_send_and_send(self):
        for policy in (RuntimePolicy(), RuntimePolicy(enabled_capabilities=('send', 'video_send', 'artifacts')),
                       RuntimePolicy(enabled_capabilities=('album_send', 'artifacts'))):
            connects = []
            with self.subTest(policy=policy.enabled_capabilities):
                prepared, sent, status = self.calls(self.proxy(policy, connects))
                self.assertEqual(connects, [])
                self.assertEqual((prepared.status, prepared.album), ('unavailable', None))
                self.assertEqual((sent.status, sent.detail), ('error', 'request rejected before dispatch: '
                                                              'capability_disabled'))
                self.assertEqual(status, album_status(DID))

    def test_a_lost_broker_is_never_sent_and_points_to_the_album_status(self):
        connects = []
        prepared, sent, status = self.calls(self.proxy(ALBUM_POLICY, connects))
        self.assertTrue(connects)
        self.assertEqual(prepared.status, 'unavailable')
        self.assertEqual((sent.status, sent.slots, sent.next_action), ('outcome_unknown', [], 'check_send_status'))
        self.assertIn('get_album_send_status', sent.detail)
        self.assertIn('never resend', sent.detail)
        self.assertEqual(status, album_status(DID))

    def test_a_preview_or_receipt_that_differs_from_the_request_is_refused(self):
        from datetime import datetime, timedelta, timezone
        from telegram_search_mcp.album_drafts import AlbumDraftPreview, AlbumRecipient
        photo, jpeg = self.item(album.PNG, 'one.png', caption='One'), self.item(album.JPEG, 'two.jpg')
        preview = AlbumDraftPreview.create(draft_id=DID, account_id=album.ACCOUNT,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            recipient=AlbumRecipient(chat_id=album.RECIPIENT, title='Recipient'), album=self.spec(photo, jpeg))
        prepared = PrepareAlbumSendResponse(status='prepared', album=preview,
            detail='immutable album draft prepared; inspect every item and obtain fresh approval')
        request = {'recipient': album.RECIPIENT, 'items': [{'artifact_id': photo.artifact_id, 'caption': 'One'},
                                                           {'artifact_id': jpeg.artifact_id}]}
        proxy = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=ALBUM_POLICY,
                             restart_callback=lambda: None)
        proxy._request = lambda operation, payload: prepared.model_dump(mode='json')
        self.assertEqual(proxy.prepare_album_send(PrepareAlbumSendRequest(**request)).album, preview)
        # A given display_name is the delivered name: the preview must state it, normalized as the broker does.
        for name in ('one.png', 'dir/one.png'):
            named = {'items': [request['items'][0] | {'display_name': name}, request['items'][1]]}
            self.assertEqual(proxy.prepare_album_send(PrepareAlbumSendRequest(**(request | named))).album, preview)
        for change in ({'recipient': album.RECIPIENT + 1}, {'group': 'documents'},
                       {'items': request['items'][::-1]},
                       {'items': [{'artifact_id': photo.artifact_id, 'caption': 'Other'}, request['items'][1]]},
                       {'items': [request['items'][0], {'artifact_id': jpeg.artifact_id, 'caption': 'Added'}]},
                       {'items': [request['items'][0] | {'display_name': 'other.png'}, request['items'][1]]},
                       {'items': [request['items'][0], request['items'][1] | {'display_name': 'я' * 130 + '.jpg'}]}):
            with self.subTest(change=change):
                refused = proxy.prepare_album_send(PrepareAlbumSendRequest(**(request | change)))
                self.assertEqual((refused.status, refused.album), ('unavailable', None))
        receipt = SendPreparedAlbumResponse(status='timeout', draft_id='draft_' + '2' * 32, detail='synthetic')
        proxy._request = lambda operation, payload: receipt.model_dump(mode='json')
        lost = proxy.send_prepared_album(SendPreparedAlbumRequest(draft_id=DID, approved=True))
        self.assertEqual((lost.status, lost.draft_id), ('outcome_unknown', DID))
        other = album_status('draft_' + '2' * 32, 'local_pending', album_sha256='a' * 64)
        proxy._request = lambda operation, payload: other.model_dump(mode='json')
        self.assertEqual(proxy.get_album_send_status(GetAlbumSendStatusRequest(draft_id=DID)), album_status(DID))


class EndToEnd:
    """The official SDK client, the production server and proxy, and a production broker on a private socket over
    the synthetic album transport: one dialog, one provider attempt, per-item outcomes and the album status."""
    POLICY = VIDEO_POLICY

    def setUp(self):
        from telegram_search_mcp.artifact_store import ArtifactStore
        from telegram_search_mcp.broker import Broker
        from telegram_search_mcp.tdjson import TDLibClient
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='a')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.raw = album.Raw()
        self.client = TDLibClient(raw=self.raw, staging_root=root / 'stage')
        self.approvals = []
        self.broker = Broker(socket_path=root / 'b.sock', lock_path=root / 'b.lock',
            artifact_store=ArtifactStore(cache_dir=root / 'cache'), client_factory=lambda: self.client,
            policy=self.POLICY, approval_prompt=lambda **facts: self.approvals.append(facts) or True)
        self.broker._staging = {'root': root / 'stage'}
        thread = threading.Thread(target=self.broker.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.broker.shutdown)
        self.assertTrue(self.broker.wait_until_ready(timeout=10))
        self.proxy = BrokerClient(socket_path=root / 'b.sock', policy=self.POLICY, restart_callback=lambda: None)
        self.addCleanup(self.proxy.close)

    def consumer(self):
        return Client(build_server(service_factory=lambda: self.proxy, policy=self.POLICY))

    async def call(self, consumer, name, arguments):
        result = await consumer.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def upload(self, consumer, data, name):
        made = await self.call(consumer, 'create_local_artifact',
                               {'file_name': name, 'content_base64': base64.b64encode(data).decode()})
        self.assertEqual(made['status'], 'complete', made)
        return made['artifact_id']

    def albums(self):
        return [request for request in self.raw.sent if request['@type'] == 'sendMessageAlbum']


class EndToEndTests(EndToEnd, unittest.IsolatedAsyncioTestCase):
    async def test_prepare_send_replay_and_status_through_the_official_client(self):
        async with self.consumer() as consumer:
            ids = [await self.upload(consumer, album.PNG, 'one.png'), await self.upload(consumer, album.MOVIE, 'two.mp4'),
                   await self.upload(consumer, album.JPEG, 'three.jpg')]
            prepared = await self.call(consumer, 'prepare_album_send', {'recipient': album.RECIPIENT, 'items': [
                {'artifact_id': ids[0], 'caption': 'First'}, {'artifact_id': ids[1]}, {'artifact_id': ids[2]}]})
            self.assertEqual(prepared['status'], 'prepared', prepared)
            preview = prepared['album']
            self.assertEqual([(item['kind'], item['artifact_id']) for item in preview['items']],
                             [('photo', ids[0]), ('video', ids[1]), ('photo', ids[2])])
            draft_id = preview['draft_id']
            pending = await self.call(consumer, 'get_album_send_status', {'draft_id': draft_id})
            self.assertEqual((pending['status'], pending['evidence']), ('pending', 'local_pending'))
            unapproved = await self.call(consumer, 'send_prepared_album', {'draft_id': draft_id, 'approved': False})
            self.assertEqual((unapproved['status'], self.approvals, self.albums()), ('declined', [], []))
            sent = await self.call(consumer, 'send_prepared_album', {'draft_id': draft_id, 'approved': True})
            self.assertEqual((sent['status'], [slot['status'] for slot in sent['slots']]), ('sent', ['sent'] * 3))
            self.assertEqual(sent['album_sha256'], preview['album_sha256'])
            replay = await self.call(consumer, 'send_prepared_album', {'draft_id': draft_id, 'approved': True})
            self.assertEqual((replay['status'], replay['slots']), ('sent', sent['slots']))
            status = await self.call(consumer, 'get_album_send_status', {'draft_id': draft_id})
            self.assertEqual((status['status'], status['evidence'], status['slots']), ('sent', 'provider', sent['slots']))
            generic = await self.call(consumer, 'get_send_status', {'draft_id': draft_id})
            self.assertEqual((generic['status'], generic['evidence']), ('outcome_unknown', 'none'))
        self.assertEqual((len(self.albums()), len(self.approvals)), (1, 1))
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['display_name'], dialog['sha256'], dialog['size_bytes']),
                         ('album', 'Album', preview['album_sha256'], preview['total_size_bytes']))

    async def test_a_large_album_left_unconfirmed_points_to_the_album_status_and_never_resends(self):
        self.raw.outcomes = ['sent', 'silent', 'silent']
        self.client.ALBUM_CONFIRMATION_SECONDS = 0.05
        async with self.consumer() as consumer:
            ids = [await self.upload(consumer, album.PNG, 'one.png'), await self.upload(consumer, album.JPEG, 'two.jpg'),
                   await self.upload(consumer, album.image('PNG', (30, 20)), 'three.png')]
            prepared = await self.call(consumer, 'prepare_album_send', {'recipient': album.RECIPIENT,
                                                                        'items': [{'artifact_id': i} for i in ids]})
            draft_id = prepared['album']['draft_id']
            sent = await self.call(consumer, 'send_prepared_album', {'draft_id': draft_id, 'approved': True})
            self.assertEqual((sent['status'], sent['next_action']), ('partial', 'check_send_status'))
            self.assertEqual([slot['status'] for slot in sent['slots']], ['sent', 'outcome_unknown', 'outcome_unknown'])
            self.assertIn('get_album_send_status', sent['detail'])
            self.assertIn('never resend', sent['detail'])
            replay = await self.call(consumer, 'send_prepared_album', {'draft_id': draft_id, 'approved': True})
            self.assertEqual((replay['status'], replay['next_action']), ('partial', 'check_send_status'))
            self.raw.events.extend([album.succeeded(-12, 902, 'messagePhoto'), album.succeeded(-13, 903, 'messagePhoto')])
            status = await self.call(consumer, 'get_album_send_status', {'draft_id': draft_id})
            self.assertEqual((status['status'], [slot.get('message_id') for slot in status['slots']]),
                             ('sent', [701, 902, 903]))
            self.assertNotIn('next_action', status)
        self.assertEqual(len(self.albums()), 1)


class VideoSendOffEndToEndTests(EndToEnd, unittest.IsolatedAsyncioTestCase):
    POLICY = ALBUM_POLICY

    async def test_a_video_album_names_video_send_and_creates_no_draft(self):
        async with self.consumer() as consumer:
            photo, video = (await self.upload(consumer, album.PNG, 'one.png'),
                            await self.upload(consumer, album.MOVIE, 'two.mp4'))
            refused = await self.call(consumer, 'prepare_album_send', {'recipient': album.RECIPIENT, 'items': [
                {'artifact_id': photo}, {'artifact_id': video}]})
            self.assertEqual(refused, {'status': 'capability_disabled', 'album': None, 'refusal': None,
                                       'missing_capabilities': ['video_send'],
                                       'detail': 'album needs capabilities the owner has not enabled: '
                                                 'missing_capabilities',
                                       'next_action': 'enable_capability'})
            self.assertEqual(self.broker._drafts.known_ids(), set())
            other = await self.upload(consumer, album.JPEG, 'three.jpg')
            photos = await self.call(consumer, 'prepare_album_send', {'recipient': album.RECIPIENT, 'items': [
                {'artifact_id': photo}, {'artifact_id': other}]})
            self.assertEqual(photos['status'], 'prepared', photos)  # Photos alone need no video_send.
        self.assertEqual((self.approvals, self.albums()), ([], []))


if __name__ == '__main__':
    unittest.main()
