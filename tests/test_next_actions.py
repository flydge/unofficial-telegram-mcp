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
        # Every spec condition except the guard-only ones is reachable from a structured response.
        reachable = {condition for model in carriers.values()
                     for condition in (*model.NEXT_ACTION_STATUSES.values(), *model.NEXT_ACTION_STOP_REASONS.values())}
        self.assertEqual(reachable, set(NEXT_ACTIONS) - {'config_stale', 'capability_disabled'})
        self.assertGreaterEqual(len(carriers), 28)

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


if __name__ == '__main__':
    unittest.main()
