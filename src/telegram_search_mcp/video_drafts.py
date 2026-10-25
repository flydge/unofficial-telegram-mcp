"""Closed public contracts for one owner-approved native Telegram video send."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .forward_drafts import _Frozen, _utc, escaped, unescaped
from .reply_drafts import EVIDENCE_MARKER, SEND_OPTIONS, digest
from .sanitize import sanitize_telegram_text
from .schemas import ArtifactId, ChatId
from .video_probe import MAX_ASPECT, MAX_FILE_BYTES, MAX_SECONDS, MAX_SIDE, MIN_SIDE, VideoFacts
from .next_actions import NextAction, NextActionHint, next_action_field
from .outgoing_entity_models import OutgoingEntityList, canonical_caption_entities, caller_outgoing_entities
from .outgoing_formatting import freeze_outgoing_entities, native_outgoing_entities

PREPARED = 'immutable video draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'artifact is not an eligible native video; no draft was created'
UNAVAILABLE = 'video draft is unavailable'
PREVIEW_VERSION = 1
MAX_VIDEO_BYTES = MAX_FILE_BYTES
MAX_NAME = 255
MAX_CAPTION = 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')

Codec = Literal['h264', 'hevc']
Refusal = Literal['unsupported_video', 'video_too_large']
SendStatus = Literal['sent', 'failed', 'local_failed', 'declined', 'timeout', 'dialog_unavailable',
                     'outcome_unknown', 'expired', 'recipient_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
Seconds = Annotated[int, Field(strict=True, ge=1, le=MAX_SECONDS)]
Side = Annotated[int, Field(strict=True, ge=MIN_SIDE, le=MAX_SIDE)]
VideoBytes = Annotated[int, Field(strict=True, gt=0, le=MAX_VIDEO_BYTES)]
# One reversible JSON layer: an astral scalar costs 12 characters, plus the two quotes.
EscapedName = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 3,
                                   max_length=len(EVIDENCE_MARKER) + 12 * MAX_NAME + 2)]
EscapedCaption = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 3,
                                      max_length=len(EVIDENCE_MARKER) + 12 * MAX_CAPTION + 2)]


class VideoRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible artifact."""

    def __init__(self, code: str):
        if code not in ('unsupported_video', 'video_too_large'):
            raise ValueError('unknown video refusal')
        self.code = code
        super().__init__(code)


def _valid_facts(facts: object) -> bool:
    return (isinstance(facts, VideoFacts) and type(facts.duration_seconds) is int and
            1 <= facts.duration_seconds <= MAX_SECONDS and type(facts.width) is int and type(facts.height) is int and
            MIN_SIDE <= min(facts.width, facts.height) and max(facts.width, facts.height) <= MAX_SIDE and
            max(facts.width, facts.height) <= MAX_ASPECT * min(facts.width, facts.height) and
            facts.codec in ('h264', 'hevc') and type(facts.has_audio) is bool and
            type(facts.supports_streaming) is bool)


class VideoRecipient(_Frozen):
    chat_id: ChatId
    title: Title

    @model_validator(mode='after')
    def sanitized(self):
        if sanitize_telegram_text(self.title, max_length=255) != self.title:
            raise ValueError('chat title is not sanitized evidence')
        return self


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, recipient: VideoRecipient,
                  artifact_id: str, display_name: str, size_bytes: int, sha256: str, duration_seconds: int,
                  width: int, height: int, codec: str, has_audio: bool, supports_streaming: bool,
                  caption: str | None, outgoing_entities: list | None = None) -> dict:
    """Bind the displayed evidence to the exact closed sendMessage(inputMessageVideo) request. A styled caption
    (0.58) is version 2 and its native styles fill the request's caption entities; a plain one keeps version 1."""
    entities = (native_outgoing_entities(freeze_outgoing_entities(unescaped(caption), outgoing_entities))
                if outgoing_entities and caption is not None else [])
    if outgoing_entities and not entities:
        raise ValueError('video caption styles need a caption')
    return {'domain': 'telegram-search-mcp.video', 'version': 2 if entities else PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'recipient': recipient.model_dump(mode='json'), 'artifact_id': artifact_id,
            'display_name': display_name, 'size_bytes': size_bytes, 'sha256': sha256,
            'video': {'duration_seconds': duration_seconds, 'width': width, 'height': height, 'codec': codec,
                      'has_audio': has_audio, 'supports_streaming': supports_streaming},
            'caption': caption,
            'request': {'@type': 'sendMessage', 'chat_id': recipient.chat_id, 'topic_id': None, 'reply_to': None,
                        'reply_markup': None, 'options': dict(SEND_OPTIONS),
                        'input_message_content': {'@type': 'inputMessageVideo',
                            'video': {'@type': 'inputVideo', 'thumbnail': None, 'cover': None, 'start_timestamp': 0,
                                      'added_sticker_file_ids': [], 'duration': duration_seconds, 'width': width,
                                      'height': height, 'supports_streaming': supports_streaming},
                            'caption': {'@type': 'formattedText',
                                        'text': unescaped(caption) if caption is not None else '',
                                        'entities': entities},
                            'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}}}


class VideoDraftPreview(_Frozen):
    """Facts declared by the file (not decoded): duration, dimensions, codec, audio and streaming
    layout as its ISO-BMFF boxes state them; plus marked, reversibly escaped untrusted file
    name and caption."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    recipient: VideoRecipient
    artifact_id: ArtifactId
    display_name: EscapedName
    size_bytes: VideoBytes
    sha256: Hash
    duration_seconds: Seconds
    width: Side
    height: Side
    codec: Codec
    has_audio: StrictBool
    supports_streaming: StrictBool
    caption: EscapedCaption | None = None
    # 0.58: the caption's canonical styles (formatted_caption_send); offsets address the raw caption.
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_serializer(mode='wrap')
    def omit_plain_caption_styles(self, handler):
        value = handler(self)
        if not self.outgoing_entities:
            value.pop('outgoing_entities', None)
        return value

    @model_validator(mode='after')
    def exact(self):
        from .outgoing_drafts import _safe_caption, _safe_display_name
        name, caption = self.raw_display_name, self.raw_caption
        _, _, artifact_hash, artifact_size = self.artifact_id.split('_', 3)
        if self.outgoing_entities:  # frozen: the canonical order is installed once, here
            object.__setattr__(self, 'outgoing_entities', canonical_caption_entities(caption, self.outgoing_entities))
        facts = preview_facts(**{key: getattr(self, key) for key in (
            'draft_id', 'account_id', 'expires_at', 'recipient', 'artifact_id', 'display_name', 'size_bytes',
            'sha256', 'duration_seconds', 'width', 'height', 'codec', 'has_audio', 'supports_streaming', 'caption')},
            outgoing_entities=caller_outgoing_entities(self.outgoing_entities))
        if (self.approval_required is not True or _safe_display_name(name) != name or
                (self.caption is not None and (not caption or _safe_caption(caption) != caption)) or
                artifact_hash != self.sha256 or int(artifact_size) != self.size_bytes or
                max(self.width, self.height) > MAX_ASPECT * min(self.width, self.height) or
                digest(facts) != self.preview_sha256):
            raise ValueError('video preview facts disagree')
        return self

    @property
    def raw_display_name(self) -> str:
        return unescaped(self.display_name)

    @property
    def raw_caption(self) -> str:
        return unescaped(self.caption) if self.caption is not None else ''

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, recipient: VideoRecipient,
               artifact_id: str, display_name: str, size_bytes: int, sha256: str, facts: VideoFacts,
               caption: str, outgoing_entities: list | None = None) -> 'VideoDraftPreview':
        if not _valid_facts(facts):
            raise ValueError('video facts are unavailable')
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), recipient=recipient,
                      artifact_id=artifact_id, display_name=escaped(display_name), size_bytes=size_bytes,
                      sha256=sha256, duration_seconds=facts.duration_seconds, width=facts.width,
                      height=facts.height, codec=facts.codec, has_audio=facts.has_audio,
                      supports_streaming=facts.supports_streaming, caption=escaped(caption) if caption else None)
        styles = list(outgoing_entities or [])
        return cls(**values, **({'outgoing_entities': styles} if styles else {}),
                   preview_sha256=digest(preview_facts(**values, outgoing_entities=styles)))


@dataclass(frozen=True)
class VideoSpec:
    """Immutable internal draft payload; the registry stores it under kind 'video'.

    The probed facts are bound to the exact artifact bytes they were read from."""
    facts: VideoFacts
    sha256: str
    size_bytes: int
    recipient_identity: tuple

    def __post_init__(self):
        if (not _valid_facts(self.facts) or type(self.sha256) is not str or _HASH.match(self.sha256) is None or
                type(self.size_bytes) is not int or not 0 < self.size_bytes <= MAX_VIDEO_BYTES or
                type(self.recipient_identity) is not tuple or not self.recipient_identity):
            raise ValueError('invalid video draft')


class PrepareVideoSendRequest(_Closed):
    artifact_id: ArtifactId
    recipient: ChatId
    caption: Annotated[str, Field(strict=True, max_length=MAX_CAPTION)] = ''
    # 0.58: optional caption styles (formatted_caption_send); a styled caption must be the text Telegram stores.
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)

    @model_validator(mode='after')
    def exact_caption_entities(self):
        if self.outgoing_entities:
            self.outgoing_entities = canonical_caption_entities(self.caption, self.outgoing_entities)
        return self

    @model_serializer(mode='wrap')
    def omit_empty_entities(self, handler):
        value = handler(self)
        if not self.outgoing_entities:
            value.pop('outgoing_entities', None)
        return value


class PrepareVideoSendResponse(_Closed):
    status: Literal['prepared', 'refused', 'unavailable']
    video: VideoDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable video draft prepared; inspect full evidence and obtain fresh approval',
                    'artifact is not an eligible native video; no draft was created', 'video draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE}[self.status]
        if (self.detail != expected or (self.status == 'prepared') != (self.video is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid video preparation response')
        return self


class SendPreparedVideoRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class SendPreparedVideoResponse(NextActionHint, _Closed):
    status: SendStatus
    draft_id: DraftId
    recipient: ChatId | None = None
    message_id: Annotated[int, Field(strict=True, gt=0, lt=2**53)] | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'sent') != (self.message_id is not None):
            raise ValueError('only a confirmed video send carries a final message ID')
        return self
