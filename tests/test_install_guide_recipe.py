"""The install guide's curl recipe, run as written (docs/install.md, "Curl path").

The block is taken from the guide and run in zsh, bash and sh, each both with -c and as an interactive
shell reading it from stdin the way a paste into Terminal does (an interactive zsh runs a `#` word as a
command), with only its version placeholder filled and /usr/bin/curl replaced by a stub that serves a
synthetic release from a local folder. It must take
the pins from the tagged repository path, never from the release assets, and run bootstrap.sh with
the pinned arguments only when the Team ID, the publication, the version and all four files match.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'docs' / 'install.md'
TEAM = '256X5MUA6S'
VERSION = '9.9.9'
ASSETS = (('bootstrap', 'bootstrap.sh'), ('seed', 'bootstrap-verifier'), ('index', 'release-index.json'),
          ('archive', 'release.tar.gz'))
# (argv, pasted): a pasted run feeds the block to the interactive shell on stdin instead of -c.
SHELLS = ((('/bin/zsh', '-f'), False), (('/bin/bash', '--norc'), False), (('/bin/sh',), False),
          (('/bin/zsh', '-f', '-i'), True), (('/bin/bash', '--norc', '-i'), True), (('/bin/sh', '-i'), True))


def recipe():
    section = GUIDE.read_text().split('\n### Curl path\n', 1)[1].split('\n### ', 1)[0]
    [block] = re.findall(r'```bash\n(.*?)```', section, re.S)
    return block


class CurlRecipeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-recipe-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.served = self.base / 'served'
        self.served.mkdir()
        self.log = self.base / 'urls'
        curl = self.base / 'curl'
        curl.write_text(f'''#!/bin/sh
out=''; url=''
while [ "$#" -gt 0 ]; do case "$1" in --output) out=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done
printf '%s\\n' "$url" >> '{self.log}'
[ -f '{self.served}/'"${{url##*/}}" ] || exit 22
/bin/cp '{self.served}/'"${{url##*/}}" "$out"
''')
        curl.chmod(0o700)
        text = recipe()
        self.assertEqual(text.count('VERSION=<version>'), 1)
        self.assertNotIn('$HOME/tgmcp-kit/release-pins.json', text)
        self.assertEqual([line for line in text.splitlines() if '#' in line], [])  # No comment to paste.
        self.script = text.replace('VERSION=<version>', f'VERSION={VERSION}').replace('/usr/bin/curl', str(curl))
        (self.served / 'bootstrap.sh').write_text(f'''#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > '{self.base}/bootstrap-argv'
''')
        (self.served / 'bootstrap-verifier').write_bytes(b'synthetic seed')
        (self.served / 'release-index.json').write_bytes(b'{"synthetic": "index"}\n')
        (self.served / 'release.tar.gz').write_bytes(b'synthetic archive ' * 32)

    def pins(self, **overrides):
        value = {'schema_version': 1, 'version': VERSION, 'publication': 'published', 'team_id': TEAM}
        for name, asset in ASSETS:
            data = (self.served / asset).read_bytes()
            value.update({name + '_sha256': hashlib.sha256(data).hexdigest(), name + '_bytes': len(data)})
        value.update(overrides)
        (self.served / 'release-pins.json').write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
        return value

    def run_recipe(self, shell, fresh=True):
        for path in (self.home / 'tgmcp-kit', self.log, self.base / 'bootstrap-argv') if fresh else ():
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
        argv, pasted = shell
        feed = {'input': self.script} if pasted else {'stdin': subprocess.DEVNULL}
        return subprocess.run([*argv] if pasted else [*argv, '-c', self.script], capture_output=True, text=True,
                              timeout=60, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, **feed)

    def test_the_tagged_pins_are_checked_before_bootstrap_runs_with_them(self):
        pins = self.pins()
        for shell in SHELLS:
            with self.subTest(shell=' '.join(shell[0])):
                done = self.run_recipe(shell)
                self.assertEqual(done.returncode, 0, done.stderr)
                urls = self.log.read_text().splitlines()
                self.assertEqual(urls[0], 'https://raw.githubusercontent.com/flydge/unofficial-telegram-mcp/'
                                          f'v{VERSION}/plugins/telegram-local/release-pins.json')
                self.assertEqual(urls[1:], [f'https://github.com/flydge/unofficial-telegram-mcp/releases/download/'
                                            f'v{VERSION}/{asset}' for _, asset in ASSETS])
                kit = self.home / 'tgmcp-kit'
                self.assertEqual((self.base / 'bootstrap-argv').read_text().splitlines(), [
                    '--seed', f'{kit}/bootstrap-verifier', '--seed-sha256', pins['seed_sha256'],
                    '--seed-bytes', str(pins['seed_bytes']), '--archive', f'{kit}/release.tar.gz',
                    '--index', f'{kit}/release-index.json', '--trusted-index-sha256', pins['index_sha256'],
                    '--seed-team', TEAM, '--claude-code', 'user'])

    def test_any_mismatch_stops_before_anything_runs(self):
        cases = {'other team': {'team_id': 'ABCDE12345'}, 'no team': {'team_id': ''},
                 'unreleased': {'publication': 'unreleased'}, 'other version': {'version': '9.9.8'}}
        for name, _ in ASSETS:
            cases[name + ' hash'] = {name + '_sha256': 'f' * 64}
            cases[name + ' size'] = {name + '_bytes': 1}
        for shell in SHELLS:
            for label, overrides in cases.items():
                with self.subTest(shell=' '.join(shell[0]), case=label):
                    self.pins(**overrides)
                    done = self.run_recipe(shell)
                    self.assertIn('Stopped:', done.stdout)
                    self.assertFalse((self.base / 'bootstrap-argv').exists())
        self.pins()
        (self.served / 'release.tar.gz').rename(self.base / 'archive')  # A failed download: nothing runs.
        for shell in SHELLS:
            with self.subTest(shell=' '.join(shell[0]), case='failed download'):
                done = self.run_recipe(shell)
                self.assertIn('Stopped:', done.stdout)
                self.assertFalse((self.base / 'bootstrap-argv').exists())
        (self.base / 'archive').rename(self.served / 'release.tar.gz')
        for shell in SHELLS:  # An existing folder, even with matching files, is not the new download.
            with self.subTest(shell=' '.join(shell[0]), case='existing folder'):
                self.run_recipe(shell)
                (self.base / 'bootstrap-argv').unlink()
                done = self.run_recipe(shell, fresh=False)
                self.assertIn('Stopped:', done.stdout)
                self.assertFalse((self.base / 'bootstrap-argv').exists())


if __name__ == '__main__':
    unittest.main()
