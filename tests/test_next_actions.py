"""Criterion 5 (0.54): next_action values come from one closed map, derived from closed status fields."""
from __future__ import annotations

import asyncio
import tempfile
import typing
import unittest
from pathlib import Path

from mcp import Client
from pydantic import BaseModel, ValidationError

from telegram_search_mcp import next_actions
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.next_actions import (NEXT_ACTIONS, STATUS_CONDITIONS, STOP_REASON_CONDITIONS, NextAction,
                                              NextActionHint, guard_suffix)
from telegram_search_mcp.schemas import ReadHistoryRequest
from telegram_search_mcp.server import build_server

VALUES = ('check_connection', 'connect_account', 'update_installation', 'enable_capability', 'search_again',
          'restart_listing', 'check_send_status')


def literal_values(annotation) -> tuple:
    if typing.get_origin(annotation) is typing.Literal:
        return typing.get_args(annotation)
    return tuple(value for arg in typing.get_args(annotation) for value in literal_values(arg))


def response_models() -> dict[str, list[type[BaseModel]]]:
    """Every model a registered tool can return at the top level, by tool name."""
    found: dict[str, list[type[BaseModel]]] = {}

    def walk(annotation, into):
        if typing.get_origin(annotation) is not None:
            for arg in typing.get_args(annotation):
                walk(arg, into)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation.__name__.endswith('Output') and set(annotation.model_fields) == {'result'}:
                walk(annotation.model_fields['result'].annotation, into)
            else:
                into.append(annotation)
    for tool in build_server(policy=RuntimePolicy())._tool_manager.list_tools():
        found[tool.name] = []
        walk(tool.fn_metadata.output_model, found[tool.name])
    return found


class ClosedMapTests(unittest.TestCase):
    def test_one_closed_map_with_the_seven_spec_values(self):
        self.assertEqual(typing.get_args(NextAction), VALUES)
        self.assertEqual(sorted(NEXT_ACTIONS.values()), sorted(VALUES))  # each value names one condition
        self.assertLessEqual(set(STATUS_CONDITIONS.values()) | set(STOP_REASON_CONDITIONS.values()), set(NEXT_ACTIONS))
        with self.assertRaises(TypeError):
            NEXT_ACTIONS['broker_unavailable'] = 'restart_listing'

    def test_every_carrying_response_model_is_closed_and_total(self):
        carriers = {}
        for tool, models in response_models().items():
            for model in models:
                statuses = set(literal_values(model.model_fields['status'].annotation)) if 'status' in model.model_fields else set()
                reasons = (set(literal_values(model.model_fields['stop_reason'].annotation))
                           if 'stop_reason' in model.model_fields else set())
                needs = bool(statuses & set(STATUS_CONDITIONS) or reasons & set(STOP_REASON_CONDITIONS))
                carries = 'next_action' in model.model_fields
                with self.subTest(tool=tool, model=model.__name__):
                    self.assertEqual(carries, needs, 'a status naming a condition must carry next_action, and only then')
                    if not carries:
                        continue
                    carriers[model.__name__] = model
                    self.assertTrue(issubclass(model, NextActionHint))
                    self.assertEqual(model.model_fields['next_action'].annotation, NextAction | None)
                    self.assertEqual(list(model.model_fields)[-1], 'next_action')
                    self.assertLessEqual(set(model.NEXT_ACTION_STATUSES), statuses)
                    self.assertLessEqual(set(model.NEXT_ACTION_STOP_REASONS), reasons)
                    # Total: every status (and stop reason) with a shared meaning maps, to its one condition.
                    for status in statuses & set(STATUS_CONDITIONS):
                        self.assertEqual(model.NEXT_ACTION_STATUSES[status], STATUS_CONDITIONS[status])
                    for reason in reasons & set(STOP_REASON_CONDITIONS):
                        self.assertEqual(model.NEXT_ACTION_STOP_REASONS[reason], STOP_REASON_CONDITIONS[reason])
                    for status in statuses:
                        for reason in reasons or {None}:
                            shell = model.model_construct(status=status, stop_reason=reason)
                            condition = shell.next_action_condition()
                            self.assertTrue(condition is None or condition in NEXT_ACTIONS)
        # Every spec condition except the guard-only config_stale is reachable from a structured response.
        reachable = {condition for model in carriers.values()
                     for condition in (*model.NEXT_ACTION_STATUSES.values(), *model.NEXT_ACTION_STOP_REASONS.values())}
        self.assertEqual(reachable, set(NEXT_ACTIONS) - {'config_stale'})
        self.assertGreaterEqual(len(carriers), 30)

    def test_only_discovery_maps_its_own_expired_status(self):
        expired = {name for name, models in response_models().items() for model in models
                   if 'next_action' in model.model_fields and 'expired' in model.NEXT_ACTION_STATUSES}
        self.assertEqual(expired, {'discover_targets'})


class DerivationTests(unittest.TestCase):
    def test_the_value_follows_the_status_and_a_contradiction_fails(self):
        from telegram_search_mcp.history_reader import terminal_history
        from telegram_search_mcp.schemas import ReadHistoryResponse, SendPreparedTextResponse
        from telegram_search_mcp.selection_refs import SelectionRefusal
        self.assertEqual(SelectionRefusal().model_dump()['next_action'], 'search_again')
        for (status, reason), expected in ((('invalid_cursor', 'invalid_cursor'), 'restart_listing'),
                                           (('blocked', 'authorization_unavailable'), 'connect_account'),
                                           (('error', 'broker_unavailable'), 'check_connection'),
                                           (('error', 'provider_error'), None)):
            with self.subTest(status=status, reason=reason):
                response = terminal_history(status, reason)
                self.assertEqual(response.next_action, expected)
                dumped = response.model_dump(mode='json')
                self.assertEqual('next_action' in dumped, expected is not None)
                self.assertEqual(ReadHistoryResponse.model_validate(dumped), response)
        unknown = SendPreparedTextResponse(status='outcome_unknown', draft_id='draft_' + 'a' * 32, detail='unconfirmed')
        self.assertEqual(unknown.next_action, 'check_send_status')
        sent = SendPreparedTextResponse(status='sent', draft_id='draft_' + 'a' * 32, message_id=5, detail='sent')
        self.assertNotIn('next_action', sent.model_dump())
        for bad in ({'status': 'sent', 'next_action': 'check_send_status'},
                    {'status': 'outcome_unknown', 'next_action': 'restart_listing'},
                    {'status': 'outcome_unknown', 'next_action': 'resend'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                SendPreparedTextResponse(draft_id='draft_' + 'a' * 32, detail='x', **bad)

    def test_proxy_broker_loss_reads_check_connection(self):
        policy = RuntimePolicy(enabled_capabilities=('read', 'read_history'))
        with tempfile.TemporaryDirectory() as root:
            proxy = BrokerClient(socket_path=Path(root) / 'absent.sock', policy=policy, restart_callback=lambda: None)
            try:
                response = proxy.read_history(ReadHistoryRequest(target=7, mode='latest', limit=1))
            finally:
                proxy.close()
        self.assertEqual((response.status, response.stop_reason, response.next_action),
                         ('error', 'broker_unavailable', 'check_connection'))

    def test_read_messages_broker_loss_reads_check_connection(self):
        from telegram_search_mcp.message_reader import terminal_read
        from telegram_search_mcp.schemas import ReadMessagesRequest
        request = ReadMessagesRequest(anchors=[{'chat_id': 7, 'message_id': 1}, {'chat_id': 7, 'message_id': 2}])
        self.assertEqual(terminal_read(request, 'error', 'broker_unavailable').next_action, 'check_connection')
        self.assertEqual(terminal_read(request, 'blocked', 'authorization_unavailable').next_action, 'connect_account')
        self.assertIsNone(terminal_read(request, 'error', 'provider_error').next_action)


class GuardAndManifestTests(unittest.TestCase):
    def test_guard_suffixes_are_fixed(self):
        self.assertEqual(guard_suffix('capability_disabled'), '; next_action: enable_capability')
        self.assertEqual(guard_suffix('config_stale'), '; next_action: update_installation')
        self.assertEqual(guard_suffix('client_schema_mismatch'), '')
        self.assertEqual(set(next_actions.GUARD_NEXT_ACTIONS), {'capability_disabled', 'config_stale'})

    def test_manifest_names_the_step_for_a_lost_broker_and_a_stale_policy(self):
        class Lost:
            def diagnostics(self):
                return {'status': 'broker_unavailable', 'broker': None, 'detail': 'local broker unavailable'}

            def close(self):
                pass

        async def manifest(policy, service=None, check_broker=True):
            server = build_server(service_factory=(lambda: service) if service else (lambda: self.fail('service')),
                                  policy=policy)
            async with Client(server) as client:
                return (await client.call_tool('_manifest', {'check_broker': check_broker})).structured_content
        compatibility = asyncio.run(manifest(RuntimePolicy(enabled_capabilities=('read',)), Lost()))['compatibility']
        self.assertEqual(compatibility, {'status': 'broker_unavailable', 'broker': None,
                                         'detail': 'local broker unavailable', 'next_action': 'check_connection'})
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'policy'
            root.mkdir(mode=0o700)
            path = root / 'runtime.toml'
            path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n')
            path.chmod(0o600)
            policy = load_runtime_policy(path)
            path.write_text('config_version = 1\nenabled_capabilities = ["read", "send"]\n')
            compatibility = asyncio.run(manifest(policy))['compatibility']
        self.assertEqual((compatibility['status'], compatibility['next_action']), ('config_stale', 'update_installation'))
        local = asyncio.run(manifest(RuntimePolicy(enabled_capabilities=('read',)), check_broker=False))
        self.assertEqual(local['compatibility']['status'], 'not_checked')
        self.assertNotIn('next_action', local['compatibility'])


SELECTION = 'selection_' + 'b' * 64


class SelectionRefusalReachesClientsTests(unittest.TestCase):
    """The documented SelectionRefusal, with next_action search_again, reaches an official SDK client from every
    attachment reader (read_attachment_page lost it to a stale published output schema before 0.54 fixes)."""

    def test_every_attachment_reader_returns_the_refusal(self):
        from telegram_search_mcp.selection_refs import SelectionRefusal
        from tool_listing import FULL_POLICY

        class Service:
            def close(self):
                pass

            def __getattr__(self, name):
                return lambda request: SelectionRefusal()

        async def call(name):
            async with Client(build_server(service_factory=Service, policy=FULL_POLICY)) as client:
                return await client.call_tool(name, {'selection_id': SELECTION})
        for name in ('read_attachment', 'analyze_media', 'read_attachment_page'):
            with self.subTest(tool=name):
                result = asyncio.run(call(name))
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(result.structured_content, {
                    'status': 'selection_unavailable', 'detail': 'Selection is unavailable; select the source again.',
                    'next_action': 'search_again'})

    def test_the_published_output_schema_admits_the_refusal(self):
        from telegram_search_mcp.selection_refs import SelectionRefusal
        from tool_listing import FULL_POLICY
        import jsonschema
        server = build_server(service_factory=lambda: None, policy=FULL_POLICY)
        for name in ('read_attachment', 'analyze_media', 'read_attachment_page'):
            with self.subTest(tool=name):
                jsonschema.validate(SelectionRefusal().model_dump(mode='json'),
                                    server._tool_manager.get_tool(name).output_schema)


# Tools whose response to a lost broker carries next_action check_connection (docs/reference.md, Recovery hints).
ANCHOR = {'chat_id': 7, 'message_id': 8}
CARRY_ON_BROKER_LOSS = {
    'read_history': {'target': 7, 'mode': 'latest'},
    'read_messages': {'anchors': [ANCHOR]},
    'read_reply_chain': {'anchor': ANCHOR},
    'read_topic_history': {'target': 7, 'topic': {'id': 1}, 'mode': 'latest'},
    'list_chats': {'scope': 'main'},
    'list_topics': {'target': 7},
    'search_messages': {'target': 7, 'query': 'x', 'mode': 'latest'},
    'search_chats': {'targets': [7], 'query': 'x', 'mode': 'latest'},
}
# The core tools named by the 0.54 finding: a plain error or a content-free unavailable, never a hint.
NO_HINT_ON_BROKER_LOSS = {
    'resolve_target': {'target': 'me'},
    'discover_targets': {'hypotheses': ['alpha', 'beta'], 'scope': 'both'},
    'search_correspondence': {'target': 7, 'query': {'text': 'x'}},
    'get_message_context': {'anchor': ANCHOR},
    'get_attachment': {'anchor': ANCHOR},
    'verify_target': {'target': 7},
    'prepare_text_send': {'recipient': 7, 'text': 'hi'},
    'prepare_reply_text_send': {'recipient': 7, 'text': 'hi', 'reply_to': ANCHOR},
    'list_drafts': {},
}


class BrokerLossHintTests(unittest.TestCase):
    """Exactly which tools carry check_connection when the broker is gone, through a real BrokerClient pointed at
    an absent socket and the official SDK client; docs/reference.md names the same set."""

    def test_the_documented_tools_carry_check_connection_and_the_others_carry_none(self):
        from tool_listing import FULL_POLICY

        async def run():
            with tempfile.TemporaryDirectory() as parent:
                def factory():
                    return BrokerClient(socket_path=Path(parent) / 'absent.sock', policy=FULL_POLICY,
                                        restart_callback=lambda: None, retry_backoff_seconds=0, request_timeout=5)
                async with Client(build_server(service_factory=factory, policy=FULL_POLICY)) as client:
                    return {name: await client.call_tool(name, arguments) for name, arguments in
                            {**CARRY_ON_BROKER_LOSS, **NO_HINT_ON_BROKER_LOSS}.items()}
        results = asyncio.run(run())
        for name, result in results.items():
            with self.subTest(tool=name):
                self.assertFalse(result.is_error, result.content)
                structured = result.structured_content
                structured = structured.get('result', structured) if set(structured) == {'result'} else structured
                self.assertIn(structured['status'], ('error', 'unavailable'))
                self.assertEqual(structured.get('next_action'),
                                 'check_connection' if name in CARRY_ON_BROKER_LOSS else None)

    def test_the_reference_names_exactly_that_set(self):
        import re
        text = (Path(__file__).parents[1] / 'docs/reference.md').read_text()
        sentence = re.search(r'On a lost broker exactly these tools carry `check_connection`: (.*?)\.', text, re.S)
        self.assertIsNotNone(sentence)
        self.assertEqual(set(re.findall(r'`([a-z_]+)`', sentence.group(1))), set(CARRY_ON_BROKER_LOSS) | {'_manifest'})
        for name in NO_HINT_ON_BROKER_LOSS:
            with self.subTest(tool=name):
                self.assertIn(f'`{name}`', text.split('On a lost broker exactly these tools', 1)[1].split('\n## ', 1)[0])


if __name__ == '__main__':
    unittest.main()
