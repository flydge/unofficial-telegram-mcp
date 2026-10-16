"""Synthetic ISO-BMFF fixtures through the bounded stdlib video probe.

Boxes are written in code; no media, no committed binaries, no Telegram.
"""
from __future__ import annotations

import dataclasses
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bmff_fixtures import (
    ONE, audio, box, ftyp, full, hdlr, mdat, matrix, moov, movie, mvhd, stsd, tkhd, trak, video)
from telegram_search_mcp import video_probe
from telegram_search_mcp.video_probe import VideoFacts, VideoProbeError, probe_video

MIB = 1024 * 1024
CANARY = b'ZQXJ-canary'
# ftyp + moov + mdat, moov > mvhd + trak, trak > tkhd + mdia, mdia > hdlr + minf, minf > stbl, stbl > stsd.
BASE_BOXES = 11


class ProbeTestCase(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix='f15a-probe-')
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.sequence = 0

    def write(self, data):
        self.sequence += 1
        path = self.root / f'clip{self.sequence}.mp4'
        path.write_bytes(data)
        return path

    def probe(self, data):
        return probe_video(self.write(data), size_bytes=len(data))

    def assertRefused(self, call):
        with self.assertRaises(VideoProbeError) as caught:
            call()
        error = caught.exception
        self.assertEqual(error.code, 'unsupported_video')
        self.assertEqual(error.args, ('unsupported_video',))
        self.assertEqual(str(error), 'unsupported_video')
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for leaked in ('ZQXJ', '-can', 'nary', str(self.root), 'clip'):
            self.assertNotIn(leaked, repr(error))

    def assertRejects(self, data):
        self.assertRefused(lambda: self.probe(data))

    def assertRejectsAll(self, cases):
        for name, data in cases.items():
            with self.subTest(name):
                self.assertRejects(data)

    def sparse(self, head, total):
        path = self.write(head)
        with open(path, 'r+b') as handle:
            handle.truncate(total)
        return path


class FactsTests(ProbeTestCase):
    def test_h264_faststart_mp4(self):
        facts = self.probe(movie())
        self.assertEqual(facts, VideoFacts(duration_seconds=5, width=1280, height=720, codec='h264',
                                           has_audio=False, supports_streaming=True))

    def test_moov_after_mdat_is_not_streaming(self):
        self.assertFalse(self.probe(movie(faststart=False)).supports_streaming)

    def test_streaming_follows_the_first_mdat_only(self):
        index = moov(mvhd(), video())
        cases = {
            'free before moov': (ftyp() + box(b'free', bytes(24)) + index + mdat(), True),
            'two mdat after moov': (ftyp() + index + mdat() + mdat(), True),
            'moov between mdat': (ftyp() + mdat() + index + mdat(), False),
            'wide and skip then mdat last': (ftyp() + box(b'wide') + index + box(b'skip', b'x') + mdat(), True),
        }
        for name, (data, streaming) in cases.items():
            with self.subTest(name):
                self.assertIs(self.probe(data).supports_streaming, streaming)

    def test_hevc_mov_with_quicktime_brand(self):
        data = movie(mvhd(600, 3000), video(b'hvc1', width=3840, height=2160), brand=(b'qt  ', (b'qt  ',)))
        self.assertEqual(self.probe(data), VideoFacts(5, 3840, 2160, 'hevc', False, True))

    def test_sample_entries(self):
        for entry, codec in ((b'avc1', 'h264'), (b'avc3', 'h264'), (b'hvc1', 'hevc'), (b'hev1', 'hevc')):
            with self.subTest(entry):
                self.assertEqual(self.probe(movie(mvhd(), video(entry))).codec, codec)

    def test_only_the_first_sample_entry_counts(self):
        data = movie(mvhd(), video(stsd_box=stsd(b'avc1', b'vp09')))
        self.assertEqual(self.probe(data).codec, 'h264')
        self.assertRejects(movie(mvhd(), video(stsd_box=stsd(b'vp09', b'avc1'))))

    def test_audio_present_and_absent(self):
        self.assertFalse(self.probe(movie(mvhd(), video())).has_audio)
        for entry in (b'mp4a', b'Opus'):
            with self.subTest(entry):
                self.assertTrue(self.probe(movie(mvhd(), video(), audio(entry))).has_audio)
        self.assertTrue(self.probe(movie(mvhd(), audio(), video())).has_audio)

    def test_brands(self):
        for brand in (b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42', b'avc1', b'M4V ', b'qt  '):
            with self.subTest(brand=brand, role='major'):
                self.probe(movie(brand=(brand, (b'zzzz',))))
            with self.subTest(brand=brand, role='compatible'):
                self.probe(movie(brand=(b'zzzz', (b'yyyy', brand))))

    def test_rotation_swaps_only_quarter_turns(self):
        for degrees, expected in ((0, (1280, 720)), (90, (720, 1280)), (180, (1280, 720)), (270, (720, 1280))):
            for version in (0, 1):
                with self.subTest(degrees=degrees, version=version):
                    facts = self.probe(movie(mvhd(), video(rotation=degrees, version=version)))
                    self.assertEqual((facts.width, facts.height), expected)

    def test_tkhd_translation_and_fraction_do_not_change_size(self):
        data = movie(mvhd(), video(rotation_matrix=matrix(0, ONE, -ONE, 0, x=720 * ONE, y=-5), fraction=0x8000))
        facts = self.probe(data)
        self.assertEqual((facts.width, facts.height), (720, 1280))

    def test_odd_matrices_refuse(self):
        cases = {
            'mirrored': matrix(-ONE, 0, 0, ONE),
            'flipped': matrix(ONE, 0, 0, -ONE),
            'scaled': matrix(2 * ONE, 0, 0, 2 * ONE),
            'skew': matrix(ONE, ONE, ONE, ONE),
            'degrees 45': matrix(46341, 46341, -46341, 46341),
            'all zero': bytes(36),
            'perspective u': matrix(u=1),
            'perspective v': matrix(v=1),
            'w not unity': matrix(w=1),
            'w zero': matrix(w=0),
            'mirrored quarter turn': matrix(0, ONE, ONE, 0),
        }
        self.assertRejectsAll({name: movie(mvhd(), video(rotation_matrix=value)) for name, value in cases.items()})

    def test_dimension_bounds(self):
        accepted = ((16, 16), (8192, 8192), (8192, 410), (410, 8192), (3200, 160), (160, 3200), (16, 320))
        for width, height in accepted:
            with self.subTest(width=width, height=height):
                facts = self.probe(movie(mvhd(), video(width=width, height=height)))
                self.assertEqual((facts.width, facts.height), (width, height))
        self.assertRejectsAll({f'{w}x{h}': movie(mvhd(), video(width=w, height=h)) for w, h in (
            (15, 720), (720, 15), (0, 0), (8193, 720), (720, 8193), (65535, 65535), (3216, 160), (160, 3216), (8192, 409))})

    def test_duration_rounds_up_and_is_bounded(self):
        for scale, units, seconds in ((1000, 1, 1), (1000, 1000, 1), (1000, 1001, 2), (1000, 1999, 2), (1000, 2000, 2),
                                      (90000, 90001, 2), (1, 10800, 10800), (1000, 10_800_000, 10800), (600, 6_479_999, 10800)):
            for version in (0, 1):
                with self.subTest(scale=scale, units=units, version=version):
                    self.assertEqual(self.probe(movie(mvhd(scale, units, version=version), video())).duration_seconds, seconds)
        self.assertRejectsAll({f'{scale}/{units}/v{version}': movie(mvhd(scale, units, version=version), video())
                               for version in (0, 1)
                               for scale, units in ((1000, 0), (1000, 10_800_001), (1, 10801), (600, 6_480_001), (1000, 0xFFFFFFFF))})

    def test_version_one_duration_beyond_32_bits(self):
        self.assertEqual(self.probe(movie(mvhd((1 << 32) - 1, (1 << 32) * 3, version=1), video())).duration_seconds, 4)
        self.assertRejects(movie(mvhd(1, 1 << 40, version=1), video()))

    def test_timescale_zero_refuses(self):
        self.assertRejectsAll({f'v{version}': movie(mvhd(0, 5000, version=version), video()) for version in (0, 1)})

    def test_mvhd_shape(self):
        self.assertRejectsAll({
            'missing': movie(video()),
            'twice': movie(mvhd(), mvhd(), video()),
            'version 2': movie(full(b'mvhd', bytes(100), version=2), video()),
            'truncated v0': movie(full(b'mvhd', bytes(15)), video()),
            'truncated v1': movie(full(b'mvhd', bytes(27), version=1), video()),
            'empty': movie(box(b'mvhd'), video()),
        })

    def test_fragmented_files_refuse(self):
        self.assertRejects(movie(mvhd(), video(), box(b'mvex', full(b'trex', bytes(20)))))
        self.assertRejects(movie(mvhd(), box(b'mvex'), video()))


class TrackTests(ProbeTestCase):
    def test_track_set_shapes(self):
        self.assertRejectsAll({
            'no tracks': movie(mvhd()),
            'audio only': movie(mvhd(), audio()),
            'two video': movie(mvhd(), video(), video()),
            'two audio': movie(mvhd(), video(), audio(), audio(b'Opus')),
            'three tracks': movie(mvhd(), video(), audio(), video()),
        })

    def test_unknown_codecs_refuse(self):
        self.assertRejectsAll({
            **{entry.decode(): movie(mvhd(), video(entry)) for entry in (b'vp09', b'av01', b'mp4v', b'jpeg', b'apcn', CANARY[:4])},
            **{f'audio {entry.decode()}': movie(mvhd(), video(), audio(entry)) for entry in (b'ac-3', b'samr', b'alac', b'sowt', b'.mp3')},
        })

    def test_other_handler_types_refuse(self):
        extra = {entry.decode(): movie(mvhd(), video(), trak(entry, b'text', width=0, height=0))
                 for entry in (b'text', b'hint', b'meta', b'tmcd', b'sbtl', b'subt', b'mebx', b'clcp', b'\0\0\0\0')}
        self.assertRejectsAll(extra)
        self.assertRejects(movie(mvhd(), trak(b'text', b'text', width=0, height=0)))

    def test_track_structure(self):
        def nested(*parts):
            return box(b'trak', tkhd(), *parts)
        mdia = lambda *inner: box(b'mdia', *inner)
        minf = lambda *inner: box(b'minf', *inner)
        stbl = lambda *inner: box(b'stbl', *inner)
        good_stbl = stbl(stsd(b'avc1'))
        self.assertRejectsAll({
            'no mdia': movie(mvhd(), nested()),
            'two mdia': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(good_stbl)), mdia(hdlr(b'vide'), minf(good_stbl)))),
            'no hdlr': movie(mvhd(), nested(mdia(minf(good_stbl)))),
            'two hdlr': movie(mvhd(), nested(mdia(hdlr(b'vide'), hdlr(b'vide'), minf(good_stbl)))),
            'short hdlr': movie(mvhd(), nested(mdia(full(b'hdlr', bytes(7)), minf(good_stbl)))),
            'no minf': movie(mvhd(), nested(mdia(hdlr(b'vide')))),
            'no stbl': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf()))),
            'no stsd': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl())))),
            'two stsd': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(stsd(b'avc1'), stsd(b'avc1')))))),
            'empty stsd': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(full(b'stsd', struct.pack('>I', 0))))))),
            'zero entry count': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(
                full(b'stsd', struct.pack('>I', 0) + box(b'avc1', bytes(78)))))))),
            'short stsd': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(full(b'stsd', bytes(7))))))),
            'entry past stsd': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(
                full(b'stsd', struct.pack('>I', 1) + box(b'avc1', bytes(4), size=200))))))),
            'entry under 8': movie(mvhd(), nested(mdia(hdlr(b'vide'), minf(stbl(
                full(b'stsd', struct.pack('>I', 1) + box(b'avc1', size=7))))))),
            'no tkhd': movie(mvhd(), box(b'trak', mdia(hdlr(b'vide'), minf(good_stbl)))),
            'two tkhd': movie(mvhd(), video(extra=tkhd())),
            'short tkhd v0': movie(mvhd(), video(tkhd_box=full(b'tkhd', bytes(79)))),
            'short tkhd v1': movie(mvhd(), video(tkhd_box=full(b'tkhd', bytes(91), version=1))),
            'tkhd version 2': movie(mvhd(), video(tkhd_box=full(b'tkhd', bytes(100), version=2))),
        })

    def test_unrelated_boxes_are_skipped(self):
        udta = box(b'udta', box(b'meta', CANARY))
        extra = movie(mvhd(), udta, video(extra=box(b'edts', box(b'elst', bytes(8)))), box(b'free', CANARY))
        self.assertEqual(self.probe(extra).codec, 'h264')


class StructureTests(ProbeTestCase):
    def test_ftyp_rules(self):
        index = moov(mvhd(), video())
        self.assertRejectsAll({
            'empty file': b'',
            'moov first': index + mdat(),
            'ftyp not first': box(b'free', bytes(8)) + ftyp() + index + mdat(),
            'ftyp after moov': index + ftyp() + mdat(),
            'bad major and compatible': ftyp(b'3gp4', (b'3gp5', b'mif1')) + index + mdat(),
            'bad brand only': ftyp(b'heic', ()) + index + mdat(),
            'ftyp without major': box(b'ftyp', bytes(4)) + index + mdat(),
            'ftyp payload not multiple of 4': box(b'ftyp', b'isom', bytes(4), b'xy') + index + mdat(),
            'ftyp over 4096 bytes': ftyp(b'isom', (b'isom',) * 1100) + index + mdat(),
            'ftyp to end of file': box(b'ftyp', b'isom', bytes(4), b'isom', size=0) + index + mdat(),
            'major spelled wrongly': ftyp(b'ISOM', (b'MP42',)) + index + mdat(),
        })

    def test_ftyp_at_the_size_cap_is_read(self):
        crowded = ftyp(b'zzzz', (b'yyyy',) * 1021 + (b'mp42',))
        self.assertEqual(len(crowded), 4096 + 8)
        self.probe(crowded + moov(mvhd(), video()) + mdat())
        self.assertRejects(ftyp(b'zzzz', (b'yyyy',) * 1022 + (b'mp42',)) + moov(mvhd(), video()) + mdat())

    def test_moov_and_mdat_cardinality(self):
        index = moov(mvhd(), video())
        self.assertRejectsAll({
            'two moov': ftyp() + index + index + mdat(),
            'two moov around mdat': ftyp() + index + mdat() + index,
            'no moov': ftyp() + mdat(),
            'no mdat': ftyp() + index,
            'only ftyp': ftyp(),
            'empty moov': ftyp() + moov() + mdat(),
        })

    def test_moov_inside_another_box_does_not_count(self):
        self.assertRejects(ftyp() + box(b'free', moov(mvhd(), video())) + mdat())

    def test_truncation(self):
        whole = movie()
        self.assertRejectsAll({
            'header cut at 3 bytes': whole + bytes(3),
            'header cut at 7 bytes': whole + bytes(7),
            'mdat longer than file': ftyp() + moov(mvhd(), video()) + box(b'mdat', bytes(16), size=1000),
            'moov longer than file': ftyp() + box(b'moov', mvhd(), video(), size=5000) + mdat(),
            'ftyp longer than file': box(b'ftyp', b'isom', size=100),
            'file cut inside moov': whole[:80],
            'file cut inside mdat': whole[:-5],
        })

    def test_every_strict_prefix_refuses(self):
        for data in (movie(), movie(faststart=False), movie(mvhd(), video(), audio())):
            for length in range(len(data)):
                with self.subTest(length=length):
                    self.assertRejects(data[:length])

    def test_nested_box_past_its_parent(self):
        inner = video(extra=box(b'free', bytes(8), size=64))
        self.assertRejectsAll({
            'child past trak but inside moov': ftyp() + moov(mvhd(), inner, audio()) + mdat(),
            'child past trak and file': ftyp() + moov(mvhd(), video(extra=box(b'free', size=10_000))) + mdat(),
            'stsd entry past stsd': ftyp() + moov(mvhd(), video(stsd_box=full(b'stsd', struct.pack('>I', 1) + box(b'avc1', size=500)))) + mdat(),
            'child past moov': ftyp() + moov(mvhd(), video(), box(b'udta', size=4000)) + mdat(),
            'moov claims one byte too many': ftyp() + box(b'moov', mvhd(), video(), size=len(moov(mvhd(), video())) + 1) + mdat(),
        })

    def test_slack_inside_a_container_must_be_a_box(self):
        self.assertRejects(ftyp() + moov(mvhd(), video(), bytes(5)) + mdat())
        self.assertRejects(ftyp() + moov(mvhd(), video(), bytes(8)) + mdat())

    def test_size_zero_is_only_the_last_top_level_box(self):
        index = moov(mvhd(), video())
        self.assertTrue(self.probe(ftyp() + index + box(b'mdat', bytes(40), size=0)).supports_streaming)
        self.assertFalse(self.probe(ftyp() + box(b'mdat', bytes(40)) + box(b'moov', mvhd(), video(), size=0)).supports_streaming)
        self.assertTrue(self.probe(ftyp() + index + box(b'mdat') + box(b'free', bytes(9), size=0)).supports_streaming)
        self.assertRejectsAll({
            'size 0 inside moov': ftyp() + moov(mvhd(), video(), box(b'free', size=0)) + mdat(),
            'size 0 container inside moov': ftyp() + moov(mvhd(), box(b'trak', size=0)) + mdat(),
            'size 0 nested deeper': ftyp() + moov(mvhd(), video(extra=box(b'free', bytes(8), size=0))) + mdat(),
            'size 0 swallows moov': ftyp() + box(b'mdat', index, size=0),
            'size 0 free before moov swallows it': ftyp() + box(b'free', index, mdat(), size=0),
        })

    def test_zero_progress_sizes_refuse(self):
        for length in range(1, 8):
            with self.subTest(length=length, where='top level'):
                self.assertRejects(movie() + struct.pack('>I4s', length, b'free') + bytes(16))
            with self.subTest(length=length, where='nested'):
                self.assertRejects(ftyp() + moov(mvhd(), video(), struct.pack('>I4s', length, b'free') + bytes(16)) + mdat())
        for length in (0, 8, 9, 15):
            with self.subTest(large=length):
                self.assertRejects(movie() + struct.pack('>I4sQ', 1, b'free', length) + bytes(16))

    def test_header_only_boxes_make_progress(self):
        self.probe(ftyp() + box(b'free') + moov(mvhd(), video(), box(b'free'), box(b'udta')) + box(b'skip') + mdat())

    def test_largesize_boxes(self):
        index = moov(mvhd(), video())
        self.assertTrue(self.probe(ftyp() + index + mdat(large=True)).supports_streaming)
        self.assertTrue(self.probe(ftyp() + moov(mvhd(), video(), large=True) + mdat()).supports_streaming)
        self.assertFalse(self.probe(ftyp(large=True) + mdat(large=True) + moov(mvhd(), video(), large=True)).supports_streaming)
        large_child = box(b'trak', tkhd(), box(b'mdia', hdlr(b'vide'), box(b'minf', box(
            b'stbl', stsd(b'hvc1'), large=True), large=True), large=True), large=True)
        self.assertEqual(self.probe(ftyp() + moov(mvhd(), large_child) + mdat()).codec, 'hevc')
        self.assertRejectsAll({
            'largesize 0': ftyp() + index + box(b'mdat', size=0, large=True),
            'largesize below 16': ftyp() + index + box(b'mdat', bytes(8), size=12, large=True),
            'largesize past file': ftyp() + index + box(b'mdat', bytes(8), size=10_000, large=True),
            'largesize 2**63': ftyp() + index + box(b'mdat', bytes(8), size=1 << 63, large=True),
            'largesize 2**64-1': ftyp() + index + box(b'mdat', bytes(8), size=(1 << 64) - 1, large=True),
            'largesize header cut': ftyp() + index + struct.pack('>I4sI', 1, b'mdat', 0),
            'largesize nested past parent': ftyp() + moov(mvhd(), video(), box(b'free', bytes(8), size=1 << 40, large=True)) + mdat(),
            'largesize nested header cut': ftyp() + box(b'moov', mvhd(), video(), struct.pack('>I4sI', 1, b'free', 0)) + mdat(),
            'largesize moov past file': ftyp() + box(b'moov', mvhd(), video(), size=1 << 40, large=True) + mdat(),
        })

    def test_total_box_budget(self):
        free = box(b'free')
        index = moov(mvhd(), video())
        self.assertEqual(self.probe(ftyp() + index + mdat()).width, 1280)
        spare = video_probe.MAX_BOXES - BASE_BOXES
        self.assertEqual(spare, 4085)
        self.probe(ftyp() + index + mdat() + free * spare)
        self.probe(ftyp() + moov(mvhd(), video(), free * spare) + mdat())
        self.probe(ftyp() + moov(mvhd(), video(extra=free * spare)) + mdat())
        self.assertRejectsAll({
            'top level over budget by one': ftyp() + index + mdat() + free * (spare + 1),
            'moov children over budget by one': ftyp() + moov(mvhd(), video(), free * (spare + 1)) + mdat(),
            'trak children over budget by one': ftyp() + moov(mvhd(), video(extra=free * (spare + 1))) + mdat(),
            'split across levels': ftyp() + moov(mvhd(), video(), free * 2000) + mdat() + free * 2086,
            'far over': ftyp() + index + mdat() + free * 20000,
        })

    def test_nesting_depth(self):
        def chain(levels):
            inner = b''
            for _ in range(levels):
                inner = box(b'trak', inner)
            return inner
        # moov is depth 1, the real trak depth 2, so six nested trak boxes end at depth 8.
        self.assertEqual(self.probe(ftyp() + moov(mvhd(), video(extra=chain(6))) + mdat()).codec, 'h264')
        self.assertRejects(ftyp() + moov(mvhd(), video(extra=chain(7))) + mdat())
        self.assertRejects(ftyp() + moov(mvhd(), video(extra=chain(40))) + mdat())
        self.assertRejects(ftyp() + moov(mvhd(), video(), chain(8)) + mdat())

    def test_moov_payload_cap(self):
        kids = mvhd() + video()
        for excess, accepted in ((0, True), (1, False)):
            with self.subTest(excess=excess):
                padding = video_probe.MAX_MOOV_BYTES - len(kids) - 8 + excess
                data = ftyp() + moov(kids, box(b'free', bytes(padding))) + mdat()
                self.assertEqual(len(data) - len(ftyp()) - len(mdat()) - 8, video_probe.MAX_MOOV_BYTES + excess)
                if accepted:
                    self.assertEqual(self.probe(data).codec, 'h264')
                else:
                    self.assertRejects(data)

    def test_oversized_moov_is_refused_before_it_is_read(self):
        path = self.sparse(ftyp() + struct.pack('>I4s', 40 * MIB, b'moov'), 41 * MIB)
        with mock.patch.object(os, 'pread', wraps=os.pread) as reads:
            self.assertRefused(lambda: probe_video(path, size_bytes=41 * MIB))
        self.assertLess(max(call.args[1] for call in reads.call_args_list), 4096)


class FileTests(ProbeTestCase):
    def test_size_must_match_exactly(self):
        data = movie()
        path = self.write(data)
        self.assertEqual(probe_video(path, size_bytes=len(data)).codec, 'h264')
        for claimed in (len(data) - 1, len(data) + 1, len(data) + 64 * MIB, 0, -1):
            with self.subTest(claimed=claimed):
                self.assertRefused(lambda: probe_video(path, size_bytes=claimed))

    def test_size_bytes_must_be_a_plain_int(self):
        data = movie()
        path = self.write(data)
        for claimed in (True, float(len(data)), str(len(data)), None, b'%d' % len(data)):
            with self.subTest(claimed=claimed):
                self.assertRefused(lambda: probe_video(path, size_bytes=claimed))

    def test_file_cap_is_64_mib_inclusive(self):
        cap = video_probe.MAX_FILE_BYTES
        self.assertEqual(cap, 64 * MIB)
        index = ftyp() + moov(mvhd(), video())
        head = index + struct.pack('>I4s', cap - len(index), b'mdat')
        exact = self.sparse(head, cap)
        self.assertEqual(probe_video(exact, size_bytes=cap).codec, 'h264')
        over_head = index + struct.pack('>I4s', cap + 1 - len(index), b'mdat')
        over = self.sparse(over_head, cap + 1)
        self.assertRefused(lambda: probe_video(over, size_bytes=cap + 1))
        self.assertRefused(lambda: probe_video(over, size_bytes=cap))

    def test_symlink_is_refused(self):
        data = movie()
        target = self.write(data)
        link = self.root / 'link.mp4'
        link.symlink_to(target)
        self.assertEqual(probe_video(target, size_bytes=len(data)).codec, 'h264')
        self.assertRefused(lambda: probe_video(link, size_bytes=len(data)))
        dangling = self.root / 'dangling.mp4'
        dangling.symlink_to(self.root / 'nowhere.mp4')
        self.assertRefused(lambda: probe_video(dangling, size_bytes=len(data)))

    def test_non_regular_and_missing_paths_are_refused(self):
        fifo = self.root / 'pipe.mp4'
        os.mkfifo(fifo)
        directory = self.root / 'folder.mp4'
        directory.mkdir()
        for name, path in (('fifo', fifo), ('directory', directory), ('missing', self.root / 'gone.mp4'),
                           ('nul byte', Path(str(self.root) + '/a\0b.mp4')), ('device', Path('/dev/null'))):
            with self.subTest(name):
                self.assertRefused(lambda: probe_video(path, size_bytes=len(movie())))

    def test_file_changed_while_probing_is_refused(self):
        data = movie()
        path = self.write(data)
        real = video_probe._top

        def grow(*arguments):
            outcome = real(*arguments)
            with open(path, 'ab') as handle:
                handle.write(b'x')
            return outcome

        with mock.patch.object(video_probe, '_top', grow):
            self.assertRefused(lambda: probe_video(path, size_bytes=len(data)))

    def test_descriptor_is_closed_on_every_outcome(self):
        good, bad = self.write(movie()), self.write(movie()[:-3])
        before = len(os.listdir('/dev/fd'))
        for _ in range(20):
            probe_video(good, size_bytes=len(movie()))
            self.assertRefused(lambda: probe_video(bad, size_bytes=len(movie()) - 3))
        self.assertEqual(len(os.listdir('/dev/fd')), before)


class ContractTests(ProbeTestCase):
    def test_error_shape(self):
        self.assertTrue(issubclass(VideoProbeError, ValueError))
        error = VideoProbeError()
        self.assertEqual((error.code, str(error), error.args), ('unsupported_video', 'unsupported_video', ('unsupported_video',)))
        self.assertEqual(VideoProbeError.code, 'unsupported_video')

    def test_refusals_never_carry_file_bytes_or_chain(self):
        hostile = {
            'canary codec': movie(mvhd(), video(CANARY[:4])),
            'canary in free box and codec': ftyp() + moov(mvhd(), video(stsd_box=stsd(CANARY))) + box(b'free', CANARY) + mdat(),
            'canary brand': ftyp(CANARY[:4], (CANARY[4:8],)) + moov(mvhd(), video()) + mdat(),
            'canary handler': movie(mvhd(), video(), trak(CANARY[:4], b'text', width=0, height=0)),
        }
        self.assertRejectsAll(hostile)

    def test_facts_are_frozen(self):
        facts = self.probe(movie())
        self.assertTrue(dataclasses.is_dataclass(VideoFacts))
        self.assertEqual([field.name for field in dataclasses.fields(VideoFacts)],
                         ['duration_seconds', 'width', 'height', 'codec', 'has_audio', 'supports_streaming'])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            facts.width = 1
        self.assertEqual(hash(facts), hash(self.probe(movie())))

    def test_module_limits(self):
        self.assertEqual((video_probe.MAX_BOXES, video_probe.MAX_DEPTH, video_probe.MAX_MOOV_BYTES, video_probe.MAX_SECONDS),
                         (4096, 8, 16 * MIB, 10800))

    def test_mutated_bytes_never_escape_the_error_contract(self):
        base = movie(mvhd(), video(), audio())
        path = self.write(base)
        outcomes = set()
        for at in range(len(base)):
            for value in (0x00, 0x01, 0x7F, 0xFF):
                if base[at] == value:
                    continue
                path.write_bytes(base[:at] + bytes([value]) + base[at + 1:])
                try:
                    probe_video(path, size_bytes=len(base))
                except VideoProbeError as error:
                    self.assertEqual((error.code, str(error)), ('unsupported_video', 'unsupported_video'))
                    self.assertIsNone(error.__context__)
                    outcomes.add('refused')
                else:
                    outcomes.add('accepted')
        self.assertEqual(outcomes, {'refused', 'accepted'})

    def test_probe_does_not_read_media_payload(self):
        index = moov(mvhd(), video())
        head = ftyp() + index + struct.pack('>I4s', 60 * MIB, b'mdat')
        path = self.sparse(head, len(head) + 60 * MIB - 8)
        with mock.patch.object(os, 'pread', wraps=os.pread) as reads:
            self.assertEqual(probe_video(path, size_bytes=len(head) + 60 * MIB - 8).supports_streaming, True)
        self.assertLessEqual(sum(call.args[1] for call in reads.call_args_list), 2 * len(head) + 64)


if __name__ == '__main__':
    unittest.main()
