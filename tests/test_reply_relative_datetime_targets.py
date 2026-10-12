"""Fresh Relative messageText risks; no historical fixture or render oracle.

This RED slice deliberately exercises only reply source admission and evidence.
It does not authorize Relative media or any outgoing DateTime formatting.
"""
import copy
import hashlib
import json
import unittest

from telegram_search_mcp.reply_drafts import ReplySource, source_from_message


CHAT_ID = -1003456789012
MESSAGE_ID = 1048576
RELATIVE = {"@type": "dateTimeFormattingTypeRelative"}
DISPLAY_PREFIX = "[untrusted Telegram evidence] Formatted target: "
OFFSET_BASIS = "original source UTF-16 code units"


def json_bytes(value):
    """Independently encode the specified canonical JSON contract."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def entity(offset, length, kind, **fields):
    return {
        "@type": "textEntity", "offset": offset, "length": length,
        "type": {"@type": kind, **fields},
    }


def message(text, entities):
    """Build provider input independently, with nullable fields omitted."""
    return {
        "@type": "message", "chat_id": CHAT_ID, "id": MESSAGE_ID,
        "sender_id": {"@type": "messageSenderUser", "user_id": 7654321},
        "is_outgoing": False, "is_from_offline": False,
        "ephemeral_message_id": 0, "date": 1700000000, "edit_date": 0,
        "self_destruct_in": 0, "auto_delete_in": 0,
        "content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": text, "entities": entities},
        },
    }


def expected_projection(text, entities, version=11):
    """The complete approved projection, without product canonical helpers."""
    return {
        "version": version,
        "anchor": {"chat_id": CHAT_ID, "message_id": MESSAGE_ID},
        "message": {
            "@type": "message", "chat_id": CHAT_ID, "id": MESSAGE_ID,
            "sender_id": {"@type": "messageSenderUser", "user_id": 7654321},
            "is_outgoing": False, "is_from_offline": False,
            "ephemeral_message_id": 0, "date": 1700000000, "edit_date": 0,
            "self_destruct_in": 0.0, "auto_delete_in": 0.0,
            "sending_state": None, "scheduling_state": None, "topic_id": None,
            "self_destruct_type": None, "ephemeral_content": None,
            "receiver_id": None, "reply_to": None, "forward_info": None,
            "import_info": None, "reply_markup": None,
            "content": {
                "@type": "messageText",
                "text": {"@type": "formattedText", "text": text, "entities": entities},
                "link_preview": None, "link_preview_options": None,
            },
        },
    }


class RelativeDateTimeTextTargetTests(unittest.TestCase):
    def admit_relative(self, raw):
        try:
            return source_from_message(raw, CHAT_ID, MESSAGE_ID)
        except ValueError as error:
            # Only the current feature-missing rejection becomes RED evidence.
            # Import/setup failures and all other source errors must propagate.
            if str(error) != "invalid formatted reply date time":
                raise
            self.fail(
                "valid messageText DateTime Relative source must be admitted; "
                "current admission rejects its exact no-field formatting constructor"
            )

    def assert_projection(self, source, expected):
        encoded = json_bytes(expected)
        self.assertEqual(source.projection_json, encoded)
        self.assertEqual(source.source_sha256, hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        self.assertEqual(source.anchor, (CHAT_ID, MESSAGE_ID))
        self.assertFalse(source.is_media)
        stored = ReplySource(encoded)
        self.assertEqual(stored.projection_json, encoded)
        self.assertEqual(stored.target(), source.target())
        self.assertEqual(stored.required_capabilities, source.required_capabilities)

    def test_relative_text_retains_exact_v11_owner_evidence_and_existing_authority(self):
        # Dropping Relative, using v9, losing raw quotes, or omitting its
        # DateTime capability would let approval bind incomplete source facts.
        text = 'say "soon"'
        span = entity(
            4, 6, "textEntityTypeDateTime", unix_time=1712345678,
            formatting_type={"@type": "dateTimeFormattingTypeRelative"},
        )
        source = self.admit_relative(message(text, [span]))
        self.assert_projection(source, expected_projection(text, [span]))
        self.assertEqual(source.raw_text, text)
        self.assertEqual(
            source.required_capabilities,
            ("reply_formatted_targets", "reply_datetime_targets"),
        )
        target = source.target()
        expected_evidence = {
            "text": 'say "soon"', "offset_basis": OFFSET_BASIS,
            "entities": [{
                "type": "date_time", "offset": 4, "length": 6, "text": '"soon"',
                "provider_type": "textEntityTypeDateTime", "unix_time": 1712345678,
                "formatting_type": {"@type": "dateTimeFormattingTypeRelative"},
            }],
        }
        self.assertEqual(target.text, DISPLAY_PREFIX + json_bytes(expected_evidence))
        self.assertEqual(target.source_sha256, source.source_sha256)
        self.assertFalse(target.sanitized)
        self.assertFalse(target.truncated)
        self.assertLessEqual(len(target.text), 4096)

    def test_mixed_null_relative_adjacency_orders_entities_and_conditions_authority(self):
        # A v11 branch must retain null DateTimes and all neighboring entity
        # families; capabilities must follow actual lexical/identity evidence.
        text = "ABCR#tag@name"
        relative = entity(2, 1, "textEntityTypeDateTime", unix_time=3, formatting_type=copy.deepcopy(RELATIVE))
        omitted = entity(0, 1, "textEntityTypeDateTime", unix_time=1)
        explicit_null = entity(1, 1, "textEntityTypeDateTime", unix_time=2, formatting_type=None)
        style = entity(3, 1, "textEntityTypeBold")
        lexical = entity(4, 4, "textEntityTypeHashtag")
        identity = entity(8, 5, "textEntityTypeMentionName", user_id=987654321)
        canonical_dates = [
            entity(0, 1, "textEntityTypeDateTime", unix_time=1, formatting_type=None),
            entity(1, 1, "textEntityTypeDateTime", unix_time=2, formatting_type=None),
            relative, style,
        ]
        cases = (
            ([], [], ()),
            ([lexical], [lexical], ("reply_lexical_targets",)),
            ([identity], [identity], ("reply_identity_targets",)),
            ([identity, lexical], [lexical, identity], ("reply_lexical_targets", "reply_identity_targets")),
        )
        for extra_input, extra_expected, extra_capabilities in cases:
            with self.subTest(capabilities=extra_capabilities):
                # Input order differs from explicitly specified offset order.
                raw = message(text, extra_input + [style, relative, explicit_null, omitted])
                source = self.admit_relative(raw)
                expected_entities = canonical_dates + extra_expected
                self.assert_projection(source, expected_projection(text, expected_entities))
                self.assertNotIn("formatting_type", omitted["type"])
                self.assertEqual(
                    source.required_capabilities,
                    ("reply_formatted_targets", "reply_datetime_targets") + extra_capabilities,
                )
                records = [
                    {"type": "date_time", "offset": 0, "length": 1, "text": "A",
                     "provider_type": "textEntityTypeDateTime", "unix_time": 1, "formatting_type": None},
                    {"type": "date_time", "offset": 1, "length": 1, "text": "B",
                     "provider_type": "textEntityTypeDateTime", "unix_time": 2, "formatting_type": None},
                    {"type": "date_time", "offset": 2, "length": 1, "text": "C",
                     "provider_type": "textEntityTypeDateTime", "unix_time": 3,
                     "formatting_type": {"@type": "dateTimeFormattingTypeRelative"}},
                    {"type": "bold", "offset": 3, "length": 1, "text": "R"},
                ]
                if "reply_lexical_targets" in extra_capabilities:
                    records.append({"type": "hashtag", "offset": 4, "length": 4, "text": "#tag"})
                if "reply_identity_targets" in extra_capabilities:
                    records.append({"type": "mention_name", "offset": 8, "length": 5,
                                    "text": "@name", "user_id": 987654321})
                self.assertEqual(
                    source.target().text,
                    DISPLAY_PREFIX + json_bytes({"text": text, "offset_basis": OFFSET_BASIS, "entities": records}),
                )

    def test_relative_signed_int32_edges_use_original_emoji_utf16_spans(self):
        # Python scalar offsets or normalized-text offsets would select the
        # wrong spans; unsigned/narrow timestamp admission loses legal edges.
        text = "😀Ａ\t🕒B"
        minimum = entity(0, 3, "textEntityTypeDateTime", unix_time=-2147483648,
                         formatting_type=copy.deepcopy(RELATIVE))
        maximum = entity(4, 3, "textEntityTypeDateTime", unix_time=2147483647,
                         formatting_type=copy.deepcopy(RELATIVE))
        source = self.admit_relative(message(text, [maximum, minimum]))
        self.assert_projection(source, expected_projection(text, [minimum, maximum]))
        self.assertEqual(source.raw_text, "😀Ａ\t🕒B")
        self.assertEqual(source.required_capabilities, ("reply_formatted_targets", "reply_datetime_targets"))
        expected_evidence = {
            "text": "😀A 🕒B", "offset_basis": OFFSET_BASIS,
            "entities": [
                {"type": "date_time", "offset": 0, "length": 3, "text": "😀A",
                 "provider_type": "textEntityTypeDateTime", "unix_time": -2147483648,
                 "formatting_type": {"@type": "dateTimeFormattingTypeRelative"}},
                {"type": "date_time", "offset": 4, "length": 3, "text": "🕒B",
                 "provider_type": "textEntityTypeDateTime", "unix_time": 2147483647,
                 "formatting_type": {"@type": "dateTimeFormattingTypeRelative"}},
            ],
        }
        target = source.target()
        self.assertEqual(target.text, DISPLAY_PREFIX + json_bytes(expected_evidence))
        self.assertTrue(target.sanitized)
        self.assertFalse(target.truncated)

    def test_relative_snapshot_is_immutable_and_digest_binds_formatting_and_timestamp(self):
        # Aliasing provider containers or ignoring constructor/timestamp changes
        # would leave an old owner approval valid against changed source facts.
        span = entity(0, 4, "textEntityTypeDateTime", unix_time=1700000300,
                      formatting_type=copy.deepcopy(RELATIVE))
        raw = message("soon", [span])
        source = self.admit_relative(raw)
        expected = expected_projection("soon", [copy.deepcopy(span)])
        self.assert_projection(source, expected)
        original_json = source.projection_json
        original_digest = source.source_sha256
        original_target = source.target()

        timestamp_span = entity(0, 4, "textEntityTypeDateTime", unix_time=1700000301,
                                formatting_type=copy.deepcopy(RELATIVE))
        timestamp_source = self.admit_relative(message("soon", [timestamp_span]))
        self.assert_projection(timestamp_source, expected_projection("soon", [timestamp_span]))
        null_span = entity(0, 4, "textEntityTypeDateTime", unix_time=1700000300, formatting_type=None)
        null_source = source_from_message(message("soon", [null_span]), CHAT_ID, MESSAGE_ID)
        self.assert_projection(null_source, expected_projection("soon", [null_span], version=9))
        self.assertEqual(len({original_digest, timestamp_source.source_sha256, null_source.source_sha256}), 3)
        self.assertNotEqual(original_target.text, timestamp_source.target().text)
        self.assertNotEqual(original_target.text, null_source.target().text)

        raw["content"]["text"]["text"] = "changed"
        span["type"]["unix_time"] = 0
        span["type"]["formatting_type"]["extra"] = True
        raw["content"]["text"]["entities"].clear()
        raw["sender_id"]["user_id"] = 1
        self.assertEqual(source.projection_json, original_json)
        self.assertEqual(source.source_sha256, original_digest)
        self.assertEqual(source.raw_text, "soon")
        self.assertEqual(source.target(), original_target)
        self.assertEqual(ReplySource(original_json).target(), original_target)
        self.assertEqual(source.required_capabilities, ("reply_formatted_targets", "reply_datetime_targets"))
