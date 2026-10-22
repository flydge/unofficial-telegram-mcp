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


def lexical_source():
    # A hashtag alone: lexical text, which needs reply_formatted_targets and reply_lexical_targets (v5).
    text = SENTINEL + ' #tag'
    return target(text, content={'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text, 'entities': [
        entity('textEntityTypeHashtag', len(SENTINEL) + 1, 4)]}})


STYLED_REQUEST = {'outgoing_entities': [{'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}}]}
LEXICAL_FAMILIES = ('reply_formatted_targets', 'reply_lexical_targets')


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

    def reads(self):
        return sum(r['@type'] == 'getMessage' for r in self.raw.sent)

    def test_a_refresh_never_names_the_stored_sources_families(self):
        # Old source styled, new source plain: the stored source is replaced, so only the new one is asked.
        self.policy(*STYLED_FAMILIES)
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.policy()
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}), disabled(*STYLED_FAMILIES))
        # A new source that needs only some families names exactly those, after reading it once.
        self.raw.source = lexical_source()
        reads = self.reads()
        self.assertEqual(self.dispatch('refresh_reply_draft', {'draft_id': did}), revised_disabled(*LEXICAL_FAMILIES))
        self.assertEqual(self.reads(), reads + 1)
        self.policy(*STYLED_FAMILIES)
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did})['status'], 'pending')
        self.policy()
        self.raw.source = target()
        refreshed = self.dispatch('refresh_reply_draft', {'draft_id': did})
        self.assertEqual((refreshed['status'], refreshed['previous_draft_id']), ('revised', did))
        self.assertNotIn('missing_capabilities', refreshed)
        self.assertEqual(refreshed['reply']['reply_target']['text'], '[untrusted Telegram evidence] Source full text')
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': refreshed['reply']['draft']['draft_id']})['status'],
                         'pending')
        self.policy(*STYLED_FAMILIES)
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did})['status'], 'unavailable')
        self.assertEqual((self.sends(), self.approvals), ([], []))

    def test_prepare_get_update_and_refresh_name_the_same_capabilities_on_one_policy(self):
        # The documented order: the tool, send, the request's or draft's options, then the source's families.
        self.policy('formatted_text_send', *STYLED_FAMILIES)
        did = self.prepare_raw(**STYLED_REQUEST)['reply']['draft']['draft_id']
        for enabled, missing in ((('reply_formatted_targets',), ('formatted_text_send',)),
                                 ((), ('formatted_text_send',)),
                                 (('formatted_text_send', 'reply_formatted_targets'), STYLED_FAMILIES[1:]),
                                 (('formatted_text_send', 'reply_lexical_targets'),
                                  ('reply_formatted_targets', 'reply_identity_targets'))):
            with self.subTest(enabled=enabled):
                self.policy(*enabled)
                self.assertEqual(self.prepare_raw(**STYLED_REQUEST), disabled(*missing))
                self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}), disabled(*missing))
                self.assertEqual(self.dispatch('update_reply_draft', {'draft_id': did, 'text': 'Final reply',
                                                                      **STYLED_REQUEST}), revised_disabled(*missing))
                self.assertEqual(self.dispatch('update_reply_draft', {'draft_id': did, 'text': 'Final reply'}),
                                 revised_disabled(*missing))
                self.assertEqual(self.dispatch('refresh_reply_draft', {'draft_id': did}), revised_disabled(*missing))
        self.policy('formatted_text_send', *STYLED_FAMILIES)
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did})['status'], 'pending')
        self.assertEqual((self.sends(), self.approvals), ([], []))

    def test_list_status_and_cancel_stay_send_only_for_a_plain_reply_draft(self):
        # Spec decision (0.54): only get, update and send ask for the stored source's families.
        self.policy(*STYLED_FAMILIES)
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.policy()  # every source family off, send and the reply tool on
        listed = self.dispatch('list_drafts', {})
        self.assertEqual((listed['status'], [d['draft_id'] for d in listed['drafts']]), ('listed', [did]))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_pending')
        self.assertEqual(self.dispatch('get_reply_draft', {'draft_id': did}), disabled(*STYLED_FAMILIES))
        self.assertEqual(self.send(did)['status'], 'expired')
        cancelled = self.dispatch('cancel_draft', {'draft_id': did})
        self.assertEqual((cancelled['status'], cancelled['draft_id']), ('cancelled', did))
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        self.assertEqual((self.sends(), self.approvals), ([], []))

    def test_formatting_asks_for_send_before_its_option(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=('reply_text_send',))
        with self.assertRaises(CapabilityDisabled) as caught:
            self.broker._require_formatting_capability()
        self.assertEqual(caught.exception.missing, ('send', 'formatted_text_send'))

    def test_a_disabled_requested_option_wins_before_the_draft_or_recipient_is_checked(self):
        self.policy(*STYLED_FAMILIES)
        did = self.prepare_raw()['reply']['draft']['draft_id']
        reads = self.reads()
        option = revised_disabled('formatted_text_send')

        def update(draft_id, client=CLIENT, **extra):
            return self.dispatch('update_reply_draft', {'draft_id': draft_id, 'text': 'Final reply', **extra}, client=client)
        # Identical for an existing, an unknown and a foreign draft, so the result reveals nothing about it.
        for draft_id, client in ((did, CLIENT), ('draft_' + 'f' * 32, CLIENT), (did, FOREIGN)):
            with self.subTest(draft_id=draft_id, client=client):
                self.assertEqual(update(draft_id, client, **STYLED_REQUEST), option)
        self.assertEqual(self.dispatch('prepare_reply_text_send', {'recipient': 124, 'text': 'Final reply',
                                                                   'reply_to': ANCHOR, **STYLED_REQUEST}),
                         disabled('formatted_text_send'))
        self.assertEqual(self.reads(), reads)
        self.raw.account = 8  # account switch: the option is still named first
        self.assertEqual(update(did, **STYLED_REQUEST), option)
        self.raw.account = 7
        # Without a disabled option the same requests keep their fixed unavailable result.
        plain = {'status': 'unavailable', 'previous_draft_id': None, 'reply': None, 'detail': UNAVAILABLE}
        self.assertEqual(update('draft_' + 'f' * 32), plain)
        self.assertEqual(update(did, FOREIGN), plain)
        self.assertEqual(self.dispatch('prepare_reply_text_send', {'recipient': 124, 'text': 'x', 'reply_to': ANCHOR}),
                         {'status': 'unavailable', 'reply': None, 'detail': UNAVAILABLE})

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

    def test_an_artifact_refresh_names_only_what_the_newly_read_source_needs(self):
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, *STYLED_FAMILIES))
        did = self.prepare_raw()['reply']['draft']['draft_id']
        self.broker._policy = RuntimePolicy(enabled_capabilities=BASE)
        self.assertEqual(self.dispatch('get_reply_artifact_draft', {'draft_id': did}), disabled(*STYLED_FAMILIES))
        self.raw.source = lexical_source()
        self.assertEqual(self.prepare_raw(), disabled(*LEXICAL_FAMILIES))
        self.assertEqual(self.dispatch('refresh_reply_artifact_draft', {'draft_id': did}), revised_disabled(*LEXICAL_FAMILIES))
        self.raw.source = target()
        refreshed = self.dispatch('refresh_reply_artifact_draft', {'draft_id': did})
        self.assertEqual((refreshed['status'], refreshed['previous_draft_id']), ('revised', did))
        self.assertEqual(refreshed['reply']['reply_target']['text'], '[untrusted Telegram evidence] Source full text')
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
