"""Closed public contracts for moving one chat between the Main and Archive lists."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .draft_models import _Closed, AccountId, DraftId, Hash
from .forward_drafts import _Frozen, _utc
from .reply_drafts import digest
from .sanitize import sanitize_telegram_text
from .schemas import ChatId

PREPARED = 'immutable archive draft prepared; inspect full evidence and obtain fresh approval'
ALREADY = 'chat is already in the target list; no draft was created'
REFUSED = 'chat is not eligible; no draft was created'
UNAVAILABLE = 'archive draft is unavailable'
PREVIEW_VERSION = 1
# Telegram behaviour stated in the preview and the dialog; `applied` reflects only the state
# observed right after the call.
ARCHIVE_NOTE = ('Telegram may move an archived chat whose notifications are enabled back to Main '
                'when a new message arrives')
LIST_TYPES = {'main': 'chatListMain', 'archive': 'chatListArchive'}
_MAX_POSITIONS = 64

ChatList = Literal['main', 'archive']
ChatKind = Literal['private', 'basic_group', 'supergroup', 'channel']
Refusal = Literal['unsupported_chat']
ApplyStatus = Literal['applied', 'already_in_target', 'failed', 'local_failed', 'declined', 'timeout',
                      'dialog_unavailable', 'outcome_unknown', 'expired', 'chat_changed', 'error']
StrictBool = Annotated[bool, Field(strict=True)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]


class ArchiveRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible chat."""

    def __init__(self, code: str):
        if code != 'unsupported_chat':
            raise ValueError('unknown archive refusal')
        self.code = code
        super().__init__(code)


def _order(value: object) -> bool:
    """TDLib serializes the int64 position order as a decimal string; a listed position is positive."""
    return (type(value) is str and 0 < len(value) <= 19 and value.isascii() and value.isdigit() and
            not (len(value) > 1 and value.startswith('0')) and 0 < int(value) < 2**63)


def _sponsored(source: object) -> bool:
    """TDLib JSON omits a null `source`; any ChatSource (MTProxy sponsor, PSA) is not membership."""
    if source is None:
        return False
    kind = source.get('@type') if type(source) is dict else None
    if type(kind) is not str or not kind.startswith('chatSource'):
        raise ValueError('chat positions are unavailable')
    return True


def current_list(positions: object) -> str:
    """The Main or Archive list that carries an own (source null) position; folder positions are
    out of scope and a sponsored or PSA position is never membership."""
    if type(positions) is not list or len(positions) > _MAX_POSITIONS:
        raise ValueError('chat positions are unavailable')
    seen, found = [], []
    for position in positions:
        chat_list = position.get('list') if type(position) is dict else None
        if (type(position) is not dict or position.get('@type') != 'chatPosition' or type(chat_list) is not dict or
                type(chat_list.get('@type')) is not str):
            raise ValueError('chat positions are unavailable')
        name = {value: key for key, value in LIST_TYPES.items()}.get(chat_list['@type'])
        if name is None:
            continue
        if set(chat_list) != {'@type'} or not _order(position.get('order')) or name in seen:
            raise ValueError('chat positions are unavailable')
        seen.append(name)
        if not _sponsored(position.get('source')):
            found.append(name)
    if len(found) > 1:
        raise ValueError('chat positions are unavailable')
    return found[0] if found else 'none'


@dataclass(frozen=True)
class ArchiveSnapshot:
    """One read of chat identity, sanitized title, public kind and current list."""
    chat_id: int
    identity: tuple
    title: str
    kind: str
    current_list: str

    def __post_init__(self):
        if (type(self.chat_id) is not int or self.chat_id == 0 or type(self.identity) is not tuple or
                not self.identity or type(self.title) is not str or not self.title or
                self.kind not in ('private', 'basic_group', 'supergroup', 'channel') or
                self.current_list not in ('main', 'archive', 'none')):
            raise ValueError('invalid archive snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int) -> 'ArchiveSnapshot':
        """Reuse the verified-target identity rules; secret chats are refused before this point."""
        from .verified_targets import TargetGuardFailure, _identity
        try:
            identity, metadata = _identity(chat, chat_id)
        except TargetGuardFailure:
            raise ValueError('archive chat is unavailable') from None
        return cls(chat_id, identity, metadata.title, metadata.chat_type, current_list(chat.get('positions')))


def archive_digest(chat_id: int, kind: str, current: str, target: str) -> str:
    """Draft-side binding of the dialog to the reviewed move; not a provider fact."""
    return digest({'domain': 'telegram-search-mcp.archive-draft', 'chat_id': chat_id, 'kind': kind,
                   'current_list': current, 'target_list': target})


class ArchiveChat(_Frozen):
    chat_id: ChatId
    title: Title
    kind: ChatKind

    @model_validator(mode='after')
    def sanitized(self):
        if sanitize_telegram_text(self.title, max_length=255) != self.title:
            raise ValueError('chat title is not sanitized evidence')
        return self


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, chat: ArchiveChat, current_list: str,
                  target_list: str, note: str | None) -> dict:
    """Bind the displayed evidence to the exact closed addChatToList request."""
    return {'domain': 'telegram-search-mcp.archive', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'chat': chat.model_dump(mode='json'), 'current_list': current_list, 'target_list': target_list,
            'note': note,
            'request': {'@type': 'addChatToList', 'chat_id': chat.chat_id,
                        'chat_list': {'@type': LIST_TYPES[target_list]}}}


class ArchiveDraftPreview(_Frozen):
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    chat: ArchiveChat
    current_list: ChatList
    target_list: ChatList
    note: Literal['Telegram may move an archived chat whose notifications are enabled back to Main '
                  'when a new message arrives'] | None = None
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        facts = preview_facts(draft_id=self.draft_id, account_id=self.account_id, expires_at=self.expires_at,
                              chat=self.chat, current_list=self.current_list, target_list=self.target_list,
                              note=self.note)
        if (self.approval_required is not True or self.current_list == self.target_list or
                (self.note is not None) != (self.target_list == 'archive') or digest(facts) != self.preview_sha256):
            raise ValueError('archive preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, chat: ArchiveChat, current_list: str,
               target_list: str) -> 'ArchiveDraftPreview':
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), chat=chat,
                      current_list=current_list, target_list=target_list,
                      note=ARCHIVE_NOTE if target_list == 'archive' else None)
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


@dataclass(frozen=True)
class ArchiveSpec:
    """Immutable internal draft payload; the registry stores it under kind 'archive'."""
    snapshot: ArchiveSnapshot
    target_list: str

    def __post_init__(self):
        if (not isinstance(self.snapshot, ArchiveSnapshot) or self.target_list not in ('main', 'archive') or
                self.snapshot.current_list not in ('main', 'archive') or
                self.snapshot.current_list == self.target_list):
            raise ValueError('invalid archive draft')

    @property
    def supersede_key(self) -> tuple:
        return ('archive', self.snapshot.chat_id)

    @property
    def sha256(self) -> str:
        return archive_digest(self.snapshot.chat_id, self.snapshot.kind, self.snapshot.current_list, self.target_list)

    @property
    def size_bytes(self) -> int:
        return 1


class PrepareChatArchiveRequest(_Closed):
    chat: ChatId
    target_list: ChatList


class PrepareChatArchiveResponse(_Closed):
    status: Literal['prepared', 'already_in_target', 'refused', 'unavailable']
    archive: ArchiveDraftPreview | None = None
    refusal: Refusal | None = None
    detail: Literal['immutable archive draft prepared; inspect full evidence and obtain fresh approval',
                    'chat is already in the target list; no draft was created',
                    'chat is not eligible; no draft was created', 'archive draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'already_in_target': ALREADY, 'refused': REFUSED,
                    'unavailable': UNAVAILABLE}[self.status]
        if (self.detail != expected or (self.status == 'prepared') != (self.archive is not None) or
                (self.status == 'refused') != (self.refusal is not None)):
            raise ValueError('invalid archive preparation response')
        return self


class ApplyPreparedArchiveRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class ApplyPreparedArchiveResponse(_Closed):
    status: ApplyStatus
    draft_id: DraftId
    chat_id: ChatId | None = None
    observed_list: ChatList | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'applied') != (self.observed_list is not None):
            raise ValueError('only an applied move carries the observed list')
        return self
