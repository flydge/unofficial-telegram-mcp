"""Closed public contracts for one owner-approved edit of an own text message.

The current message is projected and hashed exactly like an F14 forward source (text,
entities, edit_date, link preview options); only fieldless visible entities are admitted.
A plain edit sends the normalized new text without entities (0.52). A styled edit (0.57, also
needs edit_own_styled_text) sends the caller's bold, italic, underline, strikethrough or spoiler
spans over new text already in the form Telegram stores (input the normalisation would change is
refused, never rewritten, since the spans address the text as given); its result must carry
exactly those spans, Telegram's auto-detected entities being the only extras tolerated.
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
from .reply_drafts import MissingCapabilities, digest
from .sanitize import sanitize_telegram_text
from .schemas import ChatId
from .next_actions import NextAction, NextActionHint, next_action_field, optional_field
from .outgoing_entity_models import OutgoingEntity, OutgoingEntityList
from .outgoing_formatting import (FrozenOutgoingEntities, _STYLES, _boundaries, freeze_outgoing_entities,
                                  native_outgoing_entities, outgoing_entity_facts)

PREPARED = 'immutable text edit draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for an own text edit; no draft was created'
UNAVAILABLE = 'text edit draft is unavailable'
# Telegram trims spaces and line feeds at both ends and its clean_input_string rewrites the characters below. Since 20.10.2026
# the shared normalisation applies those rewrites before the preview, so these checks only guard input that
# skipped it; the two fixed details stay in the output schema.
WHITESPACE = 'new text must not start or end with whitespace; Telegram trims it'
# U+202A-U+202E are format characters the normalisation already refuses.
REWRITTEN_CHARACTERS = frozenset('\t\u2028\u2029\u202a\u202b\u202c\u202d\u202e\u030a\u0333\u033f')
REWRITTEN = 'new text contains characters Telegram rewrites (tab, U+2028–U+202E, U+030A, U+0333, U+033F)'
# 0.57 styled edits: the caller's spans address new_text as given, so it is refused unless already stored.
DISABLED = 'a styled text edit needs capabilities the owner has not enabled: missing_capabilities'
STYLED_REWRITTEN = ('styled new text changes under Telegram normalization (NFKC, line breaks, rewritten characters); '
                    'send its stored form')
PREVIEW_VERSION = 1
REFUSALS = frozenset({'not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                      'unsupported_source_state', 'not_editable', 'unchanged_text', 'invalid_input'})

Refusal = Literal['not_outgoing', 'unsupported_source_content', 'source_is_forward', 'hidden_source_content',
                  'unsupported_source_state', 'not_editable', 'unchanged_text', 'invalid_input']
ApplyStatus = Literal['applied', 'failed', 'local_failed', 'declined', 'timeout', 'dialog_unavailable',
                      'outcome_unknown', 'expired', 'message_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
# Entity kinds Telegram detects again in plain text; the dialog lists them apart from removed formatting.
AUTO_DETECTED = frozenset({'textEntityTypeUrl', 'textEntityTypeEmailAddress', 'textEntityTypePhoneNumber',
                           'textEntityTypeMention', 'textEntityTypeHashtag', 'textEntityTypeCashtag',
                           'textEntityTypeBotCommand', 'textEntityTypeBankCardNumber'})
ChatKind = Literal['private', 'basic_group', 'supergroup', 'channel']
EditDate = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
NewText = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
# A styled edit's own spans: the styles prepare_text_send admits, gated by edit_own_styled_text (0.57).
StyleList = Annotated[list[OutgoingEntity], Field(strict=True, min_length=1, max_length=32)]


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
    """A space or line feed first or last: the only characters the pinned TDLib trims at the edges."""
    return type(text) is str and bool(text) and (text[0] in ' \n' or text[-1] in ' \n')


def rewritten_text(text: object) -> bool:
    """A character of the normalized text that the pinned TDLib rewrites or removes."""
    return type(text) is str and not REWRITTEN_CHARACTERS.isdisjoint(text)


def styled_input_refusal(value: str) -> str | None:
    """None for new text already in the form Telegram stores; else the fixed detail that names why a styled edit
    refuses it: edge whitespace (Telegram trims it), any other change under the normalisation, or the generic refusal
    for a control character or an empty result. Never a rewrite: the caller's spans address the text as given."""
    import unicodedata
    try:
        if normalized_text(value) == value and not edge_whitespace(value) and not rewritten_text(value):
            return None
    except (ValueError, UnicodeError):
        return REFUSED
    unified = unicodedata.normalize('NFKC', value.replace('\r\n', '\n').replace('\r', '\n'))
    return WHITESPACE if unified[:1].isspace() or unified[-1:].isspace() else STYLED_REWRITTEN


def frozen_styles(text: str, entities) -> FrozenOutgoingEntities:
    """The caller's canonical styles over the final text; none for no entities."""
    return freeze_outgoing_entities(text, list(entities)) if entities else ()


def exact_styles(text: object, entities: object, styles: FrozenOutgoingEntities) -> bool:
    """The verification rule of every styled edit (0.57): exactly the caller's styles, plus only fieldless
    auto-detected entities on whole scalars of the text; any other entity, field or span is not the edit."""
    if type(text) is not str or type(entities) is not list or len(entities) > MAX_ENTITIES:
        return False
    caller, extras = [], []
    for row in entities:
        kind = row.get('type') if type(row) is dict else None
        if (type(row) is not dict or set(row) != {'@type', 'offset', 'length', 'type'} or row['@type'] != 'textEntity'
                or type(kind) is not dict or set(kind) != {'@type'} or type(row['offset']) is not int or
                type(row['length']) is not int):
            return False
        if kind['@type'] in _STYLES:
            caller.append({'offset': row['offset'], 'length': row['length'], 'type': {'@type': kind['@type']}})
        elif kind['@type'] in AUTO_DETECTED:
            extras.append((row['offset'], row['length']))
        else:
            return False
    try:
        if frozen_styles(text, caller) != styles:
            return False
        bounds = _boundaries(text) if extras else {}
        if any(length <= 0 or offset not in bounds or offset + length not in bounds for offset, length in extras):
            return False
    except (ValueError, TypeError):
        return False
    return True


def own_text_source(raw: object, chat_id: int, message_id: int) -> ForwardSource:
    """Own-message checks in the stated order, then F14's projection (state, markup, entities,
    link preview), whose refusals keep their codes. Absence of a safety field is never safe."""
    return own_source(raw, chat_id, message_id, content_types=frozenset({'messageText'}))


def own_source(raw: object, chat_id: int, message_id: int, *, content_types: frozenset) -> ForwardSource:
    """The own-message checks of every edit (0.57: a caption edit admits the media kinds), in the stated order, then
    F14's projection, whose refusals keep their codes."""
    if (type(raw) is not dict or raw.get('@type') != 'message' or type(raw.get('chat_id')) is not int or
            raw['chat_id'] != chat_id or type(raw.get('id')) is not int or raw['id'] != message_id or
            type(raw.get('is_outgoing')) is not bool):
        raise ValueError('edit message is unavailable')
    if raw['is_outgoing'] is not True:
        raise EditRefusal('not_outgoing')
    content = raw.get('content')
    if type(content) is not dict or content.get('@type') not in content_types:
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

    def formatting(self) -> FrozenOutgoingEntities:
        """The current text's formatting Telegram does not detect again, as canonically ordered rows."""
        return tuple(sorted(((row['offset'], row['length'], row['type']) for row in self.entities
                             if row['type'] not in AUTO_DETECTED), key=lambda row: (row[0], -row[1], row[2])))

    def unchanged(self, new_text: str, styles: FrozenOutgoingEntities) -> bool:
        """Nothing for Telegram to change: the same text (plain, 0.52), or the same text with the same formatting
        (styled)."""
        return new_text == self.raw_text and (not styles or styles == self.formatting())

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


def edit_request(chat_id: int, message_id: int, new_text: str, styles: FrozenOutgoingEntities = ()) -> dict:
    """The closed editMessageText request: no markup, preview options or draft clearing. The plain shape carries no
    entities; the styled shape (0.57) only the draft's reviewed caller styles."""
    return {'@type': 'editMessageText', 'chat_id': chat_id, 'message_id': message_id, 'reply_markup': None,
            'input_message_content': {'@type': 'inputMessageText',
                                      'text': {'@type': 'formattedText', 'text': new_text,
                                               'entities': native_outgoing_entities(styles)},
                                      'link_preview_options': None, 'clear_draft': False}}


def _evidence_size(current: str, rows, styles=()) -> int:
    return len(current) + sum(len(row[3]) for row in rows) + sum(len(row[3]) for row in styles)


def _style_rows(text: str, styles: FrozenOutgoingEntities) -> list:
    """Each new style with its covered text escaped, as the dialog prints it."""
    return [(*row[:3], escaped(row[3])) for row in outgoing_entity_facts(text, styles)] if styles else []


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat, message_id: int,
                  current_text: str, current_entities: list, new_text: str, current_edit_date: int,
                  source_sha256: str, outgoing_entities: list | None = None) -> dict:
    """Bind the displayed evidence to the exact closed editMessageText request. A plain edit's facts are 0.52's; a
    styled edit adds its caller styles, and its request carries them."""
    rows = None if outgoing_entities is None else [
        entity.model_dump(mode='json') if isinstance(entity, OutgoingEntity) else dict(entity)
        for entity in outgoing_entities]
    raw_new = unescaped(new_text)
    return {'domain': 'telegram-search-mcp.text_edit', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'chat': chat.model_dump(mode='json'), 'message_id': message_id, 'current_text': current_text,
            'current_entities': [entity.model_dump(mode='json') if isinstance(entity, ForwardEntity) else dict(entity)
                                 for entity in current_entities],
            'new_text': new_text, **({} if rows is None else {'outgoing_entities': rows}),
            'current_edit_date': current_edit_date, 'source_sha256': source_sha256,
            'request': edit_request(chat.chat_id, message_id, raw_new,
                                    () if rows is None else frozen_styles(raw_new, rows))}


class TextEditDraftPreview(_Frozen):
    """Current and new text as marked reversible JSON escapes (never instructions); raw UTF-16
    offsets. Of the current entities, the auto-detected kinds (URL, email, phone, mention,
    hashtag, cashtag, bot command, bank card) are detected again by Telegram in the new text;
    the rest is formatting this edit removes, or replaces with outgoing_entities (a styled edit
    only: the caller's styles over the new text; absent for a plain edit)."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    chat: EditChat
    message_id: PositiveMessageId
    current_text: Escaped
    current_entities: Annotated[list[ForwardEntity], Field(max_length=MAX_ENTITIES)] = Field(default_factory=list)
    new_text: Escaped
    outgoing_entities: StyleList | None = optional_field()
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
        try:
            styles = self.styles()
        except (ValueError, KeyError):
            raise ValueError('text edit preview facts disagree') from None
        current_formatting = tuple(sorted(((row.offset, row.length, row.type) for row in self.current_entities
                                           if row.type not in AUTO_DETECTED), key=lambda row: (row[0], -row[1], row[2])))
        facts = preview_facts(**{key: getattr(self, key) for key in (
            'draft_id', 'account_id', 'expires_at', 'chat', 'message_id', 'current_text', 'current_entities',
            'new_text', 'current_edit_date', 'source_sha256', 'outgoing_entities')})
        if (self.approval_required is not True or normalized_text(new) != new or edge_whitespace(new) or
                rewritten_text(new) or (new == current and (not styles or styles == current_formatting)) or
                (self.outgoing_entities is not None and
                 [row.model_dump(mode='json') for row in self.outgoing_entities] !=
                 [{'offset': offset, 'length': length, 'type': {'@type': kind}} for offset, length, kind in styles]) or
                _evidence_size(self.current_text, rows, _style_rows(new, styles)) > MAX_EVIDENCE or
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

    def styles(self) -> FrozenOutgoingEntities:
        """The styled edit's canonical caller styles over the new text; () for a plain edit."""
        return () if self.outgoing_entities is None else frozen_styles(
            self.raw_new_text, [row.model_dump(mode='json') for row in self.outgoing_entities])

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
               snapshot: EditSnapshot, new_text: str,
               styles: FrozenOutgoingEntities = ()) -> 'TextEditDraftPreview':
        if (not isinstance(snapshot, EditSnapshot) or not isinstance(chat, EditChat) or
                chat.chat_id != snapshot.anchor[0] or chat.kind != snapshot.chat_kind or type(styles) is not tuple):
            raise ValueError('edit snapshot is unavailable')
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), chat=chat,
                      message_id=snapshot.anchor[1], current_text=escaped(snapshot.raw_text),
                      current_entities=[ForwardEntity(**row) for row in snapshot.entities],
                      new_text=escaped(new_text), current_edit_date=snapshot.edit_date,
                      source_sha256=snapshot.source.source_sha256)
        if styles:
            values['outgoing_entities'] = [OutgoingEntity(offset=offset, length=length, type={'@type': kind})
                                           for offset, length, kind in styles]
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


def evidence_fits(snapshot: EditSnapshot, new_text: str = '', styles: FrozenOutgoingEntities = ()) -> bool:
    """The dialog prints the current text and every covered span (and a styled edit's new styles with their covered
    text); bound them like F14."""
    current = escaped(snapshot.raw_text)
    return _evidence_size(current, entity_rows(snapshot.raw_text, snapshot.entities),
                          _style_rows(new_text, styles)) <= MAX_EVIDENCE


def edited_text_result(value: object, *, chat_id: int, message_id: int, new_text: str,
                       previous_edit_date: int, styles: FrozenOutgoingEntities | None = None) -> int | None:
    """The returned message must be the same message, now messageText with exactly the new
    text and a non-zero edit_date not before the previous one; anything else is unknown. A styled
    edit (styles given, 0.57) also needs exactly its styles back, with only auto-detected extras."""
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
    if styles is not None and (set(text) != {'@type', 'text', 'entities'} or
                               not exact_styles(text['text'], text['entities'], styles)):
        return None
    return edit_date


@dataclass(frozen=True)
class TextEditSpec:
    """Immutable internal draft payload; the registry stores it under kind 'text_edit'. `styles` (0.57) makes it a
    styled edit, which also needs edit_own_styled_text."""
    snapshot: EditSnapshot
    new_text: str
    styles: FrozenOutgoingEntities = ()

    def __post_init__(self):
        if (not isinstance(self.snapshot, EditSnapshot) or type(self.new_text) is not str or
                normalized_text(self.new_text) != self.new_text or edge_whitespace(self.new_text) or
                rewritten_text(self.new_text) or type(self.styles) is not tuple or
                self.snapshot.can_be_edited is not True):
            raise ValueError('invalid text edit draft')
        try:
            canonical = frozen_styles(self.new_text, [
                {'offset': offset, 'length': length, 'type': {'@type': kind}} for offset, length, kind in self.styles])
        except (ValueError, TypeError):
            raise ValueError('invalid text edit draft') from None
        if canonical != self.styles or self.snapshot.unchanged(self.new_text, self.styles):
            raise ValueError('invalid text edit draft')

    @property
    def supersede_key(self) -> tuple:
        return ('text_edit', *self.snapshot.anchor)

    @property
    def chat_id(self) -> int:
        return self.snapshot.anchor[0]

    @property
    def sha256(self) -> str:
        return self.snapshot.source.source_sha256

    @property
    def size_bytes(self) -> int:
        return len(self.new_text.encode('utf-8'))

    @property
    def extra_capabilities(self) -> tuple[str, ...]:
        """Beyond the kind's own capability: a styled edit needs edit_own_styled_text too (0.57)."""
        return ('edit_own_styled_text',) if self.styles else ()


class PrepareTextEditRequest(_Closed):
    chat: ChatId
    message_id: PositiveMessageId
    new_text: NewText
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)


class PrepareTextEditResponse(NextActionHint, _Closed):
    """`capability_disabled` (0.57, a styled edit) names only the missing capabilities, from the closed set and never
    from input; `unavailable` reveals nothing."""
    status: Literal['prepared', 'refused', 'capability_disabled', 'unavailable']
    text_edit: TextEditDraftPreview | None = None
    refusal: Refusal | None = None
    missing_capabilities: MissingCapabilities | None = optional_field()
    detail: Literal['immutable text edit draft prepared; inspect full evidence and obtain fresh approval',
                    'message is not eligible for an own text edit; no draft was created',
                    'new text must not start or end with whitespace; Telegram trims it',
                    'new text contains characters Telegram rewrites (tab, U+2028–U+202E, U+030A, U+0333, U+033F)',
                    'styled new text changes under Telegram normalization (NFKC, line breaks, rewritten characters); '
                    'send its stored form',
                    'text edit draft is unavailable',
                    'a styled text edit needs capabilities the owner has not enabled: missing_capabilities']
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE,
                    'capability_disabled': DISABLED}[self.status]
        if self.status == 'refused' and self.refusal == 'invalid_input' and self.detail in (
                WHITESPACE, REWRITTEN, STYLED_REWRITTEN):
            expected = self.detail
        names = self.missing_capabilities
        if (self.detail != expected or (self.status == 'prepared') != (self.text_edit is not None) or
                (self.status == 'refused') != (self.refusal is not None) or
                (self.status == 'capability_disabled') != (names is not None) or
                (names is not None and len(set(names)) != len(names))):
            raise ValueError('invalid text edit preparation response')
        return self


class ApplyPreparedTextEditRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedTextEditResponse(NextActionHint, _Closed):
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
