"""Bounded exact source facts for change detection, never outgoing admission."""
from __future__ import annotations

import hashlib
import json
from .attachment_service import select_attachment
from .message_reader import _CONTENT, _album, _integer, _reply, _topic


class SelectionUnavailable(RuntimeError):
    """Content-free refusal; callers must discard the entire candidate response."""


def _bounded(value, *, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 12 or count[0] > 8192:
        raise SelectionUnavailable()
    if value is None or type(value) in (bool, int):
        if type(value) is int and not -(2**63) <= value < 2**63:
            raise SelectionUnavailable()
        return
    if type(value) is str:
        if len(value) > 65536:
            raise SelectionUnavailable()
        value.encode('utf-8')
        return
    if type(value) is list and len(value) <= 1024:
        for item in value:_bounded(item, depth=depth+1, count=count)
        return
    if type(value) is dict and len(value) <= 128:
        for key,item in value.items():
            if type(key) is not str or len(key)>128:raise SelectionUnavailable()
            _bounded(item, depth=depth+1, count=count)
        return
    raise SelectionUnavailable()


def digest(value):
    try:
        _bounded(value)
        body=json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        if len(body)>65536:raise SelectionUnavailable()
        return hashlib.sha256(body).hexdigest()
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise SelectionUnavailable() from None


def safe_source(raw):
    if (not isinstance(raw,dict) or raw.get('sending_state') is not None or
            raw.get('ephemeral_content') is not None or raw.get('self_destruct_type') is not None or
            raw.get('can_be_saved') is False):
        raise SelectionUnavailable()
    # Independent safety facts, never ticking fingerprint inputs. Legacy ordinary
    # sources may omit countdowns; present values must be exact numeric zero.
    for key in ('self_destruct_in','auto_delete_in'):
        value=raw.get(key,0)
        if type(value) not in {int,float} or value!=0:raise SelectionUnavailable()
    content=raw.get('content')
    if not isinstance(content,dict) or content.get('is_secret') is True:
        raise SelectionUnavailable()


def attachment_facts(raw):
    """Use the existing variant choice; bind raw metadata and stable remote identity."""
    try:
        safe_source(raw)
        chosen=select_attachment(raw)
        content=raw['content']
        media=content.get(chosen.kind)
        variant=None
        if chosen.kind=='photo':
            media=content['photo']
            sizes=media['sizes']
            candidates=[size for size in sizes if isinstance(size,dict) and isinstance(size.get('photo'),dict)]
            variant=max(candidates,key=lambda size:size['photo'].get('size') if type(size['photo'].get('size')) is int else 0)
            file=variant['photo']
        else:
            file=media['voice' if chosen.kind=='voice_note' else 'video' if chosen.kind=='video_note' else chosen.kind]
        unique=(file.get('remote') or {}).get('unique_id')
        if type(unique) is not str or not unique or len(unique)>4096:raise SelectionUnavailable()
        size=file.get('size')
        if type(size) is not int or not 0<size<=256*1024*1024:raise SelectionUnavailable()
        facts={'kind':chosen.kind,'file_id':chosen.file_id,'size':size,
            'unique_hash':hashlib.sha256(unique.encode('utf-8')).hexdigest(),
            'raw_name':media.get('file_name'),'raw_mime':media.get('mime_type'),
            'duration':media.get('duration'),'width':media.get('width'),
            'height':media.get('height'),'length':media.get('length'),
            'variant':{key:variant.get(key) for key in ('type','width','height')} if variant else None}
        for key in ('duration','width','height','length'):
            value=facts[key]
            if value is not None and (type(value) is not int or not 0<=value<2**31):raise SelectionUnavailable()
        for key in ('raw_name','raw_mime'):
            if facts[key] is not None and type(facts[key]) is not str:raise SelectionUnavailable()
        digest(facts)
        return facts
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise SelectionUnavailable() from None


def message_projection(raw, anchor):
    try:
        safe_source(raw)
        if (raw.get('@type')!='message' or type(raw.get('chat_id')) is not int or
                type(raw.get('id')) is not int or (raw['chat_id'],raw['id'])!=anchor):raise SelectionUnavailable()
        content=raw['content'];kind,role=_CONTENT[content['@type']]
        formatted=content.get(role) if role!='none' else None
        text='';entities=[]
        if role!='none':
            if not isinstance(formatted,dict) or type(formatted.get('text')) is not str:raise SelectionUnavailable()
            text=formatted['text'];entities=formatted.get('entities',[])
            if len(text)>65536:raise SelectionUnavailable()
            if type(entities) is not list or len(entities)>1024:raise SelectionUnavailable()
            # Original UTF-16 coordinates must land on complete Unicode scalars.
            offsets={0};position=0
            for scalar in text:
                position+=2 if ord(scalar)>0xffff else 1;offsets.add(position)
            # TDLib's own shape exactly, as the forward, edit and reply projections read it: every object carries
            # its @type, an entity "textEntity", and nothing else beside offset, length and type.
            for entity in entities:
                if (not isinstance(entity,dict) or set(entity)!={'@type','offset','length','type'} or
                        entity['@type']!='textEntity'):raise SelectionUnavailable()
                offset,length=entity['offset'],entity['length'];payload=entity['type']
                if (type(offset) is not int or type(length) is not int or offset<0 or length<=0 or
                        offset not in offsets or offset+length not in offsets or not isinstance(payload,dict) or
                        type(payload.get('@type')) is not str or not payload['@type'].startswith('textEntityType')):raise SelectionUnavailable()
        sender=raw.get('sender_id')
        if not isinstance(sender,dict) or sender.get('@type') not in {'messageSenderUser','messageSenderChat'}:raise SelectionUnavailable()
        key='user_id' if sender['@type']=='messageSenderUser' else 'chat_id'
        sender_id=_integer(sender.get(key),minimum=1 if key=='user_id' else -(2**53-1))
        if not sender_id or type(raw.get('is_outgoing')) is not bool:raise SelectionUnavailable()
        issues=[];reply=_reply(raw.get('reply_to'),issues);topic=_topic(raw.get('topic_id'),issues)
        if issues:raise SelectionUnavailable()
        facts={'v':1,'anchor':list(anchor),'kind':kind,'role':role,'text':text,'entities':entities,
            'sender':[sender['@type'],sender_id],'outgoing':raw['is_outgoing'],
            'date':_integer(raw.get('date'),maximum=2**31-1),'edit':_integer(raw.get('edit_date'),maximum=2**31-1),
            'reply':reply.model_dump() if reply else None,'topic':topic.model_dump() if topic else None,
            'album':_album(raw.get('media_album_id')),'media':None if kind=='text' else attachment_facts(raw)}
        return digest(facts),len(text)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError):
        raise SelectionUnavailable() from None
