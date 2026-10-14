"""Closed capture forms and content-free refusal for exact retained sources."""
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .schemas import ArtifactId, SelectedMessageAnchors, SelectedMessageAnchor, ReadMessagesResponse, TargetHandle, TargetMessageIds
from .selection_refs import SelectionId, SelectionRefusal


class _Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)


class MessagesSource(_Closed):
    kind: Literal['messages']
    anchors: SelectedMessageAnchors

    @field_validator('anchors',mode='before')
    @classmethod
    def array(cls,value):
        if type(value) is not list:raise ValueError('anchors must be an array')
        return value

    @model_validator(mode='after')
    def unique(self):
        if len({(a.chat_id,a.message_id) for a in self.anchors})!=len(self.anchors):raise ValueError('duplicate anchors')
        return self


class TargetMessagesSource(_Closed):
    kind: Literal['target_messages']
    target_handle: TargetHandle
    message_ids: TargetMessageIds

    @field_validator('message_ids',mode='before')
    @classmethod
    def array(cls,value):return MessagesSource.array(value)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.message_ids))!=len(self.message_ids):raise ValueError('duplicate message IDs')
        return self


class FileSource(_Closed):
    kind: Literal['file']
    artifact_id: ArtifactId


RetainSelectionSource=Annotated[MessagesSource|TargetMessagesSource|FileSource,Field(discriminator='kind')]


class RetainSelectionRequest(_Closed):
    source: RetainSelectionSource


class RetainedFile(_Closed):
    artifact_id: ArtifactId
    sha256: Annotated[str,Field(strict=True,pattern=r'^[0-9a-f]{64}$')]
    size_bytes: Annotated[int,Field(strict=True,ge=0,le=256*1024*1024)]
    file_name: Annotated[str,Field(strict=True,max_length=255)]|None=None
    mime_type: Annotated[str,Field(strict=True,max_length=127)]|None=None
    media_type: Literal['document','photo','audio','voice_note','video','video_note']
    anchor: SelectedMessageAnchor|None=None
    provenance: Literal['telegram','local','derived']


class RetainedSelection(_Closed):
    status: Literal['retained']='retained'
    selection_id: SelectionId
    kind: Literal['messages','file']
    expires_at: datetime
    messages: ReadMessagesResponse|None=None
    file: RetainedFile|None=None

    @field_validator('expires_at')
    @classmethod
    def aware(cls,value):
        if value.tzinfo is None or value.utcoffset() is None:raise ValueError('timezone required')
        return value

    @model_validator(mode='after')
    def shape(self):
        if ((self.kind=='messages')!=(self.messages is not None) or
                (self.kind=='file')!=(self.file is not None)):raise ValueError('retained kind differs')
        if self.messages is not None and any(row.message is None for row in self.messages.results):raise ValueError('capture must have all messages')
        return self


RetainSelectionResponse=RetainedSelection|SelectionRefusal
