"""Outgoing formatting through real broker, registry and native reducer.

The raw transport and owner decision are synthetic. Assertions count every
owned provider's raw sends and actual claim/registration entry points.
"""
import copy
import hashlib
import json
import unittest

from pydantic import ValidationError
from telegram_search_mcp.config import KNOWN_CAPABILITIES, LEGACY_CAPABILITIES
from telegram_search_mcp.contract import CompatibilityError
from test_topic_text_reply import (
    Harness, CAPS, CHAT, TOPIC, ANCHOR, NATIVE_TOPIC, reply_metadata,
    SEND_OPTIONS, LINK_OPTIONS,
)

TEXT = 'A🙂BC'
STYLES = [
    {'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}},
    {'offset': 1, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}},
]
NATIVE = [
    {'@type': 'textEntity', 'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}},
    {'@type': 'textEntity', 'offset': 1, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}},
]
ROUTES = ('plain', 'topic', 'reply', 'topic_reply')


class FormattedHarness(Harness):
    def __init__(self, route, *, enabled=True, styled=False):
        super().__init__(styled=styled,
            capabilities=(*CAPS, 'formatted_text_send') if enabled else CAPS)
        self.route = route
        self.is_reply = route in ('reply', 'topic_reply')
        self.is_topic = route in ('topic', 'topic_reply')
        if not self.is_topic:
            self.raw.source['topic_id'] = None
        self.body = TEXT
        self.entities = copy.deepcopy(NATIVE)
        self.output_mutation = None
        self.raw.preliminary_mutation = self.response
        self.raw.final_mutation = self.response

    def response(self, message):
        message['topic_id'] = copy.deepcopy(NATIVE_TOPIC) if self.is_topic else None
        message['reply_to'] = reply_metadata() if self.is_reply else None
        message['content']['text'] = {'@type': 'formattedText', 'text': self.body,
                                     'entities': copy.deepcopy(self.entities)}
        if self.output_mutation is not None:
            self.output_mutation(message)

    def prepare_result(self, **changes):
        payload = {'recipient': CHAT, 'text': self.body,
                   'outgoing_entities': copy.deepcopy(STYLES)}
        if self.is_topic:
            payload['topic'] = copy.deepcopy(TOPIC)
        if self.is_reply:
            payload['reply_to'] = copy.deepcopy(ANCHOR)
        payload.update(changes)
        return self.dispatch('prepare_reply_text_send' if self.is_reply else 'prepare_text_send', payload)

    def prepare(self, **changes):
        result = self.prepare_result(**changes)
        if result['status'] != 'prepared':
            raise AssertionError('formatted text must prepare: ' + repr(result))
        self.current_id = result['reply']['draft']['draft_id'] if self.is_reply else result['draft_id']
        return self.get()

    def get(self):
        result = self.dispatch('get_reply_draft' if self.is_reply else 'get_draft',
                               {'draft_id': self.current_id})
        return result.get('reply') if self.is_reply else result.get('draft')

    def update(self, **changes):
        return self.dispatch('update_reply_draft' if self.is_reply else 'update_draft',
                             {'draft_id': self.current_id, **changes})


class FormattedTextLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('formatted_text_send', KNOWN_CAPABILITIES,
                      'FEATURE ABSENT: explicit outgoing formatting capability')
        self.assertNotIn('formatted_text_send', LEGACY_CAPABILITIES)

    def test_four_routes_freeze_complete_styles_and_send_exactly_once(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                preview = h.prepare()
                draft = preview['draft'] if h.is_reply else preview
                self.assertEqual(draft['outgoing_entities'], STYLES)
                self.assertEqual(draft['text'], TEXT)
                self.assertTrue(preview['preview_sha256'])
                if h.is_reply:
                    self.assertNotIn('preview_sha256', draft)
                self.assertEqual(h.get(), preview)
                sent = h.send()
                self.assertEqual((sent['status'], sent['message_id']), ('sent', 701))
                self.assertEqual((len(h.approvals), h.claim_count, h.register_count, len(h.sends())), (1, 1, 1, 1))
                wire = h.sends()[0]
                self.assertEqual(wire['input_message_content']['text'],
                                 {'@type': 'formattedText', 'text': TEXT, 'entities': NATIVE})
                self.assertEqual(wire['topic_id'], NATIVE_TOPIC if h.is_topic else None)
                self.assertEqual(wire['reply_to'],
                    {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
                     'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''} if h.is_reply else None)
                self.assertEqual(h.send()['message_id'], 701)
                self.assertEqual(len(h.sends()), 1)

    def test_disabled_formatting_refuses_before_provider_hydration(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route, enabled=False) as h:
                try:
                    result = h.prepare_result()
                except CompatibilityError as error:
                    self.assertEqual(error.code, 'capability_disabled')
                else:
                    self.assertNotEqual(result['status'], 'prepared')
                self.assertEqual((h.raw.sent, h.approvals, h.claim_count, h.register_count), ([], [], 0, 0))

    def test_bad_shapes_and_entity_only_revision_refuse_without_hydration(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                for bad in (None, False, {}, [{'offset': True, 'length': 1, 'type': {'@type': 'textEntityTypeBold'}}],
                            [{'offset': 1, 'length': 1, 'type': {'@type': 'textEntityTypeBold'}}]):
                    with self.subTest(value=bad), self.assertRaises((ValidationError, ValueError)):
                        h.prepare_result(outgoing_entities=bad)
                self.assertEqual(h.raw.sent, [])
                h.prepare()
                before = copy.deepcopy(h.get())
                count = len(h.raw.sent)
                with self.assertRaises((ValidationError, ValueError)):
                    h.update(outgoing_entities=[])
                self.assertEqual(h.get(), before)
                self.assertEqual((h.claim_count, h.register_count, h.sends()), (0, 0, []))

    def test_input_mutation_cannot_change_frozen_style_or_approval(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                values = copy.deepcopy(STYLES)
                preview = h.prepare(outgoing_entities=values)
                values[0]['type']['@type'] = 'textEntityTypeItalic'
                values.clear()
                self.assertEqual(h.get(), preview)
                self.assertEqual(h.send()['status'], 'sent')
                self.assertEqual(h.sends()[0]['input_message_content']['text']['entities'], NATIVE)

    def test_replace_clear_and_topic_refresh_have_explicit_lifecycle(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                old = h.prepare()
                old_id = h.current_id
                italic = [{'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeItalic'}}]
                updated = h.update(text=TEXT, outgoing_entities=italic)
                self.assertEqual(updated['status'], 'revised')
                new = updated['reply'] if h.is_reply else updated['draft']
                draft = new['draft'] if h.is_reply else new
                h.current_id = draft['draft_id']
                self.assertNotEqual(h.current_id, old_id)
                self.assertNotEqual(new['preview_sha256'], old['preview_sha256'])
                self.assertEqual(draft['outgoing_entities'], italic)
                self.assertEqual(h.send(old_id)['status'], 'expired')
                operation = 'refresh_reply_draft' if h.is_reply else 'refresh_draft'
                refreshed = h.dispatch(operation, {'draft_id': h.current_id})
                self.assertEqual(refreshed['status'], 'unavailable' if h.is_topic else 'revised')
                if not h.is_topic:
                    new = refreshed['reply'] if h.is_reply else refreshed['draft']
                    draft = new['draft'] if h.is_reply else new
                    self.assertEqual(draft['outgoing_entities'], italic)
                    h.current_id = draft['draft_id']
                cleared = h.update(text=TEXT)
                self.assertEqual(cleared['status'], 'revised')
                new = cleared['reply'] if h.is_reply else cleared['draft']
                self.assertNotIn('outgoing_entities', new['draft'] if h.is_reply else new)
                self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

    def test_provider_epoch_or_capability_drift_refuses_before_owner(self):
        for route in ROUTES:
            for drift in ('provider', 'epoch', 'account', 'formatted_text_send'):
                with self.subTest(route=route, drift=drift), FormattedHarness(route) as h:
                    h.prepare()
                    h.mutate(drift)
                    self.assertNotEqual(h.send()['status'], 'sent')
                    self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

    def test_owner_revocation_and_registration_epoch_change_never_reach_wire(self):
        for route in ROUTES:
            with self.subTest(route=route, phase='owner'), FormattedHarness(route) as h:
                h.prepare()
                h.decision = lambda: (h.policy_without('formatted_text_send') or True)
                self.assertNotEqual(h.send()['status'], 'sent')
                self.assertEqual((len(h.approvals), h.claim_count, h.register_count, h.sends()), (1, 0, 0, []))
            with self.subTest(route=route, phase='registration'), FormattedHarness(route) as h:
                h.prepare()
                h.register_hook = lambda: h.mutate('epoch')
                self.assertNotEqual(h.send()['status'], 'sent')
                self.assertEqual((len(h.approvals), h.claim_count, h.register_count, h.sends()), (1, 1, 1, []))

    def test_mismatched_final_style_is_unknown_and_never_retries(self):
        for route in ROUTES:
            for mutation in ('missing', 'shift', 'extra', 'type'):
                with self.subTest(route=route, mutation=mutation), FormattedHarness(route) as h:
                    h.prepare()
                    def corrupt(message):
                        h.response(message)
                        values = message['content']['text']['entities']
                        if mutation == 'missing':
                            values.clear()
                        elif mutation == 'shift':
                            values[1]['offset'] = 3
                        elif mutation == 'extra':
                            values.append({'@type': 'textEntity', 'offset': 3, 'length': 1,
                                           'type': {'@type': 'textEntityTypeItalic'}})
                        else:
                            values[0]['type']['@type'] = 'textEntityTypeItalic'
                    h.raw.final_mutation = corrupt
                    result = h.send()
                    self.assertEqual(result['status'], 'outcome_unknown')
                    self.assertIsNone(result['message_id'])
                    self.assertEqual(h.send()['status'], 'outcome_unknown')
                    self.assertEqual(len(h.sends()), 1)

    def test_provider_entity_order_is_ignored_but_entity_facts_are_exact(self):
        for route in ROUTES:
            with self.subTest(route=route), FormattedHarness(route) as h:
                h.prepare()
                h.entities.reverse()
                self.assertEqual(h.send()['status'], 'sent')
                self.assertEqual(len(h.sends()), 1)

    def test_incoming_source_styles_do_not_grant_outgoing_style_authority(self):
        for route in ('reply', 'topic_reply'):
            with self.subTest(route=route), FormattedHarness(route, enabled=False, styled=True) as h:
                result = h.prepare_result(outgoing_entities=[])
                self.assertEqual(result['status'], 'prepared')
                self.assertNotIn('outgoing_entities', result['reply']['draft'])
                self.assertEqual(h.sends(), [])


    def test_formatted_ordinary_reply_listing_requires_reply_route_capability(self):
        with FormattedHarness('reply') as h:
            h.prepare()
            h.policy_without('reply_text_send')
            result = h.dispatch('list_drafts', {})
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['drafts'], [])
            self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))

    def test_formatted_ordinary_reply_receipt_requires_reply_route_capability(self):
        with FormattedHarness('reply') as h:
            h.prepare()
            self.assertEqual(h.send()['status'], 'sent')
            h.policy_without('reply_text_send')
            result = h.dispatch('get_send_status', {'draft_id': h.current_id})
            self.assertEqual(result['status'], 'outcome_unknown')
            self.assertIsNone(result['message_id'])
            self.assertNotEqual(h.send()['status'], 'sent')
            self.assertEqual((h.claim_count, h.register_count, len(h.sends())), (1, 1, 1))

    def test_formatted_ordinary_reply_final_disclosure_requires_reply_route_capability(self):
        with FormattedHarness('reply') as h:
            h.prepare()
            h.raw.final_hook = lambda: h.policy_without('reply_text_send')
            result = h.send()
            self.assertEqual(result['status'], 'outcome_unknown')
            self.assertIsNone(result['message_id'])
            self.assertNotEqual(h.send()['status'], 'sent')
            self.assertEqual((h.claim_count, h.register_count, len(h.sends())), (1, 1, 1))


    def test_formatted_ordinary_reply_eligibility_rechecked_before_owner(self):
        with FormattedHarness('reply') as h:
            h.prepare()
            h.mutate('properties')
            self.assertNotEqual(h.send()['status'], 'sent')
            self.assertEqual((h.approvals, h.claim_count, h.register_count, h.sends()), ([], 0, 0, []))


if __name__ == '__main__':
    unittest.main()
