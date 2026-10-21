"""`--claude-code user|plugin|skip` through bootstrap.sh, launcher.py and install() (CLIENTS-11 installer half).

Every home is disposable. bootstrap.sh runs for real against a synthetic seed (a shell script) whose
release holds a recording stand-in for python3, so no real runtime, client or installation is touched;
the stage bootstrap.sh makes under /private/tmp is removed by that stand-in.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_bundle_install import InstallHarness
from telegram_search_mcp import claude_registration

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'packaging' / 'bootstrap.sh'


class ModeInstallTests(InstallHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.packet = self.fixture()
        self.bundle = self.packet[0]

    def receipt(self):
        return json.loads((self.runtime / 'receipt.json').read_text())

    def journal(self):
        return json.loads((self.runtime / 'transactions' / ('install_' + self.packet[3][:24]) / 'journal.json').read_text())

    def selection_check(self):
        from telegram_search_mcp import bundle_runtime
        selection = bundle_runtime.Selection(self.runtime / 'releases' / self.packet[3], self.packet[3])
        with mock.patch.object(bundle_runtime, '_SELECTION', selection):
            selection.check()
        return selection

    def test_plugin_and_skip_leave_no_user_scope_or_claude_skill_effects(self):
        for mode, status in (('plugin', 'plugin'), ('skip', 'skipped')):
            with self.subTest(mode=mode):
                self.setUp()
                fake = self.claude()
                (self.home / '.claude').mkdir(mode=0o755)  # Claude Code is present ...
                foreign = {'type': 'stdio', 'command': '/foreign/python3', 'args': ['mcp']}
                fake.write_document({'mcpServers': {'telegram_search': foreign}})  # ... even with a different entry.
                before = (fake.path.read_bytes(), fake.path.stat().st_ino)
                stale = self.claude_skills / 'telegram-search'
                stale.mkdir(parents=True)
                (stale / 'SKILL.md').write_bytes(b'the user\'s own copy')
                with mock.patch.object(claude_registration, 'state', side_effect=AssertionError('no ~/.claude.json read')), \
                        mock.patch.object(claude_registration, 'locate', side_effect=AssertionError('no CLI lookup')):
                    result = self.install(self.packet, claude_code=mode)
                self.assertEqual((result['claude_code'], result['claude_skills']), (status, 'skipped'))
                self.assertNotIn('claude_registration_command', result)
                # Plugin mode notes the existing user-scope entry, read-only (it would duplicate the tools).
                self.assertEqual(result.get('claude_code_user_entry'), 'present' if mode == 'plugin' else None)
                receipt = self.receipt()
                self.assertEqual((receipt['claude_code_mode'], receipt['claude_code'], receipt['claude_skills']),
                                 (mode, status, 'skipped'))
                self.assertEqual(set(receipt['publication_objects']), {'release', 'telegram-search', 'telegram-setup'})
                self.assertEqual(fake.calls, [])
                self.assertEqual((fake.path.read_bytes(), fake.path.stat().st_ino), before)
                self.assertEqual(sorted(p.name for p in self.claude_skills.iterdir()), ['telegram-search'])
                self.assertEqual((stale / 'SKILL.md').read_bytes(), b'the user\'s own copy')
                self.assertTrue((self.codex / 'config.toml').is_file())  # Codex and ~/.agents/skills as always.
                self.assertTrue((self.skills / 'telegram-setup' / 'SKILL.md').is_file())
                self.assertEqual(self.journal()['claude_code_mode'], mode)
                self.selection_check()
                again = self.install(self.packet, claude_code=mode)
                self.assertEqual((again['installation'], again['claude_code']), ('reused', status))

    def test_plugin_mode_without_a_user_scope_entry_adds_no_note(self):
        fake = self.claude()
        for document in (None, {'mcpServers': {'other': {'type': 'stdio', 'command': '/o', 'args': []}}}):
            with self.subTest(document=document):
                self.setUp()
                fake = self.claude()
                if document is not None: fake.write_document(document)
                result = self.install(self.packet, claude_code='plugin')
                self.assertNotIn('claude_code_user_entry', result)
                self.assertEqual(fake.calls, [])

    def test_plugin_mode_never_inspects_the_claude_skills_root(self):
        self.claude_skills.mkdir(parents=True)
        self.claude_skills.chmod(0o777)  # Would refuse a user-mode install; plugin mode never uses it.
        self.addCleanup(self.claude_skills.chmod, 0o700)
        self.assertEqual(self.install(self.packet, claude_code='plugin')['claude_code'], 'plugin')
        self.setUp()
        self.claude_skills.mkdir(parents=True)
        self.claude_skills.chmod(0o777)
        self.addCleanup(self.claude_skills.chmod, 0o700)
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet)

    def test_user_mode_is_the_default_and_recorded(self):
        result = self.install(self.packet)
        self.assertEqual(result['claude_code'], 'registered')
        self.assertEqual(self.receipt()['claude_code_mode'], 'user')
        self.selection_check()

    def test_without_a_mode_a_repeat_or_a_resume_keeps_the_recorded_one(self):
        fake = self.claude()
        def crash(value):
            if value == 'release': raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash, claude_code='plugin')
        result = self.install(self.packet)  # The resumed first install keeps its intent's mode.
        self.assertEqual((result['installation'], result['claude_code']), ('installed', 'plugin'))
        again = self.install(self.packet, claude_code=None)
        self.assertEqual((again['installation'], again['claude_code']), ('reused', 'plugin'))
        self.assertEqual((self.receipt()['claude_code_mode'], fake.calls), ('plugin', []))
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, claude_code='user')

    def test_an_unknown_mode_refuses_before_anything_is_created(self):
        for mode in ('', 'User', 'both', 1, ['plugin']):
            with self.subTest(mode=mode):
                self.setUp()
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet, claude_code=mode)
                self.assertEqual(str(caught.exception), 'installation_refused')
                self.assertFalse(self.runtime.exists())

    def test_a_resume_in_another_mode_is_refused_and_the_original_mode_completes(self):
        for stage in ('recorded:root', 'release', 'telegram-setup'):  # After the durable intent.
            with self.subTest(stage=stage):
                self.setUp()
                fake = self.claude()
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash, claude_code='plugin')
                for other in ('user', 'skip'):
                    with self.assertRaises(self.bundle.InstallError):
                        self.install(self.packet, claude_code=other)
                self.assertFalse((self.runtime / 'current.json').exists())
                result = self.install(self.packet, claude_code='plugin')
                self.assertEqual((result['claude_code'], result['claude_skills']), ('plugin', 'skipped'))
                self.assertEqual(fake.calls, [])
                self.assertFalse((self.home / '.claude').exists())

    def test_the_receipt_ties_each_status_to_its_mode(self):
        from telegram_search_mcp import bundle_runtime
        self.install(self.packet, claude_code='plugin')
        path = self.runtime / 'receipt.json'
        original = json.loads(path.read_text())
        self.selection_check()
        broken = {'plugin mode, user status': {**original, 'claude_code': 'registered'},
                  'plugin mode, skip status': {**original, 'claude_code': 'skipped'},
                  'user mode, plugin status': {**original, 'claude_code_mode': 'user'},
                  'plugin mode with claude skills': {**original, 'claude_skills': 'installed'},
                  'unknown mode': {**original, 'claude_code_mode': 'both'},
                  'mode not a string': {**original, 'claude_code_mode': ['plugin']},
                  'no mode': {k: v for k, v in original.items() if k != 'claude_code_mode'}}
        for label, value in broken.items():
            with self.subTest(label):
                path.write_text(json.dumps(value))
                with self.assertRaises(bundle_runtime.BundleError): self.selection_check()
        path.write_text(json.dumps(original))
        self.selection_check()

    def test_the_status_tuples(self):
        from telegram_search_mcp import bundle_runtime as runtime
        self.assertEqual(runtime.CLAUDE_CODE_MODES, ('user', 'plugin', 'skip'))
        self.assertEqual(runtime.CLAUDE_STATUSES, ('registered', 'reused', 'failed', 'unavailable', 'not_found', 'plugin', 'skipped'))
        self.assertEqual(set(runtime.CLAUDE_STATUSES), set().union(*runtime.CLAUDE_MODE_STATUSES.values()))
        self.assertEqual(runtime.CLAUDE_DESKTOP_STATUSES, ('registered', 'reused', 'failed', 'unavailable', 'skipped'))


def load_launcher(name='claude_code_mode_launcher'):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / 'launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherOptionTests(unittest.TestCase):
    REQUIRED = ['--installation-root', '/x', '--codex-home', '/y', '--skills-root', '/z',
                '--index', '/i', '--trusted-index-sha256', '0' * 64]

    def run_launcher(self, options, error=None):
        from telegram_search_mcp import bundle_install
        module = load_launcher()
        seen = []
        def install(*args, **kwargs):
            seen.append(kwargs)
            if error is not None: raise error
            return {'status': 'activation_required'}
        saved = list(sys.path)
        try:
            with mock.patch.object(module, 'admit'), mock.patch.object(bundle_install, 'install', side_effect=install), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                code = module.main(['bootstrap-install', *options])
        finally:
            sys.path[:] = saved
        return code, out.getvalue(), err.getvalue(), seen

    def test_the_mode_reaches_install_only_when_given(self):
        # Not given, install() gets None: it keeps the installed mode on an upgrade and uses user for a first install.
        for options, mode in ((self.REQUIRED, None), (self.REQUIRED + ['--claude-code', 'plugin'], 'plugin'),
                              (['--claude-code', 'skip'] + self.REQUIRED, 'skip'),
                              (self.REQUIRED[:4] + ['--claude-code', 'user'] + self.REQUIRED[4:], 'user')):
            with self.subTest(options=options):
                code, out, err, seen = self.run_launcher(options)
                self.assertEqual((code, err), (0, ''))
                self.assertEqual(json.loads(out), {'status': 'activation_required'})
                [kwargs] = seen
                self.assertEqual(kwargs['claude_code'], mode)

    def test_any_other_grammar_is_the_generic_refusal_before_install(self):
        generic = json.dumps({'status': 'refused', 'code': 'bundle_admission_or_install_failed'}) + '\n'
        for options in (self.REQUIRED + ['--claude-code', 'both'], self.REQUIRED + ['--claude-code', ''],
                        self.REQUIRED + ['--claude-code', 'plugin', '--claude-code', 'user'],
                        self.REQUIRED + ['--claude-code'], self.REQUIRED + ['--other', 'x'],
                        self.REQUIRED[2:] + ['--claude-code', 'plugin'], self.REQUIRED[:-2] + ['--codex-home', '/y'],
                        self.REQUIRED + ['--claude-code', 'plugin', '--json', 'x']):
            with self.subTest(options=options):
                code, out, err, seen = self.run_launcher(options)
                self.assertEqual((code, out, err, seen), (2, '', generic, []))

    def test_the_desktop_codes_reach_the_output(self):
        from telegram_search_mcp.bundle_install import InstallError
        for name in ('claude_desktop_registration_conflict', 'claude_desktop_config_unsupported'):
            with self.subTest(name):
                self.assertIn(name, load_launcher().INSTALL_CODES)
                code, out, err, _ = self.run_launcher(self.REQUIRED, InstallError(name))
                self.assertEqual((code, out, err), (2, '', json.dumps({'status': 'refused', 'code': name}) + '\n'))

    def test_both_version_sites_are_untouched(self):
        from telegram_search_mcp import __version__
        text = (ROOT / 'packaging' / 'launcher.py').read_text()
        self.assertEqual(text.count(__version__), 2)


FAKE_SEED = '''#!/bin/sh
# Synthetic seed: answers `prepare` like the verifier and stages a recording stand-in for python3.
dest=''; pin=''
while [ "$#" -gt 0 ]; do
    case "$1" in --destination) dest=$2; shift 2;; --trusted-index-sha256) pin=$2; shift 2;; *) shift;; esac
done
stage=${dest%/release}
/bin/mkdir -p "$dest/python/bin"
/bin/cat > "$dest/python/bin/python3" <<'PYTHON'
#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > '@OUT@/launcher-argv'
/usr/bin/env > '@OUT@/launcher-env'
stage=${0%/release/python/bin/python3}
case "$stage" in /private/tmp/telegram-bootstrap.????????) /bin/rm -rf "$stage";; esac
printf '{"activation_command":"synthetic","claude_code":"plugin","status":"activation_required"}\\n'
PYTHON
/bin/chmod 700 "$dest/python/bin/python3"
printf 'seeded\\n' > '@OUT@/seed-ran'
printf '{"index_path":"%s","index_sha256":"%s","os_home":"@HOME@","release_root":"%s","verified":true}\\n' \\
    "$stage/verified-index.json" "$pin" "$dest"
'''


def synthetic_kit(directory, home, out):
    """seed/index/archive inputs for the real bootstrap.sh: (paths, seed sha256, seed bytes, index sha256)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed = directory / 'bootstrap-verifier'
    seed.write_text(FAKE_SEED.replace('@OUT@', str(out)).replace('@HOME@', str(home)))
    index = directory / 'release-index.json'
    index.write_bytes(b'{"synthetic": "index"}\n')
    archive = directory / 'release.tar.gz'
    archive.write_bytes(b'synthetic archive bytes')
    for path in (seed, index, archive):
        path.chmod(0o600)
    data = seed.read_bytes()
    return (seed, index, archive), hashlib.sha256(data).hexdigest(), len(data), hashlib.sha256(index.read_bytes()).hexdigest()


class BootstrapPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-modes-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        (self.seed, self.index, self.archive), self.seed_sha, self.seed_bytes, self.index_sha = synthetic_kit(
            self.base / 'kit', self.home, self.out)

    def bootstrap(self, *extra):
        argv = ['/bin/sh', '-p', str(BOOTSTRAP), '--seed', str(self.seed), '--seed-sha256', self.seed_sha,
                '--seed-bytes', str(self.seed_bytes), '--archive', str(self.archive), '--index', str(self.index),
                '--trusted-index-sha256', self.index_sha, *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, env={'PATH': '/usr/bin:/bin'})

    def launcher_argv(self):
        return (self.out / 'launcher-argv').read_text().splitlines()

    def test_the_mode_is_passed_to_the_launcher_only_when_given(self):
        for extra, mode in (((), None), (('--claude-code', 'plugin'), 'plugin'), (('--claude-code', 'skip'), 'skip'),
                            (('--claude-code', 'user'), 'user')):
            with self.subTest(mode=mode, extra=extra):
                done = self.bootstrap('--installation-root', str(self.home / 'runtime'), *extra)
                self.assertEqual((done.returncode, done.stderr), (0, ''))
                self.assertEqual(json.loads(done.stdout)['status'], 'activation_required')
                argv = self.launcher_argv()
                stage = argv[3][:-len('/release/launcher.py')]
                self.assertFalse(Path(stage).exists())  # The stand-in removed bootstrap's stage.
                self.assertEqual(argv, ['-I', '-S', '-B', stage + '/release/launcher.py', 'bootstrap-install',
                                        '--installation-root', str(self.home / 'runtime'),
                                        '--codex-home', str(self.home / '.codex'),
                                        '--skills-root', str(self.home / '.agents/skills'),
                                        '--index', stage + '/verified-index.json',
                                        '--trusted-index-sha256', self.index_sha]
                                 + (['--claude-code', mode] if mode else []))
                (self.out / 'launcher-argv').unlink()

    def test_an_invalid_mode_refuses_before_the_seed_runs(self):
        for extra in (('--claude-code', 'both'), ('--claude-code', ''), ('--claude-code',),
                      ('--claude-code', 'plugin', '--claude-code', 'plugin'), ('--claude-code', 'Plugin')):
            with self.subTest(extra=extra):
                done = self.bootstrap(*extra)
                self.assertEqual(done.returncode, 6)
                self.assertEqual(json.loads(done.stderr), {'schema_version': 1, 'error_code': 'selection_invalid',
                                                           'retained_stage': ''})
                self.assertFalse((self.out / 'seed-ran').exists())


if __name__ == '__main__': unittest.main()
