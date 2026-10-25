"""E-only outgoing formatting model/digest RED proposal; no broker/native work.

The pure helper has separate evidence. This slice uses independent literal digest
facts and tests closed model admission. Root alone imports/runs this proposal.
"""
import copy
import hashlib
import importlib
import json
import unittest
from datetime import datetime, timezone
from pydantic import TypeAdapter, ValidationError
from telegram_search_mcp.draft_models import DraftPreview, UpdateDraftRequest
from telegram_search_mcp.reply_drafts import (
    PrepareReplyTextSendRequest, UpdateReplyDraftRequest, ReplyTextDraftPreview,
    ReplyDraftPreview, ReplyTarget,
)
from telegram_search_mcp.topic_models import TopicSnapshot, topic_preview_digest

DID = 'draft_' + '1' * 32
TEXT = 'A😀B cafe'
EXPIRY = '2030-01-02T03:04:05Z'
CHAT = -1007
STYLES = ('textEntityTypeBold', 'textEntityTypeItalic', 'textEntityTypeUnderline',
          'textEntityTypeStrikethrough', 'textEntityTypeSpoiler')
SEND = {'@type': 'messageSendOptions', 'suggested_post_info': None,
    'disable_notification': False, 'from_background': False, 'protect_content': False,
    'allow_paid_broadcast': False, 'paid_message_star_count': 0,
    'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
    'effect_id': 0, 'only_preview': False}
LINK = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
    'force_small_media': False, 'force_large_media': False, 'show_above_text': False}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def entity(offset=0, length=1, style=STYLES[0]):
    return {'offset': offset, 'length': length, 'type': {'@type': style}}


def native(rows):
    # Independent expected adapter; never imports the implementation/helper.
    return [dict({'@type': 'textEntity'}, **copy.deepcopy(row)) for row in rows]


def topic():
    value = {'version': 1, 'account_id': 7, 'chat_id': CHAT,
        'chat_type': 'forum_supergroup', 'peer_id': 7, 'forum_topic_id': 41,
        'name': '[untrusted Telegram evidence] Topic',
        'original_name_sha256': hashlib.sha256(b'Topic').hexdigest(),
        'creation_date': 1700000000, 'creator_kind': 'user', 'creator_id': 7,
        'is_outgoing': False, 'is_general': False, 'is_closed': False, 'is_hidden': False}
    return dict(value, source_sha256=digest(value))


def draft(rows=None, with_topic=False, text=TEXT):
    data = text.encode()
    value = {'draft_id': DID, 'account_id': 7, 'recipient': CHAT,
        'recipient_title': 'Chat', 'kind': 'text', 'expires_at': EXPIRY,
        'approval_required': True, 'sha256': hashlib.sha256(data).hexdigest(),
        'size_bytes': len(data), 'text': text, 'artifact_id': None,
        'display_name': None, 'mime_type': None, 'caption': None,
        'duration_seconds': None, 'waveform_base64': None, 'source_sha256': None,
        'source_display_name': None, 'converted': False}
    if with_topic: value['topic'] = topic()
    if rows is not None: value['outgoing_entities'] = copy.deepcopy(rows)
    return value


def nonreply_facts(value, rows):
    return {'version': 2 if rows else 1, 'draft_id': value['draft_id'],
        'account_id': value['account_id'], 'recipient': value['recipient'],
        'recipient_title': value['recipient_title'],
        'expires_at': datetime.fromisoformat(value['expires_at']).astimezone(timezone.utc).isoformat(),
        'kind': 'text', 'approval_required': True, 'topic': value.get('topic'),
        'text': value['text'], 'sha256': value['sha256'], 'size_bytes': value['size_bytes'],
        'entities': native(rows), 'reply_to': None, 'link_preview_options': LINK,
        'send_options': SEND, 'reply_markup': None, 'clear_draft': False}


def target(with_topic=False):
    value = {'anchor': {'chat_id': CHAT, 'message_id': 55},
        'text': '[untrusted Telegram evidence] Source', 'source_sha256': 'a' * 64,
        'sanitized': False, 'truncated': False}
    if with_topic: value['forum_topic_id'] = 41
    return value


def reply_facts(value, selected, rows):
    return {'version': 3 if rows else 2 if 'topic' in value else 1,
        'draft': value, 'reply_target': selected,
        'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41} if 'topic' in value else None,
        'outgoing_entities': native(rows), 'send_options': SEND,
        'link_preview_options': LINK, 'clear_draft': False, 'reply_markup': None,
        'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
            'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}}


def reply(rows=None, with_topic=False):
    value, selected = draft(rows, with_topic), target(with_topic)
    return {'draft': value, 'reply_target': selected,
        'preview_sha256': digest(reply_facts(value, selected, rows or []))}


class OutgoingFormattingModelTests(unittest.TestCase):
    def setUp(self):
        name = 'telegram_search_mcp.outgoing_entity_models'
        try:
            self.models = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name == name:
                self.fail('FEATURE ABSENT: closed outgoing entity models')
            raise
        for symbol in ('OutgoingEntityType', 'OutgoingEntity', 'OutgoingEntityList'):
            self.assertTrue(hasattr(self.models, symbol), 'FEATURE ABSENT: ' + symbol)
        self.adapter = TypeAdapter(self.models.OutgoingEntityList)

    def test_five_strict_fieldless_public_records(self):
        for style in STYLES:
            with self.subTest(style=style):
                typed = self.models.OutgoingEntity.model_validate(entity(style=style))
                self.assertEqual(typed.model_dump(mode='json'), entity(style=style))
                self.assertNotIn('@type', typed.model_dump(mode='json'))

    def test_closed_shapes_types_and_integer_ranges(self):
        bad = [dict(entity(), **{'@type': 'textEntity'}), dict(entity(), extra=1),
               {'offset': 0, 'length': 1, 'type': {'@type': STYLES[0], 'url': 'x'}},
               entity(style='textEntityTypeTextUrl'), entity(style='textEntityTypeCode')]
        for field, values in [('offset', [True, 0.0, '0', -1, 2**31]),
                              ('length', [False, 1.0, '1', 0, -1, 2**31])]:
            for value in values:
                row = entity(); row[field] = value; bad.append(row)
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValidationError):
                self.models.OutgoingEntity.model_validate(row)

    def test_actual_list_cap_and_explicit_null(self):
        for rows in (None, (), {}, '[]', [entity()] * 33):
            with self.subTest(rows=type(rows).__name__), self.assertRaises(ValidationError):
                self.adapter.validate_python(rows)
        self.assertEqual(self.adapter.validate_python([]), [])
        valid = [entity(offset=i*2) for i in range(32)]
        self.assertEqual(len(self.adapter.validate_python(valid)), 32)

    def test_reply_requests_nonempty_omission_empty_and_null(self):
        base = {'recipient': CHAT, 'text': TEXT, 'reply_to': {'chat_id': CHAT, 'message_id': 55}}
        for rows in (None, []):
            value = copy.deepcopy(base)
            if rows is not None: value['outgoing_entities'] = rows
            self.assertEqual(PrepareReplyTextSendRequest.model_validate(value).model_dump(mode='json'), base)
        value = dict(base, outgoing_entities=[entity(1, 2, STYLES[4])])
        self.assertEqual(PrepareReplyTextSendRequest.model_validate(value).model_dump(mode='json'), value)
        with self.assertRaises(ValidationError):
            PrepareReplyTextSendRequest.model_validate(dict(base, outgoing_entities=None))

    def test_updates_replace_or_clear_require_text_and_reject_null(self):
        for model in (UpdateDraftRequest, UpdateReplyDraftRequest):
            for rows in (None, []):
                value = {'draft_id': DID, 'text': TEXT}
                if rows is not None: value['outgoing_entities'] = rows
                typed = model.model_validate(value).model_dump(mode='json')
                self.assertNotIn('outgoing_entities', typed)
            value = {'draft_id': DID, 'text': TEXT, 'outgoing_entities': [entity()]}
            self.assertEqual(model.model_validate(value).model_dump(mode='json')['outgoing_entities'], [entity()])
            for bad in ({'draft_id': DID, 'outgoing_entities': [entity()]},
                        {'draft_id': DID, 'outgoing_entities': []},
                        {'draft_id': DID, 'text': TEXT, 'outgoing_entities': None}):
                with self.subTest(model=model.__name__, bad=bad), self.assertRaises(ValidationError):
                    model.model_validate(bad)
        # 0.58: a caption revision may carry styles too (formatted_caption_send), but only over a caption that is
        # already the text Telegram stores; one that normalization or Telegram's rewrite would change is refused.
        styled = UpdateDraftRequest.model_validate({'draft_id': DID, 'caption': 'x', 'outgoing_entities': [entity()]})
        self.assertEqual(styled.model_dump(mode='json')['outgoing_entities'], [entity()])
        for caption in (' x', 'x\n', 'ｘ', '\tx'):
            with self.subTest(caption=caption), self.assertRaises(ValidationError):
                UpdateDraftRequest.model_validate({'draft_id': DID, 'caption': caption,
                                                   'outgoing_entities': [entity()]})

    def test_request_ranges_are_checked_against_final_normalized_text(self):
        base = {'recipient': CHAT, 'reply_to': {'chat_id': CHAT, 'message_id': 55}}
        # Final NFKC text is ffi\n😀; units0..3 letters,3..4 LF,4..6 astral.
        value = dict(base, text='ﬃ\r\n😀', outgoing_entities=[entity(4, 2)])
        self.assertEqual(PrepareReplyTextSendRequest.model_validate(value).outgoing_entities[0].offset, 4)
        for row in (entity(5, 1), entity(4, 1), entity(0, 7)):
            with self.subTest(row=row), self.assertRaises(ValidationError):
                PrepareReplyTextSendRequest.model_validate(dict(base, text='ﬃ\r\n😀', outgoing_entities=[row]))

    def test_explicit_empty_preview_lists_omit_fields_for_all_four_routes(self):
        for with_topic in (False, True):
            value = draft(with_topic=with_topic)
            if with_topic: value['preview_sha256'] = digest(nonreply_facts(value, []))
            self.assertEqual(DraftPreview.model_validate(dict(value, outgoing_entities=[])).model_dump(mode='json'), value)
            outer = reply(with_topic=with_topic)
            empty = copy.deepcopy(outer); empty['draft']['outgoing_entities'] = []
            self.assertEqual(ReplyDraftPreview.model_validate(empty).model_dump(mode='json'), outer)
            with self.assertRaises(ValidationError):
                ReplyTextDraftPreview.model_validate(dict(outer['draft'], outgoing_entities=None))

    def test_plain_formatted_preview_has_sole_version2_digest(self):
        rows = [entity(1, 2, STYLES[4])]; value = draft(rows)
        expected = digest(nonreply_facts(value, rows)); value['preview_sha256'] = expected
        typed = DraftPreview.model_validate(value)
        self.assertEqual(typed.model_dump(mode='json'), value)
        self.assertEqual(typed.preview_sha256, expected)
        self.assertNotIn('topic', typed.model_dump(mode='json'))
        self.assertEqual(topic_preview_digest(draft_id=DID, account_id=7, recipient=CHAT,
            recipient_title='Chat', expires_at=EXPIRY, text=TEXT, sha256=value['sha256'],
            size_bytes=value['size_bytes'], outgoing_entities=rows), expected)

    def test_topic_formatted_preview_version2_binds_real_snapshot(self):
        rows = [entity()]; value = draft(rows, True)
        expected = digest(nonreply_facts(value, rows)); value['preview_sha256'] = expected
        self.assertEqual(DraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertEqual(topic_preview_digest(draft_id=DID, account_id=7, recipient=CHAT,
            recipient_title='Chat', expires_at=EXPIRY, text=TEXT, sha256=value['sha256'],
            size_bytes=value['size_bytes'], topic=TopicSnapshot.model_validate(topic()),
            outgoing_entities=rows), expected)

    def test_preview_final_text_is_not_silently_normalized(self):
        for text in ('ﬃ', 'A\r\nB', 'Cafe\u0301'):
            rows = [entity()]; value = draft(rows, text=text)
            value['preview_sha256'] = digest(nonreply_facts(value, rows))
            with self.subTest(text=text), self.assertRaises(ValidationError):
                DraftPreview.model_validate(value)
            with self.assertRaises(ValidationError):
                ReplyTextDraftPreview.model_validate(draft(rows, text=text))

    def test_canonical_order_native_digest_and_mutation_isolation(self):
        canonical_rows = [entity(0, 4, STYLES[0]), entity(0, 1, STYLES[1])]
        supplied = list(reversed(copy.deepcopy(canonical_rows)))
        value = draft(supplied); value['preview_sha256'] = digest(nonreply_facts(value, canonical_rows))
        typed = DraftPreview.model_validate(value)
        expected = draft(canonical_rows); expected['preview_sha256'] = value['preview_sha256']
        self.assertEqual(typed.model_dump(mode='json'), expected)
        supplied[0]['type']['@type'] = STYLES[4]; value['outgoing_entities'][0]['offset'] = 3
        projected = typed.model_dump(mode='json'); projected['outgoing_entities'][0]['type']['@type'] = STYLES[4]
        self.assertEqual(typed.model_dump(mode='json'), expected)

    def test_rehashed_invalid_spans_types_null_and_extra_still_refuse(self):
        for rows in ([entity(2, 1)], [entity(0, 99)], [entity(style='textEntityTypeTextUrl')],
                     [dict(entity(), extra=True)], [entity(), entity()],
                     [entity(0, 4), entity(3, 2, STYLES[1])]):
            value = draft(rows); value['preview_sha256'] = digest(nonreply_facts(value, rows))
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                DraftPreview.model_validate(value)
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate(dict(draft(), outgoing_entities=None))

    def test_changed_styles_change_digest_and_old_digest_refuses(self):
        first, second = [entity()], [entity(style=STYLES[4])]
        value = draft(first); value['preview_sha256'] = digest(nonreply_facts(value, first))
        self.assertEqual(DraftPreview.model_validate(value).preview_sha256, value['preview_sha256'])
        changed = draft(second); changed['preview_sha256'] = value['preview_sha256']
        with self.assertRaises(ValidationError): DraftPreview.model_validate(changed)
        changed['preview_sha256'] = digest(nonreply_facts(changed, second))
        self.assertNotEqual(changed['preview_sha256'], value['preview_sha256'])
        self.assertEqual(DraftPreview.model_validate(changed).preview_sha256, changed['preview_sha256'])

    def test_artifact_preview_refuses_unsolicited_nonempty_styles(self):
        value = draft(); value.update(kind='document', text=None, display_name='x.txt',
            mime_type='text/plain', caption='', artifact_id='artifact_' + '2'*32 + '_' + value['sha256'] + '_' + str(value['size_bytes']))
        self.assertEqual(DraftPreview.model_validate(value).model_dump(mode='json'), value)
        for rows in ([entity()], None):
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                DraftPreview.model_validate(dict(value, outgoing_entities=rows))
        with self.assertRaises(ValidationError):
            DraftPreview.model_validate(dict(value, outgoing_entities=[entity()], preview_sha256='b'*64))

    def test_plain_reply_uses_one_outer_version3_with_native_entities(self):
        rows = [entity(1, 2, STYLES[4])]; value = reply(rows)
        self.assertEqual(ReplyDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertNotIn('preview_sha256', value['draft'])
        self.assertEqual(value['preview_sha256'], digest(reply_facts(value['draft'], value['reply_target'], rows)))
        old = copy.deepcopy(value); old['preview_sha256'] = digest(dict(reply_facts(old['draft'], old['reply_target'], rows), version=1))
        with self.assertRaises(ValidationError): ReplyDraftPreview.model_validate(old)

    def test_topic_reply_version3_binds_anchor_topic_and_styles(self):
        rows = [entity()]; value = reply(rows, True)
        self.assertEqual(ReplyDraftPreview.model_validate(value).model_dump(mode='json'), value)
        for mutate in ('recipient', 'forum_topic_id', 'approval_required'):
            bad = copy.deepcopy(value)
            if mutate == 'recipient': bad['reply_target']['anchor']['chat_id'] = -999
            elif mutate == 'forum_topic_id': bad['reply_target']['forum_topic_id'] = 42
            else: bad['draft']['approval_required'] = False
            bad['preview_sha256'] = digest(reply_facts(bad['draft'], bad['reply_target'], rows))
            with self.subTest(mutate=mutate), self.assertRaises(ValidationError):
                ReplyDraftPreview.model_validate(bad)

    def test_reply_no_inner_digest_and_invalid_rehash_refuses(self):
        rows = [entity()]; value = reply(rows)
        bad = copy.deepcopy(value); bad['draft']['preview_sha256'] = 'f'*64
        bad['preview_sha256'] = digest(reply_facts(bad['draft'], bad['reply_target'], rows))
        with self.assertRaises(ValidationError): ReplyDraftPreview.model_validate(bad)
        for row in (entity(2, 1), entity(style='textEntityTypeCode')):
            bad = reply([row])
            with self.subTest(row=row), self.assertRaises(ValidationError):
                ReplyDraftPreview.model_validate(bad)


class LegacyFormattingEmptyGoldens(unittest.TestCase):
    def test_empty_ordinary_wire_exact(self):
        value = draft()
        self.assertEqual(DraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertNotIn('preview_sha256', DraftPreview.model_validate(value).model_dump(mode='json'))

    def test_empty_topic_version1_wire_and_digest_exact(self):
        value = draft(with_topic=True)
        value['preview_sha256'] = digest(nonreply_facts(value, []))
        self.assertEqual(DraftPreview.model_validate(value).model_dump(mode='json'), value)

    def test_empty_reply_version1_wire_and_digest_exact(self):
        value = reply()
        self.assertEqual(ReplyDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertNotIn('outgoing_entities', value['draft'])

    def test_empty_combined_topic_reply_version2_exact(self):
        value = reply(with_topic=True)
        self.assertEqual(ReplyDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertNotIn('outgoing_entities', value['draft'])


if __name__ == '__main__':
    unittest.main()
