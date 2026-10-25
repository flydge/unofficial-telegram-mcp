"""Rebuild the pinned TDLib commit from its source archive ("source-rebuild"); development host only.

Never part of a bundle and never the shipped lock: packaging/tdlib-lock.json stays prepare_native.py's
relocated Homebrew build. This route shows that the pinned commit rebuilds from its hash-pinned source,
byte for byte and from any build directory, and gives an upgrade the generated td_api.h to check against.

`build` inputs, all pinned: the codeload archive of the commit payload-lock.json's tdlib_source names
(bytes, sha256, member count and unpacked size, the pax commit comment), APPROVED_PINS row 0, the row
prepare_native builds, which must name that commit (schema sha256, version, minimum OS, OpenSSL version),
the pinned OpenSSL keg and fixed host tools. The archive's length and sha256 are checked before anything
is created. It extracts the archive into a fresh private build root, adds a detached .git/HEAD naming the
commit (TDLib's CMake reads it for the commit_hash option; an archive has no .git), configures the fixed
flag set (Release, the formula's -DCMAKE_POLICY_VERSION_MINIMUM=3.5, the pinned OpenSSL keg, the lock's
minimum macOS, no rpath, optional packages off), and builds only the tdjson target with SOURCE_DATE_EPOCH
set to the archive's commit time, ZERO_AR_DATE, a private HOME and TMPDIR and -ffile-prefix-map for every
build path. libtdjson and the keg's libssl/libcrypto then get prepare_native's relocation, dependency
checks and ad-hoc signature, and validate_native probes the result. Output, one fresh private directory:
native/, include/td/telegram/td_api.h (the generated header the media schema pins read), notices/,
provenance/ and source-rebuild-lock.json, a document with the v1 lock's keys whose build.method is
"source-rebuild" (the v1 grammar refuses it, so it can never stand in for tdlib-lock.json). The build
root is removed after success and kept after a failure; tool output goes only to the private --log. The
comparison with the committed tdlib-lock.json is information: a lock the v1 grammar cannot read is
reported, never refused. A build step runs in its own process group, which ends with the step on a
timeout, an interrupt, SIGTERM or SIGHUP.

`facts` prints a candidate archive's payload-lock.json tdlib_source record and the schema sha256, version and
commit time it implies (docs/tdlib-upgrade.md, step 1). `compare` checks two build outputs byte for byte
(names, modes and contents). `requests` reports how a candidate td_api.tl changes the definition of every
request tdjson's allowlist admits.

Refusals are fixed codes; no path, schema text or tool output is echoed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import zlib

from telegram_search_mcp.native_runtime import APPROVED_PINS, NAMES, NativeRuntimeError, parse_native_manifest, validate_native

HERE = Path(__file__).resolve().parent


def _sibling(name):
    spec = importlib.util.spec_from_file_location(f'build_tdlib_source_{name}', HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


native = _sibling('prepare_native')

OPENSSL_KEG = native.OPENSSL_KEG
CMAKE = Path('/opt/homebrew/bin/cmake')
GPERF = Path('/opt/homebrew/bin/gperf')
DEVELOPER_DIR = Path('/Applications/Xcode.app/Contents/Developer')
MAKE = '/usr/bin/make'
XCRUN = '/usr/bin/xcrun'
PAYLOAD_LOCK = HERE / 'payload-lock.json'
CODELOAD = 'https://codeload.github.com/tdlib/td/tar.gz/'
COMMITTED_LOCK = HERE / 'tdlib-lock.json'
DOCUMENT = 'source-rebuild-lock.json'
METHOD = 'source-rebuild'
TARGET = 'tdjson'
HEADER = 'td/generate/auto/td/telegram/td_api.h'
MAP_ROOT = '/tdlib-rebuild'
BUILD_PATHS = ('source', 'build', 'tmp', 'home')
TEMP_ROOTS = ('/tmp', '/private/tmp', '/var/folders', '/private/var/folders')
PATH_GRAMMAR = re.compile(r'(/[A-Za-z0-9_][A-Za-z0-9._-]{0,127}){1,32}')
TOOL_PATH_GRAMMAR = re.compile(r'(/[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}){1,32}')
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBER = 16 * 1024 * 1024
MAX_MEMBERS = 8192
MAX_UNPACKED = 256 * 1024 * 1024
MAX_HEADER = 32 * 1024 * 1024
MAX_LOCK = 65536
MAX_OUTPUT_FILES = 64
MAX_JOBS = 64
TOOL_TIMEOUT = 120
CONFIGURE_TIMEOUT = 1800
BUILD_TIMEOUT = 4 * 3600
CODES = native.CODES | frozenset({
    'build_root_refused', 'log_refused', 'pin_not_approved', 'archive_mismatch', 'source_refused',
    'toolchain_refused', 'build_failed', 'build_interrupted', 'compare_refused'})
VERSION_GRAMMAR = {
    'cmake': re.compile(r'cmake version ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,5})\n'),
    'gperf': re.compile(r'GNU gperf ([0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)\n'),
    'clang': re.compile(r'(Apple clang version [0-9.]{1,16} \(clang-[0-9.]{1,24}\))\n'),
    'ld': re.compile(r'@\(#\)PROGRAM:ld {1,4}PROJECT:(ld-[0-9.]{1,24})\n'),
    'sdk': re.compile(r'([0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)\n'),
    'build_os': re.compile(r'([0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)\n'),
    'clt': re.compile(r'(?m)^version: ([0-9][0-9.]{0,40})$'),
}


class BuildError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'build_failed')


def _need(condition, code):
    if not condition:
        raise BuildError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, limit, code):
    """A bounded regular non-symlink file, refused under `code`."""
    try:
        return native._read(path, limit)
    except native.NativePreparationError:
        raise BuildError(code) from None


def fixed_flags(row):
    """The path-free cmake flags of every rebuild, in order; the document records exactly these."""
    return ('-DCMAKE_BUILD_TYPE=Release', *native.KNOWN_FORMULA_FLAGS, '-DCMAKE_OSX_ARCHITECTURES=arm64',
            f'-DCMAKE_OSX_DEPLOYMENT_TARGET={row.minimum_os}', '-DCMAKE_SKIP_RPATH=ON',
            '-DCMAKE_BUILD_WITH_INSTALL_NAME_DIR=ON', '-DCMAKE_INSTALL_NAME_DIR=@loader_path',
            '-DCMAKE_SHARED_LINKER_FLAGS=-Wl,-headerpad_max_install_names', '-DCMAKE_FIND_FRAMEWORK=NEVER',
            '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF', '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF',
            '-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local', '-DCMAKE_DISABLE_FIND_PACKAGE_Crc32c=ON',
            '-DCMAKE_DISABLE_FIND_PACKAGE_ABSL=ON', '-DCMAKE_DISABLE_FIND_PACKAGE_Readline=ON',
            '-DOPENSSL_USE_STATIC_LIBS=OFF', '-DTD_ENABLE_LTO=OFF', '-DTD_ENABLE_JNI=OFF', '-DTD_ENABLE_DOTNET=OFF',
            '-DBUILD_TESTING=OFF')


def prefix_maps(root):
    """-ffile-prefix-map for every build path, then the root that holds them, onto fixed names."""
    return ' '.join([f'-ffile-prefix-map={root}/{name}={MAP_ROOT}/{name}' for name in BUILD_PATHS]
                    + [f'-ffile-prefix-map={root}={MAP_ROOT}'])


@dataclass(frozen=True)
class Toolchain:
    """The compilers and SDK one rebuild uses, and the version record the provenance keeps."""
    cc: str
    cxx: str
    sdk_path: str
    versions: tuple[tuple[str, str], ...]

    def version(self, name):
        return dict(self.versions)[name]


def configure_command(cmake, gperf, root, toolchain, openssl_keg, row):
    maps = prefix_maps(root)
    return [str(cmake), '-S', f'{root}/source', '-B', f'{root}/build', '-G', 'Unix Makefiles',
            f'-DCMAKE_MAKE_PROGRAM={MAKE}', f'-DCMAKE_C_COMPILER={toolchain.cc}',
            f'-DCMAKE_CXX_COMPILER={toolchain.cxx}', f'-DCMAKE_OSX_SYSROOT={toolchain.sdk_path}',
            f'-DOPENSSL_ROOT_DIR={openssl_keg}', f'-DGPERF_EXECUTABLE={gperf}', f'-DCMAKE_C_FLAGS={maps}',
            f'-DCMAKE_CXX_FLAGS={maps}', *fixed_flags(row)]


def build_command(cmake, root, jobs):
    return [str(cmake), '--build', f'{root}/build', '--target', TARGET, '--parallel', str(jobs)]


def build_env(root, developer_dir, epoch, row):
    """The whole environment of every build tool: nothing of the caller's leaks in."""
    return {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': f'{root}/home', 'TMPDIR': f'{root}/tmp/',
            'DEVELOPER_DIR': str(developer_dir), 'SOURCE_DATE_EPOCH': str(epoch), 'ZERO_AR_DATE': '1',
            'MACOSX_DEPLOYMENT_TARGET': row.minimum_os}


def source_pin(data):
    """payload-lock.json's tdlib_source and APPROVED_PINS row 0, which must name its commit.

    Row 0 is the pin prepare_native builds. A kept row may name the same commit (an OpenSSL-only upgrade);
    it is never the one rebuilt."""
    try:
        pin = json.loads(data)['tdlib_source']
        _need(type(pin) is dict and re.fullmatch(r'[0-9a-f]{40}', pin['commit']) is not None
              and re.fullmatch(r'[0-9a-f]{64}', pin['sha256']) is not None
              and all(type(pin[k]) is int and 0 < pin[k] for k in ('bytes', 'files', 'unpacked_bytes'))
              and pin['bytes'] <= MAX_ARCHIVE and pin['files'] <= MAX_MEMBERS, 'pin_not_approved')
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise BuildError('pin_not_approved') from None
    row = APPROVED_PINS[0]
    _need(row.source_commit == pin['commit'], 'pin_not_approved')
    return pin, row


def _relative(name):
    parts = name.split('/')
    return all(part not in ('', '.', '..') and '\\' not in part and '\0' not in part for part in parts)


def archive_members(archive, commit=None):
    """(commit, files, epoch) of a codeload archive: its pax commit comment (equal to `commit` when given), its
    regular files under td-<commit>/ as (relative name, bytes, executable), and the one mtime every member has,
    the commit time, which becomes SOURCE_DATE_EPOCH. Bounded; links and every other member kind refuse."""
    files, total, seen, mtimes = [], 0, 0, set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            named = tar.pax_headers.get('comment')
            _need(type(named) is str and re.fullmatch(r'[0-9a-f]{40}', named) is not None
                  and commit in (None, named), 'archive_mismatch')
            prefix = 'td-' + named + '/'
            for member in tar:
                seen += 1
                _need(seen <= 2 * MAX_MEMBERS, 'source_refused')
                mtimes.add(member.mtime)
                name = member.name.rstrip('/') if member.isdir() else member.name
                if member.isdir() and (name + '/' == prefix or name.startswith(prefix) and _relative(name[len(prefix):])):
                    continue
                relative = name[len(prefix):]
                _need(member.isfile() and name.startswith(prefix) and _relative(relative)
                      and member.size <= MAX_MEMBER, 'source_refused')
                data = tar.extractfile(member).read(member.size + 1)
                _need(len(data) == member.size, 'source_refused')
                total += member.size
                files.append((relative, data, bool(member.mode & 0o111)))
                _need(len(files) <= MAX_MEMBERS and total <= MAX_UNPACKED, 'source_refused')
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise BuildError('source_refused') from None
    _need(len(mtimes) == 1, 'source_refused')
    epoch = mtimes.pop()
    _need(type(epoch) is int and 10 ** 9 < epoch < 4 * 10 ** 9, 'source_refused')
    return named, files, epoch


def archive_facts(archive):
    """The payload-lock.json tdlib_source record of a candidate archive, with the row values it implies."""
    commit, files, epoch = archive_members(archive)
    contents = {name: data for name, data, _ in files}
    _need(native.SCHEMA in contents, 'schema_mismatch')
    project = re.search(rb'(?m)^project\(TDLib VERSION ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}) ',
                        contents.get('CMakeLists.txt', b''))
    _need(project is not None, 'source_refused')
    return {'commit': commit, 'url': CODELOAD + commit, 'bytes': len(archive), 'sha256': sha256(archive),
            'files': len(files), 'unpacked_bytes': sum(len(data) for _, data, _ in files),
            'schema_sha256': sha256(contents[native.SCHEMA]), 'tdlib_version': project.group(1).decode('ascii'),
            'source_date_epoch': epoch}


def extract_source(archive, pin, destination):
    """The pinned archive's regular files under `destination`, modes kept; returns its commit time."""
    _need(len(archive) == pin['bytes'] and sha256(archive) == pin['sha256'], 'archive_mismatch')
    _, files, epoch = archive_members(archive, pin['commit'])
    _need(len(files) == pin['files'] and sum(len(data) for _, data, _ in files) == pin['unpacked_bytes'],
          'source_refused')
    try:
        for relative, data, executable in files:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            native._write(target, data, 0o755 if executable else 0o644)
    except OSError:
        raise BuildError('source_refused') from None
    return epoch


def check_source(source, row):
    """The extracted tree is the row's: schema sha256 and TDLib version; then the detached HEAD."""
    if sha256(_read(source / native.SCHEMA, native.MAX_SCHEMA, 'schema_mismatch')) != row.schema_sha256:
        raise BuildError('schema_mismatch')
    project = re.search(rb'(?m)^project\(TDLib VERSION ([0-9.]+) ', _read(source / 'CMakeLists.txt', 1 << 20,
                                                                            'source_refused'))
    _need(project is not None and project.group(1).decode('ascii') == row.tdlib_version, 'source_refused')
    try:
        (source / '.git').mkdir(mode=0o755)
        native._write(source / '.git/HEAD', (row.source_commit + '\n').encode('ascii'), 0o644)
    except OSError:
        raise BuildError('source_refused') from None


def _query(runner, command, env):
    """A short version query: stdout and stderr, never echoed."""
    try:
        result = runner(command, capture_output=True, check=False, timeout=TOOL_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError('toolchain_refused') from None
    _need(result.returncode == 0 and len(result.stdout) + len(result.stderr) <= 65536, 'toolchain_refused')
    try:
        return (result.stdout + result.stderr).decode('utf-8')
    except UnicodeError:
        raise BuildError('toolchain_refused') from None


def _version(name, text):
    match = VERSION_GRAMMAR[name].search(text) if name == 'clt' else VERSION_GRAMMAR[name].match(text)
    _need(match is not None, 'toolchain_refused')
    return match.group(1)


def _under(path, parent):
    return TOOL_PATH_GRAMMAR.fullmatch(path) is not None and path.startswith(str(parent) + '/')


def detect_toolchain(runner, cmake, gperf, developer_dir):
    """Exact compilers and SDK under `developer_dir`, and every tool version the provenance records."""
    _need(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'toolchain_refused')
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'DEVELOPER_DIR': str(developer_dir)}
    find = lambda tool: _query(runner, [XCRUN, '--sdk', 'macosx', '--find', tool], env).strip()
    cc, cxx = find('clang'), find('clang++')
    sdk_path = _query(runner, [XCRUN, '--sdk', 'macosx', '--show-sdk-path'], env).strip()
    _need(all(_under(p, developer_dir) for p in (cc, cxx, sdk_path)), 'toolchain_refused')
    versions = {'cmake': _version('cmake', _query(runner, [str(cmake), '--version'], env)),
                'gperf': _version('gperf', _query(runner, [str(gperf), '--version'], env)),
                'clang': _version('clang', _query(runner, [cc, '--version'], env)),
                'ld': _version('ld', _query(runner, [XCRUN, '--sdk', 'macosx', 'ld', '-v'], env)),
                'sdk': _version('sdk', _query(runner, [XCRUN, '--sdk', 'macosx', '--show-sdk-version'], env)),
                'build_os': 'macOS ' + _version('build_os', _query(runner, ['/usr/bin/sw_vers', '-productVersion'], env))}
    try:
        versions['clt'] = _version('clt', _query(runner, ['/usr/sbin/pkgutil',
                                                          '--pkg-info=com.apple.pkg.CLTools_Executables'], env))
    except BuildError:
        versions['clt'] = 'none'
    versions['xcode'] = _xcode_version(developer_dir)
    return Toolchain(cc, cxx, sdk_path, tuple(sorted(versions.items())))


def _xcode_version(developer_dir):
    """The Xcode release of an Xcode developer directory; `none` for the Command Line Tools."""
    try:
        record = plistlib.loads(native._read(Path(developer_dir).parent / 'version.plist', 65536))
    except native.NativePreparationError:
        return 'none'
    except (plistlib.InvalidFileException, ValueError, OverflowError):
        raise BuildError('toolchain_refused') from None
    value = record.get('CFBundleShortVersionString') if type(record) is dict else None
    _need(type(value) is str and re.fullmatch(r'[0-9]{1,3}(\.[0-9]{1,3}){0,2}', value) is not None, 'toolchain_refused')
    return value


class _Terminated(BaseException):
    """SIGTERM or SIGHUP while a build step runs; raised in the main thread so the step's group ends first."""


def _terminated(number, frame):
    raise _Terminated()


def _step(popen, command, *, env, timeout, log):
    """One long build tool, its output appended to the private log only.

    It runs in a session of its own, so a terminal's SIGINT reaches only this process: its whole process group
    is killed and reaped whenever the wait ends other than by its exit (timeout, KeyboardInterrupt, SIGTERM or
    SIGHUP, any error), so no compiler outlives the tool. SIGTERM and SIGHUP, fatal by default, are caught for
    the step's duration (main thread only, and never where the signal was inherited as ignored, as under nohup)
    and, like KeyboardInterrupt, refused as build_interrupted; a timeout is build_failed. The previous handlers
    come back."""
    log.write(('\n$ ' + ' '.join(command) + '\n').encode())
    log.flush()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(number) is not signal.SIG_IGN:
                previous[number] = signal.signal(number, _terminated)
    process, code = None, None
    try:
        try:
            process = popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
        except OSError:
            raise BuildError('build_failed') from None
        try:
            code = process.wait(timeout=timeout)
        finally:
            if code is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
    except subprocess.TimeoutExpired:
        raise BuildError('build_failed') from None
    except (_Terminated, KeyboardInterrupt):
        raise BuildError('build_interrupted') from None
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    _need(code == 0, 'build_failed')


def lock_shaped_document(row, artifacts, toolchain):
    """prepare_native's v1 lock keys and order, recording a source rebuild; never the shipped lock."""
    document = native.lock_document(row, artifacts, {
        'compiler': 'clang', 'used_options': [],
        'built_on': {'os_version': toolchain.version('build_os'), 'xcode': toolchain.version('xcode'),
                     'clt': toolchain.version('clt')}})
    document['build'].update(method=METHOD, known_formula_flags=list(fixed_flags(row)),
                             implicit_flags='none-every-flag-explicit')
    return document


def validation_view(document, row):
    """The document as the v1 grammar would pin it, held in memory only.

    validate_native admits only the v1 grammar, whose build block names the shipped route; swapping that
    block lets it check this rebuild's artifacts, dependencies, minimum OS, signatures and probe."""
    view = json.loads(native.lock_bytes(document))
    view['build'].update(method=row.build_method, implicit_flags='homebrew-std_cmake_args-not-captured')
    try:
        return parse_native_manifest(native.lock_bytes(view))
    except NativeRuntimeError:
        raise BuildError('native_validation_failed') from None


def provenance_document(pin, row, epoch, toolchain):
    """Everything a rebuild depends on besides the archive bytes, path-free."""
    return {'archive': {k: pin[k] for k in ('commit', 'sha256', 'bytes', 'files', 'unpacked_bytes')},
            'schema_sha256': row.schema_sha256, 'tdlib_version': row.tdlib_version, 'source_date_epoch': epoch,
            'cmake_flags': list(fixed_flags(row)), 'prefix_map_root': MAP_ROOT, 'build_paths': list(BUILD_PATHS),
            'target': TARGET, 'openssl_keg': 'openssl@3/' + row.openssl_version, 'toolchain': dict(toolchain.versions)}


def _new_path(path, code):
    path = Path(path)
    _need(path.is_absolute() and '..' not in path.parts and not path.exists() and not path.is_symlink()
          and path.parent.is_dir(), code)
    return path


def check_build_root(build_root, output, log):
    """A fresh canonical build root outside the OS temp roots, disjoint from the output and the log."""
    root = _new_path(build_root, 'build_root_refused')
    text = str(root)
    _need(PATH_GRAMMAR.fullmatch(text) is not None and str(root.parent.resolve()) == str(root.parent)
          and not any(text == t or text.startswith(t + '/') for t in TEMP_ROOTS), 'build_root_refused')
    for other in (Path(output), Path(log)):
        _need(other != root and root not in other.parents and other not in root.parents, 'build_root_refused')
    return root


def _create(root, names, code):
    try:
        root.mkdir(mode=0o700)
        for name in names:
            (root / name).mkdir(mode=0o700)
    except OSError:
        raise BuildError(code) from None


OUTPUT_DIRECTORIES = ('native', 'include', 'include/td', 'include/td/telegram', 'notices', 'provenance', 'notices/tdlib')


def build(output, *, source_archive, build_root, log, openssl_keg=OPENSSL_KEG, cmake=CMAKE, gperf=GPERF,
          developer_dir=DEVELOPER_DIR, jobs=8, payload_lock=PAYLOAD_LOCK, committed=COMMITTED_LOCK,
          keep_build_tree=False, runner=subprocess.run, popen=subprocess.Popen, probe=True):
    """Rebuild the pinned commit into one fresh private output; returns a nonsecret summary."""
    _need(type(jobs) is int and 1 <= jobs <= MAX_JOBS, 'build_failed')
    pin, row = source_pin(_read(payload_lock, MAX_LOCK, 'pin_not_approved'))
    output = _new_path(output, 'output_refused')
    log_path = _new_path(log, 'log_refused')
    root = check_build_root(build_root, output, log_path)
    try:
        openssl_receipt, openssl_inputs = native.check_openssl_keg(openssl_keg, row)
    except native.NativePreparationError as error:
        raise BuildError(str(error)) from None
    archive = _read(source_archive, MAX_ARCHIVE, 'archive_mismatch')
    _need(len(archive) == pin['bytes'] and sha256(archive) == pin['sha256'], 'archive_mismatch')  # Before any write.
    toolchain = detect_toolchain(runner, cmake, gperf, developer_dir)
    try:
        log_stream = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')
    except OSError:
        raise BuildError('log_refused') from None
    with log_stream:
        _create(root, BUILD_PATHS, 'build_root_refused')
        _create(output, (*OUTPUT_DIRECTORIES, 'notices/openssl-' + row.openssl_version), 'output_refused')
        epoch = extract_source(archive, pin, root / 'source')
        check_source(root / 'source', row)
        env = build_env(root, developer_dir, epoch, row)
        _step(popen, configure_command(cmake, gperf, root, toolchain, openssl_keg, row), env=env,
              timeout=CONFIGURE_TIMEOUT, log=log_stream)
        _step(popen, build_command(cmake, root, jobs), env=env, timeout=BUILD_TIMEOUT, log=log_stream)
    inputs = {'libtdjson.dylib': root / 'build' / f'libtdjson.{row.tdlib_version}.dylib', **openssl_inputs}
    artifacts = []
    try:
        for name in NAMES:
            target = output / 'native' / name
            native._write(target, _read(inputs[name], native.MAX_DYLIB, 'build_failed'), 0o600)
            artifacts.append(native.relocate(target, name, openssl_keg, row, runner))
        native._write(output / 'include/td/telegram/td_api.h', _read(root / 'source' / HEADER, MAX_HEADER, 'build_failed'))
        document = lock_shaped_document(row, artifacts, toolchain)
        data = native.lock_bytes(document)
        lock = validation_view(document, row)
        native._write(output / DOCUMENT, data)
        native.write_notices(output, row, openssl_keg, _read(root / 'source/LICENSE_1_0.txt', 1 << 20, 'source_refused'))
        native._write(output / 'provenance/openssl-build.json', native.provenance_bytes(openssl_keg, openssl_receipt))
        native._write(output / 'provenance/source-build.json', (json.dumps(
            provenance_document(pin, row, epoch, toolchain), indent=2, sort_keys=True) + '\n').encode())
    except native.NativePreparationError as error:
        raise BuildError(str(error)) from None
    try:
        validate_native(lock, 'macos-arm64', directory=output / 'native', probe=probe)
    except NativeRuntimeError:
        raise BuildError('native_validation_failed') from None
    summary = {'document_sha256': sha256(data), 'probed': probe, 'source_date_epoch': epoch,
               'artifacts': [{k: a[k] for k in ('name', 'bytes', 'sha256')} for a in artifacts],
               'header_sha256': sha256((output / 'include/td/telegram/td_api.h').read_bytes()),
               'toolchain': dict(toolchain.versions)}
    if committed is not None:
        # Information, never a criterion: between step 1 (the v1 literals moved to the candidate) and step 2's
        # cp the committed lock no longer parses, and a complete build still reports success.
        try:
            expected = parse_native_manifest(_read(committed, MAX_LOCK, 'pin_not_approved')).model_dump(mode='json')
        except (NativeRuntimeError, BuildError):
            summary['committed_lock'] = 'unreadable'
        else:
            pinned = {a['name']: (a['bytes'], a['sha256']) for a in expected['artifacts']}
            summary['committed_lock'] = 'compared'
            summary['equals_committed_artifact'] = {a['name']: pinned.get(a['name']) == (a['bytes'], a['sha256'])
                                                    for a in artifacts}
            summary['differing_fields'] = native.differences(document, expected)
    removed = False
    if not keep_build_tree:
        try:
            shutil.rmtree(root)
            removed = True
        except OSError:
            pass  # The outputs are complete; a tree left behind is reported, never reused.
    summary['build_tree_removed'] = removed
    return summary


def output_listing(directory):
    """{relative name: (mode, sha256)} of a build output; regular files and directories only, bounded."""
    directory = Path(directory)
    _need(directory.is_absolute() and directory.is_dir() and not directory.is_symlink(), 'compare_refused')
    listing, pending = {}, [directory]
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir()):
            metadata = entry.lstat()
            relative = str(entry.relative_to(directory))
            if stat.S_ISDIR(metadata.st_mode):
                listing[relative + '/'] = (oct(stat.S_IMODE(metadata.st_mode)), None)
                pending.append(entry)
            else:
                _need(stat.S_ISREG(metadata.st_mode), 'compare_refused')
                listing[relative] = (oct(stat.S_IMODE(metadata.st_mode)),
                                     sha256(_read(entry, native.MAX_DYLIB, 'compare_refused')))
            _need(len(listing) <= MAX_OUTPUT_FILES, 'compare_refused')
    return listing


def compare_outputs(first, second):
    """Two build outputs byte for byte: every name, mode and content."""
    a, b = output_listing(first), output_listing(second)
    rows = []
    for name in sorted(set(a) | set(b)):
        row = {'path': name, 'equal': a.get(name) == b.get(name)}
        if row['equal']:
            row.update(mode=a[name][0], sha256=a[name][1])
        else:
            row.update(first=list(a[name]) if name in a else None, second=list(b[name]) if name in b else None)
        rows.append(row)
    return {'identical': a == b, 'files': rows}


def tl_functions(data):
    """name -> definition of every function in a td_api.tl; bounded, closed shape."""
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise BuildError('schema_mismatch') from None
    _, separator, functions = text.partition('\n---functions---\n')
    _need(separator != '', 'schema_mismatch')
    result = {}
    for line in functions.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        name = line.split(' ', 1)[0]
        _need(re.fullmatch(r'[a-z][A-Za-z0-9]{0,127}', name) is not None and line.endswith(';')
              and ' = ' in line and name not in result, 'schema_mismatch')
        result[name] = line
    return result


def request_changes(old, new, names):
    """How the new schema changes each allowlisted request: names only, never schema text."""
    before, after = tl_functions(old), tl_functions(new)
    report = {'requests': len(names), 'unchanged': 0, 'changed': [], 'removed': [], 'added': [], 'absent': []}
    for name in sorted(names):
        if name in before and name in after:
            if before[name] == after[name]:
                report['unchanged'] += 1
            else:
                report['changed'].append(name)
        else:
            report['removed' if name in before else 'added' if name in after else 'absent'].append(name)
    return report


def allowlisted_requests():
    from telegram_search_mcp.tdjson import _ALLOWED_REQUEST_TYPES
    return frozenset(_ALLOWED_REQUEST_TYPES)


def _jobs(value):
    import argparse
    if re.fullmatch(r'[1-9][0-9]?', value) is None or int(value) > MAX_JOBS:
        raise argparse.ArgumentTypeError('jobs must be 1-64')
    return int(value)


def parser():
    import argparse
    result = argparse.ArgumentParser(description='Rebuild the pinned TDLib commit from its source archive.')
    commands = result.add_subparsers(dest='command', required=True)
    build_parser = commands.add_parser('build', help='one rebuild into a fresh private output directory')
    build_parser.add_argument('--source-archive', required=True, help="the pinned codeload archive (payload-lock.json's tdlib_source)")
    build_parser.add_argument('--build-root', required=True, help='a fresh build directory; removed after success')
    build_parser.add_argument('--output-directory', required=True)
    build_parser.add_argument('--log', required=True, help='a new private file for the tool output')
    build_parser.add_argument('--openssl-keg', default=str(OPENSSL_KEG))
    build_parser.add_argument('--cmake', default=str(CMAKE))
    build_parser.add_argument('--gperf', default=str(GPERF))
    build_parser.add_argument('--developer-dir', default=str(DEVELOPER_DIR))
    build_parser.add_argument('--jobs', type=_jobs, default=8)
    build_parser.add_argument('--keep-build-tree', action='store_true')
    facts_parser = commands.add_parser('facts', help="a candidate archive's tdlib_source record")
    facts_parser.add_argument('--source-archive', required=True)
    compare_parser = commands.add_parser('compare', help='two build outputs, byte for byte')
    compare_parser.add_argument('--first', required=True)
    compare_parser.add_argument('--second', required=True)
    requests_parser = commands.add_parser('requests', help="the allowlisted requests' definitions in two schemas")
    requests_parser.add_argument('--old', required=True, help='td_api.tl of the current pin')
    requests_parser.add_argument('--new', required=True, help='td_api.tl of the candidate')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'build':
            result = build(Path(args.output_directory), source_archive=Path(args.source_archive),
                           build_root=Path(args.build_root), log=Path(args.log), openssl_keg=Path(args.openssl_keg),
                           cmake=Path(args.cmake), gperf=Path(args.gperf), developer_dir=Path(args.developer_dir),
                           jobs=args.jobs, keep_build_tree=args.keep_build_tree)
            status = 0
        elif args.command == 'facts':
            result = archive_facts(_read(Path(args.source_archive), MAX_ARCHIVE, 'archive_mismatch'))
            status = 0
        elif args.command == 'compare':
            result = compare_outputs(Path(args.first), Path(args.second))
            status = 0 if result['identical'] else 3
        else:
            result = request_changes(_read(Path(args.old), native.MAX_SCHEMA, 'schema_mismatch'),
                                     _read(Path(args.new), native.MAX_SCHEMA, 'schema_mismatch'), allowlisted_requests())
            status = 3 if result['changed'] or result['removed'] else 0
    except BuildError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
