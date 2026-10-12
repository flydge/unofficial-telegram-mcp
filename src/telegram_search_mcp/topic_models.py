"""Closed immutable topic evidence and complete ordinary-text preview binding."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .sanitize import sanitize_telegram_text

MARKER = '[untrusted Telegram evidence] '
PositiveId = Annotated[int, Field(strict=True, gt=0, lt=2**53)]
ChatId = Annotated[int, Field(strict=True, gt=-(2**53), lt=2**53)]
TopicId = Annotated[int, Field(strict=True, ge=1, le=2**31-1)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]


def _digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


class ForumTopicReference(BaseModel):
    # Public read selectors retain their existing mutable request semantics.
    # Send authority is copied into the immutable TopicSnapshot below.
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    kind: Literal['forum'] = 'forum'
    id: TopicId


class TopicSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True)
    version: Literal[1] = 1
    account_id: PositiveId
    chat_id: ChatId
    chat_type: Literal['forum_supergroup', 'topics_bot']
    peer_id: PositiveId
    forum_topic_id: TopicId
    name: Annotated[str, Field(strict=True, min_length=len(MARKER)+1,
                               max_length=len(MARKER)+256)]
    original_name_sha256: Hash
    creation_date: Annotated[int, Field(strict=True, ge=1, le=2**31-1)]
    creator_kind: Literal['user', 'chat']
    creator_id: ChatId
    is_outgoing: Annotated[bool, Field(strict=True)]
    is_general: Annotated[bool, Field(strict=True)]
    is_closed: Annotated[bool, Field(strict=True)]
    is_hidden: Annotated[bool, Field(strict=True)]
    source_sha256: Hash

    @field_validator('version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('topic evidence version is invalid')
        return value

    @model_validator(mode='after')
    def exact_snapshot(self):
        if (self.chat_id == 0 or self.creator_id == 0 or
                (self.creator_kind == 'user' and self.creator_id < 1) or
                type(self.version) is not int or self.is_closed or self.is_hidden):
            raise ValueError('topic evidence is unavailable')
        if (not self.name.startswith(MARKER) or
                sanitize_telegram_text(self.name[len(MARKER):], max_length=256) != self.name[len(MARKER):]):
            raise ValueError('topic name evidence is invalid')
        if _digest(self.model_dump(exclude={'source_sha256'})) != self.source_sha256:
            raise ValueError('topic evidence digest disagrees')
        return self


def snapshot_from_provider(account_id: int, chat_id: int, topic_id: int,
                           chat: object, raw: object) -> TopicSnapshot:
    """Project only after resolve_forum_chat verified the current native capability."""
    if (type(chat) is not dict or chat.get('@type') != 'chat' or
            type(chat.get('id')) is not int or chat['id'] != chat_id or
            type(chat.get('type')) is not dict or type(raw) is not dict or
            raw.get('@type') != 'forumTopic' or type(raw.get('info')) is not dict):
        raise ValueError('topic evidence is unavailable')
    kind = chat['type']
    if kind.get('@type') == 'chatTypeSupergroup' and kind.get('is_channel') is False:
        chat_type, peer_id = 'forum_supergroup', kind.get('supergroup_id')
    elif kind.get('@type') == 'chatTypePrivate':
        chat_type, peer_id = 'topics_bot', kind.get('user_id')
    else:
        raise ValueError('topic chat type is unavailable')
    info = raw['info']
    if (info.get('@type') != 'forumTopicInfo' or type(info.get('chat_id')) is not int or
            info['chat_id'] != chat_id or type(info.get('forum_topic_id')) is not int or
            info['forum_topic_id'] != topic_id):
        raise ValueError('topic identity disagrees')
    name = info.get('name')
    if type(name) is not str or not 1 <= len(name) <= 256 or len(name.encode('utf-8')) > 1024:
        raise ValueError('topic name is incomplete')
    normalized = sanitize_telegram_text(name, max_length=4096)
    if not normalized or len(normalized) > 256:
        raise ValueError('topic name is incomplete')
    creator = info.get('creator_id')
    if type(creator) is not dict:
        raise ValueError('topic creator is unavailable')
    if set(creator) == {'@type', 'user_id'} and creator['@type'] == 'messageSenderUser':
        creator_kind, creator_id = 'user', creator['user_id']
    elif set(creator) == {'@type', 'chat_id'} and creator['@type'] == 'messageSenderChat':
        creator_kind, creator_id = 'chat', creator['chat_id']
    else:
        raise ValueError('topic creator is unavailable')
    value = dict(version=1, account_id=account_id, chat_id=chat_id,
        chat_type=chat_type, peer_id=peer_id, forum_topic_id=topic_id,
        name=MARKER+normalized, original_name_sha256=hashlib.sha256(name.encode('utf-8')).hexdigest(),
        creation_date=info.get('creation_date'), creator_kind=creator_kind, creator_id=creator_id,
        **{key: info.get(key) for key in ('is_outgoing', 'is_general', 'is_closed', 'is_hidden')})
    return TopicSnapshot(**value, source_sha256=_digest(value))


def topic_preview_digest(*, draft_id: str, account_id: int, recipient: int,
                         recipient_title: str, expires_at: datetime | str,
                         text: str, sha256: str, size_bytes: int,
                         topic: TopicSnapshot | None = None,
                         outgoing_entities: list[dict] | None = None) -> str:
    # Lazy import keeps schemas/draft models acyclic; these are the existing
    # conservative wire constants, so reviewed options cannot silently diverge.
    from .reply_drafts import LINK_OPTIONS, SEND_OPTIONS
    if type(expires_at) is str:
        expires_at = datetime.fromisoformat(expires_at)
    if (not isinstance(expires_at, datetime) or expires_at.tzinfo is None or
            type(draft_id) is not str or re.fullmatch(r'draft_[0-9a-f]{32}', draft_id) is None or
            type(account_id) is not int or type(recipient) is not int or
            type(recipient_title) is not str or not 1 <= len(recipient_title) <= 255 or
            type(size_bytes) is not int or type(sha256) is not str or
            (topic is not None and (not isinstance(topic, TopicSnapshot) or
                topic.account_id != account_id or topic.chat_id != recipient)) or
            type(text) is not str or not 1 <= len(text) <= 4096 or len(text.encode('utf-8')) != size_bytes or
            hashlib.sha256(text.encode('utf-8')).hexdigest() != sha256):
        raise ValueError('topic preview facts disagree')
    if outgoing_entities is None:
        outgoing_entities = []
    if type(outgoing_entities) is not list:
        raise ValueError('outgoing entities require a list')
    entities = []
    if outgoing_entities:
        from .outgoing_drafts import _safe_message_text
        from .outgoing_formatting import freeze_outgoing_entities, native_outgoing_entities
        if _safe_message_text(text) != text:
            raise ValueError('preview text must be final normalized text')
        entities = native_outgoing_entities(freeze_outgoing_entities(text, outgoing_entities))
    elif topic is None:
        raise ValueError('text approval digest requires topic or outgoing styles')
    return _digest({'version': 2 if entities else 1, 'draft_id': draft_id, 'account_id': account_id,
        'recipient': recipient, 'recipient_title': recipient_title,
        'expires_at': expires_at.astimezone(timezone.utc).isoformat(),
        'kind': 'text', 'approval_required': True, 'topic': topic.model_dump() if topic is not None else None,
        'text': text, 'sha256': sha256, 'size_bytes': size_bytes,
        'entities': entities, 'reply_to': None, 'link_preview_options': LINK_OPTIONS,
        'send_options': SEND_OPTIONS, 'reply_markup': None, 'clear_draft': False})
