"""`--adopt-legacy`: foreign entries and a foreign broker plist are replaced only after an exact backup
is recorded (CLIENTS-13 migration), never by default. Disposable homes and fakes only.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from claude_fake import entry
from test_bundle_upgrade import ChangeHarness, NEIGHBOUR_CODEX, NEIGHBOUR_DESKTOP, WRITE_AHEAD
from telegram_search_mcp import claude_desktop_registration as desktop, codex_registration

LEGACY_PYTHON = '/Users/example/legacy-runtime/venv/bin/python3'
LEGACY_CODEX = b'\n[mcp_servers.telegram_search]\ncommand = "%s"\nargs = ["-m", "telegram_search_mcp.server"]\n' % LEGACY_PYTHON.encode()
LEGACY_ENTRY = {'command': LEGACY_PYTHON, 'args': ['-m', 'telegram_search_mcp.server'], 'env': {'TELEGRAM_SEARCH_PROFILE': 'legacy'}}


class AdoptionHarness(ChangeHarness):

    def legacy(self, *, loaded=True, pin='0.52.0'):
        """A 0.51/0.52-style install: foreign entries in every client and a loaded source-venv broker."""
        self.codex_present(NEIGHBOUR_CODEX + LEGACY_CODEX)
        document = json.loads(NEIGHBOUR_DESKTOP)
        document['mcpServers']['telegram_search'] = LEGACY_ENTRY
        self.desktop_present(json.dumps(document, indent=2) + '\n')
        (self.home / '.claude').mkdir(mode=0o755, exist_ok=True)
        self.claude().entry = entry(LEGACY_PYTHON, ['-m', 'telegram_search_mcp.server'])
        self.session(pin=pin)
        payload = {'Label': self.label, 'ProgramArguments': [LEGACY_PYTHON, '-m', 'telegram_search_mcp.launch_agent', 'run'],
                   'RunAtLoad': True, 'KeepAlive': True, 'ProcessType': 'Background', 'LimitLoadToSessionType': 'Aqua',
                   'Umask': 0o077, 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
                   'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}}
        self.plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps(payload))
        self.plist.chmod(0o600)
        if loaded:
            self.launchctl.load()
        return {'codex': codex_registration.owned_subtree((self.codex / 'config.toml').read_bytes()),
                'claude_desktop': json.dumps(LEGACY_ENTRY, separators=(',', ':')).encode(),
                'claude_code': json.dumps(entry(LEGACY_PYTHON, ['-m', 'telegram_search_mcp.server']), separators=(',', ':')).encode(),
                'service': self.plist.read_bytes()}

    def tx(self, packet):
        return self.runtime / 'transactions' / ('install_' + packet[3][:24])


class AdoptLegacyTests(AdoptionHarness, unittest.TestCase):

    def test_never_by_default(self):
        for kind in ('codex', 'claude_code', 'claude_desktop', 'service'):
            with self.subTest(kind=kind):
                self.setUp()
                self.legacy()
                if kind != 'codex': self.codex_present()
                if kind != 'claude_code': self.claude().entry = None
                if kind != 'claude_desktop': self.desktop_present()
                if kind != 'service': self.plist.unlink()
                snapshot = {p: p.read_bytes() for p in (self.codex / 'config.toml', self.desktop_config,
                                                         self.claude().path, self.session_root / 'runtime.toml')}
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(self.build('a'))
                expected = {'codex': 'registration_conflict', 'claude_code': 'claude_registration_conflict',
                            'claude_desktop': 'claude_desktop_registration_conflict', 'service': 'service_conflict'}[kind]
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
                self.assertFalse(self.runtime.exists())
                self.assertNotIn('bootout', self.launchctl.verbs())

    def test_first_install_adopts_every_foreign_object_after_recording_its_backup(self):
        backups = self.legacy()
        before = self.session_hashes()
        packet = self.build('a')
        with self.no_other_process():
            result = self.install(packet, adopt_legacy=True)
        self.assertEqual(result['adopted'], ['claude_code', 'claude_desktop', 'codex', 'service'])
        self.assertEqual((result['claude_code'], result['claude_desktop'], result['service']),
                         ('registered', 'registered', 'restarted'))
        installed = Path(result['release_root'])
        command, args = self.commands(installed)
        self.assertEqual(self.claude().entry, entry(command, args))
        self.assertEqual(self.claude().verbs(), ['remove', 'add-json'])
        document = json.loads(self.desktop_config.read_text())
        self.assertEqual(document['mcpServers'], {'weather': {'command': '/opt/w', 'args': []},
                                                  'telegram_search': {'command': command, 'args': args}})
        self.assertEqual(oct(self.desktop_config.stat().st_mode & 0o777), '0o644')
        config = (self.codex / 'config.toml').read_bytes()
        self.assertTrue(config.startswith(NEIGHBOUR_CODEX) and command.encode() in config and LEGACY_PYTHON.encode() not in config)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())['ProgramArguments'][4], str(installed / 'launcher.py'))
        self.assertEqual(self.launchctl.loaded[4], str(installed / 'launcher.py'))
        self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
        self.assertEqual([p for p, _ in self.probes], [Path(LEGACY_PYTHON)] * 3)
        self.assertEqual([a for _, a in self.probes], [None, None, None])
        self.assertIn(b'expected_package_version = "0.57.0.dev0"', (self.session_root / 'runtime.toml').read_bytes())
        self.assertEqual(self.session_hashes(), before)
        receipt = self.receipt()
        tx = self.tx(packet)
        for client, data in backups.items():
            self.assertEqual((tx / ('adopted-' + client)).read_bytes(), data)
            self.assertEqual(oct((tx / ('adopted-' + client)).stat().st_mode & 0o777), '0o600')
            self.assertEqual(receipt['adopted'][client], {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                                                          'transaction': tx.name})
        self.selection_check()
        # A repeat without the flag is a plain reuse; nothing is adopted again.
        again = self.install(packet)
        self.assertEqual(again['installation'], 'reused')
        self.assertEqual(self.claude().verbs(), ['remove', 'add-json'])

    def test_a_pending_send_on_the_legacy_broker_refuses_before_anything_is_created(self):
        self.legacy()
        for answer in ('pending', 'unknown'):
            with self.subTest(answer=answer):
                self.answers = [answer]
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(self.build('a-' + answer), adopt_legacy=True)
                self.assertEqual(str(caught.exception), 'pending_send_unresolved')
                self.assertFalse(self.runtime.exists())
                self.assertNotIn('bootout', self.launchctl.verbs())

    def test_every_adoption_checkpoint_resumes_without_double_effects(self):
        seen = []
        self.legacy()
        self.install(self.build('a'), adopt_legacy=True, checkpoint=seen.append)
        stages = list(dict.fromkeys(seen))
        for wanted in ('recorded:claude_remove', 'stopped:service', 'recorded:plist', 'recorded:policy', 'restarted'):
            self.assertIn(wanted, stages)
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                self.legacy()
                packet = self.build('a')
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError):
                    self.install(packet, adopt_legacy=True, checkpoint=crash)
                result = self.install(packet, adopt_legacy=True)
                window = WRITE_AHEAD.get(stage)
                command, args = self.commands(result['release_root'])
                if window != 'claude':
                    self.assertEqual(self.claude().entry, entry(command, args))
                if window != 'desktop':
                    self.assertEqual(json.loads(self.desktop_config.read_text())['mcpServers']['telegram_search'],
                                     {'command': command, 'args': args})
                self.assertEqual(self.claude().verbs(), {'recorded:claude_remove': [], 'recorded:claude_add': ['remove']}.get(
                    stage, ['remove', 'add-json']))
                self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
                self.assertEqual(self.launchctl.verbs().count('bootstrap'), 1)
                self.assertEqual(self.launchctl.loaded[4], str(Path(result['release_root']) / 'launcher.py'))
                self.assertEqual(sorted(p.name for p in desktop.directory(self.home).iterdir()), [desktop.CONFIG_NAME])
                self.selection_check()

    def test_upgrade_adopts_only_with_the_flag_and_uninstall_keeps_every_backup(self):
        self.codex_present()
        self.desktop_present()
        self.session()
        _, old = self.old()
        document = json.loads(self.desktop_config.read_text())
        document['mcpServers']['telegram_search'] = LEGACY_ENTRY  # Claude Desktop rewrote it from an old copy.
        self.desktop_present(json.dumps(document, indent=2) + '\n')
        new = self.build('b', launcher=b'# second release\n')
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(new)
        self.assertEqual(str(caught.exception), 'claude_desktop_registration_conflict')
        result = self.install(new, adopt_legacy=True)
        self.assertEqual((result['adopted'], result['claude_desktop']), (['claude_desktop'], 'registered'))
        backup = json.dumps(LEGACY_ENTRY, separators=(',', ':')).encode()
        receipt = self.receipt()
        self.assertEqual(receipt['adopted'], {'claude_desktop': {'sha256': hashlib.sha256(backup).hexdigest(),
                                                                 'bytes': len(backup), 'transaction': 'change_1'}})
        self.assertEqual((self.runtime / 'transactions/change_1/adopted-claude_desktop').read_bytes(), backup)
        removed = self.change('uninstall')
        kept = Path(removed['legacy_backups'])
        self.assertFalse(self.runtime.exists())
        self.assertEqual((kept / 'change_1-claude_desktop').read_bytes(), backup)
        self.assertEqual(oct(kept.stat().st_mode & 0o777), '0o700')


class AdoptFlagGrammarTests(unittest.TestCase):
    REQUIRED = ['--installation-root', '/x', '--codex-home', '/y', '--skills-root', '/z',
                '--index', '/i', '--trusted-index-sha256', '0' * 64, '--claude-code', 'user']

    def run_launcher(self, options):
        from telegram_search_mcp import bundle_install
        spec = importlib.util.spec_from_file_location('adopt_flag_launcher', Path(__file__).parents[1] / 'packaging/launcher.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seen = []
        saved = list(sys.path)
        try:
            with mock.patch.object(module, 'admit'), \
                    mock.patch.object(bundle_install, 'install', side_effect=lambda *a, **k: seen.append(k) or {'status': 'x'}), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                code = module.main(['bootstrap-install', *options])
        finally:
            sys.path[:] = saved
        return code, out.getvalue(), err.getvalue(), seen

    def test_the_flag_is_explicit_last_and_once(self):
        for options, adopt in ((self.REQUIRED, False), (self.REQUIRED + ['--adopt-legacy'], True),
                               (self.REQUIRED[:-2] + ['--adopt-legacy'], True)):
            with self.subTest(options=options):
                code, out, err, [kwargs] = self.run_launcher(options)
                self.assertEqual((code, err, kwargs['adopt_legacy']), (0, '', adopt))
        generic = json.dumps({'status': 'refused', 'code': 'bundle_admission_or_install_failed'}) + '\n'
        for options in (['--adopt-legacy'] + self.REQUIRED, self.REQUIRED + ['--adopt-legacy', '--adopt-legacy'],
                        self.REQUIRED + ['--adopt-legacy', 'yes'], self.REQUIRED[:2] + ['--adopt-legacy'] + self.REQUIRED[2:]):
            with self.subTest(options=options):
                self.assertEqual(self.run_launcher(options), (2, '', generic, []))


class AdoptBootstrapTests(unittest.TestCase):
    """The supported entry point: bootstrap.sh runs for real and passes --adopt-legacy to a stub launcher last."""

    def setUp(self):
        from test_claude_code_modes import synthetic_kit
        self.base = Path(tempfile.mkdtemp(prefix='tg-adopt-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        (self.seed, self.index, self.archive), self.seed_sha, self.seed_bytes, self.index_sha = synthetic_kit(
            self.base / 'kit', self.home, self.out)

    def bootstrap(self, *extra):
        from test_claude_code_modes import BOOTSTRAP
        argv = ['/bin/sh', '-p', str(BOOTSTRAP), '--seed', str(self.seed), '--seed-sha256', self.seed_sha,
                '--seed-bytes', str(self.seed_bytes), '--archive', str(self.archive), '--index', str(self.index),
                '--trusted-index-sha256', self.index_sha, '--installation-root', str(self.home / 'runtime'), *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, env={'PATH': '/usr/bin:/bin'})

    def test_the_flag_reaches_the_launcher_last_and_only_when_given(self):
        # --claude-code reaches the launcher only when given (the installer then keeps or defaults the mode).
        for extra, mode, adopt in (((), None, False), (('--adopt-legacy',), None, True),
                                   (('--claude-code', 'plugin', '--adopt-legacy'), 'plugin', True),
                                   (('--json', '--claude-code', 'skip', '--adopt-legacy'), 'skip', True)):
            with self.subTest(extra=extra):
                done = self.bootstrap(*extra)
                self.assertEqual((done.returncode, done.stderr), (0, ''))
                argv = (self.out / 'launcher-argv').read_text().splitlines()
                stage = argv[3][:-len('/release/launcher.py')]
                self.assertEqual(argv, ['-I', '-S', '-B', stage + '/release/launcher.py', 'bootstrap-install',
                                        '--installation-root', str(self.home / 'runtime'),
                                        '--codex-home', str(self.home / '.codex'),
                                        '--skills-root', str(self.home / '.agents/skills'),
                                        '--index', stage + '/verified-index.json',
                                        '--trusted-index-sha256', self.index_sha]
                                 + (['--claude-code', mode] if mode else []) + (['--adopt-legacy'] if adopt else []))
                (self.out / 'launcher-argv').unlink()

    def test_anywhere_but_last_or_twice_refuses_before_the_seed_runs(self):
        for extra in (('--adopt-legacy', '--claude-code', 'user'), ('--adopt-legacy', '--adopt-legacy'),
                      ('--adopt-legacy', 'yes'), ('--adopt-legacy', '--json')):
            with self.subTest(extra=extra):
                done = self.bootstrap(*extra)
                self.assertEqual(done.returncode, 6)
                self.assertEqual(json.loads(done.stderr), {'schema_version': 1, 'error_code': 'selection_invalid',
                                                           'retained_stage': ''})
                self.assertFalse((self.out / 'seed-ran').exists())

    def test_install_command_and_the_plugin_never_pass_it_and_the_spec_names_the_command(self):
        root = Path(__file__).parents[1]
        for path in [root / 'packaging/install_command.py', *(p for p in (root / 'plugins').rglob('*') if p.is_file())]:
            with self.subTest(path=str(path.relative_to(root))):
                self.assertNotIn(b'--adopt-legacy', path.read_bytes())
        spec = (root / 'docs/plans/0.53-client-distribution-spec.md').read_text()
        section = spec[spec.index('### E '):spec.index('### F ')]
        self.assertIn('/usr/bin/env -i /bin/sh -p "$KIT/bootstrap.sh"', section)
        self.assertIn('--adopt-legacy`', section)


if __name__ == '__main__': unittest.main()
