"""One release-pins.json schema (CLIENTS-12, CLIENTS-14): release_build writes it, bin/install reads it.

release_pins.parse mirrors bin/install's checks; these tests hold the two to each other. The plugin's
real bin/install (only its passwd lookup and curl path redirected, as in test_plugin_install) runs
against pins files that parse and pins files that do not, and against the kit of a synthetic
release_build run served by a stub curl: the build's own release-pins.json, copied as the plugin's,
passes every check, every asset matches it and the pinned bootstrap.sh runs with the pinned arguments.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from telegram_search_mcp import bundle_runtime, native_runtime
from test_plugin_install import CURL_LINE, INSTALL, PASSWD_LINE, PINS as COMMITTED, PLUGIN
from test_release_build import IDENTITY, PINS, RB, TEAM, VERSION, Fakes, git, sha

ZERO = '0' * 64


def install_text():
    return INSTALL.read_text()


def valid(publication='published', team=TEAM, version=VERSION):
    assets = {name: (sha(name.encode()), index + 1) for index, (name, _, _) in enumerate(PINS.ASSETS)}
    return PINS.encode(version=version, publication=publication, team_id=team, assets=assets)


class SchemaTests(unittest.TestCase):
    def test_the_publication_vocabulary_is_one_set(self):
        [pattern] = re.findall(r'case "\$tg_publication" in ([a-z|]+)\)', install_text())
        self.assertEqual(set(pattern.split('|')), set(PINS.PUBLICATIONS))
        self.assertEqual(tuple(PINS.PUBLICATIONS), tuple(bundle_runtime.PUBLICATIONS))  # The manifest's.

    def test_bin_install_compares_exactly_the_schema_keys_and_bounds(self):
        text = install_text()
        [layout] = re.findall(r"tg_expected=\$\(printf '([^']+)'", text)
        self.assertEqual(re.findall(r'"([a-z0-9_]+)": ', layout), sorted(PINS.KEYS))
        for name, asset, bound in PINS.ASSETS:
            self.assertIn(f'[ "$tg_{name}_bytes" -ge 1 ] && [ "$tg_{name}_bytes" -le {bound} ]', text)
            self.assertIn(f'tg_fetch {asset} "$tg_{name}_bytes"', text)

    def test_the_committed_plugin_pins_have_the_schema(self):
        value = PINS.parse(COMMITTED.read_bytes())
        plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_bytes())
        self.assertEqual(value['version'], plugin['version'])
        if value['publication'] == 'unreleased' and value['team_id'] == '':  # The placeholder until a release commit.
            self.assertEqual({value[k] for k in value if k.endswith('_sha256')}, {ZERO})
            self.assertEqual({value[k] for k in value if k.endswith('_bytes')}, {0})
        else:  # A release commit's copy of the build's pins: the reviewed publisher Team ID.
            self.assertIn(value['team_id'], native_runtime.APPROVED_PINS[0].team_ids)

    def test_encode_refuses_what_parse_refuses(self):
        for options in ({'publication': 'published', 'team': ''}, {'publication': 'draft'}, {'team': 'abcde12345'},
                        {'version': '1..2'}, {'version': 'v1.2.3'}):
            with self.subTest(options=options), self.assertRaises(PINS.PinsError) as caught:
                valid(**options)
            self.assertEqual(str(caught.exception), 'pins_invalid')


class InstallerHarness:
    """The plugin's real bin/install in a disposable plugin folder, home and served-assets folder."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-release-pins-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.served = self.base / 'served'
        self.served.mkdir(mode=0o700)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        self.plugin = self.base / 'plugin'
        (self.plugin / 'bin').mkdir(mode=0o700, parents=True)
        (self.plugin / '.claude-plugin').mkdir(mode=0o700)
        plugin = json.loads((PLUGIN / '.claude-plugin' / 'plugin.json').read_bytes())
        (self.plugin / '.claude-plugin' / 'plugin.json').write_text(json.dumps({**plugin, 'version': VERSION}))
        curl = self.base / 'curl'
        curl.write_text(f'''#!/bin/sh
out=''; url=''
while [ "$#" -gt 0 ]; do case "$1" in --output) out=$2; shift 2;; *) url=$1; shift;; esac; done
printf '%s\\n' "$url" >> '{self.out}/urls'
[ -f '{self.served}/'"${{url##*/}}" ] || exit 22
/bin/cp '{self.served}/'"${{url##*/}}" "$out"
''')
        curl.chmod(0o700)
        passwd = f'tester:*:501:20::0:0:Test User:{self.home}:/bin/zsh'
        text = install_text()
        self.assertEqual((text.count(PASSWD_LINE), text.count(CURL_LINE)), (1, 1))
        (self.plugin / 'bin' / 'install').write_text(
            text.replace(PASSWD_LINE, f"tg_home='{passwd}'\n").replace(CURL_LINE, f"tg_curl='{curl}'\n"))

    def run_install(self, pins):
        (self.plugin / 'release-pins.json').write_bytes(pins)
        (self.plugin / 'release-pins.json').chmod(0o644)
        done = subprocess.run(['/bin/sh', '-p', str(self.plugin / 'bin' / 'install')], capture_output=True, text=True,
                              timeout=120, env={'PATH': '/usr/bin:/bin'})
        for workspace in self.home.glob('.telegram-local-install.*'):
            shutil.rmtree(workspace)
        return done

    def code(self, done):
        return json.loads(done.stderr.splitlines()[-1])['error_code'] if done.returncode else None


class InstallerAgreementTests(InstallerHarness, unittest.TestCase):
    """bin/install itself on each pins file: its verdict and parse's agree."""

    def test_bin_install_refuses_as_pins_invalid_exactly_what_parse_refuses(self):
        good = json.loads(valid())
        cases = {
            'published': valid(),
            'unreleased placeholder': PINS.canonical({**{k: ZERO if k.endswith('_sha256') else 0 for k in PINS.KEYS},
                                                      'schema_version': 1, 'version': VERSION,
                                                      'publication': 'unreleased', 'team_id': ''}),
            'unreleased with a team': valid('unreleased'),
            'old vocabulary': PINS.canonical({**good, 'publication': 'unpublished'}),
            'published without a team': PINS.canonical({**good, 'team_id': ''}),
            'lowercase team': PINS.canonical({**good, 'team_id': 'abcde12345'}),
            'zero archive': PINS.canonical({**good, 'archive_bytes': 0}),
            'oversized bootstrap': PINS.canonical({**good, 'bootstrap_bytes': 262145}),
            'bootstrap at its bound': PINS.canonical({**good, 'bootstrap_bytes': 262144}),
            'thirteen digits': PINS.canonical({**good, 'seed_bytes': 10 ** 12}),
            'float size': PINS.canonical({**good, 'seed_bytes': 1.0}),
            'upper hash': PINS.canonical({**good, 'index_sha256': good['index_sha256'].upper()}),
            'extra key': PINS.canonical({**good, 'url': 'https://example.invalid'}),
            'missing key': PINS.canonical({k: v for k, v in good.items() if k != 'bootstrap_bytes'}),
            'schema 2': PINS.canonical({**good, 'schema_version': 2}),
            'compact': json.dumps(good, sort_keys=True).encode() + b'\n',
            'unsorted': (json.dumps(good, indent=2) + '\n').encode(),
            'no newline': valid().rstrip(b'\n'),
        }
        for label, data in cases.items():
            with self.subTest(label):
                try:
                    PINS.parse(data)
                    parsed = True
                except PINS.PinsError:
                    parsed = False
                code = self.code(self.run_install(data))
                # Pins that parse get past every pins check: unreleased ones stop at the publication gate,
                # published ones at the first download (nothing is served here).
                self.assertEqual(code != 'pins_invalid', parsed, code)
                self.assertIn(code, ('release_not_published', 'download_failed') if parsed else ('pins_invalid',))


class ReleaseBuildThroughPluginInstallTests(InstallerHarness, unittest.TestCase):
    """A synthetic Developer ID release_build run, then bin/install with its pins over its kit."""

    BOOTSTRAP = '''#!/bin/sh -p
for a in "$@"; do printf '%s\\n' "$a"; done > "$TG_OUT/bootstrap-argv"
printf '%s\\n' '{"status":"activation_required","installation":"installed"}'
'''

    def setUp(self):
        super().setUp()
        self.root = self.base / 'build'
        self.repo = self.root / 'repo'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-q')
        self.v1 = json.dumps({'manifest_version': 1, 'tdlib_version': '1.8.67'}).encode()
        bootstrap = self.BOOTSTRAP.replace('"$TG_OUT', f'"{self.out}')
        files = {'packaging/bootstrap.sh': bootstrap.encode(), 'packaging/tdlib-lock.json': self.v1,
                 'packaging/payload-lock.json': b'{}', 'LICENSE': b'license\n', 'NOTICE': b'notice\n',
                 'docs/third-party-notices.md': b'# notices\n'}
        for name, data in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'synthetic')
        self.commit = git(self.repo, 'rev-parse', 'HEAD')
        (self.root / 'native').mkdir()
        (self.root / 'cache').mkdir(mode=0o700)

    def build(self, name, publication, **options):
        fakes = Fakes(self)
        fakes.publication = publication
        RB.release_build(self.root / name, native_directory=self.root / 'native', cache=self.root / 'cache',
                         checkout=self.repo, offline=True, steps=fakes.steps(), **options)
        kit = self.root / name / 'kit'
        for _, asset, _ in PINS.ASSETS:
            shutil.copyfile(kit / asset, self.served / asset)
        return kit

    def test_the_build_pins_install_through_the_plugin_installer(self):
        kit = self.build('out', 'published', identity=IDENTITY, team_id=TEAM, notary_profile='release-notary')
        data = (kit / 'release-pins.json').read_bytes()
        pins = PINS.parse(data)
        self.assertEqual((pins['publication'], pins['team_id'], pins['version']), ('published', TEAM, VERSION))
        done = self.run_install(data)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(done.stdout), {'status': 'activation_required', 'installation': 'installed'})
        self.assertEqual([url.rsplit('/', 2)[1:] for url in (self.out / 'urls').read_text().splitlines()],
                         [['v' + VERSION, asset] for _, asset, _ in PINS.ASSETS])
        argv = (self.out / 'bootstrap-argv').read_text().splitlines()
        workspace = argv[1].rsplit('/', 1)[0]
        self.assertEqual(argv, ['--seed', workspace + '/bootstrap-verifier', '--seed-sha256', pins['seed_sha256'],
                                '--seed-bytes', str(pins['seed_bytes']), '--archive', workspace + '/release.tar.gz',
                                '--index', workspace + '/release-index.json',
                                '--trusted-index-sha256', pins['index_sha256'], '--claude-code', 'plugin',
                                '--seed-team', TEAM])
        # Install.command was rendered from the same bytes.
        command = (kit / 'Install.command').read_text()
        for line in (f"install_seed_sha256={pins['seed_sha256']}\n", f"install_seed_bytes={pins['seed_bytes']}\n",
                     f"install_index_sha256={pins['index_sha256']}\n", f"install_seed_team='{TEAM}'\n"):
            self.assertIn(line, command)

    def test_a_changed_asset_is_refused_against_the_build_pins(self):
        kit = self.build('out', 'published', identity=IDENTITY, team_id=TEAM, notary_profile='release-notary')
        (self.served / 'bootstrap.sh').write_bytes((kit / 'bootstrap.sh').read_bytes() + b'# changed\n')
        done = self.run_install((kit / 'release-pins.json').read_bytes())
        self.assertEqual(self.code(done), 'pin_mismatch')
        self.assertFalse((self.out / 'bootstrap-argv').exists())

    def test_a_dry_run_build_pins_parse_and_stop_at_the_publication_gate(self):
        kit = self.build('adhoc', 'unreleased', dry_run=True)
        data = (kit / 'release-pins.json').read_bytes()
        self.assertEqual(PINS.parse(data)['team_id'], '')
        self.assertEqual(self.code(self.run_install(data)), 'release_not_published')
        self.assertFalse((self.out / 'urls').exists())


if __name__ == '__main__':
    unittest.main()
