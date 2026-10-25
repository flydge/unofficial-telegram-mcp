"""URL media-caption risks: complete literal evidence, authority and plain wire.

Only external raw transport and owner callback are synthetic.
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
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.outgoing_stage import stage_approved_document, retire_staged_document
from telegram_search_mcp.reply_drafts import ReplySource, source_from_message
from telegram_search_mcp.reply_media_sources import media_content, validate_content, render_content
from telegram_search_mcp.tdjson import TDLibClient
from test_reply_url_targets import entity, message, media, mixed, absolute, encode, record, UrlRaw, disabled

CHAT, MESSAGE = 123, 55
CLIENT = 'client_' + 'c' * 24
BASE = ('send', 'reply_text_send', 'reply_artifact_send')
SOURCE_CAPS = ('reply_media_targets', 'reply_formatted_targets', 'reply_url_targets',
               'reply_datetime_targets', 'reply_lexical_targets', 'reply_identity_targets')
POLICY = RuntimePolicy(enabled_capabilities=BASE + SOURCE_CAPS)
# Independent approved wire facts; production constants cannot supply the oracle.
SEND_OPTIONS = {'@type':'messageSendOptions','suggested_post_info':None,
    'disable_notification':False,'from_background':False,'protect_content':False,
    'allow_paid_broadcast':False,'paid_message_star_count':0,
    'update_order_of_installed_sticker_sets':False,'scheduling_state':None,
    'effect_id':0,'only_preview':False}
LINK_OPTIONS = {'@type':'linkPreviewOptions','is_disabled':True,'url':'',
    'force_small_media':False,'force_large_media':False,'show_above_text':False}
MARKER = '[untrusted Telegram evidence] '
KINDS = ('document', 'photo', 'voice_note')
# Literal SHA256 of the fixture's remote unique_id, not computed by source code.
MEDIA_ID = '26e373ec48e9cada4a730cd43b63ccc4e780af3ebc5162b96b983f58db8e7975'
FACTS = {
    'document': {'file_name':'fixture.txt','mime_type':'text/plain','size':4,'unique_id_sha256':MEDIA_ID},
    'photo': {'variants':[{'type':'i','width':2,'height':2,'size':4,'unique_id_sha256':MEDIA_ID}]},
    'voice_note': {'duration':2,'mime_type':'audio/ogg','size':4,'unique_id_sha256':MEDIA_ID},
}


def media_message(kind='document', raw=None):
    raw = copy.deepcopy(message() if raw is None else raw)
    raw['content'] = media(kind, raw['content']['text'])
    return raw


def evidence(kind='document', text='abc', records=None, facts=None):
    return MARKER + 'Media target: ' + encode({'kind':kind,
        'caption':{'text':text,'offset_basis':'original source UTF-16 code units',
                   'entities':[record()] if records is None else records},
        'media':FACTS[kind] if facts is None else facts})


class UrlMediaSourceTests(unittest.TestCase):
    def accepted(self, raw=None):
        try:
            return source_from_message(media_message() if raw is None else raw, CHAT, MESSAGE)
        except ValueError as error:
            self.fail('exact fieldless URL media caption must produce complete v16 evidence: '+str(error))

    def reject(self, raw):
        with self.assertRaises(ValueError): source_from_message(raw, CHAT, MESSAGE)

    def test_v15_simple_and_mixed_canonical_display_and_authority_preserve_baseline(self):
        # Break: media admission changes v15; captured before production edits against 0.40.
        goldens = (
            ('1acd2d5bd941c76de5dde661a78b852f53198c5f2353898218c82163219a4cdb',
             '4f73e401bbb3b945d30bd3cf3223c40e6862897dd1947ed5369c5376280f5773',
             ('reply_formatted_targets','reply_url_targets')),
            ('ceee09bb76154b51d642d56710ccfed6829fe1540a63bd025d6f5deae8c0b2df',
             '7c77144de6116ce031a619a9c1bde608e7672e9ee9a2041a99b34a104000bc54',
             ('reply_formatted_targets','reply_url_targets','reply_datetime_targets','reply_lexical_targets','reply_identity_targets')))
        for raw, (raw_sha, display_sha, caps) in zip((message(),mixed()),goldens):
            source=source_from_message(raw,CHAT,MESSAGE)
            self.assertEqual(source.source_sha256,raw_sha)
            self.assertEqual(hashlib.sha256(source.target().text.encode()).hexdigest(),display_sha)
            self.assertEqual(source.required_capabilities,caps)
            self.assertFalse(source.is_media)

    def test_all_media_complete_literal_evidence_v16_authority_and_immutable_raw(self):
        # Break: missing admission or wrong media facts, coordinates, version or mutable alias.
        for kind in KINDS:
            raw=media_message(kind); source=self.accepted(raw)
            self.assertEqual(json.loads(source.projection_json)['version'],16)
            self.assertTrue(source.is_media)
            self.assertEqual(source.required_capabilities,('reply_media_targets','reply_formatted_targets','reply_url_targets'))
            with self.assertRaises(ValueError): _=source.required_capability
            self.assertEqual(source.target().text,evidence(kind))
            self.assertFalse(source.target().sanitized); self.assertFalse(source.target().truncated)
            before=source.projection_json,source.target()
            raw['content']['caption']['entities'][0]['type']['url']='hidden'
            raw['content']['caption']['text']='changed'
            self.assertEqual((source.projection_json,source.target()),before)
            self.assertEqual(ReplySource(source.projection_json).target(),source.target())

    def test_conditional_conjunctions_and_null_relative_absolute_datetime(self):
        # Break: requiring absent caps, losing present caps or reinterpreting datetime payloads.
        for kind in KINDS:
            for date_on, lexical_on, identity_on in itertools.product((False, True),repeat=3):
                raw=mixed(); spans=raw['content']['text']['entities']
                raw['content']['text']['entities']=[spans[0]]+([spans[1]] if date_on else [])+([spans[2]] if lexical_on else [])+([spans[3]] if identity_on else [])
                source=self.accepted(media_message(kind,raw))
                caps=('reply_media_targets','reply_formatted_targets','reply_url_targets')
                if date_on: caps+=('reply_datetime_targets',)
                if lexical_on: caps+=('reply_lexical_targets',)
                if identity_on: caps+=('reply_identity_targets',)
                self.assertEqual(source.required_capabilities,caps)
                records=[record()]
                if date_on: records.append({'type':'date_time','provider_type':'textEntityTypeDateTime','offset':3,'length':1,'text':'D','unix_time':1,'formatting_type':absolute()})
                if lexical_on: records.append({'type':'hashtag','offset':4,'length':4,'text':'#tag'})
                if identity_on: records.append({'type':'mention_name','offset':8,'length':5,'text':'@user','user_id':42})
                self.assertEqual(source.target().text,evidence(kind,'abcD#tag@user',records))
            for fmt in (None,{'@type':'dateTimeFormattingTypeRelative'},absolute()):
                raw=mixed(); raw['content']['text']['entities'][1]['type']['formatting_type']=fmt
                source=self.accepted(media_message(kind,raw))
                self.assertEqual(json.loads(source.projection_json)['version'],16)
                self.assertEqual(json.loads(source.projection_json)['message']['content']['caption']['entities'][1]['type']['formatting_type'],fmt)
            # All three supported date constructors can coexist, disjoint from URL.
            raw=message('abcNRA',[entity(),entity('DateTime',3,1,unix_time=-1),
                entity('DateTime',4,1,unix_time=0,formatting_type={'@type':'dateTimeFormattingTypeRelative'}),
                entity('DateTime',5,1,unix_time=1,formatting_type=absolute())])
            source=self.accepted(media_message(kind,raw))
            self.assertEqual(source.required_capabilities,('reply_media_targets','reply_formatted_targets','reply_url_targets','reply_datetime_targets'))
            self.assertEqual(source.target().text,evidence(kind,'abcNRA',[record(),
                {'type':'date_time','provider_type':'textEntityTypeDateTime','offset':3,'length':1,'text':'N','unix_time':-1,'formatting_type':None},
                {'type':'date_time','provider_type':'textEntityTypeDateTime','offset':4,'length':1,'text':'R','unix_time':0,'formatting_type':{'@type':'dateTimeFormattingTypeRelative'}},
                {'type':'date_time','provider_type':'textEntityTypeDateTime','offset':5,'length':1,'text':'A','unix_time':1,'formatting_type':absolute()}]))

    def test_default_helper_modes_closed_and_explicit_url_propagates(self):
        # Break: global URL admission or parser/validator/renderer loses URL opt-in.
        for kind in KINDS:
            raw=media_message(kind)['content']; projected={'kind':kind,'caption':raw['caption'],'media':FACTS[kind]}
            with self.assertRaises(ValueError): media_content(raw)
            with self.assertRaises(ValueError): validate_content(projected,styled=True)
            with self.assertRaises(ValueError): render_content(projected,MARKER,styled=True)
            try:
                parsed=media_content(raw,url=True)
            except (ValueError,TypeError) as error:
                self.fail('explicit URL media opt-in must parse complete caption: '+str(error))
            self.assertEqual(parsed,projected)
            validate_content(projected,url=True)
            self.assertEqual(render_content(projected,MARKER,url=True),(evidence(kind),False))
            empty=media_message(kind,message(entities=[]))['content']
            with self.assertRaises(ValueError): media_content(empty,url=True)

    def test_fieldless_shapes_closed_families_and_utf16_refuse(self):
        # Break: open fields, hidden targets, coercion or split-scalar spans.
        for kind in KINDS:
            for typ,fields in (('Url',{'url':'hidden'}),('Url',{'extra':None}),('PhoneNumber',{'phone':'hidden'}),('CustomEmoji',{'custom_emoji_id':1}),('MediaTimestamp',{'media_timestamp':1})):
                self.reject(media_message(kind,message(entities=[entity(typ,**fields)])))
            # 0.58 criterion 4: the TextUrl probe is a listed flip (v24 media TextUrl); the others stay refused.
            flipped=source_from_message(media_message(kind,message(entities=[entity('TextUrl',url='https://example.invalid')])),CHAT,MESSAGE)
            self.assertEqual(json.loads(flipped.projection_json)['version'],24)
            for offset,length in ((0,1),(1,1),(True,2),(-1,2),(0,False),(0,99),(2**31,2)):
                self.reject(media_message(kind,message('😀abc',[entity(offset=offset,length=length)])))
            for level in ('content','caption','entity'):
                raw=media_message(kind)
                obj=raw['content'] if level=='content' else raw['content']['caption'] if level=='caption' else raw['content']['caption']['entities'][0]
                obj['extra']=None; self.reject(raw)
            raw=media_message(kind,message('😀Ａ\tB',[entity(offset=0,length=3)])); source=self.accepted(raw)
            self.assertEqual(source.target().text,evidence(kind,'😀A B',[record('😀A',0,3)]))
            self.assertTrue(source.target().sanitized)
            self.assertEqual(json.loads(source.projection_json)['message']['content']['caption']['text'],'😀Ａ\tB')

    def test_overlap_subset_style_containment_and_exclusive_spans(self):
        # Break: URL overlap relaxes conservative policy or rejects ordinary style containment.
        for kind in KINDS:
            for typ in ('Bold','Italic','Underline','Strikethrough','Spoiler'):
                for spans in ([entity(typ,0,5),entity(offset=1,length=3)],[entity(length=5),entity(typ,1,3)],[entity(),entity(typ)]):
                    self.accepted(media_message(kind,message('abcde',spans)))
            for typ,fields in (('Url',{}),('Hashtag',{}),('Cashtag',{}),('BotCommand',{}),('Mention',{}),('MentionName',{'user_id':42}),('Code',{}),('Pre',{}),('PreCode',{'language':'py'}),('BlockQuote',{}),('ExpandableBlockQuote',{}),('DateTime',{'unix_time':1,'formatting_type':None})):
                for spans in ([entity(length=5),entity(typ,1,3,**fields)],[entity(typ,0,5,**fields),entity(offset=1,length=3)]):
                    self.reject(media_message(kind,message('abcde',spans)))
            for spans in ([entity(),entity()],[entity(),entity('Bold',2,3)]): self.reject(media_message(kind,message('abcde',spans)))

    def test_forged_versions_and_v16_without_url_refuse_all_entries(self):
        # Break: laundering version erases URL/media authority, including bypassed constructor.
        for kind in KINDS:
            stored=json.loads(self.accepted(media_message(kind)).projection_json)
            values=[{**stored,'version':v} for v in (*range(1,16),True,16.0,'16',17)]
            for spans in ([],[entity('Bold')],[entity('Hashtag')],[entity('MentionName',user_id=42)],[entity('DateTime',unix_time=1,formatting_type=absolute())]):
                old=source_from_message(media_message(kind,message(entities=spans)),CHAT,MESSAGE)
                values.append({**json.loads(old.projection_json),'version':16})
            for value in values:
                raw=encode(value); forged=object.__new__(ReplySource); object.__setattr__(forged,'projection_json',raw)
                with self.assertRaises(ValueError): ReplySource(raw)
                with self.assertRaises(ValueError): forged.target()
                with self.assertRaises(ValueError): _=forged.required_capabilities

    def test_caption_entity_projection_and_complete_display_bounds(self):
        # Break: widening source/projection/display limits or truncating review evidence.
        for kind in KINDS:
            text='x'*1024
            self.assertEqual(self.accepted(media_message(kind,message(text,[entity(length=1)]))).target().text,evidence(kind,text,[record('x',0,1)]))
            self.reject(media_message(kind,message(text+'x',[entity(length=1)])))
            spans=[entity(offset=i,length=1) for i in range(32)]
            self.assertEqual(len(json.loads(self.accepted(media_message(kind,message('x'*32,spans))).projection_json)['message']['content']['caption']['entities']),32)
            self.reject(media_message(kind,message('x'*33,spans+[entity(offset=32,length=1)])))
            for text in ('"'*1024,'\ufdfa'*250,'\x00abc','\ud800abc'):
                self.reject(media_message(kind,message(text,[entity(length=len(text))])))
        text='x'*850; spans=[entity('Bold',0,850),entity('Italic',0,850),entity(length=850)]
        records=[{'type':typ,'offset':0,'length':850,'text':text} for typ in ('bold','italic')]+[record(text,0,850)]
        facts={**FACTS['document'],'file_name':'n'}
        facts['file_name']+='n'*(4096-len(evidence('document',text,records,facts)))
        self.assertLessEqual(len(facts['file_name']),255)
        raw=media_message('document',message(text,spans)); raw['content']['document']['file_name']=facts['file_name']
        self.assertEqual(len(self.accepted(raw).target().text),4096)
        self.assertEqual(self.accepted(raw).target().text,evidence('document',text,records,facts))
        raw['content']['document']['file_name']+='n'; self.reject(raw)
        with self.assertRaises(ValueError): ReplySource('x'*65537)
        with self.assertRaises(ValueError): ReplySource('é'*32769)


class UrlMediaDocumentLifecycleTests(unittest.TestCase):
    KIND = "document"

    def fresh_source(self):
        return media_message(self.KIND, mixed())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='url-'); self.root = Path(self.tmp.name)
        self.raw = UrlRaw(); self.raw.source = self.fresh_source(); self.provider = TDLibClient(raw=self.raw, session_directory=self.root/'unused',
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
        result=self.prepare(family); self.assertEqual(result['status'], 'prepared', 'complete URL source must reach immutable draft')
        self.assertIn('"provider_type":"textEntityTypeUrl"', result['reply']['reply_target']['text'])
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
        # Break: checking URL authority only once or authorizing a mixed family with fewer caps.
        for family in ('text','artifact'):
            for missing in SOURCE_CAPS:
                denied=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                self.broker._policy=denied; self.assertEqual(disabled(self.prepare(family)),('capability_disabled',[missing]))
                self.broker._policy=POLICY; did=self.prepared(family)['draft']['draft_id']; self.broker._policy=denied
                reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
                self.assertEqual(disabled(self.dispatch(self.operation('get',family),{'draft_id':did})),('capability_disabled',[missing]))
                self.assertEqual(disabled(self.revise(did,family)),('capability_disabled',[missing]))
                self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
                # A refresh reads the source once and names what that newly read source needs (0.54).
                self.assertEqual(disabled(self.revise(did,family,True)),('capability_disabled',[missing]))
                self.assertEqual(self.send(did,family)['status'],'expired')
                self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads+1)
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
        # Break: ignoring caption/media identity drift or losing claimed/staged failure cleanup.
        for family in ('text','artifact'):
            for mutation in ('text','offset','length','type','open','media'):
                self.raw.source=self.fresh_source(); self.broker._policy=POLICY; self.raw.hook=None
                preview=self.prepared(family); did=preview['draft']['draft_id']
                history_start=len(self.history)
                def drift():
                    span=self.raw.source['content']['caption']['entities'][0]
                    if mutation=='text': self.raw.source['content']['caption']['text']='xyzD#tag@user'
                    elif mutation=='offset': span['offset']=1
                    elif mutation=='length': span['length']=2
                    elif mutation=='type': span['type']={'@type':'textEntityTypeBold'}
                    elif mutation=='open': span['type']['url']='hidden'
                    else:
                        content=self.raw.source['content']
                        file=(content['document']['document'] if self.KIND=='document' else content['photo']['sizes'][0]['photo'] if self.KIND=='photo' else content['voice_note']['voice'])
                        file['remote']['unique_id']='changed-stable-media'
                    return True
                self.decision=drift
                self.assertEqual(self.send(did,family)['status'],'failed')
                events=self.history[history_start:]
                self.assertEqual([event[0] for event in events], ['claim'] if family=='text' else ['claim','stage','retire'])
                self.assertEqual(events[0],('claim',did))
                if family=='artifact':
                    self.assertEqual(events[1][1],did)
                    self.assertEqual(events[2],('retire',events[1][2]))
                    self.assertFalse(events[1][2].exists())
                status=self.dispatch('get_send_status',{'draft_id':did})
                self.assertEqual((status['status'],status['evidence']),('failed','local_failed'))
                self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
                self.assertEqual(self.send(did,family)['status'],'failed')
                self.assertEqual(self.dispatch(self.operation('get',family),{'draft_id':did})['status'],'unavailable')
        self.no_effects()

    def test_update_is_immutable_refresh_requires_new_source_authority_and_invalidates_ids(self):
        # Break: rereading source on update or transferring approval across URL/legacy refresh.
        for family in ('text','artifact'):
            self.broker._policy=POLICY; self.raw.source=self.fresh_source(); original=self.prepared(family)
            did=original['draft']['draft_id']; self.raw.source=media_message(self.KIND,message('abc',[entity('Bold')]))
            reads=sum(r['@type']=='getMessage' for r in self.raw.requests)
            revised=self.revise(did,family)['reply']
            self.assertEqual(revised['reply_target'],original['reply_target'])
            self.assertEqual(sum(r['@type']=='getMessage' for r in self.raw.requests),reads)
            self.assertEqual(self.send(did,family)['status'],'expired')
            # A refresh needs only what the newly read source needs, never the stored source's families (0.54).
            self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_media_targets','reply_formatted_targets'))
            older=self.revise(revised['draft']['draft_id'],family,True)['reply']
            self.broker._policy=POLICY
            self.assertNotEqual(older['reply_target']['source_sha256'],original['reply_target']['source_sha256'])
            self.assertEqual(self.send(revised['draft']['draft_id'],family)['status'],'expired')
            self.raw.source=self.fresh_source(); self.broker._policy=RuntimePolicy(enabled_capabilities=BASE+('reply_media_targets','reply_formatted_targets'))
            self.assertEqual(disabled(self.revise(older['draft']['draft_id'],family,True)),('capability_disabled',list(SOURCE_CAPS[2:])))
            self.broker._policy=POLICY
            newest=self.revise(older['draft']['draft_id'],family,True)['reply']
            self.assertEqual(newest['reply_target'],original['reply_target'])
            self.assertNotEqual(newest['preview_sha256'],original['preview_sha256'])
            self.assertEqual(self.send(older['draft']['draft_id'],family)['status'],'expired')
            self.assertEqual(self.send(newest['draft']['draft_id'],family,False)['status'],'declined')
            self.decision=lambda:False
            self.assertEqual(self.send(newest['draft']['draft_id'],family)['status'],'declined')
        self.assertEqual(self.history, [], 'caller false and owner denial must not claim or stage')
        self.no_effects()

    def test_plain_send_once_terminal_replay_requires_all_source_capabilities(self):
        # Break: source-directed action, formatted outgoing payload, changed anchor/options, or resend.
        for family in ('text','artifact'):
            self.broker._policy=POLICY; self.raw.source=self.fresh_source(); self.decision=lambda:True
            preview=self.prepared(family); did=preview['draft']['draft_id']
            self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(self.dialogs[-1]['reply_preview' if family=='text' else 'reply_artifact_preview'],preview)
            sends=[r for r in self.raw.requests if r['@type']=='sendMessage']; wire=sends[-1]
            self.assertEqual(wire['chat_id'],CHAT)
            self.assertEqual(wire['reply_to'],{'@type':'inputMessageReplyToMessage','message_id':MESSAGE,'quote':None,'checklist_task_id':0,'poll_option_id':''})
            self.assertIsNone(wire['topic_id']); self.assertIsNone(wire['reply_markup'])
            self.assertEqual({k:v for k,v in wire['options'].items() if k!='sending_id'}, SEND_OPTIONS)
            self.assertIs(type(wire['options']['sending_id']), int)
            if family=='text':
                self.assertEqual(wire['input_message_content']['link_preview_options'], LINK_OPTIONS)
                self.assertIs(wire['input_message_content']['clear_draft'], False)
            self.assertEqual(wire['input_message_content']['text' if family=='text' else 'caption'],{'@type':'formattedText','text':'Plain','entities':[]})
            for missing in SOURCE_CAPS:
                self.broker._policy=RuntimePolicy(enabled_capabilities=tuple(c for c in POLICY.enabled_capabilities if c!=missing))
                self.assertEqual(self.send(did,family)['status'],'expired')
            self.broker._policy=POLICY; self.assertEqual(self.send(did,family)['status'],'sent')
            self.assertEqual(len([r for r in self.raw.requests if r['@type']=='sendMessage']),len(sends))
        self.assertTrue(all(r['@type'] in ('getAuthorizationState','getMe','getChat','getMessage','getMessageProperties','sendMessage') for r in self.raw.requests))
        self.assertFalse(self.staging.exists() and any(self.staging.iterdir()))


class UrlMediaPhotoLifecycleTests(UrlMediaDocumentLifecycleTests):
    KIND = 'photo'


class UrlMediaVoiceLifecycleTests(UrlMediaDocumentLifecycleTests):
    KIND = 'voice_note'
