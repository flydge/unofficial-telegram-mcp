"""Closed synthetic v12 Relative caption boundary tests.

Only literal provider fixture builders are reused. Expected projection, evidence,
version and authority are specified independently of production helpers. No
TDLib initialization, installed runtime, credentials or Telegram access.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.outgoing_drafts import DraftError, OutgoingDraftRegistry
from telegram_search_mcp.reply_artifact_drafts import ReplyArtifactDraftPreview
from telegram_search_mcp.reply_drafts import ReplyDraftPreview, ReplySource, source_from_message
from telegram_search_mcp.tdjson import MessageSendNotAttempted
from test_reply_datetime_media import (
    ACCOUNT, BASE_CAPS, CHAT, MARKER, MESSAGE, OWNER, RecordingProvider,
    caption, draft_preview, entity, expected_projection, message, shell,
    stable_media, wire_json,
)

RELATIVE = {'@type': 'dateTimeFormattingTypeRelative'}
KINDS = ('document', 'photo', 'voice_note')
OFFSET_BASIS = 'original source UTF-16 code units'
PROJECTION_SHA = {
    'document': '96fe69b0f965b95b93f5e648bb0921d263d699ec4fadb5bfd521de9dffe9c44c',
    'photo': '4a3cf4f764824ac0ea4bc2f7adf9185855eb8b3175a4c5acd3d492d7e1c05bdb',
    'voice_note': '373f7fd15c6364dfddc89b219f8e627d48122e98e9df65e6c877b4a0ef41312a',
}


def relative(offset=2, length=5, timestamp=-1):
    return entity(offset, length, unix_time=timestamp, formatting_type=copy.deepcopy(RELATIVE))


def provider(kind='document', formatted=None):
    return message(kind, caption(entities=[relative()]) if formatted is None else formatted)


def projection(kind='document', formatted=None, version=12):
    return expected_projection(kind, caption(entities=[relative()]) if formatted is None else formatted, version)


def evidence(kind='document', text='😀today', records=None):
    if records is None:
        records = [{'type': 'date_time', 'offset': 2, 'length': 5, 'text': 'today',
                    'provider_type': 'textEntityTypeDateTime', 'unix_time': -1,
                    'formatting_type': {'@type': 'dateTimeFormattingTypeRelative'}}]
    return MARKER + 'Media target: ' + wire_json({
        'kind': kind, 'caption': {'text': text, 'offset_basis': OFFSET_BASIS, 'entities': records},
        'media': stable_media(kind)})


class RelativeCaptionTests(unittest.TestCase):
    def admit(self, raw):
        try:
            return source_from_message(raw, CHAT, MESSAGE)
        except ValueError as error:
            if str(error) != 'invalid formatted reply date time':
                raise
            self.fail('exact fieldless Relative caption must be admitted as v12')

    def assert_source(self, source, want):
        serialized = wire_json(want)
        self.assertEqual(source.projection_json, serialized)
        self.assertEqual(source.source_sha256, hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(source.anchor, (CHAT, MESSAGE))
        self.assertTrue(source.is_media)
        stored = ReplySource(serialized)
        self.assertEqual(stored.projection_json, serialized)
        self.assertEqual(stored.target(), source.target())
        self.assertEqual(stored.required_capabilities, source.required_capabilities)

    def test_all_media_kinds_bind_original_relative_constructor_and_private_v12(self):
        # Break: admitting via v10/v11, losing constructor, or dropping media authority.
        for kind in KINDS:
            with self.subTest(kind=kind):
                raw = provider(kind)
                source = self.admit(raw)
                self.assert_source(source, projection(kind))
                self.assertEqual(source.source_sha256, PROJECTION_SHA[kind])
                self.assertEqual(source.target().text, evidence(kind))
                self.assertEqual(source.target().source_sha256, source.source_sha256)
                self.assertEqual(source.required_capabilities, BASE_CAPS)
                self.assertFalse(source.target().sanitized)
                self.assertFalse(source.target().truncated)
                with self.assertRaises(ValueError):
                    _ = source.required_capability
                raw['content']['caption']['entities'][0]['type']['formatting_type']['extra'] = True
                self.assertEqual(source.projection_json, wire_json(projection(kind)))

    def test_quotes_and_emoji_keep_full_escaped_evidence_with_original_utf16_coordinates(self):
        # Break: unescaped quotes or scalar indexing change what the owner approves.
        fmt = caption('😀"soon"', [relative(2, 6, 1712345678)])
        source = self.admit(provider(formatted=fmt))
        self.assert_source(source, projection(formatted=fmt))
        self.assertEqual(source.target().text, evidence(text='😀"soon"', records=[{
            'type': 'date_time', 'offset': 2, 'length': 6, 'text': '"soon"',
            'provider_type': 'textEntityTypeDateTime', 'unix_time': 1712345678,
            'formatting_type': {'@type': 'dateTimeFormattingTypeRelative'}}]))
        self.assertIn('\\"soon\\"', source.target().text)
        self.assertFalse(source.target().truncated)

    def test_mixed_null_relative_style_lexical_identity_keep_exact_conditional_authority(self):
        # Break: v12 precedence erases null evidence or conditional adjacent authority.
        omitted = entity(0, 1, unix_time=1)
        null = entity(1, 1, unix_time=2, formatting_type=None)
        rel = relative(2, 1, 3)
        bold = entity(3, 1, 'textEntityTypeBold')
        lexical = entity(4, 4, 'textEntityTypeHashtag')
        identity = entity(8, 5, 'textEntityTypeMentionName', user_id=53)
        dates = [entity(0, 1, unix_time=1, formatting_type=None), null, rel, bold]
        cases = (([], [], ()), ([lexical], [lexical], ('reply_lexical_targets',)),
                 ([identity], [identity], ('reply_identity_targets',)),
                 ([identity, lexical], [lexical, identity], ('reply_lexical_targets', 'reply_identity_targets')))
        for kind in KINDS:
            for extras, canonical, caps in cases:
                with self.subTest(kind=kind, caps=caps):
                    source = self.admit(provider(kind, caption('ABCR#tag@name', [*extras, bold, rel, null, omitted])))
                    self.assert_source(source, projection(kind, caption('ABCR#tag@name', dates + canonical)))
                    self.assertEqual(source.required_capabilities, BASE_CAPS + caps)
                    records = [
                        {'type': 'date_time', 'offset': 0, 'length': 1, 'text': 'A',
                         'provider_type': 'textEntityTypeDateTime', 'unix_time': 1, 'formatting_type': None},
                        {'type': 'date_time', 'offset': 1, 'length': 1, 'text': 'B',
                         'provider_type': 'textEntityTypeDateTime', 'unix_time': 2, 'formatting_type': None},
                        {'type': 'date_time', 'offset': 2, 'length': 1, 'text': 'C',
                         'provider_type': 'textEntityTypeDateTime', 'unix_time': 3, 'formatting_type': RELATIVE},
                        {'type': 'bold', 'offset': 3, 'length': 1, 'text': 'R'}]
                    if 'reply_lexical_targets' in caps:
                        records.append({'type': 'hashtag', 'offset': 4, 'length': 4, 'text': '#tag'})
                    if 'reply_identity_targets' in caps:
                        records.append({'type': 'mention_name', 'offset': 8, 'length': 5, 'text': '@name', 'user_id': 53})
                    self.assertEqual(source.target().text, evidence(kind, 'ABCR#tag@name', records))
        self.assertNotIn('formatting_type', omitted['type'])

    def test_signed_int32_edges_and_sanitation_preserve_original_utf16_spans(self):
        # Break: normalizing before slicing or narrowing signed provider timestamps.
        fmt = caption('😀Ａ\t🕒B', [relative(4, 3, 2147483647), relative(0, 3, -2147483648)])
        source = self.admit(provider('photo', fmt))
        self.assert_source(source, projection('photo', caption('😀Ａ\t🕒B',
            [relative(0, 3, -2147483648), relative(4, 3, 2147483647)])))
        self.assertEqual(source.target().text, evidence('photo', '😀A 🕒B', [
            {'type': 'date_time', 'offset': 0, 'length': 3, 'text': '😀A',
             'provider_type': 'textEntityTypeDateTime', 'unix_time': -2147483648, 'formatting_type': RELATIVE},
            {'type': 'date_time', 'offset': 4, 'length': 3, 'text': '🕒B',
             'provider_type': 'textEntityTypeDateTime', 'unix_time': 2147483647, 'formatting_type': RELATIVE}]))
        self.assertTrue(source.target().sanitized)
        self.assertFalse(source.target().truncated)

    def test_private_volatile_media_facts_are_excluded_but_stable_identity_is_bound(self):
        # Break: leaking paths/file IDs or binding approval to mutable download state.
        for kind in KINDS:
            with self.subTest(kind=kind):
                raw = provider(kind)
                before = self.admit(raw)
                file = (raw['content']['document']['document'] if kind == 'document' else
                        raw['content']['photo']['sizes'][0]['photo'] if kind == 'photo' else
                        raw['content']['voice_note']['voice'])
                file['id'] = 999
                file['remote']['id'] = 'volatile-id'
                file['local'].update(path='/synthetic/secret', downloaded_size=5, is_downloading_active=True)
                file['expected_size'] = 18
                after = self.admit(raw)
                self.assertEqual(before.projection_json, after.projection_json)
                self.assertEqual(before.target(), after.target())
                for private in ('provider-private', 'synthetic-raw-file-id', 'volatile-id', 'secret', '"id":999', '"path"'):
                    self.assertNotIn(private, after.projection_json)
                    self.assertNotIn(private, after.target().text)
                file['remote']['unique_id'] = 'changed stable identity'
                self.assertNotEqual(after.source_sha256, self.admit(raw).source_sha256)

    def test_omitted_null_media_v10_and_relative_text_v11_keep_exact_legacy_bytes(self):
        # Break: choosing v12 for all DateTimes changes prior accepted manifests.
        for kind in KINDS:
            for explicit in (False, True):
                fmt = caption(entities=[entity(unix_time=-1, **({'formatting_type': None} if explicit else {}))])
                source = source_from_message(message(kind, fmt), CHAT, MESSAGE)
                self.assert_source(source, expected_projection(kind))
                self.assertEqual(source.required_capabilities, BASE_CAPS)
        fmt = caption(entities=[relative()])
        content = {'@type': 'messageText', 'text': fmt, 'link_preview': None, 'link_preview_options': None}
        want = {'version': 11, 'anchor': {'chat_id': CHAT, 'message_id': MESSAGE}, 'message': shell(content)}
        source = source_from_message(shell(content), CHAT, MESSAGE)
        self.assertEqual(source.projection_json, wire_json(want))
        self.assertEqual(source.required_capabilities, ('reply_formatted_targets', 'reply_datetime_targets'))
        self.assertFalse(source.is_media)


class RelativeCaptionRejections(unittest.TestCase):
    def reject(self, fmt):
        # Test both untrusted provider and persisted projected facts.
        for kind in KINDS:
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    source_from_message(provider(kind, fmt), CHAT, MESSAGE)
                with self.assertRaises(ValueError):
                    ReplySource(wire_json(projection(kind, fmt)))

    def test_unknown_absolute_open_and_nonobject_constructors_fail_closed(self):
        # Break: matching constructor names loosely or ignoring extra fields.
        for bad in ({'@type': 'dateTimeFormattingTypeRelative', 'extra': None},
                    {'@type': 'dateTimeFormattingTypeRelative', 'format': 'yyyy'},
                    {'@type': 'dateTimeFormattingTypeAbsolute'}, {'@type': 'textEntityTypeDateTimeFormatRelative'},
                    {'@type': 'unknown'}, {'@type': None}, {}, False, 0, '', [RELATIVE]):
            with self.subTest(format=bad):
                self.reject(caption(entities=[entity(unix_time=0, formatting_type=bad)]))

    def test_datetime_timestamps_and_closed_type_fields_require_exact_int32(self):
        # Break: bool/float coercion or widening timestamps at the new boundary.
        for bad in (True, False, None, '-1', -1.0, -2147483649, 2147483648, [], {}):
            with self.subTest(timestamp=bad):
                self.reject(caption(entities=[relative(timestamp=bad)]))
        for change in ('missing', 'extra'):
            bad = relative()
            if change == 'missing': del bad['type']['unix_time']
            else: bad['type']['timezone'] = 'UTC'
            with self.subTest(change=change): self.reject(caption(entities=[bad]))

    def test_invalid_utf16_spans_and_any_datetime_overlap_are_rejected(self):
        # Break: scalar offsets or nesting DateTime inside ordinary styles.
        for offset, length in ((1, 1), (0, 1), (2, 0), (-1, 5), (True, 5), (2, True),
                               (2, 6), (2147483648, 1), (2, 2147483648)):
            with self.subTest(offset=offset, length=length): self.reject(caption(entities=[relative(offset, length)]))
        for other in (relative(), entity(2, 5, 'textEntityTypeBold'), entity(3, 1, 'textEntityTypeItalic'),
                      entity(0, 7, 'textEntityTypeBlockQuote'), entity(3, 4, 'textEntityTypeHashtag'),
                      entity(3, 4, 'textEntityTypeMentionName', user_id=53), entity(3, 4)):
            with self.subTest(other=other): self.reject(caption(entities=[relative(), other]))

    def test_relative_does_not_bypass_caption_shape_adjacent_entities_or_evidence_bounds(self):
        # Break: dropping malformed surplus data or showing truncated evidence for approval.
        for adjacent in (entity(8, 4, 'textEntityTypeMentionName', user_id=True),
                         entity(8, 4, 'textEntityTypeMentionName', user_id=0),
                         entity(8, 4, 'textEntityTypeUrl', extra=None), entity(8, 4, 'textEntityTypeCustomEmoji', custom_emoji_id=1),
                         entity(8, 4, 'textEntityTypeBold', ignored=True)):
            with self.subTest(adjacent=adjacent): self.reject(caption('😀today next', [relative(), adjacent]))
        for change in ('caption_key', 'entity_key', 'wrong_tag', 'too_many', 'oversize', 'nul', 'surrogate'):
            bad = caption(entities=[relative()])
            if change == 'caption_key': bad['ignored'] = True
            elif change == 'entity_key': bad['entities'][0]['ignored'] = True
            elif change == 'wrong_tag': bad['entities'][0]['@type'] = 'notTextEntity'
            elif change == 'too_many': bad = caption('x' * 33, [relative(i, 1) for i in range(33)])
            elif change == 'oversize': bad['text'] += 'x' * 1024
            elif change == 'nul': bad['text'] += '\x00'
            else: bad['text'] += '\ud800'
            with self.subTest(change=change): self.reject(bad)
        self.reject(caption('"' * 1024, [relative(0, 1024)]))

    def test_relabelled_old_versions_and_v12_without_relative_are_refused(self):
        # Break: laundering Relative under old authority or v12 without its defining evidence.
        for kind in KINDS:
            for version in (*range(1, 12), True, 12.0, '12', 13):
                with self.subTest(kind=kind, version=version), self.assertRaises(ValueError):
                    ReplySource(wire_json(projection(kind, version=version)))
            for entities in ([], [entity()], [entity(unix_time=-1, formatting_type=None)],
                             [entity(2, 5, 'textEntityTypeBold')]):
                with self.subTest(kind=kind, entities=entities), self.assertRaises(ValueError):
                    ReplySource(wire_json(projection(kind, caption(entities=entities))))
        fmt = caption(entities=[relative()])
        content = {'@type': 'messageText', 'text': fmt, 'link_preview': None, 'link_preview_options': None}
        text_v12 = {'version': 12, 'anchor': {'chat_id': CHAT, 'message_id': MESSAGE}, 'message': shell(content)}
        with self.assertRaises(ValueError): ReplySource(wire_json(text_v12))

    def test_v12_stored_open_media_and_noncanonical_json_refuse(self):
        # Break: giving trusted-store input weaker rules than provider admission.
        for change in ('top', 'media', 'anchor', 'media_kind', 'raw_content'):
            bad = projection()
            if change == 'top': bad['ignored'] = True
            elif change == 'media': bad['message']['content']['media']['path'] = '/synthetic/private'
            elif change == 'anchor': bad['anchor']['message_id'] += 1
            elif change == 'media_kind': bad['message']['content']['kind'] = 'video'
            else: bad['message']['content'] = provider()['content']
            with self.subTest(change=change), self.assertRaises(ValueError): ReplySource(wire_json(bad))
        with self.assertRaises(ValueError): ReplySource(json.dumps(projection(), ensure_ascii=False))


class RelativeCaptionApprovalTests(unittest.TestCase):
    def source(self, raw=None):
        try:
            return source_from_message(provider() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            if str(error) != 'invalid formatted reply date time': raise
            self.fail('Relative caption admission required before approval checks')

    def send(self, boundary, source, kind, guard=lambda: True):
        options = {'reply_source': source, 'expected_account_id': ACCOUNT,
                   'expected_recipient_title': 'Synthetic chat', 'attempt_id': 'synthetic-relative-media',
                   'pre_send_guard': guard}
        if kind == 'text': return boundary.send_reply_text_message(CHAT, 'Plain', **options)
        return boundary.send_reply_artifact_message(CHAT, Path('/synthetic/reply.txt'), 'Plain', kind='document', **options)

    def test_caption_constructor_timestamp_text_and_stable_media_drift_refuse_before_transport(self):
        # Break: revalidating only visible text instead of immutable caption/media facts.
        for kind in ('text', 'document'):
            for change in ('timestamp', 'null', 'open', 'absolute', 'text', 'stable'):
                with self.subTest(kind=kind, change=change):
                    raw = provider()
                    source = self.source(raw)
                    boundary = RecordingProvider(raw)
                    typ = boundary.raw['content']['caption']['entities'][0]['type']
                    if change == 'timestamp': typ['unix_time'] = 0
                    elif change == 'null': typ['formatting_type'] = None
                    elif change == 'open': typ['formatting_type']['extra'] = True
                    elif change == 'absolute': typ['formatting_type'] = {'@type': 'dateTimeFormattingTypeAbsolute'}
                    elif change == 'text': boundary.raw['content']['caption']['text'] = '😀later'
                    else: boundary.raw['content']['document']['document']['remote']['unique_id'] = 'changed'
                    with self.assertRaises(MessageSendNotAttempted): self.send(boundary, source, kind)
                    self.assertEqual(boundary.transports, [])

    def test_each_present_capability_is_conjunctive_and_plain_transport_has_no_datetime_entities(self):
        # Break: alternative rather than conjunctive authority or source formatting sent out.
        fmt = caption('today #plan @Sam', [relative(0, 5), entity(6, 5, 'textEntityTypeHashtag'),
                                         entity(12, 4, 'textEntityTypeMentionName', user_id=53)])
        raw = provider(formatted=fmt)
        source = self.source(raw)
        caps = BASE_CAPS + ('reply_lexical_targets', 'reply_identity_targets')
        self.assertEqual(source.required_capabilities, caps)
        for kind in ('text', 'document'):
            for missing in caps:
                boundary = RecordingProvider(raw)
                enabled = set(caps) - {missing}
                with self.subTest(kind=kind, missing=missing), self.assertRaises(MessageSendNotAttempted):
                    self.send(boundary, source, kind, lambda: all(cap in enabled for cap in source.required_capabilities))
                self.assertEqual(boundary.transports, [])
            boundary = RecordingProvider(raw)
            self.assertEqual(self.send(boundary, source, kind), 4096)
            self.assertEqual(len(boundary.transports), 1)
            recipient, content, expected, options = boundary.transports[0]
            self.assertEqual(recipient, CHAT)
            self.assertEqual(options['reply_anchor'], (CHAT, MESSAGE))
            self.assertEqual(expected, 'messageText' if kind == 'text' else 'messageDocument')
            self.assertEqual(content['text' if kind == 'text' else 'caption'],
                             {'@type': 'formattedText', 'text': 'Plain', 'entities': []})

    def test_text_and_artifact_revisions_refresh_denial_and_replay_keep_approval_binding(self):
        # Break: carrying approval through revisions/refresh or replaying a finished claim.
        with tempfile.TemporaryDirectory(prefix='relative-media-') as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / 'cache')
            payload = root / 'synthetic.txt'
            payload.write_bytes(b'synthetic')
            artifact = store.store(payload)
            registry = OutgoingDraftRegistry(store)
            source = self.source()
            for kind in ('text', 'document'):
                with self.subTest(kind=kind):
                    if kind == 'text':
                        initial = registry.prepare_text(owner=OWNER, recipient=CHAT, text='First',
                            recipient_title='Synthetic chat', reply_source=source)
                        preview = ReplyDraftPreview.create
                        change = {'text': 'Second'}
                    else:
                        initial = registry.prepare(owner=OWNER, artifact_id=artifact.artifact_id, recipient=CHAT,
                            display_name='reply.txt', mime_type='text/plain', caption='First',
                            recipient_title='Synthetic chat', reply_source=source)
                        preview = ReplyArtifactDraftPreview.create
                        change = {'caption': 'Second'}
                    first = preview(draft_preview(initial), initial.reply_source)
                    revised = registry.revise(initial.draft_id, owner=OWNER, recipient_title='Synthetic chat', reply=True, **change)
                    second = preview(draft_preview(revised), revised.reply_source)
                    self.assertEqual(first.reply_target.text, evidence())
                    self.assertEqual(first.reply_target, second.reply_target)
                    self.assertNotEqual(first.preview_sha256, second.preview_sha256)
                    self.assertEqual(registry.source_required_capabilities(revised.draft_id, owner=OWNER), BASE_CAPS)
                    with self.assertRaises(DraftError): registry.claim(initial.draft_id, owner=OWNER, approved=True)
                    changed = self.source(provider(formatted=caption(entities=[relative(timestamp=0)])))
                    refreshed = registry.revise(revised.draft_id, owner=OWNER, recipient_title='Synthetic chat', reply=True, reply_source=changed)
                    third = preview(draft_preview(refreshed), refreshed.reply_source)
                    self.assertNotEqual(second.reply_target.source_sha256, third.reply_target.source_sha256)
                    self.assertNotEqual(second.preview_sha256, third.preview_sha256)
                    with self.assertRaises(DraftError): registry.claim(revised.draft_id, owner=OWNER, approved=True)
                    with self.assertRaises(DraftError): registry.claim(refreshed.draft_id, owner=OWNER, approved=False)
                    claimed = registry.claim(refreshed.draft_id, owner=OWNER, approved=True)
                    self.assertEqual(claimed.draft.reply_source.source_sha256, changed.source_sha256)
                    registry.finish(refreshed.draft_id, owner=OWNER, status='outcome_unknown')
                    with self.assertRaises(DraftError): registry.claim(refreshed.draft_id, owner=OWNER, approved=True)


if __name__ == '__main__':
    unittest.main()
