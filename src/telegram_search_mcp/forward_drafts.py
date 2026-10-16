"""Closed public contracts for one exact, owner-approved forward or copy."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .forward_sources import MAX_ENTITIES, MAX_TEXT, VISIBLE_ENTITY_TYPES, ForwardRefusal, ForwardSnapshot
from .reply_drafts import EVIDENCE_MARKER, SEND_OPTIONS, digest
from .reply_formatted_sources import _boundaries, _inert_json_string
from .sanitize import sanitize_telegram_text
from .schemas import ChatId

PREPARED = 'immutable forward draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'source message is not eligible; no draft was created'
UNAVAILABLE = 'forward draft is unavailable'
PREVIEW_VERSION = 2
# One reversible JSON layer (the reply v23 encoding): only ASCII letters, digits, _ and -
# stay literal, so one astral scalar costs 12 characters; plus the two quotes.
MAX_ESCAPED = 12 * MAX_TEXT + 2
MAX_EVIDENCE = 131072  # Escaped text plus every escaped covered span the dialog prints.
MAX_NAME = 255

ForwardMode = Literal['forward', 'copy']
ContentKind = Literal['text', 'photo', 'document', 'voice_note', 'video', 'audio', 'animation']
EntityType = Literal[VISIBLE_ENTITY_TYPES]
PositiveMessageId = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
StrictBool = Annotated[bool, Field(strict=True)]
Refusal = Literal['unsupported_source_content', 'unsupported_source_state', 'source_not_forwardable',
                  'protected_content', 'hidden_source_content', 'source_is_forward', 'invalid_input']
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


def escaped(raw: str) -> str:
    """Marked reversible evidence: decoding one JSON layer yields the exact raw string."""
    return EVIDENCE_MARKER + _inert_json_string(raw)


_MARK_OPEN = '\u27e6'


def readable(raw: str) -> str:
    """For the human dialog: letters of every script stay literal, so Cyrillic or CJK text
    is readable, while anything invisible, direction-changing or changed by NFKC (look-alike
    compatibility forms) is shown as ⟦U+XXXX⟧, as are Cyrillic/Greek letters inside a word
    that also has ASCII letters (the classic look-alike domain). The literal ⟦ is marked too,
    so the rendering is reversible. The MCP preview keeps the full JSON escapes."""
    def lookalike(char):
        return char.isalpha() and not char.isascii() and unicodedata.name(char, '').startswith(('CYRILLIC', 'GREEK'))
    out = []
    for token in re.split(r'(\s+)', raw):
        mixed = any(char.isascii() and char.isalpha() for char in token) and any(map(lookalike, token))
        for char in token:
            category = unicodedata.category(char)
            if (char == _MARK_OPEN or category in ('Cc', 'Cf', 'Co', 'Cn', 'Cs', 'Zl', 'Zp')
                    or (category == 'Zs' and char != ' ') or unicodedata.normalize('NFKC', char) != char
                    or (mixed and lookalike(char))):
                out.append('\u27e6U+%04X\u27e7' % ord(char))
            else:
                out.append(char)
    return ''.join(out)


def unescaped(value: object) -> str:
    """Strictly invert `escaped`; any other spelling of the same text is refused."""
    if (type(value) is not str or not value.startswith(EVIDENCE_MARKER) or
            len(value) > len(EVIDENCE_MARKER) + MAX_ESCAPED):
        raise ValueError('evidence is not marked reversible escapes')
    body = value[len(EVIDENCE_MARKER):]
    try:
        raw = json.loads(body)
        if type(raw) is not str or not raw or _inert_json_string(raw) != body:
            raise ValueError('evidence is not marked reversible escapes')
        raw.encode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('evidence is not marked reversible escapes') from None
    return raw


def entity_rows(raw: str, entities) -> list[tuple[str, int, int, str]]:
    """Covered spans derived from the raw hashed text by raw UTF-16 offsets."""
    boundaries = _boundaries(raw)
    rows = []
    for entity in entities:
        kind, offset, length = ((entity['type'], entity['offset'], entity['length']) if type(entity) is dict
                                else (entity.type, entity.offset, entity.length))
        if offset not in boundaries or offset + length not in boundaries:
            raise ValueError('entity is outside the raw source text')
        covered = raw[boundaries[offset]:boundaries[offset + length]]
        rows.append((kind[len('textEntityType'):], offset, length, _inert_json_string(covered)))
    return rows


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
    type: EntityType
    offset: Annotated[int, Field(strict=True, ge=0, le=2 * MAX_TEXT)]
    length: Annotated[int, Field(strict=True, gt=0, le=2 * MAX_TEXT)]


Marked = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 1, max_length=len(EVIDENCE_MARKER) + MAX_NAME)]
Escaped = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 3,
                               max_length=len(EVIDENCE_MARKER) + MAX_ESCAPED)]


def _marked(value: object) -> str | None:
    safe = sanitize_telegram_text(value, max_length=MAX_NAME)
    return EVIDENCE_MARKER + safe if safe else None


def _name(value: str) -> str | None:
    """Short Telegram names stay sanitized evidence; nothing may vanish or be truncated."""
    safe = sanitize_telegram_text(value, max_length=4 * MAX_NAME)
    if len(safe) > MAX_NAME:
        raise ForwardRefusal('unsupported_source_content')
    if not safe and value.strip():
        raise ForwardRefusal('hidden_source_content')
    return EVIDENCE_MARKER + safe if safe else None


def _check_marked(*values) -> None:
    for value in values:
        if value is not None and (not value.startswith(EVIDENCE_MARKER) or
                _marked(value[len(EVIDENCE_MARKER):]) != value):
            raise ValueError('evidence is not marked sanitized evidence')


class ForwardMedia(_Frozen):
    """Stable identity plus marked, sanitized Telegram-originated names; covers stay undisplayed."""
    file_name: Marked | None = None
    mime_type: Marked | None = None
    title: Marked | None = None
    performer: Marked | None = None
    duration_seconds: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    width: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    height: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    start_timestamp_seconds: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)] | None = None
    has_spoiler: StrictBool | None = None
    caption_above_media: StrictBool | None = None
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
    unique_id_sha256: Hash
    variants: Annotated[int, Field(strict=True, ge=1, le=20)] = 1

    @model_validator(mode='after')
    def sanitized(self):
        _check_marked(self.file_name, self.mime_type, self.title, self.performer)
        if (self.has_spoiler is None) != (self.caption_above_media is None):
            raise ValueError('media presentation flags disagree')
        return self


class ForwardLinkPreview(_Frozen):
    """Text-source link preview options; a URL is admitted only when visible in the text."""
    url: Escaped | None = None
    is_disabled: StrictBool
    force_small_media: StrictBool
    force_large_media: StrictBool
    show_above_text: StrictBool

    @model_validator(mode='after')
    def reversible(self):
        if self.url is not None:
            unescaped(self.url)
        return self


class ForwardSourcePreview(_Frozen):
    """Marked display of the hashed projection; text in reversible escapes, never instructions."""
    chat: ForwardChat
    message_id: PositiveMessageId
    sender: ForwardSender
    author_signature: Marked | None = None
    via_bot_user_id: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)] | None = None
    is_forward: StrictBool
    date: datetime
    content_kind: ContentKind
    text: Escaped | None = None
    entities: Annotated[list[ForwardEntity], Field(max_length=MAX_ENTITIES)] = Field(default_factory=list)
    link_preview: ForwardLinkPreview | None = None
    media: ForwardMedia | None = None
    source_sha256: Hash

    @field_validator('date', mode='before')
    @classmethod
    def utc_date(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def evidence(self):
        _check_marked(self.author_signature)
        raw = self.raw_text
        rows = entity_rows(raw, self.entities)  # Offsets address the raw text, never a display form.
        if len(self.text or '') + sum(len(row[3]) for row in rows) > MAX_EVIDENCE:
            raise ValueError('source evidence exceeds the display bound')
        if (self.content_kind == 'text') != (self.media is None):
            raise ValueError('source media evidence disagrees with content kind')
        if self.link_preview is not None and (self.content_kind != 'text' or (
                self.link_preview.url is not None and unescaped(self.link_preview.url) not in raw)):
            raise ValueError('link preview evidence is not visible in the text')
        return self

    @property
    def raw_text(self) -> str:
        return unescaped(self.text) if self.text is not None else ''

    def entity_rows(self) -> list[tuple[str, int, int, str]]:
        return entity_rows(self.raw_text, self.entities)

    @classmethod
    def create(cls, snapshot: ForwardSnapshot) -> 'ForwardSourcePreview':
        if not isinstance(snapshot, ForwardSnapshot):
            raise ValueError('forward snapshot is unavailable')
        source = snapshot.source
        value = source.value
        content = value['content']
        kind = content['kind']
        formatted = content['text'] if kind == 'text' else content['caption']
        raw = formatted['text']
        text = escaped(raw) if raw else None
        if len(text or '') + sum(len(row[3]) for row in entity_rows(raw, formatted['entities'])) > MAX_EVIDENCE:
            raise ForwardRefusal('unsupported_source_content')
        media = link = None
        if kind == 'text':
            options = content['link_preview_options']
            if options is not None:
                link = ForwardLinkPreview(url=escaped(options['url']) if options['url'] else None,
                    **{key: options[key] for key in ('is_disabled', 'force_small_media', 'force_large_media',
                                                     'show_above_text')})
        else:
            if kind == 'photo':
                primary = max(content['variants'], key=lambda row: (
                    row['width'] * row['height'], row['size'], row['unique_id_sha256']))
                facts = dict(width=primary['width'], height=primary['height'], variants=len(content['variants']))
            else:
                primary = content['file']
                facts = {key: content[key] for key in ('width', 'height') if key in content}
                facts['duration_seconds'] = content.get('duration')
                for key in ('file_name', 'mime_type', 'title', 'performer'):
                    if key in content:
                        facts[key] = _name(content[key])
            if 'start_timestamp' in content:
                facts['start_timestamp_seconds'] = content['start_timestamp']
            if 'has_spoiler' in content:
                facts.update(has_spoiler=content['has_spoiler'], caption_above_media=content['show_caption_above_media'])
            media = ForwardMedia(size_bytes=primary['size'], unique_id_sha256=primary['unique_id_sha256'], **facts)
        return cls(chat=ForwardChat(chat_id=value['chat_id'], title=snapshot.chat_title),
                   message_id=value['message_id'],
                   sender=ForwardSender(kind=value['sender']['type'], id=value['sender']['id']),
                   author_signature=_name(value['author_signature']),
                   via_bot_user_id=value['via_bot_user_id'] or None, is_forward=value['is_forward'],
                   date=datetime.fromtimestamp(value['date'], tz=timezone.utc),
                   content_kind=kind, text=text, entities=formatted['entities'], link_preview=link,
                   media=media, source_sha256=source.source_sha256)


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, mode: str, remove_caption: bool,
                  source: ForwardSourcePreview, recipient: ForwardChat) -> dict:
    """Bind the displayed evidence to the exact closed forwardMessages request."""
    return {'domain': 'telegram-search-mcp.forward', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'mode': mode, 'remove_caption': remove_caption,
            'source': source.model_dump(mode='json'), 'recipient': recipient.model_dump(mode='json'),
            'request': {'@type': 'forwardMessages', 'chat_id': recipient.chat_id, 'topic_id': None,
                        'from_chat_id': source.chat.chat_id, 'message_ids': [source.message_id],
                        'options': dict(SEND_OPTIONS), 'send_copy': mode == 'copy',
                        'remove_caption': remove_caption}}


class ForwardDraftPreview(_Frozen):
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    mode: ForwardMode
    remove_caption: StrictBool
    source: ForwardSourcePreview
    recipient: ForwardChat
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        facts = preview_facts(draft_id=self.draft_id, account_id=self.account_id, expires_at=self.expires_at,
                              mode=self.mode, remove_caption=self.remove_caption, source=self.source,
                              recipient=self.recipient)
        if (self.approval_required is not True or (self.remove_caption and (
                self.mode != 'copy' or self.source.content_kind == 'text')) or
                (self.mode == 'forward' and self.source.is_forward) or digest(facts) != self.preview_sha256):
            raise ValueError('forward preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, mode: str, remove_caption: bool,
               source: ForwardSourcePreview, recipient: ForwardChat) -> 'ForwardDraftPreview':
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), mode=mode,
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
                (self.remove_caption and (self.mode != 'copy' or self.source.content_kind == 'text')) or
                type(self.recipient_identity) is not tuple or
                self.source.source_sha256 != self.snapshot.source.source_sha256 or
                (self.source.chat.chat_id, self.source.message_id) != self.snapshot.source.anchor):
            raise ValueError('invalid forward draft')

    @property
    def supersede_key(self) -> tuple:
        return (*self.snapshot.source.anchor, self.mode, self.remove_caption)


class PrepareForwardSendRequest(_Closed):
    source_chat: ChatId
    source_message_id: PositiveMessageId
    recipient: ChatId
    mode: ForwardMode
    remove_caption: StrictBool = False

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
    approved: StrictBool


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
