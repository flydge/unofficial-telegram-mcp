"""prepare_payload over synthetic pinned inputs: build, prune, reproducibility, fetch and refusals.

Synthetic PBS/TDLib archives and wheels, a throwaway git checkout holding this package's real
sources, and inert natives. The manifest probe imports the exported package (real contract).
No network, Keychain or real runtime.
"""
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
import zipfile
from unittest import mock

from bundle_payload import inert_natives, load_assembler, synthetic_pin, v1_lock, v2_lock
import telegram_search_mcp
from telegram_search_mcp import __version__, native_runtime
from telegram_search_mcp.release_archive import load_archive_index

ROOT = Path(__file__).parents[1]
COMMIT = native_runtime.COMMIT
SCHEMA = b'synthetic td_api.tl\n'


def load():
    spec = importlib.util.spec_from_file_location('prepare_payload_under_test', ROOT / 'packaging/prepare_payload.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PP = load()
MACHO64 = b'\xcf\xfa\xed\xfe' + b'\0' * 60
UNIVERSAL = b'\xca\xfe\xba\xbe' + (1).to_bytes(4, 'big') + b'\0' * 56


def tar_gz(members):
    """members: name -> bytes | ('link', target) | ('dir',) | (bytes, mode)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.PAX_FORMAT) as tar:
        for name, value in members.items():
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if isinstance(value, tuple) and value[0] == 'link':
                info.type, info.linkname = tarfile.SYMTYPE, value[1]
                tar.addfile(info)
            elif isinstance(value, tuple) and value[0] == 'dir':
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                data, mode = value if isinstance(value, tuple) else (value, 0o644)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def pbs(**changes):
    members = {
        'python/': ('dir',), 'python/bin/': ('dir',),
        'python/bin/python3.14': (MACHO64 + b'python', 0o755), 'python/bin/python3': ('link', 'python3.14'),
        'python/bin/python': ('link', 'python3.14'), 'python/bin/pip3': (b'#!pip', 0o755),
        'python/bin/idle3.14': (b'#!idle', 0o755),
        'python/lib/libpython3.14.dylib': (MACHO64 + b'libpython', 0o755),
        'python/lib/python3.14/os.py': b'# os\n', 'python/lib/python3.14/LICENSE.txt': b'PSF license\n',
        'python/lib/python3.14/__pycache__/os.cpython-314.pyc': b'bytecode',
        'python/lib/python3.14/tkinter/__init__.py': b'# tk\n', 'python/lib/python3.14/idlelib/idle.py': b'# idle\n',
        'python/lib/python3.14/turtledemo/demo.py': b'# turtle\n', 'python/lib/python3.14/ensurepip/__init__.py': b'# pip\n',
        'python/lib/python3.14/site-packages/pip/__init__.py': b'# pip\n',
        'python/lib/python3.14/site-packages/pip-26.0.dist-info/RECORD': b'',
        'python/lib/python3.14/site-packages/README.txt': b'site\n',
        'python/lib/python3.14/lib-dynload/_tkinter.cpython-314-darwin.so': MACHO64 + b'tk',
        'python/lib/python3.14/lib-dynload/_json.cpython-314-darwin.so': (MACHO64 + b'json', 0o755),
        'python/lib/tcl9.0/init.tcl': b'tcl', 'python/lib/tk9.0/tk.tcl': b'tk', 'python/lib/itcl4.3.8/x': b'i',
        'python/lib/thread3.0.6/x': b't', 'python/lib/tcl9/x': b't', 'python/lib/libtcl9.0.dylib': MACHO64 + b'tcl',
        'python/lib/libtcl9tk9.0.dylib': MACHO64 + b'tk', 'python/lib/pkgconfig/python3.pc': ('link', 'python-3.14.pc'),
        'python/include/python3.14/Python.h': b'/* h */', 'python/share/man/man1/python3.14.1': b'man',
    }
    members.update(changes)
    return tar_gz({k: v for k, v in members.items() if v is not None})


def tdlib(schema=SCHEMA, extra=None):
    prefix = f'td-{COMMIT}/'
    members = {prefix: ('dir',), prefix + 'LICENSE_1_0.txt': b'Boost Software License\n',
               prefix + 'CMakeLists.txt': b'project(TDLib)\n', prefix + 'td/generate/scheme/td_api.tl': schema}
    members.update(extra or {})
    return tar_gz({k: v for k, v in members.items() if v is not None})


def provenance(record):
    """prepare_native's provenance file over a synthetic receipt, path redacted as it does."""
    redacted = json.loads(json.dumps(record))
    if 'path' in redacted['source']:
        redacted['source']['path'] = PP.REDACTED_PATH
    return (json.dumps({'input_sha256': hashlib.sha256(json.dumps(record).encode()).hexdigest(), 'record': redacted},
                       sort_keys=True) + '\n').encode()


def receipts(**tdlib_changes):
    """Synthetic TDLib and OpenSSL receipts that agree with bundle_payload.v1_lock's build fields."""
    tdlib_receipt = {'arch': 'arm64', 'compiler': 'clang', 'used_options': [],
                     'built_on': {'os_version': 'synthetic', 'xcode': 'synthetic', 'clt': 'synthetic'},
                     'runtime_dependencies': [{'full_name': 'openssl@3', 'version': '3.6.4'}],
                     'source': {'path': '/private-formula-cache/tdlib.rb', 'scm_revision': COMMIT,
                                'versions': {'stable': '1.8.0', 'head': 'HEAD-' + COMMIT[:7]}}}
    tdlib_receipt.update(tdlib_changes)
    openssl_receipt = {'arch': 'arm64', 'built_on': {}, 'source': {'path': '/private-api-cache/packages.json',
                                                                   'versions': {'stable': '3.6.4'}}}
    return {'tdlib-build.json': provenance(tdlib_receipt), 'openssl-build.json': provenance(openssl_receipt)}


def wheel(project, version, files, *, data=None, tamper=False, links=()):
    stem = f'{PP.normalize(project)}-{version}'
    members = dict(files)
    members[stem + '.dist-info/METADATA'] = f'Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n'.encode()
    members[stem + '.dist-info/WHEEL'] = b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    members[stem + '.dist-info/licenses/LICENSE'] = b'license for ' + project.encode()
    for name, value in (data or {}).items():
        members[stem + '.data/' + name] = value
    rows = [[name, PP.record_hash(value if isinstance(value, bytes) else value[0]),
             str(len(value if isinstance(value, bytes) else value[0]))] for name, value in members.items()]
    if tamper:
        rows[0][1] = PP.record_hash(b'other')
    rows.append([stem + '.dist-info/RECORD', '', ''])
    record = ''.join(','.join(r) + '\n' for r in rows).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, value in list(members.items()) + [(stem + '.dist-info/RECORD', record)]:
            data_bytes, mode = (value, 0o644) if isinstance(value, bytes) else value
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o120777 if name in links else (0o100000 | mode)) << 16
            archive.writestr(info, data_bytes)
    return buffer.getvalue()


def wheel_pin(project, version, data):
    filename = f'{PP.normalize(project)}-{version}-py3-none-any.whl'
    return {'project': project, 'version': version, 'filename': filename,
            'url': f'https://files.pythonhosted.org/packages/py3/{project[0]}/{project}/{filename}',
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class Inputs:
    """One synthetic input set: checkout (its HEAD carries the payload lock), native directory and a primed cache."""

    NOTICES = {name: ('openssl ' + name).encode() for name in ('LICENSE.txt', 'AUTHORS.md', 'README.md')}
    GIT_ENV = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}

    def __init__(self, root, *, python=None, source=None, wheels=None, prime=True, native_lock=None, provenance=None):
        self.root = root
        self.natives = inert_natives()
        self.row = synthetic_pin(self.natives, schema_sha256=hashlib.sha256(SCHEMA).hexdigest())
        self.native_lock = json.dumps(native_lock or v2_lock(v1_lock(self.natives), schema_sha256=self.row.schema_sha256),
                                      indent=2).encode()
        self.native = self.make_native(provenance or receipts())
        self.python = python if python is not None else pbs()
        self.source = source if source is not None else tdlib()
        self.wheels = wheels or {
            'app': [('segno', '1.6.6', wheel('segno', '1.6.6', {'segno/__init__.py': b'# qr\n'},
                                             data={'data/share/man/man1/segno.1': b'man page'})),
                    ('demo-native', '1.0', wheel('demo-native', '1.0', {'demo_native/__init__.py': b'',
                                                                         'demo_native/_core.so': (UNIVERSAL, 0o755)}))],
            'installer': [('tomlkit', '0.13.3', wheel('tomlkit', '0.13.3', {'tomlkit/__init__.py': b'# toml\n'}))]}
        self.cache = root / 'cache'
        self.cache.mkdir(mode=0o700)
        self.blobs = {}
        members = [m for m in tarfile.open(fileobj=io.BytesIO(self.source)).getmembers() if m.isfile()]
        lock = {'schema_version': 1, 'target': 'macos-arm64',
                'python': {'version': '3.14.8', 'pbs_tag': '20261003', **self.pin(
                    'https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython.tar.gz', self.python)},
                'tdlib_source': {'commit': COMMIT, 'files': len(members), 'unpacked_bytes': sum(m.size for m in members),
                                 **self.pin(f'https://codeload.github.com/tdlib/td/tar.gz/{COMMIT}', self.source)},
                'native_notices': {'openssl_version': '3.6.4', 'files': {
                    name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in self.NOTICES.items()}},
                'app': [], 'installer': []}
        for group, items in self.wheels.items():
            for project, version, data in items:
                pin = wheel_pin(project, version, data)
                self.blobs[pin['url']] = data
                lock[group].append(pin)
        self.checkout = self.make_checkout((json.dumps(lock, indent=2) + '\n').encode())
        self.lock = self.checkout / 'packaging/payload-lock.json'
        if prime:
            (self.cache / 'downloads').mkdir(mode=0o700)
            for data in self.blobs.values():
                (self.cache / 'downloads' / hashlib.sha256(data).hexdigest()).write_bytes(data)

    def pin(self, url, data):
        self.blobs[url] = data
        return {'url': url, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    def git(self, *args):
        return subprocess.run(['/usr/bin/git', '-C', str(self.checkout), '-c', 'user.name=synthetic',
                               '-c', 'user.email=synthetic@example.invalid', '-c', 'commit.gpgsign=false',
                               '-c', 'init.defaultBranch=main', *args],
                              check=True, capture_output=True, env=self.GIT_ENV, timeout=60).stdout

    def commit(self, changes):
        """Commit {path: bytes} into the checkout's HEAD."""
        for name, data in changes.items():
            (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / name).write_bytes(data)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'synthetic')

    def make_checkout(self, payload_lock):
        self.checkout = self.root / 'checkout'
        package = Path(telegram_search_mcp.__file__).parent
        for path in sorted(package.rglob('*.py')):
            if '__pycache__' not in path.parts:
                target = self.checkout / 'src/telegram_search_mcp' / path.relative_to(package)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        for name in ('pyproject.toml', 'LICENSE', 'NOTICE', 'packaging/launcher.py',
                     'skills/telegram-search/SKILL.md', 'skills/telegram-setup/SKILL.md'):
            (self.checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.checkout / name)
        self.git('init', '-q')
        self.commit({'packaging/tdlib-lock.json': self.native_lock, 'packaging/payload-lock.json': payload_lock})
        return self.checkout

    def make_native(self, provenance_files):
        native = self.root / 'native-out'
        for name, data in self.natives.items():
            (native / 'native').mkdir(parents=True, exist_ok=True)
            (native / 'native' / name).write_bytes(data)
        (native / 'provenance').mkdir()
        for name, data in provenance_files.items():
            (native / 'provenance' / name).write_bytes(data)
        (native / 'notices/openssl-3.6.4').mkdir(parents=True)
        for name, data in self.NOTICES.items():
            (native / 'notices/openssl-3.6.4' / name).write_bytes(data)
        return native

    def opener(self, served=None, fetched=None):
        served = served if served is not None else self.blobs

        class Response(io.BytesIO):
            def __init__(self, url, data):
                super().__init__(data)
                self.url = url

            def geturl(self):
                return self.url

        def open_url(url, timeout):
            assert timeout <= 120
            if fetched is not None:
                fetched.append(url)
            if url not in served:
                raise OSError('synthetic network refusal')
            return Response(url, served[url])
        return open_url

    def build(self, name='payload', **options):
        options.setdefault('offline', True)
        return PP.prepare_payload(self.root / name, native_directory=self.native, cache=self.cache,
                                  checkout=self.checkout, **options)


class PreparePayloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def inputs(self, **options):
        return Inputs(self.root, **options)

    def assemble(self, inputs, payload, name='output'):
        assembler = load_assembler('assembly_prepared_payload')
        with mock.patch.object(native_runtime, 'APPROVED_PINS', (inputs.row,)):
            return assembler.assemble_bundle(payload, self.root / name)

    def test_builds_a_pruned_payload_that_the_assembler_accepts(self):
        inputs = self.inputs()
        summary = inputs.build()
        payload = self.root / 'payload'
        files = {str(p.relative_to(payload)) for p in payload.rglob('*') if p.is_file()}
        self.assertEqual({f for f in files if f.startswith('python/bin/')}, {'python/bin/python3'})
        self.assertEqual((payload / 'python/bin/python3').read_bytes(), MACHO64 + b'python')
        self.assertEqual((payload / 'python/bin/python3').stat().st_mode & 0o777, 0o500)
        for pruned in ('tkinter', 'idlelib', 'turtledemo', 'ensurepip', 'site-packages/pip', '_tkinter', 'include/',
                       'share/', 'tcl9', 'tk9.0', 'itcl', 'thread3', 'libtcl', 'pkgconfig', '__pycache__', 'idle3'):
            self.assertFalse([f for f in files if f.startswith('python/') and pruned in f], pruned)
        self.assertIn('python/lib/python3.14/lib-dynload/_json.cpython-314-darwin.so', files)
        self.assertIn('python/lib/python3.14/site-packages/README.txt', files)
        manifest = json.loads((payload / 'manifest/release.json').read_bytes())
        self.assertEqual((manifest['schema_version'], manifest['publication'], manifest['source_completeness'],
                          manifest['publisher'], manifest['package_version']),
                         (2, 'unreleased', 'incomplete', {'kind': 'adhoc', 'team_id': None}, __version__))
        self.assertEqual(manifest['native_lock_sha256'], hashlib.sha256(inputs.native_lock).hexdigest())
        launcher = (payload / 'launcher.py').read_text()
        self.assertEqual(launcher_versions(launcher), [[__version__], [__version__]])
        self.assertEqual((payload / 'source/tdlib/td/generate/scheme/td_api.tl').read_bytes(), SCHEMA)
        self.assertEqual((payload / 'source/project/packaging/tdlib-lock.json').read_bytes(), inputs.native_lock)
        self.assertNotIn('app/share/man/man1/segno.1', files)
        components = json.loads((payload / 'provenance/components.json').read_bytes())
        segno = next(c for c in components['wheels'] if c['project'] == 'segno')
        self.assertEqual(segno['dropped_wheel_data'], ['segno-1.6.6.data/data/share/man/man1/segno.1'])
        self.assertIn('notices/app/segno-1.6.6.dist-info/licenses/LICENSE', files)
        self.assertIn('notices/python/LICENSE.txt', files)
        inventory = json.loads((payload / 'provenance/macho-inventory.json').read_bytes())
        self.assertEqual(inventory['counts'], {'mach-o-64': 3, 'universal': 1})
        self.assertEqual(summary['macho'], inventory['counts'])
        self.assertEqual({f['path'] for f in inventory['files']}, {
            'python/bin/python3', 'python/lib/libpython3.14.dylib',
            'python/lib/python3.14/lib-dynload/_json.cpython-314-darwin.so', 'app/demo_native/_core.so'})
        runtime = json.loads((payload / 'provenance/python-runtime.json').read_bytes())
        self.assertIn('include/', runtime['pruned'])
        dist = payload / f'app/telegram_search_mcp-{__version__}.dist-info'
        rows = [line.split(',') for line in (dist / 'RECORD').read_text().splitlines()]
        for name, digest, size in rows[:-1]:
            data = (payload / 'app' / name).read_bytes()
            self.assertEqual((digest, size), (PP.record_hash(data), str(len(data))))
        self.assertIn(f'Version: {__version__}', (dist / 'METADATA').read_text())
        modes = {p.stat().st_mode & 0o777 for p in payload.rglob('*')}
        self.assertLessEqual(modes, {0o700, 0o500, 0o400})
        result = self.assemble(inputs, payload)
        index = load_archive_index(result.index.read_bytes())
        self.assertEqual(index.release_version, __version__)
        self.assertEqual(summary['files'], len(index.files))

    def test_two_builds_give_identical_trees_archives_and_indexes(self):
        inputs = self.inputs()
        first, second = inputs.build('one'), inputs.build('two')
        self.assertEqual(first['tree_sha256'], second['tree_sha256'])
        one, two = self.root / 'one', self.root / 'two'
        listing = lambda root: sorted((str(p.relative_to(root)), p.stat().st_mode & 0o777, p.read_bytes() if p.is_file() else b'')
                                      for p in root.rglob('*'))
        self.assertEqual(listing(one), listing(two))
        a, b = self.assemble(inputs, one, 'bundle-one'), self.assemble(inputs, two, 'bundle-two')
        self.assertEqual((a.archive_sha256, a.index_sha256), (b.archive_sha256, b.index_sha256))

    def test_downloads_once_into_the_hash_addressed_cache_and_only_on_a_match(self):
        inputs = self.inputs(prime=False)
        with self.assertRaises(PP.PayloadError) as caught:
            inputs.build('offline')
        self.assertEqual(str(caught.exception), 'input_unavailable')
        fetched = []
        inputs.build('online', offline=False, opener=inputs.opener(fetched=fetched))
        self.assertEqual(sorted(fetched), sorted(inputs.blobs))
        cached = sorted(p.name for p in (inputs.cache / 'downloads').iterdir())
        self.assertEqual(cached, sorted(hashlib.sha256(d).hexdigest() for d in inputs.blobs.values()))
        fetched.clear()
        inputs.build('again', offline=False, opener=inputs.opener(fetched=fetched))
        self.assertEqual(fetched, [])

    def test_a_tampered_download_is_never_cached(self):
        inputs = self.inputs(prime=False)
        served = dict(inputs.blobs)
        url = next(u for u in served if 'segno' in u)
        served[url] = served[url][:-1] + b'X'
        with self.assertRaises(PP.PayloadError) as caught:
            inputs.build(offline=False, opener=inputs.opener(served))
        self.assertEqual(str(caught.exception), 'input_mismatch')
        self.assertNotIn(hashlib.sha256(inputs.blobs[url]).hexdigest(),
                         {p.name for p in (inputs.cache / 'downloads').iterdir()})
        self.assertFalse([p for p in (inputs.cache / 'downloads').iterdir() if p.name.endswith('.partial')])

    def test_a_truncated_download_is_a_fixed_code_and_leaves_no_partial(self):
        import http.client
        inputs = self.inputs(prime=False)
        plain = inputs.opener()

        def truncated(url, timeout):
            response = plain(url, timeout)
            def read(size=-1):
                raise http.client.IncompleteRead(b'partial')
            response.read = read
            return response
        with self.assertRaises(PP.PayloadError) as caught:
            inputs.build(offline=False, opener=truncated)
        self.assertEqual(str(caught.exception), 'input_unavailable')
        self.assertFalse([p for p in (inputs.cache / 'downloads').iterdir() if p.name.endswith('.partial')])

    def test_corrupt_or_unsupported_wheel_members_are_fixed_codes(self):
        good = wheel('segno', '1.6.6', {'segno/__init__.py': b'# qr\n' * 64})
        tomlkit = [('tomlkit', '0.13.3', wheel('tomlkit', '0.13.3', {'tomlkit/__init__.py': b''}))]

        def patch_first_entry(data, offset, value):
            # Patch the first central-directory entry (segno/__init__.py), which zipfile reads.
            data = bytearray(data)
            central = data.find(b'PK\x01\x02')
            data[central + offset:central + offset + 2] = value.to_bytes(2, 'little')
            return bytes(data)
        variants = {'encrypted': patch_first_entry(good, 8, 0x1), 'unknown compression': patch_first_entry(good, 10, 99)}
        for label, data in variants.items():
            with self.subTest(label):
                self.fresh()
                self.refused('wheel_refused', self.inputs(wheels={'app': [('segno', '1.6.6', data)], 'installer': tomlkit}))

    def refused(self, code, inputs=None, **options):
        inputs = inputs or self.inputs()
        with self.assertRaises(PP.PayloadError) as caught:
            inputs.build(**options)
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)

    def fresh(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_wheel_refusals(self):
        segno = {'segno/__init__.py': b'# qr\n'}
        tomlkit = [('tomlkit', '0.13.3', wheel('tomlkit', '0.13.3', {'tomlkit/__init__.py': b''}))]
        for label, data in {'record mismatch': wheel('segno', '1.6.6', segno, tamper=True),
                            'symlink member': wheel('segno', '1.6.6', {**segno, 'segno/link': b'/etc'}, links=('segno/link',)),
                            'traversal': wheel('segno', '1.6.6', {**segno, '../escape.py': b''}),
                            'unknown data scheme': wheel('segno', '1.6.6', segno, data={'other/x': b''}),
                            'file and directory of one name': wheel('segno', '1.6.6', {**segno, 'segno/x': b'',
                                                                                       'segno/x/y': b''})}.items():
            with self.subTest(label):
                self.fresh()
                self.refused('wheel_refused', self.inputs(wheels={'app': [('segno', '1.6.6', data)], 'installer': tomlkit}))

    def test_native_source_and_prune_refusals(self):
        inputs = self.inputs()
        (inputs.native / 'native/libssl.3.dylib').write_bytes(b'x' * 48)
        self.refused('native_mismatch', inputs)
        self.fresh()
        self.refused('source_refused', self.inputs(source=tdlib(schema=b'another schema\n')))
        self.fresh()
        self.refused('source_refused', self.inputs(source=tdlib(extra={f'td-{COMMIT}/link': ('link', '/etc/passwd')})))
        self.fresh()
        self.refused('prune_refused', self.inputs(python=pbs(**{'python/bin/python3.14': None})))
        self.fresh()
        self.refused('prune_refused', self.inputs(python=pbs(**{'python/lib/libunexpected.dylib': MACHO64})))
        self.fresh()
        self.refused('archive_refused', self.inputs(python=pbs(**{'python/lib/python3.14/odd': ('link', '/etc')})))

    def test_output_cache_and_lock_refusals(self):
        inputs = self.inputs()
        with self.assertRaises(PP.PayloadError) as caught:
            PP.prepare_payload(inputs.checkout / 'payload', native_directory=inputs.native, cache=inputs.cache,
                               checkout=inputs.checkout, offline=True)
        self.assertEqual(str(caught.exception), 'output_refused')
        shared = self.root / 'shared-cache'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        for cache in (Path('/private/tmp/tgmcp-cache-never-created'), Path('/tmp/tgmcp-cache-never-created'), shared):
            with self.subTest(cache=str(cache)), self.assertRaises(PP.PayloadError) as caught:
                PP.prepare_payload(self.root / 'p', native_directory=inputs.native, cache=cache,
                                   checkout=inputs.checkout, offline=True)
            self.assertEqual(str(caught.exception), 'cache_refused')
        self.assertFalse(Path('/private/tmp/tgmcp-cache-never-created').exists())
        self.assertFalse((self.root / 'p').exists())
        base = json.loads(inputs.lock.read_bytes())
        def variant(change):
            value = json.loads(json.dumps(base))
            change(value)
            return json.dumps(value).encode()
        notices = lambda v: v['native_notices']
        for label, data in {
                'plain http': variant(lambda v: v['python'].update(url=v['python']['url'].replace('https:', 'http:'))),
                'unknown host': variant(lambda v: v['app'][0].update(url='https://example.invalid/' + v['app'][0]['filename'])),
                'extra key': variant(lambda v: v.update(extra=1)),
                'filename mismatch': variant(lambda v: v['app'][0].update(filename='other-1.0-py3-none-any.whl')),
                'duplicate project': variant(lambda v: v['app'].append(v['app'][0])),
                'bad hash': variant(lambda v: v['python'].update(sha256='A' * 64)),
                'no notices': variant(lambda v: v.pop('native_notices')),
                'missing notice': variant(lambda v: notices(v)['files'].pop('README.md')),
                'extra notice': variant(lambda v: notices(v)['files'].update(NEWS={'bytes': 1, 'sha256': '0' * 64})),
                'notice hash': variant(lambda v: notices(v)['files']['LICENSE.txt'].update(sha256='A' * 64)),
                'notice size': variant(lambda v: notices(v)['files']['LICENSE.txt'].update(bytes=True)),
                'traversing openssl version': variant(lambda v: notices(v).update(openssl_version='../3.6.4')),
                'not json': b'{'}.items():
            with self.subTest(label), self.assertRaises(PP.PayloadError) as caught:
                PP.load_lock(data)
            self.assertEqual(str(caught.exception), 'lock_invalid')

    def test_only_heads_payload_lock_is_read(self):
        inputs = self.inputs()
        committed = inputs.lock.read_bytes()
        first = inputs.build('one')
        # An uncommitted working-tree edit of the lock changes nothing: valid, malformed or deleted.
        edited = json.loads(committed)
        edited['installer'][0]['sha256'] = '0' * 64
        for name, data in (('two', json.dumps(edited).encode()), ('three', b'{'), ('four', None)):
            with self.subTest(working_tree=name):
                if data is None:
                    inputs.lock.unlink()
                else:
                    inputs.lock.write_bytes(data)
                self.assertEqual(inputs.build(name)['tree_sha256'], first['tree_sha256'])
                components = json.loads((self.root / name / 'provenance/components.json').read_bytes())
                self.assertEqual(components['payload_lock_sha256'], hashlib.sha256(committed).hexdigest())
                self.assertEqual((self.root / name / 'source/project/packaging/payload-lock.json').read_bytes(), committed)
        inputs.git('checkout', '-q', '--', 'packaging/payload-lock.json')
        # A committed change is what counts.
        inputs.commit({'packaging/payload-lock.json': b'{'})
        self.refused('lock_invalid', inputs, name='five')
        inputs.git('rm', '-q', 'packaging/payload-lock.json')
        inputs.git('commit', '-q', '-m', 'no lock')
        self.refused('source_refused', inputs, name='six')

    def test_native_notices_and_provenance_must_match_their_pins(self):
        inputs = self.inputs()
        (inputs.native / 'notices/openssl-3.6.4/LICENSE.txt').write_bytes(b'another license')
        self.refused('native_mismatch', inputs)
        (inputs.native / 'notices/openssl-3.6.4/LICENSE.txt').unlink()
        self.refused('native_mismatch', inputs, name='missing')
        unredacted = json.loads(receipts()['tdlib-build.json'])
        unredacted['record']['source']['path'] = '/private-formula-cache/tdlib.rb'
        cases = {
            'other source commit': receipts(source={'scm_revision': '0' * 40, 'versions': {'head': 'HEAD-0000000'}}),
            'other build host': receipts(built_on={'os_version': 'macOS 26.5', 'xcode': 'synthetic', 'clt': 'synthetic'}),
            'other compiler': receipts(compiler='gcc'),
            'other options': receipts(used_options=['--HEAD']),
            'other openssl dependency': receipts(runtime_dependencies=[{'full_name': 'openssl@3', 'version': '3.6.3'}]),
            'other arch': receipts(arch='x86_64'),
            'unredacted path': {**receipts(), 'tdlib-build.json': (json.dumps(unredacted, sort_keys=True) + '\n').encode()},
            'not canonical': {**receipts(), 'openssl-build.json': receipts()['openssl-build.json'].rstrip(b'\n')},
            'record not an object': {**receipts(), 'openssl-build.json': b'{"input_sha256": "' + b'0' * 64
                                     + b'", "record": "synthetic"}\n'},
            'list of objects': {**receipts(), 'tdlib-build.json': b'[{}]\n'},
            'duplicate key': {**receipts(), 'tdlib-build.json': b'{"input_sha256": "x", "input_sha256": "y"}\n'},
        }
        for label, files in cases.items():
            with self.subTest(label):
                self.fresh()
                self.refused('native_mismatch', self.inputs(provenance=files))
        self.fresh()
        lock = v2_lock(v1_lock(inert_natives()), schema_sha256=hashlib.sha256(SCHEMA).hexdigest())
        lock['build']['openssl_version'] = '3.6.5'  # no notices pinned for it
        self.refused('native_mismatch', self.inputs(native_lock=lock))

    def test_escaping_lookups_refuse_with_fixed_codes(self):
        lock = v2_lock(v1_lock(inert_natives()), schema_sha256=hashlib.sha256(SCHEMA).hexdigest())
        del lock['build']['openssl_version']
        self.refused('native_mismatch', self.inputs(native_lock=lock))
        self.fresh()
        self.refused('source_refused', self.inputs(source=tdlib(schema=None)))
        self.fresh()
        self.refused('source_refused', self.inputs(source=tdlib(extra={f'td-{COMMIT}/LICENSE_1_0.txt': None})))
        project = (ROOT / 'pyproject.toml').read_text()
        for label, text in {'no description': re.sub(r'(?m)^description = .*\n', '', project, count=1),
                            'license file not committed': project.replace('license-files = [', 'license-files = ["ABSENT", ', 1),
                            'header injection': re.sub(r'(?m)^description = .*$', r'description = "x\\nName: other"', project,
                                                       count=1)}.items():
            with self.subTest(label):
                self.assertNotEqual(text, project)
                self.fresh()
                inputs = self.inputs()
                inputs.commit({'pyproject.toml': text.encode()})
                self.refused('source_refused', inputs)


def launcher_versions(text):
    return [site.findall(text) for site in PP.LAUNCHER_SITES]


class GitExportTests(unittest.TestCase):
    def test_exports_committed_blobs_despite_local_config_and_attributes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / 'repo'
        repo.mkdir()
        run = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True)
        run('init', '-q')
        run('config', 'user.email', 'noreply@users.noreply.github.com')
        run('config', 'user.name', 'Test')
        (repo / 'packaging').mkdir()
        committed = b'{"a": 1}\n'
        (repo / 'packaging' / 'payload-lock.json').write_bytes(committed)
        (repo / 'tool.sh').write_bytes(b'#!/bin/sh\n')
        (repo / 'tool.sh').chmod(0o755)
        run('add', '.')
        run('commit', '-qm', 'one')
        run('config', 'core.autocrlf', 'true')
        run('config', 'filter.x.smudge', 'sed s/1/2/')
        (repo / '.git' / 'info').mkdir(exist_ok=True)
        (repo / '.git' / 'info' / 'attributes').write_text('*.json filter=x text eol=crlf\n')
        commit, files = PP.git_export(repo)
        self.assertEqual(files['packaging/payload-lock.json'], (committed, False))
        self.assertEqual(files['tool.sh'], (b'#!/bin/sh\n', True))
        self.assertRegex(commit, r'^[0-9a-f]{40}$')

    def test_links_refuse(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / 'repo'
        repo.mkdir()
        run = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True)
        run('init', '-q')
        run('config', 'user.email', 'noreply@users.noreply.github.com')
        run('config', 'user.name', 'Test')
        (repo / 'link').symlink_to('/etc')
        run('add', '.')
        run('commit', '-qm', 'link')
        with self.assertRaises(PP.PayloadError) as caught:
            PP.git_export(repo)
        self.assertEqual(str(caught.exception), 'source_refused')


class LauncherVersionTests(unittest.TestCase):
    def test_committed_launcher_admits_exactly_this_package_version(self):
        text = (ROOT / 'packaging/launcher.py').read_text()
        self.assertEqual(launcher_versions(text), [[__version__], [__version__]])
        self.assertEqual(text.count(__version__), 2)  # Only the two generated sites name a version.

    def test_generation_rewrites_both_sites_and_refuses_anything_else(self):
        template = (ROOT / 'packaging/launcher.py').read_bytes()
        generated = PP.generate_launcher(template, '9.9.9.dev1').decode()
        self.assertEqual(launcher_versions(generated), [['9.9.9.dev1'], ['9.9.9.dev1']])
        self.assertEqual(generated.replace('9.9.9.dev1', __version__), template.decode())
        admitted, helper = b"value['release_version']!='1.0'\n", b"print('1.0: mcp |')\n"
        for label, (data, version) in {
                'no sites': (b'print(1)\n', '1.0'), 'help site missing': (admitted, '1.0'),
                'admitted site missing': (helper, '1.0'), 'duplicate site': (admitted * 2 + helper, '1.0'),
                'sites disagree': (admitted + helper.replace(b'1.0', b'2.0'), '1.0'),
                'quote in version': (template, "1.0'"), 'empty version': (template, '')}.items():
            with self.subTest(label), self.assertRaises(PP.PayloadError):
                PP.generate_launcher(data, version)
        self.assertEqual(launcher_versions(PP.generate_launcher(admitted + helper, '3.0').decode()), [['3.0'], ['3.0']])


class CommittedPayloadLockTests(unittest.TestCase):
    def test_lock_is_the_tested_runtime_closure_with_predictable_pinned_urls(self):
        lock = PP.load_lock((ROOT / 'packaging/payload-lock.json').read_bytes())
        pins = {}
        for match in re.finditer(r'^([A-Za-z0-9_.-]+)==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})$',
                                 (ROOT / 'packaging/constraints-0.54.0.txt').read_text(), re.M):
            pins[match.group(1)] = (match.group(2), match.group(3))
        self.assertEqual({w['project']: (w['version'], w['sha256']) for w in lock['app']}, pins)
        self.assertEqual([(w['project'], w['version'], w['sha256']) for w in lock['installer']],
                         [('tomlkit', '0.13.3', 'c89c649d79ee40629a9fda55f8ace8c6a1b42deb912b2a8fd8d942ddadb606b0')])
        for w in lock['app'] + lock['installer']:
            python_tag = w['filename'].split('-')[-3]
            stem = w['filename'].split('-')[0]
            self.assertEqual(w['url'], f'https://files.pythonhosted.org/packages/{python_tag}/{stem[0]}/{stem}/{w["filename"]}')
        self.assertEqual(lock['python'], {
            'version': '3.14.8', 'pbs_tag': '20261003', 'bytes': 26663219,
            'sha256': 'ed14e9042f36c0aa18383f5d9729bcbf17847e842666a793b5a5a5d8505c0ca1',
            'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20261003/'
                   'cpython-3.14.8%2B20261003-aarch64-apple-darwin-install_only_stripped.tar.gz'})
        self.assertEqual(lock['tdlib_source']['commit'], COMMIT)
        self.assertEqual(lock['tdlib_source']['url'], f'https://codeload.github.com/tdlib/td/tar.gz/{COMMIT}')
        # The notices pinned are those of the OpenSSL the native lock and its reviewed row name.
        native = json.loads((ROOT / 'packaging/tdlib-lock.json').read_bytes())
        self.assertEqual(lock['native_notices']['openssl_version'], native['build']['openssl_version'])
        self.assertEqual(lock['native_notices']['openssl_version'], native_runtime.APPROVED_PINS[0].openssl_version)


if __name__ == '__main__':
    unittest.main()
