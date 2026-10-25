"""Closed, immutable evidence for bounded same-chat reply sources."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, get_args

from pydantic import ConfigDict, Field, field_validator, model_validator, model_serializer
from .draft_models import _Closed, AccountId, DraftId, DraftPreview, DraftText, Hash
from .schemas import ArtifactId, ChatId, SelectedMessageAnchor
from .topic_models import ForumTopicReference, TopicId, TopicSnapshot
from .outgoing_entity_models import OutgoingEntityList, canonical_outgoing_entities, caller_outgoing_entities
from .outgoing_formatting import freeze_outgoing_entities, native_outgoing_entities
from .reply_source_ladder import (PLAIN_TEXT_VERSION, admission_for, capabilities_for, is_media_version,
    is_text_version, kind_admission, present, version_for)
from .sanitize import sanitize_telegram_text

EVIDENCE_MARKER = '[untrusted Telegram evidence] '
UNAVAILABLE = 'reply draft is unavailable'
CAPABILITY_DISABLED = 'reply needs capabilities the owner has not enabled: missing_capabilities'
PREPARED = 'immutable reply draft prepared; inspect full evidence and obtain fresh approval'
PENDING = 'exact pending reply snapshot; inspection is not approval'
REVISED = 'new immutable reply revision; prior draft invalidated; fresh approval required'

# sending_id is random transport correlation, not a user-significant send option.
SEND_OPTIONS = {'@type':'messageSendOptions','suggested_post_info':None,
    'disable_notification':False,'from_background':False,'protect_content':False,
    'allow_paid_broadcast':False,'paid_message_star_count':0,
    'update_order_of_installed_sticker_sets':False,'scheduling_state':None,
    'effect_id':0,'only_preview':False}
LINK_OPTIONS = {'@type':'linkPreviewOptions','is_disabled':True,'url':'',
    'force_small_media':False,'force_large_media':False,'show_above_text':False}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def strict_shape(value: object, expected: dict) -> bool:
    return (isinstance(value, dict) and set(value) == set(expected) and
            all(type(value[k]) is type(v) and value[k] == v for k,v in expected.items()))


def integer(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and -(2**53) < value < 2**53 and (value > 0 if positive else value != 0)


def _retained_forum_topic_id(value: object) -> int | None:
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {'@type', 'forum_topic_id'} or
            type(value['@type']) is not str or value['@type'] != 'messageTopicForum' or
            type(value['forum_topic_id']) is not int or not 0 < value['forum_topic_id'] < 2**31):
        raise ValueError('reply source topic is unavailable')
    return value['forum_topic_id']


@dataclass(frozen=True)
class ReplySource:
    """Canonical serialized projection prevents nested mutable provider state escaping."""
    projection_json: str

    def __post_init__(self):
        self.target()

    def _validate_projection(self):
        if type(self.projection_json) is not str:
            raise ValueError('invalid reply projection')
        if len(self.projection_json)>65536 or len(self.projection_json.encode('utf-8'))>65536:
            raise ValueError('invalid reply projection')
        try:
            value=json.loads(self.projection_json)
        except RecursionError as error:
            raise ValueError('invalid reply projection') from error
        if (type(value) is not dict or set(value)!={'version','anchor','message'} or
                self.projection_json!=canonical(value) or type(value['version']) is not int or
                type(value['anchor']) is not dict or set(value['anchor'])!={'chat_id','message_id'} or
                type(value['message']) is not dict):
            raise ValueError('invalid reply projection')
        topic_id = _retained_forum_topic_id(value['message'].get('topic_id'))
        if is_media_version(value['version']):
            from .reply_media_sources import validate_content_for
            content=value['message'].get('content')
            validate_content_for(content,admission_for(value['version']))
            # Authority comes only from the complete validated caption: it must select the stored version.
            if version_for(present(content['caption']['entities']),media=True)!=value['version']:
                raise ValueError('reply caption version disagrees with its entities')
            surrogate={**value['message'],'content':{'@type':'messageText','text':{'@type':'formattedText','text':'validation','entities':[]}}}
            shell=_projection_from_message(surrogate,value['anchor']['chat_id'],value['anchor']['message_id'],
                expected_forum_topic_id=topic_id)['message']
            shell['content']=value['message']['content']
            if canonical(shell)!=canonical(value['message']):
                raise ValueError('invalid reply projection')
        elif is_text_version(value['version']):
            rebuilt=_projection_from_message(value['message'],value['anchor']['chat_id'],value['anchor']['message_id'],
                expected_forum_topic_id=topic_id)
            if canonical(rebuilt)!=self.projection_json:
                raise ValueError('invalid reply projection')
        else:
            raise ValueError('invalid reply projection')
        return value

    @classmethod
    def _validated(cls, value):
        result = object.__new__(cls)
        object.__setattr__(result, 'projection_json', canonical(value))
        return result

    @property
    def anchor(self) -> tuple[int, int]:
        value=json.loads(self.projection_json)['anchor']
        return value['chat_id'], value['message_id']

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.projection_json.encode('utf-8')).hexdigest()

    @property
    def is_media(self) -> bool:
        return is_media_version(json.loads(self.projection_json)['version'])

    @property
    def forum_topic_id(self) -> int | None:
        value = self._validate_projection()
        return _retained_forum_topic_id(value['message'].get('topic_id'))

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        capabilities = self._content_required_capabilities
        return (*capabilities, 'topic_text_send') if self.forum_topic_id is not None else capabilities

    @property
    def _content_required_capabilities(self) -> tuple[str, ...]:
        value=self._validate_projection()
        media=is_media_version(value['version'])
        entities=value['message']['content']['caption' if media else 'text']['entities']
        return capabilities_for(present(entities),media=media)

    @property
    def required_capability(self) -> str | None:
        capabilities=self.required_capabilities
        if len(capabilities)>1:
            raise ValueError('reply source requires multiple capabilities')
        return capabilities[0] if capabilities else None

    @property
    def raw_text(self) -> str:
        return json.loads(self.projection_json)['message']['content']['text']['text']

    def target(self) -> 'ReplyTarget':
        value=self._validate_projection()
        topic_id = _retained_forum_topic_id(value['message'].get('topic_id'))
        if value['version']!=PLAIN_TEXT_VERSION:
            # A stored version renders with its top family and every family below it, never with authority.
            if is_media_version(value['version']):
                from .reply_media_sources import render_content_for
            else:
                from .reply_formatted_sources import render_content_for
            display,changed=render_content_for(value['message']['content'],EVIDENCE_MARKER,admission_for(value['version']))
            return ReplyTarget(anchor=SelectedMessageAnchor(chat_id=self.anchor[0],message_id=self.anchor[1]),
                text=display,source_sha256=self.source_sha256,sanitized=changed,truncated=False,forum_topic_id=topic_id)
        raw=self.raw_text
        # No truncation: expansion outside the full preview bound fails closed.
        safe=sanitize_telegram_text(raw, max_length=4096*18+1)
        if len(safe)>4096:
            raise ValueError('reply evidence exceeds preview bound')
        return ReplyTarget(anchor=SelectedMessageAnchor(chat_id=self.anchor[0],message_id=self.anchor[1]),
            text=EVIDENCE_MARKER+safe,source_sha256=self.source_sha256,sanitized=safe!=raw,truncated=False,forum_topic_id=topic_id)


def _projection_from_message(raw: object, chat_id: int, message_id: int, *,
                             expected_forum_topic_id: int | None = None) -> dict:
    """Validate only the immutable relevant subset, never volatile whole-message hashes.

    TDLib omits nullable object fields in JSON. Required scalar safety fields must
    still be present and correctly typed; absence never becomes a safe zero.
    """
    if not integer(chat_id) or not integer(message_id,positive=True) or not isinstance(raw,dict):
        raise ValueError('reply source is unavailable')
    if (raw.get('@type')!='message' or type(raw.get('chat_id')) is not int or raw['chat_id']!=chat_id or
        type(raw.get('id')) is not int or raw['id']!=message_id or type(raw.get('is_outgoing')) is not bool):
        raise ValueError('reply source is unavailable')
    sender=raw.get('sender_id')
    if not isinstance(sender,dict) or type(sender.get('@type')) is not str:raise ValueError('reply source is unavailable')
    sender_key={'messageSenderUser':'user_id','messageSenderChat':'chat_id'}.get(sender.get('@type'))
    if (sender_key is None or set(sender)!= {'@type',sender_key} or
        not integer(sender.get(sender_key),positive=sender_key=='user_id')):
        raise ValueError('reply source is unavailable')
    for key in ('date','edit_date'):
        if type(raw.get(key)) is not int or not 0 <= raw[key] < 2**31:
            raise ValueError('reply source is unavailable')
    for key in ('self_destruct_in','auto_delete_in'):
        if type(raw.get(key)) not in {int,float} or raw[key]!=0:
            raise ValueError('reply source is unavailable')
    nulls=('sending_state','scheduling_state','topic_id','self_destruct_type','ephemeral_content',
           'receiver_id','reply_to','forward_info','import_info','reply_markup')
    if any(raw.get(key) is not None for key in nulls if key != 'topic_id'):
        raise ValueError('reply source is unavailable')
    topic_id = _retained_forum_topic_id(raw.get('topic_id'))
    if expected_forum_topic_id is None:
        if topic_id is not None:
            raise ValueError('reply source topic was not selected')
    elif (type(expected_forum_topic_id) is not int or not 0 < expected_forum_topic_id < 2**31 or
            topic_id != expected_forum_topic_id):
        raise ValueError('reply source topic differs from selection')
    if (type(raw.get('ephemeral_message_id')) is not int or raw['ephemeral_message_id']!=0 or
        type(raw.get('is_from_offline')) is not bool):
        raise ValueError('reply source is unavailable')
    content=raw.get('content')
    text='';link_options=None;entities=[]
    media=type(content) is dict and content.get('@type') in ('messageDocument','messagePhoto','messageVoiceNote')
    if media:
        from .reply_media_sources import media_content_for
        projected_content=media_content_for(content,kind_admission(media=True))
        entities=projected_content['caption']['entities']
    else:
        if (not isinstance(content,dict) or content.get('@type')!='messageText' or content.get('link_preview') is not None or
            set(content)-{'@type','text','link_preview','link_preview_options'}):
            raise ValueError('reply source is unavailable')
        link_options=content.get('link_preview_options')
        if link_options is not None and not strict_shape(link_options,LINK_OPTIONS):raise ValueError('reply source is unavailable')
        formatted=content.get('text')
        if (not isinstance(formatted,dict) or set(formatted)!= {'@type','text','entities'} or
            formatted.get('@type')!='formattedText' or type(formatted.get('text')) is not str or type(formatted.get('entities')) is not list):
            raise ValueError('reply source is unavailable')
        if formatted['entities']:
            if len(formatted['entities'])>32:
                raise ValueError('invalid formatted reply source')
            from .reply_formatted_sources import formatted_text_for
            entities=formatted_text_for(formatted,kind_admission(media=False))['entities']
        text=formatted['text']
        if (not 0<len(text)<=4096 or not text.strip() or
            any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in text)):
            raise ValueError('reply source is unavailable')
        text.encode('utf-8')
    message={'@type':'message','chat_id':chat_id,'id':message_id,'sender_id':dict(sender),
        'is_outgoing':raw['is_outgoing'],'is_from_offline':raw['is_from_offline'],
        'ephemeral_message_id':0,'date':raw['date'],'edit_date':raw['edit_date'],
        'self_destruct_in':0.0,'auto_delete_in':0.0,**{key:None for key in nulls},
        'content':{'@type':'messageText','text':{'@type':'formattedText','text':text,'entities':entities},
                   'link_preview':None,'link_preview_options':None if link_options is None else dict(link_options)}}
    if topic_id is not None:
        message['topic_id'] = dict(raw['topic_id'])
    if media:
        message['content']=projected_content
    # The version is selected only from complete validated entities, never a raw constructor hint.
    return {'version':version_for(present(entities),media=media),
        'anchor':{'chat_id':chat_id,'message_id':message_id},'message':message}


def source_from_message(raw: object, chat_id: int, message_id: int, *,
                        expected_forum_topic_id: int | None = None) -> ReplySource:
    source=ReplySource._validated(_projection_from_message(raw,chat_id,message_id,
        expected_forum_topic_id=expected_forum_topic_id))
    source.target() # validates raw canonical bytes and full display before registry mutation
    return source


class ReplyTarget(_Closed):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True,frozen=True)
    anchor: SelectedMessageAnchor
    text: Annotated[str,Field(strict=True,min_length=1,max_length=4096+len(EVIDENCE_MARKER))]
    source_sha256: Hash
    sanitized: Annotated[bool,Field(strict=True)]
    truncated: Annotated[bool,Field(strict=True,json_schema_extra={'const':False})]=False
    forum_topic_id: TopicId | None = None

    @model_serializer(mode='wrap')
    def omit_absent_topic(self, handler):
        value = handler(self)
        if self.forum_topic_id is None:
            value.pop('forum_topic_id', None)
        return value

    @model_validator(mode='after')
    def evidence(self):
        if self.truncated or not self.text.startswith(EVIDENCE_MARKER):raise ValueError('full marked reply evidence required')
        value=self.text[len(EVIDENCE_MARKER):]
        if not value or sanitize_telegram_text(value,max_length=4097)!=value:
            raise ValueError('reply display is not sanitized full evidence')
        return self


class ReplyTextDraftPreview(_Closed):
    """Complete text content; topic and reply share the sole outer approval digest."""
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True)
    draft_id: DraftId
    account_id: AccountId
    recipient: ChatId
    recipient_title: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    kind: Literal['text']
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    topic: TopicSnapshot | None = None
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)

    sha256: Hash
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=64*1024*1024)]
    text: Annotated[str, Field(strict=True, min_length=1, max_length=4096)] | None = None
    artifact_id: ArtifactId | None = None
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=255)] | None = None
    mime_type: Annotated[str, Field(strict=True, min_length=3, max_length=127)] | None = None
    caption: Annotated[str, Field(strict=True, max_length=1024)] | None = None
    duration_seconds: Annotated[int, Field(strict=True, gt=0, le=600)] | None = None
    waveform_base64: Annotated[str, Field(strict=True, max_length=256)] | None = None
    source_sha256: Hash | None = None
    source_display_name: Annotated[str, Field(strict=True, min_length=1, max_length=255)] | None = None
    converted: Annotated[bool, Field(strict=True)] = False

    @model_serializer(mode='wrap')
    def omit_absent_topic(self, handler):
        value = handler(self)
        for key in ('reply_to','reply_selection_id'):
            if value.get(key) is None:value.pop(key,None)
        if self.topic is None:
            value.pop('topic', None)
        if not self.outgoing_entities:
            value.pop('outgoing_entities', None)
        return value

    @field_validator('expires_at', mode='before')
    @classmethod
    def expiry_input(cls, value):
        if type(value) not in {str, datetime}:
            raise ValueError('expiry must be an ISO datetime')
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError('expiry must be an ISO datetime') from None
        return value

    @field_validator('expires_at')
    @classmethod
    def expiry_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('expiry requires a timezone')
        return value.astimezone(timezone.utc)

    @field_validator('approval_required')
    @classmethod
    def requires_approval(cls, value):
        if value is not True:
            raise ValueError('draft always requires approval')
        return value

    @model_validator(mode='after')
    def exact_content(self):
        from .outgoing_drafts import _safe_message_text
        if (self.text is None or _safe_message_text(self.text) != self.text or
                any(value is not None for value in (self.artifact_id, self.display_name,
                    self.mime_type, self.caption, self.duration_seconds, self.waveform_base64,
                    self.source_sha256, self.source_display_name)) or self.converted):
            raise ValueError('reply text preview contains invalid content or artifact metadata')
        if self.outgoing_entities:
            object.__setattr__(self, 'outgoing_entities', canonical_outgoing_entities(self.text, self.outgoing_entities))
        data = self.text.encode('utf-8')
        if len(data) != self.size_bytes or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError('reply text preview facts disagree')
        if self.topic is not None and (self.topic.account_id != self.account_id or
                self.topic.chat_id != self.recipient):
            raise ValueError('reply topic identity disagrees')
        return self


def preview_facts(draft: ReplyTextDraftPreview | DraftPreview, target: ReplyTarget) -> dict:
    return {'version':3 if draft.outgoing_entities else 2 if draft.topic is not None else 1,'draft':draft.model_dump(mode='json'),'reply_target':target.model_dump(mode='json'),
            'topic_id':({'@type':'messageTopicForum','forum_topic_id':draft.topic.forum_topic_id}
                if draft.topic is not None else None),'outgoing_entities':native_outgoing_entities(freeze_outgoing_entities(draft.text,
                caller_outgoing_entities(draft.outgoing_entities))) if draft.outgoing_entities else [],'send_options':dict(SEND_OPTIONS),
            'link_preview_options':dict(LINK_OPTIONS),'clear_draft':False,'reply_markup':None,
            'reply_to':{'@type':'inputMessageReplyToMessage','message_id':target.anchor.message_id,
                        'quote':None,'checklist_task_id':0,'poll_option_id':''}}


class ReplyDraftPreview(_Closed):
    draft: ReplyTextDraftPreview
    reply_target: ReplyTarget
    preview_sha256: Hash

    @model_validator(mode='after')
    def exact(self):
        from .outgoing_drafts import _safe_message_text
        if self.draft.text is None or _safe_message_text(self.draft.text)!=self.draft.text:
            raise ValueError('reply text must be final normalized plain text')
        topic_id = self.draft.topic.forum_topic_id if self.draft.topic is not None else None
        if (self.reply_target.forum_topic_id != topic_id or self.draft.kind!='text' or
            self.draft.recipient!=self.reply_target.anchor.chat_id or
            digest(preview_facts(self.draft,self.reply_target))!=self.preview_sha256):
            raise ValueError('reply preview facts disagree')
        return self

    @classmethod
    def create(cls, draft: ReplyTextDraftPreview | DraftPreview, source: ReplySource):
        if isinstance(draft, DraftPreview):
            # Preserve ordinary full wire; do not strip a topic-only inner digest.
            draft = ReplyTextDraftPreview.model_validate(draft.model_dump(mode='json'))
        target=source.target()
        return cls(draft=draft,reply_target=target,preview_sha256=digest(preview_facts(draft,target)))


from .selection_refs import SelectedInput, SelectionId
from .contract import CapabilityName
from .next_actions import NextAction, NextActionHint, next_action_field, optional_field

class PrepareReplyTextSendRequest(SelectedInput):
    recipient: ChatId
    text: DraftText
    reply_to: SelectedMessageAnchor | None = None
    reply_selection_id: SelectionId | None = None
    topic: ForumTopicReference | None = None
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)

    @model_serializer(mode='wrap')
    def omit_absent_topic(self, handler):
        value = handler(self)
        for key in ('reply_to','reply_selection_id'):
            if getattr(self,key) is None:value.pop(key,None)
        if self.topic is None:
            value.pop('topic', None)
        if not self.outgoing_entities:
            value.pop('outgoing_entities', None)
        return value

    @model_validator(mode='after')
    def exact_outgoing_entities(self):
        if self.outgoing_entities:
            from .outgoing_drafts import _safe_message_text
            self.outgoing_entities = canonical_outgoing_entities(_safe_message_text(self.text), self.outgoing_entities)
        return self

class GetReplyDraftRequest(_Closed):
    draft_id: DraftId

class UpdateReplyDraftRequest(GetReplyDraftRequest):
    text: DraftText
    outgoing_entities: OutgoingEntityList = Field(default_factory=list)

    @model_validator(mode='after')
    def exact_outgoing_entities(self):
        if self.outgoing_entities:
            from .outgoing_drafts import _safe_message_text
            self.outgoing_entities = canonical_outgoing_entities(_safe_message_text(self.text), self.outgoing_entities)
        return self

    @model_serializer(mode='wrap')
    def omit_empty_entities(self, handler):
        value = handler(self)
        if not self.outgoing_entities:
            value.pop('outgoing_entities', None)
        return value

class RefreshReplyDraftRequest(GetReplyDraftRequest):
    pass

MissingCapabilities = Annotated[list[CapabilityName], Field(min_length=1, max_length=len(get_args(CapabilityName)),
    description='Only the capabilities the refused check needs and the policy lacks, in requirement order.')]


class ReplyRefusal(NextActionHint, _Closed):
    """The refusal half of every reply result: `unavailable` reveals nothing; `capability_disabled`
    names only the missing capabilities, which come from the closed set, never from input."""

    def refused(self) -> bool:
        if self.status == 'capability_disabled':
            names = self.missing_capabilities
            return self.detail == CAPABILITY_DISABLED and names is not None and len(set(names)) == len(names)
        return self.status == 'unavailable' and self.detail == UNAVAILABLE and self.missing_capabilities is None


class PrepareReplyTextSendResponse(ReplyRefusal):
    status: Literal['prepared','unavailable','capability_disabled']
    reply: ReplyDraftPreview | None=None
    detail: Literal['immutable reply draft prepared; inspect full evidence and obtain fresh approval','reply draft is unavailable',
        'reply needs capabilities the owner has not enabled: missing_capabilities']
    missing_capabilities: MissingCapabilities | None = optional_field()
    next_action: NextAction | None = next_action_field()
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='prepared')!=(self.reply is not None) or not (
                self.detail==PREPARED and self.missing_capabilities is None if self.status=='prepared' else self.refused()):
            raise ValueError('invalid reply preparation response')
        return self

class GetReplyDraftResponse(ReplyRefusal):
    status: Literal['pending','unavailable','capability_disabled']
    reply: ReplyDraftPreview | None=None
    detail: Literal['exact pending reply snapshot; inspection is not approval','reply draft is unavailable',
        'reply needs capabilities the owner has not enabled: missing_capabilities']
    missing_capabilities: MissingCapabilities | None = optional_field()
    next_action: NextAction | None = next_action_field()
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='pending')!=(self.reply is not None) or not (
                self.detail==PENDING and self.missing_capabilities is None if self.status=='pending' else self.refused()):
            raise ValueError('invalid reply inspection response')
        return self

class ReviseReplyDraftResponse(ReplyRefusal):
    status: Literal['revised','unavailable','capability_disabled']
    previous_draft_id: DraftId | None=None
    reply: ReplyDraftPreview | None=None
    detail: Literal['new immutable reply revision; prior draft invalidated; fresh approval required','reply draft is unavailable',
        'reply needs capabilities the owner has not enabled: missing_capabilities']
    missing_capabilities: MissingCapabilities | None = optional_field()
    next_action: NextAction | None = next_action_field()
    @model_validator(mode='after')
    def shape(self):
        if self.status=='revised':
            if (self.reply is None or self.previous_draft_id is None or self.reply.draft.draft_id==self.previous_draft_id
                    or self.detail!=REVISED or self.missing_capabilities is not None):
                raise ValueError('invalid reply revision response')
        elif self.reply is not None or self.previous_draft_id is not None or not self.refused():
            raise ValueError('unavailable reply revision contains evidence')
        return self
