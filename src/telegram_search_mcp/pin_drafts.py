"""Closed public contracts for one owner-approved pin or unpin of one message (0.57, F17 rest).

The message may be anyone's, so its content is untrusted evidence: it is projected and hashed exactly like an F14
forward source, and the preview shows it the way a forward preview does. Content that projection cannot show (a
service message, a sticker, a poll, buttons, a hidden entity, a scheduled or sending message) is refused. The
snapshot adds the message's own `is_pinned` and `messageProperties.can_be_pinned`.

A pin is silent and for every participant: `pinChatMessage` with `disable_notification` true and `only_for_self`
false, both fixed; an unpin is `unpinChatMessage` with exact keys. Pinning posts a visible service message. The final
state is a bounded `getMessage.is_pinned` observation. A 4xx proves the attempt changed nothing; one read-back then
classifies it as the message already being in the target state (`already_pinned`, `not_pinned`), since a pin that
races another pin answers a 4xx rather than "not modified", or as `failed`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .edit_drafts import EditChat
from .forward_drafts import ForwardSourcePreview, PositiveMessageId, _Frozen, _utc
from .forward_sources import ForwardRefusal, ForwardSource
from .next_actions import NextAction, NextActionHint, next_action_field
from .reply_drafts import digest
from .schemas import ChatId

PREPARED = 'immutable pin change draft prepared; inspect full evidence and obtain fresh approval'
ALREADY_PINNED = 'message is already pinned; no draft was created'
NOT_PINNED = 'message is not pinned; no draft was created'
REFUSED = 'message is not eligible for this pin change; no draft was created'
UNAVAILABLE = 'pin change draft is unavailable'
PREVIEW_VERSION = 1
ACTIONS = ('pin', 'unpin')
REFUSALS = frozenset({'unsupported_source_content', 'unsupported_source_state', 'hidden_source_content',
                      'not_pinnable'})
# The receipt evidence of a 4xx whose read-back found the target state: the attempt failed, the state holds.
IN_TARGET_EVIDENCE = {True: 'provider_failed_already_pinned', False: 'provider_failed_not_pinned'}
Action = Literal['pin', 'unpin']
Refusal = Literal['unsupported_source_content', 'unsupported_source_state', 'hidden_source_content', 'not_pinnable']
ApplyStatus = Literal['applied', 'already_pinned', 'not_pinned', 'failed', 'local_failed', 'declined', 'timeout',
                      'dialog_unavailable', 'outcome_unknown', 'expired', 'message_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
ChatKinds = ('private', 'basic_group', 'supergroup', 'channel')


class PinRefusal(ValueError):
    """A closed, content-free refusal code for a message this pin change cannot show or act on."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown pin refusal')
        self.code = code
        super().__init__(code)


def pin_state(raw: object, chat_id: int, message_id: int) -> bool:
    """The exact message's own `is_pinned`; anything but a strict bool of that message is unavailable."""
    if (type(raw) is not dict or raw.get('@type') != 'message' or type(raw.get('chat_id')) is not int or
            raw['chat_id'] != chat_id or type(raw.get('id')) is not int or raw['id'] != message_id or
            type(raw.get('is_pinned')) is not bool):
        raise ValueError('pin state is unavailable')
    return raw['is_pinned']


def pin_request(action: str, chat_id: int, message_id: int) -> dict:
    """The one closed request of each action: a silent pin for everyone, or an unpin with exact keys."""
    if action == 'pin':
        return {'@type': 'pinChatMessage', 'chat_id': chat_id, 'message_id': message_id,
                'disable_notification': True, 'only_for_self': False}
    if action == 'unpin':
        return {'@type': 'unpinChatMessage', 'chat_id': chat_id, 'message_id': message_id}
    raise ValueError('unknown pin action')


@dataclass(frozen=True)
class PinSnapshot:
    """One read of chat identity, the message projection, its pin state and its pin permission."""
    source: ForwardSource
    chat_identity: tuple
    chat_title: str
    chat_kind: str
    is_pinned: bool
    can_be_pinned: bool

    def __post_init__(self):
        if (not isinstance(self.source, ForwardSource) or type(self.chat_identity) is not tuple or
                type(self.chat_title) is not str or not self.chat_title or self.chat_kind not in ChatKinds or
                type(self.is_pinned) is not bool or type(self.can_be_pinned) is not bool):
            raise ValueError('invalid pin snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int, message: object, message_id: int, properties: object) -> 'PinSnapshot':
        """Reuse the verified-target identity rules (secret chats are refused), F14's projection (its refusals keep
        their codes) and its display rules, so a prepared preview can always be shown."""
        from .verified_targets import TargetGuardFailure, _identity
        try:
            identity, metadata = _identity(chat, chat_id)
        except TargetGuardFailure:
            raise ValueError('pin chat is unavailable') from None
        try:
            source = ForwardSource.from_message(message, chat_id, message_id)
            ForwardSourcePreview.from_source(source, metadata.title)  # display bounds and name rules
        except ForwardRefusal as refusal:
            raise PinRefusal(refusal.code if refusal.code in REFUSALS else 'unsupported_source_content') from None
        is_pinned = pin_state(message, chat_id, message_id)
        if (type(properties) is not dict or properties.get('@type') != 'messageProperties' or
                type(properties.get('can_be_pinned')) is not bool):
            raise ValueError('pin message properties are unavailable')
        return cls(source, identity, metadata.title, metadata.chat_type, is_pinned, properties['can_be_pinned'])

    @property
    def anchor(self) -> tuple[int, int]:
        return self.source.anchor

    def refusal(self) -> str | None:
        return None if self.can_be_pinned else 'not_pinnable'


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, action: str, chat: EditChat,
                  message: ForwardSourcePreview) -> dict:
    """Bind the displayed evidence to the exact closed pin or unpin request."""
    return {'domain': 'telegram-search-mcp.pin_change', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'action': action, 'chat': chat.model_dump(mode='json'), 'message': message.model_dump(mode='json'),
            'request': pin_request(action, chat.chat_id, message.message_id)}


class PinChangeDraftPreview(_Frozen):
    """The action, the chat and the message as a forward preview shows a source: its text or caption in marked
    reversible escapes (never instructions), its sender, date and media facts. The message may be another member's."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    action: Action
    chat: EditChat
    message: ForwardSourcePreview
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        facts = preview_facts(draft_id=self.draft_id, account_id=self.account_id, expires_at=self.expires_at,
                              action=self.action, chat=self.chat, message=self.message)
        if (self.approval_required is not True or self.message.chat.chat_id != self.chat.chat_id or
                self.message.chat.title != self.chat.title or digest(facts) != self.preview_sha256):
            raise ValueError('pin change preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
               spec: 'PinChangeSpec') -> 'PinChangeDraftPreview':
        if (type(spec) is not PinChangeSpec or not isinstance(chat, EditChat) or
                chat.chat_id != spec.snapshot.anchor[0] or chat.kind != spec.snapshot.chat_kind or
                chat.title != spec.snapshot.chat_title):
            raise ValueError('pin snapshot is unavailable')
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), action=spec.action,
                      chat=chat, message=ForwardSourcePreview.from_source(spec.snapshot.source, chat.title))
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


@dataclass(frozen=True)
class PinChangeSpec:
    """Immutable internal draft payload; the registry stores it under kind 'pin_change'. The reviewed state is the
    opposite of the action's target, and the message must be pinnable."""
    snapshot: PinSnapshot
    action: str

    def __post_init__(self):
        if (not isinstance(self.snapshot, PinSnapshot) or self.action not in ACTIONS or
                self.snapshot.can_be_pinned is not True or self.snapshot.is_pinned is self.target):
            raise ValueError('invalid pin change draft')

    @property
    def target(self) -> bool:
        """The pin state the action produces."""
        return self.action == 'pin'

    @property
    def supersede_key(self) -> tuple:
        return ('pin_change', *self.snapshot.anchor)

    @property
    def chat_id(self) -> int:
        return self.snapshot.anchor[0]

    @property
    def sha256(self) -> str:
        return self.snapshot.source.source_sha256

    @property
    def size_bytes(self) -> int:
        return 1


class PreparePinChangeRequest(_Closed):
    chat: ChatId
    message_id: PositiveMessageId
    action: Action


class PreparePinChangeResponse(_Closed):
    """`already_pinned` and `not_pinned` mean the message already is in the requested state: no draft. No status
    names a recovery condition, so it carries no next_action (the 0.54 rule)."""
    status: Literal['prepared', 'already_pinned', 'not_pinned', 'refused', 'unavailable']
    pin_change: PinChangeDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable pin change draft prepared; inspect full evidence and obtain fresh approval',
                    'message is already pinned; no draft was created', 'message is not pinned; no draft was created',
                    'message is not eligible for this pin change; no draft was created',
                    'pin change draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'already_pinned': ALREADY_PINNED, 'not_pinned': NOT_PINNED,
                    'refused': REFUSED, 'unavailable': UNAVAILABLE}[self.status]
        if (self.detail != expected or (self.status == 'prepared') != (self.pin_change is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid pin change preparation response')
        return self


class ApplyPreparedPinChangeRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedPinChangeResponse(NextActionHint, _Closed):
    """`is_pinned` is the observed pin state: after `applied`, or with `already_pinned`/`not_pinned` (the state
    already held, before any attempt or as the read-back of a rejected one)."""
    status: ApplyStatus
    draft_id: DraftId
    chat_id: ChatId | None = None
    message_id: PositiveMessageId | None = None
    is_pinned: StrictBool | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def exact(self):
        if ((self.status in ('applied', 'already_pinned', 'not_pinned')) != (self.is_pinned is not None) or
                (self.status == 'already_pinned' and self.is_pinned is not True) or
                (self.status == 'not_pinned' and self.is_pinned is not False)):
            raise ValueError('only an observed pin state carries is_pinned')
        return self
