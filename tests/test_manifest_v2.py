"""Release manifest v2 (publication, source completeness, publisher); v1 stays admitted."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bundle_payload import inert_natives, keychain_runner, load_assembler, make_payload, synthetic_pin, v1_lock, v2_lock
from test_bundle_install import InstallHarness
from telegram_search_mcp import __version__, native_runtime
from telegram_search_mcp.bundle_runtime import BundleError, check_manifest, check_publisher_lock
from telegram_search_mcp.release_archive import extract_verified_archive, load_archive_index

TEAM = 'ABCDE12345'
SCHEMA = 'source/tdlib/td/generate/scheme/td_api.tl'


def manifest(version=1, **fields):
    value = dict(schema_version=version, package_version=__version__, target='macos-arm64', minimum_os='26.0',
                 contract_version=1, schema_fingerprint='a' * 64, tools=['_manifest'], native_lock_sha256='b' * 64,
                 skills=[], source_completeness='incomplete', publication='unreleased')
    if version == 2:
        value['publisher'] = {'kind': 'adhoc', 'team_id': None}
    value.update(fields)
    return value


class ManifestGrammarTests(unittest.TestCase):
    def test_v1_and_every_v2_combination_of_the_closed_grammar(self):
        check_manifest(manifest(), __version__)
        developer = {'kind': 'developer-id', 'team_id': TEAM}
        for publication, completeness, publisher in [
                ('unreleased', 'incomplete', {'kind': 'adhoc', 'team_id': None}),
                ('unreleased', 'complete', {'kind': 'adhoc', 'team_id': None}),
                ('unreleased', 'incomplete', developer), ('unreleased', 'complete', developer),
                ('published', 'complete', developer)]:
            with self.subTest(publication=publication, completeness=completeness, publisher=publisher['kind']):
                check_manifest(manifest(2, publication=publication, source_completeness=completeness,
                                        publisher=publisher), __version__)

    def test_every_other_value_refuses(self):
        developer = {'kind': 'developer-id', 'team_id': TEAM}
        cases = {
            'v1 published': manifest(publication='published'),
            'v1 complete': manifest(source_completeness='complete'),
            'v1 with publisher': manifest(publisher={'kind': 'adhoc', 'team_id': None}),
            'v2 without publisher': {k: v for k, v in manifest(2).items() if k != 'publisher'},
            'published incomplete': manifest(2, publication='published', publisher=developer),
            'published ad hoc': manifest(2, publication='published', source_completeness='complete'),
            'ad hoc with team': manifest(2, publisher={'kind': 'adhoc', 'team_id': TEAM}),
            'developer-id without team': manifest(2, publisher={'kind': 'developer-id', 'team_id': None}),
            'lowercase team': manifest(2, publisher={'kind': 'developer-id', 'team_id': TEAM.lower()}),
            'short team': manifest(2, publisher={'kind': 'developer-id', 'team_id': TEAM[:9]}),
            'unknown publisher kind': manifest(2, publisher={'kind': 'apple', 'team_id': None}),
            'extra publisher key': manifest(2, publisher={'kind': 'adhoc', 'team_id': None, 'name': 'x'}),
            'unknown publication': manifest(2, publication='public'),
            'unknown completeness': manifest(2, source_completeness='partial'),
            'extra key': manifest(2, extra=True),
            'version 3': manifest(3),
            'string version': manifest('2'),
            'boolean version': manifest(True),
            'other package version': manifest(2, package_version='0.0.0'),
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(BundleError):
                check_manifest(value, __version__)

    def test_publisher_must_name_the_native_lock_signature(self):
        natives = inert_natives()
        v1 = json.dumps(v1_lock(natives)).encode()
        adhoc, developer = manifest(2), manifest(2, publisher={'kind': 'developer-id', 'team_id': TEAM})
        check_publisher_lock(adhoc, v1)
        check_publisher_lock(manifest(), v1)
        with self.assertRaises(BundleError) as caught:
            check_publisher_lock(developer, v1)
        self.assertEqual(str(caught.exception), 'publisher_mismatch')
        with self.assertRaises(BundleError) as caught:
            check_publisher_lock(adhoc, b'{}')
        self.assertEqual(str(caught.exception), 'native_lock_mismatch')
        signed = {n: d + b'-signed' for n, d in natives.items()}
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (synthetic_pin(natives, team_ids=(TEAM, 'ZYXWV98765')),)):
            check_publisher_lock(manifest(), json.dumps(v2_lock(v1_lock(natives))).encode())
            lock = json.dumps(v2_lock(v1_lock(natives), signature='developer-id', team_id=TEAM, signed=signed)).encode()
            check_publisher_lock(developer, lock)
            for value in (manifest(2, publisher={'kind': 'developer-id', 'team_id': 'ZYXWV98765'}), adhoc,
                          manifest()):  # A v1 manifest has no publisher: it pairs only with an ad-hoc lock.
                with self.subTest(manifest=value), self.assertRaises(BundleError) as caught:
                    check_publisher_lock(value, lock)
                self.assertEqual(str(caught.exception), 'publisher_mismatch')


class AssemblerManifestTests(unittest.TestCase):
    def assemble(self, root, **options):
        module = load_assembler('assembly_manifest_v2')
        return module, module.assemble_bundle(make_payload(root, **options), root / 'output')

    def refused(self, code='bundle_producer_integrity_refused', **options):
        module = load_assembler('assembly_manifest_v2')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(module.ReleasePackageError) as caught:
                module.assemble_bundle(make_payload(root, **options), root / 'output')
            self.assertEqual(str(caught.exception), code)
            self.assertFalse((root / 'output').exists())

    def test_v1_kit_and_v2_unreleased_assemble_with_matching_provenance(self):
        for options, expected in [({}, ('unreleased', False, None, 'incomplete')),
                                  ({'manifest_version': 2}, ('unreleased', False, None, 'incomplete'))]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                _, result = self.assemble(root, **options)
                provenance = json.loads(result.provenance.read_bytes())
                self.assertEqual((provenance['publication'], provenance['publisher_identity'], provenance['team_id'],
                                  provenance['source_completeness']), expected)

    def test_published_or_inconsistent_publisher_refuses(self):
        self.refused(manifest_version=2, publication='published')
        self.refused(manifest_version=2, publication='published', source_completeness='complete',
                     publisher={'kind': 'developer-id', 'team_id': TEAM})  # v1 ad-hoc lock: publisher mismatch
        self.refused(manifest_version=2, publisher={'kind': 'developer-id', 'team_id': TEAM})
        self.refused(manifest_version=1, publication='published')

    def test_a_v1_manifest_never_pairs_with_a_developer_id_lock(self):
        natives = inert_natives()
        signed = {n: d + b'-developer-id' for n, d in natives.items()}
        lock = v2_lock(v1_lock(natives), signature='developer-id', team_id=TEAM, signed=signed)
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (synthetic_pin(natives, team_ids=(TEAM,)),)):
            self.refused(manifest_version=1, natives=signed, lock=lock)
            with tempfile.TemporaryDirectory() as tmp:  # The same lock under a v2 Developer ID manifest assembles.
                self.assemble(Path(tmp).resolve(), manifest_version=2, natives=signed, lock=lock,
                              publisher={'kind': 'developer-id', 'team_id': TEAM})
            with tempfile.TemporaryDirectory() as tmp:  # A v1 manifest with a v2 ad-hoc lock assembles.
                self.assemble(Path(tmp).resolve(), manifest_version=1, lock=v2_lock(v1_lock(natives)))

    def test_shipped_tdlib_schema_must_match_the_lock(self):
        self.refused('native_schema_mismatch', extra_files={SCHEMA: b'not the pinned schema'})
        self.refused('native_schema_mismatch', extra_files={'source/tdlib/README.md': b'source without its schema'})
        self.refused('native_schema_mismatch', manifest_version=2, source_completeness='complete')

    def test_a_published_developer_id_bundle_assembles_under_an_approved_row(self):
        natives = inert_natives()
        signed = {n: d + b'-developer-id' for n, d in natives.items()}
        schema = b'synthetic td_api.tl\n'
        row = synthetic_pin(natives, team_ids=(TEAM,), schema_sha256=hashlib.sha256(schema).hexdigest())
        lock = v2_lock(v1_lock(natives), signature='developer-id', team_id=TEAM, signed=signed,
                       schema_sha256=row.schema_sha256)
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (row,)), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _, result = self.assemble(root, manifest_version=2, publication='published', source_completeness='complete',
                                      publisher={'kind': 'developer-id', 'team_id': TEAM}, natives=signed, lock=lock,
                                      extra_files={SCHEMA: schema})
            provenance = json.loads(result.provenance.read_bytes())
            self.assertEqual((provenance['publication'], provenance['publisher_identity'], provenance['team_id'],
                              provenance['source_completeness']), ('published', True, TEAM, 'complete'))
            index = load_archive_index(result.index.read_bytes())
            self.assertIn(SCHEMA, {f.path for f in index.files})


class InstalledManifestV2Tests(InstallHarness, unittest.TestCase):
    def test_v2_bundle_installs_and_every_launch_check_admits_it(self):
        from telegram_search_mcp import bundle_runtime
        payload = make_payload(self.base, manifest_version=2)
        result = load_assembler('assembly_v2_install').assemble_bundle(payload, self.base / 'output',
                                                                         keychain_runner=keychain_runner(1, 'f' * 32))
        extract_verified_archive(result.archive, load_archive_index(result.index.read_bytes()), self.base / 'admitted')
        import importlib
        packet = (importlib.import_module('telegram_search_mcp.bundle_install'), self.base / 'admitted',
                  result.index, result.index_sha256)
        installed = self.install(packet)
        selection = bundle_runtime.Selection(Path(installed['release_root']), packet[3])
        index, manifest_value = selection.check()
        self.assertEqual((manifest_value['schema_version'], manifest_value['publisher']), (2, {'kind': 'adhoc', 'team_id': None}))


if __name__ == '__main__':
    unittest.main()
