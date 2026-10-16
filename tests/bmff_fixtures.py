"""Synthetic ISO-BMFF writer for the video probe tests: boxes only, no media."""
import struct

ONE, UNITY = 0x10000, 0x40000000


def box(kind, *parts, large=False, size=None):
    body = b''.join(parts)
    if large:
        return struct.pack('>I4sQ', 1, kind, 16 + len(body) if size is None else size) + body
    return struct.pack('>I4s', 8 + len(body) if size is None else size, kind) + body


def full(kind, body, *, version=0, flags=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, 'big'), body)


def matrix(a=ONE, b=0, c=0, d=ONE, *, u=0, v=0, x=0, y=0, w=UNITY):
    return struct.pack('>9i', a, b, u, c, d, v, x, y, w)


ROTATIONS = {0: matrix(), 90: matrix(0, ONE, -ONE, 0), 180: matrix(-ONE, 0, 0, -ONE), 270: matrix(0, -ONE, ONE, 0)}


def ftyp(major=b'isom', compatible=(b'isom', b'mp42'), **options):
    return box(b'ftyp', major, struct.pack('>I', 0), *compatible, **options)


def mvhd(timescale=1000, duration=5000, *, version=0):
    times = struct.pack('>IIII', 0, 0, timescale, duration) if version == 0 else struct.pack('>QQIQ', 0, 0, timescale, duration)
    return full(b'mvhd', times + bytes(80), version=version)


def tkhd(width=1280, height=720, *, rotation=0, rotation_matrix=None, version=0, fraction=0, track_id=1):
    head = (struct.pack('>IIIII', 0, 0, track_id, 0, 0) if version == 0
            else struct.pack('>QQIIQ', 0, 0, track_id, 0, 0))
    size = struct.pack('>II', width << 16 | fraction, height << 16 | fraction)
    return full(b'tkhd', head + bytes(16) + (ROTATIONS[rotation] if rotation_matrix is None else rotation_matrix) + size,
                version=version, flags=3)


def hdlr(handler):
    return full(b'hdlr', struct.pack('>I4s', 0, handler) + bytes(12) + b'\0')


def stsd(*entries):
    return full(b'stsd', struct.pack('>I', len(entries)) + b''.join(box(entry, bytes(78)) for entry in entries))


def trak(handler=b'vide', entry=b'avc1', *, stsd_box=None, tkhd_box=None, extra=b'', **dimensions):
    sample = stsd(entry) if stsd_box is None else stsd_box
    media = box(b'mdia', hdlr(handler), box(b'minf', box(b'stbl', sample)))
    return box(b'trak', tkhd(**dimensions) if tkhd_box is None else tkhd_box, media, extra)


def video(entry=b'avc1', **options):
    return trak(b'vide', entry, **options)


def audio(entry=b'mp4a', **options):
    return trak(b'soun', entry, width=0, height=0, **options)


def moov(*children, **options):
    return box(b'moov', *children, **options)


def mdat(payload=bytes(64), **options):
    return box(b'mdat', payload, **options)


def movie(*children, faststart=True, brand=None):
    """ftyp + moov (default: one avc1 1280x720 5 s track) + mdat; moov last when not faststart."""
    header = ftyp() if brand is None else ftyp(*brand)
    index = moov(*(children or (mvhd(), video())))
    return header + (index + mdat() if faststart else mdat() + index)
