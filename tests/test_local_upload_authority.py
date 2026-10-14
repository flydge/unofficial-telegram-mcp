"""NEW external bytes are usable only after the exact issuer consumes them.

Removing any owner/epoch/deadline/publication fence must break these tests.
The provider factory fails: local authority work cannot accidentally start TDLib.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from functools import partial
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.local_upload import LocalUploadRegistry
from telegram_search_mcp.server import build_server
from telegram_search_mcp import transfer_cli

A, B, C = ['client_' + c * 24 for c in 'abc']


class UploadAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='telegram-b2c-')).resolve()
        self.wall, self.mono = [1000.], [500.]
        self.store = ArtifactStore(cache_dir=self.root / 'cache', clock=lambda:self.wall[0])
        self.provider_calls = []
        def forbidden():
            self.provider_calls.append('construction')
            raise AssertionError('offline provider construction')
        with patch('telegram_search_mcp.broker.LocalUploadRegistry',
                   side_effect=partial(LocalUploadRegistry, root=self.root/'uploads', clock=lambda:self.wall[0])):
            self.broker = Broker(socket_path=self.root/'run'/'broker.sock', artifact_store=self.store,
                policy=RuntimePolicy(enabled_capabilities=('artifacts',)), client_factory=forbidden,
                verify_peer_uid=False)
        if hasattr(self.broker, '_upload_authorities'):
            self.broker._upload_authorities.clock = lambda:self.mono[0]
        self.data = b'issuer-bound NEW bytes'
        self.manifest = dict(file_name='report.txt',kind='document',size_bytes=len(self.data),
                             sha256=hashlib.sha256(self.data).hexdigest())
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.broker.shutdown()
        for entry in list(self.broker._uploads._entries.values()):
            if entry.fd >= 0: os.close(entry.fd)
        self.broker._uploads._entries.clear()
        self.assertEqual(self.provider_calls, [])

    def call(self, op, payload, who=A, publish=None):
        return self.broker._dispatch(dict(operation=op,payload=payload,client_id=who,
            deadline=time.monotonic()+30,broker_generation=self.broker._generation),publish=publish)

    def issue(self):
        result=self.call('issue_local_upload_authority',{'manifest':self.manifest})
        self.assertEqual(result['status'],'issued')
        return result

    def redeem(self,ticket,who=B,manifest=None):
        return self.call('redeem_local_upload_authority',{'upload_authority':ticket['upload_authority'],
                        'manifest':self.manifest if manifest is None else manifest},who)

    def append(self,ready,who=B,index=0,data=None,op='append_authorized_local_upload'):
        data=self.data if data is None else data
        return self.call(op,dict(upload_id=ready['upload_id'],index=index,
            content_base64=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest()),who)

    def complete(self,ticket=None):
        ticket=ticket or self.issue()
        ready=self.redeem(ticket)
        self.assertEqual(ready['status'],'ready')
        self.assertEqual(self.append(ready)['status'],'accepted')
        result=self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B)
        self.assertEqual(result,{'status':'upload_complete','result_ref':ticket['result_ref'],
                               'detail':'local upload supplied; original owner must consume'})
        return ticket,ready

    def consume(self,ticket,who=A,publish=None):
        return self.call('consume_local_upload_result',{'result_ref':ticket['result_ref']},who,publish)

    def cancel(self,ticket,who=A):
        return self.call('cancel_local_upload_authority',{'result_ref':ticket['result_ref']},who)

    def test_registration_has_exact_three_public_tools_no_helper_operations(self):
        names={t.name for t in build_server(policy=RuntimePolicy())._tool_manager.list_tools()}
        self.assertEqual(len(names),47)
        self.assertTrue({'issue_local_upload_authority','consume_local_upload_result','cancel_local_upload_authority'}<=names)
        self.assertFalse({'redeem_local_upload_authority','append_authorized_local_upload','finish_authorized_local_upload'}&names)

    def test_issue_pending_and_foreign_are_content_free_without_grants(self):
        ticket=self.issue()
        self.assertEqual(self.consume(ticket),{'status':'pending','detail':'local upload is pending'})
        unknown={'result_ref':'upload_result_'+'0'*64}
        for op in ('consume_local_upload_result','cancel_local_upload_authority'):
            self.assertEqual(self.call(op,unknown,B),self.call(op,{'result_ref':ticket['result_ref']},B))
            self.assertEqual(self.call(op,unknown,B),{'status':'unavailable','detail':'local upload is unavailable'})
        self.assertEqual(self.broker._artifact_authority._grants,{})
        self.assertNotIn(ticket['upload_authority'],repr(self.broker._upload_authorities._tickets))

    def test_supply_has_no_grant_consume_is_fixed_offline_local_receipt(self):
        ticket,_=self.complete()
        self.assertEqual(self.broker._artifact_authority._grants,{})
        self.wall[0]+=100
        receipt=self.consume(ticket)
        self.assertEqual(receipt['status'],'complete')
        self.assertEqual((receipt['sha256'],receipt['size_bytes'],receipt['file_name']),
            (self.manifest['sha256'],len(self.data),'report.txt'))
        grant=self.broker._artifact_authority._grants[receipt['artifact_id']]
        self.assertEqual(grant.owner.client_id,A)
        self.assertIsNone(grant.account_id)
        self.assertIsNone(grant.account_epoch)
        self.assertEqual(grant.expires_at,1000+43200)
        self.assertEqual(self.store._lookup_bytes(receipt['artifact_id']).path.read_bytes(),self.data)
        self.assertEqual(self.consume(ticket),receipt)
        self.assertEqual(self.cancel(ticket)['status'],'unavailable')
        self.broker._artifact_authority.invalidate_account()
        self.assertEqual(self.consume(ticket),receipt)
        self.assertEqual(self.call('read_attachment',{'artifact_id':receipt['artifact_id']})['text'],self.data.decode())

    def test_foreign_result_read_metadata_and_prepare_never_reach_content(self):
        ticket,_=self.complete(); receipt=self.consume(ticket)
        with patch.object(self.store,'_lookup_bytes',side_effect=AssertionError('foreign content')):
            self.assertEqual(self.consume(ticket,C)['status'],'unavailable')
            for op,payload in [('read_attachment',{}),('read_attachment_page',{}),
                ('read_spreadsheet',{'selections':[{'sheet_index':1,'range':'A1'}]}),('read_presentation',{}),
                ('prepare_artifact_send',{'recipient':17,'display_name':'Synthetic','mime_type':'text/plain','kind':'document'})]:
                policy=RuntimePolicy(enabled_capabilities=('artifacts','attachment_pages','spreadsheets','presentations','send'))
                with patch.object(self.broker,'_policy',policy):
                    result=self.call(op,{'artifact_id':receipt['artifact_id'],**payload},C)
                    self.assertNotEqual(result['status'],'complete')
            with self.broker._artifact_authority.scope(C,self.store):
                self.assertIsNone(self.broker._attachment_metadata(receipt['artifact_id']))

    def test_manifest_mismatch_spends_single_redemption(self):
        ticket=self.issue(); wrong=dict(self.manifest,sha256='0'*64)
        self.assertEqual(self.redeem(ticket,manifest=wrong)['status'],'unavailable')
        self.assertEqual(self.redeem(ticket)['status'],'unavailable')
        self.assertEqual(self.consume(ticket)['status'],'unavailable')

    def test_two_claimants_get_only_one_session(self):
        ticket=self.issue(); barrier=threading.Barrier(3); results=[]
        def claim(who):
            barrier.wait();results.append(self.redeem(ticket,who))
        ts=[threading.Thread(target=claim,args=(who,)) for who in (B,C)]
        for t in ts:t.start()
        barrier.wait()
        for t in ts:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(sorted(r['status'] for r in results),['ready','unavailable'])
        self.assertEqual(len(self.broker._uploads._entries),1)

    def test_supplier_and_legacy_operations_cannot_impersonate_external_stream(self):
        ticket=self.issue();ready=self.redeem(ticket)
        self.assertEqual(self.append(ready,C)['status'],'unavailable')
        self.assertEqual(self.append(ready,op='append_local_upload')['status'],'error')
        self.assertEqual(self.call('finish_local_upload',{'upload_id':ready['upload_id']},B)['status'],'error')
        self.assertEqual(self.append(ready)['status'],'accepted')

    def test_original_or_supplier_release_and_reused_string_never_resurrect(self):
        for who in (A,B):
            with self.subTest(who=who):
                ticket=self.issue();ready=self.redeem(ticket)
                self.call('release_client',{},who)
                with self.broker._artifact_authority.scope(who,self.store):pass
                self.assertEqual(self.append(ready)['status'],'unavailable')
                self.assertEqual(self.consume(ticket)['status'],'unavailable')

    def test_observed_epoch_change_and_switch_back_permanently_revoke_unconsumed(self):
        for stage in ('issued','complete'):
            with self.subTest(stage=stage):
                self.broker._artifact_authority.observe_account(17)
                ticket=self.issue() if stage=='issued' else self.complete()[0]
                self.broker._artifact_authority.observe_account(18)
                self.broker._artifact_authority.observe_account(17)
                self.assertEqual(self.consume(ticket)['status'],'unavailable')
                self.assertEqual(self.redeem(ticket)['status'],'unavailable')

    def test_fixed_both_clocks_expire_even_if_other_moves_back(self):
        for clock in ('wall','monotonic'):
            with self.subTest(clock=clock):
                ticket=self.issue();ready=self.redeem(ticket)
                self.wall[0]+=800;self.mono[0]+=800
                self.assertEqual(self.append(ready)['status'],'accepted')
                if clock=='wall':self.wall[0]+=100;self.mono[0]-=800
                else:self.mono[0]+=100;self.wall[0]-=800
                self.assertEqual(self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B)['status'],'unavailable')
                self.assertEqual(self.consume(ticket)['status'],'unavailable')
                self.wall[0]+=1000;self.mono[0]+=1000

    def test_shared_budget_counts_issued_uploading_complete_once(self):
        tickets=[self.issue() for _ in range(6)]
        self.complete(tickets[0])
        self.redeem(tickets[1])
        legacy=[self.call('begin_local_upload',self.manifest) for _ in range(2)]
        self.assertTrue(all(x['status']=='ready' for x in legacy))
        self.assertEqual(self.call('issue_local_upload_authority',{'manifest':self.manifest})['status'],'unavailable')
        self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'error')
        self.assertEqual(self.cancel(tickets[2])['status'],'cancelled')
        self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'ready')

    def test_cancel_each_stage_prevents_grant(self):
        for stage in ('issued','uploading','complete'):
            with self.subTest(stage=stage):
                ticket=self.issue()
                if stage=='uploading':self.redeem(ticket)
                if stage=='complete':self.complete(ticket)
                self.assertEqual(self.cancel(ticket)['status'],'cancelled')
                self.assertEqual(self.consume(ticket)['status'],'unavailable')
                self.assertEqual(self.redeem(ticket)['status'],'unavailable')
        self.assertEqual(self.broker._artifact_authority._grants,{})

    def test_cancel_while_copying_keeps_tracked_path_and_no_locks_or_grants(self):
        ticket=self.issue();ready=self.redeem(ticket);self.append(ready)
        entered,proceed=threading.Event(),threading.Event();results=[]
        original=self.store._store_bytes
        def blocked(path,**kwargs):
            entered.set();self.assertTrue(proceed.wait(4));return original(path,**kwargs)
        with patch.object(self.store,'_store_bytes',side_effect=blocked):
            t=threading.Thread(target=lambda:results.append(self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B)))
            t.start();self.assertTrue(entered.wait(3))
            path=self.broker._uploads._entries[ready['upload_id']].path
            self.assertTrue(path.exists())
            self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'ready')
            self.assertTrue(path.exists())
            self.assertEqual(self.cancel(ticket)['status'],'cancelled')
            self.assertEqual(self.append(ready)['status'],'unavailable')
            proceed.set();t.join(4);self.assertFalse(t.is_alive())
        self.assertEqual(results[0]['status'],'unavailable')
        self.assertEqual(self.broker._artifact_authority._grants,{})

    def test_cancel_after_bytes_lookup_before_consume_publication_wins(self):
        ticket,_=self.complete();original=self.store._lookup_bytes
        def revoked(artifact):
            value=original(artifact);self.cancel(ticket);return value
        with patch.object(self.store,'_lookup_bytes',side_effect=revoked):
            self.assertEqual(self.consume(ticket)['status'],'unavailable')
        self.assertEqual(self.broker._artifact_authority._grants,{})

    def test_lost_finish_and_consume_replies_recover_without_regrant_or_copy(self):
        ticket=self.issue();ready=self.redeem(ticket);self.append(ready)
        def lost(result):raise OSError('response lost')
        with self.assertRaises(OSError):
            self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B,lost)
        captured=[]
        def lost_owner(result):captured.append(result);raise OSError('response lost')
        with self.assertRaises(OSError):self.consume(ticket,publish=lost_owner)
        with (patch.object(self.broker._artifact_authority,'grant',side_effect=AssertionError('regrant')),
              patch.object(self.store,'_store_bytes',side_effect=AssertionError('recopy'))):
            self.assertEqual(self.consume(ticket),captured[0])
        self.assertEqual(self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B)['status'],'unavailable')

    def test_shutdown_and_generation_drift_refuse(self):
        ticket=self.issue();self.broker._artifact_authority.generation='broker_'+'0'*32
        self.assertEqual(self.consume(ticket)['status'],'unavailable')
        self.broker.shutdown()
        self.assertEqual(self.redeem(ticket)['status'],'unavailable')

    def test_disabled_stale_policy_and_wrong_generation_are_same_closed_refusal(self):
        ticket=self.issue()
        unavailable={'status':'unavailable','detail':'local upload is unavailable'}
        disabled=RuntimePolicy(enabled_capabilities=())
        with patch.object(self.broker,'_policy',disabled):
            self.assertEqual(self.consume(ticket),unavailable)
            self.assertEqual(self.cancel(ticket),unavailable)
            self.assertEqual(self.redeem(ticket),unavailable)
        stale=RuntimePolicy(enabled_capabilities=('artifacts',),expected_schema_fingerprint='0'*64)
        with patch.object(self.broker,'_policy',stale):
            self.assertEqual(self.consume(ticket),unavailable)
        request=dict(operation='consume_local_upload_result',payload={'result_ref':ticket['result_ref']},
            client_id=A,deadline=time.monotonic()+30,broker_generation='broker_'+'0'*32)
        self.assertEqual(self.broker._dispatch(request),unavailable)

    def test_chunk_hash_work_is_outside_all_revocation_locks(self):
        ticket=self.issue();ready=self.redeem(ticket);original=hashlib.sha256
        def checked(*args,**kwargs):
            if args and args[0] == self.data:
                self.assertFalse(self.broker._uploads._lock._is_owned(),'hash under upload lock')
                self.assertFalse(self.broker._artifact_authority.lock._is_owned(),'hash under authority lock')
                self.assertFalse(self.broker._state_lock._is_owned(),'hash under metadata lock')
            return original(*args,**kwargs)
        with patch('telegram_search_mcp.local_upload.hashlib.sha256',side_effect=checked):
            self.assertEqual(self.append(ready)['status'],'accepted')

    def test_policy_observation_never_runs_under_revocation_locks(self):
        original=RuntimePolicy.require_current
        def checked(policy):
            self.assertFalse(self.broker._uploads._lock._is_owned(),'policy IO under upload lock')
            self.assertFalse(self.broker._artifact_authority.lock._is_owned(),'policy IO under authority lock')
            self.assertFalse(self.broker._state_lock._is_owned(),'policy IO under state lock')
            return original(policy)
        with patch.object(RuntimePolicy,'require_current',checked):
            ticket,_=self.complete();self.assertEqual(self.consume(ticket)['status'],'complete')

    def test_blocked_write_or_cumulative_hash_cannot_block_revocation_or_publish_acceptance(self):
        for work in ('write','digest'):
            for action in ('cancel','release','finish','append'):
                with self.subTest(work=work,action=action):
                    ticket=self.issue();ready=self.redeem(ticket)
                    entry=self.broker._uploads._entries[ready['upload_id']]
                    entered,proceed,revoked=threading.Event(),threading.Event(),threading.Event()
                    results=[];published=[];other=[];errors=[];original=os.write
                    def stall():
                        entered.set()
                        if not proceed.wait(5):raise AssertionError('bounded test IO gate timed out')
                    def write(fd,data):
                        if fd==entry.fd:stall()
                        return original(fd,data)
                    class Digest:
                        def __init__(self,raw):self.raw=raw
                        def update(self,data):stall();self.raw.update(data)
                        def hexdigest(self):return self.raw.hexdigest()
                    if work=='digest':entry.digest=Digest(entry.digest)
                    payload=dict(upload_id=ready['upload_id'],index=0,
                        content_base64=base64.b64encode(self.data).decode(),sha256=hashlib.sha256(self.data).hexdigest())
                    def append():
                        try:results.append(self.call('append_authorized_local_upload',payload,B,published.append))
                        except BaseException as exc:errors.append(type(exc).__name__)
                    def invalidate():
                        try:
                            if action=='cancel':other.append(self.cancel(ticket))
                            elif action=='release':other.append(self.call('release_client',{},A))
                            elif action=='append':other.append(self.append(ready))
                            else:other.append(self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B))
                        finally:revoked.set()
                    with patch('telegram_search_mcp.local_upload.os.write',side_effect=write) if work=='write' else patch.dict({},{}):
                        writer=threading.Thread(target=append);writer.start()
                        self.assertTrue(entered.wait(3))
                        invalidator=threading.Thread(target=invalidate);invalidator.start()
                        before_unblock=revoked.wait(1)
                        try:
                            if before_unblock:
                                self.assertIs(self.broker._uploads._entries[ready['upload_id']],entry)
                                self.assertIn(ticket['result_ref'],self.broker._uploads._reservations)
                                self.assertTrue(entry.path.exists())
                                self.assertEqual(entry.received,0)
                        finally:
                            proceed.set()
                            for thread in (writer,invalidator):thread.join(5);self.assertFalse(thread.is_alive())
                    self.assertTrue(before_unblock,'revocation or concurrent finish waited for target IO/hash')
                    self.assertEqual(errors,[])
                    self.assertEqual(results,[{'status':'unavailable','detail':'local upload is unavailable'}])
                    self.assertEqual(published,results)
                    self.assertNotIn(ready['upload_id'],self.broker._uploads._entries)
                    self.assertNotIn(ticket['result_ref'],self.broker._uploads._reservations)
                    self.assertEqual(self.broker._artifact_authority._grants,{})

    def test_cumulative_hash_finish_hash_setup_and_retirement_are_outside_pure_locks(self):
        def unlocked():
            self.assertFalse(self.broker._uploads._lock._is_owned(),'IO/hash under upload lock')
            self.assertFalse(self.broker._artifact_authority.lock._is_owned(),'IO/hash under authority lock')
            self.assertFalse(self.broker._state_lock._is_owned(),'IO/hash under state lock')
        setup=self.broker._uploads._ensure_root
        retire=self.broker._uploads._retire
        def checked_setup():unlocked();return setup()
        def checked_retire(entry):unlocked();return retire(entry)
        class Digest:
            def __init__(self,raw):self.raw=raw
            def update(self,data):unlocked();self.raw.update(data)
            def hexdigest(self):unlocked();return self.raw.hexdigest()
        with patch.object(self.broker._uploads,'_ensure_root',side_effect=checked_setup),patch.object(self.broker._uploads,'_retire',side_effect=checked_retire):
            ticket=self.issue();ready=self.redeem(ticket)
            entry=self.broker._uploads._entries[ready['upload_id']];entry.digest=Digest(entry.digest)
            self.assertEqual(self.append(ready)['status'],'accepted')
            self.assertEqual(self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B)['status'],'upload_complete')
            self.assertEqual(self.consume(ticket)['status'],'complete')
            expired=self.issue();self.redeem(expired);self.wall[0]+=901
            self.assertEqual(self.consume(expired)['status'],'unavailable')

    def test_cancel_during_root_setup_retains_claim_capacity_until_setup_exits(self):
        ticket=self.issue();entered,proceed,revoked=threading.Event(),threading.Event(),threading.Event()
        results=[];original=self.broker._uploads._ensure_root
        def blocked():entered.set();self.assertTrue(proceed.wait(5));return original()
        def cancel():
            try:self.cancel(ticket)
            finally:revoked.set()
        with patch.object(self.broker._uploads,'_ensure_root',side_effect=blocked):
            supplier=threading.Thread(target=lambda:results.append(self.redeem(ticket)));supplier.start()
            self.assertTrue(entered.wait(3));canceller=threading.Thread(target=cancel);canceller.start()
            before_unblock=revoked.wait(1)
            try:
                if before_unblock:
                    self.assertIn(ticket['result_ref'],self.broker._uploads._reservations)
                    self.assertEqual(len(self.broker._uploads._entries),1)
            finally:
                proceed.set()
                for thread in (supplier,canceller):thread.join(5);self.assertFalse(thread.is_alive())
        self.assertTrue(before_unblock,'cancel waited for root setup')
        self.assertEqual(results,[{'status':'unavailable','detail':'local upload is unavailable'}])
        self.assertEqual(self.broker._uploads._entries,{})
        self.assertNotIn(ticket['result_ref'],self.broker._uploads._reservations)

    def test_concurrent_consume_creates_one_grant_and_identical_receipt(self):
        ticket,_=self.complete();barrier=threading.Barrier(2);results=[];calls=[]
        lookup=self.store._lookup_bytes;grant=self.broker._artifact_authority.grant
        def together(artifact):
            value=lookup(artifact);barrier.wait(4);return value
        def counted(*args):calls.append('grant');return grant(*args)
        with patch.object(self.store,'_lookup_bytes',side_effect=together),patch.object(self.broker._artifact_authority,'grant',side_effect=counted):
            ts=[threading.Thread(target=lambda:results.append(self.consume(ticket))) for _ in range(2)]
            for t in ts:t.start()
            for t in ts:t.join(6);self.assertFalse(t.is_alive())
        self.assertEqual(len(results),2);self.assertEqual(results[0],results[1]);self.assertEqual(calls,['grant'])

    def test_publication_holds_cancel_until_consumed_receipt_is_sent(self):
        ticket,_=self.complete();entered,proceed=threading.Event(),threading.Event();receipts=[];cancelled=[]
        def publish(result):entered.set();self.assertTrue(proceed.wait(4));receipts.append(result)
        consumer=threading.Thread(target=lambda:self.consume(ticket,publish=publish))
        consumer.start();self.assertTrue(entered.wait(3))
        canceller=threading.Thread(target=lambda:cancelled.append(self.cancel(ticket)))
        canceller.start();self.assertFalse(proceed.is_set());proceed.set()
        for t in (consumer,canceller):t.join(5);self.assertFalse(t.is_alive())
        self.assertEqual(cancelled,[{'status':'unavailable','detail':'local upload is unavailable'}])
        self.assertEqual(self.consume(ticket),receipts[0])

    def test_lost_redemption_cannot_be_reclaimed_and_owner_can_cancel(self):
        ticket=self.issue()
        def lost(result):raise OSError('lost claim')
        with self.assertRaises(OSError):
            self.call('redeem_local_upload_authority',{'upload_authority':ticket['upload_authority'],'manifest':self.manifest},B,lost)
        self.assertEqual(self.redeem(ticket)['status'],'unavailable')
        self.assertEqual(self.consume(ticket)['status'],'pending')
        self.assertEqual(self.cancel(ticket)['status'],'cancelled')
        self.assertEqual(len(self.broker._uploads._entries),0)

    def test_deadlines_release_legacy_capacity_without_owner_status_call(self):
        tickets=[self.issue() for _ in range(8)]
        self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'error')
        self.mono[0]+=900
        self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'ready')
        self.assertEqual(self.consume(tickets[0])['status'],'unavailable')

    def test_cancellation_of_finishing_preserves_shared_reservation_until_copy_exits(self):
        ticket=self.issue();ready=self.redeem(ticket);self.append(ready)
        entered,proceed=threading.Event(),threading.Event();original=self.store._store_bytes
        def blocked(path,**kwargs):entered.set();self.assertTrue(proceed.wait(4));return original(path,**kwargs)
        with patch.object(self.store,'_store_bytes',side_effect=blocked):
            worker=threading.Thread(target=lambda:self.call('finish_authorized_local_upload',{'upload_id':ready['upload_id']},B))
            worker.start();self.assertTrue(entered.wait(3));self.cancel(ticket)
            for _ in range(7):self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'ready')
            self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'error')
            proceed.set();worker.join(5);self.assertFalse(worker.is_alive())
        self.assertEqual(self.call('begin_local_upload',self.manifest)['status'],'ready')

    def test_damaged_ungranted_cache_refuses_without_grant(self):
        ticket,_=self.complete()
        entry=self.broker._upload_authorities._tickets[ticket['result_ref']]
        entry.completed.artifact.path.write_bytes(b'wrong')
        self.assertEqual(self.consume(ticket)['status'],'unavailable')
        self.assertEqual(self.broker._artifact_authority._grants,{})

    def test_manifest_and_refusal_models_reject_smuggled_fields_naive_dates_and_bounds(self):
        from pydantic import ValidationError
        from telegram_search_mcp.local_upload_models import LocalUploadManifest, upload_response
        for delta in ({'kind':'photo','size_bytes':10000001},{'size_bytes':True},{'file_name':'../secret'},
                      {'sha256':'a'*64+'\n'},{'extra':'secret'}):
            with self.subTest(delta=tuple(delta)):
                with self.assertRaises(ValidationError):LocalUploadManifest.model_validate(dict(self.manifest,**delta))
        with self.assertRaises(ValidationError):upload_response('consume_local_upload_result',
            {'status':'unavailable','detail':'local upload is unavailable','artifact_id':'secret'})
        ticket=self.issue();ticket['expires_at']='2026-01-01T00:00:00'
        with self.assertRaises(ValidationError):upload_response('issue_local_upload_authority',ticket)

    def test_standalone_cli_main_supplies_two_chunks_to_distinct_original_proxy(self):
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.local_upload_models import IssueLocalUploadAuthorityRequest, LocalUploadResultRequest
        from telegram_search_mcp.schemas import ReadAttachmentRequest
        source=self.root/'chosen.txt';data=b'A'*524288+b'B2C tail beyond first chunk'
        source.write_bytes(data)
        policy=self.broker._policy
        worker=threading.Thread(target=self.broker.serve_forever);worker.start()
        self.assertTrue(self.broker.wait_until_ready(timeout=3))
        owner=BrokerClient(socket_path=self.broker._socket_path,policy=policy,restart_callback=lambda:None,
                           retry_backoff_seconds=0)
        supplier=transfer_cli._UploadBrokerClient(socket_path=self.broker._socket_path,policy=policy,
                           restart_callback=lambda:None,retry_backoff_seconds=0)
        self.assertNotEqual(owner.client_id,supplier.client_id)
        try:
            manifest=transfer_cli.manifest_source(source=str(source),kind='document')
            ticket=owner.issue_local_upload_authority(IssueLocalUploadAuthorityRequest(manifest=manifest))
            self.assertEqual(ticket.status,'issued')
            envelope=json.dumps(dict(upload_authority=ticket.upload_authority,manifest=manifest))
            output,error=io.StringIO(),io.StringIO()
            with patch.object(transfer_cli,'_make_client',return_value=supplier),patch('sys.stdin',io.StringIO(envelope)),patch('sys.stdout',output),patch('sys.stderr',error):
                status=transfer_cli.main(['upload','--source',str(source),'--kind','document','--upload-authority-stdin'])
            self.assertEqual(status,0,error.getvalue());self.assertEqual(error.getvalue(),'')
            receipt=json.loads(output.getvalue());self.assertEqual(receipt['status'],'upload_complete')
            self.assertEqual(receipt['result_ref'],ticket.result_ref)
            self.assertEqual(set(receipt),{'status','result_ref','detail'})
            self.assertNotIn(ticket.upload_authority,output.getvalue())
            self.assertEqual(self.broker._artifact_authority._grants,{})
            consumed=owner.consume_local_upload_result(LocalUploadResultRequest(result_ref=ticket.result_ref))
            self.assertEqual(consumed.status,'complete')
            self.assertEqual(consumed.sha256,hashlib.sha256(data).hexdigest())
            self.assertEqual(self.store._lookup_bytes(consumed.artifact_id).path.read_bytes(),data)
            read=owner.read_attachment(ReadAttachmentRequest(artifact_id=consumed.artifact_id,max_chars=32))
            self.assertEqual(read.text,'A'*32);self.assertFalse(read.coverage_complete)
            with self.broker._artifact_authority.scope(supplier.client_id,self.store) as scope:
                self.assertFalse(self.broker._artifact_authority.authorize_local(scope,consumed.artifact_id))
        finally:
            self.broker.shutdown();worker.join(6);self.assertFalse(worker.is_alive())


class UploadAuthorityCliTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='telegram-b2c-cli-')).resolve()
        self.source=self.root/'data.txt';self.source.write_bytes(b'original chosen bytes')

    def test_manifest_main_is_exact_metadata_with_no_client_or_policy(self):
        output=io.StringIO()
        with (patch.object(transfer_cli,'_make_client',side_effect=AssertionError('broker')),
              patch.object(transfer_cli,'load_runtime_policy',side_effect=AssertionError('policy')),
              patch('sys.stdout',output)):
            status=transfer_cli.main(['manifest','--source',str(self.source),'--kind','document'])
        self.assertEqual(status,0)
        self.assertEqual(json.loads(output.getvalue()),dict(file_name='data.txt',kind='document',
            size_bytes=21,sha256=hashlib.sha256(b'original chosen bytes').hexdigest()))

    def test_missing_malformed_extra_and_duplicate_authority_are_sanitized_no_fallback(self):
        for envelope in ('','secret','{}','{"upload_authority":"SENSITIVE_ENVELOPE_VALUE"}',
                         '{"upload_authority":"SENSITIVE_ENVELOPE_VALUE","upload_authority":"other"}', 'x'*2049):
            with self.subTest(envelope_length=len(envelope)):
                output,error=io.StringIO(),io.StringIO()
                with (patch('sys.stdin',io.StringIO(envelope)),patch('sys.stdout',output),patch('sys.stderr',error),
                      patch.object(transfer_cli,'_make_client',side_effect=AssertionError('fallback'))):
                    status=transfer_cli.main(['upload','--source',str(self.source),'--kind','document','--upload-authority-stdin'])
                self.assertNotEqual(status,0);self.assertEqual(output.getvalue(),'')
                self.assertNotIn('SENSITIVE_ENVELOPE_VALUE',error.getvalue());self.assertNotIn(envelope[:100],error.getvalue()) if envelope else None

    def test_changed_source_refuses_before_any_client_or_redemption(self):
        self.assertTrue(hasattr(transfer_cli,'manifest_source'),'manifest command is missing')
        manifest=transfer_cli.manifest_source(source=str(self.source),kind='document')
        self.source.write_bytes(b'changed bytes')
        envelope=json.dumps(dict(upload_authority='upload_authority_'+'a'*64,manifest=manifest))
        with (patch('sys.stdin',io.StringIO(envelope)),patch('sys.stderr',io.StringIO()),
              patch.object(transfer_cli,'_make_client',side_effect=AssertionError('early redemption'))):
            self.assertEqual(transfer_cli.main(['upload','--source',str(self.source),'--kind','document','--upload-authority-stdin']),1)


class UploadAuthoritySDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_owner_tools_return_closed_unavailable_without_service(self):
        from mcp import Client
        server=build_server(policy=RuntimePolicy(enabled_capabilities=()),
            service_factory=lambda: (_ for _ in ()).throw(AssertionError('disabled service')))
        async with Client(server) as client:
            for op in ('consume_local_upload_result','cancel_local_upload_authority'):
                result=await client.call_tool(op,{'result_ref':'upload_result_'+'a'*64})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content,{'result':{'status':'unavailable','detail':'local upload is unavailable'}})

    async def test_final_sdk_schema_matches_closed_nested_manifest_and_helper_exclusion(self):
        from mcp import Client
        from telegram_search_mcp.contract import schema_document, fingerprint, TOOL_OPERATIONS, HELPER_OPERATION_CAPABILITIES
        server=build_server(policy=RuntimePolicy())
        async with Client(server) as client:
            tools=(await client.list_tools()).tools
            names={t.name for t in tools}
            self.assertEqual(len(names),47)
            self.assertFalse(names & set(HELPER_OPERATION_CAPABILITIES))
            self.assertFalse(set(TOOL_OPERATIONS) & set(HELPER_OPERATION_CAPABILITIES))
            issue=next(t for t in tools if t.name=='issue_local_upload_authority')
            self.assertEqual(issue.input_schema['required'],['manifest'])
            self.assertFalse(issue.input_schema['additionalProperties'])
            manifest=issue.input_schema['$defs']['LocalUploadManifest']
            self.assertFalse(manifest['additionalProperties'])
            self.assertEqual(set(manifest['required']),{'file_name','kind','size_bytes','sha256'})
            local=(await client.call_tool('_manifest',{})).structured_content
            self.assertEqual(len(local['tools']),47)
        document=schema_document(server)
        self.assertEqual(document['tool_operations'],TOOL_OPERATIONS)
        # Private evidence, produced only by the guarded focused runner.
        work=os.environ.get('TG_B2C_WORK');run=os.environ.get('TG_B2C_RUN')
        if work and run:
            path=Path(work)/(run+'-schema.json')
            with path.open('x') as out:json.dump(dict(fingerprint=fingerprint(document),document=document),out,sort_keys=True)
