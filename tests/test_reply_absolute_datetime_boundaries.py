"""Fresh Absolute evidence through real broker/registry/TDJSON components.

Only the closed raw provider and local owner decision are synthetic. The
fixture module belongs to this same new slice, not a historical helper.
"""
import copy
import functools
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
    GetReplyDraftRequest, PrepareReplyTextSendRequest, RefreshReplyDraftRequest, UpdateReplyDraftRequest,
)
from telegram_search_mcp.schemas import SendPreparedArtifactRequest, SendPreparedTextRequest
from telegram_search_mcp.tdjson import TDLibClient
from test_reply_absolute_datetime_targets import CHAT, MESSAGE, PRECISIONS, RELATIVE, absolute, message, span

CLIENT = "client_" + "f" * 32
CAPABILITIES = ("send", "reply_text_send", "reply_artifact_send", "reply_formatted_targets",
                "reply_datetime_targets", "reply_lexical_targets", "reply_identity_targets")
SOURCE_CAPABILITIES = CAPABILITIES[3:]


def mixed():
    return message("soon#tag@user", [span(), span(4, 4, kind="textEntityTypeHashtag"),
                   span(8, 5, kind="textEntityTypeMentionName")])


class ClosedRaw:
    def __init__(self):
        self.source = mixed()
        self.requests = []
        self.events = deque()

    def send(self, request):
        self.requests.append(copy.deepcopy(request))
        kind = request["@type"]
        if kind == "getAuthorizationState": result = {"@type": "authorizationStateReady"}
        elif kind == "getMe": result = {"@type": "user", "id": 42}
        elif kind == "getChat":
            if request["chat_id"] != CHAT: raise AssertionError("unexpected synthetic chat")
            result = {"@type": "chat", "id": CHAT, "title": "Synthetic Absolute room",
                      "type": {"@type": "chatTypeSupergroup", "supergroup_id": 73, "is_channel": False}}
        elif kind == "getMessage":
            if (request["chat_id"], request["message_id"]) != (CHAT, MESSAGE):
                raise AssertionError("unexpected synthetic source")
            result = copy.deepcopy(self.source)
        elif kind == "getMessageProperties": result = {"@type": "messageProperties", "can_be_replied": True}
        else: raise AssertionError("raw effect forbidden in boundary risk checks: " + kind)
        self.events.append({**result, "@extra": request["@extra"]})

    def receive(self, timeout):
        if self.events: return self.events.popleft()
        if timeout != 0: raise AssertionError("no synthetic provider wait")
        return None

    def close(self): pass


class AbsoluteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="absolute-boundary-")
        self.root = Path(self.temporary.name)
        self.raw = ClosedRaw()
        self.provider = TDLibClient(raw=self.raw, session_directory=self.root / "unused-session",
                                   credential_loader=lambda: self.fail("credentials forbidden"))
        self.provider._ready = True
        self.dialogs = []
        self.decision = lambda: True
        def owner(**kwargs):
            self.dialogs.append(copy.deepcopy(kwargs))
            return self.decision()
        self.store = ArtifactStore(cache_dir=self.root / "artifacts")
        payload = self.root / "fixture.txt"; payload.write_bytes(b"data")
        self.artifact = self.store.store(payload)
        self.broker = Broker(socket_path=self.root / "unused.sock", client_factory=lambda: self.provider,
            artifact_store=self.store, approval_prompt=owner, policy=RuntimePolicy(enabled_capabilities=CAPABILITIES))
        self.stack = ExitStack(); self.staging = self.root / "staging"
        self.stack.enter_context(patch("telegram_search_mcp.tdjson.STAGING_ROOT", self.staging))
        self.stack.enter_context(patch("telegram_search_mcp.broker.stage_approved_document",
            functools.partial(stage_approved_document, root=self.staging)))
        self.stack.enter_context(patch("telegram_search_mcp.broker.retire_staged_document",
            functools.partial(retire_staged_document, root=self.staging)))

    def tearDown(self):
        self.stack.close(); self.broker.shutdown(); self.broker._executor.shutdown(wait=True)
        self.provider.close(); self.temporary.cleanup()

    def prepare(self, family):
        anchor = {"chat_id": CHAT, "message_id": MESSAGE}
        if family == "text":
            return self.broker._prepare_reply_text_send(PrepareReplyTextSendRequest(
                recipient=CHAT, text="Plain", reply_to=anchor), client_id=CLIENT)
        return self.broker._prepare_reply_artifact_send(PrepareReplyArtifactSendRequest(recipient=CHAT,
            artifact_id=self.artifact.artifact_id, display_name="fixture.txt", mime_type="text/plain",
            kind="document", caption="Plain", reply_to=anchor), client_id=CLIENT)

    def inspect(self, draft_id, family):
        if family == "text":
            return self.broker._get_reply_draft(GetReplyDraftRequest(draft_id=draft_id), client_id=CLIENT)
        return self.broker._get_reply_artifact_draft(GetReplyArtifactDraftRequest(draft_id=draft_id), client_id=CLIENT)

    def send(self, draft_id, family, approved=True):
        if family == "text": return self.broker._send_prepared_text(
            SendPreparedTextRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)
        return self.broker._send_prepared_artifact(
            SendPreparedArtifactRequest(draft_id=draft_id, approved=approved), client_id=CLIENT)

    def revise(self, draft_id, family, refresh=False):
        if family == "text":
            request = RefreshReplyDraftRequest(draft_id=draft_id) if refresh else UpdateReplyDraftRequest(draft_id=draft_id, text="Changed")
            return self.broker._revise_reply_draft(request, client_id=CLIENT)
        request = RefreshReplyArtifactDraftRequest(draft_id=draft_id) if refresh else UpdateReplyArtifactDraftRequest(draft_id=draft_id, caption="Changed")
        return self.broker._revise_reply_artifact_draft(request, client_id=CLIENT)

    def prepared(self, family):
        result = self.prepare(family)
        self.assertEqual(result.status, "prepared", "complete Absolute messageText must reach immutable broker draft")
        self.assertIn('"show_day_of_week":false', result.reply.reply_target.text)
        return result

    def no_effects(self):
        self.assertFalse(any(r["@type"] == "sendMessage" for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def test_each_of_four_required_capabilities_absent_at_prepare_refuses_both_families(self):
        # Break: missing conjunction for mixed Absolute sources or preparation before authority.
        for family in ("text", "artifact"):
            for missing in SOURCE_CAPABILITIES:
                with self.subTest(family=family, missing=missing):
                    self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                    self.assertEqual(self.prepare(family).status, "unavailable")
            self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
            self.prepared(family)
        self.assertEqual(self.dialogs, []); self.no_effects()

    def test_each_capability_revoked_before_inspect_update_refresh_or_send_refuses_without_read(self):
        # Break: assuming capabilities are only required at preparation.
        for family in ("text", "artifact"):
            for missing in SOURCE_CAPABILITIES:
                self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                draft_id = self.prepared(family).reply.draft.draft_id
                self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                before = sum(r["@type"] == "getMessage" for r in self.raw.requests)
                with self.subTest(family=family, missing=missing):
                    self.assertEqual(self.inspect(draft_id, family).status, "unavailable")
                    self.assertEqual(self.revise(draft_id, family).status, "unavailable")
                    self.assertEqual(self.revise(draft_id, family, True).status, "unavailable")
                    self.assertEqual(self.send(draft_id, family).status, "expired")
                    self.assertEqual(sum(r["@type"] == "getMessage" for r in self.raw.requests), before)
        self.assertEqual(self.dialogs, []); self.no_effects()

    def test_each_capability_revoked_during_owner_confirmation_prevents_claim_and_staging(self):
        # Break: source authority loses validity while exact owner preview is shown.
        for family in ("text", "artifact"):
            for missing in SOURCE_CAPABILITIES:
                self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                prepared = self.prepared(family)
                def revoke():
                    self.broker._policy = RuntimePolicy(enabled_capabilities=tuple(c for c in CAPABILITIES if c != missing))
                    return True
                self.decision = revoke
                with self.subTest(family=family, missing=missing):
                    self.assertEqual(self.send(prepared.reply.draft.draft_id, family).status, "expired")
        self.no_effects()

    def test_nested_precision_weekday_timestamp_or_raw_text_drift_is_terminal_before_send(self):
        # Break: reread digest ignores nested Absolute semantics after owner approval.
        for family in ("text", "artifact"):
            for mutation in ("time_precision", "date_precision", "show_day_of_week", "timestamp", "text", "open", "relative"):
                self.raw.source = mixed(); self.broker._policy = RuntimePolicy(enabled_capabilities=CAPABILITIES)
                prepared = self.prepared(family); draft_id = prepared.reply.draft.draft_id
                def drift():
                    typ = self.raw.source["content"]["text"]["entities"][0]["type"]
                    fmt = typ["formatting_type"]
                    if mutation in ("time_precision", "date_precision"): fmt[mutation]["@type"] = PRECISIONS[0]
                    elif mutation == "show_day_of_week": fmt[mutation] = True
                    elif mutation == "timestamp": typ["unix_time"] += 1
                    elif mutation == "text": self.raw.source["content"]["text"]["text"] = "late#tag@user"
                    elif mutation == "open": fmt["date_precision"]["extra"] = None
                    else: typ["formatting_type"] = copy.deepcopy(RELATIVE)
                    return True
                self.decision = drift
                with self.subTest(family=family, mutation=mutation):
                    self.assertEqual(self.send(draft_id, family).status, "failed")
                    key = "reply_preview" if family == "text" else "reply_artifact_preview"
                    self.assertEqual(self.dialogs[-1][key], prepared.reply.model_dump(mode="json"))
                    self.assertEqual(self.send(draft_id, family).status, "failed")
                    self.assertEqual(self.inspect(draft_id, family).status, "unavailable")
        self.no_effects()

    def test_update_retains_source_and_refresh_rebinds_absolute_relative_null_absolute(self):
        # Break: outgoing update rereads source or refresh carries old approval into new evidence.
        for family in ("text", "artifact"):
            self.raw.source = mixed(); original = self.prepared(family)
            old = original.reply.draft.draft_id
            self.raw.source["content"]["text"]["entities"][0]["type"]["formatting_type"] = copy.deepcopy(RELATIVE)
            reads = sum(r["@type"] == "getMessage" for r in self.raw.requests)
            updated = self.revise(old, family)
            self.assertEqual(updated.status, "revised")
            self.assertEqual(updated.reply.reply_target, original.reply.reply_target)
            self.assertEqual(sum(r["@type"] == "getMessage" for r in self.raw.requests), reads)
            self.assertEqual(self.send(old, family).status, "expired")
            live = updated.reply
            for fmt, text in ((RELATIVE, '"dateTimeFormattingTypeRelative"'), (None, '"formatting_type":null'),
                              (absolute(weekday=True), '"show_day_of_week":true')):
                self.raw.source["content"]["text"]["entities"][0]["type"]["formatting_type"] = copy.deepcopy(fmt)
                refreshed = self.revise(live.draft.draft_id, family, True)
                self.assertEqual(refreshed.status, "revised")
                self.assertIn(text, refreshed.reply.reply_target.text)
                self.assertNotEqual(refreshed.reply.reply_target.source_sha256, live.reply_target.source_sha256)
                self.assertNotEqual(refreshed.reply.preview_sha256, live.preview_sha256)
                self.assertEqual(self.send(live.draft.draft_id, family).status, "expired")
                live = refreshed.reply
            self.assertEqual(self.send(live.draft.draft_id, family, False).status, "declined")
            self.decision = lambda: False
            self.assertEqual(self.send(live.draft.draft_id, family).status, "declined")
            self.assertEqual(self.inspect(live.draft.draft_id, family).reply, live)
        self.no_effects()
