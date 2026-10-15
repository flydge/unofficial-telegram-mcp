"""Private metadata binding for a new encrypted default TDLib session.

This is not an account registry or authorization proof. Existing unrecognized
databases are never adopted, migrated or rekeyed. Missing managed keys refuse.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat
from .config import APP_SUPPORT_ROOT
from .credential_identity import CredentialIdentityError
from .credentials import SecretValue, CredentialStoreError
from .session_owner import open_private_directory, OwnerLeaseError


class EnrollmentError(RuntimeError):
    def __init__(self, code='enrollment_inconsistent'):
        self.code = code if code in {'enrollment_inconsistent', 'legacy_unmanaged',
            'enrollment_missing', 'managed_key_missing'} else 'enrollment_inconsistent'
        super().__init__(self.code)


@dataclass(frozen=True)
class SetupPaths:
    """The CLI has no path override. Explicit paths are an inert test boundary."""
    root: Path = APP_SUPPORT_ROOT
    @property
    def session(self): return self.root / 'tdlib'
    @property
    def lock(self): return self.root / 'run' / 'broker.lock'
    @property
    def policy(self): return self.root / 'runtime.toml'
    @property
    def record(self): return self.root / 'enrollment.json'


_FIELDS = {'version', 'uid', 'enrollment_id', 'session', 'root_device', 'root_inode',
           'session_device', 'session_inode', 'key_account', 'state'}
# 0.52 binds the API identity source. Records written earlier have neither field
# and remain Keychain sessions; both fields appear together or not at all.
_IDENTITY_FIELDS = {'credential_source', 'identity_fingerprint'}


class EnrollmentStore:
    def __init__(self, paths: SetupPaths):
        self.paths = paths

    def _binding(self, record, root_fd):
        if (type(record) is not dict or set(record) not in (_FIELDS, _FIELDS | _IDENTITY_FIELDS)
                or (_IDENTITY_FIELDS <= set(record) and (
                    record['credential_source'] not in {'keychain', 'bundled'}
                    or type(record['identity_fingerprint']) is not str
                    or re.fullmatch('[0-9a-f]{64}', record['identity_fingerprint']) is None))
                or type(record['version']) is not int
                or record['version'] != 1 or type(record['uid']) is not int or record['uid'] != os.getuid()
                or type(record['enrollment_id']) is not str
                or re.fullmatch('[0-9a-f]{32}', record['enrollment_id']) is None
                or record['key_account'] != 'database_key_' + record['enrollment_id']
                or record['session'] != str(self.paths.session)
                or record['state'] not in {'pending', 'ready'}
                or any(type(record[field]) is not int or record[field] < 0 for field in
                       ('root_device', 'root_inode', 'session_device', 'session_inode'))):
            raise EnrollmentError()
        session_fd = open_private_directory(self.paths.session)
        try:
            root, session = os.fstat(root_fd), os.fstat(session_fd)
            if ((root.st_dev, root.st_ino) != (record['root_device'], record['root_inode'])
                    or (session.st_dev, session.st_ino) != (record['session_device'], record['session_inode'])):
                raise EnrollmentError()
            marker = os.open('.managed-enrollment', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=session_fd)
            try:
                metadata = os.fstat(marker)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_size != 33
                        or os.read(marker, 34) != (record['enrollment_id'] + '\n').encode('ascii')):
                    raise EnrollmentError()
            finally:
                os.close(marker)
        finally:
            os.close(session_fd)

    @staticmethod
    def _has_marker(root_fd):
        try:
            session = os.open('tdlib', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        try:
            try:
                os.stat('.managed-enrollment', dir_fd=session, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False
        finally:
            os.close(session)

    def read(self):
        """Bounded no-follow read, including parent/session inode binding."""
        if not self.paths.root.exists() and not self.paths.root.is_symlink():
            return None
        root_fd = descriptor = -1
        try:
            root_fd = open_private_directory(self.paths.root)
            try:
                descriptor = os.open(self.paths.record.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=root_fd)
            except FileNotFoundError:
                if self._has_marker(root_fd):
                    raise EnrollmentError('enrollment_missing')
                return None
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or not 0 < metadata.st_size <= 4096):
                raise EnrollmentError()
            data = os.read(descriptor, 4097)
            if len(data) != metadata.st_size:
                raise EnrollmentError()
            visible = os.stat(self.paths.record.name, dir_fd=root_fd, follow_symlinks=False)
            after = os.fstat(descriptor)
            if ((visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino)
                    or (after.st_size, after.st_mtime_ns) != (metadata.st_size, metadata.st_mtime_ns)):
                raise EnrollmentError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            record = json.loads(data, object_pairs_hook=unique)
            self._binding(record, root_fd)
            return record
        except (OSError, OwnerLeaseError, ValueError, TypeError, UnicodeError, KeyError):
            raise EnrollmentError() from None
        finally:
            if descriptor >= 0: os.close(descriptor)
            if root_fd >= 0: os.close(root_fd)

    def require_new(self):
        """Names only; even an unrecognized empty subdirectory means legacy."""
        if self.read() is not None:
            return
        if self.paths.session.exists() or self.paths.session.is_symlink():
            try:
                descriptor = open_private_directory(self.paths.session)
                try:
                    with os.scandir(descriptor) as entries:
                        if next(entries, None) is not None:
                            raise EnrollmentError('legacy_unmanaged')
                finally:
                    os.close(descriptor)
            except OwnerLeaseError:
                raise EnrollmentError('legacy_unmanaged') from None

    def _write(self, record, *, replace=False):
        descriptor = open_private_directory(self.paths.root)
        temporary = '.enrollment-' + secrets.token_hex(16)
        created = False
        try:
            self._binding(record, descriptor)
            prior = None
            if replace:
                self.read()
                prior = os.stat(self.paths.record.name, dir_fd=descriptor, follow_symlinks=False)
            data = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
            if len(data) > 4096: raise EnrollmentError()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            created = True
            try:
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                os.fsync(fd)
            finally:
                os.close(fd)
            if replace:
                now = os.stat(self.paths.record.name, dir_fd=descriptor, follow_symlinks=False)
                if (now.st_dev, now.st_ino, now.st_mtime_ns) != (prior.st_dev, prior.st_ino, prior.st_mtime_ns):
                    raise EnrollmentError()
                os.replace(temporary, self.paths.record.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                created = False
            else:
                # Link publishes a complete durable record without replacing an existing name.
                os.link(temporary, self.paths.record.name, src_dir_fd=descriptor, dst_dir_fd=descriptor,
                        follow_symlinks=False)
            if created:
                os.unlink(temporary, dir_fd=descriptor)
                created = False
            os.fsync(descriptor)
        except (OSError, OwnerLeaseError):
            raise EnrollmentError() from None
        finally:
            if created:
                os.unlink(temporary, dir_fd=descriptor)
            os.close(descriptor)

    def create(self, store, bound=None):
        self.require_new()
        if self.read() is not None:
            raise EnrollmentError()
        root_fd = open_private_directory(self.paths.root, create=True)
        session_fd = -1
        try:
            session_fd = open_private_directory(self.paths.session, create=True)
            root, session = os.fstat(root_fd), os.fstat(session_fd)
            identity = secrets.token_hex(16)
            record = dict(version=1, uid=os.getuid(), enrollment_id=identity,
                session=str(self.paths.session), root_device=root.st_dev, root_inode=root.st_ino,
                session_device=session.st_dev, session_inode=session.st_ino,
                key_account='database_key_' + identity, state='pending')
            if bound is not None:
                record.update(credential_source=bound.source, identity_fingerprint=bound.fingerprint)
            key = SecretValue(secrets.token_bytes(32))
            # Key first: a crash leaves an unused orphan, never an opened unkeyed database.
            store.put(record['key_account'], key)
            marker = os.open('.managed-enrollment', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=session_fd)
            try:
                with os.fdopen(marker, 'wb', closefd=False) as stream:
                    stream.write((identity + '\n').encode('ascii'))
                    stream.flush()
                os.fsync(marker)
                os.fsync(session_fd)
            finally:
                os.close(marker)
            self._write(record)
            return record, key
        finally:
            os.close(root_fd)
            if session_fd >= 0: os.close(session_fd)

    def set_state(self, state, bound=None):
        """`bound` adds the identity binding to a pre-0.52 record in the same write.

        An existing binding never changes: a different identity refuses.
        """
        record = self.read()
        if record is None or state not in {'pending', 'ready'}:
            raise EnrollmentError()
        updated = dict(record, state=state)
        if bound is not None:
            if 'credential_source' in record:
                if (record['credential_source'], record['identity_fingerprint']) != (bound.source, bound.fingerprint):
                    raise CredentialIdentityError('identity_changed')
            else:
                updated.update(credential_source=bound.source, identity_fingerprint=bound.fingerprint)
        self._write(updated, replace=True)


def managed_key(record, store):
    value = store.get(record['key_account'])
    if value is None:
        raise EnrollmentError('managed_key_missing')
    if type(value) is not SecretValue or len(value.reveal()) != 32:
        raise CredentialStoreError('invalid_record')
    return value


def load_database_key(paths=None, store=None) -> bytes:
    """Broker calls under its session lease; legacy default remains empty."""
    paths = SetupPaths() if paths is None else paths
    record = EnrollmentStore(paths).read()
    if record is None:
        return b''
    if store is None:
        from .credentials import MacOSSecretStore
        store = MacOSSecretStore()
    return managed_key(record, store).reveal()
