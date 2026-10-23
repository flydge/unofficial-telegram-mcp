"""0.56 F15b, part 1 (core): ordered albums with per-item outcomes and no resend.

Synthetic artifacts and a synthetic raw TDLib transport only: the contracts, drafts, staging, transport
validator, observation reducer and broker are production code. No live TDLib and no owner dialog.
"""
from __future__ import annotations

import copy
import re
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import bmff_fixtures as bmff

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
            'message': message(new_id, content_type, **{'sending_state': None, **changes})}


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
        self.check(album_request([document_content(root, 1), document_content(root, 2)], chat_id=-RECIPIENT))

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


TYPES = ('messagePhoto', 'messageVideo', 'messagePhoto')


def news(types=TYPES, sending_id=5):
    return [{'@type': 'updateNewMessage', 'message': pending(-11 - i, kind, sending_id)} for i, kind in enumerate(types)]


def result(types=TYPES, sending_id=5, extra='e', **changes):
    return {'@type': 'messages', 'total_count': len(types),
            'messages': [pending(-11 - i, kind, sending_id) for i, kind in enumerate(types)], '@extra': extra, **changes}


class ObservationTests(unittest.TestCase):
    """Criterion 4: one observation with N slots, bound only by the exact `messages` result."""

    def observed(self, types=TYPES, clock=None):
        from telegram_search_mcp.send_observations import SendObservations
        observations = SendObservations(**({'clock': clock} if clock is not None else {}))
        observations.register_album('a', 'e', 5, RECIPIENT, types)
        return observations

    def slots(self, observations):
        snapshot = observations.snapshot('a')
        return snapshot.status, [(slot.status, slot.temporary_id, slot.message_id) for slot in snapshot.slots]

    def test_registration_is_closed_and_shares_correlation_with_single_sends(self):
        from telegram_search_mcp.send_observations import ObservationUnavailable, SendObservations
        observations = SendObservations()
        for types in ((), ('messagePhoto',), ('messageDocument',) * 11, ('messagePhoto', 'messageDocument'),
                      ('messageVoiceNote', 'messagePhoto'), ('messageAudio', 'messageAudio'),
                      ('messageText', 'messageText'), ['messagePhoto', 'messagePhoto'], 'messagePhoto'):
            with self.subTest(types=types), self.assertRaises(ObservationUnavailable):
                observations.register_album('a', 'e', 5, RECIPIENT, types)
        with self.assertRaises(ObservationUnavailable):
            observations.register_album('a', 'e', 5, 0, TYPES)
        observations.register('single', 'x', 5, RECIPIENT, 'messageDocument')
        with self.assertRaises(ObservationUnavailable):
            observations.register_album('a', 'e', 5, RECIPIENT, TYPES)  # same sending_id
        with self.assertRaises(ObservationUnavailable):
            observations.register_album('a', 'x', 6, RECIPIENT, TYPES)  # same extra
        observations.register_album('a', 'e', 6, RECIPIENT, ('messageDocument', 'messageDocument'))
        self.assertFalse(observations.sending_id_available(6))
        with self.assertRaises(ObservationUnavailable):
            observations.register('b', 'y', 6, RECIPIENT, 'messageDocument')
        snapshot = observations.snapshot('a')
        self.assertEqual((snapshot.status, len(snapshot.slots)), ('registered', 2))
        observations.discard_unattempted('a')
        self.assertIsNone(observations.snapshot('a'))

    def test_new_messages_before_the_result_never_bind(self):
        observations = self.observed()
        for event in news():
            observations.reduce(event)
        self.assertEqual(self.slots(observations), ('registered', [('pending', None, None)] * 3))
        observations.reduce(result())
        self.assertEqual(self.slots(observations), ('bound', [('pending', -11, None), ('pending', -12, None),
                                                             ('pending', -13, None)]))
        self.assertEqual(observations.snapshot('a').media_album_id, int(ALBUM_ID))

    def test_each_slot_ends_only_by_its_own_bound_terminal_update(self):
        observations = self.observed()
        observations.reduce(result())
        observations.reduce(succeeded(-12, 802, 'messageVideo'))
        observations.reduce(failed(-11, 'messagePhoto'))
        observations.reduce({'@type': 'updateDeleteMessages', 'chat_id': RECIPIENT, 'message_ids': [-13],
                             'is_permanent': True, 'from_cache': False})
        observations.reduce(succeeded(-99, 803, 'messagePhoto'))  # not one of ours
        self.assertEqual(self.slots(observations), ('bound', [('failed', -11, None), ('sent', -12, 802),
                                                             ('pending', -13, None)]))
        # Terminal is final: a later contradicting update changes nothing.
        observations.reduce(failed(-12, 'messageVideo'))
        observations.reduce(succeeded(-11, 804, 'messagePhoto'))
        self.assertEqual(self.slots(observations)[1][:2], [('failed', -11, None), ('sent', -12, 802)])

    def test_terminal_updates_that_overtake_the_result_apply_only_once_exactly_bound(self):
        observations = self.observed()
        observations.reduce(succeeded(-13, 903, 'messagePhoto'))
        observations.reduce(failed(-12, 'messageVideo'))
        observations.reduce(succeeded(-50, 950, 'messagePhoto'))  # someone else's message
        self.assertEqual(self.slots(observations), ('registered', [('pending', None, None)] * 3))
        observations.reduce(result())
        self.assertEqual(self.slots(observations), ('bound', [('pending', -11, None), ('failed', -12, None),
                                                             ('sent', -13, 903)]))
        early = self.observed()
        early.reduce(succeeded(-12, 902, 'messagePhoto'))  # the right id with the wrong content type
        early.reduce(result())
        self.assertEqual(self.slots(early)[1][1], ('conflict', -12, None))

    def test_any_mismatch_in_the_result_is_a_conflict_and_every_slot_stays_unknown(self):
        def changed(index, **fields):
            def apply(value):
                value = copy.deepcopy(value)
                value['messages'][index].update(fields)
                return value
            return apply
        tampered = [
            lambda r: {**r, 'total_count': 2}, lambda r: {**r, 'messages': r['messages'][:2]},
            lambda r: {**r, 'messages': [r['messages'][1], r['messages'][0], r['messages'][2]]},
            changed(0, content=message_content('messageDocument')), changed(1, sending_state=None),
            changed(2, sending_state={'@type': 'messageSendingStatePending', 'sending_id': 6}),
            changed(1, media_album_id='-7000000000000000002'), changed(0, media_album_id='0'),
            changed(0, media_album_id='7000000000000000001'), changed(0, media_album_id=-7),
            changed(1, id=-11), changed(2, chat_id=RECIPIENT + 1), changed(0, is_outgoing=False),
            changed(0, id=0), lambda r: {**r, '@type': 'message'}, lambda r: {**r, 'messages': None},
            lambda r: {**r, 'total_count': '3'}]
        for tamper in tampered:
            observations = self.observed()
            observations.reduce(tamper(result()))
            for index, kind in enumerate(TYPES):
                observations.reduce(succeeded(-11 - index, 801 + index, kind))
            with self.subTest(tamper=tamper):
                self.assertEqual(observations.snapshot('a').status, 'conflict')
        observations = self.observed()
        observations.reduce(result())
        observations.reduce(succeeded(-11, 801, 'messagePhoto'))
        observations.reduce(result())  # a second result for the same request
        self.assertEqual(observations.snapshot('a').status, 'conflict')

    def test_a_rejected_request_fails_every_slot(self):
        observations = self.observed()
        observations.reduce({'@type': 'error', 'code': 400, 'message': 'PRIVATE', '@extra': 'e'})
        self.assertEqual(observations.snapshot('a').status, 'failed')
        observations.reduce(result())
        self.assertEqual(observations.snapshot('a').status, 'failed')

    def test_malformed_final_messages_make_the_slot_a_conflict(self):
        bad = [succeeded(-11, 801, 'messageDocument'), succeeded(-11, -801, 'messagePhoto'),
               succeeded(-11, 801, 'messagePhoto', chat_id=RECIPIENT + 1),
               succeeded(-11, 801, 'messagePhoto', is_outgoing=False),
               succeeded(-11, 801, 'messagePhoto', sending_state={'@type': 'messageSendingStatePending',
                                                                  'sending_id': 5}),
               {**failed(-11, 'messagePhoto'), 'error': {'@type': 'error', 'code': 401}},
               {**failed(-11, 'messagePhoto'), 'error': None}]
        for event in bad:
            observations = self.observed()
            observations.reduce(result())
            observations.reduce(event)
            observations.reduce(succeeded(-11, 801, 'messagePhoto'))
            with self.subTest(event=repr(event)[:90]):
                self.assertEqual(self.slots(observations)[1][0], ('conflict', -11, None))
        video = self.observed()
        video.reduce(result())
        final = succeeded(-12, 802, 'messageVideo')
        final['message']['content']['video'] = None
        video.reduce(final)
        self.assertEqual(self.slots(video)[1][1], ('conflict', -12, None))

    def test_late_updates_after_expiry_change_nothing(self):
        from telegram_search_mcp.send_observations import RETENTION_SECONDS
        now = [100.0]
        observations = self.observed(clock=lambda: now[0])
        observations.reduce(result())
        now[0] += RETENTION_SECONDS
        observations.reduce(succeeded(-11, 801, 'messagePhoto'))
        self.assertIsNone(observations.snapshot('a'))


def image(kind='PNG', size=(64, 48), color=(200, 30, 30)):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, kind)
    return buffer.getvalue()


PNG, JPEG = image('PNG'), image('JPEG', (40, 30))
PDF = b'%PDF-1.4\n% synthetic\n'
MOVIE = bmff.movie()


class Store:
    """Owned artifacts in a temporary cache, outside any broker scope."""

    def setUp(self):
        from telegram_search_mcp.artifact_store import ArtifactStore
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ArtifactStore(cache_dir=self.root / 'cache')
        self.counter = 0

    def artifact(self, data):
        self.counter += 1
        source = self.root / f'source-{self.counter}'
        source.write_bytes(data)
        return self.store.store(source, kind='media')

    def item(self, data, name=None, group='media', caption='', mime=None):
        from telegram_search_mcp.album_drafts import album_item
        return album_item(group=group, artifact=self.artifact(data), recorded_name=name, recorded_mime=mime,
                          caption=caption)

    def spec(self, *items, group='media'):
        from telegram_search_mcp.album_drafts import AlbumSpec
        return AlbumSpec(group=group, items=tuple(items), recipient_identity=('chatTypePrivate', RECIPIENT))


class ContractTests(Store, unittest.TestCase):
    """Criterion 5 (reduction table) and the closed album contracts."""

    def test_overall_status_reduction_table(self):
        from telegram_search_mcp.album_drafts import album_overall
        table = [(('sent', 'sent'), 'sent'), (('sent',) * 10, 'sent'), (('failed', 'failed'), 'failed'),
                 (('sent', 'failed'), 'partial'), (('failed', 'sent', 'failed'), 'partial'),
                 (('sent', 'outcome_unknown'), 'partial'), (('outcome_unknown', 'sent', 'failed'), 'partial'),
                 (('failed', 'outcome_unknown'), 'outcome_unknown'), (('outcome_unknown',) * 3, 'outcome_unknown')]
        for statuses, expected in table:
            with self.subTest(statuses=statuses):
                self.assertEqual(album_overall(statuses), expected)
        for statuses in ((), ('sent',), ('sent',) * 11, ('sent', 'pending'), ('sent', 'partial'), ('sent', None)):
            with self.subTest(statuses=statuses), self.assertRaises(ValueError):
                album_overall(statuses)

    def test_status_model_is_closed_and_separate_from_get_send_status(self):
        from pydantic import ValidationError
        from telegram_search_mcp.album_drafts import GetAlbumSendStatusResponse, album_status
        did, sha = 'draft_' + '1' * 32, 'a' * 64
        self.assertEqual(album_status(did).model_dump(mode='json'),
                         {'draft_id': did, 'status': 'outcome_unknown', 'evidence': 'none',
                          'detail': 'outcome is unknown; no resend is authorized', 'album_sha256': None,
                          'slots': [], 'next_action': 'check_send_status'})
        partial = album_status(did, 'provider', album_sha256=sha, outcomes=(('sent', 9), ('outcome_unknown', None)))
        self.assertEqual((partial.status, [s.status for s in partial.slots]), ('partial', ['sent', 'outcome_unknown']))
        claimed = album_status(did, 'local_claimed', album_sha256=sha, outcomes=(('pending', None), ('sent', 9)))
        self.assertEqual(claimed.status, 'pending')
        self.assertEqual(album_status(did, 'local_pending', album_sha256=sha).status, 'pending')
        self.assertEqual(album_status(did, 'local_failed', album_sha256=sha).status, 'failed')
        base = partial.model_dump(mode='json')
        base.pop('next_action', None)
        for changes in ({'status': 'sent'}, {'status': 'outcome_unknown'}, {'slots': base['slots'][:1]},
                        {'slots': [{'position': 1, 'status': 'pending', 'message_id': None}, base['slots'][1]]},
                        {'slots': [{'position': 2, 'status': 'sent', 'message_id': 9}, base['slots'][1]]},
                        {'slots': [{'position': 1, 'status': 'sent', 'message_id': None}, base['slots'][1]]},
                        {'album_sha256': None}, {'evidence': 'none'}, {'detail': 'outcome is unknown; no resend is authorized'},
                        {'next_action': 'search_again'}, {'extra': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                GetAlbumSendStatusResponse.model_validate({**base, **changes})

    def test_send_response_carries_every_slot_for_a_provider_outcome(self):
        from pydantic import ValidationError
        from telegram_search_mcp.album_drafts import SendPreparedAlbumResponse
        did, sha = 'draft_' + '1' * 32, 'a' * 64
        slots = [{'position': 1, 'status': 'sent', 'message_id': 5}, {'position': 2, 'status': 'failed'}]
        self.assertEqual(SendPreparedAlbumResponse(status='partial', draft_id=did, album_sha256=sha, slots=slots,
                                                   detail='x').status, 'partial')
        SendPreparedAlbumResponse(status='outcome_unknown', draft_id=did, detail='x')  # content-free
        for values in (dict(status='sent', album_sha256=sha, slots=slots), dict(status='sent'),
                       dict(status='partial'), dict(status='failed', album_sha256=sha),
                       dict(status='partial', slots=slots), dict(status='declined', album_sha256=sha, slots=slots),
                       dict(status='partial', album_sha256=sha, slots=slots[:1]),
                       dict(status='partial', album_sha256=sha, slots=list(reversed(slots))),
                       dict(status='outcome_unknown', album_sha256=sha,
                            slots=[{'position': 1, 'status': 'pending'}, {'position': 2, 'status': 'failed'}])):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                SendPreparedAlbumResponse(draft_id=did, detail='x', **values)

    def test_unconfirmed_items_point_to_the_album_status_and_never_to_a_resend(self):
        """Part 2, work item 4: a large album's outcome_unknown says so and names get_album_send_status; a partial
        album with an unconfirmed item carries check_send_status too, a settled one carries no hint."""
        from telegram_search_mcp.album_drafts import SEND_DETAILS, SendPreparedAlbumResponse, album_status
        did, sha = 'draft_' + '1' * 32, 'a' * 64
        for key in ('outcome_unknown', 'partial_unknown', 'replay_unknown', 'lost'):
            with self.subTest(detail=key):
                self.assertIn('get_album_send_status', SEND_DETAILS[key])
                self.assertIn('never resend', SEND_DETAILS[key])
        self.assertIn('large album', SEND_DETAILS['outcome_unknown'])
        for detail in SEND_DETAILS.values():
            self.assertLessEqual(len(detail), 200)
            self.assertNotIn('get_send_status', detail)
            # Every resend or retry the text names is negated: never a suggestion to send again.
            for match in re.finditer(r'(\w+) (?:resend|retry)', detail):
                self.assertIn(match.group(1), ('never', 'no'), detail)
            self.assertNotIn('try again', detail)
        sent, failed, unknown = ({'status': 'sent', 'message_id': 5}, {'status': 'failed'}, {'status': 'outcome_unknown'})
        def slots(*values):
            return [{'position': index, **value} for index, value in enumerate(values, start=1)]
        for status, items, hint in (('partial', slots(sent, unknown), 'check_send_status'),
                                    ('partial', slots(sent, failed), None),
                                    ('outcome_unknown', slots(failed, unknown), 'check_send_status'),
                                    ('sent', slots(sent, sent), None), ('failed', slots(failed, failed), None)):
            with self.subTest(status=status, items=items):
                response = SendPreparedAlbumResponse(status=status, draft_id=did, album_sha256=sha, slots=items,
                                                     detail='x')
                self.assertEqual(response.next_action, hint)
                outcomes = [(item['status'], item.get('message_id')) for item in items]
                self.assertEqual(album_status(did, 'provider', album_sha256=sha, outcomes=outcomes).next_action, hint)
        self.assertEqual(SendPreparedAlbumResponse(status='outcome_unknown', draft_id=did, detail='x').next_action,
                         'check_send_status')

    def test_request_is_closed_two_to_ten_distinct_items(self):
        from pydantic import ValidationError
        from telegram_search_mcp.album_drafts import PrepareAlbumSendRequest
        ids = [self.artifact(PNG + bytes([i])).artifact_id for i in range(11)]
        request = PrepareAlbumSendRequest.model_validate({'recipient': RECIPIENT, 'items': [
            {'artifact_id': ids[0]}, {'artifact_id': ids[1], 'caption': 'Two'}]})
        self.assertEqual((request.group, request.items[0].caption), ('media', ''))
        for payload in ({'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}]},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': i} for i in ids]},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}, {'artifact_id': ids[0]}]},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}, {'artifact_id': ids[1]}],
                         'group': 'audio'},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}, {'artifact_id': ids[1], 'kind': 'x'}]},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}, {'artifact_id': ids[1],
                                                                                    'caption': 'x' * 1025}]},
                        {'recipient': RECIPIENT, 'items': [{'artifact_id': ids[0]}, {'artifact_id': ids[1]}], 'x': 1}):
            with self.subTest(payload=repr(payload)[:90]), self.assertRaises(ValidationError):
                PrepareAlbumSendRequest.model_validate(payload)

    def test_items_are_classified_locally_with_per_kind_limits(self):
        from telegram_search_mcp.album_drafts import AlbumRefusal
        photo = self.item(PNG, 'a/b\\shot.png', caption='Cap\r\n')
        self.assertEqual((photo.kind, photo.display_name, photo.mime_type, photo.facts, photo.caption),
                         ('photo', 'shot.png', 'image/png', (64, 48), 'Cap'))
        unnamed = self.item(JPEG)
        self.assertEqual((unnamed.kind, unnamed.display_name, unnamed.facts), ('photo', 'photo.jpg', (40, 30)))
        video = self.item(MOVIE, 'clip.mov')
        self.assertEqual((video.kind, video.display_name, video.mime_type, video.facts.width),
                         ('video', 'clip.mov', 'video/quicktime', 1280))
        self.assertEqual(self.item(MOVIE).display_name, 'video.mp4')
        document = self.item(PNG, 'scan.png', group='documents', mime='image/png')
        self.assertEqual((document.kind, document.display_name, document.mime_type, document.facts),
                         ('document', 'scan.png', 'image/png', None))
        self.assertEqual(self.item(PDF, group='documents', mime='not a type').mime_type, 'application/octet-stream')
        self.assertEqual(self.item(PDF, group='documents').display_name, 'attachment')
        for data, name, code in ((PDF, 'doc.pdf', 'unsupported_item'), (PNG, 'shot.jpg', 'unsupported_item'),
                                 (b'\x00' * 32, None, 'unsupported_item')):
            with self.subTest(name=name, code=code), self.assertRaises(AlbumRefusal) as refused:
                self.item(data, name)
            self.assertEqual(refused.exception.code, code)
        with patch('telegram_search_mcp.album_drafts.MAX_ITEM_BYTES', {'photo': 10, 'video': 10, 'document': 10}):
            for group in ('media', 'documents'):
                with self.subTest(group=group), self.assertRaises(AlbumRefusal) as refused:
                    self.item(PNG, 'shot.png', group=group)
                self.assertEqual(refused.exception.code, 'item_too_large')

    def test_album_spec_bounds_groups_duplicates_and_total(self):
        from telegram_search_mcp.album_drafts import AlbumSpec
        photo, video = self.item(PNG, 'a.png'), self.item(MOVIE)
        document = self.item(PDF, 'doc.pdf', group='documents')
        spec = self.spec(photo, video)
        self.assertEqual((spec.kinds, spec.artifact_ids, spec.content_types, spec.total_bytes),
                         (frozenset({'photo', 'video'}), (photo.artifact_id, video.artifact_id),
                          ('messagePhoto', 'messageVideo'), photo.size_bytes + video.size_bytes))
        self.spec(document, self.item(PDF + b'2', 'two.pdf', group='documents'), group='documents')
        for items, group in (((photo,), 'media'), ((photo, document), 'media'), ((document, document), 'documents'),
                             ((photo, video), 'documents'), ((photo,) * 11, 'media'), ((photo, photo), 'media'),
                             ((photo, video), 'audio')):
            with self.subTest(items=len(items), group=group), self.assertRaises(ValueError):
                self.spec(*items, group=group)
        with self.assertRaises(ValueError):
            AlbumSpec(group='media', items=(photo, video), recipient_identity=())
        with patch('telegram_search_mcp.album_drafts.MAX_ALBUM_BYTES', photo.size_bytes + video.size_bytes - 1), \
                self.assertRaises(ValueError):
            self.spec(photo, video)

    def test_one_ordered_digest_binds_the_preview(self):
        from datetime import datetime, timezone
        from pydantic import ValidationError
        from telegram_search_mcp.album_drafts import AlbumDraftPreview, AlbumRecipient, album_digest
        photo, video = self.item(PNG, 'a.png', caption='First'), self.item(MOVIE, caption='Second')
        spec = self.spec(photo, video)
        swapped = self.spec(video, photo)
        self.assertNotEqual(spec.sha256, swapped.sha256)
        self.assertEqual(spec.sha256, album_digest('media', (photo, video)))
        preview = AlbumDraftPreview.create(draft_id='draft_' + '1' * 32, account_id=ACCOUNT,
            expires_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
            recipient=AlbumRecipient(chat_id=RECIPIENT, title='Recipient'), album=spec)
        self.assertEqual((preview.album_sha256, preview.total_size_bytes, [i.position for i in preview.items]),
                         (spec.sha256, spec.total_bytes, [1, 2]))
        self.assertEqual([i.raw_caption for i in preview.items], ['First', 'Second'])
        self.assertTrue(preview.items[0].display_name.startswith('[untrusted Telegram evidence] '))
        dumped = preview.model_dump(mode='json')
        AlbumDraftPreview.model_validate(dumped)
        tampered = []
        for change in (lambda d: d['items'].reverse(), lambda d: d.__setitem__('total_size_bytes', 1),
                       lambda d: d['items'][0].__setitem__('position', 2),
                       lambda d: d['items'][1].__setitem__('caption', None),
                       lambda d: d.__setitem__('album_sha256', 'b' * 64), lambda d: d.__setitem__('group', 'documents'),
                       lambda d: d['recipient'].__setitem__('title', 'Other'),
                       lambda d: d['items'][0]['photo'].__setitem__('width', 65)):
            value = copy.deepcopy(dumped)
            change(value)
            tampered.append(value)
        for value in tampered:
            with self.subTest(value=repr(value)[:60]), self.assertRaises(ValidationError):
                AlbumDraftPreview.model_validate(value)


class Drafts(Store):
    def setUp(self):
        super().setUp()
        from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
        self.clock = [1000.0]
        self.registry = OutgoingDraftRegistry(self.store, attempt_clock=lambda: self.clock[0])
        self.owner = DraftOwner(CLIENT, ACCOUNT)
        self.photo, self.video = self.item(PNG, 'a.png'), self.item(MOVIE, caption='Two')
        self.album = self.spec(self.photo, self.video)

    def prepare(self, album=None, owner=None, recipient=RECIPIENT):
        return self.registry.prepare_album(owner=owner or self.owner, recipient=recipient,
                                           recipient_title='Recipient', album=album or self.album)


class RegistryTests(Drafts, unittest.TestCase):
    """Work item 2: a dedicated, account-bound album draft kind with one receipt of N slots."""

    def test_prepare_binds_the_ordered_digest_and_total(self):
        draft = self.prepare()
        self.assertEqual((draft.kind, draft.sha256, draft.size_bytes, draft.artifact_id, draft.album),
                         ('album', self.album.sha256, self.album.total_bytes, '', self.album))
        self.assertEqual(self.registry.peek(draft.draft_id, owner=self.owner), draft)
        self.assertEqual(self.registry.kind(draft.draft_id, owner=self.owner), 'album')

    def test_generic_draft_tools_never_list_cancel_or_revise_an_album(self):
        from telegram_search_mcp.outgoing_drafts import DraftError
        draft = self.prepare()
        self.assertEqual(self.registry.list_pending(owner=self.owner), ([], False))
        with self.assertRaises(DraftError):
            self.registry.cancel(draft.draft_id, owner=self.owner)
        with self.assertRaises(DraftError):
            self.registry.revise(draft.draft_id, owner=self.owner, recipient_title='Recipient', caption='x')
        with self.assertRaises(DraftError):
            self.prepare(album=object())

    def test_supersede_key_is_owner_recipient_and_ordered_item_ids(self):
        from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner
        first = self.prepare()
        reordered = self.prepare(album=self.spec(self.video, self.photo))
        other_owner = self.prepare(owner=DraftOwner(FOREIGN, ACCOUNT))
        other_account = self.prepare(owner=DraftOwner(CLIENT, ACCOUNT + 1))
        other_recipient = self.prepare(recipient=RECIPIENT + 1)
        again = self.prepare()
        with self.assertRaises(DraftError):
            self.registry.claim(first.draft_id, owner=self.owner, approved=True)
        for draft, owner in ((reordered, self.owner), (other_owner, DraftOwner(FOREIGN, ACCOUNT)),
                             (other_account, DraftOwner(CLIENT, ACCOUNT + 1)), (other_recipient, self.owner),
                             (again, self.owner)):
            with self.subTest(draft=draft.draft_id):
                self.assertEqual(self.registry.peek(draft.draft_id, owner=owner), draft)
        with self.assertRaises(DraftError):  # Account-bound: another account of the same client sees nothing.
            self.registry.peek(again.draft_id, owner=DraftOwner(CLIENT, ACCOUNT + 1))

    def test_claim_verifies_every_item_and_returns_their_paths_in_order(self):
        from telegram_search_mcp.outgoing_drafts import DraftError
        draft = self.prepare()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        self.assertIsNone(claim.path)
        self.assertEqual(claim.paths, tuple(self.store.lookup(i).path for i in self.album.artifact_ids))
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        broken = self.prepare(album=self.spec(self.item(PNG, 'b.png'), self.item(JPEG)))
        (self.store.cache_dir / broken.album.items[1].artifact_id).write_bytes(b'changed')
        with self.assertRaises(DraftError):
            self.registry.claim(broken.draft_id, owner=self.owner, approved=True)

    def test_one_receipt_of_every_slot_and_never_a_single_receipt(self):
        draft = self.prepare()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        with self.assertRaises(Exception):
            self.registry.finish(draft.draft_id, owner=self.owner, claim=claim, status='sent', message_id=5)
        for slots in ((('sent', 5),), (('sent', 5), ('sent', 6), ('sent', 7)), (('sent', None), ('failed', None)),
                      (('pending', None), ('sent', 6)), (('failed', 3), ('sent', 6)), [('sent', 5), ('sent', 6)]):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                self.registry.finish_album(draft.draft_id, owner=self.owner, claim=claim, slots=slots)
        receipt = self.registry.finish_album(draft.draft_id, owner=self.owner, claim=claim,
                                             slots=(('sent', 5), ('outcome_unknown', None)))
        self.assertEqual((receipt.status, receipt.slots, receipt.message_id, receipt.evidence),
                         ('partial', (('sent', 5), ('outcome_unknown', None)), None, 'provider'))
        self.assertIs(self.registry.receipt(draft.draft_id, owner=self.owner), receipt)
        # A finished receipt never changes by another finish.
        self.assertIs(self.registry.finish_album(draft.draft_id, owner=self.owner, claim=claim,
                                                 slots=(('failed', None), ('failed', None))), receipt)
        self.assertEqual(self.registry.unsettled_attempts(), 1)
        self.assertEqual(self.registry.attempt_outcome(draft.draft_id, client_id=CLIENT, owner=self.owner,
                                                       kinds=frozenset({'album'})), 'attempted')

    def test_local_failure_receipt(self):
        draft = self.prepare()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        receipt = self.registry.finish_album(draft.draft_id, owner=self.owner, claim=claim, slots=None,
                                             evidence='local_failed')
        self.assertEqual((receipt.status, receipt.slots, receipt.evidence), ('failed', None, 'local_failed'))
        self.assertEqual(self.registry.attempt_outcome(draft.draft_id, client_id=CLIENT, owner=None,
                                                       kinds=frozenset({'album'})), 'local_failed')
        self.assertEqual(self.registry.unsettled_attempts(), 0)

    def test_reconcile_refines_only_unknown_slots_with_exact_evidence(self):
        from telegram_search_mcp.outgoing_drafts import DraftError
        draft = self.prepare(album=self.spec(self.photo, self.video, self.item(JPEG)))
        provider, epoch = object(), object()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True, provider=provider,
                                    provider_epoch=epoch)
        state, receipt, _, attempted_at, _ = self.registry.status_snapshot(draft.draft_id, owner=self.owner)
        self.assertEqual((state, receipt), ('claimed', None))
        self.registry.finish_album(draft.draft_id, owner=self.owner, claim=claim,
                                   slots=(('sent', 5), ('outcome_unknown', None), ('outcome_unknown', None)))
        with self.assertRaises(Exception):
            self.registry.reconcile(draft.draft_id, owner=self.owner, provider=provider, provider_epoch=epoch,
                                    attempted_at=attempted_at, status='sent', message_id=9)
        for changes in ({'provider': object()}, {'provider_epoch': object()}, {'attempted_at': attempted_at + 1}):
            with self.subTest(changes=changes), self.assertRaises(DraftError):
                self.registry.reconcile_album(draft.draft_id, owner=self.owner, **{
                    'provider': provider, 'provider_epoch': epoch, 'attempted_at': attempted_at, **changes},
                    outcomes=(('sent', 5), ('sent', 6), ('failed', None)))
        receipt = self.registry.reconcile_album(draft.draft_id, owner=self.owner, provider=provider,
            provider_epoch=epoch, attempted_at=attempted_at,
            outcomes=(('failed', None), ('sent', 6), ('outcome_unknown', None)))
        self.assertEqual((receipt.status, receipt.slots), ('partial', (('sent', 5), ('sent', 6), ('outcome_unknown', None))))
        receipt = self.registry.reconcile_album(draft.draft_id, owner=self.owner, provider=provider,
            provider_epoch=epoch, attempted_at=attempted_at,
            outcomes=(('sent', 5), ('failed', None), ('failed', None)))
        self.assertEqual((receipt.status, receipt.slots), ('partial', (('sent', 5), ('sent', 6), ('failed', None))))
        self.assertEqual(self.registry.unsettled_attempts(), 0)
        self.clock[0] += 900
        with self.assertRaises(DraftError):
            self.registry.reconcile_album(draft.draft_id, owner=self.owner, provider=provider, provider_epoch=epoch,
                                          attempted_at=attempted_at, outcomes=(('sent', 5),) * 3)


class StagingTests(Drafts, unittest.TestCase):
    """Criterion 3: one draft_<32hex> directory per item; single sends unaffected; whole-album retirement."""

    def setUp(self):
        super().setUp()
        self.stage = self.root / 'outgoing'

    def staged(self, album=None):
        from telegram_search_mcp.outgoing_stage import stage_approved_album
        draft = self.prepare(album=album)
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        return draft, claim, stage_approved_album(claim, root=self.stage)

    def single(self, data=b'single document'):
        from telegram_search_mcp.outgoing_stage import stage_approved_document
        artifact = self.artifact(data)
        draft = self.registry.prepare(owner=self.owner, artifact_id=artifact.artifact_id, recipient=RECIPIENT,
                                      display_name='single.pdf', mime_type='application/pdf', caption='')
        return stage_approved_document(self.registry.claim(draft.draft_id, owner=self.owner, approved=True),
                                       root=self.stage)

    def test_one_private_directory_per_item_derived_from_the_album_handle(self):
        import re
        from telegram_search_mcp.outgoing_stage import album_item_directory, album_staged_paths
        from telegram_search_mcp.tdjson import _valid_staged_input_file
        draft, claim, paths = self.staged()
        self.assertEqual(len(paths), 2)
        self.assertEqual(album_staged_paths(draft.draft_id, draft.album, root=self.stage), paths)
        for index, (path, item, source) in enumerate(zip(paths, self.album.items, claim.paths)):
            with self.subTest(index=index):
                self.assertEqual(path.parent.parent, self.stage)
                self.assertEqual(path.parent.name, album_item_directory(draft.draft_id, index))
                self.assertRegex(path.parent.name, re.compile(r'draft_[0-9a-f]{32}\Z'))
                self.assertNotEqual(path.parent.name, draft.draft_id)
                self.assertEqual(path.name, item.display_name)
                self.assertEqual(list(path.parent.iterdir()), [path])
                self.assertEqual(path.read_bytes(), source.read_bytes())
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertTrue(_valid_staged_input_file({'@type': 'inputFileLocal', 'path': str(path)}, self.stage))
        self.assertEqual(len({path.parent.stat().st_mtime for path in paths}), 1)  # They expire together.

    def test_duplicate_delivered_names_are_allowed(self):
        album = self.spec(self.item(PNG, 'same.png'), self.item(image('PNG', (30, 20)), 'same.png'))
        _, _, paths = self.staged(album)
        self.assertEqual([path.name for path in paths], ['same.png', 'same.png'])
        self.assertNotEqual(paths[0].parent, paths[1].parent)

    def test_single_sends_are_unaffected_while_an_album_is_staged(self):
        from telegram_search_mcp.outgoing_stage import retire_staged_document
        _, _, paths = self.staged()
        single = self.single()
        self.assertEqual(single.read_bytes(), b'single document')
        retire_staged_document(single, root=self.stage)
        self.assertFalse(single.exists())
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(self.single(b'another').read_bytes(), b'another')

    def test_capacity_counts_the_whole_album_before_copying(self):
        from telegram_search_mcp.outgoing_stage import stage_approved_album
        draft = self.prepare()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        with patch('telegram_search_mcp.outgoing_stage.MAX_STAGING_BYTES', self.album.total_bytes - 1), \
                self.assertRaises(ValueError):
            stage_approved_album(claim, root=self.stage)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_a_failure_mid_way_leaves_no_item_staged(self):
        from telegram_search_mcp.outgoing_stage import stage_approved_album
        draft = self.prepare()
        claim = self.registry.claim(draft.draft_id, owner=self.owner, approved=True)
        data = bytearray(claim.paths[1].read_bytes()); data[-1] ^= 1
        claim.paths[1].write_bytes(bytes(data))  # Same size, other bytes: the second copy fails its hash.
        with self.assertRaises(ValueError):
            stage_approved_album(claim, root=self.stage)
        self.assertEqual(list(self.stage.iterdir()), [])
        with self.assertRaises(ValueError):  # Never a claim of another kind.
            stage_approved_album(self.registry.claim(
                self.registry.prepare(owner=self.owner, artifact_id=self.artifact(b'x').artifact_id,
                                      recipient=RECIPIENT, display_name='x.pdf', mime_type='application/pdf',
                                      caption='').draft_id, owner=self.owner, approved=True), root=self.stage)

    def test_an_album_is_staged_at_most_once(self):
        from telegram_search_mcp.outgoing_stage import stage_approved_album
        _, claim, paths = self.staged()
        with self.assertRaises(OSError):
            stage_approved_album(claim, root=self.stage)
        self.assertTrue(all(path.exists() for path in paths))

    def test_retirement_takes_every_item(self):
        from telegram_search_mcp.outgoing_stage import retire_staged_album
        _, _, paths = self.staged()
        retire_staged_album(paths, root=self.stage)
        self.assertEqual(list(self.stage.iterdir()), [])
        retire_staged_album(paths, root=self.stage)  # Idempotent.

    def test_expired_items_are_swept_together_by_a_later_single_send(self):
        import os
        import time
        _, _, paths = self.staged()
        old = time.time() - 13 * 3600
        for path in paths:
            os.utime(path.parent, (old, old))
        single = self.single()
        self.assertEqual(sorted(entry.name for entry in self.stage.iterdir()), [single.parent.name])


VIDEO_FACTS = (5, 1280, 720, True)


class TransportSendTests(unittest.TestCase):
    """Criteria 2 and 4 through the real transport and reducer: exactly one attempt, never retried."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.raw = Raw()
        self.client = TDLibClient(raw=self.raw, staging_root=self.root)
        self.client._ready = True

    def items(self, *kinds, captions=None):
        names = {'photo': 'p.jpg', 'video': 'v.mp4', 'document': 'd.pdf'}
        result = []
        for index, kind in enumerate(kinds):
            path = self.root / f'draft_{index + 1:032x}' / names[kind]
            result.append((kind, path, (captions or [''] * len(kinds))[index], VIDEO_FACTS if kind == 'video' else None))
        return tuple(result)

    def send(self, items=None, **changes):
        arguments = dict(expected_account_id=ACCOUNT, expected_recipient_title='Recipient',
                         expected_recipient_identity=('chatTypePrivate', RECIPIENT), attempt_id='attempt',
                         pre_send_guard=lambda: True)
        arguments.update(changes)
        return self.client.send_album_message(RECIPIENT, items or self.items('photo', 'video', 'photo'), **arguments)

    def albums(self):
        return [request for request in self.raw.sent if request['@type'] == 'sendMessageAlbum']

    def test_one_exact_closed_request_and_every_slot_confirmed(self):
        items = self.items('photo', 'video', 'photo', captions=['One', '', 'Three'])
        self.assertEqual(self.send(items), (('sent', 701), ('sent', 702), ('sent', 703)))
        [request] = self.albums()
        sending_id = request['options']['sending_id']
        self.assertTrue(0 < sending_id < 2**31)
        self.assertEqual(request, {
            '@type': 'sendMessageAlbum', 'chat_id': RECIPIENT, 'topic_id': None, 'reply_to': None,
            'options': {**SEND_OPTIONS, 'sending_id': sending_id}, '@extra': request['@extra'],
            'input_message_contents': [photo_content(self.root, 1, 'One'), video_content(self.root, 2),
                                       photo_content(self.root, 3, 'Three')]})
        documents = self.items('document', 'document')
        self.raw.sent.clear()
        self.client._send_observations.invalidate()
        self.assertEqual(self.send(documents, attempt_id='second'), (('sent', 701), ('sent', 702)))
        self.assertEqual(self.albums()[0]['input_message_contents'],
                         [document_content(self.root, 1), document_content(self.root, 2)])

    def test_mixed_outcomes_are_per_slot_and_never_retried(self):
        self.raw.outcomes = ['sent', 'failed', 'silent']
        clock = [1000.0]
        def receive(timeout, raw=self.raw):
            if raw.events:
                return raw.events.popleft()
            clock[0] += timeout
            return None
        self.raw.receive = receive
        with patch('time.monotonic', lambda: clock[0]):
            start = clock[0]
            self.assertEqual(self.send(), (('sent', 701), ('failed', None), ('outcome_unknown', None)))
        self.assertAlmostEqual(clock[0] - start, 120, delta=1)  # Bounded wait, then status reconciles.
        self.assertEqual(len(self.albums()), 1)

    def test_rejection_conflict_and_lost_transport(self):
        self.raw.rejected = True
        self.assertEqual(self.send(attempt_id='rejected'), (('failed', None),) * 3)
        self.raw.rejected = False
        self.raw.tamper = lambda value: {**value, 'messages': [value['messages'][1], value['messages'][0],
                                                                 value['messages'][2]]}  # video first: wrong order
        self.assertEqual(self.send(attempt_id='conflict'), (('outcome_unknown', None),) * 3)  # despite successes
        self.raw.tamper = None
        self.raw.outcomes = ['sent', 'silent', 'silent']
        original = self.raw.album_events
        def lost(request):
            events = original(request)
            self.raw.fail_receive = True
            return events
        self.raw.album_events = lost
        self.assertEqual(self.send(attempt_id='lost'), (('sent', 701), ('outcome_unknown', None),
                                                        ('outcome_unknown', None)))
        self.raw.album_events = original
        send = self.raw.send
        def broken(request):
            if request['@type'] != 'sendMessageAlbum':
                return send(request)
            self.raw.sent.append(copy.deepcopy(request))
            raise OSError('synthetic transport failure')
        self.raw.send = broken
        self.assertEqual(self.send(attempt_id='broken'), (('outcome_unknown', None),) * 3)
        self.assertEqual(len(self.albums()), 4)  # One attempt each, never a second.

    def test_new_messages_before_the_result_never_bind_through_the_transport(self):
        original = self.raw.album_events
        def early(request):
            events = original(request)
            # Every terminal update overtakes the result: they wait for the exact binding.
            return events[:3] + events[4:] + [events[3]]
        self.raw.album_events = early
        self.assertEqual(self.send(), (('sent', 701), ('sent', 702), ('sent', 703)))

    def test_refusals_before_registration_are_no_attempt(self):
        from telegram_search_mcp.tdjson import MessageSendNotAttempted
        outside = (('photo', Path('/tmp/unapproved.jpg'), '', None), *self.items('photo', 'photo')[1:])
        voice = (*self.items('photo'), ('voice_note', self.root / f'draft_{9:032x}' / 'n.ogg', '', None))
        mixed = self.items('photo', 'document')
        for items, changes in ((None, {'expected_account_id': 8}), (None, {'expected_recipient_title': 'Other'}),
                               (None, {'expected_recipient_identity': ('chatTypePrivate', 1)}),
                               (None, {'pre_send_guard': lambda: False}), (outside, {}), (voice, {}), (mixed, {}),
                               (self.items('photo'), {}), (self.items(*['photo'] * 11), {}),
                               ((('video', self.root / f'draft_{1:032x}' / 'v.mp4', '', (5, 15, 720, True)),
                                 *self.items('photo', 'photo')[1:]), {}),
                               ((('photo', self.root / f'draft_{1:032x}' / 'p.jpg', ' lead', None),
                                 *self.items('photo', 'photo')[1:]), {}),
                               (None, {'pre_send_fence': lambda: (_ for _ in ()).throw(MessageSendNotAttempted('x'))})):
            with self.subTest(items=repr(items)[:60], changes=changes), self.assertRaises(MessageSendNotAttempted):
                self.send(items, **changes)
            self.assertEqual(self.albums(), [])
            self.assertIsNone(self.client._send_observations.snapshot('attempt'))

    def test_matches_the_pinned_tdlib_constructor_fields(self):
        import re
        from telegram_search_mcp.config import TDLIB_LIBRARY
        header_path = TDLIB_LIBRARY.resolve().parent.parent / 'include/td/telegram/td_api.h'
        if not header_path.is_file():
            self.skipTest('pinned TDLib development header is not installed')
        header = header_path.read_text()
        def fields(class_name):
            declaration = header.split(f'class {class_name} final', 1)[1].split('\n};', 1)[0]
            return {name for _, name in re.findall(r'^\s+([^\n]+?)\s+(\w+)_;$', declaration, re.MULTILINE)}
        self.send(self.items('photo', 'video', 'document')[:2])
        [request] = self.albums()
        self.assertEqual(set(request) - {'@type', '@extra'}, fields('sendMessageAlbum'))
        self.assertEqual(set(request['options']) - {'@type'}, fields('messageSendOptions'))


class DialogSeamTests(unittest.TestCase):
    """Part 2 renders an album (tests/test_album_dialog.py); a missing, partial or misplaced album preview still never
    renders: no partial list is ever shown."""

    def test_an_unbound_album_preview_never_renders_and_decides_dialog_unavailable(self):
        from telegram_search_mcp.approval_prompt import decide_approved_send, render_approval
        arguments = dict(recipient_title='Recipient', recipient=RECIPIENT, display_name='Album', size_bytes=10,
                         sha256='a' * 64, caption='')
        for kind, preview in (('album', {'draft_id': 'draft_' + '1' * 32}), ('album', None),
                              ('document', {'draft_id': 'draft_' + '1' * 32})):
            with self.subTest(kind=kind, preview=preview is not None):
                self.assertIsNone(render_approval(**arguments, kind=kind, album_preview=preview))
                with patch('telegram_search_mcp.approval_prompt.subprocess.run',
                           side_effect=AssertionError('no dialog may be shown')):
                    self.assertEqual(decide_approved_send(**arguments, kind=kind, album_preview=preview).outcome,
                                     'dialog_unavailable')


FULL = ('artifacts', 'send', 'album_send', 'video_send')


class BrokerFixture:
    """The production broker over the synthetic transport; staging under a private temporary root."""
    policy = FULL

    def setUp(self):
        import time
        from telegram_search_mcp.artifact_store import ArtifactStore
        from telegram_search_mcp.broker import Broker
        from telegram_search_mcp.config import RuntimePolicy
        self.time = time
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.stage = self.root / 'stage'
        self.raw = Raw(); self.client = TDLibClient(raw=self.raw, staging_root=self.stage)
        self.approvals = []
        self.broker = Broker(socket_path=self.root / 'broker.sock',
            artifact_store=ArtifactStore(cache_dir=self.root / 'cache'), client_factory=lambda: self.client,
            policy=RuntimePolicy(enabled_capabilities=self.policy),
            approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.broker._staging = {'root': self.stage}
        self.addCleanup(self.broker.shutdown)

    def set_policy(self, *capabilities):
        from telegram_search_mcp.config import RuntimePolicy
        self.broker._policy = RuntimePolicy(enabled_capabilities=capabilities)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
            'deadline': self.time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def upload(self, data, name, client=CLIENT):
        import base64
        result = self.dispatch('create_local_artifact', {'file_name': name,
            'content_base64': base64.b64encode(data).decode()}, client)
        self.assertEqual(result['status'], 'complete', result)
        return result['artifact_id']

    def request(self, *artifact_ids, group='media', captions=None):
        return {'recipient': RECIPIENT, 'group': group, 'items': [
            {'artifact_id': artifact_id, 'caption': (captions or [''] * len(artifact_ids))[index]}
            for index, artifact_id in enumerate(artifact_ids)]}

    def media(self):
        return (self.upload(PNG, 'one.png'), self.upload(MOVIE, 'two.mp4'), self.upload(JPEG, 'three.jpg'))

    def prepare(self, *artifact_ids, client=CLIENT, **extra):
        result = self.dispatch('prepare_album_send', self.request(*(artifact_ids or self.media()), **extra), client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['album']

    def send(self, did, approved=True, client=CLIENT):
        return self.dispatch('send_prepared_album', {'draft_id': did, 'approved': approved}, client)

    def status(self, did, client=CLIENT):
        return self.dispatch('get_album_send_status', {'draft_id': did}, client)

    def albums(self):
        return [request for request in self.raw.sent if request['@type'] == 'sendMessageAlbum']

    def staged(self):
        return sorted(path for path in self.stage.rglob('*') if path.is_file()) if self.stage.exists() else []


UNAVAILABLE_PREPARE = {'status': 'unavailable', 'album': None, 'refusal': None, 'detail': 'album draft is unavailable'}


class CapabilityConjunctionTests(BrokerFixture, unittest.TestCase):
    """Criterion 6 at the broker: album_send with send, and video_send as well for video items."""

    def test_album_send_and_send_are_both_required_before_any_artifact_or_provider_work(self):
        from telegram_search_mcp.contract import CompatibilityError
        ids = self.media()
        for capabilities in (('artifacts', 'send', 'video_send'), ('artifacts', 'album_send', 'video_send'),
                             ('artifacts',)):
            self.set_policy(*capabilities)
            before = len(self.raw.sent)
            for op, payload in (('prepare_album_send', self.request(*ids)),
                                ('send_prepared_album', {'draft_id': 'draft_' + '1' * 32, 'approved': True}),
                                ('get_album_send_status', {'draft_id': 'draft_' + '1' * 32})):
                with self.subTest(capabilities=capabilities, op=op), self.assertRaises(CompatibilityError):
                    self.dispatch(op, payload)
            self.assertEqual(len(self.raw.sent), before)
        self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_video_items_also_need_video_send(self):
        photo, video, jpeg = self.media()
        self.set_policy('artifacts', 'send', 'album_send')
        self.assertEqual(self.prepare(photo, jpeg)['items'][0]['kind'], 'photo')  # Photos alone need no video_send.
        before = len(self.raw.sent)
        # Part 2: a structured refusal naming only the missing capability, so the client can tell the owner.
        self.assertEqual(self.dispatch('prepare_album_send', self.request(photo, video)),
                         {'status': 'capability_disabled', 'album': None, 'refusal': None,
                          'missing_capabilities': ['video_send'],
                          'detail': 'album needs capabilities the owner has not enabled: missing_capabilities',
                          'next_action': 'enable_capability'})
        # Refused before the recipient is read (only the account was).
        self.assertNotIn('getChat', [request['@type'] for request in self.raw.sent[before:]])
        self.assertEqual(len(self.broker._drafts.known_ids()), 1)

    def test_a_prepared_video_album_never_sends_or_discloses_once_video_send_is_off(self):
        from telegram_search_mcp.contract import CapabilityDisabled
        album = self.prepare()
        self.set_policy('artifacts', 'send', 'album_send')
        with self.assertRaises(CapabilityDisabled) as refused:
            self.send(album['draft_id'])
        self.assertEqual(refused.exception.missing, ('video_send',))
        self.assertEqual(self.status(album['draft_id'])['evidence'], 'none')
        self.assertEqual((self.approvals, self.albums()), ([], []))
        self.set_policy(*FULL)
        def disable(**kw):
            self.set_policy('artifacts', 'send', 'album_send')
            return True
        self.broker._approval_prompt = disable  # Turned off while the dialog is open.
        result = self.send(album['draft_id'])
        self.assertIn(result['status'], ('expired', 'local_failed'))
        self.assertEqual(self.albums(), [])

    def test_the_policy_is_rechecked_inside_the_raw_dispatch_fence(self):
        album = self.prepare()
        fence = self.broker._album_send_fence
        calls = []
        def late_disable(*args):
            calls.append(args)
            if len(calls) == 2:  # The provider's raw-dispatch fence, after every guard has passed.
                self.set_policy('artifacts', 'send', 'album_send')
            return fence(*args)
        with patch.object(self.broker, '_album_send_fence', side_effect=late_disable):
            result = self.send(album['draft_id'])
        self.assertEqual(len(calls), 2)
        self.assertEqual((result['status'], self.albums()), ('local_failed', []))


class AdmissionTests(BrokerFixture, unittest.TestCase):
    """Work item 2: every item id is authorized before any other work; one refusal refuses the draft."""

    def test_a_foreign_or_unknown_item_refuses_the_whole_draft_with_one_fixed_shape(self):
        own = self.media()
        foreign = self.upload(PNG + b'foreign', 'foreign.png', client=FOREIGN)
        unknown = 'artifact_' + 'c' * 32 + '_' + 'd' * 64 + '_10'
        for position in range(3):
            for intruder in (foreign, unknown):
                ids = list(own); ids[position] = intruder
                before = len(self.raw.sent)
                with self.subTest(position=position, intruder=intruder[:20]):
                    with patch('telegram_search_mcp.album_drafts.album_item',
                               side_effect=AssertionError('classified before admission')), \
                            patch.object(self.broker, '_attachment_metadata',
                                         side_effect=AssertionError('metadata before admission')):
                        self.assertEqual(self.dispatch('prepare_album_send', self.request(*ids)), UNAVAILABLE_PREPARE)
                    self.assertEqual(len(self.raw.sent), before)
        self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_malformed_requests_are_refused_by_the_closed_model(self):
        from pydantic import ValidationError
        photo, video, _ = self.media()
        for payload in (self.request(photo), self.request(photo, photo), self.request(photo, video, group='audio'),
                        {**self.request(photo, video), 'reply_to': 5}):
            with self.subTest(payload=repr(payload)[:80]), self.assertRaises(ValidationError):
                self.dispatch('prepare_album_send', payload)
        self.assertEqual(self.broker._drafts.known_ids(), set())


class PrepareTests(BrokerFixture, unittest.TestCase):
    def test_prepare_binds_every_item_in_order(self):
        from telegram_search_mcp.album_drafts import AlbumDraftPreview
        ids = self.media()
        album = self.prepare(*ids, captions=['First', '', 'Third'])
        AlbumDraftPreview.model_validate(album)
        self.assertEqual([(i['position'], i['kind'], i['artifact_id']) for i in album['items']],
                         [(1, 'photo', ids[0]), (2, 'video', ids[1]), (3, 'photo', ids[2])])
        self.assertEqual((album['recipient'], album['group'], album['account_id']),
                         ({'chat_id': RECIPIENT, 'title': 'Recipient'}, 'media', ACCOUNT))
        from telegram_search_mcp.outgoing_drafts import DraftOwner
        draft = self.broker._drafts.peek(album['draft_id'], owner=DraftOwner(CLIENT, ACCOUNT))
        self.assertEqual((draft.kind, draft.sha256, draft.size_bytes), ('album', album['album_sha256'],
                                                                         album['total_size_bytes']))

    def test_documents_group_and_refusals(self):
        pdf, png = self.upload(PDF, 'report.pdf'), self.upload(PNG, 'scan.png')
        album = self.prepare(pdf, png, group='documents')
        self.assertEqual([i['kind'] for i in album['items']], ['document', 'document'])
        before = self.broker._drafts.known_ids()
        refused = self.dispatch('prepare_album_send', self.request(pdf, png))
        self.assertEqual(refused, {'status': 'refused', 'album': None, 'refusal': 'unsupported_item',
                                   'detail': 'an item is not eligible for this album; no draft was created'})
        with patch('telegram_search_mcp.album_drafts.MAX_ALBUM_BYTES', 10):
            self.assertEqual(self.dispatch('prepare_album_send', self.request(pdf, png, group='documents'))['refusal'],
                             'album_too_large')
        self.assertEqual(self.broker._drafts.known_ids(), before)

    def test_unreadable_recipient_is_unavailable(self):
        self.raw.chats.pop(RECIPIENT)
        self.assertEqual(self.dispatch('prepare_album_send', self.request(*self.media())), UNAVAILABLE_PREPARE)
        self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_generic_and_other_send_tools_never_touch_album_drafts(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.dispatch('get_draft', {'draft_id': did})['status'], 'unavailable')
        self.assertEqual(self.dispatch('cancel_draft', {'draft_id': did})['status'], 'unavailable')
        self.assertEqual(self.dispatch('list_drafts', {})['drafts'], [])
        self.assertEqual(self.dispatch('send_prepared_artifact', {'draft_id': did, 'approved': True})['status'], 'error')
        self.assertEqual(self.dispatch('send_prepared_video', {'draft_id': did, 'approved': True})['status'], 'error')
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'none')
        self.assertEqual((self.approvals, self.albums()), ([], []))
        self.assertEqual(self.status(did)['status'], 'pending')


class SendTests(BrokerFixture, unittest.TestCase):
    """Criteria 2, 3 and 5 through the broker: one dialog, one attempt, receipt replay, whole-album retirement."""

    def test_one_dialog_one_attempt_then_receipt_replay(self):
        from telegram_search_mcp.album_drafts import AlbumDraftPreview
        album = self.prepare()
        result = self.send(album['draft_id'])
        self.assertEqual((result['status'], result['album_sha256'], result['recipient']),
                         ('sent', album['album_sha256'], RECIPIENT))
        self.assertEqual(result['slots'], [{'position': 1, 'status': 'sent', 'message_id': 701},
                                           {'position': 2, 'status': 'sent', 'message_id': 702},
                                           {'position': 3, 'status': 'sent', 'message_id': 703}])
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['sha256'], dialog['size_bytes'], dialog['recipient'],
                          dialog['recipient_title']),
                         ('album', album['album_sha256'], album['total_size_bytes'], RECIPIENT, 'Recipient'))
        self.assertEqual(AlbumDraftPreview.model_validate(dialog['album_preview']).model_dump(mode='json'), album)
        [request] = self.albums()
        paths = [Path(content[kind][kind]['path']) for content, kind in zip(
            request['input_message_contents'], ('photo', 'video', 'photo'))]
        self.assertTrue(all(path.parent.parent == self.stage for path in paths))
        self.assertEqual(self.staged(), [])  # Every slot is terminal: every item is retired.
        replay = self.send(album['draft_id'])
        self.assertEqual((replay['status'], replay['slots']), ('sent', result['slots']))
        self.assertIn('no new send', replay['detail'])
        self.assertEqual((len(self.albums()), len(self.approvals)), (1, 1))

    def test_partial_outcome_keeps_the_staged_items_until_every_slot_ends(self):
        self.raw.outcomes = ['sent', 'failed', 'silent']
        self.client.ALBUM_CONFIRMATION_SECONDS = 0.05
        album = self.prepare()
        result = self.send(album['draft_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([slot['status'] for slot in result['slots']], ['sent', 'failed', 'outcome_unknown'])
        self.assertEqual(len(self.staged()), 3)  # Slot 3 may still upload from its staged copy.
        status = self.status(album['draft_id'])
        self.assertEqual((status['status'], status['evidence'], [s['status'] for s in status['slots']]),
                         ('partial', 'provider', ['sent', 'failed', 'outcome_unknown']))
        self.assertEqual(len(self.staged()), 3)
        self.raw.events.append(succeeded(-13, 903, 'messagePhoto'))  # The late, exact end of slot 3.
        status = self.status(album['draft_id'])
        self.assertEqual([(s['status'], s.get('message_id')) for s in status['slots']],
                         [('sent', 701), ('failed', None), ('sent', 903)])
        self.assertEqual(status['status'], 'partial')
        self.assertEqual(self.staged(), [])
        replay = self.send(album['draft_id'])
        self.assertEqual([s['status'] for s in replay['slots']], ['sent', 'failed', 'sent'])
        self.assertEqual(len(self.albums()), 1)  # No path resends.

    def test_unknown_and_failed_outcomes_never_resend(self):
        for outcomes, expected in ((['silent', 'silent', 'silent'], 'outcome_unknown'),
                                   (['failed', 'silent', 'failed'], 'outcome_unknown'),
                                   (['failed', 'failed', 'failed'], 'failed')):
            with self.subTest(outcomes=outcomes):
                self.setUp()
                self.raw.outcomes = outcomes
                self.client.ALBUM_CONFIRMATION_SECONDS = 0.05
                did = self.prepare()['draft_id']
                self.assertEqual(self.send(did)['status'], expected)
                for _ in range(2):
                    self.assertEqual(self.send(did)['status'], expected)
                    self.assertEqual(self.status(did)['status'], expected)
                self.assertEqual((len(self.albums()), len(self.approvals)), (1, 1))
                self.assertNotIn('PRIVATE', repr(self.send(did)))
        self.setUp()
        self.raw.rejected = True
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did)['status'], 'failed')
        self.assertEqual(self.staged(), [])

    def test_not_approved_and_dialog_outcomes_make_no_attempt(self):
        from telegram_search_mcp.approval_prompt import ApprovalDecision, DETAILS, NOT_GIVEN
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, approved=False)['detail'], NOT_GIVEN)
        for answer, status in ((False, 'declined'), (ApprovalDecision('timeout'), 'timeout'),
                               (ApprovalDecision('dialog_unavailable'), 'dialog_unavailable')):
            with self.subTest(answer=answer):
                self.broker._approval_prompt = lambda answer=answer, **kw: answer
                result = self.send(did)
                self.assertEqual((result['status'], result['detail']), (status, DETAILS[status]))
        self.assertEqual((self.albums(), self.staged()), ([], []))
        self.broker._approval_prompt = lambda **kw: True
        self.assertEqual(self.send(did)['status'], 'sent')

    def test_the_shipped_dialog_lists_every_item_and_only_an_approval_sends(self):
        from types import SimpleNamespace
        from telegram_search_mcp.approval_prompt import decide_approved_send
        self.broker._approval_prompt = decide_approved_send
        album = self.prepare(captions=['First', '', 'Third'])
        did, shown = album['draft_id'], []

        def answer(stdout, returncode=0):
            def run(argv, **options):
                shown.append(argv[-1])
                return SimpleNamespace(returncode=returncode, stdout=stdout)
            return run
        for stdout, returncode, status in ((b'DECLINED\n', 0, 'declined'), (b'TIMEOUT\n', 0, 'timeout'),
                                           (b'APPROVED\n', 1, 'dialog_unavailable'), (b'', 0, 'dialog_unavailable')):
            with self.subTest(status=status, returncode=returncode), \
                    patch('telegram_search_mcp.approval_prompt.subprocess.run', side_effect=answer(stdout, returncode)):
                self.assertEqual(self.send(did)['status'], status)
        self.assertEqual((self.albums(), self.staged()), ([], []))
        rows = shown[0].replace('\n┆ ', '').split('\n')
        self.assertEqual(rows[0], f'Send an album of 3 items to «Recipient» (chat {RECIPIENT})')
        self.assertEqual([row.split(';')[0] for row in rows if row.startswith('Item ') and ' of 3: ' in row],
                         ['Item 1 of 3: photo «one.png»', 'Item 2 of 3: video «two.mp4»', 'Item 3 of 3: photo «three.jpg»'])
        self.assertIn(f'Album SHA-256: {album["album_sha256"]}', rows)
        self.assertEqual(len(set(shown)), 1)  # The same complete document every time.
        with patch('telegram_search_mcp.approval_prompt.subprocess.run', side_effect=answer(b'APPROVED\n')):
            self.assertEqual(self.send(did)['status'], 'sent')
        self.assertEqual(len(self.albums()), 1)

    def test_revalidation_before_the_dialog(self):
        for expected, change in (
                ('recipient_changed', lambda ids: self.raw.chats[RECIPIENT].update(title='Renamed')),
                ('expired', lambda ids: (self.broker._artifact_store.cache_dir / ids[1]).write_bytes(MOVIE[:-1] + b'X')),
                ('expired', lambda ids: (self.broker._artifact_store.cache_dir / ids[2]).unlink())):
            with self.subTest(expected=expected):
                self.setUp()
                ids = self.media()
                did = self.prepare(*ids)['draft_id']
                change(ids)
                self.assertEqual(self.send(did)['status'], expected)
                self.assertEqual((self.approvals, self.albums()), ([], []))

    def test_a_local_staging_failure_is_local_failed_and_replays(self):
        did = self.prepare()['draft_id']
        with patch('telegram_search_mcp.outgoing_stage.MAX_STAGING_BYTES', 10):
            result = self.send(did)
        self.assertEqual((result['status'], result['slots']), ('local_failed', []))
        self.assertEqual((self.albums(), self.staged()), ([], []))
        self.assertEqual(self.send(did)['status'], 'local_failed')
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence']), ('failed', 'local_failed'))

    def test_a_foreign_client_or_account_sees_nothing(self):
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did, client=FOREIGN)['status'], 'expired')
        self.assertEqual(self.status(did, client=FOREIGN)['evidence'], 'none')
        self.raw.account = ACCOUNT + 1
        self.assertEqual(self.send(did)['status'], 'expired')
        self.assertEqual(self.status(did)['evidence'], 'none')
        self.assertEqual((self.approvals, self.albums()), ([], []))

    def test_release_or_account_switch_during_the_wait_is_outcome_unknown_never_expired(self):
        unknown = {'status': 'outcome_unknown', 'recipient': None, 'album_sha256': None, 'slots': [],
                   'detail': 'current receipt authority is unavailable; do not retry automatically',
                   'next_action': 'check_send_status'}
        for action in (lambda: self.broker._artifact_authority.release(CLIENT),
                       lambda: setattr(self.raw, 'account', ACCOUNT + 1)):
            with self.subTest(action=action):
                self.setUp()
                did = self.prepare()['draft_id']
                real = self.raw.send
                def send(request, real=real, action=action):
                    real(request)
                    if request['@type'] == 'sendMessageAlbum':
                        action()
                self.raw.send = send
                self.assertEqual(self.send(did), {**unknown, 'draft_id': did})
                receipt = self.broker._drafts._entries[did].receipt
                self.assertEqual(receipt.status, 'sent')  # Kept, never disclosed to a lost authority.
                self.assertEqual(self.send(did)['status'], 'expired')  # A later call: content-free, no attempt.
                self.assertEqual((len(self.albums()), len(self.approvals)), (1, 1))

    def test_late_updates_after_the_observation_expired_change_nothing(self):
        self.raw.outcomes = ['sent', 'silent', 'silent']
        self.client.ALBUM_CONFIRMATION_SECONDS = 0.05
        did = self.prepare()['draft_id']
        self.assertEqual(self.send(did)['status'], 'partial')
        with patch('telegram_search_mcp.send_observations.RETENTION_SECONDS', 0):
            self.raw.events.extend([succeeded(-12, 902, 'messageVideo'), succeeded(-13, 903, 'messagePhoto')])
            status = self.status(did)
        self.assertEqual([s['status'] for s in status['slots']], ['sent', 'outcome_unknown', 'outcome_unknown'])
        self.assertEqual(len(self.staged()), 3)  # Never retired on an unknown slot; the 12-hour sweep expires them.
        self.assertEqual(len(self.albums()), 1)

    def test_an_attempt_in_flight_reads_pending_with_live_slots(self):
        from telegram_search_mcp.outgoing_drafts import DraftOwner
        did = self.prepare()['draft_id']
        owner = DraftOwner(CLIENT, ACCOUNT)
        self.broker._drafts.claim(did, owner=owner, approved=True, provider=self.client,
                                  provider_epoch=self.client.send_observation_epoch)
        status = self.status(did)
        self.assertEqual((status['status'], status['evidence'], [s['status'] for s in status['slots']]),
                         ('pending', 'local_claimed', ['pending'] * 3))
        types = ('messagePhoto', 'messageVideo', 'messagePhoto')
        self.client._send_observations.register_album(did, 'e', 5, RECIPIENT, types)
        self.client._send_observations.reduce(result(types))
        self.client._send_observations.reduce(succeeded(-11, 801, 'messagePhoto'))
        status = self.status(did)
        self.assertEqual((status['status'], [(s['status'], s.get('message_id')) for s in status['slots']]),
                         ('pending', [('sent', 801), ('pending', None), ('pending', None)]))  # Never partial in flight.
        self.assertEqual(self.albums(), [])

    def test_counted_as_a_send_in_flight(self):
        from telegram_search_mcp.broker import SEND_OPERATIONS
        self.assertIn('send_prepared_album', SEND_OPERATIONS)
        self.raw.outcomes = ['sent', 'silent', 'silent']
        self.client.ALBUM_CONFIRMATION_SECONDS = 0.05
        self.send(self.prepare()['draft_id'])
        self.assertEqual(self.broker._health()['pending_sends']['unreconciled_receipts'], 1)


if __name__ == '__main__':
    unittest.main()
