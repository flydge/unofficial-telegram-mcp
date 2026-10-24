"""Closed public contracts for one owner-approved caption edit of an own media message (0.57, F17 rest).

The current message is an own photo, video, animation, document, audio or voice note, projected and hashed exactly
like an F14 forward source (caption, its entities, the above-media flag, edit_date); a caption with a hidden entity
(TextUrl, CustomEmoji, MentionName and every other parameterised kind) stays refused until the 0.58 families land.
The new caption is either a caption already in the form Telegram stores (input that NFKC, the line-break rewrite,
Telegram's character rewrite or its edge trim would change is refused, never rewritten) with optional caller styles
(bold, italic, underline, strikethrough, spoiler; they also need edit_own_styled_text), or exactly the empty
formatted text, which removes the caption. One closed editMessageCaption request carries it; the result must be the
same message of the same content kind with exactly the new caption and the caller's styles, Telegram's auto-detected
entities (links, mentions, hashtags, ...) being the only extras tolerated.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .edit_drafts import AUTO_DETECTED, ApplyStatus, EditChat, EditDate, EditRefusal, exact_styles, own_source
from .forward_drafts import (MAX_EVIDENCE, Escaped, ForwardEntity, PositiveMessageId, _Frozen, _utc, entity_rows,
                             escaped, unescaped)
from .forward_sources import MAX_ENTITIES, ForwardSource
from .next_actions import NextAction, NextActionHint, next_action_field, optional_field
from .outgoing_entity_models import OutgoingEntity, OutgoingEntityList
from .outgoing_formatting import (FrozenOutgoingEntities, freeze_outgoing_entities, native_outgoing_entities,
                                  outgoing_entity_facts)
from .reply_drafts import MissingCapabilities, digest
from .schemas import ChatId

PREPARED = 'immutable caption edit draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for this caption edit; no draft was created'
UNAVAILABLE = 'caption edit draft is unavailable'
DISABLED = 'a styled caption needs capabilities the owner has not enabled: missing_capabilities'
WHITESPACE = 'new caption must not start or end with whitespace; Telegram trims it'
REWRITTEN = 'new caption changes under Telegram normalization (NFKC, line breaks, rewritten characters); send its stored form'
PREVIEW_VERSION = 1
MAX_CAPTION = 1024
# The own media messages whose caption this edit replaces, and their public kinds.
CONTENT_KINDS = {'messagePhoto': 'photo', 'messageVideo': 'video', 'messageAnimation': 'animation',
                 'messageDocument': 'document', 'messageAudio': 'audio', 'messageVoiceNote': 'voice_note'}
# Only these carry a caption position; TDLib drops show_caption_above_media for every other kind.
ABOVE_MEDIA_KINDS = frozenset({'photo', 'video', 'animation'})
# The kinds whose own media duration bounds a media timestamp in the caption (get_message_content_media_duration; 0 is
# no bound). Every other kind takes the bound from the replied message's media, which the result does not carry.
OWN_DURATION = {'messageVideo': 'video', 'messageAudio': 'audio', 'messageVoiceNote': 'voice_note'}
REFUSALS = frozenset({'not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                      'unsupported_source_state', 'not_editable', 'unchanged_caption', 'invalid_input'})
Refusal = Literal['not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                  'unsupported_source_state', 'not_editable', 'unchanged_caption', 'invalid_input']
ContentKind = Literal['photo', 'video', 'animation', 'document', 'audio', 'voice_note']
StrictBool = Annotated[bool, Field(strict=True)]
NewCaption = Annotated[str, Field(strict=True, max_length=MAX_CAPTION)]
# The edit's own spans: the styles prepare_text_send admits, now gated by edit_own_styled_text.
StyleList = Annotated[list[OutgoingEntity], Field(strict=True, max_length=32)]


class CaptionRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible message or caption."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown caption edit refusal')
        self.code = code
        super().__init__(code)


def normalized_caption(value: object) -> str:
    """The shared text normalisation for a caption: NFKC, CRLF/CR to LF, a control or format character other than
    LF/tab refused (never removed), then Telegram's own rewrite and edge trim; at most 1024 characters."""
    from .outgoing_drafts import _telegram_stored
    if type(value) is not str:
        raise ValueError('caption must be a string')
    result = unicodedata.normalize('NFKC', value.replace('\r\n', '\n').replace('\r', '\n'))
    if any(unicodedata.category(character).startswith('C') and character not in '\n\t' for character in result):
        raise ValueError('caption contains control characters')
    result = _telegram_stored(result)
    if len(result) > MAX_CAPTION:
        raise ValueError('caption is too long')
    return result


def stored_form(value: object) -> bool:
    """The empty caption, or a caption Telegram stores exactly as given."""
    try:
        return type(value) is str and (value == '' or normalized_caption(value) == value)
    except (ValueError, UnicodeError):
        return False


def input_refusal(value: str) -> str | None:
    """None for a caption in its stored form; else the fixed detail that names why it is refused: edge whitespace
    (Telegram trims it), any other change under the normalisation, or the generic refusal for a control character."""
    if value == '' or stored_form(value):
        return None
    try:
        normalized_caption(value)
    except (ValueError, UnicodeError):
        return REFUSED
    unified = unicodedata.normalize('NFKC', value.replace('\r\n', '\n').replace('\r', '\n'))
    return WHITESPACE if unified[:1].isspace() or unified[-1:].isspace() else REWRITTEN


def caption_styles(caption: str, entities) -> FrozenOutgoingEntities:
    """The caller's canonical styles over the final caption; an empty caption takes none."""
    if caption == '':
        if entities:
            raise ValueError('an empty caption carries no styles')
        return ()
    return freeze_outgoing_entities(caption, list(entities))


def caption_request(chat_id: int, message_id: int, new_caption: str, styles: FrozenOutgoingEntities,
                    show_caption_above_media: bool) -> dict:
    """The one closed editMessageCaption request: no markup, the reviewed above-media flag, and the reviewed caption
    with the caller's styles, or exactly the empty formatted text that removes the caption (never null)."""
    return {'@type': 'editMessageCaption', 'chat_id': chat_id, 'message_id': message_id, 'reply_markup': None,
            'caption': {'@type': 'formattedText', 'text': new_caption, 'entities': native_outgoing_entities(styles)},
            'show_caption_above_media': show_caption_above_media}


def own_media_source(raw: object, chat_id: int, message_id: int) -> ForwardSource:
    """Own-message checks in the stated order for an own media message, then F14's projection, whose refusals keep
    their codes. Absence of a safety field is never safe."""
    try:
        return own_source(raw, chat_id, message_id, content_types=frozenset(CONTENT_KINDS))
    except EditRefusal as refusal:
        raise CaptionRefusal(refusal.code if refusal.code in REFUSALS else 'unsupported_source_content') from None


@dataclass(frozen=True)
class CaptionEditSnapshot:
    """One read of chat identity, the own-media projection and its current edit permission."""
    source: ForwardSource
    chat_identity: tuple
    chat_title: str
    can_be_edited: bool
    chat_kind: str

    def __post_init__(self):
        if (not isinstance(self.source, ForwardSource) or self.source.content_type not in CONTENT_KINDS or
                self.source.value['is_forward'] or type(self.chat_identity) is not tuple or
                type(self.chat_title) is not str or not self.chat_title or type(self.can_be_edited) is not bool or
                self.chat_kind not in ('private', 'basic_group', 'supergroup', 'channel')):
            raise ValueError('invalid caption edit snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int, message: object, message_id: int,
               properties: object) -> 'CaptionEditSnapshot':
        """Reuse the verified-target identity rules (secret chats are refused) for identity, sanitized title and
        public kind."""
        from .verified_targets import TargetGuardFailure, _identity
        try:
            identity, metadata = _identity(chat, chat_id)
        except TargetGuardFailure:
            raise ValueError('caption edit chat is unavailable') from None
        source = own_media_source(message, chat_id, message_id)
        if (type(properties) is not dict or properties.get('@type') != 'messageProperties' or
                type(properties.get('can_be_edited')) is not bool):
            raise ValueError('caption edit message properties are unavailable')
        return cls(source, identity, metadata.title, properties['can_be_edited'], metadata.chat_type)

    @property
    def anchor(self) -> tuple[int, int]:
        return self.source.anchor

    @property
    def content_type(self) -> str:
        return self.source.content_type

    @property
    def content_kind(self) -> str:
        return CONTENT_KINDS[self.content_type]

    @property
    def raw_caption(self) -> str:
        return self.source.value['content']['caption']['text']

    @property
    def entities(self) -> list[dict]:
        return self.source.value['content']['caption']['entities']

    @property
    def show_caption_above_media(self) -> bool:
        return self.source.value['content'].get('show_caption_above_media', False) is True

    @property
    def edit_date(self) -> int:
        return self.source.value['edit_date']

    def formatting(self) -> FrozenOutgoingEntities:
        """The current caption's formatting Telegram does not detect again, as canonically ordered rows."""
        return tuple(sorted(((row['offset'], row['length'], row['type']) for row in self.entities
                             if row['type'] not in AUTO_DETECTED), key=lambda row: (row[0], -row[1], row[2])))

    def unchanged(self, new_caption: str, styles: FrozenOutgoingEntities) -> bool:
        """The same caption with the same formatting: nothing for Telegram to change."""
        return new_caption == self.raw_caption and styles == self.formatting()

    def refusal(self) -> str | None:
        return None if self.can_be_edited else 'not_editable'


def _evidence_size(current: str | None, rows, new: str | None, styles) -> int:
    return (len(current or '') + sum(len(row[3]) for row in rows) + len(new or '') +
            sum(len(row[3]) for row in styles))


def _style_rows(caption: str, styles: FrozenOutgoingEntities) -> list[tuple[int, int, str, str]]:
    return list(outgoing_entity_facts(caption, styles)) if styles else []


def evidence_fits(snapshot: CaptionEditSnapshot, new_caption: str, styles: FrozenOutgoingEntities) -> bool:
    """The dialog prints the current caption, every covered span, the new caption and every new style; bound them."""
    current = escaped(snapshot.raw_caption) if snapshot.raw_caption else None
    rows = entity_rows(snapshot.raw_caption, snapshot.entities) if snapshot.raw_caption else []
    new = escaped(new_caption) if new_caption else None
    return _evidence_size(current, rows, new, [(*row[:3], escaped(row[3])) for row in
                                               _style_rows(new_caption, styles)]) <= MAX_EVIDENCE


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat, message_id: int,
                  content_kind: str, current_caption: str | None, current_entities: list,
                  show_caption_above_media: bool, new_caption: str | None, outgoing_entities: list,
                  current_edit_date: int, source_sha256: str) -> dict:
    """Bind the displayed evidence to the exact closed editMessageCaption request."""
    raw_new = unescaped(new_caption) if new_caption is not None else ''
    rows = [entity.model_dump(mode='json') if isinstance(entity, OutgoingEntity) else dict(entity)
            for entity in outgoing_entities]
    return {'domain': 'telegram-search-mcp.caption_edit', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'chat': chat.model_dump(mode='json'), 'message_id': message_id, 'content_kind': content_kind,
            'current_caption': current_caption,
            'current_entities': [entity.model_dump(mode='json') if isinstance(entity, ForwardEntity) else dict(entity)
                                 for entity in current_entities],
            'show_caption_above_media': show_caption_above_media, 'new_caption': new_caption,
            'outgoing_entities': rows, 'current_edit_date': current_edit_date, 'source_sha256': source_sha256,
            'request': caption_request(chat.chat_id, message_id, raw_new, caption_styles(raw_new, rows),
                                       show_caption_above_media)}


class CaptionEditDraftPreview(_Frozen):
    """Current and new caption as marked reversible JSON escapes (never instructions); raw UTF-16 offsets. A null
    current_caption means the message has none; a null new_caption means the edit removes the caption. Of the
    current entities, the auto-detected kinds are detected again by Telegram in the new caption; the rest is
    formatting this edit replaces with outgoing_entities, the caller's styles over the new caption."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    chat: EditChat
    message_id: PositiveMessageId
    content_kind: ContentKind
    current_caption: Escaped | None = None
    current_entities: Annotated[list[ForwardEntity], Field(max_length=MAX_ENTITIES)] = Field(default_factory=list)
    show_caption_above_media: StrictBool
    new_caption: Escaped | None = None
    outgoing_entities: StyleList = Field(default_factory=list)
    current_edit_date: EditDate
    source_sha256: Hash
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        current, new = self.raw_current_caption, self.raw_new_caption
        try:
            rows = entity_rows(current, self.current_entities) if current else []
            styles = caption_styles(new, [row.model_dump(mode='json') for row in self.outgoing_entities])
        except (ValueError, KeyError):
            raise ValueError('caption edit preview facts disagree') from None
        current_formatting = tuple(sorted(((row.offset, row.length, row.type) for row in self.current_entities
                                           if row.type not in AUTO_DETECTED), key=lambda row: (row[0], -row[1], row[2])))
        facts = preview_facts(**{key: getattr(self, key) for key in (
            'draft_id', 'account_id', 'expires_at', 'chat', 'message_id', 'content_kind', 'current_caption',
            'current_entities', 'show_caption_above_media', 'new_caption', 'outgoing_entities', 'current_edit_date',
            'source_sha256')})
        if (self.approval_required is not True or (current == '') != (self.current_caption is None) or
                (current == '' and self.current_entities) or not stored_form(new) or
                [row.model_dump(mode='json') for row in self.outgoing_entities] !=
                [{'offset': offset, 'length': length, 'type': {'@type': kind}} for offset, length, kind in styles] or
                (new == current and styles == current_formatting) or
                (self.show_caption_above_media and self.content_kind not in ABOVE_MEDIA_KINDS) or
                _evidence_size(self.current_caption, rows, self.new_caption,
                               [(*row[:3], escaped(row[3])) for row in _style_rows(new, styles)]) > MAX_EVIDENCE or
                digest(facts) != self.preview_sha256):
            raise ValueError('caption edit preview facts disagree')
        return self

    @property
    def raw_current_caption(self) -> str:
        return unescaped(self.current_caption) if self.current_caption is not None else ''

    @property
    def raw_new_caption(self) -> str:
        return unescaped(self.new_caption) if self.new_caption is not None else ''

    def entity_rows(self) -> list[tuple[str, int, int, str]]:
        return entity_rows(self.raw_current_caption, self.current_entities) if self.raw_current_caption else []

    def styles(self) -> FrozenOutgoingEntities:
        return caption_styles(self.raw_new_caption, [row.model_dump(mode='json') for row in self.outgoing_entities])

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
               spec: 'CaptionEditSpec') -> 'CaptionEditDraftPreview':
        if (type(spec) is not CaptionEditSpec or not isinstance(chat, EditChat) or
                chat.chat_id != spec.snapshot.anchor[0] or chat.kind != spec.snapshot.chat_kind):
            raise ValueError('caption edit snapshot is unavailable')
        snapshot = spec.snapshot
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), chat=chat,
                      message_id=snapshot.anchor[1], content_kind=snapshot.content_kind,
                      current_caption=escaped(snapshot.raw_caption) if snapshot.raw_caption else None,
                      current_entities=[ForwardEntity(**row) for row in snapshot.entities],
                      show_caption_above_media=snapshot.show_caption_above_media,
                      new_caption=escaped(spec.new_caption) if spec.new_caption else None,
                      outgoing_entities=[OutgoingEntity(offset=offset, length=length, type={'@type': kind})
                                         for offset, length, kind in spec.styles],
                      current_edit_date=snapshot.edit_date, source_sha256=snapshot.source.source_sha256)
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


def edited_caption_result(value: object, *, chat_id: int, message_id: int, content_type: str, new_caption: str,
                          styles: FrozenOutgoingEntities, show_caption_above_media: bool,
                          previous_edit_date: int) -> int | None:
    """The returned message must be the same message of the same content kind, now with exactly the new caption,
    exactly the caller's styles plus only fieldless auto-detected entities, the reviewed above-media flag where the
    kind has one, and a non-zero edit_date not before the previous one; anything else is unknown."""
    content = value.get('content') if type(value) is dict else None
    caption = content.get('caption') if type(content) is dict else None
    edit_date = value.get('edit_date') if type(value) is dict else None
    if (type(value) is not dict or value.get('@type') != 'message' or type(value.get('id')) is not int or
            value['id'] != message_id or type(value.get('chat_id')) is not int or value['chat_id'] != chat_id or
            type(content) is not dict or content_type not in CONTENT_KINDS or content.get('@type') != content_type or
            type(caption) is not dict or set(caption) != {'@type', 'text', 'entities'} or
            caption['@type'] != 'formattedText' or type(caption['text']) is not str or caption['text'] != new_caption or
            type(caption['entities']) is not list or len(caption['entities']) > MAX_ENTITIES or
            type(edit_date) is not int or not 0 < edit_date <= 2**31 - 1 or type(previous_edit_date) is not int or
            edit_date < previous_edit_date):
        return None
    if CONTENT_KINDS[content_type] in ABOVE_MEDIA_KINDS and content.get('show_caption_above_media') is not \
            show_caption_above_media:
        return None
    bound = None
    if content_type in OWN_DURATION:
        media = content.get(OWN_DURATION[content_type])
        duration = media.get('duration') if type(media) is dict else None
        bound = -1 if type(duration) is not int or duration < 0 else duration or None
    # The shared styled-edit rule (0.57): exactly the caller's styles, only auto-detected extras on whole scalars.
    return edit_date if exact_styles(new_caption, caption['entities'], styles, max_media_timestamp=bound) else None


@dataclass(frozen=True)
class CaptionEditSpec:
    """Immutable internal draft payload; the registry stores it under kind 'caption_edit'."""
    snapshot: CaptionEditSnapshot
    new_caption: str
    styles: FrozenOutgoingEntities = ()

    def __post_init__(self):
        if (not isinstance(self.snapshot, CaptionEditSnapshot) or not stored_form(self.new_caption) or
                type(self.styles) is not tuple or self.snapshot.can_be_edited is not True):
            raise ValueError('invalid caption edit draft')
        try:
            canonical = caption_styles(self.new_caption, [
                {'offset': offset, 'length': length, 'type': {'@type': kind}} for offset, length, kind in self.styles])
        except (ValueError, TypeError):
            raise ValueError('invalid caption edit draft') from None
        if canonical != self.styles or self.snapshot.unchanged(self.new_caption, self.styles):
            raise ValueError('invalid caption edit draft')

    @property
    def supersede_key(self) -> tuple:
        return ('caption_edit', *self.snapshot.anchor)

    @property
    def chat_id(self) -> int:
        return self.snapshot.anchor[0]

    @property
    def sha256(self) -> str:
        return self.snapshot.source.source_sha256

    @property
    def size_bytes(self) -> int:
        return len(self.new_caption.encode('utf-8'))

    @property
    def extra_capabilities(self) -> tuple[str, ...]:
        """Beyond the kind's own capability: a styled caption needs edit_own_styled_text too (0.57)."""
        return ('edit_own_styled_text',) if self.styles else ()


class PrepareCaptionEditRequest(_Closed):
    chat: ChatId
    message_id: PositiveMessageId
    new_caption: NewCaption
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)


class PrepareCaptionEditResponse(NextActionHint, _Closed):
    """`capability_disabled` names only the missing capabilities (edit_own_styled_text for a styled caption), from
    the closed set and never from input; `unavailable` reveals nothing."""
    status: Literal['prepared', 'refused', 'capability_disabled', 'unavailable']
    caption_edit: CaptionEditDraftPreview | None = None
    refusal: Refusal | None = None
    missing_capabilities: MissingCapabilities | None = optional_field()
    detail: Literal['immutable caption edit draft prepared; inspect full evidence and obtain fresh approval',
                    'message is not eligible for this caption edit; no draft was created',
                    'new caption must not start or end with whitespace; Telegram trims it',
                    'new caption changes under Telegram normalization (NFKC, line breaks, rewritten characters); '
                    'send its stored form',
                    'caption edit draft is unavailable',
                    'a styled caption needs capabilities the owner has not enabled: missing_capabilities']
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE,
                    'capability_disabled': DISABLED}[self.status]
        if self.status == 'refused' and self.refusal == 'invalid_input' and self.detail in (WHITESPACE, REWRITTEN):
            expected = self.detail
        names = self.missing_capabilities
        if (self.detail != expected or (self.status == 'prepared') != (self.caption_edit is not None) or
                (self.status == 'refused') != (self.refusal is not None) or
                (self.status == 'capability_disabled') != (names is not None) or
                (names is not None and len(set(names)) != len(names))):
            raise ValueError('invalid caption edit preparation response')
        return self


class ApplyPreparedCaptionEditRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedCaptionEditResponse(NextActionHint, _Closed):
    status: ApplyStatus
    draft_id: DraftId
    chat_id: ChatId | None = None
    message_id: PositiveMessageId | None = None
    edit_date: Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)] | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'applied') != (self.edit_date is not None):
            raise ValueError('only an applied edit carries the observed edit date')
        return self
