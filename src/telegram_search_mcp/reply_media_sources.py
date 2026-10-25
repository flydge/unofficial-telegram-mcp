"""Closed stable media evidence; parsing never reads paths or fetches provider data.

Which caption entity families are admitted comes from the reply-source ladder; the keyword
functions are shims over the admission-taking `*_for` functions. A caption with an inert family
(TextUrl, v24) renders the whole record through one inert JSON layer, never the sanitizer; a
voice note's own duration bounds its caption's media timestamps (v28).
"""
from __future__ import annotations

import base64
import hashlib
import json
import unicodedata

from .reply_source_ladder import Admission, STYLED, present
from .sanitize import sanitize_telegram_text


def _shape(value, kind, required, nullable=()):
    return (type(value) is dict and value.get('@type') == kind
            and {'@type', *required} <= set(value) <= {'@type', *required, *nullable})


def _number(value, maximum=2**53-1, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _string(value, maximum=65536):
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        return len(value.encode('utf-8')) <= maximum
    except UnicodeError:
        return False


def _text(value, maximum):
    return (_string(value, maximum) and not any(
        unicodedata.category(c).startswith('C') and c not in '\n\t' for c in value))


def _blob(value, maximum):
    if not _string(value, maximum*2):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) <= maximum
    except ValueError:
        return False


def _file(value):
    if not _shape(value, 'file', ('id','size','expected_size','local','remote')):
        return False
    if not (_number(value['id'], 2**31-1, 1) and _number(value['size']) and _number(value['expected_size'])):
        return False
    local, remote = value['local'], value['remote']
    return (_shape(local, 'localFile', ('path','can_be_downloaded','can_be_deleted','is_downloading_active',
                'is_downloading_completed','download_offset','downloaded_prefix_size','downloaded_size'))
        and _string(local['path'], 4096)
        and all(type(local[k]) is bool for k in ('can_be_downloaded','can_be_deleted','is_downloading_active','is_downloading_completed'))
        and all(_number(local[k]) for k in ('download_offset','downloaded_prefix_size','downloaded_size'))
        and _shape(remote, 'remoteFile', ('id','unique_id','is_uploading_active','is_uploading_completed','uploaded_size'))
        and _string(remote['id'],4096) and _string(remote['unique_id'],4096)
        and all(type(remote[k]) is bool for k in ('is_uploading_active','is_uploading_completed'))
        and _number(remote['uploaded_size']))


def _minithumbnail(value):
    return (value is None or (_shape(value,'minithumbnail',('width','height','data'))
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _blob(value['data'],65536)))


def _thumbnail(value):
    if value is None:
        return True
    if not _shape(value,'thumbnail',('format','width','height','file')):
        return False
    fmt = value['format']
    return (type(fmt) is dict and set(fmt)=={'@type'} and type(fmt['@type']) is str
        and fmt['@type'] in {'thumbnailFormatJpeg','thumbnailFormatGif','thumbnailFormatMpeg4',
            'thumbnailFormatPng','thumbnailFormatTgs','thumbnailFormatWebm','thumbnailFormatWebp'}
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _file(value['file']))


def caption_for(value, admission: Admission, *, max_media_timestamp=None):
    """Validate a bounded caption against an admission and return its canonical shape; `max_media_timestamp` is the
    media's own duration (a voice note's), without which a media timestamp is refused."""
    if not (_shape(value,'formattedText',('text','entities')) and type(value['text']) is str
            and len(value['text'])<=1024 and type(value['entities']) is list
            and not any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in value['text'])):
        raise ValueError('reply caption is unavailable')
    value['text'].encode('utf-8')
    if value['entities']:
        from .reply_formatted_sources import formatted_text_for
        return formatted_text_for(value,admission,max_media_timestamp=max_media_timestamp)
    return {'@type':'formattedText','text':value['text'],'entities':[]}


def _asserted(caption, admission: Admission, styled):
    """The keyword shims' 0.57 contract: `styled`, when given, says whether any entity exists, and every flagged
    family except a datetime level must be present. Stored versions use version equality instead."""
    if styled is not None and bool(caption['entities'])!=styled:
        raise ValueError('reply caption version disagrees with styles')
    found=present(caption['entities'])
    if any(f is not STYLED and f.formatting is None and f not in found for f in admission.families):
        raise ValueError('reply caption version disagrees with entities')


def _identity(value):
    if not _file(value) or value['size']<=0 or not value['remote']['unique_id']:
        raise ValueError('reply media identity is unavailable')
    return {'size':value['size'], 'unique_id_sha256':hashlib.sha256(value['remote']['unique_id'].encode('utf-8')).hexdigest()}


def _variant_key(value):
    return value['type'],value['width'],value['height'],value['unique_id_sha256']


def media_content_for(content, admission: Admission):
    """Validate bounded pinned provider shapes and return only stable fresh facts."""
    if type(content) is not dict:
        raise ValueError('reply media is unavailable')
    kind = content.get('@type')
    if kind == 'messageDocument':
        doc = content.get('document')
        if not (_shape(content,kind,('document','caption'))
            and _shape(doc,'document',('file_name','mime_type','document'),('thumbnail','minithumbnail'))
            and _text(doc['file_name'],255) and _text(doc['mime_type'],127)
            and _thumbnail(doc.get('thumbnail')) and _minithumbnail(doc.get('minithumbnail'))):
            raise ValueError('reply document is unavailable')
        media={'file_name':doc['file_name'],'mime_type':doc['mime_type'],**_identity(doc['document'])}
        source_kind='document'
    elif kind == 'messagePhoto':
        photo = content.get('photo')
        if not (_shape(content,kind,('photo','caption','has_spoiler','is_secret','show_caption_above_media'),('video',))
            and content.get('video') is None
            and all(content[k] is False for k in ('has_spoiler','is_secret','show_caption_above_media'))
            and _shape(photo,'photo',('has_stickers','sizes'),('minithumbnail',)) and photo['has_stickers'] is False
            and _minithumbnail(photo.get('minithumbnail')) and type(photo['sizes']) is list and 1<=len(photo['sizes'])<=20):
            raise ValueError('reply photo is unavailable')
        variants=[]
        for size in photo['sizes']:
            if not (_shape(size,'photoSize',('type','width','height','photo','progressive_sizes'))
                and _text(size['type'],16) and _number(size['width'],16384,1) and _number(size['height'],16384,1)
                and type(size['progressive_sizes']) is list and len(size['progressive_sizes'])<=100
                and all(_number(n,2**31-1) for n in size['progressive_sizes'])):
                raise ValueError('reply photo variant is unavailable')
            variants.append({'type':size['type'],'width':size['width'],'height':size['height'],**_identity(size['photo'])})
        variants.sort(key=_variant_key)
        if any(_variant_key(a)==_variant_key(b) for a,b in zip(variants,variants[1:])):
            raise ValueError('duplicate reply photo variant')
        media={'variants':variants};source_kind='photo'
    elif kind == 'messageVoiceNote':
        voice = content.get('voice_note')
        if not (_shape(content,kind,('voice_note','caption','is_listened')) and type(content['is_listened']) is bool
            and _shape(voice,'voiceNote',('duration','waveform','mime_type','voice'),('speech_recognition_result',))
            and _number(voice['duration'],600,1) and voice['mime_type']=='audio/ogg'
            and _blob(voice['waveform'],100) and voice.get('speech_recognition_result') is None):
            raise ValueError('reply voice note is unavailable')
        media={'duration':voice['duration'],'mime_type':'audio/ogg',**_identity(voice['voice'])};source_kind='voice_note'
    else:
        raise ValueError('reply media kind is unavailable')
    caption=caption_for(content.get('caption'),admission,max_media_timestamp=_duration(source_kind,media))
    result={'kind':source_kind,'caption':caption,'media':media}
    validate_content_for(result,admission)
    return result


def _stable_identity(value):
    sha=value.get('unique_id_sha256')
    return (_number(value.get('size'),minimum=1) and type(sha) is str and len(sha)==64
            and all(c in '0123456789abcdef' for c in sha))


def _duration(kind, media):
    """The media's own duration, the only bound of a caption's media timestamps: a voice note's; none otherwise."""
    return media['duration'] if kind=='voice_note' else None


def validate_content_for(content, admission: Admission):
    """Strictly validate stored media facts and a caption canonical under the admission."""
    if type(content) is not dict or set(content)!={'kind','caption','media'}:
        raise ValueError('invalid media projection')
    kind,media=content['kind'],content['media']
    if type(kind) is not str or type(media) is not dict:
        raise ValueError('invalid media projection')
    valid=False
    if kind=='document':
        valid=(set(media)=={'file_name','mime_type','size','unique_id_sha256'} and _stable_identity(media)
            and _text(media['file_name'],255) and _text(media['mime_type'],127))
    elif kind=='voice_note':
        valid=(set(media)=={'duration','mime_type','size','unique_id_sha256'} and _stable_identity(media)
            and _number(media['duration'],600,1) and media['mime_type']=='audio/ogg')
    elif kind=='photo' and set(media)=={'variants'}:
        variants=media['variants']
        if type(variants) is list and 1<=len(variants)<=20:
            valid=all(type(v) is dict and set(v)=={'type','width','height','size','unique_id_sha256'}
                and _text(v['type'],16) and _number(v['width'],16384,1) and _number(v['height'],16384,1)
                and _stable_identity(v) for v in variants)
            if valid:
                valid=all(_variant_key(a)<_variant_key(b) for a,b in zip(variants,variants[1:]))
    if not valid:
        raise ValueError('invalid media projection')
    if caption_for(content['caption'],admission,max_media_timestamp=_duration(kind,media))!=content['caption']:
        raise ValueError('noncanonical reply caption entities')


def _strings(value):
    """Every string value of the stored media facts, for the inert record's `sanitized` comparison."""
    if type(value) is str:
        yield value
    elif type(value) is dict:
        for item in value.values():
            yield from _strings(item)
    elif type(value) is list:
        for item in value:
            yield from _strings(item)


def _render(content, marker, admission: Admission):
    changed=False
    def safe(value):
        nonlocal changed
        if type(value) is str:
            result=sanitize_telegram_text(value,max_length=65537)
            changed=changed or result!=value
            return result
        if type(value) is dict:
            return {k:safe(v) for k,v in value.items()}
        if type(value) is list:
            return [safe(v) for v in value]
        return value
    caption=content['caption']['text']
    bound=_duration(content['kind'],content['media'])
    if any(family.inert for family in present(content['caption']['entities'])):
        # The whole record in one reversible inert JSON layer, as v23 text: safe() would rewrite hidden payloads.
        # `sanitized` compares that layer with ordinary JSON for every provider string; fixed labels are excluded.
        from .reply_formatted_sources import _inert_json, _inert_json_string, _json, render_formatted_text_for
        caption,changed=render_formatted_text_for(content['caption'],admission,max_media_timestamp=bound)
        changed|=any(_inert_json_string(value)!=_json(value) for value in _strings(content['media']))
        display=marker+'Media target: '+_inert_json({'kind':content['kind'],'caption':caption,'media':content['media']})
    else:
        if content['caption']['entities']:
            from .reply_formatted_sources import render_formatted_text_for
            caption,changed=render_formatted_text_for(content['caption'],admission,max_media_timestamp=bound)
        record=safe({'kind':content['kind'],'caption':caption,'media':content['media']})
        display=marker+'Media target: '+json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if len(display)>4096 or sanitize_telegram_text(display,max_length=4097)!=display:
        raise ValueError('reply evidence exceeds preview bound')
    return display,changed


def render_content_for(content, marker, admission: Admission):
    validate_content_for(content,admission)
    return _render(content,marker,admission)


# Keyword shims: the 0.57 contract (styles always; each truthy flag admits its family and, see _asserted,
# requires it present). Media captions have no TextUrl keyword.

def media_content(content, *, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False):
    """Validate bounded pinned provider shapes and return only stable fresh facts."""
    admission=Admission.of(lexical=lexical,identity=identity,datetime=datetime,datetime_relative=datetime_relative,
        datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,bank_card=bank_card)
    result=media_content_for(content,admission)
    _asserted(result['caption'],admission,None)
    return result


def validate_content(content, *, styled=False, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False):
    """Strictly validate stored media facts and their caption-version boundary."""
    admission=Admission.of(lexical=lexical,identity=identity,datetime=datetime,datetime_relative=datetime_relative,
        datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,bank_card=bank_card)
    validate_content_for(content,admission)
    _asserted(content['caption'],admission,styled or lexical or identity or datetime or url or email or phone or bank_card)


def render_content(content, marker, *, styled=False, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False):
    validate_content(content,styled=styled,lexical=lexical,identity=identity,datetime=datetime,datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,bank_card=bank_card)
    return _render(content,marker,Admission.of(lexical=lexical,identity=identity,datetime=datetime,
        datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,
        bank_card=bank_card))
