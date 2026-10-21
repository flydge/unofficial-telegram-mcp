"""build_bootstrap_seed.sh (CLIENTS-7): the unchanged ad hoc seed, and the Developer ID --sign-identity mode.

The script runs for real (clang, otool, git) from a synthetic repository holding copies of itself, the
verifier source and product_identity.py. The default mode uses the stock codesign. The signing mode
runs a copy whose tool block points codesign at a recorder that answers `-d` with a canned display, so
no identity, Keychain or timestamp service is touched; the argv, the designated requirement and every
closed check of the display are observed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from telegram_search_mcp import product_identity
from telegram_search_mcp.native_runtime import designated_requirement

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'packaging/build_bootstrap_seed.sh'
TOOL_BLOCK = 'tool_codesign=/usr/bin/codesign\n'
TEAM = 'ABCDE12345'
IDENTITY = f'Developer ID Application: Synthetic Example ({TEAM})'
SHA1 = '0123456789ABCDEF0123456789ABCDEF01234567'
IDENTIFIER = product_identity.CODESIGN_ID_PREFIX + '.bootstrap-verifier'
AD_HOC_KEYS = {'schema_version', 'kind', 'target', 'minimum_macos', 'source_commit', 'source_sha256',
               'build_script_sha256', 'binary_sha256', 'binary_bytes', 'compiler', 'publisher_identity'}
DISPLAY = f'''Executable=/synthetic/bootstrap-verifier
Identifier={IDENTIFIER}
Format=Mach-O thin (arm64)
CodeDirectory v=20500 size=900 flags=0x10000(runtime) hashes=18+7 location=embedded
Signature size=9000
Authority=Developer ID Application: Synthetic Example ({TEAM})
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=Oct 21, 2026 at 12:00:00
Info.plist=not bound
TeamIdentifier={TEAM}
Runtime Version=26.0.0
'''
FAILED = {'schema_version': 1, 'error_code': 'bootstrap_seed_build_failed'}


def git(repo, *args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'LC_ALL': 'C'}
    return subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic Author',
                           '-c', 'user.email=author@example.invalid', '-c', 'commit.gpgsign=false',
                           '-c', 'init.defaultBranch=main', *args],
                          env=env, capture_output=True, text=True, timeout=60, check=True).stdout.strip()


class SeedBuildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.out = self.root / 'out'
        self.out.mkdir(mode=0o700)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.control = self.root / 'control'
        self.control.mkdir(mode=0o700)
        for name, value in (('sign-exit', '0'), ('verify-exit', '0'), ('display', DISPLAY)):
            (self.control / name).write_text(value)
        stub = self.root / 'codesign'
        stub.write_text(f'''#!/bin/sh
{{ printf 'HOME=%s\\n' "${{HOME-unset}}"; for a in "$@"; do printf '%s\\n' "$a"; done; printf '%s\\n' --end--; }} >> '{self.control}/calls'
case "$1" in
    --force) exit "$(/bin/cat '{self.control}/sign-exit')";;
    --verify) exit "$(/bin/cat '{self.control}/verify-exit')";;
    -d) /bin/cat '{self.control}/display' >&2;;
esac
''')
        stub.chmod(0o700)
        self.stub = stub

    def repo(self, *, stubbed, identity_text=None, with_identity=True):
        """A committed synthetic checkout; returns its copy of the script."""
        repo = self.root / ('repo-stubbed' if stubbed else 'repo')
        if repo.exists():
            shutil.rmtree(repo)
        (repo / 'packaging').mkdir(parents=True)
        text = SCRIPT.read_text()
        self.assertEqual(text.count(TOOL_BLOCK), 1)
        if stubbed:
            text = text.replace(TOOL_BLOCK, f"tool_codesign='{self.stub}'\n")
        (repo / 'packaging/build_bootstrap_seed.sh').write_text(text)
        shutil.copyfile(ROOT / 'packaging/bootstrap_verifier.c', repo / 'packaging/bootstrap_verifier.c')
        if with_identity:
            (repo / 'src/telegram_search_mcp').mkdir(parents=True)
            source = (ROOT / 'src/telegram_search_mcp/product_identity.py').read_text()
            (repo / 'src/telegram_search_mcp/product_identity.py').write_text(
                source if identity_text is None else identity_text(source))
        git(repo, 'init', '-q')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'synthetic')
        return repo / 'packaging/build_bootstrap_seed.sh'

    def run_script(self, script, name, *extra, env=None):
        return subprocess.run(['/bin/sh', str(script), '--output-directory', str(self.out / name), *extra],
                              capture_output=True, text=True, timeout=300,
                              env=env if env is not None else {'PATH': '/usr/bin:/bin', 'HOME': str(self.home)})

    def calls(self):
        path = self.control / 'calls'
        if not path.exists():
            return []
        calls, current = [], []
        for line in path.read_text().splitlines():
            if line == '--end--':
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls

    def refused(self, done, name):
        self.assertEqual((done.returncode, done.stdout), (1, ''))
        self.assertEqual(json.loads(done.stderr), FAILED)
        return self.out / name

    def test_the_default_is_the_ad_hoc_seed_and_reads_no_product_identity(self):
        script = self.repo(stubbed=False, with_identity=False)
        digests = []
        for name in ('one', 'two'):
            done = self.run_script(script, name)
            self.assertEqual((done.returncode, done.stderr), (0, ''))
            self.assertEqual(json.loads(done.stdout), {'schema_version': 1, 'built': True, 'target': 'macos-arm64',
                                                       'minimum_macos': '26.0', 'publisher_identity': False})
            provenance = json.loads((self.out / name / 'provenance.json').read_bytes())
            self.assertEqual(set(provenance), AD_HOC_KEYS)
            self.assertEqual((provenance['kind'], provenance['publisher_identity']),
                             ('developer-built-ad-hoc-seed', False))
            binary = (self.out / name / 'bootstrap-verifier').read_bytes()
            self.assertEqual(provenance['binary_sha256'], hashlib.sha256(binary).hexdigest())
            digests.append(provenance['binary_sha256'])
        shown = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(self.out / 'one/bootstrap-verifier')],
                               capture_output=True, text=True, timeout=60).stderr.splitlines()
        self.assertIn('Signature=adhoc', shown)
        # The ad hoc seed keeps codesign's default identifier (file name plus a hash), never the product prefix.
        self.assertEqual([line[:30] for line in shown if line.startswith('Identifier=')], ['Identifier=bootstrap-verifier-'])
        self.assertIn('flags=0x10002(adhoc,runtime)', ' '.join(shown))
        self.assertEqual(digests[0], digests[1])
        self.assertFalse((self.out / 'one/signature.txt').exists())

    def test_sign_identity_signs_with_the_product_prefix_and_checks_the_team(self):
        script = self.repo(stubbed=True)
        done = self.run_script(script, 'seed', '--sign-identity', IDENTITY, '--team-id', TEAM)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(done.stdout), {'schema_version': 1, 'built': True, 'target': 'macos-arm64',
                                                   'minimum_macos': '26.0', 'publisher_identity': True,
                                                   'team_id': TEAM})
        binary = str(self.out / 'seed/bootstrap-verifier')
        self.assertEqual(self.calls(), [
            [f'HOME={self.home}', '--force', '--timestamp', '--options', 'runtime', '--sign', IDENTITY,
             '--identifier', IDENTIFIER, binary],
            ['HOME=unset', '--verify', '--strict', '-R', designated_requirement(TEAM), binary],
            ['HOME=unset', '-d', '--verbose=4', binary]])
        provenance = json.loads((self.out / 'seed/provenance.json').read_bytes())
        self.assertEqual(set(provenance), AD_HOC_KEYS | {'team_id', 'identifier', 'hardened_runtime', 'secure_timestamp'})
        self.assertEqual((provenance['kind'], provenance['publisher_identity'], provenance['team_id'],
                          provenance['identifier'], provenance['hardened_runtime'], provenance['secure_timestamp']),
                         ('developer-id-seed', True, TEAM, IDENTIFIER, True, True))
        self.assertEqual(provenance['source_commit'], git(script.parents[1], 'rev-parse', 'HEAD'))
        self.assertEqual(provenance['binary_sha256'], hashlib.sha256(Path(binary).read_bytes()).hexdigest())

    def test_a_certificate_sha1_identity_is_accepted(self):
        script = self.repo(stubbed=True)
        done = self.run_script(script, 'seed', '--sign-identity', SHA1, '--team-id', TEAM)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls()[0][6], SHA1)

    def test_a_weaker_signature_or_a_failing_codesign_refuses(self):
        script = self.repo(stubbed=True)
        cases = {'unauthenticated time': ('display', DISPLAY.replace('Timestamp=', 'Signed Time=')),
                 'no time': ('display', DISPLAY.replace('Timestamp=Oct 21, 2026 at 12:00:00\n', '')),
                 'other team': ('display', DISPLAY.replace(f'TeamIdentifier={TEAM}', 'TeamIdentifier=ZYXWV98765')),
                 'other leaf': ('display', DISPLAY.replace(f'Example ({TEAM})', 'Example (ZYXWV98765)')),
                 'not developer id': ('display', DISPLAY.replace('Developer ID Application:', 'Apple Development:')),
                 'other identifier': ('display', DISPLAY.replace(IDENTIFIER, 'bootstrap-verifier')),
                 'no runtime': ('display', DISPLAY.replace('flags=0x10000(runtime)', 'flags=0x0(none)')),
                 'ad hoc runtime': ('display', DISPLAY.replace('flags=0x10000(runtime)', 'flags=0x10002(adhoc,runtime)')),
                 'verify fails': ('verify-exit', '3'), 'sign fails': ('sign-exit', '1')}
        for index, (label, (name, value)) in enumerate(cases.items()):
            with self.subTest(label):
                previous = (self.control / name).read_text()
                (self.control / name).write_text(value)
                self.refused(self.run_script(script, f'seed-{index}', '--sign-identity', IDENTITY, '--team-id', TEAM),
                             f'seed-{index}')
                self.assertFalse((self.out / f'seed-{index}/provenance.json').exists())
                (self.control / name).write_text(previous)

    def test_the_grammar_refuses_before_anything_is_built(self):
        script = self.repo(stubbed=True)
        cases = [('--sign-identity', 'Developer ID Application: Synthetic Example (ZYXWV98765)', '--team-id', TEAM),
                 ('--sign-identity', IDENTITY, '--team-id', 'abcde12345'),
                 ('--sign-identity', IDENTITY, '--team-id', 'ABCDE1234'),
                 ('--sign-identity', 'Developer ID Application: $(id) (ABCDE12345)', '--team-id', TEAM),
                 ('--sign-identity', SHA1.lower(), '--team-id', TEAM),
                 ('--sign-identity', 'Apple Development: X (ABCDE12345)', '--team-id', TEAM),
                 ('--sign-identity', '', '--team-id', TEAM),
                 ('--team-id', TEAM, '--sign-identity', IDENTITY), ('--sign-identity', IDENTITY),
                 ('--sign-identity', IDENTITY, '--team-id')]
        for index, extra in enumerate(cases):
            with self.subTest(extra=extra):
                self.assertFalse(self.refused(self.run_script(script, f'g{index}', *extra), f'g{index}').exists())
        for index, env in enumerate(({'PATH': '/usr/bin:/bin'}, {'PATH': '/usr/bin:/bin', 'HOME': 'relative'})):
            with self.subTest(env=env):
                done = self.run_script(script, f'h{index}', '--sign-identity', IDENTITY, '--team-id', TEAM, env=env)
                self.assertFalse(self.refused(done, f'h{index}').exists())
        self.assertEqual(self.calls(), [])

    def test_the_prefix_comes_from_product_identity_under_its_grammar(self):
        line = f"CODESIGN_ID_PREFIX: Final = '{product_identity.CODESIGN_ID_PREFIX}'\n"
        broken = {'repeated': lambda s: s.replace(line, line + line),
                  'upper case': lambda s: s.replace(line, "CODESIGN_ID_PREFIX: Final = 'io.Example.product'\n"),
                  'one dot': lambda s: s.replace(line, "CODESIGN_ID_PREFIX: Final = 'example.product'\n"),
                  'empty label': lambda s: s.replace(line, "CODESIGN_ID_PREFIX: Final = 'io..example.product'\n"),
                  'hyphen edge': lambda s: s.replace(line, "CODESIGN_ID_PREFIX: Final = 'io.example.-product'\n"),
                  'other form': lambda s: s.replace(line, 'CODESIGN_ID_PREFIX = "io.example.product"\n')}
        for index, (label, change) in enumerate(broken.items()):
            with self.subTest(label):
                self.assertEqual((ROOT / 'src/telegram_search_mcp/product_identity.py').read_text().count(line), 1)
                script = self.repo(stubbed=True, identity_text=change)
                done = self.run_script(script, f'p{index}', '--sign-identity', IDENTITY, '--team-id', TEAM)
                self.assertFalse(self.refused(done, f'p{index}').exists())
        script = self.repo(stubbed=True, with_identity=False)
        self.assertFalse(self.refused(self.run_script(script, 'missing', '--sign-identity', IDENTITY, '--team-id', TEAM),
                                      'missing').exists())
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
