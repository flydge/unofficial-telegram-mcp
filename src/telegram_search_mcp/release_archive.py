"""Development/test-only extraction against a caller-authenticated file index.

Index authenticity belongs to the caller. A checksum alone is not publisher trust.
Failed private stages are retained for the installer's recoverable quarantine.
This helper is not the shipping bootstrap verifier. Its Python tar/gzip policy
is broader and member hashes are checked while writing the private stage. The
shipping native seed preflights every canonical member before extraction.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zlib

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 10000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(?:\.dev(0|[1-9][0-9]{0,5}))?\Z')


class ReleaseArchiveError(ValueError):
    """Redacted integrity or ownership failure before registration."""


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    path: str
    bytes: int
    sha256: str
    executable: bool


@dataclass(frozen=True, slots=True)
class ArchiveIndex:
    schema_version: int
    target: str
    release_version: str
    archive_bytes: int
    archive_sha256: str
    unpacked_bytes: int
    files: tuple[ArchiveFile, ...]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    files: int
    bytes: int


class _ReleaseTarInfo(tarfile.TarInfo):
    @classmethod
    def frombuf(cls, buf, encoding, errors):
        if len(buf) != tarfile.BLOCKSIZE or buf[257:265] != b'ustar\0' + b'00':
            raise ReleaseArchiveError('unsupported release archive header')
        member = super().frombuf(buf, encoding, errors)
        # Refuse extension headers before tarfile reads their variable payload.
        # The producer uses USTAR; links, PAX/GNU metadata and sparse data are not
        # part of the consumer format, even if an extension appears harmless.
        if (member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                or not 0 <= member.size <= MAX_MEMBER_BYTES):
            raise ReleaseArchiveError('unsupported release archive member')
        return member


def _name(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 1024
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in ('', '.', '..') or len(p.encode('utf-8')) > 255 for p in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        raise ValueError
    return value


def _integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError
    return value


def _checked(value: object) -> ArchiveIndex:
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'target', 'release_version', 'archive_bytes',
            'archive_sha256', 'unpacked_bytes', 'files'}:
        raise ValueError
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['target'] != 'macos-arm64'
            or not isinstance(value['release_version'], str) or not _VERSION.fullmatch(value['release_version'])
            or not isinstance(value['archive_sha256'], str) or not _HASH.fullmatch(value['archive_sha256'])):
        raise ValueError
    archive_bytes = _integer(value['archive_bytes'], 1, MAX_ARCHIVE_BYTES)
    unpacked_bytes = _integer(value['unpacked_bytes'], 0, MAX_UNPACKED_BYTES)
    raw_files = value['files']
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_FILES:
        raise ValueError
    files = []
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {'path', 'bytes', 'sha256', 'executable'}:
            raise ValueError
        name = _name(raw['path'])
        size = _integer(raw['bytes'], 0, MAX_MEMBER_BYTES)
        if (not isinstance(raw['sha256'], str) or not _HASH.fullmatch(raw['sha256'])
                or type(raw['executable']) is not bool):
            raise ValueError
        files.append(ArchiveFile(name, size, raw['sha256'], raw['executable']))
    names = [f.path for f in files]
    if names != sorted(names) or len(set(names)) != len(names) or sum(f.bytes for f in files) != unpacked_bytes:
        raise ValueError
    names_set = set(names)
    if any(str(parent) in names_set for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'):
        raise ValueError
    return ArchiveIndex(1, value['target'], value['release_version'], archive_bytes,
                        value['archive_sha256'], unpacked_bytes, tuple(files))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def load_archive_index(content: bytes) -> ArchiveIndex:
    """Strict index parse; caller must verify its signature or trusted exact hash."""
    try:
        if not isinstance(content, bytes) or len(content) > MAX_INDEX_BYTES:
            raise ValueError
        value = json.loads(content, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return _checked(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ReleaseArchiveError('invalid release archive index') from None


def _safe_ancestors(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or metadata.st_mode & 0o022 and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX)):
            raise ValueError


def _metadata(fd: int, expected_bytes: int):
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022 or metadata.st_size != expected_bytes):
        raise ValueError
    return metadata


def _same_file(before, after) -> bool:
    return ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))


def _bounded_digest(stream, size: int) -> str:
    h = hashlib.sha256()
    left = size
    while left:
        chunk = stream.read(min(left, 1024 * 1024))
        if not chunk:
            raise ValueError
        h.update(chunk)
        left -= len(chunk)
    if stream.read(1):
        raise ValueError
    return h.hexdigest()


def _exact(stream, size: int) -> bytes:
    content = stream.read(size)
    if len(content) != size:
        raise ValueError
    return content


def extract_verified_archive(archive: Path, index: ArchiveIndex, destination: Path) -> ExtractionResult:
    """Development-only helper: audit index/archive bytes, then extract and hash.

    Never follow archive links, trust tar modes, overwrite a target, or remove a
    failed stage. No configuration, credential, service or provider operations.
    Member hashes are checked during stage writes, not in an earlier complete
    member preflight. Do not substitute this helper for the shipping native seed.
    """
    try:
        # Recheck even manually constructed index instances before using their bounds.
        if not isinstance(index, ArchiveIndex):
            raise ValueError
        index = _checked({'schema_version': index.schema_version, 'target': index.target,
                          'release_version': index.release_version, 'archive_bytes': index.archive_bytes,
                          'archive_sha256': index.archive_sha256, 'unpacked_bytes': index.unpacked_bytes,
                          'files': [{'path': f.path, 'bytes': f.bytes, 'sha256': f.sha256,
                                     'executable': f.executable} for f in index.files]})
        archive, destination = Path(archive), Path(destination)
        _safe_ancestors(archive)
        _safe_ancestors(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        expected = {f.path: f for f in index.files}
        allowed_dirs = {str(p) for name in expected for p in PurePosixPath(name).parents if str(p) != '.'}
        with os.fdopen(os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            before = _metadata(stream.fileno(), index.archive_bytes)
            digest = _bounded_digest(stream, index.archive_bytes)
            if digest != index.archive_sha256 or not _same_file(before, _metadata(stream.fileno(), index.archive_bytes)):
                raise ValueError
            stream.seek(0)
            destination.mkdir(mode=0o700)
            os.chmod(destination, 0o700, follow_symlinks=False)
            seen = set()
            count = total = 0
            # TarFile iteration treats malformed later headers as normal EOF and
            # ignores data after end markers. Parse fixed USTAR records explicitly
            # so every authenticated byte has a bounded, closed interpretation.
            with gzip.GzipFile(fileobj=stream, mode='rb') as payload:
                while True:
                    header = _exact(payload, tarfile.BLOCKSIZE)
                    if header == b'\0' * tarfile.BLOCKSIZE:
                        if _exact(payload, tarfile.BLOCKSIZE) != header:
                            raise ValueError
                        tail = payload.read(tarfile.RECORDSIZE + 1)
                        if len(tail) > tarfile.RECORDSIZE or any(tail):
                            raise ValueError
                        break
                    member = _ReleaseTarInfo.frombuf(header, 'utf-8', 'strict')
                    name = _name(member.name.rstrip('/') if member.isdir() else member.name)
                    if name in seen or len(seen) >= len(expected) + len(allowed_dirs):
                        raise ValueError
                    seen.add(name)
                    if member.isdir():
                        if name not in allowed_dirs or member.size != 0:
                            raise ValueError
                        continue
                    if not member.isfile() or member.sparse is not None or name not in expected:
                        raise ValueError
                    record = expected[name]
                    if member.size != record.bytes:
                        raise ValueError
                    parts = PurePosixPath(name).parts
                    parent = destination
                    for part in parts[:-1]:
                        parent = parent / part
                        try:
                            parent.mkdir(mode=0o700)
                        except FileExistsError:
                            pass
                        else:
                            os.chmod(parent, 0o700, follow_symlinks=False)
                        metadata = parent.lstat()
                        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                                or metadata.st_mode & 0o077):
                            raise ValueError
                    target = parent / parts[-1]
                    mode = 0o500 if record.executable else 0o400
                    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), 'wb') as output:
                        os.fchmod(output.fileno(), mode)
                        h = hashlib.sha256()
                        left = record.bytes
                        while left:
                            chunk = payload.read(min(left, 1024 * 1024))
                            if not chunk:
                                raise ValueError
                            output.write(chunk)
                            h.update(chunk)
                            left -= len(chunk)
                        if h.hexdigest() != record.sha256:
                            raise ValueError
                        output.flush()
                        os.fsync(output.fileno())
                    padding = (-record.bytes) % tarfile.BLOCKSIZE
                    if any(_exact(payload, padding)):
                        raise ValueError
                    count += 1
                    total += record.bytes
            after = _metadata(stream.fileno(), index.archive_bytes)
            if (not _same_file(before, after) or count != len(index.files) or total != index.unpacked_bytes):
                raise ValueError
        return ExtractionResult(count, total)
    except (OSError, ValueError, TypeError, AttributeError, tarfile.TarError, EOFError, zlib.error):
        raise ReleaseArchiveError('release archive integrity or ownership failure') from None
