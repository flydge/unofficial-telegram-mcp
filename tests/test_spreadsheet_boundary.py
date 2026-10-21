"""Public XLSX capability and compatibility boundaries use literal synthetic data."""
from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from mcp import Client
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from tool_listing import usable
from telegram_search_mcp.contract import CompatibilityError, fingerprint, schema_document
from telegram_search_mcp.server import build_server
from test_attachment_page_broker import Provider

ART = 'artifact_' + '0'*32 + '_' + 'a'*64 + '_10'


class SpreadsheetBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_is_append_only_and_default_off_with_strict_raw_sdk_inputs(self):
        class Service:
            def __init__(self): self.calls = []
            def close(self): pass
            def read_spreadsheet(self, request):
                from telegram_search_mcp.spreadsheet_models import ReadSpreadsheetResponse
                self.calls.append(request)
                return ReadSpreadsheetResponse(status='unsupported')
        for enabled in (False, True):
            service = Service()
            policy = RuntimePolicy(enabled_capabilities=('spreadsheets',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                tools = (await consumer.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, usable(policy))
                self.assertEqual(any(tool.name == 'read_spreadsheet' for tool in tools), enabled)
                for tool in (tool for tool in tools if tool.name == 'read_spreadsheet'):
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.idempotent_hint)
                    self.assertFalse(tool.input_schema['additionalProperties'])
                result = await consumer.call_tool('read_spreadsheet', {'artifact_id': ART})
                self.assertEqual(bool(result.is_error), not enabled)
                before = len(service.calls)
                bad_inputs = [
                    {'selections': '[{"sheet_index":1,"range":"A1"}]'},
                    {'selections': [{'sheet_index': True, 'range': 'A1'}]},
                    {'selections': [{'sheet_index': 1.0, 'range': 'A1'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A:A'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'B2:A1'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A1:A10001'}]},
                    {'selections': [{'sheet_index': 1, 'range': 'A1', 'extra': 1}]},
                    {'selections': []}, {'max_cells': True}, {'max_cells': 1.0},
                    {'max_cells': 201}, {'cursor': 'invalid'}, {'unknown': True},
                ]
                for bad in bad_inputs:
                    result = await consumer.call_tool('read_spreadsheet', {'artifact_id': ART, **bad})
                    self.assertTrue(result.is_error, repr(bad))
                self.assertEqual(len(service.calls), before)

    async def test_unextended_prior_tool_schemas_and_annotations_are_unchanged(self):
        expected = json.loads((Path(__file__).parent/'fixtures/legacy_0_15_tool_hashes.json').read_text())
        # Preserve historical fixtures; explicitly pin topic and safe-ID changes.
        expected.update({
            'discover_targets': 'a54593acb9e7d02a6b36dcbc15a1a4a88e2c78863243e1d4f241268138b4991f',
            'get_attachment': '78f0deeb1dcd804967d1675f6fb4229e3d6f4b6d4fb0db1824f43f0f61ac0886',
            'get_message_context': '9963e67c5fefe8504bf309c27e4ae4cc4a3a925f4f4c20a3e79099dc62c444dc',
            'prepare_artifact_send': '6564642002e3975458c3f11e002fb004ede38d3c8b579ad6caed4b238ec1d9d2',
            'prepare_text_send': '2d1895770311016191e360afacd2913f060299ba91fcab8f569835b5dcae2e9e',
            'read_attachment_page': 'afae1d24823918dddd5c236c5852a0c4a06d5deb2c18571ee4ace2580a1c6749',
            'read_history': '35df5a2f4501145b3681bf202baf4e49dae06e9f614fe957715dcd6ec09fecb7',
            'read_messages': '305baa1649afb07a6e3f91c5491e5c04c0cb3c81af375fbfded33cebe23375c0',
            'read_reply_chain': 'dbed366c765a3e08c3e8af62fd2122233bf25f7ad90b3ead9715bdd62595e276',
            'read_target_messages': 'd05d7e06a80643f53ba25dd35f9005cb3a31e6cdb77bbb187b7834f654f3ac6e',
            'read_topic_history': '62950e541bf52550f944ee8386a9263d7434d6e3c644fe66ff322bb0aaca1139',
            'resolve_target': '3f38d5c455865ef235337f59e8ecd5c916ac52e8af40fc09dc35677cf17b2270',
            'search_chats': '1933e7d5c21416b94b8603304c8d45476477df9ac026582fbc12410f4ce75f24',
            'search_correspondence': 'eccbe60273f05eb090ac7dab8261af981e5e3c6170ca71b41ddb4e90cc0bdd90',
            'search_messages': '8dffc376a79704a8799161431f9a8c8ea7a188a5d64cadeeac3b96d638b3906f',
        })
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        # Historical fixtures remain immutable; retained alternatives intentionally
        # extend this bounded set while all other old schemas stay identical.
        extended = {'analyze_media','get_attachment','get_message_context','prepare_artifact_send',
            'read_attachment','read_messages','read_reply_chain','read_attachment_page',
            'read_spreadsheet','read_presentation'}
        unchanged = {name: digest for name,digest in expected.items() if name not in extended}
        self.assertEqual({name: actual[name] for name in unchanged}, unchanged)


class SpreadsheetBrokerBoundaryTests(unittest.TestCase):
    def test_xlsx_local_artifact_and_broker_capability_are_enforced(self):
        self.assertIn('read_spreadsheet', OPERATIONS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker(socket_path=root/'broker.sock', client_factory=Provider,
                artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=RuntimePolicy())
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op, payload):
                return broker._dispatch({'operation': op, 'payload': payload,
                    'client_id': 'client_'+'a'*24, 'broker_generation': broker._generation,
                    'deadline': time.monotonic()+30})
            made = dispatch('create_local_artifact', {'file_name': 'synthetic.xlsx',
                'content_base64': base64.b64encode(b'not-a-workbook').decode()})
            self.assertEqual(made['status'], 'complete')
            with self.assertRaises(CompatibilityError):
                dispatch('read_spreadsheet', {'artifact_id': made['artifact_id']})
            broker._policy = RuntimePolicy(enabled_capabilities=('artifacts','spreadsheets'))
            response = dispatch('read_spreadsheet', {'artifact_id': made['artifact_id']})
            self.assertIn(response['status'], {'error', 'unsupported'})
            self.assertIsNone(response['scope'])
            self.assertEqual(response['cells'], [])


if __name__ == '__main__': unittest.main()
