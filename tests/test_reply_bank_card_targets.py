"""BankCardNumber is inert provider evidence, never a bank lookup or payment.

The production formatter, immutable drafts, broker, staging and send lifecycle
are real. Only raw provider transport and owner decisions are synthetic.
"""
import copy
import hashlib
import itertools
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy, ConfigurationError
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_formatted_sources import formatted_text, validate_content, render_formatted_text, render_content
from telegram_search_mcp.tdjson import TDLibClient
from test_reply_phone_targets import entity as old_entity, message as old_message, media, absolute, encode, PhoneNumberRaw

CHAT, MESSAGE = 123, 55
CLIENT = 'client_' + 'c' * 24
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
CAPS = ('reply_formatted_targets', 'reply_bank_card_targets', 'reply_phone_targets',
        'reply_email_targets', 'reply_url_targets', 'reply_datetime_targets',
        'reply_lexical_targets', 'reply_identity_targets')
POLICY = RuntimePolicy(enabled_capabilities=BASE + CAPS)
PREFIX = '[untrusted Telegram evidence] Formatted target: '
BANK = 'textEntityTypeBankCardNumber'


def entity(kind='BankCardNumber', offset=0, length=3, **fields):
    return old_entity(kind, offset, length, **fields)


def message(text='abc', spans=None):
    return old_message(text, [entity()] if spans is None else spans)


def mixed():
    # Independently counted disjoint UTF-16 spans; no family is inferred from literal grammar.
    return message('abcPD#tag@userUE', [entity(), entity('PhoneNumber',3,1),
        entity('DateTime',4,1,unix_time=1,formatting_type=absolute()),
        entity('Hashtag',5,4),entity('MentionName',9,5,user_id=42),
        entity('Url',14,1),entity('EmailAddress',15,1)])


def record(text='abc', offset=0, length=3):
    return {'type':'bank_card_number','provider_type':BANK,'text':text,'offset':offset,'length':length}


def display(text, records):
    return PREFIX + encode({'text':text,'offset_basis':'original source UTF-16 code units','entities':records})


class BankCardSourceTests(unittest.TestCase):
    def accepted(self, raw=None):
        try:
            return source_from_message(message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            self.fail('exact fieldless BankCardNumber messageText must produce complete v21 evidence: '+str(error))

    def test_v1_through_v20_complete_legacy_bytes_and_authority(self):
        # Break: new text admission changes a previously accepted display/digest/authority contract.
        expected_hashes = {
            'reply_email_legacy_goldens.json':'c5c52c06efc963b4bf2a2ed2989a11aae161e3965f25783d3b8d5a273e955510',
            'reply_email_v17_goldens.json':'ad6ffde31748fdbbf78c0df1532878d6150b89ec87323d620f0458397d59f569',
            'reply_phone_v18_goldens.json':'03b3c5e6b41c80e4f48df733c01df9400fae93726c8b38b8f2cee90c8164ae82',
            'reply_phone_v19_goldens.json':'72505fbc35322a3d7b4b00f9420f561c1347a16edab87c8bbe273639abc95d4e',
            'reply_bank_card_v20_goldens.json':'8d1c0a51c8482c440d002beb46b7845a84c99d983c88850df24fdf53a70957ed',
        }
        versions = set()
        for name, sha in expected_hashes.items():
            fixture = Path(__file__).parent/'fixtures'/name
            self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), sha)
            for row in json.loads(fixture.read_text()):
                with self.subTest(name=name,label=row.get('label')):
                    source = source_from_message(row['raw'],CHAT,MESSAGE)
                    versions.add(json.loads(source.projection_json)['version'])
                    self.assertEqual(source.projection_json,row['projection_json'])
                    self.assertEqual(source.target().text,row['display'])
                    self.assertEqual(source.source_sha256,row['raw_sha256'])
                    self.assertEqual(hashlib.sha256(source.target().text.encode()).hexdigest(),row['display_sha256'])
                    self.assertEqual(source.required_capabilities,tuple(row['capabilities']))
                    self.assertEqual(ReplySource(source.projection_json).target(),source.target())
        self.assertEqual(versions,set(range(1,21)))

    def test_fieldless_literal_no_grammar_and_immutable_v21_roundtrip(self):
        # Break: excluding BankCard, interpreting numbers or silently omitting the provider assertion.
        for text in ('abc','not a number','4111 1111 1111 1111','opaque?x="\\','getBankCardInfo'):
            raw=message(text,[entity(length=len(text))]); source=self.accepted(raw)
            self.assertEqual(json.loads(source.projection_json)['version'],21)
            self.assertEqual(source.target().text,display(text,[record(text,length=len(text))]))
            self.assertEqual(source.required_capabilities,('reply_formatted_targets','reply_bank_card_targets'))
            self.assertFalse(source.is_media); self.assertFalse(source.target().truncated)
            self.assertFalse(source.target().sanitized)
            self.assertEqual(ReplySource(source.projection_json).target(),source.target())
            with self.assertRaises(ValueError): _=source.required_capability
            before=source.projection_json,source.target(),source.source_sha256
            raw['content']['text']['text']='changed'
            raw['content']['text']['entities'][0]['type']['bank']='hidden'
            self.assertEqual((source.projection_json,source.target(),source.source_sha256),before)

    def test_utf16_original_whole_scalars_and_sanitized_collision_identity(self):
        # Break: slicing normalized text, accepting split surrogates, or hashing sanitized evidence.
        source=self.accepted(message('😀Ａ\tB',[entity(length=3)]))
        self.assertEqual(source.raw_text,'😀Ａ\tB')
        self.assertEqual(source.target().text,display('😀A B',[record('😀A',length=3)]))
        self.assertTrue(source.target().sanitized)
        left=self.accepted(message('Ａ',[entity(length=1)]))
        right=self.accepted(message('A',[entity(length=1)]))
        self.assertEqual(left.target().text,right.target().text)
        self.assertNotEqual(left.source_sha256,right.source_sha256)
        for offset,length in ((0,1),(1,1),(True,2),(-1,2),(0,False),(0,99),(2**31,2)):
            with self.subTest(offset=offset,length=length),self.assertRaises(ValueError):
                source_from_message(message('😀abc',[entity(offset=offset,length=length)]),CHAT,MESSAGE)

    def test_exact_fieldless_shape_and_entity_shell_refuse_unknown_or_coerced_values(self):
        # Break: unchecked type hints, permissive fields, scalar coercion or unknown entity keys.
        for typ in ({'@type':BANK,'bank_card_number':'4111'}, {'@type':BANK,'extra':None},
                    {'@type':BANK,'url':'https://example.invalid'}, {'@type':BANK,'bank':False},
                    {'@type':[BANK]}, {'@type':True}, {'@type':BANK+' '}, BANK, None):
            raw=message();raw['content']['text']['entities'][0]['type']=typ
            with self.subTest(typ=typ),self.assertRaises(ValueError):source_from_message(raw,CHAT,MESSAGE)
        raw=message();raw['content']['text']['entities'][0]['hidden']=1
        with self.assertRaises(ValueError):source_from_message(raw,CHAT,MESSAGE)

    def test_all_present_families_condition_authority_and_full_literal_records(self):
        # Break: losing a present conjunction, adding absent authority, or dropping nested DateTime facts.
        for on in itertools.product((False,True),repeat=6):
            raw=mixed();spans=raw['content']['text']['entities']
            raw['content']['text']['entities']=[spans[0]]+[span for span,enabled in zip(spans[1:],on) if enabled]
            source=self.accepted(raw)
            phone,date,lexical,identity,url,email=on
            required=('reply_formatted_targets','reply_bank_card_targets')
            if phone:required+=('reply_phone_targets',)
            if email:required+=('reply_email_targets',)
            if url:required+=('reply_url_targets',)
            if date:required+=('reply_datetime_targets',)
            if lexical:required+=('reply_lexical_targets',)
            if identity:required+=('reply_identity_targets',)
            self.assertEqual(source.required_capabilities,required)
            records=[record()]
            if phone:records.append({'type':'phone_number','provider_type':'textEntityTypePhoneNumber','offset':3,'length':1,'text':'P'})
            if date:records.append({'type':'date_time','provider_type':'textEntityTypeDateTime','offset':4,'length':1,'text':'D','unix_time':1,'formatting_type':absolute()})
            if lexical:records.append({'type':'hashtag','offset':5,'length':4,'text':'#tag'})
            if identity:records.append({'type':'mention_name','offset':9,'length':5,'text':'@user','user_id':42})
            if url:records.append({'type':'url','provider_type':'textEntityTypeUrl','offset':14,'length':1,'text':'U'})
            if email:records.append({'type':'email_address','provider_type':'textEntityTypeEmailAddress','offset':15,'length':1,'text':'E'})
            self.assertEqual(source.target().text,display('abcPD#tag@userUE',records))
        for fmt in (None,{'@type':'dateTimeFormattingTypeRelative'},absolute()):
            raw=mixed();raw['content']['text']['entities'][2]['type']['formatting_type']=fmt
            source=self.accepted(raw)
            self.assertEqual(json.loads(source.target().text[len(PREFIX):])['entities'][2]['formatting_type'],fmt)

    def test_default_formatter_closed_explicit_bank_card_flag_roundtrips(self):
        # Break: global admission or failing to carry explicit opt-in through validation/rendering.
        raw=message()['content']['text']
        with self.assertRaises(ValueError):formatted_text(raw)
        source=self.accepted(); content=source._validate_projection()['message']['content']
        for operation in (lambda:validate_content(content),lambda:render_formatted_text(raw),
                          lambda:render_content(content,'[untrusted Telegram evidence] ')):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(formatted_text(raw,bank_card=True),raw)
        validate_content(content,bank_card=True)
        self.assertEqual(render_formatted_text(raw,bank_card=True),
            ({'text':'abc','offset_basis':'original source UTF-16 code units','entities':[record()]},False))
        self.assertEqual(render_content(content,'[untrusted Telegram evidence] ',bank_card=True),
            (display('abc',[record()]),False))

    def test_overlap_exclusivity_duplicates_crossings_and_style_containment(self):
        # Break: BankCard is not treated as an exclusive entity, or valid containment is lost.
        for kind in ('Bold','Italic','Underline','Strikethrough','Spoiler'):
            for spans in ([entity(kind,0,5),entity(offset=1,length=3)],
                          [entity(length=5),entity(kind,1,3)], [entity(),entity(kind)]):
                self.accepted(message('abcde',spans))
        for kind,fields in (('BankCardNumber',{}),('PhoneNumber',{}),('EmailAddress',{}),('Url',{}),
                ('Hashtag',{}),('Cashtag',{}),('BotCommand',{}),('Mention',{}),('MentionName',{'user_id':42}),
                ('Code',{}),('Pre',{}),('PreCode',{'language':'py'}),('BlockQuote',{}),
                ('ExpandableBlockQuote',{}),('DateTime',{'unix_time':1,'formatting_type':None})):
            for spans in ([entity(length=5),entity(kind,1,3,**fields)],
                          [entity(kind,0,5,**fields),entity(offset=1,length=3)]):
                with self.subTest(kind=kind),self.assertRaises(ValueError):
                    source_from_message(message('abcde',spans),CHAT,MESSAGE)
        for spans in ([entity(),entity()],[entity(),entity('Bold',2,3)]):
            with self.assertRaises(ValueError):source_from_message(message('abcde',spans),CHAT,MESSAGE)

    def test_forged_old_version_missing_bank_v21_and_media_as_v21_refuse(self):
        # Break: version laundering or admission solely from an unvalidated BankCard hint.
        stored=json.loads(self.accepted().projection_json)
        for version in range(1,21):
            with self.subTest(version=version),self.assertRaises(ValueError):
                ReplySource(encode({**stored,'version':version}))
        for spans in ([],[entity('Bold')],[entity('PhoneNumber')],[entity('EmailAddress')],
                      [entity('Url')],[entity('DateTime',unix_time=1,formatting_type=absolute())]):
            old=source_from_message(message(spans=spans),CHAT,MESSAGE)
            with self.assertRaises(ValueError):ReplySource(encode({**json.loads(old.projection_json),'version':21}))
        for kind in ('document','photo','voice_note'):
            raw=old_message();raw['content']=media(kind,raw['content']['text'])
            old=source_from_message(raw,CHAT,MESSAGE)
            with self.assertRaises(ValueError):ReplySource(encode({**json.loads(old.projection_json),'version':21}))
        stored['message']['content']['text']['entities'][0]['type']['extra']=None
        with self.assertRaises(ValueError):ReplySource(encode(stored))

    def test_hidden_destination_families_remain_closed(self):
        # Break: admitting unrelated hidden destinations during BankCard caption expansion.
        for kind,fields in (('TextUrl',{'url':'https://example.invalid','extra':None}),
                            ('CustomEmoji',{'custom_emoji_id':1}),('MediaTimestamp',{'media_timestamp':1})):
            with self.assertRaises(ValueError):source_from_message(message(spans=[entity(kind,**fields)]),CHAT,MESSAGE)

    def test_complete_escaped_display_scalar_entity_and_projection_bounds(self):
        # Break: truncating evidence, exceeding its complete display bound, or accepting oversized raw input.
        overhead=len(display('',[record('x',length=1)]));text='x'*(4096-overhead)
        source=self.accepted(message(text,[entity(length=1)]))
        self.assertEqual(source.target().text,display(text,[record('x',length=1)]))
        self.assertEqual(len(source.target().text),4096)
        spans=[entity(offset=i,length=1) for i in range(32)]
        self.assertEqual(len(json.loads(self.accepted(message('x'*32,spans)).projection_json)['message']['content']['text']['entities']),32)
        for raw in (message(text+'x',[entity(length=1)]),message('x'*4097),
                message('"'*2100,[entity(length=1)]),message('\ufdfa'*250,[entity(length=250)]),
                message('x'*33,spans+[entity(offset=32,length=1)]),message('\x00abc'),message('\ud800abc')):
            with self.assertRaises(ValueError):source_from_message(raw,CHAT,MESSAGE)
        self.assertEqual(formatted_text(message('x'*4096,[entity(length=1)])['content']['text'],bank_card=True)['text'],'x'*4096)
        with self.assertRaises(ValueError):formatted_text(message('x'*4097,[entity(length=1)])['content']['text'],bank_card=True)
        with self.assertRaises(ValueError):ReplySource('x'*65537)
        with self.assertRaises(ValueError):ReplySource('界'*21846)

    def test_trusted_bank_card_authority_known_explicit_and_default_off(self):
        # Break: granting authority by default or failing to accept an exact trusted opt-in.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700);path=root/'runtime.toml'
            self.assertNotIn('reply_bank_card_targets',load_runtime_policy(path).enabled_capabilities)
            path.write_text('config_version=1\nenabled_capabilities=["reply_bank_card_targets","reply_formatted_targets"]\n')
            path.chmod(0o600)
            try: policy=load_runtime_policy(path)
            except ConfigurationError as error:self.fail('trusted explicit BankCard authority must be recognized: '+str(error))
            self.assertEqual(policy.enabled_capabilities,('reply_bank_card_targets','reply_formatted_targets'))


class BankCardRaw(PhoneNumberRaw):
    """Reuse only the complete synthetic transport boundary, never inherited tests."""
    def __init__(self):
        super().__init__();self.source=mixed()


class BankCardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bankcard-');self.root=Path(self.tmp.name)
        self.raw=BankCardRaw();self.provider=TDLibClient(raw=self.raw,session_directory=self.root/'unused',
            credential_loader=lambda:self.fail('credentials forbidden'))
        self.dialogs=[];self.decision=lambda:True
        def owner(**facts):self.dialogs.append(copy.deepcopy(facts));return self.decision()
        store=ArtifactStore(cache_dir=self.root/'artifacts');payload=self.root/'fixture.txt';payload.write_bytes(b'data')
        self.artifact=store.store(payload)
        self.broker=Broker(socket_path=self.root/'unused.sock',client_factory=lambda:self.provider,
            artifact_store=store,approval_prompt=owner,policy=POLICY)
        self.stack=ExitStack();self.staging=self.root/'staging';self.history=[]
        real_claim=self.broker._drafts.claim
        def claim(*args,**kwargs):
            result=real_claim(*args,**kwargs);self.history.append(('claim',result.draft.draft_id));return result
        self.stack.enter_context(patch.object(self.broker._drafts,'claim',side_effect=claim))
        def stage(claimed):
            result=stage_approved_document(claimed,root=self.staging)
            self.assertTrue(result.is_file());self.history.append(('stage',claimed.draft.draft_id,result));return result
        def retire(path):
            self.assertTrue(path.is_file());retire_staged_document(path,root=self.staging)
            self.assertFalse(path.exists());self.history.append(('retire',path))
        self.stack.enter_context(patch('telegram_search_mcp.tdjson.STAGING_ROOT',self.staging))
        self.stack.enter_context(patch('telegram_search_mcp.broker.stage_approved_document',side_effect=stage))
        self.stack.enter_context(patch('telegram_search_mcp.broker.retire_staged_document',side_effect=retire))

    def tearDown(self):
        self.stack.close();self.broker.shutdown();self.broker._executor.shutdown(wait=True)
        self.provider.close();self.tmp.cleanup()

    def dispatch(self,op,payload):
        return self.broker._dispatch({'operation':op,'payload':payload,'client_id':CLIENT,
            'deadline':time.monotonic()+10,'broker_generation':self.broker._generation})

    def prepare(self,family):
        payload={'recipient':CHAT,'reply_to':{'chat_id':CHAT,'message_id':MESSAGE}}
        if family=='text':payload['text']='Plain';op='prepare_reply_text_send'
        else:
            payload.update(artifact_id=self.artifact.artifact_id,display_name='fixture.txt',mime_type='text/plain',kind='document',caption='Plain')
            op='prepare_reply_artifact_send'
        return self.dispatch(op,payload)

    def prepared(self,family):
        result=self.prepare(family)
        self.assertEqual(result['status'],'prepared','BankCard source must reach immutable draft')
        self.assertIn('"provider_type":"textEntityTypeBankCardNumber"',result['reply']['reply_target']['text'])
        return result['reply']

    def send(self,did,family,approved=True):
        return self.dispatch('send_prepared_text' if family=='text' else 'send_prepared_artifact',{'draft_id':did,'approved':approved})

    def operation(self,verb,family):
        return verb+'_reply_draft' if family=='text' else verb+'_reply_artifact_draft'

    def revise(self,did,family,refresh=False):
        return self.dispatch(self.operation('refresh' if refresh else 'update',family),
            {'draft_id':did,**({} if refresh else {'text' if family=='text' else 'caption':'Changed'})})

    def denied(self,cap):
        return RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=cap))

    def no_effects(self):
        self.assertFalse(any(r['@type']=='sendMessage' for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))

    def test_missing_each_present_authority_blocks_prepare_get_update_refresh_owner_preclaim(self):
        # Break: a lifecycle branch checks only BankCard or skips a present-family conjunct.
        for family in ('text','artifact'):
            for cap in CAPS:
                self.broker._policy=self.denied(cap);self.assertEqual(self.prepare(family)['status'],'unavailable')
                self.broker._policy=POLICY;did=self.prepared(family)['draft']['draft_id'];self.broker._policy=self.denied(cap)
                reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
                self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['status'],'unavailable')
                self.assertEqual(self.revise(did,family)['status'],'unavailable')
                self.assertEqual(self.revise(did,family,True)['status'],'unavailable')
                self.assertEqual(self.send(did,family)['status'],'expired')
                self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
        self.assertEqual(self.dialogs,[]);self.assertEqual(self.history,[]);self.no_effects()

    def test_owner_properties_and_prewire_revocation_refuse_with_actual_staging_retirement(self):
        # Break: post-owner, final-properties or immediate prewire guards omit any present family.
        for family in ('text','artifact'):
            for phase in ('owner','properties','registration'):
                for cap in CAPS:
                    self.broker._policy=POLICY;self.raw.hook=None;self.decision=lambda:True
                    did=self.prepared(family)['draft']['draft_id'];start=len(self.history);denied=self.denied(cap)
                    if phase=='owner':
                        def revoke():self.broker._policy=denied;return True
                        self.decision=revoke
                    elif phase=='properties':
                        self.raw.hook=lambda r:setattr(self.broker,'_policy',denied) if r['@type']=='getMessageProperties' else None
                    if phase=='registration':
                        original=self.provider._send_observations.register
                        def register(*args,**kwargs):original(*args,**kwargs);self.broker._policy=denied
                        with patch.object(self.provider._send_observations,'register',side_effect=register):result=self.send(did,family)
                    else:result=self.send(did,family)
                    self.assertEqual(result['status'],'expired' if phase=='owner' else 'failed')
                    events=self.history[start:]
                    self.assertEqual([e[0] for e in events],[] if phase=='owner' else ['claim'] if family=='text' else ['claim','stage','retire'])
                    self.assertIsNone(self.provider._send_observations.snapshot(did))
                    if phase!='owner':
                        self.assertEqual(events[0],('claim',did))
                        status=self.dispatch('get_send_status',{'draft_id':did})
                        self.assertEqual((status['status'],status['evidence']),('failed','local_failed'))
                        if family=='artifact':
                            self.assertEqual(events[1][1],did);self.assertEqual(events[2],('retire',events[1][2]))
                            self.assertFalse(events[1][2].exists())
        self.no_effects()

    def test_raw_source_drift_and_sanitized_collision_terminal_fail_before_transport(self):
        # Break: digest checks omit original literal/range/provider type or use normalized display identity.
        for family in ('text','artifact'):
            for mutation in ('text','offset','length','type','open','collision'):
                self.raw.source=mixed() if mutation!='collision' else message('Ａ',[entity(length=1)])
                self.broker._policy=POLICY;self.raw.hook=None;self.decision=lambda:True
                preview=self.prepared(family);did=preview['draft']['draft_id'];start=len(self.history)
                def drift():
                    span=self.raw.source['content']['text']['entities'][0]
                    if mutation=='text':self.raw.source['content']['text']['text']='xyzPD#tag@userUE'
                    elif mutation=='offset':span['offset']=1
                    elif mutation=='length':span['length']=2
                    elif mutation=='type':span['type']={'@type':'textEntityTypeBold'}
                    elif mutation=='open':span['type']['bank']='hidden'
                    else:
                        self.raw.source['content']['text']['text']='A'
                        self.assertEqual(source_from_message(self.raw.source,CHAT,MESSAGE).target().text,preview['reply_target']['text'])
                    return True
                self.decision=drift
                self.assertEqual(self.send(did,family)['status'],'failed')
                events=self.history[start:]
                self.assertEqual([e[0] for e in events],['claim'] if family=='text' else ['claim','stage','retire'])
                self.assertEqual(events[0],('claim',did))
                status=self.dispatch('get_send_status',{'draft_id':did})
                self.assertEqual((status['status'],status['evidence']),('failed','local_failed'))
                if family=='artifact':self.assertEqual(events[2],('retire',events[1][2]));self.assertFalse(events[1][2].exists())
                self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
                self.assertEqual(self.send(did,family)['status'],'failed')
                self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['status'],'unavailable')
        self.no_effects()

    def test_update_immutable_refresh_checks_old_new_authority_and_fresh_approval(self):
        # Break: source reread during update, missing old/new authority, or prior approval survives revision.
        legacy_policy=RuntimePolicy(enabled_capabilities=BASE+('reply_formatted_targets','reply_email_targets'))
        for family in ('text','artifact'):
            self.broker._policy=POLICY;self.raw.source=mixed();original=self.prepared(family);did=original['draft']['draft_id']
            reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
            self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['reply'],original)
            self.raw.source=message(spans=[entity('EmailAddress')]);revised=self.revise(did,family)['reply']
            self.assertEqual(revised['reply_target'],original['reply_target'])
            self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
            self.assertEqual(self.send(did,family)['status'],'expired')
            self.broker._policy=legacy_policy
            self.assertEqual(self.revise(revised['draft']['draft_id'],family,True)['status'],'unavailable')
            self.broker._policy=POLICY;older=self.revise(revised['draft']['draft_id'],family,True)['reply']
            self.assertNotEqual(older['reply_target']['source_sha256'],original['reply_target']['source_sha256'])
            self.assertEqual(self.send(revised['draft']['draft_id'],family)['status'],'expired')
            self.raw.source=mixed();self.broker._policy=legacy_policy
            self.assertEqual(self.revise(older['draft']['draft_id'],family,True)['status'],'unavailable')
            self.broker._policy=POLICY;newest=self.revise(older['draft']['draft_id'],family,True)['reply']
            self.assertEqual(newest['reply_target'],original['reply_target'])
            self.assertNotEqual(newest['preview_sha256'],original['preview_sha256'])
            self.assertEqual(self.send(older['draft']['draft_id'],family)['status'],'expired')
            self.assertEqual(self.send(newest['draft']['draft_id'],family,False)['status'],'not_approved')
            self.decision=lambda:False
            self.assertEqual(self.send(newest['draft']['draft_id'],family)['status'],'not_approved')
        self.assertEqual(self.history,[]);self.no_effects()

    def test_exact_plain_wire_once_and_terminal_replay_needs_all_present_authority(self):
        # Break: BankCard initiates lookup/payment/navigation, modifies wire formatting/options or resends.
        for family in ('text','artifact'):
            self.broker._policy=POLICY;self.raw.source=mixed();self.decision=lambda:True
            preview=self.prepared(family);did=preview['draft']['draft_id']
            self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
            sends=[r for r in self.raw.requests if r['@type']=='sendMessage'];wire=sends[-1]
            self.assertEqual(wire['chat_id'],CHAT)
            self.assertEqual(wire['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':MESSAGE,'quote':None,'checklist_task_id':0,'poll_option_id':''})
            self.assertIsNone(wire['topic_id']);self.assertIsNone(wire['reply_markup'])
            self.assertEqual({k:v for k,v in wire['options'].items() if k!='sending_id'}, {
                '@type':'messageSendOptions','suggested_post_info':None,'disable_notification':False,'from_background':False,
                'protect_content':False,'allow_paid_broadcast':False,'paid_message_star_count':0,
                'update_order_of_installed_sticker_sets':False,'scheduling_state':None,'effect_id':0,'only_preview':False})
            incoming=wire['input_message_content']
            self.assertEqual(incoming['text' if family=='text' else 'caption'],{'@type':'formattedText','text':'Plain','entities':[]})
            if family=='text':
                self.assertFalse(incoming['clear_draft'])
                self.assertEqual(incoming['link_preview_options'],{'@type':'linkPreviewOptions','is_disabled':True,'url':'',
                    'force_small_media':False,'force_large_media':False,'show_above_text':False})
            for cap in CAPS:
                self.broker._policy=self.denied(cap);self.assertEqual(self.send(did,family)['status'],'expired')
            self.broker._policy=POLICY;self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(len([r for r in self.raw.requests if r['@type']=='sendMessage']),len(sends))
        self.assertTrue(all(r['@type'] in ('getAuthorizationState','getMe','getChat','getMessage','getMessageProperties','sendMessage') for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))
