"""build_tdlib_source: argument grammar, refusals, the archive check, the fixed flags and the lock-shaped document.

No real build, Homebrew, codesign or network: a synthetic archive, a synthetic OpenSSL keg, a fake runner for the
version queries and the relocation tools, and a fake process for cmake that writes a synthetic dylib. The real
two-build rehearsal is recorded in docs/plans/final-tdlib-rehearsal.md.
"""
import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from telegram_search_mcp import native_runtime
from telegram_search_mcp.native_runtime import APPROVED_PINS, COMMIT, NativeManifest, NativeRuntimeError, REQUIRED_SYMBOLS, parse_native_manifest
from test_prepare_native import OPT, SYSTEM, dylib

ROOT = Path(__file__).parents[1]
ROW = APPROVED_PINS[0]
EPOCH = 1787589927
DEVELOPER = '/Applications/Xcode.app/Contents/Developer'
CC = DEVELOPER + '/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang'
SDK = DEVELOPER + '/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk'
SCHEMA = (b'// synthetic\nuser id:int53 = User;\n\n---functions---\n\n//@description d\ngetChat chat_id:int53 = Chat;\n'
          b'getMe = User;\n')


def load():
    spec = importlib.util.spec_from_file_location('build_tdlib_source_under_test', ROOT / 'packaging/build_tdlib_source.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


B = load()


def archive(files=None, *, commit=COMMIT, prefix=None, comment=None, mtimes=None, extra=()):
    """A codeload-shaped tar.gz: one pax comment, every member under td-<commit>/ at one mtime."""
    files = files if files is not None else {
        'CMakeLists.txt': b'cmake_minimum_required(VERSION 3.10)\nproject(TDLib VERSION 1.8.67 LANGUAGES CXX C)\n',
        'LICENSE_1_0.txt': b'boost license\n', 'td/generate/scheme/td_api.tl': SCHEMA, 'src.sh': b'#!/bin/sh\n'}
    prefix = prefix if prefix is not None else f'td-{commit}/'
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': comment if comment is not None else commit}) as tar:
        directory = tarfile.TarInfo(prefix.rstrip('/'))
        directory.type, directory.mode, directory.mtime = tarfile.DIRTYPE, 0o775, EPOCH
        tar.addfile(directory)
        for index, (name, data) in enumerate(files.items()):
            info = tarfile.TarInfo(prefix + name)
            info.size, info.mtime = len(data), (mtimes or {}).get(name, EPOCH)
            info.mode = 0o775 if name.endswith('.sh') else 0o664
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info, io.BytesIO(b'x' * info.size) if info.isfile() else None)
    return buffer.getvalue(), files


def pin_for(data, files, commit=COMMIT):
    return {'commit': commit, 'url': 'https://codeload.github.com/tdlib/td/tar.gz/' + commit, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'files': len(files), 'unpacked_bytes': sum(map(len, files.values()))}


class Fakes:
    """A fake host: version queries, the relocation tools, and cmake writing a synthetic build."""

    def __init__(self, root, *, build_code=0, clang_path=CC, ld_text='@(#)PROGRAM:ld PROJECT:ld-27037.1\nBUILD x\n',
                 clt=True):
        self.root, self.build_code, self.clang_path, self.ld_text, self.clt = root, build_code, clang_path, ld_text, clt
        self.commands, self.steps = [], []
        self.openssl = root / 'Cellar/openssl@3/3.6.4'
        (self.openssl / 'lib').mkdir(parents=True)
        crypto = str(self.openssl / 'lib/libcrypto.3.dylib')
        self.specs = {'libtdjson.dylib': dict(deps=[OPT + 'libssl.3.dylib', OPT + 'libcrypto.3.dylib', *SYSTEM],
                                              symbols=REQUIRED_SYMBOLS),
                      'libssl.3.dylib': dict(deps=[crypto, SYSTEM[2]]), 'libcrypto.3.dylib': dict(deps=[SYSTEM[2]])}
        (self.openssl / 'lib/libssl.3.dylib').write_bytes(dylib(**self.specs['libssl.3.dylib']))
        (self.openssl / 'lib/libcrypto.3.dylib').write_bytes(dylib(**self.specs['libcrypto.3.dylib']))
        (self.openssl / 'INSTALL_RECEIPT.json').write_text(json.dumps({
            'compiler': 'clang', 'arch': 'arm64', 'built_on': {'os_version': 'macOS 26.6'},
            'source': {'path': '/private-api-cache/packages.json', 'versions': {'stable': '3.6.4'}}}))
        for name in ('LICENSE.txt', 'AUTHORS.md', 'README.md'):
            (self.openssl / name).write_text('openssl ' + name)

    def answers(self):
        return {('--find', 'clang'): self.clang_path + '\n', ('--find', 'clang++'): CC + '++\n',
                ('--show-sdk-path',): SDK + '\n', ('--show-sdk-version',): '27.0\n', ('ld', '-v'): self.ld_text}

    def run(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs['timeout'] <= 120 and kwargs['env']['PATH'] == '/usr/bin:/bin'
        done = lambda out, code=0: subprocess.CompletedProcess(command, code, out.encode(), b'')
        if command[0] == '/usr/bin/xcrun':
            return done(self.answers()[tuple(command[3:])])
        if command[-1] == '--version':
            return done({'cmake': 'cmake version 4.4.3\n\nCMake suite\n', 'gperf': 'GNU gperf 3.3\nCopyright\n'}.get(
                Path(command[0]).name, 'Apple clang version 21.0.0 (clang-2100.3.34.2)\nTarget: arm64\n'))
        if command[0] == '/usr/bin/sw_vers':
            return done('26.6.2\n')
        if command[0] == '/usr/sbin/pkgutil':
            return done('package-id: x\nversion: 26.6.0.0.1\nvolume: /\n', 0 if self.clt else 1)
        target = Path(command[-1])
        if command[0] == '/usr/bin/install_name_tool':
            spec = dict(self.specs[target.name])
            changes = dict(zip(command[4:-1:3], command[5:-1:3]))
            spec['deps'] = [changes.get(d, d) for d in spec['deps']]
            target.write_bytes(dylib(**spec))
        else:
            target.write_bytes(target.read_bytes() + b'synthetic-adhoc-signature')
        return done('')

    def popen(self, command, **kwargs):
        self.steps.append((command, kwargs))
        kwargs['stdout'].write(b'private build output\n')
        fake = self

        class Process:
            pid = 4242

            def wait(self, timeout=None):
                if '--build' in command and fake.build_code == 0:
                    root = Path(command[2]).parent
                    (root / 'build/libtdjson.1.8.67.dylib').write_bytes(dylib(**fake.specs['libtdjson.dylib']))
                    header = root / 'source' / B.HEADER
                    header.parent.mkdir(parents=True)
                    header.write_text('class getChat final : public Function {};\n')
                return fake.build_code if '--build' in command else 0
        return Process()


class GrammarTests(unittest.TestCase):
    BUILD = ['build', '--source-archive', '/a', '--build-root', '/b', '--output-directory', '/o', '--log', '/l']

    def test_each_command_requires_its_arguments_and_jobs_is_bounded(self):
        args = B.parser().parse_args(self.BUILD)
        self.assertEqual((args.jobs, args.keep_build_tree, args.cmake, args.developer_dir),
                         (8, False, str(B.CMAKE), str(B.DEVELOPER_DIR)))
        self.assertEqual(B.parser().parse_args(self.BUILD + ['--jobs', '64']).jobs, 64)
        refused = [self.BUILD[:-2], self.BUILD + ['--jobs', '0'], self.BUILD + ['--jobs', '65'],
                   self.BUILD + ['--jobs', '08'], self.BUILD + ['--jobs', 'x'], self.BUILD + ['--unknown'],
                   ['compare', '--first', '/a'], ['requests', '--new', '/n'], ['rebuild'], []]
        for argv in refused:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                B.parser().parse_args(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_every_refusal_is_a_fixed_code(self):
        self.assertEqual(str(B.BuildError('archive_mismatch')), 'archive_mismatch')
        self.assertEqual(str(B.BuildError('/private/path')), 'build_failed')
        self.assertTrue(B.native.CODES <= B.CODES)


class PinAndArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_the_payload_lock_must_name_the_commit_of_row_0(self):
        data, files = archive()
        pin, row = B.source_pin(json.dumps({'tdlib_source': pin_for(data, files)}).encode())
        self.assertIs(row, ROW)
        self.assertEqual(pin['commit'], COMMIT)
        self.assertEqual(B.source_pin((ROOT / 'packaging/payload-lock.json').read_bytes())[1], ROW)
        for value in ({'tdlib_source': dict(pin, commit='0' * 40)}, {'tdlib_source': dict(pin, bytes=True)},
                      {'tdlib_source': dict(pin, sha256='A' * 64)}, {'other': 1}, []):
            with self.subTest(value=value), self.assertRaises(B.BuildError) as caught:
                B.source_pin(json.dumps(value).encode())
            self.assertEqual(str(caught.exception), 'pin_not_approved')

    def test_the_rebuild_builds_row_0_like_prepare_native_also_beside_a_row_of_the_same_commit(self):
        # An OpenSSL-only upgrade keeps the commit: the candidate row 0 and the kept row name one commit.
        data, files = archive()
        lock = json.dumps({'tdlib_source': pin_for(data, files)}).encode()
        candidate = dataclasses.replace(ROW, openssl_version='3.6.5')
        with mock.patch.object(B, 'APPROVED_PINS', (candidate, ROW)):
            self.assertIs(B.source_pin(lock)[1], candidate)
        with mock.patch.object(B, 'APPROVED_PINS', (dataclasses.replace(ROW, source_commit='1' * 40), ROW)), \
                self.assertRaises(B.BuildError) as caught:
            B.source_pin(lock)
        self.assertEqual(str(caught.exception), 'pin_not_approved')

    def test_a_verified_archive_extracts_with_its_modes_and_commit_time(self):
        data, files = archive()
        self.assertEqual(B.extract_source(data, pin_for(data, files), self.root / 'source'), EPOCH)
        for name, content in files.items():
            self.assertEqual((self.root / 'source' / name).read_bytes(), content)
        self.assertTrue(os.stat(self.root / 'source/src.sh').st_mode & 0o111)
        self.assertFalse(os.stat(self.root / 'source/CMakeLists.txt').st_mode & 0o111)

    def test_facts_give_the_tdlib_source_record_and_the_row_values_a_candidate_implies(self):
        data, files = archive()
        self.assertEqual(B.archive_facts(data), dict(pin_for(data, files), schema_sha256=hashlib.sha256(SCHEMA).hexdigest(),
                                                     tdlib_version='1.8.67', source_date_epoch=EPOCH))
        for options, code in ((dict(comment='not-a-commit'), 'archive_mismatch'), (dict(comment='0' * 40), 'source_refused'),
                              (dict(files={'CMakeLists.txt': b'project(TDLib VERSION 1.8.67 )\n'}), 'schema_mismatch'),
                              (dict(files={B.native.SCHEMA: SCHEMA}), 'source_refused')):
            with self.subTest(options=options), self.assertRaises(B.BuildError) as caught:
                B.archive_facts(archive(**options)[0])
            self.assertEqual(str(caught.exception), code)
        stdout = io.StringIO()
        (self.root / 'td.tar.gz').write_bytes(data)
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(B.main(['facts', '--source-archive', str(self.root / 'td.tar.gz')]), 0)
        self.assertEqual(json.loads(stdout.getvalue())['sha256'], hashlib.sha256(data).hexdigest())

    def refused(self, code, data, pin):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(B.BuildError) as caught:
            B.extract_source(data, pin, Path(tmp) / 'source')
        self.assertEqual(str(caught.exception), code)
        self.assertIsNone(caught.exception.__cause__)

    def test_any_archive_difference_refuses(self):
        data, files = archive()
        pin = pin_for(data, files)
        self.refused('archive_mismatch', data + b'\0', pin)
        self.refused('archive_mismatch', data, dict(pin, sha256='0' * 64))
        other, files = archive(comment='0' * 40)
        self.refused('archive_mismatch', other, pin_for(other, files))
        link = tarfile.TarInfo(f'td-{COMMIT}/link')
        link.type, link.linkname, link.mtime = tarfile.SYMTYPE, '/etc/passwd', EPOCH
        escape = tarfile.TarInfo(f'td-{COMMIT}/../escape')
        escape.size, escape.mtime = 1, EPOCH
        outside = tarfile.TarInfo('elsewhere/file')
        outside.size, outside.mtime = 1, EPOCH
        for label, member in (('symlink', link), ('dot-dot', escape), ('outside the prefix', outside)):
            with self.subTest(label):
                bad, files = archive(extra=(member,))
                self.refused('source_refused', bad, dict(pin_for(bad, files), files=len(files) + 1,
                                                         unpacked_bytes=pin['unpacked_bytes'] + member.size))
        mixed, files = archive(mtimes={'src.sh': EPOCH + 1})
        self.refused('source_refused', mixed, pin_for(mixed, files))
        self.refused('source_refused', data, dict(pin, files=pin['files'] + 1))
        self.refused('source_refused', data, dict(pin, unpacked_bytes=pin['unpacked_bytes'] - 1))

    def test_the_tree_must_be_the_rows_schema_and_version_then_gets_a_detached_head(self):
        data, files = archive()
        source = self.root / 'source'
        B.extract_source(data, pin_for(data, files), source)
        with self.assertRaises(B.BuildError) as caught:
            B.check_source(source, ROW)
        self.assertEqual(str(caught.exception), 'schema_mismatch')
        row = dataclasses.replace(ROW, schema_sha256=hashlib.sha256(SCHEMA).hexdigest())
        with self.assertRaises(B.BuildError) as caught:
            B.check_source(source, dataclasses.replace(row, tdlib_version='1.8.68'))
        self.assertEqual(str(caught.exception), 'source_refused')
        B.check_source(source, row)
        self.assertEqual((source / '.git/HEAD').read_text(), COMMIT + '\n')


class FlagTests(unittest.TestCase):
    def test_fixed_flags_name_release_the_formula_flag_the_lock_minimum_and_no_path_of_a_build(self):
        flags = B.fixed_flags(ROW)
        for flag in ('-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_POLICY_VERSION_MINIMUM=3.5', '-DCMAKE_OSX_ARCHITECTURES=arm64',
                     '-DCMAKE_OSX_DEPLOYMENT_TARGET=26.0', '-DCMAKE_SKIP_RPATH=ON', '-DTD_ENABLE_LTO=OFF'):
            self.assertIn(flag, flags)
        self.assertEqual(len(set(flags)), len(flags))
        self.assertTrue(all('/Users' not in f and 'tdlib-rebuild' not in f for f in flags))

    def test_every_build_path_is_mapped_and_the_environment_is_closed(self):
        root = Path('/Volumes/build/tree-a')
        toolchain = B.Toolchain(CC, CC + '++', SDK, (('xcode', '27.1'),))
        command = B.configure_command(B.CMAKE, B.GPERF, root, toolchain, B.OPENSSL_KEG, ROW)
        maps = B.prefix_maps(root)
        for name in ('source', 'build', 'tmp', 'home'):
            self.assertIn(f'-ffile-prefix-map={root}/{name}={B.MAP_ROOT}/{name}', maps)
        self.assertTrue(maps.endswith(f'-ffile-prefix-map={root}={B.MAP_ROOT}'))
        self.assertIn(f'-DCMAKE_C_FLAGS={maps}', command)
        self.assertIn(f'-DCMAKE_CXX_FLAGS={maps}', command)
        self.assertIn(f'-DOPENSSL_ROOT_DIR={B.OPENSSL_KEG}', command)
        self.assertEqual(command[-len(B.fixed_flags(ROW)):], list(B.fixed_flags(ROW)))
        self.assertEqual(B.build_command(B.CMAKE, root, 6)[-4:], ['--target', 'tdjson', '--parallel', '6'])
        with mock.patch.dict(os.environ, {'CFLAGS': '-g', 'LDFLAGS': '-L/x'}):
            env = B.build_env(root, Path(DEVELOPER), EPOCH, ROW)
        self.assertEqual(env, {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': f'{root}/home', 'TMPDIR': f'{root}/tmp/',
                               'DEVELOPER_DIR': DEVELOPER, 'SOURCE_DATE_EPOCH': str(EPOCH), 'ZERO_AR_DATE': '1',
                               'MACOSX_DEPLOYMENT_TARGET': '26.0'})

    def test_the_build_root_is_fresh_canonical_and_outside_the_temp_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            output, log = base / 'out', base / 'build.log'
            # The suite's own temp directory is under the macOS TMPDIR, a refused temp root itself.
            with self.assertRaises(B.BuildError):
                B.check_build_root(base / 'root', output, log)
            with mock.patch.object(B, 'TEMP_ROOTS', ('/tmp', '/private/tmp')):
                self.assertEqual(B.check_build_root(base / 'root', output, log), base / 'root')
                (base / 'exists').mkdir()
                for root in ('/tmp/tdlib', '/private/tmp/tdlib', 'relative/root', str(base / 'exists'), str(base / 'with space'),
                             str(base / 'missing/root'), str(output), str(base / 'x/../root'), str(base / '-dash')):
                    with self.subTest(root=root), self.assertRaises(B.BuildError) as caught:
                        B.check_build_root(Path(root), output, log)
                    self.assertEqual(str(caught.exception), 'build_root_refused')
                with self.assertRaises(B.BuildError):
                    B.check_build_root(base / 'root', base / 'root/out', log)
                with self.assertRaises(B.BuildError):
                    B.check_build_root(base / 'root', output, base / 'root/build.log')


class DocumentTests(unittest.TestCase):
    TOOLCHAIN = B.Toolchain(CC, CC + '++', SDK, tuple(sorted({
        'build_os': 'macOS 26.6.2', 'clang': 'Apple clang version 21.0.0 (clang-2100.3.34.2)', 'clt': '26.6.0.0.1',
        'cmake': '4.4.3', 'gperf': '3.3', 'ld': 'ld-27037.1', 'sdk': '27.0', 'xcode': '27.1'}.items())))

    def test_the_document_has_the_locks_keys_records_a_source_rebuild_and_is_never_a_lock(self):
        committed = json.loads((ROOT / 'packaging/tdlib-lock.json').read_bytes())
        document = B.lock_shaped_document(ROW, committed['artifacts'], self.TOOLCHAIN)
        self.assertEqual(list(document), list(committed))
        self.assertEqual(list(document['build']), list(committed['build']))
        self.assertEqual({k: v for k, v in document.items() if k != 'build'}, {k: v for k, v in committed.items() if k != 'build'})
        self.assertEqual(document['build'], {
            'method': 'source-rebuild', 'reproducibility': 'unverified', 'compiler': 'clang', 'build_os': 'macOS 26.6.2',
            'xcode': '27.1', 'clt': '26.6.0.0.1', 'used_options': [], 'known_formula_flags': list(B.fixed_flags(ROW)),
            'implicit_flags': 'none-every-flag-explicit', 'openssl_version': '3.6.4'})
        with self.assertRaises(NativeRuntimeError):
            parse_native_manifest(B.native.lock_bytes(document))
        view = B.validation_view(document, ROW)
        self.assertIs(type(view), NativeManifest)
        self.assertEqual([a.model_dump() for a in view.artifacts], committed['artifacts'])
        self.assertEqual(B.provenance_document({'commit': COMMIT, 'sha256': '0' * 64, 'bytes': 1, 'files': 2,
                                                'unpacked_bytes': 3, 'url': 'u'}, ROW, EPOCH, self.TOOLCHAIN)['toolchain'],
                         dict(self.TOOLCHAIN.versions))

    def test_toolchain_detection_records_versions_and_refuses_anything_unexpected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fakes = Fakes(Path(tmp))
            with mock.patch.object(B, '_xcode_version', return_value='27.1'):
                toolchain = B.detect_toolchain(fakes.run, B.CMAKE, B.GPERF, Path(DEVELOPER))
                self.assertEqual((toolchain.cc, toolchain.sdk_path), (CC, SDK))
                self.assertEqual(toolchain.versions, self.TOOLCHAIN.versions)
                fakes.clt = False
                self.assertEqual(B.detect_toolchain(fakes.run, B.CMAKE, B.GPERF, Path(DEVELOPER)).version('clt'), 'none')
                for index, options in enumerate((dict(clang_path='/usr/local/bin/clang'), dict(ld_text='ld version unknown\n'))):
                    with self.subTest(options=options), self.assertRaises(B.BuildError) as caught:
                        B.detect_toolchain(Fakes(Path(tmp) / str(index), **options).run, B.CMAKE, B.GPERF, Path(DEVELOPER))
                    self.assertEqual(str(caught.exception), 'toolchain_refused')
            with mock.patch.object(B.platform, 'machine', return_value='x86_64'), self.assertRaises(B.BuildError):
                B.detect_toolchain(fakes.run, B.CMAKE, B.GPERF, Path(DEVELOPER))

    def test_a_long_step_logs_privately_and_its_process_group_ends_on_timeout(self):
        log = io.BytesIO()

        def process(code=0, expires=False):
            class Process:
                pid = 77
                pending = [expires]

                def wait(self, timeout=None):
                    if self.pending[0] and timeout is not None:
                        self.pending[0] = False
                        raise subprocess.TimeoutExpired('cmake', timeout)
                    return code
            return lambda command, **kwargs: Process()
        B._step(process(), ['cmake', '--build'], env={}, timeout=5, log=log)
        self.assertIn(b'$ cmake --build', log.getvalue())
        with self.assertRaises(B.BuildError) as caught:
            B._step(process(code=2), ['cmake'], env={}, timeout=5, log=log)
        self.assertEqual(str(caught.exception), 'build_failed')
        with mock.patch.object(B.os, 'killpg') as killpg, self.assertRaises(B.BuildError):
            B._step(process(expires=True), ['cmake'], env={}, timeout=5, log=log)
        killpg.assert_called_once_with(77, B.signal.SIGKILL)

    def test_an_interrupt_ends_the_steps_process_group_and_restores_the_signal_handlers(self):
        log, received, numbers = io.BytesIO(), [], (B.signal.SIGTERM, B.signal.SIGHUP)

        def sentinel(number, frame):  # Takes a signal the step failed to handle, instead of ending the suite.
            received.append(number)
        previous = {n: B.signal.signal(n, sentinel) for n in numbers}
        self.addCleanup(lambda: [B.signal.signal(n, h) for n, h in previous.items()])

        class Interrupted:
            pid, calls = 78, []

            def wait(self, timeout=None):
                self.calls.append(timeout)
                if len(self.calls) == 1:
                    raise KeyboardInterrupt
                return -9
        with mock.patch.object(B.os, 'killpg') as killpg, self.assertRaises(KeyboardInterrupt):
            B._step(lambda command, **kwargs: Interrupted(), ['make'], env={}, timeout=5, log=log)
        killpg.assert_called_once_with(78, B.signal.SIGKILL)
        self.assertEqual(Interrupted.calls, [5, None])  # Reaped after the kill.
        self.assertTrue(all(B.signal.getsignal(n) is sentinel for n in numbers))
        for number in numbers:  # SIGTERM and SIGHUP become a fixed refusal once the group has ended.
            def wait(timeout=None, number=number, calls=[]):
                calls.append(timeout)
                if len(calls) == 1:
                    B.signal.raise_signal(number)
                return -9
            process = mock.Mock(pid=79, wait=wait)
            with self.subTest(signal=number), mock.patch.object(B.os, 'killpg') as killpg, \
                    self.assertRaises(B.BuildError) as caught:
                B._step(lambda command, **kwargs: process, ['make'], env={}, timeout=5, log=log)
            self.assertEqual(str(caught.exception), 'build_interrupted')
            killpg.assert_called_once_with(79, B.signal.SIGKILL)
            self.assertTrue(all(B.signal.getsignal(n) is sentinel for n in numbers))
        self.assertEqual(received, [])

    def test_a_signalled_tool_leaves_no_build_process_behind(self):
        # A real child group that would outlive the tool: a shell and its background sleep, in their own session.
        script = (
            'import importlib.util, io, subprocess, sys\n'
            'spec = importlib.util.spec_from_file_location("b", sys.argv[1])\n'
            'b = importlib.util.module_from_spec(spec); sys.modules["b"] = b; spec.loader.exec_module(b)\n'
            'b._step(subprocess.Popen, ["/bin/sh", "-c", sys.argv[2]], env={"PATH": "/usr/bin:/bin"}, timeout=60,'
            ' log=open(sys.argv[3], "wb"))\n')
        for number in (B.signal.SIGINT, B.signal.SIGTERM):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as tmp:
                pids = Path(tmp) / 'pids'
                child = f"sleep 30 & echo \"$$ $!\" > '{pids}.part' && mv '{pids}.part' '{pids}'; wait"
                env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'src'), os.environ.get('PYTHONPATH', '')]))
                tool = subprocess.Popen([sys.executable, '-c', script, str(ROOT / 'packaging/build_tdlib_source.py'), child,
                                         str(Path(tmp) / 'step.log')], env=env, stderr=subprocess.DEVNULL)
                self.addCleanup(lambda: tool.poll() is None and tool.kill())
                for _ in range(200):
                    if pids.exists() or tool.poll() is not None:
                        break
                    time.sleep(0.05)
                shell, sleeper = map(int, pids.read_text().split())
                self.addCleanup(self.kill_group, shell)
                tool.send_signal(number)
                self.assertNotEqual(tool.wait(timeout=10), 0)
                for pid in (shell, sleeper):
                    self.assertTrue(self.gone(pid), pid)

    @staticmethod
    def kill_group(pid):
        try:
            os.killpg(pid, B.signal.SIGKILL)
        except OSError:
            pass

    @staticmethod
    def gone(pid):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


class BuildFlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.fakes = Fakes(self.root)
        check_source = B.check_source
        data, files = archive()
        (self.root / 'td.tar.gz').write_bytes(data)
        (self.root / 'payload-lock.json').write_text(json.dumps({'tdlib_source': pin_for(data, files)}))
        for patcher in (mock.patch.object(native_runtime, '_verify_signature'),
                        mock.patch.object(B, '_xcode_version', return_value='27.1'),
                        # The suite's temp directory lies under the macOS TMPDIR, a refused build root.
                        mock.patch.object(B, 'TEMP_ROOTS', ('/tmp', '/private/tmp')),
                        # The real schema pin cannot be matched synthetically; check_source has its own test.
                        mock.patch.object(B, 'check_source', side_effect=lambda source, row: check_source(
                            source, dataclasses.replace(row, schema_sha256=hashlib.sha256(SCHEMA).hexdigest())))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, name, **options):
        return B.build(self.root / f'out-{name}', source_archive=self.root / 'td.tar.gz', build_root=self.root / f'root-{name}',
                       log=self.root / f'{name}.log', openssl_keg=self.fakes.openssl, payload_lock=self.root / 'payload-lock.json',
                       runner=self.fakes.run, popen=self.fakes.popen, probe=False, **options)

    def test_a_rebuild_relocates_signs_writes_the_document_and_removes_its_tree(self):
        summary = self.build('a', jobs=3)
        out = self.root / 'out-a'
        self.assertFalse((self.root / 'root-a').exists())
        self.assertTrue(summary['build_tree_removed'])
        self.assertEqual(sorted(str(p.relative_to(out)) for p in out.rglob('*') if p.is_file()), sorted([
            'include/td/telegram/td_api.h', 'native/libcrypto.3.dylib', 'native/libssl.3.dylib', 'native/libtdjson.dylib',
            'notices/openssl-3.6.4/AUTHORS.md', 'notices/openssl-3.6.4/LICENSE.txt', 'notices/openssl-3.6.4/README.md',
            'notices/tdlib/LICENSE_1_0.txt', 'provenance/openssl-build.json', 'provenance/source-build.json',
            'source-rebuild-lock.json']))
        document = json.loads((out / 'source-rebuild-lock.json').read_bytes())
        self.assertEqual(document['build']['method'], 'source-rebuild')
        self.assertEqual(summary['document_sha256'], hashlib.sha256((out / 'source-rebuild-lock.json').read_bytes()).hexdigest())
        self.assertEqual([a['dependencies'] for a in document['artifacts']], [
            ['@loader_path/libssl.3.dylib', '@loader_path/libcrypto.3.dylib', *SYSTEM], ['@loader_path/libcrypto.3.dylib', SYSTEM[2]],
            [SYSTEM[2]]])
        self.assertEqual(summary['committed_lock'], 'compared')
        self.assertEqual(summary['equals_committed_artifact'], dict.fromkeys(native_runtime.NAMES, False))
        self.assertIn('artifacts[0].sha256', summary['differing_fields'])
        self.assertIn('build.method', summary['differing_fields'])
        provenance = json.loads((out / 'provenance/source-build.json').read_bytes())
        self.assertEqual((provenance['source_date_epoch'], provenance['archive']['commit']), (EPOCH, COMMIT))
        self.assertNotIn(str(self.root), json.dumps(provenance) + json.dumps(document) + json.dumps(summary))
        (configure, configure_options), (build, build_options) = self.fakes.steps
        self.assertEqual(build[-2:], ['--parallel', '3'])
        self.assertEqual(configure_options['env']['SOURCE_DATE_EPOCH'], str(EPOCH))
        self.assertTrue(configure_options['start_new_session'])
        self.assertIn(f'-ffile-prefix-map={self.root}/root-a/source={B.MAP_ROOT}/source', ' '.join(configure))
        log = (self.root / 'a.log').read_bytes()
        self.assertIn(b'private build output', log)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'a.log').st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(out / 'native/libtdjson.dylib').st_mode), 0o500)

    def test_two_rebuilds_in_different_trees_compare_identical_and_a_changed_byte_shows(self):
        self.build('a')
        self.build('second-tree-with-a-longer-name')
        first, second = self.root / 'out-a', self.root / 'out-second-tree-with-a-longer-name'
        report = B.compare_outputs(first, second)
        self.assertTrue(report['identical'])
        self.assertTrue(all(row['equal'] for row in report['files']))
        target = second / 'notices/tdlib/LICENSE_1_0.txt'
        target.chmod(0o600)
        target.write_bytes(b'changed')
        target.chmod(0o400)
        report = B.compare_outputs(first, second)
        self.assertFalse(report['identical'])
        self.assertEqual([row['path'] for row in report['files'] if not row['equal']], ['notices/tdlib/LICENSE_1_0.txt'])
        target.chmod(0o600)
        target.write_bytes(b'boost license\n')
        self.assertFalse(B.compare_outputs(first, second)['identical'])  # the mode differs now
        target.chmod(0o400)
        (second / 'extra').write_bytes(b'')
        self.assertEqual([r['path'] for r in B.compare_outputs(first, second)['files'] if not r['equal']], ['extra'])
        (second / 'extra').unlink()
        (second / 'link').symlink_to(first)
        with self.assertRaises(B.BuildError) as caught:
            B.compare_outputs(first, second)
        self.assertEqual(str(caught.exception), 'compare_refused')

    def test_a_failed_build_keeps_its_tree_and_existing_paths_refuse(self):
        self.fakes.build_code = 1
        with self.assertRaises(B.BuildError) as caught:
            self.build('a')
        self.assertEqual(str(caught.exception), 'build_failed')
        self.assertTrue((self.root / 'root-a/source/CMakeLists.txt').is_file())
        self.fakes.build_code = 0
        for code, name in (('output_refused', 'a'), ):
            with self.assertRaises(B.BuildError) as caught:
                self.build(name)
            self.assertEqual(str(caught.exception), code)
        with self.assertRaises(B.BuildError) as caught:
            B.build(self.root / 'out-b', source_archive=self.root / 'td.tar.gz', build_root=self.root / 'root-b',
                    log=self.root / 'a.log', openssl_keg=self.fakes.openssl, payload_lock=self.root / 'payload-lock.json',
                    runner=self.fakes.run, popen=self.fakes.popen, probe=False)
        self.assertEqual(str(caught.exception), 'log_refused')
        with self.assertRaises(B.BuildError) as caught:
            B.build(self.root / 'out-c', source_archive=self.root / 'td.tar.gz', build_root=self.root / 'root-c',
                    log=self.root / 'c.log', openssl_keg=self.root / 'Cellar/openssl@3', payload_lock=self.root / 'payload-lock.json',
                    runner=self.fakes.run, popen=self.fakes.popen, probe=False)
        self.assertEqual(str(caught.exception), 'keg_mismatch')
        self.assertFalse((self.root / 'out-c').exists())

    def test_a_kept_or_unremovable_tree_is_reported_and_an_unreadable_committed_lock_is_only_reported(self):
        self.assertFalse(self.build('kept', keep_build_tree=True)['build_tree_removed'])
        self.assertTrue((self.root / 'root-kept/source/.git/HEAD').is_file())
        with mock.patch.object(B.shutil, 'rmtree', side_effect=OSError('busy')):
            self.assertFalse(self.build('busy')['build_tree_removed'])
        # Between step 1 (the v1 literals moved to the candidate) and step 2's cp the committed lock no longer
        # parses: the comparison is information, so a complete build still succeeds and says so.
        (self.root / 'bad-lock.json').write_text('{"manifest_version": 1}')
        for name, committed in (('bad', self.root / 'bad-lock.json'), ('missing', self.root / 'missing-lock.json')):
            with self.subTest(name):
                summary = self.build(name, committed=committed)
                self.assertEqual(summary['committed_lock'], 'unreadable')
                self.assertNotIn('equals_committed_artifact', summary)
                self.assertNotIn('differing_fields', summary)
                self.assertTrue(summary['build_tree_removed'])
                self.assertTrue((self.root / f'out-{name}' / B.DOCUMENT).is_file())

    def test_an_archive_that_is_not_the_pinned_one_refuses_before_anything_is_created(self):
        (self.root / 'longer.tar.gz').write_bytes((self.root / 'td.tar.gz').read_bytes() + b'\0')
        with self.assertRaises(B.BuildError) as caught:
            B.build(self.root / 'out-x', source_archive=self.root / 'longer.tar.gz', build_root=self.root / 'root-x',
                    log=self.root / 'x.log', openssl_keg=self.fakes.openssl, payload_lock=self.root / 'payload-lock.json',
                    runner=self.fakes.run, popen=self.fakes.popen, probe=False)
        self.assertEqual(str(caught.exception), 'archive_mismatch')
        for path in ('out-x', 'root-x', 'x.log'):
            self.assertFalse((self.root / path).exists(), path)
        self.assertEqual((self.fakes.commands, self.fakes.steps), ([], []))

    def test_main_prints_only_a_code_when_it_refuses(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = B.main(['build', '--source-archive', str(self.root / 'missing.tar.gz'), '--build-root', str(self.root / 'r'),
                           '--output-directory', str(self.root), '--log', str(self.root / 'x.log')])
        self.assertEqual((code, stdout.getvalue()), (2, ''))
        self.assertEqual(json.loads(stderr.getvalue()), {'status': 'refused', 'code': 'output_refused'})


class RequestDiffTests(unittest.TestCase):
    OLD = SCHEMA + b'searchChats query:string limit:int32 = Chats;\ngoneRequest = Ok;\n'
    NEW = SCHEMA.replace(b'getChat chat_id:int53', b'getChat chat_id:int53 secret_field:Bool') + b'searchChats query:string limit:int32 = Chats;\naddedRequest = Ok;\n'

    def test_each_allowlisted_request_is_unchanged_changed_removed_added_or_absent(self):
        names = {'getChat', 'getMe', 'searchChats', 'goneRequest', 'addedRequest', 'neverDefined'}
        self.assertEqual(B.request_changes(self.OLD, self.NEW, names), {
            'requests': 6, 'unchanged': 2, 'changed': ['getChat'], 'removed': ['goneRequest'], 'added': ['addedRequest'],
            'absent': ['neverDefined']})
        self.assertEqual(B.tl_functions(SCHEMA), {'getChat': 'getChat chat_id:int53 = Chat;', 'getMe': 'getMe = User;'})
        for bad in (b'no functions section\n', SCHEMA + b'getMe = User;\n', SCHEMA + b'Broken line\n', b'\xff'):
            with self.subTest(bad=bad), self.assertRaises(B.BuildError) as caught:
                B.tl_functions(bad)
            self.assertEqual(str(caught.exception), 'schema_mismatch')

    def test_the_command_reads_tdjsons_allowlist_and_never_prints_schema_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / 'old.tl', Path(tmp) / 'new.tl'
            old.write_bytes(self.OLD)
            new.write_bytes(self.NEW)
            for argv, expected in ((['--old', str(old), '--new', str(old)], 0), (['--old', str(old), '--new', str(new)], 3)):
                stdout = io.StringIO()
                with self.subTest(argv=argv), contextlib.redirect_stdout(stdout):
                    self.assertEqual(B.main(['requests', *argv]), expected)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report['requests'], 35)
                self.assertNotIn('secret_field', stdout.getvalue())
            self.assertEqual(report['changed'], ['getChat'])
            self.assertIn('checkDatabaseEncryptionKey', report['absent'])
        self.assertIn('sendMessage', B.allowlisted_requests())


class HeaderOverrideTests(unittest.TestCase):
    def test_the_schema_pins_read_a_candidate_header_and_a_wrong_override_fails(self):
        import tdlib_header
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / 'td_api.h'
            header.write_text('class getChat final : public Function {};\n')
            with mock.patch.dict(os.environ, {tdlib_header.ENVIRONMENT: str(header)}):
                self.assertEqual(tdlib_header.header_path(), (header, True))
                self.assertIn('getChat', tdlib_header.pinned_header(self))
            with mock.patch.dict(os.environ, {tdlib_header.ENVIRONMENT: str(Path(tmp) / 'missing.h')}), \
                    self.assertRaises(self.failureException):
                tdlib_header.pinned_header(self)
        with mock.patch.dict(os.environ, {tdlib_header.ENVIRONMENT: ''}):
            self.assertEqual(tdlib_header.header_path()[0].parts[-4:], ('include', 'td', 'telegram', 'td_api.h'))


if __name__ == '__main__':
    unittest.main()
