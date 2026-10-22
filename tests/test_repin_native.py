"""repin_native (CLIENTS-4): v1 unsigned pins + signed outputs -> the v2 lock, through the native grammar.

Real codesign over copies of stock signed executables standing in for the three dylibs (the lock and the
assembler never load them); a synthetic APPROVED_PINS row approves the test Team ID. No Developer ID,
Keychain or network.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bundle_payload import load_assembler, make_payload, synthetic_pin, v1_lock
from telegram_search_mcp import native_runtime
from telegram_search_mcp.native_runtime import NAMES, NativeManifestV2, lock_signature, parse_native_manifest
from telegram_search_mcp.release_archive import load_archive_index

ROOT = Path(__file__).parents[1]
TEAM, OTHER = 'ABCDE12345', 'ZYXWV98765'
STAND_INS = dict(zip(NAMES, ('/usr/bin/true', '/usr/bin/false', '/bin/echo')))


def load():
    spec = importlib.util.spec_from_file_location('repin_native_under_test', ROOT / 'packaging/repin_native.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RN = load()


def codesign(*args):
    subprocess.run(['/usr/bin/codesign', *args], check=True, capture_output=True, timeout=60)


def team_stand_in(argv, **options):
    """Real codesign without the Team ID requirement: ad hoc copies stand in for a team signature."""
    if '-R' in argv:
        at = argv.index('-R')
        argv = argv[:at] + argv[at + 2:]
    return subprocess.run(argv, **options)


class RepinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.unsigned, self.signed = self.root / 'unsigned', self.root / 'signed'
        self.unsigned.mkdir()
        self.signed.mkdir()
        for name, source in STAND_INS.items():
            shutil.copyfile(source, self.unsigned / name)
            shutil.copyfile(source, self.signed / name)
            (self.signed / name).chmod(0o700)
            codesign('--force', '--sign', '-', str(self.signed / name))  # ad hoc: the test's "any identity"
        self.natives = {name: (self.unsigned / name).read_bytes() for name in NAMES}
        self.v1 = json.dumps(v1_lock(self.natives), indent=2).encode()
        self.row = synthetic_pin(self.natives, team_ids=(TEAM,))
        table = mock.patch.object(native_runtime, 'APPROVED_PINS', (self.row,))
        table.start()
        self.addCleanup(table.stop)

    def repin(self, name='tdlib-lock.json', **options):
        options.setdefault('v1_lock', self.v1)
        options.setdefault('unsigned_directory', self.unsigned)
        options.setdefault('signed_directory', self.signed)
        return RN.repin_native(self.root / name, **options)

    def refused(self, code, **options):
        with self.assertRaises(RN.RepinError) as caught:
            self.repin('refused.json', **options)
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse((self.root / 'refused.json').exists())

    def test_adhoc_signed_copies_repin_and_assemble_under_a_test_pin_row(self):
        signed = {name: (self.signed / name).read_bytes() for name in NAMES}
        self.assertTrue(all(signed[n] != self.natives[n] for n in NAMES))
        summary = self.repin(team_id=TEAM, runner=team_stand_in)
        data = (self.root / 'tdlib-lock.json').read_bytes()
        self.assertEqual((self.root / 'tdlib-lock.json').stat().st_mode & 0o777, 0o400)
        lock = parse_native_manifest(data)
        self.assertIs(type(lock), NativeManifestV2)
        self.assertEqual(lock_signature(lock), ('developer-id', TEAM))
        self.assertEqual(native_runtime.approved_pin(lock), self.row)
        self.assertEqual([(a.name, a.unsigned_bytes, a.unsigned_sha256) for a in lock.artifacts], list(self.row.unsigned))
        self.assertEqual([(a.bytes, a.sha256) for a in lock.artifacts],
                         [(len(signed[n]), hashlib.sha256(signed[n]).hexdigest()) for n in NAMES])
        self.assertEqual(summary['lock_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(summary['publisher'], {'kind': 'developer-id', 'team_id': TEAM})
        # Derived from the v1 lock alone: everything but the signature fields is the v1 document.
        v1, v2 = json.loads(self.v1), json.loads(data)
        self.assertEqual(list(v2), [*list(v1)[:list(v1).index('signature') + 1], 'team_id',
                                    *list(v1)[list(v1).index('signature') + 1:]])
        self.assertEqual({k: v for k, v in v2.items() if k not in ('manifest_version', 'signature', 'team_id', 'artifacts',
                                                                    'notices')},
                         {k: v for k, v in v1.items() if k not in ('manifest_version', 'signature', 'artifacts', 'notices')})
        self.assertEqual(v2['notices'], dict(v1['notices'], publisher_signature='developer-id'))
        assembler = load_assembler('assembly_repinned')
        payload = make_payload(self.root, manifest_version=2, publisher={'kind': 'developer-id', 'team_id': TEAM},
                               natives=signed, lock=data)
        result = assembler.assemble_bundle(payload, self.root / 'output')
        index = load_archive_index(result.index.read_bytes())
        shipped = {f.path: f.sha256 for f in index.files}
        self.assertEqual(shipped['tdlib-lock.json'], hashlib.sha256(data).hexdigest())
        self.assertEqual({n: shipped['native/' + n] for n in NAMES}, {n: hashlib.sha256(signed[n]).hexdigest() for n in NAMES})
        provenance = json.loads(result.provenance.read_bytes())
        self.assertEqual((provenance['publisher_identity'], provenance['team_id']), (True, TEAM))

    def test_developer_id_lock_requires_outputs_signed_by_that_team(self):
        real = subprocess.run
        self.refused('signature_invalid', team_id=TEAM, runner=real)  # ad hoc outputs fail the Team ID requirement

    def test_unchanged_outputs_repin_to_an_adhoc_v2_lock(self):
        summary = self.repin(signed_directory=self.unsigned)
        lock = parse_native_manifest((self.root / 'tdlib-lock.json').read_bytes())
        self.assertEqual(lock_signature(lock), ('adhoc', None))
        self.assertEqual(summary['publisher'], {'kind': 'adhoc', 'team_id': None})
        self.assertTrue(all((a.bytes, a.sha256) == (a.unsigned_bytes, a.unsigned_sha256) for a in lock.artifacts))

    def test_a_mismatching_unsigned_input_refuses(self):
        target = self.unsigned / 'libssl.3.dylib'
        original = target.read_bytes()
        target.write_bytes(original + b'\0')
        self.refused('unsigned_mismatch', team_id=TEAM)
        target.write_bytes(original)
        (self.unsigned / 'extra.dylib').write_bytes(b'x' * 64)
        self.refused('unsigned_mismatch', team_id=TEAM)
        (self.unsigned / 'extra.dylib').unlink()
        target.unlink()
        target.symlink_to(self.signed / 'libssl.3.dylib')
        self.refused('unsigned_mismatch', team_id=TEAM)

    def test_unsigned_or_unapproved_outputs_and_bad_inputs_refuse(self):
        self.refused('pin_not_approved', team_id=OTHER, runner=team_stand_in)
        self.refused('pin_not_approved')  # re-signed bytes under the ad-hoc kind
        self.refused('pin_not_approved', team_id=TEAM, signed_directory=self.unsigned,
                     runner=team_stand_in)  # Developer ID needs new bytes
        self.refused('pin_not_approved', team_id='abcde12345', runner=team_stand_in)
        v2 = json.dumps(json.loads(RN.lock_bytes(RN.v2_document(json.loads(self.v1), self.natives, None)))).encode()
        for lock in (v2, b'{}', b'not json', b'{"manifest_version": 1, "manifest_version": 1}'):
            with self.subTest(lock=lock[:24]):
                self.refused('lock_invalid', v1_lock=lock, team_id=TEAM)
        stripped = self.signed / 'libcrypto.3.dylib'
        codesign('--remove-signature', str(stripped))
        self.refused('signature_invalid', team_id=TEAM)
        (self.signed / 'extra').write_bytes(b'x')
        self.refused('signed_refused', team_id=TEAM)
        (self.root / 'taken.json').write_bytes(b'')
        with self.assertRaises(RN.RepinError) as caught:
            self.repin('taken.json', team_id=TEAM)
        self.assertEqual(str(caught.exception), 'output_refused')

    def test_tool_failure_is_a_fixed_code(self):
        def failing(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, b'private tool output', b'private')
        with self.assertRaises(RN.RepinError) as caught:
            self.repin(team_id=TEAM, runner=failing)
        self.assertEqual(str(caught.exception), 'signature_invalid')
        self.assertNotIn('private', repr(caught.exception.args))


class HeadLockTests(unittest.TestCase):
    def test_reads_heads_committed_lock_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp).resolve()
            env = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
            git = ['/usr/bin/git', '-C', str(checkout), '-c', 'user.name=synthetic', '-c', 'user.email=synthetic@example.invalid',
                   '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main']
            (checkout / 'packaging').mkdir()
            (checkout / 'packaging/tdlib-lock.json').write_bytes(b'committed\n')
            for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'synthetic']):
                subprocess.run(git + args, check=True, capture_output=True, env=env, timeout=60)
            (checkout / 'packaging/tdlib-lock.json').write_bytes(b'uncommitted edit\n')
            self.assertEqual(RN.head_lock(checkout), b'committed\n')
            subprocess.run(git + ['rm', '-qf', 'packaging/tdlib-lock.json'], check=True, capture_output=True, env=env, timeout=60)
            subprocess.run(git + ['commit', '-q', '-m', 'no lock'], check=True, capture_output=True, env=env, timeout=60)
            with self.assertRaises(RN.RepinError) as caught:
                RN.head_lock(checkout)
            self.assertEqual(str(caught.exception), 'lock_invalid')
        # The default checkout is this one. In a git checkout its default read is HEAD's committed lock; the release
        # gate runs the suite in an archive tree with no .git, where only the default path itself is checked.
        self.assertEqual(RN.ROOT, ROOT)
        if (ROOT / '.git').exists():
            self.assertEqual(RN.head_lock(), subprocess.run(
                ['/usr/bin/git', '-C', str(ROOT), 'show', 'HEAD:packaging/tdlib-lock.json'],
                check=True, capture_output=True, timeout=60).stdout)


if __name__ == '__main__':
    unittest.main()
