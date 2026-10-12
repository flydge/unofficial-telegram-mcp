"""Exact ordinary-reply version1 source, preview and digest regression."""
import unittest
from telegram_search_mcp.draft_models import DraftPreview
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, canonical, preview_facts, source_from_message

RAW_SOURCE = {'@type': 'message', 'auto_delete_in': 0.0, 'chat_id': 123, 'content': {'@type': 'messageText', 'text': {'@type': 'formattedText', 'entities': [], 'text': 'Source full text'}}, 'date': 1700000000, 'edit_date': 0, 'ephemeral_message_id': 0, 'id': 55, 'is_from_offline': False, 'is_outgoing': False, 'self_destruct_in': 0.0, 'sender_id': {'@type': 'messageSenderUser', 'user_id': 8}}

DRAFT_INPUT = {'account_id': 7, 'draft_id': 'draft_11111111111111111111111111111111', 'expires_at': '2030-01-01T00:00:00Z', 'kind': 'text', 'recipient': 123, 'recipient_title': 'Recipient', 'sha256': '09c876bab6e80384df62e57c527bae3bac339515aa967086eb18866c40f54477', 'size_bytes': 11, 'text': 'Final reply'}

CANONICAL_SOURCE = '{"anchor":{"chat_id":123,"message_id":55},"message":{"@type":"message","auto_delete_in":0.0,"chat_id":123,"content":{"@type":"messageText","link_preview":null,"link_preview_options":null,"text":{"@type":"formattedText","entities":[],"text":"Source full text"}},"date":1700000000,"edit_date":0,"ephemeral_content":null,"ephemeral_message_id":0,"forward_info":null,"id":55,"import_info":null,"is_from_offline":false,"is_outgoing":false,"receiver_id":null,"reply_markup":null,"reply_to":null,"scheduling_state":null,"self_destruct_in":0.0,"self_destruct_type":null,"sender_id":{"@type":"messageSenderUser","user_id":8},"sending_state":null,"topic_id":null},"version":1}'

CANONICAL_FACTS = '{"clear_draft":false,"draft":{"account_id":7,"approval_required":true,"artifact_id":null,"caption":null,"converted":false,"display_name":null,"draft_id":"draft_11111111111111111111111111111111","duration_seconds":null,"expires_at":"2030-01-01T00:00:00Z","kind":"text","mime_type":null,"recipient":123,"recipient_title":"Recipient","sha256":"09c876bab6e80384df62e57c527bae3bac339515aa967086eb18866c40f54477","size_bytes":11,"source_display_name":null,"source_sha256":null,"text":"Final reply","waveform_base64":null},"link_preview_options":{"@type":"linkPreviewOptions","force_large_media":false,"force_small_media":false,"is_disabled":true,"show_above_text":false,"url":""},"outgoing_entities":[],"reply_markup":null,"reply_target":{"anchor":{"chat_id":123,"message_id":55},"sanitized":false,"source_sha256":"bf82651503b6cbdc5561fb5a7549bddbb6b623566c0275af78bd29660c0feafe","text":"[untrusted Telegram evidence] Source full text","truncated":false},"reply_to":{"@type":"inputMessageReplyToMessage","checklist_task_id":0,"message_id":55,"poll_option_id":"","quote":null},"send_options":{"@type":"messageSendOptions","allow_paid_broadcast":false,"disable_notification":false,"effect_id":0,"from_background":false,"only_preview":false,"paid_message_star_count":0,"protect_content":false,"scheduling_state":null,"suggested_post_info":null,"update_order_of_installed_sticker_sets":false},"topic_id":null,"version":1}'

CANONICAL_WIRE = '{"draft":{"account_id":7,"approval_required":true,"artifact_id":null,"caption":null,"converted":false,"display_name":null,"draft_id":"draft_11111111111111111111111111111111","duration_seconds":null,"expires_at":"2030-01-01T00:00:00Z","kind":"text","mime_type":null,"recipient":123,"recipient_title":"Recipient","sha256":"09c876bab6e80384df62e57c527bae3bac339515aa967086eb18866c40f54477","size_bytes":11,"source_display_name":null,"source_sha256":null,"text":"Final reply","waveform_base64":null},"preview_sha256":"73f0e6db7f53b422091e2e96207f31f4218fadd63417ca3a15c06fad527edfaa","reply_target":{"anchor":{"chat_id":123,"message_id":55},"sanitized":false,"source_sha256":"bf82651503b6cbdc5561fb5a7549bddbb6b623566c0275af78bd29660c0feafe","text":"[untrusted Telegram evidence] Source full text","truncated":false}}'

PREVIEW_SHA256 = '73f0e6db7f53b422091e2e96207f31f4218fadd63417ca3a15c06fad527edfaa'

class OrdinaryReplyGoldenTests(unittest.TestCase):
    def test_ordinary_reply_keeps_exact_v1_source_facts_wire_and_digest(self):
        source = source_from_message(RAW_SOURCE, 123, 55)
        draft = DraftPreview.model_validate(DRAFT_INPUT)
        preview = ReplyDraftPreview.create(draft, source)
        self.assertEqual(source.projection_json, CANONICAL_SOURCE)
        self.assertEqual(canonical(preview_facts(preview.draft, preview.reply_target)), CANONICAL_FACTS)
        self.assertEqual(canonical(preview.model_dump(mode='json')), CANONICAL_WIRE)
        self.assertEqual(preview.preview_sha256, PREVIEW_SHA256)
        self.assertNotIn('topic', preview.model_dump(mode='json')['draft'])
        self.assertNotIn('forum_topic_id', preview.model_dump(mode='json')['reply_target'])
