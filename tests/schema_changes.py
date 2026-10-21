"""Intentional public schema changes since the frozen 0.52.0 fixtures.

`fixtures/legacy_0_52_tool_hashes.json` (full schema_document rows) and
`fixtures/legacy_0_52_structure_hashes.json` (the same rows with every schema `description` string
removed) never change; both were generated from `git archive v0.52.0`.

Each slice lists the tools it changes in its own `fixtures/schema_changes/<slice>.json`, so parallel
slices never edit one shared file:

    {"structure": {tool: reason}, "descriptions": {tool: reason}}

- `structure`: input or output schema shape, bounds, statuses or annotations change.
- `descriptions`: only schema `description` strings change; the structure hash must stay equal.

Either key may be absent; a present key is a non-empty object. A tool may be listed by several slices.
A tool listed under `structure` by any slice counts as structurally changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from telegram_search_mcp.contract import fingerprint

DIRECTORY = Path(__file__).parent / 'fixtures/schema_changes'
LEGACY_0_52 = Path(__file__).parent / 'fixtures/legacy_0_52_tool_hashes.json'
LEGACY_0_52_STRUCTURE = Path(__file__).parent / 'fixtures/legacy_0_52_structure_hashes.json'
KINDS = ('structure', 'descriptions')

# Keywords whose value maps names to subschemas: the names are data, never keywords.
_SCHEMA_MAPS = frozenset({'properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'})
# Keywords whose value is instance data, never a schema.
_DATA_KEYWORDS = frozenset({'default', 'const', 'enum', 'examples'})


def _without_descriptions(node: object) -> object:
    if type(node) is list:
        return [_without_descriptions(item) for item in node]
    if type(node) is not dict:
        return node
    stripped: dict[str, object] = {}
    for key, value in node.items():
        if key == 'description' and type(value) is str:
            continue
        if key in _SCHEMA_MAPS and type(value) is dict:
            stripped[key] = {name: _without_descriptions(schema) for name, schema in value.items()}
        elif key in _DATA_KEYWORDS:
            stripped[key] = value
        else:
            stripped[key] = _without_descriptions(value)
    return stripped


def structure_row(row: dict[str, object]) -> dict[str, object]:
    """A schema_document tool row with every schema `description` string removed; a property NAMED
    `description` inside a `properties` mapping stays."""
    return {key: _without_descriptions(value) if key in ('inputSchema', 'outputSchema') else value
            for key, value in row.items()}


def structure_fingerprint(row: dict[str, object]) -> str:
    return fingerprint(structure_row(row))


def _slices() -> dict[str, dict[str, dict[str, str]]]:
    slices: dict[str, dict[str, dict[str, str]]] = {}
    for path in sorted(DIRECTORY.glob('*.json')):
        document = json.loads(path.read_text(encoding='utf-8'))
        if type(document) is not dict or not document or not set(document) <= set(KINDS):
            raise AssertionError(f'{path.name}: expected an object with "structure" and/or "descriptions"')
        listed: set[str] = set()
        for kind, rows in document.items():
            if type(rows) is not dict or not rows or not all(
                    type(name) is str and type(reason) is str and reason.strip() for name, reason in rows.items()):
                raise AssertionError(f'{path.name}: "{kind}" must be a non-empty object of tool name -> reason')
            if listed & set(rows):
                raise AssertionError(f'{path.name}: a tool is listed under both kinds')
            listed |= set(rows)
        slices[path.stem] = document
    return slices


def _collect(kinds: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    changes: dict[str, list[str]] = {}
    for stem, document in _slices().items():
        for kind in kinds:
            for name, reason in document.get(kind, {}).items():
                changes.setdefault(name, []).append(f'{stem} ({kind}): {reason}')
    return {name: tuple(reasons) for name, reasons in sorted(changes.items())}


def changed_since_0_52() -> dict[str, tuple[str, ...]]:
    """Tool name -> the reasons, one per slice listing; the union of both kinds."""
    return _collect(KINDS)


def structurally_changed_since_0_52() -> dict[str, tuple[str, ...]]:
    """Tool name -> the reasons of the slices that list it under "structure"."""
    return _collect(('structure',))


def description_only_since_0_52() -> dict[str, tuple[str, ...]]:
    """Tools listed under "descriptions" and never under "structure"."""
    structure = structurally_changed_since_0_52()
    return {name: reasons for name, reasons in _collect(('descriptions',)).items() if name not in structure}


def legacy_0_52() -> dict[str, str]:
    return json.loads(LEGACY_0_52.read_text(encoding='utf-8'))


def legacy_0_52_structure() -> dict[str, str]:
    return json.loads(LEGACY_0_52_STRUCTURE.read_text(encoding='utf-8'))
