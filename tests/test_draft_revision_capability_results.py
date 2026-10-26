"""update_draft names a disabled style option, as prepare_text_send names its own (final acceptance, closing pass).

`update_draft` with `outgoing_entities` under a policy without the style capability (`formatted_text_send` for text,
`formatted_caption_send` for a caption) answers `capability_disabled` naming only the missing capabilities, with the
fixed detail and `next_action: enable_capability`, before the draft is looked up (the same for any draft ID) and
before Telegram is read: at the broker, through the proxy and through the official SDK, whose listed output schema
admits the result. It answered `unavailable` / `draft is unavailable` before, although the draft stays pending. A
stored draft's own state (a refresh of a styled draft after the owner revoked its styles) keeps `unavailable`, as
`get_draft` does. Production broker, proxy and server over the synthetic raw transport.
"""
from __future__ import annotations

import asyncio
import copy
import unittest

from jsonschema import Draft202012Validator
from mcp import Client
from pydantic import ValidationError

from test_formatted_text_lifecycle import STYLES, TEXT, FormattedHarness
from test_topic_text_reply import CHAT
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.draft_models import ReviseDraftResponse, UpdateDraftRequest
from telegram_search_mcp.server import build_server

DETAIL = 'draft revision needs capabilities the owner has not enabled: missing_capabilities'
UNKNOWN = 'draft_' + 'f' * 32


def disabled(*missing):
    return {'status': 'capability_disabled', 'missing_capabilities': list(missing), 'detail': DETAIL,
            'next_action': 'enable_capability'}


def refusal(result):
    """The refusal's own fields; it carries no draft and no previous draft ID."""
    for key in ('previous_draft_id', 'draft'):
        if result.get(key) is not None:
            raise AssertionError(f'a refusal carries {key}')
    return {key: result.get(key) for key in ('status', 'missing_capabilities', 'detail', 'next_action')}


def styled(draft_id):
    return {'draft_id': draft_id, 'text': TEXT, 'outgoing_entities': copy.deepcopy(STYLES)}


def plain_draft(h):
    prepared = h.dispatch('prepare_text_send', {'recipient': CHAT, 'text': TEXT})
    if prepared['status'] != 'prepared':
        raise AssertionError('a plain text draft must prepare: ' + repr(prepared))
    return prepared['draft_id']


class ResponseModelTests(unittest.TestCase):
    def test_the_refusal_is_closed_and_derives_its_next_action(self):
        value = ReviseDraftResponse(status='capability_disabled', missing_capabilities=['formatted_text_send'],
                                    detail=DETAIL)
        self.assertEqual(value.model_dump(mode='json', exclude_none=True), disabled('formatted_text_send'))
        for bad in (dict(missing_capabilities=None), dict(missing_capabilities=[]),
                    dict(missing_capabilities=['formatted_text_send', 'formatted_text_send']),
                    dict(missing_capabilities=['not_a_capability']), dict(detail='draft is unavailable'),
                    dict(previous_draft_id=UNKNOWN), dict(next_action='check_connection')):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                ReviseDraftResponse.model_validate({'status': 'capability_disabled',
                                                    'missing_capabilities': ['formatted_text_send'],
                                                    'detail': DETAIL, **bad})
        with self.assertRaises(ValidationError):
            ReviseDraftResponse(status='unavailable', missing_capabilities=['send'], detail='draft is unavailable')
        with self.assertRaises(ValidationError):
            ReviseDraftResponse(status='unavailable', detail=DETAIL)
        unavailable = ReviseDraftResponse(status='unavailable', detail='draft is unavailable')
        self.assertEqual(unavailable.model_dump(mode='json', exclude_none=True),
                         {'status': 'unavailable', 'detail': 'draft is unavailable'})


class BrokerTests(unittest.TestCase):
    def test_a_disabled_text_style_option_is_named_before_the_draft_is_looked_up(self):
        with FormattedHarness('plain', enabled=False) as h:
            did = plain_draft(h)
            reads = len(h.raw.sent)
            for draft_id in (did, UNKNOWN):
                with self.subTest(known=draft_id == did):
                    self.assertEqual(refusal(h.dispatch('update_draft', styled(draft_id))),
                                     disabled('formatted_text_send'))
            self.assertEqual(len(h.raw.sent), reads)
            self.assertEqual((h.approvals, h.claim_count, h.register_count), ([], 0, 0))
            self.assertEqual(h.dispatch('get_draft', {'draft_id': did})['status'], 'pending')
            # Without the option the same revision goes ahead.
            self.assertEqual(h.dispatch('update_draft', {'draft_id': did, 'text': TEXT})['status'], 'revised')

    def test_with_the_option_enabled_the_same_revision_goes_ahead(self):
        with FormattedHarness('plain') as h:
            result = h.dispatch('update_draft', styled(plain_draft(h)))
            self.assertEqual(result['status'], 'revised')
            self.assertEqual(result['draft']['outgoing_entities'], STYLES)
            self.assertNotIn('missing_capabilities', result)
            self.assertNotIn('next_action', result)

    def test_a_stored_drafts_revoked_styles_keep_unavailable_as_get_draft_does(self):
        with FormattedHarness('plain') as h:
            h.prepare()
            h.policy_without('formatted_text_send')
            for operation, payload in (('get_draft', {'draft_id': h.current_id}),
                                       ('refresh_draft', {'draft_id': h.current_id})):
                with self.subTest(operation=operation):
                    result = h.dispatch(operation, payload)
                    self.assertEqual((result['status'], result['detail']), ('unavailable', 'draft is unavailable'))
                    self.assertNotIn('missing_capabilities', result)


class ProxyTests(unittest.TestCase):
    def test_the_proxy_passes_the_brokers_typed_refusal_through(self):
        with FormattedHarness('plain') as h:
            did = plain_draft(h)
            proxy = h.start_ipc()
            h.policy_without('formatted_text_send')  # The broker's policy only: the proxy's still allows styles.
            reads = len(h.raw.sent)
            result = proxy.update_draft(UpdateDraftRequest.model_validate(styled(did)))
            self.assertEqual(refusal(result.model_dump(mode='json')), disabled('formatted_text_send'))
            self.assertEqual(len(h.raw.sent), reads)


class SDKTests(unittest.TestCase):
    def test_the_official_client_sees_the_typed_refusal_valid_under_the_listed_output_schema(self):
        with FormattedHarness('plain', enabled=False) as h:
            did = plain_draft(h)
            proxy = h.start_ipc()
            policy = h.broker._policy

            async def run():
                async with Client(build_server(service_factory=lambda: proxy, policy=policy)) as consumer:
                    tools = {t.name: t for t in (await consumer.list_tools()).tools}
                    return tools, await consumer.call_tool('update_draft', styled(did))
            tools, result = asyncio.run(run())
            self.assertFalse(result.is_error, repr(result))
            data = result.structured_content
            Draft202012Validator(tools['update_draft'].output_schema).validate(data)
            self.assertEqual(refusal(data), disabled('formatted_text_send'))
            schema = tools['refresh_draft'].output_schema
            Draft202012Validator(schema).validate(data)  # one response model for both revisions


if __name__ == '__main__':
    unittest.main()
