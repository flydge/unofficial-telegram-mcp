"""Direct SDK, coherent proxy tampering and synthetic owner-display tests."""
import copy, hashlib, json, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from telegram_search_mcp.config import RuntimePolicy
from tool_listing import usable
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.schemas import PrepareTextSendRequest
from telegram_search_mcp.reply_drafts import PrepareReplyTextSendRequest,UpdateReplyDraftRequest
from telegram_search_mcp.draft_models import UpdateDraftRequest
from telegram_search_mcp.approval_prompt import confirm_approved_send
from approval_layout import logical, tail
DID = 'draft_' + '1' * 32
TEXT = 'A😀B cafe'
EXPIRY = '2030-01-02T03:04:05Z'
CHAT = -1007
STYLES = ('textEntityTypeBold', 'textEntityTypeItalic', 'textEntityTypeUnderline',
          'textEntityTypeStrikethrough', 'textEntityTypeSpoiler')
SEND = {'@type': 'messageSendOptions', 'suggested_post_info': None,
    'disable_notification': False, 'from_background': False, 'protect_content': False,
    'allow_paid_broadcast': False, 'paid_message_star_count': 0,
    'update_order_of_installed_sticker_sets': False, 'scheduling_state': None,
    'effect_id': 0, 'only_preview': False}
LINK = {'@type': 'linkPreviewOptions', 'is_disabled': True, 'url': '',
    'force_small_media': False, 'force_large_media': False, 'show_above_text': False}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def entity(offset=0, length=1, style=STYLES[0]):
    return {'offset': offset, 'length': length, 'type': {'@type': style}}


def native(rows):
    # Independent expected adapter; never imports the implementation/helper.
    return [dict({'@type': 'textEntity'}, **copy.deepcopy(row)) for row in rows]


def topic():
    value = {'version': 1, 'account_id': 7, 'chat_id': CHAT,
        'chat_type': 'forum_supergroup', 'peer_id': 7, 'forum_topic_id': 41,
        'name': '[untrusted Telegram evidence] Topic',
        'original_name_sha256': hashlib.sha256(b'Topic').hexdigest(),
        'creation_date': 1700000000, 'creator_kind': 'user', 'creator_id': 7,
        'is_outgoing': False, 'is_general': False, 'is_closed': False, 'is_hidden': False}
    return dict(value, source_sha256=digest(value))


def draft(rows=None, with_topic=False, text=TEXT):
    data = text.encode()
    value = {'draft_id': DID, 'account_id': 7, 'recipient': CHAT,
        'recipient_title': 'Chat', 'kind': 'text', 'expires_at': EXPIRY,
        'approval_required': True, 'sha256': hashlib.sha256(data).hexdigest(),
        'size_bytes': len(data), 'text': text, 'artifact_id': None,
        'display_name': None, 'mime_type': None, 'caption': None,
        'duration_seconds': None, 'waveform_base64': None, 'source_sha256': None,
        'source_display_name': None, 'converted': False}
    if with_topic: value['topic'] = topic()
    if rows is not None: value['outgoing_entities'] = copy.deepcopy(rows)
    return value


def nonreply_facts(value, rows):
    return {'version': 2 if rows else 1, 'draft_id': value['draft_id'],
        'account_id': value['account_id'], 'recipient': value['recipient'],
        'recipient_title': value['recipient_title'],
        'expires_at': datetime.fromisoformat(value['expires_at']).astimezone(timezone.utc).isoformat(),
        'kind': 'text', 'approval_required': True, 'topic': value.get('topic'),
        'text': value['text'], 'sha256': value['sha256'], 'size_bytes': value['size_bytes'],
        'entities': native(rows), 'reply_to': None, 'link_preview_options': LINK,
        'send_options': SEND, 'reply_markup': None, 'clear_draft': False}


def target(with_topic=False):
    value = {'anchor': {'chat_id': CHAT, 'message_id': 55},
        'text': '[untrusted Telegram evidence] Source', 'source_sha256': 'a' * 64,
        'sanitized': False, 'truncated': False}
    if with_topic: value['forum_topic_id'] = 41
    return value


def reply_facts(value, selected, rows):
    return {'version': 3 if rows else 2 if 'topic' in value else 1,
        'draft': value, 'reply_target': selected,
        'topic_id': {'@type': 'messageTopicForum', 'forum_topic_id': 41} if 'topic' in value else None,
        'outgoing_entities': native(rows), 'send_options': SEND,
        'link_preview_options': LINK, 'clear_draft': False, 'reply_markup': None,
        'reply_to': {'@type': 'inputMessageReplyToMessage', 'message_id': 55,
            'quote': None, 'checklist_task_id': 0, 'poll_option_id': ''}}


def reply(rows=None, with_topic=False):
    value, selected = draft(rows, with_topic), target(with_topic)
    return {'draft': value, 'reply_target': selected,
        'preview_sha256': digest(reply_facts(value, selected, rows or []))}



ROWS=[entity(0,5),entity(1,2,'textEntityTypeSpoiler')]
CAPS=('send','reply_text_send','topic_text_send','formatted_text_send')
def preview(rows=ROWS,with_topic=False):
 v=draft(rows,with_topic);v['preview_sha256']=digest(nonreply_facts(v,rows));return v
def prepared(rows=ROWS):
 v=preview(rows);o={k:v[k] for k in ('draft_id','account_id','recipient','recipient_title','text','sha256','expires_at','approval_required','preview_sha256','outgoing_entities')};o.update(status='prepared',detail='Review')
 if not rows:o.pop('account_id');o.pop('preview_sha256');o.pop('outgoing_entities')
 return o
class ProxyFormattingTests(unittest.TestCase):
 def proxy(self,response):
  p=BrokerClient(socket_path=Path('/nonexistent/no-connect.sock'),policy=RuntimePolicy(enabled_capabilities=CAPS));self.addCleanup(p.close);p._request=lambda *a:copy.deepcopy(response);return p
 def test_plain_prepare_rejects_coherent_changed_missing_unsolicited(self):
  for expected,actual in [(ROWS,[entity(0,5,'textEntityTypeItalic')]),(ROWS,[]),([],ROWS)]:
   with self.subTest(expected=expected,actual=actual):self.assertEqual(self.proxy(prepared(actual)).prepare_text_send(PrepareTextSendRequest(recipient=CHAT,text=TEXT,outgoing_entities=expected)).status,'error')
 def test_reply_prepare_rejects_coherent_changed_missing_unsolicited(self):
  for expected,actual in [(ROWS,[entity(0,5,'textEntityTypeItalic')]),(ROWS,[]),([],ROWS)]:
   with self.subTest(expected=expected,actual=actual):self.assertEqual(self.proxy({'status':'prepared','reply':reply(actual),'detail':'immutable reply draft prepared; inspect full evidence and obtain fresh approval'}).prepare_reply_text_send(PrepareReplyTextSendRequest(recipient=CHAT,text=TEXT,reply_to=target()['anchor'],outgoing_entities=expected)).status,'unavailable')
 def test_plain_update_rejects_coherent_style_tamper_and_clear_tamper(self):
  for expected,actual in [(ROWS,[entity(0,5,'textEntityTypeItalic')]),(ROWS,[]),([],ROWS)]:
   v=preview(actual);v['draft_id']='draft_'+'2'*32;v['preview_sha256']=digest(nonreply_facts(v,actual));result={'status':'revised','previous_draft_id':DID,'draft':v,'detail':'Review'}
   with self.subTest(expected=expected,actual=actual):self.assertEqual(self.proxy(result).update_draft(UpdateDraftRequest(draft_id=DID,text=TEXT,outgoing_entities=expected)).status,'unavailable')
 def test_reply_update_rejects_coherent_style_tamper_and_clear_tamper(self):
  for expected,actual in [(ROWS,[entity(0,5,'textEntityTypeItalic')]),(ROWS,[]),([],ROWS)]:
   v=reply(actual);v['draft']['draft_id']='draft_'+'2'*32;v['preview_sha256']=digest(reply_facts(v['draft'],v['reply_target'],actual));result={'status':'revised','previous_draft_id':DID,'reply':v,'detail':'Review'}
   with self.subTest(expected=expected,actual=actual):self.assertEqual(self.proxy(result).update_reply_draft(UpdateReplyDraftRequest(draft_id=DID,text=TEXT,outgoing_entities=expected)).status,'unavailable')
 def test_exact_canonical_prepare_projection_accepts(self):
  self.assertEqual(self.proxy(prepared()).prepare_text_send(PrepareTextSendRequest(recipient=CHAT,text=TEXT,outgoing_entities=list(reversed(ROWS)))).status,'prepared')
  self.assertEqual(self.proxy({'status':'prepared','reply':reply(ROWS),'detail':'immutable reply draft prepared; inspect full evidence and obtain fresh approval'}).prepare_reply_text_send(PrepareReplyTextSendRequest(recipient=CHAT,text=TEXT,reply_to=target()['anchor'],outgoing_entities=ROWS)).status,'prepared')
class OwnerFormattingTests(unittest.TestCase):
 def display(self,**kw):
  seen=[]
  with patch('telegram_search_mcp.approval_prompt.subprocess.run',side_effect=lambda argv,**options:seen.append(logical(argv[-1])) or SimpleNamespace(returncode=0,stdout=b'DECLINED')):
   self.assertFalse(confirm_approved_send(**{**dict(recipient_title='Chat',recipient=CHAT,display_name='Text message',size_bytes=len(TEXT.encode()),sha256=hashlib.sha256(TEXT.encode()).hexdigest(),caption='',kind='text',text=TEXT),**kw}))
  return seen
 def assert_styles(self,text):
  self.assertIn(TEXT,text);self.assertIn('textEntityTypeBold',text);self.assertIn('textEntityTypeSpoiler',text);self.assertIn('offset 1',text);self.assertIn('length 2',text);self.assertIn('😀',text);self.assertIn('Account: 7',text);self.assertIn('Preview SHA-256:',text)
 def test_plain_formatted_uses_single_existing_preview_channel(self):
  seen=self.display(topic_preview=preview());self.assertEqual(len(seen),1);self.assert_styles(seen[0]);self.assertNotIn('no reply, formatting',seen[0])
 def test_topic_formatted_displays_actual_topic_and_all_styles(self):
  seen=self.display(topic_preview=preview(with_topic=True));self.assertEqual(len(seen),1);self.assert_styles(seen[0]);self.assertIn('Topic [untrusted Telegram evidence]: «Topic» (topic 41)\n',seen[0]);self.assertNotIn('no reply, formatting',seen[0])
 def test_reply_formatted_displays_outer_digest_anchor_all_styles(self):
  seen=self.display(reply_preview=reply(ROWS,True));self.assertEqual(len(seen),1);self.assert_styles(seen[0]);self.assertIn('message 55',seen[0]);self.assertIn('Topic [untrusted Telegram evidence]: «Topic» (topic 41)\n',seen[0])
 def test_competing_preview_channels_and_artifact_styles_refuse(self):
  self.assertEqual(self.display(topic_preview=preview(),reply_preview=reply(ROWS)),[])
  v=preview();v['kind']='document';self.assertEqual(self.display(topic_preview=v),[])
 def test_empty_topic_reply_owner_bytes_match_pinned_baseline(self):
  # 0.54 layout, regenerated from the 06f68bad baselines: summary, gutter payloads, technical block. Every
  # fact of the baseline stays; the topic name and the reply evidence lose their raw marker prefix to a
  # label, and both texts now sit behind the payload gutter; the target's sanitized flag stays directly under
  # its evidence. DECLINED mock, no owner side effect.
  text_sha = '6bc441d67a3e8a2c44e8156abfb2df5a95b0b32a3cbbc9fdcdf6508d73e4484d'
  topic_sha = 'dccb9739ac11238eeb34b6c9519ab3d54e54e1901c36c52c756241f8d60058fb'
  head = 'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n'
  topic_line = 'Topic [untrusted Telegram evidence]: «Topic» (topic 41)\n'
  topic_facts = ('Topic type: forum_supergroup; peer: 7', 'Topic created: 1700000000; creator: user 7',
                 'Topic general: False; outgoing: False; closed: False; hidden: False', f'Topic SHA-256: {topic_sha}')
  replied = ('\nIn reply to message 55 in chat -1007 [untrusted Telegram evidence]:\n│ «Source»\n'
             'Target sanitized: False; truncated: False\n\nReply text (exact):\n│ «A😀B cafe»\n')
  target_facts = ('Target source SHA-256: ' + 'a' * 64,)
  reply_closing = 'Unofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.'
  cases = [
   ('empty_topic', {'topic_preview':preview([],True)}, 'Send text to a topic in «Chat» (chat -1007)\n' + head + topic_line +
    '\nText (exact):\n│ «A😀B cafe»\n\n'
    'Plain text; normal notifications; no reply, formatting, link preview, schedule or draft clearing.\n' +
    tail(f'Draft: {DID}', *topic_facts, 'Bytes: 11', f'Text SHA-256: {text_sha}',
         'Preview SHA-256: 5b19a72da30145a9eb21b80255c8e5940c2e3d43122776f6b053f3ef98aaa80d',
         closing='Unofficial Telegram MCP will revalidate this account and topic, then make one send attempt.')),
   ('empty_reply', {'reply_preview':reply(None,False)}, 'Send a reply to «Chat» (chat -1007)\n' + head + replied +
    tail(f'Draft: {DID}', 'Bytes: 11', f'Text SHA-256: {text_sha}', *target_facts,
         'Preview SHA-256: 04da8ff0d0e347cd511c3513066dc1a76bbef0e982fda97dc95ad8d673b11ef3', closing=reply_closing)),
   ('empty_topic_reply', {'reply_preview':reply(None,True)}, 'Send a reply to «Chat» (chat -1007)\n' + head + topic_line +
    replied + tail(f'Draft: {DID}', *topic_facts, 'Bytes: 11', f'Text SHA-256: {text_sha}', *target_facts,
                   'Preview SHA-256: a71a4f0786a0754608099ecd870419a6ae05df1f98e6296a3934e998a0b7dbe7',
                   closing=reply_closing)),
  ]
  for label,kw,expected in cases:
   with self.subTest(case=label):
    self.assertEqual(self.display(**kw), [expected])
 def test_empty_ordinary_owner_golden(self):
  # A plain text send shows its bound draft (account, expiry, draft ID); without it no dialog is shown.
  self.assertEqual(self.display(), [])
  seen=self.display(draft_preview=draft());self.assertEqual(seen,['Send text to «Chat» (chat -1007)\n'
   'Account: 7\nExpires: 2030-01-02T03:04:05+00:00\n\nText (exact):\n│ «A😀B cafe»\n' +
   tail(f'Draft: {DID}', 'Bytes: 11', f'Text SHA-256: {hashlib.sha256(TEXT.encode()).hexdigest()}',
        closing='Unofficial Telegram MCP will make one send attempt.')])
  for key,value in (('recipient',CHAT+1),('recipient_title','Other'),('text','Other'),('caption','x')):
   with self.subTest(key=key):self.assertEqual(self.display(draft_preview=draft(),**{key:value}),[])
  self.assertEqual(self.display(draft_preview=draft(),topic_preview=preview()),[])
  self.assertEqual(self.display(draft_preview=draft(ROWS)),[])
class SDKFormattingTests(unittest.IsolatedAsyncioTestCase):
 async def test_real_sdk_schema_strict_admission_before_service(self):
  from mcp import Client
  from mcp.shared.exceptions import MCPError
  from telegram_search_mcp.server import build_server
  class Service:
   def __init__(self):self.calls=[]
   def close(self):pass
   def prepare_text_send(self,r):self.calls.append(r.model_dump(mode='json'));return {'status':'error','detail':'synthetic no broker'}
   def prepare_reply_text_send(self,r):self.calls.append(r.model_dump(mode='json'));return {'status':'unavailable','detail':'synthetic no broker'}
   def update_draft(self,r):self.calls.append(r.model_dump(mode='json'));return {'status':'unavailable','detail':'synthetic no broker'}
   def update_reply_draft(self,r):self.calls.append(r.model_dump(mode='json'));return {'status':'unavailable','detail':'synthetic no broker'}
  s=Service();server=build_server(service_factory=lambda:s,policy=RuntimePolicy(enabled_capabilities=CAPS))
  async with Client(server) as client:
   tools={t.name:t for t in (await client.list_tools()).tools};self.assertEqual(set(tools),usable(RuntimePolicy(enabled_capabilities=CAPS)))
   for name,base in [('prepare_text_send',{'recipient':CHAT,'text':TEXT}),('prepare_reply_text_send',{'recipient':CHAT,'text':TEXT,'reply_to':target()['anchor']}),('update_draft',{'draft_id':DID,'text':TEXT}),('update_reply_draft',{'draft_id':DID,'text':TEXT})]:
    with self.subTest(tool=name):
     self.assertIn('outgoing_entities',tools[name].input_schema['properties'],'FEATURE ABSENT: typed formatting SDK input')
     for invalid in [None,{},[dict(ROWS[0],offset=True)],[dict(ROWS[0],type={'@type':'textEntityTypeCode'})],[dict(ROWS[0],extra=1)]]:
      before=len(s.calls)
      try:r=await client.call_tool(name,{**base,'outgoing_entities':invalid})
      except MCPError:pass
      else:self.assertTrue(r.is_error)
      self.assertEqual(len(s.calls),before)
     await client.call_tool(name,{**base,'outgoing_entities':ROWS});self.assertEqual(s.calls[-1]['outgoing_entities'],ROWS)
     await client.call_tool(name,base);self.assertNotIn('outgoing_entities',s.calls[-1])
