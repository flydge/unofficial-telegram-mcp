"""Fieldless EmailAddress source risks, with independent literal span expectations.

No email parser, native TDLib, network, credentials or historical helper execution.
Only raw provider transport and the local owner callback are synthetic.
"""
import copy
import hashlib
import itertools
import json
import tempfile
import time
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_formatted_sources import formatted_text
from telegram_search_mcp.tdjson import TDLibClient

CHAT = 123
MESSAGE = 55
CLIENT = 'client_' + 'c' * 24
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
SOURCE_CAPS = ('reply_formatted_targets', 'reply_email_targets', 'reply_datetime_targets',
               'reply_lexical_targets', 'reply_identity_targets', 'reply_url_targets')
POLICY = RuntimePolicy(enabled_capabilities=BASE + SOURCE_CAPS)
PREFIX = '[untrusted Telegram evidence] Formatted target: '
EMAIL = 'textEntityTypeEmailAddress'


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def entity(kind='EmailAddress', offset=0, length=3, **fields):
    return {'@type': 'textEntity', 'offset': offset, 'length': length,
            'type': {'@type': 'textEntityType' + kind, **fields}}


def message(text='abc', entities=None):
    return {'@type': 'message', 'chat_id': CHAT, 'id': MESSAGE,
            'sender_id': {'@type': 'messageSenderUser', 'user_id': 8},
            'is_outgoing': False, 'is_from_offline': False, 'ephemeral_message_id': 0,
            'date': 1700000000, 'edit_date': 0, 'self_destruct_in': 0, 'auto_delete_in': 0,
            'content': {'@type': 'messageText', 'text': {'@type': 'formattedText', 'text': text,
                        'entities': [entity()] if entities is None else entities}}}


def absolute():
    return {'@type': 'dateTimeFormattingTypeAbsolute',
            'time_precision': {'@type': 'dateTimePartPrecisionNone'},
            'date_precision': {'@type': 'dateTimePartPrecisionLong'}, 'show_day_of_week': False}


def mixed():
    return message('abcD#tag@userU', [entity(), entity('DateTime', 3, 1, unix_time=1, formatting_type=absolute()),
                   entity('Hashtag', 4, 4), entity('MentionName', 8, 5, user_id=42), entity('Url', 13, 1)])


def record(text='abc', offset=0, length=3):
    return {'type': 'email_address', 'provider_type': EMAIL, 'text': text, 'offset': offset, 'length': length}


def display(text, records):
    return PREFIX + encode({'text': text, 'offset_basis': 'original source UTF-16 code units', 'entities': records})


def media(kind, caption):
    file = {'@type':'file','id':1,'size':4,'expected_size':4,
        'local':{'@type':'localFile','path':'','can_be_downloaded':True,'can_be_deleted':False,
                 'is_downloading_active':False,'is_downloading_completed':False,'download_offset':0,'downloaded_prefix_size':0,'downloaded_size':0},
        'remote':{'@type':'remoteFile','id':'remote','unique_id':'email-fixture','is_uploading_active':False,'is_uploading_completed':True,'uploaded_size':4}}
    if kind=='document':
        return {'@type':'messageDocument','caption':caption,'document':{'@type':'document','file_name':'fixture.txt','mime_type':'text/plain','document':file}}
    if kind=='photo':
        return {'@type':'messagePhoto','caption':caption,'has_spoiler':False,'is_secret':False,'show_caption_above_media':False,
            'photo':{'@type':'photo','has_stickers':False,'sizes':[{'@type':'photoSize','type':'i','width':2,'height':2,'photo':file,'progressive_sizes':[]}]}}
    return {'@type':'messageVoiceNote','caption':caption,'is_listened':False,
        'voice_note':{'@type':'voiceNote','duration':2,'waveform':'AAAA','mime_type':'audio/ogg','voice':file}}


class EmailAddressSourceTests(unittest.TestCase):
    def accepted(self, raw=None):
        try:
            return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            self.fail('exact fieldless EmailAddress messageText must produce complete v17 evidence: ' + str(error))

    def test_v1_through_v16_canonical_display_and_authority_preserve_baseline(self):
        # Break: email admission changes previously accepted complete evidence or authority.
        fixture = Path(__file__).parent/'fixtures/reply_email_legacy_goldens.json'
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
            'c5c52c06efc963b4bf2a2ed2989a11aae161e3965f25783d3b8d5a273e955510')
        for row in json.loads(fixture.read_text()):
            source = source_from_message(row['raw'], CHAT, MESSAGE)
            with self.subTest(version=row['version']):
                self.assertEqual(source.projection_json, row['projection_json'])
                self.assertEqual(source.target().text, row['display'])
                self.assertEqual(source.source_sha256, row['raw_sha256'])
                self.assertEqual(hashlib.sha256(source.target().text.encode()).hexdigest(), row['display_sha256'])
                self.assertEqual(source.required_capabilities, tuple(row['capabilities']))

    def test_fieldless_provider_label_admits_literal_complete_span_without_email_grammar(self):
        # Break: excluding the declared constructor, interpreting EMAIL grammar, or dropping evidence.
        source = self.accepted()
        self.assertEqual(json.loads(source.projection_json)['version'], 17)
        self.assertEqual(source.raw_text, 'abc')
        self.assertEqual(source.target().text, display('abc', [record()]))
        self.assertEqual(source.required_capabilities, ('reply_formatted_targets', 'reply_email_targets'))
        self.assertFalse(source.is_media)
        self.assertFalse(source.target().sanitized)
        self.assertFalse(source.target().truncated)
        self.assertEqual(ReplySource(source.projection_json).target(), source.target())
        with self.assertRaises(ValueError): _ = source.required_capability
        for text in ('mailto:a@example.invalid', 'not an address', 'a @ b', 'a@example.invalid?subject="\\'):
            source = self.accepted(message(text, [entity(length=len(text))]))
            self.assertEqual(source.target().text, display(text, [record(text, length=len(text))]))

    def test_original_utf16_whole_scalars_and_display_only_sanitation_are_bound(self):
        # Break: applying scalar coordinates or slicing the sanitized rather than original text.
        source = self.accepted(message('😀Ａ\tB', [entity(offset=0, length=3)]))
        self.assertEqual(source.raw_text, '😀Ａ\tB')
        self.assertEqual(source.target().text, display('😀A B', [record('😀A', 0, 3)]))
        self.assertTrue(source.target().sanitized)
        original = source.projection_json, source.target(), source.source_sha256
        raw = message('😀Ａ\tB', [entity(offset=0, length=3)])
        separate = self.accepted(raw); raw['content']['text']['entities'][0]['type']['email'] = 'hidden'
        raw['content']['text']['text'] = 'changed'
        self.assertEqual((separate.projection_json, separate.target(), separate.source_sha256), original)
        for offset, length in ((0, 1), (1, 1), (True, 2), (-1, 2), (0, False), (0, 99), (2**31, 2)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                source_from_message(message('😀abc', [entity(offset=offset, length=length)]), CHAT, MESSAGE)

    def test_raw_identity_preserves_distinct_unsanitized_sources(self):
        # Break: hashing sanitized display hides a changed original provider assertion.
        fullwidth=self.accepted(message('Ａ', [entity(length=1)]))
        ascii_source=self.accepted(message('A', [entity(length=1)]))
        self.assertEqual(fullwidth.target().text, display('A', [record('A', length=1)]))
        self.assertEqual(fullwidth.target().text, ascii_source.target().text)
        self.assertNotEqual(fullwidth.source_sha256, ascii_source.source_sha256)
        self.assertEqual(fullwidth.raw_text, 'Ａ')
        self.assertEqual(ascii_source.raw_text, 'A')

    def test_email_constructor_exact_fieldless_shape_is_not_coerced(self):
        # Break: a type hint selects v17 before shape validation, or extra fields escape.
        for typ in ({'@type':'textEntityTypeEmailAddress','address':'a@example.invalid'},
                {'@type':'textEntityTypeEmailAddress','email':None},
                {'@type':'textEntityTypeEmailAddress','url':'mailto:a@example.invalid'},
                {'@type':'textEntityTypeEmailAddress','extra':False},
                {'@type':['textEntityTypeEmailAddress']}, {'@type':True},
                {'@type':'textEntityTypeEmailAddress '}, 'textEntityTypeEmailAddress', None):
            raw=message();raw['content']['text']['entities'][0]['type']=typ
            with self.subTest(typ=typ), self.assertRaises(ValueError):
                source_from_message(raw,CHAT,MESSAGE)

    def test_v17_mixed_family_authority_is_conditional_and_datetime_semantics_preserved(self):
        # Break: requiring absent families, forgetting present ones, or losing nested DateTime facts.
        for date_on, lexical_on, identity_on, url_on in itertools.product((False, True), repeat=4):
            raw = mixed(); spans = raw['content']['text']['entities']
            raw['content']['text']['entities'] = [spans[0]] + ([spans[1]] if date_on else []) + ([spans[2]] if lexical_on else []) + ([spans[3]] if identity_on else []) + ([spans[4]] if url_on else [])
            source = self.accepted(raw)
            required = ('reply_formatted_targets', 'reply_email_targets')
            if url_on: required += ('reply_url_targets',)
            if date_on: required += ('reply_datetime_targets',)
            if lexical_on: required += ('reply_lexical_targets',)
            if identity_on: required += ('reply_identity_targets',)
            self.assertEqual(source.required_capabilities, required)
            records = [record()]
            if date_on: records.append({'type': 'date_time', 'provider_type': 'textEntityTypeDateTime',
                'offset': 3, 'length': 1, 'text': 'D', 'unix_time': 1, 'formatting_type': absolute()})
            if lexical_on: records.append({'type': 'hashtag', 'offset': 4, 'length': 4, 'text': '#tag'})
            if identity_on: records.append({'type': 'mention_name', 'offset': 8, 'length': 5, 'text': '@user', 'user_id': 42})
            if url_on: records.append({'type': 'url', 'provider_type': 'textEntityTypeUrl', 'offset': 13, 'length': 1, 'text': 'U'})
            self.assertEqual(source.target().text, display('abcD#tag@userU', records))
        for fmt in (None, {'@type': 'dateTimeFormattingTypeRelative'}, absolute()):
            raw = mixed(); raw['content']['text']['entities'][1]['type']['formatting_type'] = fmt
            source = self.accepted(raw)
            self.assertEqual(json.loads(source.projection_json)['version'], 17)
            self.assertEqual(json.loads(source.target().text[len(PREFIX):])['entities'][1]['formatting_type'], fmt)

    def test_exact_constructor_and_default_formatter_closure(self):
        # Break: an open field set, alternate hidden-destination family, or global formatter opt-in.
        formatted = message()['content']['text']
        with self.assertRaises(ValueError): formatted_text(formatted)
        self.assertEqual(self.accepted()._validate_projection()['message']['content']['text'], formatted)
        self.assertEqual(formatted_text(formatted, email=True), formatted)
        from telegram_search_mcp.reply_formatted_sources import validate_content, render_content, render_formatted_text
        content=self.accepted()._validate_projection()['message']['content']
        for operation in (lambda:validate_content(content), lambda:render_formatted_text(formatted),
                lambda:render_content(content, '[untrusted Telegram evidence] ')):
            with self.assertRaises(ValueError):operation()
        validate_content(content,email=True)
        self.assertEqual(render_content(content,'[untrusted Telegram evidence] ',email=True),
                         (display('abc',[record()]),False))
        self.assertEqual(render_formatted_text(formatted,email=True),
                         ({'text':'abc','offset_basis':'original source UTF-16 code units','entities':[record()]},False))
        for kind, fields in (('EmailAddress', {'email': 'hidden'}), ('EmailAddress', {'extra': None}),
                ('TextUrl', {'url': 'https://example.invalid', 'extra': None}), ('PhoneNumber', {'phone':'hidden'}),
                ('BankCardNumber', {'bank_card_number':'hidden'}), ('CustomEmoji', {'custom_emoji_id': 1}), ('MediaTimestamp', {'media_timestamp': 1})):
            with self.subTest(kind=kind, fields=fields), self.assertRaises(ValueError):
                source_from_message(message(entities=[entity(kind, **fields)]), CHAT, MESSAGE)

    def test_email_overlap_local_subset_allows_ordinary_style_containment_only(self):
        # Break: losing exclusive overlap policy or rejecting supported containment.
        for kind in ('Bold', 'Italic', 'Underline', 'Strikethrough', 'Spoiler'):
            for spans in ([entity(kind, 0, 5), entity(offset=1, length=3)],
                          [entity(length=5), entity(kind, 1, 3)], [entity(length=3), entity(kind)]):
                self.accepted(message('abcde', spans))
        for kind, fields in (('EmailAddress', {}), ('Url', {}), ('Hashtag', {}), ('Cashtag', {}), ('BotCommand', {}),
                ('Mention', {}), ('MentionName', {'user_id': 42}), ('Code', {}), ('Pre', {}),
                ('PreCode', {'language': 'py'}), ('BlockQuote', {}), ('ExpandableBlockQuote', {}),
                ('DateTime', {'unix_time': 1, 'formatting_type': None})):
            for spans in ([entity(length=5), entity(kind, 1, 3, **fields)],
                          [entity(kind, 0, 5, **fields), entity(offset=1, length=3)]):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    source_from_message(message('abcde', spans), CHAT, MESSAGE)
        for spans in ([entity(), entity()], [entity(), entity('Bold', 2, 3)]):
            with self.assertRaises(ValueError): source_from_message(message('abcde', spans), CHAT, MESSAGE)

    def test_forged_old_tags_and_v17_without_email_refuse(self):
        # Break: laundering EMAIL through a legacy version or enabling v17 for unrelated content.
        source = self.accepted(); stored = json.loads(source.projection_json)
        for version in range(1, 17):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ReplySource(encode({**stored, 'version': version}))
        for spans in ([], [entity('Url')], [entity('Bold')], [entity('DateTime', unix_time=1, formatting_type=absolute())]):
            old = source_from_message(message(entities=spans), CHAT, MESSAGE)
            with self.assertRaises(ValueError): ReplySource(encode({**json.loads(old.projection_json), 'version': 17}))
        stored['message']['content']['text']['entities'][0]['type']['extra'] = None
        with self.assertRaises(ValueError): ReplySource(encode(stored))

    def test_complete_escaped_bounds_and_entity_count_refuse_overflow(self):
        # Break: relaxing bounds or truncating rather than refusing complete evidence.
        overhead = len(display('', [record('x', 0, 1)]))
        text = 'x' * (4096 - overhead)
        source = self.accepted(message(text, [entity(length=1)]))
        self.assertEqual(source.target().text, display(text, [record('x', 0, 1)]))
        self.assertEqual(len(source.target().text), 4096)
        spans = [entity(offset=i, length=1) for i in range(32)]
        self.assertEqual(len(json.loads(self.accepted(message('x'*32, spans)).projection_json)['message']['content']['text']['entities']), 32)
        for raw in (message(text+'x', [entity(length=1)]), message('x'*4097),
                    message('"'*2100, [entity(length=1)]), message('\ufdfa'*250, [entity(length=250)]),
                    message('x'*33, spans+[entity(offset=32, length=1)]), message('\x00abc'), message('\ud800abc')):
            with self.subTest(length=len(raw['content']['text']['text'])), self.assertRaises(ValueError):
                source_from_message(raw, CHAT, MESSAGE)
        with self.assertRaises(ValueError): ReplySource('x'*65537)

    def test_trusted_policy_email_capability_is_known_and_default_off(self):
        # Break: silently granting EMAIL evidence authority or rejecting the trusted explicit opt-in.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700); path = root/'runtime.toml'
            self.assertNotIn('reply_email_targets', load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["reply_email_targets","reply_formatted_targets"]\n')
            path.chmod(0o600)
            self.assertEqual(load_runtime_policy(path).enabled_capabilities,
                             ('reply_email_targets', 'reply_formatted_targets'))

    def test_email_captions_use_distinct_media_authority_other_five_remain_closed(self):
        # Break: caption evidence loses media authority or admits hidden/action constructors.
        for kind in ('document', 'photo', 'voice_note'):
            raw=message(); raw['content']=media(kind,raw['content']['text'])
            source=source_from_message(raw,CHAT,MESSAGE)
            with self.subTest(kind=kind):
                self.assertEqual(json.loads(source.projection_json)['version'],18)
                self.assertEqual(source.required_capabilities,('reply_media_targets','reply_formatted_targets','reply_email_targets'))
        for kind, fields in (('TextUrl', {'url':'mailto:a@example.invalid', 'extra': None}), ('PhoneNumber', {'phone':'hidden'}),
                ('BankCardNumber', {'bank_card_number':'hidden'}), ('CustomEmoji', {'custom_emoji_id':1}),
                ('MediaTimestamp', {'media_timestamp':1})):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                source_from_message(message(entities=[entity(kind, **fields)]),CHAT,MESSAGE)

    def test_default_media_helper_stays_closed_even_with_other_family_optins(self):
        # Break: media helper EMAIL opt-in silently becomes global merely from other family opt-ins.
        from telegram_search_mcp.reply_media_sources import media_content
        for kind in ('document','photo','voice_note'):
            for spans in ([entity()], mixed()['content']['text']['entities']):
                raw=message('abcD#tag@userU',spans); raw['content']=media(kind,raw['content']['text'])
                with self.assertRaises(ValueError):
                    media_content(raw['content'],lexical=True,identity=True,datetime=True,datetime_relative=True,datetime_absolute=True,url=True)


class EmailAddressRaw:
    def __init__(self):
        self.source = mixed(); self.requests = []; self.events = deque(); self.hook = None

    def send(self, request):
        self.requests.append(copy.deepcopy(request)); kind = request['@type']
        if self.hook: self.hook(request)
        if kind == 'getAuthorizationState': result = {'@type': 'authorizationStateReady'}
        elif kind == 'getMe': result = {'@type': 'user', 'id': 7}
        elif kind == 'getChat':
            if request['chat_id'] != CHAT: raise AssertionError('unexpected recipient')
            result = {'@type': 'chat', 'id': CHAT, 'title': 'Synthetic EMAIL room', 'type': {'@type': 'chatTypePrivate'}}
        elif kind == 'getMessage':
            if (request['chat_id'], request['message_id']) != (CHAT, MESSAGE): raise AssertionError('unexpected anchor')
            result = copy.deepcopy(self.source)
        elif kind == 'getMessageProperties': result = {'@type': 'messageProperties', 'can_be_replied': True}
        elif kind == 'sendMessage':
            incoming = request['input_message_content']
            content = ({'@type': 'messageText', 'text': copy.deepcopy(incoming['text'])} if incoming['@type']=='inputMessageText'
                       else media('document',copy.deepcopy(incoming['caption'])))
            shell = {'@type': 'message', 'chat_id': CHAT, 'is_outgoing': True, 'content': content,
                'reply_to': {'@type': 'messageReplyToMessage', 'chat_id': CHAT, 'message_id': MESSAGE,
                'checklist_task_id': 0, 'poll_option_id': '', 'origin_send_date': 0}}
            self.events.append({**shell, 'id': -10, 'sending_state': {'@type': 'messageSendingStatePending',
                'sending_id': request['options']['sending_id']}, '@extra': request['@extra']})
            self.events.append({'@type': 'updateMessageSendSucceeded', 'old_message_id': -10,
                'message': {**shell, 'id': 701, 'sending_state': None}})
            return
        else: raise AssertionError('source-directed operation forbidden: ' + kind)
        self.events.append({**result, '@extra': request['@extra']})

    def receive(self, timeout): return self.events.popleft() if self.events else None
    def close(self): pass


class EmailAddressLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='email-'); self.root = Path(self.tmp.name)
        self.raw = EmailAddressRaw(); self.provider = TDLibClient(raw=self.raw, session_directory=self.root/'unused',
            credential_loader=lambda: self.fail('credentials forbidden'))
        self.dialogs = []; self.decision = lambda: True
        def owner(**facts): self.dialogs.append(copy.deepcopy(facts)); return self.decision()
        store = ArtifactStore(cache_dir=self.root/'artifacts'); payload = self.root/'fixture.txt'; payload.write_bytes(b'data')
        self.broker = Broker(socket_path=self.root/'unused.sock', client_factory=lambda: self.provider,
            artifact_store=store, approval_prompt=owner, policy=POLICY)
        with self.broker._artifact_authority.scope(CLIENT, store):
            self.artifact = store.store(payload)
        self.stack = ExitStack(); self.staging = self.root/'staging'
        self.history = []
        real_claim = self.broker._drafts.claim
        def claim(*args, **kwargs):
            result = real_claim(*args, **kwargs)
            self.history.append(('claim', result.draft.draft_id))
            return result
        self.stack.enter_context(patch.object(self.broker._drafts, 'claim', side_effect=claim))
        def stage(claimed):
            result = stage_approved_document(claimed, root=self.staging)
            self.assertTrue(result.is_file(), 'successful staging must be observed while the file exists')
            self.history.append(('stage', claimed.draft.draft_id, result))
            return result
        def retire(path):
            self.assertTrue(path.is_file(), 'retirement must follow an actual staged file')
            retire_staged_document(path, root=self.staging)
            self.assertFalse(path.exists(), 'real retirement must remove the staged file')
            self.history.append(('retire', path))
        self.stack.enter_context(patch('telegram_search_mcp.tdjson.STAGING_ROOT', self.staging))
        self.stack.enter_context(patch('telegram_search_mcp.broker.stage_approved_document', side_effect=stage))
        self.stack.enter_context(patch('telegram_search_mcp.broker.retire_staged_document', side_effect=retire))

    def tearDown(self):
        self.stack.close(); self.broker.shutdown(); self.broker._executor.shutdown(wait=True)
        self.provider.close(); self.tmp.cleanup()

    def dispatch(self, op, payload):
        return self.broker._dispatch({'operation': op, 'payload': payload, 'client_id': CLIENT,
            'deadline': time.monotonic()+10, 'broker_generation': self.broker._generation})

    def prepare(self, family):
        payload = {'recipient': CHAT, 'reply_to': {'chat_id': CHAT, 'message_id': MESSAGE}}
        if family=='text': payload['text']='Plain'; op='prepare_reply_text_send'
        else:
            payload.update(artifact_id=self.artifact.artifact_id, display_name='fixture.txt', mime_type='text/plain', kind='document', caption='Plain')
            op='prepare_reply_artifact_send'
        return self.dispatch(op, payload)

    def prepared(self, family):
        result=self.prepare(family); self.assertEqual(result['status'], 'prepared', 'complete EMAIL source must reach immutable draft')
        self.assertIn('"provider_type":"textEntityTypeEmailAddress"', result['reply']['reply_target']['text'])
        return result['reply']

    def send(self, did, family, approved=True):
        return self.dispatch('send_prepared_text' if family=='text' else 'send_prepared_artifact', {'draft_id':did,'approved':approved})

    def operation(self, verb, family):
        return verb+'_reply_draft' if family=='text' else verb+'_reply_artifact_draft'

    def revise(self, did, family, refresh=False):
        return self.dispatch(self.operation('refresh' if refresh else 'update', family),
            {'draft_id':did, **({} if refresh else {'text' if family=='text' else 'caption':'Changed'})})

    def no_effects(self):
        # Final absence proves cleanup, not historical absence of claim/staging.
        self.assertFalse(any(r['@type']=='sendMessage' for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def test_missing_or_revoked_each_conjunct_blocks_prepare_lifecycle_and_owner(self):
        # Break: checking EMAIL authority only once or authorizing a mixed family with fewer caps.
        for family in ('text','artifact'):
            for missing in SOURCE_CAPS:
                denied=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                self.broker._policy=denied; self.assertEqual(self.prepare(family)['status'],'unavailable')
                self.broker._policy=POLICY; did=self.prepared(family)['draft']['draft_id']; self.broker._policy=denied
                reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
                self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['status'],'unavailable')
                self.assertEqual(self.revise(did,family)['status'],'unavailable')
                self.assertEqual(self.revise(did,family,True)['status'],'unavailable')
                self.assertEqual(self.send(did,family)['status'],'expired')
                self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
        self.assertEqual(self.dialogs,[])
        self.assertEqual(self.history, [], 'already absent/revoked capability must refuse before claim/staging')
        self.no_effects()

    def test_capability_revocation_at_owner_properties_and_registration_stops_wire(self):
        # Break: missing post-owner, final-properties, or immediate prewire plural authority guard.
        for family in ('text','artifact'):
            for phase in ('owner','properties','registration'):
                for missing in SOURCE_CAPS:
                    self.broker._policy=POLICY; self.raw.hook=None; self.decision=lambda:True
                    did=self.prepared(family)['draft']['draft_id']
                    history_start = len(self.history)
                    denied=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                    if phase=='owner':
                        def revoke(): self.broker._policy=denied; return True
                        self.decision=revoke
                    elif phase=='properties':
                        self.raw.hook=lambda r:setattr(self.broker,'_policy',denied) if r['@type']=='getMessageProperties' else None
                    if phase=='registration':
                        original=self.provider._send_observations.register
                        def register(*args,**kwargs): original(*args,**kwargs); self.broker._policy=denied
                        with patch.object(self.provider._send_observations,'register',side_effect=register): result=self.send(did,family)
                    else: result=self.send(did,family)
                    self.assertEqual(result['status'], 'expired' if phase=='owner' else 'failed')
                    self.assertIsNone(self.provider._send_observations.snapshot(did))
                    events = self.history[history_start:]
                    expected = [] if phase=='owner' else ['claim'] if family=='text' else ['claim','stage','retire']
                    self.assertEqual([event[0] for event in events], expected,
                        'owner revocation is preclaim; later guards refuse transport after claim and transient artifact staging')
                    if phase!='owner':
                        self.assertEqual(events[0], ('claim', did))
                        status=self.dispatch('get_send_status', {'draft_id':did})
                        self.assertEqual((status['status'],status['evidence']), ('failed','local_failed'))
                        if family=='artifact':
                            self.assertEqual(events[1][1], did)
                            self.assertEqual(events[2], ('retire', events[1][2]))
                            self.assertFalse(events[1][2].exists())
        self.no_effects()

    def test_source_drift_after_owner_is_terminal_before_raw_transport(self):
        # Break: ignoring original EMAIL text/span/type in the final immutable-source digest.
        for family in ('text','artifact'):
            for mutation in ('text','offset','length','type','open'):
                self.raw.source=mixed(); self.broker._policy=POLICY; self.raw.hook=None
                preview=self.prepared(family); did=preview['draft']['draft_id']
                def drift():
                    span=self.raw.source['content']['text']['entities'][0]
                    if mutation=='text': self.raw.source['content']['text']['text']='xyzD#tag@userU'
                    elif mutation=='offset': span['offset']=1
                    elif mutation=='length': span['length']=2
                    elif mutation=='type': span['type']={'@type':'textEntityTypeBold'}
                    else: span['type']['email']='hidden'
                    return True
                self.decision=drift
                self.assertEqual(self.send(did,family)['status'],'failed')
                self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
                self.assertEqual(self.send(did,family)['status'],'failed')
                self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['status'],'unavailable')
        self.no_effects()

    def test_update_is_immutable_refresh_requires_old_and_new_authority_and_invalidates_ids(self):
        # Break: rereading source on update or transferring approval across EMAIL/legacy refresh.
        for family in ('text','artifact'):
            self.broker._policy=POLICY; self.raw.source=mixed(); original=self.prepared(family)
            reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
            self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':original['draft']['draft_id']})['reply'],original)
            self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
            did=original['draft']['draft_id']; self.raw.source=message('abc',[entity('Url')])
            reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
            revised=self.revise(did,family)['reply']
            self.assertEqual(revised['reply_target'],original['reply_target'])
            self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
            self.assertEqual(self.send(did,family)['status'],'expired')
            self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_formatted_targets','reply_url_targets'))
            self.assertEqual(self.revise(revised['draft']['draft_id'],family,True)['status'],'unavailable')
            self.broker._policy=POLICY
            older=self.revise(revised['draft']['draft_id'],family,True)['reply']
            self.assertNotEqual(older['reply_target']['source_sha256'],original['reply_target']['source_sha256'])
            self.assertEqual(self.send(revised['draft']['draft_id'],family)['status'],'expired')
            self.raw.source=mixed(); self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_formatted_targets','reply_url_targets'))
            self.assertEqual(self.revise(older['draft']['draft_id'],family,True)['status'],'unavailable')
            self.broker._policy=POLICY
            newest=self.revise(older['draft']['draft_id'],family,True)['reply']
            self.assertEqual(newest['reply_target'],original['reply_target'])
            self.assertNotEqual(newest['preview_sha256'],original['preview_sha256'])
            self.assertEqual(self.send(older['draft']['draft_id'],family)['status'],'expired')
            self.assertEqual(self.send(newest['draft']['draft_id'],family,False)['status'],'not_approved')
            self.decision=lambda:False
            self.assertEqual(self.send(newest['draft']['draft_id'],family)['status'],'not_approved')
        self.assertEqual(self.history, [], 'caller false and owner denial must not claim or stage')
        self.no_effects()

    def test_plain_send_once_terminal_replay_requires_all_source_capabilities(self):
        # Break: source-directed action, formatted outgoing payload, changed anchor/options, or resend.
        for family in ('text','artifact'):
            self.broker._policy=POLICY; self.raw.source=mixed(); self.decision=lambda:True
            preview=self.prepared(family); did=preview['draft']['draft_id']
            self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
            sends=[r for r in self.raw.requests if r['@type']=='sendMessage']; wire=sends[-1]
            self.assertEqual(wire['chat_id'],CHAT)
            self.assertEqual(wire['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':MESSAGE,'quote':None,'checklist_task_id':0,'poll_option_id':''})
            self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
            self.assertEqual({k:v for k,v in wire['options'].items() if k!='sending_id'}, {
                '@type':'messageSendOptions','suggested_post_info':None,'disable_notification':False,
                'from_background':False,'protect_content':False,'allow_paid_broadcast':False,
                'paid_message_star_count':0,'update_order_of_installed_sticker_sets':False,
                'scheduling_state':None,'effect_id':0,'only_preview':False})
            if family=='text':
                self.assertFalse(wire['input_message_content']['clear_draft'])
                self.assertEqual(wire['input_message_content']['link_preview_options'], {
                    '@type':'linkPreviewOptions','is_disabled':True,'url':'','force_small_media':False,
                    'force_large_media':False,'show_above_text':False})
            self.assertEqual(wire['input_message_content']['text' if family=='text' else 'caption'],{'@type':'formattedText','text':'Plain','entities':[]})
            for missing in SOURCE_CAPS:
                self.broker._policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                self.assertEqual(self.send(did,family)['status'],'expired')
            self.broker._policy=POLICY; self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(len([r for r in self.raw.requests if r['@type']=='sendMessage']),len(sends))
        self.assertTrue(all(r['@type'] in ('getAuthorizationState','getMe','getChat','getMessage','getMessageProperties','sendMessage') for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))
