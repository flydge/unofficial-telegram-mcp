"""Audited no-follow/ACL primitives for owned bundled installation paths.

Adapted from the historical portable installer; no profile/environment selector.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import errno
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
from collections.abc import Mapping

_ID = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}\Z')


class RuntimePathError(ValueError):
    """An unsafe local path or incomplete trusted selection, without private values."""


class MissingRuntimePath(RuntimePathError):
    """A safely traversed local metadata path has not been created yet."""


class ChangedRuntimePath(RuntimePathError):
    """Security metadata or a path binding changed while it was being checked: no answer about the object."""


class UnsupportedRuntimePlatform(RuntimePathError):
    """The portable runtime has no approved artifact for this target."""


def validate_identifier(value: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise RuntimePathError('profile and installation identifiers must be bounded safe IDs')
    return value


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path) or len(os.fsencode(path)) > 4096:
        raise RuntimePathError('portable paths must be absolute without traversal')
    return path


def _safe_metadata(path: Path, metadata: os.stat_result, *, final: bool = False,
                   private: bool = False, regular: bool = False) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimePathError('refusing a symlink in portable storage')
    if metadata.st_uid not in ({os.getuid()} if final else {0, os.getuid()}):
        raise RuntimePathError('portable storage owner is unsafe')
    mode = stat.S_IMODE(metadata.st_mode)
    # This one system-owned sticky boundary holds secure per-user socket dirs.
    sticky_temp = path == Path('/private/tmp') and metadata.st_uid == 0 and mode == 0o1777
    if mode & 0o022 and not sticky_temp:
        raise RuntimePathError('portable storage permissions are unsafe')
    if regular:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimePathError('portable metadata must be a single-link regular file')
    elif not stat.S_ISDIR(metadata.st_mode):
        raise RuntimePathError('portable storage ancestor is not a directory')
    if private and mode != 0o700:
        raise RuntimePathError('portable storage directory must be owner-only')


@lru_cache(maxsize=1)
def _darwin_acl_api():
    """Bind documented read-only libSystem ACL metadata APIs, never Keychain."""
    try:
        api = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        for name, arguments, result in (
            ('acl_get_fd_np', [ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            ('acl_valid', [ctypes.c_void_p], ctypes.c_int),
            ('acl_get_entry', [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            ('acl_get_tag_type', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            ('acl_free', [ctypes.c_void_p], ctypes.c_int),
        ):
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        return api
    except (OSError, AttributeError):
        raise RuntimePathError('portable ACL metadata is unavailable') from None


def _descriptor_acl_snapshot(descriptor: int) -> None:
    """Reject extended allow entries even when POSIX permissions look private.

    Darwin ACL_TYPE_EXTENDED is 0x100; its iterator returns zero for an entry
    and -1/EINVAL at the end. Missing ACL metadata is NULL/ENOENT. A deny-only
    ACL (including the normal home-directory deny-delete entry) is preserved.
    Other hosts retain the POSIX path checks used by the inert test harness.

    acl_get_fd_np reads the ACL in one fstatx_np call, so its entries are one
    consistent state. Across that read a directory keeps its binding identity
    (device, inode, owner, group, mode, flags); its timestamps, size and link
    count change with every unrelated child created or removed, which is not a
    security change. A regular file keeps its whole identity.
    """
    if sys.platform != 'darwin':
        return
    api = _darwin_acl_api()
    before_metadata = os.fstat(descriptor)
    before = file_metadata_identity(before_metadata)
    ctypes.set_errno(0)
    acl = api.acl_get_fd_np(descriptor, 0x100)
    if not acl:
        if ctypes.get_errno() != errno.ENOENT:
            raise RuntimePathError('portable ACL metadata is unavailable')
    else:
        try:
            if api.acl_valid(acl) != 0:
                raise RuntimePathError('portable ACL metadata is unsafe')
            for position in range(129):
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                result = api.acl_get_entry(acl, 0 if position == 0 else -1, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    break
                tag = ctypes.c_int()
                if (result != 0 or position == 128 or not entry.value
                        or api.acl_get_tag_type(entry, ctypes.byref(tag)) != 0
                        or tag.value != 2):
                    raise RuntimePathError('portable storage ACL permissions are unsafe')
        finally:
            if api.acl_free(acl) != 0:
                raise RuntimePathError('portable ACL metadata is unavailable')
    after_metadata = os.fstat(descriptor)
    if (_path_binding_identity(before_metadata) != _path_binding_identity(after_metadata)
            or not stat.S_ISDIR(before_metadata.st_mode)
            and before != file_metadata_identity(after_metadata)):
        raise ChangedRuntimePath('portable storage security metadata changed')


def _validate_descriptor_acl(descriptor: int) -> None:
    # One atomic ACL read on this descriptor, its binding identity unchanged around it (the launcher's admission
    # reads it the same way). An ancestor's timestamps change with unrelated children (another app writing beside
    # it), so they are no reason to refuse: a busy ~/Library/Application Support never refuses an install.
    # Allow/API errors and a changed binding fail immediately.
    admitted = _path_binding_identity(os.fstat(descriptor))
    _descriptor_acl_snapshot(descriptor)
    if _path_binding_identity(os.fstat(descriptor)) != admitted:
        raise ChangedRuntimePath('portable storage security metadata changed')


def _path_binding_identity(metadata: os.stat_result) -> tuple:
    if stat.S_ISDIR(metadata.st_mode):
        return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
                metadata.st_mode, getattr(metadata, 'st_flags', 0))
    return file_metadata_identity(metadata)


def validate_owned_path(path: Path, *, regular: bool = False,
                        private: bool = False, allow_missing: bool = True) -> None:
    """Inspect exact ancestry without following symlinks or changing metadata."""
    path = _absolute(path)
    components = [Path(path.anchor), *reversed(list(path.parents)[:-1]), path]
    # parents excludes the leaf and includes /; preserve unique root-to-leaf order.
    components = list(dict.fromkeys(components))
    owned_chain = False
    for component in components:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise RuntimePathError('portable storage is unavailable') from None
        except OSError:
            raise RuntimePathError('portable storage metadata is unavailable') from None
        owned_chain = owned_chain or metadata.st_uid == os.getuid()
        _safe_metadata(component, metadata, final=component == path or owned_chain,
                       private=private and component == path,
                       regular=regular and component == path)
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not (regular and component == path):
                flags |= os.O_DIRECTORY
            descriptor = os.open(component, flags)
            if _path_binding_identity(metadata) != _path_binding_identity(os.fstat(descriptor)):
                raise ChangedRuntimePath('portable storage binding changed')
            _validate_descriptor_acl(descriptor)
            if _path_binding_identity(component.lstat()) != _path_binding_identity(metadata):
                raise ChangedRuntimePath('portable storage binding changed')
        except OSError:
            raise RuntimePathError('portable ACL metadata is unsafe or unavailable') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def open_owned_directory(path: Path, *, private: bool = False):
    """Hold a no-follow descriptor to the owner-safe directory binding."""
    path = _absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path(path.anchor)
    owned_chain = False
    try:
        _safe_metadata(current, os.fstat(descriptor), final=current == path,
                       private=private and current == path)
        _validate_descriptor_acl(descriptor)
        for part in path.parts[1:]:
            current /= part
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(child)
            owned_chain = owned_chain or metadata.st_uid == os.getuid()
            _safe_metadata(current, metadata, final=current == path or owned_chain,
                           private=private and current == path)
            _validate_descriptor_acl(descriptor)
            _safe_metadata(current, os.fstat(descriptor), final=current == path or owned_chain,
                           private=private and current == path)
        yield descriptor
    except FileNotFoundError:
        raise MissingRuntimePath('portable metadata has not been created') from None
    except OSError:
        raise RuntimePathError('portable metadata is unsafe or unavailable') from None
    finally:
        os.close(descriptor)


@contextmanager
def open_owned_file(path: Path, *, private_parent: bool = False):
    """Open a regular file through held no-follow directory descriptors."""
    path = _absolute(path)
    file_fd = None
    try:
        with open_owned_directory(path.parent, private=private_parent) as directory_fd:
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            _safe_metadata(path, os.fstat(file_fd), final=True, regular=True)
            _validate_descriptor_acl(file_fd)
            _safe_metadata(path, os.fstat(file_fd), final=True, regular=True)
            with os.fdopen(file_fd, 'rb') as stream:
                file_fd = None
                yield stream
    except FileNotFoundError:
        raise MissingRuntimePath('portable metadata has not been created') from None
    except OSError:
        raise RuntimePathError('portable metadata is unsafe or unavailable') from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


def file_metadata_identity(metadata: os.stat_result) -> tuple:
    """Include security metadata and mutation times, excluding read-only atime."""
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns, metadata.st_mode, metadata.st_uid, metadata.st_gid,
            metadata.st_nlink)


def object_binding(path: Path, *, directory=False) -> dict:
    """Stable no-follow publication identity; content is checked separately.

    Atomic rename may change ctime and directory membership changes size/nlink.
    Those values are not ownership anchors. This cooperative identity does not
    protect against a hostile process controlling the same OS UID.
    """
    if directory:
        with open_owned_directory(path,private=True) as fd:
            m=os.fstat(fd)
    else:
        with open_owned_file(path) as stream:
            m=os.fstat(stream.fileno())
    value={'kind':'directory' if directory else 'file','dev':m.st_dev,'ino':m.st_ino,
           'uid':m.st_uid,'mode':stat.S_IMODE(m.st_mode)}
    if not directory:value.update(bytes=m.st_size,nlink=m.st_nlink)
    return value


def require_object_binding(path: Path, binding: dict) -> None:
    if type(binding) is not dict or binding.get('kind') not in {'directory','file'}:
        raise RuntimePathError('invalid publication binding')
    if object_binding(path,directory=binding['kind']=='directory')!=binding:
        raise RuntimePathError('foreign publication object')


def read_owned_file(path: Path, *, max_bytes: int, mode: int | None = None,
                    private_parent: bool = False) -> bytes:
    """Read bounded owner-safe metadata, rejecting concurrent file mutation."""
    if type(max_bytes) is not int or not 0 < max_bytes <= 65536:
        raise RuntimePathError('portable metadata read boundary is invalid')
    with open_owned_file(path, private_parent=private_parent) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_size > max_bytes or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode):
            raise RuntimePathError('portable metadata exceeds its size or permission boundary')
        data = stream.read(max_bytes+1)
        after = os.fstat(stream.fileno())
        if len(data) != metadata.st_size or file_metadata_identity(metadata) != file_metadata_identity(after):
            raise RuntimePathError('portable metadata changed during reading')
        return data


def ensure_owned_private_directory(path: Path, *, private_leaf: bool = True) -> Path:
    """Create missing private components safely; refuse unsafe existing metadata.

    `private_leaf=False` is for client-owned roots (Codex home, skills roots): an
    existing leaf must be the user's and not group/world-writable, but need not be
    0700. Components this call creates are always 0700.
    """
    path = _absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path(path.anchor)
    owned_chain = False
    try:
        _safe_metadata(current, os.fstat(descriptor), final=current == path,
                       private=private_leaf and current == path)
        _validate_descriptor_acl(descriptor)
        for part in path.parts[1:]:
            current /= part
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            else:
                # Only this freshly created directory may have its umask-reduced
                # mode corrected; existing/foreign directories are never chmodded.
                created = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(created.st_mode) or created.st_uid != os.getuid():
                    raise RuntimePathError('new portable directory binding changed')
                os.chmod(part, 0o700, dir_fd=descriptor, follow_symlinks=False)
                secured = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (created.st_dev, created.st_ino) != (secured.st_dev, secured.st_ino):
                    raise RuntimePathError('new portable directory binding changed')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                metadata = os.fstat(child)
                owned_chain = owned_chain or metadata.st_uid == os.getuid()
                _safe_metadata(current, metadata, final=current == path or owned_chain, private=private_leaf and current == path)
                _validate_descriptor_acl(child)
                _safe_metadata(current, os.fstat(child), final=current == path or owned_chain,
                               private=private_leaf and current == path)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return path
    except OSError:
        raise RuntimePathError('unable to prepare owner-only portable storage') from None
    finally:
        os.close(descriptor)
