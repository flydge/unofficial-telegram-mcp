"""Exact TextUrl evidence without navigation, source download or outgoing entities.

Expected records and escaped examples are hand-written; json only decodes the
one display layer or canonically encodes an independently specified projection.
The broker, registry, parser/hash, TDLib guards, reducers and staging stay real.
Only raw provider I/O and the owner callback are synthetic. This file is staged
outside the candidate until the root accepts the fresh preproduction baseline.
"""
import copy
import hashlib
import itertools
import io
import json
import logging
import tempfile
import time
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import ConfigurationError, RuntimePolicy, load_runtime_policy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner
from telegram_search_mcp.outgoing_stage import retire_staged_document, stage_approved_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_formatted_sources import (
    formatted_text, render_content, render_formatted_text, validate_content,
)
from telegram_search_mcp.sanitize import sanitize_telegram_text
from telegram_search_mcp.tdjson import TDLibClient
from test_reply_phone_targets import absolute, entity as old_entity, media, message as old_message, disabled

CHAT, MESSAGE, ACCOUNT = 123, 55, 7
CLIENT = 'client_' + 'c' * 24
FOREIGN = 'client_' + 'd' * 24
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
CAPS = ('reply_formatted_targets', 'reply_text_url_targets', 'reply_bank_card_targets',
        'reply_phone_targets', 'reply_email_targets', 'reply_url_targets',
        'reply_datetime_targets', 'reply_lexical_targets', 'reply_identity_targets')
POLICY = RuntimePolicy(enabled_capabilities=BASE + CAPS)
PREFIX = '[untrusted Telegram evidence] Formatted target: '
TEXT_URL = 'textEntityTypeTextUrl'
URL = 'https://example.invalid/a?x=1&y=2'
LEGACY_FIXTURE = 'reply_text_url_v22_goldens.json'
# Independently accepted preproduction bytes; never regenerate with v23.
LEGACY_FIXTURE_SHA256 = 'e676ba7383eb342d240e13ef3401c430a4bf2c3a109b2795fc6c10f2f8e1afce'
LEGACY_FIXTURE_ROWS = 285

EXACT_BODY = (
    r'{"entities":[{"length":4,"offset":8,"provider_type":"textEntityTypeTextUrl",'
    r'"provider_url":"https\u003a\u002f\u002fexample\u002einvalid\u002fa\u003fx\u003d1\u0026y\u003d2",'
    r'"text":"docs","type":"text_url"}],'
    r'"offset_basis":"original\u0020source\u0020UTF-16\u0020code\u0020units",'
    r'"text":"Read\u0020\ud83d\ude00\u0020docs"}'
)
STYLE_BODY = (
    r'{"entities":[{"length":4,"offset":8,"text":"docs","type":"bold"},'
    r'{"length":4,"offset":8,"provider_type":"textEntityTypeTextUrl",'
    r'"provider_url":"https\u003a\u002f\u002fexample\u002einvalid\u002fa\u003fx\u003d1\u0026y\u003d2",'
    r'"text":"docs","type":"text_url"}],'
    r'"offset_basis":"original\u0020source\u0020UTF-16\u0020code\u0020units",'
    r'"text":"Read\u0020\ud83d\ude00\u0020docs"}'
)
SIMPLE_TEMPLATE = (
    r'{"entities":[{"length":1,"offset":0,"provider_type":"textEntityTypeTextUrl",'
    r'"provider_url":"%s","text":"x","type":"text_url"}],'
    r'"offset_basis":"original\u0020source\u0020UTF-16\u0020code\u0020units","text":"x"}'
)
SEND_OPTIONS = {'@type': 'messageSendOptions', 'suggested_post_info': None,
    'disable_notification': False, 'from_background': False, 'protect_content': False,
    'allow_paid_broadcast': False, 'paid_message_star_count': 0,
    'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
    'effect_id': 0, 'only_preview': False}
LINK_OPTIONS = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
    'force_small_media': False, 'force_large_media': False, 'show_above_text': False}
REPLY_TO = {'@type': 'inputMessageReplyToMessage', 'message_id': MESSAGE,
    'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}


def encode(value):
    """Standard JSON, not a production canonicalizer or display encoder."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(kind='TextUrl', offset=0, length=1, **fields):
    if kind == 'TextUrl' and 'url' not in fields:
        fields['url'] = URL
    return old_entity(kind, offset, length, **fields)


def message(text='Read 😀 docs', spans=None):
    return old_message(text, [entity(offset=8, length=4)] if spans is None else spans)


def simple(url=''):
    return message('x', [entity(url=url)])


def mixed():
    # Disjoint spans counted in the original source, not inferred from grammar.
    return message('TBPEUD#tag@user', [entity(url='OpaqueA'),
        entity('BankCardNumber', 1), entity('PhoneNumber', 2),
        entity('EmailAddress', 3), entity('Url', 4),
        entity('DateTime', 5, unix_time=1, formatting_type=absolute()),
        entity('Hashtag', 6, 4), entity('MentionName', 10, 5, user_id=42)])


def expected_projection(text='Read 😀 docs', spans=None):
    """Hand-specified complete canonical shell for the synthetic fixture."""
    return {'version': 23, 'anchor': {'chat_id': CHAT, 'message_id': MESSAGE}, 'message': {
        '@type': 'message', 'chat_id': CHAT, 'id': MESSAGE,
        'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
        'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
        'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0.0, 'auto_delete_in': 0.0,
        'sending_state': None, 'scheduling_state': None, 'topic_id': None,
        'self_destruct_type': None, 'ephemeral_content': None, 'receiver_id': None,
        'reply_to': None, 'forward_info': None, 'import_info': None, 'reply_markup': None,
        'content': {'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text,
            'entities': [entity(offset=8, length=4)] if spans is None else spans},
            'link_preview': None, 'link_preview_options': None}}}


def projection_at_character_count(characters, multibyte_count=0):
    """Independent test input: fill 32 valid hidden fields, each <=4096 scalars.

    Replacing ASCII with 界 changes only UTF-8 byte count (+2 per scalar).
    This creates canonical valid parser inputs; target rendering is separate.
    """
    spans = [entity(offset=i, url='') for i in range(32)]
    projection = expected_projection('x' * 32, spans)
    payload = characters - len(encode(projection))
    if not 0 <= multibyte_count <= payload <= 32 * 4096:
        raise AssertionError('invalid independently specified projection fixture size')
    remaining_wide = multibyte_count
    for span in spans:
        count = min(payload, 4096)
        wide = min(remaining_wide, count)
        span['type']['url'] = '界' * wide + 'x' * (count - wide)
        payload -= count
        remaining_wide -= wide
    if payload or remaining_wide or len(encode(projection)) != characters:
        raise AssertionError('projection input construction missed its specified size')
    return projection


class TextUrlSourceTests(unittest.TestCase):
    def accepted(self, raw=None):
        try:
            return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            self.fail('validated TextUrl must preserve visible and hidden v23 evidence: ' + str(error))

    def explicit_formatted(self, value):
        # A genuine old-constructor refusal precedes any new keyword call in RED.
        self.accepted()
        return formatted_text(value, text_url=True)

    def test_exact_single_entity_raw_display_hash_and_immutable_roundtrip(self):
        # Break: omission/normalization of the hidden field or wrong v23 display/hash.
        source = self.accepted()
        expected = encode(expected_projection())
        self.assertEqual(source.projection_json, expected)
        self.assertEqual(source.source_sha256, hashlib.sha256(expected.encode('utf-8')).hexdigest())
        target = source.target()
        self.assertEqual(target.text, PREFIX + EXACT_BODY)
        self.assertEqual(len(target.text), 367)
        self.assertEqual(json.loads(EXACT_BODY), {
            'text': 'Read 😀 docs', 'offset_basis': 'original source UTF-16 code units',
            'entities': [{'type': 'text_url', 'provider_type': TEXT_URL,
                'provider_url': URL, 'text': 'docs', 'offset': 8, 'length': 4}]})
        self.assertEqual(json.loads(target.text[len(PREFIX):]), json.loads(EXACT_BODY))
        self.assertEqual(source.raw_text, 'Read 😀 docs')
        self.assertEqual(source.required_capabilities, ('reply_formatted_targets', 'reply_text_url_targets'))
        self.assertFalse(source.is_media)
        self.assertTrue(target.sanitized)
        self.assertFalse(target.truncated)
        self.assertEqual(sanitize_telegram_text(target.text, max_length=4097), target.text)
        self.assertEqual(ReplySource(expected).target(), target)
        with self.assertRaises(ValueError):
            _ = source.required_capability
        raw = message(); isolated = self.accepted(raw)
        raw['content']['text']['text'] = 'changed'
        raw['content']['text']['entities'][0]['type']['url'] = 'changed'
        self.assertEqual((isolated.projection_json, isolated.target(), isolated.source_sha256),
                         (source.projection_json, target, source.source_sha256))

    def test_v1_through_v22_complete_preproduction_bytes_and_authority(self):
        # Break: new admission rewrites any accepted legacy canonical/display/hash/capability.
        self.assertNotEqual(LEGACY_FIXTURE_SHA256, 'UNBOUND_PREPRODUCTION_V22_FIXTURE_SHA256',
                            'root must bind independently accepted baseline before RED')
        self.assertIs(type(LEGACY_FIXTURE_ROWS), int)
        fixture = Path(__file__).parent / 'fixtures' / LEGACY_FIXTURE
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), LEGACY_FIXTURE_SHA256)
        rows = json.loads(fixture.read_text())
        self.assertEqual(len(rows), LEGACY_FIXTURE_ROWS)
        versions = set()
        for row in rows:
            with self.subTest(label=row.get('label'), version=row.get('version')):
                source = source_from_message(row['raw'], CHAT, MESSAGE)
                versions.add(json.loads(source.projection_json)['version'])
                self.assertEqual(source.projection_json, row['projection_json'])
                self.assertEqual(source.target().text, row['display'])
                self.assertEqual(source.source_sha256, row['raw_sha256'])
                self.assertEqual(hashlib.sha256(source.target().text.encode('utf-8')).hexdigest(), row['display_sha256'])
                self.assertEqual(source.required_capabilities, tuple(row['capabilities']))
                self.assertEqual(ReplySource(row['projection_json']).target(), source.target())
        self.assertEqual(versions, set(range(1, 23)))

    def test_opaque_hidden_scalars_roundtrip_exactly_with_literal_escaping(self):
        # Break: URL validation/stripping/normalization, unescaped active syntax or double decoding.
        examples = (
            ('', '', False), ('Ab_9-Z', 'Ab_9-Z', False),
            (' ', r'\u0020', True), ('\t\n\r', r'\u0009\u000a\u000d', True),
            ('\x00', r'\u0000', False), ('\x01\x1f\x7f\x85\x9f', r'\u0001\u001f\u007f\u0085\u009f', True),
            ('\u202e', r'\u202e', True), (r'\u202e', r'\u005cu202e', True),
            ('Ａ', r'\uff21', True), ('😀', r'\ud83d\ude00', True),
            ('<b>[x](y)</b>', r'\u003cb\u003e\u005bx\u005d\u0028y\u0029\u003c\u002fb\u003e', True),
            ('https://x.invalid/a?b=1&c=2', r'https\u003a\u002f\u002fx\u002einvalid\u002fa\u003fb\u003d1\u0026c\u003d2', True),
            ('tg://user?id=42', r'tg\u003a\u002f\u002fuser\u003fid\u003d42', True),
            ('mailto:a@b.invalid', r'mailto\u003aa\u0040b\u002einvalid', True),
            ('tel:+123', r'tel\u003a\u002b123', True),
            ('javascript:alert(1)', r'javascript\u003aalert\u00281\u0029', True),
            ('file:///tmp/a', r'file\u003a\u002f\u002f\u002ftmp\u002fa', True),
            ('"\\', r'\u0022\u005c', True),
        )
        for hidden, escaped, changed in examples:
            with self.subTest(hidden=repr(hidden)):
                source = self.accepted(simple(hidden)); target = source.target()
                expected = PREFIX + SIMPLE_TEMPLATE % escaped
                self.assertEqual(target.text, expected)
                record = json.loads(target.text[len(PREFIX):])
                self.assertEqual(record['entities'][0]['provider_url'], hidden)
                self.assertEqual(json.loads(source.projection_json)['message']['content']['text']['entities'][0]['type'],
                                 {'@type': TEXT_URL, 'url': hidden})
                self.assertEqual(target.sanitized, changed)
                # Normal JSON is ensure_ascii=False; the fixed offset label is excluded.
                self.assertEqual(changed, ('"' + escaped + '"') != json.dumps(hidden, ensure_ascii=False))
                self.assertFalse(target.truncated)
                self.assertEqual(sanitize_telegram_text(target.text, max_length=4097), target.text)

    def test_previous_sanitation_collisions_keep_distinct_display_and_identity(self):
        # Break: applying legacy sanitizer before hashing/encoding collapses distinct provider data.
        for left, right in (('Ａ', 'A'), ('a  b', 'a b'), ('a\tb', 'a b'),
                            ('a\nb', 'a b'), ('\u202e', r'\u202e'), ('', ' ')):
            with self.subTest(left=repr(left), right=repr(right)):
                a = self.accepted(simple(left)); b = self.accepted(simple(right))
                self.assertNotEqual(a.projection_json, b.projection_json)
                self.assertNotEqual(a.source_sha256, b.source_sha256)
                self.assertNotEqual(a.target().text, b.target().text)
                self.assertEqual(json.loads(a.target().text[len(PREFIX):])['entities'][0]['provider_url'], left)
                self.assertEqual(json.loads(b.target().text[len(PREFIX):])['entities'][0]['provider_url'], right)
        for left, right in (('Ａ', 'A'), ('A\tB', 'A B'), ('A\nB', 'A B')):
            a = self.accepted(message(left, [entity(length=1, url='')]))
            b = self.accepted(message(right, [entity(length=1, url='')]))
            self.assertNotEqual(a.target().text, b.target().text)
            self.assertEqual(json.loads(a.target().text[len(PREFIX):])['text'], left)
            self.assertEqual(json.loads(b.target().text[len(PREFIX):])['text'], right)

    def test_original_visible_body_policy_stays_nonblank_bounded_and_control_closed(self):
        # Break: copying permissive hidden-string rules onto original visible message text.
        self.accepted(simple(''))
        for text in ('', ' ', '\t\n', 'x\x00', 'x\x85', 'x\u202e', 'x\r', 'x\ud800', 'x' * 4097):
            with self.subTest(text=repr(text)[:80]), self.assertRaises(ValueError):
                source_from_message(message(text, [entity(url='')]), CHAT, MESSAGE)
        for text in ('x\tZ', 'x\nZ', '😀ＡZ'):
            source = self.accepted(message(text, [entity(length=2 if text.startswith('😀') else 1, url='')]))
            self.assertEqual(json.loads(source.target().text[len(PREFIX):])['text'], text)

    def test_entire_original_body_outside_hidden_span_is_escaped_inert_evidence(self):
        # Break: the new encoder protects only the URL/span while leaving other original text active.
        text = 'x <b>[go](https://x.invalid)</b> @name 😀'
        source = self.accepted(message(text, [entity(url='')]))
        record = json.loads(source.target().text[len(PREFIX):])
        self.assertEqual(record['text'], text)
        self.assertEqual(record['entities'][0]['text'], 'x')
        self.assertEqual(record['entities'][0]['provider_url'], '')
        self.assertNotIn('https://', source.target().text)
        self.assertNotIn('<b>', source.target().text)
        self.assertNotIn('@name', source.target().text)
        self.assertNotIn('😀', source.target().text)
        self.assertIn(r'\u003c', source.target().text)
        self.assertTrue(source.target().sanitized)

    def test_strict_hidden_string_type_utf8_and_closed_shapes_refuse(self):
        # Break: coercing hidden values, accepting invalid UTF-8 or leaking open fields.
        self.accepted()
        class StringSubclass(str):
            pass
        for hidden in (None, False, True, 0, 1.5, [], {}, b'url', StringSubclass('x'),
                       '\ud800', '\udfff', '\ud83d\ude00', 'x' * 4097):
            raw = simple(hidden)
            with self.subTest(hidden=repr(hidden)[:80]), self.assertRaises(ValueError):
                source_from_message(raw, CHAT, MESSAGE)
        for typ in ({'@type': TEXT_URL}, {'@type': TEXT_URL, 'url': '', 'extra': None},
                    {'@type': TEXT_URL, 'url': '', 'user_id': 42},
                    {'@type': [TEXT_URL], 'url': ''}, {'@type': True, 'url': ''},
                    {'@type': TEXT_URL + ' ', 'url': ''}, TEXT_URL, None):
            raw = simple(); raw['content']['text']['entities'][0]['type'] = typ
            with self.subTest(typ=typ), self.assertRaises(ValueError):
                source_from_message(raw, CHAT, MESSAGE)
        for field in ('extra', 'url'):
            raw = simple(); raw['content']['text']['entities'][0][field] = ''
            with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
        for field in ('@type', 'offset', 'length', 'type'):
            raw = simple(); del raw['content']['text']['entities'][0][field]
            with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
        for field, bad in (('@type', []), ('text', 1), ('entities', (entity(url=''),))):
            raw = simple(); raw['content']['text'][field] = bad
            with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
        raw = simple(); raw['content']['text']['extra'] = None
        with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
        for kind, fields in (('CustomEmoji', {'custom_emoji_id': 1}),
                             ('MediaTimestamp', {'media_timestamp': 1}), ('Unknown', {})):
            with self.assertRaises(ValueError):
                source_from_message(message('x', [entity(kind, **fields)]), CHAT, MESSAGE)

    def test_utf16_whole_scalar_spans_and_exact_coextensive_style_order(self):
        # Break: scalar-based slicing, split astral ranges, or order-dependent canonical evidence.
        raw = message(spans=[entity(offset=8, length=4), entity('Bold', 8, 4)])
        source = self.accepted(raw)
        self.assertEqual(source.target().text, PREFIX + STYLE_BODY)
        expected = expected_projection(spans=[entity('Bold', 8, 4), entity(offset=8, length=4)])
        self.assertEqual(source.projection_json, encode(expected))
        raw['content']['text']['entities'].reverse()
        self.assertEqual(self.accepted(raw).projection_json, source.projection_json)
        astral = self.accepted(message('😀Ａ\tB', [entity(length=3, url='')]))
        self.assertEqual(json.loads(astral.target().text[len(PREFIX):])['entities'][0],
            {'type': 'text_url', 'provider_type': TEXT_URL, 'provider_url': '',
             'offset': 0, 'length': 3, 'text': '😀Ａ'})
        for offset, length in ((0, 1), (1, 1), (True, 2), ('0', 2), (0.0, 2), (None, 2),
                               (0, False), (0, '2'), (0, 2.0), (0, None),
                               (-1, 2), (0, 0), (0, -1), (0, 99), (2**31, 2), (0, 2**31)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                source_from_message(message('😀abc', [entity(offset=offset, length=length)]), CHAT, MESSAGE)

    def test_semantic_exclusivity_duplicate_crossing_and_style_containment(self):
        # Break: hidden links escape semantic overlap policy or ordinary styles become unavailable.
        self.accepted()
        for kind in ('Bold', 'Italic', 'Underline', 'Strikethrough', 'Spoiler'):
            for spans in ([entity(kind, 0, 5), entity(offset=1, length=3)],
                          [entity(length=5), entity(kind, 1, 3)],
                          [entity(length=3), entity(kind, 0, 3)]):
                self.accepted(message('abcde', spans))
        for kind, fields in (('TextUrl', {'url': 'other'}), ('BankCardNumber', {}),
                ('PhoneNumber', {}), ('EmailAddress', {}), ('Url', {}), ('Hashtag', {}),
                ('Cashtag', {}), ('BotCommand', {}), ('Mention', {}), ('MentionName', {'user_id': 42}),
                ('Code', {}), ('Pre', {}), ('PreCode', {'language': 'py'}), ('BlockQuote', {}),
                ('ExpandableBlockQuote', {}), ('DateTime', {'unix_time': 1, 'formatting_type': None})):
            for spans in ([entity(length=5), entity(kind, 1, 3, **fields)],
                          [entity(kind, 0, 5, **fields), entity(offset=1, length=3)]):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    source_from_message(message('abcde', spans), CHAT, MESSAGE)
        for spans in ([entity(length=3), entity(length=3)],
                      [entity(length=3, url='a'), entity(length=3, url='b')],
                      [entity(length=3), entity('Bold', 2, 3)]):
            with self.assertRaises(ValueError): source_from_message(message('abcde', spans), CHAT, MESSAGE)

    def test_every_present_family_authority_and_complete_original_metadata(self):
        # Break: dropping mixed evidence or omitting/reordering a conditional authority conjunct.
        labels = ('bank_card_number', 'phone_number', 'email_address', 'url', 'date_time', 'hashtag', 'mention_name')
        present_caps = CAPS[2:]
        expected_records = [
            {'type': 'text_url', 'provider_type': TEXT_URL, 'provider_url': 'OpaqueA', 'offset': 0, 'length': 1, 'text': 'T'},
            {'type': 'bank_card_number', 'provider_type': 'textEntityTypeBankCardNumber', 'offset': 1, 'length': 1, 'text': 'B'},
            {'type': 'phone_number', 'provider_type': 'textEntityTypePhoneNumber', 'offset': 2, 'length': 1, 'text': 'P'},
            {'type': 'email_address', 'provider_type': 'textEntityTypeEmailAddress', 'offset': 3, 'length': 1, 'text': 'E'},
            {'type': 'url', 'provider_type': 'textEntityTypeUrl', 'offset': 4, 'length': 1, 'text': 'U'},
            {'type': 'date_time', 'provider_type': 'textEntityTypeDateTime', 'offset': 5, 'length': 1, 'text': 'D',
                'unix_time': 1, 'formatting_type': absolute()},
            {'type': 'hashtag', 'offset': 6, 'length': 4, 'text': '#tag'},
            {'type': 'mention_name', 'offset': 10, 'length': 5, 'text': '@user', 'user_id': 42},
        ]
        for enabled in itertools.product((False, True), repeat=7):
            raw = mixed(); spans = raw['content']['text']['entities']
            raw['content']['text']['entities'] = [spans[0]] + [span for span, on in zip(spans[1:], enabled) if on]
            source = self.accepted(raw)
            self.assertEqual(source.required_capabilities, CAPS[:2] + tuple(cap for cap, on in zip(present_caps, enabled) if on))
            records = [expected_records[0]] + [record for record, on in zip(expected_records[1:], enabled) if on]
            self.assertEqual(json.loads(source.target().text[len(PREFIX):]), {
                'text': 'TBPEUD#tag@user', 'offset_basis': 'original source UTF-16 code units', 'entities': records})
            self.assertEqual([r['type'] for r in records[1:]], [label for label, on in zip(labels, enabled) if on])
        for fmt in (None, {'@type': 'dateTimeFormattingTypeRelative'}, absolute()):
            raw = mixed(); raw['content']['text']['entities'][5]['type']['formatting_type'] = fmt
            source = self.accepted(raw)
            self.assertEqual(json.loads(source.projection_json)['version'], 23)
            self.assertEqual(json.loads(source.target().text[len(PREFIX):])['entities'][5]['formatting_type'], fmt)
        raw = message('xZ', [entity(url=''), entity('PreCode', 1, language='Ａ')])
        source = self.accepted(raw)
        self.assertIn(r'"language":"\uff21"', source.target().text)
        self.assertEqual(json.loads(source.target().text[len(PREFIX):])['entities'][1]['language'], 'Ａ')
        self.assertTrue(source.target().sanitized)

    def test_default_shared_helpers_refuse_and_explicit_text_only_optin_is_scoped(self):
        # Break: accidental global TextUrl admission or opt-in lost in validator/renderer.
        source = self.accepted(); content = json.loads(source.projection_json)['message']['content']
        formatted = content['text']
        for operation in (lambda: formatted_text(formatted), lambda: validate_content(content),
                          lambda: render_formatted_text(formatted),
                          lambda: render_content(content, '[untrusted Telegram evidence] ')):
            with self.assertRaises(ValueError): operation()
        self.assertEqual(self.explicit_formatted(formatted), formatted)
        validate_content(content, text_url=True)
        record, changed = render_formatted_text(formatted, text_url=True)
        self.assertEqual(record, json.loads(EXACT_BODY)); self.assertTrue(changed)
        self.assertEqual(render_content(content, '[untrusted Telegram evidence] ', text_url=True),
                         (PREFIX + EXACT_BODY, True))

    def test_texturl_captions_select_v24_but_the_media_keyword_shims_stay_closed(self):
        # Break: 0.58 media TextUrl (v24, criterion 4's listed flip) stays refused, or the 0.57 media helper keywords
        # silently admit it.
        from telegram_search_mcp.reply_media_sources import media_content, validate_content as validate_media, render_content as render_media
        self.accepted()
        for kind in ('document', 'photo', 'voice_note'):
            for raw, flags in ((message(), {}), (mixed(), dict(lexical=True, identity=True,
                    datetime=True, datetime_relative=True, datetime_absolute=True,
                    url=True, email=True, phone=True, bank_card=True))):
                caption = raw['content']['text']; raw['content'] = media(kind, caption)
                # Positive controls keep all other admission predicates valid.
                without = copy.deepcopy(raw)
                without['content']['caption']['entities'] = caption['entities'][1:]
                canonical = json.loads(source_from_message(without, CHAT, MESSAGE).projection_json)['message']['content']
                self.assertEqual(media_content(without['content'], **flags), canonical)
                validate_media(canonical, **flags)
                render_media(canonical, '[untrusted Telegram evidence] ', **flags)
                admitted = source_from_message(raw, CHAT, MESSAGE)
                self.assertEqual(json.loads(admitted.projection_json)['version'], 24)
                self.assertEqual(admitted.required_capabilities[:3],
                                 ('reply_media_targets', 'reply_formatted_targets', 'reply_text_url_targets'))
                closed = json.loads(admitted.projection_json)['message']['content']
                for operation in (lambda: media_content(raw['content'], **flags),
                                  lambda: validate_media(closed, **flags),
                                  lambda: render_media(closed, '[untrusted Telegram evidence] ', **flags)):
                    with self.subTest(kind=kind), self.assertRaises(ValueError): operation()

    def test_forged_versions_without_texturl_and_media_as_v23_refuse(self):
        # Break: constructor hints authorize forged old/new tags or a media projection.
        stored = json.loads(self.accepted().projection_json)
        for version in (*range(1, 23), 24, '23', True, 23.0, None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ReplySource(encode({**stored, 'version': version}))
        for spans in ([], [entity('Bold')], [entity('BankCardNumber')], [entity('Url')],
                      [entity('DateTime', unix_time=1, formatting_type=absolute())]):
            old = source_from_message(message('x', spans), CHAT, MESSAGE)
            with self.assertRaises(ValueError): ReplySource(encode({**json.loads(old.projection_json), 'version': 23}))
        for kind in ('document', 'photo', 'voice_note'):
            raw = message('x', [entity('BankCardNumber')]); raw['content'] = media(kind, raw['content']['text'])
            old = source_from_message(raw, CHAT, MESSAGE)
            with self.assertRaises(ValueError): ReplySource(encode({**json.loads(old.projection_json), 'version': 23}))
        for field in ('anchor', 'message', 'top'):
            forged = copy.deepcopy(stored)
            if field == 'top': forged['extra'] = None
            else: forged[field]['extra'] = None
            with self.assertRaises(ValueError): ReplySource(encode(forged))

    def test_hidden_scalar_cap_is_separate_from_complete_display_cap(self):
        # Break: incorrect hidden allocation bound or mistaken truncation at display overflow.
        self.accepted(simple(''))
        for hidden in ('x' * 4096, '😀' * 4096):
            value = simple(hidden)['content']['text']
            self.assertEqual(self.explicit_formatted(value)['entities'][0]['type']['url'], hidden)
            with self.assertRaises(ValueError): source_from_message(simple(hidden), CHAT, MESSAGE)
        for hidden in ('x' * 4097, '😀' * 4097):
            with self.assertRaises(ValueError): self.explicit_formatted(simple(hidden)['content']['text'])
        for text in ('x' * 4096, '😀' * 4096):
            spans = [entity(length=2 if text.startswith('😀') else 1, url='')]
            self.assertEqual(self.explicit_formatted(message(text, spans)['content']['text'])['text'], text)
        with self.assertRaises(ValueError): self.explicit_formatted(message('x' * 4097, [entity(url='')])['content']['text'])

    def test_complete_marked_display_exact_edge_and_escaped_expansion_refuse(self):
        # Break: marker exclusion, truncating hidden evidence or measuring before escaping.
        overhead = len(PREFIX + SIMPLE_TEMPLATE % '')
        hidden = 'a' * (4096 - overhead)
        source = self.accepted(simple(hidden))
        self.assertEqual(source.target().text, PREFIX + SIMPLE_TEMPLATE % hidden)
        self.assertEqual(len(source.target().text), 4096)
        self.assertFalse(source.target().truncated)
        with self.assertRaises(ValueError): source_from_message(simple(hidden + 'a'), CHAT, MESSAGE)
        dots, remainder = divmod(4096 - overhead, 6)
        hidden = '.' * dots + 'a' * remainder
        expected = PREFIX + SIMPLE_TEMPLATE % (r'\u002e' * dots + 'a' * remainder)
        self.assertEqual(self.accepted(simple(hidden)).target().text, expected)
        self.assertEqual(len(expected), 4096)
        with self.assertRaises(ValueError): source_from_message(simple(hidden + '.'), CHAT, MESSAGE)

    def test_32_entities_accept_and_33_refuse_without_display_obscuring_parser_bound(self):
        # Break: silently dropping spans or accepting the 33rd entity.
        spans = [entity(url='')] + [entity('Bold', offset=i) for i in range(1, 32)]
        source = self.accepted(message('x' * 32, spans))
        self.assertEqual(len(json.loads(source.target().text[len(PREFIX):])['entities']), 32)
        value = message('x' * 32, [entity(offset=i, url='') for i in range(32)])['content']['text']
        self.assertEqual(len(self.explicit_formatted(value)['entities']), 32)
        with self.assertRaises(ValueError):
            self.explicit_formatted(message('x' * 33, spans + [entity('Bold', offset=32)])['content']['text'])
        with self.assertRaises(ValueError):
            source_from_message(message('x' * 33, spans + [entity('Bold', offset=32)]), CHAT, MESSAGE)

    def test_projection_character_limit_on_canonical_valid_texturl_input(self):
        # Break: relaxing the 65536 canonical projection ceiling before rendering.
        self.accepted()
        for size, accepted in ((65536, True), (65537, False)):
            projection = projection_at_character_count(size)
            serialized = encode(projection)
            self.assertEqual((len(serialized), len(serialized.encode('utf-8'))), (size, size))
            source = ReplySource._validated(projection)
            self.assertEqual(source.projection_json, serialized)
            if accepted:
                self.assertEqual(source._validate_projection(), projection)
                with self.assertRaises(ValueError): source.target()
            else:
                with self.assertRaises(ValueError): source._validate_projection()
        # ASCII character and byte ceilings coincide mathematically. The next
        # test isolates the byte guard with a valid smaller-character projection.

    def test_projection_utf8_byte_limit_independent_of_character_limit(self):
        # Break: checking Unicode length only permits too many canonical UTF-8 bytes.
        self.accepted()
        for characters, accepted in ((63536, True), (63537, False)):
            projection = projection_at_character_count(characters, multibyte_count=1000)
            serialized = encode(projection)
            self.assertLess(len(serialized), 65536)
            self.assertEqual(len(serialized.encode('utf-8')), characters + 2000)
            source = ReplySource._validated(projection)
            self.assertEqual(source.projection_json, serialized)
            if accepted:
                self.assertEqual(source._validate_projection(), projection)
                with self.assertRaises(ValueError): source.target()
            else:
                with self.assertRaises(ValueError): source._validate_projection()

    def test_trusted_texturl_authority_is_known_separate_and_default_off(self):
        # Break: implicit grant, unrecognized explicit grant or dependence on fieldless-Url opt-in.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700); path = root / 'runtime.toml'
            self.assertNotIn('reply_text_url_targets', load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["reply_text_url_targets","reply_formatted_targets"]\n')
            path.chmod(0o600)
            try: policy = load_runtime_policy(path)
            except ConfigurationError as error:
                self.fail('trusted explicit TextUrl authority must be recognized: ' + str(error))
            self.assertEqual(policy.enabled_capabilities, ('reply_formatted_targets', 'reply_text_url_targets'))
        self.assertNotIn('reply_url_targets', self.accepted().required_capabilities)


class TextUrlRaw:
    """Complete synthetic raw transport; no parser/authorization behavior is mocked.

    getMessage returns fresh deep copies; getMe supports account revalidation;
    sendMessage emits pending + success evidence through actual TDLib reducers.
    'lost' exposes posttransport uncertainty immediately rather than a busy wait.
    Any provider method outside the six listed branches fails the test.
    """
    def __init__(self):
        self.source = mixed(); self.requests = []; self.events = deque()
        self.hook = None; self.account = ACCOUNT; self.mode = 'sent'; self.fail_receive = False

    def send(self, request):
        self.requests.append(copy.deepcopy(request)); kind = request['@type']
        if self.hook: self.hook(request)
        if kind == 'getAuthorizationState': result = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe': result = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            if request['chat_id'] != CHAT: raise AssertionError('unexpected recipient')
            result = {'@type': 'chat', 'id': CHAT, 'title': 'Synthetic TextUrl room', 'type': {'@type': 'chatTypePrivate'}}
        elif kind == 'getMessage':
            if (request['chat_id'], request['message_id']) != (CHAT, MESSAGE): raise AssertionError('unexpected anchor')
            result = copy.deepcopy(self.source)
        elif kind == 'getMessageProperties': result = {'@type': 'messageProperties', 'can_be_replied': True}
        elif kind == 'sendMessage':
            incoming = request['input_message_content']
            content = ({'@type': 'messageText', 'text': copy.deepcopy(incoming['text'])}
                       if incoming['@type'] == 'inputMessageText' else media('document', copy.deepcopy(incoming['caption'])))
            shell = {'@type': 'message', 'chat_id': CHAT, 'is_outgoing': True, 'content': content,
                'reply_to': {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': MESSAGE,
                    'checklist_task_id': 0, 'poll_option_id': '', 'origin_send_date': 0}}
            self.events.append({**shell, 'id': -10, 'sending_state': {'@type': 'messageSendingStatePending',
                'sending_id': request['options']['sending_id']}, '@extra': request['@extra']})
            if self.mode == 'lost': self.fail_receive = True; return
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                'message': {**shell, 'id': 701, 'sending_state': None}})
            return
        else: raise AssertionError('source-directed operation forbidden: ' + kind)
        self.events.append({**result, '@extra': request['@extra']})

    def receive(self, timeout):
        if self.events: return self.events.popleft()
        if self.fail_receive:
            self.fail_receive = False
            raise OSError('synthetic posttransport loss')
        return None

    def close(self):
        pass


class TextUrlLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='texturl-'); self.root = Path(self.tmp.name)
        self.raw = TextUrlRaw()
        self.provider = TDLibClient(raw=self.raw, session_directory=self.root / 'unused',
            credential_loader=lambda: self.fail('credentials forbidden'))
        self.dialogs = []; self.decision = lambda: True
        def owner(**facts): self.dialogs.append(copy.deepcopy(facts)); return self.decision()
        store = ArtifactStore(cache_dir=self.root / 'artifacts')
        payload = self.root / 'fixture.txt'; payload.write_bytes(b'data')
        self.broker = Broker(socket_path=self.root / 'unused.sock', client_factory=lambda: self.provider,
            artifact_store=store, approval_prompt=owner, policy=POLICY)
        with self.broker._artifact_authority.scope(CLIENT, store):
            self.artifact = store.store(payload)
        self.stack = ExitStack(); self.staging = self.root / 'staging'; self.history = []; self.registrations = []
        real_claim = self.broker._drafts.claim
        def claim(*args, **kwargs):
            result = real_claim(*args, **kwargs); self.history.append(('claim', result.draft.draft_id)); return result
        self.stack.enter_context(patch.object(self.broker._drafts, 'claim', side_effect=claim))
        for name in ('prepare', 'prepare_text'):
            original = getattr(self.broker._drafts, name)
            def observe(*args, _original=original, _name=name, **kwargs):
                result = _original(*args, **kwargs); self.registrations.append((_name, result.draft_id)); return result
            self.stack.enter_context(patch.object(self.broker._drafts, name, side_effect=observe))
        def stage(claimed):
            result = stage_approved_document(claimed, root=self.staging)
            self.assertTrue(result.is_file()); self.assertEqual(result.read_bytes(), b'data')
            self.history.append(('stage', claimed.draft.draft_id, result)); return result
        def retire(path):
            retire_staged_document(path, root=self.staging); self.history.append(('retire', path))
        self.stack.enter_context(patch('telegram_search_mcp.tdjson.STAGING_ROOT', self.staging))
        self.stack.enter_context(patch('telegram_search_mcp.broker.stage_approved_document', side_effect=stage))
        self.stack.enter_context(patch('telegram_search_mcp.broker.retire_staged_document', side_effect=retire))

    def tearDown(self):
        self.stack.close(); self.broker.shutdown(); self.broker._executor.shutdown(wait=True)
        self.provider.close(); self.tmp.cleanup()

    def dispatch(self, operation, payload, client=CLIENT):
        return self.broker._dispatch({'operation': operation, 'payload': payload, 'client_id': client,
            'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def prepare(self, family):
        payload = {'recipient': CHAT, 'reply_to': {'chat_id': CHAT, 'message_id': MESSAGE}}
        if family == 'text': payload['text'] = 'Acknowledged'; operation = 'prepare_reply_text_send'
        else:
            payload.update(artifact_id=self.artifact.artifact_id, display_name='fixture.txt',
                           mime_type='text/plain', kind='document', caption='Acknowledged')
            operation = 'prepare_reply_artifact_send'
        return self.dispatch(operation, payload)

    def prepared(self, family):
        result = self.prepare(family)
        self.assertEqual(result['status'], 'prepared', 'validated TextUrl must reach a full immutable preview')
        self.assertIn('"provider_type":"textEntityTypeTextUrl"', result['reply']['reply_target']['text'])
        return result['reply']

    def send(self, did, family, approved=True, client=CLIENT):
        return self.dispatch('send_prepared_text' if family == 'text' else 'send_prepared_artifact',
                             {'draft_id': did, 'approved': approved}, client)

    def operation(self, verb, family):
        return verb + '_reply_draft' if family == 'text' else verb + '_reply_artifact_draft'

    def revise(self, did, family, refresh=False):
        return self.dispatch(self.operation('refresh' if refresh else 'update', family),
            {'draft_id': did, **({} if refresh else {'text' if family == 'text' else 'caption': 'Changed'})})

    def denied(self, capability):
        return RuntimePolicy(enabled_capabilities=tuple(cap for cap in POLICY.enabled_capabilities if cap != capability))

    def sends(self):
        return [request for request in self.raw.requests if request['@type'] == 'sendMessage']

    def no_effects(self):
        self.assertEqual(self.sends(), [])
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def assert_late_local_failure(self, did, family, start):
        events = self.history[start:]
        self.assertEqual([event[0] for event in events], ['claim'] if family == 'text' else ['claim', 'stage', 'retire'])
        self.assertEqual(events[0], ('claim', did))
        self.assertIsNone(self.provider._send_observations.snapshot(did))
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
        if family == 'artifact':
            self.assertEqual(events[1][1], did); self.assertEqual(events[2], ('retire', events[1][2]))
            # This deterministic local filesystem permits immediate retirement;
            # production retirement remains best effort with a TTL fallback.
            self.assertFalse(events[1][2].exists())

    def test_exact_preview_hash_binds_hidden_only_changes_and_owner_complete_evidence(self):
        # Break: preview digest drops the hidden string/display or owner gets abbreviated evidence.
        for family in ('text', 'artifact'):
            self.raw.source = message(); preview = self.prepared(family)
            self.assertEqual(preview['reply_target']['text'], PREFIX + EXACT_BODY)
            if family == 'text':
                facts = {'version': 1, 'draft': preview['draft'], 'reply_target': preview['reply_target'],
                    'topic_id': None, 'outgoing_entities': [], 'send_options': SEND_OPTIONS,
                    'link_preview_options': LINK_OPTIONS, 'clear_draft': False, 'reply_markup': None, 'reply_to': REPLY_TO}
            else:
                facts = {'domain': 'telegram-search-mcp.reply-artifact', 'version': 1, 'kind': 'document',
                    'draft': preview['draft'], 'reply_target': preview['reply_target'], 'topic_id': None,
                    'outgoing_entities': [], 'send_options': SEND_OPTIONS, 'reply_markup': None,
                    'reply_to': REPLY_TO, 'content_options': {'thumbnail': None, 'disable_content_type_detection': True}}
            self.assertEqual(preview['preview_sha256'], hashlib.sha256(encode(facts).encode('utf-8')).hexdigest())
            self.raw.source['content']['text']['entities'][0]['type']['url'] = URL + 'changed'
            revised = self.revise(preview['draft']['draft_id'], family, True)['reply']
            self.assertNotEqual(revised['reply_target']['text'], preview['reply_target']['text'])
            self.assertNotEqual(revised['reply_target']['source_sha256'], preview['reply_target']['source_sha256'])
            self.assertNotEqual(revised['preview_sha256'], preview['preview_sha256'])
            self.assertEqual(json.loads(revised['reply_target']['text'][len(PREFIX):])['text'], 'Read 😀 docs')
            self.assertEqual(self.send(preview['draft']['draft_id'], family)['status'], 'expired')
            self.decision = lambda: False
            self.assertEqual(self.send(revised['draft']['draft_id'], family)['status'], 'declined')
            self.assertEqual(self.dialogs[-1]['reply_preview' if family == 'text' else 'reply_artifact_preview'], revised)
        self.assertEqual(self.history, []); self.no_effects()

    def test_missing_every_present_authority_blocks_prepare_and_pending_lifecycle(self):
        # Break: a plural guard checks only TextUrl or silently grants any present-family authority.
        for family in ('text', 'artifact'):
            self.raw.source = mixed()
            for capability in CAPS:
                self.broker._policy = self.denied(capability); before = len(self.registrations)
                self.assertEqual(disabled(self.prepare(family)),('capability_disabled',[capability]))
                self.assertEqual(len(self.registrations), before)
                self.broker._policy = POLICY; did = self.prepared(family)['draft']['draft_id']
                self.broker._policy = self.denied(capability)
                reads = sum(r['@type'] == 'getMessage' for r in self.raw.requests)
                self.assertEqual(disabled(self.dispatch(self.operation('get', family), {'draft_id': did})),('capability_disabled',[capability]))
                self.assertEqual(disabled(self.revise(did, family)),('capability_disabled',[capability]))
                self.assertEqual(sum(r['@type'] == 'getMessage' for r in self.raw.requests), reads)
                # A refresh reads the source once and names what that newly read source needs (0.54).
                self.assertEqual(disabled(self.revise(did, family, True)),('capability_disabled',[capability]))
                self.assertEqual(self.send(did, family)['status'], 'expired')
                self.assertEqual(sum(r['@type'] == 'getMessage' for r in self.raw.requests), reads + 1)
        self.assertEqual(self.dialogs, []); self.assertEqual(self.history, []); self.no_effects()

    def test_every_present_authority_revoked_at_owner_properties_and_immediate_prewire(self):
        # Break: owner-return/final-properties/immediate prewire guard misses a conjunction member.
        for family in ('text', 'artifact'):
            for phase in ('owner', 'properties', 'registration'):
                for capability in CAPS:
                    self.raw.source = mixed(); self.raw.hook = None; self.broker._policy = POLICY; self.decision = lambda: True
                    preview = self.prepared(family); did = preview['draft']['draft_id']; start = len(self.history)
                    denied = self.denied(capability)
                    if phase == 'owner':
                        def revoke(): self.broker._policy = denied; return True
                        self.decision = revoke
                    elif phase == 'properties':
                        self.raw.hook = lambda request: setattr(self.broker, '_policy', denied) if request['@type'] == 'getMessageProperties' else None
                    if phase == 'registration':
                        original = self.provider._send_observations.register
                        def register(*args, **kwargs):
                            result = original(*args, **kwargs); self.broker._policy = denied; return result
                        with patch.object(self.provider._send_observations, 'register', side_effect=register):
                            result = self.send(did, family)
                    else: result = self.send(did, family)
                    self.assertEqual(result['status'], 'expired' if phase == 'owner' else 'failed')
                    if phase == 'owner':
                        self.assertEqual(self.history[start:], [])
                        self.assertIsNone(self.provider._send_observations.snapshot(did))
                    else: self.assert_late_local_failure(did, family, start)
                    self.assertEqual(self.dialogs[-1]['reply_preview' if family == 'text' else 'reply_artifact_preview'], preview)
        self.no_effects()

    def test_hidden_only_drift_at_owner_and_final_provider_read_refuses_before_raw_send(self):
        # Break: real sender's last fresh source read hashes only visible span or sanitized text.
        for family in ('text', 'artifact'):
            for phase in ('owner', 'final_provider_read'):
                for hidden in (' ', '\t', 'Ａ', r'\u202e', '\u202e', 'other'):
                    self.raw.source = simple(''); self.raw.hook = None; self.broker._policy = POLICY; self.decision = lambda: True
                    preview = self.prepared(family); did = preview['draft']['draft_id']; start = len(self.history)
                    def drift():
                        self.raw.source['content']['text']['entities'][0]['type']['url'] = hidden
                        return True
                    if phase == 'owner': self.decision = drift
                    else: self.raw.hook = lambda request: drift() if request['@type'] == 'getMessage' else None
                    self.assertEqual(self.send(did, family)['status'], 'failed')
                    self.assert_late_local_failure(did, family, start)
                    current = source_from_message(self.raw.source, CHAT, MESSAGE)
                    self.assertEqual(current.raw_text, 'x')
                    self.assertNotEqual(current.source_sha256, preview['reply_target']['source_sha256'])
                    self.assertNotEqual(current.target().text, preview['reply_target']['text'])
                    self.assertEqual(self.send(did, family)['status'], 'failed')
        self.no_effects()

    def test_update_snapshot_and_refresh_require_the_new_sources_full_authority(self):
        # Break: update rereads provider state, refresh drops new-source authority, or old approval survives.
        legacy_policy = RuntimePolicy(enabled_capabilities=BASE)
        for family in ('text', 'artifact'):
            self.raw.source = mixed(); self.broker._policy = POLICY; self.decision = lambda: True
            original = self.prepared(family); did = original['draft']['draft_id']
            self.assertEqual(self.dispatch(self.operation('get', family), {'draft_id': did})['reply'], original)
            reads = sum(r['@type'] == 'getMessage' for r in self.raw.requests)
            self.raw.source = message('plain', [])
            updated = self.revise(did, family)['reply']
            self.assertEqual(updated['reply_target'], original['reply_target'])
            self.assertEqual(sum(r['@type'] == 'getMessage' for r in self.raw.requests), reads)
            self.assertEqual(self.send(did, family)['status'], 'expired')
            # A refresh needs only what the newly read source needs, never the stored source's families (0.54).
            self.broker._policy = legacy_policy
            legacy = self.revise(updated['draft']['draft_id'], family, True)['reply']
            self.broker._policy = POLICY
            self.assertNotEqual(legacy['reply_target']['source_sha256'], original['reply_target']['source_sha256'])
            self.assertEqual(legacy['reply_target']['text'], '[untrusted Telegram evidence] plain')
            self.assertEqual(self.send(updated['draft']['draft_id'], family)['status'], 'expired')
            self.raw.source = mixed()
            for capability in CAPS:
                self.broker._policy = self.denied(capability)
                self.assertEqual(disabled(self.revise(legacy['draft']['draft_id'], family, True)),('capability_disabled',[capability]))
            self.broker._policy = POLICY
            fresh = self.revise(legacy['draft']['draft_id'], family, True)['reply']
            self.assertEqual(fresh['reply_target'], original['reply_target'])
            self.assertNotEqual(fresh['preview_sha256'], original['preview_sha256'])
            self.assertEqual(self.send(legacy['draft']['draft_id'], family)['status'], 'expired')
            self.assertEqual(self.send(fresh['draft']['draft_id'], family, False)['status'], 'declined')
            self.decision = lambda: False
            self.assertEqual(self.send(fresh['draft']['draft_id'], family)['status'], 'declined')
        self.assertEqual(self.history, []); self.no_effects()

    def test_texturl_only_uses_its_minimum_authority_without_fieldless_url_or_media_optin(self):
        # Break: hidden URL syntax implicitly requires an absent family or caption/media authority.
        for family in ('text', 'artifact'):
            self.raw.source = message(); self.decision = lambda: True
            self.broker._policy = RuntimePolicy(enabled_capabilities=BASE + CAPS[:2])
            preview = self.prepared(family)
            self.assertEqual(preview['reply_target']['text'], PREFIX + EXACT_BODY)
            self.assertEqual(self.send(preview['draft']['draft_id'], family)['status'], 'sent')
            self.assertEqual(self.sends()[-1]['input_message_content']['text' if family == 'text' else 'caption'],
                             {'@type': 'formattedText', 'text': 'Acknowledged', 'entities': []})

    def test_closed_source_never_mutates_real_draft_registry(self):
        # Break: parser/capability failure creates a draft before complete evidence validation.
        self.raw.source = message(); self.prepared('text')
        for family in ('text', 'artifact'):
            for mutation in ('extra', 'bad_hidden', 'bad_offset', 'caption', 'oversize'):
                raw = message()
                if mutation == 'extra': raw['content']['text']['entities'][0]['type']['extra'] = None
                elif mutation == 'bad_hidden': raw['content']['text']['entities'][0]['type']['url'] = False
                elif mutation == 'bad_offset': raw['content']['text']['entities'][0]['offset'] = 6
                elif mutation == 'caption':
                    # 0.58 admits a TextUrl caption (v24); one past the complete display bound stays closed.
                    raw['content'] = media('document', raw['content']['text'])
                    raw['content']['caption']['entities'][0]['type']['url'] = 'x' * 4096
                else: raw['content']['text']['entities'][0]['type']['url'] = 'x' * 4096
                self.raw.source = raw; before = list(self.registrations)
                self.assertEqual(self.prepare(family)['status'], 'unavailable')
                self.assertEqual(self.registrations, before)
        self.assertEqual(self.history, []); self.no_effects()

    def test_owner_account_client_provider_epoch_and_generation_binding(self):
        # Break: TextUrl source evidence escapes its exact account/client/provider/broker lifetime.
        for family in ('text', 'artifact'):
            self.raw.source = mixed(); self.broker._policy = POLICY; self.decision = lambda: True
            preview = self.prepared(family); did = preview['draft']['draft_id']
            self.assertEqual(self.dispatch(self.operation('get', family), {'draft_id': did}, FOREIGN)['status'], 'unavailable')
            self.assertEqual(self.send(did, family, client=FOREIGN)['status'], 'expired')
            self.raw.account = ACCOUNT + 1
            self.assertEqual(self.dispatch(self.operation('get', family), {'draft_id': did})['status'], 'unavailable')
            self.raw.account = ACCOUNT
            if family == 'artifact':
                # A provider observation permanently revokes the original draft;
                # recovery cannot turn that old approval into another send.
                count = len(self.sends())
                self.assertEqual(self.send(did, family)['status'], 'expired')
                self.assertEqual(self.dispatch(self.operation('get', family), {'draft_id': did})['status'], 'unavailable')
                self.assertEqual(len(self.sends()), count)
                fresh = self.root / 'fresh-fixture.txt'; fresh.write_bytes(b'data')
                with self.broker._artifact_authority.scope(CLIENT, self.broker._artifact_store):
                    self.artifact = self.broker._artifact_store.store(fresh)
                replacement = self.prepared(family)['draft']['draft_id']
                self.assertNotEqual(replacement, did); did = replacement
            owner = DraftOwner(CLIENT, ACCOUNT)
            for wrong in (DraftOwner(FOREIGN, ACCOUNT), DraftOwner(CLIENT, ACCOUNT + 1)):
                with self.assertRaises(DraftError): self.broker._drafts.source_required_capabilities(did, owner=wrong)
            self.assertEqual(self.send(did, family)['status'], 'sent'); before = len(self.sends())
            epoch = self.provider.send_observation_epoch; self.provider._send_observation_epoch = object()
            self.assertEqual(self.send(did, family)['status'], 'expired')
            self.provider._send_observation_epoch = epoch
            replacement = TDLibClient(raw=TextUrlRaw(), session_directory=self.root / 'other-unused',
                                      credential_loader=lambda: self.fail('credentials forbidden'))
            original = self.broker._client; self.broker._client = replacement
            try: self.assertEqual(self.send(did, family)['status'], 'expired')
            finally: self.broker._client = original; replacement.close()
            with self.assertRaises(CompatibilityError):
                self.broker._dispatch({'operation': self.operation('get', family), 'payload': {'draft_id': did},
                    'client_id': CLIENT, 'deadline': time.monotonic() + 10, 'broker_generation': 'wrong-generation'})
            self.assertEqual(len(self.sends()), before)

    def test_plain_exact_wire_and_terminal_replay_require_all_present_authorities(self):
        # Break: hidden strings trigger URL methods, change recipient/options/outgoing entities or resend.
        for family in ('text', 'artifact'):
            self.broker._policy = POLICY; self.raw.source = mixed(); self.decision = lambda: True
            preview = self.prepared(family); did = preview['draft']['draft_id']
            self.assertEqual(self.send(did, family)['status'], 'sent')
            self.assertEqual(self.dialogs[-1]['reply_preview' if family == 'text' else 'reply_artifact_preview'], preview)
            wire = self.sends()[-1]
            self.assertEqual(wire['chat_id'], CHAT); self.assertEqual(wire['reply_to'], REPLY_TO)
            self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
            self.assertEqual({k: v for k, v in wire['options'].items() if k != 'sending_id'}, SEND_OPTIONS)
            self.assertIs(type(wire['options']['sending_id']), int)
            incoming = wire['input_message_content']
            self.assertEqual(incoming['text' if family == 'text' else 'caption'],
                             {'@type': 'formattedText', 'text': 'Acknowledged', 'entities': []})
            if family == 'text':
                self.assertEqual(incoming['link_preview_options'], LINK_OPTIONS)
                self.assertIs(incoming['clear_draft'], False)
            else:
                self.assertEqual(incoming['@type'], 'inputMessageDocument')
                self.assertTrue(incoming['document']['disable_content_type_detection'])
                self.assertIsNone(incoming['document']['thumbnail'])
                staged = Path(incoming['document']['document']['path'])
                self.assertEqual(staged.parent.parent, self.staging)
                self.assertEqual(staged.parent.name, did)
                self.assertEqual(staged.name, 'fixture.txt')
            before = len(self.sends())
            for capability in CAPS:
                self.broker._policy = self.denied(capability)
                self.assertEqual(self.send(did, family)['status'], 'expired')
            self.broker._policy = POLICY
            self.assertEqual(self.send(did, family)['status'], 'sent')
            self.assertEqual(len(self.sends()), before)
        self.assertTrue(all(r['@type'] in ('getAuthorizationState', 'getMe', 'getChat', 'getMessage', 'getMessageProperties', 'sendMessage')
                            for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def test_malicious_hidden_syntax_stays_inert_through_owner_and_real_plain_send(self):
        # Break: an admitted provider string gets decoded into a URL action or outgoing formatting.
        malicious = ('javascript:alert(1)', 'tg://user?id=42', 'mailto:a@b.invalid',
                     '<b>[go](https://x.invalid)</b>', 'secret\x00\x85\u202e', r'secret\u202e')
        escaped_leaks = (
            r'javascript\u003aalert\u00281\u0029', r'tg\u003a\u002f\u002fuser\u003fid\u003d42',
            r'mailto\u003aa\u0040b\u002einvalid',
            r'\u003cb\u003e\u005bgo\u005d\u0028https\u003a\u002f\u002fx\u002einvalid\u0029\u003c\u002fb\u003e',
            r'secret\u0000\u0085\u202e', r'secret\u005cu202e',
        )
        logs = io.StringIO(); handler = logging.StreamHandler(logs)
        loggers = (logging.getLogger(), logging.getLogger('telegram_search_mcp'))
        original_states = tuple((logger, logger.level, logger.disabled, logger.propagate,
                                 tuple(logger.handlers)) for logger in loggers)
        try:
            handler.setLevel(logging.DEBUG)
            for logger in loggers:
                logger.setLevel(logging.DEBUG); logger.disabled = False
                logger.propagate = False; logger.handlers[:] = [handler]
            for family in ('text', 'artifact'):
                for hidden in malicious:
                    self.raw.source = simple(hidden); self.broker._policy = POLICY; self.decision = lambda: True
                    preview = self.prepared(family)
                    self.assertEqual(json.loads(preview['reply_target']['text'][len(PREFIX):])['entities'][0]['provider_url'], hidden)
                    self.assertEqual(self.send(preview['draft']['draft_id'], family)['status'], 'sent')
                    self.assertEqual(self.dialogs[-1]['reply_preview' if family == 'text' else 'reply_artifact_preview'], preview)
                    self.assertEqual(self.sends()[-1]['input_message_content']['text' if family == 'text' else 'caption'],
                                     {'@type': 'formattedText', 'text': 'Acknowledged', 'entities': []})
            for hidden in malicious: self.assertNotIn(hidden, logs.getvalue())
            for escaped in escaped_leaks: self.assertNotIn(escaped, logs.getvalue())
        finally:
            for logger, level, disabled, propagate, handlers in original_states:
                logger.handlers[:] = handlers; logger.setLevel(level)
                logger.disabled = disabled; logger.propagate = propagate
            handler.close()
        self.assertTrue(all(r['@type'] in ('getAuthorizationState', 'getMe', 'getChat', 'getMessage', 'getMessageProperties', 'sendMessage')
                            for r in self.raw.requests))

    def test_preview_models_refuse_hidden_display_anchor_or_digest_forgery(self):
        # Break: preview validation accepts a display/anchor edit under the previous approval digest.
        from telegram_search_mcp.reply_drafts import ReplyDraftPreview
        from telegram_search_mcp.reply_artifact_drafts import ReplyArtifactDraftPreview
        for family, model in (('text', ReplyDraftPreview), ('artifact', ReplyArtifactDraftPreview)):
            self.raw.source = message(); preview = self.prepared(family)
            for mutation in ('hidden', 'source_hash', 'anchor', 'preview_hash'):
                forged = copy.deepcopy(preview)
                if mutation == 'hidden': forged['reply_target']['text'] = forged['reply_target']['text'].replace('example', 'different')
                elif mutation == 'source_hash': forged['reply_target']['source_sha256'] = '0' * 64
                elif mutation == 'anchor': forged['reply_target']['anchor']['message_id'] = MESSAGE + 1
                else: forged['preview_sha256'] = '0' * 64
                with self.subTest(family=family, mutation=mutation), self.assertRaises(ValueError):
                    model.model_validate(forged)
        self.assertEqual(self.history, []); self.no_effects()

    def test_posttransport_uncertainty_never_retries_and_replay_requires_authority(self):
        # Break: uncertain outgoing transport gets retried or gated source is exposed on replay.
        for family in ('text', 'artifact'):
            self.raw.mode = 'lost'; self.raw.source = mixed(); self.broker._policy = POLICY; self.decision = lambda: True
            preview = self.prepared(family); did = preview['draft']['draft_id']; before = len(self.sends())
            self.assertEqual(self.send(did, family)['status'], 'outcome_unknown')
            self.assertIsNotNone(self.provider._send_observations.snapshot(did))
            self.assertNotEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_failed')
            for capability in CAPS:
                self.broker._policy = self.denied(capability)
                self.assertEqual(self.send(did, family)['status'], 'expired')
            self.broker._policy = POLICY
            self.assertEqual(self.send(did, family)['status'], 'outcome_unknown')
            self.assertEqual(len(self.sends()), before + 1)
