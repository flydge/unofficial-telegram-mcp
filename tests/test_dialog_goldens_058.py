"""0.58 owner-dialog goldens (criterion 6): one reply per source family and one send per styled caption kind.

A reply's dialog keeps the bound rendering of its target and now adds one row per stored target entity: its style or
family, the exact covered source text and its hidden payload (a text link's URL, a custom emoji's id, a voice note's
media timestamp, a mentioned user, a date). The rows come from the stored source projection, which must render the
reviewed target exactly; ReplyTarget gains no field. A styled caption lists its styles with their covered text and
the draft's preview digest. The documents are pinned byte for byte in fixtures/dialog_goldens_058.json; no real
dialog runs (render_approval builds the document only).
"""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from approval_layout import logical
from telegram_search_mcp.approval_prompt import render_approval
from telegram_search_mcp.draft_models import DraftPreview, artifact_preview_digest
from telegram_search_mcp.reply_artifact_drafts import ReplyArtifactDraftPreview
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, ReplyTextDraftPreview, source_from_message
from telegram_search_mcp.video_drafts import VideoDraftPreview, VideoRecipient
from telegram_search_mcp.video_probe import VideoFacts

FIXTURE = Path(__file__).parent / 'fixtures' / 'dialog_goldens_058.json'
FIXTURE_SHA256 = 'a0357a0b8e89e80e1ad41e84e7fe6277630d8e338c61bd7d6b9ec952ec92faaa'
CHAT, MESSAGE, ACCOUNT = 123, 55, 7
DID = 'draft_' + '1' * 32
EXPIRES = '2030-01-02T03:04:05+00:00'
TITLE = 'Synthetic Recipient'
EMOJI_ID = '5368324170671202286'
REPLY = 'Thanks, noted.'
CAPTION = 'Report 😀 final'
STYLES = [{'offset': 0, 'length': 6, 'type': {'@type': 'textEntityTypeBold'}},
          {'offset': 7, 'length': 2, 'type': {'@type': 'textEntityTypeSpoiler'}}]


def entity(kind, offset, length, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **fields}}


def absolute():
    return {'@type': 'dateTimeFormattingTypeAbsolute', 'time_precision': {'@type': 'dateTimePartPrecisionShort'},
            'date_precision': {'@type': 'dateTimePartPrecisionLong'}, 'show_day_of_week': True}


# One source text per family, each span over a short word; every value is synthetic.
TEXT_SOURCES = {
    'styled': ('Please review the bold part', [entity('Bold', 18, 4), entity('Italic', 23, 4)]),
    'lexical': ('Tag #release here', [entity('Hashtag', 4, 8)]),
    'identity': ('Ask @alice and Bob', [entity('Mention', 4, 6), entity('MentionName', 15, 3, user_id=42)]),
    'datetime': ('Meet at noon', [entity('DateTime', 8, 4, unix_time=1700000000)]),
    'datetime_relative': ('Due in two days', [entity('DateTime', 7, 8, unix_time=1700172800,
                                                     formatting_type={'@type': 'dateTimeFormattingTypeRelative'})]),
    'datetime_absolute': ('Starts Friday', [entity('DateTime', 7, 6, unix_time=1700179200,
                                                   formatting_type=absolute())]),
    'url': ('See https://example.org/docs now', [entity('Url', 4, 24)]),
    'email': ('Mail team@example.org today', [entity('EmailAddress', 5, 16)]),
    'phone': ('Call 555-0100 now', [entity('PhoneNumber', 5, 8)]),
    'bank_card': ('Card XXXX-0000 here', [entity('BankCardNumber', 5, 9)]),
    'text_url': ('Read the guide first', [entity('TextUrl', 9, 5, url='https://example.org/guide')]),
    'custom_emoji': ('Nice 😀 work', [entity('CustomEmoji', 5, 2, custom_emoji_id=EMOJI_ID)]),
}


def file_fixture():
    return {'@type': 'file', 'id': 1, 'size': 4, 'expected_size': 4,
            'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False, 'download_offset': 0,
                      'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'golden', 'is_uploading_active': False,
                       'is_uploading_completed': True, 'uploaded_size': 4}}


def message(content):
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE, 'sender_id': {'@type': 'messageSenderUser',
            'user_id': 8}, 'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0, 'auto_delete_in': 0, 'content': content}


def text_source(text, entities):
    return source_from_message(message({'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text,
                                                                         'entities': entities}}), CHAT, MESSAGE)


def document_source(caption, entities):
    return source_from_message(message({'@type': 'messageDocument', 'caption': {
        '@type': 'formattedText', 'text': caption, 'entities': entities}, 'document': {
        '@type': 'document', 'file_name': 'guide.pdf', 'mime_type': 'application/pdf', 'document': file_fixture()}}),
        CHAT, MESSAGE)


def voice_source(caption, entities):
    return source_from_message(message({'@type': 'messageVoiceNote', 'caption': {
        '@type': 'formattedText', 'text': caption, 'entities': entities}, 'is_listened': False, 'voice_note': {
        '@type': 'voiceNote', 'duration': 4, 'waveform': 'AAAA', 'mime_type': 'audio/ogg', 'voice': file_fixture()}}),
        CHAT, MESSAGE)


def reply_arguments(source):
    data = REPLY.encode()
    content = ReplyTextDraftPreview(draft_id=DID, account_id=ACCOUNT, recipient=CHAT, recipient_title=TITLE,
                                    kind='text', expires_at=EXPIRES, sha256=hashlib.sha256(data).hexdigest(),
                                    size_bytes=len(data), text=REPLY)
    preview = ReplyDraftPreview.create(content, source).model_dump(mode='json')
    return dict(recipient_title=TITLE, recipient=CHAT, display_name='Text message', size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), caption='', kind='text', text=REPLY, reply_preview=preview,
                reply_source=source.projection_json)


def artifact_draft(kind='document', caption=CAPTION, styles=STYLES):
    data = b'synthetic file'
    sha = hashlib.sha256(data).hexdigest()
    names = {'document': ('report.txt', 'text/plain'), 'photo': ('shot.png', 'image/png'),
             'voice_note': ('memo.ogg', 'audio/ogg')}[kind]
    voice = (dict(duration_seconds=3, waveform_base64='AAAA', source_sha256='b' * 64, source_display_name='memo.m4a',
                  converted=True) if kind == 'voice_note' else {})
    value = dict(draft_id=DID, account_id=ACCOUNT, recipient=CHAT, recipient_title=TITLE, kind=kind,
                 expires_at=EXPIRES, sha256=sha, size_bytes=len(data), artifact_id=f'artifact_{"c" * 32}_{sha}_14',
                 display_name=names[0], mime_type=names[1], caption=caption, **voice)
    if styles:
        value['outgoing_entities'] = copy.deepcopy(styles)
        value['preview_sha256'] = artifact_preview_digest(**{key: value[key] for key in (
            'draft_id', 'account_id', 'recipient', 'recipient_title', 'expires_at', 'kind', 'artifact_id', 'sha256',
            'size_bytes', 'display_name', 'mime_type', 'caption')}, outgoing_entities=copy.deepcopy(styles), **voice)
    return DraftPreview.model_validate(value)


def artifact_arguments(draft, **extra):
    return dict(recipient_title=TITLE, recipient=CHAT, display_name=draft.display_name, size_bytes=draft.size_bytes,
                sha256=draft.sha256, caption=draft.caption, kind=draft.kind, duration_seconds=draft.duration_seconds,
                source_sha256=draft.source_sha256, source_display_name=draft.source_display_name, **extra)


def cases():
    documents = {}
    for family, (text, entities) in TEXT_SOURCES.items():
        documents[f'reply_text/{family}'] = reply_arguments(text_source(text, entities))
    documents['reply_text/media_text_url'] = reply_arguments(document_source(
        'Read the guide first', [entity('TextUrl', 9, 5, url='https://example.org/guide')]))
    documents['reply_text/media_timestamp'] = reply_arguments(voice_source(
        'Listen from 0:03', [entity('MediaTimestamp', 12, 4, media_timestamp=3)]))
    for kind in ('document', 'photo', 'voice_note'):
        draft = artifact_draft(kind)
        documents[f'artifact/{kind}'] = artifact_arguments(draft, draft_preview=draft.model_dump(mode='json'))
    source = text_source(*TEXT_SOURCES['text_url'])
    reply = ReplyArtifactDraftPreview.create(artifact_draft(), source)
    documents['reply_artifact/document'] = artifact_arguments(reply.draft, reply_artifact_preview=reply.model_dump(
        mode='json'), reply_source=source.projection_json)
    movie = 'a' * 64
    video = VideoDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRES,
        recipient=VideoRecipient(chat_id=CHAT, title=TITLE), artifact_id=f'artifact_{"c" * 32}_{movie}_9',
        display_name='clip.mp4', size_bytes=9, sha256=movie, facts=VideoFacts(5, 1280, 720, 'h264', False, True),
        caption=CAPTION, outgoing_entities=copy.deepcopy(STYLES))
    documents['video'] = dict(recipient_title=TITLE, recipient=CHAT, display_name='clip.mp4', size_bytes=9,
                              sha256=movie, caption=CAPTION, kind='video', video_preview=video.model_dump(mode='json'))
    return documents


def rendered():
    result = {}
    for name, arguments in cases().items():
        document = render_approval(**arguments)
        assert document is not None, name
        result[name] = document[0]
    return result


class DialogGoldenTests(unittest.TestCase):
    def test_every_family_and_styled_caption_matches_its_pinned_document(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURE_SHA256)
        self.assertEqual(rendered(), json.loads(raw))

    def test_each_reply_states_its_hidden_payload_and_styles_as_rows(self):
        documents = {name: logical(document) for name, document in rendered().items()}
        header = 'Target styles and hidden payloads [untrusted Telegram evidence] (exact source text; UTF-16 code units):'
        expected = {
            'styled': ['Bold: offset 18, length 4; covered «bold»', 'Italic: offset 23, length 4; covered «part»'],
            'lexical': ['Hashtag: offset 4, length 8; covered «#release»'],
            'identity': ['Mention: offset 4, length 6; covered «@alice»',
                         'MentionName: offset 15, length 3; covered «Bob»; mentions user 42'],
            'datetime': ['DateTime: offset 8, length 4; covered «noon»; unix time 1700000000; no formatting'],
            'datetime_relative': ['DateTime: offset 7, length 8; covered «two days»; unix time 1700172800; '
                                  'relative formatting'],
            'datetime_absolute': ['DateTime: offset 7, length 6; covered «Friday»; unix time 1700179200; absolute '
                                  'formatting (time short, date long, day of week shown)'],
            'url': ['Url: offset 4, length 24; covered «https://example.org/docs»'],
            'email': ['EmailAddress: offset 5, length 16; covered «team@example.org»'],
            'phone': ['PhoneNumber: offset 5, length 8; covered «555-0100»'],
            'bank_card': ['BankCardNumber: offset 5, length 9; covered «XXXX-0000»'],
            'text_url': ['TextUrl: offset 9, length 5; covered «guide»; hidden URL «https://example.org/guide»'],
            'custom_emoji': [f'CustomEmoji: offset 5, length 2; covered «😀»; custom emoji id {EMOJI_ID}'],
            'media_text_url': ['TextUrl: offset 9, length 5; covered «guide»; hidden URL «https://example.org/guide»'],
            'media_timestamp': ['MediaTimestamp: offset 12, length 4; covered «0:03»; media timestamp 3 seconds'],
        }
        for family, rows in expected.items():
            with self.subTest(family=family):
                document = documents[f'reply_text/{family}']
                self.assertIn('\n' + '\n'.join([header, *rows]) + '\n\nReply text (exact):', document)
        self.assertIn('\n' + header + '\nTextUrl: offset 9, length 5; covered «guide»; hidden URL '
                      '«https://example.org/guide»\n\nFile: «report.txt»', documents['reply_artifact/document'])

    def test_each_styled_caption_lists_its_styles_and_digest(self):
        documents = {name: logical(document) for name, document in rendered().items()}
        styles = ('Caption styles (exact caption; UTF-16 code units):\nBold: offset 0, length 6; covered «Report»\n'
                  'Spoiler: offset 7, length 2; covered «😀»')
        for name in ('artifact/document', 'artifact/photo', 'artifact/voice_note', 'reply_artifact/document', 'video'):
            with self.subTest(name=name):
                self.assertIn('│ «Report 😀 final»\n' + styles + '\n', documents[name])
                self.assertIn('\nPreview SHA-256: ', documents[name])
        for name in ('artifact/document', 'artifact/photo', 'artifact/voice_note'):
            self.assertTrue(documents[name].endswith('will revalidate this account and recipient, then make one '
                                                     'upload/send attempt.'))

    def test_a_projection_that_does_not_render_the_reviewed_target_shows_no_dialog(self):
        arguments = reply_arguments(text_source(*TEXT_SOURCES['text_url']))
        other = text_source('Read the guide first', [entity('TextUrl', 9, 5, url='https://example.org/other')])
        for changed in (other.projection_json, arguments['reply_source'] + ' ', 'not json'):
            with self.subTest(changed=changed[:20]):
                self.assertIsNone(render_approval(**{**arguments, 'reply_source': changed}))
        draft = artifact_draft()
        self.assertIsNone(render_approval(**artifact_arguments(draft, draft_preview=draft.model_dump(mode='json'),
                                                               reply_source=arguments['reply_source'])))
        plain = dict(arguments)
        del plain['reply_source']
        self.assertNotIn('Target styles and hidden payloads', render_approval(**plain)[0])

    def test_a_plain_caption_and_a_plain_target_keep_their_0_54_documents(self):
        draft = artifact_draft(styles=[])
        document = logical(render_approval(**artifact_arguments(draft, draft_preview=draft.model_dump(
            mode='json')))[0])
        self.assertNotIn('Caption styles', document)
        self.assertNotIn('Preview SHA-256', document)
        self.assertTrue(document.endswith('Unofficial Telegram MCP will make one upload/send attempt.'))
        plain = logical(render_approval(**reply_arguments(text_source('Plain source', [])))[0])
        self.assertNotIn('Target styles and hidden payloads', plain)


if __name__ == '__main__':
    unittest.main()
