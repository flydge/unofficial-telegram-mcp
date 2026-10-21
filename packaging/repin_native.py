"""Write the v2 native lock for re-signed natives (CLIENTS-4).

Development host only; never part of a bundle. Inputs: HEAD's v1 packaging/tdlib-lock.json (the
unsigned pins; never the working tree's), the unsigned directory those pins describe
(prepare_native's native/) and the directory of signed outputs. Every unsigned input must equal its
v1 pin. Each signed output must carry a code signature that verifies, of any identity, ad hoc `-`
included: which identity a release may carry is native_runtime's designated-requirement check at
load time, not this script's. The v2 document is derived from the v1 lock alone (shipped bytes and
sha256 from the signed outputs, unsigned bytes and sha256 from the v1 pins, signature kind and Team
ID) and written only after native_runtime's grammar admits it under one reviewed APPROVED_PINS
row; nothing is hand-edited. Refusals are fixed codes; no path or tool output is echoed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from telegram_search_mcp.native_runtime import (NAMES, NativeManifest, NativeManifestV2, NativeRuntimeError,
                                                designated_requirement, lock_signature, parse_native_manifest)

ROOT = Path(__file__).resolve().parents[1]
LOCK = 'packaging/tdlib-lock.json'
MAX_DYLIB = 32 * 1024 * 1024
MAX_LOCK = 65536
TOOL_TIMEOUT = 120
TOOL_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
CODES = frozenset({'output_refused', 'lock_invalid', 'unsigned_mismatch', 'signed_refused', 'signature_invalid',
                   'pin_not_approved'})


class RepinError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'pin_not_approved')


def _need(condition, code):
    if not condition:
        raise RepinError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def head_lock(checkout=ROOT):
    """HEAD's committed native lock bytes."""
    try:
        data = subprocess.run(['/usr/bin/git', '-C', str(checkout), 'show', 'HEAD:' + LOCK], capture_output=True,
                              check=True, timeout=TOOL_TIMEOUT, env=TOOL_ENV).stdout
    except (OSError, subprocess.SubprocessError):
        raise RepinError('lock_invalid') from None
    _need(len(data) <= MAX_LOCK, 'lock_invalid')
    return data


def _read(path, code):
    """A bounded regular non-symlink file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise RepinError(code) from None
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        _need(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_DYLIB, code)
        data = stream.read(MAX_DYLIB + 1)
    _need(len(data) == metadata.st_size, code)
    return data


def _exact_names(directory, code):
    try:
        names = sorted(p.name for p in Path(directory).iterdir())
    except OSError:
        raise RepinError(code) from None
    _need(names == sorted(NAMES), code)


def _signed(path, runner, team_id=None):
    """One signed output: its signature verifies, against the Team ID requirement for a Developer ID lock,
    and its bytes do not change meanwhile."""
    data = _read(path, 'signed_refused')
    requirement = [] if team_id is None else ['-R', designated_requirement(team_id)]
    try:
        result = runner(['/usr/bin/codesign', '--verify', '--strict', *requirement, str(path)], capture_output=True,
                        check=False, timeout=TOOL_TIMEOUT, env=TOOL_ENV)
    except (OSError, subprocess.TimeoutExpired):
        raise RepinError('signature_invalid') from None
    _need(result.returncode == 0, 'signature_invalid')
    _need(_read(path, 'signed_refused') == data, 'signed_refused')
    return data


def v2_document(v1, signed, team_id):
    """The v2 lock from the v1 lock alone, key order kept; `signed` maps name -> shipped bytes."""
    kind = 'adhoc' if team_id is None else 'developer-id'
    document = {}
    for key, value in v1.items():
        document[key] = value
        if key == 'signature':
            document.update(signature=kind, team_id=team_id)
    document['manifest_version'] = 2
    document['artifacts'] = [{'name': a['name'], 'bytes': len(signed[a['name']]), 'sha256': sha256(signed[a['name']]),
                              'unsigned_bytes': a['bytes'], 'unsigned_sha256': a['sha256'],
                              'dependencies': a['dependencies']} for a in v1['artifacts']]
    document['notices'] = dict(v1['notices'], publisher_signature='unavailable' if team_id is None else 'developer-id')
    return document


def lock_bytes(document):
    return (json.dumps(document, indent=2) + '\n').encode()


def repin_native(output, *, v1_lock, unsigned_directory, signed_directory, team_id=None, runner=subprocess.run):
    """Write one new v2 lock file; returns a nonsecret summary. `team_id` None keeps the ad-hoc kind."""
    output = Path(output)
    _need(team_id is None or (type(team_id) is str and re.fullmatch(r'[A-Z0-9]{10}', team_id) is not None),
          'pin_not_approved')
    _need(output.is_absolute() and '..' not in output.parts and not output.exists() and not output.is_symlink()
          and output.parent.is_dir(), 'output_refused')
    try:
        _need(type(parse_native_manifest(v1_lock)) is NativeManifest, 'lock_invalid')
        v1 = json.loads(v1_lock)
    except (NativeRuntimeError, ValueError):
        raise RepinError('lock_invalid') from None
    _exact_names(unsigned_directory, 'unsigned_mismatch')
    _exact_names(signed_directory, 'signed_refused')
    for artifact in v1['artifacts']:
        data = _read(Path(unsigned_directory) / artifact['name'], 'unsigned_mismatch')
        _need((len(data), sha256(data)) == (artifact['bytes'], artifact['sha256']), 'unsigned_mismatch')
    signed = {name: _signed(Path(signed_directory) / name, runner, team_id) for name in NAMES}
    data = lock_bytes(v2_document(v1, signed, team_id))
    try:
        lock = parse_native_manifest(data)
    except NativeRuntimeError:
        raise RepinError('pin_not_approved') from None
    _need(type(lock) is NativeManifestV2 and lock_signature(lock) == ('adhoc' if team_id is None else 'developer-id',
                                                                      team_id), 'pin_not_approved')
    try:
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), 0o400)
    except OSError:
        raise RepinError('output_refused') from None
    kind, team = lock_signature(lock)
    return {'lock_sha256': sha256(data), 'publisher': {'kind': kind, 'team_id': team},
            'artifacts': [{'name': a.name, 'bytes': a.bytes, 'sha256': a.sha256, 'unsigned_sha256': a.unsigned_sha256}
                          for a in lock.artifacts]}


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Write the v2 native lock for re-signed natives from HEAD's v1 lock.")
    parser.add_argument('--output', required=True, help='new v2 lock file')
    parser.add_argument('--unsigned-directory', required=True, help="prepare_native.py's native/ directory")
    parser.add_argument('--signed-directory', required=True, help='the same three dylibs after signing')
    parser.add_argument('--team-id', help='Developer ID Team ID; omitted keeps the ad-hoc kind (outputs unchanged)')
    parser.add_argument('--checkout', default=str(ROOT), help='checkout whose HEAD holds the v1 lock')
    args = parser.parse_args()
    try:
        result = repin_native(Path(args.output), v1_lock=head_lock(args.checkout),
                              unsigned_directory=Path(args.unsigned_directory),
                              signed_directory=Path(args.signed_directory), team_id=args.team_id)
    except RepinError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True))
