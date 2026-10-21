"""Relative DateTime boundary and approval risks, using closed synthetic inputs.

Real source/registry/broker/TDJSON logic runs; only raw provider events and the
local owner decision are synthetic. No credentials, TDLib load or network.
"""
import copy
import functools
import json
import tempfile
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_artifact_drafts import (
    GetReplyArtifactDraftRequest, PrepareReplyArtifactSendRequest,
    RefreshReplyArtifactDraftRequest, UpdateReplyArtifactDraftRequest,
)
from telegram_search_mcp.reply_drafts import (
    GetReplyDraftRequest, PrepareReplyTextSendRequest, RefreshReplyDraftRequest,
    ReplySource, UpdateReplyDraftRequest, source_from_message,
)
from telegram_search_mcp.reply_formatted_sources import formatted_text
from telegram_search_mcp.schemas import SendPreparedArtifactRequest, SendPreparedTextRequest
from telegram_search_mcp.tdjson import TDLibClient

CHAT = -10072
MESSAGE = 902
CLIENT = "client_" + "e" * 32
RELATIVE = {"@type": "dateTimeFormattingTypeRelative"}
CAPABILITIES = ("send", "reply_text_send", "reply_artifact_send", "reply_formatted_targets",
                "reply_datetime_targets", "reply_lexical_targets", "reply_identity_targets")


def span(offset=0, length=4, stamp=1700000000, formatting=RELATIVE, kind="textEntityTypeDateTime"):
    typ = {"@type": kind}
    if kind == "textEntityTypeDateTime":
        typ.update(unix_time=stamp, formatting_type=copy.deepcopy(formatting))
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


def document(caption):
    return {"@type": "messageDocument", "document": {"@type": "document",
            "file_name": "fixture.txt", "mime_type": "text/plain", "thumbnail": None,
            "minithumbnail": None, "document": {"@type": "file", "id": 73, "size": 4,
            "expected_size": 4, "local": {"@type": "localFile", "path": "",
            "can_be_downloaded": True, "can_be_deleted": False, "is_downloading_active": False,
            "is_downloading_completed": False, "download_offset": 0, "downloaded_prefix_size": 0,
            "downloaded_size": 0}, "remote": {"@type": "remoteFile", "id": "remote-73",
            "unique_id": "unique-73", "is_uploading_active": False, "is_uploading_completed": True,
            "uploaded_size": 4}}}, "caption": caption}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class BoundaryTests(unittest.TestCase):
    def source(self, raw=None):
        try:
            return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            if str(error) != "invalid formatted reply date time":
                raise
            self.fail("exact Relative messageText must be admitted before boundary verification")

    def test_open_absolute_unknown_and_nonobject_formats_refuse(self):
        # Break: loose constructor matching or string/coercion-based admission.
        bad = ({"@type": "dateTimeFormattingTypeRelative", "extra": None},
               {"@type": "dateTimeFormattingTypeRelative", "format": "yyyy"},
               {"@type": "dateTimeFormattingTypeAbsolute"}, {"@type": "unknown"},
               {"@type": None}, {}, False, 0, "dateTimeFormattingTypeRelative", [RELATIVE])
        for formatting in bad:
            with self.subTest(formatting=formatting), self.assertRaises(ValueError):
                source_from_message(message(entities=[span(formatting=formatting)]), CHAT, MESSAGE)

    def test_relative_timestamps_and_type_keys_remain_strict(self):
        # Break: accepting bool/coerced/out-of-int32 timestamps or open entity data.
        for stamp in (True, False, 1.0, "1", None, -2147483649, 2147483648):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                source_from_message(message(entities=[span(stamp=stamp)]), CHAT, MESSAGE)
        for mutation in ("missing", "extra"):
            raw = message()
            typ = raw["content"]["text"]["entities"][0]["type"]
            if mutation == "missing":
                typ.pop("unix_time")
            else:
                typ["timezone"] = "UTC"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                source_from_message(raw, CHAT, MESSAGE)

    def test_surrogate_splits_offsets_overlap_and_complete_bounds_refuse(self):
        # Break: scalar-index admission, permissive bounds, nesting, or truncated evidence.
        for offset, length in ((1, 1), (0, 1), (True, 2), (-1, 2), (0, False), (0, 99)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                source_from_message(message("😀soon", [span(offset, length)]), CHAT, MESSAGE)
        for kind in ("textEntityTypeDateTime", "textEntityTypeBold", "textEntityTypeHashtag",
                     "textEntityTypeMentionName", "textEntityTypePre", "textEntityTypeBlockQuote"):
            for offset, length in ((0, 4), (1, 2), (0, 8), (3, 3)):
                with self.subTest(kind=kind, offset=offset, length=length), self.assertRaises(ValueError):
                    source_from_message(message("soonTail", [span(), span(offset, length, kind=kind)]), CHAT, MESSAGE)
        for text in ("x" * 4097, '"' * 2100, "\x00soon", "\ud800soon", " " * 4):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                source_from_message(message(text, [span(length=1)]), CHAT, MESSAGE)
        with self.assertRaises(ValueError):
            source_from_message(message("x" * 33, [span(i, 1) for i in range(33)]), CHAT, MESSAGE)
        accepted = self.source(message("x" * 32, [span(length=1),
            *[span(i, 1, kind="textEntityTypeBold") for i in range(1, 32)]]))
        self.assertEqual(len(json.loads(accepted.projection_json)["message"]["content"]["text"]["entities"]), 32)
        self.assertFalse(accepted.target().truncated)

    def test_relative_stored_under_any_old_version_or_v11_without_relative_refuses(self):
        # Break: using legacy authority for Relative, or laundering non-Relative through v11.
        projection = json.loads(self.source().projection_json)
        for version in range(1, 11):
            changed = copy.deepcopy(projection)
            changed["version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                ReplySource(encode(changed))
        for entities in ([], [span(formatting=None)], [span(kind="textEntityTypeBold")]):
            changed = copy.deepcopy(projection)
            changed["message"]["content"]["text"]["entities"] = entities
            with self.subTest(entities=entities), self.assertRaises(ValueError):
                ReplySource(encode(changed))
        changed = copy.deepcopy(projection)
        changed["message"]["content"]["text"]["entities"][0]["type"]["formatting_type"]["extra"] = None
        with self.assertRaises(ValueError):
            ReplySource(encode(changed))

    def test_default_formatter_and_legacy_media_versions_remain_null_only(self):
        # Break: enabling Relative by default or laundering it through v10/v11.
        caption = message()["content"]["text"]
        with self.assertRaises(ValueError):
            formatted_text(caption, datetime=True)
        raw = message()
        null_caption = {"@type": "formattedText", "text": "soon", "entities": [span(formatting=None)]}
        raw["content"] = document(null_caption)
        stored = json.loads(source_from_message(raw, CHAT, MESSAGE).projection_json)
        self.assertEqual(stored["version"], 10)
        stored["message"]["content"]["caption"] = caption
        with self.assertRaises(ValueError):
            ReplySource(encode(stored))
        stored["version"] = 11
        with self.assertRaises(ValueError):
            ReplySource(encode(stored))


class RawBoundary:
    def __init__(self):
        self.source = message()
        self.requests = []
        self.events = deque()

    def send(self, request):
        self.requests.append(copy.deepcopy(request))
        kind = request["@type"]
        if kind == "getAuthorizationState":
            result = {"@type": "authorizationStateReady"}
        elif kind == "getMe":
            result = {"@type": "user", "id": 42}
        elif kind == "getChat":
            if request["chat_id"] != CHAT:
                raise AssertionError("unexpected synthetic chat")
            result = {"@type": "chat", "id": CHAT, "title": "Synthetic room",
                      "type": {"@type": "chatTypeSupergroup", "supergroup_id": 72, "is_channel": False}}
        elif kind == "getMessage":
            if (request["chat_id"], request["message_id"]) != (CHAT, MESSAGE):
                raise AssertionError("unexpected synthetic source")
            result = copy.deepcopy(self.source)
        elif kind == "getMessageProperties":
            result = {"@type": "messageProperties", "can_be_replied": True}
        else:
            raise AssertionError("raw effect forbidden in refusal tests: " + kind)
        self.events.append({**result, "@extra": request["@extra"]})

    def receive(self, timeout):
        if self.events:
            return self.events.popleft()
        if timeout != 0:
            raise AssertionError("no synthetic provider wait permitted")
        return None

    def close(self):
        pass


class ApprovalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relative-boundary-")
        self.root = Path(self.temporary.name)
        self.raw = RawBoundary()
        self.provider = TDLibClient(raw=self.raw, session_directory=self.root / "unused-session",
                                   credential_loader=lambda: self.fail("credentials forbidden"))
        self.provider._ready = True
        self.dialogs = []
        self.decision = lambda: True
        def owner(**kwargs):
            self.dialogs.append(copy.deepcopy(kwargs))
            return self.decision()
        self.store = ArtifactStore(cache_dir=self.root / "artifacts")
        payload = self.root / "fixture.txt"
        payload.write_bytes(b"data")
        self.artifact = self.store.store(payload)
        self.broker = Broker(socket_path=self.root / "unused.sock", client_factory=lambda: self.provider,
            artifact_store=self.store, approval_prompt=owner, policy=RuntimePolicy(enabled_capabilities=CAPABILITIES))
        self.stack = ExitStack()
        staging = self.root / "staging"
        self.stack.enter_context(patch("telegram_search_mcp.tdjson.STAGING_ROOT", staging))
        self.stack.enter_context(patch("telegram_search_mcp.broker.stage_approved_document",
            functools.partial(stage_approved_document, root=staging)))
        self.stack.enter_context(patch("telegram_search_mcp.broker.retire_staged_document",
            functools.partial(retire_staged_document, root=staging)))

    def tearDown(self):
        self.stack.close()
        self.broker.shutdown()
        self.broker._executor.shutdown(wait=True)
        self.provider.close()
        self.temporary.cleanup()

    def prepare(self, family):
        anchor = {"chat_id": CHAT, "message_id": MESSAGE}
        if family == "text":
            return self.broker._prepare_reply_text_send(
                PrepareReplyTextSendRequest(recipient=CHAT, text="Plain", reply_to=anchor), client_id=CLIENT)
        return self.broker._prepare_reply_artifact_send(PrepareReplyArtifactSendRequest(recipient=CHAT,
            artifact_id=self.artifact.artifact_id, display_name="fixture.txt", mime_type="text/plain",
            kind="document", caption="Plain", reply_to=anchor), client_id=CLIENT)

    def inspect(self, draft_id, family):
        if family == "text":
            return self.broker._get_reply_draft(GetReplyDraftRequest(draft_id=draft_id), client_id=CLIENT)
        return self.broker._get_reply_artifact_draft(GetReplyArtifactDraftRequest(draft_id=draft_id), client_id=CLIENT)

    def send(self, draft_id, family, approved=True):
        if family == "text":
            return self.broker._send_prepared_text(SendPreparedTextRequest(draft_id=draft_id,
                approved=approved), client_id=CLIENT)
        return self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=draft_id,
            approved=approved), client_id=CLIENT)

    def test_exact_mixed_authority_and_unrelated_capability_absence(self):
        # Break: overlooking any present family, or requiring authority for absent ones.
        for family in ("text", "artifact"):
            self.raw.source = message("soon#tag@user", [span(), span(4, 4, kind="textEntityTypeHashtag"),
                                     span(8, 5, kind="textEntityTypeMentionName")])
            for missing in ("reply_formatted_targets", "reply_datetime_targets", "reply_lexical_targets",
                            "reply_identity_targets"):
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                with self.subTest(family=family, missing=missing):
                    self.assertEqual(self.prepare(family).model_dump(mode="json"),
                        {"status": "unavailable", "reply": None, "detail": "reply draft is unavailable"})
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
            self.assertEqual(self.prepare(family).status, "prepared")
            self.raw.source = message()
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES[:5])
            self.assertEqual(self.prepare(family).status, "prepared")
        self.assertEqual(self.dialogs, [])
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.requests))

    def test_owner_rendered_source_or_constructor_change_fails_terminally_in_both_workflows(self):
        # Break: trusting old approval after timestamp, raw text, or constructor drift.
        for family in ("text", "artifact"):
            for mutation in ("timestamp", "text", "null", "open_relative", "absolute"):
                with self.subTest(family=family, mutation=mutation):
                    self.raw.source = message()
                    prepared = self.prepare(family)
                    self.assertEqual(prepared.status, "prepared")
                    draft_id = prepared.reply.draft.draft_id
                    def drift():
                        typ = self.raw.source["content"]["text"]["entities"][0]["type"]
                        if mutation == "timestamp":
                            typ["unix_time"] += 1
                        elif mutation == "text":
                            self.raw.source["content"]["text"]["text"] = "late"
                        elif mutation == "null":
                            typ["formatting_type"] = None
                        elif mutation == "open_relative":
                            typ["formatting_type"]["extra"] = False
                        else:
                            typ["formatting_type"] = {"@type": "dateTimeFormattingTypeAbsolute"}
                        return True
                    self.decision = drift
                    self.assertEqual(self.send(draft_id, family).status, "failed")
                    key = "reply_preview" if family == "text" else "reply_artifact_preview"
                    self.assertEqual(self.dialogs[-1][key], prepared.reply.model_dump(mode="json"))
                    self.assertIn('"formatting_type":{"@type":"dateTimeFormattingTypeRelative"}',
                                  prepared.reply.reply_target.text)
                    self.assertEqual(self.send(draft_id, family).status, "failed")
                    self.assertEqual(self.inspect(draft_id, family).status, "unavailable")
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.requests))

    def test_update_keeps_source_and_refresh_rebinds_constructor_under_fresh_approval(self):
        # Break: re-reading during update or transferring approval through Relative-to-null refresh.
        for family in ("text", "artifact"):
            self.raw.source = message()
            prepared = self.prepare(family)
            self.assertEqual(prepared.status, "prepared")
            old = prepared.reply.draft.draft_id
            self.raw.source["content"]["text"]["entities"][0]["type"]["formatting_type"] = None
            reads = len([r for r in self.raw.requests if r["@type"] == "getMessage"])
            if family == "text":
                updated = self.broker._revise_reply_draft(UpdateReplyDraftRequest(draft_id=old, text="Changed"), client_id=CLIENT)
            else:
                updated = self.broker._revise_reply_artifact_draft(UpdateReplyArtifactDraftRequest(draft_id=old, caption="Changed"), client_id=CLIENT)
            self.assertEqual(updated.status, "revised")
            self.assertEqual(updated.reply.reply_target, prepared.reply.reply_target)
            self.assertEqual(len([r for r in self.raw.requests if r["@type"] == "getMessage"]), reads)
            self.assertEqual(self.send(old, family).status, "expired")
            live = updated.reply.draft.draft_id
            if family == "text":
                refreshed = self.broker._revise_reply_draft(RefreshReplyDraftRequest(draft_id=live), client_id=CLIENT)
            else:
                refreshed = self.broker._revise_reply_artifact_draft(RefreshReplyArtifactDraftRequest(draft_id=live), client_id=CLIENT)
            self.assertEqual(refreshed.status, "revised")
            self.assertNotEqual(refreshed.reply.reply_target.source_sha256, prepared.reply.reply_target.source_sha256)
            self.assertNotEqual(refreshed.reply.preview_sha256, updated.reply.preview_sha256)
            self.assertEqual(self.send(live, family).status, "expired")
            self.assertEqual(self.send(refreshed.reply.draft.draft_id, family, approved=False).status, "declined")
            self.assertEqual(self.inspect(refreshed.reply.draft.draft_id, family).reply, refreshed.reply)
        self.assertEqual(self.dialogs, [])
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.requests))

    def test_datetime_capability_revoked_at_owner_confirmation_never_claims_or_sends(self):
        # Break: assuming immutable source authority remains current through local owner approval.
        for family in ("text", "artifact"):
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
            prepared = self.prepare(family)
            self.assertEqual(prepared.status, "prepared")
            def revoke():
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != "reply_datetime_targets"))
                return True
            self.decision = revoke
            self.assertEqual(self.send(prepared.reply.draft.draft_id, family).status, "expired")
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.requests))
