"""prepare_native over synthetic kegs: relocation, refusals and the emitted lock. No Homebrew or codesign."""
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from telegram_search_mcp import native_runtime
from telegram_search_mcp.native_runtime import APPROVED_PINS, COMMIT, REQUIRED_SYMBOLS, parse_native_manifest

ROOT = Path(__file__).parents[1]
OPT = '/opt/homebrew/opt/openssl@3/lib/'
SYSTEM = ['/usr/lib/libz.1.dylib', '/usr/lib/libc++.1.dylib', '/usr/lib/libSystem.B.dylib']


def load(name='prepare_native_under_test', script='prepare_native.py'):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def dylib(deps, *, rpath=False, symbols=(), minos=(26, 0)):
    """A minimal thin arm64 MH_DYLIB that native_runtime's reader accepts."""
    def named(command, name, header=24):
        raw = name.encode() + b'\0'
        size = (header + len(raw) + 7) // 8 * 8
        fixed = struct.pack('<6I', command, size, 24, 2, 0x10000, 0x10000) if header == 24 else struct.pack('<3I', command, size, 12)
        return fixed + raw.ljust(size - header, b'\0')
    commands = [named(0xd, '/opt/homebrew/opt/synthetic/lib/self.dylib')] + [named(0xc, d) for d in deps]
    if rpath:
        commands.append(named(0x8000001c, '/opt/homebrew/lib', header=12))
    commands.append(struct.pack('<6I', 0x32, 24, 1, (minos[0] << 16) | (minos[1] << 8), 0, 0))
    names = b'\0' + b''.join(b'_' + s.encode() + b'\0' for s in symbols)
    size = sum(len(c) for c in commands) + 24
    symoff = 32 + size
    entries, index = b'', 1
    for symbol in symbols:
        entries += struct.pack('<IBBHQ', index, 0x0f, 1, 0, 0)
        index += len(symbol) + 2
    commands.append(struct.pack('<6I', 2, 24, symoff, len(symbols), symoff + len(entries), len(names)))
    header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, len(commands), size, 0, 0)
    return header + b''.join(commands) + entries + names + b'\0' * 16


class Kegs:
    def __init__(self, root, *, tdlib_name='HEAD-d1085f9', revision=COMMIT, openssl='3.6.4', dependency='3.6.4',
                 tdjson_deps=None, rpath=False):
        self.tdlib = root / 'Cellar/tdlib' / tdlib_name
        self.openssl = root / 'Cellar/openssl@3' / openssl
        self.source = root / 'td-source'
        for path in (self.tdlib / 'lib', self.openssl / 'lib', self.source / 'td/generate/scheme'):
            path.mkdir(parents=True)
        crypto = str(self.openssl / 'lib/libcrypto.3.dylib')
        self.specs = {
            'libtdjson.dylib': dict(deps=tdjson_deps or [OPT + 'libssl.3.dylib', OPT + 'libcrypto.3.dylib', *SYSTEM],
                                    symbols=REQUIRED_SYMBOLS, rpath=rpath),
            'libssl.3.dylib': dict(deps=[crypto, SYSTEM[2]], symbols=(), rpath=False),
            'libcrypto.3.dylib': dict(deps=[SYSTEM[2]], symbols=(), rpath=False)}
        (self.tdlib / 'lib/libtdjson.1.8.67.dylib').write_bytes(dylib(**self.specs['libtdjson.dylib']))
        (self.openssl / 'lib/libssl.3.dylib').write_bytes(dylib(**self.specs['libssl.3.dylib']))
        (self.openssl / 'lib/libcrypto.3.dylib').write_bytes(dylib(**self.specs['libcrypto.3.dylib']))
        built = {'os': 'Macintosh', 'os_version': 'macOS 26.6', 'xcode': '26.6', 'clt': '26.6.0.0.1'}
        (self.tdlib / 'INSTALL_RECEIPT.json').write_text(json.dumps({
            'compiler': 'clang', 'used_options': [], 'arch': 'arm64', 'built_on': built,
            'runtime_dependencies': [{'full_name': 'openssl@3', 'version': dependency}],
            'source': {'path': '/private-formula-cache/tdlib.rb', 'scm_revision': revision,
                       'versions': {'stable': '1.8.0', 'head': tdlib_name}}}))
        (self.openssl / 'INSTALL_RECEIPT.json').write_text(json.dumps({
            'compiler': 'clang', 'arch': 'arm64', 'built_on': built,
            'source': {'path': '/private-api-cache/packages.json', 'versions': {'stable': openssl}}}))
        for name in ('LICENSE.txt', 'AUTHORS.md', 'README.md'):
            (self.openssl / name).write_text('openssl ' + name)
        (self.source / 'LICENSE_1_0.txt').write_text('boost license')
        (self.source / 'td/generate/scheme/td_api.tl').write_bytes(b'synthetic schema\n')

    def runner(self, calls, *, fail=None, inject_rpath=False):
        def run(command, **kwargs):
            calls.append(command)
            self.assertion(kwargs)
            if fail and Path(command[0]).name == fail:
                return subprocess.CompletedProcess(command, 1, b'private tool output', b'')
            target = Path(command[-1])
            if command[0] == '/usr/bin/install_name_tool':
                spec = dict(self.specs[target.name])
                changes = dict(zip(command[4:-1:3], command[5:-1:3]))
                spec['deps'] = [changes.get(d, d) for d in spec['deps']]
                spec['rpath'] = spec['rpath'] or inject_rpath
                target.write_bytes(dylib(**spec))
            else:
                target.write_bytes(target.read_bytes() + b'synthetic-adhoc-signature')
            return subprocess.CompletedProcess(command, 0, b'', b'')
        return run

    @staticmethod
    def assertion(kwargs):
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'} and kwargs['timeout'] <= 120


class PrepareNativeTests(unittest.TestCase):
    def setUp(self):
        self.module = load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        # The real schema pin cannot be matched synthetically; check_schema has its own tests and the
        # build Mac run checks the real td_api.tl.
        patcher = mock.patch.object(self.module, 'check_schema')
        patcher.start()
        self.addCleanup(patcher.stop)
        signature = mock.patch.object(native_runtime, '_verify_signature')
        signature.start()
        self.addCleanup(signature.stop)

    def prepare(self, kegs, *, runner=None, calls=None, **options):
        return self.module.prepare_native(self.root / 'out', tdlib_source=kegs.source, tdlib_keg=kegs.tdlib,
                                          openssl_keg=kegs.openssl, runner=runner or kegs.runner([] if calls is None else calls),
                                          probe=False, **options)

    def refused(self, code, kegs, **options):
        with self.assertRaises(self.module.NativePreparationError) as caught:
            self.prepare(kegs, **options)
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn('private', repr(caught.exception.args))

    def test_relocates_signs_and_emits_a_parseable_lock_with_redacted_provenance(self):
        kegs, calls = Kegs(self.root), []
        summary = self.prepare(kegs, calls=calls)
        lock = parse_native_manifest((self.root / 'out/tdlib-lock.json').read_bytes())
        self.assertEqual([a.dependencies for a in lock.artifacts], [
            ['@loader_path/libssl.3.dylib', '@loader_path/libcrypto.3.dylib', *SYSTEM],
            ['@loader_path/libcrypto.3.dylib', SYSTEM[2]], [SYSTEM[2]]])
        for artifact in lock.artifacts:
            data = (self.root / 'out/native' / artifact.name).read_bytes()
            self.assertEqual((artifact.bytes, artifact.sha256), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertTrue(data.endswith(b'synthetic-adhoc-signature'))
        self.assertEqual(calls[0][:3], ['/usr/bin/install_name_tool', '-id', '@loader_path/libtdjson.dylib'])
        self.assertEqual(calls[1][:4], ['/usr/bin/codesign', '--force', '--sign', '-'])
        self.assertEqual((lock.build.build_os, lock.build.xcode, lock.build.openssl_version), ('macOS 26.6', '26.6', '3.6.4'))
        self.assertEqual(summary['artifacts'][0]['name'], 'libtdjson.dylib')
        for label in ('tdlib', 'openssl'):
            text = (self.root / 'out/provenance' / f'{label}-build.json').read_text()
            self.assertNotIn('private-', text.replace('private-machine-path-redacted', ''))
        self.assertTrue((self.root / 'out/notices/openssl-3.6.4/LICENSE.txt').is_file())
        self.assertTrue((self.root / 'out/notices/tdlib/LICENSE_1_0.txt').is_file())
        # The payload builder admits exactly this provenance against exactly this lock.
        builder = load('prepare_payload_under_native_test', 'prepare_payload.py')
        emitted = json.loads((self.root / 'out/tdlib-lock.json').read_bytes())
        for label, check in (('tdlib', builder._tdlib_receipt), ('openssl', builder._openssl_receipt)):
            check(builder._provenance_record((self.root / 'out/provenance' / f'{label}-build.json').read_bytes()), emitted)
        emitted['build']['xcode'] = '26.5'
        with self.assertRaises(builder.PayloadError):
            builder._tdlib_receipt(builder._provenance_record((self.root / 'out/provenance/tdlib-build.json').read_bytes()),
                                   emitted)

    def test_compare_reports_reproduction_and_each_differing_field(self):
        kegs = Kegs(self.root)
        committed = self.root / 'committed.json'
        reference = self.root / 'reference'
        first = self.module.prepare_native(reference, tdlib_source=kegs.source, tdlib_keg=kegs.tdlib, openssl_keg=kegs.openssl,
                                           runner=kegs.runner([]), probe=False)
        committed.write_bytes((reference / 'tdlib-lock.json').read_bytes())
        summary = self.prepare(kegs, committed=committed)
        self.assertEqual((summary['reproduces_committed_lock'], summary['differing_fields']), (True, []))
        self.assertEqual(summary['lock_sha256'], first['lock_sha256'])
        value = json.loads(committed.read_bytes())
        value['build']['xcode'] = '26.5'
        value['artifacts'][1]['sha256'] = '0' * 64
        committed.chmod(0o600)
        committed.write_text(json.dumps(value, indent=2) + '\n')
        (self.root / 'out').rename(self.root / 'previous')
        summary = self.prepare(kegs, committed=committed)
        self.assertFalse(summary['reproduces_committed_lock'])
        self.assertEqual(summary['differing_fields'], ['artifacts[1].sha256', 'build.xcode'])

    def test_a_wrong_keg_refuses(self):
        cases = {'other keg version': dict(tdlib_name='HEAD-0000000'), 'other scm revision': dict(revision='0' * 40),
                 'other openssl keg': dict(openssl='3.6.3'), 'receipt names other openssl': dict(dependency='3.6.3')}
        for label, options in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                self.root = Path(tmp).resolve()
                self.refused('keg_mismatch', Kegs(self.root, **options))

    def test_a_stray_rpath_or_unexpected_dependency_refuses(self):
        self.refused('search_path_command', Kegs(self.root, rpath=True))
        with tempfile.TemporaryDirectory() as tmp:
            self.root = Path(tmp).resolve()
            kegs = Kegs(self.root)
            self.refused('search_path_command', kegs, runner=kegs.runner([], inject_rpath=True))
        with tempfile.TemporaryDirectory() as tmp:
            self.root = Path(tmp).resolve()
            self.refused('dependency_mismatch', Kegs(self.root, tdjson_deps=[OPT + 'libssl.3.dylib', '/usr/local/lib/libfoo.dylib']))
        with tempfile.TemporaryDirectory() as tmp:
            self.root = Path(tmp).resolve()
            self.refused('dependency_mismatch', Kegs(self.root, tdjson_deps=['/opt/homebrew/opt/other/lib/libssl.3.dylib']))

    def test_tool_failures_and_an_existing_output_refuse(self):
        kegs = Kegs(self.root)
        self.refused('tool_failed', kegs, runner=kegs.runner([], fail='codesign'))
        self.assertTrue((self.root / 'out').is_dir())  # A failed private output is retained, never reused.
        self.refused('output_refused', kegs)

    def test_schema_must_match_the_approved_row(self):
        module = load()
        kegs = Kegs(self.root)
        row = dataclasses.replace(APPROVED_PINS[0], schema_sha256=hashlib.sha256(b'synthetic schema\n').hexdigest())
        module.check_schema(kegs.source, row)
        with self.assertRaises(module.NativePreparationError) as caught:
            module.check_schema(kegs.source, APPROVED_PINS[0])
        self.assertEqual(str(caught.exception), 'schema_mismatch')
        (kegs.source / 'td/generate/scheme/td_api.tl').unlink()
        with self.assertRaises(module.NativePreparationError):
            module.check_schema(kegs.source, row)


if __name__ == '__main__':
    unittest.main()
