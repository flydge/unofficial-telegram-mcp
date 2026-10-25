"""Hash-only characterization matrix of the reply-source ladder (0.58 freeze).

`fixtures/reply_ladder_matrix_0_57.json` was generated once by this module at the 0.57 tip, before the ladder
table existed, and is pinned by sha256 in `test_reply_ladder_matrix.py`; it is never regenerated. The test
recomputes every row from the code under test and compares the serialized bytes.

A row (COLUMNS) records what a source becomes, never its bytes: the projection version, the capability tuple
(`reply_X_targets` written `X`), `is_media`, `sanitized`, and sha256 digests cut to 16 hex digits of the
projection, the complete display and the preview facts of one fixed text reply (none for a topic source), plus
every forged version in 0..31 that the stored projection would also accept (always empty). A refused input
records only `null` columns: refusal messages are internal and not frozen.

Cases (labels code the families S styled, L lexical, I identity, U url, E email, P phone, B bank card,
T TextUrl, '-' none):
- `subset`: every subset of the eight span families x {text, document, photo, voice note} x the datetime levels
  (absent, plain, relative, absolute, all three), one disjoint span per family in a fixed text;
- `dirty`: single families and datetime levels with a body the display sanitizer rewrites;
- `topic`: a forum-topic source per kind and a few families;
- `probe`: CustomEmoji and MediaTimestamp shapes, all refused at 0.57;
- `v23`: the inline TextUrl literals of `test_reply_text_url_targets.py`, rebuilt here;
- `fixture`: every row of the existing reply goldens, from their recorded raw input.

`python tests/reply_ladder_matrix.py OUTPUT` writes the current matrix to OUTPUT for a diff, under a throwaway
HOME; the pinned fixture itself is never an output.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if __name__ == '__main__':  # as tests/conftest.py: HOME must change before the package is imported
    _HOME = tempfile.mkdtemp(prefix='tgmcp-matrix-home-')
    os.environ['HOME'] = _HOME

from telegram_search_mcp.draft_models import DraftPreview  # noqa: E402
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, ReplySource, canonical, source_from_message  # noqa: E402

FIXTURE = Path(__file__).parent / 'fixtures' / 'reply_ladder_matrix_0_57.json'
GOLDENS = ('reply_source_v1_v7.json', 'reply_email_legacy_goldens.json', 'reply_email_v17_goldens.json',
           'reply_phone_v18_goldens.json', 'reply_phone_v19_goldens.json', 'reply_bank_card_v20_goldens.json',
           'reply_bank_card_v21_goldens.json', 'reply_text_url_v22_goldens.json')
COLUMNS = ('label', 'version', 'capabilities', 'is_media', 'sanitized', 'projection', 'display', 'preview',
           'forged_accepted')
CHAT, MESSAGE, TOPIC = 123, 55, 9
KINDS = ('text', 'document', 'photo', 'voice_note')
FORGED = range(32)
DIGEST = 16  # hex digits kept per digest; the whole file is pinned by its full sha256
DRAFT = {'account_id': 7, 'draft_id': 'draft_' + '1' * 32, 'expires_at': '2030-01-01T00:00:00Z', 'kind': 'text',
         'recipient': CHAT, 'recipient_title': 'Recipient',
         'sha256': '09c876bab6e80384df62e57c527bae3bac339515aa967086eb18866c40f54477', 'size_bytes': 11,
         'text': 'Final reply'}
URL = 'https://example.invalid/a?x=1&y=2'


def entity(kind, offset, length, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **fields}}


def message(text, entities):
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE,
            'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
            'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0, 'auto_delete_in': 0,
            'content': {'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text,
                                                         'entities': entities}}}


def media(kind, caption):
    file = {'@type': 'file', 'id': 1, 'size': 4, 'expected_size': 4,
            'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False, 'download_offset': 0,
                      'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'ladder-fixture',
                       'is_uploading_active': False, 'is_uploading_completed': True, 'uploaded_size': 4}}
    if kind == 'document':
        return {'@type': 'messageDocument', 'caption': caption, 'document': {
            '@type': 'document', 'file_name': 'fixture.txt', 'mime_type': 'text/plain', 'document': file}}
    if kind == 'photo':
        return {'@type': 'messagePhoto', 'caption': caption, 'has_spoiler': False, 'is_secret': False,
                'show_caption_above_media': False, 'photo': {'@type': 'photo', 'has_stickers': False, 'sizes': [
                    {'@type': 'photoSize', 'type': 'i', 'width': 2, 'height': 2, 'photo': file,
                     'progressive_sizes': []}]}}
    return {'@type': 'messageVoiceNote', 'caption': caption, 'is_listened': False, 'voice_note': {
        '@type': 'voiceNote', 'duration': 2, 'waveform': 'AAAA', 'mime_type': 'audio/ogg', 'voice': file}}


def of_kind(kind, raw):
    if kind != 'text':
        raw['content'] = media(kind, raw['content']['text'])
    return raw


def absolute():
    return {'@type': 'dateTimeFormattingTypeAbsolute', 'time_precision': {'@type': 'dateTimePartPrecisionNone'},
            'date_precision': {'@type': 'dateTimePartPrecisionLong'}, 'show_day_of_week': False}


# One disjoint span per family; UTF-16 offsets count the leading astral emoji as two units.
BODY = '😀 S#tag@userDxyUEPBT end'
DIRTY = '😀 Ｓ#tag@userDxyUEPBT  end'
# Label codes: S styled, L lexical, I identity, U url, E email, P phone, B bank card, T TextUrl; '-' is none.
FAMILIES = (('S', lambda: entity('Bold', 3, 1)),
            ('L', lambda: entity('Hashtag', 4, 4)),
            ('I', lambda: entity('MentionName', 8, 5, user_id=42)),
            ('U', lambda: entity('Url', 16, 1)),
            ('E', lambda: entity('EmailAddress', 17, 1)),
            ('P', lambda: entity('PhoneNumber', 18, 1)),
            ('B', lambda: entity('BankCardNumber', 19, 1)),
            ('T', lambda: entity('TextUrl', 20, 1, url=URL)))
DATETIME = {'absent': (), 'plain': ((13, None),), 'relative': ((13, {'@type': 'dateTimeFormattingTypeRelative'}),),
            'absolute': ((13, absolute()),),
            'all': ((13, None), (14, {'@type': 'dateTimeFormattingTypeRelative'}), (15, absolute()))}


def spans(codes, level):
    chosen = [build() for code, build in FAMILIES if code in codes]
    return chosen + [entity('DateTime', offset, 1, unix_time=1700000000, formatting_type=fmt)
                     for offset, fmt in DATETIME[level]]


def subset_cases():
    for kind in KINDS:
        for mask in itertools.product((False, True), repeat=len(FAMILIES)):
            codes = ''.join(code for (code, _), on in zip(FAMILIES, mask) if on)
            for level in DATETIME:
                yield (f'subset/{kind}/{codes or "-"}/{level}',
                       of_kind(kind, message(BODY, spans(codes, level))), None)


def dirty_cases():
    for kind in KINDS:
        for codes, level in (('', 'absent'), *((code, 'absent') for code, _ in FAMILIES),
                             *(('', level) for level in DATETIME if level != 'absent')):
            yield (f'dirty/{kind}/{codes or "-"}/{level}',
                   of_kind(kind, message(DIRTY, spans(codes, level))), None)


def topic_cases():
    for kind in KINDS:
        for codes, level in (('', 'absent'), ('S', 'absent'), ('LI', 'relative'), ('UB', 'absolute'),
                             ('PT', 'all')):
            raw = of_kind(kind, message(BODY, spans(codes, level)))
            raw['topic_id'] = {'@type': 'messageTopicForum', 'forum_topic_id': TOPIC}
            yield f'topic/{kind}/{codes or "-"}/{level}', raw, TOPIC


def probe_cases():
    emoji = '1234567890123456789'  # sequential synthetic int64 id, as TDLib's JSON writes it: a decimal string
    probes = {'custom_emoji_string': ('😀 x', [entity('CustomEmoji', 0, 2, custom_emoji_id=emoji)]),
              'custom_emoji_integer': ('😀 x', [entity('CustomEmoji', 0, 2, custom_emoji_id=int(emoji))]),
              'custom_emoji_styled': ('😀 x', [entity('Bold', 0, 4), entity('CustomEmoji', 0, 2, custom_emoji_id=emoji)]),
              'media_timestamp': ('0:01 x', [entity('MediaTimestamp', 0, 4, media_timestamp=1)]),
              'media_timestamp_beyond_duration': ('0:03 x', [entity('MediaTimestamp', 0, 4, media_timestamp=3)]),
              'media_timestamp_styled': ('0:01 x', [entity('Italic', 0, 6),
                                                   entity('MediaTimestamp', 0, 4, media_timestamp=1)])}
    for kind in KINDS:
        for name, (text, entities) in probes.items():
            yield f'probe/{kind}/{name}', of_kind(kind, message(text, entities)), None


def v23_cases():
    def text_url(offset=0, length=1, url=URL):
        return entity('TextUrl', offset, length, url=url)
    hidden = ('', 'Ab_9-Z', ' ', '\t\n\r', '\x00', '\x01\x1f\x7f\x85\x9f', '‮', r'‮', 'Ａ', 'A', '😀',
              '<b>[x](y)</b>', 'https://x.invalid/a?b=1&c=2', 'tg://user?id=42', 'mailto:a@b.invalid', 'tel:+123',
              'javascript:alert(1)', 'file:///tmp/a', '"\\', 'a  b', 'a b', 'a\tb', 'a\nb', 'x' * 4096, '😀' * 4096)
    yield 'v23/exact', message('Read 😀 docs', [text_url(8, 4)]), None
    yield 'v23/styled', message('Read 😀 docs', [text_url(8, 4), entity('Bold', 8, 4)]), None
    for index, value in enumerate(hidden):
        yield f'v23/hidden/{index:02d}', message('x', [text_url(url=value)]), None
    for index, (text, length) in enumerate((('x\tZ', 1), ('x\nZ', 1), ('😀ＡZ', 2), ('Ａ', 1), ('A', 1), ('A\tB', 1),
                                            ('A B', 1), ('A\nB', 1), ('😀Ａ\tB', 3),
                                            ('x <b>[go](https://x.invalid)</b> @name 😀', 1))):
        yield f'v23/visible/{index:02d}', message(text, [text_url(length=length, url='')]), None
    overhead = 255  # len(marker + 'Formatted target: ' + the single-span record with an empty URL)
    for name, value in (('edge', 'a' * (4096 - overhead)), ('edge+1', 'a' * (4097 - overhead)),
                        ('dots', '.' * ((4096 - overhead) // 6) + 'a' * ((4096 - overhead) % 6)),
                        ('dots+1', '.' * ((4096 - overhead) // 6) + 'a' * ((4096 - overhead) % 6) + '.')):
        yield f'v23/bound/{name}', message('x', [text_url(url=value)]), None
    yield 'v23/precode', message('xZ', [text_url(url=''), entity('PreCode', 1, 1, language='Ａ')]), None
    yield 'v23/32', message('x' * 32, [text_url(url='')] + [entity('Bold', i, 1) for i in range(1, 32)]), None
    yield 'v23/33', message('x' * 33, [text_url(url='')] + [entity('Bold', i, 1) for i in range(1, 33)]), None
    mixed = (text_url(0, 1, 'OpaqueA'), entity('BankCardNumber', 1, 1), entity('PhoneNumber', 2, 1),
             entity('EmailAddress', 3, 1), entity('Url', 4, 1),
             entity('DateTime', 5, 1, unix_time=1, formatting_type=absolute()),
             entity('Hashtag', 6, 4), entity('MentionName', 10, 5, user_id=42))
    for mask in itertools.product((False, True), repeat=7):
        chosen = [mixed[0]] + [span for span, on in zip(mixed[1:], mask) if on]
        yield (f'v23/mixed/{"".join("1" if on else "0" for on in mask)}',
               message('TBPEUD#tag@user', copy.deepcopy(chosen)), None)
    for name, fmt in (('plain', None), ('relative', {'@type': 'dateTimeFormattingTypeRelative'})):
        chosen = copy.deepcopy(list(mixed))
        chosen[5]['type']['formatting_type'] = fmt
        yield f'v23/mixed-datetime/{name}', message('TBPEUD#tag@user', chosen), None


def fixture_cases():
    for name in GOLDENS:
        rows = json.loads((Path(__file__).parent / 'fixtures' / name).read_text())
        for index, row in enumerate(rows):
            yield f'fixture/{name[:-5]}/{index:03d}', row['raw'], None


def capabilities(names):
    """One space-separated string: reply_X_targets is written X, any other name in full."""
    return ' '.join(name[6:-8] if name.startswith('reply_') and name.endswith('_targets') else name
                    for name in names)


def short(value):
    return None if value is None else value[:DIGEST]


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def row(label, raw, topic):
    try:
        source = source_from_message(copy.deepcopy(raw), CHAT, MESSAGE, expected_forum_topic_id=topic)
        target = source.target()
    except ValueError:
        return [label, None, None, None, None, None, None, None, None]
    stored = ReplySource(source.projection_json)
    if (stored.target() != target or stored.required_capabilities != source.required_capabilities or
            stored.is_media != source.is_media or stored.source_sha256 != source.source_sha256):
        raise AssertionError('stored projection disagrees with its source: ' + label)
    value = json.loads(source.projection_json)
    forged = []
    for version in FORGED:
        if version == value['version']:
            continue
        try:
            ReplySource(canonical({**value, 'version': version}))
        except ValueError:
            continue
        forged.append(version)
    preview = None
    if topic is None:
        preview = ReplyDraftPreview.create(DraftPreview.model_validate(DRAFT), source).preview_sha256
    return [label, value['version'], capabilities(source.required_capabilities), source.is_media, target.sanitized,
            short(source.source_sha256), short(sha(target.text)), short(preview), forged]


def cases():
    yield from subset_cases()
    yield from dirty_cases()
    yield from topic_cases()
    yield from probe_cases()
    yield from v23_cases()
    yield from fixture_cases()


def rows():
    result = [row(label, raw, topic) for label, raw, topic in cases()]
    labels = [entry[0] for entry in result]
    if len(set(labels)) != len(labels):
        raise AssertionError('matrix labels must be unique')
    return result


def serialize(matrix) -> bytes:
    lines = ',\n'.join(json.dumps(entry, separators=(',', ':'), ensure_ascii=True) for entry in matrix)
    return ('{"columns":' + json.dumps(list(COLUMNS), separators=(',', ':')) + ',"rows":[\n' + lines +
            '\n]}\n').encode('ascii')


def main(argv):
    if len(argv) != 2 or Path(argv[1]).resolve() == FIXTURE.resolve():
        raise SystemExit('usage: reply_ladder_matrix.py OUTPUT (never the pinned fixture)')
    Path(argv[1]).write_bytes(serialize(rows()))


if __name__ == '__main__':
    try:
        main(sys.argv)
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
