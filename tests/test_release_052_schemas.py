"""The 0.52.0 public tool schemas and annotations stay frozen.

The fixture fingerprints input and output schemas and annotations (contract.schema_document); tool
descriptions and titles are not part of it. Every intentional later change is listed, with its reason,
in fixtures/schema_changes/ (see schema_changes.py).
"""
import unittest

from schema_changes import changed_since_0_52, legacy_0_52
from telegram_search_mcp.contract import fingerprint, schema_document
from telegram_search_mcp.server import build_server

CHANGED_SINCE_0_52 = changed_since_0_52()


class Release052SchemaTests(unittest.TestCase):
    def test_0_52_tool_schemas_and_annotations_are_unchanged(self):
        expected = legacy_0_52()
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        self.assertEqual(len(expected), 55)
        for name, digest in expected.items():
            if name in CHANGED_SINCE_0_52:
                continue
            with self.subTest(tool=name):
                self.assertEqual(actual.get(name), digest)

    def test_every_listed_change_names_a_changed_tool(self):
        expected = legacy_0_52()
        actual = {row['name']: fingerprint(row) for row in schema_document(build_server())['tools']}
        for name in CHANGED_SINCE_0_52:
            with self.subTest(tool=name):
                self.assertIn(name, actual)
                self.assertNotEqual(actual[name], expected.get(name))
