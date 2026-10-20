"""Product names live in `product_identity`; renaming the product is one edit, proven here."""
from __future__ import annotations

from collections import deque
import importlib
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client

from telegram_search_mcp import __version__, claude_registration, codex_registration, connect_page, product_identity
from telegram_search_mcp.approval_prompt import _CHANGE_SCRIPT, _SCRIPT, _dialog_script, confirm_approved_send
from telegram_search_mcp.auth_transport import AuthTransport
from telegram_search_mcp.contract import schema_fingerprint
from telegram_search_mcp.credentials import ApiCredentials, SecretValue
from telegram_search_mcp.product_identity import IdentityError, check_names
from telegram_search_mcp.server import _TOOL_NAMES, build_server
from telegram_search_mcp.tdjson import TDLibClient

SOURCE = Path(product_identity.__file__).resolve().parent
# Schema fingerprint at bbadc84, before the identity module existed: routing names must not change it.
FINGERPRINT_BBADC84 = '02c92d5e9a55fd4a74c2a5e1d8079b0b8730c0dc1421b3bdfd519642df1b0454'
RENAMED = 'Renamed Product'
# Explicit allowlist, relative to src/telegram_search_mcp. `TelegramSearchMCP` is also the internal
# support-directory name (config.py) and stays; as a device model it may appear only in product_identity.
LITERALS = {
    'Unofficial Telegram MCP': {'product_identity.py'},
    'TelegramSearchMCP': {'product_identity.py', 'config.py'},
}
SERVER_LITERAL = re.compile(r'''['"]telegram_search['"]''')


def codes(**names):
    try:
        check_names(**names)
    except IdentityError as error:
        return error.code
    raise AssertionError('check_names accepted ' + repr(sorted(names)))


class CheckNamesTests(unittest.IsolatedAsyncioTestCase):
    def test_real_constants_pass_for_every_registered_tool(self):
        check_names(tool_names=_TOOL_NAMES)
        longest = max(_TOOL_NAMES, key=len)
        head = f'mcp__plugin_{product_identity.PLUGIN_NAME}_{product_identity.PLUGIN_SERVER_NAME}__'
        self.assertLessEqual(len(head + longest), 64)

    async def test_registered_tools_are_the_checked_tools(self):
        async with Client(build_server(service_factory=lambda: self.fail('provider opened'))) as client:
            listed = [tool.name for tool in (await client.list_tools()).tools]
        self.assertTrue(listed)
        self.assertLessEqual(set(listed), set(_TOOL_NAMES))
        check_names(tool_names=listed)

    def test_twenty_characters_is_exactly_the_plugin_budget_of_the_longest_tool(self):
        check_names(plugin_name='a' * 18, plugin_server_name='tg', tool_names=_TOOL_NAMES)
        self.assertEqual(codes(plugin_name='a' * 19, plugin_server_name='tg', tool_names=_TOOL_NAMES),
                         'plugin_tool_name_length')

    def test_plugin_named_telegram_is_refused(self):
        self.assertEqual(codes(plugin_name='telegram'), 'plugin_name_reserved')

    def test_overlong_plugin_and_server_pair_is_refused(self):
        self.assertEqual(codes(plugin_name='telegram-local-a', plugin_server_name='tg123'), 'plugin_name_budget')
        self.assertEqual(codes(plugin_name='a' * 19, plugin_server_name='tg'), 'plugin_name_budget')
        check_names(plugin_name='a' * 18, plugin_server_name='tg')

    def test_invalid_reverse_dns_prefix_is_refused(self):
        for bad in ('', 'unofficial', 'io.github', 'IO.github.flydge', 'io..github.flydge', 'io.github.-flydge',
                    'io.github.flydge-', 'io.github.fly_dge', 'io.github.fly dge', 'io.github.fly"dge',
                    '1o.github.flydge', 'io.github.' + 'a' * 100, 7):
            with self.subTest(bad=bad):
                self.assertEqual(codes(codesign_id_prefix=bad), 'codesign_prefix')
        for good in ('io.github.flydge.unofficial-telegram-mcp', 'com.example.product', 'org.a1.b2.c3'):
            with self.subTest(good=good):
                check_names(codesign_id_prefix=good)

    def test_server_plugin_and_marketplace_names_match_the_slug_grammar(self):
        for field in ('mcp_server_name', 'plugin_name', 'plugin_server_name', 'marketplace_name'):
            for bad in ('', 'Telegram_search', '1tg', 'tg server', 'tg.x', '-tg', 'tg"', 'tg\n'):
                with self.subTest(field=field, bad=bad):
                    self.assertEqual(codes(**{field: bad}), 'name_grammar')
            check_names(**{field: 'ok-1'})

    def test_display_strings_cannot_break_the_applescript_dialog_title(self):
        for field in ('display_name', 'device_model', 'dmg_volume_name'):
            for bad in ('', ' lead', 'trail ', 'Say "hi"', 'back\\slash', 'line\nbreak', 'café', 'x' * 65):
                with self.subTest(field=field, bad=bad):
                    self.assertEqual(codes(**{field: bad}), 'display_grammar')
        self.assertEqual(codes(dmg_volume_name='x' * 28), 'display_grammar')
        check_names(dmg_volume_name='x' * 27)
        self.assertEqual(codes(bundle_root_name='Telegram Search'), 'bundle_root_grammar')

    def test_names_are_read_at_call_time(self):
        with patch.object(product_identity, 'PLUGIN_NAME', 'telegram'):
            self.assertEqual(codes(), 'plugin_name_reserved')
        check_names()

    def test_identity_error_carries_only_a_fixed_code(self):
        with self.assertRaises(IdentityError) as caught:
            check_names(plugin_name='telegram')
        self.assertEqual((str(caught.exception), caught.exception.code), ('plugin_name_reserved',) * 2)


class RenameImpactTests(unittest.TestCase):
    def sources(self):
        files = sorted(path for path in SOURCE.rglob('*.py') if '__pycache__' not in path.parts)
        self.assertIn(SOURCE / 'product_identity.py', files)
        return files

    def test_legacy_literals_appear_only_on_the_explicit_allowlist(self):
        for literal, allowed in LITERALS.items():
            found = {str(path.relative_to(SOURCE)) for path in self.sources() if literal in path.read_text('utf-8')}
            self.assertEqual(found - allowed, set(), literal)
            self.assertIn('product_identity.py', found, literal)

    def test_mcp_server_name_literal_appears_only_in_product_identity(self):
        found = {path.name for path in self.sources() if SERVER_LITERAL.search(path.read_text('utf-8'))}
        self.assertEqual(found, {'product_identity.py'})

    def test_no_source_hardcodes_a_device_model(self):
        pattern = re.compile(r'''device_model['"]?\s*[:=]\s*['"]''')
        self.assertEqual([path.name for path in self.sources() if pattern.search(path.read_text('utf-8'))], [])

    def test_constants_are_read_through_the_module_so_one_edit_renames_everything(self):
        pattern = re.compile(r'from\s+(?:telegram_search_mcp)?\.?\s*product_identity\s+import')
        self.assertEqual([path.name for path in self.sources() if pattern.search(path.read_text('utf-8'))], [])

    def test_registrations_use_the_identity_server_name(self):
        self.assertEqual(codex_registration.SERVER_NAME, product_identity.MCP_SERVER_NAME)
        self.assertEqual(claude_registration.SERVER_NAME, product_identity.MCP_SERVER_NAME)
        self.assertEqual(product_identity.MCP_SERVER_NAME, 'telegram_search')

    def test_internal_names_keep_their_values(self):
        self.assertEqual(product_identity.DISPLAY_NAME, 'Unofficial Telegram MCP')
        self.assertEqual((product_identity.PLUGIN_NAME, product_identity.PLUGIN_SERVER_NAME,
                          product_identity.MARKETPLACE_NAME, product_identity.CODESIGN_ID_PREFIX,
                          product_identity.BUNDLE_ROOT_NAME, product_identity.DMG_VOLUME_NAME,
                          product_identity.DEVICE_MODEL),
                         ('telegram-local', 'tg', 'unofficial-telegram-mcp',
                          'io.github.flydge.unofficial-telegram-mcp', 'TelegramSearch',
                          'Unofficial Telegram MCP', 'Unofficial Telegram MCP'))

    def test_schema_fingerprint_is_unchanged(self):
        self.assertEqual(schema_fingerprint(), FINGERPRINT_BBADC84)


class DisplayNameFollowsIdentityTests(unittest.TestCase):
    ARGUMENTS = dict(recipient_title='Synthetic Recipient', recipient=123, display_name='report.txt',
                     size_bytes=14, sha256='a' * 64, caption='Reviewed')

    def shown(self, **extra):
        done = subprocess.CompletedProcess([], 0, b'APPROVED\n')
        with patch('telegram_search_mcp.approval_prompt.subprocess.run', return_value=done) as run:
            self.assertTrue(confirm_approved_send(**self.ARGUMENTS, **extra))
        argv = run.call_args.args[0]
        return argv[argv.index('-e') + 1], argv[-1]

    def test_approval_dialog_title_and_body_follow_the_display_name(self):
        script, body = self.shown()
        self.assertIn('with title "Unofficial Telegram MCP — approve send"', script)
        self.assertTrue(body.endswith('Unofficial Telegram MCP will make one upload/send attempt.'))
        with patch.object(product_identity, 'DISPLAY_NAME', RENAMED):
            script, body = self.shown()
            self.assertIn(f'with title "{RENAMED} — approve send"', script)
            self.assertNotIn('Unofficial', script)
            self.assertTrue(body.endswith(f'{RENAMED} will make one upload/send attempt.'))
            self.assertIn(f'with title "{RENAMED} — approve change"', _dialog_script(_CHANGE_SCRIPT))
            self.assertNotIn('@@NAME@@', _dialog_script(_SCRIPT))

    def test_connect_page_title_follows_the_display_name(self):
        self.addCleanup(importlib.reload, connect_page)  # Runs after the patch below is undone.
        patcher = patch.object(product_identity, 'DISPLAY_NAME', RENAMED)
        patcher.start()
        self.addCleanup(patcher.stop)
        importlib.reload(connect_page)
        page = connect_page.ASSETS['/'][1].decode('utf-8')
        self.assertIn(f'<title>{RENAMED}</title>', page)
        self.assertEqual(connect_page.STRINGS['en']['title'], RENAMED)
        self.assertEqual(connect_page.STRINGS['ru']['title'], RENAMED)
        self.assertIn(f'"title": "{RENAMED}"', connect_page.APP_JS)
        self.assertNotIn('Unofficial Telegram MCP', page + connect_page.APP_JS)

    def test_connect_page_title_is_the_real_name_without_a_patch(self):
        self.assertIn('<title>Unofficial Telegram MCP</title>', connect_page.INDEX_HTML)


class StubRaw:
    """Answers each request type from its script, else ok; keeps what was sent."""

    def __init__(self, replies=None):
        self.sent, self.pending = [], deque()
        self.replies = {kind: deque(rows) for kind, rows in (replies or {}).items()}

    def send(self, payload):
        self.sent.append(payload)
        script = self.replies.get(payload['@type'])
        reply = script.popleft() if script else {'@type': 'ok'}
        self.pending.append({**reply, '@extra': payload['@extra']})

    def receive(self, timeout):
        return self.pending.popleft() if self.pending else None

    def close(self):
        pass


class DeviceIdentityTests(unittest.TestCase):
    def test_tdjson_parameters_use_the_device_model_and_the_package_version(self):
        raw = StubRaw({'getAuthorizationState': [{'@type': 'authorizationStateWaitTdlibParameters'},
                                                 {'@type': 'authorizationStateWaitEncryptionKey'},
                                                 {'@type': 'authorizationStateReady'}]})
        with tempfile.TemporaryDirectory() as parent:
            client = TDLibClient(raw=raw, session_directory=Path(parent) / 'tdlib',
                                 credential_loader=lambda: ApiCredentials(api_id=123456, api_hash='test-hash'))
            client.ensure_ready()
        parameters = next(row for row in raw.sent if row['@type'] == 'setTdlibParameters')
        self.assertEqual(parameters['device_model'], product_identity.DEVICE_MODEL)
        self.assertEqual(parameters['application_version'], __version__)
        self.assertNotIn('0.1.0', parameters.values())

    def test_auth_transport_and_tdjson_send_the_same_identity(self):
        raw = StubRaw()
        AuthTransport(raw).set_parameters(ApiCredentials(api_id=123456, api_hash='test-hash'),
                                          SecretValue(b'k' * 32), database_directory='/db', files_directory='/files',
                                          application_version=__version__)
        parameters = raw.sent[0]
        self.assertEqual(parameters['@type'], 'setTdlibParameters')
        self.assertEqual(parameters['device_model'], product_identity.DEVICE_MODEL)
        self.assertEqual(parameters['application_version'], __version__)

    def test_device_model_is_the_display_name_not_the_internal_directory_name(self):
        self.assertEqual(product_identity.DEVICE_MODEL, 'Unofficial Telegram MCP')
        self.assertNotEqual(product_identity.DEVICE_MODEL, 'TelegramSearchMCP')


if __name__ == '__main__':
    unittest.main()
