"""bootstrap.sh --seed-team (CLIENTS-7): the staged seed's Developer ID designated requirement.

bootstrap.sh runs for real. With a stubbed codesign (a copy of the script whose one tool line points
at a recorder) the exact requirement, its place after the sha256/bytes pins and before the seed, and the
kept quarantine are observed; with the stock /usr/bin/codesign an unsigned, an ad hoc and an
other-team (Apple-signed) seed are each refused with the fixed code. A refused run retains its
/private/tmp/telegram-bootstrap.* stage, which each case removes by its exact path.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from telegram_search_mcp.native_runtime import designated_requirement
from test_claude_code_modes import BOOTSTRAP, synthetic_kit

TEAM = 'ABCDE12345'
TOOL_LINE = 'bootstrap_codesign=/usr/bin/codesign\n'
STAGE = re.compile(r'/private/tmp/telegram-bootstrap\.[A-Za-z0-9]{8}')


class SeedTeamTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-seed-team-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        (self.seed, self.index, self.archive), self.seed_sha, self.seed_bytes, self.index_sha = synthetic_kit(
            self.base / 'kit', self.home, self.out)
        self.codesign = self.base / 'stub' / 'codesign'
        self.codesign.parent.mkdir(mode=0o700)
        self.codesign.write_text(f'''#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > '{self.out}/codesign-argv'
for last in "$@"; do :; done
if [ -e '{self.out}/seed-ran' ]; then echo after; else echo before; fi > '{self.out}/codesign-order'
/usr/bin/xattr -p com.apple.quarantine "$last" > '{self.out}/codesign-quarantine' 2>/dev/null || :
exit "$(/bin/cat '{self.out}/codesign-exit')"
''')
        self.codesign.chmod(0o700)
        (self.out / 'codesign-exit').write_text('0')
        text = BOOTSTRAP.read_text()
        self.assertEqual(text.count(TOOL_LINE), 1)
        self.stubbed = self.base / 'bootstrap.sh'
        self.stubbed.write_text(text.replace(TOOL_LINE, f"bootstrap_codesign='{self.codesign}'\n"))

    def use_seed(self, data):
        self.seed.chmod(0o600)
        self.seed.write_bytes(data)
        self.seed_sha, self.seed_bytes = hashlib.sha256(data).hexdigest(), len(data)

    def bootstrap(self, *extra, script=None):
        argv = ['/bin/sh', '-p', str(script or BOOTSTRAP), '--seed', str(self.seed), '--seed-sha256', self.seed_sha,
                '--seed-bytes', str(self.seed_bytes), '--archive', str(self.archive), '--index', str(self.index),
                '--trusted-index-sha256', self.index_sha, '--installation-root', str(self.home / 'runtime'), *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, env={'PATH': '/usr/bin:/bin'})

    def refused(self, done, code):
        """The fixed refusal; the retained stage is removed by its exact path."""
        self.assertEqual(done.returncode, 6, done.stderr)
        line = json.loads(done.stderr.splitlines()[-1])
        self.assertEqual(set(line), {'schema_version', 'error_code', 'retained_stage'})
        self.assertEqual(line['error_code'], code)
        if line['retained_stage']:
            self.assertIsNotNone(STAGE.fullmatch(line['retained_stage']))
            shutil.rmtree(line['retained_stage'])
        return line

    def launcher_argv(self):
        return (self.out / 'launcher-argv').read_text().splitlines()

    def test_the_requirement_is_checked_after_the_pins_and_before_the_seed(self):
        done = self.bootstrap('--seed-team', TEAM, '--claude-code', 'plugin', script=self.stubbed)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        argv = (self.out / 'codesign-argv').read_text().splitlines()
        stage = self.launcher_argv()[3][:-len('/release/launcher.py')]
        self.assertIsNotNone(STAGE.fullmatch(stage))
        self.assertEqual(argv, ['--verify', '--strict', '-R', designated_requirement(TEAM), stage + '/seed'])
        self.assertEqual((self.out / 'codesign-order').read_text().strip(), 'before')
        self.assertEqual((self.out / 'codesign-quarantine').read_text(), '')  # None on the source, none added.
        self.assertTrue((self.out / 'seed-ran').exists())
        self.assertEqual(self.launcher_argv()[-2:], ['--claude-code', 'plugin'])  # --seed-team is bootstrap's only.
        self.assertNotIn(TEAM, self.launcher_argv())

    def test_without_seed_team_codesign_is_never_run_and_behaviour_is_unchanged(self):
        done = self.bootstrap(script=self.stubbed)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertFalse((self.out / 'codesign-argv').exists())
        self.assertEqual(self.launcher_argv()[-2:], ['--claude-code', 'user'])

    def test_a_wrong_pin_refuses_before_codesign(self):
        self.seed_sha = 'f' * 64
        self.refused(self.bootstrap('--seed-team', TEAM, script=self.stubbed), 'integrity_failure')
        self.assertFalse((self.out / 'codesign-argv').exists())
        self.assertFalse((self.out / 'seed-ran').exists())

    def test_a_failed_requirement_refuses_with_the_fixed_code_and_the_seed_never_runs(self):
        (self.out / 'codesign-exit').write_text('3')
        self.refused(self.bootstrap('--seed-team', TEAM, script=self.stubbed), 'seed_signature_invalid')
        self.assertTrue((self.out / 'codesign-argv').exists())
        self.assertFalse((self.out / 'seed-ran').exists())

    def test_the_staged_seed_keeps_its_quarantine(self):
        quarantine = '0081;00000000;Synthetic;'
        subprocess.run(['/usr/bin/xattr', '-w', 'com.apple.quarantine', quarantine, str(self.seed)], check=True,
                       timeout=30)
        (self.out / 'codesign-exit').write_text('1')  # Observe the staged copy, then stop before any exec.
        self.refused(self.bootstrap('--seed-team', TEAM, script=self.stubbed), 'seed_signature_invalid')
        # cp carries the attribute over; macOS may restamp its flags and time for a copy, never drop it.
        self.assertRegex((self.out / 'codesign-quarantine').read_text(), r'\A[0-9a-f]{4};[0-9a-f]{8};')

    def test_unsigned_ad_hoc_and_other_team_seeds_are_refused_by_the_stock_codesign(self):
        adhoc = self.base / 'adhoc'
        shutil.copyfile('/usr/bin/true', adhoc)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(adhoc)], check=True, capture_output=True,
                       timeout=60)
        cases = {'unsigned script': None, 'ad hoc Mach-O': adhoc.read_bytes(),
                 'other team (Apple)': Path('/usr/bin/true').read_bytes()}
        for label, data in cases.items():
            with self.subTest(label):
                if data is not None:
                    self.use_seed(data)
                self.refused(self.bootstrap('--seed-team', TEAM), 'seed_signature_invalid')
                self.assertFalse((self.out / 'seed-ran').exists())
                self.assertFalse((self.out / 'launcher-argv').exists())

    def test_the_team_grammar_refuses_before_anything_is_staged(self):
        for extra in (('--seed-team', ''), ('--seed-team', 'abcde12345'), ('--seed-team', 'ABCDE1234'),
                      ('--seed-team', 'ABCDE123456'), ('--seed-team', 'ABCDE-2345'), ('--seed-team',),
                      ('--seed-team', TEAM, '--seed-team', TEAM), ('--seed-team', 'ABCDÉ12345')):
            with self.subTest(extra=extra):
                done = self.bootstrap(*extra, script=self.stubbed)
                self.assertEqual(done.returncode, 6)
                self.assertEqual(json.loads(done.stderr), {'schema_version': 1, 'error_code': 'selection_invalid',
                                                           'retained_stage': ''})
                self.assertFalse((self.out / 'codesign-argv').exists())
                self.assertFalse((self.out / 'seed-ran').exists())

    def test_the_requirement_text_is_native_runtimes(self):
        text = BOOTSTRAP.read_text()
        literal = designated_requirement('QQQQQ00000').replace('"', '\\"').replace('QQQQQ00000', '$bootstrap_team')
        self.assertEqual(text.count(f'-R "{literal}"'), 1)
        self.assertTrue(os.access(BOOTSTRAP, os.X_OK))


if __name__ == '__main__':
    unittest.main()
