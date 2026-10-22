"""Strict public alternatives shared without importing reader schemas."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from .next_actions import NextAction, NextActionHint, next_action_field

SelectionId = Annotated[str, Field(strict=True, pattern=r'^selection_[0-9a-f]{64}$', min_length=74, max_length=74)]


class SelectionRefusal(NextActionHint):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    status: Literal['selection_unavailable']='selection_unavailable'
    detail: Literal['Selection is unavailable; select the source again.']='Selection is unavailable; select the source again.'
    next_action: NextAction | None = next_action_field()


class SelectedInput(BaseModel):
    """Exactly one raw source or retained selection per source_pairs rule. The rule is checked here, at
    runtime, and stated in the tool description; the published input schema stays a plain object, since a
    top-level allOf/oneOf is something some MCP clients mishandle."""
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)

    @model_validator(mode='after')
    def exactly_one_source(self):
        for raw,selected in source_pairs(type(self)):
            if (getattr(self,selected) is None)==(getattr(self,raw) is None):
                raise ValueError('provide exactly one raw source or retained selection')
        return self

    @model_serializer(mode='wrap')
    def omit_absent_sources(self,handler):
        result=handler(self)
        for key in ('artifact_id','anchor','anchors','selection_id','reply_to','reply_selection_id'):
            if result.get(key) is None:result.pop(key,None)
        return result


def source_pairs(model):
    """The (raw, selected) exactly-one source rules of a selected request model, in a fixed order."""
    fields=model.model_fields
    pairs=[]
    if 'selection_id' in fields:
        pairs.append((next(key for key in ('artifact_id','anchors','anchor') if key in fields),'selection_id'))
    if 'reply_selection_id' in fields:pairs.append(('reply_to','reply_selection_id'))
    return tuple(pairs)
