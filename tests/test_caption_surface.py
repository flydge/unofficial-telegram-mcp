"""0.57 work item 3, the surface: the two caption edit tools, their guards, the census and the official client.

The official MCP SDK client talks to the production server; the proxy is the production BrokerClient, against an
absent socket or a production broker serving a private socket over the synthetic transport of
tests/test_caption_edit.py. No Telegram and no owner dialog: the approval prompt is a fake.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from mcp import Client
from mcp.shared.exceptions import MCPError

from schema_changes import DIRECTORY as SCHEMA_CHANGES
from test_caption_edit import (BOLD, CAPTION, DISABLED, STYLED, STYLED_ONLY, TOOLS, Raw)
from test_archive_edit import CHAT, DID, MID
from test_guard_refusals import profile
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.caption_edit_drafts import (ApplyPreparedCaptionEditRequest, ApplyPreparedCaptionEditResponse,
    PrepareCaptionEditRequest, PrepareCaptionEditResponse, UNAVAILABLE as CAPTION_UNAVAILABLE)
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES, TOOL_OPERATIONS
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES, available_tools, build_server
from telegram_search_mcp.tdjson import TDLibClient


class ContractTests(unittest.TestCase):
    """Work items 2 and 3: the two tools, their capability rows, the draft kind and the census."""

    def test_each_operation_is_a_capability_row_an_ipc_operation_and_a_tool(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(OPERATION_CAPABILITIES[name], 'edit_own_caption')
                self.assertEqual(TOOL_OPERATIONS[name], name)
                self.assertIn(name, OPERATIONS)
                self.assertEqual(_TOOL_CAPABILITIES[name], ('edit_own_caption', 'send', 'read'))

    def test_the_census_grows_by_two_after_the_album_tools(self):
        self.assertEqual(len(_TOOL_NAMES), 60)
        self.assertEqual(len(set(_TOOL_NAMES)), 60)
        self.assertEqual(tuple(_TOOL_NAMES[-2:]), TOOLS)

    def test_the_slice_fixtures_list_the_new_tools_and_the_grown_capability_enum(self):
        tools = json.loads((SCHEMA_CHANGES / '0.57-caption-tools.json').read_text(encoding='utf-8'))
        self.assertEqual(set(tools), {'structure'})
        self.assertEqual(set(tools['structure']), set(TOOLS))
        capabilities = json.loads((SCHEMA_CHANGES / '0.57-capabilities.json').read_text(encoding='utf-8'))
        self.assertEqual(set(capabilities), {'structure'})
        self.assertEqual(set(capabilities['structure']), {
            'get_reply_artifact_draft', 'get_reply_draft', 'prepare_reply_artifact_send', 'prepare_reply_text_send',
            'refresh_reply_artifact_draft', 'refresh_reply_draft', 'update_reply_artifact_draft',
            'update_reply_draft', 'prepare_album_send'})


class _Service:
    def __init__(self):
        self.calls = []

    def close(self):
        pass

    def prepare_caption_edit(self, request):
        self.calls.append(request)
        return PrepareCaptionEditResponse(status='unavailable', detail=CAPTION_UNAVAILABLE)

    def apply_prepared_caption_edit(self, request):
        self.calls.append(request)
        return ApplyPreparedCaptionEditResponse(status='dialog_unavailable', draft_id=request.draft_id,
                                                detail='synthetic')


GOOD = {'prepare_caption_edit': {'chat': CHAT, 'message_id': MID, 'new_caption': 'New caption'},
        'apply_prepared_caption_edit': {'draft_id': DID, 'approved': True}}


class SdkSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemas_titles_and_annotations(self):
        from tool_listing import full_listing
        tools = await full_listing()
        self.assertEqual(list(tools)[-2:], list(TOOLS))
        prepare, apply = (tools[name] for name in TOOLS)
        self.assertEqual(set(prepare.input_schema['properties']), {'chat', 'message_id', 'new_caption',
                                                                   'outgoing_entities'})
        self.assertEqual(sorted(prepare.input_schema['required']), ['chat', 'message_id', 'new_caption'])
        self.assertEqual(prepare.input_schema['properties']['new_caption']['maxLength'], 1024)
        self.assertNotIn('minLength', prepare.input_schema['properties']['new_caption'])
        self.assertIn('edit_own_styled_text', prepare.input_schema['properties']['outgoing_entities']['description'])
        self.assertEqual(set(apply.input_schema['properties']), {'draft_id', 'approved'})
        output = json.dumps(prepare.output_schema)
        for code in ('unchanged_caption', 'hidden_source_content', 'capability_disabled', 'enable_capability'):
            self.assertIn(f'"{code}"', output)
        for status in ('declined', 'timeout', 'dialog_unavailable', 'check_send_status'):
            self.assertIn(f'"{status}"', json.dumps(apply.output_schema))
        for tool in (prepare, apply):
            self.assertIs(tool.input_schema['additionalProperties'], False)
            self.assertTrue(tool.title)
        self.assertEqual((prepare.annotations.read_only_hint, prepare.annotations.destructive_hint), (True, False))
        self.assertEqual((apply.annotations.read_only_hint, apply.annotations.destructive_hint), (False, True))
        self.assertEqual(apply.annotations, tools['apply_prepared_text_edit'].annotations)

    async def test_listed_only_with_every_capability_and_refused_at_the_guard_otherwise(self):
        cases = ((RuntimePolicy(enabled_capabilities=()), 'edit_own_caption, send, read', None),
                 (RuntimePolicy(), 'edit_own_caption', profile('archive_edit')),
                 (STYLED_ONLY, 'edit_own_caption', profile('archive_edit')),
                 (RuntimePolicy(enabled_capabilities=('send', 'edit_own_caption')), 'read', None),
                 (CAPTION, None, None), (STYLED, None, None))
        for policy, missing, hint in cases:
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
                        if hint is not None:
                            self.assertTrue(result.content[0].text.endswith(hint), result.content[0].text)
            self.assertEqual(service.calls, [] if missing else [PrepareCaptionEditRequest(**GOOD[TOOLS[0]]),
                                                                ApplyPreparedCaptionEditRequest(**GOOD[TOOLS[1]])])

    async def test_closed_inputs_never_reach_the_service(self):
        service = _Service()
        bad = {'prepare_caption_edit': (
                   {'message_id': 0}, {'message_id': 2**53}, {'message_id': '5'}, {'new_caption': 'x' * 1025},
                   {'new_caption': 5}, {'new_caption': None}, {'chat': 0}, {'chat': True}, {'caption': 'x'},
                   {'reply_markup': None}, {'show_caption_above_media': True},
                   {'outgoing_entities': [{'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeTextUrl'}}]},
                   {'outgoing_entities': [{'offset': 0, 'length': 3, 'type': {'@type': 'textEntityTypeBold'}, 'x': 1}]},
                   {'outgoing_entities': [BOLD] * 33}),
               'apply_prepared_caption_edit': ({'approved': 'true'}, {'approved': 1}, {'draft_id': 'draft_bad'},
                                               {'extra': 1})}
        async with Client(build_server(service_factory=lambda: service, policy=STYLED)) as consumer:
            for name, cases in bad.items():
                for change in cases:
                    with self.subTest(tool=name, change=repr(change)[:60]):
                        try:
                            result = await consumer.call_tool(name, GOOD[name] | change)
                        except MCPError:
                            continue
                        self.assertTrue(result.is_error)
                        self.assertIn('invalid_arguments', result.content[0].text)
                        self.assertNotIn('x' * 20, result.content[0].text)
        self.assertEqual(service.calls, [])


class EndToEnd:
    """The official SDK client, the production server and proxy, and a production broker on a private socket over
    the synthetic transport: the conjunction, one dialog, one provider attempt and the receipt."""
    POLICY = STYLED

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='c')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.raw = Raw()
        self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=root / 'b.sock', lock_path=root / 'b.lock',
                             artifact_store=ArtifactStore(cache_dir=root / 'cache'), client_factory=lambda: self.client,
                             policy=self.POLICY, approval_prompt=lambda **facts: self.approvals.append(facts) or True)
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


class EndToEndTests(EndToEnd, unittest.IsolatedAsyncioTestCase):
    async def test_prepare_apply_replay_and_status_through_the_official_client(self):
        async with self.consumer() as consumer:
            prepared = await self.call(consumer, 'prepare_caption_edit', {**GOOD['prepare_caption_edit'],
                                                                          'outgoing_entities': [BOLD]})
            self.assertEqual(prepared['status'], 'prepared', prepared)
            did = prepared['caption_edit']['draft_id']
            unapproved = await self.call(consumer, 'apply_prepared_caption_edit', {'draft_id': did, 'approved': False})
            self.assertEqual((unapproved['status'], self.approvals), ('declined', []))
            applied = await self.call(consumer, 'apply_prepared_caption_edit', {'draft_id': did, 'approved': True})
            self.assertEqual((applied['status'], applied['edit_date']), ('applied', 1700000500))
            replay = await self.call(consumer, 'apply_prepared_caption_edit', {'draft_id': did, 'approved': True})
            self.assertEqual(replay['status'], 'applied')
            status = await self.call(consumer, 'get_send_status', {'draft_id': did})
            self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))
        self.assertEqual((len([r for r in self.raw.sent if r['@type'] == 'editMessageCaption']), len(self.approvals)),
                         (1, 1))


class StyledOffEndToEndTests(EndToEnd, unittest.IsolatedAsyncioTestCase):
    POLICY = CAPTION

    async def test_a_styled_caption_names_edit_own_styled_text_and_creates_no_draft(self):
        async with self.consumer() as consumer:
            refused = await self.call(consumer, 'prepare_caption_edit', {**GOOD['prepare_caption_edit'],
                                                                         'outgoing_entities': [BOLD]})
            self.assertEqual(refused, {'status': 'capability_disabled', 'caption_edit': None, 'refusal': None,
                                       'missing_capabilities': ['edit_own_styled_text'], 'detail': DISABLED,
                                       'next_action': 'enable_capability'})
            self.assertEqual(self.broker._drafts.known_ids(), set())
            plain = await self.call(consumer, 'prepare_caption_edit', GOOD['prepare_caption_edit'])
            self.assertEqual(plain['status'], 'prepared', plain)
        self.assertEqual(self.approvals, [])
        self.assertEqual([r for r in self.raw.sent if r['@type'] == 'editMessageCaption'], [])


if __name__ == '__main__':
    unittest.main()
