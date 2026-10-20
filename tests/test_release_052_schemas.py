"""The 0.52.0 public tool schemas and annotations stay frozen.

The fixture fingerprints input and output schemas and annotations (contract.schema_document); tool
descriptions are not part of it. List every intentional later change below with its reason.
"""
import json
import unittest
from pathlib import Path

from telegram_search_mcp.contract import fingerprint, schema_document
from telegram_search_mcp.server import build_server

CHANGED_SINCE_0_52: dict[str, str] = {}


class Release052SchemaTests(unittest.TestCase):
    def test_0_52_tool_schemas_and_annotations_are_unchanged(self):
        expected = json.loads((Path(__file__).parent / 'fixtures/legacy_0_52_tool_hashes.json').read_text())
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        self.assertEqual(len(expected), 55)
        for name, digest in expected.items():
            if name in CHANGED_SINCE_0_52:
                continue
            with self.subTest(tool=name):
                self.assertEqual(actual.get(name), digest)
