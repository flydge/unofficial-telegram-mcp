"""0.55 phase D (spec 3D, criteria 2 and 3): client registrations take an optional server name.

The default alias's entries are the literal bytes 0.54.0rc1 (c6338a0) produced for the same inputs: the Codex
table, its receipt and rollback, the Claude Code argv and payload, the Claude Desktop subtree, receipt and
edits. A named alias's entries carry `telegram_search_<alias>` and the launcher argv `mcp --account <alias>`;
its Codex entry is never required; every other server, the default's included, is a neighbour kept byte for
byte. Disposable homes and fake runners only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import tomlkit

from telegram_search_mcp import claude_desktop_registration as desktop, claude_registration as claude
from telegram_search_mcp import codex_registration as codex
from telegram_search_mcp.config import alias_server_name

RELEASE = '/fixture/TelegramSearch/releases/' + 'a' * 64
COMMAND = RELEASE + '/python/bin/python3'
ARGS = ('-I', '-S', '-B', RELEASE + '/launcher.py', 'mcp')
WORK = ARGS + ('--account', 'work')
TOOLS = ('_manifest', 'search_correspondence')
NEIGHBOUR = b'# preserved neighbor\nmodel = "example"\n'
INLINE = b'mcp_servers = { weather = { command = "/opt/w" } }\n'
DESKTOP = '{\n  "globalShortcut": "Alt+Space",\n  "mcpServers": {\n    "weather": {"command": "/opt/w", "args": []}\n  }\n}\n'
PAYLOAD = ('{"type":"stdio","command":"%s","args":["-I","-S","-B","%s/launcher.py","mcp"]}' % (COMMAND, RELEASE))

# What c6338a0 (0.54.0rc1) produced for exactly these inputs.
GOLDEN_054 = {
    'codex_candidate': '356c7b1903e5ff954f667caac41d54ed1a571a42309aa439e1dcba3d19dab430',
    'codex_inline_candidate': 'd8d87eb60db9d53a3ce59bf1c4be5af219301a0739fb39cb8a9120149b6a1c72',
    'codex_owned': 'ec06b8fb622844e6ab2da1915cfb926c80f1b41cab4ebb1f774391b6d072b337',
    'codex_post_subtree': ('[mcp_servers.telegram_search]\ncommand = "%s"\nargs = ["-I", "-S", "-B", "%s/launcher.py", '
                           '"mcp"]\nenabled = true\nrequired = true\nstartup_timeout_sec = 30\ntool_timeout_sec = 600\n'
                           'enabled_tools = ["_manifest", "search_correspondence"]\n' % (COMMAND, RELEASE)),
    'codex_receipt': ('{"client":"codex","installation_id":"install_bbbbbbbbbbbbbbbbbbbbbbbb","post_subtree_hash":'
                      '"ec06b8fb622844e6ab2da1915cfb926c80f1b41cab4ebb1f774391b6d072b337","pre_subtree":null,'
                      '"pre_subtree_hash":null,"schema_version":1,"scope":"user","transaction_id":"first_install"}'),
    'codex_rollback': 'fe26001bcaf4543adef995351fbf4418cdfaaa5cdae07937c6f20f9cb4a435c5',
    'claude_payload': PAYLOAD,
    'claude_manual': ['claude', 'mcp', 'add-json', '--scope', 'user', 'telegram_search', PAYLOAD],
    'claude_calls': [['mcp', 'add-json', '--scope', 'user', 'telegram_search', PAYLOAD],
                     ['mcp', 'remove', '--scope', 'user', 'telegram_search']],
    'desktop_manual': {'mcpServers': {'telegram_search': {'command': COMMAND, 'args': list(ARGS)}}},
    'desktop_subtree': ('{"mcpServers":{"telegram_search":{"args":["-I","-S","-B","%s/launcher.py","mcp"],'
                        '"command":"%s"}}}' % (RELEASE, COMMAND)),
    'desktop_receipt': ('{"client":"claude-desktop","installation_id":"install_bbbbbbbbbbbbbbbbbbbbbbbb",'
                        '"post_subtree_hash":"9002e76ed701f10825edf3bc05160764f55c3558e75b43942eb8668ba30c2697",'
                        '"pre_subtree":null,"pre_subtree_hash":null,"schema_version":1,"scope":"user",'
                        '"transaction_id":"first_install"}'),
    'desktop_observe': ['absent', '514f25f100b8c0a54c14bbf034a2e03c3e393a1f466e3322173d284c8c6f67a8'],
    'desktop_removal': ['exact', 'ee9c0a4f35e22f3e0095cc385de1420a393d6076f4db567ce73c91039f28bd79'],
}
# The schema fingerprint c6338a0 computed. 0.55 left it unchanged (spec 2, criterion 2; verified at v0.55.0rc1).
SCHEMA_FINGERPRINT_054 = 'f239102a142943c7f5ff3940a10855658300aa8efe7f5ff43f510ca37ca0e87b'
# Every later slice that changes a tool schema lists the change in tests/fixtures/schema_changes/ and moves this
# pin with it: 0.56 (0.56-albums, 0.56-album-tools), 0.57 part 1 (0.57-capabilities, 0.57-caption-tools), 0.57 part 2
# (0.57-styled-text-edit). Accounts themselves never enter the fingerprint.
SCHEMA_FINGERPRINT = '8eb3eaf4fe24486be1194bbf660d15c092f415fa6fb3193429ce9b1884547a70'
SCHEMA_CHANGES_SINCE_055 = ('0.56-album-tools', '0.56-albums', '0.57-capabilities', '0.57-caption-tools',
                            '0.57-styled-text-edit')


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        return subprocess.CompletedProcess(argv, 0)


class Home:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def desktop(self, text=DESKTOP):
        desktop.directory(self.home).mkdir(parents=True, exist_ok=True)
        desktop.config_path(self.home).write_text(text)
        os.chmod(desktop.config_path(self.home), 0o644)

    def write_desktop(self, data):
        desktop.config_path(self.home).write_bytes(data)


class DefaultGoldenTests(Home, unittest.TestCase):
    """Every default call is byte-identical to 0.54 (criterion 2)."""

    def test_codex_entry_receipt_and_rollback(self):
        registration = codex.Registration(COMMAND, ARGS, {}, TOOLS)
        change = codex.merge_registration(NEIGHBOUR, registration)
        self.assertEqual(digest(change.candidate), GOLDEN_054['codex_candidate'])
        self.assertEqual(change.post_subtree.decode(), GOLDEN_054['codex_post_subtree'])
        self.assertEqual(digest(codex.merge_registration(INLINE, registration).candidate),
                         GOLDEN_054['codex_inline_candidate'])
        receipt = codex.Receipt('install_' + 'b' * 24, 'first_install', None, change.post_subtree_hash, None)
        self.assertEqual(receipt.to_bytes().decode(), GOLDEN_054['codex_receipt'])
        self.assertEqual(digest(codex.rollback_registration(change.candidate, receipt).candidate),
                         GOLDEN_054['codex_rollback'])
        self.assertEqual(digest(codex.owned_subtree(change.candidate)), GOLDEN_054['codex_owned'])
        self.assertEqual(registration.server_name, 'telegram_search')

    def test_claude_code_argv(self):
        self.assertEqual(claude.payload(COMMAND, ARGS), GOLDEN_054['claude_payload'])
        self.assertEqual(claude.manual_command(COMMAND, ARGS), GOLDEN_054['claude_manual'])
        runner = Runner()
        claude.add(runner, Path('/fixture/bin/claude'), Path('/fixture/home'), COMMAND, ARGS)
        claude.remove(runner, Path('/fixture/bin/claude'), Path('/fixture/home'))
        self.assertEqual(runner.calls, GOLDEN_054['claude_calls'])

    def test_claude_desktop_subtree_receipt_and_edits(self):
        self.assertEqual(desktop.manual_entry(COMMAND, ARGS), GOLDEN_054['desktop_manual'])
        self.assertEqual(desktop.subtree(COMMAND, ARGS).decode(), GOLDEN_054['desktop_subtree'])
        self.assertEqual(desktop.receipt('install_' + 'b' * 24, 'first_install', COMMAND, ARGS).to_bytes().decode(),
                         GOLDEN_054['desktop_receipt'])
        self.desktop()
        state, plan = desktop.observe(self.home, COMMAND, ARGS)
        self.assertEqual([state, digest(plan.candidate)], GOLDEN_054['desktop_observe'])
        self.write_desktop(plan.candidate)
        state, plan = desktop.observe_removal(self.home, COMMAND, ARGS)
        self.assertEqual([state, digest(plan.candidate)], GOLDEN_054['desktop_removal'])

    def test_the_schema_fingerprint_moves_only_with_listed_changes_and_the_default_policy_is_054s(self):
        from telegram_search_mcp import __version__
        from telegram_search_mcp.config import AccountPaths
        from telegram_search_mcp.contract import CONTRACT_VERSION, schema_fingerprint
        from telegram_search_mcp.enrollment import SetupPaths
        from telegram_search_mcp.setup_wizard import _ensure_policy
        listed = tuple(sorted(path.stem for path in (Path(__file__).parent / 'fixtures' / 'schema_changes').glob('*.json')
                              if not path.stem.startswith(('0.54-', '0.55-'))))
        self.assertEqual(listed, SCHEMA_CHANGES_SINCE_055)  # a new slice's schema change moves the pin below
        self.assertEqual((schema_fingerprint(), CONTRACT_VERSION),
                         (SCHEMA_FINGERPRINT_054 if not listed else SCHEMA_FINGERPRINT, 1))
        root = self.home / 'TelegramSearchMCP'
        root.mkdir(mode=0o700)
        _ensure_policy(SetupPaths.from_account(AccountPaths.for_alias('default', self.home).relocated(root)))
        self.assertEqual((root / 'runtime.toml').read_bytes(),
                         ('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                          'enabled_capabilities = ["read", "artifacts"]\n' % __version__).encode())

    def test_no_registration_module_binds_a_server_name_at_import(self):
        for module in (codex, claude, desktop):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, 'SERVER_NAME'))


class CodexAliasTests(unittest.TestCase):
    def registration(self, args=WORK, required=False):
        return codex.Registration(COMMAND, args, {}, TOOLS, required=required)

    def test_an_alias_entry_is_its_own_table_and_never_required(self):
        work = self.registration()
        self.assertEqual(work.server_name, 'telegram_search_work')
        default = codex.merge_registration(NEIGHBOUR, codex.Registration(COMMAND, ARGS, {}, TOOLS))
        change = codex.merge_registration(default.candidate, work, name=alias_server_name('work'))
        self.assertTrue(change.candidate.startswith(default.candidate))
        self.assertIsNone(change.pre_subtree)
        self.assertTrue(change.post_subtree.startswith(b'[mcp_servers.telegram_search_work]\n'))
        values = tomlkit.parse(change.candidate.decode())['mcp_servers']
        self.assertEqual(values['telegram_search_work']['args'], list(WORK))
        self.assertIs(values['telegram_search_work']['required'], False)
        self.assertEqual(codex.owned_subtree(change.candidate), default.post_subtree)  # The default's is unchanged.
        self.assertEqual(codex.owned_subtree(change.candidate, 'telegram_search_work'), change.post_subtree)
        # Each is the other's neighbour: the default's upgrade and the alias's removal keep the other byte for byte.
        receipt = codex.Receipt('install_' + 'b' * 24, 'account_1', None, change.post_subtree_hash, None)
        back = codex.rollback_registration(change.candidate, receipt, name='telegram_search_work')
        self.assertEqual(back.candidate, default.candidate + b'\n')  # Only the blank separator the add wrote stays.
        other = codex.merge_registration(change.candidate, codex.Registration(COMMAND.replace('a' * 64, 'c' * 64),
                                         tuple(a.replace('a' * 64, 'c' * 64) for a in ARGS), {}, TOOLS),
                                         default.ownership or codex.Receipt('install_' + 'b' * 24, 'first_install',
                                                                            None, default.post_subtree_hash, None))
        self.assertEqual(codex.owned_subtree(other.candidate, 'telegram_search_work'), change.post_subtree)

    def test_an_alias_entry_and_its_name_must_agree(self):
        work = self.registration()
        for name in (None, 'telegram_search', 'telegram_search_play', 'telegram_search_default', 'other_work',
                     'telegram_search_Work', 1):
            with self.subTest(name=name), self.assertRaises(codex.ConfigConflict) as caught:
                codex.merge_registration(NEIGHBOUR, work, name=name)
            self.assertEqual(caught.exception.code, 'invalid_registration')
        with self.assertRaises(codex.ConfigConflict):
            codex.merge_registration(NEIGHBOUR, codex.Registration(COMMAND, ARGS, {}, TOOLS), name='telegram_search_work')
        with self.assertRaises(codex.ConfigConflict):
            codex.adopt_registration(NEIGHBOUR + b'[mcp_servers.telegram_search]\ncommand = "/x"\n', work)

    def test_the_alias_argv_is_closed(self):
        for args, required in ((WORK, True), (ARGS + ('--account', 'default'), False), (ARGS + ('--account', 'Work'), False),
                               (ARGS + ('--profile', 'work'), False), (ARGS + ('--account',), False),
                               (ARGS + ('--account', 'work', 'x'), False), (ARGS + ('--account', 'a' * 13), False)):
            with self.subTest(args=args[5:], required=required), self.assertRaises(codex.ConfigConflict):
                self.registration(args, required)

    def test_an_existing_alias_entry_is_not_ours_without_its_receipt(self):
        source = NEIGHBOUR + b'\n[mcp_servers.telegram_search_work]\ncommand = "/legacy"\n'
        with self.assertRaises(codex.ConfigConflict) as caught:
            codex.merge_registration(source, self.registration(), name='telegram_search_work')
        self.assertEqual(caught.exception.code, 'ownership_conflict')
        receipt = codex.Receipt('install_' + 'b' * 24, 'account_1', None, 'f' * 64, None)
        with self.assertRaises(codex.ConfigConflict):
            codex.rollback_registration(source, receipt, name='telegram_search_work')


class ClaudeCodeAliasTests(Home, unittest.TestCase):
    def document(self, servers):
        path = claude.config_path(self.home)
        path.write_text(json.dumps({'numStartups': 1, 'mcpServers': servers}))
        path.chmod(0o600)

    def test_the_alias_entry_is_its_own_user_scope_name(self):
        runner = Runner()
        claude.add(runner, Path('/fixture/bin/claude'), self.home, COMMAND, WORK, 'telegram_search_work')
        claude.remove(runner, Path('/fixture/bin/claude'), self.home, 'telegram_search_work')
        payload = json.dumps({'type': 'stdio', 'command': COMMAND, 'args': list(WORK)}, separators=(',', ':'))
        self.assertEqual(runner.calls, [['mcp', 'add-json', '--scope', 'user', 'telegram_search_work', payload],
                                        ['mcp', 'remove', '--scope', 'user', 'telegram_search_work']])
        self.assertEqual(claude.manual_command(COMMAND, WORK, 'telegram_search_work')[5], 'telegram_search_work')

    def test_state_reads_only_the_named_entry(self):
        default = {'type': 'stdio', 'command': COMMAND, 'args': list(ARGS)}
        self.document({'telegram_search': default})
        self.assertEqual(claude.state(self.home, COMMAND, WORK, 'telegram_search_work'), 'absent')
        self.assertEqual(claude.state(self.home, COMMAND, ARGS), 'exact')
        self.document({'telegram_search': default, 'telegram_search_work': {**default, 'args': list(WORK)}})
        self.assertEqual(claude.state(self.home, COMMAND, WORK, 'telegram_search_work'), 'exact')
        self.assertEqual(claude.state(self.home, COMMAND, ARGS, 'telegram_search_work'), 'different')
        # Plugin mode's duplicate note concerns the default's user entry only.
        self.document({'telegram_search_work': {**default, 'args': list(WORK)}})
        self.assertEqual(claude.present(self.home), 'absent')

    def test_a_name_outside_the_grammar_refuses(self):
        for name in ('telegram_search_default', 'telegram', 'telegram_search_Work', 'telegram_search_'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                claude.add(Runner(), Path('/fixture/bin/claude'), self.home, COMMAND, WORK, name)


class ClaudeDesktopAliasTests(Home, unittest.TestCase):
    NAME = 'telegram_search_work'

    def test_the_alias_entry_is_a_neighbour_of_the_default(self):
        self.desktop()
        _, plan = desktop.observe(self.home, COMMAND, ARGS)
        self.write_desktop(plan.candidate)
        state, plan = desktop.observe(self.home, COMMAND, WORK, self.NAME)
        self.assertEqual(state, 'absent')
        self.write_desktop(plan.candidate)
        servers = json.loads(plan.candidate)['mcpServers']
        self.assertEqual(list(servers), ['weather', 'telegram_search', self.NAME])
        self.assertEqual(servers[self.NAME], {'command': COMMAND, 'args': list(WORK)})
        self.assertEqual(desktop.state(self.home, COMMAND, ARGS), 'exact')
        self.assertEqual(desktop.state(self.home, COMMAND, WORK, self.NAME), 'exact')
        # The default's upgrade keeps the alias entry; the alias's removal keeps the default's.
        newer = COMMAND.replace('a' * 64, 'c' * 64), tuple(a.replace('a' * 64, 'c' * 64) for a in ARGS)
        state, plan = desktop.observe_change(self.home, (COMMAND, ARGS), *newer)
        self.assertEqual(json.loads(plan.candidate)['mcpServers'][self.NAME], servers[self.NAME])
        state, plan = desktop.observe_removal(self.home, COMMAND, WORK, self.NAME)
        self.assertEqual(state, 'exact')
        self.assertEqual(list(json.loads(plan.candidate)['mcpServers']), ['weather', 'telegram_search'])

    def test_subtree_and_receipt_bind_the_name(self):
        default, work = desktop.subtree(COMMAND, ARGS), desktop.subtree(COMMAND, WORK, self.NAME)
        self.assertEqual(json.loads(work), {'mcpServers': {self.NAME: {'command': COMMAND, 'args': list(WORK)}}})
        owned = desktop.receipt('install_' + 'b' * 24, 'account_1', COMMAND, WORK, self.NAME)
        self.assertEqual(owned.post_subtree_hash, digest(work))
        self.assertNotEqual(owned.post_subtree_hash, digest(default))
        self.assertEqual(desktop.manual_entry(COMMAND, WORK, self.NAME), json.loads(work))

    def test_a_different_alias_entry_is_never_ours(self):
        self.desktop(DESKTOP.replace('"weather"', '"%s"' % self.NAME))
        self.assertEqual(desktop.observe(self.home, COMMAND, WORK, self.NAME), ('different', None))
        self.assertEqual(desktop.observe_removal(self.home, COMMAND, WORK, self.NAME), ('different', None))
        self.assertEqual(desktop.observe(self.home, COMMAND, ARGS)[0], 'absent')


if __name__ == '__main__':
    unittest.main()
