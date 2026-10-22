from __future__ import annotations
import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from mcp import Client
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.server import build_server
from telegram_search_mcp.schemas import ReadMessagesRequest, ReadAttachmentRequest
import test_retained_selections as retained_fixtures
from tool_listing import usable


class SelectionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper=retained_fixtures.RetainedBrokerTests();self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.broker=self.helper.broker;self.provider=self.helper.provider
        self.broker._contract=__import__('telegram_search_mcp.contract',fromlist=['contract_descriptor']).contract_descriptor(self.broker._policy)
        self.thread=threading.Thread(target=self.broker.serve_forever);self.thread.start()
        self.assertTrue(self.broker.wait_until_ready(timeout=2))
        self.proxy=BrokerClient(socket_path=self.broker._socket_path,policy=self.broker._policy,restart_callback=lambda:None)
        self.addCleanup(self.stop)

    def stop(self):
        self.proxy.close();self.broker.shutdown();self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def request(self,source):
        from telegram_search_mcp.retained_selection_models import RetainSelectionRequest
        return RetainSelectionRequest(source=source)

    async def test_no_skill_sdk_lists_47_tools_retain_read_file_process_and_refusal(self):
        async with Client(build_server(service_factory=lambda:self.proxy,policy=self.broker._policy,
                                      artifact_root=self.helper.store.cache_dir)) as consumer:
            tools={tool.name:tool for tool in (await consumer.list_tools()).tools}
            self.assertIn('retain_selection',list(tools),'retained-source issuer is missing from discovery')
            self.assertEqual(set(tools),usable(self.broker._policy))
            self.assertFalse(tools['retain_selection'].input_schema['additionalProperties'])
            for name in ['read_messages','get_attachment','get_message_context','read_reply_chain',
                    'read_attachment','analyze_media','read_attachment_page','read_spreadsheet','read_presentation',
                    'prepare_artifact_send','prepare_reply_artifact_send','prepare_reply_text_send']:
                self.assertIsNotNone(tools[name].output_schema,name)
            for name in ['send_prepared_artifact','send_prepared_text']:
                self.assertNotIn('selection_id',tools[name].input_schema['properties'])
            issued=(await consumer.call_tool('retain_selection',{'source':{'kind':'messages',
                'anchors':[{'chat_id':7,'message_id':12},{'chat_id':7,'message_id':11}]}})).structured_content['result']
            self.assertEqual(issued['status'],'retained')
            read=await consumer.call_tool('read_messages',{'selection_id':issued['selection_id']})
            self.assertFalse(read.is_error);self.assertEqual(read.structured_content['result']['status'],'complete')
            before=len(self.provider.calls)
            for args in [{},{'selection_id':issued['selection_id'],'anchors':[{'chat_id':7,'message_id':11}]},
                         {'selection_id':issued['selection_id'],'owner':'forged'}, {'anchors':'[{"chat_id":7,"message_id":11}]'}]:
                invalid=await consumer.call_tool('read_messages',args);self.assertTrue(invalid.is_error)
            self.assertEqual(len(self.provider.calls),before)
            wrong=await consumer.call_tool('get_attachment',{'selection_id':issued['selection_id']})
            self.assertEqual(wrong.structured_content['result']['status'],'selection_unavailable')
            created=(await consumer.call_tool('create_local_artifact',{'file_name':'memo.txt','content':'abcdef'})).structured_content
            file=(await consumer.call_tool('retain_selection',{'source':{'kind':'file','artifact_id':created['artifact_id']}})).structured_content['result']
            page=await consumer.call_tool('read_attachment_page',{'selection_id':file['selection_id'],'max_chars':2,'render_pages':False})
            self.assertFalse(page.is_error);self.assertEqual(page.structured_content['status'],'page')
            following=await consumer.call_tool('read_attachment_page',{'selection_id':file['selection_id'],'max_chars':2,
                'render_pages':False,'cursor':page.structured_content['next_cursor']})
            self.assertEqual(following.structured_content['text_start'],2)

    async def test_framed_same_client_reconnect_foreign_and_generation_deny(self):
        self.assertTrue(hasattr(self.proxy,'retain_selection'),'proxy cannot capture selections')
        issued=self.proxy.retain_selection(self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}))
        self.assertEqual(self.proxy.read_messages(ReadMessagesRequest(selection_id=issued.selection_id)).status,'complete')
        foreign=BrokerClient(socket_path=self.broker._socket_path,policy=self.broker._policy,restart_callback=lambda:None)
        self.addCleanup(foreign.close)
        before=len(self.provider.calls)
        self.assertEqual(foreign.read_messages(ReadMessagesRequest(selection_id=issued.selection_id)).status,'selection_unavailable')
        self.assertEqual(len(self.provider.calls),before)
        self.broker._generation='broker_'+'f'*32
        self.assertEqual(self.proxy.read_messages(ReadMessagesRequest(selection_id=issued.selection_id)).status,'selection_unavailable')

    async def test_proxy_rejects_substituted_order_kind_expiry_and_content_bearing_refusal(self):
        self.assertTrue(hasattr(self.proxy,'retain_selection'),'proxy cannot capture selections')
        issued=self.proxy.retain_selection(self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':12},{'chat_id':7,'message_id':11}]}))
        raw=self.helper.dispatch('read_messages',{'selection_id':issued.selection_id},self.proxy.client_id)
        bad=copy.deepcopy(raw);bad['results'].reverse()
        original=self.proxy._exchange
        body=copy.deepcopy(raw);body['results'][0]['message']['text']['value']='substituted visible body'
        for response in [bad,body,{'status':'selection_unavailable','detail':'Selection is unavailable; select the source again.','text':'secret'}]:
            def exchange(connection,operation,payload,**kwargs):
                return original(connection,operation,payload,**kwargs) if operation=='handshake' else response
            with patch.object(self.proxy,'_exchange',side_effect=exchange):
                refused=self.proxy.read_messages(ReadMessagesRequest(selection_id=issued.selection_id))
                self.assertEqual(refused.status,'selection_unavailable');self.assertEqual(set(refused.model_dump()),{'status','detail'})

    async def test_policy_ttl_bounds_issuance_in_the_broker_and_independently_in_the_proxy(self):
        import time
        from dataclasses import replace
        from datetime import datetime, timezone
        from telegram_search_mcp.contract import contract_descriptor
        from telegram_search_mcp.retained_selections import SelectionRegistry
        short=replace(self.broker._policy,selection_ttl_seconds=60)
        self.broker._policy=short;self.broker._contract=contract_descriptor(short)
        proxy=BrokerClient(socket_path=self.broker._socket_path,policy=short,restart_callback=lambda:None)
        self.addCleanup(proxy.close)
        source=self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        # A broker still issuing the 900 s default cannot widen the proxy's 60 s policy.
        self.assertEqual(self.broker._selections.ttl_seconds,900)
        self.assertEqual(proxy.retain_selection(source).status,'selection_unavailable')
        self.broker._selections=SelectionRegistry(ttl_seconds=60)
        started=time.monotonic()
        issued=proxy.retain_selection(source)
        self.assertEqual(issued.status,'retained')
        self.assertTrue(55<(issued.expires_at-datetime.now(timezone.utc)).total_seconds()<=60)
        self.assertLessEqual(proxy._selection_states[issued.selection_id].expires_monotonic,time.monotonic()+60)
        self.assertGreaterEqual(proxy._selection_states[issued.selection_id].expires_monotonic,started+55)
        self.assertEqual(proxy.read_messages(ReadMessagesRequest(selection_id=issued.selection_id)).status,'complete')

    async def test_proxy_tolerates_link_flips_but_refuses_a_link_outside_the_shared_rule(self):
        source=self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        issued=self.proxy.retain_selection(source)
        self.assertEqual(issued.messages.results[0].message.source.telegram_url,'https://t.me/c/7/11')
        # The proxy's independent recapture and selected-read comparisons ignore only the link.
        self.provider.links={11:None}
        again=self.proxy.retain_selection(source)
        self.assertEqual((again.selection_id,again.expires_at),(issued.selection_id,issued.expires_at))
        self.assertIsNone(again.messages.results[0].message.source.telegram_url)
        read=self.proxy.read_messages(ReadMessagesRequest(selection_id=issued.selection_id))
        self.assertEqual(read.status,'complete');self.assertIsNone(read.results[0].message.source.telegram_url)
        self.provider.links={}
        raw=self.helper.dispatch('read_messages',{'selection_id':issued.selection_id},self.proxy.client_id)
        plain=self.helper.dispatch('read_messages',{'anchors':[{'chat_id':7,'message_id':11}]},self.proxy.client_id)
        self.assertEqual(raw['results'][0]['message']['source']['telegram_url'],'https://t.me/c/7/11')
        original=self.proxy._exchange
        for link in ('https://evil.example/c/7/11','http://t.me/c/7/11','https://t.me:8443/c/7/11','https://t.me/c/7/1 1'):
            for request,response in ((ReadMessagesRequest(selection_id=issued.selection_id),raw),
                                     (ReadMessagesRequest(anchors=[{'chat_id':7,'message_id':11}]),plain)):
                forged=copy.deepcopy(response);forged['results'][0]['message']['source']['telegram_url']=link
                def exchange(connection,operation,payload,**kwargs):
                    return original(connection,operation,payload,**kwargs) if operation=='handshake' else forged
                with self.subTest(link=link,selected=request.selection_id is not None),patch.object(self.proxy,'_exchange',side_effect=exchange):
                    refused=self.proxy.read_messages(request)
                    self.assertIn(refused.status,('selection_unavailable','error'))
                    self.assertNotIn(link,refused.model_dump_json())

    def reply_source(self):
        from test_text_replies import target
        from telegram_search_mcp.reply_drafts import source_from_message
        self.provider.messages[11]=target(chat_id=7,id=11,media_album_id=0,
            sender_id={'@type':'messageSenderUser','user_id':17})
        from unittest.mock import Mock
        self.provider._request_context=threading.local()
        self.transport=Mock(side_effect=AssertionError('unapproved synthetic transport'))
        self.provider.send_reply_artifact_message=self.transport
        self.approval=Mock(side_effect=AssertionError('unexpected approval prompt'))
        self.broker._approval_prompt=self.approval
        self.provider.read_reply_source=lambda chat,mid,**kwargs:source_from_message(self.provider.get_message(chat,mid),chat,mid)

    def assert_artifact_reply(self,response,file):
        self.assertEqual(response['status'],'prepared')
        reply=response['reply'];draft=reply['draft']
        self.assertEqual((draft['recipient'],reply['reply_target']['anchor']),(7,{'chat_id':7,'message_id':11}))
        self.assertEqual((draft['artifact_id'],draft['sha256'],draft['size_bytes']),
            (file['artifact_id'],file['sha256'],file['size_bytes']))
        self.assertEqual((draft['kind'],draft['display_name'],draft['mime_type'],draft['caption']),
            ('document','memo.txt','text/plain','explicit outgoing caption'))
        self.assertTrue(draft['approval_required'])
        self.assertRegex(reply['preview_sha256'],r'^[0-9a-f]{64}$')
        self.transport.assert_not_called();self.approval.assert_not_called()
        return draft['draft_id']

    async def test_official_sdk_raw_or_selected_file_with_selected_reply_preserves_approval(self):
        self.reply_source()
        async with Client(build_server(service_factory=lambda:self.proxy,policy=self.broker._policy,
                                      artifact_root=self.helper.store.cache_dir)) as consumer:
            file=(await consumer.call_tool('create_local_artifact',{'file_name':'memo.txt','content':'file body'})).structured_content
            reply=(await consumer.call_tool('retain_selection',{'source':{'kind':'messages',
                'anchors':[{'chat_id':7,'message_id':11}]}})).structured_content['result']
            selected=(await consumer.call_tool('retain_selection',{'source':{'kind':'file',
                'artifact_id':file['artifact_id']}})).structured_content['result']
            for source in ({'artifact_id':file['artifact_id']},{'selection_id':selected['selection_id']}):
                with self.subTest(source_field=next(iter(source))):
                    result=await consumer.call_tool('prepare_reply_artifact_send',dict(source,recipient=7,
                        reply_selection_id=reply['selection_id'],display_name='memo.txt',mime_type='text/plain',
                        caption='explicit outgoing caption',kind='document'))
                    self.assertFalse(result.is_error)
                    self.assertEqual(set(result.structured_content),{'result'})
                    did=self.assert_artifact_reply(result.structured_content['result'],file)
                    denied=await consumer.call_tool('send_prepared_artifact',{'draft_id':did,'approved':False})
                    self.assertFalse(denied.is_error);self.assertNotEqual(denied.structured_content['status'],'sent')
                    wrong=await consumer.call_tool('prepare_reply_artifact_send',dict(source,recipient=8,
                        reply_selection_id=reply['selection_id'],display_name='memo.txt',mime_type='text/plain'))
                    self.assertFalse(wrong.is_error);self.assertNotEqual(wrong.structured_content['result']['status'],'prepared')
            self.transport.assert_not_called();self.approval.assert_not_called()

    async def test_framed_raw_or_selected_file_with_selected_reply_preserves_exact_media(self):
        from telegram_search_mcp.schemas import CreateLocalArtifactRequest
        from telegram_search_mcp.reply_artifact_drafts import PrepareReplyArtifactSendRequest
        self.reply_source()
        file=self.proxy.create_local_artifact(CreateLocalArtifactRequest(file_name='memo.txt',content='file body')).model_dump(mode='json')
        reply=self.proxy.retain_selection(self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}))
        selected=self.proxy.retain_selection(self.request({'kind':'file','artifact_id':file['artifact_id']}))
        for source in ({'artifact_id':file['artifact_id']},{'selection_id':selected.selection_id}):
            with self.subTest(source_field=next(iter(source))):
                response=self.proxy.prepare_reply_artifact_send(PrepareReplyArtifactSendRequest(**source,recipient=7,
                    reply_selection_id=reply.selection_id,display_name='memo.txt',mime_type='text/plain',
                    caption='explicit outgoing caption',kind='document'))
                self.assert_artifact_reply(response.model_dump(mode='json'),file)
        self.transport.assert_not_called();self.approval.assert_not_called()

    async def test_framed_recapture_at_capacity_replaces_proxy_issuance_and_old_token_never_reverts(self):
        from telegram_search_mcp.schemas import CreateLocalArtifactRequest
        source=self.request({'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]})
        first=self.proxy.retain_selection(source)
        unrelated=[]
        for i in range(15):
            file=self.proxy.create_local_artifact(CreateLocalArtifactRequest(file_name='memo.txt',content=str(i)))
            unrelated.append(self.proxy.retain_selection(self.request({'kind':'file','artifact_id':file.artifact_id})))
        self.assertEqual(len(self.proxy._selection_states),16)
        original=copy.deepcopy(self.provider.messages[11])
        self.provider.messages[11]['content']['text']['text']='observed changed body'
        changed=self.proxy.retain_selection(source)
        self.assertEqual(changed.status,'retained');self.assertNotEqual(changed.selection_id,first.selection_id)
        self.assertEqual(len(self.proxy._selection_states),16)
        self.assertNotIn(first.selection_id,self.proxy._selection_states)
        self.provider.messages[11]=original;self.provider.calls.clear()
        self.assertEqual(self.proxy.read_messages(ReadMessagesRequest(selection_id=first.selection_id)).status,'selection_unavailable')
        self.assertEqual(self.provider.calls,[])
        for row in unrelated:self.assertIn(row.selection_id,self.proxy._selection_states)

    async def test_default_off_and_undeclared_capture_fields_never_reach_provider(self):
        async with Client(build_server(service_factory=lambda:self.proxy,policy=RuntimePolicy())) as consumer:
            result=await consumer.call_tool('retain_selection',{'source':{'kind':'messages','anchors':[{'chat_id':7,'message_id':11}]}})
            self.assertTrue(result.is_error);self.assertEqual(self.provider.calls,[])
        async with Client(build_server(service_factory=lambda:self.proxy,policy=self.broker._policy)) as consumer:
            for source in [{'kind':'messages','anchors':[{'chat_id':7,'message_id':'11'}]},
                           {'kind':'messages','anchors':'[{"chat_id":7,"message_id":11}]'},
                           {'kind':'file','path':'/private/unknown'},
                           {'kind':'messages','anchors':[{'chat_id':7,'message_id':11}],'account_id':17}]:
                result=await consumer.call_tool('retain_selection',{'source':source});self.assertTrue(result.is_error)
            self.assertEqual(self.provider.calls,[])
