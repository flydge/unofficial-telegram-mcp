"""Developer-managed application API identity shipped inside an admitted bundle.

It identifies the application, not a user: each user still authorizes their own
account into their own session. Distributed client parameters are extractable by
design. Source checkouts carry none and keep using the owner's own Keychain pair.
"""
from __future__ import annotations
import json
import os
import stat

from .bundle_paths import MissingRuntimePath, RuntimePathError, file_metadata_identity, open_owned_file
from .keychain import ApiCredentials

IDENTITY_FILE = 'app-identity.json'
_MAX_BYTES = 4096


class AppIdentityError(RuntimeError):
    def __init__(self):
        self.code = 'app_identity_invalid'
        super().__init__(self.code)


def _invalid():
    error = AppIdentityError()
    error.__context__ = error.__cause__ = None
    raise error


def parse_app_identity(data: bytes) -> ApiCredentials:
    failed = False
    try:
        if type(data) is not bytes or len(data) > _MAX_BYTES:
            raise ValueError()
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError()
                out[key] = value
            return out
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'version', 'api_id', 'api_hash'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['api_id']) is not int or not 0 < value['api_id'] <= 2147483647
                or type(value['api_hash']) is not str or len(value['api_hash']) != 32
                or any(char not in '0123456789abcdefABCDEF' for char in value['api_hash'])):
            raise ValueError()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        failed = True
    if failed:
        _invalid()
    return ApiCredentials(value['api_id'], value['api_hash'])


def bundled_app_identity() -> ApiCredentials | None:
    """Only the admitted, re-checked release tree can supply an identity."""
    from . import bundle_runtime
    selection = bundle_runtime._SELECTION
    if selection is None:
        return None
    selection.check()  # Every indexed file, including this one, is re-authenticated.
    failed = False
    try:
        with open_owned_file(selection.root / IDENTITY_FILE) as stream:
            before = os.fstat(stream.fileno())
            if stat.S_IMODE(before.st_mode) != 0o400 or before.st_size > _MAX_BYTES:
                raise ValueError()
            data = stream.read(_MAX_BYTES + 1)
            if len(data) != before.st_size or file_metadata_identity(before) != file_metadata_identity(
                    os.fstat(stream.fileno())):
                raise ValueError()
    except MissingRuntimePath:
        return None
    except (OSError, RuntimePathError, ValueError):
        failed = True
    if failed:
        _invalid()
    return parse_app_identity(data)
