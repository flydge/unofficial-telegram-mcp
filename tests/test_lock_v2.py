"""Native lock grammar: v1 stays admitted; v2 is data-driven by the reviewed APPROVED_PINS table."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bundle_payload import inert_natives, synthetic_pin, v1_lock, v2_lock
from telegram_search_mcp import config, native_runtime
from telegram_search_mcp.native_runtime import (
    APPROVED_PINS, NativeManifest, NativeManifestV2, NativeRuntimeError, parse_native_manifest,
)

ROOT = Path(__file__).parents[1]
COMMITTED = ROOT / 'packaging/tdlib-lock.json'
TEAM, OTHER = 'ABCDE12345', 'ZYXWV98765'


def encode(value):
    return json.dumps(value).encode()


class LockGrammarTests(unittest.TestCase):
    def setUp(self):
        self.natives = inert_natives()
        self.signed = {name: data + b'-developer-id-signature' for name, data in self.natives.items()}
        self.lock = v1_lock(self.natives)

    def table(self, *rows):
        return mock.patch.object(native_runtime, 'APPROVED_PINS', rows or (synthetic_pin(self.natives, team_ids=(TEAM,)),))

    def test_committed_v1_lock_is_the_reviewed_row_and_still_parses(self):
        lock = parse_native_manifest(COMMITTED.read_bytes())
        self.assertIs(type(lock), NativeManifest)
        row = APPROVED_PINS[0]
        self.assertEqual(native_runtime.approved_pin(lock), row)
        self.assertEqual(tuple((a.name, a.bytes, a.sha256) for a in lock.artifacts), row.unsigned)
        self.assertEqual(row.team_ids, frozenset())  # No Developer ID team is approved before the owner signs.

    def test_derived_constants_follow_the_reviewed_row(self):
        from telegram_search_mcp import bundle_runtime
        row = APPROVED_PINS[0]
        self.assertEqual(len(APPROVED_PINS), 1)
        self.assertEqual((native_runtime.COMMIT, native_runtime.SCHEMA_SHA256), (row.source_commit, row.schema_sha256))
        self.assertEqual(config.EXPECTED_TDLIB_KEG, 'HEAD-' + row.source_commit[:7])
        self.assertEqual((config.EXPECTED_TDLIB_VERSION, config.EXPECTED_TDLIB_COMMIT), (row.tdlib_version, row.source_commit))
        self.assertIn(row.source_commit[:7], bundle_runtime.runtime_pin())
        for field, value in (('source_commit', row.source_commit), ('tdlib_version', row.tdlib_version),
                             ('schema_sha256', row.schema_sha256), ('minimum_os', row.minimum_os), ('abi', row.abi)):
            self.assertEqual(NativeManifest.model_fields[field].annotation.__args__, (value,))

    def test_the_committed_pin_in_v2_form_parses_against_the_real_table(self):
        sample = json.dumps(v2_lock(json.loads(COMMITTED.read_bytes())), indent=2).encode()
        lock = parse_native_manifest(sample)
        self.assertIs(type(lock), NativeManifestV2)
        self.assertEqual(native_runtime.lock_signature(lock), ('adhoc', None))
        self.assertEqual(native_runtime.approved_pin(lock), APPROVED_PINS[0])

    def test_v2_adhoc_and_developer_id_rows_parse(self):
        with self.table():
            self.assertEqual(native_runtime.lock_signature(parse_native_manifest(encode(v2_lock(self.lock)))), ('adhoc', None))
            signed = parse_native_manifest(encode(v2_lock(self.lock, signature='developer-id', team_id=TEAM, signed=self.signed)))
            self.assertEqual(native_runtime.lock_signature(signed), ('developer-id', TEAM))
            self.assertEqual([a.unsigned_sha256 for a in signed.artifacts],
                             [hashlib.sha256(self.natives[a.name]).hexdigest() for a in signed.artifacts])

    def test_v2_refuses_wrong_team_adhoc_files_and_unsigned_drift(self):
        drifted = dict(self.natives, **{'libssl.3.dylib': b'x' * 48})
        cases = {
            'unapproved team': v2_lock(self.lock, signature='developer-id', team_id=OTHER, signed=self.signed),
            'no team': v2_lock(self.lock, signature='developer-id', team_id=None, signed=self.signed),
            'malformed team': v2_lock(self.lock, signature='developer-id', team_id='abcde12345', signed=self.signed),
            'ad-hoc bytes under Developer ID': v2_lock(self.lock, signature='developer-id', team_id=TEAM),
            'ad-hoc row with a team': v2_lock(self.lock, team_id=TEAM),
            'ad-hoc row with re-signed bytes': v2_lock(self.lock, signed=self.signed),
            'unsigned inputs differ from v1 pins': v2_lock(v1_lock(drifted)),
            'publisher signature mismatch': v2_lock(self.lock, notices__publisher_signature='developer-id'),
            'unknown signature kind': v2_lock(self.lock, signature='adhoc-local-integrity-only'),
        }
        with self.table():
            for label, value in cases.items():
                with self.subTest(label), self.assertRaises(NativeRuntimeError):
                    parse_native_manifest(encode(value))
        with self.subTest('real table approves no team yet'), self.assertRaises(NativeRuntimeError):
            parse_native_manifest(encode(v2_lock(json.loads(COMMITTED.read_bytes()), signature='developer-id', team_id=TEAM,
                                                 signed={'libtdjson.dylib': b's' * 64, 'libssl.3.dylib': b's' * 65,
                                                         'libcrypto.3.dylib': b's' * 66})))

    def test_a_second_table_row_is_accepted_only_for_its_exact_values(self):
        first = synthetic_pin(self.natives)
        second_natives = inert_natives(b'-second')
        second = synthetic_pin(second_natives, source_commit='1' * 40, tdlib_version='1.8.68', schema_sha256='2' * 64,
                               tdlib_license_source='https://github.com/tdlib/td/blob/' + '1' * 40 + '/LICENSE_1_0.txt',
                               openssl_version='3.6.5')
        second_lock = v2_lock(v1_lock(second_natives), source_commit=second.source_commit, tdlib_version='1.8.68',
                              schema_sha256=second.schema_sha256, build__openssl_version='3.6.5',
                              notices__tdlib_license_source=second.tdlib_license_source)
        with self.table(first, second):
            self.assertEqual(native_runtime.approved_pin(parse_native_manifest(encode(second_lock))), second)
            self.assertEqual(native_runtime.approved_pin(parse_native_manifest(encode(v2_lock(self.lock)))), first)
            unlisted = {'source_commit': '3' * 40, 'tdlib_version': '1.8.69', 'schema_sha256': '4' * 64,
                        'minimum_os': '26.1', 'build__openssl_version': '3.6.6', 'build__method': 'source-build',
                        'notices__tdlib_license_source': 'https://example.invalid/LICENSE'}
            for key, value in unlisted.items():
                with self.subTest(unlisted=key), self.assertRaises(NativeRuntimeError):
                    parse_native_manifest(encode(v2_lock(self.lock, **{key: value})))
            # Mixing fields of two approved rows matches neither.
            with self.subTest('mixed rows'), self.assertRaises(NativeRuntimeError):
                parse_native_manifest(encode(v2_lock(self.lock, tdlib_version='1.8.68')))
            with self.subTest('second row with first unsigned pins'), self.assertRaises(NativeRuntimeError):
                parse_native_manifest(encode(dict(second_lock, artifacts=v2_lock(self.lock)['artifacts'])))

    def test_only_admitted_versions_and_exact_shapes_parse(self):
        for version in (0, 3, '2', True, None, 2.0):
            with self.subTest(version=version), self.table(), self.assertRaises(NativeRuntimeError):
                parse_native_manifest(encode(dict(v2_lock(self.lock), manifest_version=version)))
        with self.table():
            for label, value in {'extra key': dict(v2_lock(self.lock), extra=1),
                                 'missing team key': {k: v for k, v in v2_lock(self.lock).items() if k != 'team_id'},
                                 'v1 with v2 fields': dict(self.lock, team_id=None)}.items():
                with self.subTest(label), self.assertRaises(NativeRuntimeError):
                    parse_native_manifest(encode(value))
            with self.assertRaises(NativeRuntimeError):
                parse_native_manifest(b'[]')

    def test_validate_native_accepts_only_parsed_lock_models(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(NativeRuntimeError):
            native_runtime.validate_native(json.loads(COMMITTED.read_bytes()), 'macos-arm64', directory=Path(tmp))


class DesignatedRequirementTests(unittest.TestCase):
    def test_developer_id_runs_the_team_requirement_and_adhoc_does_not(self):
        natives = inert_natives()
        signed = {name: data + b'-signed' for name, data in natives.items()}
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (synthetic_pin(natives, team_ids=(TEAM,)),)):
            developer = parse_native_manifest(encode(v2_lock(v1_lock(natives), signature='developer-id', team_id=TEAM, signed=signed)))
            adhoc = parse_native_manifest(encode(v2_lock(v1_lock(natives))))
        calls = []
        def run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, b'', b'')
        with mock.patch.object(native_runtime.subprocess, 'run', run):
            native_runtime._verify_signature(Path('/synthetic/libtdjson.dylib'), developer)
            native_runtime._verify_signature(Path('/synthetic/libtdjson.dylib'), adhoc)
            native_runtime._verify_signature(Path('/synthetic/libtdjson.dylib'), parse_native_manifest(COMMITTED.read_bytes()))
        requirement = ('=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
                       ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
                       f' and certificate leaf[subject.OU] = "{TEAM}"')
        self.assertEqual(calls[0], ['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, '/synthetic/libtdjson.dylib'])
        self.assertEqual(calls[1], ['/usr/bin/codesign', '--verify', '--strict', '/synthetic/libtdjson.dylib'])
        self.assertEqual(calls[2], calls[1])
        failing = lambda args, **kwargs: subprocess.CompletedProcess(args, 3, b'private', b'private')
        with mock.patch.object(native_runtime.subprocess, 'run', failing), self.assertRaises(NativeRuntimeError) as caught:
            native_runtime._verify_signature(Path('/synthetic/libtdjson.dylib'), developer)
        self.assertNotIn('private', str(caught.exception))
        for bad in ('abcde12345', 'ABCDE1234', 'ABCDE12345"', None):
            with self.subTest(team=bad), self.assertRaises(NativeRuntimeError):
                native_runtime.designated_requirement(bad)

    def test_a_real_adhoc_signature_fails_the_developer_id_requirement(self):
        """Real codesign: an ad-hoc file passes the ad-hoc check and refuses the Team ID requirement."""
        natives = inert_natives()
        signed = {name: data + b'-signed' for name, data in natives.items()}
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (synthetic_pin(natives, team_ids=(TEAM,)),)):
            developer = parse_native_manifest(encode(v2_lock(v1_lock(natives), signature='developer-id', team_id=TEAM, signed=signed)))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'adhoc-tool'
            shutil.copyfile('/usr/bin/true', target)
            target.chmod(0o700)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(target)], check=True,
                           capture_output=True, timeout=60)
            native_runtime._verify_signature(target, parse_native_manifest(COMMITTED.read_bytes()))
            with self.assertRaises(NativeRuntimeError):
                native_runtime._verify_signature(target, developer)


if __name__ == '__main__':
    unittest.main()
