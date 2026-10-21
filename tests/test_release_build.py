"""release_build (CLIENTS-14): step order, kit and assets, pins, provenance, source archive and `check`.

The heavy steps (payload, signing, matrix, repin, assembly, seed, notarization) are fakes that write
synthetic outputs; the orchestrator, the kit, the corresponding-source archive and the evidence are
real. The checkout is a synthetic git repository. The real ad hoc dry run is recorded separately.
"""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

from telegram_search_mcp import product_identity
from telegram_search_mcp.native_runtime import designated_requirement

ROOT = Path(__file__).parents[1]
DLV = 'com.apple.security.cs.disable-library-validation'
TEAM = 'ABCDE12345'
IDENTITY = f'Developer ID Application: Synthetic Example ({TEAM})'
VERSION = '1.2.3'
SERVICE = 'io.example.synthetic.app-identity'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RB = load('release_build_under_test', 'release_build.py')
SP = load('sign_payload_for_release_build', 'sign_payload.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'LC_ALL': 'C'}
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Synthetic Author',
                           '-c', 'user.email=author@example.invalid', '-c', 'commit.gpgsign=false',
                           '-c', 'tag.gpgsign=false', '-c', 'init.defaultBranch=main', *args],
                          env=env, capture_output=True, text=True, timeout=60, check=True).stdout.strip()


TDLIB = b'synthetic tdlib source archive'
LOCK = {'tdlib_source': {'commit': 'd' * 40, 'bytes': len(TDLIB), 'sha256': sha(TDLIB), 'url': 'https://github.com/x'},
        'python': {'version': '3.14.8', 'pbs_tag': '20990101', 'url': 'https://github.com/y', 'bytes': 1,
                   'sha256': 'e' * 64},
        'native_notices': {'openssl_version': '3.6.4'}}


class Fakes:
    """Synthetic step outputs, and the order the orchestrator calls them in."""

    def __init__(self, test, service=None):
        self.test, self.calls, self.service = test, [], service
        self.minimal = None
        self.seed_commit = None
        self.seed_adhoc = False
        self.seed_call = None
        self.signature_refusal = False
        self.signature_call = None
        self.dmg_tamper = False
        self.notarized = None

    def steps(self):
        names = ('prepare', 'sign', 'matrix', 'repin', 'assemble', 'seed', 'signatures', 'notarize')
        steps = {name: self.recorded(name, getattr(self, name)) for name in names}
        steps.update(git_export=RB._sibling('prepare_payload').git_export, check_matrix=SP.check_matrix,
                     identity_kind=SP.identity_kind, load_lock=lambda data: LOCK, fetch=self.fetch)
        return steps

    def recorded(self, name, function):
        def call(*args):
            self.calls.append(name)
            return function(*args)
        return call

    def fetch(self, pin, cache, offline):
        self.test.assertEqual((pin, offline), (LOCK['tdlib_source'], True))
        return TDLIB

    @staticmethod
    def write(path, data, mode=0o400):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def prepare(self, output, native_directory, cache, checkout, offline):
        files = {'tdlib-lock.json': self.test.v1, 'provenance/components.json': b'{"wheels": []}\n',
                 'native/libtdjson.dylib': b'native', 'manifest/release.json': json.dumps({
                     'schema_version': 2, 'publisher': {'kind': 'adhoc', 'team_id': None}, 'native_lock_sha256': '0' * 64,
                     'publication': 'unreleased', 'source_completeness': 'incomplete'}).encode()}
        for name, data in files.items():
            self.write(output / name, data)
        return {'source_commit': self.test.commit, 'package_version': VERSION, 'tree_sha256': '1' * 64, 'files': 4,
                'macho': {'mach-o-64': 1}, 'macho_total': 1, 'manifest_sha256': '2' * 64, 'bytes': 9}

    def sign(self, payload, output, identity, team_id):
        for path in sorted(payload.rglob('*')):
            if path.is_file():
                self.write(output / path.relative_to(payload), path.read_bytes())
        return {'identity_kind': SP.identity_kind(identity, team_id), 'team_id': team_id, 'total': 1, 'signed': 1,
                'kept_pinned': 0, 'signing_sha256': '3' * 64, 'tree_sha256': '4' * 64,
                'interpreter_entitlements': [DLV] if identity == '-' else []}

    def matrix(self, signed, scratch, identity, team_id, version):
        self.test.assertEqual(version, '1.8.67')
        scratch.mkdir(mode=0o700)
        minimal = self.minimal if self.minimal is not None else ([DLV] if identity == '-' else [])
        return {'identity_kind': SP.identity_kind(identity, team_id), 'minimal': minimal, 'candidates': []}

    def repin(self, output, v1, unsigned, signed, team_id):
        self.test.assertEqual(v1, self.test.v1)
        data = json.dumps({'manifest_version': 2, 'team_id': team_id}).encode()
        self.write(output, data)
        return {'lock_sha256': sha(data), 'publisher': {'kind': 'adhoc' if team_id is None else 'developer-id',
                                                        'team_id': team_id}}

    def assemble(self, signed, output, service):
        self.test.assertEqual(service, self.service)
        manifest = json.loads((signed / 'manifest/release.json').read_bytes())
        lock = (signed / 'tdlib-lock.json').read_bytes()
        self.test.assertEqual(manifest['native_lock_sha256'], sha(lock))
        files = [{'path': 'launcher.py', 'sha256': '5' * 64, 'bytes': 1, 'executable': False}]
        if service is not None:
            files.append({'path': 'app-identity.json', 'sha256': '6' * 64, 'bytes': 1, 'executable': False})
        archive = b'synthetic archive ' + json.dumps(manifest['publisher']).encode()
        index = json.dumps({'release_version': VERSION, 'archive_sha256': sha(archive), 'files': files}).encode()
        self.write(output / 'release.tar.gz', archive)
        self.write(output / 'release-index.json', index)
        return SimpleNamespace(archive_sha256=sha(archive), index_sha256=sha(index), provenance_sha256='7' * 64,
                               files=len(files))

    def seed(self, checkout, output, identity, team_id):
        self.seed_call = (identity, team_id)
        data = b'synthetic seed'
        self.write(output / 'bootstrap-verifier', data, 0o500)
        value = {'source_commit': self.seed_commit or self.test.commit, 'binary_sha256': sha(data),
                 'binary_bytes': len(data), 'kind': 'developer-built-ad-hoc-seed', 'publisher_identity': False}
        if identity is not None and not self.seed_adhoc:
            value.update(kind='developer-id-seed', publisher_identity=True, team_id=team_id)
        self.write(output / 'provenance.json', json.dumps(value).encode())

    def signatures(self, kit, signed, index, kind, team_id):
        self.signature_call = (kit, signed, kind, team_id, sorted(p.name for p in kit.iterdir()), index['files'])
        if self.signature_refusal:
            raise RB.BuildError('signature_refused')
        return {'kind': kind, 'team_id': team_id, 'checked': 2, 'seed': 1, 'payload': 1, 'counted_by': 'magic'}

    def notarize(self, checkout, arguments, real):
        options = dict(zip(arguments[::2], arguments[1::2])) if not arguments[-1] == '--dry-run' else \
            dict(zip(arguments[:-1:2], arguments[1:-1:2]))
        self.notarized = (arguments, real)
        dmg = Path(options['--dmg'])
        self.test.assertEqual(oct(Path(options['--kit']).stat().st_mode & 0o777), oct(0o755))
        self.test.assertEqual(options['--volume-name'], product_identity.DMG_VOLUME_NAME)
        self.test.assertEqual(Path(options['--payload']).name, 'signed')
        dmg.write_bytes(b'synthetic dmg')
        digest = sha(b'other' if self.dmg_tamper else dmg.read_bytes())
        record = {'file': dmg.name, 'sha256': digest, 'contents': 'matches SHA256SUMS'}
        record.update({'team_id': options['--team-id']} if real else {'signature': 'adhoc'})
        Path(options['--evidence']).write_text(json.dumps({'mode': 'notarized' if real else 'dry-run', 'dmg': record}))


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        self.v1 = json.dumps({'manifest_version': 1, 'tdlib_version': '1.8.67'}).encode()
        files = {'packaging/bootstrap.sh': b'#!/bin/sh -p\n# synthetic\n', 'packaging/tdlib-lock.json': self.v1,
                 'packaging/payload-lock.json': b'{}', 'packaging/release_build.py': b'# copy\n',
                 'LICENSE': b'license text\n', 'NOTICE': b'notice text\n', 'docs/third-party-notices.md': b'# notices\n'}
        for name, data in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_bytes(data)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'synthetic')
        git(self.repo, 'tag', 'v1.2.3')
        self.commit = git(self.repo, 'rev-parse', 'HEAD')
        self.native, self.cache = self.root / 'native', self.root / 'cache'
        self.native.mkdir()
        self.cache.mkdir(mode=0o700)

    def build(self, name='out', fakes=None, **options):
        fakes = fakes or Fakes(self)
        options = {'dry_run': True, 'offline': True, **options}
        return RB.release_build(self.root / name, native_directory=self.native, cache=self.cache, checkout=self.repo,
                                steps=fakes.steps(), **options), fakes

    def test_dry_run_builds_every_asset_and_check_rederives_them(self):
        evidence, fakes = self.build(tag='v1.2.3')
        self.assertEqual(fakes.calls, ['prepare', 'sign', 'matrix', 'repin', 'assemble', 'seed', 'signatures',
                                       'notarize'])
        self.assertEqual(fakes.seed_call, (None, None))  # The ad hoc seed.
        kit_dir, signed, kind, team, listed, _ = fakes.signature_call
        self.assertEqual((kit_dir, signed, kind, team), (self.root / 'out/kit', self.root / 'out/work/signed', 'adhoc',
                                                         None))
        self.assertEqual(listed, sorted([*RB.KIT_DATA, *RB.KIT_EXECUTABLE, 'SHA256SUMS']))  # The finished kit.
        arguments, real = fakes.notarized
        self.assertEqual((arguments[-3:], real), (['--payload', str(signed), '--dry-run'], False))
        out = self.root / 'out'
        dmg = f'unofficial-telegram-mcp-{VERSION}-macos-arm64.dmg'
        self.assertEqual(sorted(evidence['kit']), sorted([*RB.KIT_DATA, *RB.KIT_EXECUTABLE, 'SHA256SUMS']))
        self.assertEqual(sorted(evidence['assets']), sorted([*RB.CURL_PATH, 'SHA256SUMS', 'release-provenance.json',
                                                             dmg, f'corresponding-source-{VERSION}.tar.gz']))
        self.assertEqual((evidence['mode'], evidence['source_commit'], evidence['tag']), ('adhoc-dry-run', self.commit,
                                                                                         'v1.2.3'))
        self.assertEqual(json.loads((out / 'release-build.json').read_bytes()), evidence)
        for group in ('kit', 'assets'):
            for name, record in evidence[group].items():
                self.assertEqual((sha((out / group / name).read_bytes()), (out / group / name).stat().st_size),
                                 (record['sha256'], record['bytes']))
        pins = json.loads((out / 'kit/release-pins.json').read_bytes())
        self.assertEqual(pins['seed'], {'file': 'bootstrap-verifier', 'sha256': sha(b'synthetic seed'), 'bytes': 14})
        self.assertEqual((pins['publisher'], pins['team_id'], pins['publication']), ('adhoc', None, 'unreleased'))
        self.assertEqual(set(pins), RB.PIN_KEYS)
        command = (out / 'kit/Install.command').read_text()
        self.assertIn(f"install_index_sha256={pins['index']['sha256']}\n", command)
        self.assertIn("install_seed_team=''\n", command)
        self.assertEqual((out / 'kit/Install.command').stat().st_mode & 0o777, 0o555)
        self.assertEqual((out / 'kit/bootstrap.sh').read_bytes(), b'#!/bin/sh -p\n# synthetic\n')
        self.assertIn(b'Ad hoc test build', (out / 'kit/README.txt').read_bytes())
        provenance = json.loads((out / 'assets/release-provenance.json').read_bytes())
        self.assertEqual(provenance['bundle']['app_identity'], {'source': 'none', 'sha256': None})
        self.assertEqual(provenance['signing']['interpreter_entitlements'], [DLV])
        self.assertEqual(provenance['native_signing'], RB.NATIVE_SIGNING['adhoc'])
        self.assertEqual(provenance['native_signing']['policy'], SP.NATIVE_POLICY['adhoc'])
        self.assertEqual(provenance['kit_signatures']['kind'], 'adhoc')
        self.assertEqual(provenance['seed']['provenance']['kind'], 'developer-built-ad-hoc-seed')
        self.assertEqual(provenance['source_completeness'], 'incomplete')
        self.assertEqual(json.loads((out / 'work/signed/manifest/release.json').read_bytes())['publisher'],
                         {'kind': 'adhoc', 'team_id': None})
        self.assertEqual(RB.check(out, self.repo)['status'], 'verified')
        self.assertFalse((out / 'work/matrix').exists())
        self.assertEqual(stat_mode(out), 0o700)

    def test_corresponding_source_is_deterministic_and_inventoried(self):
        first, _ = self.build('one')
        second, _ = self.build('two')
        name = f'corresponding-source-{VERSION}.tar.gz'
        self.assertEqual(first['assets'][name], second['assets'][name])
        self.assertEqual(first['assets']['release.tar.gz'], second['assets']['release.tar.gz'])
        with tarfile.open(self.root / 'one/assets' / name) as tar:
            members = {m.name: m for m in tar.getmembers()}
            prefix = f'corresponding-source-{VERSION}/'
            self.assertIn(prefix + 'project/LICENSE', members)
            self.assertEqual(tar.extractfile(members[prefix + f'tdlib/td-{"d" * 40}.tar.gz']).read(), TDLIB)
            inventory = json.loads(tar.extractfile(members[prefix + 'INVENTORY.json']).read())
            self.assertTrue(all(m.uid == 0 and m.mtime == 0 and m.uname == '' for m in members.values()))
        self.assertEqual(inventory['source_completeness'], 'incomplete')
        self.assertEqual(inventory['source_commit'], self.commit)
        self.assertTrue(inventory['missing'])
        with gzip.open(self.root / 'one/assets' / name) as stream:
            self.assertTrue(stream.read(1))

    def test_developer_id_mode_notarizes_with_the_profile_and_pins_the_team(self):
        evidence, fakes = self.build(dry_run=False, identity=IDENTITY, team_id=TEAM, notary_profile='release-notary')
        arguments, real = fakes.notarized
        self.assertTrue(real)
        self.assertEqual(arguments[-8:], ['--payload', str(self.root / 'out/work/signed'), '--identity', IDENTITY,
                                          '--team-id', TEAM, '--profile', 'release-notary'])
        self.assertEqual(fakes.seed_call, (IDENTITY, TEAM))  # The seed is signed with the payload's identity.
        self.assertEqual(fakes.signature_call[2:4], ('developer-id', TEAM))
        self.assertEqual(fakes.calls.index('signatures') + 1, fakes.calls.index('notarize'))
        self.assertEqual(evidence['mode'], 'developer-id')
        pins = json.loads((self.root / 'out/kit/release-pins.json').read_bytes())
        self.assertEqual((pins['publisher'], pins['team_id']), ('developer-id', TEAM))
        self.assertIn(f"install_seed_team='{TEAM}'\n", (self.root / 'out/kit/Install.command').read_text())
        provenance = json.loads((self.root / 'out/assets/release-provenance.json').read_bytes())
        self.assertEqual(provenance['native_signing'], RB.NATIVE_SIGNING['developer-id'])
        self.assertEqual(provenance['native_signing']['policy'], SP.NATIVE_POLICY['developer-id'])
        self.assertEqual((provenance['seed']['provenance']['kind'], provenance['seed']['provenance']['team_id']),
                         ('developer-id-seed', TEAM))
        self.assertEqual(json.loads((self.root / 'out/work/signed/manifest/release.json').read_bytes())['publisher'],
                         {'kind': 'developer-id', 'team_id': TEAM})
        self.assertEqual(RB.check(self.root / 'out', self.repo)['mode'], 'developer-id')

    def test_named_identity_service_is_recorded_by_fingerprint_only(self):
        fakes = Fakes(self, service=SERVICE)
        self.build(fakes=fakes, app_identity_service=SERVICE)
        provenance = json.loads((self.root / 'out/assets/release-provenance.json').read_bytes())
        self.assertEqual(provenance['bundle']['app_identity'], {'source': 'named-keychain-service', 'sha256': '6' * 64})
        for path in (self.root / 'out').rglob('*'):
            if path.is_file():
                self.assertNotIn(SERVICE.encode(), path.read_bytes())

    def test_check_refuses_any_change_on_disk_or_a_moved_head(self):
        self.build()
        out = self.root / 'out'
        sums = out / 'assets/SHA256SUMS'
        original = sums.read_bytes()
        sums.chmod(0o644)
        sums.write_bytes(original + b'0' * 64 + b'  extra\n')
        with self.assertRaises(RB.BuildError) as caught:
            RB.check(out, self.repo)
        self.assertEqual(str(caught.exception), 'evidence_mismatch')
        sums.write_bytes(original)
        evidence = out / 'release-build.json'
        value = json.loads(evidence.read_bytes())
        evidence.chmod(0o600)
        evidence.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
        with self.assertRaises(RB.BuildError) as caught:
            RB.check(out, self.repo)
        self.assertEqual(str(caught.exception), 'evidence_invalid')
        evidence.write_bytes(RB.canonical(value))
        self.assertEqual(RB.check(out, self.repo)['status'], 'verified')
        (self.repo / 'LICENSE').write_text('changed\n')
        git(self.repo, 'commit', '-q', '-am', 'moved')
        with self.assertRaises(RB.BuildError) as caught:
            RB.check(out, self.repo)
        self.assertEqual(str(caught.exception), 'evidence_mismatch')

    def test_refusals(self):
        def refused(code, name, fakes=None, **options):
            with self.subTest(code=code, name=name), self.assertRaises(RB.BuildError) as caught:
                self.build(name, fakes, **options)
            self.assertEqual(str(caught.exception), code)
            self.assertIsNone(caught.exception.__cause__)
        refused('usage', 'a', identity=IDENTITY)
        refused('usage', 'b', dry_run=False, identity=IDENTITY, team_id=TEAM)
        refused('usage', 'c', dry_run=False, identity=IDENTITY, team_id='ZYXWV98765', notary_profile='p')
        refused('usage', 'd', app_identity_service='com.x/../y')
        refused('tag_mismatch', 'e', tag='v9.9.9')
        refused('usage', 'f', tag='latest')
        fakes = Fakes(self)
        fakes.minimal = []
        refused('entitlements_unproven', 'g', fakes)
        fakes = Fakes(self)
        fakes.seed_commit = 'f' * 40
        refused('seed_failed', 'h', fakes)
        fakes = Fakes(self)
        fakes.dmg_tamper = True
        refused('notarize_failed', 'i', fakes)
        real = dict(dry_run=False, identity=IDENTITY, team_id=TEAM, notary_profile='release-notary')
        fakes = Fakes(self)
        fakes.seed_adhoc = True  # A real build whose seed came out ad hoc.
        refused('seed_unsigned', 'm', fakes, **real)
        self.assertIsNone(fakes.notarized)
        fakes = Fakes(self)
        fakes.signature_refusal = True
        refused('signature_refused', 'n', fakes, **real)
        self.assertIsNone(fakes.notarized)
        fakes = Fakes(self)
        fakes.signature_refusal = True
        refused('signature_refused', 'o', fakes)
        self.assertIsNone(fakes.notarized)
        fakes = Fakes(self)
        fakes.seed = lambda checkout, output, identity, team_id: Fakes.seed(fakes, checkout, output, IDENTITY, TEAM)
        refused('seed_failed', 'p', fakes)  # A dry run whose seed claims a publisher.
        failing = Fakes(self)

        def payload_error(*args):
            raise type('PayloadError', (ValueError,), {})('lock_invalid')
        failing.prepare = payload_error
        refused('prepare_failed', 'j', failing)
        (self.root / 'k').mkdir()
        refused('output_refused', 'k')
        with self.subTest(case='inside checkout'), self.assertRaises(RB.BuildError) as caught:
            RB.release_build(self.repo / 'out', native_directory=self.native, cache=self.cache, checkout=self.repo,
                             dry_run=True, steps=Fakes(self).steps())
        self.assertEqual(str(caught.exception), 'output_refused')
        with self.subTest(case='tmp'), self.assertRaises(RB.BuildError) as caught:
            RB.release_build(Path('/private/tmp/release-build-never'), native_directory=self.native, cache=self.cache,
                             checkout=self.repo, dry_run=True, steps=Fakes(self).steps())
        self.assertEqual(str(caught.exception), 'output_refused')
        (self.repo / 'NOTICE').write_text('dirty\n')
        refused('checkout_dirty', 'l')

    def forge(self, out, pins=None, command=None):
        """Rewrite the kit pins / Install.command with consistent sums and evidence, so check reaches them."""
        for name, data in (('release-pins.json', pins), ('Install.command', command)):
            if data is None:
                continue
            for group in ('kit', 'assets'):
                path = out / group / name
                if path.exists():
                    path.chmod(0o600)
                    path.write_bytes(data)
        for group in ('kit', 'assets'):
            (out / group / 'SHA256SUMS').chmod(0o600)
            (out / group / 'SHA256SUMS').write_bytes(RB.sums(out / group))
        value = json.loads((out / 'release-build.json').read_bytes())
        evidence = RB.derive_evidence(out, commit=value['source_commit'], version=value['release_version'],
                                      tag=value['tag'], mode=value['mode'])
        (out / 'release-build.json').chmod(0o600)
        (out / 'release-build.json').write_bytes(RB.canonical(evidence))

    def test_check_maps_malformed_pins_and_kit_text_to_fixed_codes(self):
        self.build()
        out = self.root / 'out'
        good = json.loads((out / 'kit/release-pins.json').read_bytes())
        command = (out / 'kit/Install.command').read_bytes()
        cases = {'not json': (b'\xff\xfe pins', 'evidence_invalid'),
                 'not a dict': (b'[1, 2]\n', 'evidence_invalid'),
                 'missing seed': (RB.json_bytes({k: v for k, v in good.items() if k != 'seed'}), 'evidence_invalid'),
                 'seed not a dict': (RB.json_bytes({**good, 'seed': 'x'}), 'evidence_invalid'),
                 'seed without sha': (RB.json_bytes({**good, 'seed': {'bytes': 1}}), 'evidence_invalid'),
                 'ad hoc with a team': (RB.json_bytes({**good, 'team_id': TEAM}), 'evidence_mismatch'),
                 'ad hoc published': (RB.json_bytes({**good, 'publication': 'published'}), 'evidence_mismatch'),
                 'developer-id publisher': (RB.json_bytes({**good, 'publisher': 'developer-id'}), 'evidence_mismatch')}
        for label, (data, code) in cases.items():
            with self.subTest(label), self.assertRaises(RB.BuildError) as caught:
                self.forge(out, pins=data)
                RB.check(out, self.repo)
            self.assertEqual(str(caught.exception), code)
            self.assertIsNone(caught.exception.__cause__)
        self.forge(out, pins=RB.json_bytes(good))
        self.assertEqual(RB.check(out, self.repo)['status'], 'verified')
        for label, data in (('not ascii', command.replace(b'#!/bin/sh -p', b'#!/bin/sh -p \xe2\x80\x94')),
                            ('team literal', command.replace(b"install_seed_team=''", b"install_seed_team='X'"))):
            with self.subTest(label), self.assertRaises(RB.BuildError) as caught:
                self.forge(out, command=data)
                RB.check(out, self.repo)
            self.assertIn(str(caught.exception), ('evidence_invalid', 'evidence_mismatch'))
        self.forge(out, command=command)
        self.assertEqual(RB.check(out, self.repo)['status'], 'verified')
        sums = out / 'kit/SHA256SUMS'
        sums.chmod(0o600)
        sums.write_bytes(b'\xff\n')  # Not UTF-8, yet consistent with a re-derived release-build.json.
        value = RB.derive_evidence(out, commit=self.commit, version=VERSION, tag=None, mode='adhoc-dry-run')
        (out / 'release-build.json').write_bytes(RB.canonical(value))
        with self.assertRaises(RB.BuildError) as caught:
            RB.check(out, self.repo)
        self.assertEqual(str(caught.exception), 'evidence_invalid')
        (out / 'release-build.json').write_bytes(RB.canonical({**value, 'kit': []}))
        with self.assertRaises(RB.BuildError) as caught:
            RB.check(out, self.repo)
        self.assertEqual(str(caught.exception), 'evidence_invalid')

    def test_command_line_refusals_echo_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = RB.main(['build', '--output', '/x', '--secret-like', 'Hunter2'])
        self.assertEqual((code, json.loads(err.getvalue())), (2, {'status': 'refused', 'code': 'usage'}))
        self.assertNotIn('Hunter2', out.getvalue() + err.getvalue())


def stat_mode(path):
    return path.stat().st_mode & 0o777


def thin_macho(filetype):
    return b'\xcf\xfa\xed\xfe' + (0x0100000c).to_bytes(4, 'little') + bytes(4) + filetype.to_bytes(4, 'little') + bytes(48)


DEVELOPER_ID_TEXT = f'''Executable=/synthetic
Identifier=io.example.synthetic
Format=Mach-O thin (arm64)
CodeDirectory v=20500 size=100 flags=0x10000(runtime) hashes=1+7 location=embedded
Signature size=9000
Authority=Developer ID Application: Synthetic Example ({TEAM})
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=Oct 21, 2026 at 12:00:00
Info.plist=not bound
TeamIdentifier={TEAM}
Runtime Version=26.0.0
'''
AD_HOC_TEXT = '''Executable=/synthetic
Identifier=synthetic
CodeDirectory v=20400 size=100 flags=0x2(adhoc) hashes=1+7 location=embedded
Signature=adhoc
Info.plist=not bound
TeamIdentifier=not set
'''


class KitSignatureTests(unittest.TestCase):
    """kit_signatures: every Mach-O by magic, bound to the index, then codesign's verdict and display."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.kit, self.signed = self.root / 'kit', self.root / 'signed'
        self.kit.mkdir(mode=0o755)
        for name, data, mode in (('bootstrap-verifier', thin_macho(2), 0o555), ('Install.command', b'#!/bin/sh\n', 0o555),
                                 ('release-index.json', b'{}', 0o444)):
            (self.kit / name).write_bytes(data)
            (self.kit / name).chmod(mode)
        files = {'python/bin/python3': (thin_macho(2), 0o500), 'native/libtdjson.dylib': (thin_macho(6), 0o400),
                 'app/module.py': (b'import os\n', 0o400)}
        for name, (data, mode) in files.items():
            Fakes.write(self.signed / name, data, mode)
        self.index = {'files': [{'path': n, 'sha256': sha(d)} for n, (d, _) in files.items()]}
        self.calls, self.text, self.exit = [], {}, {}

    def runner(self, argv, **options):
        self.calls.append(argv)
        self.assertEqual(options['env'], {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
        path, verb = argv[-1], argv[1]
        text = self.text.get(path, self.default)
        return SimpleNamespace(returncode=self.exit.get((path, verb), 0), stdout=b'',
                               stderr=text.encode() if verb == '-d' else b'')

    def check(self, kind='developer-id', team=TEAM):
        return RB.kit_signatures(self.kit, self.signed, self.index, kind, team, runner=self.runner)

    def refused(self, kind='developer-id', team=TEAM):
        with self.assertRaises(RB.BuildError) as caught:
            self.check(kind, team)
        self.assertEqual(str(caught.exception), 'signature_refused')

    def test_developer_id_checks_the_seed_and_every_payload_macho_with_the_team_requirement(self):
        self.default = DEVELOPER_ID_TEXT
        summary = self.check()
        self.assertEqual(summary, {'kind': 'developer-id', 'team_id': TEAM, 'checked': 3, 'seed': 1, 'payload': 2,
                                   'counted_by': 'magic'})
        paths = [str(self.kit / 'bootstrap-verifier'), str(self.signed / 'native/libtdjson.dylib'),
                 str(self.signed / 'python/bin/python3')]
        expected = []
        for path in paths:
            expected += [['/usr/bin/codesign', '--verify', '--strict', '-R', designated_requirement(TEAM), path],
                         ['/usr/bin/codesign', '-d', '--verbose=4', path]]
        self.assertEqual(self.calls, expected)

    def test_any_weaker_signature_on_any_macho_refuses(self):
        self.default = DEVELOPER_ID_TEXT
        weaker = {'unauthenticated time': DEVELOPER_ID_TEXT.replace('Timestamp=', 'Signed Time='),
                  'no time': DEVELOPER_ID_TEXT.replace('Timestamp=Oct 21, 2026 at 12:00:00\n', ''),
                  'other team': DEVELOPER_ID_TEXT.replace(f'TeamIdentifier={TEAM}', 'TeamIdentifier=ZYXWV98765'),
                  'other leaf team': DEVELOPER_ID_TEXT.replace(f'Synthetic Example ({TEAM})', 'Other (ZYXWV98765)'),
                  'not developer id': DEVELOPER_ID_TEXT.replace('Developer ID Application:', 'Apple Development:'),
                  'no runtime': DEVELOPER_ID_TEXT.replace('flags=0x10000(runtime)', 'flags=0x0(none)'),
                  'ad hoc': AD_HOC_TEXT}
        for target in (self.kit / 'bootstrap-verifier', self.signed / 'native/libtdjson.dylib'):
            for label, text in weaker.items():
                with self.subTest(target=target.name, label=label):
                    self.text = {str(target): text}
                    self.refused()
            for verb in ('--verify', '-d'):
                with self.subTest(target=target.name, failing=verb):
                    self.text, self.exit = {}, {(str(target), verb): 1}
                    self.refused()
            self.exit = {}

    def test_ad_hoc_mode_requires_ad_hoc_signatures_without_a_requirement(self):
        self.default = AD_HOC_TEXT
        self.assertEqual(self.check('adhoc', None)['checked'], 3)
        self.assertFalse([argv for argv in self.calls if '-R' in argv])
        self.default = DEVELOPER_ID_TEXT
        self.refused('adhoc', None)

    def test_the_inventory_is_bound_to_the_kit_and_the_index(self):
        self.default = DEVELOPER_ID_TEXT
        self.index['files'][1]['sha256'] = 'f' * 64  # native/libtdjson.dylib differs from what the archive holds
        self.refused()
        self.setUp()
        self.default = DEVELOPER_ID_TEXT
        (self.kit / 'Install.command').chmod(0o755)
        (self.kit / 'Install.command').write_bytes(thin_macho(2))  # a second Mach-O at the kit's top level
        self.refused()
        self.setUp()
        self.default = DEVELOPER_ID_TEXT
        (self.kit / 'bootstrap-verifier').chmod(0o755)
        (self.kit / 'bootstrap-verifier').write_bytes(b'#!/bin/sh\n')  # the seed is not a Mach-O
        self.refused()
        self.setUp()
        self.default = DEVELOPER_ID_TEXT
        (self.kit / 'link').symlink_to(self.kit / 'release-index.json')
        self.refused()
        self.assertEqual(self.calls, [])

    def test_the_stock_codesign_accepts_ad_hoc_and_refuses_it_as_developer_id(self):
        for path in (self.kit / 'bootstrap-verifier', self.signed / 'python/bin/python3',
                     self.signed / 'native/libtdjson.dylib'):
            path.chmod(0o700)
            path.unlink()
            shutil.copyfile('/usr/bin/true', path)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True, capture_output=True,
                           timeout=60)
            path.chmod(0o500)
        self.index = {'files': [{'path': p, 'sha256': sha((self.signed / p).read_bytes())}
                                for p in ('python/bin/python3', 'native/libtdjson.dylib', 'app/module.py')]}
        self.assertEqual(RB.kit_signatures(self.kit, self.signed, self.index, 'adhoc', None)['checked'], 3)
        with self.assertRaises(RB.BuildError) as caught:
            RB.kit_signatures(self.kit, self.signed, self.index, 'developer-id', TEAM)
        self.assertEqual(str(caught.exception), 'signature_refused')


if __name__ == '__main__':
    unittest.main()
