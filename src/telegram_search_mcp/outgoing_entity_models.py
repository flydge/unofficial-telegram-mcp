"""Closed caller-style records shared by text request and preview contracts."""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from .outgoing_formatting import freeze_outgoing_entities

class OutgoingEntityType(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True, serialize_by_alias=True)
    constructor: Literal['textEntityTypeBold', 'textEntityTypeItalic',
        'textEntityTypeUnderline', 'textEntityTypeStrikethrough',
        'textEntityTypeSpoiler'] = Field(alias='@type')

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault('by_alias', True)
        return super().model_dump(*args, **kwargs)

class OutgoingEntity(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, frozen=True, serialize_by_alias=True)
    offset: Annotated[int, Field(strict=True, ge=0, lt=2**31)]
    length: Annotated[int, Field(strict=True, gt=0, lt=2**31)]
    type: OutgoingEntityType

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault('by_alias', True)
        return super().model_dump(*args, **kwargs)

OutgoingEntityList = Annotated[list[OutgoingEntity], Field(strict=True, max_length=32)]

def caller_outgoing_entities(entities: list[OutgoingEntity]) -> list[dict]:
    return [entity.model_dump(mode='json', by_alias=True) for entity in entities]

def canonical_outgoing_entities(final_text: str, entities: list[OutgoingEntity]) -> list[OutgoingEntity]:
    frozen = freeze_outgoing_entities(final_text, caller_outgoing_entities(entities))
    return [OutgoingEntity(offset=offset, length=length,
        type={'@type': kind}) for offset, length, kind in frozen]


def canonical_caption_entities(caption: str, entities: list[OutgoingEntity]) -> list[OutgoingEntity]:
    """Caller styles over a send's caption (0.58), which must already be the text Telegram stores."""
    from .outgoing_drafts import styled_caption_entities
    frozen = styled_caption_entities(caption, caller_outgoing_entities(entities))
    return [OutgoingEntity(offset=offset, length=length,
        type={'@type': kind}) for offset, length, kind in frozen]
