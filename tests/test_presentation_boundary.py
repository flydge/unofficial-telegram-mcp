"""Risk tests for the selected PPTX public surface and its trusted opt-in."""
from __future__ import annotations

import base64
import importlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from mcp import Client
from schema_changes import changed_since_0_52
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_protocol import OPERATIONS
from telegram_search_mcp.config import RuntimePolicy
from tool_listing import usable
from telegram_search_mcp.contract import CompatibilityError, fingerprint, schema_document
from telegram_search_mcp.server import build_server
from test_attachment_page_broker import Provider

ART = 'artifact_' + '0'*32 + '_' + 'a'*64 + '_10'


class PresentationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_and_strict_raw_sdk_inputs_prevent_dispatch(self):
        class Service:
            def __init__(self): self.calls = []
            def close(self): pass
            def read_presentation(self, request):
                from telegram_search_mcp.presentation_models import ReadPresentationResponse
                self.calls.append(request)
                return ReadPresentationResponse(status='unsupported')
        for enabled in (False, True):
            service = Service()
            policy = RuntimePolicy(enabled_capabilities=('presentations',)) if enabled else RuntimePolicy()
            async with Client(build_server(service_factory=lambda: service, policy=policy)) as consumer:
                tools = (await consumer.list_tools()).tools
                self.assertEqual({t.name for t in tools}, usable(policy))
                self.assertEqual('read_presentation' in [t.name for t in tools], enabled)
                for tool in (t for t in tools if t.name == 'read_presentation'):
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertFalse(tool.input_schema['additionalProperties'])
                result = await consumer.call_tool('read_presentation', {'artifact_id': ART})
                self.assertEqual(bool(result.is_error), not enabled)
                before = len(service.calls)
                for bad in ({'slides': '[1]'}, {'slides': [True]}, {'slides': [1.0]},
                            {'slides': [0]}, {'slides': [129]}, {'slides': [1, 1]},
                            {'slides': []}, {'slides': [1,2,3,4,5,6]},
                            {'include_notes': 1}, {'include_notes': 'true'},
                            {'cursor': 'unused'}, {'unknown': True}):
                    result = await consumer.call_tool('read_presentation', {'artifact_id': ART, **bad})
                    self.assertTrue(result.is_error, repr(bad))
                self.assertEqual(len(service.calls), before)

    async def test_unextended_prior_tool_schemas_and_annotations_are_unchanged(self):
        expected = json.loads((Path(__file__).parent/'fixtures/legacy_0_16_tool_hashes.json').read_text())
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
            'read_spreadsheet': 'f2c0e1edd8fb5d1fd85161d7eb5f1e22aa8eebc053be3d3f4374dcc9d5880a78',
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
            'read_spreadsheet','read_presentation'} | set(changed_since_0_52())
        unchanged = {name: digest for name,digest in expected.items() if name not in extended}
        self.assertEqual({name: actual[name] for name in unchanged}, unchanged)


class PresentationBrokerBoundaryTests(unittest.TestCase):
    def test_inflight_policy_and_generation_drift_are_rechecked(self):
        from telegram_search_mcp.presentation_models import ReadPresentationResponse
        for drift in ('policy','generation'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                broker=Broker(socket_path=root/'broker.sock',client_factory=Provider,
                    artifact_store=ArtifactStore(cache_dir=root/'cache'),
                    policy=RuntimePolicy(enabled_capabilities=('presentations',)))
                self.addCleanup(broker._executor.shutdown)
                # In-flight drift must start from an admitted synthetic artifact.
                source=root/'inert.pptx'; source.write_bytes(b'inert presentation')
                with broker._artifact_authority.scope('client_'+'a'*24, broker._artifact_store):
                    artifact=broker._artifact_store.store(source)
                entered=[]
                def change(reader,request,**kwargs):
                    entered.append(drift)
                    if drift=='policy':broker._policy=RuntimePolicy()
                    else:broker._generation='broker_'+'d'*32
                    return ReadPresentationResponse(status='unsupported')
                with patch('telegram_search_mcp.presentations.PresentationReader.read',new=change):
                    with self.assertRaises(CompatibilityError):
                        broker._dispatch({'operation':'read_presentation','payload':{'artifact_id':artifact.artifact_id},
                            'client_id':'client_'+'a'*24,'broker_generation':broker._generation,
                            'deadline':time.monotonic()+30})
                self.assertEqual(entered,[drift])

    def test_local_pptx_metadata_and_broker_capability(self):
        self.assertIn('read_presentation', OPERATIONS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker(socket_path=root/'broker.sock', client_factory=Provider,
                artifact_store=ArtifactStore(cache_dir=root/'cache'), policy=RuntimePolicy())
            self.addCleanup(broker._executor.shutdown)
            def dispatch(op, payload):
                return broker._dispatch({'operation': op, 'payload': payload,
                    'client_id': 'client_'+'a'*24, 'broker_generation': broker._generation,
                    'deadline': time.monotonic()+30})
            made = dispatch('create_local_artifact', {'file_name': 'synthetic.pptx',
                'content_base64': base64.b64encode(b'not-a-presentation').decode()})
            self.assertEqual(made['status'], 'complete')
            metadata = broker._attachment_metadata(made['artifact_id'])
            self.assertEqual(metadata[2], 'application/vnd.openxmlformats-officedocument.presentationml.presentation')
            with self.assertRaises(CompatibilityError):
                dispatch('read_presentation', {'artifact_id': made['artifact_id']})
            broker._policy = RuntimePolicy(enabled_capabilities=('artifacts','presentations'))
            response = dispatch('read_presentation', {'artifact_id': made['artifact_id']})
            self.assertIn(response['status'], {'error', 'unsupported'})
            self.assertIsNone(response['scope'])
            self.assertEqual(response['slides'], [])


if __name__ == '__main__': unittest.main()
