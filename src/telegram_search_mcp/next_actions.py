"""Closed recovery hints (0.54): one map from a condition to the next step a client should take.

A response never chooses its hint: `next_action` is derived from the response's own closed status
fields (status, then a closed `stop_reason`), so existing constructors need no change and a
contradicting value fails validation. The field is omitted when no condition applies, so every exact
dump of an unaffected status stays byte-identical. Guard refusals, which have no structured content,
carry the same values as a fixed text suffix.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import ClassVar, Literal, Mapping, get_args, get_origin

from pydantic import BaseModel, Field, model_validator

NextAction = Literal['check_connection', 'connect_account', 'update_installation', 'enable_capability',
                     'search_again', 'restart_listing', 'check_send_status']

# The one closed map: condition -> next_action (spec 0.54, decisions of 21.10).
NEXT_ACTIONS: Mapping[str, NextAction] = MappingProxyType({
    'broker_unavailable': 'check_connection',
    'authorization_not_ready': 'connect_account',
    'config_stale': 'update_installation',
    'capability_disabled': 'enable_capability',
    'selection_unavailable': 'search_again',
    'cursor_invalid_or_expired': 'restart_listing',
    'outcome_unknown': 'check_send_status',  # never resend
})

# Status values with one meaning on every response that has them.
STATUS_CONDITIONS: Mapping[str, str] = MappingProxyType({
    'blocked': 'authorization_not_ready',
    'invalid_cursor': 'cursor_invalid_or_expired',
    'outcome_unknown': 'outcome_unknown',
    'capability_disabled': 'capability_disabled',
    'selection_unavailable': 'selection_unavailable',
})
# Closed stop reasons, consulted only when the status itself names no condition.
STOP_REASON_CONDITIONS: Mapping[str, str] = MappingProxyType({
    'broker_unavailable': 'broker_unavailable',
    'authorization_unavailable': 'authorization_not_ready',
    'invalid_cursor': 'cursor_invalid_or_expired',
})
# Guard ToolError codes that carry a hint, as the fixed suffix of their text.
GUARD_NEXT_ACTIONS: Mapping[str, NextAction] = MappingProxyType(
    {code: NEXT_ACTIONS[code] for code in ('capability_disabled', 'config_stale')})


def guard_suffix(code: str) -> str:
    return f'; next_action: {GUARD_NEXT_ACTIONS[code]}' if code in GUARD_NEXT_ACTIONS else ''


def _absent(value: object) -> bool:
    return value is None


def optional_field(**kwargs):
    """An optional field omitted from dumps while unset, so pinned exact dumps stay unchanged."""
    return Field(default=None, exclude_if=_absent, **kwargs)


def next_action_field():
    """The derived field; declared last on each response so dumps keep their key order."""
    return optional_field(description='Recovery step derived from the status; absent when none applies.')


class NextActionHint(BaseModel):
    """Derives `next_action` from the closed status fields of the response that mixes it in.

    `NEXT_ACTION_STATUSES` maps this response's status values to conditions; it defaults to every
    `STATUS_CONDITIONS` entry the status Literal has. A response may add a status whose meaning is its
    own (a discovery cursor's `expired`). `NEXT_ACTION_STOP_REASONS` maps a closed `stop_reason`.
    """
    NEXT_ACTION_STATUSES: ClassVar[Mapping[str, str]] = MappingProxyType({})
    NEXT_ACTION_STOP_REASONS: ClassVar[Mapping[str, str]] = MappingProxyType({})
    EXTRA_NEXT_ACTION_STATUSES: ClassVar[Mapping[str, str]] = MappingProxyType({})

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        super().__pydantic_init_subclass__(**kwargs)
        field = cls.model_fields.get('status')
        if field is None or 'next_action' not in cls.model_fields:
            return
        statuses = set(_literal_values(field.annotation))
        cls.NEXT_ACTION_STATUSES = MappingProxyType({
            **{status: condition for status, condition in STATUS_CONDITIONS.items() if status in statuses},
            **cls.EXTRA_NEXT_ACTION_STATUSES})
        reason = cls.model_fields.get('stop_reason')
        if reason is not None:
            reasons = set(_literal_values(reason.annotation))
            cls.NEXT_ACTION_STOP_REASONS = MappingProxyType(
                {value: condition for value, condition in STOP_REASON_CONDITIONS.items() if value in reasons})

    def next_action_condition(self) -> str | None:
        condition = type(self).NEXT_ACTION_STATUSES.get(self.status)
        if condition is None:
            condition = type(self).NEXT_ACTION_STOP_REASONS.get(getattr(self, 'stop_reason', None))
        return condition

    @model_validator(mode='after')
    def derived_next_action(self):
        condition = self.next_action_condition()
        expected = None if condition is None else NEXT_ACTIONS[condition]
        if self.next_action is None:
            self.next_action = expected
        elif self.next_action != expected:
            raise ValueError('next_action must follow the response status')
        return self


def _literal_values(annotation) -> tuple[object, ...]:
    """Every value of a Literal, a union of Literals, or an Annotated/optional wrapper of them."""
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    return tuple(value for arg in get_args(annotation) for value in _literal_values(arg))
