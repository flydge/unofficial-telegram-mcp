"""Official MCP client -> actual local broker -> real TDLibClient with synthetic raw transport."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jsonschema import Draft202012Validator
from mcp import Client
from telegram_search_mcp.config import KNOWN_CAPABILITIES
from telegram_search_mcp.server import build_server

try:
    from test_formatted_text_lifecycle import FormattedHarness, TEXT, STYLES, ROUTES
except ModuleNotFoundError as error:
    if error.name != 'test_formatted_text_lifecycle':
        raise
    FormattedHarness = None
    TEXT, STYLES, ROUTES = None, None, None

SEND = {'@type': 'messageSendOptions', 'suggested_post_info': None,
        'disable_notification': False, 'from_background': False, 'protect_content': False,
        'allow_paid_broadcast': False, 'paid_message_star_count': 0,
        'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
        'effect_id': 0, 'only_preview': False}
LINK = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
        'force_small_media': False, 'force_large_media': False, 'show_above_text': False}
UPDATED = [{'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeItalic'}},
           {'offset': 1, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}}]
UPDATED_NATIVE = [{'@type': 'textEntity', 'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeItalic'}},
                  {'@type': 'textEntity', 'offset': 1, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}}]
INITIAL_NATIVE = [{'@type': 'textEntity', 'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}},
                  {'@type': 'textEntity', 'offset': 1, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}}]


def digest(facts):
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def expected_digest(preview, is_reply, native):
    if is_reply:
        draft = preview['draft']
        facts = {'version': 3 if native else 2 if 'topic' in draft else 1,
                 'draft': draft, 'reply_target': preview['reply_target'],
                 'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41} if 'topic' in draft else None,
                 'outgoing_entities': native, 'send_options': SEND, 'link_preview_options': LINK,
                 'clear_draft': False, 'reply_markup': None,
                 'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
                              'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}}
    else:
        facts = {'version': 2 if native else 1, 'draft_id': preview['draft_id'],
                 'account_id': preview['account_id'], 'recipient': preview['recipient'],
                 'recipient_title': preview['recipient_title'],
                 'expires_at': datetime.fromisoformat(preview['expires_at']).astimezone(timezone.utc).isoformat(),
                 'kind': 'text', 'approval_required': True, 'topic': preview.get('topic'),
                 'text': preview['text'], 'sha256': preview['sha256'], 'size_bytes': preview['size_bytes'],
                 'entities': native, 'reply_to': None, 'link_preview_options': LINK,
                 'send_options': SEND, 'reply_markup': None, 'clear_draft': False}
    return digest(facts)


class FormattedTextSDKTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIn('formatted_text_send', KNOWN_CAPABILITIES,
                      'FEATURE ABSENT: official SDK outgoing-formatting lifecycle')
        self.assertIsNotNone(FormattedHarness, 'FEATURE ABSENT: real formatting lifecycle fixture')

    async def call(self, consumer, tools, name, arguments):
        self.assertIn(name, tools)
        Draft202012Validator(tools[name].input_schema).validate(arguments)
        result = await consumer.call_tool(name, arguments)
        self.assertFalse(result.is_error, repr(result))
        data = result.structured_content
        self.assertIsInstance(data, dict)
        self.assertIsNotNone(tools[name].output_schema)
        Draft202012Validator(tools[name].output_schema).validate(data)
        # The extended union return has the SDK's declared result envelope.
        # Validate it first, then require that exact shape rather than a fallback.
        if name == 'prepare_reply_text_send':
            self.assertEqual(set(tools[name].output_schema['properties']), {'result'})
            self.assertEqual(set(data), {'result'})
            data = data['result']
        text = [block.text for block in result.content if getattr(block, 'type', None) == 'text']
        self.assertEqual(len(text), 1)
        self.assertEqual(json.loads(text[0]), data)
        return data

    def assert_preview(self, preview, is_reply, expected, native):
        draft = preview['draft'] if is_reply else preview
        self.assertEqual(draft['text'], 'A🙂BC')
        self.assertEqual(draft['outgoing_entities'], expected)
        self.assertEqual(draft['sha256'], hashlib.sha256('A🙂BC'.encode()).hexdigest())
        self.assertEqual(draft['size_bytes'], len('A🙂BC'.encode()))
        self.assertEqual(preview['preview_sha256'], expected_digest(preview, is_reply, native))
        if is_reply:
            self.assertNotIn('preview_sha256', draft)
        return draft['draft_id']

    async def test_four_routes_real_sdk_ipc_revision_receipt_and_single_transport(self):
        from test_topic_text_reply import CHAT, TOPIC, ANCHOR
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                proxy = h.start_ipc()
                async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
                    tools = {t.name: t for t in (await consumer.list_tools()).tools}
                    self.assertEqual(len(tools), 55)
                    for name in ('prepare_text_send', 'prepare_reply_text_send', 'update_draft', 'update_reply_draft'):
                        self.assertIn('outgoing_entities', tools[name].input_schema['properties'])
                    payload = {'recipient': CHAT, 'text': TEXT, 'outgoing_entities': copy.deepcopy(STYLES[::-1])}
                    if h.is_topic: payload['topic'] = copy.deepcopy(TOPIC)
                    if h.is_reply: payload['reply_to'] = copy.deepcopy(ANCHOR)
                    prepared = await self.call(consumer, tools, 'prepare_reply_text_send' if h.is_reply else 'prepare_text_send', payload)
                    self.assertEqual(prepared['status'], 'prepared')
                    # Nonreply prepare is a flat envelope; get supplies the full canonical draft.
                    did = prepared['reply']['draft']['draft_id'] if h.is_reply else prepared['draft_id']
                    got = await self.call(consumer, tools, 'get_reply_draft' if h.is_reply else 'get_draft', {'draft_id': did})
                    preview = got['reply'] if h.is_reply else got['draft']
                    self.assertEqual(self.assert_preview(preview, h.is_reply, STYLES, INITIAL_NATIVE), did)
                    if h.is_reply: self.assertEqual(prepared['reply'], preview)
                    else:
                        for field in ('text', 'outgoing_entities', 'preview_sha256'):
                            self.assertEqual(prepared[field], preview[field])
                    revised = await self.call(consumer, tools, 'update_reply_draft' if h.is_reply else 'update_draft',
                        {'draft_id': did, 'text': TEXT, 'outgoing_entities': copy.deepcopy(UPDATED[::-1])})
                    self.assertEqual((revised['status'], revised['previous_draft_id']), ('revised', did))
                    updated = revised['reply'] if h.is_reply else revised['draft']
                    fresh = self.assert_preview(updated, h.is_reply, UPDATED, UPDATED_NATIVE)
                    self.assertNotEqual(fresh, did)
                    self.assertNotEqual(updated['preview_sha256'], preview['preview_sha256'])
                    if h.is_reply:
                        self.assertEqual(updated['reply_target'], preview['reply_target'])
                    self.assertEqual((updated['draft'] if h.is_reply else updated).get('topic'),
                                     (preview['draft'] if h.is_reply else preview).get('topic'))
                    self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))
                    h.current_id = fresh; h.entities = copy.deepcopy(UPDATED_NATIVE)
                    sent = await self.call(consumer, tools, 'send_prepared_text', {'draft_id': fresh, 'approved': True})
                    self.assertEqual((sent['status'], sent['message_id']), ('sent', 701))
                    status = await self.call(consumer, tools, 'get_send_status', {'draft_id': fresh})
                    replay = await self.call(consumer, tools, 'send_prepared_text', {'draft_id': fresh, 'approved': True})
                    self.assertEqual((status['status'], status['message_id'], replay['status'], replay['message_id']),
                                     ('sent', 701, 'sent', 701))
                    self.assertEqual((len(h.approvals), h.claim_count, h.claim_success_count,
                                      h.register_count, h.register_success_count, len(h.sends())), (1, 1, 1, 1, 1, 1))
                    expected_wire = {'@type': 'sendMessage', 'chat_id': CHAT,
                        'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41} if h.is_topic else None,
                        'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55, 'quote': None,
                                     'checklist_task_id': 0, 'poll_option_id': ''} if h.is_reply else None,
                        'options': SEND, 'reply_markup': None,
                        'input_message_content': {'@type': 'inputMessageText',
                            'text': {'@type': 'formattedText', 'text': 'A🙂BC', 'entities': UPDATED_NATIVE},
                            'link_preview_options': LINK, 'clear_draft': False}}
                    observation = h.provider._send_observations.snapshot(fresh)
                    self.assertIsNotNone(observation)
                    self.assertEqual((observation.status, observation.message_id), ('sent', 701))
                    self.assertIs(type(observation.sending_id), int)
                    self.assertGreater(observation.sending_id, 0)
                    expected_wire['options'] = dict(SEND, sending_id=observation.sending_id)
                    expected_wire['@extra'] = observation.extra
                    self.assertEqual(h.sends()[0], expected_wire)
            self.assertFalse(h.thread.is_alive())
            self.assertTrue(all(provider._raw.closed for provider in h.providers))

    async def test_actual_sdk_owner_revocation_has_zero_claim_registration_or_send_attempts(self):
        from test_topic_text_reply import CHAT, TOPIC, ANCHOR
        with FormattedHarness('topic_reply') as h:
            proxy = h.start_ipc()
            async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
                tools = {t.name: t for t in (await consumer.list_tools()).tools}
                prepared = await self.call(consumer, tools, 'prepare_reply_text_send',
                    {'recipient': CHAT, 'text': TEXT, 'reply_to': ANCHOR, 'topic': TOPIC,
                     'outgoing_entities': copy.deepcopy(STYLES)})
                did = self.assert_preview(prepared['reply'], True, STYLES, INITIAL_NATIVE)
                h.decision = lambda: (h.policy_without('formatted_text_send') or True)
                refused = await self.call(consumer, tools, 'send_prepared_text', {'draft_id': did, 'approved': True})
                self.assertNotEqual(refused['status'], 'sent')
                self.assertIsNone(refused['message_id'])
                self.assertEqual((len(h.approvals), h.claim_count, h.claim_success_count,
                                  h.register_count, h.register_success_count, h.sends()), (1, 0, 0, 0, 0, []))
        self.assertFalse(h.thread.is_alive())

    async def test_five_styles_nfkc_revision_exact_wire_and_replay_on_four_routes(self):
        from test_topic_text_reply import CHAT, TOPIC, ANCHOR
        kinds = ('textEntityTypeBold', 'textEntityTypeItalic', 'textEntityTypeSpoiler',
                 'textEntityTypeStrikethrough', 'textEntityTypeUnderline')
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                proxy = h.start_ipc()
                async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
                    tools = {tool.name: tool for tool in (await consumer.list_tools()).tools}
                    self.assertEqual(len(tools), 55)
                    initial = [{'offset': 3, 'length': 2, 'type': {'@type': kind}} for kind in kinds]
                    payload = {'recipient': CHAT, 'text': 'ﬃ🙂B', 'outgoing_entities': initial[::-1]}
                    if h.is_topic: payload['topic'] = copy.deepcopy(TOPIC)
                    if h.is_reply: payload['reply_to'] = copy.deepcopy(ANCHOR)
                    prepared = await self.call(consumer, tools,
                        'prepare_reply_text_send' if h.is_reply else 'prepare_text_send', payload)
                    old_id = prepared['reply']['draft']['draft_id'] if h.is_reply else prepared['draft_id']
                    got = await self.call(consumer, tools, 'get_reply_draft' if h.is_reply else 'get_draft',
                                          {'draft_id': old_id})
                    old = got['reply'] if h.is_reply else got['draft']
                    old_draft = old['draft'] if h.is_reply else old
                    initial_native = [{'@type': 'textEntity', **entity} for entity in initial]
                    self.assertEqual((old_draft['text'], old_draft['outgoing_entities'], old_draft['size_bytes']),
                                     ('ffi🙂B', initial, 8))
                    self.assertEqual(old_draft['sha256'], hashlib.sha256('ffi🙂B'.encode()).hexdigest())
                    self.assertEqual(old['preview_sha256'], expected_digest(old, h.is_reply, initial_native))
                    revised_styles = [{'offset': 4, 'length': 2, 'type': {'@type': kind}} for kind in kinds]
                    revised_native = [{'@type': 'textEntity', **entity} for entity in revised_styles]
                    revised = await self.call(consumer, tools,
                        'update_reply_draft' if h.is_reply else 'update_draft',
                        {'draft_id': old_id, 'text': 'Ａﬃ🙂C', 'outgoing_entities': revised_styles[::-1]})
                    self.assertEqual((revised['status'], revised['previous_draft_id']), ('revised', old_id))
                    preview = revised['reply'] if h.is_reply else revised['draft']
                    draft = preview['draft'] if h.is_reply else preview
                    new_id = draft['draft_id']
                    self.assertNotEqual(new_id, old_id)
                    self.assertEqual((draft['text'], draft['outgoing_entities'], draft['size_bytes']),
                                     ('Affi🙂C', revised_styles, 9))
                    self.assertEqual(draft['sha256'], hashlib.sha256('Affi🙂C'.encode()).hexdigest())
                    self.assertEqual(preview['preview_sha256'], expected_digest(preview, h.is_reply, revised_native))
                    self.assertNotEqual(preview['preview_sha256'], old['preview_sha256'])
                    self.assertEqual(draft.get('topic'), old_draft.get('topic'))
                    if h.is_reply: self.assertEqual(preview['reply_target'], old['reply_target'])
                    inspected = await self.call(consumer, tools, 'get_reply_draft' if h.is_reply else 'get_draft',
                                                {'draft_id': new_id})
                    self.assertEqual(inspected['reply'] if h.is_reply else inspected['draft'], preview)
                    denied = await self.call(consumer, tools, 'send_prepared_text',
                                             {'draft_id': new_id, 'approved': False})
                    self.assertEqual((denied['status'], denied['message_id']), ('not_approved', None))
                    self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))
                    h.current_id = new_id; h.body = 'Affi🙂C'; h.entities = copy.deepcopy(revised_native)
                    sent = await self.call(consumer, tools, 'send_prepared_text',
                                           {'draft_id': new_id, 'approved': True})
                    replay = await self.call(consumer, tools, 'send_prepared_text',
                                             {'draft_id': new_id, 'approved': True})
                    self.assertEqual((sent['status'], sent['message_id']), ('sent', 701))
                    self.assertEqual(replay, {'status': 'sent', 'draft_id': new_id, 'recipient': None,
                        'message_id': 701, 'detail': 'previous provider attempt receipt; no new send was attempted'})
                    observation = h.provider._send_observations.snapshot(new_id)
                    self.assertIsNotNone(observation)
                    self.assertEqual(h.sends(), [{
                        '@type': 'sendMessage', 'chat_id': CHAT,
                        'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41} if h.is_topic else None,
                        'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55, 'quote': None,
                                     'checklist_task_id': 0, 'poll_option_id': ''} if h.is_reply else None,
                        'options': {**SEND, 'sending_id': observation.sending_id}, 'reply_markup': None,
                        'input_message_content': {'@type': 'inputMessageText',
                            'text': {'@type': 'formattedText', 'text': 'Affi🙂C', 'entities': revised_native},
                            'link_preview_options': LINK, 'clear_draft': False}, '@extra': observation.extra}])
                    self.assertEqual((len(h.approvals), h.claim_count, h.claim_success_count,
                                      h.register_count, h.register_success_count), (1, 1, 1, 1, 1))
            self.assertFalse(h.thread.is_alive())
            self.assertTrue(all(provider._raw.closed for provider in h.providers))

    async def test_coherent_post_ipc_style_digest_tamper_reaches_proxy_request_binding(self):
        from test_topic_text_reply import CHAT, TOPIC, ANCHOR
        from telegram_search_mcp.reply_drafts import PrepareReplyTextSendResponse
        with FormattedHarness('topic_reply') as h:
            proxy = h.start_ipc(); original = proxy._request; forged = []
            async with Client(build_server(service_factory=lambda: proxy, policy=h.broker._policy)) as consumer:
                tools = {t.name: t for t in (await consumer.list_tools()).tools}
                def changed(operation, payload):
                    envelope = original(operation, payload)  # Genuine Unix IPC still runs.
                    if operation == 'prepare_reply_text_send':
                        envelope = copy.deepcopy(envelope)
                        preview = envelope['reply']; preview['draft']['outgoing_entities'] = copy.deepcopy(UPDATED)
                        preview['preview_sha256'] = expected_digest(preview, True, UPDATED_NATIVE)
                        self.assertNotIn('preview_sha256', preview['draft'])
                        self.assertEqual(PrepareReplyTextSendResponse.model_validate(envelope).model_dump(mode='json'), envelope)
                        Draft202012Validator(tools['prepare_reply_text_send'].output_schema).validate({'result': envelope})
                        forged.append(copy.deepcopy(envelope))
                    return envelope
                with patch.object(proxy, '_request', side_effect=changed):
                    refused = await self.call(consumer, tools, 'prepare_reply_text_send',
                        {'recipient': CHAT, 'text': TEXT, 'reply_to': ANCHOR, 'topic': TOPIC,
                         'outgoing_entities': copy.deepcopy(STYLES)})
                self.assertEqual((refused['status'], refused['reply']), ('unavailable', None))
                self.assertEqual(len(forged), 1)
                self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))
        self.assertFalse(h.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
