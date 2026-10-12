"""Absolute captions: literal wire/evidence oracles and closed provider fixtures.

No native TDLib, installed owner runtime, credentials, network or Telegram.
Mutations caught: missing media Absolute opt-in/version/authority, discarded
precision evidence, weakened malformed/legacy boundaries and changed bounds.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import unittest

from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_media_sources import media_content, validate_content, render_content
from test_reply_datetime_media import (
    CHAT, MESSAGE, MARKER, BASE_CAPS, caption, entity, message,
    expected_projection, stable_media, wire_json,
)

DATE = 'textEntityTypeDateTime'
ABSOLUTE = 'dateTimeFormattingTypeAbsolute'
RELATIVE = {'@type': 'dateTimeFormattingTypeRelative'}
PRECISIONS = ('dateTimePartPrecisionNone', 'dateTimePartPrecisionShort', 'dateTimePartPrecisionLong')
KINDS = ('document', 'photo', 'voice_note')


def absolute(time=PRECISIONS[1], date=PRECISIONS[2], weekday=False):
    return {'@type': ABSOLUTE, 'time_precision': {'@type': time},
            'date_precision': {'@type': date}, 'show_day_of_week': weekday}


def span(offset=2, length=5, stamp=-1, formatting=None):
    return entity(offset, length, unix_time=stamp,
                  formatting_type=absolute() if formatting is None else copy.deepcopy(formatting))


def provider(kind='document', formatted=None):
    return message(kind, caption(entities=[span()]) if formatted is None else formatted)


def projection(kind='document', formatted=None, version=14):
    return expected_projection(kind, caption(entities=[span()]) if formatted is None else formatted, version)


def record(text='today', offset=2, length=5, stamp=-1, formatting=None):
    return {'type': 'date_time', 'offset': offset, 'length': length, 'text': text,
            'provider_type': DATE, 'unix_time': stamp,
            'formatting_type': absolute() if formatting is None else copy.deepcopy(formatting)}


def evidence(kind='document', text='😀today', records=None):
    return MARKER + 'Media target: ' + wire_json({'kind': kind,
        'caption': {'text': text, 'offset_basis': 'original source UTF-16 code units',
                    'entities': [record()] if records is None else records}, 'media': stable_media(kind)})


def file_of(raw, kind):
    content = raw['content']
    return (content['document']['document'] if kind == 'document' else
            content['photo']['sizes'][0]['photo'] if kind == 'photo' else content['voice_note']['voice'])


class AbsoluteCaptionTests(unittest.TestCase):
    def source(self, raw=None):
        try:
            return source_from_message(provider() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            if str(error) != 'invalid formatted reply date time': raise
            self.fail('complete explicitly specified Absolute caption must be admitted')

    def assert_source(self, source, want):
        literal = wire_json(want)
        self.assertEqual(source.projection_json, literal)
        self.assertEqual(source.source_sha256, hashlib.sha256(literal.encode('utf-8')).hexdigest())
        self.assertEqual(ReplySource(literal).target(), source.target())

    def test_three_media_kinds_all_18_combinations_bind_exact_bytes_digest_escaped_display(self):
        # Break: rejecting None/None, false weekday, missing nested facts or wrong version.
        for kind, time, date, weekday in itertools.product(KINDS, PRECISIONS, PRECISIONS, (False, True)):
            with self.subTest(kind=kind, time=time, date=date, weekday=weekday):
                fmt = absolute(time, date, weekday)
                formatted = caption('😀"soon"', [span(2, 6, 1712345678, fmt)])
                source = self.source(provider(kind, formatted))
                self.assert_source(source, projection(kind, formatted))
                self.assertEqual(source.target().text, evidence(kind, '😀"soon"',
                    [record('"soon"', 2, 6, 1712345678, fmt)]))
                self.assertIn('\\"soon\\"', source.target().text)
                self.assertTrue(source.is_media)
                self.assertEqual(source.required_capabilities, BASE_CAPS)
                self.assertFalse(source.target().truncated)
                self.assertFalse(source.target().sanitized)

    def test_mixed_null_relative_absolute_style_and_conditional_lexical_identity(self):
        # Break: wrong precedence or lost conditional authority from adjacent entities.
        omitted = entity(0, 1, unix_time=1)
        null = entity(1, 1, unix_time=2, formatting_type=None)
        rel = entity(2, 1, unix_time=3, formatting_type=RELATIVE)
        abs_span = span(3, 1, 4)
        bold = entity(4, 1, 'textEntityTypeBold')
        lexical = entity(5, 4, 'textEntityTypeHashtag')
        identity = entity(9, 5, 'textEntityTypeMentionName', user_id=53)
        for kind, lex, ident in itertools.product(KINDS, (False, True), (False, True)):
            with self.subTest(kind=kind, lex=lex, ident=ident):
                extras = ([lexical] if lex else []) + ([identity] if ident else [])
                source = self.source(provider(kind, caption('NNRAS#tag@name', list(reversed(
                    [omitted, null, rel, abs_span, bold, *extras])))))
                canonical = [entity(0, 1, unix_time=1, formatting_type=None), null, rel, abs_span, bold, *extras]
                self.assert_source(source, projection(kind, caption('NNRAS#tag@name', canonical)))
                caps = BASE_CAPS + (('reply_lexical_targets',) if lex else ()) + (('reply_identity_targets',) if ident else ())
                self.assertEqual(source.required_capabilities, caps)
                records = [{'type': 'date_time', 'offset': i, 'length': 1, 'text': text,
                    'provider_type': DATE, 'unix_time': i + 1, 'formatting_type': fmt}
                    for i, text, fmt in ((0, 'N', None), (1, 'N', None), (2, 'R', RELATIVE), (3, 'A', absolute()))]
                records += [{'type': 'bold', 'offset': 4, 'length': 1, 'text': 'S'}]
                if lex: records += [{'type': 'hashtag', 'offset': 5, 'length': 4, 'text': '#tag'}]
                if ident: records += [{'type': 'mention_name', 'offset': 9, 'length': 5, 'text': '@name', 'user_id': 53}]
                self.assertEqual(source.target().text, evidence(kind, 'NNRAS#tag@name', records))

    def test_original_utf16_signed_int32_sanitation_and_nested_immutability(self):
        # Break: sanitizing before UTF16 slicing, unsigned time or escaping mutable aliases.
        for kind in KINDS:
            with self.subTest(kind=kind):
                raw = provider(kind, caption('😀Ａ\t🕒B', [span(4, 3, 2147483647), span(0, 3, -2147483648)]))
                source = self.source(raw)
                self.assert_source(source, projection(kind, caption('😀Ａ\t🕒B',
                    [span(0, 3, -2147483648), span(4, 3, 2147483647)])))
                self.assertEqual(source.target().text, evidence(kind, '😀A 🕒B',
                    [record('😀A', 0, 3, -2147483648), record('🕒B', 4, 3, 2147483647)]))
                self.assertTrue(source.target().sanitized)
                before = source.projection_json, source.target(), source.source_sha256
                raw['content']['caption']['entities'][0]['type']['formatting_type']['time_precision']['extra'] = None
                raw['content']['caption']['entities'].clear()
                self.assertEqual((source.projection_json, source.target(), source.source_sha256), before)

    def test_legacy_v10_v12_keep_exact_bytes_and_authority_and_v13_text_unchanged(self):
        # Break: v14 chosen without validated Absolute evidence.
        for kind in KINDS:
            for fmt, version in ((None, 10), (RELATIVE, 12)):
                formatted = caption(entities=[entity(unix_time=-1, formatting_type=fmt)])
                source = source_from_message(message(kind, formatted), CHAT, MESSAGE)
                self.assert_source(source, expected_projection(kind, formatted, version))
                self.assertEqual(source.required_capabilities, BASE_CAPS)
        from test_reply_absolute_datetime_targets import message as text_message, projection as text_projection, encode, CHAT as TCHAT, MESSAGE as TMESSAGE, span as text_span
        source = source_from_message(text_message(), TCHAT, TMESSAGE)
        self.assertEqual(source.projection_json, encode(text_projection('soon', [text_span()])))
        self.assertEqual(source.required_capabilities, ('reply_formatted_targets', 'reply_datetime_targets'))
        self.assertFalse(source.is_media)

    def test_default_off_media_helpers_refuse_absolute_and_opted_in_path_renders(self):
        # Break: default widening or a dropped option along media validation/render path.
        raw = provider()['content']; stored = projection()['message']['content']
        for operation in (lambda: media_content(raw, datetime=True, datetime_relative=True),
                          lambda: validate_content(stored, datetime=True, datetime_relative=True),
                          lambda: render_content(stored, MARKER, datetime=True, datetime_relative=True)):
            with self.assertRaises(ValueError): operation()
        source = self.source()
        # These calls follow a positive assertion so RED cannot become a setup TypeError.
        self.assertEqual(media_content(raw, datetime=True, datetime_absolute=True), stored)
        validate_content(stored, datetime=True, datetime_absolute=True)
        self.assertEqual(render_content(stored, MARKER, datetime=True, datetime_absolute=True), (source.target().text, False))

    def test_32_entities_and_1024_scalar_caption_bound_accept_without_truncation(self):
        # Break: widening/narrowing existing count/scalar bounds at Absolute selection.
        for kind in KINDS:
            fmt = caption('x' * 32, [span(0, 1), *[entity(i, 1, 'textEntityTypeBold') for i in range(1, 32)]])
            source = self.source(provider(kind, fmt))
            self.assert_source(source, projection(kind, fmt))
            self.assertFalse(source.target().truncated)
            fmt = caption('x' * 1024, [span(0, 1)])
            source = self.source(provider(kind, fmt))
            self.assert_source(source, projection(kind, fmt))
            self.assertFalse(source.target().truncated)

    def test_stable_media_identity_bound_private_volatile_facts_excluded(self):
        # Break: caption support leaks or hashes transient provider paths/state.
        for kind in KINDS:
            raw = provider(kind); original = self.source(raw)
            file = file_of(raw, kind)
            file['id'] = 999; file['expected_size'] = 999
            file['remote']['id'] = 'private-transient'
            file['local'].update(path='/synthetic/private', downloaded_size=5, is_downloading_active=True)
            unchanged = self.source(raw)
            self.assertEqual(original.projection_json, unchanged.projection_json)
            for private in ('private', 'provider-private', 'synthetic-raw-file-id', '"path"', '"id":999'):
                self.assertNotIn(private, unchanged.projection_json)
                self.assertNotIn(private, unchanged.target().text)
            file['remote']['unique_id'] = 'changed'
            self.assertNotEqual(original.source_sha256, self.source(raw).source_sha256)
            file['size'] += 1
            self.assertNotEqual(unchanged.source_sha256, self.source(raw).source_sha256)


class AbsoluteCaptionRejections(unittest.TestCase):
    def reject(self, formatted):
        for kind in KINDS:
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError): source_from_message(provider(kind, formatted), CHAT, MESSAGE)
                with self.assertRaises(ValueError): ReplySource(wire_json(projection(kind, formatted)))

    def test_malformed_explicit_absolute_fields_precisions_boolean_and_open_shapes_refuse(self):
        # Break: coercions/null/partial subset, unknown constructors or ignored nested fields.
        malformed = [False, 0, '', [], {}, {'@type': ABSOLUTE}, {'@type': ABSOLUTE, 'format': 'yyyy'}]
        for key in ('time_precision', 'date_precision', 'show_day_of_week'):
            fmt = absolute(); del fmt[key]; malformed.append(fmt)
            fmt = absolute(); fmt[key] = None; malformed.append(fmt)
        for key in ('time_precision', 'date_precision'):
            for value in ({}, {'@type': 'unknown'}, {'@type': None}, {'@type': PRECISIONS[0], 'extra': None},
                          {'@type': PRECISIONS[0], 'format': 'yyyy'}, PRECISIONS[0], 0, False, []):
                fmt = absolute(); fmt[key] = value; malformed.append(fmt)
        for value in (0, 1, 'true', 'false', 1.0, {}, []):
            fmt = absolute(); fmt['show_day_of_week'] = value; malformed.append(fmt)
        fmt = absolute(); fmt['extra'] = None; malformed.append(fmt)
        for fmt in malformed:
            with self.subTest(format=fmt): self.reject(caption(entities=[span(formatting=fmt)]))

    def test_timestamp_span_overlap_adjacent_entity_and_full_evidence_bounds_refuse(self):
        # Break: Absolute bypasses shared strict int32/spans/overlap/count/full display gates.
        for stamp in (True, False, None, '-1', -1.0, -2147483649, 2147483648, [], {}):
            self.reject(caption(entities=[span(stamp=stamp)]))
        for offset, length in ((1, 1), (0, 1), (2, 0), (-1, 5), (True, 5), (2, True), (2, 6), (2147483648, 1)):
            self.reject(caption(entities=[span(offset, length)]))
        for other in (span(), entity(2, 5, 'textEntityTypeBold'), entity(3, 1, 'textEntityTypeItalic'),
                      entity(0, 7, 'textEntityTypeBlockQuote'), entity(3, 4, 'textEntityTypeHashtag'),
                      entity(3, 4, 'textEntityTypeMentionName', user_id=53)):
            self.reject(caption(entities=[span(), other]))
        for other in (entity(8, 4, 'textEntityTypeMentionName', user_id=True),
                      entity(8, 4, 'textEntityTypeUrl', extra=None), entity(8, 4, 'textEntityTypeBold', ignored=True)):
            self.reject(caption('😀today next', [span(), other]))
        for change in ('caption', 'entity', 'tag', 'many', 'scalar', 'nul', 'surrogate', 'escaping'):
            bad = caption(entities=[span()])
            if change == 'caption': bad['ignored'] = True
            elif change == 'entity': bad['entities'][0]['ignored'] = True
            elif change == 'tag': bad['entities'][0]['@type'] = 'notTextEntity'
            elif change == 'many': bad = caption('x' * 33, [span(i, 1) for i in range(33)])
            elif change == 'scalar': bad['text'] = 'x' * 1025
            elif change == 'nul': bad['text'] += '\x00'
            elif change == 'surrogate': bad['text'] += '\ud800'
            else: bad = caption('"' * 1024, [span(0, 1024)])
            self.reject(bad)

    def test_forged_legacy_versions_v14_without_absolute_and_text_v14_refuse(self):
        # Break: laundering evidence via legacy authority or v14 without validated presence.
        for kind in KINDS:
            for version in (*range(1, 14), True, 14.0, '14', 15):
                with self.subTest(kind=kind, version=version), self.assertRaises(ValueError):
                    ReplySource(wire_json(projection(kind, version=version)))
            for entities in ([], [entity()], [entity(unix_time=-1, formatting_type=None)],
                             [entity(unix_time=-1, formatting_type=RELATIVE)], [entity(2, 5, 'textEntityTypeBold')]):
                with self.subTest(kind=kind, entities=entities), self.assertRaises(ValueError):
                    ReplySource(wire_json(projection(kind, caption(entities=entities))))
        from test_reply_absolute_datetime_targets import projection as text_projection, span as text_span
        with self.assertRaises(ValueError): ReplySource(wire_json(text_projection('soon', [text_span()], 14)))
        oversized = wire_json(projection(formatted=caption('x' * 66000, [span(0, 1)])))
        with self.assertRaises(ValueError): ReplySource(oversized)

    def test_media_shape_identity_and_stored_canonicality_stay_closed(self):
        # Break: newly selected v14 bypasses established media constraints or canonical JSON.
        for kind in KINDS:
            for change in ('identity', 'size', 'extra', 'unsafe'):
                raw = provider(kind)
                if change == 'identity': file_of(raw, kind)['remote']['unique_id'] = ''
                elif change == 'size': file_of(raw, kind)['size'] = 0
                elif change == 'extra': raw['content']['ignored'] = None
                elif kind == 'document': raw['content']['document']['file_name'] += '\x00'
                elif kind == 'photo': raw['content']['has_spoiler'] = True
                else: raw['content']['voice_note']['mime_type'] = 'audio/mp3'
                with self.subTest(kind=kind, change=change), self.assertRaises(ValueError):
                    source_from_message(raw, CHAT, MESSAGE)
            for change in ('extra', 'media', 'anchor', 'raw', 'kind', 'missing_null'):
                bad = projection(kind)
                if change == 'extra': bad['ignored'] = True
                elif change == 'media': bad['message']['content']['media']['path'] = '/private'
                elif change == 'anchor': bad['anchor']['message_id'] += 1
                elif change == 'raw': bad['message']['content'] = provider(kind)['content']
                elif change == 'kind': bad['message']['content']['kind'] = 'video'
                else: del bad['message']['reply_to']
                with self.subTest(kind=kind, change=change), self.assertRaises(ValueError): ReplySource(wire_json(bad))
        with self.assertRaises(ValueError): ReplySource(json.dumps(projection(), ensure_ascii=False))
