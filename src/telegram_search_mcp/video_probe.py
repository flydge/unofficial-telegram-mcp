"""Bounded stdlib ISO-BMFF (MP4/MOV) metadata probe for native video sends.

Reads box headers by seeking and the ``moov`` payload into memory; it never decodes
media. Every refusal is the one fixed ``unsupported_video`` code: no detail, no chained
exception, nothing from the file or the path in the message.
"""
from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_MOOV_BYTES = 16 * 1024 * 1024
MAX_FTYP_BYTES = 4096
MAX_BOXES = 4096
MAX_DEPTH = 8
MAX_SECONDS = 3 * 3600
MIN_SIDE, MAX_SIDE, MAX_ASPECT = 16, 8192, 20
_CHUNK = 1024 * 1024
_BRANDS = frozenset({b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42', b'avc1', b'M4V ', b'qt  '})
# Only boxes this probe reads through; every other box is skipped by its size.
_CONTAINERS = frozenset({b'moov', b'trak', b'mdia', b'minf', b'stbl'})
_VIDEO = {b'avc1': 'h264', b'avc3': 'h264', b'hvc1': 'hevc', b'hev1': 'hevc'}
_AUDIO = frozenset({b'mp4a', b'Opus'})
_ONE, _UNITY = 0x10000, 0x40000000
# tkhd matrix (a, b, c, d) -> swaps width and height (90/270 degrees) or not (0/180).
_TURNS = {(_ONE, 0, 0, _ONE): False, (-_ONE, 0, 0, -_ONE): False,
          (0, _ONE, -_ONE, 0): True, (0, -_ONE, _ONE, 0): True}


class VideoProbeError(ValueError):
    code = 'unsupported_video'

    def __init__(self) -> None:
        super().__init__('unsupported_video')


@dataclass(frozen=True)
class VideoFacts:
    duration_seconds: int
    width: int
    height: int
    codec: Literal['h264', 'hevc']
    has_audio: bool
    supports_streaming: bool


@dataclass(frozen=True)
class _Box:
    kind: bytes
    body: int
    end: int
    kids: tuple[_Box, ...] = ()


class _Budget:
    __slots__ = ('left',)

    def __init__(self) -> None:
        self.left = MAX_BOXES

    def take(self) -> None:
        if self.left <= 0:
            _refuse()
        self.left -= 1


def _refuse() -> NoReturn:
    raise VideoProbeError()


def probe_video(path: Path, *, size_bytes: int) -> VideoFacts:
    try:
        return _probe(path, size_bytes)
    except VideoProbeError:
        raise
    except (OSError, ValueError, struct.error):
        pass
    _refuse()  # raised outside the handler so no exception is chained


def _probe(path: Path, size_bytes: int) -> VideoFacts:
    if type(size_bytes) is not int or not 0 < size_bytes <= MAX_FILE_BYTES:
        _refuse()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size_bytes:
            _refuse()
        buf, moov, streaming = _top(fd, size_bytes, _Budget())
        facts = _facts(buf, moov, streaming)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            _refuse()
        return facts
    finally:
        os.close(fd)


def _read(fd: int, pos: int, count: int) -> bytes:
    parts, got = [], 0
    for _ in range(count // _CHUNK + 16):
        if got == count:
            break
        part = os.pread(fd, min(_CHUNK, count - got), pos + got)
        if not part:
            break
        parts.append(part)
        got += len(part)
    if got != count:
        _refuse()
    return b''.join(parts)


def _top(fd: int, size: int, budget: _Budget) -> tuple[bytes, _Box, bool]:
    pos, buf, moov, moov_at, mdat_at = 0, b'', None, -1, -1
    while pos < size:
        budget.take()
        head = _read(fd, pos, min(16, size - pos))
        if len(head) < 8:
            _refuse()
        length, kind = struct.unpack_from('>I4s', head)
        hlen = 8
        if length == 1:
            if len(head) < 16:
                _refuse()
            length, hlen = struct.unpack_from('>Q', head, 8)[0], 16
        elif length == 0:
            length = size - pos  # "to end of file": necessarily the last top-level box
        if length < hlen or length > size - pos or (pos == 0 and kind != b'ftyp'):
            _refuse()
        payload = length - hlen
        if kind == b'ftyp' and pos == 0:
            if not 8 <= payload <= MAX_FTYP_BYTES or payload % 4:
                _refuse()
            data = _read(fd, pos + hlen, payload)
            if not ({data[:4]} | {data[at:at + 4] for at in range(8, payload, 4)}) & _BRANDS:
                _refuse()
        elif kind == b'moov':
            if moov is not None or payload > MAX_MOOV_BYTES:
                _refuse()
            buf, moov_at = _read(fd, pos + hlen, payload), pos
            moov = _Box(kind, 0, payload, _walk(buf, 0, payload, 2, budget))
        elif kind == b'mdat' and mdat_at < 0:
            mdat_at = pos
        pos += length
    if moov is None or mdat_at < 0:
        _refuse()
    return buf, moov, moov_at < mdat_at


def _walk(buf: bytes, start: int, end: int, depth: int, budget: _Budget) -> tuple[_Box, ...]:
    boxes, pos = [], start
    while pos < end:
        budget.take()
        if depth > MAX_DEPTH or end - pos < 8:
            _refuse()
        length, kind = struct.unpack_from('>I4s', buf, pos)
        hlen = 8
        if length == 1:
            if end - pos < 16:
                _refuse()
            length, hlen = struct.unpack_from('>Q', buf, pos + 8)[0], 16
        # size 0 ("to end") is a top-level privilege and fails here like any undersized box
        if length < hlen or length > end - pos:
            _refuse()
        body, stop = pos + hlen, pos + length
        kids = _walk(buf, body, stop, depth + 1, budget) if kind in _CONTAINERS else ()
        boxes.append(_Box(kind, body, stop, kids))
        pos = stop
    return tuple(boxes)


def _one(boxes: tuple[_Box, ...], kind: bytes) -> _Box:
    found = [box for box in boxes if box.kind == kind]
    if len(found) != 1:
        _refuse()
    return found[0]


def _at(box: _Box, need: int) -> int:
    if box.end - box.body < need:
        _refuse()
    return box.body


def _full(buf: bytes, box: _Box, v0: int, v1: int) -> tuple[int, int]:
    at = _at(box, 4)
    version = buf[at]
    if version not in (0, 1):
        _refuse()
    _at(box, v0 if version == 0 else v1)
    return at, version


def _seconds(buf: bytes, mvhd: _Box) -> int:
    at, version = _full(buf, mvhd, 20, 32)
    scale, units = struct.unpack_from('>II', buf, at + 12) if version == 0 else struct.unpack_from('>IQ', buf, at + 20)
    seconds = -(-units // scale) if scale else 0
    if not 1 <= seconds <= MAX_SECONDS:
        _refuse()
    return seconds


def _handler(buf: bytes, trak: _Box) -> bytes:
    hdlr = _one(_one(trak.kids, b'mdia').kids, b'hdlr')
    at = _at(hdlr, 12)
    return buf[at + 8:at + 12]


def _entry(buf: bytes, trak: _Box) -> bytes:
    stbl = _one(_one(_one(trak.kids, b'mdia').kids, b'minf').kids, b'stbl')
    stsd = _one(stbl.kids, b'stsd')
    at = _at(stsd, 16)
    count, length, kind = struct.unpack_from('>4xII4s', buf, at)
    if count < 1 or length < 8 or length > stsd.end - at - 8:
        _refuse()
    return kind


def _dimensions(buf: bytes, tkhd: _Box) -> tuple[int, int]:
    at, version = _full(buf, tkhd, 84, 96)
    at += 40 if version == 0 else 52
    a, b, u, c, d, v, _x, _y, w = struct.unpack_from('>9i', buf, at)  # translation is irrelevant to size
    swap = _TURNS.get((a, b, c, d))
    if swap is None or u or v or w != _UNITY:
        _refuse()
    width, height = (value >> 16 for value in struct.unpack_from('>II', buf, at + 36))
    return (height, width) if swap else (width, height)


def _facts(buf: bytes, moov: _Box, streaming: bool) -> VideoFacts:
    kids = moov.kids
    if any(box.kind == b'mvex' for box in kids):
        _refuse()
    seconds = _seconds(buf, _one(kids, b'mvhd'))
    video = audio = None
    for trak in (box for box in kids if box.kind == b'trak'):
        handler = _handler(buf, trak)
        if handler == b'vide' and video is None:
            video = trak
        elif handler == b'soun' and audio is None:
            audio = trak
        else:
            _refuse()
    if video is None or (audio is not None and _entry(buf, audio) not in _AUDIO):
        _refuse()
    codec = _VIDEO.get(_entry(buf, video))
    width, height = _dimensions(buf, _one(video.kids, b'tkhd'))
    if codec is None or not (MIN_SIDE <= min(width, height) and max(width, height) <= MAX_SIDE
                             and max(width, height) <= MAX_ASPECT * min(width, height)):
        _refuse()
    return VideoFacts(seconds, width, height, codec, audio is not None, streaming)
