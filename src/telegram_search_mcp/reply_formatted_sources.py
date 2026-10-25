"""Closed formatted source projection and complete escaped evidence.

Entity semantics use the pinned TDLib UTF-16 coordinate basis. Display sanitation
never supplies coordinates for validation or changes the raw source identity.
Which entity families are admitted comes from the reply-source ladder; the keyword
functions are shims over the admission-taking `*_for` functions. A media timestamp is
admitted only with the source's own media duration as its bound (a voice note's).
"""
from __future__ import annotations

import json
import re
import unicodedata

from .outgoing_formatting import media_timestamp_tokens
from .reply_source_ladder import (Admission, EXCLUSIVE_TYPES, present, STYLED, LEXICAL as _LEXICAL,
    IDENTITY as _IDENTITY, DATETIME as _DATETIME, URL as _URL, TEXT_URL as _TEXT_URL, EMAIL as _EMAIL,
    PHONE as _PHONE, BANK_CARD as _BANK_CARD, CUSTOM_EMOJI as _CUSTOM_EMOJI, MEDIA_TIMESTAMP as _MEDIA_TIMESTAMP)
from .sanitize import sanitize_telegram_text

OFFSET_BASIS = 'original source UTF-16 code units'
STYLES = STYLED.entities
LEXICAL = _LEXICAL.entities
IDENTITY = _IDENTITY.entities
DATETIME = _DATETIME.entities
URL = _URL.entities
TEXT_URL = _TEXT_URL.entities
EMAIL = _EMAIL.entities
PHONE = _PHONE.entities
BANK_CARD = _BANK_CARD.entities
CUSTOM_EMOJI = _CUSTOM_EMOJI.entities
MEDIA_TIMESTAMP = _MEDIA_TIMESTAMP.entities
_PROVIDER_TYPED = frozenset((*URL, *TEXT_URL, *EMAIL, *PHONE, *BANK_CARD, *CUSTOM_EMOJI, *MEDIA_TIMESTAMP))
# TDLib's JSON writes an int64 as a decimal string; an integer, sign, padding or non-ASCII digit is refused.
_INT64_STRING = re.compile(r'[1-9][0-9]{0,18}')
# Extended_Pictographic (Unicode emoji-data): the bases of one emoji. Skin-tone modifiers, regional indicators and
# keycap bases are not pictographs; they form an emoji only in the shapes _one_emoji admits.
_PICTOGRAPHS = (
    (0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0),
    (0x25FB, 0x25FE), (0x2600, 0x2605), (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734),
    (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F), (0x1F12F, 0x1F12F),
    (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5),
    (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F3FA), (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F),
    (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD))
_PRECISIONS = frozenset(('dateTimePartPrecisionNone','dateTimePartPrecisionShort','dateTimePartPrecisionLong'))
_CODE = frozenset(('textEntityTypeCode','textEntityTypePre','textEntityTypePreCode'))
_QUOTES = frozenset(('textEntityTypeBlockQuote','textEntityTypeExpandableBlockQuote'))


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _inert_json_string(value):
    """One reversible JSON layer; only ASCII words remain literal in values."""
    parts=['"']
    for char in value:
        code=ord(char)
        if ('A'<=char<='Z' or 'a'<=char<='z' or '0'<=char<='9' or char in '_-'):
            parts.append(char)
        elif code<=0xffff:
            parts.append(f'\\u{code:04x}')
        else:
            scalar=code-0x10000
            parts.append(f'\\u{0xd800+(scalar>>10):04x}\\u{0xdc00+(scalar&0x3ff):04x}')
    parts.append('"')
    return ''.join(parts)


def _inert_json(value):
    """Encode only the closed inert display record, keeping fixed keys ordinary."""
    if type(value) is str:
        return _inert_json_string(value)
    if type(value) is dict:
        return '{'+','.join(_json(key)+':'+_inert_json(value[key]) for key in sorted(value))+'}'
    if type(value) is list:
        return '['+','.join(_inert_json(item) for item in value)+']'
    return _json(value)


def _text_url(value):
    """Bound an opaque provider assertion without interpreting its contents."""
    if type(value) is not str or len(value)>4096:
        raise ValueError('invalid formatted reply text url')
    value.encode('utf-8')
    return value


def _custom_emoji_id(value):
    """An int64 custom emoji id exactly as TDLib's JSON writes it: a positive decimal string, at most 2**63-1."""
    if type(value) is not str or _INT64_STRING.fullmatch(value) is None or int(value)>2**63-1:
        raise ValueError('invalid formatted reply custom emoji')
    return value


def _one_emoji(covered):
    """Whether the covered text is exactly one emoji (td_api.tl: the text behind a custom emoji must be an emoji).

    A deliberate local subset of TDLib's is_emoji, never a font or sticker lookup: one element, a pictograph with at
    most one skin-tone modifier, a lone skin-tone modifier, a keycap or a two-letter regional flag, plus at most one
    trailing VS16, which TDLib ignores. ZWJ sequences and tag flags never get here: their format characters fail the
    visible-text control policy."""
    if not 1<=len(covered)<=3:  # the longest admitted shapes: pictograph, modifier, VS16; keycap; flag, VS16
        return False
    body=covered[:-1] if covered[-1]=='\ufe0f' else covered
    if covered[0] in '0123456789#*' and covered[1:] in ('\u20e3','\ufe0f\u20e3'):
        return True
    codes=[ord(c) for c in body]
    if len(codes)==2 and all(0x1F1E6<=c<=0x1F1FF for c in codes):
        return True
    if not codes:
        return False
    first,rest=codes[0],codes[1:]
    modifier=0x1F3FB<=first<=0x1F3FF
    if not modifier and not any(low<=first<=high for low,high in _PICTOGRAPHS):
        return False
    if rest and not modifier and 0x1F3FB<=rest[0]<=0x1F3FF:
        rest=rest[1:]
    return not rest


def _media_timestamp(value, text, offset, length, bound):
    """A media timestamp exactly on one whole token of the text, with that token's value, from 0 to the source's own
    media duration (TDLib drops one beyond it, and only a voice note has a duration here)."""
    if (type(bound) is not int or type(value) is not int or not 0<=value<=bound or
            media_timestamp_tokens(text).get((offset,length))!=value):
        raise ValueError('invalid formatted reply media timestamp')
    return value


def _text(value, *, language=False):
    if type(value) is not str:
        raise ValueError('invalid formatted reply source')
    if language:
        if len(value)>64 or len(value.encode('utf-8'))>128:
            raise ValueError('invalid formatted reply language')
    elif not 0<len(value)<=4096 or not value.strip():
        raise ValueError('invalid formatted reply source')
    if any(unicodedata.category(c).startswith('C') and (language or c not in '\n\t') for c in value):
        raise ValueError('invalid formatted reply source')
    value.encode('utf-8')
    return value


def _boundaries(text):
    """Map original UTF-16 boundaries to Python scalar indices, without normalization."""
    boundaries={0:0};units=0
    for index,char in enumerate(text):
        units+=2 if ord(char)>0xffff else 1
        boundaries[units]=index+1
    return boundaries


def _datetime_formatting(value, *, relative=False, absolute=False):
    if value is None:
        return None
    if type(value) is dict and type(value.get('@type')) is str:
        if relative and set(value)=={'@type'} and value['@type']=='dateTimeFormattingTypeRelative':
            return {'@type':'dateTimeFormattingTypeRelative'}
        if (absolute and set(value)=={'@type','time_precision','date_precision','show_day_of_week'}
                and value['@type']=='dateTimeFormattingTypeAbsolute' and type(value['show_day_of_week']) is bool):
            # Deliberate local subset: explicit closed precision constructors.
            # No inference about TDLib's nullable object parser or rendering.
            for key in ('time_precision','date_precision'):
                precision=value[key]
                if (type(precision) is not dict or set(precision)!={'@type'} or
                        type(precision['@type']) is not str or precision['@type'] not in _PRECISIONS):
                    raise ValueError('invalid formatted reply date time')
            return {'@type':'dateTimeFormattingTypeAbsolute',
                'time_precision':{'@type':value['time_precision']['@type']},
                'date_precision':{'@type':value['date_precision']['@type']},
                'show_day_of_week':value['show_day_of_week']}
    raise ValueError('invalid formatted reply date time')


def formatted_text_for(value, admission: Admission, *, max_media_timestamp=None):
    """Validate exact TDLib entities of the admitted families and return a fresh canonically ordered shape.

    `max_media_timestamp` is the source's own media duration; without it a media timestamp is refused."""
    if (type(value) is not dict or set(value)!={'@type','text','entities'} or
            value['@type']!='formattedText' or type(value['entities']) is not list or
            not 1<=len(value['entities'])<=32):
        raise ValueError('invalid formatted reply source')
    allowed=admission.entities
    text=_text(value['text']);boundaries=_boundaries(text);entities=[];seen=set()
    for entity in value['entities']:
        if type(entity) is not dict or set(entity)!={'@type','offset','length','type'} or entity['@type']!='textEntity':
            raise ValueError('invalid formatted reply entity')
        offset=entity['offset'];length=entity['length'];typ=entity['type']
        if (type(offset) is not int or type(length) is not int or
                not 0<=offset<2**31 or not 1<=length<2**31 or
                offset not in boundaries or offset+length not in boundaries or
                type(typ) is not dict or type(typ.get('@type')) is not str or typ['@type'] not in allowed):
            raise ValueError('invalid formatted reply entity')
        kind=typ['@type'];language='';user_id=None;unix_time=None;formatting_type=None;provider_url=None
        emoji_id=None;timestamp=None
        keys=({'@type','language'} if kind=='textEntityTypePreCode' else {'@type','user_id'} if kind=='textEntityTypeMentionName'
              else {'@type','url'} if kind in TEXT_URL else {'@type','custom_emoji_id'} if kind in CUSTOM_EMOJI
              else {'@type','media_timestamp'} if kind in MEDIA_TIMESTAMP else {'@type'})
        if kind in DATETIME:
            if not {'@type','unix_time'}<=set(typ)<={'@type','unix_time','formatting_type'}:
                raise ValueError('invalid formatted reply entity')
            unix_time=typ['unix_time']
            if type(unix_time) is not int or not -(2**31)<=unix_time<2**31:
                raise ValueError('invalid formatted reply date time')
            formatting_type=_datetime_formatting(typ.get('formatting_type'),
                relative=admission.relative,absolute=admission.absolute)
        elif set(typ)!=keys:
            raise ValueError('invalid formatted reply entity')
        if kind=='textEntityTypePreCode':language=_text(typ['language'],language=True)
        if kind in TEXT_URL:provider_url=_text_url(typ['url'])
        if kind in CUSTOM_EMOJI:
            emoji_id=_custom_emoji_id(typ['custom_emoji_id'])
            if not _one_emoji(text[boundaries[offset]:boundaries[offset+length]]):
                raise ValueError('invalid formatted reply custom emoji')
        if kind in MEDIA_TIMESTAMP:
            timestamp=_media_timestamp(typ['media_timestamp'],text,offset,length,max_media_timestamp)
        if kind=='textEntityTypeMentionName':
            user_id=typ['user_id']
            if type(user_id) is not int or not 0<user_id<2**53:
                raise ValueError('invalid formatted reply identity')
        key=(offset,length,kind,language,user_id,unix_time,provider_url,emoji_id,timestamp)
        if key in seen:raise ValueError('duplicate formatted reply entity')
        seen.add(key)
        entities.append({'@type':'textEntity','offset':offset,'length':length,
            'type':{'@type':kind,**({'language':language} if kind=='textEntityTypePreCode' else {}),
                    **({'user_id':user_id} if kind=='textEntityTypeMentionName' else {}),
                    **({'url':provider_url} if kind in TEXT_URL else {}),
                    **({'custom_emoji_id':emoji_id} if kind in CUSTOM_EMOJI else {}),
                    **({'media_timestamp':timestamp} if kind in MEDIA_TIMESTAMP else {}),
                    **({'unix_time':unix_time,'formatting_type':formatting_type} if kind in DATETIME else {})}})
    entities.sort(key=lambda e:(e['offset'],-e['length'],e['type']['@type'],e['type'].get('language',''),e['type'].get('user_id',0),
                                e['type'].get('custom_emoji_id',''),e['type'].get('media_timestamp',0)))
    exclusive=EXCLUSIVE_TYPES
    for index,left in enumerate(entities):
        start=left['offset'];end=start+left['length'];kind=left['type']['@type']
        for right in entities[index+1:]:
            rstart=right['offset'];rend=rstart+right['length'];rkind=right['type']['@type']
            if rstart>=end:break
            if (kind in DATETIME or rkind in DATETIME or kind in _CODE or rkind in _CODE or (kind in _QUOTES and rkind in _QUOTES) or
                    (kind in exclusive and rkind in exclusive) or
                    (kind in exclusive and rkind in _QUOTES) or (kind in _QUOTES and rkind in exclusive)):
                raise ValueError('excluded formatted reply overlap')
            if not (start<=rstart and rend<=end):
                raise ValueError('crossing formatted reply entities')
    return {'@type':'formattedText','text':text,'entities':entities}


def validate_content_for(value, admission: Admission):
    if (type(value) is not dict or set(value)!={'@type','text','link_preview','link_preview_options'} or
            value['@type']!='messageText' or value['link_preview'] is not None):
        raise ValueError('invalid formatted reply content')
    # Link option validation belongs to the unchanged message shell parser.
    if formatted_text_for(value['text'],admission)!=value['text']:
        raise ValueError('noncanonical formatted reply entities')


def render_formatted_text_for(formatted, admission: Admission, *, max_media_timestamp=None):
    """Complete span evidence, deriving covered text before display sanitation."""
    if formatted_text_for(formatted,admission,max_media_timestamp=max_media_timestamp)!=formatted:
        raise ValueError('noncanonical formatted reply entities')
    labels=admission.entities
    text=formatted['text'];boundaries=_boundaries(text)
    inert=any(family.inert for family in present(formatted['entities']))
    safe=text if inert else sanitize_telegram_text(text,max_length=65537)
    changed=_inert_json_string(text)!=_json(text) if inert else safe!=text;entities=[]
    for entity in formatted['entities']:
        offset=entity['offset'];length=entity['length'];typ=entity['type']
        covered=text[boundaries[offset]:boundaries[offset+length]]
        span=covered if inert else sanitize_telegram_text(covered,max_length=65537)
        changed|=_inert_json_string(covered)!=_json(covered) if inert else span!=covered
        record={'type':labels[typ['@type']],'offset':offset,'length':length,'text':span}
        if typ['@type']=='textEntityTypePreCode':
            language=typ['language'] if inert else sanitize_telegram_text(typ['language'],max_length=65537)
            changed|=_inert_json_string(typ['language'])!=_json(typ['language']) if inert else language!=typ['language'];record['language']=language
        if typ['@type']=='textEntityTypeMentionName':record['user_id']=typ['user_id']
        if typ['@type'] in _PROVIDER_TYPED:record['provider_type']=typ['@type']
        if typ['@type'] in TEXT_URL:
            record['provider_url']=typ['url']
            changed|=_inert_json_string(typ['url'])!=_json(typ['url'])
        if typ['@type'] in CUSTOM_EMOJI:record['custom_emoji_id']=typ['custom_emoji_id']  # ASCII digits only
        if typ['@type'] in MEDIA_TIMESTAMP:record['media_timestamp']=typ['media_timestamp']
        if typ['@type'] in DATETIME:
            record.update(provider_type=typ['@type'],unix_time=typ['unix_time'],
                formatting_type=_datetime_formatting(typ['formatting_type'],
                    relative=admission.relative,absolute=admission.absolute))
        entities.append(record)
    return {'text':safe,'offset_basis':OFFSET_BASIS,'entities':entities},changed


def render_content_for(value, marker, admission: Admission):
    validate_content_for(value,admission)
    record,changed=render_formatted_text_for(value['text'],admission)
    inert=any(family.inert for family in present(value['text']['entities']))
    display=marker+'Formatted target: '+(_inert_json(record) if inert else _json(record))
    if len(display)>4096 or sanitize_telegram_text(display,max_length=4097)!=display:
        raise ValueError('formatted reply evidence exceeds preview bound')
    return display,changed


# Keyword shims: the 0.57 admission contract (styles always; each truthy flag adds its family).

def formatted_text(value, *, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False, text_url=False):
    """Validate exact TDLib entities and return a fresh canonically ordered shape."""
    return formatted_text_for(value,Admission.of(lexical=lexical,identity=identity,datetime=datetime,
        datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,
        bank_card=bank_card,text_url=text_url))


def validate_content(value, *, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False, text_url=False):
    validate_content_for(value,Admission.of(lexical=lexical,identity=identity,datetime=datetime,
        datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,
        bank_card=bank_card,text_url=text_url))


def render_formatted_text(formatted, *, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False, text_url=False):
    """Complete span evidence, deriving covered text before display sanitation."""
    return render_formatted_text_for(formatted,Admission.of(lexical=lexical,identity=identity,datetime=datetime,
        datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,
        bank_card=bank_card,text_url=text_url))


def render_content(value, marker, *, lexical=False, identity=False, datetime=False, datetime_relative=False, datetime_absolute=False, url=False, email=False, phone=False, bank_card=False, text_url=False):
    return render_content_for(value,marker,Admission.of(lexical=lexical,identity=identity,datetime=datetime,
        datetime_relative=datetime_relative,datetime_absolute=datetime_absolute,url=url,email=email,phone=phone,
        bank_card=bank_card,text_url=text_url))
