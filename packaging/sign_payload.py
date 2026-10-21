"""Sign every Mach-O of a prepared payload inside out (CLIENTS-5).

Development host only; stdlib only. The input is a sealed prepare_payload.py tree, never changed: the
signed tree is a fresh copy. Mach-O files are found by magic (thin 64/32-bit and universal), never by
name, and must equal the payload's provenance/macho-inventory.json. They are signed dylibs first, then
bundles, then executables, python/bin/python3 last, each with
`codesign --force --sign <identity> --identifier <stable id> --options runtime [--timestamp]`: the
identifier is product_identity.CODESIGN_ID_PREFIX plus `python3` for the interpreter (a designated
requirement that stays stable across releases, which keeps Keychain ACLs valid) or plus the relative
path. Only python3 carries entitlements, from packaging/entitlements/: python3.plist for Developer ID,
python3-adhoc.plist for the ad hoc identity `-`. The entitlement matrix re-signs a scratch interpreter
with each candidate set and runs the probes; a build accepts only the plist that equals the smallest
passing set. allow-dyld-environment-variables, get-task-allow and disabled page protection are refused
everywhere. With `-` there is no --timestamp, and native/ keeps its pinned bytes: an ad-hoc v2 native
lock admits only the unsigned pins. Every result is verified (`codesign --verify --strict`, plus the
Team ID requirement for Developer ID) and recorded in provenance/signing.json: path, pre- and post-sign
sha256, cdhash, identifier, flags. Refusals are fixed codes; no path or tool output is echoed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import secrets
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENTITLEMENTS = {'developer-id': Path(__file__).resolve().parent / 'entitlements/python3.plist',
                'adhoc': Path(__file__).resolve().parent / 'entitlements/python3-adhoc.plist'}
CODES = frozenset({'output_refused', 'payload_refused', 'identity_refused', 'macho_refused', 'inventory_mismatch',
                   'identifier_refused', 'entitlement_forbidden', 'entitlement_refused', 'sign_failed',
                   'verify_failed', 'matrix_failed', 'signing_refused'})
INTERPRETER = 'python/bin/python3'
MACHO = {b'\xcf\xfa\xed\xfe': 'mach-o-64', b'\xce\xfa\xed\xfe': 'mach-o-32', b'\xca\xfe\xba\xbe': 'universal',
         b'\xca\xfe\xba\xbf': 'universal-64'}  # the prepare_payload.py inventory table
MH_EXECUTE, MH_DYLIB, MH_BUNDLE = 2, 6, 8
RANK = {MH_DYLIB: 0, MH_BUNDLE: 1, MH_EXECUTE: 2}  # inside out; python3 is sorted last of all
CS_RUNTIME = 0x10000
# Entitlements an interpreter may be granted after the matrix proves them; everything else refuses.
ALLOWED = frozenset({'com.apple.security.cs.disable-library-validation',
                     'com.apple.security.cs.allow-unsigned-executable-memory',
                     'com.apple.security.cs.allow-jit'})
FORBIDDEN = frozenset({'com.apple.security.cs.allow-dyld-environment-variables',
                       'com.apple.security.get-task-allow',
                       'com.apple.security.cs.disable-executable-page-protection'})
# Smallest first: the first candidate under which every probe passes is the proven minimum.
CANDIDATES = ((), ('com.apple.security.cs.disable-library-validation',),
              ('com.apple.security.cs.allow-unsigned-executable-memory',), ('com.apple.security.cs.allow-jit',),
              ('com.apple.security.cs.disable-library-validation',
               'com.apple.security.cs.allow-unsigned-executable-memory'))
PROBES = ('distributions', 'tdlib', 'security', 'subprocess')
# Need the owner's launchd GUI domain (a LaunchAgent and the transient connect job): owner session only.
DEFERRED = ('launch_agent_broker', 'connect_helper')
TEAM = re.compile(r'[A-Z0-9]{10}')
SHA1 = re.compile(r'[0-9A-F]{40}')
CN = re.compile(r'Developer ID Application: [^\x00-\x1f\x7f"]{1,160} \(([A-Z0-9]{10})\)')
PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*')
MAX_FILES = 20000
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_OUTPUT = 1 << 20
TOOL_TIMEOUT = 300
PROBE_TIMEOUT = 300
CHUNK = 1024 * 1024
PROBE = r'''
import ctypes, importlib, json, subprocess, sys
from pathlib import Path
root, version = Path(sys.argv[1]), sys.argv[2]
sys.path[:0] = [str(root / 'app'), str(root / 'installer')]
out = {}
def probe(name, function):
    try:
        out[name] = 'ok' if function() is True else 'failed'
    except BaseException as error:
        out[name] = 'failed:' + type(error).__name__
def distributions():
    count = 0
    for wheel in json.loads((root / 'provenance/components.json').read_bytes())['wheels']:
        tops = set()
        for line in (root / wheel['destination'] / wheel['dist_info'] / 'RECORD').read_text().splitlines():
            name = line.split(',')[0]
            first = name.split('/')[0]
            if first.endswith(('.dist-info', '.data')) or first == '__pycache__':
                continue
            if '/' in name and name.split('/')[1] == '__init__.py':
                tops.add(first)
            elif '/' not in name and name.endswith(('.py', '.so')):
                tops.add(name.split('.')[0])
        for module in sorted(tops):
            importlib.import_module(module)
            count += 1
    import telegram_search_mcp.server
    return count > 0
def tdlib():
    library = ctypes.CDLL(str(root / 'native/libtdjson.dylib'))
    execute = library.td_json_client_execute
    execute.argtypes, execute.restype = [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_char_p
    return json.loads(execute(None, b'{"@type":"getOption","name":"version"}'))['value'] == version
def security():
    from telegram_search_mcp.credentials import _SecurityBackend
    native = _SecurityBackend()
    owned = [native.string(sys.argv[3])]
    query = native.dictionary([(native.constant('kSecClass'), native.constant('kSecClassGenericPassword')),
                               (native.constant('kSecAttrService'), owned[0]),
                               (native.constant('kSecMatchLimit'), native.constant('kSecMatchLimitOne'))])
    owned.append(query)
    try:
        status, result = native.copy_matching(query)
        if result:
            owned.append(result)
        return status == -25300
    finally:
        for item in reversed(owned):
            native.release(item)
def spawn():
    result = subprocess.run(['/usr/bin/osascript', '-e', 'return "ok"'], capture_output=True, text=True, timeout=60,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    return result.returncode == 0 and result.stdout.strip() == 'ok'
for name, function in (('distributions', distributions), ('tdlib', tdlib), ('security', security),
                       ('subprocess', spawn)):
    probe(name, function)
print(json.dumps(out, sort_keys=True))
'''


class SigningError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'signing_refused')


def _need(condition, code):
    if not condition:
        raise SigningError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def _tool_env(identity):
    """codesign needs the login Keychain only for a real identity."""
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    if identity != '-' and os.environ.get('HOME', '').startswith('/'):
        env['HOME'] = os.environ['HOME']
    return env


# Identity, identifiers, entitlements ---------------------------------------------------------------

def identity_kind(identity, team_id):
    """'adhoc' for `-`; 'developer-id' for a SHA-1 or Developer ID Application name with its Team ID."""
    if identity == '-':
        _need(team_id is None, 'identity_refused')
        return 'adhoc'
    _need(type(identity) is str and type(team_id) is str and TEAM.fullmatch(team_id) is not None, 'identity_refused')
    named = CN.fullmatch(identity)
    _need(SHA1.fullmatch(identity) is not None or (named is not None and named.group(1) == team_id), 'identity_refused')
    return 'developer-id'


def identifier(relative, prefix):
    """The stable codesign identifier of one payload path."""
    if relative == INTERPRETER:
        return prefix + '.python3'
    parts = [part.lstrip('.') for part in relative.split('/')]
    _need(all(PART.fullmatch(part) for part in parts), 'identifier_refused')
    value = prefix + '.' + '.'.join(parts)
    _need(len(value) <= 255, 'identifier_refused')
    return value


def load_entitlements(data):
    """The closed entitlement grammar: a dict of ALLOWED keys set to true; FORBIDDEN keys refuse."""
    try:
        value = plistlib.loads(data)
    except Exception:  # plistlib raises several types for malformed input; none is forwarded.
        raise SigningError('entitlement_refused') from None
    _need(type(value) is dict, 'entitlement_refused')
    _need(not set(value) & FORBIDDEN, 'entitlement_forbidden')
    _need(set(value) <= ALLOWED and all(item is True for item in value.values()), 'entitlement_refused')
    return frozenset(value)


def entitlement_plist(keys):
    _need(not set(keys) & FORBIDDEN, 'entitlement_forbidden')
    _need(set(keys) <= ALLOWED, 'entitlement_refused')
    return plistlib.dumps({key: True for key in sorted(keys)}, fmt=plistlib.FMT_XML, sort_keys=True)


# Mach-O ------------------------------------------------------------------------------------------

def _filetype(header):
    _need(len(header) >= 16 and header[:4] in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'), 'macho_refused')
    filetype = int.from_bytes(header[12:16], 'little')
    _need(filetype in RANK, 'macho_refused')
    return filetype


def macho(path):
    """(kind, filetype) of a Mach-O found by magic, None for any other file; a malformed one refuses."""
    with open(path, 'rb', buffering=0) as stream:
        head = stream.read(8 + 8 * 32)
        kind = MACHO.get(head[:4])
        if kind is None:
            return None
        if not kind.startswith('universal'):
            return kind, _filetype(head)
        count = int.from_bytes(head[4:8], 'big')
        if not 0 < count <= 8:  # Not a fat Mach-O (a Java class file shares the magic): prepare_payload's rule.
            return None
        size = 20 if kind == 'universal' else 32
        types = set()
        for index in range(count):
            entry = head[8 + index * size:8 + (index + 1) * size]
            _need(len(entry) == size, 'macho_refused')
            offset = int.from_bytes(entry[8:12] if size == 20 else entry[8:16], 'big')
            stream.seek(offset)
            types.add(_filetype(stream.read(16)))
        _need(len(types) == 1, 'macho_refused')
        return kind, types.pop()


# Tree copy ---------------------------------------------------------------------------------------

def _safe_relative(name):
    path = PurePosixPath(name)
    return (0 < len(name.encode()) <= 1024 and not path.is_absolute() and '\\' not in name
            and all(part not in ('', '.', '..') for part in name.split('/'))
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _fresh(output):
    output = Path(output)
    _need(output.is_absolute() and '..' not in output.parts and not output.exists() and not output.is_symlink()
          and output.parent.is_dir() and not output.parent.is_symlink(), 'output_refused')
    _need(not any(output == p or output.is_relative_to(p) for p in (Path('/tmp'), Path('/private/tmp'))),
          'output_refused')
    return output


def _walk(root):
    """{relative: executable} of a payload of regular files and private directories only."""
    root = Path(root)
    _need(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'payload_refused')
    files, total = {}, 0
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            _need(not (Path(base) / name).is_symlink(), 'payload_refused')
        for name in names:
            path = Path(base) / name
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            _need(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid() and metadata.st_nlink == 1
                  and not metadata.st_mode & 0o022 and metadata.st_size <= MAX_FILE and _safe_relative(relative),
                  'payload_refused')
            total += metadata.st_size
            files[relative] = bool(metadata.st_mode & 0o100)
            _need(len(files) <= MAX_FILES and total <= MAX_TOTAL, 'payload_refused')
    return dict(sorted(files.items()))


def _copy(source, target, mode=0o600):
    """One no-follow copy into a new file, owner-writable while it is signed (sealed afterwards)."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream, \
            open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'payload_refused')
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            out.write(chunk)
    os.chmod(target, mode)


def tree_digest(root, files):
    rows = (f'{name}\0{"x" if files[name] else "-"}\0{sha256((Path(root) / name).read_bytes())}\n' for name in sorted(files))
    return sha256(''.join(rows).encode())


def _seal(root, files):
    for name, executable in files.items():
        os.chmod(Path(root) / name, 0o500 if executable else 0o400)
    for base, _, _ in os.walk(root):
        os.chmod(base, 0o700)


# codesign ----------------------------------------------------------------------------------------

def _run(runner, argv, identity, code):
    try:
        result = runner(argv, capture_output=True, check=False, timeout=TOOL_TIMEOUT, env=_tool_env(identity))
    except (OSError, subprocess.SubprocessError):
        raise SigningError(code) from None
    _need(result.returncode == 0 and len(result.stdout or b'') <= MAX_OUTPUT and len(result.stderr or b'') <= MAX_OUTPUT,
          code)
    return result


def sign_command(path, ident, identity, entitlements=None, *, timestamp=True):
    argv = ['/usr/bin/codesign', '--force', '--sign', identity, '--identifier', ident, '--options', 'runtime']
    if identity != '-':
        argv.append('--timestamp' if timestamp else '--timestamp=none')
    if entitlements is not None:
        argv += ['--entitlements', str(entitlements)]
    return argv + [str(path)]


def _text(data):
    return data.decode('utf-8', 'replace') if type(data) is bytes else (data or '')


def describe(path, runner, identity):
    """(identifier, cdhash, flags, team) from `codesign -d --verbose=4`, a closed parse."""
    result = _run(runner, ['/usr/bin/codesign', '-d', '--verbose=4', str(path)], identity, 'verify_failed')
    fields = {}
    for line in _text(result.stderr).splitlines():
        key, _, value = line.partition('=')
        if key in ('Identifier', 'CDHash', 'TeamIdentifier') and key not in fields:
            fields[key] = value
        elif key == 'CodeDirectory v' and 'flags' not in fields:
            found = re.search(r' flags=0x([0-9a-f]{1,8})\(', value)
            _need(found is not None, 'verify_failed')
            fields['flags'] = int(found.group(1), 16)
    _need(set(fields) == {'Identifier', 'CDHash', 'TeamIdentifier', 'flags'}
          and re.fullmatch(r'[0-9a-f]{40}', fields['CDHash']) is not None, 'verify_failed')
    return fields['Identifier'], fields['CDHash'], fields['flags'], fields['TeamIdentifier']


def signed_entitlements(path, runner, identity):
    result = _run(runner, ['/usr/bin/codesign', '-d', '--entitlements', '-', '--xml', str(path)], identity,
                  'verify_failed')
    data = result.stdout if type(result.stdout) is bytes else _text(result.stdout).encode()
    return frozenset() if not data.strip() else load_entitlements(data.strip())


def verify(path, runner, identity, team_id):
    from telegram_search_mcp.native_runtime import designated_requirement
    requirement = [] if team_id is None else ['-R', designated_requirement(team_id)]
    _run(runner, ['/usr/bin/codesign', '--verify', '--strict', *requirement, str(path)], identity, 'verify_failed')


# Signing -----------------------------------------------------------------------------------------

def inventory(root, files):
    """Every Mach-O by magic, in signing order: dylibs, bundles, executables, python3 last."""
    found = []
    for relative in files:
        kind = macho(Path(root) / relative)
        if kind is not None:
            found.append((relative == INTERPRETER, RANK[kind[1]], relative, kind[0], kind[1]))
    return [{'path': path, 'kind': kind, 'filetype': filetype} for _, _, path, kind, filetype in sorted(found)]


def _expected_inventory(payload):
    try:
        value = json.loads((Path(payload) / 'provenance/macho-inventory.json').read_bytes())
        return {f['path']: f['sha256'] for f in value['files']}
    except (OSError, ValueError, KeyError, TypeError):
        raise SigningError('inventory_mismatch') from None


def sign_payload(payload, output, *, identity, team_id=None, prefix=None, entitlements=None, runner=subprocess.run):
    """Write one fresh signed copy of `payload`; returns a nonsecret summary."""
    try:
        return _sign_payload(Path(payload), Path(output), identity, team_id, prefix, entitlements, runner)
    except SigningError:
        raise
    except Exception:  # The last boundary: never a traceback, a path or tool text.
        raise SigningError('signing_refused') from None


def _sign_payload(payload, output, identity, team_id, prefix, entitlements, runner):
    kind = identity_kind(identity, team_id)
    if prefix is None:
        from telegram_search_mcp.product_identity import CODESIGN_ID_PREFIX as prefix
    granted_path = ENTITLEMENTS[kind] if entitlements is None else Path(entitlements)
    granted = load_entitlements(granted_path.read_bytes())
    output = _fresh(output)
    files = _walk(payload)
    _need(INTERPRETER in files and files[INTERPRETER] and 'provenance/signing.json' not in files, 'payload_refused')
    expected = _expected_inventory(payload)
    found = inventory(payload, files)
    _need({f['path'] for f in found} == set(expected), 'inventory_mismatch')
    identifiers = {f['path']: identifier(f['path'], prefix) for f in found}
    _need(len(set(identifiers.values())) == len(identifiers), 'identifier_refused')
    output.mkdir(mode=0o700)  # Kept on a later failure, for diagnosis.
    for relative in files:
        _copy(payload / relative, output / relative)
    records = []
    for entry in found:
        relative, path = entry['path'], output / entry['path']
        before = path.read_bytes()
        _need(sha256(before) == expected[relative], 'inventory_mismatch')
        pinned = kind == 'adhoc' and relative.startswith('native/')
        if not pinned:
            plist = granted_path if relative == INTERPRETER and granted else None
            _run(runner, sign_command(path, identifiers[relative], identity, plist), identity, 'sign_failed')
        verify(path, runner, identity, None if pinned else team_id)
        ident, cdhash, flags, team = describe(path, runner, identity)
        if not pinned:
            _need(ident == identifiers[relative] and flags & CS_RUNTIME
                  and team == (team_id if kind == 'developer-id' else 'not set'), 'verify_failed')
            _need(signed_entitlements(path, runner, identity) == (granted if relative == INTERPRETER else frozenset()),
                  'verify_failed')
        after = path.read_bytes()
        _need(not pinned or after == before, 'verify_failed')
        records.append({'path': relative, 'kind': entry['kind'], 'filetype': entry['filetype'],
                        'action': 'kept_pinned' if pinned else 'signed', 'identifier': ident, 'cdhash': cdhash,
                        'runtime': bool(flags & CS_RUNTIME), 'pre_sha256': sha256(before), 'post_sha256': sha256(after),
                        'bytes': len(after)})
    document = {'schema_version': 1, 'identity_kind': kind, 'team_id': team_id, 'identifier_prefix': prefix,
                'interpreter_entitlements': sorted(granted), 'entitlements_sha256': sha256(granted_path.read_bytes()),
                'counted_by': 'magic', 'total': len(records),
                'signed': sum(r['action'] == 'signed' for r in records),
                'kept_pinned': sum(r['action'] == 'kept_pinned' for r in records), 'files': records}
    data = json_bytes(document)
    with open(os.open(output / 'provenance/signing.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
              'wb') as out:
        out.write(data)
    files['provenance/signing.json'] = False
    _seal(output, files)
    _need(_walk(output) == files, 'signing_refused')
    return {'identity_kind': kind, 'team_id': team_id, 'total': len(records), 'signed': document['signed'],
            'kept_pinned': document['kept_pinned'], 'signing_sha256': sha256(data),
            'interpreter_entitlements': sorted(granted), 'tree_sha256': tree_digest(output, files)}


# Entitlement matrix ------------------------------------------------------------------------------

def _probe(python, signed, version, home, runner):
    """One probe run under `env -i` with a synthetic HOME; the Keychain query names no existing item."""
    service = 'entitlement-matrix-probe-' + secrets.token_hex(8)
    try:
        result = runner([str(python), '-I', '-S', '-B', '-c', PROBE, str(signed), version, service], capture_output=True,
                        check=False, timeout=PROBE_TIMEOUT, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(home)})
    except (OSError, subprocess.SubprocessError):
        return {name: 'failed:spawn' for name in PROBES}
    try:
        value = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= MAX_OUTPUT else None
    except ValueError:
        value = None
    if type(value) is not dict or set(value) != set(PROBES) or not all(type(v) is str and len(v) <= 64
                                                                        for v in value.values()):
        return {name: 'failed:probe' for name in PROBES}
    return dict(sorted(value.items()))


def entitlement_matrix(signed, scratch, *, identity, team_id=None, prefix=None, version='1.8.67', runner=subprocess.run):
    """Re-sign a scratch copy of the signed interpreter with each candidate and run the probes.

    Returns the table and the smallest passing set; the scratch directory is fresh and kept.
    """
    try:
        return _matrix(Path(signed), Path(scratch), identity, team_id, prefix, version, runner)
    except SigningError:
        raise
    except Exception:
        raise SigningError('matrix_failed') from None


def _matrix(signed, scratch, identity, team_id, prefix, version, runner):
    kind = identity_kind(identity, team_id)
    if prefix is None:
        from telegram_search_mcp.product_identity import CODESIGN_ID_PREFIX as prefix
    scratch = _fresh(scratch)
    files = _walk(signed)
    _need(INTERPRETER in files, 'payload_refused')
    scratch.mkdir(mode=0o700)
    for relative in files:  # The interpreter's own tree; app/, native/ and provenance/ stay in `signed`.
        if relative.startswith('python/'):
            _copy(signed / relative, scratch / relative, 0o700 if relative == INTERPRETER else 0o600)
    python, home = scratch / INTERPRETER, scratch / 'home'
    home.mkdir(mode=0o700)
    rows = []
    for candidate in CANDIDATES:
        plist = None
        if candidate:
            plist = scratch / 'candidate.plist'
            plist.unlink(missing_ok=True)
            plist.write_bytes(entitlement_plist(candidate))
        _run(runner, sign_command(python, prefix + '.python3', identity, plist, timestamp=False), identity,
             'matrix_failed')
        _need(signed_entitlements(python, runner, identity) == frozenset(candidate), 'matrix_failed')
        probes = _probe(python, signed, version, home, runner)
        rows.append({'entitlements': list(candidate), 'probes': probes,
                     'passed': all(value == 'ok' for value in probes.values())})
    minimal = next((row['entitlements'] for row in rows if row['passed']), None)
    return {'schema_version': 1, 'identity_kind': kind, 'team_id': team_id, 'probes': list(PROBES),
            'deferred_probes': list(DEFERRED), 'candidates': rows, 'minimal': minimal}


def check_matrix(result, kind, entitlements=None):
    """The committed plist for `kind` must equal the matrix's smallest passing set."""
    granted = load_entitlements((ENTITLEMENTS[kind] if entitlements is None else Path(entitlements)).read_bytes())
    _need(type(result) is dict and result.get('identity_kind') == kind and result.get('minimal') is not None
          and frozenset(result['minimal']) == granted, 'matrix_failed')
    return sorted(granted)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Sign every Mach-O of a prepared payload (a fresh copy).')
    commands = parser.add_subparsers(dest='command', required=True)
    sign = commands.add_parser('sign')
    sign.add_argument('--payload', required=True, help='sealed prepare_payload.py output (never changed)')
    sign.add_argument('--output', required=True, help='fresh signed tree')
    matrix = commands.add_parser('matrix')
    matrix.add_argument('--signed', required=True, help='a sign_payload.py output')
    matrix.add_argument('--scratch', required=True, help='fresh scratch directory')
    for command in (sign, matrix):
        command.add_argument('--identity', required=True, help='"-" (ad hoc) or the Developer ID Application identity')
        command.add_argument('--team-id', help='Team ID of a Developer ID identity')
    args = parser.parse_args()
    if str(ROOT / 'src') not in sys.path:
        sys.path.insert(0, str(ROOT / 'src'))
    try:
        if args.command == 'sign':
            result = sign_payload(Path(args.payload), Path(args.output), identity=args.identity, team_id=args.team_id)
        else:
            result = entitlement_matrix(Path(args.signed), Path(args.scratch), identity=args.identity,
                                        team_id=args.team_id)
    except SigningError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True))
