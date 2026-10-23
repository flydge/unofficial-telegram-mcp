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


if __name__ == '__main__':
    unittest.main()
