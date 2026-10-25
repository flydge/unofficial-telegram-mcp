"""The reply-source ladder table (0.58 part 1): one module owns every family's name, keyword, capability, entity
map, rank, text and media version and the fixed capability listing order.

Expected rows are hand-written from the 0.57 hand-copied ladder, not read back from the module. Byte identity of
every source is the separate matrix test (test_reply_ladder_matrix.py); this file pins the table and the rules
that derive version, capabilities and admission from it.
"""
import inspect
import itertools
import json
import unittest

from telegram_search_mcp import reply_formatted_sources, reply_media_sources
from telegram_search_mcp.config import KNOWN_CAPABILITIES
from telegram_search_mcp.reply_drafts import source_from_message

CHAT, MESSAGE = 123, 55
STYLE_TYPES = {
    'textEntityTypeBold': 'bold', 'textEntityTypeItalic': 'italic', 'textEntityTypeUnderline': 'underline',
    'textEntityTypeStrikethrough': 'strikethrough', 'textEntityTypeSpoiler': 'spoiler', 'textEntityTypeCode': 'code',
    'textEntityTypePre': 'pre', 'textEntityTypePreCode': 'pre_code', 'textEntityTypeBlockQuote': 'block_quote',
    'textEntityTypeExpandableBlockQuote': 'expandable_block_quote'}
# name, keyword, capability, rank, text version, media version, entity map, exclusive, inert
TABLE = (
    ('styled', 'styled', 'reply_formatted_targets', 1, 3, 4, STYLE_TYPES, False, False),
    ('lexical', 'lexical', 'reply_lexical_targets', 2, 5, 6,
     {'textEntityTypeHashtag': 'hashtag', 'textEntityTypeCashtag': 'cashtag',
      'textEntityTypeBotCommand': 'bot_command'}, True, False),
    ('identity', 'identity', 'reply_identity_targets', 3, 7, 8,
     {'textEntityTypeMention': 'mention', 'textEntityTypeMentionName': 'mention_name'}, True, False),
    ('datetime', 'datetime', 'reply_datetime_targets', 4, 9, 10, {'textEntityTypeDateTime': 'date_time'}, False, False),
    ('datetime_relative', 'datetime_relative', 'reply_datetime_targets', 5, 11, 12, {}, False, False),
    ('datetime_absolute', 'datetime_absolute', 'reply_datetime_targets', 6, 13, 14, {}, False, False),
    ('url', 'url', 'reply_url_targets', 7, 15, 16, {'textEntityTypeUrl': 'url'}, True, False),
    ('email', 'email', 'reply_email_targets', 8, 17, 18, {'textEntityTypeEmailAddress': 'email_address'}, True, False),
    ('phone', 'phone', 'reply_phone_targets', 9, 19, 20, {'textEntityTypePhoneNumber': 'phone_number'}, True, False),
    ('bank_card', 'bank_card', 'reply_bank_card_targets', 10, 21, 22,
     {'textEntityTypeBankCardNumber': 'bank_card_number'}, True, False),
    ('text_url', 'text_url', 'reply_text_url_targets', 11, 23, None, {'textEntityTypeTextUrl': 'text_url'}, True, True),
)
LEVELS = {'datetime_relative': 'dateTimeFormattingTypeRelative', 'datetime_absolute': 'dateTimeFormattingTypeAbsolute'}
LISTING = ('text_url', 'bank_card', 'phone', 'email', 'url', 'datetime', 'lexical', 'identity')
FORMATTED_KEYWORDS = ('lexical', 'identity', 'datetime', 'datetime_relative', 'datetime_absolute', 'url', 'email',
                      'phone', 'bank_card', 'text_url')
MEDIA_KEYWORDS = FORMATTED_KEYWORDS[:-1]


def ladder():
    from telegram_search_mcp import reply_source_ladder
    return reply_source_ladder


def absolute():
    return {'@type': 'dateTimeFormattingTypeAbsolute', 'time_precision': {'@type': 'dateTimePartPrecisionNone'},
            'date_precision': {'@type': 'dateTimePartPrecisionLong'}, 'show_day_of_week': False}


def entity(kind, offset, length=1, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **fields}}


# One disjoint span per family over 'SLIDRAUEPBT'; a datetime level also needs its DateTime entity.
SPANS = {'styled': lambda: [entity('Bold', 0)], 'lexical': lambda: [entity('Hashtag', 1)],
         'identity': lambda: [entity('Mention', 2)],
         'datetime': lambda: [entity('DateTime', 3, unix_time=1, formatting_type=None)],
         'datetime_relative': lambda: [entity('DateTime', 4, unix_time=1,
                                              formatting_type={'@type': 'dateTimeFormattingTypeRelative'})],
         'datetime_absolute': lambda: [entity('DateTime', 5, unix_time=1, formatting_type=absolute())],
         'url': lambda: [entity('Url', 6)], 'email': lambda: [entity('EmailAddress', 7)],
         'phone': lambda: [entity('PhoneNumber', 8)], 'bank_card': lambda: [entity('BankCardNumber', 9)],
         'text_url': lambda: [entity('TextUrl', 10, url='')]}


def raw(kind, entities):
    caption = {'@type': 'formattedText', 'text': 'SLIDRAUEPBT', 'entities': entities}
    file = {'@type': 'file', 'id': 1, 'size': 4, 'expected_size': 4,
            'local': {'@type': 'localFile', 'path': '', 'can_be_downloaded': True, 'can_be_deleted': False,
                      'is_downloading_active': False, 'is_downloading_completed': False, 'download_offset': 0,
                      'downloaded_prefix_size': 0, 'downloaded_size': 0},
            'remote': {'@type': 'remoteFile', 'id': 'remote', 'unique_id': 'ladder', 'is_uploading_active': False,
                       'is_uploading_completed': True, 'uploaded_size': 4}}
    content = ({'@type': 'messageText', 'text': caption} if kind == 'text' else
               {'@type': 'messageDocument', 'caption': caption, 'document': {
                   '@type': 'document', 'file_name': 'a.txt', 'mime_type': 'text/plain', 'document': file}}
               if kind == 'document' else
               {'@type': 'messageVoiceNote', 'caption': caption, 'is_listened': False, 'voice_note': {
                   '@type': 'voiceNote', 'duration': 2, 'waveform': 'AAAA', 'mime_type': 'audio/ogg', 'voice': file}})
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE, 'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
            'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0, 'date': 1700000000,
            'edit_date': 0, 'self_destruct_in': 0, 'auto_delete_in': 0, 'content': content}


class LadderTableTests(unittest.TestCase):
    def test_one_table_row_per_family_in_rank_order(self):
        # Break: a family missing, renamed, re-ranked, re-versioned or moved to another capability.
        rows = tuple((f.name, f.kwarg, f.capability, f.rank, f.text_version, f.media_version, dict(f.entities),
                      f.exclusive, f.inert) for f in ladder().LADDER)
        self.assertEqual(rows, TABLE)
        self.assertEqual({f.name: f.formatting for f in ladder().LADDER if f.formatting is not None}, LEVELS)
        self.assertEqual(tuple(f.name for f in ladder().LISTING_ORDER), LISTING)
        self.assertEqual(ladder().MEDIA_CAPABILITY, 'reply_media_targets')
        self.assertEqual((ladder().PLAIN_TEXT_VERSION, ladder().PLAIN_MEDIA_VERSION), (1, 2))

    def test_versions_keep_parity_and_every_capability_is_a_known_default_off_conjunct(self):
        # Break: parity no longer decides is_media, a version reused, or an unknown capability required.
        versions = [1, 2] + [v for f in ladder().LADDER for v in (f.text_version, f.media_version) if v is not None]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertTrue(all(f.text_version is None or f.text_version % 2 == 1 for f in ladder().LADDER))
        self.assertTrue(all(f.media_version is None or f.media_version % 2 == 0 for f in ladder().LADDER))
        self.assertEqual(ladder().TEXT_VERSIONS, frozenset(range(1, 24, 2)))
        self.assertEqual(ladder().MEDIA_VERSIONS, frozenset(range(2, 23, 2)))
        for f in ladder().LADDER:
            self.assertIn(f.capability, KNOWN_CAPABILITIES)
        self.assertIn(ladder().MEDIA_CAPABILITY, KNOWN_CAPABILITIES)

    def test_entity_maps_are_disjoint_and_the_legacy_names_are_the_ladder_maps(self):
        # Break: two families claiming one TDLib type, or a module keeping its own copy of a family map.
        seen = [t for f in ladder().LADDER for t in f.entities]
        self.assertEqual(len(seen), len(set(seen)))
        by_name = {f.name: f for f in ladder().LADDER}
        for module_name, family in (('STYLES', 'styled'), ('LEXICAL', 'lexical'), ('IDENTITY', 'identity'),
                                    ('DATETIME', 'datetime'), ('URL', 'url'), ('EMAIL', 'email'), ('PHONE', 'phone'),
                                    ('BANK_CARD', 'bank_card'), ('TEXT_URL', 'text_url')):
            self.assertIs(getattr(reply_formatted_sources, module_name), by_name[family].entities)
        with self.assertRaises(TypeError):
            by_name['url'].entities['textEntityTypeCustomEmoji'] = 'custom_emoji'
        with self.assertRaises(AttributeError):
            by_name['url'].rank = 99

    def test_public_keyword_shims_and_forwarded_private_names_stay(self):
        # Break: callers lose a keyword, gain an open **kwargs, or forward sources lose their imports.
        def keywords(function):
            return tuple(name for name, p in inspect.signature(function).parameters.items()
                         if p.kind is inspect.Parameter.KEYWORD_ONLY)
        for function in (reply_formatted_sources.formatted_text, reply_formatted_sources.validate_content,
                         reply_formatted_sources.render_formatted_text, reply_formatted_sources.render_content):
            self.assertEqual(keywords(function), FORMATTED_KEYWORDS)
        self.assertEqual(keywords(reply_media_sources.media_content), MEDIA_KEYWORDS)
        for function in (reply_media_sources.validate_content, reply_media_sources.render_content):
            self.assertEqual(keywords(function), ('styled',) + MEDIA_KEYWORDS)
        with self.assertRaises(TypeError):
            reply_media_sources.media_content({}, text_url=True)
        self.assertTrue(callable(reply_formatted_sources._boundaries))
        self.assertTrue(callable(reply_formatted_sources._inert_json_string))

    def test_media_display_fails_closed_on_an_inert_family_until_it_renders_inertly(self):
        # Break: giving TextUrl a media version silently sanitizes hidden URLs through the media display.
        content = {'kind': 'document', 'media': {'file_name': 'a.txt', 'mime_type': 'text/plain', 'size': 4,
                                                 'unique_id_sha256': '0' * 64},
                   'caption': {'@type': 'formattedText', 'text': 'x', 'entities': [entity('TextUrl', 0, url='a  b')]}}
        with self.assertRaises(ValueError):
            reply_media_sources._render(content, '[untrusted Telegram evidence] ', ladder().kind_admission(media=False))
        content['caption']['entities'] = [entity('Url', 0)]
        display, changed = reply_media_sources._render(content, 'M ', ladder().kind_admission(media=False))
        self.assertTrue(display.startswith('M Media target: '))
        self.assertFalse(changed)


class LadderRuleTests(unittest.TestCase):
    def family(self, name):
        return {f.name: f for f in ladder().LADDER}[name]

    def test_single_families_select_their_version_and_capabilities(self):
        # Break: a family selects another version, or its capability moves out of order.
        expected = {None: ((1, ()), (2, ('reply_media_targets',)))}
        for name, _, capability, _, text, media, *_ in TABLE:
            caps = tuple(dict.fromkeys(('reply_formatted_targets', capability)))
            expected[name] = ((text, caps), None if media is None else (media, ('reply_media_targets',) + caps))
        for name, (text, media) in expected.items():
            families = () if name is None else (self.family(name),)
            if name in LEVELS:
                families += (self.family('datetime'),)
            self.assertEqual((ladder().version_for(families, media=False),
                              ladder().capabilities_for(families, media=False)), text)
            if media is None:
                with self.assertRaises(ValueError):
                    ladder().version_for(families, media=True)
            else:
                self.assertEqual((ladder().version_for(families, media=True),
                                  ladder().capabilities_for(families, media=True)), media)

    def test_the_top_family_follows_formatted_then_the_fixed_listing_order(self):
        # Break: the identity/lexical swap of v7/v8 lost, or a non-top family listed by rank.
        identity, lexical = self.family('identity'), self.family('lexical')
        self.assertEqual(ladder().capabilities_for((lexical, identity), media=False),
                         ('reply_formatted_targets', 'reply_identity_targets', 'reply_lexical_targets'))
        everything = tuple(ladder().LADDER)
        self.assertEqual(ladder().capabilities_for(everything, media=True), (
            'reply_media_targets', 'reply_formatted_targets', 'reply_text_url_targets', 'reply_bank_card_targets',
            'reply_phone_targets', 'reply_email_targets', 'reply_url_targets', 'reply_datetime_targets',
            'reply_lexical_targets', 'reply_identity_targets'))

    def test_a_stored_version_admits_exactly_the_families_at_or_below_its_top(self):
        # Break: a stored version renders or validates with a family above its top, or loses one below it.
        for version in range(1, 24):
            media = version % 2 == 0
            top = (version - 1) // 2
            want = {name for name, _, _, rank, text, media_version, *_ in TABLE
                    if (rank <= top and (media_version if media else text) is not None) or name == 'styled'}
            self.assertEqual({f.name for f in ladder().admission_for(version).families}, want, version)
        for version in (0, 24, 25, 26, 27, 28, -1, True, '3', 3.0, None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ladder().admission_for(version)

    def test_keyword_admission_is_styles_plus_each_flag(self):
        # Break: a shim keyword admits another family, or styles stop being the base of every admission.
        self.assertEqual({f.name for f in ladder().Admission.of().families}, {'styled'})
        for keyword in FORMATTED_KEYWORDS:
            self.assertEqual({f.name for f in ladder().Admission.of(**{keyword: True}).families}, {'styled', keyword})
        with self.assertRaises(TypeError):
            ladder().Admission.of(custom_emoji=True)

    def test_every_family_pair_parses_to_the_ladder_version_and_capabilities(self):
        # Break: the parser, validator and capability tuple disagree with the table for some conjunction.
        self.assertEqual(set(SPANS), {f.name for f in ladder().LADDER})
        names = [f.name for f in ladder().LADDER]
        for kind in ('text', 'document', 'voice_note'):
            for chosen in itertools.chain([()], ((n,) for n in names), itertools.combinations(names, 2)):
                entities = [e for name in chosen for e in SPANS[name]()]
                families = ladder().present(entities)
                media = kind != 'text'
                with self.subTest(kind=kind, families=chosen):
                    if media and 'text_url' in chosen:
                        with self.assertRaises(ValueError):
                            source_from_message(raw(kind, entities), CHAT, MESSAGE)
                        continue
                    source = source_from_message(raw(kind, entities), CHAT, MESSAGE)
                    self.assertEqual(json.loads(source.projection_json)['version'],
                                     ladder().version_for(families, media=media))
                    self.assertEqual(source.required_capabilities, ladder().capabilities_for(families, media=media))
                    self.assertIs(source.is_media, media)


if __name__ == '__main__':
    unittest.main()
