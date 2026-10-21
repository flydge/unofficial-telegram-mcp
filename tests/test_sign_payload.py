"""sign_payload (CLIENTS-5): Mach-O by magic, inside-out hardened-runtime signing, the entitlement matrix.

Real codesign with the ad hoc identity over stock signed Mach-O files standing in for the payload's
(thin slices and a universal dylib, one slice's filetype patched to a bundle). The Developer ID path
runs real ad hoc signing behind a runner that stands in for the team identity. No Keychain identity,
network or payload interpreter is used; the matrix runs against a fake probe.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from telegram_search_mcp.native_runtime import designated_requirement

ROOT = Path(__file__).parents[1]
TEAM = 'ABCDE12345'
CN = f'Developer ID Application: Synthetic Example ({TEAM})'
PREFIX = 'io.example.synthetic'
DLV = 'com.apple.security.cs.disable-library-validation'


def load():
    spec = importlib.util.spec_from_file_location('sign_payload_under_test', ROOT / 'packaging/sign_payload.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SP = load()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def thin(path, cpu=0x0100000c):
    """The arm64 slice of a stock universal Mach-O: a complete thin Mach-O."""
    data = Path(path).read_bytes()
    for index in range(int.from_bytes(data[4:8], 'big')):
        kind, _, offset, size, _ = struct.unpack('>5I', data[8 + 20 * index:28 + 20 * index])
        if kind == cpu:
            return data[offset:offset + size]
    raise AssertionError('no arm64 slice')


def codesign(*args):
    return subprocess.run(['/usr/bin/codesign', *args], capture_output=True, check=True, timeout=60)


def display(path):
    return codesign('-d', '--verbose=4', str(path)).stderr.decode()


def make_payload(root):
    """A sealed synthetic payload: three kinds of Mach-O, three pinned natives, non-Mach-O files."""
    dylib = thin('/usr/lib/libffi-trampolines.dylib')
    bundle = bytearray(dylib)
    bundle[12:16] = (8).to_bytes(4, 'little')  # MH_BUNDLE, as a Python extension module is
    files = {'python/bin/python3': (thin('/usr/bin/true'), True),
             'python/lib/libpython3.14.dylib': (Path('/usr/lib/libffi-trampolines.dylib').read_bytes(), True),
             'app/PIL/.dylibs/libfoo.1.dylib': (dylib, True),
             'app/_ext.cpython-314-darwin.so': (bytes(bundle), True),
             'app/pkg/data.txt': (b'not a mach-o', False),
             'app/pkg/Thing.class': (b'\xca\xfe\xba\xbe\x00\x00\x00\x34' + b'\0' * 32, False),  # Java, not fat
             'manifest/release.json': (b'{}', False)}
    payload = root / 'payload'
    stage = root / 'stage'
    stage.mkdir()
    for name in ('libtdjson.dylib', 'libssl.3.dylib', 'libcrypto.3.dylib'):  # pinned ad hoc natives
        (stage / name).write_bytes(dylib)
        codesign('--force', '--sign', '-', str(stage / name))
        files['native/' + name] = ((stage / name).read_bytes(), True)
    inventory = [{'path': n, 'sha256': sha(d)} for n, (d, _) in sorted(files.items()) if _kind(d)]
    files['provenance/macho-inventory.json'] = (json.dumps({'files': inventory}).encode(), False)
    for name, (data, executable) in files.items():
        path = payload / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o500 if executable else 0o400)
    for base, _, _ in os.walk(payload):
        os.chmod(base, 0o700)
    return payload


def _kind(data):
    head = data[:4]
    if head == b'\xcf\xfa\xed\xfe':
        return True
    return head == b'\xca\xfe\xba\xbe' and 0 < int.from_bytes(data[4:8], 'big') <= 8



def tree(root):
    return {p.relative_to(root).as_posix(): (sha(p.read_bytes()), p.stat().st_mode & 0o777)
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


def team_runner(calls):
    """Real codesign standing in for a Developer ID identity: ad hoc signing, Team ID reported."""
    def run(argv, **options):
        calls.append(list(argv))
        argv = list(argv)
        if '--sign' in argv:
            argv[argv.index('--sign') + 1] = '-'
            argv = [a for a in argv if not a.startswith('--timestamp')]
        if '-R' in argv:
            at = argv.index('-R')
            argv = argv[:at] + argv[at + 2:]
        result = subprocess.run(argv, **options)
        if argv[1:3] == ['-d', '--verbose=4']:
            result = subprocess.CompletedProcess(argv, result.returncode, result.stdout,
                                                 result.stderr.replace(b'TeamIdentifier=not set',
                                                                       f'TeamIdentifier={TEAM}'.encode()))
        return result
    return run


class SignPayloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.payload = make_payload(self.root)
        self.before = tree(self.payload)

    def test_ad_hoc_signs_inside_out_with_runtime_and_stable_identifiers(self):
        result = SP.sign_payload(self.payload, self.root / 'signed', identity='-', prefix=PREFIX)
        signed = self.root / 'signed'
        self.assertEqual(tree(self.payload), self.before)  # the input is never changed
        record = json.loads((signed / 'provenance/signing.json').read_bytes())
        order = [f['path'] for f in record['files']]
        self.assertEqual(order, ['app/PIL/.dylibs/libfoo.1.dylib', 'native/libcrypto.3.dylib', 'native/libssl.3.dylib',
                                 'native/libtdjson.dylib', 'python/lib/libpython3.14.dylib',
                                 'app/_ext.cpython-314-darwin.so', 'python/bin/python3'])
        inventory = json.loads((self.payload / 'provenance/macho-inventory.json').read_bytes())
        self.assertEqual(sorted(order), sorted(f['path'] for f in inventory['files']))
        self.assertEqual((result['total'], result['signed'], result['kept_pinned']), (7, 4, 3))
        self.assertEqual(result['interpreter_entitlements'], [DLV])
        for entry in record['files']:
            path = signed / entry['path']
            codesign('--verify', '--strict', str(path))
            self.assertEqual(entry['post_sha256'], sha(path.read_bytes()))
            shown = display(path)
            self.assertIn(f"CDHash={entry['cdhash']}", shown)
            if entry['path'].startswith('native/'):
                self.assertEqual(entry['action'], 'kept_pinned')
                self.assertEqual(entry['pre_sha256'], entry['post_sha256'])  # an ad-hoc v2 lock admits only the pins
                continue
            self.assertEqual(entry['action'], 'signed')
            self.assertNotEqual(entry['pre_sha256'], entry['post_sha256'])
            self.assertIn('runtime)', shown.split('flags=')[1].split()[0])
            self.assertTrue(entry['runtime'])
        identifiers = {f['path']: f['identifier'] for f in record['files']}
        self.assertEqual(identifiers['python/bin/python3'], PREFIX + '.python3')
        self.assertEqual(identifiers['app/PIL/.dylibs/libfoo.1.dylib'], PREFIX + '.app.PIL.dylibs.libfoo.1.dylib')
        self.assertEqual(identifiers['app/_ext.cpython-314-darwin.so'], PREFIX + '.app._ext.cpython-314-darwin.so')
        entitlements = codesign('-d', '--entitlements', '-', '--xml', str(signed / 'python/bin/python3')).stdout
        self.assertEqual(set(plistlib.loads(entitlements)), {DLV})
        self.assertEqual(codesign('-d', '--entitlements', '-', '--xml',
                                  str(signed / 'app/_ext.cpython-314-darwin.so')).stdout.strip(), b'')
        modes = {name: mode for name, (_, mode) in tree(signed).items()}
        self.assertEqual(modes['python/bin/python3'], 0o500)
        self.assertEqual(modes['app/pkg/data.txt'], 0o400)
        self.assertEqual(modes['provenance/signing.json'], 0o400)
        self.assertEqual(set(modes) - {'provenance/signing.json'}, set(self.before))
        for name, (digest, _) in self.before.items():
            if name not in identifiers:
                self.assertEqual(tree(signed)[name][0], digest)
        self.assertEqual(result['tree_sha256'], SP.tree_digest(signed, SP._walk(signed)))

    def test_ad_hoc_signing_is_reproducible(self):
        first = SP.sign_payload(self.payload, self.root / 'one', identity='-', prefix=PREFIX)
        second = SP.sign_payload(self.payload, self.root / 'two', identity='-', prefix=PREFIX)
        self.assertEqual(first, second)

    def test_developer_id_signs_natives_with_timestamp_and_the_team_requirement(self):
        calls = []
        result = SP.sign_payload(self.payload, self.root / 'signed', identity=CN, team_id=TEAM, prefix=PREFIX,
                                 runner=team_runner(calls))
        self.assertEqual((result['signed'], result['kept_pinned'], result['interpreter_entitlements']), (7, 0, []))
        signs = [c for c in calls if '--sign' in c]
        self.assertEqual(len(signs), 7)
        for call in signs:
            self.assertEqual(call[:9], ['/usr/bin/codesign', '--force', '--sign', CN, '--identifier', call[5],
                                        '--options', 'runtime', '--timestamp'])
            self.assertNotIn('--entitlements', call)  # the Developer ID plist is empty until the matrix proves more
        verifies = [c for c in calls if '--verify' in c]
        self.assertTrue(all(c[3:5] == ['-R', designated_requirement(TEAM)] for c in verifies))
        record = json.loads((self.root / 'signed/provenance/signing.json').read_bytes())
        self.assertEqual({f['action'] for f in record['files']}, {'signed'})
        self.assertEqual(record['team_id'], TEAM)

    def test_refusals_are_fixed_codes_and_echo_nothing(self):
        cases = []
        bad = self.root / 'bad-inventory'
        shutil.copytree(self.payload, bad)
        os.chmod(bad / 'provenance', 0o700)
        os.chmod(bad / 'provenance/macho-inventory.json', 0o600)
        (bad / 'provenance/macho-inventory.json').write_text('{"files": []}')
        cases.append((dict(payload=bad, identity='-'), 'inventory_mismatch'))
        cases.append((dict(identity=CN, team_id='ZYXWV98765'), 'identity_refused'))
        cases.append((dict(identity='Apple Development: x (ABCDE12345)', team_id=TEAM), 'identity_refused'))
        cases.append((dict(identity='-', team_id=TEAM), 'identity_refused'))
        forbidden = self.root / 'forbidden.plist'
        forbidden.write_bytes(plistlib.dumps({'com.apple.security.cs.allow-dyld-environment-variables': True}))
        cases.append((dict(identity='-', entitlements=forbidden), 'entitlement_forbidden'))
        unknown = self.root / 'unknown.plist'
        unknown.write_bytes(plistlib.dumps({'com.apple.security.network.client': True}))
        cases.append((dict(identity='-', entitlements=unknown), 'entitlement_refused'))
        failing = lambda argv, **options: subprocess.CompletedProcess(argv, 1, b'', b'private-codesign-output')
        cases.append((dict(identity='-', runner=failing), 'sign_failed'))
        for index, (options, code) in enumerate(cases):
            options = {'payload': self.payload, 'prefix': PREFIX, **options}
            with self.subTest(code=code), self.assertRaises(SP.SigningError) as caught:
                SP.sign_payload(options.pop('payload'), self.root / f'out-{index}', **options)
            self.assertEqual(str(caught.exception), code)
            self.assertIsNone(caught.exception.__cause__)
        os.symlink(self.root / 'elsewhere', self.root / 'exists')
        with self.assertRaises(SP.SigningError) as caught:
            SP.sign_payload(self.payload, self.root / 'exists', identity='-', prefix=PREFIX)
        self.assertEqual(str(caught.exception), 'output_refused')

    def test_a_wrong_identifier_after_signing_refuses(self):
        def lying(argv, **options):
            result = subprocess.run(argv, **options)
            if argv[1:3] == ['-d', '--verbose=4'] and 'python3' in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, result.stdout,
                                                   result.stderr.replace(b'Identifier=' + PREFIX.encode(), b'Identifier=x'))
            return result
        with self.assertRaises(SP.SigningError) as caught:
            SP.sign_payload(self.payload, self.root / 'signed', identity='-', prefix=PREFIX, runner=lying)
        self.assertEqual(str(caught.exception), 'verify_failed')


class MachOTests(unittest.TestCase):
    def test_magic_not_name_decides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {'a.txt': (thin('/usr/bin/true'), ('mach-o-64', 2)),
                     'b.dylib': (b'plain text', None),
                     'c': (Path('/usr/lib/libffi-trampolines.dylib').read_bytes(), ('universal', 6)),
                     'd.class': (b'\xca\xfe\xba\xbe\x00\x00\x00\x34' + b'\0' * 64, None)}
            for name, (data, expected) in cases.items():
                (root / name).write_bytes(data)
                self.assertEqual(SP.macho(root / name), expected, name)
            (root / 'e').write_bytes(b'\xcf\xfa\xed\xfe' + b'\0' * 8)  # truncated header
            with self.assertRaises(SP.SigningError):
                SP.macho(root / 'e')
            mixed = bytearray(Path('/usr/lib/libffi-trampolines.dylib').read_bytes())
            offset = int.from_bytes(mixed[16:20], 'big')
            mixed[offset + 12:offset + 16] = (2).to_bytes(4, 'little')  # one slice an executable
            (root / 'f').write_bytes(bytes(mixed))
            with self.assertRaises(SP.SigningError):
                SP.macho(root / 'f')

    def test_identifiers_are_stable_and_closed(self):
        self.assertEqual(SP.identifier('python/bin/python3', PREFIX), PREFIX + '.python3')
        self.assertEqual(SP.identifier('native/libtdjson.dylib', PREFIX), PREFIX + '.native.libtdjson.dylib')
        for bad in ('app/a b.so', 'app/ä.so', 'app/' + 'x' * 300 + '.so'):
            with self.assertRaises(SP.SigningError):
                SP.identifier(bad, PREFIX)


class EntitlementTests(unittest.TestCase):
    def test_committed_plists_are_minimal_and_never_forbidden(self):
        directory = ROOT / 'packaging/entitlements'
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['python3-adhoc.plist', 'python3.plist'])
        self.assertEqual(SP.load_entitlements((directory / 'python3.plist').read_bytes()), frozenset())
        self.assertEqual(SP.load_entitlements((directory / 'python3-adhoc.plist').read_bytes()), frozenset({DLV}))
        for path in directory.iterdir():
            for key in SP.FORBIDDEN:
                self.assertNotIn(key, path.read_text())
        self.assertNotIn('allow-dyld-environment-variables', ''.join(map(''.join, SP.CANDIDATES)))

    def test_grammar(self):
        for value, code in (({'com.apple.security.get-task-allow': True}, 'entitlement_forbidden'),
                            ({DLV: False}, 'entitlement_refused'), ({DLV: 'yes'}, 'entitlement_refused'),
                            ({'com.apple.security.app-sandbox': True}, 'entitlement_refused')):
            with self.subTest(value=value), self.assertRaises(SP.SigningError) as caught:
                SP.load_entitlements(plistlib.dumps(value))
            self.assertEqual(str(caught.exception), code)
        for data in (b'not a plist', plistlib.dumps([DLV])):
            with self.assertRaises(SP.SigningError):
                SP.load_entitlements(data)


class MatrixTests(unittest.TestCase):
    """The matrix logic over a fake codesign and a fake probe; the real run is recorded by release_build."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        signed = self.root / 'signed'
        for name, data in (('python/bin/python3', b'interpreter'), ('python/lib/os.py', b''), ('app/x.py', b'')):
            (signed / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            (signed / name).write_bytes(data)
            (signed / name).chmod(0o500 if name.endswith('python3') else 0o400)
        self.signed = signed
        self.calls = []

    def runner(self, needs):
        state = {'granted': ()}

        def run(argv, **options):
            self.calls.append((list(argv), options))
            if '--sign' in argv:
                at = argv.index('--entitlements') + 1 if '--entitlements' in argv else None
                state['granted'] = tuple(plistlib.loads(Path(argv[at]).read_bytes())) if at else ()
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            if '--entitlements' in argv:
                body = plistlib.dumps({k: True for k in state['granted']}) if state['granted'] else b''
                return subprocess.CompletedProcess(argv, 0, body, b'')
            self.assertEqual(argv[1:5], ['-I', '-S', '-B', '-c'])
            self.assertEqual(set(options['env']), {'PATH', 'LC_ALL', 'HOME'})
            self.assertTrue(Path(options['env']['HOME']).is_relative_to(self.root / 'matrix'))
            ok = set(needs) <= set(state['granted'])
            probes = {name: 'ok' for name in SP.PROBES}
            if not ok:
                probes.update(distributions='failed:ImportError', tdlib='failed:OSError')
            return subprocess.CompletedProcess(argv, 0, json.dumps(probes).encode(), b'')
        return run

    def test_smallest_passing_set_is_the_proof(self):
        result = SP.entitlement_matrix(self.signed, self.root / 'matrix', identity='-', prefix=PREFIX,
                                       runner=self.runner({DLV}))
        self.assertEqual(result['minimal'], [DLV])
        self.assertEqual([row['entitlements'] for row in result['candidates']], [list(c) for c in SP.CANDIDATES])
        self.assertFalse(result['candidates'][0]['passed'])
        self.assertEqual(result['deferred_probes'], ['launch_agent_broker', 'connect_helper'])
        self.assertEqual(SP.check_matrix(result, 'adhoc'), [DLV])
        with self.assertRaises(SP.SigningError):
            SP.check_matrix(result, 'developer-id')
        python = self.root / 'matrix/python/bin/python3'
        self.assertTrue(all(call[0][-1] == str(python) for call in self.calls if '--sign' in call[0]))
        self.assertEqual((self.signed / 'python/bin/python3').read_bytes(), b'interpreter')

    def test_empty_set_suffices_for_a_team_signed_tree(self):
        result = SP.entitlement_matrix(self.signed, self.root / 'matrix', identity=CN, team_id=TEAM, prefix=PREFIX,
                                       runner=self.runner(set()))
        self.assertEqual(result['minimal'], [])
        self.assertEqual(SP.check_matrix(result, 'developer-id'), [])
        signs = [call[0] for call in self.calls if '--sign' in call[0]]
        self.assertTrue(all('--timestamp=none' in argv for argv in signs))

    def test_no_passing_candidate_or_a_broken_probe_proves_nothing(self):
        inner = self.runner(set())

        def broken(argv, **options):
            if '--sign' in argv or '--entitlements' in argv:
                return inner(argv, **options)
            return subprocess.CompletedProcess(argv, 0, b'not json', b'')
        result = SP.entitlement_matrix(self.signed, self.root / 'matrix', identity='-', prefix=PREFIX, runner=broken)
        self.assertIsNone(result['minimal'])
        self.assertEqual({v for row in result['candidates'] for v in row['probes'].values()}, {'failed:probe'})
        with self.assertRaises(SP.SigningError):
            SP.check_matrix(result, 'adhoc')


if __name__ == '__main__':
    unittest.main()
