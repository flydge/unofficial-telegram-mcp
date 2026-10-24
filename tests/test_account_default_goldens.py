"""0.55 criterion 2: the default alias stays byte-identical to 0.54 through the installer.

Every literal here is what v0.54.0rc1 (c6338a0) produced for the same inputs, computed by running that commit's
own code (its `bundle_install`, `bundle_runtime` and test harness), never recomputed by the code under test: the
five-element `mcp` argv, the broker plist `_service_payload` writes, `.mcp.json`, the receipt.json key sets, the
runtime.toml version-line rewrite, and the journal shapes (top-level keys, `state` keys and record names) of a
first install, an upgrade, a rollback and an uninstall; only the rewritten version line is the target release's, the
version the change fixture builds with (critic fix 055f). The client entries, receipts, the runtime.toml template and
the schema fingerprint are pinned in tests/test_account_registrations.py; paths and labels in
tests/test_account_paths.py. With named aliases present, the default's part of a change is the same bytes as
without them. Disposable homes and fakes only.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_account_changes import AliasChangeHarness
from test_bundle_kill_sweep import FixedDirectory
from test_bundle_upgrade import ChangeHarness
from telegram_search_mcp import bundle_install, bundle_runtime

ROOT = Path(__file__).resolve().parents[1]
RELEASE = '/fixture/TelegramSearch/releases/' + 'a' * 64
COMMANDS_054 = (RELEASE + '/python/bin/python3', ['-I', '-S', '-B', RELEASE + '/launcher.py', 'mcp'])
RECEIPT_V1_054 = ['claude_code', 'claude_code_mode', 'claude_desktop', 'claude_skills', 'claude_skills_root',
                  'codex_home', 'index_sha256', 'installation_root', 'package_version', 'publication_objects',
                  'registration_sha256', 'release_root', 'schema_version', 'skills', 'skills_root', 'status', 'user_home']
RECEIPT_V2_054 = sorted(RECEIPT_V1_054 + ['adopted', 'claude_desktop_ownership', 'previous', 'transaction'])
MCP_JSON_054 = ('{\n  "mcpServers": {\n    "tg": {\n      "command": "/bin/sh",\n'
                '      "args": ["-p", "${CLAUDE_PLUGIN_ROOT}/bin/mcp-entry"]\n    }\n  }\n}\n')
MCP_JSON_SHA256_054 = '06ef85328b9708803b1bfd93ff79d10a3ccf0706db1c9c1bf4530d317c21d3ab'
POLICY = (b'config_version = 1\nexpected_package_version = "0.52.9"\nexpected_contract_version = 1\n'
          b'enabled_capabilities = ["read", "artifacts", "send"]\n')
# The version line 0.54's rewrite writes is the target release's: the version the change fixture builds with (its
# `build` default, which follows the package version at every release bump), never a literal of its own (critic
# fix 055f), so a version bump leaves this golden's shape to compare.
BUILD_VERSION = inspect.signature(ChangeHarness.build).parameters['version'].default
POLICY_UPGRADED_054 = (b'config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                       b'enabled_capabilities = ["read", "artifacts", "send"]\n' % BUILD_VERSION.encode())
SERVICE_PLIST_054 = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n\t<key>EnvironmentVariables</key>\n'
    '\t<dict/>\n\t<key>KeepAlive</key>\n\t<true/>\n\t<key>Label</key>\n'
    '\t<string>com.sample-owner.telegram-search-mcp.broker</string>\n\t<key>LimitLoadToSessionType</key>\n'
    '\t<string>Aqua</string>\n\t<key>ProcessType</key>\n\t<string>Background</string>\n\t<key>ProgramArguments</key>\n'
    '\t<array>\n\t\t<string><release>/python/bin/python3</string>\n\t\t<string>-I</string>\n\t\t<string>-S</string>\n'
    '\t\t<string>-B</string>\n\t\t<string><release>/launcher.py</string>\n\t\t<string>broker</string>\n\t</array>\n'
    '\t<key>RunAtLoad</key>\n\t<true/>\n\t<key>StandardErrorPath</key>\n\t<string>/dev/null</string>\n'
    '\t<key>StandardOutPath</key>\n\t<string>/dev/null</string>\n\t<key>Umask</key>\n\t<integer>63</integer>\n</dict>\n'
    '</plist>\n')
STATE_054 = ['enrollment.json', 'runtime.toml', 'runtime.toml without enabled_capabilities']
_CHANGE_KEYS_054 = ['claude_code_mode', 'claude_skills_root', 'codex_home', 'config_existed', 'from_index_sha256',
                    'from_package_version', 'from_release_root', 'index_sha256', 'installation_root', 'kind',
                    'package_version', 'publications', 'release_root', 'schema_version', 'skills_root', 'stage',
                    'state', 'user_home']
JOURNALS_054 = {
    'install': {'keys': ['claude_code_mode', 'claude_skills_root', 'codex_home', 'config_existed', 'index_sha256',
                         'installation_root', 'package_version', 'publications', 'release_root', 'schema_version',
                         'skills_root', 'stage', 'user_home'],
                'records': ['claude-telegram-search', 'claude-telegram-setup', 'claude_add', 'claude_code',
                            'claude_desktop', 'claude_desktop_add', 'claude_desktop_write', 'claude_skills', 'pointer',
                            'receipt', 'registration', 'release', 'telegram-search', 'telegram-setup'],
                'state': None},
    'change_1': {'keys': _CHANGE_KEYS_054,
                 'records': ['claude-telegram-search', 'claude_add', 'claude_code', 'claude_desktop',
                             'claude_desktop_add', 'claude_desktop_write', 'claude_remove', 'plist', 'pointer', 'policy',
                             'policy_previous', 'receipt', 'registration', 'release', 'service', 'service_cycle',
                             'service_previous', 'service_restarted', 'telegram-search'],
                 'state': STATE_054},
    'change_2': {'keys': _CHANGE_KEYS_054,
                 'records': ['claude-telegram-search', 'claude_add', 'claude_code', 'claude_desktop',
                             'claude_desktop_add', 'claude_desktop_write', 'claude_remove', 'plist', 'pointer', 'policy',
                             'policy_previous', 'receipt', 'registration', 'service', 'service_cycle',
                             'service_previous', 'service_restarted', 'telegram-search'],
                 'state': STATE_054},
    'uninstall': {'keys': ['index_sha256', 'installation_root', 'kind', 'publications', 'release_root',
                           'schema_version', 'stage', 'user_home'],
                  'records': ['claude_code', 'claude_desktop', 'claude_desktop_add', 'claude_desktop_write',
                              'claude_remove', 'codex', 'registration', 'service', 'service_owned', 'skills'],
                  'state': None},
}


def shape(path):
    value = json.loads(path.read_bytes())
    return {'keys': sorted(value), 'state': sorted(value['state']) if 'state' in value else None,
            'records': sorted(value['publications'])}


class InstallerGoldenTests(unittest.TestCase):
    def test_the_mcp_argv_the_service_plist_and_the_plugin_entry_are_054s(self):
        command, arguments = bundle_install._commands(Path(RELEASE))
        self.assertEqual((command, arguments), COMMANDS_054)
        self.assertEqual(len(arguments), 5)
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve() / 'release'
            (release / 'python/bin').mkdir(parents=True)
            (release / 'python/bin/python3').write_bytes(b'inert')
            data = bundle_install._plist_bytes(bundle_install._service_payload(
                release, 'com.sample-owner.telegram-search-mcp.broker'))
        self.assertEqual(data.decode().replace(str(release), '<release>'), SERVICE_PLIST_054)
        mcp = (ROOT / 'plugins' / 'telegram-local' / '.mcp.json').read_bytes()
        self.assertEqual((mcp.decode(), hashlib.sha256(mcp).hexdigest()), (MCP_JSON_054, MCP_JSON_SHA256_054))

    def test_the_receipt_keys_and_the_version_line_rewrite_are_054s(self):
        self.assertEqual({k: sorted(v) for k, v in bundle_runtime.RECEIPT_KEYS.items()},
                         {1: RECEIPT_V1_054, 2: RECEIPT_V2_054})
        self.assertEqual(bundle_install._policy_candidate(POLICY, '0.52.9', BUILD_VERSION), POLICY_UPGRADED_054)


class JournalGoldenTests(ChangeHarness, unittest.TestCase):
    """Without an alias every journal, receipt.json and runtime.toml as installed are 0.54's."""

    def test_install_upgrade_and_rollback_journals_receipts_and_runtime_toml_are_054s(self):
        self.full()
        self.assertEqual(sorted(self.receipt()), RECEIPT_V2_054)
        self.install(self.build('b', launcher=b'# second release\n', search=b'search-skill-2'))
        self.assertEqual(sorted(self.receipt()), RECEIPT_V2_054)
        self.assertEqual((self.session_root / 'runtime.toml').read_bytes(), POLICY_UPGRADED_054)
        self.claude()
        self.change('rollback')
        journals = {('install' if p.parent.name.startswith('install_') else p.parent.name): shape(p)
                    for p in sorted((self.runtime / 'transactions').glob('*/journal.json'))}
        self.assertEqual(journals, {k: v for k, v in JOURNALS_054.items() if k != 'uninstall'})
        self.assertEqual(sorted(bundle_install._state(self.session_root)), STATE_054)
        self.assertEqual((self.session_root / 'runtime.toml').read_bytes(), POLICY)

    def test_the_uninstall_journal_is_054s(self):
        self.full()
        self.claude()
        seen = {}

        def capture(stage):
            if stage == 'retired':
                seen['uninstall'] = shape(next((self.runtime / 'transactions').glob('change_*/journal.json')))
        self.change('uninstall', checkpoint=capture)
        self.assertEqual(seen, {'uninstall': JOURNALS_054['uninstall']})


def normal(value, base):
    """`value` with the fixture base and every binding's inode left out: what two fixtures can compare."""
    if type(value) is dict:
        return {k: normal(v, base) for k, v in value.items() if k not in ('dev', 'ino')}
    if type(value) is list:
        return [normal(v, base) for v in value]
    return value.replace(base, '<base>') if type(value) is str else value


class DefaultBesideAliasesTests(AliasChangeHarness, unittest.TestCase):
    """With named aliases present, an upgrade and an uninstall do to the default exactly what they do without."""

    def default_state(self):
        config = (self.codex / 'config.toml').read_bytes()
        journal = self.runtime / 'transactions' / 'change_1' / 'journal.json'
        value = json.loads(journal.read_bytes())
        default_records = {k: v for k, v in value['publications'].items() if ':' not in k and k != 'accounts_sidecar'}
        return normal({'plist': self.plist.read_bytes().decode(), 'loaded': self.launchctl.loaded,
                       'codex': bundle_install.owned_subtree(config).decode(), 'claude': self.claude().entry,
                       'desktop': self.desktop_servers().get('telegram_search'), 'receipt': self.receipt(),
                       'pointer': self.pointer(), 'policy': (self.session_root / 'runtime.toml').read_bytes().decode(),
                       'journal': {k: v for k, v in value.items() if k not in ('publications', 'accounts')},
                       'records': sorted(default_records), 'result': self.result}, str(self.base))

    def test_an_upgrade_beside_aliases_leaves_the_default_as_without_them(self):
        """Both runs at one fixed base, so even the hashes that bind a path (receipts, ownership) compare."""
        states = []
        parent = Path(tempfile.mkdtemp(prefix='tgmcp-default-golden-')).resolve()
        self.addCleanup(shutil.rmtree, parent, True)
        for aliases in ((), ('work', 'play')):
            with self.subTest(aliases=aliases):
                with mock.patch('tempfile.TemporaryDirectory', lambda: FixedDirectory(parent / 'base')):
                    self.setUp()
                self._claude_base = None  # The fake CLI is rebuilt in the fresh home at the same path.
                self.full()
                self.claude()
                for alias in aliases:
                    self.account('account_add', alias)
                if aliases:
                    self.alias_session()
                self.result = {k: v for k, v in self.install(self.newer()).items() if k != 'accounts'}
                states.append(self.default_state())
        self.maxDiff = None
        self.assertEqual(states[0], states[1])


    def test_an_uninstall_beside_aliases_does_to_the_default_what_it_does_without_them(self):
        results = []
        parent = Path(tempfile.mkdtemp(prefix='tgmcp-default-golden-')).resolve()
        self.addCleanup(shutil.rmtree, parent, True)
        for aliases in ((), ('work', 'play')):
            with self.subTest(aliases=aliases):
                with mock.patch('tempfile.TemporaryDirectory', lambda: FixedDirectory(parent / 'base')):
                    self.setUp()
                self._claude_base = None
                self.full()
                self.claude()
                for alias in aliases:
                    self.account('account_add', alias)
                if aliases:
                    self.alias_session()
                result = {k: v for k, v in self.change('uninstall').items() if k not in ('accounts', 'account_registry')}
                config = (self.codex / 'config.toml').read_bytes()
                results.append(normal({'result': result, 'loaded': self.launchctl.loaded, 'plist': self.plist.exists(),
                                       'codex': bundle_install.owned_subtree(config), 'claude': self.claude().entry,
                                       'desktop': self.desktop_servers().get('telegram_search'),
                                       'session': sorted(str(p.relative_to(self.session_root))
                                                         for p in self.session_root.rglob('*'))}, str(self.base)))
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
