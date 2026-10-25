"""0.58 part 2: media TextUrl (v24), CustomEmoji (v25/v26) and voice-note MediaTimestamp (v28) reply sources.

Expected displays, records and capability tuples are hand-written from the spec, not read back from production
helpers; json only decodes the one display layer. The broker cases run the production broker, registry and
reducers over the synthetic raw transport of test_text_replies; only raw provider I/O and the owner callback are
synthetic. No case contacts Telegram.
"""
import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

from telegram_search_mcp.config import CAPABILITY_PROFILES, KNOWN_CAPABILITIES, RuntimePolicy, profile_of
from telegram_search_mcp.contract import (OPERATION_CAPABILITIES, READ_DEPENDENT_CAPABILITIES,
                                          SEND_DEPENDENT_CAPABILITIES, CapabilityName)
from telegram_search_mcp.reply_drafts import ReplySource, canonical, source_from_message
from telegram_search_mcp import reply_formatted_sources, reply_media_sources
from test_artifact_replies import ArtifactReplyFixture
from test_text_replies import ANCHOR, ReplyFixture

CHAT, MESSAGE = 123, 55
MARK = '[untrusted Telegram evidence] '
FORMATTED, MEDIA = MARK + 'Formatted target: ', MARK + 'Media target: '
EMOJI_ID = '5368324170671202286'  # synthetic 19-digit decimal string, as TDLib's JSON writes an int64
URL = 'https://example.invalid/a?x=1&y=2'
UNIQUE = hashlib.sha256(b'families-fixture').hexdigest()
FAMILY_CAPS = ('reply_custom_emoji_targets', 'reply_media_timestamp_targets')
SHIM_KEYWORDS = dict(lexical=True, identity=True, datetime=True, datetime_relative=True, datetime_absolute=True,
                     url=True, email=True, phone=True, bank_card=True)
# The exact 0.57 TDLib request allowlist (tests/test_caption_edit.py ALLOWED); 0.58 adds nothing.
ALLOWED_0_57 = frozenset({
    'getAuthorizationState', 'setTdlibParameters', 'checkDatabaseEncryptionKey', 'getMe', 'createPrivateChat',
    'searchPublicChat', 'searchChats', 'searchChatsOnServer', 'getChats', 'loadChats', 'getChat',
    'getChatListsToAddChat', 'getSupergroup', 'getForumTopics', 'getForumTopic', 'getForumTopicHistory',
    'searchMessages', 'searchChatMessages', 'getMessage', 'getMessageProperties', 'getChatHistory', 'getMessageLink',
    'getUser', 'downloadFile', 'getFile', 'cancelDownloadFile', 'sendMessage', 'sendMessageAlbum', 'forwardMessages',
    'addChatToList', 'editMessageText', 'editMessageCaption', 'pinChatMessage', 'unpinChatMessage', 'deleteMessages'})


def entity(kind, offset=0, length=1, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **fields}}


def emoji(offset=0, length=2, custom_emoji_id=EMOJI_ID):
    return entity('CustomEmoji', offset, length, custom_emoji_id=custom_emoji_id)


def stamp(offset=0, length=4, value=1):
    return entity('MediaTimestamp', offset, length, media_timestamp=value)


def units(text):
    return sum(2 if ord(c) > 0xffff else 1 for c in text)


def media(kind, caption, duration=2):
    file = {'@type': 'file', 'id': 1, 'size': 4, 'expected_size': 4,
            'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False, 'download_offset': 0,
                      'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'families-fixture',
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
        '@type': 'voiceNote', 'duration': duration, 'waveform': 'AAAA', 'mime_type': 'audio/ogg', 'voice': file}}


def message(text, entities, kind='text', duration=2):
    caption = {'@type': 'formattedText', 'text': text, 'entities': entities}
    content = ({'@type': 'messageText', 'text': caption} if kind == 'text' else media(kind, caption, duration))
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE, 'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
            'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0, 'date': 1700000000,
            'edit_date': 0, 'self_destruct_in': 0, 'auto_delete_in': 0, 'content': content}


def source(raw):
    return source_from_message(copy.deepcopy(raw), CHAT, MESSAGE)


def version(raw):
    return json.loads(source(raw).projection_json)['version']


def forged(accepted, new_version=None, **media_changes):
    value = json.loads(accepted.projection_json)
    if new_version is not None:
        value['version'] = new_version
    value['message']['content'].get('media', {}).update(media_changes)
    return canonical(value)


class Refusals:
    def refused(self, raw, **context):
        with self.subTest(**context), self.assertRaises(ValueError):
            source(raw)


class MediaTextUrlTests(Refusals, unittest.TestCase):
    """Work item 3: document, photo and voice-note TextUrl captions select v24 and render the whole record inertly."""

    def test_every_media_kind_selects_v24_under_text_url_authority(self):
        # Break: media TextUrl stays refused, selects a text version or another family's, or skips its capability.
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                accepted = source(message('Read docs', [entity('TextUrl', 5, 4, url=URL)], kind))
                self.assertEqual(json.loads(accepted.projection_json)['version'], 24)
                self.assertIs(accepted.is_media, True)
                self.assertEqual(accepted.required_capabilities,
                                 ('reply_media_targets', 'reply_formatted_targets', 'reply_text_url_targets'))
                mixed = source(message('TBU#tag', [entity('TextUrl', 0, 1, url=''), entity('BankCardNumber', 1),
                                                   entity('Url', 2), entity('Hashtag', 3, 4)], kind))
                self.assertEqual(mixed.required_capabilities, (
                    'reply_media_targets', 'reply_formatted_targets', 'reply_text_url_targets',
                    'reply_bank_card_targets', 'reply_url_targets', 'reply_lexical_targets'))

    def test_the_whole_media_record_is_one_inert_json_layer(self):
        # Break: the sanitizing media display rewrites a hidden URL, a media string escapes the inert layer, or the
        # display is not exactly the hand-written bytes.
        accepted = source(message('Read 😀 docs', [entity('TextUrl', 8, 4, url=URL)], 'document'))
        target = accepted.target()
        self.assertEqual(target.text, MEDIA + (
            r'{"caption":{"entities":[{"length":4,"offset":8,"provider_type":"textEntityTypeTextUrl",'
            r'"provider_url":"https\u003a\u002f\u002fexample\u002einvalid\u002fa\u003fx\u003d1\u0026y\u003d2",'
            r'"text":"docs","type":"text_url"}],'
            r'"offset_basis":"original\u0020source\u0020UTF-16\u0020code\u0020units",'
            r'"text":"Read\u0020\ud83d\ude00\u0020docs"},"kind":"document",'
            r'"media":{"file_name":"fixture\u002etxt","mime_type":"text\u002fplain","size":4,'
            r'"unique_id_sha256":"' + UNIQUE + '"}}'))
        self.assertIs(target.sanitized, True)
        record = json.loads(target.text[len(MEDIA):])
        self.assertEqual(record['caption']['entities'][0]['provider_url'], URL)
        self.assertEqual(record['media']['mime_type'], 'text/plain')

    def test_hidden_values_survive_exactly_and_the_display_bound_refuses_without_truncation(self):
        # Break: a control, bidi, scheme or emoji URL is stripped or normalized, or overflow truncates it.
        for kind in ('document', 'photo', 'voice_note'):
            for hidden in ('', 'a  b', '\u202e', '\x00\x1f\x7f', 'javascript:alert(1)', 'tg://user?id=42', '😀',
                           '<b>[x](y)</b>', '"\\'):
                with self.subTest(kind=kind, hidden=hidden):
                    target = source(message('x', [entity('TextUrl', url=hidden)], kind)).target()
                    self.assertEqual(json.loads(target.text[len(MEDIA):])['caption']['entities'][0]['provider_url'],
                                     hidden)
            self.refused(message('x', [entity('TextUrl', url='a' * 4096)], kind), kind=kind, case='display bound')
            self.refused(message('x', [entity('TextUrl', url='\u202e' * 700)], kind), kind=kind, case='escaped bound')

    def test_forged_versions_refuse_in_both_directions(self):
        # Break: a stored caption without TextUrl claims v24, or a v24 caption claims another version.
        accepted = source(message('x', [entity('TextUrl', url=URL)], 'document'))
        for wrong in (22, 23, 25, 26, 27, 28, '24', True, 24.0):
            with self.subTest(version=wrong), self.assertRaises(ValueError):
                ReplySource(forged(accepted, wrong))
        bank = source(message('x', [entity('BankCardNumber')], 'document'))
        self.assertEqual(json.loads(bank.projection_json)['version'], 22)
        with self.assertRaises(ValueError):
            ReplySource(forged(bank, 24))
        self.assertEqual(ReplySource(accepted.projection_json).target(), accepted.target())

    def test_the_0_57_media_keyword_shims_stay_closed_to_text_url(self):
        # Break: the media helpers' keyword contract silently admits TextUrl through another flag.
        accepted = source(message('x', [entity('TextUrl', url=URL)], 'document'))
        content = json.loads(accepted.projection_json)['message']['content']
        raw = message('x', [entity('TextUrl', url=URL)], 'document')['content']
        for operation in (lambda: reply_media_sources.media_content(raw, **SHIM_KEYWORDS),
                          lambda: reply_media_sources.validate_content(content, **SHIM_KEYWORDS),
                          lambda: reply_media_sources.render_content(content, MARK, **SHIM_KEYWORDS)):
            with self.assertRaises(ValueError):
                operation()


class CustomEmojiTests(Refusals, unittest.TestCase):
    """Work item 4: a decimal-string id over exactly one emoji selects v25 (text) or v26 (captions)."""

    def test_the_id_is_a_bounded_decimal_string(self):
        # Break: an integer id (the 0.57 probes), a signed, padded, non-ASCII or out-of-int64 id is admitted.
        for value in ('1', '9', '10', EMOJI_ID, '9223372036854775807'):
            with self.subTest(value=value):
                self.assertEqual(version(message('😀 x', [emoji(custom_emoji_id=value)])), 25)
        for value in ('0', '01', '-1', '+1', ' 1', '1 ', '1\n', '9223372036854775808', '1' * 20, '١', '１',
                      '', '1.0', '0x1', 1, int(EMOJI_ID), 2**63 - 1, True, None, 1.0, ['1']):
            self.refused(message('😀 x', [emoji(custom_emoji_id=value)]), value=repr(value))
        for typ in ({'@type': 'textEntityTypeCustomEmoji'},
                    {'@type': 'textEntityTypeCustomEmoji', 'custom_emoji_id': EMOJI_ID, 'extra': None},
                    {'@type': 'textEntityTypeCustomEmoji', 'custom_emoji_id': EMOJI_ID, 'url': ''}):
            raw = message('😀 x', [emoji()])
            raw['content']['text']['entities'][0]['type'] = typ
            self.refused(raw, typ=typ)

    def test_the_covered_text_is_exactly_one_emoji(self):
        # Break: a custom emoji hides ordinary text, two emoji, a half sequence or padding behind one picture. ZWJ
        # sequences and tag flags never get here: their format characters (Cf) fail the visible-text control policy.
        # Development check (not vendored): every one of the 2,331 control-free elements of the pinned TDLib's emoji
        # table (tdutils/td/utils/emoji.cpp, d1085f9) is admitted.
        family = '\U0001f468\u200d\U0001f469\u200d\U0001f467'
        england = '\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f'
        for cover in ('\U0001f600', '❤\ufe0f', '❤', '\U0001f44d\U0001f3fb', '\U0001f44d\U0001f3fb\ufe0f',
                      '\U0001f3fb', '\U0001f1fa\U0001f1e6', '#\ufe0f\u20e3', '1\u20e3', '*\u20e3', '©',
                      '™\ufe0f', '\U0001fae8', '\U0001f004', '\U0001f1fa\U0001f1e6\ufe0f',
                      '\U0001f3fb\ufe0f'):
            with self.subTest(cover=cover):
                self.assertEqual(version(message(cover + ' x', [emoji(length=units(cover))])), 25)
        for cover in ('x', 'xy', '\U0001f600\U0001f600', '\U0001f600 ', '\u200d\U0001f600', '\U0001f600\u200d',
                      family, england, '\U0001f1fa', '\U0001f1fa\U0001f1e6\U0001f1fa', '#', '1', '#\u20e3\u20e3',
                      '\u20e3#', '\U0001f600\ufe0f\ufe0f', '\U0001f600\ufe0f\U0001f3fb', '\U0001f44d\U0001f3fb\U0001f3fb',
                      '\U0001f3fb\U0001f3fb', '\u20e3', '\ufe0f', 'é', 'あ', '©©',
                      '\U0001f3fa\U0001f3fb\U0001f3fb', '\U0001f600' * 17, '\U0001f1e6' * 4, '#\u20e3\ufe0f',
                      '\U0001f1fa\U0001f1e6\ufe0f\ufe0f'):
            text = cover + ' x' if cover.strip() else 'x' + cover
            offset = 0 if cover.strip() else 1
            self.refused(message(text, [emoji(offset, units(cover))]), cover=cover)

    def test_text_and_captions_select_their_versions_and_capabilities(self):
        # Break: v25/v26 swapped, the capability missing, or listed after an older family.
        accepted = source(message('😀 x', [emoji()]))
        self.assertEqual(json.loads(accepted.projection_json)['version'], 25)
        self.assertEqual(accepted.required_capabilities, ('reply_formatted_targets', 'reply_custom_emoji_targets'))
        both = source(message('😀 x', [emoji(), entity('TextUrl', 3, 1, url=URL), entity('Bold', 0, 4)]))
        self.assertEqual(both.required_capabilities,
                         ('reply_formatted_targets', 'reply_custom_emoji_targets', 'reply_text_url_targets'))
        for kind in ('document', 'photo', 'voice_note'):
            with self.subTest(kind=kind):
                caption = source(message('😀 x', [emoji()], kind))
                self.assertEqual(json.loads(caption.projection_json)['version'], 26)
                self.assertEqual(caption.required_capabilities,
                                 ('reply_media_targets', 'reply_formatted_targets', 'reply_custom_emoji_targets'))

    def test_the_display_names_the_id_and_the_emoji_exactly(self):
        # Break: the record drops the id, its provider type or offsets, or reformats the id as a number.
        target = source(message('😀 x', [emoji()])).target()
        self.assertEqual(target.text, FORMATTED + (
            '{"entities":[{"custom_emoji_id":"5368324170671202286","length":2,"offset":0,'
            '"provider_type":"textEntityTypeCustomEmoji","text":"😀","type":"custom_emoji"}],'
            '"offset_basis":"original source UTF-16 code units","text":"😀 x"}'))
        self.assertIs(target.sanitized, False)
        caption = source(message('😀 x', [emoji()], 'photo')).target()
        self.assertEqual(json.loads(caption.text[len(MEDIA):])['caption']['entities'],
                         [{'custom_emoji_id': EMOJI_ID, 'length': 2, 'offset': 0,
                           'provider_type': 'textEntityTypeCustomEmoji', 'text': '😀', 'type': 'custom_emoji'}])

    def test_overlap_is_exclusive_and_styles_may_contain_it(self):
        # Break: a custom emoji nests in a link, quote or code span, crosses another entity, or a style refuses it.
        for kind in ('Bold', 'Italic', 'Underline', 'Strikethrough', 'Spoiler'):
            self.assertEqual(version(message('😀 x', [entity(kind, 0, 4), emoji()])), 25)
            self.assertEqual(version(message('😀 x', [entity(kind, 0, 2), emoji()])), 25)
        for other in (entity('TextUrl', 0, 4, url=''), entity('Url', 0, 4), entity('Mention', 0, 4),
                      entity('BlockQuote', 0, 4), entity('Code', 0, 4), entity('Pre', 0, 4),
                      entity('DateTime', 0, 4, unix_time=1, formatting_type=None), emoji(custom_emoji_id='7'),
                      emoji(), entity('Bold', 1, 3)):
            self.refused(message('😀 x', [emoji(), other]), other=other['type']['@type'])

    def test_messages_heavy_with_custom_emoji_hit_the_display_bound_and_refuse(self):
        # Break: the complete display is truncated, or the 4,096 bound is not applied to custom emoji records. Each
        # record is about 140 characters, so 27 custom emoji with 19-digit ids fill a text display and the 28th
        # refuses the source (disclosed in docs/reference.md); short ids fit 31, and 33 entities always refuse.
        def emojis(count, custom_emoji_id=EMOJI_ID, kind='text'):
            return message('\U0001f600' * count, [emoji(2 * i, custom_emoji_id=custom_emoji_id) for i in range(count)],
                           kind)
        for count, custom_emoji_id, kind in ((27, EMOJI_ID, 'text'), (31, '1', 'text'), (26, EMOJI_ID, 'document')):
            with self.subTest(count=count, kind=kind):
                accepted = source(emojis(count, custom_emoji_id, kind))
                self.assertEqual(json.loads(accepted.projection_json)['version'], 25 if kind == 'text' else 26)
                self.assertLessEqual(len(accepted.target().text) - len(MARK), 4096)
        for count, custom_emoji_id, kind in ((28, EMOJI_ID, 'text'), (32, '1', 'text'), (27, EMOJI_ID, 'document'),
                                             (33, '1', 'text')):
            self.refused(emojis(count, custom_emoji_id, kind), count=count, kind=kind)

    def test_forged_versions_and_the_0_57_shims_refuse(self):
        # Break: v25/v26 accepted under another tag, or a 0.57 keyword admits CustomEmoji.
        text = source(message('😀 x', [emoji()]))
        for wrong in (23, 24, 26, 27, 28):
            with self.subTest(version=wrong), self.assertRaises(ValueError):
                ReplySource(forged(text, wrong))
        caption = source(message('😀 x', [emoji()], 'voice_note'))
        for wrong in (22, 24, 25, 27, 28):
            with self.subTest(version=wrong), self.assertRaises(ValueError):
                ReplySource(forged(caption, wrong))
        formatted = json.loads(text.projection_json)['message']['content']['text']
        with self.assertRaises(ValueError):
            reply_formatted_sources.formatted_text(formatted, text_url=True, **SHIM_KEYWORDS)
        with self.assertRaises(ValueError):
            reply_media_sources.media_content(message('😀 x', [emoji()], 'photo')['content'], **SHIM_KEYWORDS)


class MediaTimestampTests(Refusals, unittest.TestCase):
    """Work item 5: a voice-note caption timestamp within the duration, on its whole token, selects v28."""

    def test_a_voice_note_timestamp_selects_v28_and_renders_its_value(self):
        # Break: the timestamp is refused, selects another version, or the record hides its value.
        accepted = source(message('0:01 x', [stamp()], 'voice_note'))
        self.assertEqual(json.loads(accepted.projection_json)['version'], 28)
        self.assertEqual(accepted.required_capabilities,
                         ('reply_media_targets', 'reply_formatted_targets', 'reply_media_timestamp_targets'))
        target = accepted.target()
        self.assertEqual(target.text, MEDIA + (
            '{"caption":{"entities":[{"length":4,"media_timestamp":1,"offset":0,'
            '"provider_type":"textEntityTypeMediaTimestamp","text":"0:01","type":"media_timestamp"}],'
            '"offset_basis":"original source UTF-16 code units","text":"0:01 x"},"kind":"voice_note",'
            '"media":{"duration":2,"mime_type":"audio/ogg","size":4,"unique_id_sha256":"' + UNIQUE + '"}}'))
        self.assertIs(target.sanitized, False)

    def test_the_value_runs_from_zero_to_the_duration(self):
        # Break: a timestamp beyond the voice note's own duration (which TDLib drops) or below zero is admitted.
        for text, value, duration in (('0:00 x', 0, 2), ('0:02 x', 2, 2), ('10:00 x', 600, 600), ('0:59 x', 59, 60),
                                      ('1:00 x', 60, 60)):
            with self.subTest(text=text):
                length = text.index(' ')
                self.assertEqual(version(message(text, [stamp(0, length, value)], 'voice_note', duration)), 28)
        for text, value, duration in (('0:03 x', 3, 2), ('10:01 x', 601, 600), ('1:00 x', 60, 59)):
            self.refused(message(text, [stamp(0, text.index(' '), value)], 'voice_note', duration), text=text)

    def test_the_entity_is_exactly_one_whole_timestamp_token_with_its_value(self):
        # Break: a timestamp disagrees with its visible text, covers part of a token or more, or has a bad field.
        for text, offset, length, value in (('0:01 x', 0, 4, 2), ('0:01 x', 1, 3, 1), ('0:01 x', 0, 3, 0),
                                            ('0:01 x', 0, 5, 1), ('x0:01 y', 1, 4, 1), ('0:011 x', 0, 5, 11),
                                            ('0:1 x', 0, 3, 1), ('a 0:01', 0, 1, 1)):
            self.refused(message(text, [stamp(offset, length, value)], 'voice_note'), text=text, offset=offset)
        for value in (True, False, '1', 1.0, -1, None, 2**31):
            self.refused(message('0:01 x', [stamp(value=value)], 'voice_note'), value=repr(value))
        raw = message('0:01 x', [stamp()], 'voice_note')
        raw['content']['caption']['entities'][0]['type']['extra'] = None
        self.refused(raw, case='extra field')
        del raw['content']['caption']['entities'][0]['type']['extra']
        del raw['content']['caption']['entities'][0]['type']['media_timestamp']
        self.refused(raw, case='missing field')

    def test_text_documents_and_photos_stay_refused_and_v27_is_reserved(self):
        # Break: a timestamp outside a voice note is admitted (TDLib has no duration to bound it there), or a
        # forged v27 decides is_media by something other than parity.
        for kind in ('text', 'document', 'photo'):
            self.refused(message('0:01 x', [stamp()], kind), kind=kind)
            self.refused(message('0:01 x', [entity('Italic', 0, 6), stamp()], kind), kind=kind, case='styled')
        accepted = source(message('0:01 x', [stamp()], 'voice_note'))
        for wrong in (26, 27, 29, 24, 2):
            with self.subTest(version=wrong), self.assertRaises(ValueError):
                ReplySource(forged(accepted, wrong))

    def test_a_stored_timestamp_is_bounded_by_the_stored_duration(self):
        # Break: a stored projection whose voice note was shortened keeps a timestamp beyond its duration.
        accepted = source(message('0:02 x', [stamp(value=2)], 'voice_note'))
        self.assertEqual(ReplySource(forged(accepted, duration=2)).target(), accepted.target())
        with self.assertRaises(ValueError):
            ReplySource(forged(accepted, duration=1))

    def test_all_three_new_families_list_in_the_extended_order(self):
        # Break: the v28 tuple puts an older family before a newer one, or TextUrl stops forcing the inert record.
        raw = message('0:01 😀 x', [stamp(), emoji(5), entity('TextUrl', 8, 1, url='h'), entity('Bold', 0, 9)],
                      'voice_note')
        accepted = source(raw)
        self.assertEqual(json.loads(accepted.projection_json)['version'], 28)
        self.assertEqual(accepted.required_capabilities, (
            'reply_media_targets', 'reply_formatted_targets', 'reply_media_timestamp_targets',
            'reply_custom_emoji_targets', 'reply_text_url_targets'))
        display = accepted.target().text
        self.assertIn(r'"mime_type":"audio\u002fogg"', display)
        self.assertEqual([e['type'] for e in json.loads(display[len(MEDIA):])['caption']['entities']],
                         ['bold', 'media_timestamp', 'custom_emoji', 'text_url'])


class CapabilityPlacementTests(unittest.TestCase):
    """Work item 6: both capabilities are reply conjuncts in KNOWN_CAPABILITIES and the replies profile only."""

    def test_both_capabilities_are_known_reply_conjuncts_only(self):
        # Break: a family capability becomes an operation gate, gains send/read dependence, or leaves the profile.
        for name in FAMILY_CAPS:
            with self.subTest(capability=name):
                self.assertIn(name, KNOWN_CAPABILITIES)
                self.assertIn(name, CapabilityName.__args__)
                self.assertEqual(profile_of(name), 'replies')
                self.assertNotIn(name, SEND_DEPENDENT_CAPABILITIES | READ_DEPENDENT_CAPABILITIES)
                self.assertNotIn(name, set(OPERATION_CAPABILITIES.values()))
        self.assertEqual(CAPABILITY_PROFILES['replies'][-3:], ('reply_text_url_targets',) + FAMILY_CAPS)
        default = RuntimePolicy()
        self.assertFalse(set(FAMILY_CAPS) & set(default.enabled_capabilities))

    def test_the_shared_reply_text_names_both_families(self):
        # Break: the reply tool description omits a family capability a refusal can name.
        from telegram_search_mcp import server
        for name in FAMILY_CAPS:
            self.assertIn(name, server._REPLY)


class UnchangedSurfaceTests(unittest.TestCase):
    """Work item 9 / task item 6: forward and edit admission, and the TDLib allowlist, do not move."""

    def test_forward_and_edit_admission_keep_refusing_both_families(self):
        # Break: the reply families leak into the forward projection or an own-message edit.
        from telegram_search_mcp.edit_drafts import EditRefusal, own_source, own_text_source
        from telegram_search_mcp.forward_sources import ForwardRefusal, project_message
        cases = (('messageText', message('😀 x', [emoji()])),
                 ('messageVoiceNote', message('0:01 x', [stamp()], 'voice_note')),
                 ('messageVoiceNote', message('😀 x', [emoji()], 'voice_note')))
        for content_type, raw in cases:
            raw = dict(copy.deepcopy(raw), author_signature='', via_bot_user_id=0)
            with self.subTest(content=content_type, entity=json.dumps(raw['content'])[:60]):
                with self.assertRaises(ForwardRefusal) as caught:
                    project_message(raw, CHAT, MESSAGE)
                self.assertEqual(caught.exception.code, 'hidden_source_content')
                own = dict(raw, is_outgoing=True)
                with self.assertRaises(EditRefusal) as caught:
                    if content_type == 'messageText':
                        own_text_source(own, CHAT, MESSAGE)
                    else:
                        own_source(own, CHAT, MESSAGE, content_types=frozenset({content_type}))
                self.assertEqual(caught.exception.code, 'hidden_source_content')

    def test_the_tdlib_allowlist_is_the_0_57_set_and_custom_emoji_stickers_stay_unreachable(self):
        # Break: a family adds a TDLib request (getCustomEmojiStickers would fetch sticker files for an id).
        from telegram_search_mcp.tdjson import _ALLOWED_REQUEST_TYPES
        self.assertEqual(set(_ALLOWED_REQUEST_TYPES), set(ALLOWED_0_57))
        self.assertNotIn('getCustomEmojiStickers', _ALLOWED_REQUEST_TYPES)
        root = Path(__file__).resolve().parents[1] / 'src' / 'telegram_search_mcp'
        self.assertEqual([p.name for p in sorted(root.glob('*.py')) if 'getCustomEmojiStickers' in p.read_text()], [])


BASE = ('send', 'reply_text_send', 'reply_artifact_send')


def disabled(*missing):
    return {'status': 'capability_disabled', 'reply': None,
            'detail': 'reply needs capabilities the owner has not enabled: missing_capabilities',
            'missing_capabilities': list(missing), 'next_action': 'enable_capability'}


class TextReplyBrokerTests(ReplyFixture, unittest.TestCase):
    """The production broker names exactly the missing family capability and shows the custom emoji evidence."""

    def test_a_custom_emoji_source_needs_its_capability(self):
        self.raw.source = message('😀 x', [emoji()])
        request = {'recipient': 123, 'text': 'Final reply', 'reply_to': ANCHOR}
        for enabled, missing in (((), ('reply_formatted_targets', 'reply_custom_emoji_targets')),
                                 (('reply_formatted_targets',), ('reply_custom_emoji_targets',)),
                                 (('reply_custom_emoji_targets',), ('reply_formatted_targets',))):
            with self.subTest(enabled=enabled):
                self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, *enabled))
                self.assertEqual(self.dispatch('prepare_reply_text_send', request), disabled(*missing))
        self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_formatted_targets',
                                                                  'reply_custom_emoji_targets'))
        prepared = self.dispatch('prepare_reply_text_send', request)
        self.assertEqual(prepared['status'], 'prepared')
        self.assertIn('"custom_emoji_id":"5368324170671202286"', prepared['reply']['reply_target']['text'])
        self.assertEqual((self.sends(), self.approvals), ([], []))


class ArtifactReplyBrokerTests(ArtifactReplyFixture, unittest.TestCase):
    def prepare_raw(self):
        return self.dispatch('prepare_reply_artifact_send', dict(
            artifact_id=self.artifact.artifact_id, recipient=123, display_name='evidence.txt',
            mime_type='text/plain', caption='Caption', kind='document', reply_to=ANCHOR))

    def test_voice_note_timestamps_and_media_text_urls_need_their_capabilities(self):
        cases = ((message('0:01 x', [stamp()], 'voice_note'), 'reply_media_timestamp_targets'),
                 (message('x', [entity('TextUrl', url=URL)], 'photo'), 'reply_text_url_targets'))
        for raw, family in cases:
            with self.subTest(family=family):
                self.raw.source = raw
                self.broker._policy = RuntimePolicy(enabled_capabilities=BASE)
                self.assertEqual(self.prepare_raw(),
                                 disabled('reply_media_targets', 'reply_formatted_targets', family))
                self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_media_targets',
                                                                          'reply_formatted_targets'))
                self.assertEqual(self.prepare_raw(), disabled(family))
                self.broker._policy = RuntimePolicy(enabled_capabilities=(*BASE, 'reply_media_targets',
                                                                          'reply_formatted_targets', family))
                prepared = self.prepare_raw()
                self.assertEqual(prepared['status'], 'prepared')
                self.assertTrue(prepared['reply']['reply_target']['text'].startswith(MEDIA))
        self.assertEqual(self.sends(), [])


if __name__ == '__main__':
    unittest.main()
