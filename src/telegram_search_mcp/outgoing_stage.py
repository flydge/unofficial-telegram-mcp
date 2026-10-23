"""Bounded private file names for approved TDLib sends.

Every staged file lives alone in its own `draft_<32hex>` directory directly under the staging root: a single
send in the directory named by its draft, an album item (0.56) in a directory whose name is derived from the
album's handle and the item's position. The sweep, the capacity bound and the transport's staged-path validator
therefore see one shape only.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

from .config import STAGING_ROOT, ensure_private_directory
from .outgoing_drafts import ClaimedDraft

MAX_STAGING_BYTES = 512 * 1024 * 1024
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024
_DRAFT_NAME = re.compile(r"draft_[0-9a-f]{32}\Z")
_LOCK = threading.Lock()


def _safe_name(name: object) -> bool:
    return (type(name) is str and bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name
            and "\x00" not in name and len(name.encode("utf-8")) <= 240)


def _sweep_locked(root: Path) -> int:
    """Retire expired copies under the staging lock; return the bytes still occupied (quarantine included).

    Retirement takes the same lock, so a copy and its directory leave together. An entry that vanishes while the
    sweep reads it (another process) has nothing left to count or refuse and is skipped; anything still present
    and unsafe is refused, a fresh empty directory included."""
    ensure_private_directory(root)
    quarantine = root.with_name(root.name + "-quarantine")
    ensure_private_directory(quarantine)
    occupied = 0
    now = time.time()
    for item in quarantine.iterdir():
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (not item.name.startswith("quarantine_") or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise ValueError("unsafe outgoing quarantine entry")
        if info.st_mtime + 12 * 3600 <= now:
            item.unlink(missing_ok=True)
        else:
            occupied += info.st_size
    for entry in root.iterdir():
        try:
            info = entry.lstat()
            if not _DRAFT_NAME.fullmatch(entry.name) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("unsafe outgoing staging entry")
            files = list(entry.iterdir())
            if not files and info.st_mtime + 15 * 60 <= now:
                entry.rmdir()
                continue
            if len(files) != 1:
                raise ValueError("unsafe outgoing staging contents")
            file_info = files[0].lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or file_info.st_nlink != 1:
            raise ValueError("unsafe outgoing staging file")
        if info.st_mtime + 12 * 3600 <= now:
            _retire_locked(files[0], root)
            if file_info.st_size > LARGE_FILE_THRESHOLD:
                occupied += file_info.st_size
        else:
            occupied += file_info.st_size
    return occupied


def _copy_verified(source: Path, directory: Path, name: str, size_bytes: int, sha256: str) -> Path:
    """Copy exactly the reviewed bytes into a fresh `directory`; on any failure remove the copy and the directory."""
    target = directory / name
    digest = hashlib.sha256()
    copied = 0
    source_fd = -1
    target_fd = -1
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        source_info = os.fstat(source_fd)
        if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.geteuid()
            or source_info.st_size != size_bytes):
            raise ValueError("source changed before staging")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        while chunk := os.read(source_fd, 1024 * 1024):
            copied += len(chunk)
            if copied > size_bytes:
                raise ValueError("source grew while staging")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(target_fd, view):]
        if copied != size_bytes or digest.hexdigest() != sha256:
            raise ValueError("source hash changed before staging")
        os.fsync(target_fd)
        return target
    except BaseException:
        if target_fd >= 0:
            os.close(target_fd)
            target_fd = -1
        target.unlink(missing_ok=True)
        directory.rmdir()
        raise
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        if source_fd >= 0:
            os.close(source_fd)


def stage_approved_document(claim: ClaimedDraft, *, root: Path | None = None) -> Path:
    """Copy verified bytes to the exact reviewed file name before provider use."""
    root = STAGING_ROOT if root is None else root
    draft = claim.draft
    name = draft.display_name
    if not _DRAFT_NAME.fullmatch(draft.draft_id) or not _safe_name(name):
        raise ValueError("unsafe outgoing file name")
    with _LOCK:
        occupied = _sweep_locked(root)
        if occupied + draft.size_bytes > MAX_STAGING_BYTES:
            raise ValueError("outgoing staging capacity exhausted")
        directory = root / draft.draft_id
        directory.mkdir(mode=0o700)
        return _copy_verified(claim.path, directory, name, draft.size_bytes, draft.sha256)


def album_item_directory(album_draft_id: str, index: int) -> str:
    """The staging directory of one album item: a `draft_<32hex>` name derived from the album handle and the
    item's position, so every item of one album is found from its handle and no two items share a directory."""
    if (type(album_draft_id) is not str or not _DRAFT_NAME.fullmatch(album_draft_id) or type(index) is not int
            or not 0 <= index < 10):
        raise ValueError("unsafe outgoing album")
    seed = b"telegram-search-mcp.album-item\x00" + album_draft_id.encode("ascii") + b"\x00%d" % index
    return "draft_" + hashlib.sha256(seed).hexdigest()[:32]


def album_staged_paths(album_draft_id: str, album, *, root: Path | None = None) -> tuple[Path, ...]:
    """Where every item of one album draft is (or would be) staged, in send order."""
    root = STAGING_ROOT if root is None else root
    items = getattr(album, "items", None)
    if type(items) is not tuple or not items:
        raise ValueError("unsafe outgoing album")
    return tuple(root / album_item_directory(album_draft_id, index) / item.display_name
                 for index, item in enumerate(items))


def stage_approved_album(claim: ClaimedDraft, *, root: Path | None = None) -> tuple[Path, ...]:
    """Copy every verified item to its own directory before the one provider attempt.

    The whole album's bytes count against the capacity before anything is copied; a failure on any item removes
    every copy made, and an album is staged at most once (its directories already exist). Every item directory
    gets one modification time, so the 12-hour sweep retires the album's items together."""
    root = STAGING_ROOT if root is None else root
    draft = claim.draft
    album = getattr(draft, "album", None)
    if (draft.kind != "album" or album is None or type(claim.paths) is not tuple or
            len(claim.paths) != len(album.items) or not all(_safe_name(item.display_name) for item in album.items)):
        raise ValueError("unsafe outgoing album")
    targets = album_staged_paths(draft.draft_id, album, root=root)
    with _LOCK:
        occupied = _sweep_locked(root)
        if occupied + album.total_bytes > MAX_STAGING_BYTES:
            raise ValueError("outgoing staging capacity exhausted")
        copied: list[Path] = []
        try:
            for target, item, source in zip(targets, album.items, claim.paths):
                target.parent.mkdir(mode=0o700)
                copied.append(_copy_verified(source, target.parent, item.display_name, item.size_bytes, item.sha256))
            stamp = time.time()
            for target in copied:
                os.utime(target.parent, (stamp, stamp))
            return targets
        except BaseException:
            for target in copied:
                target.unlink(missing_ok=True)
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
            raise


def retire_staged_album(paths, *, root: Path | None = None) -> None:
    """Retire every item of one album under the staging lock; the caller does so only once every slot is
    terminal."""
    root = STAGING_ROOT if root is None else root
    failure = None
    with _LOCK:
        for path in paths:
            try:
                _retire_locked(path, root)
            except (OSError, ValueError) as error:
                failure = failure or error
    if failure is not None:
        raise failure


def retire_staged_document(path: Path, *, root: Path | None = None) -> None:
    """Retire a finished copy, quarantining large files outside the worktree.

    Under the staging lock: a concurrent sweep never sees the copy gone and its directory still present."""
    root = STAGING_ROOT if root is None else root
    with _LOCK:
        _retire_locked(path, root)


def _retire_locked(path: Path, root: Path) -> None:
    """Retire one copy and its directory; the caller holds `_LOCK`."""
    if path.parent.parent != root or not _DRAFT_NAME.fullmatch(path.parent.name):
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("unsafe outgoing staging file")
    if info.st_size > LARGE_FILE_THRESHOLD:
        quarantine = root.with_name(root.name + "-quarantine")
        ensure_private_directory(quarantine)
        target = quarantine / f"quarantine_{uuid.uuid4().hex}"
        os.replace(path, target)
        os.utime(target, (info.st_mtime, info.st_mtime))
    else:
        path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
