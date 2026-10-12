"""Direct native admission and reducer identity; only raw transport is synthetic."""
import copy, hashlib, inspect, unittest
from collections import deque
from telegram_search_mcp.tdjson import TDLibClient, _validate_bounded_request, ForbiddenTDLibRequest, MessageSendNotAttempted, MessageSendOutcomeUnknown
from telegram_search_mcp.send_observations import SendObservations, ObservationUnavailable
from telegram_search_mcp.reply_drafts import SEND_OPTIONS, LINK_OPTIONS, source_from_message

CHAT=-1007;TEXT='A🙂BC';FROZEN=((0,5,'textEntityTypeBold'),(1,2,'textEntityTypeSpoiler'))
NATIVE=[{'@type':'textEntity','offset':0,'length':5,'type':{'@type':'textEntityTypeBold'}},{'@type':'textEntity','offset':1,'length':2,'type':{'@type':'textEntityTypeSpoiler'}}]
TOPIC={'@type':'messageTopicForum','forum_topic_id':41}
def reply():return {'@type':'messageReplyToMessage','chat_id':CHAT,'message_id':55,'checklist_task_id':0,'poll_option_id':'','origin_send_date':0}
def message(identifier=-9,entities=None,topic=None,anchor=None):
 return {'@type':'message','id':identifier,'chat_id':CHAT,'is_outgoing':True,'topic_id':copy.deepcopy(topic),'reply_to':copy.deepcopy(anchor),'content':{'@type':'messageText','text':{'@type':'formattedText','text':TEXT,'entities':copy.deepcopy(NATIVE if entities is None else entities)}}}
def payload(entities=None):
 return {'@type':'sendMessage','chat_id':CHAT,'topic_id':None,'reply_to':None,'reply_markup':None,'options':{**SEND_OPTIONS,'sending_id':12},'input_message_content':{'@type':'inputMessageText','text':{'@type':'formattedText','text':TEXT,'entities':copy.deepcopy(NATIVE if entities is None else entities)},'link_preview_options':dict(LINK_OPTIONS),'clear_draft':False}}
class Raw:
 def __init__(self):self.sent=[];self.events=deque();self.account=7;self.title='Synthetic forum';self.loss=False;self.mutate=None;self.source_topic=copy.deepcopy(TOPIC)
 def send(self,p):
  self.sent.append(copy.deepcopy(p));kind=p['@type']
  if kind=='getMe':value={'@type':'user','id':self.account}
  elif kind=='getChat':value={'@type':'chat','id':CHAT,'title':self.title,'type':{'@type':'chatTypeSupergroup','supergroup_id':7,'is_channel':False}}
  elif kind=='getAuthorizationState':value={'@type':'authorizationStateReady'}
  elif kind=='getSupergroup':value={'@type':'supergroup','id':7,'is_forum':True}
  elif kind=='getForumTopic':value={'@type':'forumTopic','info':{'@type':'forumTopicInfo','chat_id':CHAT,'forum_topic_id':41,'name':'Selected topic','creation_date':1700000000,'creator_id':{'@type':'messageSenderUser','user_id':7},'is_general':False,'is_outgoing':True,'is_closed':False,'is_hidden':False}}
  elif kind=='getMessage':value={'@type':'message','chat_id':CHAT,'id':55,'sender_id':{'@type':'messageSenderUser','user_id':8},'is_outgoing':False,'is_from_offline':False,'ephemeral_message_id':0,'date':1700000000,'edit_date':0,'self_destruct_in':0.0,'auto_delete_in':0.0,'topic_id':copy.deepcopy(self.source_topic),'content':{'@type':'messageText','text':{'@type':'formattedText','text':'Source full text','entities':[]}}}
  elif kind=='getMessageProperties':value={'@type':'messageProperties','can_be_replied':True}
  elif kind=='sendMessage':
   a=reply() if p['reply_to'] is not None else None
   pending=message(entities=p['input_message_content']['text']['entities'],topic=p['topic_id'],anchor=a);pending['sending_state']={'@type':'messageSendingStatePending','sending_id':p['options']['sending_id']}
   final=message(701,entities=p['input_message_content']['text']['entities'],topic=p['topic_id'],anchor=a);final['sending_state']=None
   if self.mutate:self.mutate(final)
   self.events.append({**pending,'@extra':p['@extra']})
   if not self.loss:self.events.append({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':final})
   return
  else:raise AssertionError(kind)
  self.events.append({**value,'@extra':p['@extra']})
 def receive(self,t):
  if self.events:return self.events.popleft()
  raise OSError('synthetic confirmation loss')
 def close(self):pass
class FormattedNativeTests(unittest.TestCase):
 def sender(self):
  raw=Raw();p=TDLibClient(raw=raw);p._ready=True;self.addCleanup(p.close);return raw,p
 def require_api(self,p,method):
  self.assertIn('outgoing_entities',inspect.signature(getattr(p,method)).parameters,'FEATURE ABSENT: direct formatted sender API')
 def send(self,p,route='plain',**changes):
  method={'plain':'send_text_message','topic':'send_topic_text_message','reply':'send_reply_text_message','topic_reply':'send_reply_text_message'}[route];self.require_api(p,method)
  if route=='reply':p._raw.source_topic=None
  kwargs={'attempt_id':'attempt','outgoing_entities':FROZEN,'expected_account_id':7,'expected_recipient_title':'Synthetic forum','pre_send_guard':lambda:True}
  if route in ('topic','topic_reply'):kwargs['topic']=p.read_topic_snapshot(CHAT,41,expected_account_id=7,expected_recipient_title='Synthetic forum')
  if route in ('reply','topic_reply'):
   kwargs['reply_source']=p.read_reply_source(CHAT,55,**({'expected_forum_topic_id':41} if route=='topic_reply' else {})) if route=='topic_reply' else source_from_message({**RawMessage(),'topic_id':None},CHAT,55)
  kwargs.update(changes);return getattr(p,method)(CHAT,TEXT,**kwargs)
 def test_canonical_native_body_admission_and_refusal(self):
  try:_validate_bounded_request(payload())
  except ForbiddenTDLibRequest as e:self.fail('FEATURE ABSENT: five-style inputMessageText admission: '+str(e))
  for entity in [dict(NATIVE[0],extra=1),dict(NATIVE[0],offset=True),dict(NATIVE[0],length=2),{'offset':0,'length':5,'type':{'@type':'textEntityTypeBold'}},dict(NATIVE[0],type={'@type':'textEntityTypeCode'})]:
   with self.subTest(entity=entity),self.assertRaises(ForbiddenTDLibRequest):_validate_bounded_request(payload([entity]))
 def test_four_routes_exact_single_native_send(self):
  for route in ('plain','topic','reply','topic_reply'):
   with self.subTest(route=route):
    raw,p=self.sender();self.assertEqual(self.send(p,route),701);s=[r for r in raw.sent if r['@type']=='sendMessage'];self.assertEqual(len(s),1);self.assertEqual(s[0]['input_message_content']['text']['entities'],NATIVE);self.assertEqual(p._send_observations.snapshot('attempt').status,'sent')
 def test_plain_requires_reviewed_authority_and_attempt(self):
  for field,value in [('expected_account_id',None),('expected_recipient_title',None),('pre_send_guard',None),('attempt_id',None),('expected_account_id',True),('expected_account_id',8),('expected_recipient_title','Other')]:
   with self.subTest(field=field,value=value):
    raw,p=self.sender()
    with self.assertRaises(MessageSendNotAttempted):self.send(p,**{field:value})
    self.assertFalse(any(r['@type']=='sendMessage' for r in raw.sent));self.assertIsNone(p._send_observations.snapshot('attempt'))
 def test_registration_guard_drift_discards_without_raw_attempt(self):
  for drift in ('account','title','guard'):
   with self.subTest(drift=drift):
    raw,p=self.sender();self.require_api(p,'send_text_message');original=p._send_observations.register;allowed=[True]
    def register(*a,**kw):
     original(*a,**kw)
     if drift=='account':raw.account=8
     elif drift=='title':raw.title='Changed'
     else:allowed[0]=False
    p._send_observations.register=register
    with self.assertRaises(MessageSendNotAttempted):self.send(p,pre_send_guard=lambda:allowed[0])
    self.assertIsNone(p._send_observations.snapshot('attempt'));self.assertFalse(any(r['@type']=='sendMessage' for r in raw.sent))
 def test_noncanonical_frozen_input_refused_without_attempt(self):
  for rows in [list(FROZEN),tuple(reversed(FROZEN)),((True,5,'textEntityTypeBold'),),((1,1,'textEntityTypeBold'),),((0,5,'textEntityTypeCode'),)]:
   with self.subTest(rows=rows):
    raw,p=self.sender()
    with self.assertRaises(MessageSendNotAttempted):self.send(p,outgoing_entities=rows)
    self.assertFalse(any(r['@type']=='sendMessage' for r in raw.sent))
 def test_final_extra_style_unknown_single_attempt(self):
  raw,p=self.sender();raw.mutate=lambda m:m['content']['text']['entities'].append({'@type':'textEntity','offset':3,'length':1,'type':{'@type':'textEntityTypeItalic'}})
  with self.assertRaises(MessageSendOutcomeUnknown):self.send(p)
  self.assertEqual(sum(r['@type']=='sendMessage' for r in raw.sent),1);self.assertEqual(p._send_observations.snapshot('attempt').status,'pending')
 def test_empty_plain_wire_golden(self):
  raw,p=self.sender();self.assertEqual(p.send_text_message(CHAT,TEXT,attempt_id='plain'),701)
  s=next(r for r in raw.sent if r['@type']=='sendMessage');self.assertEqual(s['input_message_content'],payload([])['input_message_content']);self.assertIsNone(s['topic_id']);self.assertIsNone(s['reply_to']);self.assertEqual([r['@type'] for r in raw.sent],['sendMessage'])
def RawMessage():
 r=Raw();r.send({'@type':'getMessage','@extra':'x'});return r.events.popleft()
class FormattedReducerTests(unittest.TestCase):
 def register(self,topic=None,anchor=None):
  self.assertIn('outgoing_entities',inspect.signature(SendObservations.register).parameters,'FEATURE ABSENT: immutable expected styles')
  s=SendObservations();s.register('a','x',12,CHAT,'messageText',text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),outgoing_entities=FROZEN,forum_topic_id=41 if topic else None,reply_anchor=(CHAT,55) if anchor else None);return s
 def pending(self,m):return {**m,'@extra':'x','sending_state':{'@type':'messageSendingStatePending','sending_id':12}}
 def test_exact_pending_final_and_order_only_equivalence(self):
  s=self.register();s.reduce(self.pending(message(entities=list(reversed(NATIVE)))));self.assertEqual(s.snapshot('a').status,'pending');s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':message(701)});self.assertEqual(s.snapshot('a').message_id,701);self.assertEqual(s.snapshot('a').outgoing_entities,FROZEN)
 def test_lost_preliminary_recovery_and_late_success(self):
  s=self.register();s.reduce({'@type':'updateNewMessage','message':{**message(),'sending_state':{'@type':'messageSendingStatePending','sending_id':12}}});self.assertEqual(s.snapshot('a').status,'pending');s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':message(802)});self.assertEqual(s.snapshot('a').message_id,802)
 def test_pending_style_mismatch_never_registers_temporary_id(self):
  for rows in [[],NATIVE[:1],NATIVE+[NATIVE[0]], [dict(NATIVE[0],length=4),NATIVE[1]], [dict(NATIVE[0],type={'@type':'textEntityTypeItalic'}),NATIVE[1]], [dict(NATIVE[0],extra=1),NATIVE[1]]]:
   with self.subTest(rows=rows):
    s=self.register();s.reduce(self.pending(message(entities=rows)));self.assertEqual((s.snapshot('a').status,s.snapshot('a').temporary_id),('registered',None))
 def test_final_style_text_oldid_mutations_ignored_then_exact_late(self):
  for mut in [lambda m:m['content']['text'].update(entities=[]),lambda m:m['content']['text'].update(text='Different'),lambda m:m.update(chat_id=999),lambda m:m.update(topic_id=TOPIC),lambda m:m.update(reply_to=reply())]:
   with self.subTest(mut=mut):
    s=self.register();s.reduce(self.pending(message()));bad=message(701);mut(bad);s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':bad});self.assertEqual(s.snapshot('a').status,'pending');s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-8,'message':message(701)});self.assertEqual(s.snapshot('a').status,'pending');s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':message(803)});self.assertEqual(s.snapshot('a').message_id,803)
 def test_topic_reply_both_identities_independent(self):
  for topic,anchor in [(TOPIC,reply()),(TOPIC,None),(None,reply())]:
   s=self.register(topic,anchor);s.reduce(self.pending(message(topic=topic,anchor=anchor)));self.assertEqual(s.snapshot('a').status,'pending')
   for changed in ['topic_id','reply_to']:
    if (changed=='topic_id' and topic) or (changed=='reply_to' and anchor):
     bad=message(701,topic=topic,anchor=anchor);bad[changed]=None;s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':bad});self.assertEqual(s.snapshot('a').status,'pending')
   s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':message(804,topic=topic,anchor=anchor)});self.assertEqual(s.snapshot('a').message_id,804)
 def test_registration_mutable_or_artifact_expectation_refused(self):
  self.register()
  for rows,kind in [(list(FROZEN),'messageText'),(tuple(reversed(FROZEN)),'messageText'),(FROZEN,'messageDocument')]:
   with self.assertRaises(ObservationUnavailable):SendObservations().register('a','x',12,CHAT,kind,text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),outgoing_entities=rows)
 def test_retention_and_terminal_monotonicity(self):
  s=self.register();now=[1.];s._clock=lambda:now[0];# set stable initial timestamp with fresh store
  s=SendObservations(clock=lambda:now[0]);s.register('a','x',12,CHAT,'messageText',text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),outgoing_entities=FROZEN)
  s.reduce(self.pending(message()));s.reduce({'@type':'updateMessageSendSucceeded','old_message_id':-9,'message':message(805)});now[0]=900.;self.assertEqual(s.snapshot('a').message_id,805);now[0]=901.;self.assertIsNone(s.snapshot('a'))
