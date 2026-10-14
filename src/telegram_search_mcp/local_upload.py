"""Bounded, ordered local upload into the existing private artifact store."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from .artifact_store import ArtifactStore, StoredArtifact
from .artifact_authority import active_scope
from .config import APP_SUPPORT_ROOT, ensure_private_directory

UPLOAD_ROOT = APP_SUPPORT_ROOT / "incoming-uploads"
UPLOAD_TTL_SECONDS = 15 * 60
MAX_CHUNK_BYTES = 512 * 1024
MAX_ACTIVE_UPLOADS = 8
MAX_QUARANTINE_BYTES = 256 * 1024 * 1024
QUARANTINE_TTL_SECONDS = 12 * 60 * 60
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UPLOAD_ID = re.compile(r"upload_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LIMITS = {"document": 64 * 1024 * 1024, "photo": 10_000_000, "voice_note": 64 * 1024 * 1024}


class UploadError(RuntimeError):
    """The upload failed a size, integrity, order, or lifecycle check."""


@dataclass(frozen=True)
class CompletedUpload:
    artifact: StoredArtifact
    file_name: str
    kind: str


@dataclass
class _Upload:
    path: Path
    file_name: str
    kind: str
    declared_size: int
    declared_sha256: str
    expires_at: float
    fd: int
    digest: object
    received: int = 0
    next_index: int = 0
    owner: object | None = None
    external: bool = False
    finishing: bool = False
    busy: object | None = None
    retired: bool = False
    cleaned: bool = False
    reservation: str | None = None
    retire_capacity: bool = False


class LocalUploadRegistry:
    def __init__(self, store: ArtifactStore, *, root: Path = UPLOAD_ROOT,
                 clock: Callable[[], float] = time.time, capacity: int = MAX_ACTIVE_UPLOADS,
                 quarantine_limit_bytes: int = MAX_QUARANTINE_BYTES) -> None:
        self._store = store
        self._root = Path(root)
        self._clock = clock
        self._capacity = capacity
        self._quarantine_limit_bytes = quarantine_limit_bytes
        self._entries: dict[str, _Upload] = {}
        # External tickets reserve one slot from issue through owner consumption.
        # Their redeemed stream is not counted a second time.
        self._reservations: set[str] = set()
        self._reservation_prune = lambda: None
        self._lock = threading.RLock()
        # Only root setup/physical retirement serialize here; revocation locks
        # are never held while waiting for this filesystem lock or performing IO.
        self._io_lock = threading.RLock()
        if capacity < 1 or quarantine_limit_bytes < 1:
            raise ValueError("upload capacity must be positive")

    def _ensure_root(self) -> None:
        ensure_private_directory(self._root)
        for path in self._root.iterdir():
            with self._lock:
                tracked = path.name in self._entries
            if tracked:
                continue
            if _UPLOAD_ID.fullmatch(path.name) is None:
                raise UploadError("unknown upload entry")
            self._discard_path(path)
        self._prune_quarantine()

    def _prune_quarantine(self) -> None:
        quarantine = self._root.with_name(self._root.name + "-quarantine")
        ensure_private_directory(quarantine)
        retained: list[tuple[float, Path, int]] = []
        occupied = 0
        for path in quarantine.iterdir():
            info = path.lstat()
            if (not re.fullmatch(r"quarantine_[0-9a-f]{32}", path.name)
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
                raise UploadError("unsafe upload quarantine entry")
            if info.st_mtime + QUARANTINE_TTL_SECONDS <= self._clock():
                path.unlink()
            else:
                occupied += info.st_size
                retained.append((info.st_mtime, path, info.st_size))
        for _, path, size in sorted(retained):
            if occupied <= self._quarantine_limit_bytes:
                break
            path.unlink()
            occupied -= size

    def _discard_path(self, path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise UploadError("unsafe upload entry")
        if info.st_size > 10 * 1024 * 1024:
            quarantine = self._root.with_name(self._root.name + "-quarantine")
            ensure_private_directory(quarantine)
            os.replace(path, quarantine / f"quarantine_{uuid.uuid4().hex}")
            self._prune_quarantine()
        else:
            path.unlink()

    def _retire(self, entry: _Upload) -> None:
        if entry.fd >= 0:
            os.close(entry.fd)
            entry.fd = -1
        try:
            self._discard_path(entry.path)
        except FileNotFoundError:
            pass
        entry.cleaned = True

    def _retire_fact(self, upload_id: str, *, release_capacity: bool = True) -> None:
        """Pure retirement request. Keep path and slot until all pinned IO exits."""
        entry = self._entries.get(upload_id)
        if entry is not None:
            entry.retired = True
            entry.retire_capacity |= release_capacity

    def _cleanup_retired(self) -> None:
        while True:
            with self._lock:
                candidate = next(((key, entry) for key, entry in self._entries.items()
                    if entry.retired and entry.busy is None), None)
                if candidate is None:
                    return
                upload_id, entry = candidate
                pin = entry.busy = object()
            try:
                if not entry.cleaned:
                    with self._io_lock:
                        self._retire(entry)
            finally:
                with self._lock:
                    if entry.busy is pin:
                        entry.busy = None
                    if entry.cleaned and self._entries.get(upload_id) is entry:
                        self._entries.pop(upload_id)
                        if entry.retire_capacity and entry.reservation is not None:
                            self._reservations.discard(entry.reservation)

    def _release_pin(self, upload_id, entry, pin, *, retire=False, release_capacity=True):
        with self._lock:
            if self._entries.get(upload_id) is entry:
                if retire:
                    self._retire_fact(upload_id, release_capacity=release_capacity)
                if entry.busy is pin:
                    entry.busy = None
        self._cleanup_retired()

    @contextmanager
    def _legacy_fence(self):
        scope = active_scope(self._store)
        if scope is not None:
            scope.authority.check_account(scope)
        with self._lock, scope.authority.lock if scope is not None else nullcontext():
            if scope is not None:
                scope.authority.live(scope)
            yield scope

    def _prune_and_clean(self):
        with self._lock:
            self._prune()
        self._cleanup_retired()

    def _prune(self) -> None:
        self._reservation_prune()
        now = self._clock()
        for upload_id, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._retire_fact(upload_id)

    def _get(self, upload_id: str, *, external: bool = False) -> _Upload:
        scope = active_scope(self._store)
        if scope is not None:
            scope.authority.live(scope)
            candidate = self._entries.get(upload_id)
            if candidate is None or candidate.owner != scope.owner:
                raise UploadError("upload is unavailable")
        self._prune()
        if not isinstance(upload_id, str) or _UPLOAD_ID.fullmatch(upload_id) is None:
            raise UploadError("upload ID is invalid")
        try:
            entry = self._entries[upload_id]
            if entry.external != external or entry.finishing or entry.retired or entry.busy is not None:
                raise UploadError('upload is unavailable')
            return entry
        except KeyError:
            raise UploadError("upload is unavailable") from None

    def _claim_begin(self, *, file_name: str, kind: str, size_bytes: int, sha256: str,
                     _reservation: str | None = None, _expires_at: float | None = None):
        """Caller holds the pure fence; install tracking before any setup IO."""
        if not isinstance(file_name, str) or _NAME.fullmatch(file_name) is None or file_name in {".", ".."}:
            raise UploadError("file name is invalid")
        if kind not in _LIMITS or type(size_bytes) is not int or not 0 < size_bytes <= _LIMITS[kind]:
            raise UploadError("upload kind or size is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise UploadError("declared hash is invalid")
        scope = active_scope(self._store)
        if scope is not None:
            scope.authority.live(scope)
        self._prune()
        occupied = len(self._reservations) + sum(not e.external for e in self._entries.values())
        if (_reservation is None and occupied >= self._capacity) or (
                _reservation is not None and _reservation not in self._reservations):
            raise UploadError("upload capacity is exhausted")
        upload_id = f"upload_{uuid.uuid4().hex}"
        if upload_id in self._entries:
            raise UploadError('upload is unavailable')
        pin = object()
        entry = _Upload(path=self._root / upload_id, file_name=file_name, kind=kind,
            declared_size=size_bytes, declared_sha256=sha256,
            expires_at=min(self._clock() + UPLOAD_TTL_SECONDS, _expires_at or float('inf')),
            fd=-1, digest=None, owner=scope.owner if scope is not None else None,
            external=_reservation is not None, reservation=_reservation, busy=pin)
        self._entries[upload_id] = entry
        return upload_id, entry, pin

    def _setup(self, entry):
        with self._io_lock:
            self._ensure_root()
            entry.fd = os.open(entry.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        entry.digest = hashlib.sha256()

    def _check_pin(self, upload_id, entry, pin):
        scope = active_scope(self._store)
        if scope is not None:
            scope.authority.live(scope)
            if entry.owner != scope.owner:
                raise UploadError('upload is unavailable')
        if (self._entries.get(upload_id) is not entry or entry.busy is not pin
                or entry.retired or entry.expires_at <= self._clock()):
            raise UploadError('upload is unavailable')

    def begin(self, *, file_name: str, kind: str, size_bytes: int, sha256: str,
              _reservation: str | None = None, _expires_at: float | None = None) -> str:
        self._prune_and_clean()
        with self._legacy_fence():
            upload_id, entry, pin = self._claim_begin(file_name=file_name, kind=kind,
                size_bytes=size_bytes, sha256=sha256, _reservation=_reservation, _expires_at=_expires_at)
        committed = False
        try:
            self._setup(entry)
            with self._legacy_fence():
                self._check_pin(upload_id, entry, pin)
                entry.busy = None
                committed = True
                return upload_id
        finally:
            self._release_pin(upload_id, entry, pin, retire=not committed)

    @staticmethod
    def _decode_chunk(*, index: int, content_base64: str, sha256: str) -> bytes:
        if type(index) is not int or index < 0 or not isinstance(content_base64, str) or len(content_base64) > 700_000:
            raise UploadError("chunk input is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise UploadError("chunk hash is invalid")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise UploadError("chunk base64 is invalid") from error
        if not 0 < len(data) <= MAX_CHUNK_BYTES or hashlib.sha256(data).hexdigest() != sha256:
            raise UploadError("chunk size or hash is invalid")
        return data

    def append(self, upload_id: str, *, index: int, content_base64: str, sha256: str,
               _external: bool = False) -> tuple[int, int]:
        data = self._decode_chunk(index=index, content_base64=content_base64, sha256=sha256)
        return self._append_verified(upload_id, index=index, data=data, external=_external)

    def _append_verified(self, upload_id: str, *, index: int, data: bytes, external: bool = False):
        self._prune_and_clean()
        with self._legacy_fence():
            entry, pin = self._claim_append(upload_id, index=index, size=len(data), external=external)
        committed = False
        try:
            self._append_io(entry, data)
            with self._legacy_fence():
                result = self._accept_append(upload_id, entry, pin, size=len(data))
                committed = True
                return result
        finally:
            self._release_pin(upload_id, entry, pin, retire=not committed)

    def _claim_append(self, upload_id, *, index, size, external=False):
        entry = self._get(upload_id, external=external)
        if index != entry.next_index or entry.received + size > entry.declared_size:
            raise UploadError('chunk order or total size is invalid')
        pin = entry.busy = object()
        return entry, pin

    def _append_io(self, entry, data):
        view = memoryview(data)
        while view:
            written = os.write(entry.fd, view)
            if written <= 0:
                raise UploadError('chunk write failed')
            view = view[written:]
        entry.digest.update(data)

    def _accept_append(self, upload_id, entry, pin, *, size):
        self._check_pin(upload_id, entry, pin)
        entry.received += size
        entry.next_index += 1
        entry.busy = None
        return entry.next_index, entry.received

    def finish(self, upload_id: str) -> StoredArtifact:
        return self.finish_with_metadata(upload_id).artifact

    def finish_with_metadata(self, upload_id: str) -> CompletedUpload:
        """Consume one completed entry and return its coherent immutable metadata."""
        self._prune_and_clean()
        with self._legacy_fence():
            entry = self._claim_finish(upload_id)
            pin = entry.busy
        try:
            completed = self._finish_io(entry, external=False)
            with self._legacy_fence():
                self._check_pin(upload_id, entry, pin)
                self._detach_cleaned(upload_id, entry, pin)
                return completed
        finally:
            self._release_pin(upload_id, entry, pin, retire=True)

    def _claim_finish(self, upload_id, *, external=False):
        entry = self._get(upload_id, external=external)
        if entry.received != entry.declared_size:
            raise UploadError('upload is incomplete')
        entry.finishing = True
        entry.busy = object()
        return entry

    def _mark_external_finishing(self, upload_id: str) -> _Upload:
        """Caller holds the shared registry lock; keep path/capacity tracked."""
        return self._claim_finish(upload_id, external=True)

    def _copy_external(self, entry: _Upload) -> CompletedUpload:
        """Slow fsync/hash/copy outside registry, authority and broker state locks."""
        return self._finish_io(entry, external=True)

    def _finish_io(self, entry, *, external):
        try:
            if entry.digest.hexdigest() != entry.declared_sha256:
                raise UploadError('upload hash does not match')
            os.fsync(entry.fd)
            os.close(entry.fd)
            entry.fd = -1
            # Legacy explicitly grants through store; external copy never grants.
            store = self._store._store_bytes if external else self._store.store
            stored = store(entry.path, kind='media' if entry.kind == 'voice_note' else 'document')
            if stored.sha256 != entry.declared_sha256 or stored.size_bytes != entry.declared_size:
                raise UploadError('stored artifact differs')
            return CompletedUpload(stored, entry.file_name, entry.kind)
        finally:
            with self._io_lock:
                self._retire(entry)

    def _detach_cleaned(self, upload_id, entry, pin, *, release_capacity=True):
        if self._entries.get(upload_id) is not entry or entry.busy is not pin or not entry.cleaned:
            raise UploadError('upload is unavailable')
        self._entries.pop(upload_id)
        entry.busy = None
        if release_capacity and entry.reservation is not None:
            self._reservations.discard(entry.reservation)

    def _drop_external(self, upload_id: str, *, copied: bool = False) -> None:
        with self._lock:
            entry = self._entries.get(upload_id)
            if entry is not None and entry.external:
                self._retire_fact(upload_id)
        self._cleanup_retired()
