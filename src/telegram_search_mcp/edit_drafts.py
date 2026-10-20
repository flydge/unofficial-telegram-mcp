"""Closed public contracts for one owner-approved plain-text edit of an own text message.

The current message is projected and hashed exactly like an F14 forward source (text,
entities, edit_date, link preview options); only fieldless visible entities are admitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .forward_drafts import (MAX_EVIDENCE, Escaped, ForwardEntity, PositiveMessageId, _Frozen, _utc, entity_rows,
                             escaped, unescaped)
from .forward_sources import MAX_ENTITIES, ForwardRefusal, ForwardSource
from .reply_drafts import digest
from .sanitize import sanitize_telegram_text
from .schemas import ChatId

PREPARED = 'immutable text edit draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for an own text edit; no draft was created'
UNAVAILABLE = 'text edit draft is unavailable'
# Telegram trims edge whitespace and its clean_input_string rewrites the characters below. Since 20.10.2026
# the shared normalisation applies those rewrites before the preview, so these checks only guard input that
# skipped it; the two fixed details stay in the output schema.
WHITESPACE = 'new text must not start or end with whitespace; Telegram trims it'
# U+202A-U+202E are format characters the normalisation already refuses.
REWRITTEN_CHARACTERS = frozenset('\t\u2028\u2029\u202a\u202b\u202c\u202d\u202e\u030a\u0333\u033f')
REWRITTEN = 'new text contains characters Telegram rewrites (tab, U+2028–U+202E, U+030A, U+0333, U+033F)'
PREVIEW_VERSION = 1
REFUSALS = frozenset({'not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                      'unsupported_source_state', 'not_editable', 'unchanged_text', 'invalid_input'})

Refusal = Literal['not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                  'unsupported_source_state', 'not_editable', 'unchanged_text', 'invalid_input']
ApplyStatus = Literal['applied', 'failed', 'local_failed', 'not_approved', 'outcome_unknown', 'expired',
                      'message_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
# Entity kinds Telegram detects again in plain text; the dialog lists them apart from removed formatting.
AUTO_DETECTED = frozenset({'textEntityTypeUrl', 'textEntityTypeEmailAddress', 'textEntityTypePhoneNumber',
                           'textEntityTypeMention', 'textEntityTypeHashtag', 'textEntityTypeCashtag',
                           'textEntityTypeBotCommand', 'textEntityTypeBankCardNumber'})
ChatKind = Literal['private', 'basic_group', 'supergroup', 'channel']
EditDate = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
NewText = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class EditRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible message."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown edit refusal')
        self.code = code
        super().__init__(code)


def normalized_text(value: object) -> str:
    """The prepare_text_send normalisation: NFKC, CRLF/CR to LF, no controls but LF/tab."""
    from .outgoing_drafts import _safe_message_text
    return _safe_message_text(value)


def edge_whitespace(text: object) -> bool:
    """Any Unicode whitespace (incl. LF, tab, U+2028) first or last in the normalized text."""
    return type(text) is str and bool(text) and (text[0].isspace() or text[-1].isspace())


def rewritten_text(text: object) -> bool:
    """A character of the normalized text that the pinned TDLib rewrites or removes."""
    return type(text) is str and not REWRITTEN_CHARACTERS.isdisjoint(text)


def own_text_source(raw: object, chat_id: int, message_id: int) -> ForwardSource:
    """Own-message checks in the stated order, then F14's projection (state, markup, entities,
    link preview), whose refusals keep their codes. Absence of a safety field is never safe."""
    if (type(raw) is not dict or raw.get('@type') != 'message' or type(raw.get('chat_id')) is not int or
            raw['chat_id'] != chat_id or type(raw.get('id')) is not int or raw['id'] != message_id or
            type(raw.get('is_outgoing')) is not bool):
        raise ValueError('edit message is unavailable')
    if raw['is_outgoing'] is not True:
        raise EditRefusal('not_outgoing')
    content = raw.get('content')
    if type(content) is not dict or content.get('@type') != 'messageText':
        raise EditRefusal('unsupported_source_content')
    if raw.get('forward_info') is not None:
        raise EditRefusal('source_is_forward')
    if raw.get('reply_markup') is not None:
        raise EditRefusal('hidden_source_content')
    try:
        return ForwardSource.from_message(raw, chat_id, message_id)
    except ForwardRefusal as refusal:
        raise EditRefusal(refusal.code if refusal.code in REFUSALS else 'unsupported_source_content') from None


@dataclass(frozen=True)
class EditSnapshot:
    """One read of chat identity, the own-text projection and its current edit permission."""
    source: ForwardSource
    chat_identity: tuple
    chat_title: str
    can_be_edited: bool
    chat_kind: str

    def __post_init__(self):
        if (not isinstance(self.source, ForwardSource) or self.source.content_type != 'messageText' or
                self.source.value['is_forward'] or type(self.chat_identity) is not tuple or
                type(self.chat_title) is not str or not self.chat_title or type(self.can_be_edited) is not bool or
                self.chat_kind not in ('private', 'basic_group', 'supergroup', 'channel')):
            raise ValueError('invalid edit snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int, message: object, message_id: int,
               properties: object) -> 'EditSnapshot':
        """Reuse the verified-target identity rules (secret chats are refused) for identity,
        sanitized title and public kind."""
        from .verified_targets import TargetGuardFailure, _identity
        try:
            identity, metadata = _identity(chat, chat_id)
        except TargetGuardFailure:
            raise ValueError('edit chat is unavailable') from None
        source = own_text_source(message, chat_id, message_id)
        if (type(properties) is not dict or properties.get('@type') != 'messageProperties' or
                type(properties.get('can_be_edited')) is not bool):
            raise ValueError('edit message properties are unavailable')
        return cls(source, identity, metadata.title, properties['can_be_edited'], metadata.chat_type)

    @property
    def anchor(self) -> tuple[int, int]:
        return self.source.anchor

    @property
    def raw_text(self) -> str:
        return self.source.value['content']['text']['text']

    @property
    def entities(self) -> list[dict]:
        return self.source.value['content']['text']['entities']

    @property
    def edit_date(self) -> int:
        return self.source.value['edit_date']

    def refusal(self) -> str | None:
        return None if self.can_be_edited else 'not_editable'


class EditChat(_Frozen):
    """The chat of the edited message; a channel edit is visible to all subscribers."""
    chat_id: ChatId
    title: Title
    kind: ChatKind

    @model_validator(mode='after')
    def sanitized(self):
        if sanitize_telegram_text(self.title, max_length=255) != self.title:
            raise ValueError('chat title is not sanitized evidence')
        return self


def edit_request(chat_id: int, message_id: int, new_text: str) -> dict:
    """The one closed editMessageText request; no styles, markup, preview options or draft clearing."""
    return {'@type': 'editMessageText', 'chat_id': chat_id, 'message_id': message_id, 'reply_markup': None,
            'input_message_content': {'@type': 'inputMessageText',
                                      'text': {'@type': 'formattedText', 'text': new_text, 'entities': []},
                                      'link_preview_options': None, 'clear_draft': False}}


def _evidence_size(current: str, rows) -> int:
    return len(current) + sum(len(row[3]) for row in rows)


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat, message_id: int,
                  current_text: str, current_entities: list, new_text: str, current_edit_date: int,
                  source_sha256: str) -> dict:
    """Bind the displayed evidence to the exact closed editMessageText request."""
    return {'domain': 'telegram-search-mcp.text_edit', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'chat': chat.model_dump(mode='json'), 'message_id': message_id, 'current_text': current_text,
            'current_entities': [entity.model_dump(mode='json') if isinstance(entity, ForwardEntity) else dict(entity)
                                 for entity in current_entities],
            'new_text': new_text, 'current_edit_date': current_edit_date, 'source_sha256': source_sha256,
            'request': edit_request(chat.chat_id, message_id, unescaped(new_text))}


class TextEditDraftPreview(_Frozen):
    """Current and new text as marked reversible JSON escapes (never instructions); raw UTF-16
    offsets. Of the current entities, the auto-detected kinds (URL, email, phone, mention,
    hashtag, cashtag, bot command, bank card) are detected again by Telegram in the new text;
    the rest is formatting this plain-text edit removes."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    chat: EditChat
    message_id: PositiveMessageId
    current_text: Escaped
    current_entities: Annotated[list[ForwardEntity], Field(max_length=MAX_ENTITIES)] = Field(default_factory=list)
    new_text: Escaped
    current_edit_date: EditDate
    source_sha256: Hash
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        current, new = self.raw_current_text, self.raw_new_text
        rows = entity_rows(current, self.current_entities)  # Offsets address the raw current text.
        facts = preview_facts(**{key: getattr(self, key) for key in (
            'draft_id', 'account_id', 'expires_at', 'chat', 'message_id', 'current_text', 'current_entities',
            'new_text', 'current_edit_date', 'source_sha256')})
        if (self.approval_required is not True or normalized_text(new) != new or edge_whitespace(new) or
                rewritten_text(new) or new == current or _evidence_size(self.current_text, rows) > MAX_EVIDENCE or
                digest(facts) != self.preview_sha256):
            raise ValueError('text edit preview facts disagree')
        return self

    @property
    def raw_current_text(self) -> str:
        return unescaped(self.current_text)

    @property
    def raw_new_text(self) -> str:
        return unescaped(self.new_text)

    def entity_rows(self) -> list[tuple[str, int, int, str]]:
        return entity_rows(self.raw_current_text, self.current_entities)

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
               snapshot: EditSnapshot, new_text: str) -> 'TextEditDraftPreview':
        if (not isinstance(snapshot, EditSnapshot) or not isinstance(chat, EditChat) or
                chat.chat_id != snapshot.anchor[0] or chat.kind != snapshot.chat_kind):
            raise ValueError('edit snapshot is unavailable')
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), chat=chat,
                      message_id=snapshot.anchor[1], current_text=escaped(snapshot.raw_text),
                      current_entities=[ForwardEntity(**row) for row in snapshot.entities],
                      new_text=escaped(new_text), current_edit_date=snapshot.edit_date,
                      source_sha256=snapshot.source.source_sha256)
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


def evidence_fits(snapshot: EditSnapshot) -> bool:
    """The dialog prints the current text and every covered span; bound them like F14."""
    current = escaped(snapshot.raw_text)
    return _evidence_size(current, entity_rows(snapshot.raw_text, snapshot.entities)) <= MAX_EVIDENCE


def edited_text_result(value: object, *, chat_id: int, message_id: int, new_text: str,
                       previous_edit_date: int) -> int | None:
    """The returned message must be the same message, now messageText with exactly the new
    text and a non-zero edit_date not before the previous one; anything else is unknown."""
    content = value.get('content') if type(value) is dict else None
    text = content.get('text') if type(content) is dict else None
    edit_date = value.get('edit_date') if type(value) is dict else None
    if (type(value) is not dict or value.get('@type') != 'message' or type(value.get('id')) is not int or
            value['id'] != message_id or type(value.get('chat_id')) is not int or value['chat_id'] != chat_id or
            type(content) is not dict or content.get('@type') != 'messageText' or type(text) is not dict or
            text.get('@type') != 'formattedText' or type(text.get('text')) is not str or text['text'] != new_text or
            type(edit_date) is not int or not 0 < edit_date <= 2**31 - 1 or type(previous_edit_date) is not int or
            edit_date < previous_edit_date):
        return None
    return edit_date


@dataclass(frozen=True)
class TextEditSpec:
    """Immutable internal draft payload; the registry stores it under kind 'text_edit'."""
    snapshot: EditSnapshot
    new_text: str

    def __post_init__(self):
        if (not isinstance(self.snapshot, EditSnapshot) or type(self.new_text) is not str or
                normalized_text(self.new_text) != self.new_text or edge_whitespace(self.new_text) or
                rewritten_text(self.new_text) or self.new_text == self.snapshot.raw_text or
                self.snapshot.can_be_edited is not True):
            raise ValueError('invalid text edit draft')

    @property
    def supersede_key(self) -> tuple:
        return ('text_edit', *self.snapshot.anchor)

    @property
    def sha256(self) -> str:
        return self.snapshot.source.source_sha256

    @property
    def size_bytes(self) -> int:
        return len(self.new_text.encode('utf-8'))


class PrepareTextEditRequest(_Closed):
    chat: ChatId
    message_id: PositiveMessageId
    new_text: NewText


class PrepareTextEditResponse(_Closed):
    status: Literal['prepared', 'refused', 'unavailable']
    text_edit: TextEditDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable text edit draft prepared; inspect full evidence and obtain fresh approval',
                    'message is not eligible for an own text edit; no draft was created',
                    'new text must not start or end with whitespace; Telegram trims it',
                    'new text contains characters Telegram rewrites (tab, U+2028–U+202E, U+030A, U+0333, U+033F)',
                    'text edit draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE}[self.status]
        if self.status == 'refused' and self.refusal == 'invalid_input' and self.detail in (WHITESPACE, REWRITTEN):
            expected = self.detail
        if (self.detail != expected or (self.status == 'prepared') != (self.text_edit is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid text edit preparation response')
        return self


class ApplyPreparedTextEditRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedTextEditResponse(_Closed):
    status: ApplyStatus
    draft_id: DraftId
    chat_id: ChatId | None = None
    message_id: PositiveMessageId | None = None
    edit_date: Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)] | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'applied') != (self.edit_date is not None):
            raise ValueError('only an applied edit carries the observed edit date')
        return self
