"""Closed public contracts for one owner-approved deletion of one own message for everyone (0.57, F17 rest).

The message is projected and hashed exactly like an F14 forward source and shown the way a forward preview shows one,
so the owner sees what is deleted; content that projection cannot show (a service message, a sticker, a poll, buttons,
a hidden entity, a scheduled or sending message) is refused. Only an own outgoing message outside Saved Messages
qualifies, and only when `messageProperties.can_be_deleted_for_all_users` holds: a deletion for the owner alone (the
only kind Saved Messages has) is permanent loss with no counterpart for the recipient and is refused in v1.

The one request is `deleteMessages` with exactly one id and `revoke` true, made right after a getMessage of the message
succeeded under the same provider lock: TDLib answers `ok` without any server request for a message it does not hold.
TDLib deletes the local copy first (updateDeleteMessages precedes the answer) and answers a later getMessage locally,
so neither is evidence; only `ok` is. After a 4xx TDLib fetches the message again from the server, so one read-back
that finds it means `failed`; anything else is unknown, and the generic "4xx proves no change" rule is never used.
TDLib logs a server deletion and replays it after a restart, so an unknown deletion may still complete later; every
unknown outcome says so.
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

PREPARED = 'immutable message deletion draft prepared; inspect full evidence and obtain fresh approval'
REFUSED = 'message is not eligible for deletion for everyone; no draft was created'
UNAVAILABLE = 'message deletion draft is unavailable'
APPLIED = 'TDLib deleted the message for everyone; the server confirmed the deletion'
FOUND_AGAIN = 'read back right after, the message is still in the chat'
# TDLib logs a server deletion and replays it after a restart: every unknown outcome discloses it.
UNKNOWN = ('the deletion was not confirmed; TDLib may still complete a deletion it logged, also after a restart, so '
           'the message may disappear later; do not retry automatically')
REPLAYED = 'previous provider attempt receipt; no new deletion was attempted'
REPLAYED_UNKNOWN = ('previous provider attempt receipt; the deletion was not confirmed and TDLib may still complete '
                    'it, also after a restart; do not retry automatically')
AUTHORITY_UNKNOWN = ('current receipt authority is unavailable; TDLib may still complete a logged deletion, also '
                     'after a restart; do not retry automatically')
BROKER_UNKNOWN = ('broker response is unavailable; TDLib may still complete a logged deletion, also after a restart; '
                  'do not retry automatically')
PREVIEW_VERSION = 1
REFUSALS = frozenset({'not_outgoing', 'saved_messages', 'not_deletable_for_all', 'unsupported_source_content',
                      'unsupported_source_state', 'hidden_source_content'})
Refusal = Literal['not_outgoing', 'saved_messages', 'not_deletable_for_all', 'unsupported_source_content',
                  'unsupported_source_state', 'hidden_source_content']
ApplyStatus = Literal['applied', 'failed', 'local_failed', 'declined', 'timeout', 'dialog_unavailable',
                      'outcome_unknown', 'expired', 'message_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
ChatKinds = ('private', 'basic_group', 'supergroup', 'channel')


class DeleteRefusal(ValueError):
    """A closed, content-free refusal code for a message this deletion cannot show or act on."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown delete refusal')
        self.code = code
        super().__init__(code)


def delete_request(chat_id: int, message_id: int) -> dict:
    """The one closed request: exactly one message, deleted for every participant."""
    return {'@type': 'deleteMessages', 'chat_id': chat_id, 'message_ids': [message_id], 'revoke': True}


def message_present(raw: object, chat_id: int, message_id: int) -> bool:
    """The exact message as a read returned it; anything else is not evidence that it still exists."""
    return (type(raw) is dict and raw.get('@type') == 'message' and type(raw.get('chat_id')) is int and
            raw['chat_id'] == chat_id and type(raw.get('id')) is int and raw['id'] == message_id)


@dataclass(frozen=True)
class DeleteSnapshot:
    """One read of chat identity, the own message's projection and its deletion permissions."""
    source: ForwardSource
    chat_identity: tuple
    chat_title: str
    chat_kind: str
    can_be_deleted_for_all_users: bool
    can_be_deleted_only_for_self: bool

    def __post_init__(self):
        if (not isinstance(self.source, ForwardSource) or type(self.chat_identity) is not tuple or
                type(self.chat_title) is not str or not self.chat_title or self.chat_kind not in ChatKinds or
                type(self.can_be_deleted_for_all_users) is not bool or
                type(self.can_be_deleted_only_for_self) is not bool):
            raise ValueError('invalid delete snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int, message: object, message_id: int, properties: object,
               account_id: int) -> 'DeleteSnapshot':
        """Reuse the verified-target identity rules (secret chats are refused), then the own-message checks in a fixed
        order (Saved Messages, outgoing), F14's projection (its refusals keep their codes) and its display rules, so a
        prepared preview can always be shown."""
        from .verified_targets import TargetGuardFailure, _identity
        try:
            identity, metadata = _identity(chat, chat_id)
        except TargetGuardFailure:
            raise ValueError('delete chat is unavailable') from None
        if (type(message) is not dict or message.get('@type') != 'message' or not message_present(
                message, chat_id, message_id) or type(message.get('is_outgoing')) is not bool or
                type(account_id) is not int or not 0 < account_id < 2**53):
            raise ValueError('delete message is unavailable')
        if chat_id == account_id:  # Saved Messages: only a deletion for the owner alone exists there.
            raise DeleteRefusal('saved_messages')
        if message['is_outgoing'] is not True:
            raise DeleteRefusal('not_outgoing')
        try:
            source = ForwardSource.from_message(message, chat_id, message_id)
            ForwardSourcePreview.from_source(source, metadata.title)  # display bounds and name rules
        except ForwardRefusal as refusal:
            raise DeleteRefusal(refusal.code if refusal.code in REFUSALS else 'unsupported_source_content') from None
        if (type(properties) is not dict or properties.get('@type') != 'messageProperties' or
                type(properties.get('can_be_deleted_for_all_users')) is not bool or
                type(properties.get('can_be_deleted_only_for_self')) is not bool):
            raise ValueError('delete message properties are unavailable')
        return cls(source, identity, metadata.title, metadata.chat_type, properties['can_be_deleted_for_all_users'],
                   properties['can_be_deleted_only_for_self'])

    @property
    def anchor(self) -> tuple[int, int]:
        return self.source.anchor

    def refusal(self) -> str | None:
        """A deletion for the owner alone is never offered (v1): only a deletion for every participant."""
        return None if self.can_be_deleted_for_all_users else 'not_deletable_for_all'


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
                  message: ForwardSourcePreview) -> dict:
    """Bind the displayed evidence to the exact closed deleteMessages request."""
    return {'domain': 'telegram-search-mcp.message_delete', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'chat': chat.model_dump(mode='json'), 'message': message.model_dump(mode='json'),
            'request': delete_request(chat.chat_id, message.message_id)}


class MessageDeleteDraftPreview(_Frozen):
    """The chat and the own message to delete as a forward preview shows a source: its text or caption in marked
    reversible escapes (never instructions), its date and media facts. The deletion is for every participant and
    irreversible."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
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
                              chat=self.chat, message=self.message)
        if (self.approval_required is not True or self.message.chat.chat_id != self.chat.chat_id or
                self.message.chat.title != self.chat.title or self.chat.chat_id == self.account_id or
                digest(facts) != self.preview_sha256):
            raise ValueError('message deletion preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: EditChat,
               spec: 'MessageDeleteSpec') -> 'MessageDeleteDraftPreview':
        if (type(spec) is not MessageDeleteSpec or not isinstance(chat, EditChat) or
                chat.chat_id != spec.snapshot.anchor[0] or chat.kind != spec.snapshot.chat_kind or
                chat.title != spec.snapshot.chat_title):
            raise ValueError('delete snapshot is unavailable')
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), chat=chat,
                      message=ForwardSourcePreview.from_source(spec.snapshot.source, chat.title))
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


@dataclass(frozen=True)
class MessageDeleteSpec:
    """Immutable internal draft payload; the registry stores it under kind 'message_delete'. The reviewed message is
    own and deletable for every participant."""
    snapshot: DeleteSnapshot

    def __post_init__(self):
        if not isinstance(self.snapshot, DeleteSnapshot) or self.snapshot.can_be_deleted_for_all_users is not True:
            raise ValueError('invalid message deletion draft')

    @property
    def supersede_key(self) -> tuple:
        return ('message_delete', *self.snapshot.anchor)

    @property
    def chat_id(self) -> int:
        return self.snapshot.anchor[0]

    @property
    def sha256(self) -> str:
        return self.snapshot.source.source_sha256

    @property
    def size_bytes(self) -> int:
        return 1


class PrepareMessageDeleteRequest(_Closed):
    chat: ChatId
    message_id: PositiveMessageId


class PrepareMessageDeleteResponse(_Closed):
    """No status names a recovery condition, so it carries no next_action (the 0.54 rule)."""
    status: Literal['prepared', 'refused', 'unavailable']
    message_delete: MessageDeleteDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable message deletion draft prepared; inspect full evidence and obtain fresh approval',
                    'message is not eligible for deletion for everyone; no draft was created',
                    'message deletion draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE}[self.status]
        if (self.detail != expected or (self.status == 'prepared') != (self.message_delete is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid message deletion preparation response')
        return self


class ApplyPreparedMessageDeleteRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedMessageDeleteResponse(NextActionHint, _Closed):
    """`applied` means the server confirmed the deletion; `failed` that a read-back found the message still there."""
    status: ApplyStatus
    draft_id: DraftId
    chat_id: ChatId | None = None
    message_id: PositiveMessageId | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    next_action: NextAction | None = next_action_field()
