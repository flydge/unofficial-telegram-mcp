"""Absolute messageText risks from the pinned structured constructor contract.

Fixtures and escaped evidence are independently specified. No native TDLib,
credentials, timezone conversion, network, or historical helper execution.
"""
import copy
import hashlib
import itertools
import json
import unittest

from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_formatted_sources import formatted_text

CHAT = -10073
MESSAGE = 903
DATE = "textEntityTypeDateTime"
ABSOLUTE = "dateTimeFormattingTypeAbsolute"
RELATIVE = {"@type": "dateTimeFormattingTypeRelative"}
PRECISIONS = ("dateTimePartPrecisionNone", "dateTimePartPrecisionShort", "dateTimePartPrecisionLong")
PREFIX = "[untrusted Telegram evidence] Formatted target: "
OMITTED = object()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def absolute(time=PRECISIONS[1], date=PRECISIONS[2], weekday=False):
    return {"@type": ABSOLUTE, "time_precision": {"@type": time},
            "date_precision": {"@type": date}, "show_day_of_week": weekday}


def span(offset=0, length=4, stamp=1700000000, formatting=OMITTED, kind=DATE):
    typ = {"@type": kind}
    if kind == DATE:
        typ["unix_time"] = stamp
        typ["formatting_type"] = absolute() if formatting is OMITTED else copy.deepcopy(formatting)
    if kind == "textEntityTypeMentionName":
        typ["user_id"] = 51
    return {"@type": "textEntity", "offset": offset, "length": length, "type": typ}


def message(text="soon", entities=None):
    return {"@type": "message", "chat_id": CHAT, "id": MESSAGE,
            "sender_id": {"@type": "messageSenderUser", "user_id": 51},
            "is_outgoing": False, "is_from_offline": False, "ephemeral_message_id": 0,
            "date": 1700000000, "edit_date": 0, "self_destruct_in": 0, "auto_delete_in": 0,
            "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": text,
                        "entities": [span()] if entities is None else entities}}}


def projection(text, entities, version=13):
    shell = {"@type": "message", "chat_id": CHAT, "id": MESSAGE,
             "sender_id": {"@type": "messageSenderUser", "user_id": 51},
             "is_outgoing": False, "is_from_offline": False, "ephemeral_message_id": 0,
             "date": 1700000000, "edit_date": 0, "self_destruct_in": 0.0, "auto_delete_in": 0.0,
             **{key: None for key in ("sending_state", "scheduling_state", "topic_id", "self_destruct_type",
                  "ephemeral_content", "receiver_id", "reply_to", "forward_info", "import_info", "reply_markup")},
             "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": text,
                         "entities": entities}, "link_preview": None, "link_preview_options": None}}
    return {"version": version, "anchor": {"chat_id": CHAT, "message_id": MESSAGE}, "message": shell}


def evidence(text, records):
    return PREFIX + encode({"text": text, "offset_basis": "original source UTF-16 code units", "entities": records})


def date_record(text="soon", offset=0, length=4, stamp=1700000000, formatting=OMITTED):
    return {"type": "date_time", "offset": offset, "length": length, "text": text,
            "provider_type": DATE, "unix_time": stamp,
            "formatting_type": absolute() if formatting is OMITTED else copy.deepcopy(formatting)}


def document(caption):
    return {"@type": "messageDocument", "document": {"@type": "document", "file_name": "fixture.txt",
            "mime_type": "text/plain", "thumbnail": None, "minithumbnail": None,
            "document": {"@type": "file", "id": 73, "size": 4, "expected_size": 4,
            "local": {"@type": "localFile", "path": "", "can_be_downloaded": True, "can_be_deleted": False,
            "is_downloading_active": False, "is_downloading_completed": False, "download_offset": 0,
            "downloaded_prefix_size": 0, "downloaded_size": 0}, "remote": {"@type": "remoteFile", "id": "remote-73",
            "unique_id": "unique-73", "is_uploading_active": False, "is_uploading_completed": True,
            "uploaded_size": 4}}}, "caption": caption}


class AbsoluteTargetTests(unittest.TestCase):
    def source(self, raw=None):
        try:
            return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            if str(error) != "invalid formatted reply date time":
                raise
            self.fail("complete explicitly specified Absolute messageText must be admitted")

    def test_v1_through_v12_canonical_bytes_and_authority_preserve_baseline(self):
        # Break: broadening admission must not reinterpret already reviewable snapshots.
        # Golden hashes captured from accepted 0.37 baseline before production edits.
        hashes = (
            "ed161ae146d1cadd687e2b0417663e55e2bcf65435e1876c0653366fa3474cb1",
            "36100164c3da3a3b2c9e63afcb63dbc5016d46f0bcfd87aa14bc11a022a680d2",
            "333a27ca79eeb0d720ce57a9f439a99f4760dd9df9f82be1e06dfd300344d9c5",
            "ce179e19f4400a5aa72b1a0643c3f7408c4a7473c7d3bebb36b998f3398a0496",
            "181c5b86bbd7c0d8b11071a7842308d6fafef6d39b3660e91f03e9e7822c84d3",
            "f4279b87670ceddb50943e1fe8a62bfe949738e836e85c2ae8973c56da6b9b73",
            "e0f0374f9008d7866978903f6ff64e4f64223854d2b438ed7a32e4340a8ce266",
            "62df3319f038e3ed010ea1c6fe424e70e6a5f3c859efa80ff721a09cc531d704",
            "22706e96c5acd28859caae5f8ec7538f6baf86a38a508db8ee27f4bca3d56f65",
            "ac45abaf9700a6eee91bd6889fb63a8ef82c8b1d7ff77bba207e8323f76944aa",
            "106eafa83964dfaf8630727e29e41eb4168080468ed4cd85c4d2a91c2ccf393c",
            "e8603656c53c9758c316455eeb42433015920dfcddf250b5aa3392bb7caa7a3a")
        for version, golden in enumerate(hashes, 1):
            entities = ([] if version <= 2 else [span(kind="textEntityTypeBold")] if version <= 4
                        else [span(kind="textEntityTypeHashtag")] if version <= 6
                        else [span(kind="textEntityTypeMentionName")] if version <= 8
                        else [span(formatting=None)] if version <= 10 else [span(formatting=RELATIVE)])
            raw = message(entities=entities)
            if version % 2 == 0: raw["content"] = document(raw["content"]["text"])
            source = source_from_message(raw, CHAT, MESSAGE)
            with self.subTest(version=version):
                self.assertEqual(json.loads(source.projection_json)["version"], version)
                self.assertEqual(source.source_sha256, golden)
                required = ("reply_media_targets",) if version % 2 == 0 else ()
                if version >= 3: required += ("reply_formatted_targets",)
                if 5 <= version <= 6: required += ("reply_lexical_targets",)
                if 7 <= version <= 8: required += ("reply_identity_targets",)
                if version >= 9: required += ("reply_datetime_targets",)
                self.assertEqual(source.required_capabilities, required)
                self.assertEqual(ReplySource(source.projection_json).target(), source.target())

    def test_all_18_precision_weekday_combinations_bind_exact_v13_bytes_and_display(self):
        # Break: invented combination restrictions, false treated as absence,
        # discarded precision facts, or selection of legacy authority.
        for time, date, weekday in itertools.product(PRECISIONS, PRECISIONS, (False, True)):
            with self.subTest(time=time, date=date, weekday=weekday):
                fmt = absolute(time, date, weekday)
                source = self.source(message(entities=[span(formatting=fmt)]))
                expected = encode(projection("soon", [span(formatting=fmt)]))
                self.assertEqual(source.projection_json, expected)
                self.assertEqual(source.source_sha256, hashlib.sha256(expected.encode()).hexdigest())
                self.assertEqual(source.target().text, evidence("soon", [date_record(formatting=fmt)]))
                self.assertEqual(source.required_capabilities, ("reply_formatted_targets", "reply_datetime_targets"))
                self.assertEqual(ReplySource(expected).target(), source.target())
                self.assertFalse(source.is_media)
                self.assertFalse(source.target().truncated)

    def test_adjacent_mixed_families_retain_null_relative_absolute_and_exact_authority(self):
        # Break: dropping earlier DateTimes or requiring absent lexical/identity families.
        dates = [span(0, 1, formatting=None), span(1, 1, formatting=RELATIVE), span(2, 1)]
        style = span(3, 1, kind="textEntityTypeBold")
        lexical = span(4, 4, kind="textEntityTypeHashtag")
        identity = span(8, 5, kind="textEntityTypeMentionName")
        for lexical_on, identity_on in itertools.product((False, True), repeat=2):
            extras = ([lexical] if lexical_on else []) + ([identity] if identity_on else [])
            source = self.source(message("NRAS#tag@user", list(reversed(dates + [style] + extras))))
            self.assertEqual(source.projection_json, encode(projection("NRAS#tag@user", dates + [style] + extras)))
            required = ("reply_formatted_targets", "reply_datetime_targets")
            if lexical_on: required += ("reply_lexical_targets",)
            if identity_on: required += ("reply_identity_targets",)
            self.assertEqual(source.required_capabilities, required)
            records = [date_record("N", 0, 1, formatting=None), date_record("R", 1, 1, formatting=RELATIVE),
                       date_record("A", 2, 1), {"type": "bold", "offset": 3, "length": 1, "text": "S"}]
            if lexical_on: records.append({"type": "hashtag", "offset": 4, "length": 4, "text": "#tag"})
            if identity_on: records.append({"type": "mention_name", "offset": 8, "length": 5, "text": "@user", "user_id": 51})
            self.assertEqual(source.target().text, evidence("NRAS#tag@user", records))

    def test_int32_edges_original_nonbmp_utf16_spans_and_sanitation_bind_raw_evidence(self):
        # Break: scalar offsets, normalization before slicing, or unsigned timestamps.
        text = "😀Ａ\t🕒B"
        spans = [span(0, 3, -2147483648), span(4, 3, 2147483647)]
        source = self.source(message(text, list(reversed(spans))))
        self.assertEqual(source.projection_json, encode(projection(text, spans)))
        self.assertEqual(source.raw_text, text)
        self.assertEqual(source.target().text, evidence("😀A 🕒B", [date_record("😀A", 0, 3, -2147483648),
                            date_record("🕒B", 4, 3, 2147483647)]))
        self.assertTrue(source.target().sanitized)
        self.assertFalse(source.target().truncated)

    def test_nested_snapshot_is_immutable_and_digest_binds_each_absolute_field(self):
        # Break: retaining nested aliases or omitting precision/weekday from digest.
        raw = message()
        source = self.source(raw)
        original = source.projection_json, source.target(), source.source_sha256
        digests = {source.source_sha256}
        for key, replacement in (("time_precision", {"@type": PRECISIONS[0]}),
                                 ("date_precision", {"@type": PRECISIONS[1]}), ("show_day_of_week", True)):
            changed = copy.deepcopy(raw)
            changed["content"]["text"]["entities"][0]["type"]["formatting_type"][key] = replacement
            new = self.source(changed)
            self.assertNotEqual(new.target().text, source.target().text)
            digests.add(new.source_sha256)
        self.assertEqual(len(digests), 4)
        fmt = raw["content"]["text"]["entities"][0]["type"]["formatting_type"]
        fmt["time_precision"]["@type"] = PRECISIONS[2]
        fmt["date_precision"]["extra"] = True
        fmt["show_day_of_week"] = True
        raw["content"]["text"]["entities"].clear()
        self.assertEqual((source.projection_json, source.target(), source.source_sha256), original)
        self.assertEqual(ReplySource(original[0]).target(), original[1])

    def test_explicit_fields_only_closed_precisions_and_strict_boolean_are_local_subset(self):
        # Break: accepting JSON parser coercions or null/omitted precision without semantics.
        malformed = [False, 0, "dateTimeFormattingTypeAbsolute", [], {},
                     {"@type": ABSOLUTE}, {"@type": ABSOLUTE, "format": "yyyy"}]
        for key in ("time_precision", "date_precision", "show_day_of_week"):
            fmt = absolute(); fmt.pop(key); malformed.append(fmt)
            fmt = absolute(); fmt[key] = None; malformed.append(fmt)
        for key in ("time_precision", "date_precision"):
            for value in ({}, {"@type": "unknown"}, {"@type": None}, {"@type": PRECISIONS[0], "extra": None},
                          {"@type": PRECISIONS[0], "format": "yyyy"}, PRECISIONS[0], 0, False, []):
                fmt = absolute(); fmt[key] = value; malformed.append(fmt)
        for value in (0, 1, "false", "true", [], {}):
            fmt = absolute(); fmt["show_day_of_week"] = value; malformed.append(fmt)
        for key in ("extra", "format", "timezone"):
            fmt = absolute(); fmt[key] = "yyyy"; malformed.append(fmt)
        for fmt in malformed:
            with self.subTest(formatting=fmt), self.assertRaises(ValueError):
                source_from_message(message(entities=[span(formatting=fmt)]), CHAT, MESSAGE)

    def test_timestamp_entity_shape_utf16_overlap_and_count_bounds_refuse(self):
        # Break: coercion, open entity types, surrogate splitting, overlaps, or relaxed cap.
        for stamp in (True, False, 1.0, "1", None, -2147483649, 2147483648):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                source_from_message(message(entities=[span(stamp=stamp)]), CHAT, MESSAGE)
        for offset, length in ((1, 1), (0, 1), (True, 2), (-1, 2), (0, False), (0, 99), (2147483648, 2)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                source_from_message(message("😀soon", [span(offset, length)]), CHAT, MESSAGE)
        for kind in (DATE, "textEntityTypeBold", "textEntityTypeHashtag", "textEntityTypeMentionName"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                source_from_message(message("soonTail", [span(), span(1, 2, kind=kind)]), CHAT, MESSAGE)
        for mutation in ("missing", "extra"):
            raw = message(); typ = raw["content"]["text"]["entities"][0]["type"]
            if mutation == "missing": typ.pop("unix_time")
            else: typ["timezone"] = "UTC"
            with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)
        with self.assertRaises(ValueError):
            source_from_message(message("x" * 33, [span(i, 1) for i in range(33)]), CHAT, MESSAGE)
        accepted = self.source(message("x" * 32, [span(length=1)] + [span(i, 1, kind="textEntityTypeBold") for i in range(1, 32)]))
        self.assertEqual(len(json.loads(accepted.projection_json)["message"]["content"]["text"]["entities"]), 32)
        self.assertFalse(accepted.target().truncated)

    def test_complete_escaped_display_bound_admits_exact_edge_and_never_truncates(self):
        # Break: checking raw rather than complete escaped evidence or truncating to admit.
        overhead = len(evidence("", [date_record("x", 0, 1)]))
        text = "x" * (4096 - overhead)
        source = self.source(message(text, [span(length=1)]))
        self.assertEqual(len(source.target().text), 4096)
        self.assertEqual(source.target().text, evidence(text, [date_record("x", 0, 1)]))
        self.assertFalse(source.target().truncated)
        for raw_text in (text + "x", "x" * 4097, '"' * 2100, "\x00soon", "\ud800soon", " " * 4):
            with self.subTest(length=len(raw_text)), self.assertRaises(ValueError):
                source_from_message(message(raw_text, [span(length=1)]), CHAT, MESSAGE)

    def test_forged_legacy_tags_and_v13_without_absolute_refuse(self):
        # Break: laundering Absolute authority through any previous version or v13 without it.
        stored = projection("soon", [span()])
        for version in range(1, 13):
            changed = copy.deepcopy(stored); changed["version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError): ReplySource(encode(changed))
        for spans in ([], [span(formatting=None)], [span(formatting=RELATIVE)], [span(kind="textEntityTypeBold")]):
            with self.subTest(entities=spans), self.assertRaises(ValueError): ReplySource(encode(projection("soon", spans)))
        changed = copy.deepcopy(stored)
        changed["message"]["content"]["text"]["entities"][0]["type"]["formatting_type"]["date_precision"]["extra"] = None
        with self.assertRaises(ValueError): ReplySource(encode(changed))
        oversized = encode(projection("x" * 66000, [span()]))
        with self.assertRaises(ValueError): ReplySource(oversized)

    def test_default_formatter_and_legacy_media_do_not_gain_absolute_admission(self):
        # Break: making formatter opt-in global or enabling Absolute in legacy media versions.
        formatted = message()["content"]["text"]
        with self.assertRaises(ValueError): formatted_text(formatted, datetime=True, datetime_relative=True)
        try:
            opted_in = formatted_text(formatted, datetime=True, datetime_absolute=True)
        except TypeError as error:
            if "unexpected keyword argument 'datetime_absolute'" not in str(error): raise
            self.fail("closed formatter must expose default-off Absolute admission option")
        self.assertEqual(opted_in, formatted)
        from telegram_search_mcp.reply_media_sources import media_content
        with self.assertRaises(ValueError): media_content(document(formatted), datetime=True, datetime_relative=True)
        for version in (2, 4, 6, 8, 10, 12, 13):
            stored = projection("soon", [], version)
            stored["message"]["content"] = {"@type": "replySourceMedia", "kind": "document",
                "media": {"file_name": "fixture.txt", "mime_type": "text/plain", "size": 4,
                "unique_id_sha256": hashlib.sha256(b"unique-73").hexdigest()}, "caption": formatted}
            with self.subTest(version=version), self.assertRaises(ValueError): ReplySource(encode(stored))
