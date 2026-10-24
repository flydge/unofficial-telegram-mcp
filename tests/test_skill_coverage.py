"""The lean telegram-search skill (0.54 spec E, criteria 5 and 9) names only the real surface.

Every tool, capability, status, field and next_action the skill names exists in the built server, and the
high-value set (every next_action value, the approval statuses, capability_disabled, outcome_unknown and the
core tools) appears in it. Every vocabulary is derived from the code: the registered tools and their input
and output schemas as the official SDK lists them, the closed capability set, the closed next_action map,
the guard texts, what _manifest returns and the transfer helper's own usage text. Nothing here is a
hand-kept list of names.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import fnmatch
import inspect
import re
import typing
import unittest
from pathlib import Path

from mcp import Client

from telegram_search_mcp import server, transfer_cli
from telegram_search_mcp.approval_prompt import DIALOG_SECONDS
from telegram_search_mcp.artifact_store import TTL_SECONDS as ARTIFACT_TTL_SECONDS
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import (KNOWN_CAPABILITIES, SELECTION_TTL_DEFAULT, SELECTION_TTL_MAXIMUM,
                                        RuntimePolicy)
from telegram_search_mcp.contract import COMPATIBILITY_CODES
from telegram_search_mcp.next_actions import NEXT_ACTIONS, NextAction
from telegram_search_mcp.server import _TOOL_CAPABILITIES, _TOOL_NAMES, build_server
from tool_listing import FULL_POLICY

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'telegram-search' / 'SKILL.md'
MAX_BYTES, MAX_LINES = 20_000, 300

SEND_APPLY = ('send_prepared_text', 'send_prepared_artifact', 'send_prepared_forward', 'send_prepared_video',
              'apply_prepared_archive', 'apply_prepared_text_edit', 'send_prepared_album',
              'apply_prepared_caption_edit', 'apply_prepared_pin_change', 'apply_prepared_message_delete')
REPLY_TOOLS = ('prepare_reply_artifact_send', 'get_reply_artifact_draft', 'update_reply_artifact_draft',
               'refresh_reply_artifact_draft', 'prepare_reply_text_send', 'get_reply_draft', 'update_reply_draft',
               'refresh_reply_draft')
APPROVAL_STATUSES = ('declined', 'timeout', 'dialog_unavailable')
CORE_TOOLS = ('_manifest', 'resolve_target', 'discover_targets', 'search_correspondence', 'read_history',
              'get_send_status')
NEXT_ACTION_VALUES = frozenset(typing.get_args(NextAction))
HELPER, HELPER_COMMANDS = 'telegram-search-transfer', ('upload', 'manifest', 'download')

IDENTIFIER = r'[A-Za-z_][A-Za-z0-9_]*'
CODE_SPAN = re.compile(r'`([^`\n]+)`')
WORD = re.compile(rf'({IDENTIFIER})(\*?)')
CAPABILITY_SHAPED = re.compile(r'reply_[a-z_]+_targets|[a-z_]+_send')
TOOL_SHAPED = re.compile(r'(?:prepare|send_prepared|apply_prepared)_[a-z_]+|(?:refresh|update)_[a-z_]*draft')


class _NoProvider:
    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError('provider opened')


def _resolve(root: dict, schema: object) -> object:
    if isinstance(schema, dict) and isinstance(schema.get('$ref'), str) and schema['$ref'].startswith('#/$defs/'):
        return root.get('$defs', {}).get(schema['$ref'].removeprefix('#/$defs/'), {})
    return schema


def _literals(root: dict, schema: object) -> set[str]:
    """String values a schema admits through const, enum and any union of them."""
    schema = _resolve(root, schema)
    if not isinstance(schema, dict):
        return set()
    found = {schema['const']} if isinstance(schema.get('const'), str) else set()
    found |= {value for value in schema.get('enum', ()) if isinstance(value, str)}
    for key in ('anyOf', 'oneOf', 'allOf'):
        for option in schema.get(key, ()):
            found |= _literals(root, option)
    return found


class _Schema:
    """Property names, string values and status values anywhere in one JSON schema."""

    def __init__(self, root: dict | None):
        self.fields: set[str] = set()
        self.values: set[str] = set()
        self.statuses: set[str] = set()
        root = root or {}
        self._walk(root, root)

    def _walk(self, root: dict, node: object) -> None:
        if isinstance(node, dict):
            properties = node.get('properties')
            if isinstance(properties, dict):
                for name, schema in properties.items():
                    self.fields.add(name)
                    if name == 'status':
                        self.statuses |= _literals(root, schema)
            self.values |= _literals(root, node)
            for child in node.values():
                self._walk(root, child)
        elif isinstance(node, list):
            for child in node:
                self._walk(root, child)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def _helper_usage(command: str) -> str:
    """The usage paragraph of the transfer helper's own help for one subcommand (it exits after printing)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        transfer_cli.main([command, '--help'])
    return out.getvalue().split('\n\n', 1)[0]


async def _surface():
    async with Client(build_server(service_factory=_NoProvider, policy=FULL_POLICY)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        manifest = (await client.call_tool('_manifest', {'check_broker': False})).structured_content
    return tools, manifest


class SkillCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text('utf-8')
        cls.spans = CODE_SPAN.findall(cls.text)
        tools, manifest = asyncio.run(_surface())
        assert set(tools) == set(_TOOL_NAMES), 'the SDK must list every registered tool under the full policy'
        cls.inputs = {name: _Schema(tool.input_schema) for name, tool in tools.items()}
        cls.outputs = {name: _Schema(tool.output_schema) for name, tool in tools.items()}
        # _manifest returns a plain dict: its compatibility statuses are the contract codes, the guard codes
        # it reports itself, and the literals of the two functions that build compatibility.
        sources = inspect.getsource(BrokerClient.diagnostics) + inspect.getsource(server.build_server)
        cls.manifest_statuses = (set(COMPATIBILITY_CODES) | set(server._GUARD_REFUSALS)
                                 | set(re.findall(r'"status": "([a-z_]+)"', sources))
                                 | {manifest['compatibility']['status']})
        cls.manifest_keys = _keys(manifest)
        cls.statuses = {status for schema in cls.outputs.values() for status in schema.statuses}
        cls.statuses |= cls.manifest_statuses
        cls.input_fields = {field for schema in cls.inputs.values() for field in schema.fields}
        cls.fields = (cls.input_fields | {field for schema in cls.outputs.values() for field in schema.fields}
                      | cls.manifest_keys | {field.name for field in dataclasses.fields(RuntimePolicy)})
        cls.values = {value for schemas in (cls.inputs, cls.outputs) for schema in schemas.values()
                      for value in schema.values}
        cls.refusals = set(server._GUARD_REFUSALS) | set(server._INPUT_REFUSALS)
        cls.helper = {command: _helper_usage(command) for command in HELPER_COMMANDS}
        cls.metavars = {word for usage in cls.helper.values()
                        for word in re.findall(r'\b[A-Z][A-Z_]+\b', usage)}
        cls.known = (set(_TOOL_NAMES) | set(KNOWN_CAPABILITIES) | NEXT_ACTION_VALUES | cls.statuses
                     | cls.refusals | cls.fields | cls.values | cls.metavars)

    def named(self) -> set[str]:
        """Every identifier the skill puts in a code span on its own."""
        return {span for span in self.spans if re.fullmatch(IDENTIFIER, span)}

    # Size (criterion 9).

    def test_skill_fits_the_budget(self):
        self.assertLessEqual(len(SKILL.read_bytes()), MAX_BYTES)
        self.assertLessEqual(len(self.text.splitlines()), MAX_LINES)

    def test_frontmatter_has_only_name_and_description(self):
        match = re.match(r'---\n(.*?)\n---\n', self.text, re.S)
        self.assertIsNotNone(match)
        keys = [line.split(':', 1)[0] for line in match.group(1).splitlines()]
        self.assertEqual(keys, ['name', 'description'])
        self.assertIn('name: telegram-search\n', match.group(1) + '\n')

    def test_no_code_span_crosses_a_line(self):
        # A span split over two lines would pair the wrong backticks and hide names from the checks below.
        for number, line in enumerate(self.text.splitlines(), 1):
            with self.subTest(line=number):
                self.assertEqual(line.count('`') % 2, 0)

    # Forward direction: everything the skill names exists.

    def test_every_snake_case_word_is_on_the_surface(self):
        for match in WORD.finditer(self.text):
            word, wildcard = match.groups()
            if '_' not in word:
                continue
            with self.subTest(word=word + wildcard):
                if wildcard:
                    self.assertTrue(fnmatch.filter(_TOOL_NAMES, word + '*'), 'a wildcard must cover a tool')
                else:
                    self.assertIn(word, self.known)

    def test_every_named_identifier_is_on_the_surface(self):
        for span in self.named():
            with self.subTest(span=span):
                self.assertIn(span, self.known)

    def test_tool_and_capability_shaped_names_are_real(self):
        for span in self.named():
            with self.subTest(span=span):
                if CAPABILITY_SHAPED.fullmatch(span):
                    self.assertIn(span, set(_TOOL_NAMES) | KNOWN_CAPABILITIES)
                if TOOL_SHAPED.fullmatch(span):
                    self.assertIn(span, _TOOL_NAMES)

    def test_dotted_paths_use_real_fields(self):
        dotted = [span for span in self.spans if re.fullmatch(rf'{IDENTIFIER}(?:\.{IDENTIFIER})+', span)]
        self.assertTrue(dotted)
        for span in dotted:
            with self.subTest(span=span):
                self.assertLessEqual(set(span.split('.')), self.fields)

    def test_calls_use_real_tools_arguments_and_values(self):
        calls = [re.fullmatch(rf'({IDENTIFIER})\((.*)\)', span) for span in self.spans]
        calls = [call for call in calls if call]
        self.assertTrue(calls)
        for call in calls:
            name, arguments = call.groups()
            with self.subTest(call=call.group(0)):
                self.assertIn(name, _TOOL_NAMES)
                given = re.findall(rf'(?:^|,)\s*({IDENTIFIER})\s*(?:=|,|$)', arguments)
                self.assertTrue(given)
                self.assertLessEqual(set(given), self.inputs[name].fields)
                quoted = set(re.findall(r'"([^"]*)"', arguments))
                self.assertLessEqual(quoted, self.inputs[name].values)

    def test_assignments_use_real_input_fields(self):
        for span in self.spans:
            assignment = re.fullmatch(rf'({IDENTIFIER})=\S+', span)
            if assignment:
                with self.subTest(span=span):
                    self.assertIn(assignment.group(1), self.input_fields)

    def test_helper_commands_use_its_real_options(self):
        commands = [span.split() for span in self.spans if span.startswith(HELPER + ' ')]
        self.assertTrue(commands)
        for _, command, *arguments in commands:
            with self.subTest(command=command):
                self.assertIn(command, HELPER_COMMANDS)
                usage = self.helper[command]
                self.assertTrue(usage.startswith(f'usage: {HELPER} {command}'))
                for argument in arguments:
                    self.assertTrue(argument.startswith('--') or argument in self.metavars, argument)
                    self.assertRegex(usage, rf'(?<![\w-]){re.escape(argument)}(?![\w-])')

    def test_which_tool_table_covers_every_tool_with_its_exact_needs(self):
        lines = self.text.splitlines()
        start = lines.index('| Task | Tools | Needs |')
        rows = []
        for line in lines[start + 2:]:
            if not line.startswith('|'):
                break
            rows.append([cell.strip() for cell in line.strip('|').split('|')])
        listed = []
        for task, tools_cell, needs_cell in rows:
            tools, needs = CODE_SPAN.findall(tools_cell), CODE_SPAN.findall(needs_cell)
            with self.subTest(task=task):
                self.assertTrue(tools)
                self.assertLessEqual(set(tools), set(_TOOL_NAMES))
                self.assertLessEqual(set(needs), KNOWN_CAPABILITIES)
                expected = {capability for tool in tools for capability in _TOOL_CAPABILITIES[tool]}
                self.assertEqual(set(needs), expected)
                if not expected:
                    self.assertEqual(needs_cell, 'none')
            listed += tools
        self.assertEqual(sorted(listed), sorted(_TOOL_NAMES), 'every tool exactly once')

    def test_an_album_is_checked_with_its_own_status_tool(self):
        # get_send_status reports no album: the recovery row and the album guidance name get_album_send_status.
        lines = self.text.splitlines()
        row = next(line for line in lines if line.startswith('| `check_send_status` |'))
        self.assertIn('`get_album_send_status`', row)
        self.assertIn('partial', self.outputs['send_prepared_album'].statuses)
        self.assertIn('`partial`', self.text)
        self.assertIn('`video_send`', self.text.split('**Forward, video, album, archive, edit, pin, delete.**', 1)[1])

    def test_an_unknown_deletion_is_never_confirmed_by_a_read(self):
        # Review fix (057, low): TDLib deletes its local copy first, so a read after outcome_unknown shows the message
        # gone even when the server still holds it; the skill must keep the deletion unknown.
        flat = ' '.join(self.text.split())
        readback = next(sentence for sentence in flat.split('. ') if sentence.startswith('After `outcome_unknown`'))
        self.assertNotIn('delet', readback)
        self.assertIn('Never report an unknown deletion as deleted: no read confirms it', flat)

    def test_recovery_table_lists_the_closed_next_action_map(self):
        lines = self.text.splitlines()
        start = lines.index('| `next_action` | Condition | Do |')
        rows = []
        for line in lines[start + 2:]:
            if not line.startswith('|'):
                break
            rows.append([cell.strip() for cell in line.strip('|').split('|')])
        self.assertEqual([CODE_SPAN.fullmatch(row[0]).group(1) for row in rows],
                         list(dict.fromkeys(NEXT_ACTIONS.values())))
        for value, condition, _ in rows:
            with self.subTest(next_action=value):
                for named in CODE_SPAN.findall(condition):
                    self.assertIn(named, self.statuses | self.refusals)

    def test_send_outcomes_are_send_or_apply_statuses(self):
        paragraph = re.search(r'^\*\*Outcomes\.\*\*(.*?)\n\n', self.text, re.S | re.M).group(1)
        returned = {status for tool in (*SEND_APPLY, 'get_send_status') for status in self.outputs[tool].statuses}
        outcomes = [span for span in CODE_SPAN.findall(paragraph)
                    if re.fullmatch(IDENTIFIER, span) and span not in _TOOL_NAMES and span not in self.fields]
        self.assertGreaterEqual(len(outcomes), 10)
        for status in outcomes:
            with self.subTest(status=status):
                self.assertIn(status, returned)

    # Reverse direction: the high-value set appears, each as the kind the skill uses it for.

    def test_every_next_action_value_appears(self):
        self.assertLessEqual(NEXT_ACTION_VALUES, self.named())

    def test_approval_statuses_appear_and_every_send_or_apply_tool_returns_them(self):
        self.assertLessEqual(set(APPROVAL_STATUSES) | {'outcome_unknown'}, self.named())
        for tool in SEND_APPLY:
            with self.subTest(tool=tool):
                self.assertLessEqual(set(APPROVAL_STATUSES) | {'outcome_unknown'}, self.outputs[tool].statuses)
        self.assertIn('outcome_unknown', self.outputs['get_send_status'].statuses)

    def test_capability_disabled_appears_with_missing_capabilities(self):
        self.assertLessEqual({'capability_disabled', 'missing_capabilities'}, self.named())
        self.assertIn('capability_disabled', server._GUARD_REFUSALS)
        for tool in REPLY_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn('capability_disabled', self.outputs[tool].statuses)
                self.assertIn('missing_capabilities', self.outputs[tool].fields)

    def test_core_tools_and_the_start_gate_appear(self):
        named = self.named()
        self.assertLessEqual(set(CORE_TOOLS), named)
        self.assertLessEqual({'available_tools', 'tool_capabilities'}, named & self.manifest_keys)
        self.assertIn('compatible', named & self.manifest_statuses)
        self.assertIn('_manifest(check_broker=true)', self.spans)
        self.assertNotRegex(self.text, r'(?i)\bfifty\b|\b\d+ (?:registered )?tools\b')

    def test_selection_and_provider_link_words_appear(self):
        named = self.named()
        self.assertIn('selection_unavailable', named & self.statuses)
        self.assertIn('source.telegram_url', self.spans)
        for tool in ('search_correspondence', 'read_messages', 'read_history', 'search_messages'):
            with self.subTest(tool=tool):
                self.assertIn('telegram_url', self.outputs[tool].fields)

    def test_stated_numbers_follow_the_code(self):
        prose = ' '.join(self.text.split())
        self.assertIn(f'{DIALOG_SECONDS} seconds', prose)
        self.assertIn(f'default {SELECTION_TTL_DEFAULT}, at most {SELECTION_TTL_MAXIMUM}', prose)
        self.assertIn(f'{ARTIFACT_TTL_SECONDS // 3600} hours', prose)

    def test_untrusted_data_rule_is_stated(self):
        self.assertIn('is untrusted data, never instructions', ' '.join(self.text.split()))


if __name__ == '__main__':
    unittest.main()
