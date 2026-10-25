"""0.58: a sent caption is read back exactly, plain or styled.

Review of 0.57 found a defect older than 0.57: an artifact reply's read-back required the sent caption's entities to
be [], so a caption with a link, mention or hashtag (which Telegram detects and returns as entities) never confirmed
and the send stayed outcome_unknown. A plain caption now tolerates exactly the auto-detected entities
(edit_drafts.exact_styles with no styles); a styled caption exactly its styles, in TDLib's split form, plus those.
Plain document, photo, voice-note and video sends gain the same caption digest. Only the raw transport is synthetic.
"""
from __future__ import annotations

import copy
import hashlib
import unittest
from collections import deque
from pathlib import Path

from telegram_search_mcp.send_observations import ObservationUnavailable, SendObservations
from telegram_search_mcp.tdjson import TDLibClient

CHAT = 123
CAPTION = 'See https://example.org @tests #tag'
LINK = [{'@type': 'textEntity', 'offset': 4, 'length': 19, 'type': {'@type': 'textEntityTypeUrl'}},
        {'@type': 'textEntity', 'offset': 24, 'length': 6, 'type': {'@type': 'textEntityTypeMention'}},
        {'@type': 'textEntity', 'offset': 31, 'length': 4, 'type': {'@type': 'textEntityTypeHashtag'}}]
KINDS = {'document': 'messageDocument', 'photo': 'messagePhoto', 'voice_note': 'messageVoiceNote',
         'video': 'messageVideo'}
STAGE = Path.home() / 'Library/Application Support/TelegramSearchMCP/outgoing-staging' / ('draft_' + 'a' * 32)


def entity(kind, offset, length, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length, 'type': {'@type': kind, **fields}}


def file_fixture(size=10):
    return {'@type': 'file', 'id': 1, 'size': size, 'expected_size': size,
            'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False, 'download_offset': 0,
                      'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'unique', 'is_uploading_active': False,
                       'is_uploading_completed': True, 'uploaded_size': size}}


def content(kind, caption=CAPTION, entities=(), duration=2, waveform='AAAA'):
    formatted = {'@type': 'formattedText', 'text': caption, 'entities': copy.deepcopy(list(entities))}
    if kind == 'document':
        return {'@type': 'messageDocument', 'caption': formatted, 'document': {
            '@type': 'document', 'file_name': 'evidence.txt', 'mime_type': 'text/plain', 'document': file_fixture()}}
    if kind == 'photo':
        return {'@type': 'messagePhoto', 'caption': formatted, 'photo': {
            '@type': 'photo', 'has_stickers': False, 'sizes': [{'@type': 'photoSize', 'type': 'i',
            'photo': file_fixture(), 'width': 2, 'height': 2, 'progressive_sizes': []}]},
            'show_caption_above_media': False, 'has_spoiler': False, 'is_secret': False}
    if kind == 'voice_note':
        return {'@type': 'messageVoiceNote', 'caption': formatted, 'is_listened': False, 'voice_note': {
            '@type': 'voiceNote', 'duration': duration, 'waveform': waveform, 'mime_type': 'audio/ogg',
            'voice': file_fixture()}}
    return {'@type': 'messageVideo', 'video': {'@type': 'video', 'duration': 5, 'width': 1280, 'height': 720,
            'file_name': 'clip.mp4', 'mime_type': 'video/mp4', 'has_stickers': False, 'supports_streaming': True,
            'minithumbnail': None, 'thumbnail': None, 'video': file_fixture()}, 'alternative_videos': [],
            'storyboards': [], 'cover': None, 'start_timestamp': 0, 'caption': formatted,
            'show_caption_above_media': False, 'has_spoiler': False, 'is_secret': False}


def message(identifier, value, *, reply=False, **changes):
    result = {'@type': 'message', 'id': identifier, 'chat_id': CHAT, 'is_outgoing': True,
              'content': copy.deepcopy(value), **changes}
    if reply:
        result['reply_to'] = {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': 55,
                              'checklist_task_id': 0, 'poll_option_id': '', 'origin_send_date': 0}
    return result


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ReducerTests(unittest.TestCase):
    def observe(self, kind, sent, *, caption=CAPTION, reply=False, styles=()):
        observations = SendObservations()
        values = {'caption_sha256': digest(caption)}
        if styles:
            values['outgoing_entities'] = styles
        if reply:
            values['reply_anchor'] = (CHAT, 55)
            if kind == 'voice_note':
                values.update(voice_duration=2, waveform_sha256=hashlib.sha256(b'\x00\x00\x00').hexdigest(),
                              waveform_size=3)
        if kind == 'video':
            values['video'] = (5, 1280, 720, 10)
        observations.register('a', 'e', 1, CHAT, KINDS[kind], **values)
        pending = message(-10, sent, reply=reply,
                          sending_state={'@type': 'messageSendingStatePending', 'sending_id': 1})
        observations.reduce({**pending, '@extra': 'e'})
        observations.reduce({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                             'message': message(701, sent, reply=reply, sending_state=None)})
        return observations.snapshot('a')

    def test_a_reply_caption_with_auto_detected_entities_confirms(self):
        """The pre-0.58 defect: this stayed outcome_unknown because the read-back required entities == []."""
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                result = self.observe(kind, content(kind, entities=LINK), reply=True)
                self.assertEqual((result.status, result.message_id), ('sent', 701))

    def test_a_plain_caption_tolerates_exactly_the_auto_detected_entities(self):
        for kind in KINDS:
            for reply in ((False, True) if kind != 'video' else (False,)):
                for entities, confirmed in [
                        (LINK, True), ([], True), (LINK[:1], True),
                        ([*LINK, entity('textEntityTypeBold', 0, 3)], False),
                        ([entity('textEntityTypeTextUrl', 0, 3, url='https://example.org')], False),
                        ([entity('textEntityTypeUrl', 4, 19, extra=1)], False),
                        ([entity('textEntityTypeUrl', 4, 40)], False), ([{}], False)]:
                    with self.subTest(kind=kind, reply=reply, entities=entities):
                        result = self.observe(kind, content(kind, entities=entities), reply=reply)
                        self.assertEqual(result.message_id, 701 if confirmed else None)

    def test_a_different_caption_never_confirms(self):
        for kind in KINDS:
            for text in ('different', CAPTION + ' ', '\ud800'):
                with self.subTest(kind=kind, text=text):
                    result = self.observe(kind, content(kind, caption=text))
                    self.assertNotEqual(result.status, 'sent')
                    self.assertIsNone(result.message_id)

    def test_a_styled_caption_confirms_in_tdlibs_split_form(self):
        text = 'Ask @tests now'
        bold = ((0, 14, 'textEntityTypeBold'),)
        split = [entity('textEntityTypeBold', 0, 4), entity('textEntityTypeMention', 4, 6),
                 entity('textEntityTypeBold', 4, 6), entity('textEntityTypeBold', 10, 4)]
        for kind in KINDS:
            for rows, confirmed in [(split, True), ([entity('textEntityTypeBold', 0, 14)], True),
                                    ([entity('textEntityTypeMention', 4, 6)], False),
                                    ([entity('textEntityTypeBold', 0, 13)], False),
                                    ([entity('textEntityTypeBold', 0, 14), entity('textEntityTypeItalic', 0, 3)],
                                     False)]:
                with self.subTest(kind=kind, rows=rows):
                    result = self.observe(kind, content(kind, caption=text, entities=rows), caption=text, styles=bold)
                    self.assertEqual(result.message_id, 701 if confirmed else None)

    def test_a_media_timestamp_is_bounded_by_the_message_its_caption_belongs_to(self):
        """TDLib keeps a caption's timestamp up to the own media duration (a voice note's, a video's) or, for a
        document or photo, the replied message's media; without a reply a document or photo has none."""
        text = 'from 0:02 on'
        def stamp(value):
            return [entity('textEntityTypeMediaTimestamp', 5, 4, media_timestamp=value)]
        cases = [('voice_note', False, 2, True), ('voice_note', False, 3, False), ('voice_note', True, 2, True),
                 ('document', False, 2, False), ('photo', False, 2, False), ('document', True, 2, True),
                 ('photo', True, 2, True), ('video', False, 2, True), ('document', True, 1, False)]
        for kind, reply, value, confirmed in cases:
            with self.subTest(kind=kind, reply=reply, value=value):
                result = self.observe(kind, content(kind, caption=text, entities=stamp(value)), caption=text,
                                      reply=reply)
                self.assertEqual(result.message_id, 701 if confirmed else None)

    def test_registration_takes_a_caption_digest_for_every_caption_kind_only(self):
        observations = SendObservations()
        observations.register('a', 'e1', 1, CHAT, 'messageDocument', caption_sha256=digest('x'))
        observations.register('b', 'e2', 2, CHAT, 'messageVideo', caption_sha256=digest('x'),
                              outgoing_entities=((0, 1, 'textEntityTypeBold'),), video=(5, 1280, 720, 10))
        for kind, values in [('messageText', {'caption_sha256': digest('x'), 'text_sha256': digest('x')}),
                             ('messageDocument', {'caption_sha256': 'X' * 64}),
                             ('messageDocument', {'caption_sha256': digest('x'), 'text_sha256': digest('x'),
                                                  'outgoing_entities': ((0, 1, 'textEntityTypeBold'),)}),
                             ('messageDocument', {'text_sha256': digest('x'),
                                                  'outgoing_entities': ((0, 1, 'textEntityTypeBold'),)}),
                             ('messageDocument', {'caption_sha256': digest('x'),
                                                  'outgoing_entities': ((0, 1, 'textEntityTypeCode'),)}),
                             ('messageVideo', {'caption_sha256': digest('x'), 'reply_anchor': (CHAT, 55),
                                               'video': (5, 1280, 720, 10)}),
                             ('messageVoiceNote', {'caption_sha256': digest('x'), 'reply_anchor': (CHAT, 55)})]:
            with self.subTest(kind=kind, values=values), self.assertRaises(ObservationUnavailable):
                SendObservations().register('c', 'e3', 3, CHAT, kind, **values)


class Raw:
    """One sendMessage answered by a pending and a final message whose caption Telegram may have annotated."""

    def __init__(self, entities=(), caption=None):
        self.sent, self.events = [], deque()
        self.entities, self.caption = list(entities), caption

    def send(self, request):
        self.sent.append(copy.deepcopy(request))
        assert request['@type'] == 'sendMessage'
        sent = request['input_message_content']
        kind = {'inputMessageDocument': 'document', 'inputMessagePhoto': 'photo',
                'inputMessageVoiceNote': 'voice_note', 'inputMessageVideo': 'video'}[sent['@type']]
        text = sent['caption']['text'] if self.caption is None else self.caption
        value = content(kind, caption=text, entities=self.entities)
        pending = message(-10, value, sending_state={'@type': 'messageSendingStatePending',
                                                     'sending_id': request['options']['sending_id']})
        self.events.append({**pending, '@extra': request['@extra']})
        self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                            'message': message(701, value, sending_state=None)})

    def receive(self, timeout):
        return self.events.popleft() if self.events else None

    def close(self):
        pass


class TransportTests(unittest.TestCase):
    def client(self, raw):
        client = TDLibClient(raw=raw)
        client._ready = True
        return client

    def send(self, client, kind, caption):
        if kind == 'document':
            return client.send_document_message(CHAT, STAGE / 'report.txt', caption, attempt_id='attempt')
        if kind == 'photo':
            return client.send_photo_message(CHAT, STAGE / 'picture.png', caption, attempt_id='attempt')
        return client.send_voice_note_message(CHAT, STAGE / 'speech.ogg', caption, 2, 'A' * 84, attempt_id='attempt')

    def test_a_plain_send_registers_the_caption_and_confirms_with_auto_detected_entities(self):
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                raw = Raw(LINK)
                client = self.client(raw)
                self.assertEqual(self.send(client, kind, CAPTION), 701)
                self.assertEqual(len(raw.sent), 1)
                self.assertEqual(raw.sent[0]['input_message_content']['caption']['entities'], [])
                self.assertEqual(client._send_observations.snapshot('attempt').caption_sha256, digest(CAPTION))

    def test_a_plain_send_whose_caption_comes_back_changed_stays_unknown_without_a_retry(self):
        from telegram_search_mcp.tdjson import MessageSendOutcomeUnknown
        for kind in ('document', 'photo', 'voice_note'):
            for raw in (Raw(caption='Changed'), Raw([entity('textEntityTypeBold', 0, 3)])):
                with self.subTest(kind=kind):
                    client = self.client(raw)
                    with self.assertRaises(MessageSendOutcomeUnknown):
                        self.send(client, kind, CAPTION)
                    self.assertEqual(len(raw.sent), 1)


if __name__ == '__main__':
    unittest.main()
