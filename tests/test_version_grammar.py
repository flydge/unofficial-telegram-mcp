"""One release version grammar at every site (tests/version_grammar.py holds the one table).

The Python sites are called as they run. The native seed is the real packaging/bootstrap_verifier.c, compiled
here with the release build's compiler flags and run as `prepare` against an archive and index that differ only
in release_version. bin/install is covered in test_plugin_install.
"""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from telegram_search_mcp import __version__, bundle_install
from telegram_search_mcp.release_archive import ReleaseArchiveError, load_archive_index
from version_grammar import BAD, GOOD, tag_admitted

ROOT = Path(__file__).resolve().parents[1]
SEED_SOURCE = ROOT / 'packaging/bootstrap_verifier.c'
BUILD_SCRIPT = ROOT / 'packaging/build_bootstrap_seed.sh'
DATA = b'checked'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def index_bytes(version, archive=None):
    data = archive if archive is not None else b'0'
    return json.dumps({'schema_version': 1, 'target': 'macos-arm64', 'release_version': version,
                       'archive_bytes': len(data), 'archive_sha256': hashlib.sha256(data).hexdigest(),
                       'unpacked_bytes': len(DATA),
                       'files': [{'path': 'safe/value.txt', 'bytes': len(DATA),
                                  'sha256': hashlib.sha256(DATA).hexdigest(), 'executable': False}]},
                      ensure_ascii=False).encode()


class PythonSitesTests(unittest.TestCase):
    """Every Python site admits exactly the shared table."""

    @classmethod
    def setUpClass(cls):
        cls.gate = load('release_gate_for_versions', 'release_gate.py')
        cls.build = load('release_build_for_versions', 'release_build.py')
        cls.pins = load('release_pins_for_versions', 'release_pins.py')
        cls.payload = load('prepare_payload_for_versions', 'prepare_payload.py')

    def sites(self):
        def archive_index(version):
            try:
                return load_archive_index(index_bytes(version)).release_version == version
            except ReleaseArchiveError:
                return False

        def release_pins(version):
            try:
                self.pins.encode(version=version, publication='unreleased', team_id='',
                                 assets={name: ('0' * 64, 0) for name, _, _ in self.pins.ASSETS})
                return True
            except self.pins.PinsError:
                return False

        def installer_order(version):
            try:
                bundle_install._version_key(version)
                return True
            except bundle_install.InstallError as error:
                self.assertEqual(str(error), 'downgrade_refused')
                return False

        def gate_project_version(version):
            return self.gate.VERSION.findall(f'__version__ = "{version}"\n') == [version]

        template = (ROOT / 'packaging/launcher.py').read_bytes()

        def launcher(version):
            try:
                self.payload.generate_launcher(template, version)
                return True
            except self.payload.PayloadError as error:
                self.assertEqual(str(error), 'manifest_failed')
                return False

        return {'archive index': archive_index, 'release pins': release_pins, 'installer order': installer_order,
                'gate project version': gate_project_version, 'launcher generator': launcher}

    def test_every_version_site_admits_the_good_versions_and_refuses_the_bad(self):
        for name, admits in self.sites().items():
            with self.subTest(site=name, version='__version__'):
                self.assertTrue(admits(__version__))
            for version in GOOD:
                with self.subTest(site=name, version=version):
                    self.assertTrue(admits(version))
            for version in BAD:
                with self.subTest(site=name, version=version):
                    self.assertFalse(admits(version))

    def test_a_release_tag_is_v_and_a_release_or_rc_version(self):
        tags = {'gate': self.gate.TAG, 'build': self.build.TAG}
        for name, pattern in tags.items():
            for version in GOOD + BAD:
                with self.subTest(site=name, version=version):
                    self.assertEqual(pattern.fullmatch('v' + version) is not None, tag_admitted(version))
            for tag in ('0.53.0', 'V0.53.0', 'v0.53.0rc01', 'v00.53.0', 'v0.53.0.dev0', 'v0.53.0a1', 'v0.53.0b1'):
                with self.subTest(site=name, tag=tag):
                    self.assertIsNone(pattern.fullmatch(tag))

    def test_the_installer_orders_every_admitted_version_as_pep_440(self):
        ordered = ['0.52.9', '0.53.0.dev0', '0.53.0.dev1', '0.53.0a1.dev0', '0.53.0a1', '0.53.0b2', '0.53.0rc1.dev3',
                   '0.53.0rc1', '0.53.0rc2', '0.53.0', '0.53.1', '1.0.0']
        self.assertEqual(sorted(reversed(ordered), key=bundle_install._version_key), ordered)


class SeedVersionTests(unittest.TestCase):
    """The real native seed admits exactly the shared table."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='tg-seed-versions-')).resolve()
        os.chmod(cls.tmp, 0o700)
        script = BUILD_SCRIPT.read_text()
        # The release build's own compiler line, so this binary is the one the kit ships, short of signing.
        flags = re.search(r'/usr/bin/clang (-target .*?) \\\n\s*(-Wno-[^\n]*?) \\\n', script)
        assert flags is not None
        cls.seed = cls.tmp / 'bootstrap-verifier'
        subprocess.run(['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL=C', '/usr/bin/clang',
                        *flags.group(1).split(), *flags.group(2).split(), str(SEED_SOURCE), '-lz',
                        '-o', str(cls.seed)], check=True, capture_output=True, timeout=300)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            info = tarfile.TarInfo('safe/value.txt')
            info.size, info.mode, info.uid, info.gid, info.mtime = len(DATA), 0o400, 0, 0, 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(DATA))
        cls.archive = cls.tmp / 'release.tar.gz'
        cls.archive.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
        cls.archive.chmod(0o400)
        cls.runs = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def prepare(self, version):
        type(self).runs += 1
        run = self.tmp / f'run-{self.runs}'
        run.mkdir(mode=0o700)
        index = run / 'release-index.json'
        data = index_bytes(version, self.archive.read_bytes())
        index.write_bytes(data)
        index.chmod(0o400)
        destination = run / 'release'
        done = subprocess.run(['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'LC_ALL=C', str(self.seed), 'prepare',
                               '--index', str(index), '--trusted-index-sha256', hashlib.sha256(data).hexdigest(),
                               '--archive', str(self.archive), '--destination', str(destination), '--json'],
                              capture_output=True, text=True, timeout=60)
        return done, destination

    def test_the_compiled_seed_prepares_every_good_version_and_refuses_every_bad_one(self):
        for version in GOOD:
            with self.subTest(version=version):
                done, destination = self.prepare(version)
                self.assertEqual((done.returncode, done.stderr), (0, ''))
                self.assertTrue(json.loads(done.stdout)['verified'])
                self.assertEqual((destination / 'safe/value.txt').read_bytes(), DATA)
        for version in BAD:
            with self.subTest(version=version):
                done, destination = self.prepare(version)
                self.assertEqual((done.returncode, done.stdout), (1, ''))
                self.assertEqual(json.loads(done.stderr),
                                 {'schema_version': 1, 'error_code': 'bootstrap_verification_failed'})
                self.assertFalse(destination.exists())

    def test_the_compiled_seed_prepares_an_index_of_this_package_version(self):
        done, destination = self.prepare(__version__)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual((destination / 'safe/value.txt').read_bytes(), DATA)

    def test_the_release_builds_seed_check_runs_the_kits_seed_on_the_kits_index(self):
        build = load('release_build_for_seed', 'release_build.py')
        step = build._default_steps()['seed_prepare']
        for version, admitted in ((__version__, True), ('0.53.0rc2', True), ('0.53', False), ('0.53.0rc01', False)):
            with self.subTest(version=version):
                type(self).runs += 1
                kit = self.tmp / f'kit-{self.runs}'
                kit.mkdir(mode=0o700)
                shutil.copy2(self.seed, kit / 'bootstrap-verifier')
                shutil.copy2(self.archive, kit / 'release.tar.gz')
                (kit / 'release-index.json').write_bytes(index_bytes(version, self.archive.read_bytes()))
                scratch = kit.parent / f'{kit.name}-scratch'
                if admitted:
                    step(kit, scratch)
                else:
                    with self.assertRaises(build.BuildError) as caught:
                        step(kit, scratch)
                    self.assertEqual(str(caught.exception), 'kit_failed')
                self.assertEqual(scratch.exists(), not admitted)


if __name__ == '__main__':
    unittest.main()
