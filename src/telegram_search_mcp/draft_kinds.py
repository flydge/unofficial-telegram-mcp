"""The one table of outgoing draft kinds (0.57): what each kind is, which tools may touch it, and its status rule.

Every kind is one row. `change`: the draft changes provider state through a dedicated apply tool, and its receipt
may be `applied`. `unbacked`: the draft is never backed by an artifact. `dedicated`: only the kind's own tools see
it; the generic draft tools never list, show, revise or cancel it, and the generic send tools refuse it.
`status`: the capability a status read and a receipt replay of the kind stay bound to (`required_capabilities` of
it), None for the generic send rules, or REFUSED for a kind with its own status model that generic status never
reads. A change kind also names its payload class (`spec`, stored in the OutgoingDraft field of the kind's name)
and its fixed display name; a payload may name more capabilities its status and replay need
(`extra_capabilities`, e.g. edit_own_styled_text for a styled caption). Adding a kind is adding a row; nothing dispatches on kind names elsewhere.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import MappingProxyType

REFUSED = 'refused'


@dataclass(frozen=True)
class DraftKind:
    name: str
    change: bool = False
    unbacked: bool = False
    dedicated: bool = False
    status: str | None = None
    spec: str | None = None  # "module:Class" of a change kind's payload, relative to this package
    display_name: str | None = None

    def __post_init__(self):
        if (type(self.name) is not str or not self.name or
                any(type(getattr(self, flag)) is not bool for flag in ('change', 'unbacked', 'dedicated')) or
                (self.status is not None and (type(self.status) is not str or not self.status)) or
                # A change is never artifact-backed, has its own tools and status, and names its payload.
                (self.change and not (self.unbacked and self.dedicated and self.status not in (None, REFUSED)
                                      and self.spec and self.display_name)) or
                (not self.change and (self.spec is not None or self.display_name is not None)) or
                # Only a dedicated kind leaves the generic status rules.
                (self.status is not None and not self.dedicated)):
            raise ValueError('invalid draft kind')

    def spec_type(self) -> type:
        """The change payload class, imported lazily (the payload modules import the draft registry)."""
        if self.spec is None:
            raise ValueError('not a change kind')
        module, name = self.spec.split(':')
        return getattr(importlib.import_module(f'{__package__}.{module}'), name)


DRAFT_KINDS = MappingProxyType({row.name: row for row in (
    DraftKind('document'),
    DraftKind('photo'),
    DraftKind('voice_note'),
    DraftKind('text', unbacked=True),
    DraftKind('forward', unbacked=True, dedicated=True, status='forward_send'),
    DraftKind('video', dedicated=True, status='video_send'),
    DraftKind('archive', change=True, unbacked=True, dedicated=True, status='archive_chats',
              spec='archive_drafts:ArchiveSpec', display_name='Chat list move'),
    DraftKind('text_edit', change=True, unbacked=True, dedicated=True, status='edit_own_text',
              spec='edit_drafts:TextEditSpec', display_name='Text edit'),
    DraftKind('caption_edit', change=True, unbacked=True, dedicated=True, status='edit_own_caption',
              spec='caption_edit_drafts:CaptionEditSpec', display_name='Caption edit'),
    # An album has its own status model (get_album_send_status); generic status never reads one (0.56).
    DraftKind('album', dedicated=True, status=REFUSED),
)})

CHANGE_KINDS = frozenset(name for name, row in DRAFT_KINDS.items() if row.change)
UNBACKED_KINDS = frozenset(name for name, row in DRAFT_KINDS.items() if row.unbacked)
DEDICATED_KINDS = frozenset(name for name, row in DRAFT_KINDS.items() if row.dedicated)
# The artifact-backed kinds of the generic send tools (send_prepared_artifact).
GENERIC_ARTIFACT_KINDS = frozenset(name for name, row in DRAFT_KINDS.items() if not (row.unbacked or row.dedicated))
# The capabilities whose change drafts the broker's change policy admits.
CHANGE_CAPABILITIES = frozenset(DRAFT_KINDS[name].status for name in CHANGE_KINDS)


def change_kind_of(change: object) -> DraftKind | None:
    """The change row whose payload class this payload is, or None."""
    return next((row for row in DRAFT_KINDS.values() if row.change and isinstance(change, row.spec_type())), None)
