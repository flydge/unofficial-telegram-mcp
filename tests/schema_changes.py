"""Intentional public schema changes since the frozen 0.52.0 fixture.

`fixtures/legacy_0_52_tool_hashes.json` never changes. Each slice lists the tools whose input schema,
output schema or annotations it changes, name -> reason, in its own
`fixtures/schema_changes/<slice>.json`, so parallel slices never edit one shared file. A tool may be
listed by several slices; every listed tool must exist and must actually differ from the fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

DIRECTORY = Path(__file__).parent / 'fixtures/schema_changes'
LEGACY_0_52 = Path(__file__).parent / 'fixtures/legacy_0_52_tool_hashes.json'


def changed_since_0_52() -> dict[str, tuple[str, ...]]:
    """Tool name -> the reasons, one per slice that lists it."""
    changes: dict[str, list[str]] = {}
    for path in sorted(DIRECTORY.glob('*.json')):
        rows = json.loads(path.read_text(encoding='utf-8'))
        if type(rows) is not dict or not rows or not all(
                type(name) is str and type(reason) is str and reason.strip() for name, reason in rows.items()):
            raise AssertionError(f'{path.name}: expected a non-empty object of tool name -> reason')
        for name, reason in rows.items():
            changes.setdefault(name, []).append(f'{path.stem}: {reason}')
    return {name: tuple(reasons) for name, reasons in sorted(changes.items())}


def legacy_0_52() -> dict[str, str]:
    return json.loads(LEGACY_0_52.read_text(encoding='utf-8'))
