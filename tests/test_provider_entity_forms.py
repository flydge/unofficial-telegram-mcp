"""Review fixes (057, medium and low): the entity forms the pinned TDLib (d1085f9) returns for a styled text.

`fix_entities` (td/telegram/MessageEntity.cpp) runs on every message TDLib returns: `split_entities` cuts a bold,
italic, underline, strikethrough or spoiler span at the start of every style nested in it and at both ends of every
auto-detected entity inside it, so one caller span can come back as touching pieces. The expected forms below are the
literal outputs pinned by TDLib's own unit tests (test/message_entities.cpp, fix_formatted_text) or traced by hand
through split_entities; nothing here derives them from the code under test. The media timestamp grammar is TDLib's
`find_media_timestamps` test, case by case.
"""
from __future__ import annotations

import unittest

from telegram_search_mcp.edit_drafts import exact_styles
from telegram_search_mcp.outgoing_formatting import joined_outgoing_entities, media_timestamp_tokens

B, I, U, S, P = ('textEntityTypeBold', 'textEntityTypeItalic', 'textEntityTypeUnderline',
                 'textEntityTypeStrikethrough', 'textEntityTypeSpoiler')
MENTION, URL, STAMP = 'textEntityTypeMention', 'textEntityTypeUrl', 'textEntityTypeMediaTimestamp'


def span(kind, offset, length):
    return {'offset': offset, 'length': length, 'type': {'@type': kind}}


def entity(kind, offset, length, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': kind, **fields}}


# (text, the caller's canonical spans, TDLib's returned entities)
TDLIB_FORMS = (
    # test/message_entities.cpp: Underline(0,2) + Strikethrough(1,1) on "ab".
    ('ab', ((0, 2, U), (1, 1, S)), [entity(U, 0, 1), entity(U, 1, 1), entity(S, 1, 1)]),
    # The same file: Strikethrough(0,2) + Underline(1,1).
    ('ab', ((0, 2, S), (1, 1, U)), [entity(S, 0, 1), entity(U, 1, 1), entity(S, 1, 1)]),
    # The same file: Italic(0,13) on "@tests @tests", cut around both mentions.
    ('@tests @tests', ((0, 13, I),), [entity(MENTION, 0, 6), entity(I, 0, 6), entity(I, 6, 1),
                                      entity(MENTION, 7, 6), entity(I, 7, 6)]),
    # The same file: a nested span starting with its parent is not cut (Underline(0,2) + Spoiler(0,1)).
    ('ab', ((0, 2, U), (0, 1, P)), [entity(U, 0, 2), entity(P, 0, 1)]),
    # Traced through split_entities: Bold(0,11) + Italic(6,5) on "Hello there".
    ('Hello there', ((0, 11, B), (6, 5, I)), [entity(B, 0, 6), entity(B, 6, 5), entity(I, 6, 5)]),
    # Traced: Bold(0,15) over the link in "see example.com".
    ('see example.com', ((0, 15, B),), [entity(B, 0, 4), entity(URL, 4, 11), entity(B, 4, 11)]),
)


class JoinedPiecesTests(unittest.TestCase):
    def test_tdlib_split_forms_join_to_the_caller_spans(self):
        for text, styles, returned in TDLIB_FORMS:
            with self.subTest(text=text, styles=styles):
                pieces = [{key: row[key] for key in ('offset', 'length', 'type')} for row in returned
                          if row['type']['@type'] in (B, I, U, S, P)]
                self.assertEqual(joined_outgoing_entities(text, pieces), styles)
                self.assertTrue(exact_styles(text, returned, styles))
                # The echoed request (what the fakes returned before) still reads as the edit.
                self.assertTrue(exact_styles(text, [entity(kind, offset, length) for offset, length, kind in styles],
                                             styles))

    def test_pieces_that_are_not_the_caller_span_never_join(self):
        styles = ((0, 11, B), (6, 5, I))
        for returned in (
                [entity(B, 0, 6), entity(B, 5, 6), entity(I, 6, 5)],  # overlapping pieces of one style
                [entity(B, 0, 6), entity(B, 6, 5), entity(B, 6, 5), entity(I, 6, 5)],  # a duplicated piece
                [entity(B, 0, 5), entity(B, 6, 5), entity(I, 6, 5)],  # a gap: two spans, not the caller's one
                [entity(B, 0, 6), entity(B, 6, 4), entity(I, 6, 5)],  # one piece short
                [entity(B, 0, 6), entity(B, 6, 5)],  # the nested style missing
                [entity(B, 0, 6), entity(B, 6, 5), entity(I, 6, 5), entity(U, 0, 1)],  # an added style
                [entity(B, 0, 6), entity(B, 6, 5, extra=1), entity(I, 6, 5)]):  # a piece with a field
            with self.subTest(returned=returned):
                self.assertFalse(exact_styles('Hello there', returned, styles))
        # Two separate caller spans never come back merged, and a piece never ends inside a surrogate pair.
        self.assertFalse(exact_styles('Hello there', [entity(B, 0, 11)], ((0, 5, B), (6, 5, B))))
        self.assertFalse(exact_styles('A\U0001f642B', [entity(B, 0, 2), entity(B, 2, 2)], ((0, 4, B),)))
        with self.assertRaises(ValueError):
            joined_outgoing_entities('Hello there', [span(B, 0, 6), span(B, 5, 6)])


class MediaTimestampGrammarTests(unittest.TestCase):
    """TDLib's own find_media_timestamps cases (test/message_entities.cpp, media_timestamp)."""

    CASES = (
        ('', []), (':', []), (':1', []), ('a:1', []), ('01', []), ('01:', []), ('01::', []), ('a1:1a', []),
        ('a1::01a', []), ('2001:db8::8a2e:f70:13a4', []), ('0:00', [('0:00', 0)]), ('+0:00', [('0:00', 0)]),
        ('0:00+', [('0:00', 0)]), ('a0:00', []), ('0:00a', []), ('б0:00', []), ('0:00б', []), ('_0:00', []),
        ('0:00_', []), ('00:00:00:00', []), ('1:1:01 1:1:1', [('1:1:01', 3661)]),
        ('0:0:00 00:00 000:00 0000:00 00000:00 00:00:00 000:00:00 00:000:00 00:00:000',
         [('0:0:00', 0), ('00:00', 0), ('000:00', 0), ('0000:00', 0), ('00:00:00', 0)]),
        ('00:0:00 0:00:00 00::00 :00:00 00:00: 00:00:0 00:00:', [('00:0:00', 0), ('0:00:00', 0)]),
        ('1:1:59 1:1:-1 1:1:60', [('1:1:59', 3719)]), ('1:59:00 1:-1:00 1:60:00', [('1:59:00', 7140), ('1:00', 60)]),
        ('59:59 60:00', [('59:59', 3599), ('60:00', 3600)]),
        ('9999:59 99:59:59 99:60:59', [('9999:59', 599999), ('99:59:59', 360000 - 1)]),
    )

    def test_tdlib_cases(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                tokens = media_timestamp_tokens(text)
                found = sorted((offset, length, value) for (offset, length), value in tokens.items())
                self.assertEqual([(text[offset:offset + length], value) for offset, length, value in found], expected)

    def test_offsets_are_utf16_code_units(self):
        self.assertEqual(media_timestamp_tokens('\U0001f642 at 0:05'), {(6, 4): 5})


class MediaTimestampToleranceTests(unittest.TestCase):
    """Review fix (057, low): a media timestamp is tolerated exactly on a whole token, with that token's value."""

    def test_exact_token_and_value_only(self):
        text = 'Chorus at 0:05'
        self.assertTrue(exact_styles(text, [entity(STAMP, 10, 4, media_timestamp=5)], ()))
        self.assertTrue(exact_styles(text, [entity(STAMP, 10, 4, media_timestamp=5)], (), max_media_timestamp=5))
        bold = ((0, 14, B),)  # TDLib cuts the bold span at both ends of the timestamp.
        self.assertTrue(exact_styles(text, [entity(B, 0, 10), entity(B, 10, 4), entity(STAMP, 10, 4,
                                                                                      media_timestamp=5)], bold))
        for row, bound in ((entity(STAMP, 10, 4, media_timestamp=6), None),  # another value
                           (entity(STAMP, 11, 3, media_timestamp=5), None),  # part of the token
                           (entity(STAMP, 0, 6, media_timestamp=5), None),  # not a token
                           (entity(STAMP, 10, 4, media_timestamp=True), None),
                           (entity(STAMP, 10, 4, media_timestamp='5'), None),
                           (entity(STAMP, 10, 4), None),  # no value
                           (entity(STAMP, 10, 4, media_timestamp=5, extra=1), None),
                           (entity(STAMP, 10, 4, media_timestamp=5), 4)):  # past the media's own duration
            with self.subTest(row=row, bound=bound):
                self.assertFalse(exact_styles(text, [row], (), max_media_timestamp=bound))
        self.assertFalse(exact_styles(text, [entity(B, 0, 6, media_timestamp=5)], ((0, 6, B),)))


if __name__ == '__main__':
    unittest.main()
