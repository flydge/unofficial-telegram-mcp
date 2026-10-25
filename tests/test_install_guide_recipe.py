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
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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


RECIPE_SECONDS = 60


def run_shell(argv, script, *, pasted, env, timeout=None):
    """One shell run of `script`: with -c, or (pasted) fed on stdin to the shell as given. Bounded in every case:
    the shell runs in its own session (no controlling terminal, so an interactive shell never stops our group),
    its output goes to files (it can never block on a pipe we have to drain), the paste is written by a thread (a
    blocked write never holds the deadline), and at the deadline, or when the shell exits, its whole process
    group is killed, which also ends a blocked write. A timeout raises subprocess.TimeoutExpired."""
    limit = RECIPE_SECONDS if timeout is None else timeout
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen([*argv] if pasted else [*argv, '-c', script],
                                   stdin=subprocess.PIPE if pasted else subprocess.DEVNULL, stdout=out, stderr=err,
                                   env=env, start_new_session=True)
        writer = None
        if pasted:
            def paste(stream=process.stdin, data=script.encode()):
                try:
                    stream.write(data)
                    stream.close()
                except OSError:  # The shell stopped reading (exited or was killed): the paste ends there.
                    pass
            writer = threading.Thread(target=paste, daemon=True)
            writer.start()
        try:
            returncode = process.wait(timeout=limit)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
            if writer is not None:
                writer.join(timeout=10)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, returncode, out.read().decode(errors='replace'),
                                           err.read().decode(errors='replace'))


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
        return run_shell(argv, self.script, pasted=pasted, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'})

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


# Runs run_shell in a child process: the stop log of 25.10 sampled a pasted `bash --norc -i` and its writer
# deadlocked on pipes (the shell blocked on its full stderr pipe while the runner sat in a write to its stdin, so
# the timeout could never fire). The selector stands in for the sampled host: it reports the stdin pipe writable,
# and nothing else, while input is left, so the runner writes before it drains and a blocking write can start
# into a full pipe. `prompts` is that interactive bash; `silent` is a shell that never reads and never exits.
GUARDED = r"""
import json, selectors, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import test_install_guide_recipe as recipe


class WritesFirst(subprocess._PopenSelector):
    def select(self, timeout=None):
        writers = [(key, selectors.EVENT_WRITE) for key in self.get_map().values()
                   if key.events & selectors.EVENT_WRITE]
        return writers or super().select(timeout)


subprocess._PopenSelector = WritesFirst
mode, home = sys.argv[2], sys.argv[3]
env = {'HOME': home, 'PATH': '/usr/bin:/bin'}
started = time.monotonic()
try:
    if mode == 'prompts':
        done = recipe.run_shell(('/bin/bash', '--norc', '-i'), ':\n' * 100000, pasted=True, env=env, timeout=20)
    else:
        done = recipe.run_shell(('/bin/sleep', '613'), 'x' * 200000, pasted=True, env=env, timeout=2)
    outcome = {'outcome': 'completed', 'returncode': done.returncode}
except subprocess.TimeoutExpired:
    outcome = {'outcome': 'timeout'}
outcome['seconds'] = round(time.monotonic() - started, 1)
print(json.dumps(outcome))
"""
GUARD_SECONDS = 45


class BoundedRunTests(unittest.TestCase):
    """run_shell is bounded in every case: a shell blocked on its own output, or one that never reads or exits,
    cannot hold the runner past its timeout, and no process of the run outlives it."""

    def guarded(self, mode):
        with tempfile.TemporaryDirectory(prefix='tg-recipe-guard-') as home:
            child = subprocess.Popen([sys.executable, '-I', '-c', GUARDED, str(ROOT / 'tests'), mode, home],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
            try:
                out, err = child.communicate(timeout=GUARD_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                self.fail(f'run_shell hung past its timeout ({mode})')
            self.assertEqual(child.returncode, 0, err.decode(errors='replace')[-2000:])
            return json.loads(out.decode().strip().splitlines()[-1])

    def test_a_pasted_interactive_shell_blocked_on_its_prompts_cannot_deadlock_the_writer(self):
        outcome = self.guarded('prompts')
        self.assertEqual((outcome['outcome'], outcome.get('returncode')), ('completed', 0))

    def test_a_shell_that_never_reads_is_killed_at_the_timeout_with_its_whole_group(self):
        outcome = self.guarded('silent')
        self.assertEqual(outcome['outcome'], 'timeout')
        self.assertLess(outcome['seconds'], 15)
        listed = subprocess.run(['/bin/ps', '-axo', 'command'], capture_output=True, text=True, timeout=30).stdout
        self.assertNotIn('/bin/sleep 613', listed)


if __name__ == '__main__':
    unittest.main()
