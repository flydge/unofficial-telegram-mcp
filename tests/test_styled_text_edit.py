"""0.57 work item 1 of part 2: a styled own text edit (prepare_text_edit's optional outgoing_entities).

Synthetic raw transport only (test_archive_edit's, answering a styled editMessageText with the request's own
entities): projections, drafts, broker, proxy, server and TDLib reducers are production code. No live TDLib and no
owner dialog: the prompt is a fake, and the dialog goldens patch osascript.
"""
from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client

import test_archive_edit as archive
from approval_layout import tail as dialog_tail
from schema_changes import DIRECTORY as SCHEMA_CHANGES
from test_archive_edit import (ACCOUNT, CHAT, CLIENT, DID, ERRORS, EXPIRY, MARK, MID, PROPERTIES, chat, entity,
                               escape, own_message)
from telegram_search_mcp.approval_prompt import confirm_approved_send
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError
from telegram_search_mcp.draft_kinds import DRAFT_KINDS
from telegram_search_mcp.edit_drafts import (ApplyPreparedTextEditRequest, EditChat, EditSnapshot,
    PrepareTextEditRequest, TextEditDraftPreview, TextEditSpec, edit_request, edited_text_result)
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.server import build_server
from telegram_search_mcp.tdjson import ForbiddenTDLibRequest, TDLibClient, _validate_bounded_request

EDIT = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text'))
STYLED = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_text', 'edit_own_styled_text'))
STYLED_ONLY = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_styled_text'))
CAPTION_STYLED = RuntimePolicy(enabled_capabilities=('send', 'read', 'edit_own_caption', 'edit_own_styled_text'))
DISABLED = 'a styled text edit needs capabilities the owner has not enabled: missing_capabilities'
STYLED_REWRITTEN = ('styled new text changes under Telegram normalization (NFKC, line breaks, rewritten characters); '
                    'send its stored form')
WHITESPACE = 'new text must not start or end with whitespace; Telegram trims it'
REFUSED = 'message is not eligible for an own text edit; no draft was created'
UNAVAILABLE = 'text edit draft is unavailable'
BOLD = {'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}}
ITALIC = {'offset': 6, 'length': 5, 'type': {'@type': 'textEntityTypeItalic'}}


def native(row):
    return {'@type': 'textEntity', **row}


class Raw(archive.Raw):
    """test_archive_edit's transport; a styled editMessageText returns the request's own entities, shaped by mode."""

    def __init__(self):
        super().__init__()
        self.styled_mode = 'ok'

    def send(self, request):
        if request['@type'] != 'editMessageText' or not request['input_message_content']['text']['entities']:
            return super().send(request)
        self.sent.append(copy.deepcopy(request))
        mode = self.styled_mode
        text = copy.deepcopy(request['input_message_content']['text'])
        if mode in ERRORS:
            error = {'@type': 'error', 'message': 'MESSAGE_NOT_MODIFIED PRIVATE', '@extra': request['@extra']}
            self.events.append(error if ERRORS[mode] is None else {**error, 'code': ERRORS[mode]})
            return
        if mode == 'auto_detected':  # Telegram detects the link in "see example.com" again.
            text['entities'].append(entity('textEntityTypeUrl', text['text'].index('example.com'), 11))
        if mode == 'extra_style':
            text['entities'].append(entity('textEntityTypeUnderline', 0, 1))
        if mode == 'missing_style':
            text['entities'] = []
        if mode == 'hidden_entity':
            text['entities'].append(entity('textEntityTypeTextUrl', 0, 1, url='https://hidden.invalid'))
        if mode == 'wrong_text':
            text['text'] += '!'
        value = copy.deepcopy(self.message)
        value['content'] = {'@type': 'messageText', 'text': text}
        value['edit_date'] = 1700000500
        if mode in ('ok', 'auto_detected'):
            self.message = copy.deepcopy(value)
        self.events.append({'@type': 'updateMessageEdited', 'chat_id': CHAT, 'message_id': MID,
                            'edit_date': value['edit_date'], 'reply_markup': None})
        self.events.append({**value, '@extra': request['@extra']})


def snapshot(message=None, title='Team Chat', kind='supergroup'):
    return EditSnapshot.create(chat(CHAT, title, kind), CHAT, message or own_message(), MID, dict(PROPERTIES))


def preview(new='Hello there', styles=((0, 5, 'textEntityTypeBold'),), message=None, kind='supergroup'):
    return TextEditDraftPreview.create(draft_id=DID, account_id=ACCOUNT, expires_at=EXPIRY,
                                       chat=EditChat(chat_id=CHAT, title='Team Chat', kind=kind),
                                       snapshot=snapshot(message, kind=kind), new_text=new, styles=styles)


def shown(value, **kw):
    from approval_layout import logical
    seen = []
    arguments = dict(recipient_title=value.chat.title, recipient=value.chat.chat_id, display_name='Text edit',
                     size_bytes=1, sha256=value.source_sha256, caption='', kind='text_edit',
                     text_edit_preview=value.model_dump(mode='json'))
    arguments.update(kw)
    with patch('telegram_search_mcp.approval_prompt.subprocess.run',
               side_effect=lambda argv, **options: seen.append(argv) or SimpleNamespace(returncode=0, stdout=b'DECLINED')):
        assert confirm_approved_send(**arguments) is False
    return [logical(argv[-1]) for argv in seen]


class Fixture:
    policy = STYLED

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.raw = Raw()
        self.client = TDLibClient(raw=self.raw)
        self.approvals = []
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                             client_factory=lambda: self.client, policy=self.policy,
                             approval_prompt=lambda **kw: self.approvals.append(kw) or True)
        self.addCleanup(self.broker.shutdown)

    def dispatch(self, op, payload, client=CLIENT):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': client,
                                      'deadline': time.monotonic() + 10, 'broker_generation': self.broker._generation})

    def requests(self):
        return [request for request in self.raw.sent if request['@type'] == 'editMessageText']

    def payload(self, new='Hello there', entities=(BOLD,)):
        return {'chat': CHAT, 'message_id': MID, 'new_text': new,
                **({'outgoing_entities': list(entities)} if entities is not None else {})}

    def prepare(self, new='Hello there', entities=(BOLD,), client=CLIENT):
        result = self.dispatch('prepare_text_edit', self.payload(new, entities), client)
        self.assertEqual(result['status'], 'prepared', result)
        return result['text_edit']

    def apply(self, did, approved=True, client=CLIENT):
        return self.dispatch('apply_prepared_text_edit', {'draft_id': did, 'approved': approved}, client)


class ConjunctionTests(Fixture, unittest.TestCase):
    """Criterion 5: a styled text edit needs edit_own_styled_text with edit_own_text, send and read."""
    policy = EDIT

    def test_styles_without_edit_own_styled_text_name_it_before_any_read_and_create_no_draft(self):
        result = self.dispatch('prepare_text_edit', self.payload())
        self.assertEqual(result, {'status': 'capability_disabled', 'text_edit': None, 'refusal': None,
                                  'missing_capabilities': ['edit_own_styled_text'], 'detail': DISABLED,
                                  'next_action': 'enable_capability'})
        self.assertEqual([r['@type'] for r in self.raw.sent if r['@type'] in ('getMessage', 'getChat')], [])
        self.assertEqual(self.broker._drafts.known_ids(), set())
        plain = self.prepare('Hello there', None)  # The plain edit needs no style capability; its dump is 0.52's.
        self.assertNotIn('outgoing_entities', plain)

    def test_edit_own_styled_text_alone_or_with_the_caption_edit_is_not_a_text_edit(self):
        for policy in (STYLED_ONLY, CAPTION_STYLED):
            with self.subTest(policy=policy.enabled_capabilities):
                self.broker._policy = policy
                with self.assertRaises(CompatibilityError):
                    self.dispatch('prepare_text_edit', self.payload())
        self.assertEqual(self.raw.sent, [])

    def test_a_styled_draft_stays_bound_to_both_edit_capabilities(self):
        self.broker._policy = STYLED
        did = self.prepare()['draft_id']
        self.assertEqual(self.dispatch('get_send_status', {'draft_id': did})['evidence'], 'local_pending')
        self.broker._policy = EDIT
        status = self.dispatch('get_send_status', {'draft_id': did})
        self.assertEqual((status['status'], status['evidence']), ('outcome_unknown', 'none'))
        self.assertEqual(self.apply(did)['status'], 'expired')
        self.assertEqual((self.approvals, self.requests()), ([], []))
        self.broker._policy = STYLED
        self.assertEqual(self.apply(did)['status'], 'applied')
        self.broker._policy = EDIT  # The receipt is not replayed without the style capability either.
        self.assertNotEqual(self.apply(did)['status'], 'applied')
        self.assertEqual(len(self.requests()), 1)

    def test_turning_the_style_capability_off_inside_the_provider_lock_is_local_failed(self):
        self.broker._policy = STYLED
        did = self.prepare()['draft_id']
        real = self.raw.send

        def send(request):
            real(request)
            if request['@type'] == 'getMessageProperties' and self.approvals:
                self.broker._policy = EDIT
        self.raw.send = send
        result = self.broker._apply_prepared_text_edit(ApplyPreparedTextEditRequest(draft_id=did, approved=True),
                                                       client_id=CLIENT)
        self.assertEqual((result.status, result.edit_date), ('local_failed', None))
        self.assertEqual(self.requests(), [])


class PrepareTests(Fixture, unittest.TestCase):
    def test_a_styled_preview_binds_the_canonical_styles_and_the_plain_one_has_none(self):
        value = self.prepare('Hello there', (ITALIC, BOLD))
        self.assertEqual(value['outgoing_entities'], [BOLD, ITALIC])
        self.assertEqual(value['new_text'], MARK + escape('Hello there'))
        self.assertEqual(TextEditDraftPreview.model_validate(value).model_dump(mode='json'), value)
        self.assertEqual(self.requests(), [])
        self.setUp()
        plain = self.prepare('Hello there', ())
        self.assertNotIn('outgoing_entities', plain)
        self.assertEqual(TextEditDraftPreview.model_validate(plain).model_dump(mode='json'), plain)

    def test_restyling_the_same_text_is_a_change_and_the_same_styles_are_unchanged(self):
        self.raw.message = own_message('Hello world', [entity('textEntityTypeBold', 0, 5)])
        restyled = self.prepare('Hello world', ({'offset': 6, 'length': 5, 'type': {'@type': 'textEntityTypeSpoiler'}},))
        self.assertEqual(restyled['new_text'], MARK + escape('Hello world'))
        self.setUp()
        self.raw.message = own_message('Hello world', [entity('textEntityTypeBold', 0, 5),
                                                       entity('textEntityTypeUrl', 6, 5)])
        result = self.dispatch('prepare_text_edit', self.payload('Hello world'))
        self.assertEqual((result['status'], result['refusal']), ('refused', 'unchanged_text'))
        self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_styled_input_is_refused_never_rewritten_before_any_read(self):
        for new, detail in (('Hello\r\nthere', STYLED_REWRITTEN), ('Hello\tthere', STYLED_REWRITTEN),
                            ('Hello\u00a0there', STYLED_REWRITTEN), ('Cafe\u0301 there', STYLED_REWRITTEN),
                            (' Hello there', WHITESPACE), ('Hello there\n', WHITESPACE), ('\tHello', WHITESPACE),
                            ('bad\u0007bell', REFUSED), ('Hel', REFUSED)):
            with self.subTest(new=new):
                self.setUp()
                result = self.dispatch('prepare_text_edit', self.payload(new))
                self.assertEqual(result, {'status': 'refused', 'text_edit': None, 'refusal': 'invalid_input',
                                          'detail': detail})
                self.assertEqual([r for r in self.raw.sent if r['@type'] == 'getMessage'], [])
                self.assertEqual(self.broker._drafts.known_ids(), set())
        # The same input without styles keeps the 0.52 normalisation.
        self.setUp()
        self.assertEqual(self.prepare('Hello\r\nthere', ())['new_text'], MARK + escape('Hello\nthere'))

    def test_hidden_current_formatting_stays_refused(self):
        for kind, fields in (('textEntityTypeTextUrl', {'url': 'https://evil.invalid'}),
                             ('textEntityTypeCustomEmoji', {'custom_emoji_id': '1'}),
                             ('textEntityTypeMentionName', {'user_id': 9})):
            with self.subTest(kind=kind):
                self.setUp()
                self.raw.message = own_message('Hello world', [entity(kind, 0, 5, **fields)])
                result = self.dispatch('prepare_text_edit', self.payload())
                self.assertEqual((result['status'], result['refusal']), ('refused', 'hidden_source_content'))
                self.assertEqual(self.broker._drafts.known_ids(), set())

    def test_the_proxy_checks_text_and_styles_against_the_request(self):
        payload = self.payload('Hello there', (BOLD,))
        broker_result = self.dispatch('prepare_text_edit', payload)

        def proxy(result, request=PrepareTextEditRequest(**payload)):
            client = BrokerClient(socket_path=Path('/nonexistent/no.sock'), policy=STYLED)
            client._request = lambda *args: copy.deepcopy(result)
            self.addCleanup(client.close)
            return client.prepare_text_edit(request)
        self.assertEqual(proxy(broker_result).model_dump(mode='json'), broker_result)
        for other in ({'outgoing_entities': [ITALIC]}, {'outgoing_entities': []}, {'new_text': 'Hello where'}):
            with self.subTest(other=other):
                self.assertEqual(proxy(broker_result, PrepareTextEditRequest(**{**payload, **other})).status,
                                 'unavailable')
        dropped = copy.deepcopy(broker_result)
        del dropped['text_edit']['outgoing_entities']
        self.assertEqual(proxy(dropped).status, 'unavailable')


class ApplyTests(Fixture, unittest.TestCase):
    def test_one_exact_styled_edit_message_text_then_receipt_replay(self):
        value = self.prepare('Hello there', (ITALIC, BOLD))
        result = self.apply(value['draft_id'])
        self.assertEqual(result, {'status': 'applied', 'draft_id': value['draft_id'], 'chat_id': CHAT,
                                  'message_id': MID, 'edit_date': 1700000500,
                                  'detail': 'TDLib returned the edited message with the new text and styles'})
        [request] = self.requests()
        self.assertEqual(request, {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                                   'input_message_content': {'@type': 'inputMessageText', 'text': {
                                       '@type': 'formattedText', 'text': 'Hello there',
                                       'entities': [native(BOLD), native(ITALIC)]},
                                       'link_preview_options': None, 'clear_draft': False},
                                   '@extra': request['@extra']})
        [dialog] = self.approvals
        self.assertEqual((dialog['kind'], dialog['text_edit_preview']), ('text_edit', value))
        replay = self.apply(value['draft_id'])
        self.assertEqual((replay['status'], replay['edit_date']), ('applied', 1700000500))
        self.assertEqual((len(self.requests()), len(self.approvals)), (1, 1))

    def test_a_plain_draft_still_sends_no_entities(self):
        did = self.prepare('Hello there', ())['draft_id']
        self.assertEqual(self.apply(did)['detail'], 'TDLib returned the edited message with the new text')
        [request] = self.requests()
        self.assertEqual(request['input_message_content']['text']['entities'], [])

    def test_only_auto_detected_extras_are_tolerated_and_nothing_is_retried(self):
        self.raw.styled_mode = 'auto_detected'
        did = self.prepare('Hello see example.com')['draft_id']
        self.assertEqual(self.apply(did)['status'], 'applied')
        for mode, expected in (('extra_style', 'outcome_unknown'), ('missing_style', 'outcome_unknown'),
                               ('hidden_entity', 'outcome_unknown'), ('wrong_text', 'outcome_unknown'),
                               ('server_error', 'outcome_unknown'), ('codeless', 'outcome_unknown'),
                               ('rejected', 'failed'), ('flood', 'failed')):
            with self.subTest(mode=mode):
                self.setUp()
                self.raw.styled_mode = mode
                did = self.prepare()['draft_id']
                result = self.apply(did)
                self.assertEqual((result['status'], result['edit_date']), (expected, None))
                self.assertEqual(self.apply(did)['status'], expected)
                self.assertEqual(len(self.requests()), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_a_changed_message_or_style_before_the_dialog_makes_no_attempt(self):
        did = self.prepare()['draft_id']
        self.raw.message['content']['text']['entities'] = [entity('textEntityTypeItalic', 0, 5)]
        result = self.apply(did)
        self.assertEqual(result['status'], 'message_changed')
        self.assertEqual((self.approvals, self.requests()), ([], []))


class VerificationTests(unittest.TestCase):
    def result(self, text='Hello there', entities=()):
        return {**own_message(text, entities), 'edit_date': 1700000500}

    def check(self, value, styles=None, new='Hello there'):
        return edited_text_result(value, chat_id=CHAT, message_id=MID, new_text=new, previous_edit_date=0,
                                  **({} if styles is None else {'styles': styles}))

    def test_styled_results_need_exactly_the_caller_styles_and_only_auto_detected_extras(self):
        bold = ((0, 5, 'textEntityTypeBold'),)
        self.assertEqual(self.check(self.result(entities=[entity('textEntityTypeBold', 0, 5)]), bold), 1700000500)
        self.assertEqual(self.check(self.result(entities=[entity('textEntityTypeBold', 0, 5),
                                                          entity('textEntityTypeMention', 6, 5)]), bold), 1700000500)
        for entities in ([], [entity('textEntityTypeItalic', 0, 5)], [entity('textEntityTypeBold', 0, 4)],
                         [entity('textEntityTypeBold', 0, 5), entity('textEntityTypeCode', 6, 5)],
                         [entity('textEntityTypeBold', 0, 5), entity('textEntityTypeTextUrl', 6, 5, url='x')],
                         [entity('textEntityTypeBold', 0, 5), entity('textEntityTypeUrl', 6, 99)]):
            with self.subTest(entities=entities):
                self.assertIsNone(self.check(self.result(entities=entities), bold))

    def test_the_plain_rule_is_unchanged(self):
        self.assertEqual(self.check(self.result(entities=[entity('textEntityTypeBold', 0, 5)])), 1700000500)


class DialogTests(unittest.TestCase):
    maxDiff = None
    CLOSING = ('Unofficial Telegram MCP will revalidate the account, the chat, the message and its edit permission, '
               'then make one edit attempt.')

    def test_styled_golden_lists_every_new_style_with_its_covered_text(self):
        value = preview('Hello there', ((0, 5, 'textEntityTypeBold'), (6, 5, 'textEntityTypeSpoiler')),
                        message=own_message('Hello world', [entity('textEntityTypeItalic', 0, 5),
                                                            entity('textEntityTypeUrl', 6, 5)]))
        self.assertEqual(shown(value), [
            f'Edit your message {MID} in «Team Chat» (chat -1001234, supergroup)\n'
            'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\nLast edited: never\n\n'
            'Current text [untrusted Telegram evidence] (exact):\n│ «Hello world»\n'
            'Current formatting, replaced by this edit (raw text UTF-16 code units):\n'
            'Italic: offset 0, length 5; covered «Hello»\n'
            'Current auto-detected entities, auto-detected again by Telegram wherever the new text contains them '
            '(raw text UTF-16 code units):\n'
            'Url: offset 6, length 5; covered «world»\n'
            'New text (exact):\n│ «Hello there»\n'
            'New text styles (final text; UTF-16 code units):\n'
            'Bold: offset 0, length 5; covered «Hello»\n'
            'Spoiler: offset 6, length 5; covered «there»\n\n'
            'The new text carries only the styles listed here; Telegram may detect links in it and show a link '
            'preview. Link preview settings return to Telegram\'s default. Recipients see the message marked '
            '"edited".\n' +
            dialog_tail(f'Draft: {DID}', f'Source SHA-256: {value.source_sha256}',
                        f'Preview SHA-256: {value.preview_sha256}', closing=self.CLOSING)])

    def test_a_tampered_styled_preview_refuses(self):
        value = preview()
        for changed in ([], [ITALIC], [{'offset': 0, 'length': 99, 'type': {'@type': 'textEntityTypeBold'}}]):
            with self.subTest(changed=changed):
                data = value.model_dump(mode='json')
                data['outgoing_entities'] = changed
                self.assertEqual(shown(value, text_edit_preview=data), [])
        data = value.model_dump(mode='json')
        del data['outgoing_entities']
        self.assertEqual(shown(value, text_edit_preview=data), [])


class TransportShapeTests(unittest.TestCase):
    """Criterion 3: the plain shape keeps entities == []; the second, styled shape admits only canonical styles."""

    @staticmethod
    def request(text='Hello there', entities=()):
        return {'@type': 'editMessageText', 'chat_id': CHAT, 'message_id': MID, 'reply_markup': None,
                'input_message_content': {'@type': 'inputMessageText',
                                          'text': {'@type': 'formattedText', 'text': text, 'entities': list(entities)},
                                          'link_preview_options': None, 'clear_draft': False}}

    def test_the_styled_shape_is_closed(self):
        for good in (self.request(), self.request(entities=[native(BOLD)]),
                     self.request(entities=[native(BOLD), native(ITALIC)])):
            _validate_bounded_request(good)
        self.assertEqual(edit_request(CHAT, MID, 'Hello there', ((0, 5, 'textEntityTypeBold'),)),
                         self.request(entities=[native(BOLD)]))
        self.assertEqual(edit_request(CHAT, MID, 'Hello there'), self.request())
        url = {'@type': 'textEntity', 'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeUrl'}}
        text_url = {'@type': 'textEntity', 'offset': 0, 'length': 5,
                    'type': {'@type': 'textEntityTypeTextUrl', 'url': 'https://x.invalid'}}
        for bad in (self.request(entities=[url]), self.request(entities=[text_url]),
                    self.request(entities=[native(ITALIC), native(BOLD)]),  # not canonical order
                    self.request(entities=[native(BOLD), native(BOLD)]),
                    self.request(entities=[{**native(BOLD), 'length': 99}]),
                    self.request(entities=[{**native(BOLD), 'x': 1}]),
                    self.request(entities=[{'offset': 0, 'length': 5, 'type': {'@type': 'textEntityTypeBold'}}]),
                    self.request(text=' Hello there', entities=[native(BOLD)]),
                    self.request(text='Hello\tthere', entities=[native(BOLD)]),
                    self.request(entities=[native(BOLD)] * 33)):
            with self.subTest(bad=bad['input_message_content']['text']), self.assertRaises(ForbiddenTDLibRequest):
                _validate_bounded_request(bad)


class RegistryTests(unittest.TestCase):
    def test_the_spec_binds_styles_and_names_the_extra_capability(self):
        spec = TextEditSpec(snapshot=snapshot(), new_text='Hello there', styles=((0, 5, 'textEntityTypeBold'),))
        self.assertEqual(spec.extra_capabilities, ('edit_own_styled_text',))
        self.assertEqual(TextEditSpec(snapshot=snapshot(), new_text='Hello there').extra_capabilities, ())
        self.assertIs(DRAFT_KINDS['text_edit'].spec_type(), TextEditSpec)
        for new, styles in (('Hello there', ((0, 99, 'textEntityTypeBold'),)),
                            ('Hello there', ((0, 5, 'textEntityTypeCode'),)),
                            ('Hello there', ((6, 5, 'textEntityTypeItalic'), (0, 5, 'textEntityTypeBold'))),
                            ('Hello world', ()), ('Hello there ', ((0, 5, 'textEntityTypeBold'),))):
            with self.subTest(new=new, styles=styles), self.assertRaises(ValueError):
                TextEditSpec(snapshot=snapshot(), new_text=new, styles=styles)
        with tempfile.TemporaryDirectory() as directory:
            registry = OutgoingDraftRegistry(ArtifactStore(cache_dir=Path(directory) / 'cache'))
            owner = DraftOwner(client_id=CLIENT, account_id=ACCOUNT)
            draft = registry.prepare_change(owner=owner, chat_id=CHAT, chat_title='Team Chat', change=spec)
            self.assertEqual(registry.extra_capabilities(draft.draft_id, owner=owner), ('edit_own_styled_text',))


class SurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_schema_change_is_listed_and_published(self):
        listed = json.loads((SCHEMA_CHANGES / '0.57-styled-text-edit.json').read_text(encoding='utf-8'))
        self.assertEqual(set(listed), {'structure'})
        self.assertEqual(set(listed['structure']), {'prepare_text_edit'})
        from tool_listing import full_listing
        tools = await full_listing()
        prepare = tools['prepare_text_edit']
        self.assertEqual(set(prepare.input_schema['properties']), {'chat', 'message_id', 'new_text',
                                                                   'outgoing_entities'})
        self.assertEqual(sorted(prepare.input_schema['required']), ['chat', 'message_id', 'new_text'])
        self.assertIn('edit_own_styled_text', prepare.input_schema['properties']['outgoing_entities']['description'])
        output = json.dumps(prepare.output_schema)
        for code in ('capability_disabled', 'enable_capability', 'edit_own_styled_text'):
            self.assertIn(f'"{code}"', output)

    async def test_styled_and_plain_edits_through_the_official_client_and_a_served_broker(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='s')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        raw, approvals = Raw(), []
        client = TDLibClient(raw=raw)
        for policy, expected in ((EDIT, 'capability_disabled'), (STYLED, 'prepared')):
            with self.subTest(policy=policy.enabled_capabilities):
                broker = Broker(socket_path=root / 'b.sock', lock_path=root / 'b.lock',
                                artifact_store=ArtifactStore(cache_dir=root / 'cache'), client_factory=lambda: client,
                                policy=policy, approval_prompt=lambda **facts: approvals.append(facts) or True)
                thread = threading.Thread(target=broker.serve_forever, daemon=True)
                thread.start()
                self.assertTrue(broker.wait_until_ready(timeout=10))
                proxy = BrokerClient(socket_path=root / 'b.sock', policy=policy, restart_callback=lambda: None)
                try:
                    async with Client(build_server(service_factory=lambda: proxy, policy=policy)) as consumer:
                        result = await consumer.call_tool('prepare_text_edit', {
                            'chat': CHAT, 'message_id': MID, 'new_text': 'Hello there', 'outgoing_entities': [BOLD]})
                        self.assertFalse(result.is_error, result.content)
                        prepared = result.structured_content
                        self.assertEqual(prepared['status'], expected, prepared)
                        if expected == 'prepared':
                            applied = await consumer.call_tool('apply_prepared_text_edit', {
                                'draft_id': prepared['text_edit']['draft_id'], 'approved': True})
                            self.assertEqual(applied.structured_content['status'], 'applied')
                        else:
                            self.assertEqual(prepared['missing_capabilities'], ['edit_own_styled_text'])
                finally:
                    proxy.close()
                    broker.shutdown()
                    thread.join(10)
        self.assertEqual(len([r for r in raw.sent if r['@type'] == 'editMessageText']), 1)
        self.assertEqual(len(approvals), 1)


if __name__ == '__main__':
    unittest.main()
