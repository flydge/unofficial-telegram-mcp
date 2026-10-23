"""In-process, single-use approvals for sending cached local artifacts.

The broker owns the actual TDLib call. A claim marks the attempt before the
broker invokes it, so a lost or uncertain response cannot be retried here.
"""

from __future__ import annotations

import re
import hashlib
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

from .artifact_store import ArtifactStore, ArtifactStoreError, DOCUMENT_LIMIT
from .artifact_authority import active_scope, ArtifactUnavailable
from .outgoing_media import PHOTO_LIMIT
from .outgoing_voice import MAX_VOICE_BYTES
from .sanitize import sanitize_telegram_text
from .reply_drafts import ReplySource
from .topic_models import TopicSnapshot
from .outgoing_formatting import FrozenOutgoingEntities, freeze_outgoing_entities

DRAFT_TTL_SECONDS = 15 * 60
DEFAULT_CAPACITY = 4096
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}\Z")
ReceiptStatus = Literal["sent", "failed", "outcome_unknown", "applied"]


class DraftError(RuntimeError):
    """The draft cannot proceed through the requested transition."""


def _safe_display_name(display_name: str) -> str:
    # NFKC before splitting: a compatibility solidus (U+FF0F, U+FF3C, ...) must not
    # become a path separator only after the split.
    normalized = unicodedata.normalize("NFKC", display_name)
    safe_name = sanitize_telegram_text(
        normalized.replace("\\", "/").rsplit("/", 1)[-1], max_length=255
    )
    if safe_name in {"", ".", ".."}:
        safe_name = "attachment"
    if len(safe_name.encode("utf-8")) > 240:
        raise ValueError("display_name is too long for local staging")
    return safe_name


# The pinned TDLib's clean_input_string (td/telegram/misc.cpp at d1085f9) turns a tab into a space and removes
# U+2028, U+2029 and the combining marks U+030A, U+0333 and U+033F, and fix_formatted_text trims only spaces and
# line feeds at both ends (td/telegram/MessageEntity.cpp at d1085f9).
# Applying the same rewrite before the preview makes the reviewed text the stored text (owner decision 20.10.2026).
_TELEGRAM_REWRITE = {ord("\t"): " ", **dict.fromkeys(map(ord, "\u2028\u2029\u030a\u0333\u033f"))}


def _telegram_stored(text: str) -> str:
    """The text Telegram stores for an NFKC, LF-only input: rewritten, NFKC-stable and edge-trimmed."""
    for _ in range(4):  # NFKC can reintroduce a removed mark (U+02DA is a space plus U+030A); rounds settle it.
        rewritten = unicodedata.normalize("NFKC", text.translate(_TELEGRAM_REWRITE))
        if rewritten == text:
            return text.strip(" \n")
        text = rewritten
    raise ValueError("text does not settle under Telegram's rewrite")


def _safe_caption(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    result = _telegram_stored("".join(character for character in normalized
                                      if character == "\n" or character == "\t" or
                                      not unicodedata.category(character).startswith("C")))
    if len(result) > 1024:
        raise ValueError("caption exceeds 1024 characters after normalization")
    return result


def _safe_message_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    result = unicodedata.normalize("NFKC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if any(unicodedata.category(character).startswith("C") and character not in "\n\t" for character in result):
        raise ValueError("text is empty, too long, or contains control characters")
    result = _telegram_stored(result)
    if len(result) > 4096 or not result:
        raise ValueError("text is empty, too long, or contains control characters")
    return result


@dataclass(frozen=True)
class DraftOwner:
    client_id: str
    account_id: int

    def __post_init__(self) -> None:
        if (type(self.client_id) is not str or
                re.fullmatch(r"client_[A-Za-z0-9_-]{24,80}", self.client_id) is None or
                type(self.account_id) is not int or not 0 < self.account_id < 2**53):
            raise ValueError("invalid draft owner")


@dataclass(frozen=True)
class OutgoingDraft:
    draft_id: str
    artifact_id: str
    sha256: str
    size_bytes: int
    display_name: str
    mime_type: str
    caption: str
    recipient: int
    expires_at: float
    recipient_title: str = ""
    approval_required: bool = True
    kind: Literal["document", "photo", "voice_note", "text", "forward", "video", "archive", "text_edit",
                  "album"] = "document"
    text: str = ""
    duration_seconds: int | None = None
    waveform_base64: str | None = None
    source_sha256: str | None = None
    source_display_name: str | None = None
    converted: bool = False
    reply_source: ReplySource | None = None
    topic: TopicSnapshot | None = None
    topic_provider: object | None = None
    topic_provider_epoch: object | None = None
    outgoing_entities: FrozenOutgoingEntities = ()
    formatting_provider: object | None = None
    formatting_provider_epoch: object | None = None
    forward: object | None = None  # forward_drafts.ForwardSpec; never artifact-backed
    video: object | None = None  # video_drafts.VideoSpec; artifact-backed
    archive: object | None = None  # archive_drafts.ArchiveSpec; never artifact-backed
    text_edit: object | None = None  # edit_drafts.TextEditSpec; never artifact-backed
    album: object | None = None  # album_drafts.AlbumSpec; every item artifact-backed


# Provider state changes with a dedicated apply tool; a receipt may be `applied`.
_CHANGE_KINDS = frozenset({"archive", "text_edit"})
_UNBACKED_KINDS = frozenset({"text", "forward"}) | _CHANGE_KINDS
# Kinds with a dedicated send tool; the generic draft tools never list, revise or cancel them.
# The 0.57 draft-kind table has not landed: the album kind sits in this dedicated set (0.56 spec).
_DEDICATED_KINDS = frozenset({"forward", "video", "album"}) | _CHANGE_KINDS
_SLOT_OUTCOMES = frozenset({"sent", "failed", "outcome_unknown"})


@dataclass(frozen=True)
class ClaimedDraft:
    draft: OutgoingDraft
    path: Path | None
    claim_token: object | None = None
    provider: object | None = None
    provider_epoch: object | None = None
    paths: tuple[Path, ...] | None = None  # An album's verified item artifacts, in send order.


@dataclass(frozen=True)
class SendReceipt:
    draft_id: str
    status: ReceiptStatus
    message_id: int | None
    finished_at: float
    evidence: str = "none"
    observed: object | None = None  # `applied` only: the provider state observed right after the call.
    # An album only: one (status, final message ID) per item in send order; `status` is their reduction.
    slots: tuple[tuple[str, int | None], ...] | None = None


def _album_slots(slots) -> tuple[tuple[str, int | None], ...]:
    """Validated internal album slots: 2-10 pairs, each sent with a final ID, or failed/unknown without one."""
    if (type(slots) is not tuple or not 2 <= len(slots) <= 10 or
            any(type(slot) is not tuple or len(slot) != 2 or slot[0] not in _SLOT_OUTCOMES or
                (slot[0] == "sent") != (slot[1] is not None) or
                (slot[1] is not None and (type(slot[1]) is not int or not 0 < slot[1] < 2**53)) for slot in slots)):
        raise ValueError("invalid album slots")
    return slots


@dataclass
class _Entry:
    draft: OutgoingDraft
    owner: DraftOwner
    state: str = "pending"
    receipt: SendReceipt | None = None
    artifact_expires_at: float | None = None
    attempted_at: float | None = None
    provider: object | None = None
    provider_epoch: object | None = None
    artifact_authority: tuple | None = None
    claim_token: object | None = None


class OutgoingDraftRegistry:
    def __init__(
        self,
        store: ArtifactStore,
        *,
        clock: Callable[[], float] = time.time,
        capacity: int = DEFAULT_CAPACITY,
        attempt_clock: Callable[[], float] = time.monotonic,
        max_ttl_seconds: int = DRAFT_TTL_SECONDS,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if type(max_ttl_seconds) is not int or not 1 <= max_ttl_seconds <= 86400:
            raise ValueError("draft TTL must be an integer from 1 to 86400 seconds")
        self._store = store
        self._clock = clock
        self._attempt_clock = attempt_clock
        self._capacity = capacity
        self._max_ttl_seconds = max_ttl_seconds
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _owned(self, draft_id: object, owner: DraftOwner) -> _Entry | None:
        """The entry only for its own DraftOwner: the client and the account (re-login hardening, 0.55 spec 3C)."""
        entry = self._entries.get(draft_id) if isinstance(draft_id, str) else None
        return entry if entry is not None and type(owner) is DraftOwner and entry.owner == owner else None

    def is_owned_artifact_reply(self, draft_id: str, *, owner: DraftOwner) -> bool:
        """Private budget routing only; never exposes draft/account facts."""
        with self._lock:
            entry = self._owned(draft_id, owner)
            return entry is not None and entry.draft.reply_source is not None and entry.draft.kind != 'text'

    def is_owned_topic(self, draft_id: str, *, owner: DraftOwner) -> bool:
        """Private budget routing only; never exposes draft/account facts."""
        with self._lock:
            entry = self._owned(draft_id, owner)
            return entry is not None and entry.draft.topic is not None

    def is_owned_formatted(self, draft_id: str, *, owner: DraftOwner) -> bool:
        """Private budget routing only; no account or draft disclosure."""
        with self._lock:
            entry = self._owned(draft_id, owner)
            return bool(entry is not None and entry.draft.outgoing_entities)

    def prepare(
        self,
        *,
        owner: DraftOwner,
        artifact_id: str,
        recipient: int,
        recipient_title: str = "",
        display_name: str,
        mime_type: str,
        caption: str,
        kind: Literal["document", "photo", "voice_note"] = "document",
        duration_seconds: int | None = None,
        waveform_base64: str | None = None,
        source_sha256: str | None = None,
        source_display_name: str | None = None,
        converted: bool = False,
        reply_source: ReplySource | None = None,
        local_admission_guard: Callable[[], None] | None = None,
    ) -> OutgoingDraft:
        """Record the exact server-issued artifact and proposed send details."""
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if kind not in {"document", "photo", "voice_note"}:
            raise ValueError("unsupported send kind")
        if not isinstance(display_name, str):
            raise ValueError("display_name must be a string")
        safe_name = _safe_display_name(display_name)
        if not isinstance(mime_type, str) or len(mime_type) > 127 or not _MIME_TYPE.fullmatch(mime_type):
            raise ValueError("mime_type must be a valid media type")
        if not isinstance(caption, str) or len(caption) > 1024 or "\x00" in caption:
            raise ValueError("caption must be text of at most 1024 characters")
        safe_caption = _safe_caption(caption)
        if reply_source is not None and (not isinstance(reply_source, ReplySource) or
                reply_source.anchor[0] != recipient or reply_source.forum_topic_id is not None):
            raise DraftError("draft is unavailable")
        try:
            artifact = self._store.lookup(artifact_id)
        except ArtifactStoreError as error:
            raise DraftError("artifact is unavailable") from error
        if artifact is None:
            raise DraftError("artifact is unavailable")
        limit = {"document": DOCUMENT_LIMIT, "photo": PHOTO_LIMIT,
                 "voice_note": MAX_VOICE_BYTES}[kind]
        if not 0 < artifact.size_bytes <= limit:
            raise DraftError("artifact exceeds send limit")
        now = self._clock()
        expires_at = min(now + self._max_ttl_seconds, artifact.expires_at)
        if expires_at <= now:
            raise DraftError("artifact has expired")
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            if local_admission_guard is not None:
                local_admission_guard()
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            draft_id = f"draft_{uuid.uuid4().hex}"
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            draft = OutgoingDraft(
                draft_id=draft_id,
                artifact_id=artifact_id,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                display_name=safe_name,
                mime_type=mime_type,
                caption=safe_caption,
                recipient=recipient,
                expires_at=expires_at,
                recipient_title=recipient_title,
                kind=kind,
                duration_seconds=duration_seconds,
                waveform_base64=waveform_base64,
                source_sha256=source_sha256,
                source_display_name=source_display_name,
                converted=converted, reply_source=reply_source,
            )
            self._entries[draft_id] = _Entry(draft=draft, owner=owner,
                                            artifact_expires_at=artifact.expires_at,
                                            artifact_authority=self._authority_stamp())
            record_selection_draft(draft_id)
            return draft

    def prepare_text(self, *, owner: DraftOwner, recipient: int, text: str, recipient_title: str = "", reply_source: ReplySource | None = None,
                     topic: TopicSnapshot | None = None, topic_provider: object | None = None,
                     topic_provider_epoch: object | None = None, outgoing_entities: list | None = None,
                     formatting_provider: object | None = None, formatting_provider_epoch: object | None = None) -> OutgoingDraft:
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if reply_source is not None and (not isinstance(reply_source, ReplySource) or reply_source.anchor[0] != recipient):
            raise DraftError("draft is unavailable")
        if topic is not None and (not isinstance(topic, TopicSnapshot) or
                topic.account_id != owner.account_id or topic.chat_id != recipient or
                topic_provider is None or topic_provider_epoch is None):
            raise DraftError("topic draft is unavailable")
        if topic is None and (topic_provider is not None or topic_provider_epoch is not None):
            raise DraftError("plain draft contains topic authority")
        if reply_source is not None and reply_source.forum_topic_id != (
                topic.forum_topic_id if topic is not None else None):
            raise DraftError("reply source and topic identity disagree")
        safe_text = _safe_message_text(text)
        entities = freeze_outgoing_entities(safe_text, [] if outgoing_entities is None else outgoing_entities)
        if entities and (formatting_provider is None or formatting_provider_epoch is None):
            raise DraftError('formatting authority is unavailable')
        if not entities and (formatting_provider is not None or formatting_provider_epoch is not None):
            raise DraftError('plain text contains formatting authority')
        if entities and topic is not None and (formatting_provider is not topic_provider or
                formatting_provider_epoch is not topic_provider_epoch):
            raise DraftError('text provider authority is inconsistent')
        now = self._clock()
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            draft_id = f"draft_{uuid.uuid4().hex}"
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            draft = OutgoingDraft(
                draft_id=draft_id, artifact_id="", sha256=hashlib.sha256(safe_text.encode("utf-8")).hexdigest(),
                size_bytes=len(safe_text.encode("utf-8")), display_name="Text message", mime_type="text/plain",
                caption="", recipient=recipient, expires_at=now + self._max_ttl_seconds,
                kind="text", text=safe_text, recipient_title=recipient_title, reply_source=reply_source,
                topic=topic, topic_provider=topic_provider, topic_provider_epoch=topic_provider_epoch,
                outgoing_entities=entities, formatting_provider=formatting_provider,
                formatting_provider_epoch=formatting_provider_epoch,
            )
            self._entries[draft_id] = _Entry(draft=draft, owner=owner)
            record_selection_draft(draft_id)
            return draft

    def prepare_forward(self, *, owner: DraftOwner, recipient: int, recipient_title: str,
                        forward: object, evidence: Callable[[OutgoingDraft], None] | None = None) -> OutgoingDraft:
        """Record one exact forward/copy; generic draft tools never expose this kind.

        `evidence` builds and validates the public preview of the exact candidate before
        insertion; if it raises, nothing is inserted and no pending draft is superseded."""
        from .forward_drafts import ForwardSpec
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if not isinstance(forward, ForwardSpec) or type(recipient_title) is not str or not recipient_title:
            raise DraftError("forward draft is unavailable")
        projection = forward.snapshot.source.projection_json.encode("utf-8")
        now = self._clock()
        draft = OutgoingDraft(
            draft_id=f"draft_{uuid.uuid4().hex}", artifact_id="", sha256=forward.snapshot.source.source_sha256,
            size_bytes=len(projection), display_name="Forwarded message", mime_type="application/json",
            caption="", recipient=recipient, expires_at=now + self._max_ttl_seconds,
            recipient_title=recipient_title, kind="forward", forward=forward,
        )
        if evidence is not None:
            evidence(draft)
        draft_id = draft.draft_id
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            # Preparing again supersedes the same owner's pending draft for the same
            # source, recipient, mode and caption choice; it can no longer be claimed.
            for entry in self._entries.values():
                old = entry.draft
                if (entry.owner == owner and entry.state == "pending" and old.kind == "forward" and
                        old.recipient == recipient and old.forward.supersede_key == forward.supersede_key):
                    entry.state = "superseded"
            self._entries[draft_id] = _Entry(draft=draft, owner=owner)
            record_selection_draft(draft_id)
            return draft

    def prepare_video(self, *, owner: DraftOwner, artifact_id: str, recipient: int, recipient_title: str,
                      display_name: str, caption: str, video: object,
                      local_admission_guard: Callable[[], None] | None = None,
                      evidence: Callable[[OutgoingDraft], None] | None = None) -> OutgoingDraft:
        """Record one native video bound to the exact probed artifact bytes.

        `evidence` builds and validates the public preview of the exact candidate outside the
        registry lock; if it raises, nothing is inserted and no pending draft is superseded."""
        from .video_drafts import MAX_VIDEO_BYTES, VideoSpec
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if (not isinstance(video, VideoSpec) or type(recipient_title) is not str or not recipient_title or
                not isinstance(display_name, str) or not isinstance(caption, str) or len(caption) > 1024 or
                "\x00" in caption):
            raise DraftError("video draft is unavailable")
        safe_name = _safe_display_name(display_name)
        safe_caption = _safe_caption(caption)
        mime_type = "video/quicktime" if safe_name.casefold().endswith(".mov") else "video/mp4"
        try:
            artifact = self._store.lookup(artifact_id)
        except ArtifactStoreError as error:
            raise DraftError("artifact is unavailable") from error
        if (artifact is None or artifact.sha256 != video.sha256 or artifact.size_bytes != video.size_bytes or
                not 0 < artifact.size_bytes <= MAX_VIDEO_BYTES):
            raise DraftError("artifact is unavailable")
        now = self._clock()
        expires_at = min(now + self._max_ttl_seconds, artifact.expires_at)
        if expires_at <= now:
            raise DraftError("artifact has expired")
        draft = OutgoingDraft(
            draft_id=f"draft_{uuid.uuid4().hex}", artifact_id=artifact_id, sha256=artifact.sha256,
            size_bytes=artifact.size_bytes, display_name=safe_name, mime_type=mime_type, caption=safe_caption,
            recipient=recipient, expires_at=expires_at, recipient_title=recipient_title, kind="video", video=video,
        )
        if evidence is not None:
            evidence(draft)
        draft_id = draft.draft_id
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            if local_admission_guard is not None:
                local_admission_guard()
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            # Preparing again supersedes the same owner's pending draft for the same
            # artifact and recipient; it can no longer be claimed.
            for entry in self._entries.values():
                old = entry.draft
                if (entry.owner == owner and entry.state == "pending" and old.kind == "video" and
                        old.recipient == recipient and old.artifact_id == artifact_id):
                    entry.state = "superseded"
            self._entries[draft_id] = _Entry(draft=draft, owner=owner, artifact_expires_at=artifact.expires_at,
                                            artifact_authority=self._authority_stamp())
            record_selection_draft(draft_id)
            return draft

    def prepare_album(self, *, owner: DraftOwner, recipient: int, recipient_title: str, album: object,
                      local_admission_guard: Callable[[], None] | None = None,
                      evidence: Callable[[OutgoingDraft], None] | None = None) -> OutgoingDraft:
        """Record one ordered album bound to the exact bytes of every item.

        The draft's sha256 is the ordered album digest and its size the total. `evidence` builds and validates
        the public preview of the exact candidate outside the registry lock; if it raises, nothing is inserted
        and no pending draft is superseded."""
        from .album_drafts import AlbumSpec
        self._validate_owner(owner)
        if type(recipient) is not int or recipient == 0:
            raise ValueError("recipient must be a nonzero numeric chat ID")
        if type(album) is not AlbumSpec or type(recipient_title) is not str or not recipient_title:
            raise DraftError("album draft is unavailable")
        expires = []
        for item in album.items:
            try:
                artifact = self._store.lookup(item.artifact_id)
            except ArtifactStoreError as error:
                raise DraftError("artifact is unavailable") from error
            if artifact is None or artifact.sha256 != item.sha256 or artifact.size_bytes != item.size_bytes:
                raise DraftError("artifact is unavailable")
            expires.append(artifact.expires_at)
        now = self._clock()
        expires_at = min(now + self._max_ttl_seconds, *expires)
        if expires_at <= now:
            raise DraftError("artifact has expired")
        draft = OutgoingDraft(
            draft_id=f"draft_{uuid.uuid4().hex}", artifact_id="", sha256=album.sha256, size_bytes=album.total_bytes,
            display_name="Album", mime_type="application/json", caption="", recipient=recipient,
            expires_at=expires_at, recipient_title=recipient_title, kind="album", album=album,
        )
        if evidence is not None:
            evidence(draft)
        draft_id = draft.draft_id
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            if local_admission_guard is not None:
                local_admission_guard()
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            # Preparing again supersedes the same owner's pending album for the same recipient and the same
            # ordered items; it can no longer be claimed.
            for entry in self._entries.values():
                old = entry.draft
                if (entry.owner == owner and entry.state == "pending" and old.kind == "album" and
                        old.recipient == recipient and old.album.artifact_ids == album.artifact_ids):
                    entry.state = "superseded"
            self._entries[draft_id] = _Entry(draft=draft, owner=owner, artifact_expires_at=min(expires),
                                            artifact_authority=self._authority_stamp())
            record_selection_draft(draft_id)
            return draft

    def prepare_change(self, *, owner: DraftOwner, chat_id: int, chat_title: str, change: object,
                       evidence: Callable[[OutgoingDraft], None] | None = None) -> OutgoingDraft:
        """Record one archive move or own text edit; generic draft tools never expose these kinds.

        `evidence` builds and validates the public preview of the exact candidate before
        insertion; if it raises, nothing is inserted and no pending draft is superseded."""
        from .archive_drafts import ArchiveSpec
        from .edit_drafts import TextEditSpec
        self._validate_owner(owner)
        if type(chat_id) is not int or chat_id == 0:
            raise ValueError("chat must be a nonzero numeric chat ID")
        kind = "archive" if isinstance(change, ArchiveSpec) else "text_edit" if isinstance(change, TextEditSpec) else None
        if (kind is None or type(chat_title) is not str or not chat_title or
                (change.snapshot.chat_id if kind == "archive" else change.snapshot.anchor[0]) != chat_id):
            raise DraftError("change draft is unavailable")
        now = self._clock()
        draft = OutgoingDraft(
            draft_id=f"draft_{uuid.uuid4().hex}", artifact_id="", sha256=change.sha256, size_bytes=change.size_bytes,
            display_name="Chat list move" if kind == "archive" else "Text edit", mime_type="application/json",
            caption="", recipient=chat_id, expires_at=now + self._max_ttl_seconds, recipient_title=chat_title,
            kind=kind, **{kind: change},
        )
        if evidence is not None:
            evidence(draft)
        draft_id = draft.draft_id
        from .selection_broker import record_selection_draft, selected_admission_fence
        with self._lock, selected_admission_fence():
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            if draft_id in self._entries:
                raise DraftError("draft ID collision")
            # Preparing again supersedes the same owner's pending draft for the same chat
            # (archive) or the same message (text edit); it can no longer be claimed.
            for entry in self._entries.values():
                old = entry.draft
                if (entry.owner == owner and entry.state == "pending" and old.kind == kind and
                        old.recipient == chat_id and getattr(old, kind).supersede_key == change.supersede_key):
                    entry.state = "superseded"
            self._entries[draft_id] = _Entry(draft=draft, owner=owner)
            record_selection_draft(draft_id)
            return draft

    def revise(self, draft_id: str, *, owner: DraftOwner, recipient_title: str,
               text: str | None = None, caption: str | None = None,
               reply: bool = False, reply_source: ReplySource | None = None,
               outgoing_entities: list | None = None, formatting_provider: object | None = None,
               formatting_provider_epoch: object | None = None,
               local_admission_guard: Callable[[], None] | None = None) -> OutgoingDraft:
        """Replace one live revision atomically against claim/cancel/replacement.

        The ID is the revision token. No approval or terminal receipt transfers
        to its replacement. Neither refresh nor update can revive expired data.
        """
        with self._lock:
            entry = self._get(draft_id, owner)
            self._pending_locked(entry, verify_artifact=False)
            old = entry.draft
            if old.kind in _DEDICATED_KINDS or (old.reply_source is not None) != reply:
                raise DraftError("draft is unavailable")
            changes = {}
            if reply_source is not None:
                if (not reply or not isinstance(reply_source, ReplySource) or
                        reply_source.anchor != old.reply_source.anchor or
                        reply_source.forum_topic_id != (old.topic.forum_topic_id if old.topic is not None else None)):
                    raise DraftError("draft is unavailable")
                changes["reply_source"] = reply_source
            if text is not None:
                if old.kind != "text" or caption is not None:
                    raise DraftError("draft is unavailable")
                safe_text = _safe_message_text(text)
                data = safe_text.encode("utf-8")
                changes.update(text=safe_text, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
                entities = freeze_outgoing_entities(safe_text, [] if outgoing_entities is None else outgoing_entities)
                if entities:
                    if formatting_provider is None or formatting_provider_epoch is None:
                        raise DraftError('formatting authority is unavailable')
                    if old.outgoing_entities and (old.formatting_provider is not formatting_provider or
                            old.formatting_provider_epoch is not formatting_provider_epoch):
                        raise DraftError('formatting authority changed')
                changes.update(outgoing_entities=entities,
                    formatting_provider=formatting_provider if entities else None,
                    formatting_provider_epoch=formatting_provider_epoch if entities else None)
            elif outgoing_entities is not None:
                raise DraftError('formatting revision requires complete text')
            if caption is not None:
                if old.kind == "text" or not isinstance(caption, str) or len(caption) > 1024 or "\x00" in caption:
                    raise DraftError("draft is unavailable")
                changes["caption"] = _safe_caption(caption)
            artifact = self._artifact_locked(entry) if old.kind != "text" else None
            # Hashing may have crossed the deadline while the lock was held.
            if local_admission_guard is not None:
                local_admission_guard()
            now = self._clock()
            if now >= old.expires_at:
                raise DraftError("draft is unavailable")
            expires_at = now + self._max_ttl_seconds
            if artifact is not None:
                if entry.artifact_expires_at is None:
                    raise DraftError("draft is unavailable")
                expires_at = min(expires_at, entry.artifact_expires_at, artifact.expires_at)
            if expires_at <= now:
                raise DraftError("draft is unavailable")
            self._prune_locked(now)
            if len(self._entries) >= self._capacity:
                raise DraftError("draft capacity is exhausted")
            new_id = f"draft_{uuid.uuid4().hex}"
            if new_id in self._entries:
                raise DraftError("draft ID collision")
            revised = replace(old, draft_id=new_id, recipient_title=recipient_title,
                              expires_at=expires_at, **changes)
            self._entries[new_id] = _Entry(draft=revised, owner=owner,
                                          artifact_expires_at=entry.artifact_expires_at,
                                          artifact_authority=entry.artifact_authority)
            entry.state = "superseded"
            return revised

    def known_ids(self) -> set[str]:
        """Return current draft IDs after bounded expiry pruning."""
        with self._lock:
            self._prune_locked(self._clock())
            return set(self._entries)

    def _prune_locked(self, now: float) -> None:
        self._entries = {
            draft_id: entry for draft_id, entry in self._entries.items()
            if (self._attempt_clock() < entry.attempted_at + 900 if entry.attempted_at is not None
                else entry.draft.expires_at > now)
        }

    def claim(self, draft_id: str, *, owner: DraftOwner, approved: bool, provider: object | None = None, provider_epoch: object | None = None) -> ClaimedDraft:
        """Atomically consume a live, explicitly approved draft before sending."""
        if approved is not True:
            raise DraftError("explicit approval is required")
        with self._lock:
            entry = self._get(draft_id, owner)
            if entry.state != "pending":
                raise DraftError("draft has already been used")
            path = self._pending_locked(entry)
            if (entry.draft.topic is not None and entry.draft.reply_source is not None and
                    (entry.draft.topic_provider is not provider or entry.draft.topic_provider_epoch is not provider_epoch)):
                raise DraftError("topic reply provider authority changed")
            if entry.draft.outgoing_entities and (entry.draft.formatting_provider is not provider or
                    entry.draft.formatting_provider_epoch is not provider_epoch):
                raise DraftError('formatting provider authority changed')
            entry.state = "claimed"
            entry.attempted_at = self._attempt_clock()
            entry.provider = provider
            entry.provider_epoch = provider_epoch
            entry.claim_token = object()
            paths = path if type(path) is tuple else None
            return ClaimedDraft(draft=entry.draft, path=None if paths is not None else path,
                                claim_token=entry.claim_token, provider=provider, provider_epoch=provider_epoch,
                                paths=paths)

    def unsettled_attempts(self) -> int:
        """Content-free count of attempts a status read could still settle: claimed and not finished, or
        finished `outcome_unknown` (for an album, with any slot still unknown), within receipt retention."""
        with self._lock:
            now = self._attempt_clock()
            return sum(1 for entry in self._entries.values()
                       if entry.attempted_at is not None and now < entry.attempted_at + 900
                       and (entry.receipt is None or entry.receipt.status == "outcome_unknown" or
                            (entry.receipt.slots is not None and
                             any(slot[0] == "outcome_unknown" for slot in entry.receipt.slots))))

    def attempt_outcome(self, draft_id: object, *, client_id: str, owner: DraftOwner | None,
                        kinds: frozenset[str]) -> str | None:
        """Content-free routing after a lost authority: did this client consume the draft?

        None: never claimed (or unknown, foreign, another kind, or past receipt retention).
        'local_failed': the claim ended before any provider attempt. 'attempted': anything else.
        A call that names the DraftOwner that held its claim routes only that owner's draft, its client and
        its account (re-login hardening, 0.55 spec 3C). `owner` None is a call that lost the authority to
        name it (a release, an account switch): its client alone decides, so a consumed claim never reads as
        expired. Never an authority for disclosure, status or another send; the caller maps it to a fixed
        outcome.
        """
        with self._lock:
            entry = self._entries.get(draft_id) if isinstance(draft_id, str) else None
            if (entry is None or entry.owner.client_id != client_id or entry.draft.kind not in kinds or
                    (owner is not None and (type(owner) is not DraftOwner or owner != entry.owner)) or
                    entry.attempted_at is None or self._attempt_clock() >= entry.attempted_at + 900):
                return None
            receipt = entry.receipt
            return 'local_failed' if receipt is not None and receipt.evidence == 'local_failed' else 'attempted'

    def peek(self, draft_id: str, *, owner: DraftOwner) -> OutgoingDraft:
        """Inspect an unexpired draft without claiming a send attempt."""
        with self._lock:
            entry = self._get(draft_id, owner)
            self._pending_locked(entry)
            return entry.draft

    def receipt(self, draft_id: str, *, owner: DraftOwner) -> SendReceipt | None:
        """Return a prior terminal receipt without starting another attempt."""
        with self._lock:
            return self._get(draft_id, owner).receipt

    def is_reply(self, draft_id: str, *, owner: DraftOwner) -> bool:
        with self._lock:
            return self._get(draft_id,owner).draft.reply_source is not None

    def is_topic(self, draft_id: str, *, owner: DraftOwner) -> bool:
        with self._lock:
            return self._get(draft_id, owner).draft.topic is not None

    def is_formatted(self, draft_id: str, *, owner: DraftOwner) -> bool:
        with self._lock:
            return bool(self._get(draft_id, owner).draft.outgoing_entities)

    def formatting_claim_current(self, draft_id: str, *, owner: DraftOwner,
                                 provider: object, provider_epoch: object) -> bool:
        with self._lock:
            entry = self._get(draft_id, owner)
            return (entry.state == 'claimed' and bool(entry.draft.outgoing_entities) and
                self._clock() < entry.draft.expires_at and entry.provider is provider and
                entry.provider_epoch is provider_epoch and entry.draft.formatting_provider is provider and
                entry.draft.formatting_provider_epoch is provider_epoch)

    def forward_claim_current(self, draft_id: str, *, owner: DraftOwner,
                              provider: object, provider_epoch: object) -> bool:
        """Final local prewire check for the one consumed forward claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            return (entry.state == 'claimed' and entry.draft.kind == 'forward' and
                entry.draft.forward is not None and self._clock() < entry.draft.expires_at and
                entry.provider is provider and entry.provider_epoch is provider_epoch)

    def video_claim_current(self, draft_id: str, *, owner: DraftOwner,
                            provider: object, provider_epoch: object) -> bool:
        """Final local prewire check for the one consumed video claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            return (entry.state == 'claimed' and entry.draft.kind == 'video' and
                entry.draft.video is not None and self._clock() < entry.draft.expires_at and
                entry.provider is provider and entry.provider_epoch is provider_epoch)

    def album_claim_current(self, draft_id: str, *, owner: DraftOwner,
                            provider: object, provider_epoch: object) -> bool:
        """Final local prewire check for the one consumed album claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            return (entry.state == 'claimed' and entry.draft.kind == 'album' and
                entry.draft.album is not None and self._clock() < entry.draft.expires_at and
                entry.provider is provider and entry.provider_epoch is provider_epoch)

    def album_of(self, draft_id: str, *, owner: DraftOwner):
        """The owner's album payload in any state within its lifetime or receipt retention; never a claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            if (entry.draft.kind != 'album' or entry.draft.album is None or
                    (self._clock() >= entry.draft.expires_at if entry.attempted_at is None
                     else self._attempt_clock() >= entry.attempted_at + 900)):
                raise DraftError("draft is unavailable")
            return entry.draft.album

    def change_claim_current(self, draft_id: str, *, owner: DraftOwner, kind: str,
                             provider: object, provider_epoch: object) -> bool:
        """Final local prewire check for the one consumed archive or text edit claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            return (kind in _CHANGE_KINDS and entry.state == 'claimed' and entry.draft.kind == kind and
                getattr(entry.draft, kind) is not None and self._clock() < entry.draft.expires_at and
                entry.provider is provider and entry.provider_epoch is provider_epoch)

    def topic_claim_current(self, draft_id: str, *, owner: DraftOwner,
                            provider: object, provider_epoch: object) -> bool:
        """Final local prewire check; expiry never turns into send authority."""
        with self._lock:
            entry = self._get(draft_id, owner)
            return (entry.state == 'claimed' and entry.draft.topic is not None and
                self._clock() < entry.draft.expires_at and entry.provider is provider and
                entry.provider_epoch is provider_epoch and entry.draft.topic_provider is provider and
                entry.draft.topic_provider_epoch is provider_epoch)

    def source_required_capabilities(self, draft_id: str, *, owner: DraftOwner,
                                   provider: object | None = None, provider_epoch: object | None = None,
                                   stored_source: bool = True) -> tuple[str, ...]:
        """Classify gated sources under the owner/account, lifetime and epoch lock.

        stored_source=False keeps the draft's own options but leaves out the stored source's families:
        a refresh replaces that source and names what the newly read one needs. Every check still runs.
        """
        with self._lock:
            entry=self._get(draft_id,owner)
            source=entry.draft.reply_source
            if entry.draft.topic is not None:
                capabilities = tuple(dict.fromkeys(('topic_text_send',
                    *(('reply_text_send', *source.required_capabilities) if source is not None else ()))))
            else:
                capabilities = source.required_capabilities if source is not None else ()
            if entry.draft.outgoing_entities:
                capabilities = tuple(dict.fromkeys(('formatted_text_send',
                    *(('reply_text_send',) if source is not None else ()), *capabilities)))
                if (provider is not None or provider_epoch is not None) and (
                        entry.draft.formatting_provider is not provider or
                        entry.draft.formatting_provider_epoch is not provider_epoch):
                    raise DraftError('formatting provider authority changed')
            if not capabilities:
                return ()  # Preserve the existing plain-source replay semantics.
            if entry.draft.topic is not None and (provider is not None or provider_epoch is not None):
                if entry.draft.topic_provider is not provider or entry.draft.topic_provider_epoch is not provider_epoch:
                    raise DraftError('topic provider authority changed')
            if (self._clock() >= entry.draft.expires_at if entry.attempted_at is None
                    else self._attempt_clock() >= entry.attempted_at + 900):
                raise DraftError("draft is unavailable")
            if entry.attempted_at is not None and (provider is not None or provider_epoch is not None):
                if entry.provider is not provider or entry.provider_epoch is not provider_epoch:
                    raise DraftError("draft is unavailable")
            if not stored_source and source is not None:
                # A selected topic stays an option of the draft even though a forum source also names it.
                stored = set(source.required_capabilities) - ({'topic_text_send'} if entry.draft.topic is not None else set())
                capabilities = tuple(name for name in capabilities if name not in stored)
            return capabilities

    def source_required_capability(self, draft_id: str, *, owner: DraftOwner,
                                   provider: object | None = None, provider_epoch: object | None = None) -> str | None:
        """Legacy singular classifier refuses to underreport conjunctive sources."""
        capabilities=self.source_required_capabilities(draft_id,owner=owner,
            provider=provider,provider_epoch=provider_epoch)
        if len(capabilities)>1:
            raise DraftError('reply source requires multiple capabilities')
        return capabilities[0] if capabilities else None

    def is_media_reply(self, draft_id: str, *, owner: DraftOwner) -> bool:
        return 'reply_media_targets' in self.source_required_capabilities(draft_id,owner=owner)

    def kind(self, draft_id: str, *, owner: DraftOwner) -> str:
        with self._lock:
            return self._get(draft_id, owner).draft.kind

    def finish(
        self,
        draft_id: str,
        *,
        owner: DraftOwner,
        status: ReceiptStatus,
        message_id: int | None = None,
        evidence: str | None = None,
        claim: ClaimedDraft | None = None,
        observed: object | None = None,
    ) -> SendReceipt:
        """Finalize the original consumed claim, independent of public grants.

        Captured claim identity/provider evidence never authorizes new redemption,
        disclosure or another send. Public receipt reads still require authority.
        `applied` (archive/text edit only) carries the provider state observed after the call.
        """
        if status not in {"sent", "failed", "outcome_unknown", "applied"}:
            raise ValueError("invalid receipt status")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            raise ValueError("message_id must be a positive integer")
        if status == "sent" and message_id is None:
            raise ValueError("sent requires a positive final message ID")
        if (status == "applied") != (observed is not None) or (status == "applied" and message_id is not None):
            raise ValueError("only an applied change carries observed state")
        with self._lock:
            entry = self._get(draft_id, owner, require_current=False)
            if entry.draft.kind == "album":
                raise DraftError("draft is unavailable")  # An album finishes only with every slot (finish_album).
            if (status == "applied") != (entry.draft.kind in _CHANGE_KINDS) and status in {"sent", "applied"}:
                raise ValueError("receipt status does not match the draft kind")
            if claim is not None:
                if (claim.draft is not entry.draft or claim.claim_token is not entry.claim_token or
                        entry.claim_token is None or claim.provider is not entry.provider or
                        claim.provider_epoch is not entry.provider_epoch):
                    raise DraftError("captured claim is unavailable")
            elif active_scope(self._store) is not None and entry.draft.kind not in _UNBACKED_KINDS:
                raise DraftError("captured claim is unavailable")
            if entry.receipt is not None:
                return entry.receipt
            if entry.state != "claimed":
                raise DraftError("draft has not been claimed")
            receipt = SendReceipt(
                draft_id=draft_id,
                status=status,
                message_id=message_id,
                finished_at=self._clock(),
                evidence=evidence or {"sent":"provider_confirmed", "applied":"provider_confirmed",
                                      "failed":"provider_failed", "outcome_unknown":"none"}[status],
                observed=observed,
            )
            entry.receipt = receipt
            entry.state = "finished"
            return receipt

    def finish_album(self, draft_id: str, *, owner: DraftOwner, claim: ClaimedDraft | None, slots,
                     evidence: str | None = None) -> SendReceipt:
        """Finalize the consumed album claim with every slot, independent of public grants.

        `slots` holds one (sent|failed|outcome_unknown, final message ID) per item in send order; the receipt's
        status is their closed reduction. `evidence='local_failed'` (no provider attempt) takes no slots. A
        finished receipt is returned unchanged; only `reconcile_album` refines its unknown slots later."""
        from .album_drafts import album_overall
        if evidence not in (None, "local_failed") or (evidence == "local_failed") != (slots is None):
            raise ValueError("invalid album receipt")
        if slots is not None:
            slots = _album_slots(slots)
        with self._lock:
            entry = self._get(draft_id, owner, require_current=False)
            if entry.draft.kind != "album" or (slots is not None and len(slots) != len(entry.draft.album.items)):
                raise ValueError("receipt does not match the album")
            if claim is not None:
                if (claim.draft is not entry.draft or claim.claim_token is not entry.claim_token or
                        entry.claim_token is None or claim.provider is not entry.provider or
                        claim.provider_epoch is not entry.provider_epoch):
                    raise DraftError("captured claim is unavailable")
            elif active_scope(self._store) is not None:
                raise DraftError("captured claim is unavailable")
            if entry.receipt is not None:
                return entry.receipt
            if entry.state != "claimed":
                raise DraftError("draft has not been claimed")
            entry.receipt = SendReceipt(
                draft_id=draft_id, status="failed" if slots is None else album_overall(slot[0] for slot in slots),
                message_id=None, finished_at=self._clock(), evidence=evidence or "provider", slots=slots)
            entry.state = "finished"
            return entry.receipt

    def reconcile_album(self, draft_id: str, *, owner: DraftOwner, provider: object, provider_epoch: object,
                        attempted_at: float, outcomes) -> SendReceipt:
        """Only exact terminal evidence refines an unknown slot; a terminal slot never changes and no send is
        claimed. `outcomes` is the provider's per-slot view; anything but sent or failed refines nothing."""
        from .album_drafts import album_overall
        outcomes = _album_slots(outcomes)
        with self._lock:
            entry = self._get(draft_id, owner)
            if (entry.draft.kind != "album" or entry.provider is not provider or
                    entry.provider_epoch is not provider_epoch or entry.attempted_at != attempted_at or
                    self._attempt_clock() >= attempted_at + 900):
                raise DraftError("draft is unavailable")
            receipt = entry.receipt
            if receipt is None or receipt.slots is None or len(outcomes) != len(receipt.slots):
                raise DraftError("draft is unavailable")
            slots = tuple(observed if current[0] == "outcome_unknown" and observed[0] in ("sent", "failed")
                          else current for current, observed in zip(receipt.slots, outcomes))
            if slots != receipt.slots:
                entry.receipt = replace(receipt, slots=slots, status=album_overall(slot[0] for slot in slots))
            return entry.receipt

    def status_snapshot(self, draft_id: str, *, owner: DraftOwner):
        """Return content-free state without keeping a mutex during provider reads."""
        with self._lock:
            entry = self._get(draft_id, owner)
            now = self._clock()
            if entry.attempted_at is None:
                if now >= entry.draft.expires_at or entry.state != "pending":
                    raise DraftError("draft is unavailable")
                return ("pending", None, None, None, None)
            if self._attempt_clock() >= entry.attempted_at + 900:
                raise DraftError("draft is unavailable")
            return (entry.state, entry.receipt, entry.provider, entry.attempted_at, entry.provider_epoch)

    def reconcile(self, draft_id: str, *, owner: DraftOwner, provider: object, provider_epoch: object,
                  attempted_at: float, status: ReceiptStatus, message_id: int | None = None):
        """Only exact terminal evidence may refine uncertainty; no send is claimed."""
        if status not in {"sent", "failed"} or (status == "sent" and
                (type(message_id) is not int or not 0 < message_id < 2**53)):
            raise ValueError("invalid terminal observation")
        with self._lock:
            entry = self._get(draft_id, owner)
            if entry.draft.kind in _CHANGE_KINDS or entry.draft.kind == "album":
                # A change never reconciles from a send observation; an album only per slot (reconcile_album).
                raise DraftError("draft is unavailable")
            if (entry.provider is not provider or entry.provider_epoch is not provider_epoch or entry.attempted_at != attempted_at or
                    self._attempt_clock() >= attempted_at + 900):
                raise DraftError("draft is unavailable")
            if entry.receipt is None or entry.receipt.status == "outcome_unknown":
                entry.receipt = SendReceipt(draft_id,status,message_id,self._clock(),
                    "provider_confirmed" if status == "sent" else "provider_failed")
                entry.state = "finished"
            return entry.receipt

    @staticmethod
    def _validate_owner(owner: DraftOwner) -> None:
        if not isinstance(owner, DraftOwner):
            raise ValueError("invalid draft owner")

    def _authority_stamp(self):
        scope = active_scope(self._store)
        return (scope.owner, scope.account_epoch) if scope is not None else None

    def _require_artifact_authority(self, entry):
        scope = active_scope(self._store)
        if scope is None or entry.draft.kind in _UNBACKED_KINDS:
            return
        if entry.artifact_authority is None:
            raise DraftError('draft is unavailable')
        owner, epoch = entry.artifact_authority
        # An album holds one grant per item; every one of them must still authorize.
        artifact_ids = (entry.draft.album.artifact_ids if entry.draft.kind == "album" else (entry.draft.artifact_id,))
        try:
            scope.authority.live(scope)
            if owner != scope.owner or (epoch is not None and epoch is not scope.account_epoch):
                raise ArtifactUnavailable('artifact is unavailable')
            if not all(scope.authority.authorize_local(scope, artifact_id) for artifact_id in artifact_ids):
                raise ArtifactUnavailable('artifact is unavailable')
        except ArtifactUnavailable:
            raise DraftError('draft is unavailable') from None

    def _pending_locked(self, entry: _Entry, *, verify_artifact: bool = True) -> Path | tuple[Path, ...] | None:
        """The verified artifact path (an album: every item's, in send order), or None when unbacked."""
        self._require_artifact_authority(entry)
        if entry.state != "pending" or self._clock() >= entry.draft.expires_at:
            raise DraftError("draft is unavailable")
        if entry.draft.kind in _UNBACKED_KINDS or not verify_artifact:
            return None
        if entry.draft.kind == "album":
            paths = tuple(self._item_locked(entry, item).path for item in entry.draft.album.items)
            if self._clock() >= entry.draft.expires_at:
                raise DraftError("draft is unavailable")
            return paths
        artifact = self._artifact_locked(entry)
        if self._clock() >= entry.draft.expires_at:
            raise DraftError("draft is unavailable")
        return artifact.path

    def _item_locked(self, entry: _Entry, item):
        """One album item re-hashed against its own sha256 and size."""
        try:
            artifact = self._store.lookup(item.artifact_id)
        except (ArtifactStoreError, OSError, ValueError):
            artifact = None
        if artifact is None or artifact.sha256 != item.sha256 or artifact.size_bytes != item.size_bytes:
            entry.state = "invalid"
            raise DraftError("draft is unavailable")
        return artifact

    def _artifact_locked(self, entry: _Entry):
        if entry.draft.kind == "album":
            raise DraftError("draft is unavailable")  # An album has no single artifact.
        try:
            artifact = self._store.lookup(entry.draft.artifact_id)
        except (ArtifactStoreError, OSError, ValueError):
            artifact = None
        if (artifact is None or artifact.sha256 != entry.draft.sha256 or
                artifact.size_bytes != entry.draft.size_bytes):
            entry.state = "invalid"
            raise DraftError("draft is unavailable")
        return artifact

    def list_pending(self, *, owner: DraftOwner, limit: int = 20,
                     after_draft_id: str | None = None) -> tuple[list[OutgoingDraft], bool]:
        self._validate_owner(owner)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid draft limit")
        with self._lock:
            if after_draft_id is not None:
                anchor = self._get(after_draft_id, owner)
                if self._clock() >= anchor.draft.expires_at:
                    raise DraftError("draft is unavailable")
            result = []
            for draft_id in sorted(self._entries):
                entry = self._entries[draft_id]
                if (entry.owner != owner or entry.draft.kind in _DEDICATED_KINDS or
                        (after_draft_id is not None and draft_id <= after_draft_id)):
                    continue
                try:
                    self._pending_locked(entry, verify_artifact=False)
                except DraftError:
                    continue
                result.append(entry.draft)
                if len(result) > limit:
                    break
            return result[:limit], len(result) > limit

    def cancel(self, draft_id: str, *, owner: DraftOwner) -> None:
        """Only a pending draft can lose the race to a provider claim."""
        with self._lock:
            entry = self._get(draft_id, owner)
            if (entry.draft.kind in _DEDICATED_KINDS or self._clock() >= entry.draft.expires_at or
                    entry.state not in {"pending", "cancelled"}):
                raise DraftError("draft is unavailable")
            entry.state = "cancelled"

    def _get(self, draft_id: str, owner: DraftOwner, *, require_current: bool = True) -> _Entry:
        self._validate_owner(owner)
        if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
            raise DraftError("unknown draft")
        try:
            entry = self._entries[draft_id]
            if entry.owner != owner:
                raise DraftError("unknown draft")
            if require_current:
                self._require_artifact_authority(entry)
            return entry
        except KeyError:
            raise DraftError("unknown draft") from None

    def discard_unpublished(self, draft_id):
        """Retire an unpublished local candidate; never make a provider request."""
        with self._lock:
            entry = self._entries.get(draft_id)
            if entry is not None and entry.state == "pending":
                self._entries.pop(draft_id, None)
