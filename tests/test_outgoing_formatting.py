"""Pure outgoing-style contract tests; proposed, not executed.

No provider, broker, SDK, draft/digest integration or source-copy behavior is
claimed here. The existing whole-text normalizer supplies FINAL text to the
new helper. All spans are UTF-16 code units in that final text.
"""
from __future__ import annotations

import importlib
import unittest

from telegram_search_mcp.outgoing_drafts import _safe_message_text


B = "textEntityTypeBold"
I = "textEntityTypeItalic"
U = "textEntityTypeUnderline"
S = "textEntityTypeStrikethrough"
P = "textEntityTypeSpoiler"


def span(offset, length, constructor=B):
    """Input fixture only; native expectations below are independent literals."""
    return {"offset": offset, "length": length, "type": {"@type": constructor}}


class OutgoingFormattingTests(unittest.TestCase):
    def setUp(self):
        try:
            module = importlib.import_module("telegram_search_mcp.outgoing_formatting")
        except ModuleNotFoundError as exc:
            if exc.name != "telegram_search_mcp.outgoing_formatting":
                raise  # A broken dependency/PYTHONPATH is not feature absence.
            module = None
        self.assertIsNotNone(
            module,
            "FEATURE ABSENT: approved outgoing_formatting helper is not implemented",
        )
        for name in ("freeze_outgoing_entities", "native_outgoing_entities", "outgoing_entity_facts"):
            self.assertTrue(
                callable(getattr(module, name, None)),
                f"FEATURE ABSENT: outgoing_formatting.{name} callable is not implemented",
            )
        self.freeze = module.freeze_outgoing_entities
        self.native = module.native_outgoing_entities
        self.facts = module.outgoing_entity_facts

    def test_empty_list_has_no_native_or_approval_entity_facts(self):
        frozen = self.freeze("Plain text", [])
        self.assertEqual(frozen, ())
        self.assertEqual(self.native(frozen), [])
        self.assertEqual(self.facts("Plain text", frozen), ())

    def test_all_five_fieldless_styles_have_exact_native_projection(self):
        frozen = self.freeze("ABCDE", [span(0, 5, style) for style in (U, S, P, I, B)])
        self.assertEqual(frozen, (
            (0, 5, "textEntityTypeBold"), (0, 5, "textEntityTypeItalic"),
            (0, 5, "textEntityTypeSpoiler"), (0, 5, "textEntityTypeStrikethrough"),
            (0, 5, "textEntityTypeUnderline"),
        ))
        self.assertEqual(self.native(frozen), [
            {"@type": "textEntity", "offset": 0, "length": 5, "type": {"@type": "textEntityTypeBold"}},
            {"@type": "textEntity", "offset": 0, "length": 5, "type": {"@type": "textEntityTypeItalic"}},
            {"@type": "textEntity", "offset": 0, "length": 5, "type": {"@type": "textEntityTypeSpoiler"}},
            {"@type": "textEntity", "offset": 0, "length": 5, "type": {"@type": "textEntityTypeStrikethrough"}},
            {"@type": "textEntity", "offset": 0, "length": 5, "type": {"@type": "textEntityTypeUnderline"}},
        ])

    def test_canonical_sort_is_offset_then_descending_length_then_constructor(self):
        values = [span(2, 2, P), span(0, 6, U), span(0, 8, I), span(8, 2, S), span(0, 8, B)]
        expected = ((0, 8, B), (0, 8, I), (0, 6, U), (2, 2, P), (8, 2, S))
        for order in (values, list(reversed(values)), values[2:] + values[:2]):
            with self.subTest(order=order):
                self.assertEqual(self.freeze("0123456789", order), expected)

    def test_utf16_spans_cover_whole_astral_scalar_and_exact_final_end(self):
        # A=0:1, emoji=1:3, B=3:4. Python slicing offsets are not UTF-16 offsets.
        for offset, length, covered in ((0, 1, "A"), (1, 2, "🙂"), (3, 1, "B"), (0, 4, "A🙂B")):
            with self.subTest(offset=offset, length=length):
                frozen = self.freeze("A🙂B", [span(offset, length)])
                self.assertEqual(self.facts("A🙂B", frozen), ((offset, length, B, covered),))
        for offset, length in ((2, 1), (1, 1), (0, 2), (2, 2)):
            with self.subTest(split=(offset, length)), self.assertRaises(ValueError):
                self.freeze("A🙂B", [span(offset, length)])

    def test_coordinates_use_final_nfkc_and_newline_normalization_without_rebasing(self):
        final = _safe_message_text("ﬃ\r\nCafe\u0301🙂")
        self.assertEqual(final, "ffi\nCafé🙂")
        frozen = self.freeze(final, [span(8, 2, P), span(4, 4, I), span(0, 3, B)])
        self.assertEqual(self.facts(final, frozen), (
            (0, 3, B, "ffi"), (4, 4, I, "Café"), (8, 2, P, "🙂"),
        ))
        self.assertEqual(self.native(frozen), [
            {"@type": "textEntity", "offset": 0, "length": 3, "type": {"@type": "textEntityTypeBold"}},
            {"@type": "textEntity", "offset": 4, "length": 4, "type": {"@type": "textEntityTypeItalic"}},
            {"@type": "textEntity", "offset": 8, "length": 2, "type": {"@type": "textEntityTypeSpoiler"}},
        ])
        # Telegram's own rewrite is part of the final text too (owner decision 20.10.2026).
        rewritten = _safe_message_text(" \tHi\u2028there\tyou \n")
        self.assertEqual(rewritten, "Hithere you")
        self.assertEqual(self.facts(rewritten, self.freeze(rewritten, [span(8, 3, I)])), ((8, 3, I, "you"),))
        # A former raw span can remain valid: do not guess intent or rebase it.
        shifted = self.freeze(final, [span(3, 4)])
        self.assertEqual(self.facts(final, shifted), ((3, 4, B, "\nCaf"),))
        expanded = _safe_message_text("ﬃ")
        self.assertEqual(self.facts(expanded, self.freeze(expanded, [span(0, 1)])), ((0, 1, B, "f"),))
        composed = _safe_message_text("e\u0301")
        self.assertEqual(composed, "é")
        with self.assertRaises(ValueError):
            self.freeze(composed, [span(0, 2)])
        self.assertEqual(_safe_message_text("a\rb\r\nc"), "a\nb\nc")

    def test_only_an_actual_list_is_an_entity_array(self):
        for bad in (None, False, True, 0, "", {}, (), iter(())):
            with self.subTest(kind=type(bad).__name__), self.assertRaises(ValueError):
                self.freeze("Valid", bad)

    def test_span_and_constructor_are_closed_shapes(self):
        bad_rows = [
            None, False, 1, "bold", [], {},
            {"offset": 0, "length": 1},
            {"length": 1, "type": {"@type": B}},
            {"offset": 0, "type": {"@type": B}},
            {"offset": 0, "length": 1, "type": {"@type": B}, "extra": True},
            {"@type": "textEntity", "offset": 0, "length": 1, "type": {"@type": B}},
            {"offset": 0, "length": 1, "type": None},
            {"offset": 0, "length": 1, "type": B},
            {"offset": 0, "length": 1, "type": {}},
            {"offset": 0, "length": 1, "type": {"@type": B, "parameter": "x"}},
            {"offset": 0, "length": 1, "type": {"@type": [B]}},
        ]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.freeze("Valid", [row])

        for style in ("textEntityTypeCode", "textEntityTypePre", "textEntityTypeBlockQuote",
                      "textEntityTypeUrl", "textEntityTypeTextUrl", "textEntityTypeMentionName",
                      "bold", "textEntityTypebold", "", None, True):
            with self.subTest(style=style), self.assertRaises(ValueError):
                self.freeze("Valid", [span(0, 1, style)])

    def test_integer_coordinates_do_not_coerce_booleans_floats_strings_or_null(self):
        for field in ("offset", "length"):
            for bad in (False, True, 0.0, 1.0, float("nan"), float("inf"), "0", "1", None, [], {}):
                row = span(0, 1)
                row[field] = bad
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    self.freeze("Valid", [row])

    def test_signed32_and_final_text_range_bounds_are_strict(self):
        for offset, length in ((-1, 1), (0, 0), (0, -1), (2**31, 1), (0, 2**31),
                               (2**31 - 1, 1), (0, 2**31 - 1), (4, 1), (3, 2)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                self.freeze("abcd", [span(offset, length)])
        self.assertEqual(self.freeze("abcd", [span(0, 4)]), ((0, 4, B),))
        self.assertEqual(self.freeze("a\t\nb", [span(1, 2)]), ((1, 2, B),))
        self.assertEqual(self.facts("a\t\nb", ((1, 2, B),)), ((1, 2, B, "\t\n"),))

    def test_exactly32_disjoint_spans_are_allowed_but33_are_refused(self):
        rows = [span(offset, 1) for offset in range(0, 64, 2)]
        frozen = self.freeze("A" * 65, rows)
        self.assertEqual(frozen, tuple((offset, 1, B) for offset in range(0, 64, 2)))
        self.assertEqual(self.native(frozen), [
            {"@type": "textEntity", "offset": offset, "length": 1,
             "type": {"@type": "textEntityTypeBold"}}
            for offset in range(0, 64, 2)
        ])
        with self.assertRaises(ValueError):
            self.freeze("A" * 65, rows + [span(64, 1)])

    def test_same_style_duplicates_overlap_nesting_and_adjacency_are_refused(self):
        for style in (B, I, U, S, P):
            for pair in (((0, 2), (0, 2)), ((0, 3), (2, 3)), ((0, 5), (1, 1)), ((0, 2), (2, 2))):
                values = [span(*pair[0], style), span(*pair[1], style)]
                for order in (values, list(reversed(values))):
                    with self.subTest(style=style, pair=pair, reversed=order != values), self.assertRaises(ValueError):
                        self.freeze("abcdef", order)
        # Intervening other styles must not hide a same-style overlap.
        with self.assertRaises(ValueError):
            self.freeze("abcdef", [span(0, 6, B), span(1, 4, I), span(2, 2, B)])
        self.assertEqual(self.freeze("abcdef", [span(0, 4)]), ((0, 4, B),))

    def test_crossing_different_styles_are_refused_in_both_orders(self):
        for pair in ((B, I), (U, P), (S, B)):
            rows = [span(0, 4, pair[0]), span(2, 4, pair[1])]
            for order in (rows, list(reversed(rows))):
                with self.subTest(styles=pair, order=order), self.assertRaises(ValueError):
                    self.freeze("abcdef", order)

    def test_different_style_nesting_equal_spans_and_adjacency_are_allowed(self):
        frozen = self.freeze("abcdefgh", [
            span(6, 2, S), span(0, 6, P), span(2, 2, U), span(1, 4, I), span(0, 6, B),
        ])
        self.assertEqual(frozen, ((0, 6, B), (0, 6, P), (1, 4, I), (2, 2, U), (6, 2, S)))
        self.assertEqual(self.facts("abcdefgh", frozen), (
            (0, 6, B, "abcdef"), (0, 6, P, "abcdef"), (1, 4, I, "bcde"),
            (2, 2, U, "cd"), (6, 2, S, "gh"),
        ))

    def test_frozen_facts_are_immutable_and_native_projections_are_fresh(self):
        rows = [span(2, 2, P), span(0, 2, B)]
        frozen = self.freeze("Aé🙂BC", rows)
        golden = ((0, 2, B), (2, 2, P))
        facts = self.facts("Aé🙂BC", frozen)
        self.assertEqual(facts, ((0, 2, B, "Aé"), (2, 2, P, "🙂")))
        rows[0]["offset"] = 0
        rows[0]["type"]["@type"] = U
        rows.append(span(4, 1))
        self.assertEqual(frozen, golden)
        native = self.native(frozen)
        native[0]["type"]["@type"] = U
        native[0]["offset"] = 1
        native.append({"malicious": True})
        self.assertEqual(self.native(frozen), [
            {"@type": "textEntity", "offset": 0, "length": 2, "type": {"@type": "textEntityTypeBold"}},
            {"@type": "textEntity", "offset": 2, "length": 2, "type": {"@type": "textEntityTypeSpoiler"}},
        ])
        self.assertEqual(frozen, golden)
        self.assertEqual(self.facts("Aé🙂BC", frozen), facts)
        with self.assertRaises(TypeError):
            frozen[0] = (1, 1, B)
        with self.assertRaises(TypeError):
            facts[0] = (0, 1, B, "A")

    def test_final_text_validation_uses_scalar_not_utf16_length_limit(self):
        # Existing4096-scalar policy allows8192UTF16 units for4096 astral scalars.
        final = _safe_message_text("🙂" * 4096)
        frozen = self.freeze(final, [span(8190, 2)])
        self.assertEqual(frozen, ((8190, 2, B),))
        self.assertEqual(self.facts(final, frozen), ((8190, 2, B, "🙂"),))
        for bad in (None, False, 1, b"text", "", " \t\n", "a\x00b", "a\u200bb", "\ud800", "🙂" * 4097):
            with self.subTest(kind=type(bad).__name__, length=len(bad) if isinstance(bad, (str, bytes)) else None), self.assertRaises(ValueError):
                self.freeze(bad, [])


if __name__ == "__main__":
    unittest.main()
