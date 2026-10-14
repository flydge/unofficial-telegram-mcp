"""Strict public alternatives shared without importing reader schemas."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer

SelectionId = Annotated[str, Field(strict=True, pattern=r'^selection_[0-9a-f]{64}$', min_length=74, max_length=74)]


class SelectionRefusal(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    status: Literal['selection_unavailable']='selection_unavailable'
    detail: Literal['Selection is unavailable; select the source again.']='Selection is unavailable; select the source again.'


class SelectedInput(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema=handler(core_schema)
        if 'selection_id' in cls.model_fields:
            raw=next(key for key in ('artifact_id','anchors','anchor') if key in cls.model_fields)
            source_schema(schema,raw)
        if 'reply_selection_id' in cls.model_fields:source_schema(schema,'reply_to','reply_selection_id')
        return schema

    @model_validator(mode='after')
    def exactly_one_source(self):
        for selected,raw in [('selection_id',next((key for key in ('artifact_id','anchors','anchor') if key in type(self).model_fields),None)),
                             ('reply_selection_id','reply_to')]:
            if selected in type(self).model_fields:
                if (getattr(self,selected) is None)==(getattr(self,raw) is None):
                    raise ValueError('provide exactly one raw source or retained selection')
        return self

    @model_serializer(mode='wrap')
    def omit_absent_sources(self,handler):
        result=handler(self)
        for key in ('artifact_id','anchor','anchors','selection_id','reply_to','reply_selection_id'):
            if result.get(key) is None:result.pop(key,None)
        return result


def source_schema(schema, raw, selected='selection_id'):
    """JSON-schema equivalent of the strict runtime alternative."""
    schema.setdefault('allOf',[]).append({'oneOf':[
        {'required':[raw], 'properties':{raw:{'not':{'type':'null'}},selected:{'type':'null'}}},
        {'required':[selected], 'properties':{selected:{'not':{'type':'null'}},raw:{'type':'null'}}}]})
