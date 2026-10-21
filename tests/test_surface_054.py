"""The 0.54 tool surface (spec A, criterion 2), observed through the official SDK client.

Descriptions are short user-facing contracts within budget; every tool has a top-level title and the
annotations of the 21.10 lead decision; and key inputs are described.
"""
from __future__ import annotations

import json
import re
import unittest

from telegram_search_mcp import server as server_module
from telegram_search_mcp.config import KNOWN_CAPABILITIES
from telegram_search_mcp.selection_broker import selected_models
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES
from tool_listing import full_listing

PER_TOOL, TOTAL = 1500, 30000
JARGON = re.compile(r"private v\d|\bv\d{1,2}\b|\bipc ?2\b|canonical|prewire|contract ?1\b|display bytes", re.I)
SEND_APPLY = ('send_prepared_text', 'send_prepared_artifact', 'send_prepared_forward', 'send_prepared_video',
              'apply_prepared_archive', 'apply_prepared_text_edit')
MANDATORY = ('local dialog', 'outcome_unknown', 'get_send_status', 'never resend')
# Results without Telegram-originated strings need not repeat the untrusted-data rule.
LOCAL_ONLY = {'_manifest', 'create_local_artifact', 'begin_local_upload', 'append_local_upload', 'finish_local_upload',
              'issue_local_upload_authority', 'consume_local_upload_result', 'cancel_local_upload_authority',
              'list_drafts', 'get_send_status', 'cancel_draft', *SEND_APPLY}
READ_ONLY_PREFIXES = ('_manifest', 'resolve_', 'discover_', 'search_', 'get_', 'read_', 'analyze_', 'list_',
                      'verify_', 'retain_', 'prepare_')
TOOL_LIKE = re.compile(r"\b(?:prepare|send_prepared|apply_prepared|get|read|list|update|refresh|cancel|retain|verify|"
                       r"discover|resolve|search|analyze|create_local|begin_local|append_local|finish_local|"
                       r"issue_local|consume_local)_[a-z_]+\b|\b[a-z_]+_(?:targets|send)\b")
KEY_INPUTS = {'target', 'targets', 'anchor', 'anchors', 'selection_id', 'reply_selection_id', 'reply_to',
              'artifact_id', 'recipient', 'draft_id', 'approved', 'cursor', 'mode', 'topic', 'query', 'hypotheses',
              'scope', 'source', 'manifest', 'result_ref', 'upload_id', 'chat', 'message_ids', 'target_handle',
              'text', 'new_text', 'outgoing_entities', 'source_chat', 'source_message_id', 'message_id',
              'target_list'}
REPLY_TOOLS = ('prepare_reply_artifact_send', 'get_reply_artifact_draft', 'update_reply_artifact_draft',
               'refresh_reply_artifact_draft', 'prepare_reply_text_send', 'get_reply_draft', 'update_reply_draft',
               'refresh_reply_draft')
class DescriptionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = await full_listing()

    def test_budget_is_measured_on_the_built_registration(self):
        self.assertEqual(set(self.tools), set(_TOOL_NAMES))
        lengths = {name: len(tool.description) for name, tool in self.tools.items()}
        for name, length in lengths.items():
            with self.subTest(tool=name):
                self.assertLessEqual(length, PER_TOOL)
        self.assertLessEqual(sum(lengths.values()), TOTAL)

    def test_no_internal_history(self):
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertIsNone(JARGON.search(tool.description))
                self.assertNotIn('codex', tool.description.casefold())

    def test_every_tool_has_a_top_level_title_outside_the_annotations(self):
        titles = [tool.title for tool in self.tools.values()]
        self.assertEqual(len(set(titles)), len(titles))
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertIsInstance(tool.title, str)
                self.assertTrue(0 < len(tool.title) <= 40 and tool.title == tool.title.strip())
                self.assertIsNone(tool.annotations.title)

    def test_capability_sentence_is_generated_from_the_guard_rule(self):
        for name, tool in self.tools.items():
            needed = _TOOL_CAPABILITIES[name]
            with self.subTest(tool=name):
                self.assertIn(f"Needs: {', '.join(needed)}." if needed else 'Needs no capability.', tool.description)

    def test_send_and_apply_tools_state_the_dialog_and_the_unknown_outcome_rule(self):
        for name in SEND_APPLY:
            with self.subTest(tool=name):
                for phrase in MANDATORY:
                    self.assertIn(phrase, self.tools[name].description)

    def test_telegram_data_is_named_untrusted(self):
        for name, tool in self.tools.items():
            if name not in LOCAL_ONLY:
                with self.subTest(tool=name):
                    self.assertIn('untrusted', tool.description.casefold())

    def test_named_tools_and_capabilities_exist(self):
        known = set(_TOOL_NAMES) | KNOWN_CAPABILITIES
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertEqual({word for word in TOOL_LIKE.findall(tool.description) if word not in known}, set())

    def test_reply_tools_share_one_complete_capability_text(self):
        shared = server_module._REPLY
        self.assertTrue(600 <= len(shared) <= 1300)
        families = {name for name in KNOWN_CAPABILITIES if name.startswith('reply_') and name.endswith('_targets')}
        self.assertEqual(families, set(re.findall(r"\breply_[a-z_]+_targets\b", shared)))
        for name in REPLY_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(shared, self.tools[name].description)

    def test_selection_tools_carry_the_policy_bounded_lifetime(self):
        note = server_module._SELECTION
        self.assertIn("bounded time set by the owner's policy", note)
        self.assertIsNone(re.search(r"\d", note))
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertEqual(note in tool.description, name in selected_models())
        self.assertIn("bounded time set by the owner's policy", self.tools['retain_selection'].description)
        self.assertIsNone(re.search(r"\d+ ?s(?:econds)?\b", self.tools['retain_selection'].description))


class AnnotationAndFieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = await full_listing()

    def test_annotations_follow_the_lead_decision(self):
        for name, tool in self.tools.items():
            hints = tool.annotations
            read_only = name.startswith(READ_ONLY_PREFIXES)
            with self.subTest(tool=name):
                self.assertEqual(hints.read_only_hint, read_only)
                self.assertEqual(hints.destructive_hint, name == 'apply_prepared_text_edit')
                self.assertTrue(hints.open_world_hint)
                if not read_only or name.startswith('prepare_'):
                    self.assertFalse(hints.idempotent_hint)
                if name.startswith('get_'):
                    self.assertTrue(hints.idempotent_hint)

    def test_key_inputs_are_described_briefly(self):
        documented = set(server_module._FIELD_DOCS.values())
        for name, tool in self.tools.items():
            properties = tool.input_schema['properties']
            with self.subTest(tool=name):
                for key in KEY_INPUTS & set(properties):
                    self.assertIn(properties[key].get('description'), documented, key)
                for key, value in properties.items():
                    self.assertLessEqual(len(value.get('description', '')), 120, key)
                # Shared aliases stay untouched: no output schema carries an input description.
                rendered = json.dumps(tool.output_schema or {}, ensure_ascii=False)
                self.assertFalse([text for text in documented if text in rendered])


if __name__ == '__main__':
    unittest.main()
