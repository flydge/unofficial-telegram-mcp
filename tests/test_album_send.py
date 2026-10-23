"""0.56 F15b, part 1 (core): ordered albums with per-item outcomes and no resend.

Synthetic artifacts and a synthetic raw TDLib transport only: the contracts, drafts, staging, transport
validator, observation reducer and broker are production code. No live TDLib and no owner dialog.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from collections import deque
from pathlib import Path

from telegram_search_mcp.config import (CAPABILITY_PROFILES, KNOWN_CAPABILITIES, LEGACY_CAPABILITIES,
                                        PROFILE_DEPENDENCIES, profile_of, unlocking_profiles)
from telegram_search_mcp.contract import CapabilityName, SEND_DEPENDENT_CAPABILITIES, required_capabilities
from telegram_search_mcp.tdjson import (_ALLOWED_REQUEST_TYPES, _CALL_REFUSED_TYPES, ForbiddenTDLibRequest,
                                        TDLibClient, _validate_bounded_request)

CLIENT = 'client_' + 'a' * 24
FOREIGN = 'client_' + 'b' * 24
RECIPIENT = 4242
ACCOUNT = 7
SEND_OPTIONS = {'@type': 'messageSendOptions', 'suggested_post_info': None, 'disable_notification': False,
    'from_background': False, 'protect_content': False, 'allow_paid_broadcast': False,
    'paid_message_star_count': 0, 'update_order_of_installed_sticker_sets': False,
    'scheduling_state': None, 'effect_id': 0, 'only_preview': False}


def staged(root, index, name):
    return {'@type': 'inputFileLocal', 'path': str(root / f'draft_{index:032x}' / name)}


def caption(text=''):
    return {'@type': 'formattedText', 'text': text, 'entities': []}


def photo_content(root, index, text=''):
    return {'@type': 'inputMessagePhoto', 'photo': {'@type': 'inputPhoto', 'photo': staged(root, index, 'p.jpg'),
            'thumbnail': None, 'video': None, 'added_sticker_file_ids': [], 'width': 0, 'height': 0},
            'caption': caption(text), 'show_caption_above_media': False, 'self_destruct_type': None,
            'has_spoiler': False}


def video_content(root, index, text=''):
    return {'@type': 'inputMessageVideo', 'video': {'@type': 'inputVideo', 'video': staged(root, index, 'v.mp4'),
            'thumbnail': None, 'cover': None, 'start_timestamp': 0, 'added_sticker_file_ids': [], 'duration': 5,
            'width': 1280, 'height': 720, 'supports_streaming': True},
            'caption': caption(text), 'show_caption_above_media': False, 'self_destruct_type': None,
            'has_spoiler': False}


def document_content(root, index, text=''):
    return {'@type': 'inputMessageDocument', 'document': {'@type': 'inputDocument',
            'document': staged(root, index, 'd.pdf'), 'thumbnail': None, 'disable_content_type_detection': True},
            'caption': caption(text)}


def voice_content(root, index):
    return {'@type': 'inputMessageVoiceNote', 'voice_note': {'@type': 'inputVoiceNote',
            'voice_note': staged(root, index, 'n.ogg'), 'duration': 3, 'waveform': 'A' * 84},
            'caption': caption(), 'self_destruct_type': None}


def album_request(contents, **changes):
    return {'@type': 'sendMessageAlbum', 'chat_id': RECIPIENT, 'topic_id': None, 'reply_to': None,
            'options': {**SEND_OPTIONS, 'sending_id': 77}, 'input_message_contents': contents, **changes}


def chat(chat_id, title):
    return {'@type': 'chat', 'id': chat_id, 'title': title, 'type': {'@type': 'chatTypePrivate', 'user_id': chat_id},
            'has_protected_content': False}


OUTPUT_TYPES = {'inputMessagePhoto': 'messagePhoto', 'inputMessageVideo': 'messageVideo',
                'inputMessageDocument': 'messageDocument'}
ALBUM_ID = '-7000000000000000001'  # int64 travels as a JSON string


def message_content(content_type):
    if content_type == 'messageVideo':
        return {'@type': 'messageVideo', 'video': {'@type': 'video'}, 'caption': caption()}
    return {'@type': content_type, 'caption': caption()}


def message(identifier, content_type, chat_id=RECIPIENT, **changes):
    return {'@type': 'message', 'id': identifier, 'chat_id': chat_id, 'is_outgoing': True,
            'content': message_content(content_type), 'media_album_id': ALBUM_ID, **changes}


def pending(identifier, content_type, sending_id, **changes):
    return message(identifier, content_type, sending_state={'@type': 'messageSendingStatePending',
                                                          'sending_id': sending_id}, **changes)


def succeeded(old_id, new_id, content_type, **changes):
    return {'@type': 'updateMessageSendSucceeded', 'old_message_id': old_id,
            'message': message(new_id, content_type, sending_state=None, **changes)}


def failed(old_id, content_type, code=400):
    return {'@type': 'updateMessageSendFailed', 'old_message_id': old_id,
            'message': message(old_id, content_type, sending_state={'@type': 'messageSendingStateFailed',
                                                                   'error': {'@type': 'error', 'code': code}}),
            'error': {'@type': 'error', 'code': code, 'message': 'PRIVATE PROVIDER TEXT'}}


class Raw:
    """A synthetic TDLib transport: readiness, the account, chats and one scripted sendMessageAlbum.

    `outcomes` lists each item's end ('sent', 'failed' or 'silent'); the provider emits every
    updateNewMessage first, then the exact `messages` result, then the terminal updates in send order."""

    def __init__(self):
        self.sent = []; self.events = deque(); self.account = ACCOUNT; self.fail_receive = False
        self.chats = {RECIPIENT: chat(RECIPIENT, 'Recipient'), ACCOUNT: chat(ACCOUNT, 'Saved Messages')}
        self.outcomes = None
        self.rejected = False
        self.tamper = None  # callable(result) -> result: a malformed or mismatched `messages` result

    def album_events(self, request):
        sending_id = request['options']['sending_id']
        types = [OUTPUT_TYPES[content['@type']] for content in request['input_message_contents']]
        if self.rejected:
            return [{'@type': 'error', 'code': 400, 'message': 'PRIVATE PROVIDER TEXT', '@extra': request['@extra']}]
        temporary = [-11 - index for index in range(len(types))]
        news = [{'@type': 'updateNewMessage', 'message': pending(t, kind, sending_id)} for t, kind in zip(temporary, types)]
        result = {'@type': 'messages', 'total_count': len(types),
                  'messages': [pending(t, kind, sending_id) for t, kind in zip(temporary, types)],
                  '@extra': request['@extra']}
        if self.tamper is not None:
            result = self.tamper(result)
        ends = []
        for index, (t, kind) in enumerate(zip(temporary, types)):
            outcome = (self.outcomes or ['sent'] * len(types))[index]
            if outcome == 'sent':
                ends.append(succeeded(t, 701 + index, kind))
            elif outcome == 'failed':
                ends.append(failed(t, kind))
        return [*news, result, *ends]

    def send(self, request):
        self.sent.append(copy.deepcopy(request)); kind = request['@type']
        if kind == 'getAuthorizationState':
            value = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe':
            value = {'@type': 'user', 'id': self.account}
        elif kind == 'getChat':
            value = copy.deepcopy(self.chats.get(request['chat_id'], {'@type': 'error', 'code': 400, 'message': 'x'}))
        elif kind == 'sendMessageAlbum':
            self.events.extend(self.album_events(request))
            return
        else:
            raise AssertionError(kind)
        self.events.append({**value, '@extra': request['@extra']})

    def receive(self, timeout):
        if self.events:
            return self.events.popleft()
        if self.fail_receive:
            self.fail_receive = False
            raise OSError('synthetic loss')
        return None

    def close(self):
        pass


class CapabilityTests(unittest.TestCase):
    """Work item 1: album_send is default off and requires send; video items also need video_send."""

    def test_album_send_is_a_known_default_off_send_dependent_capability(self):
        self.assertIn('album_send', KNOWN_CAPABILITIES)
        self.assertNotIn('album_send', LEGACY_CAPABILITIES)
        self.assertIn('album_send', SEND_DEPENDENT_CAPABILITIES)
        self.assertEqual(required_capabilities('album_send'), ('album_send', 'send'))
        self.assertIn('album_send', CapabilityName.__args__)

    def test_album_send_has_its_own_profile_that_depends_on_send_only(self):
        # Lead decision: never inside `video`, so enabling albums grants no video_send; video items in an album
        # need the `video` profile as well (the broker's per-item conjunction).
        self.assertEqual(profile_of('album_send'), 'albums')
        self.assertEqual(CAPABILITY_PROFILES['albums'], ('album_send',))
        self.assertEqual(PROFILE_DEPENDENCIES['albums'], ('send',))
        self.assertEqual(unlocking_profiles(['album_send']), ('albums',))
        self.assertEqual(unlocking_profiles(['album_send', 'send']), ('albums',))
        self.assertEqual(unlocking_profiles(['album_send', 'video_send']), ('video', 'albums'))

    def test_profile_switches_keep_the_dependency_rules(self):
        from telegram_search_mcp import capability_profiles as profiles
        enabled = profiles.planned(('artifacts', 'read'), 'enable', 'albums')
        self.assertIn('album_send', enabled)
        self.assertTrue(set(CAPABILITY_PROFILES['send']) <= set(enabled))
        self.assertNotIn('video_send', enabled)
        everything = tuple(sorted({'artifacts', 'read', *(c for m in CAPABILITY_PROFILES.values() for c in m)}))
        self.assertNotIn('album_send', profiles.planned(everything, 'disable', 'send'))
        self.assertIn('album_send', profiles.planned(everything, 'disable', 'video'))
        self.assertNotIn('video_send', profiles.planned(everything, 'disable', 'video'))
        self.assertEqual(profiles.states(('artifacts', 'read', 'send', 'album_send'))['albums'], 'on')


class TransportShapeTests(unittest.TestCase):
    """Criterion 2: one closed sendMessageAlbum shape, refused by the generic `_call` in every shape."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def check(self, payload):
        _validate_bounded_request(payload, self.root)

    def refused(self, payload):
        with self.assertRaises(ForbiddenTDLibRequest):
            _validate_bounded_request(payload, self.root)

    def test_allowlisted_but_refused_by_the_generic_call(self):
        self.assertIn('sendMessageAlbum', _ALLOWED_REQUEST_TYPES)
        self.assertIn('sendMessageAlbum', _CALL_REFUSED_TYPES)
        raw = Raw(); client = TDLibClient(raw=raw, staging_root=self.root); client._ready = True
        for payload in (album_request([photo_content(self.root, 1), video_content(self.root, 2)]),
                        album_request([document_content(self.root, 1), document_content(self.root, 2)])):
            self.check(payload)
            with self.subTest(kinds=[c['@type'] for c in payload['input_message_contents']]), \
                    self.assertRaises(ForbiddenTDLibRequest):
                client._call(payload)
        self.assertEqual(raw.sent, [])

    def test_media_and_document_groups_of_two_to_ten_items(self):
        root = self.root
        self.check(album_request([photo_content(root, 1, 'One'), photo_content(root, 2, 'Two')]))
        self.check(album_request([video_content(root, 1), photo_content(root, 2), video_content(root, 3)]))
        self.check(album_request([document_content(root, i) for i in range(1, 11)]))
        self.check(album_request([photo_content(root, i) if i % 2 else video_content(root, i) for i in range(1, 11)]))
        self.check(album_request([document_content(root, 1), document_content(root, 2)], chat_id=-1001234567890))

    def test_counts_groups_and_kinds_outside_the_closed_shape_refuse(self):
        root = self.root
        for contents in ([], [photo_content(root, 1)], [document_content(root, i) for i in range(1, 12)],
                         [photo_content(root, 1), document_content(root, 2)],
                         [document_content(root, 1), video_content(root, 2)],
                         [voice_content(root, 1), voice_content(root, 2)],
                         [photo_content(root, 1), voice_content(root, 2)],
                         [document_content(root, 1), voice_content(root, 2)],
                         [{'@type': 'inputMessageText', 'text': caption('x'), 'link_preview_options': None,
                           'clear_draft': False}, photo_content(root, 2)],
                         [{'@type': 'inputMessageAudio'}, {'@type': 'inputMessageAudio'}],
                         (photo_content(root, 1), photo_content(root, 2)), None):
            with self.subTest(contents=repr(contents)[:90]):
                self.refused(album_request(contents))

    def test_every_item_uses_the_shared_closed_per_kind_shape(self):
        root = self.root
        def changed(content, path, value):
            content = copy.deepcopy(content)
            target = content
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return content
        photo, video, document = photo_content(root, 1), video_content(root, 1), document_content(root, 1)
        bad = [changed(document, ('document', 'disable_content_type_detection'), False),
               changed(document, ('document', 'thumbnail'), {'@type': 'inputThumbnail'}),
               changed(photo, ('has_spoiler',), True), changed(photo, ('show_caption_above_media',), True),
               changed(photo, ('photo', 'width'), 640), changed(photo, ('photo', 'width'), False),
               changed(photo, ('self_destruct_type',), {'@type': 'messageSelfDestructTypeTimer'}),
               changed(video, ('has_spoiler',), True), changed(video, ('show_caption_above_media',), True),
               changed(video, ('video', 'duration'), 0), changed(video, ('video', 'cover'), {'@type': 'x'}),
               changed(photo, ('caption',), {'@type': 'formattedText', 'text': 'x', 'entities': [{}]}),
               changed(photo, ('caption',), caption('x' * 1025)), changed(photo, ('caption',), caption(' lead')),
               changed(document, ('caption',), caption('a\tb')),
               changed(photo, ('photo', 'photo'), {'@type': 'inputFileLocal', 'path': '/tmp/unapproved.jpg'}),
               changed(document, ('document', 'document'), {'@type': 'inputFileRemote', 'id': 'X'}),
               changed(photo, ('extra',), 1)]
        for content in bad:
            partner = (document_content(root, 2) if content['@type'] == 'inputMessageDocument'
                       else photo_content(root, 2))
            with self.subTest(content=repr(content)[:100]):
                self.refused(album_request([content, partner]))

    def test_request_keys_options_and_targets_are_exact(self):
        root = self.root
        contents = [photo_content(root, 1), photo_content(root, 2)]
        bad_options = [dict(SEND_OPTIONS), {**SEND_OPTIONS, 'sending_id': 0}, {**SEND_OPTIONS, 'sending_id': 2**31},
                       {**SEND_OPTIONS, 'sending_id': True}, {**SEND_OPTIONS, 'sending_id': 7, 'protect_content': True},
                       {**SEND_OPTIONS, 'sending_id': 7, 'disable_notification': 0},
                       {**SEND_OPTIONS, 'sending_id': 7, 'only_preview': True},
                       {**SEND_OPTIONS, 'sending_id': 7, 'extra': 1}]
        for changes in ([{'options': value} for value in bad_options] +
                        [{'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 1}},
                         {'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 5, 'quote': None,
                                       'checklist_task_id': 0, 'poll_option_id': ''}},
                         {'reply_markup': None}, {'chat_id': 0}, {'chat_id': True}, {'chat_id': '4242'},
                         {'chat_id': 2**53}, {'extra': 1}]):
            with self.subTest(changes=repr(changes)[:90]):
                self.refused(album_request(contents, **changes))
        for key in ('topic_id', 'reply_to', 'options', 'input_message_contents', 'chat_id'):
            missing = album_request(contents); missing.pop(key)
            with self.subTest(missing=key):
                self.refused(missing)

    def test_two_items_never_share_one_staged_directory(self):
        root = self.root
        same = photo_content(root, 1)
        twin = copy.deepcopy(same)
        twin['photo']['photo']['path'] = str(root / f'draft_{1:032x}' / 'other.jpg')
        self.refused(album_request([same, twin]))
        self.refused(album_request([same, copy.deepcopy(same)]))


if __name__ == '__main__':
    unittest.main()
