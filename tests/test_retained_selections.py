from __future__ import annotations

import copy
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from telegram_search_mcp.artifact_authority import ArtifactOwner
from test_message_reader import message


def countdown_bad_values():
    return (1,1.0,-1,-0.1,True,False,None,'0',float('nan'),float('inf'),float('-inf'),{},[])


class RetainedProjectionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.selection_projection'),
                             'exact retained projection is missing')
        self.module = importlib.import_module('telegram_search_mcp.selection_projection')
        self.raw = message(11)

    def digest(self, raw):
        return self.module.message_projection(raw, (7, 11))[0]

    def test_entity_only_hidden_url_drift_changes_private_fingerprint(self):
        self.raw['content']['text']['entities'] = [
            {'offset':0, 'length':3, 'type':{'@type':'textEntityTypeTextUrl','url':'https://example.test/a'}}]
        first = self.digest(self.raw)
        self.raw['content']['text']['entities'][0]['type']['url'] = 'https://example.test/b'
        self.assertNotEqual(first, self.digest(self.raw))

    def test_raw_text_after_display_limit_and_sanitization_equivalent_drift(self):
        self.raw['content']['text']['text'] = 'a' * 21000
        first = self.digest(self.raw)
        self.raw['content']['text']['text'] += 'x'
        self.assertNotEqual(first, self.digest(self.raw))
        self.raw['content']['text']['text'] = 'a\x00'
        first = self.digest(self.raw)
        self.raw['content']['text']['text'] = 'a'
        self.assertNotEqual(first, self.digest(self.raw))

    def test_volatile_fields_are_excluded_but_topic_sender_and_album_are_bound(self):
        first = self.digest(self.raw)
        self.raw.update(interaction_info={'view_count':92}, is_unread=True)
        self.assertEqual(first, self.digest(self.raw))
        for key,value in [('sender_id', {'@type':'messageSenderUser','user_id':18}),
                          ('topic_id', {'@type':'messageTopicForum','forum_topic_id':3}),
                          ('media_album_id','99')]:
            changed=copy.deepcopy(self.raw); changed[key]=value
            self.assertNotEqual(first, self.digest(changed))

    def test_oversize_invalid_utf16_and_timed_sources_refuse_without_prefix_hash(self):
        invalid=[]
        too_big=copy.deepcopy(self.raw); too_big['content']['text']['text']='a'*65536; invalid.append(too_big)
        utf=copy.deepcopy(self.raw); utf['content']['text']={'text':'😀','entities':[
            {'offset':1,'length':1,'type':{'@type':'textEntityTypeBold'}}]}; invalid.append(utf)
        timed=copy.deepcopy(self.raw);timed['self_destruct_type']={'@type':'messageSelfDestructTypeTimer','self_destruct_time':10};invalid.append(timed)
        for raw in invalid:
            with self.subTest(raw_kind=next(iter(raw))):
                with self.assertRaises(self.module.SelectionUnavailable): self.digest(raw)

    def test_media_binds_stable_remote_and_selected_variant_not_progress(self):
        self.raw['content']={'@type':'messageDocument','caption':{'text':'c','entities':[]},
            'document':{'file_name':'a.txt','mime_type':'text/plain','document':{'@type':'file','id':8,
            'size':3,'remote':{'unique_id':'stable-a'},'local':{'downloaded_size':0,'path':''}}}}
        first=self.digest(self.raw)
        self.raw['content']['document']['document']['local']['downloaded_size']=3
        self.assertEqual(first,self.digest(self.raw))
        self.raw['content']['document']['document']['remote']['unique_id']='stable-b'
        self.assertNotEqual(first,self.digest(self.raw))
        self.raw['content']['document']['document']['remote']['unique_id']=''
        with self.assertRaises(self.module.SelectionUnavailable):self.digest(self.raw)


    def test_independent_countdowns_refuse_even_without_or_with_null_self_destruct_type(self):
        for key in ('self_destruct_in','auto_delete_in'):
            for type_present in (False,True):
                for value in countdown_bad_values():
                    with self.subTest(key=key,type_present=type_present,value=repr(value)):
                        raw=copy.deepcopy(self.raw);raw[key]=value
                        if type_present:raw['self_destruct_type']=None
                        with self.assertRaises(self.module.SelectionUnavailable):self.digest(raw)

    def test_absent_and_exact_numeric_zero_countdowns_are_not_fingerprint_facts(self):
        first=self.digest(self.raw)
        for self_value in (0,0.0,-0.0):
            for auto_value in (0,0.0,-0.0):
                for type_present in (False,True):
                    with self.subTest(self_value=self_value,auto_value=auto_value,type_present=type_present):
                        raw=copy.deepcopy(self.raw)
                        raw.update(self_destruct_in=self_value,auto_delete_in=auto_value)
                        if type_present:raw['self_destruct_type']=None
                        self.assertEqual(self.digest(raw),first)
        raw=copy.deepcopy(self.raw);raw['self_destruct_type']={'@type':'messageSelfDestructTypeTimer','self_destruct_time':10}
        with self.assertRaises(self.module.SelectionUnavailable):self.digest(raw)

    def test_attachment_projection_checks_independent_countdowns_before_media_facts(self):
        self.raw['content']={'@type':'messageDocument','caption':{'text':'c','entities':[]},
            'document':{'file_name':'a.txt','mime_type':'text/plain','document':{'@type':'file','id':8,
                'size':3,'remote':{'unique_id':'stable-a'}}}}
        first=self.digest(self.raw)
        raw=copy.deepcopy(self.raw);raw.update(self_destruct_in=0,auto_delete_in=0.0,self_destruct_type=None)
        self.assertEqual(self.digest(raw),first)
        for key in ('self_destruct_in','auto_delete_in'):
            for value in countdown_bad_values():
                with self.subTest(key=key,value=repr(value)):
                    unsafe=copy.deepcopy(raw);unsafe[key]=value
                    with self.assertRaises(self.module.SelectionUnavailable):self.digest(unsafe)
                    with self.assertRaises(self.module.SelectionUnavailable):self.module.attachment_facts(unsafe)


class SelectionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('telegram_search_mcp.retained_selections'),
                             'retained selection registry is missing')
        self.module=importlib.import_module('telegram_search_mcp.retained_selections')
        self.now=100.;self.wall=datetime(2030,1,1,tzinfo=timezone.utc)
        self.owner=ArtifactOwner('client_a','broker_a',object())
        self.registry=self.module.SelectionRegistry(clock=lambda:self.now,wall_clock=lambda:self.wall)

    def issue(self, source='a', cap=None):
        with self.registry.reserve(self.owner) as started:
            return self.registry.issue(self.owner, started, kind='messages', source_key=source,
                facts=('hash',source), anchors=((7,11),), expires_at=cap)

    def test_dedupe_preserves_token_and_original_dual_expiry(self):
        first=self.issue();self.now+=20;self.wall+=timedelta(seconds=20)
        again=self.issue();self.assertIs(first,again)
        self.now=400
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)
        replacement=self.issue();self.assertNotEqual(first.selection_id,replacement.selection_id)

    def test_foreign_lifecycle_release_and_wall_expiry_never_resurrect(self):
        first=self.issue()
        for owner in [ArtifactOwner('client_b','broker_a',object()),ArtifactOwner('client_a','broker_a',object())]:
            with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,owner)
        self.registry.release('client_a')
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)
        first=self.issue();self.wall+=timedelta(seconds=300)
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)
        self.wall-=timedelta(seconds=300)
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)

    def test_capacity_pending_and_active_are_bounded_without_eviction(self):
        for i in range(16):self.issue(str(i))
        with self.assertRaises(self.module.SelectionUnavailable):self.issue('17')
        self.assertIsNotNone(self.registry.get(self.issue_existing_id(),self.owner))
        self.registry.release('client_a')
        with self.registry.reserve(self.owner):
            with self.registry.reserve(self.owner):
                with self.registry.reserve(self.owner):
                    with self.registry.reserve(self.owner):
                        with self.assertRaises(self.module.SelectionUnavailable):
                            with self.registry.reserve(self.owner):pass

    def issue_existing_id(self):
        return next(iter(self.registry._entries))

    def test_target_or_artifact_cap_never_extends_selection(self):
        first=self.issue(cap=self.wall+timedelta(seconds=12))
        self.assertEqual(first.expires_at,self.wall+timedelta(seconds=12))
        self.now+=12
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)

    def test_full_capacity_still_allows_exact_unchanged_recapture(self):
        first=self.issue('existing')
        for i in range(15):self.issue(str(i))
        with self.registry.reserve(self.owner,first.selection_id) as started:
            same=self.registry.issue(self.owner,started,kind='messages',source_key='existing',
                facts=('hash','existing'),anchors=((7,11),))
        self.assertIs(same,first)

    def test_changed_recapture_at_capacity_replaces_revoked_source_without_eviction(self):
        first=self.issue('existing')
        unrelated=[self.issue(str(i)) for i in range(15)]
        with self.registry.reserve(self.owner,first.selection_id) as started:
            changed=self.registry.issue(self.owner,started,kind='messages',source_key='existing',
                facts=('different',),anchors=((7,11),))
        self.assertNotEqual(changed.selection_id,first.selection_id)
        self.assertEqual(len(self.registry._entries),16)
        with self.assertRaises(self.module.SelectionUnavailable):self.registry.get(first.selection_id,self.owner)
        for binding in unrelated:self.assertIs(self.registry.get(binding.selection_id,self.owner),binding)


class RetainedBrokerTests(unittest.TestCase):
    from test_artifact_ownership import OwnershipTests as _Helper
    dispatch=_Helper.dispatch
    create=_Helper.create
    cleanup=_Helper.cleanup

    def setUp(self):
        from telegram_search_mcp.config import RuntimePolicy
        from test_verified_targets import TargetProvider
        self._Helper.setUp(self)
        self.provider=TargetProvider()
        self.provider.send_observation_epoch=object()
        self.broker._client_factory=lambda:self.provider
        self.broker._policy=RuntimePolicy(enabled_capabilities=('artifacts','read','send','read_messages',
            'verified_targets','read_reply_chain','attachment_pages','spreadsheets','presentations',
            'reply_text_send','reply_artifact_send','retained_selections'))

    def retain(self, source):
        return self.dispatch('retain_selection', {'source':source})

    def selected(self, op, token, **kwargs):
        return self.dispatch(op, {'selection_id':token,**kwargs})

    def unavailable(self, result):
        self.assertEqual(result,{'status':'selection_unavailable','detail':'Selection is unavailable; select the source again.'})

    def test_capture_exact_order_read_and_entity_drift_atomically_refuses(self):
        result=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':12},{'chat_id':7,'message_id':11}]})
        self.assertEqual(result['status'],'retained')
        token=result['selection_id']
        self.assertEqual([r['anchor']['message_id'] for r in result['messages']['results']],[12,11])
        self.assertEqual(self.selected('read_messages',token)['status'],'complete')
        self.provider.messages[11]['content']['text']['entities']=[
            {'offset':0,'length':3,'type':{'@type':'textEntityTypeTextUrl','url':'https://hidden.test'}}]
        self.unavailable(self.selected('read_messages',token))
        self.provider.messages[11]['content']['text']['entities']=[]
        self.unavailable(self.selected('read_messages',token))

    def test_local_file_retention_and_all_local_readers_do_no_provider_work(self):
        from xlsx_fixtures import write_xlsx
        from pptx_fixtures import make_pptx
        items=[(self.create(),'read_attachment',{}),
            (self.create(),'read_attachment_page',{'max_chars':3,'render_pages':False}),
            (self.create('book.xlsx',write_xlsx(self.root/'book.xlsx').read_bytes()),'read_spreadsheet',{}),
            (self.create('slides.pptx',make_pptx(self.root/'slides.pptx').read_bytes()),'read_presentation',{})]
        for item,op,args in items:
            retained=self.retain({'kind':'file','artifact_id':item['artifact_id']})
            self.assertEqual(retained['status'],'retained');self.assertEqual(retained['file']['provenance'],'local')
            output=self.selected(op,retained['selection_id'],**args)
            self.assertIn(output['status'],('complete','page','catalog'))
            if output.get('next_cursor'):
                follow=self.selected(op,retained['selection_id'],**args,cursor=output['next_cursor'])
                self.assertIn(follow['status'],('page','complete'))
        self.assertEqual(self.provider.calls,[])

    def test_foreign_wrong_kind_singleton_release_and_reuse_refuse_before_provider(self):
        from test_artifact_ownership import B
        result=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11},{'chat_id':7,'message_id':12}]})
        token=result['selection_id'];self.provider.calls.clear()
        self.unavailable(self.dispatch('read_messages',{'selection_id':token},B))
        self.unavailable(self.selected('read_attachment',token))
        self.unavailable(self.selected('get_attachment',token))
        self.assertEqual(self.provider.calls,[])
        self.dispatch('release_client',{})
        self.unavailable(self.selected('read_messages',token));self.assertEqual(self.provider.calls,[])

    def test_unknown_selection_does_not_initialize_provider_or_session(self):
        self.broker._client_factory=lambda:self.fail('unavailable selection initialized the provider')
        self.unavailable(self.selected('read_messages','selection_'+'e'*64))

    def test_file_recapture_at_live_capacity_reuses_original_token_and_expiry(self):
        captures=[]
        for i in range(16):
            file=self.create(data=str(i).encode())
            captures.append((file,self.retain({'kind':'file','artifact_id':file['artifact_id']})))
        original=captures[0][1]
        again=self.retain({'kind':'file','artifact_id':captures[0][0]['artifact_id']})
        self.assertEqual(again['status'],'retained')
        self.assertEqual((again['selection_id'],again['expires_at']),(original['selection_id'],original['expires_at']))

    def test_account_switch_back_does_not_resurrect_selection(self):
        result=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        self.provider.account=18;self.unavailable(self.selected('read_messages',result['selection_id']))
        self.provider.account=17;self.unavailable(self.selected('read_messages',result['selection_id']))

    def test_target_capture_capped_by_original_target_and_preserves_dependency(self):
        target=self.dispatch('verify_target',{'target':7})
        retained=self.retain({'kind':'target_messages','target_handle':target['target_handle'],'message_ids':[11]})
        self.assertEqual(retained['status'],'retained')
        self.assertLessEqual(retained['expires_at'],target['expires_at'])
        self.broker._contexts[next(iter(self.broker._contexts))].service.close_verified_targets()
        self.unavailable(self.selected('read_messages',retained['selection_id']))

    def test_capture_or_extraction_release_suppresses_whole_response(self):
        def release(name):
            if name=='sender':self.dispatch('release_client',{})
        self.provider.hook=release
        self.unavailable(self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}))
        self.provider.hook=lambda _:None
        file=self.create();retained=self.retain({'kind':'file','artifact_id':file['artifact_id']})
        from unittest.mock import patch
        from telegram_search_mcp.broker import read_document
        def interrupted(*args,**kwargs):
            result=read_document(*args,**kwargs);self.dispatch('release_client',{});return result
        with patch('telegram_search_mcp.broker.read_document',side_effect=interrupted):
            self.unavailable(self.selected('read_attachment',retained['selection_id']))

    def document(self):
        self.provider.messages[11]['content']={'@type':'messageDocument','caption':{'text':'caption','entities':[]},
            'document':{'file_name':'private.txt','mime_type':'text/plain','document':{'@type':'file','id':8,
                'size':3,'remote':{'unique_id':'stable-document'}}}}
        self.provider.download_file=lambda *args,**kwargs:self.source

    def test_download_provenance_then_source_change_blocks_file_process(self):
        self.document()
        attachment=self.dispatch('get_attachment',{'anchor':{'chat_id':7,'message_id':11}})
        self.assertEqual(attachment['status'],'complete')
        retained=self.retain({'kind':'file','artifact_id':attachment['artifact_id']})
        self.assertEqual(retained['status'],'retained');self.assertEqual(retained['file']['provenance'],'telegram')
        self.assertEqual(self.selected('read_attachment',retained['selection_id'])['text'],'abc')
        self.provider.messages[11]['content']['document']['file_name']='different.txt'
        self.unavailable(self.selected('read_attachment',retained['selection_id']))

    def test_download_drift_suppresses_handle_and_missing_identity_cannot_be_retained(self):
        self.document()
        def drift(*args,**kwargs):
            self.provider.messages[11]['content']['caption']['text']='changed'
            return self.source
        self.provider.download_file=drift
        result=self.dispatch('get_attachment',{'anchor':{'chat_id':7,'message_id':11}})
        self.assertNotEqual(result['status'],'complete');self.assertIsNone(result.get('artifact_id'))
        self.document();self.provider.messages[11]['content']['document']['document']['remote']['unique_id']=''
        attachment=self.dispatch('get_attachment',{'anchor':{'chat_id':7,'message_id':11}})
        self.assertEqual(attachment['status'],'complete')
        self.unavailable(self.retain({'kind':'file','artifact_id':attachment['artifact_id']}))

    def test_selected_continuation_binds_selection_id_and_fixed_expiry(self):
        file=self.create(data=b'abcdefghi');retained=self.retain({'kind':'file','artifact_id':file['artifact_id']})
        initial=self.selected('read_attachment_page',retained['selection_id'],max_chars=2,render_pages=False)
        self.assertEqual(initial['scope']['selection_id'],retained['selection_id'])
        self.assertLessEqual(initial['scope']['expires_at'],retained['expires_at'])
        raw=self.dispatch('read_attachment_page',{'artifact_id':file['artifact_id'],'max_chars':2,
            'render_pages':False,'cursor':initial['next_cursor']})
        self.assertNotIn(raw['status'],('page','complete'))

    def test_prepare_expiry_during_preview_removes_unpublished_draft(self):
        self.document()
        file=self.create();retained=self.retain({'kind':'file','artifact_id':file['artifact_id']})
        from unittest.mock import patch
        original=self.broker._drafts.prepare
        def expire(**kwargs):
            candidate=original(**kwargs)
            self.broker._selections.invalidate(retained['selection_id'])
            return candidate
        with patch.object(self.broker._drafts,'prepare',side_effect=expire):
            self.unavailable(self.selected('prepare_artifact_send',retained['selection_id'],recipient=7,
                display_name='private.txt',mime_type='text/plain'))
        self.assertEqual(self.broker._drafts.known_ids(),set())

    def test_transient_changed_raw_return_cannot_publish_then_revert(self):
        retained=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        original=self.provider.get_message;calls=[0]
        def transient(chat,mid):
            calls[0]+=1;raw=original(chat,mid)
            if calls[0]==2:raw['content']['text']['text']='transient edited body'
            return raw
        self.provider.get_message=transient
        self.unavailable(self.selected('read_messages',retained['selection_id']))

    def test_failed_reply_preview_removes_inserted_unpublished_text_draft(self):
        from unittest.mock import patch
        from telegram_search_mcp.reply_drafts import source_from_message
        from telegram_search_mcp.tdjson import TDLibError
        from test_text_replies import target
        self.provider.messages[11]=target(chat_id=7,id=11,media_album_id=0,
            sender_id={'@type':'messageSenderUser','user_id':17})
        self.provider._request_context=__import__('threading').local()
        self.provider.read_reply_source=lambda chat,mid,**kwargs:source_from_message(self.provider.get_message(chat,mid),chat,mid)
        retained=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        observed=[]
        def failed(*args):
            observed.append(True);raise TDLibError('synthetic preview failure')
        with patch.object(self.broker,'_reply_preview',side_effect=failed):
            result=self.dispatch('prepare_reply_text_send',{'reply_selection_id':retained['selection_id'],
                'recipient':7,'text':'approved draft text'})
        self.assertNotEqual(result['status'],'prepared')
        self.assertEqual(observed,[True])
        self.assertEqual(self.broker._drafts.known_ids(),set())

    def test_reply_source_transient_change_cannot_prepare_mismatched_preview(self):
        from test_text_replies import target
        from telegram_search_mcp.reply_drafts import source_from_message
        self.provider.messages[11]=target(chat_id=7,id=11,media_album_id=0,
            sender_id={'@type':'messageSenderUser','user_id':17})
        self.provider._request_context=__import__('threading').local()
        retained=self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        changed=copy.deepcopy(self.provider.messages[11]);changed['content']['text']['text']='transient different source'
        self.provider.read_reply_source=lambda chat,mid,**kwargs:source_from_message(changed,chat,mid)
        self.unavailable(self.dispatch('prepare_reply_text_send',{'reply_selection_id':retained['selection_id'],
            'recipient':7,'text':'outgoing text'}))
        self.assertEqual(self.broker._drafts.known_ids(),set())

    def test_capture_edit_during_sender_projection_discards_earlier_raw_body(self):
        def edit(name):
            if name=='sender':self.provider.messages[11]['content']['text']['text']='edited during capture'
        self.provider.hook=edit
        self.unavailable(self.retain({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}))

    def test_signed_group_anchor_and_aggregate_raw_bound(self):
        self.provider.chat={'@type':'chat','id':-7,'title':'Synthetic group',
            'type':{'@type':'chatTypeSupergroup','supergroup_id':17,'is_channel':False}}
        self.provider.messages[11]['chat_id']=-7
        self.provider.get_message=lambda chat,mid:copy.deepcopy(self.provider.messages[mid])
        result=self.retain({'kind':'messages','anchors':[{'chat_id':-7,'message_id':11}]})
        self.assertEqual(result['status'],'retained')
        self.assertEqual(self.selected('read_messages',result['selection_id'])['results'][0]['anchor']['chat_id'],-7)
        for mid in range(11,16):
            self.provider.messages[mid]['chat_id']=-7
            self.provider.messages[mid]['content']['text']['text']='x'*21000
        self.unavailable(self.retain({'kind':'messages','anchors':[{'chat_id':-7,'message_id':mid} for mid in range(11,16)]}))

    def test_raw_derived_output_inherits_original_provenance_for_retention(self):
        from unittest.mock import patch
        from telegram_search_mcp.document_reader import DocumentReadResult
        file=self.create('a.pdf',b'pdf fixture')
        directory=self.root/'generated-preview';directory.mkdir()
        preview=directory/'preview.png';preview.write_bytes(b'png fixture')
        with patch('telegram_search_mcp.broker.read_document',return_value=DocumentReadResult(
                status='complete',text='',processed_bytes=0,total_bytes=11,processed_pages=1,total_pages=1,
                image_paths=[preview])):
            result=self.dispatch('read_attachment',{'artifact_id':file['artifact_id']})
        self.assertEqual(result['status'],'complete')
        retained=self.retain({'kind':'file','artifact_id':result['images'][0]['artifact_id']})
        self.assertEqual(retained['status'],'retained');self.assertEqual(retained['file']['provenance'],'derived')


    def test_capture_publication_window_release_expiry_and_target_revocation_refuse(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import time
        from test_artifact_ownership import A
        for race in ('release','expiry','target'):
            with self.subTest(race=race):
                if race=='release':
                    file=self.create();source={'kind':'file','artifact_id':file['artifact_id']}
                elif race=='target':
                    target=self.dispatch('verify_target',{'target':7})
                    source={'kind':'target_messages','target_handle':target['target_handle'],'message_ids':[11]}
                else:source={'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}
                original=self.broker._selection_fence;published=[];observed=[]
                clock=self.broker._selections.clock
                def no_locked_provider(name):
                    self.assertFalse(self.broker._artifact_authority.lock._is_owned())
                    self.assertFalse(self.broker._selections.lock._is_owned())
                self.provider.hook=no_locked_provider
                @contextmanager
                def interrupted(scope,bindings):
                    observed.extend(self.broker._selections._entries)
                    if race=='release':self.broker._artifact_authority.release(A)
                    elif race=='expiry':self.broker._selections.clock=lambda:time.monotonic()+301
                    else:
                        reader=next(iter(self.broker._contexts.values())).service._verified_target_reader
                        reader.close()
                    with original(scope,bindings):yield
                request={'operation':'retain_selection','payload':{'source':source},'client_id':A,
                    'deadline':time.monotonic()+30,'broker_generation':self.broker._generation}
                try:
                    with patch.object(self.broker,'_selection_fence',side_effect=interrupted):
                        result=self.broker._dispatch_selection(request,publish=published.append)
                finally:self.broker._selections.clock=clock
                self.unavailable(result);self.assertEqual(published,[result])
                self.assertTrue(observed)
                self.assertTrue(all(token not in self.broker._selections._entries for token in observed))
        self.provider.hook=lambda _:None

    def test_empty_selection_fence_still_checks_scope_liveness(self):
        from telegram_search_mcp.artifact_authority import ArtifactUnavailable
        from test_artifact_ownership import A
        with self.broker._artifact_authority.scope(A,self.store) as scope:
            self.broker._artifact_authority.release(A)
            with self.assertRaises(ArtifactUnavailable):
                with self.broker._selection_fence(scope,[]):self.fail('released empty fence admitted')

    def full_capacity_message(self):
        source={'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}
        first=self.retain(source)
        unrelated=[]
        for i in range(15):
            file=self.create(data=str(i).encode())
            unrelated.append(self.retain({'kind':'file','artifact_id':file['artifact_id']}))
        self.assertEqual(len(self.broker._selections._entries),16)
        return source,first,unrelated

    def test_changed_recapture_then_revert_never_resurrects_at_full_capacity(self):
        source,first,unrelated=self.full_capacity_message()
        original=copy.deepcopy(self.provider.messages[11])
        self.provider.messages[11]['content']['text']['text']='observed changed source'
        changed=self.retain(source)
        self.assertEqual(changed['status'],'retained')
        self.assertNotEqual(changed['selection_id'],first['selection_id'])
        self.assertEqual(len(self.broker._selections._entries),16)
        self.provider.messages[11]=original;self.provider.calls.clear()
        self.unavailable(self.selected('read_messages',first['selection_id']))
        self.assertEqual(self.provider.calls,[])
        for row in unrelated:self.assertIn(row['selection_id'],self.broker._selections._entries)
        restored=self.retain(source)
        self.assertEqual(restored['status'],'retained')
        self.assertNotIn(restored['selection_id'],[first['selection_id'],changed['selection_id']])
        self.assertEqual(len(self.broker._selections._entries),16)

    def test_unsafe_or_missing_recapture_refusal_then_restore_never_resurrects_at_capacity(self):
        from telegram_search_mcp.tdjson import MessageNotFound
        for failure in ('unsafe','missing'):
            with self.subTest(failure=failure):
                self.broker._selections._entries.clear()
                source,first,unrelated=self.full_capacity_message()
                original=copy.deepcopy(self.provider.messages[11])
                if failure=='unsafe':self.provider.messages[11]['self_destruct_type']={'@type':'messageSelfDestructTypeTimer','self_destruct_time':10}
                else:self.provider.messages[11]=MessageNotFound('synthetic missing')
                self.unavailable(self.retain(source))
                self.assertNotIn(first['selection_id'],self.broker._selections._entries)
                self.assertEqual(len(self.broker._selections._entries),15)
                self.provider.messages[11]=original;self.provider.calls.clear()
                self.unavailable(self.selected('read_messages',first['selection_id']))
                self.assertEqual(self.provider.calls,[])
                restored=self.retain(source)
                self.assertEqual(restored['status'],'retained')
                self.assertNotEqual(restored['selection_id'],first['selection_id'])
                for row in unrelated:self.assertIn(row['selection_id'],self.broker._selections._entries)

    def test_pure_active_capacity_refusal_keeps_unchanged_and_unrelated_tokens(self):
        from contextlib import ExitStack
        from test_artifact_ownership import A
        source,first,unrelated=self.full_capacity_message()
        before=self.broker._selections._entries.copy();self.provider.calls.clear()
        with self.broker._artifact_authority.scope(A,self.store) as scope,ExitStack() as stack:
            for row in [first,*unrelated[:3]]:
                stack.enter_context(self.broker._selections.reserve(scope.owner,row['selection_id']))
            self.unavailable(self.retain(source))
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.broker._selections._entries,before)
        again=self.retain(source)
        self.assertEqual((again['selection_id'],again['expires_at']),(first['selection_id'],first['expires_at']))

    def test_changed_file_recapture_then_metadata_restore_never_resurrects(self):
        file=self.create();source={'kind':'file','artifact_id':file['artifact_id']};first=self.retain(source)
        original=self.broker._artifact_metadata[file['artifact_id']]
        self.broker._artifact_metadata[file['artifact_id']]=(original[0],'changed.txt',original[2],original[3])
        changed=self.retain(source);self.assertEqual(changed['status'],'retained')
        self.broker._artifact_metadata[file['artifact_id']]=original
        self.unavailable(self.selected('read_attachment',first['selection_id']))
        self.assertNotEqual(first['selection_id'],changed['selection_id'])


    def test_countdown_initial_capture_refuses_before_sender_or_evidence_render(self):
        source={'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}
        original=copy.deepcopy(self.provider.messages[11])
        for key in ('self_destruct_in','auto_delete_in'):
            for type_present in (False,True):
                for value in countdown_bad_values():
                    with self.subTest(key=key,type_present=type_present,value=repr(value)):
                        self.provider.messages[11]=copy.deepcopy(original);self.provider.messages[11][key]=value
                        if type_present:self.provider.messages[11]['self_destruct_type']=None
                        self.provider.calls.clear()
                        self.unavailable(self.retain(source))
                        self.assertFalse(any(call[0]=='sender' for call in self.provider.calls))
                        self.assertEqual(self.broker._selections._entries,{})
        self.provider.messages[11]=original
        retained=self.retain(source);self.assertEqual(retained['status'],'retained')
        self.provider.messages[11].update(self_destruct_in=0.0,auto_delete_in=0,self_destruct_type=None)
        same=self.retain(source)
        self.assertEqual((same['selection_id'],same['expires_at']),(retained['selection_id'],retained['expires_at']))

    def test_countdown_message_use_refuses_before_download_and_never_resurrects(self):
        from unittest.mock import Mock
        self.document();original=copy.deepcopy(self.provider.messages[11])
        source={'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}
        for key in ('self_destruct_in','auto_delete_in'):
            for type_present in (False,True):
                for value in countdown_bad_values():
                    with self.subTest(key=key,type_present=type_present,value=repr(value)):
                        self.provider.messages[11]=copy.deepcopy(original)
                        retained=self.retain(source);self.assertEqual(retained['status'],'retained')
                        self.provider.messages[11][key]=value
                        if type_present:self.provider.messages[11]['self_destruct_type']=None
                        download=Mock(side_effect=AssertionError('unsafe selected source downloaded'))
                        self.provider.download_file=download;self.provider.calls.clear()
                        self.unavailable(self.selected('get_attachment',retained['selection_id']))
                        download.assert_not_called()
                        self.assertFalse(any(call[0]=='sender' for call in self.provider.calls))
                        self.provider.messages[11]=copy.deepcopy(original);self.provider.calls.clear()
                        self.unavailable(self.selected('get_attachment',retained['selection_id']))
                        self.assertEqual(self.provider.calls,[]);download.assert_not_called()

    def test_countdown_owned_file_initial_capture_and_use_refuse_before_extraction(self):
        from unittest.mock import Mock,patch
        self.document()
        attachment=self.dispatch('get_attachment',{'anchor':{'chat_id':7,'message_id':11}})
        self.assertEqual(attachment['status'],'complete')
        source={'kind':'file','artifact_id':attachment['artifact_id']}
        original=copy.deepcopy(self.provider.messages[11])
        for key in ('self_destruct_in','auto_delete_in'):
            for value in countdown_bad_values():
                with self.subTest(key=key,value=repr(value)):
                    self.provider.messages[11]=copy.deepcopy(original);self.provider.messages[11][key]=value
                    self.provider.messages[11]['self_destruct_type']=None
                    self.unavailable(self.retain(source))
                    self.provider.messages[11]=copy.deepcopy(original)
                    retained=self.retain(source);self.assertEqual(retained['status'],'retained')
                    self.provider.messages[11][key]=value
                    self.provider.messages[11]['self_destruct_type']=None
                    extraction=Mock(side_effect=AssertionError('unsafe source reached extraction'))
                    with patch('telegram_search_mcp.broker.read_document',extraction):
                        self.unavailable(self.selected('read_attachment',retained['selection_id']))
                        extraction.assert_not_called()
                        self.provider.messages[11]=copy.deepcopy(original);self.provider.calls.clear()
                        self.unavailable(self.selected('read_attachment',retained['selection_id']))
                        self.assertEqual(self.provider.calls,[]);extraction.assert_not_called()

    def test_countdown_full_capacity_recapture_refuses_and_restore_never_resurrects(self):
        values=(1.0,-1,False,None,'0',float('nan'),float('inf'))
        for key in ('self_destruct_in','auto_delete_in'):
            for type_present in (False,True):
                for value in values:
                    with self.subTest(key=key,type_present=type_present,value=repr(value)):
                        self.broker._selections._entries.clear()
                        original=copy.deepcopy(self.provider.messages[11])
                        source,first,unrelated=self.full_capacity_message()
                        self.provider.messages[11][key]=value
                        if type_present:self.provider.messages[11]['self_destruct_type']=None
                        self.provider.calls.clear();self.unavailable(self.retain(source))
                        self.assertNotIn(first['selection_id'],self.broker._selections._entries)
                        self.assertEqual(len(self.broker._selections._entries),15)
                        self.assertFalse(any(call[0]=='sender' for call in self.provider.calls))
                        for row in unrelated:self.assertIn(row['selection_id'],self.broker._selections._entries)
                        self.provider.messages[11]=original;self.provider.calls.clear()
                        self.unavailable(self.selected('read_messages',first['selection_id']))
                        self.assertEqual(self.provider.calls,[])
                        restored=self.retain(source);self.assertEqual(restored['status'],'retained')
                        self.assertNotEqual(restored['selection_id'],first['selection_id'])
                        self.assertEqual(len(self.broker._selections._entries),16)


class RetainedSchemaTests(unittest.TestCase):
    def test_exactly_one_alternatives_reject_both_neither_and_coerced_ids(self):
        from telegram_search_mcp.schemas import ReadMessagesRequest, AttachmentRequest, ReadAttachmentRequest
        from pydantic import ValidationError
        token='selection_'+'a'*64
        self.assertEqual(ReadMessagesRequest(selection_id=token).selection_id,token)
        for model,field,raw in [(ReadMessagesRequest,'anchors',[{'chat_id':-7,'message_id':11}]),
                (AttachmentRequest,'anchor',{'chat_id':-7,'message_id':11}),
                (ReadAttachmentRequest,'artifact_id','artifact_'+'a'*32+'_'+'b'*64+'_3')]:
            for payload in [{},{field:raw,'selection_id':token},{'selection_id':3}]:
                with self.assertRaises(ValidationError):model.model_validate(payload)
