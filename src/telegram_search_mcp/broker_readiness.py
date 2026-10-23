"""The broker's recorded readiness (0.55 spec 3C): what `status --account` reads instead of asking for a check.

Once an alias registry exists, a broker records the outcome of its own account check in `run/readiness.json`, a
closed record `{"version": 1, "generation": "<broker generation>", "authorized": true|false}`: true when the session
is authorized and its account passed the pin, false when it is not authorized or the pin refused it. The broker
writes it only on a change, atomically and owner-only, and removes it when the outcome is unknown. Status reads it
beside the handshake it already makes; the record counts only while its generation is the running broker's, so a
record an earlier broker left reads unknown. Never a user id or a pin code. Without a registry nothing is written.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from .session_owner import OwnerLeaseError, open_private_directory

NAME = 'readiness.json'
MAX_BYTES = 256
_KEYS = frozenset({'version', 'generation', 'authorized'})


def record(run: Path, generation: str, authorized: bool | None) -> bool:
    """Replace the record (an exclusive temporary, fsync, rename), or remove it for an unknown outcome.

    Best effort, never raises: False when the record could not be written, after an attempt to remove it, so a
    failed write leaves no record (unknown) rather than an earlier outcome.
    """
    try:
        directory = open_private_directory(Path(run))
    except OwnerLeaseError:
        return False
    try:
        if authorized is None:
            return _remove(directory)
        data = json.dumps({'version': 1, 'generation': generation, 'authorized': authorized},
                          sort_keys=True, separators=(',', ':')).encode('ascii')
        temporary = f'.{NAME}.{secrets.token_hex(16)}.tmp'
        created = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                 | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=directory)
            created = True
            try:
                os.fchmod(descriptor, 0o600)
                if os.write(descriptor, data) != len(data):
                    raise OSError('short write')
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(temporary, NAME, src_dir_fd=directory, dst_dir_fd=directory)
            created = False
            os.fsync(directory)
            return True
        except (OSError, ValueError):
            _remove(directory)
            return False
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
    finally:
        os.close(directory)


def _remove(directory: int) -> bool:
    try:
        os.unlink(NAME, dir_fd=directory)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def read(run: Path, generation: object) -> bool | None:
    """The recorded `authorized` for the running broker's `generation`, or None (unknown). Never raises.

    Anything but an owner-only, unlinked regular file holding the closed record of that generation is unknown.
    """
    if type(generation) is not str:
        return None
    try:
        directory = open_private_directory(Path(run))
    except OwnerLeaseError:
        return None
    try:
        descriptor = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0),
                             dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > MAX_BYTES):
            return None
        value = json.loads(os.read(descriptor, MAX_BYTES + 1).decode('ascii'))
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)
    if (type(value) is not dict or set(value) != _KEYS or value['version'] != 1 or type(value['version']) is not int
            or value['generation'] != generation or type(value['authorized']) is not bool):
        return None
    return value['authorized']
