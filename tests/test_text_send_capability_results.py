"""A text send refused for a disabled request option says so, as the reply tools do (final acceptance, audit item).

A styled or topic `prepare_text_send` under a policy without `formatted_text_send` or `topic_text_send` answers
`capability_disabled` naming only the missing capabilities, in the reply tools' order (`send`, the styles, the topic),
with `next_action: enable_capability`, before Telegram is read: at the broker, at the proxy (its own policy check
included) and through the official SDK, whose listed output schema admits the result. The guard's refusal of the tool
itself (no `send`) keeps its fixed text. Production broker, proxy and server over the synthetic raw transport.
"""
from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from mcp import Client
from pydantic import ValidationError

from test_formatted_text_lifecycle import STYLES, TEXT, FormattedHarness
from test_topic_text_reply import CAPS, CHAT, TOPIC
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.tdjson import TDLibError
from telegram_search_mcp.schemas import PrepareTextSendRequest, PrepareTextSendResponse
from telegram_search_mcp.server import build_server

DETAIL = 'text draft needs capabilities the owner has not enabled: missing_capabilities'


def disabled(*missing):
    return {'status': 'capability_disabled', 'missing_capabilities': list(missing), 'detail': DETAIL,
            'next_action': 'enable_capability'}


def refusal(result):
    """The refusal's own fields; every draft field stays empty."""
    for key in ('draft_id', 'recipient', 'recipient_title', 'text', 'sha256', 'expires_at', 'account_id', 'topic',
                'preview_sha256'):
        if result.get(key) is not None:
            raise AssertionError(f'a refusal carries {key}')
    if 'outgoing_entities' in result:
        raise AssertionError('a refusal carries outgoing_entities')
    return {key: result.get(key) for key in ('status', 'missing_capabilities', 'detail', 'next_action')}


def request(*, styled=True, topic=False):
    return {'recipient': CHAT, 'text': TEXT, **({'outgoing_entities': copy.deepcopy(STYLES)} if styled else {}),
            **({'topic': copy.deepcopy(TOPIC)} if topic else {})}


class ResponseModelTests(unittest.TestCase):
    def test_the_refusal_is_closed_and_derives_its_next_action(self):
        value = PrepareTextSendResponse(status='capability_disabled', missing_capabilities=['formatted_text_send'],
                                        detail=DETAIL)
        self.assertEqual(value.model_dump(mode='json', exclude_none=True),
                         {**disabled('formatted_text_send'), 'approval_required': True})
        for bad in (dict(missing_capabilities=None), dict(missing_capabilities=[]),
                    dict(missing_capabilities=['formatted_text_send', 'formatted_text_send']),
                    dict(missing_capabilities=['not_a_capability']), dict(detail='text draft could not be prepared safely'),
                    dict(draft_id='draft_' + 'a' * 32), dict(recipient=CHAT), dict(text=TEXT),
                    dict(next_action='check_connection')):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PrepareTextSendResponse.model_validate({'status': 'capability_disabled',
                                                        'missing_capabilities': ['formatted_text_send'],
                                                        'detail': DETAIL, **bad})
        for status in ('prepared', 'blocked', 'error'):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                PrepareTextSendResponse(status=status, missing_capabilities=['send'], detail='text draft could not be '
                                        'prepared safely')


class BrokerTests(unittest.TestCase):
    def reads(self, h):
        return [r['@type'] for r in h.raw.sent]

    def test_the_request_options_are_named_together_before_telegram_is_read(self):
        for capabilities, payload, missing in (
                (CAPS, request(), ('formatted_text_send',)),
                (CAPS, request(topic=True), ('formatted_text_send',)),
                (tuple(c for c in CAPS if c != 'topic_text_send'), request(topic=True),
                 ('formatted_text_send', 'topic_text_send')),
                (tuple(c for c in (*CAPS, 'formatted_text_send') if c != 'topic_text_send'),
                 request(styled=False, topic=True), ('topic_text_send',)),
                (tuple(c for c in (*CAPS, 'formatted_text_send') if c != 'topic_text_send'), request(topic=True),
                 ('topic_text_send',))):
            with self.subTest(capabilities=capabilities, payload=sorted(payload)), \
                    FormattedHarness('topic' if 'topic' in payload else 'plain') as h:
                h.broker._policy = RuntimePolicy(enabled_capabilities=capabilities)
                result = h.dispatch('prepare_text_send', payload)
                self.assertEqual(refusal(result), disabled(*missing))
                self.assertEqual(self.reads(h), [])
                self.assertEqual((h.approvals, h.claim_count, h.register_count), ([], 0, 0))
                self.assertEqual(h.dispatch('list_drafts', {})['drafts'], [])

    def test_the_tool_itself_without_send_stays_the_dispatch_guards_refusal(self):
        with FormattedHarness('plain') as h:
            h.broker._policy = RuntimePolicy(enabled_capabilities=('read',))
            with self.assertRaises(CompatibilityError) as caught:
                h.dispatch('prepare_text_send', request(topic=True))
            self.assertEqual(caught.exception.code, 'capability_disabled')
            self.assertEqual(h.raw.sent, [])

    def test_with_every_option_enabled_the_same_requests_prepare(self):
        for payload in (request(), request(topic=True)):
            with self.subTest(topic='topic' in payload), FormattedHarness('topic' if 'topic' in payload else 'plain') as h:
                self.assertEqual(h.dispatch('prepare_text_send', payload)['status'], 'prepared')

    def test_a_policy_change_after_the_first_check_still_answers_the_typed_refusal(self):
        with FormattedHarness('plain') as h:
            checks = []
            original = h.broker._require_formatting_capability

            def revoked():
                checks.append(True)
                h.policy_without('formatted_text_send')
                return original()
            with patch.object(h.broker, '_require_formatting_capability', revoked):
                result = h.dispatch('prepare_text_send', request())
            self.assertEqual(checks, [True])
            self.assertEqual(refusal(result), disabled('formatted_text_send'))
            self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

    def test_a_provider_failure_keeps_its_error(self):
        with FormattedHarness('plain') as h:
            with patch.object(h.provider, 'resolve_target', side_effect=TDLibError('synthetic private detail')):
                result = h.dispatch('prepare_text_send', request())
            self.assertEqual((result['status'], result['detail']), ('error', 'text draft could not be prepared safely'))
            self.assertNotIn('missing_capabilities', result)
            self.assertNotIn('next_action', result)


class ProxyTests(unittest.TestCase):
    def test_the_proxy_answers_its_own_policy_check_with_the_same_typed_refusal(self):
        for capabilities, payload, missing in (
                (CAPS, request(), ('formatted_text_send',)),
                (tuple(c for c in CAPS if c != 'topic_text_send'), request(styled=False, topic=True),
                 ('topic_text_send',)),
                (tuple(c for c in CAPS if c != 'topic_text_send'), request(topic=True),
                 ('formatted_text_send', 'topic_text_send'))):
            with self.subTest(missing=missing):
                proxy = BrokerClient(socket_path=Path('/nonexistent/unused.sock'),
                                     policy=RuntimePolicy(enabled_capabilities=capabilities), restart_callback=lambda: None)
                self.addCleanup(proxy.close)
                with patch.object(proxy, '_request') as sent:
                    result = proxy.prepare_text_send(PrepareTextSendRequest.model_validate(payload))
                    sent.assert_not_called()
                self.assertEqual(refusal(result.model_dump(mode='json')), disabled(*missing))

    def test_the_proxy_passes_the_brokers_typed_refusal_through(self):
        with FormattedHarness('plain') as h:
            proxy = h.start_ipc()
            h.policy_without('formatted_text_send')  # The broker's policy only: the proxy's still allows styles.
            result = proxy.prepare_text_send(PrepareTextSendRequest.model_validate(request()))
            self.assertEqual(refusal(result.model_dump(mode='json')), disabled('formatted_text_send'))
            self.assertEqual(h.raw.sent, [])


class SDKTests(unittest.TestCase):
    def call(self, h, policy, payload):
        proxy = h.start_ipc()

        async def run():
            async with Client(build_server(service_factory=lambda: proxy, policy=policy)) as consumer:
                tools = {t.name: t for t in (await consumer.list_tools()).tools}
                return tools, await consumer.call_tool('prepare_text_send', payload)
        return asyncio.run(run())

    def test_the_official_client_sees_the_typed_refusal_valid_under_the_listed_output_schema(self):
        for capabilities, payload, missing in (
                (CAPS, request(), ('formatted_text_send',)),
                (tuple(c for c in CAPS if c != 'topic_text_send'), request(topic=True),
                 ('formatted_text_send', 'topic_text_send'))):
            with self.subTest(missing=missing), FormattedHarness('topic' if 'topic' in payload else 'plain') as h:
                policy = RuntimePolicy(enabled_capabilities=capabilities)
                h.broker._policy = policy
                tools, result = self.call(h, policy, payload)
                self.assertFalse(result.is_error, repr(result))
                data = result.structured_content
                Draft202012Validator(tools['prepare_text_send'].output_schema).validate(data)
                self.assertEqual(refusal(data), disabled(*missing))
                self.assertEqual(h.raw.sent, [])

    def test_the_guard_still_refuses_the_tool_itself_with_its_fixed_text(self):
        with FormattedHarness('plain') as h:
            policy = RuntimePolicy(enabled_capabilities=('read',))
            h.broker._policy = policy
            _, result = self.call(h, policy, request())
            self.assertTrue(result.is_error)
            self.assertEqual([block.text for block in result.content],
                             ['Error executing tool prepare_text_send: capability_disabled: send (owner enables '
                              'profile send); next_action: enable_capability'])
            self.assertEqual(h.raw.sent, [])


if __name__ == '__main__':
    unittest.main()
