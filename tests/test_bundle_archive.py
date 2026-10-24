import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from telegram_search_mcp.release_archive import load_archive_index, extract_verified_archive, ReleaseArchiveError


class BundleArchiveTests(unittest.TestCase):
    def fixture(self, root, records=None, trailing=b''):
        members = records or [('safe/value.txt', b'checked', tarfile.REGTYPE)]
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w',format=tarfile.USTAR_FORMAT) as tar:
            for name,data,kind in members:
                item=tarfile.TarInfo(name); item.size=len(data); item.type=kind; item.linkname='outside'
                tar.addfile(item,io.BytesIO(data))
        archive=root/'archive.tar.gz'; archive.write_bytes(gzip.compress(buffer.getvalue()+trailing,mtime=0)); archive.chmod(0o400)
        body=dict(schema_version=1,target='macos-arm64',release_version='0.57.0.dev0',archive_bytes=archive.stat().st_size,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),unpacked_bytes=7,
            files=[dict(path='safe/value.txt',bytes=7,sha256=hashlib.sha256(b'checked').hexdigest(),executable=False)])
        return archive,load_archive_index(json.dumps(body).encode())

    def test_current_dev_archive_extracts_only_authenticated_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); archive,index=self.fixture(root)
            extract_verified_archive(archive,index,root/'new')
            self.assertEqual((root/'new/safe/value.txt').read_bytes(),b'checked')
            self.assertEqual((root/'new/safe/value.txt').stat().st_mode&0o777,0o400)

    def test_release_version_grammar_admits_releases_pre_releases_and_development_versions_only(self):
        def index(version):
            return json.dumps(dict(schema_version=1, target='macos-arm64', release_version=version, archive_bytes=1,
                                   archive_sha256='0' * 64, unpacked_bytes=1,
                                   files=[dict(path='a', bytes=1, sha256='0' * 64, executable=False)])).encode()
        for good in ('0.53.0', '0.53.0rc1', '0.53.0a2', '0.53.0b3', '0.53.0.dev0', '0.53.0rc1.dev2', '10.0.12'):
            with self.subTest(version=good):
                self.assertEqual(load_archive_index(index(good)).release_version, good)
        for bad in ('0.53', '0.53.0rc', '0.53.0RC1', '0.53.0-rc1', '0.53.0c1', '0.53.0rc01', '00.53.0', '0.53.0.post1',
                    ' 0.53.0', '0.53.0\n'):
            with self.subTest(version=bad), self.assertRaises(ReleaseArchiveError):
                load_archive_index(index(bad))

    def test_duplicate_traversal_link_illegal_type_tail_and_corruption_are_retained(self):
        cases=[ [('safe/value.txt',b'checked',tarfile.REGTYPE)]*2,
            [('../outside',b'checked',tarfile.REGTYPE)],
            [('safe/value.txt',b'checked',tarfile.SYMTYPE)],
            [('safe/value.txt',b'checked',tarfile.FIFOTYPE)],
            [('safe/value.txt',b'changed',tarfile.REGTYPE)]]
        for records in cases:
            with self.subTest(records=records),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve(); archive,index=self.fixture(root,records)
                with self.assertRaises(ReleaseArchiveError): extract_verified_archive(archive,index,root/'new')
                self.assertTrue(archive.exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); archive,index=self.fixture(root,trailing=b'nonzero')
            with self.assertRaises(ReleaseArchiveError): extract_verified_archive(archive,index,root/'new')

    def test_assembler_authenticates_product_and_current_contract(self):
        path=Path(__file__).parents[1]/'packaging/assemble_bundle.py'
        self.assertTrue(path.is_file(),'missing current bundle producer')
        spec=importlib.util.spec_from_file_location('assembly',path)
        module=importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name]=module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); payload=root/'payload';payload.mkdir(mode=0o700)
            (payload/'unindexed-link').symlink_to('/etc/passwd')
            with self.assertRaises(module.ReleasePackageError): module.assemble_bundle(payload,root/'output')
            self.assertFalse((root/'output').exists())


class AssemblerAppIdentityTests(unittest.TestCase):
    """The only way app-identity.json enters a bundle: the explicit Keychain flag, synthetic values here."""
    API_ID = 1357924
    API_HASH = '0123456789abcdef0123456789abcdef'

    @classmethod
    def assembler(cls):
        import sys
        path = Path(__file__).parents[1] / 'packaging/assemble_bundle.py'
        spec = importlib.util.spec_from_file_location('assembly_identity', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def payload(self, root):
        from bundle_payload import make_payload
        return make_payload(root)

    def runner(self, calls=None, *, fail=None, values=None):
        import subprocess
        values = values or {'api_id': str(self.API_ID), 'api_hash': self.API_HASH}
        def run(args, **kwargs):
            if calls is not None: calls.append(list(args))
            if fail: return subprocess.CompletedProcess(args, 44, 'private-keychain-output', 'private-keychain-output')
            return subprocess.CompletedProcess(args, 0, values[args[-1]] + '\n', '')
        return run

    def extract(self, result, root):
        archive, index = result.archive, load_archive_index(result.index.read_bytes())
        extract_verified_archive(archive, index, root / 'extracted')
        return index, root / 'extracted'

    def test_flag_writes_one_canonical_indexed_read_only_identity_and_nothing_leaks_elsewhere(self):
        import contextlib
        import io as _io
        from telegram_search_mcp.app_identity import parse_app_identity
        module = self.assembler()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); payload = self.payload(root); calls = []
            before = sorted(str(p.relative_to(payload)) for p in payload.rglob('*'))
            out, err = _io.StringIO(), _io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = module.assemble_bundle(payload, root / 'output', app_identity_from_keychain=True,
                                                keychain_runner=self.runner(calls))
            self.assertEqual(out.getvalue() + err.getvalue(), '')
            self.assertEqual([c[-1] for c in calls], ['api_id', 'api_hash'])
            self.assertEqual(sorted(str(p.relative_to(payload)) for p in payload.rglob('*')), before)  # payload untouched
            index, extracted = self.extract(result, root)
            record = next(f for f in index.files if f.path == 'app-identity.json')
            written = (extracted / 'app-identity.json').read_bytes()
            self.assertEqual(written, b'{"api_hash":"%s","api_id":%d,"version":1}\n' % (self.API_HASH.encode(), self.API_ID))
            self.assertEqual((record.bytes, record.sha256, record.executable),
                             (len(written), hashlib.sha256(written).hexdigest(), False))
            self.assertEqual((extracted / 'app-identity.json').stat().st_mode & 0o777, 0o400)
            credentials = parse_app_identity(written)
            self.assertEqual((credentials.api_id, credentials.api_hash), (self.API_ID, self.API_HASH))
            self.assertEqual(index.unpacked_bytes, sum(f.bytes for f in index.files))
            for generated in (result.audit, result.index, result.provenance):  # only the archive carries the pair
                self.assertNotIn(self.API_HASH.encode(), generated.read_bytes())
                self.assertNotIn(b'"api_id"', generated.read_bytes())
            self.assertIn(b'<keychain>', result.audit.read_bytes())

    def test_without_the_flag_no_identity_exists_and_the_keychain_is_never_read(self):
        module = self.assembler()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); payload = self.payload(root); calls = []
            result = module.assemble_bundle(payload, root / 'output', keychain_runner=self.runner(calls))
            self.assertEqual(calls, [])
            index, extracted = self.extract(result, root)
            self.assertNotIn('app-identity.json', [f.path for f in index.files])
            self.assertFalse((extracted / 'app-identity.json').exists())

    def test_a_payload_carrying_its_own_identity_is_refused_with_or_without_the_flag(self):
        module = self.assembler()
        for flag in (False, True):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); payload = self.payload(root)
                (payload / 'app-identity.json').write_bytes(b'{}'); (payload / 'app-identity.json').chmod(0o600)
                with self.assertRaises(module.ReleasePackageError):
                    module.assemble_bundle(payload, root / 'output', app_identity_from_keychain=flag,
                                           keychain_runner=self.runner())
                self.assertFalse((root / 'output').exists())

    def test_unavailable_or_invalid_keychain_values_are_refused_redacted_before_anything_is_written(self):
        module = self.assembler()
        bad = {'api_id': str(self.API_ID), 'api_hash': 'not-hex-' + 'z' * 24}
        for label, runner in [('keychain failure', self.runner(fail=True)),
                              ('invalid hash', self.runner(values=bad)),
                              ('invalid id', self.runner(values={'api_id': '0', 'api_hash': self.API_HASH})),
                              ('runner raises', lambda *a, **k: (_ for _ in ()).throw(OSError(self.API_HASH)))]:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); payload = self.payload(root)
                with self.assertRaises(module.ReleasePackageError) as caught:
                    module.assemble_bundle(payload, root / 'output', app_identity_from_keychain=True, keychain_runner=runner)
                error = caught.exception
                self.assertEqual(str(error), 'app_identity_unavailable')
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                for secret in ('private-keychain-output', self.API_HASH, 'not-hex-'):
                    self.assertNotIn(secret, repr(error.args) + str(error))
                self.assertFalse((root / 'output').exists())

    def test_the_bundled_package_must_be_exactly_this_checkouts_and_carry_segno(self):
        import telegram_search_mcp
        module = self.assembler()
        source = Path(telegram_search_mcp.__file__).parent
        changes = {
            'missing module': lambda app: (app / 'connect_cli.py').unlink(),
            'stale module': lambda app: (app / 'connect_cli.py').write_bytes(b'# an older build\n'),
            'extra module': lambda app: (app / 'leftover.py').write_bytes(b''),
            'extra non-python file': lambda app: (app / 'notes.txt').write_bytes(b'x'),
            'missing segno': lambda app: ((app.parent / 'segno/__init__.py').unlink(), (app.parent / 'segno').rmdir()),
        }
        for label, change in changes.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); payload = self.payload(root)
                app = payload / 'app/telegram_search_mcp'
                self.assertEqual(sorted(p.name for p in app.iterdir()), sorted(p.name for p in source.glob('*.py')))
                change(app)
                for path in app.parent.rglob('*'):
                    if path.is_file(): path.chmod(0o600)
                with self.assertRaises(module.ReleasePackageError) as caught:
                    module.assemble_bundle(payload, root / 'output')
                self.assertEqual(str(caught.exception), 'payload_app_mismatch')
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn('connect_cli', repr(caught.exception.args))
                self.assertFalse((root / 'output').exists())
        with tempfile.TemporaryDirectory() as tmp:  # The exact checkout package passes.
            root = Path(tmp).resolve()
            result = module.assemble_bundle(self.payload(root), root / 'output')
            index = load_archive_index(result.index.read_bytes())
            bundled = {f.path for f in index.files if f.path.startswith('app/telegram_search_mcp/')}
            self.assertEqual(bundled, {'app/telegram_search_mcp/' + p.name for p in source.glob('*.py')})
            self.assertIn('app/segno/__init__.py', {f.path for f in index.files})

    def test_command_line_exposes_the_flag_only_as_an_explicit_switch(self):
        import re
        text = (Path(__file__).parents[1] / 'packaging/assemble_bundle.py').read_text()
        self.assertEqual(len(re.findall(r"add_argument\('--app-identity-from-keychain',action='store_true'", text)), 1)
