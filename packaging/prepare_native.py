"""Reproduce the native lock from the pinned Homebrew kegs ("homebrew-receipt-relocated").

Development host only; never part of a bundle. Copies libtdjson and libssl/libcrypto 3, rewrites
their install names to @loader_path, requires the approved dependency set with no search-path
commands, ad-hoc signs them, takes build provenance from INSTALL_RECEIPT.json, checks the TDLib
source's td_api.tl against the approved schema pin, writes tdlib-lock.json and runs
validate_native(probe=True) on the result. The approved values come from native_runtime's reviewed
APPROVED_PINS row; nothing here widens it. Output is one fresh private directory:
native/, notices/, provenance/ and tdlib-lock.json. Refusals are fixed codes; no path, receipt
or tool output is echoed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess

from telegram_search_mcp.native_runtime import (
    APPROVED_PINS, NAMES, NativeRuntimeError, REQUIRED_SYMBOLS, SYSTEM_LIBRARIES, _macho_metadata,
    parse_native_manifest, validate_native,
)

TDLIB_KEG = Path('/opt/homebrew/Cellar/tdlib/HEAD-d1085f9')
OPENSSL_KEG = Path('/opt/homebrew/Cellar/openssl@3/3.6.4')
OPENSSL_OPT = '/opt/homebrew/opt/openssl@3/lib/'
SCHEMA = 'td/generate/scheme/td_api.tl'
# The formula's own cmake flag; INSTALL_RECEIPT.json does not record formula arguments.
KNOWN_FORMULA_FLAGS = ('-DCMAKE_POLICY_VERSION_MINIMUM=3.5',)
LOADER = {'libtdjson.dylib': {'@loader_path/libssl.3.dylib', '@loader_path/libcrypto.3.dylib'},
          'libssl.3.dylib': {'@loader_path/libcrypto.3.dylib'}, 'libcrypto.3.dylib': set()}
SEARCH_PATH_COMMANDS = {0x8000001c, 0x27, 0xe}  # LC_RPATH, LC_DYLD_ENVIRONMENT, LC_LOAD_DYLINKER
MAX_DYLIB = 32 * 1024 * 1024
MAX_SCHEMA = 4 * 1024 * 1024
TOOL_TIMEOUT = 120
TOOL_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
CODES = frozenset({'output_refused', 'keg_mismatch', 'schema_mismatch', 'search_path_command',
                   'dependency_mismatch', 'tool_failed', 'native_validation_failed'})


class NativePreparationError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'native_validation_failed')


def _read(path, limit):
    """A bounded regular non-symlink input file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise NativePreparationError('keg_mismatch') from None
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise NativePreparationError('keg_mismatch')
        data = stream.read(limit + 1)
    if len(data) != metadata.st_size:
        raise NativePreparationError('keg_mismatch')
    return data


def _receipt(keg):
    try:
        value = json.loads(_read(Path(keg) / 'INSTALL_RECEIPT.json', 65536))
        if type(value) is not dict or type(value.get('source')) is not dict or type(value.get('built_on')) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise NativePreparationError('keg_mismatch') from None


def _redacted(receipt):
    """Provenance record without the receipt's private formula/cache path."""
    record = json.loads(json.dumps(receipt))
    if 'path' in record['source']:
        record['source']['path'] = 'private-machine-path-redacted; evidence hash retained'
    return record


def check_kegs(tdlib_keg, openssl_keg, row):
    """The exact approved kegs: names, receipts and the pinned dylib file names."""
    tdlib_keg, openssl_keg = Path(tdlib_keg), Path(openssl_keg)
    tdlib, openssl = _receipt(tdlib_keg), _receipt(openssl_keg)
    source, versions = tdlib['source'], tdlib['source'].get('versions')
    dependency = [d for d in tdlib.get('runtime_dependencies') or () if type(d) is dict and d.get('full_name') == 'openssl@3']
    if (tdlib_keg.name != 'HEAD-' + row.source_commit[:7] or source.get('scm_revision') != row.source_commit
            or type(versions) is not dict or versions.get('head') != tdlib_keg.name
            or openssl_keg.name != row.openssl_version or openssl_keg.parent.name != 'openssl@3'
            or type(openssl['source'].get('versions')) is not dict
            or openssl['source']['versions'].get('stable') != row.openssl_version
            or len(dependency) != 1 or dependency[0].get('version') != row.openssl_version
            or tdlib.get('arch') != 'arm64' or openssl.get('arch') != 'arm64'):
        raise NativePreparationError('keg_mismatch')
    return tdlib, openssl, {'libtdjson.dylib': tdlib_keg / 'lib' / f'libtdjson.{row.tdlib_version}.dylib',
                            'libssl.3.dylib': openssl_keg / 'lib/libssl.3.dylib',
                            'libcrypto.3.dylib': openssl_keg / 'lib/libcrypto.3.dylib'}


def check_schema(tdlib_source, row):
    data = _schema_bytes(tdlib_source)
    if hashlib.sha256(data).hexdigest() != row.schema_sha256:
        raise NativePreparationError('schema_mismatch')


def _schema_bytes(tdlib_source):
    try:
        return _read(Path(tdlib_source) / SCHEMA, MAX_SCHEMA)
    except NativePreparationError:
        raise NativePreparationError('schema_mismatch') from None


def load_commands(data):
    """Load-command ids of a thin 64-bit Mach-O; bounded."""
    try:
        magic, _, _, _, ncmds, size = struct.unpack_from('<6I', data, 0)
        if magic != 0xfeedfacf or ncmds > 256 or 32 + size > len(data):
            raise NativePreparationError('dependency_mismatch')
        commands, offset = [], 32
        for _ in range(ncmds):
            command, length = struct.unpack_from('<2I', data, offset)
            if length < 8 or offset + length > 32 + size:
                raise NativePreparationError('dependency_mismatch')
            commands.append(command)
            offset += length
        return commands
    except struct.error:
        raise NativePreparationError('dependency_mismatch') from None


def _metadata(path):
    data = path.read_bytes()
    if SEARCH_PATH_COMMANDS & set(load_commands(data)):
        raise NativePreparationError('search_path_command')
    with path.open('rb') as stream:
        try:
            return _macho_metadata(stream, len(data))
        except NativeRuntimeError:
            raise NativePreparationError('dependency_mismatch') from None


def _changes(dependencies, openssl_keg):
    """Every non-system dependency must be the pinned OpenSSL keg's, rewritten to @loader_path."""
    changes = []
    for dependency in dependencies:
        if dependency in SYSTEM_LIBRARIES:
            continue
        name = dependency.rsplit('/', 1)[-1]
        if name not in ('libssl.3.dylib', 'libcrypto.3.dylib') or dependency not in (
                OPENSSL_OPT + name, str(Path(openssl_keg) / 'lib' / name)):
            raise NativePreparationError('dependency_mismatch')
        changes += ['-change', dependency, '@loader_path/' + name]
    return changes


def _tool(runner, command):
    try:
        result = runner(command, capture_output=True, check=False, timeout=TOOL_TIMEOUT, env=TOOL_ENV)
    except (OSError, subprocess.TimeoutExpired):
        raise NativePreparationError('tool_failed') from None
    if result.returncode:
        raise NativePreparationError('tool_failed')


def _digest(path):
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def lock_document(row, artifacts, tdlib_receipt):
    """The v1 lock, key order and formatting exactly as committed."""
    built = tdlib_receipt['built_on']
    return {
        'manifest_version': 1, 'target': 'macos-arm64', 'platform': 'Darwin', 'architecture': 'arm64',
        'minimum_os': row.minimum_os, 'source_commit': row.source_commit, 'tdlib_version': row.tdlib_version,
        'schema_sha256': row.schema_sha256, 'abi': row.abi, 'required_symbols': list(REQUIRED_SYMBOLS),
        'signature': 'adhoc-local-integrity-only', 'artifacts': artifacts,
        'build': {'method': row.build_method, 'reproducibility': 'unverified', 'compiler': tdlib_receipt.get('compiler'),
                  'build_os': built.get('os_version'), 'xcode': built.get('xcode'), 'clt': built.get('clt'),
                  'used_options': tdlib_receipt.get('used_options'), 'known_formula_flags': list(KNOWN_FORMULA_FLAGS),
                  'implicit_flags': 'homebrew-std_cmake_args-not-captured', 'openssl_version': row.openssl_version},
        'notices': {'status': 'pending-public-distribution-review', 'tdlib_license': 'BSL-1.0',
                    'tdlib_source': 'https://github.com/tdlib/td', 'tdlib_license_source': row.tdlib_license_source,
                    'openssl_license': 'Apache-2.0', 'source_rebuild': 'pending', 'publisher_signature': 'unavailable'},
    }


def lock_bytes(document):
    return (json.dumps(document, indent=2) + '\n').encode()


def differences(actual, expected, prefix=''):
    """Dotted paths of every differing field; documents both sides' shapes, not values."""
    if type(actual) is not type(expected):
        return [prefix or '.']
    if isinstance(actual, dict):
        return [p for key in sorted(set(actual) | set(expected))
                for p in (differences(actual[key], expected[key], f'{prefix}.{key}'.lstrip('.'))
                          if key in actual and key in expected else [f'{prefix}.{key}'.lstrip('.')])]
    if isinstance(actual, list) and len(actual) == len(expected):
        return [p for i, (a, e) in enumerate(zip(actual, expected)) for p in differences(a, e, f'{prefix}[{i}]')]
    return [] if actual == expected else [prefix or '.']


def _write(path, data, mode=0o400):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
        os.fchmod(out.fileno(), mode)


def prepare_native(output, *, tdlib_source, tdlib_keg=TDLIB_KEG, openssl_keg=OPENSSL_KEG,
                   committed=None, runner=subprocess.run, probe=True):
    """Build one private native directory and its lock; returns a nonsecret summary."""
    row = APPROVED_PINS[0]
    output = Path(output)
    if not output.is_absolute() or '..' in output.parts or output.exists() or output.is_symlink():
        raise NativePreparationError('output_refused')
    tdlib_receipt, openssl_receipt, inputs = check_kegs(tdlib_keg, openssl_keg, row)
    check_schema(tdlib_source, row)
    try:
        output.mkdir(mode=0o700)
        for name in ('native', 'notices', 'notices/openssl-' + row.openssl_version, 'notices/tdlib', 'provenance'):
            (output / name).mkdir(mode=0o700)
    except OSError:
        raise NativePreparationError('output_refused') from None
    artifacts = []
    for name in NAMES:
        target = output / 'native' / name
        _write(target, _read(inputs[name], MAX_DYLIB), 0o600)
        dependencies, _, _ = _metadata(target)
        _tool(runner, ['/usr/bin/install_name_tool', '-id', '@loader_path/' + name,
                       *_changes(dependencies, openssl_keg), str(target)])
        _tool(runner, ['/usr/bin/codesign', '--force', '--sign', '-', str(target)])
        dependencies, symbols, minimum = _metadata(target)
        if ({d for d in dependencies if d.startswith('@loader_path/')} != LOADER[name]
                or any(d not in SYSTEM_LIBRARIES and not d.startswith('@loader_path/') for d in dependencies)
                or len(set(dependencies)) != len(dependencies) or minimum != row.minimum_os
                or name == 'libtdjson.dylib' and not set(REQUIRED_SYMBOLS) <= symbols):
            raise NativePreparationError('dependency_mismatch')
        os.chmod(target, 0o500)
        size, sha256 = _digest(target)
        artifacts.append({'name': name, 'bytes': size, 'sha256': sha256, 'dependencies': dependencies})
    document = lock_document(row, artifacts, tdlib_receipt)
    data = lock_bytes(document)
    try:
        lock = parse_native_manifest(data)
    except NativeRuntimeError:
        raise NativePreparationError('native_validation_failed') from None
    _write(output / 'tdlib-lock.json', data)
    for name in ('LICENSE.txt', 'AUTHORS.md', 'README.md'):
        _write(output / 'notices' / ('openssl-' + row.openssl_version) / name, _read(Path(openssl_keg) / name, 1 << 20))
    _write(output / 'notices/tdlib/LICENSE_1_0.txt', _read(Path(tdlib_source) / 'LICENSE_1_0.txt', 1 << 20))
    for label, keg, receipt in (('tdlib', tdlib_keg, tdlib_receipt), ('openssl', openssl_keg, openssl_receipt)):
        raw = _read(Path(keg) / 'INSTALL_RECEIPT.json', 65536)
        _write(output / 'provenance' / f'{label}-build.json', (json.dumps(
            {'input_sha256': hashlib.sha256(raw).hexdigest(), 'record': _redacted(receipt)}, sort_keys=True) + '\n').encode())
    try:
        validate_native(lock, 'macos-arm64', directory=output / 'native', probe=probe)
    except NativeRuntimeError:
        raise NativePreparationError('native_validation_failed') from None
    summary = {'lock_sha256': hashlib.sha256(data).hexdigest(), 'probed': probe,
               'artifacts': [{k: a[k] for k in ('name', 'bytes', 'sha256')} for a in artifacts]}
    if committed is not None:
        expected = json.loads(Path(committed).read_bytes())
        summary['reproduces_committed_lock'] = data == Path(committed).read_bytes()
        summary['differing_fields'] = differences(document, expected)
    return summary


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Reproduce tdlib-lock.json from the pinned Homebrew kegs.')
    parser.add_argument('--output-directory', required=True)
    parser.add_argument('--tdlib-source', required=True, help='exported TDLib source tree at the pinned commit')
    parser.add_argument('--tdlib-keg', default=str(TDLIB_KEG))
    parser.add_argument('--openssl-keg', default=str(OPENSSL_KEG))
    parser.add_argument('--compare', default=str(Path(__file__).resolve().parent / 'tdlib-lock.json'))
    args = parser.parse_args()
    try:
        result = prepare_native(Path(args.output_directory), tdlib_source=Path(args.tdlib_source),
                                tdlib_keg=Path(args.tdlib_keg), openssl_keg=Path(args.openssl_keg), committed=args.compare)
    except NativePreparationError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True))
