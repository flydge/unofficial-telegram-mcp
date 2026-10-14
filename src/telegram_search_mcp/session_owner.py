"""Stable, no-follow, nonblocking ownership shared by broker and setup."""
from __future__ import annotations
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys

# Deliberately no destructor/atexit close. Only OS process exit releases an
# uncertain owner's fd; preserving the owner reference also defeats normal GC.
_PROCESS_LIFETIME_OWNERS = {}


class OwnerLeaseError(RuntimeError):
    def __init__(self, code='owner_unsafe'):
        self.code = code if code in {'owner_busy', 'owner_unsafe'} else 'owner_unsafe'
        super().__init__(self.code)


def open_private_directory(path: Path, *, create=False) -> int:
    """Walk every component without following links; never chmod existing data."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OwnerLeaseError()
    # macOS publishes these root-owned aliases. Canonicalize only the exact
    # verified system prefix; user-controlled symlink components still refuse.
    if sys.platform == 'darwin' and len(path.parts) > 1 and path.parts[1] in {'var', 'tmp'}:
        alias = Path('/') / path.parts[1]
        try:
            metadata = alias.lstat()
            expected = 'private/' + path.parts[1]
            if metadata.st_uid != 0 or not stat.S_ISLNK(metadata.st_mode) or os.readlink(alias) != expected:
                raise OwnerLeaseError()
            path = Path('/private') / path.parts[1] / Path(*path.parts[2:])
        except OSError:
            raise OwnerLeaseError() from None
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                            | getattr(os, 'O_CLOEXEC', 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OwnerLeaseError()
        result, descriptor = descriptor, -1
        return result
    except OSError:
        raise OwnerLeaseError() from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def acquire_owner_lock(path: Path) -> int:
    parent = open_private_directory(path.parent, create=True)
    descriptor = -1
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
        try:
            descriptor = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            descriptor = os.open(path.name, flags, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise OwnerLeaseError()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OwnerLeaseError('owner_busy' if error.errno in {errno.EACCES, errno.EAGAIN}
                                  else 'owner_unsafe') from None
        visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OwnerLeaseError()
        result, descriptor = descriptor, -1
        return result
    except OSError:
        raise OwnerLeaseError() from None
    finally:
        os.close(parent)
        if descriptor >= 0:
            os.close(descriptor)


class SessionOwnerLease:
    def __init__(self, path: Path):
        self.path, self.descriptor = Path(path), None
        self._retained = False

    def __enter__(self):
        if self.descriptor is not None:
            raise OwnerLeaseError('owner_busy')
        self.descriptor = acquire_owner_lock(self.path)
        try:
            self.check()
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise
        return self

    def check(self):
        parent = open_private_directory(self.path.parent)
        try:
            held = os.fstat(self.descriptor)
            visible = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino) or held.st_nlink != 1:
                raise OwnerLeaseError()
        except (OSError, TypeError):
            raise OwnerLeaseError() from None
        finally:
            os.close(parent)

    def __exit__(self, *_):
        if self.descriptor is not None and not self._retained:
            os.close(self.descriptor)
            self.descriptor = None

    def retain_until_process_exit(self, owner):
        if self.descriptor is None:
            raise OwnerLeaseError()
        self._retained = True
        _PROCESS_LIFETIME_OWNERS[self.descriptor] = (self, owner)
