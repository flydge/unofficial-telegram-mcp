"""Closed public contracts for one exact, owner-approved forward or copy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .draft_models import _Closed, DraftId, Hash
from .forward_sources import MAX_ENTITIES, ForwardRefusal, ForwardSnapshot
from .reply_drafts import EVIDENCE_MARKER, SEND_OPTIONS, digest
from .sanitize import sanitize_telegram_text
from .schemas import ChatId

PREPARED = 'immutable forward draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'source message is not eligible; no draft was created'
UNAVAILABLE = 'forward draft is unavailable'
MAX_DISPLAY = 4096

ForwardMode = Literal['forward', 'copy']
ContentKind = Literal['text', 'photo', 'document', 'voice_note', 'video', 'audio', 'animation']
PositiveMessageId = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
Refusal = Literal['unsupported_source_content', 'unsupported_source_state',
                  'source_not_forwardable', 'protected_content']
SendStatus = Literal['sent', 'pending', 'failed', 'local_failed', 'not_approved', 'outcome_unknown',
                     'expired', 'recipient_changed', 'source_changed', 'source_not_forwardable',
                     'protected_content', 'error']


def _utc(value: object) -> datetime:
    if type(value) not in {str, datetime}:
        raise ValueError('timestamp must be an ISO datetime')
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError('timestamp must be an ISO datetime') from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timestamp requires a timezone')
    return value.astimezone(timezone.utc)


class _Frozen(_Closed):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True)


class ForwardChat(_Frozen):
    chat_id: ChatId
    title: Title

    @model_validator(mode='after')
    def sanitized(self):
        if sanitize_telegram_text(self.title, max_length=255) != self.title:
            raise ValueError('chat title is not sanitized evidence')
        return self


class ForwardSender(_Frozen):
    kind: Literal['user', 'chat']
    id: Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)]

    @model_validator(mode='after')
    def typed(self):
        if self.id == 0 or (self.kind == 'user' and self.id < 0):
            raise ValueError('sender identity is invalid')
        return self


class ForwardEntity(_Frozen):
    type: Annotated[str, Field(strict=True, pattern=r'^textEntityType[A-Za-z]{1,48}$')]
    offset: Annotated[int, Field(strict=True, ge=0, le=2 * MAX_DISPLAY)]
    length: Annotated[int, Field(strict=True, gt=0, le=2 * MAX_DISPLAY)]


Marked = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 1, max_length=len(EVIDENCE_MARKER) + 255)]


def _marked(value: object) -> str | None:
    safe = sanitize_telegram_text(value, max_length=255)
    return EVIDENCE_MARKER + safe if safe else None


class ForwardMedia(_Frozen):
    """Stable identity plus marked, sanitized Telegram-originated names."""
    file_name: Marked | None = None
    mime_type: Marked | None = None
    duration_seconds: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    width: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    height: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
    unique_id_sha256: Hash
    variants: Annotated[int, Field(strict=True, ge=1, le=20)] = 1

    @model_validator(mode='after')
    def sanitized(self):
        for value in (self.file_name, self.mime_type):
            if value is not None and (not value.startswith(EVIDENCE_MARKER) or
                    _marked(value[len(EVIDENCE_MARKER):]) != value):
                raise ValueError('media evidence is not marked sanitized evidence')
        return self


class ForwardSourcePreview(_Frozen):
    """Sanitized, marked display of the hashed projection; never instructions."""
    chat: ForwardChat
    message_id: PositiveMessageId
    sender: ForwardSender
    date: datetime
    content_kind: ContentKind
    text: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_DISPLAY + len(EVIDENCE_MARKER))] | None = None
    text_sanitized: Annotated[bool, Field(strict=True)]
    entities: Annotated[list[ForwardEntity], Field(max_length=MAX_ENTITIES)] = Field(default_factory=list)
    media: ForwardMedia | None = None
    source_sha256: Hash

    @field_validator('date', mode='before')
    @classmethod
    def utc_date(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def evidence(self):
        if self.text is not None:
            value = self.text[len(EVIDENCE_MARKER):]
            if (not self.text.startswith(EVIDENCE_MARKER) or not value or
                    sanitize_telegram_text(value, max_length=MAX_DISPLAY + 1) != value):
                raise ValueError('source text is not marked sanitized evidence')
        if (self.content_kind == 'text') != (self.media is None):
            raise ValueError('source media evidence disagrees with content kind')
        return self

    @classmethod
    def create(cls, snapshot: ForwardSnapshot) -> 'ForwardSourcePreview':
        if not isinstance(snapshot, ForwardSnapshot):
            raise ValueError('forward snapshot is unavailable')
        source = snapshot.source
        value = source.value
        content = value['content']
        formatted = content['text'] if content['kind'] == 'text' else content['caption']
        raw = formatted['text']
        safe = sanitize_telegram_text(raw, max_length=MAX_DISPLAY * 18 + 1)
        if len(safe) > MAX_DISPLAY:
            raise ForwardRefusal('unsupported_source_content')
        media = None
        if content['kind'] != 'text':
            if content['kind'] == 'photo':
                primary = max(content['variants'], key=lambda row: (
                    row['width'] * row['height'], row['size'], row['unique_id_sha256']))
                facts = dict(width=primary['width'], height=primary['height'], variants=len(content['variants']))
            else:
                primary = content['file']
                facts = {key: content[key] for key in ('width', 'height') if key in content}
                facts['duration_seconds'] = content.get('duration')
                for key in ('file_name', 'mime_type'):
                    facts[key] = _marked(content.get(key))
            media = ForwardMedia(size_bytes=primary['size'], unique_id_sha256=primary['unique_id_sha256'], **facts)
        return cls(chat=ForwardChat(chat_id=value['chat_id'], title=snapshot.chat_title),
                   message_id=value['message_id'],
                   sender=ForwardSender(kind=value['sender']['type'], id=value['sender']['id']),
                   date=datetime.fromtimestamp(value['date'], tz=timezone.utc),
                   content_kind=content['kind'], text=EVIDENCE_MARKER + safe if safe else None,
                   text_sanitized=safe != raw, entities=formatted['entities'], media=media,
                   source_sha256=source.source_sha256)


def preview_facts(*, draft_id: str, expires_at: datetime, mode: str, remove_caption: bool,
                  source: ForwardSourcePreview, recipient: ForwardChat) -> dict:
    """Bind the displayed evidence to the exact closed forwardMessages request."""
    return {'domain': 'telegram-search-mcp.forward', 'version': 1, 'draft_id': draft_id,
            'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'mode': mode, 'remove_caption': remove_caption,
            'source': source.model_dump(mode='json'), 'recipient': recipient.model_dump(mode='json'),
            'request': {'@type': 'forwardMessages', 'chat_id': recipient.chat_id, 'topic_id': None,
                        'from_chat_id': source.chat.chat_id, 'message_ids': [source.message_id],
                        'options': dict(SEND_OPTIONS), 'send_copy': mode == 'copy',
                        'remove_caption': remove_caption}}


class ForwardDraftPreview(_Frozen):
    draft_id: DraftId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    mode: ForwardMode
    remove_caption: Annotated[bool, Field(strict=True)]
    source: ForwardSourcePreview
    recipient: ForwardChat
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        facts = preview_facts(draft_id=self.draft_id, expires_at=self.expires_at, mode=self.mode,
                              remove_caption=self.remove_caption, source=self.source, recipient=self.recipient)
        if (self.approval_required is not True or (self.remove_caption and self.mode != 'copy') or
                digest(facts) != self.preview_sha256):
            raise ValueError('forward preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, expires_at: datetime, mode: str, remove_caption: bool,
               source: ForwardSourcePreview, recipient: ForwardChat) -> 'ForwardDraftPreview':
        values = dict(draft_id=draft_id, expires_at=_utc(expires_at), mode=mode,
                      remove_caption=remove_caption, source=source, recipient=recipient)
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


@dataclass(frozen=True)
class ForwardSpec:
    """Immutable internal draft payload; the registry stores it under kind 'forward'."""
    snapshot: ForwardSnapshot
    source: ForwardSourcePreview
    mode: str
    remove_caption: bool
    recipient_identity: tuple

    def __post_init__(self):
        if (not isinstance(self.snapshot, ForwardSnapshot) or not isinstance(self.source, ForwardSourcePreview) or
                self.mode not in ('forward', 'copy') or type(self.remove_caption) is not bool or
                (self.remove_caption and self.mode != 'copy') or type(self.recipient_identity) is not tuple or
                self.source.source_sha256 != self.snapshot.source.source_sha256 or
                (self.source.chat.chat_id, self.source.message_id) != self.snapshot.source.anchor):
            raise ValueError('invalid forward draft')


class PrepareForwardSendRequest(_Closed):
    source_chat: ChatId
    source_message_id: PositiveMessageId
    recipient: ChatId
    mode: ForwardMode
    remove_caption: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode='after')
    def caption_mode(self):
        if self.remove_caption and self.mode != 'copy':
            raise ValueError('remove_caption is only valid with copy')
        return self


class PrepareForwardSendResponse(_Closed):
    status: Literal['prepared', 'refused', 'unavailable']
    forward: ForwardDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable forward draft prepared; inspect full evidence and obtain fresh approval',
                    'source message is not eligible; no draft was created', 'forward draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE}[self.status]
        if (self.detail != expected or (self.status == 'prepared') != (self.forward is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid forward preparation response')
        return self


class SendPreparedForwardRequest(_Closed):
    draft_id: DraftId
    approved: Annotated[bool, Field(strict=True)]


class SendPreparedForwardResponse(_Closed):
    status: SendStatus
    draft_id: DraftId
    recipient: ChatId | None = None
    message_id: Annotated[int, Field(strict=True, gt=0, lt=2**53)] | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'sent') != (self.message_id is not None):
            raise ValueError('only a confirmed forward carries a final message ID')
        return self
