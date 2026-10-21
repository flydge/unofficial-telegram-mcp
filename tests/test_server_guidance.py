"""Server instructions, manifest availability and the policy-filtered tools/list (0.53 phase D).

Every assertion goes through the official SDK client; tool counts derive from the registration.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp import Client

from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.contract import (OPERATION_CAPABILITIES, TOOL_OPERATIONS, fingerprint, required_capabilities,
                                          schema_document, schema_fingerprint)
from telegram_search_mcp.server import _TOOL_NAMES, available_tools, build_server
from test_guard_refusals import CONNECT_DEFAULT
from tool_listing import FULL_POLICY, full_listing

ANCHORS = ('_manifest(check_broker=true)', 'discover_targets', 'get_send_status', 'declined', 'timeout',
           'dialog_unavailable', 'next_action', 'untrusted', 'capability_disabled', 'available_tools',
           'tool_capabilities')
CONNECT_TOOLS = ['_manifest', 'resolve_target', 'discover_targets', 'search_correspondence', 'get_attachment',
                 'get_message_context', 'read_attachment', 'analyze_media', 'create_local_artifact', 'begin_local_upload',
                 'append_local_upload', 'finish_local_upload', 'issue_local_upload_authority',
                 'consume_local_upload_result', 'cancel_local_upload_authority']
SEND_TOOLS = {'prepare_text_send', 'send_prepared_text', 'prepare_artifact_send', 'send_prepared_artifact', 'list_drafts',
              'get_draft', 'get_send_status', 'cancel_draft', 'update_draft', 'refresh_draft'}
POLICIES = (RuntimePolicy(enabled_capabilities=()), CONNECT_DEFAULT, RuntimePolicy(),
            RuntimePolicy(enabled_capabilities=('send', 'archive_chats')),
            RuntimePolicy(enabled_capabilities=('read', 'list_chats', 'retained_selections')), FULL_POLICY)


def no_provider():
    raise AssertionError('provider opened')


def server(policy):
    return build_server(service_factory=no_provider, policy=policy)


class InstructionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_carries_bounded_client_neutral_instructions(self):
        async with Client(server(CONNECT_DEFAULT)) as client:
            instructions = client.instructions
            description = client.server_info.description
        self.assertIsInstance(instructions, str)
        self.assertLessEqual(len(instructions), 2500)
        for anchor in ANCHORS:
            self.assertIn(anchor, instructions)
        for text in (instructions, description):
            self.assertNotIn('codex', text.casefold())
            self.assertNotIn('claude', text.casefold())
        self.assertIn('account-wide', description)

    async def test_instructions_are_the_same_under_every_policy(self):
        seen = set()
        for policy in POLICIES:
            async with Client(server(policy)) as client:
                seen.add(client.instructions)
        self.assertEqual(len(seen), 1)


class ManifestAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def manifest(self, policy):
        async with Client(server(policy)) as client:
            result = await client.call_tool('_manifest', {})
        self.assertFalse(result.is_error)
        return result.structured_content

    async def test_connect_default_lists_exactly_the_read_and_artifact_tools(self):
        manifest = await self.manifest(CONNECT_DEFAULT)
        self.assertEqual(manifest['available_tools'], CONNECT_TOOLS)
        self.assertEqual(manifest['tools'], list(_TOOL_NAMES))
        capabilities = manifest['tool_capabilities']
        self.assertEqual(list(capabilities), list(_TOOL_NAMES))
        self.assertEqual(capabilities['_manifest'], [])
        self.assertEqual(capabilities['resolve_target'], ['read'])
        self.assertEqual(capabilities['prepare_reply_text_send'], ['reply_text_send', 'send'])
        self.assertEqual(capabilities['prepare_chat_archive'], ['archive_chats', 'send', 'read'])

    async def test_send_adds_only_the_ten_send_and_draft_tools(self):
        manifest = await self.manifest(RuntimePolicy(enabled_capabilities=('read', 'artifacts', 'send')))
        self.assertEqual(set(manifest['available_tools']) - set(CONNECT_TOOLS), SEND_TOOLS)
        self.assertFalse({name for name in manifest['available_tools'] if 'reply' in name or 'forward' in name})

    def test_dependent_capabilities_need_their_bases(self):
        self.assertNotIn('prepare_chat_archive', available_tools(('send', 'archive_chats')))
        self.assertIn('prepare_chat_archive', available_tools(('send', 'archive_chats', 'read')))
        self.assertNotIn('send_prepared_forward', available_tools(('forward_send',)))
        self.assertEqual(available_tools(()), ['_manifest'])

    def test_each_tool_is_available_exactly_when_its_guard_requirement_is_met(self):
        for name in _TOOL_NAMES:
            needed = required_capabilities(OPERATION_CAPABILITIES[TOOL_OPERATIONS[name]]) if name != '_manifest' else ()
            with self.subTest(tool=name):
                self.assertIn(name, available_tools(needed))
                for missing in needed:
                    self.assertNotIn(name, available_tools(tuple(c for c in needed if c != missing)))

    async def test_stale_policy_reports_only_the_manifest_as_available(self):
        manifest = await self.manifest(RuntimePolicy(enabled_capabilities=('read',), expected_package_version='0.0.0'))
        self.assertEqual(manifest['compatibility']['status'], 'config_stale')
        self.assertEqual(manifest['available_tools'], ['_manifest'])
        self.assertEqual(manifest['semantic_layer'], "the MCP client's model")


class FilteredListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_list_follows_policy_while_the_fingerprint_covers_the_registration(self):
        order = list(await full_listing())
        self.assertEqual(sorted(order), sorted(_TOOL_NAMES))
        for policy in POLICIES:
            with self.subTest(enabled=policy.enabled_capabilities):
                built = server(policy)
                async with Client(built) as client:
                    listed = [tool.name for tool in (await client.list_tools()).tools]
                    manifest = (await client.call_tool('_manifest', {})).structured_content
                usable = available_tools(policy.enabled_capabilities)
                self.assertEqual(len(listed), len(usable))
                self.assertEqual(listed, [name for name in order if name in usable])
                self.assertEqual(set(manifest['available_tools']), set(listed))
                self.assertEqual(manifest['schema_fingerprint'], schema_fingerprint())
                self.assertEqual(fingerprint(schema_document(built)), schema_fingerprint())

    async def test_a_stale_policy_lists_only_the_manifest_like_the_manifest_reports(self):
        async def observe(client):
            listed = [tool.name for tool in (await client.list_tools()).tools]
            return listed, (await client.call_tool('_manifest', {})).structured_content

        stale = RuntimePolicy(enabled_capabilities=('read', 'artifacts'), expected_package_version='0.0.0')
        async with Client(server(stale)) as client:
            listed, manifest = await observe(client)
        self.assertEqual((manifest['compatibility']['status'], listed, manifest['available_tools']),
                         ('config_stale', ['_manifest'], ['_manifest']))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'runtime.toml'
            path.write_text('config_version = 1\nenabled_capabilities = ["read", "artifacts", "send"]\n')
            path.chmod(0o600)
            async with Client(server(load_runtime_policy(path))) as client:
                fresh, manifest = await observe(client)
                self.assertEqual(set(fresh), set(available_tools(('read', 'artifacts', 'send'))))
                self.assertEqual(set(manifest['available_tools']), set(fresh))
                path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n')  # changed after load
                listed, manifest = await observe(client)
        self.assertEqual((manifest['compatibility']['status'], listed, manifest['available_tools']),
                         ('config_stale', ['_manifest'], ['_manifest']))

    async def test_an_unlisted_tool_still_answers_with_its_fixed_refusal(self):
        async with Client(server(CONNECT_DEFAULT)) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool('prepare_text_send', {'recipient': 7, 'text': 'UNLISTED_CANARY'})
        self.assertNotIn('prepare_text_send', listed)
        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].text, 'Error executing tool prepare_text_send: capability_disabled: send '
                                                 '(owner must enable in runtime.toml)')


if __name__ == '__main__':
    unittest.main()
