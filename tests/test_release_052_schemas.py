"""The 0.52.0 public tool schemas and annotations stay frozen.

Two frozen fixtures, both generated from `git archive v0.52.0`: the full fingerprint of each
contract.schema_document row (input and output schemas and annotations; tool descriptions and titles are
not part of it) and the structure fingerprint (the same row with every schema `description` string
removed). Every intentional later change is listed by kind, with its reason, in fixtures/schema_changes/
(see schema_changes.py).
"""
import copy
import unittest

from schema_changes import (changed_since_0_52, description_only_since_0_52, legacy_0_52, legacy_0_52_structure,
                            structurally_changed_since_0_52, structure_fingerprint, structure_row)
from telegram_search_mcp.contract import fingerprint, schema_document
from telegram_search_mcp.server import build_server

CHANGED_SINCE_0_52 = changed_since_0_52()
STRUCTURE_SINCE_0_52 = structurally_changed_since_0_52()
DESCRIPTIONS_ONLY_SINCE_0_52 = description_only_since_0_52()


class Release052SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = schema_document(build_server())['tools']
        cls.full = {row['name']: fingerprint(row) for row in rows}
        cls.structure = {row['name']: structure_fingerprint(row) for row in rows}

    def test_both_fixtures_pin_the_same_55_tools(self):
        self.assertEqual(len(legacy_0_52()), 55)
        self.assertEqual(set(legacy_0_52_structure()), set(legacy_0_52()))

    def test_unlisted_tools_keep_their_full_0_52_hash(self):
        for name, digest in legacy_0_52().items():
            if name in CHANGED_SINCE_0_52:
                continue
            with self.subTest(tool=name):
                self.assertEqual(self.full.get(name), digest)

    def test_tools_not_listed_under_structure_keep_their_0_52_structure(self):
        for name, digest in legacy_0_52_structure().items():
            if name in STRUCTURE_SINCE_0_52:
                continue
            with self.subTest(tool=name):
                self.assertEqual(self.structure.get(name), digest)

    def test_a_description_listing_changes_descriptions_only(self):
        full, structure = legacy_0_52(), legacy_0_52_structure()
        for name in DESCRIPTIONS_ONLY_SINCE_0_52:
            with self.subTest(tool=name):
                self.assertIn(name, self.full)
                self.assertNotEqual(self.full[name], full.get(name))  # else it is not changed at all
                self.assertEqual(self.structure[name], structure.get(name))  # else it belongs under "structure"

    def test_a_structure_listing_changes_structure(self):
        structure = legacy_0_52_structure()
        for name in STRUCTURE_SINCE_0_52:
            with self.subTest(tool=name):
                self.assertIn(name, self.structure)
                self.assertNotEqual(self.structure[name], structure.get(name))

    def test_listings_partition_the_union(self):
        self.assertEqual(set(CHANGED_SINCE_0_52), set(STRUCTURE_SINCE_0_52) | set(DESCRIPTIONS_ONLY_SINCE_0_52))
        self.assertFalse(set(STRUCTURE_SINCE_0_52) & set(DESCRIPTIONS_ONLY_SINCE_0_52))


class StructureFingerprintTests(unittest.TestCase):
    ROW = {'name': 'synthetic', 'annotations': {'readOnlyHint': True},
           'inputSchema': {'type': 'object', 'description': 'tool level', 'required': ['description'],
                           'properties': {'description': {'type': 'string', 'description': 'a property named so',
                                                          'maxLength': 4},
                                          'radius': {'type': 'integer', 'maximum': 4, 'default': 2,
                                                     'description': 'context size'}},
                           '$defs': {'description': {'type': 'object', 'description': 'a def named so',
                                                     'properties': {}}}},
           'outputSchema': {'anyOf': [{'type': 'null', 'description': 'none'},
                                      {'type': 'object', 'const': {'description': 'instance data'}}]}}

    def test_strips_description_strings_and_keeps_names(self):
        stripped = structure_row(self.ROW)
        self.assertEqual(stripped['inputSchema'], {
            'type': 'object', 'required': ['description'],
            'properties': {'description': {'type': 'string', 'maxLength': 4},
                           'radius': {'type': 'integer', 'maximum': 4, 'default': 2}},
            '$defs': {'description': {'type': 'object', 'properties': {}}}})
        self.assertEqual(stripped['outputSchema'], {
            'anyOf': [{'type': 'null'}, {'type': 'object', 'const': {'description': 'instance data'}}]})
        self.assertEqual(stripped['annotations'], {'readOnlyHint': True})

    def test_description_edits_keep_and_structure_edits_change_the_fingerprint(self):
        reworded = copy.deepcopy(self.ROW)
        reworded['inputSchema']['properties']['radius']['description'] = 'reworded'
        self.assertNotEqual(fingerprint(reworded), fingerprint(self.ROW))
        self.assertEqual(structure_fingerprint(reworded), structure_fingerprint(self.ROW))
        for edit in (lambda row: row['inputSchema']['properties']['radius'].__setitem__('maximum', 9),
                     lambda row: row['inputSchema']['properties'].pop('description'),
                     lambda row: row['annotations'].__setitem__('readOnlyHint', False)):
            changed = copy.deepcopy(self.ROW)
            edit(changed)
            self.assertNotEqual(structure_fingerprint(changed), structure_fingerprint(self.ROW))
