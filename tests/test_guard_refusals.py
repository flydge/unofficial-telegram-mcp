"""Guard refusals reach the official SDK client as fixed, redacted reasons."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp import Client

from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy, load_runtime_policy
from telegram_search_mcp.contract import OPERATION_CAPABILITIES
from telegram_search_mcp.server import build_server

CONNECT_DEFAULT = RuntimePolicy(enabled_capabilities=('read', 'artifacts'))
SENTINEL = 'GUARD_INPUT_SENTINEL_5c1e'
# Each text ends with the fixed next_action suffix from the closed recovery map (0.54).
OWNER = ' (owner must enable in runtime.toml); next_action: enable_capability'
STALE = 'config_stale (restart matching components); next_action: update_installation'


class GuardRefusalTests(unittest.IsolatedAsyncioTestCase):
    def server(self, policy):
        return build_server(service_factory=lambda: self.fail('provider opened'), policy=policy)

    async def call(self, policy, tool, args):
        async with Client(self.server(policy)) as client:
            return await client.call_tool(tool, args)

    def assertRefused(self, result, tool, reason):
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual([(c.type, c.text) for c in result.content],
                         [('text', 'Error executing tool ' + tool + ': ' + reason)])

    async def test_disabled_read_capability_names_code_and_capability(self):
        result = await self.call(CONNECT_DEFAULT, 'read_history', {'target': 7, 'mode': 'latest', 'limit': 1})
        self.assertRefused(result, 'read_history', 'capability_disabled: read_history' + OWNER)

    async def test_disabled_send_names_send(self):
        result = await self.call(CONNECT_DEFAULT, 'prepare_text_send', {'recipient': 7, 'text': SENTINEL})
        self.assertRefused(result, 'prepare_text_send', 'capability_disabled: send' + OWNER)
        self.assertNotIn(SENTINEL, result.content[0].text)

    async def test_send_dependent_capability_names_every_missing_capability(self):
        args = {'recipient': 7, 'reply_to': {'chat_id': 7, 'message_id': 8}, 'text': SENTINEL}
        for enabled, missing in ((('read', 'artifacts'), 'reply_text_send, send'),
                                 (('read', 'send'), 'reply_text_send'),
                                 (('read', 'reply_text_send'), 'send')):
            with self.subTest(enabled=enabled):
                result = await self.call(RuntimePolicy(enabled_capabilities=enabled), 'prepare_reply_text_send', args)
                self.assertRefused(result, 'prepare_reply_text_send', 'capability_disabled: ' + missing + OWNER)
                self.assertNotIn(SENTINEL, result.content[0].text)
        result = await self.call(RuntimePolicy(enabled_capabilities=('send',)), 'send_prepared_forward',
                                 {'draft_id': 'draft_' + 'a' * 32, 'approved': True})
        self.assertRefused(result, 'send_prepared_forward', 'capability_disabled: forward_send' + OWNER)

    async def test_archive_and_edit_also_name_read(self):
        args = {'chat': 1, 'message_id': 1, 'new_text': 'x'}
        for enabled, missing in ((('send', 'edit_own_text'), 'read'), ((), 'edit_own_text, send, read'),
                                 (('read',), 'edit_own_text, send')):
            with self.subTest(enabled=enabled):
                result = await self.call(RuntimePolicy(enabled_capabilities=enabled), 'prepare_text_edit', args)
                self.assertRefused(result, 'prepare_text_edit', 'capability_disabled: ' + missing + OWNER)
        result = await self.call(RuntimePolicy(enabled_capabilities=('send', 'archive_chats')), 'prepare_chat_archive',
                                 {'chat': 1, 'target_list': 'archive'})
        self.assertRefused(result, 'prepare_chat_archive', 'capability_disabled: read' + OWNER)

    async def test_disabled_selection_names_retained_selections(self):
        policy = RuntimePolicy(enabled_capabilities=('read', 'read_messages'))
        result = await self.call(policy, 'read_messages', {'selection_id': 'selection_' + 'a' * 64})
        self.assertRefused(result, 'read_messages', 'capability_disabled: retained_selections' + OWNER)
        result = await self.call(RuntimePolicy(enabled_capabilities=('read',)), 'read_messages',
                                 {'selection_id': 'selection_' + 'a' * 64})
        self.assertRefused(result, 'read_messages', 'capability_disabled: read_messages, retained_selections' + OWNER)

    async def test_disabled_reason_is_redacted(self):
        result = await self.call(RuntimePolicy(enabled_capabilities=()), 'resolve_target', {'target': '@' + SENTINEL})
        self.assertRefused(result, 'resolve_target', 'capability_disabled: read' + OWNER)
        self.assertNotIn(SENTINEL, result.content[0].text)

    async def test_client_schema_mismatch_reason(self):
        async with Client(self.server(CONNECT_DEFAULT)) as client:
            await client.call_tool('_manifest', {'expected_schema_fingerprint': '0' * 64})
            result = await client.call_tool('get_message_context', {'anchor': {'chat_id': 7, 'message_id': 8}})
        self.assertRefused(result, 'get_message_context',
                           'client_schema_mismatch (refresh tools/list, then recheck _manifest)')

    async def test_config_stale_reason(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'policy'; root.mkdir(mode=0o700)
            path = root / 'runtime.toml'
            path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n'); path.chmod(0o600)
            async with Client(self.server(load_runtime_policy(path))) as client:
                path.write_text('config_version = 1\nenabled_capabilities = ["read", "send"]\n')
                result = await client.call_tool('resolve_target', {'target': '@' + SENTINEL})
        self.assertRefused(result, 'resolve_target', STALE)
        self.assertNotIn(SENTINEL, result.content[0].text)

    async def test_pinned_version_mismatch_reason(self):
        policy = RuntimePolicy(enabled_capabilities=('read',), expected_package_version='0.0.0')
        result = await self.call(policy, 'resolve_target', {'target': '@' + SENTINEL})
        self.assertRefused(result, 'resolve_target', STALE)

    async def test_upload_owner_refusal_stays_structured(self):
        manifest = {'file_name': 'report.txt', 'kind': 'document', 'size_bytes': 1, 'sha256': 'a' * 64}
        for tool, args in (('issue_local_upload_authority', {'manifest': manifest}),
                           ('cancel_local_upload_authority', {'result_ref': 'upload_result_' + 'a' * 64})):
            with self.subTest(tool=tool):
                result = await self.call(RuntimePolicy(enabled_capabilities=('read',)), tool, args)
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content,
                                 {'result': {'status': 'unavailable', 'detail': 'local upload is unavailable'}})

    def test_suffixes_come_from_the_closed_recovery_map(self):
        from telegram_search_mcp.next_actions import NEXT_ACTIONS
        self.assertTrue(OWNER.endswith('; next_action: ' + NEXT_ACTIONS['capability_disabled']))
        self.assertTrue(STALE.endswith('; next_action: ' + NEXT_ACTIONS['config_stale']))

    def test_reason_names_come_from_the_closed_capability_set(self):
        self.assertLessEqual(set(OPERATION_CAPABILITIES.values()) | {'send', 'retained_selections'}, KNOWN_CAPABILITIES)


if __name__ == '__main__':
    unittest.main()
