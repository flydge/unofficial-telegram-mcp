"""Criterion 4 (0.54): a reply refused for a disabled capability says so, naming only the missing names.

Production broker, draft and provider reducers over the synthetic raw transport of test_text_replies;
the SDK case runs the real server, proxy and broker over a Unix socket.
"""
from __future__ import annotations

import asyncio
import json
import threading
import unittest

from mcp import Client

from test_artifact_replies import ArtifactReplyFixture
from test_text_replies import ANCHOR, CLIENT, FOREIGN, ReplyFixture, target
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import CapabilityDisabled, CompatibilityError, missing_capabilities
from telegram_search_mcp.reply_drafts import CAPABILITY_DISABLED, UNAVAILABLE
from telegram_search_mcp.server import build_server

BASE = ('send', 'reply_text_send', 'reply_artifact_send')
STYLED_FAMILIES = ('reply_formatted_targets', 'reply_identity_targets', 'reply_lexical_targets')
SENTINEL = 'CAPABILITY_SOURCE_SENTINEL'


def entity(kind, offset, length):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': kind}}


def styled_source():
    # Bold selects the styled family; a mention adds identity, a hashtag lexical (v7 order).
    text = SENTINEL + ' @user #tag'
    return target(text, content={'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text, 'entities': [
        entity('textEntityTypeBold', 0, len(SENTINEL)),
        entity('textEntityTypeMention', len(SENTINEL) + 1, 5),
        entity('textEntityTypeHashtag', len(SENTINEL) + 7, 4)]}})


def disabled(*missing):
    return {'status': 'capability_disabled', 'reply': None, 'detail': CAPABILITY_DISABLED,
            'missing_capabilities': list(missing), 'next_action': 'enable_capability'}


def revised_disabled(*missing):
    return {'status': 'capability_disabled', 'previous_draft_id': None, **disabled(*missing)}


class CapabilityDisabledErrorTests(unittest.TestCase):
    def test_names_are_closed_distinct_and_keep_the_compatibility_code(self):
        error = CapabilityDisabled(('reply_url_targets', 'topic_text_send'))
        self.assertIsInstance(error, CompatibilityError)
        self.assertEqual((error.code, error.args, error.missing),
                         ('capability_disabled', ('capability_disabled',), ('reply_url_targets', 'topic_text_send')))
        for bad in ((), ('send', 'send'), ('not_a_capability',), ['send'], (SENTINEL,)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CapabilityDisabled(bad)

    def test_missing_names_follow_requirement_order_once_each(self):
        policy = RuntimePolicy(enabled_capabilities=('send', 'reply_formatted_targets'))
        self.assertEqual(missing_capabilities(policy, 'reply_text_send', 'send', 'reply_formatted_targets',
                                              'reply_identity_targets', 'reply_text_send', 'reply_lexical_targets'),
                         ('reply_text_send', 'reply_identity_targets', 'reply_lexical_targets'))
        self.assertEqual(missing_capabilities(policy, 'send'), ())
        self.assertLessEqual(set(STYLED_FAMILIES) | set(BASE), KNOWN_CAPABILITIES)


class TextReplyCapabilityTests(ReplyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.raw.source = styled_source()

    def policy(self, *extra):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, *extra))

    def prepare_raw(self, **extra):
        return self.dispatch('prepare_reply_text_send', {'recipient': 123, 'text': 'Final reply', 'reply_to': ANCHOR,
                                                          **extra})

    def test_prepare_names_exactly_the_missing_source_families_in_order(self):
        for enabled, missing in (((), STYLED_FAMILIES),
                                 (('reply_formatted_targets',), STYLED_FAMILIES[1:]),
                                 (('reply_formatted_targets', 'reply_lexical_targets'), ('reply_identity_targets',)),
                                 (('reply_identity_targets', 'reply_lexical_targets'), ('reply_formatted_targets',))):
            with self.subTest(enabled=enabled):
                self.policy(*enabled)
                result = self.prepare_raw()
                self.assertEqual(result, disabled(*missing))
                self.assertNotIn(SENTINEL, json.dumps(result))
        self.policy(*STYLED_FAMILIES)
        self.assertEqual(self.prepare_raw()['status'], 'prepared')
        self.assertEqual(self.sends(), [])

    def test_request_options_are_named_together_before_the_source_is_read(self):
        self.policy(*STYLED_FAMILIES)
        reads = len([r for r in self.raw.sent if r['@type'] == 'getMessage'])
        styled = {'outgoing_entities': [{'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}}]}
        self.assertEqual(self.prepare_raw(**styled), disabled('formatted_text_send'))
        self.assertEqual(self.prepare_raw(**styled, topic={'id': 5}), disabled('formatted_text_send', 'topic_text_send'))
        self.assertEqual(len([r for r in self.raw.sent if r['@type'] == 'getMessage']), reads)

    def test_get_update_and_refresh_name_missing_capabilities_and_send_stays_expired(self):
        self.policy(*STYLED_FAMILIES)
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.policy('reply_formatted_targets')
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}), disabled(*STYLED_FAMILIES[1:]))
        for operation, payload in (('update_reply_draft', {'draft_id': did, 'text': 'Other'}),
                                   ('refresh_reply_draft', {'draft_id': did})):
            with self.subTest(operation=operation):
                self.assertEqual(self.dispatch(operation, payload), revised_disabled(*STYLED_FAMILIES[1:]))
        # Spec decision: a disabled capability at send time keeps the existing expired result.
        sent = self.send(did)
        self.assertEqual(sent['status'], 'expired')
        self.assertNotIn('missing_capabilities', sent)
        self.assertEqual((self.sends(), self.approvals), ([], []))

    def test_a_refreshed_source_names_only_what_the_new_source_needs(self):
        self.raw.source = target()
        self.policy()
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.raw.source = styled_source()
        self.assertEqual(self.dispatch('refresh_reply_draft', {'draft_id': did}), revised_disabled(*STYLED_FAMILIES))

    def test_non_capability_refusals_stay_unavailable_with_their_fixed_detail(self):
        self.policy(*STYLED_FAMILIES)
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.policy()
        plain = {'status': 'unavailable', 'reply': None, 'detail': UNAVAILABLE}
        # Foreign owner, unknown draft and a recipient that differs from the anchor reveal nothing.
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}, client=FOREIGN), plain)
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': 'draft_' + 'f' * 32}), plain)
        self.assertEqual(self.dispatch('prepare_reply_text_send', {'recipient': 124, 'text': 'x', 'reply_to': ANCHOR}),
                         plain)
        self.raw.account = 8  # account switch
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}), plain)


class ArtifactReplyCapabilityTests(ArtifactReplyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.raw.source = styled_source()

    def prepare_raw(self):
        return self.dispatch('prepare_reply_artifact_send', dict(artifact_id=self.artifact.artifact_id, recipient=123,
            display_name='evidence.txt', mime_type='text/plain', caption='Caption', kind='document', reply_to=ANCHOR))

    def test_artifact_reply_tools_name_the_missing_source_families(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_lexical_targets'))
        self.assertEqual(self.prepare_raw(), disabled('reply_formatted_targets', 'reply_identity_targets'))
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, *STYLED_FAMILIES))
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_formatted_targets'))
        self.assertEqual(self.dispatch('get_reply_artifact_draft', {'draft_id': did}), disabled(*STYLED_FAMILIES[1:]))
        for operation, payload in (('update_reply_artifact_draft', {'draft_id': did, 'caption': 'Other'}),
                                   ('refresh_reply_artifact_draft', {'draft_id': did})):
            with self.subTest(operation=operation):
                self.assertEqual(self.dispatch(operation, payload), revised_disabled(*STYLED_FAMILIES[1:]))
        self.assertEqual(self.dispatch('send_prepared_artifact', {'draft_id': did, 'approved': True})['status'], 'expired')
        self.assertEqual(self.sends(), [])


class ReplyCapabilitySDKTests(ReplyFixture, unittest.TestCase):
    def test_official_client_sees_the_typed_result_through_proxy_and_broker(self):
        policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_formatted_targets'))
        self.raw.source = styled_source()
        broker = Broker(socket_path=self.root / 'sdk.sock', artifact_store=ArtifactStore(cache_dir=self.root / 'sdk'),
                        client_factory=lambda: self.client, policy=policy, approval_prompt=lambda **kw: True)
        self.addCleanup(broker.shutdown)
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        proxy = BrokerClient(socket_path=self.root / 'sdk.sock', policy=policy, restart_callback=lambda: None)
        self.addCleanup(proxy.close)

        async def call():
            async with Client(build_server(service_factory=lambda: proxy, policy=policy)) as client:
                return await client.call_tool('prepare_reply_text_send',
                                              {'recipient': 123, 'text': 'Final reply', 'reply_to': ANCHOR})
        try:
            result = asyncio.run(call())
        finally:
            proxy.close()
            broker.shutdown()
            thread.join(3)
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {'result': disabled(*STYLED_FAMILIES[1:])})
        self.assertNotIn(SENTINEL, json.dumps(result.structured_content))
        self.assertEqual(self.sends(), [])


if __name__ == '__main__':
    unittest.main()
