"""Bounded caller-supplied styles for already normalized outgoing text.

Coordinates refer to the final whole text in UTF-16 code units. This module
neither parses formatting syntax nor grants permission to send anything. It also
reads the form the pinned TDLib (d1085f9) returns such styles in: touching pieces.
"""
from __future__ import annotations

import unicodedata

FrozenOutgoingEntities = tuple[tuple[int, int, str], ...]
OutgoingEntityFacts = tuple[tuple[int, int, str, str], ...]

_STYLES = frozenset({
    "textEntityTypeBold", "textEntityTypeItalic", "textEntityTypeUnderline",
    "textEntityTypeStrikethrough", "textEntityTypeSpoiler",
})


def _boundaries(final_text: str) -> dict[int, int]:
    if (type(final_text) is not str or not final_text.strip()
            or len(final_text) > 4096
            or any(unicodedata.category(c).startswith("C") and c not in "\n\t"
                   for c in final_text)):
        raise ValueError("Invalid final outgoing text")
    result = {0: 0}
    units = 0
    for index, char in enumerate(final_text, 1):
        units += 2 if ord(char) > 0xFFFF else 1
        result[units] = index
    return result


def freeze_outgoing_entities(
    final_text: str, outgoing_entities: list[dict],
) -> FrozenOutgoingEntities:
    """Validate closed caller records and return immutable canonical spans."""
    boundaries = _boundaries(final_text)
    if type(outgoing_entities) is not list or len(outgoing_entities) > 32:
        raise ValueError("Outgoing entities must be a list of at most 32 spans")
    rows = []
    for entity in outgoing_entities:
        if type(entity) is not dict or set(entity) != {"offset", "length", "type"}:
            raise ValueError("Invalid outgoing entity fields")
        kind = entity["type"]
        if (type(kind) is not dict or set(kind) != {"@type"}
                or type(kind["@type"]) is not str or kind["@type"] not in _STYLES):
            raise ValueError("Unsupported outgoing style")
        offset, length = entity["offset"], entity["length"]
        if (type(offset) is not int or type(length) is not int
                or not 0 <= offset < 2**31 or not 0 < length < 2**31
                or offset not in boundaries or offset + length not in boundaries):
            raise ValueError("Invalid whole-scalar UTF-16 range")
        rows.append((offset, length, kind["@type"]))
    rows.sort(key=lambda row: (row[0], -row[1], row[2]))
    for index, (start, length, kind) in enumerate(rows):
        end = start + length
        for other_start, other_length, other_kind in rows[index + 1:]:
            other_end = other_start + other_length
            if kind == other_kind and other_start <= end:
                raise ValueError("Same-style spans must be separate and nonadjacent")
            if start < other_start < end < other_end:
                raise ValueError("Crossing outgoing styles are unsupported")
    return tuple(rows)


def joined_outgoing_entities(final_text: str, pieces: list[dict]) -> FrozenOutgoingEntities:
    """Returned style rows as the caller's canonical spans (review fix 057). TDLib's fix_entities runs on every message
    it returns, and its split_entities (MessageEntity.cpp) cuts a style at the start of every style nested in it and at
    both ends of every auto-detected entity inside it, so one caller span may come back as touching pieces. Touching
    pieces of one style are joined, as split_entities itself joins them first; overlapping pieces of one style are
    never its output and are refused, and the joined spans must pass freeze_outgoing_entities."""
    if type(pieces) is not list:
        raise ValueError("Outgoing entities must be a list")
    if not pieces:
        return ()
    boundaries = _boundaries(final_text)
    rows = []
    for entity in pieces:
        if type(entity) is not dict or set(entity) != {"offset", "length", "type"}:
            raise ValueError("Invalid outgoing entity fields")
        kind, offset, length = entity["type"], entity["offset"], entity["length"]
        if (type(kind) is not dict or set(kind) != {"@type"} or type(kind["@type"]) is not str
                or kind["@type"] not in _STYLES):
            raise ValueError("Unsupported outgoing style")
        if (type(offset) is not int or type(length) is not int or not 0 <= offset < 2**31 or not 0 < length < 2**31
                or offset not in boundaries or offset + length not in boundaries):
            raise ValueError("Invalid whole-scalar UTF-16 range")
        rows.append((kind["@type"], offset, offset + length))
    joined: list[tuple[str, int, int]] = []
    for kind, start, end in sorted(rows):
        if joined and joined[-1][0] == kind and start <= joined[-1][2]:
            if start < joined[-1][2]:
                raise ValueError("Overlapping pieces of one style")
            joined[-1] = (kind, joined[-1][1], end)
        else:
            joined.append((kind, start, end))
    return freeze_outgoing_entities(final_text, [
        {"offset": start, "length": end - start, "type": {"@type": kind}} for kind, start, end in joined])


def native_outgoing_entities(frozen: FrozenOutgoingEntities) -> list[dict]:
    """Project validated frozen spans into fresh native dictionaries."""
    return [
        {"@type": "textEntity", "offset": offset, "length": length,
         "type": {"@type": kind}}
        for offset, length, kind in frozen
    ]


def outgoing_entity_facts(
    final_text: str, frozen: FrozenOutgoingEntities,
) -> OutgoingEntityFacts:
    """Return complete covered text, including spoiler content, for review."""
    boundaries = _boundaries(final_text)
    return tuple(
        (offset, length, kind,
         final_text[boundaries[offset]:boundaries[offset + length]])
        for offset, length, kind in frozen
    )
