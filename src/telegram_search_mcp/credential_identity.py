"""Which API identity a session uses: the owner's Keychain pair or the bundled one.

Setup, connect and the broker share these rules. A session is bound to the source
and fingerprint it was enrolled with; a changed identity refuses, never re-binds.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from typing import Callable

from .credentials import CredentialStoreError, SecretValue
from .keychain import ApiCredentials

SOURCES = frozenset({'keychain', 'bundled'})
_CODES = frozenset({'app_identity_missing', 'identity_changed', 'credential_conflict',
                    'credential_invalid_record', 'credential_denied', 'credential_locked',
                    'credential_unavailable'})


class CredentialIdentityError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _CODES else 'credential_unavailable'
        super().__init__(self.code)


def _fail(code):
    error = CredentialIdentityError(code)
    error.__context__ = error.__cause__ = None
    raise error


def identity_fingerprint(credentials: ApiCredentials) -> str:
    return hashlib.sha256(f'{credentials.api_id}:{credentials.api_hash}'.encode('ascii')).hexdigest()


def valid_record(name: str, value: object) -> bool:
    if type(value) is not SecretValue:
        return False
    data = value.reveal()
    if name == 'api_id':
        return len(data) <= 10 and data.isdigit() and data.isascii() and 0 < int(data) <= 2147483647
    return len(data) == 32 and all(byte in b'0123456789abcdefABCDEF' for byte in data)


@dataclass(frozen=True, repr=False)
class ResolvedIdentity:
    credentials: ApiCredentials
    source: str

    @property
    def fingerprint(self) -> str:
        return identity_fingerprint(self.credentials)

    def __repr__(self):
        return f'ResolvedIdentity({self.source})'


def resolve_new(store, bundled_loader: Callable[[], ApiCredentials | None]) -> ResolvedIdentity:
    """For a new enrollment. A complete Keychain pair is the owner's override."""
    code = None
    try:
        values = {name: store.get(name) for name in ('api_id', 'api_hash')}
    except CredentialStoreError as error:
        code = 'credential_' + error.code
    if code:
        _fail(code)
    present = {name: value for name, value in values.items() if value is not None}
    if any(not valid_record(name, value) for name, value in present.items()):
        _fail('credential_invalid_record')
    if len(present) == 2:
        return ResolvedIdentity(ApiCredentials(int(present['api_id'].reveal()),
                                               present['api_hash'].reveal().decode('ascii')), 'keychain')
    if present:
        _fail('credential_conflict')
    bundled = bundled_loader()
    if bundled is None:
        _fail('app_identity_missing')
    return ResolvedIdentity(bundled, 'bundled')


def resolve_enrolled(record: dict, *, keychain_loader: Callable[[], ApiCredentials],
                     bundled_loader: Callable[[], ApiCredentials | None],
                     database_key: bytes = b'') -> ApiCredentials:
    """Records written before 0.52 carry no source: they are Keychain sessions."""
    source = record.get('credential_source', 'keychain')
    if source not in SOURCES:
        _fail('identity_changed')
    credentials = keychain_loader() if source == 'keychain' else bundled_loader()
    if credentials is None:
        _fail('app_identity_missing')
    expected = record.get('identity_fingerprint')
    if expected is not None and identity_fingerprint(credentials) != expected:
        _fail('identity_changed')
    return ApiCredentials(credentials.api_id, credentials.api_hash, database_key)


def session_credential_loader(paths=None, store=None, *, keychain_loader=None, bundled_loader=None):
    """Broker boundary, called under the session lease: (database key, lazy credentials).

    Without an enrollment this is the legacy owner path: empty key, Keychain pair.
    """
    from .enrollment import EnrollmentStore, SetupPaths, managed_key
    if keychain_loader is None:
        from .keychain import read_api_credentials as keychain_loader
    if bundled_loader is None:
        from .app_identity import bundled_app_identity as bundled_loader
    paths = SetupPaths() if paths is None else paths
    record = EnrollmentStore(paths).read()
    if record is None:
        def legacy():
            pair = keychain_loader()
            return ApiCredentials(pair.api_id, pair.api_hash, b'')
        return b'', legacy
    if store is None:
        from .credentials import MacOSSecretStore
        store = MacOSSecretStore()
    key = managed_key(record, store).reveal()
    return key, lambda: resolve_enrolled(record, keychain_loader=keychain_loader,
                                         bundled_loader=bundled_loader, database_key=key)


def preflight_credentials(paths=None, *, keychain_loader=None, bundled_loader=None) -> ApiCredentials:
    """Legacy install preflight: the resolved pair only, never the database key (no lease)."""
    from .enrollment import EnrollmentStore, SetupPaths
    if keychain_loader is None:
        from .keychain import read_api_credentials as keychain_loader
    if bundled_loader is None:
        from .app_identity import bundled_app_identity as bundled_loader
    record = EnrollmentStore(SetupPaths() if paths is None else paths).read()
    if record is None:
        return keychain_loader()
    return resolve_enrolled(record, keychain_loader=keychain_loader, bundled_loader=bundled_loader)
