"""The release gate's optional bundle stage: inputs, the disk-derived record, the stage and verify.

The build itself is a fake runner writing a synthetic release_build output; the synthetic git
repository and the evidence handling are real. Without the bundle flags the gate is unchanged
(tests/test_release_gate.py).
"""
import argparse
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_release_gate as base

gate = base.gate
COMMIT = 'a' * 40


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_bundle(out, commit, *, mode='adhoc-dry-run', archive=b'archive', dmg=b'dmg'):
    """A synthetic release_build output whose release-build.json lists what is on disk."""
    files = {'kit': {'release.tar.gz': archive, 'release-index.json': b'index', 'bootstrap-verifier': b'seed',
                     'release-pins.json': b'pins', 'SHA256SUMS': b'kit sums'},
             'assets': {'release.tar.gz': archive, 'product-1.2.3-macos-arm64.dmg': dmg,
                        'corresponding-source-1.2.3.tar.gz': b'source', 'SHA256SUMS': b'asset sums'}}
    listing = {}
    for group, members in files.items():
        (out / group).mkdir(parents=True)
        listing[group] = {}
        for name, data in members.items():
            (out / group / name).write_bytes(data)
            listing[group][name] = {'sha256': sha(data), 'bytes': len(data)}
    (out / 'release-build.json').write_text(json.dumps({'schema': 'tgmcp-release-build/v1', 'mode': mode,
                                                        'source_commit': commit, 'release_version': '1.2.3',
                                                        'tag': None, **listing}))
    return gate.bundle_record(out, commit) if mode == 'adhoc-dry-run' and commit == COMMIT else None


class BundleRecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_inputs_are_both_or_neither(self):
        self.assertIsNone(gate.bundle_inputs(None, None))
        self.assertEqual(gate.bundle_inputs(self.root, self.root), (self.root, self.root))
        for pair in ((self.root, None), (None, self.root), (Path('relative'), self.root),
                     (self.root / 'missing', self.root)):
            with self.subTest(pair=pair), self.assertRaisesRegex(gate.GateRefusal, '^bundle_inputs_invalid$'):
                gate.bundle_inputs(*pair)

    def test_record_is_rederived_from_disk_and_must_agree(self):
        record = write_bundle(self.root / 'out', COMMIT)
        self.assertEqual(set(record), gate.BUNDLE_KEYS)
        self.assertEqual((record['archive'], record['seed'], record['dmg']), (sha(b'archive'), sha(b'seed'), sha(b'dmg')))
        self.assertEqual(record['build_evidence'], sha((self.root / 'out/release-build.json').read_bytes()))
        (self.root / 'out/kit/bootstrap-verifier').write_bytes(b'other seed')
        with self.assertRaisesRegex(gate.GateRefusal, '^bundle_mismatch$'):
            gate.bundle_record(self.root / 'out', COMMIT)
        for name, commit, mode in (('b', 'b' * 40, 'adhoc-dry-run'), ('c', COMMIT, 'developer-id')):
            write_bundle(self.root / name, commit, mode=mode)
            with self.subTest(name=name), self.assertRaisesRegex(gate.GateRefusal, '^bundle_mismatch$'):
                gate.bundle_record(self.root / name, COMMIT)


class BundleStageTests(base.GateCase):
    def setUp(self):
        super().setUp()
        self.repo, _, self.head = base.synthetic_repo(self.root, {'README.md': 'one\n'}, {'README.md': 'two\n'})
        self.tree = self.root / 'tree'
        (self.tree / 'packaging').mkdir(parents=True)
        (self.tree / gate.BUNDLE_SCRIPT).write_text('# the commit\'s own release_build.py\n')
        self.inputs = (self.root / 'native', self.root / 'cache')
        for path in self.inputs:
            path.mkdir()
        self.calls = []

    def runner(self, command, code, *, timeout, env, cwd=None, **options):
        self.calls.append(([str(part) for part in command], code, env, cwd))
        if command[3] == 'build':
            write_bundle(Path(command[command.index('--output') + 1]), self.head)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    def test_stage_runs_the_commit_script_dry_run_then_its_check(self):
        out = self.root / 'bundle'
        record = gate.bundle_stage(gate.Git(self.repo), self.head, self.tree, out, self.inputs,
                                   Path('/synthetic/python'), {'PATH': gate.SYSTEM_PATH}, runner=self.runner)
        self.assertEqual(record['source_commit'], self.head)
        script = str(self.tree / gate.BUNDLE_SCRIPT)
        self.assertEqual([call[0] for call in self.calls], [
            ['/synthetic/python', '-B', script, 'build', '--dry-run', '--offline', '--output', str(out),
             '--native-directory', str(self.inputs[0]), '--cache', str(self.inputs[1]), '--checkout', str(self.repo)],
            ['/synthetic/python', '-B', script, 'check', '--output', str(out), '--checkout', str(self.repo)]])
        self.assertTrue(all(call[2]['PYTHONPATH'] == str(self.tree / 'src') and call[1] == 'bundle_failed'
                            for call in self.calls))

    def test_head_must_be_the_gated_commit(self):
        previous = base.git(self.repo, 'rev-parse', 'HEAD~1')
        with self.refused('bundle_commit_mismatch'):
            gate.bundle_stage(gate.Git(self.repo), previous, self.tree, self.root / 'bundle', self.inputs,
                              Path('/synthetic/python'), {}, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_run_refuses_one_bundle_flag_before_any_work(self):
        workdir = self.root / 'gate-work'
        self.admit()
        code, out, err = self.main('run', '--commit', self.head, '--previous-tag', 'v0.0.1', '--workdir', str(workdir),
                                   '--repo', str(self.repo), '--bundle-cache', str(self.inputs[1]))
        self.assertEqual((code, out, err), (1, '', 'release gate refused: bundle_inputs_invalid\n'))
        self.assertFalse(workdir.exists())


class BundleVerifyTests(base.GateCase):
    """verify with a recorded bundle: the disk record, then a rebuild that must reproduce it."""

    def setUp(self):
        super().setUp()
        self.admit()
        self.repo = base.synthetic_repo(self.root, {'README.md': 'one\n'}, {'README.md': 'two\n'})[0]
        self.base = self.root / 'evidence'
        (self.base / 'dist').mkdir(parents=True)
        self.sdist = self.base / 'dist' / f'{gate.DIST}-{base.VERSION}.tar.gz'
        self.wheel = self.base / 'dist' / f'{gate.DIST}-{base.VERSION}-py3-none-any.whl'
        self.sdist.write_bytes(b'sdist')
        self.wheel.write_bytes(b'wheel')
        (self.base / 'junit.xml').write_text('<?xml version="1.0" encoding="utf-8"?><testsuites>'
                                             '<testsuite name="pytest" tests="1"><testcase classname="t.T" name="a"/>'
                                             '</testsuite></testsuites>')
        (self.base / 'suite.log').write_text('1 passed in 0.01s\n')
        self.derived = {
            'schema': gate.SCHEMA, 'commit': COMMIT, 'tree': 'b' * 40, 'commit_time': base.EPOCH,
            'previous_tag': 'v0.0.1', 'previous_commit': 'c' * 40, 'version': base.VERSION,
            'gate': {'sha256': gate.own_sha256(), 'at_commit_sha256': None},
            'toolchain': {'python': '3.14.0'}, 'archive': {'sha256': 'd' * 64, 'files': 3},
            'sdist': {'file': f'dist/{self.sdist.name}', 'sha256': sha(b'sdist')},
            'wheel': {'file': f'dist/{self.wheel.name}', 'sha256': sha(b'wheel')},
            'constraints': {}, 'install': {}, 'smoke': {}, 'privacy': {}, 'history_findings': {}, 'identities': {}}
        self.bundle = write_bundle(self.base / 'bundle', COMMIT)
        self.evidence = {**copy.deepcopy(self.derived), 'bundle': self.bundle,
                         'tests': gate.suite_report(self.base / 'junit.xml', self.base / 'suite.log')}
        self.path = self.base / 'evidence.json'
        self.inputs = (self.root / 'native', self.root / 'cache')
        for path in self.inputs:
            path.mkdir()
        self.rebuilt = dict(self.bundle)

    def fake_derive(self, git, commit, tag, base_dir, scratch, *, online):
        return {**copy.deepcopy(self.derived), '_artifacts': (self.sdist, self.wheel, None, None)}

    def fake_rerun(self, artifacts, evidence, base_dir, scratch):
        return {**{key: evidence['tests'][key] for key in gate.SUITE_COUNTS}, 'returncode': 0}

    def fake_rebuild(self, git, commit, tree, out, inputs, python, env):
        self.assertEqual((commit, inputs), (COMMIT, self.inputs))
        self.assertTrue(out.is_relative_to(self.base) and not out.exists())
        return dict(self.rebuilt)

    def verify(self, evidence, inputs=True):
        self.path.unlink(missing_ok=True)
        gate.write_evidence(self.path, evidence)
        native, cache = self.inputs if inputs else (None, None)
        args = argparse.Namespace(evidence=self.path, repo=self.repo, delegated=False, bundle_native_directory=native,
                                  bundle_cache=cache)
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.verify(args, derive_evidence=self.fake_derive, rerun=self.fake_rerun, rebuild=self.fake_rebuild)

    def test_a_reproduced_bundle_verifies_and_only_the_dmg_may_differ(self):
        self.assertEqual(self.verify(self.evidence), 0)
        self.rebuilt.update(dmg='9' * 64, assets_sums='8' * 64, build_evidence='7' * 64)
        self.assertEqual(self.verify(self.evidence), 0)

    def test_refusals(self):
        with self.refused('bundle_inputs_invalid'):
            self.verify(self.evidence, inputs=False)
        plain = {k: v for k, v in self.evidence.items() if k != 'bundle'}
        with self.refused('bundle_inputs_invalid'):
            self.verify(plain)
        self.rebuilt['archive'] = '0' * 64
        with self.refused('evidence_mismatch') as caught:
            self.verify(self.evidence)
        self.assertEqual(caught.exception.detail, 'bundle')
        self.rebuilt = dict(self.bundle)
        edited = copy.deepcopy(self.evidence)
        edited['bundle']['seed'] = '0' * 64
        with self.refused('evidence_mismatch'):
            self.verify(edited)
        (self.base / 'bundle/kit/release-pins.json').write_bytes(b'edited pins')
        with self.refused('bundle_mismatch'):
            self.verify(self.evidence)
        extra = copy.deepcopy(self.evidence)
        extra['bundle']['note'] = 'x'
        with self.refused('evidence_invalid'):
            self.verify(extra)


if __name__ == '__main__':
    unittest.main()
