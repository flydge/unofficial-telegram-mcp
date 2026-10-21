"""Install.command (CLIENTS-8): pinned bootstrap, then the printed activation with inline `connect`.

The generated script runs for real under /bin/sh -p from a kit folder whose name has spaces, like a
mounted DMG volume. bootstrap.sh and the installed python3 are stubs that record their argv and
environment; nothing is installed and no launcher runs.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
SEED, INDEX = 'a' * 64, 'b' * 64
TEAM = 'ABCDE12345'


def load():
    spec = importlib.util.spec_from_file_location('install_command_under_test', ROOT / 'packaging/install_command.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


IC = load()

BOOTSTRAP = r'''#!/bin/sh
kit=$(cd "$(/usr/bin/dirname "$0")" && /bin/pwd -P)
for argument in "$@"; do /usr/bin/printf '%s\n' "$argument"; done > "$kit/../bootstrap.argv"
mode=$(/bin/cat "$kit/../stub-mode")
root=$(/bin/cat "$kit/../stub-root")
python="$root/python/bin/python3"
launcher="$root/launcher.py"
verb=connect
status=activation_required
extra=''
case "$mode" in
    fail) /usr/bin/printf '%s\n' '{"schema_version":1,"error_code":"integrity_failure"}' >&2; exit 6;;
    prepared) /usr/bin/printf '%s\n' '{"verified":true}'; exit 0;;
    other-root) root="$root-other";;
    other-python) python=/usr/bin/true;;
    other-verb) verb=setup;;
    other-status) status=installed;;
    seven) extra=',"--json"';;
esac
/usr/bin/printf '{"status":"%s","release_root":"%s","activation_arguments":["%s","-I","-S","-B","%s","%s"%s]}\n' \
    "$status" "$root" "$python" "$launcher" "$verb" "$extra"
'''
PYTHON = r'''#!/bin/sh
here=$(cd "$(/usr/bin/dirname "$0")/../.." && /bin/pwd -P)
for argument in "$@"; do /usr/bin/printf '%s\n' "$argument"; done > "$here/activation.argv"
/usr/bin/env > "$here/activation.env"
/usr/bin/printf '%s\n' '{"status":"connected"}'
exit "$(/bin/cat "$here/exit-code")"
'''


class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.kit = self.root / 'Synthetic Volume'
        self.kit.mkdir()
        self.release = self.root / 'home/Library/Application Support/TelegramSearch/releases' / INDEX
        (self.release / 'python/bin').mkdir(parents=True)
        (self.release / 'python/bin/python3').write_text(PYTHON)
        (self.release / 'python/bin/python3').chmod(0o700)
        (self.release / 'exit-code').write_text('0')
        for name in ('bootstrap-verifier', 'release.tar.gz', 'release-index.json'):
            (self.kit / name).write_bytes(b'inert')
        (self.kit / 'bootstrap.sh').write_text(BOOTSTRAP)
        (self.root / 'stub-root').write_text(str(self.release))
        self.mode('ok')
        self.command = self.kit / 'Install.command'
        self.command.write_bytes(IC.render(seed_sha256=SEED, seed_bytes=71600, index_sha256=INDEX))
        self.command.chmod(0o555)

    def mode(self, value):
        (self.root / 'stub-mode').write_text(value)

    def run_command(self, *args):
        return subprocess.run([str(self.command), *args], capture_output=True, text=True, timeout=60,
                              env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root / 'home')})

    def bootstrap_argv(self):
        return (self.root / 'bootstrap.argv').read_text().splitlines()

    def test_runs_the_pinned_bootstrap_then_inline_connect(self):
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bootstrap_argv(), [
            '--seed', f'{self.kit}/bootstrap-verifier', '--seed-sha256', SEED, '--seed-bytes', '71600',
            '--archive', f'{self.kit}/release.tar.gz', '--index', f'{self.kit}/release-index.json',
            '--trusted-index-sha256', INDEX])
        self.assertEqual((self.release / 'activation.argv').read_text().splitlines(),
                         ['-I', '-S', '-B', f'{self.release}/launcher.py', 'connect'])
        env = dict(line.split('=', 1) for line in (self.release / 'activation.env').read_text().splitlines())
        self.assertEqual(env['PATH'], '/usr/bin:/bin:/usr/sbin:/sbin')
        self.assertNotIn('HOME', env)  # env -i: the launcher derives its home from the account
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0])['status'], 'activation_required')
        self.assertEqual(lines[-2:], ['{"status":"connected"}', 'Install.command finished.'])

    def test_prepare_only_verifies_and_activates_nothing(self):
        self.mode('prepared')
        result = self.run_command('--prepare-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bootstrap_argv()[0], '--prepare-only')
        self.assertEqual(self.bootstrap_argv()[1:], [
            '--seed', f'{self.kit}/bootstrap-verifier', '--seed-sha256', SEED, '--seed-bytes', '71600',
            '--archive', f'{self.kit}/release.tar.gz', '--index', f'{self.kit}/release-index.json',
            '--trusted-index-sha256', INDEX])
        self.assertFalse((self.release / 'activation.argv').exists())

    def test_refusals_print_one_fixed_code(self):
        cases = [('fail', (), 'bootstrap_failed', 4), ('other-root', (), 'activation_unexpected', 5),
                 ('other-python', (), 'activation_unexpected', 5), ('other-verb', (), 'activation_unexpected', 5),
                 ('other-status', (), 'activation_unexpected', 5), ('seven', (), 'activation_unexpected', 5),
                 ('ok', ('--json',), 'usage', 2), ('ok', ('--prepare-only', '--prepare-only'), 'usage', 2)]
        for mode, args, code, status in cases:
            with self.subTest(mode=mode, args=args):
                self.mode(mode)
                (self.release / 'activation.argv').unlink(missing_ok=True)
                result = self.run_command(*args)
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stderr.splitlines()[-2:], [
                    f'Install.command stopped: {code}. See the install guide.',
                    f'{{"schema_version":1,"error_code":"{code}"}}'])
                self.assertFalse((self.release / 'activation.argv').exists())
        self.mode('ok')
        (self.release / 'exit-code').write_text('2')
        result = self.run_command()
        self.assertEqual(result.returncode, 6)
        self.assertIn('"error_code":"activation_failed"', result.stderr)
        (self.kit / 'release.tar.gz').unlink()
        result = self.run_command()
        self.assertEqual((result.returncode, result.stderr.splitlines()[-1]),
                         (3, '{"schema_version":1,"error_code":"kit_incomplete"}'))

    def test_a_pinned_team_reaches_bootstrap_as_seed_team_in_both_modes(self):
        self.command.chmod(0o755)
        self.command.write_bytes(IC.render(seed_sha256=SEED, seed_bytes=71600, index_sha256=INDEX, team_id=TEAM,
                                           published=True))
        pinned = ['--seed', f'{self.kit}/bootstrap-verifier', '--seed-sha256', SEED, '--seed-bytes', '71600',
                  '--archive', f'{self.kit}/release.tar.gz', '--index', f'{self.kit}/release-index.json',
                  '--trusted-index-sha256', INDEX, '--seed-team', TEAM]
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bootstrap_argv(), pinned)
        self.assertEqual((self.release / 'activation.argv').read_text().splitlines()[-1], 'connect')
        self.mode('prepared')
        result = self.run_command('--prepare-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bootstrap_argv(), ['--prepare-only', *pinned])

    def test_without_a_team_the_literal_is_empty_and_no_seed_team_is_passed(self):
        text = IC.render(seed_sha256=SEED, seed_bytes=1, index_sha256=INDEX).decode()
        self.assertIn("install_seed_team=''\n", text)
        self.assertEqual(self.run_command().returncode, 0)
        self.assertNotIn('--seed-team', self.bootstrap_argv())

    def test_a_published_kit_needs_a_team(self):
        with self.assertRaises(IC.InstallCommandError) as caught:
            IC.render(seed_sha256=SEED, seed_bytes=1, index_sha256=INDEX, published=True)
        self.assertEqual(str(caught.exception), 'team_required')
        text = IC.render(seed_sha256=SEED, seed_bytes=1, index_sha256=INDEX, team_id=TEAM, published=True).decode()
        self.assertIn(f"install_seed_team='{TEAM}'\n", text)
        for options in (dict(team_id='abcde12345'), dict(team_id='ABCDE1234'), dict(team_id=''), dict(team_id="A'$(x)"),
                        dict(team_id=TEAM, published=1), dict(team_id=TEAM, published='yes')):
            with self.subTest(options=options), self.assertRaises(IC.InstallCommandError) as caught:
                IC.render(seed_sha256=SEED, seed_bytes=1, index_sha256=INDEX, **options)
            self.assertEqual(str(caught.exception), 'pins_invalid')

    def test_a_relative_invocation_is_refused(self):
        result = subprocess.run(['/bin/sh', '-p', 'Install.command'], cwd=self.kit, capture_output=True, text=True,
                                timeout=60, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 3)
        self.assertIn('kit_unavailable', result.stderr)

    def test_pins_are_embedded_literals_with_a_closed_grammar(self):
        text = IC.render(seed_sha256=SEED, seed_bytes=1, index_sha256=INDEX).decode()
        self.assertIn(f'install_seed_sha256={SEED}\n', text)
        self.assertIn('install_seed_bytes=1\n', text)
        self.assertIn(f'install_index_sha256={INDEX}\n', text)
        self.assertTrue(text.startswith('#!/bin/sh -p\n'))
        self.assertIn('/usr/bin/env -i /bin/sh -p "$install_kit/bootstrap.sh"', text)
        for options in (dict(seed_sha256='A' * 64), dict(seed_sha256='a' * 63), dict(index_sha256='$(x)'),
                        dict(seed_bytes=0), dict(seed_bytes=2097153), dict(seed_bytes=True), dict(seed_bytes='1')):
            pins = {'seed_sha256': SEED, 'seed_bytes': 1, 'index_sha256': INDEX, **options}
            with self.subTest(options=options), self.assertRaises(IC.InstallCommandError) as caught:
                IC.render(**pins)
            self.assertEqual(str(caught.exception), 'pins_invalid')


if __name__ == '__main__':
    unittest.main()
