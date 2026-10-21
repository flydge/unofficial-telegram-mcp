"""Plugin-driven runtime install (CLIENTS-12) against a local stub: no network and no real home.

Each case runs a copy of plugins/<plugin>/bin/install whose only changes are the passwd lookup (a
synthetic home) and the curl path (a stub that serves files from a local directory and records its
argv). The real URL pattern, pins grammar, verification and bootstrap invocation run unchanged.
Published pins carry a Team ID, so the real bootstrap.sh checks the seed's Developer ID requirement:
through a copy whose codesign line is a recorder (success path), and with the stock codesign, which
refuses an ad hoc seed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from telegram_search_mcp import __version__, product_identity as identity
from telegram_search_mcp.native_runtime import designated_requirement
from test_claude_code_modes import BOOTSTRAP, synthetic_kit

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / identity.PLUGIN_NAME
INSTALL = PLUGIN / 'bin' / 'install'
PINS = PLUGIN / 'release-pins.json'
PASSWD_LINE = 'tg_home=$(/usr/bin/env -i /usr/bin/id -P) || tg_refuse home_invalid\n'
CURL_LINE = 'tg_curl=/usr/bin/curl\n'
ASSETS = {'bootstrap.sh': 'bootstrap', 'bootstrap-verifier': 'seed', 'release-index.json': 'index',
          'release.tar.gz': 'archive'}
PIN_KEYS = {'schema_version', 'publication', 'version', 'team_id'} | {
    name + suffix for name in ASSETS.values() for suffix in ('_sha256', '_bytes')}
TEAM = 'ABCDE12345'
CODESIGN_LINE = 'bootstrap_codesign=/usr/bin/codesign\n'
STAGE = re.compile(r'/private/tmp/telegram-bootstrap\.[A-Za-z0-9]{8}')


def base_url():
    repository = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))['project']['urls']['Repository']
    return repository + '/releases/download'


def encode_pins(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


class CommittedFilesTests(unittest.TestCase):
    def test_the_committed_pins_are_the_canonical_unpublished_placeholder_for_this_version(self):
        data = PINS.read_bytes()
        value = json.loads(data)
        self.assertEqual(set(value), PIN_KEYS)
        self.assertEqual(data, encode_pins(value))  # The script compares exactly these bytes.
        plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_text())
        self.assertEqual((value['schema_version'], value['publication'], value['version'], value['team_id']),
                         (1, 'unpublished', __version__, ''))
        self.assertEqual(plugin['version'], value['version'])

    def test_the_script_uses_the_fixed_pattern_one_attempt_and_https_only(self):
        text = INSTALL.read_text()
        self.assertTrue(text.startswith('#!/bin/sh -p\n'))
        self.assertIn("tg_base='" + base_url() + "'\n", text)
        self.assertIn('"$tg_base/v$tg_version/$1"', text)
        self.assertEqual((text.count(PASSWD_LINE), text.count(CURL_LINE)), (1, 1))
        self.assertIn('"$tg_curl" -q --proto =https --proto-redir =https', text)
        self.assertIn('--fail', text)
        code = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
        for forbidden in ('--retry', '--continue-at', ' -C ', '--insecure', ' -k ', 'eval '):
            self.assertFalse(forbidden in code, forbidden)
        self.assertIn('--claude-code plugin', text)
        self.assertTrue(os.access(INSTALL, os.X_OK))


class PluginInstallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-plugin-install-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        self.served = self.base / 'served'
        self.served.mkdir(mode=0o700)
        self.plugin = self.base / 'plugin'
        (self.plugin / 'bin').mkdir(mode=0o700, parents=True)
        (self.plugin / '.claude-plugin').mkdir(mode=0o700)
        shutil.copyfile(PLUGIN / '.claude-plugin' / 'plugin.json', self.plugin / '.claude-plugin' / 'plugin.json')
        self.curl = self.base / 'stub' / 'curl'
        self.curl.parent.mkdir(mode=0o700)
        self.curl.write_text(f'''#!/bin/sh
{{ for a in "$@"; do printf '%s\\n' "$a"; done; printf '%s\\n' '--end--'; }} >> '{self.out}/curl-log'
out=''; url=''
while [ "$#" -gt 0 ]; do case "$1" in --output) out=$2; shift 2;; *) url=$1; shift;; esac; done
name=${{url##*/}}
[ -f '{self.served}/fail-'"$name" ] && exit 22
/bin/cp '{self.served}/'"$name" "$out"
''')
        self.curl.chmod(0o700)
        self.script(INSTALL.read_text())
        self.serve_stub_bootstrap()

    def script(self, text):
        """The shipped script with only the passwd lookup and the curl path redirected."""
        passwd = f'tester:*:501:20::0:0:Test User:{self.home}:/bin/zsh'
        text = text.replace(PASSWD_LINE, f"tg_home='{passwd}'\n").replace(CURL_LINE, f"tg_curl='{self.curl}'\n")
        (self.plugin / 'bin' / 'install').write_text(text)

    def serve_stub_bootstrap(self, *, status='activation_required', installation='installed'):
        (self.served / 'bootstrap.sh').write_text(f'''#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > '{self.out}/bootstrap-argv'
/usr/bin/env > '{self.out}/bootstrap-env'
/usr/bin/stat -f '%u:%Lp' "${{0%/bootstrap.sh}}" > '{self.out}/workspace'
printf '%s\\n' "$0" > '{self.out}/bootstrap-self'
if [ -f '{self.out}/bootstrap-fail' ]; then
    printf '{{"schema_version":1,"error_code":"integrity_failure","retained_stage":""}}\\n' >&2
    exit 6
fi
printf '%s\\n' '{json.dumps({"activation_command": "synthetic", "status": status,
                               **({} if installation is None else {"installation": installation})})}'
''')
        (self.served / 'bootstrap-verifier').write_bytes(b'synthetic seed bytes')
        (self.served / 'release-index.json').write_bytes(b'{"synthetic": "index"}\n')
        (self.served / 'release.tar.gz').write_bytes(b'synthetic archive ' * 64)
        self.pin()

    def pins_for_served(self, **overrides):
        value = {'schema_version': 1, 'publication': 'published', 'version': __version__, 'team_id': TEAM}
        for asset, name in ASSETS.items():
            data = (self.served / asset).read_bytes()
            value[name + '_sha256'] = hashlib.sha256(data).hexdigest()
            value[name + '_bytes'] = len(data)
        value.update(overrides)
        return value

    def pin(self, raw=None, **overrides):
        path = self.plugin / 'release-pins.json'
        path.write_bytes(raw if raw is not None else encode_pins(self.pins_for_served(**overrides)))
        path.chmod(0o644)
        return path

    def run_install(self, *arguments, cwd=None, path=None):
        target = path or str(self.plugin / 'bin' / 'install')
        return subprocess.run(['/bin/sh', '-p', target, *arguments], capture_output=True, text=True, timeout=120,
                              cwd=cwd, env={'TG_PLUGIN_CANARY': 'leak', 'PATH': '/usr/bin:/bin', 'HOME': str(self.base),
                                            'CURL_HOME': str(self.base), 'https_proxy': 'http://example.invalid'})

    def calls(self):
        log = self.out / 'curl-log'
        if not log.exists():
            return []
        calls, current = [], []
        for line in log.read_text().splitlines():
            if line == '--end--':
                calls.append(current); current = []
            else:
                current.append(line)
        return calls

    def workspaces(self):
        return sorted(p for p in self.home.iterdir() if p.name.startswith('.telegram-local-install.'))

    def refusal(self, done, code, *, retained):
        self.assertEqual((done.returncode, done.stdout), (1, ''))
        line = json.loads(done.stderr.splitlines()[-1])
        if retained:
            [workspace] = self.workspaces()
            self.assertEqual(line, {'schema_version': 1, 'error_code': code, 'retained': str(workspace)})
            self.assertEqual(oct(workspace.stat().st_mode & 0o777), '0o700')
            return workspace
        self.assertEqual(line, {'schema_version': 1, 'error_code': code})
        self.assertEqual(self.workspaces(), [])
        return None

    # --- success ---------------------------------------------------------------------------

    def test_success_verifies_every_asset_runs_bootstrap_in_plugin_mode_and_removes_the_workspace(self):
        done = self.run_install()
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(done.stdout), {'activation_command': 'synthetic', 'status': 'activation_required',
                                                   'installation': 'installed'})
        self.assertEqual(self.workspaces(), [])
        workspace = Path((self.out / 'bootstrap-self').read_text().strip()).parent
        self.assertEqual(workspace.parent, self.home)
        self.assertRegex(workspace.name, r'^\.telegram-local-install\.[A-Za-z0-9]{8}$')
        self.assertEqual((self.out / 'workspace').read_text().strip(), f'{os.getuid()}:700')
        pins = self.pins_for_served()
        self.assertEqual((self.out / 'bootstrap-argv').read_text().splitlines(), [
            '--seed', str(workspace / 'bootstrap-verifier'), '--seed-sha256', pins['seed_sha256'],
            '--seed-bytes', str(pins['seed_bytes']), '--archive', str(workspace / 'release.tar.gz'),
            '--index', str(workspace / 'release-index.json'), '--trusted-index-sha256', pins['index_sha256'],
            '--claude-code', 'plugin', '--seed-team', TEAM])
        names = {row.split('=', 1)[0] for row in (self.out / 'bootstrap-env').read_text().splitlines()}
        self.assertFalse(names & {'TG_PLUGIN_CANARY', 'HOME', 'PATH', 'CURL_HOME', 'https_proxy'}, names)
        calls = self.calls()
        self.assertEqual([call[-1] for call in calls],
                         [base_url() + '/v' + __version__ + '/' + asset for asset in ASSETS])
        for call, asset in zip(calls, ASSETS):
            self.assertEqual(call[0], '-q')  # No curl config file is read.
            for flag in (['--proto', '=https'], ['--proto-redir', '=https'], ['--fail'],
                         ['--max-filesize', str(pins[ASSETS[asset] + '_bytes'])]):
                self.assertTrue(any(call[i:i + len(flag)] == flag for i in range(len(call))), (flag, call))
            self.assertFalse([a for a in call if a.startswith(('--retry', '--continue-at', '-C'))], call)

    def test_published_pins_without_a_team_refuse_before_any_download(self):
        self.pin(team_id='')
        self.refusal(self.run_install(), 'pins_invalid', retained=False)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.out / 'bootstrap-argv').exists())

    def serve_real_bootstrap(self, *, codesign=None):
        """The synthetic seed kit and the real bootstrap.sh, its codesign line optionally a recorder."""
        kit, _, _, _ = synthetic_kit(self.base / 'kit', self.home, self.out)
        for asset, source in zip(('bootstrap-verifier', 'release-index.json', 'release.tar.gz'), kit):
            shutil.copyfile(source, self.served / asset)
        text = BOOTSTRAP.read_text()
        self.assertEqual(text.count(CODESIGN_LINE), 1)
        if codesign is not None:
            text = text.replace(CODESIGN_LINE, f"bootstrap_codesign='{codesign}'\n")
        (self.served / 'bootstrap.sh').write_text(text)
        self.pin()

    def test_end_to_end_through_the_real_bootstrap_with_a_synthetic_seed(self):
        codesign = self.base / 'stub' / 'codesign'
        codesign.write_text(f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{self.out}/codesign-argv'\n")
        codesign.chmod(0o700)
        self.serve_real_bootstrap(codesign=codesign)
        done = self.run_install()
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(done.stdout)['claude_code'], 'plugin')
        argv = (self.out / 'launcher-argv').read_text().splitlines()
        stage = argv[3][:-len('/release/launcher.py')]
        self.assertEqual((self.out / 'codesign-argv').read_text().splitlines(),
                         ['--verify', '--strict', '-R', designated_requirement(TEAM), stage + '/seed'])
        self.assertEqual(argv[4:], ['bootstrap-install',
                                    '--installation-root', str(self.home / 'Library/Application Support/TelegramSearch'),
                                    '--codex-home', str(self.home / '.codex'), '--skills-root', str(self.home / '.agents/skills'),
                                    '--index', stage + '/verified-index.json',
                                    '--trusted-index-sha256', self.pins_for_served()['index_sha256'],
                                    '--claude-code', 'plugin'])
        self.assertFalse(Path(stage).exists())
        self.assertEqual(self.workspaces(), [])
        self.assertFalse((self.home / 'Library').exists())  # The stand-in installs nothing.

    def test_the_real_bootstrap_refuses_an_ad_hoc_seed_under_the_pinned_team(self):
        self.serve_real_bootstrap()  # The stock /usr/bin/codesign.
        seed = self.served / 'bootstrap-verifier'
        seed.unlink()
        shutil.copyfile('/usr/bin/true', seed)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(seed)], check=True, capture_output=True,
                       timeout=60)
        self.pin()
        done = self.run_install()
        workspace = self.refusal(done, 'bootstrap_failed', retained=True)
        line = json.loads(done.stderr.splitlines()[0])  # bootstrap.sh's own fixed line.
        self.assertEqual(line['error_code'], 'seed_signature_invalid')
        self.assertIsNotNone(STAGE.fullmatch(line['retained_stage']))
        shutil.rmtree(line['retained_stage'])
        self.assertFalse((self.out / 'launcher-argv').exists())
        self.assertTrue((workspace / 'bootstrap-verifier').is_file())

    # --- pins are enforced before anything runs ------------------------------------------------

    def test_a_wrong_pin_is_refused_before_any_execution_and_the_workspace_is_retained(self):
        for asset, name in ASSETS.items():
            for field in ('sha256', 'bytes'):
                with self.subTest(asset=asset, field=field):
                    self.setUp()
                    pins = self.pins_for_served()
                    wrong = 'f' * 64 if field == 'sha256' else pins[name + '_bytes'] + 1
                    self.pin(**{name + '_' + field: wrong})
                    workspace = self.refusal(self.run_install(), 'pin_mismatch', retained=True)
                    self.assertFalse((self.out / 'bootstrap-argv').exists())
                    order = list(ASSETS)
                    self.assertEqual([call[-1].rsplit('/', 1)[1] for call in self.calls()], order[:order.index(asset) + 1])
                    self.assertEqual(sorted(p.name for p in workspace.iterdir()), sorted(order[:order.index(asset) + 1]))

    def test_a_failed_download_is_never_retried_and_stops_every_later_asset(self):
        for asset in ASSETS:
            with self.subTest(asset=asset):
                self.setUp()
                (self.served / ('fail-' + asset)).write_bytes(b'')
                self.refusal(self.run_install(), 'download_failed', retained=True)
                order = list(ASSETS)
                self.assertEqual([call[-1].rsplit('/', 1)[1] for call in self.calls()], order[:order.index(asset) + 1])
                self.assertFalse((self.out / 'bootstrap-argv').exists())

    def test_an_upgrade_or_a_repeat_is_a_success_like_a_first_install(self):
        for status, installation in (('activation_required', 'installed'), ('activation_required', 'reused'),
                                     ('upgraded', 'upgraded'), ('upgraded', 'reused'), ('rolled_back', 'reused')):
            with self.subTest(status=status, installation=installation):
                self.setUp()
                self.serve_stub_bootstrap(status=status, installation=installation)
                done = self.run_install()
                self.assertEqual((done.returncode, done.stderr), (0, ''))
                self.assertEqual(json.loads(done.stdout), {'activation_command': 'synthetic', 'status': status,
                                                           'installation': installation})
                self.assertEqual(self.workspaces(), [])
                self.assertEqual((self.out / 'bootstrap-argv').read_text().splitlines()[-4:],
                                 ['--claude-code', 'plugin', '--seed-team', TEAM])

    def test_a_bootstrap_refusal_or_an_unexpected_result_retains_the_workspace(self):
        (self.out / 'bootstrap-fail').write_bytes(b'')
        done = self.run_install()
        workspace = self.refusal(done, 'bootstrap_failed', retained=True)
        self.assertEqual(json.loads(done.stderr.splitlines()[0])['error_code'], 'integrity_failure')  # bootstrap's own line.
        self.assertTrue((workspace / 'release.tar.gz').is_file())
        for status, installation in (('refused', None), ('upgraded', None), ('upgraded', 'installed'),
                                     ('rolled_back', 'rolled_back'), ('rolled_back', 'upgraded'), ('uninstalled', 'removed'),
                                     ('activation_required', None), ('activation_required', 'upgraded'),
                                     ('activation_required', 'rolled_back')):
            with self.subTest(status=status, installation=installation):
                self.setUp()
                self.serve_stub_bootstrap(status=status, installation=installation)
                self.refusal(self.run_install(), 'result_invalid', retained=True)

    def test_the_unpublished_placeholder_refuses_before_any_download(self):
        self.pin(raw=PINS.read_bytes())
        self.refusal(self.run_install(), 'release_not_published', retained=False)
        self.assertEqual(self.calls(), [])

    def test_malformed_or_unsafe_pins_refuse_before_any_download(self):
        good = self.pins_for_served()
        text = encode_pins(good).decode()
        cases = {
            'extra key': encode_pins({**good, 'url': 'https://example.invalid'}),
            'duplicate key': text.replace('{\n', '{\n  "seed_sha256": "' + 'a' * 64 + '",\n', 1).encode(),
            'compact': json.dumps(good, sort_keys=True).encode() + b'\n',
            'no newline': text.rstrip('\n').encode(),
            'schema 2': encode_pins({**good, 'schema_version': 2}),
            'string bytes': encode_pins({**good, 'seed_bytes': str(good['seed_bytes'])}),
            'upper hash': encode_pins({**good, 'index_sha256': good['index_sha256'].upper()}),
            'short hash': encode_pins({**good, 'index_sha256': good['index_sha256'][:63]}),
            'other version': encode_pins({**good, 'version': '9.9.9'}),
            'version traversal': encode_pins({**good, 'version': '../x'}),
            'bad team': encode_pins({**good, 'team_id': 'abcde12345'}),
            'no team': encode_pins({**good, 'team_id': ''}),
            'short team': encode_pins({**good, 'team_id': 'ABCDE'}),
            'unknown publication': encode_pins({**good, 'publication': 'draft'}),
            'zero bytes': encode_pins({**good, 'archive_bytes': 0}),
            'oversized seed': encode_pins({**good, 'seed_bytes': 2097153}),
            'not json': b'pins\n',
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.setUp()
                self.pin(raw=raw)
                self.refusal(self.run_install(), 'pins_invalid', retained=False)
                self.assertEqual(self.calls(), [])
        for label in ('group writable', 'symlink', 'missing'):
            with self.subTest(label):
                self.setUp()
                path = self.pin()
                if label == 'group writable':
                    path.chmod(0o664)
                elif label == 'symlink':
                    (self.base / 'pins.json').write_bytes(path.read_bytes())
                    path.unlink()
                    path.symlink_to(self.base / 'pins.json')
                else:
                    path.unlink()
                self.refusal(self.run_install(), 'pins_invalid', retained=False)
                self.assertEqual(self.calls(), [])

    def test_arguments_a_relative_invocation_or_an_unusable_home_refuse(self):
        self.refusal(self.run_install('--claude-code', 'user'), 'invalid_arguments', retained=False)
        self.refusal(self.run_install(cwd=str(self.plugin), path='bin/install'), 'plugin_invalid', retained=False)
        self.home.chmod(0o770)
        self.refusal(self.run_install(), 'home_invalid', retained=False)
        self.home.chmod(0o700)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_install().returncode, 0)


if __name__ == '__main__':
    unittest.main()
