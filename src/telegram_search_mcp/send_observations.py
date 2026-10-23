"""Bounded volatile content-free exact TDLib send observations.

No payload, text, caption, path, or provider error string is retained. A snapshot
is evidence for one provider instance, never a lease or permission to retry.
"""
from __future__ import annotations
import base64
import hashlib
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable
from .reply_drafts import LINK_OPTIONS, strict_shape
from .outgoing_formatting import FrozenOutgoingEntities, freeze_outgoing_entities

RETENTION_SECONDS = 900
MAX_OBSERVATIONS = 4096

class ObservationUnavailable(RuntimeError):
    pass

@dataclass(frozen=True)
class SendObservation:
    attempt_id: str
    extra: str
    sending_id: int
    chat_id: int
    content_type: str
    started_at: float
    text_sha256: str | None = None
    temporary_id: int | None = None
    status: str = "registered"
    message_id: int | None = None
    reply_anchor: tuple[int, int] | None = None
    caption_sha256: str | None = None
    voice_duration: int | None = None
    waveform_sha256: str | None = None
    waveform_size: int | None = None
    forum_topic_id: int | None = None
    outgoing_entities: FrozenOutgoingEntities = ()
    saved_messages_topic_id: int | None = None
    forward: bool = False
    forward_self: bool = False  # Saved Messages: the provider may mark a forwarded copy incoming.
    video: tuple[int, int, int, int] | None = None  # Reviewed (duration, width, height, size); registration only.


FORWARD_CONTENT_TYPES = frozenset({'messageText', 'messagePhoto', 'messageDocument', 'messageVoiceNote',
                                   'messageVideo', 'messageAudio', 'messageAnimation'})
# Album items (0.56): photos and videos mixed, or documents only; audio is deferred.
ALBUM_CONTENT_GROUPS = (frozenset({'messagePhoto', 'messageVideo'}), frozenset({'messageDocument'}))
# Terminal updates that overtake the album's `messages` result, kept until it binds; bounded.
MAX_EARLY_ALBUM_UPDATES = 16


@dataclass(frozen=True)
class AlbumSlot:
    """One album item: bound to its temporary message ID only by the exact `messages` result."""
    content_type: str
    temporary_id: int | None = None
    status: str = "pending"  # pending | sent | failed | conflict
    message_id: int | None = None


@dataclass(frozen=True)
class AlbumObservation:
    """One sendMessageAlbum attempt with N slots in send order.

    `registered` until the exact result arrives; `bound` once N pending messages with this sending_id, one
    negative media_album_id and the content types in input order bind each slot to its temporary ID; `failed`
    when the request itself was rejected (no message was created); `conflict` on any mismatch, after which
    every slot stays unknown. A slot becomes terminal only through updateMessageSendSucceeded or
    updateMessageSendFailed for its bound temporary ID; deletion or absence never proves failure."""
    attempt_id: str
    extra: str
    sending_id: int
    chat_id: int
    content_types: tuple[str, ...]
    started_at: float
    status: str = "registered"
    slots: tuple[AlbumSlot, ...] = ()
    media_album_id: int | None = None
    early: tuple[tuple[int, str, int | None, str], ...] = ()  # (old ID, sent|failed|conflict, final ID, type)


def _wire_int64(value):
    """TDLib's JSON int64 (a decimal string), or None."""
    if type(value) is not str or not 1 <= len(value) <= 20:
        return None
    digits = value[1:] if value.startswith('-') else value
    if not digits.isascii() or not digits.isdigit() or (len(digits) > 1 and digits.startswith('0')) or value == '-0':
        return None
    parsed = int(value)
    return parsed if -(1 << 63) <= parsed < (1 << 63) else None


def _album_message(value, chat_id, content_type, *, final=False):
    return (isinstance(value, dict) and value.get('@type') == 'message'
            and integer(value.get('chat_id')) and value['chat_id'] == chat_id
            and integer(value.get('id'), positive=final) and value.get('is_outgoing') is True
            and isinstance(value.get('content'), dict) and value['content'].get('@type') == content_type
            and (not final or (value.get('sending_state') is None and
                               (content_type != 'messageVideo' or _video_matches(value['content'])))))


def _album_terminal(event, chat_id, content_type):
    """'sent' with the final ID, or 'failed', for an exact terminal update of `content_type`; else 'conflict'."""
    value = event.get('message')
    if event.get('@type') == 'updateMessageSendSucceeded':
        return ('sent', value['id']) if _album_message(value, chat_id, content_type, final=True) else ('conflict', None)
    error = event.get('error')
    state = value.get('sending_state') if isinstance(value, dict) else None
    inner = state.get('error') if isinstance(state, dict) else None
    if (_album_message(value, chat_id, content_type) and isinstance(error, dict) and error.get('@type') == 'error'
            and type(error.get('code')) is int and 0 < error['code'] < 2**31
            and isinstance(state, dict) and state.get('@type') == 'messageSendingStateFailed'
            and isinstance(inner, dict) and inner.get('@type') == 'error'
            and type(inner.get('code')) is int and inner['code'] == error['code']):
        return ('failed', None)
    return ('conflict', None)


def _album_binding(event, observation):
    """(temporary IDs, media_album_id) from the exact `messages` result, or None for any mismatch."""
    count = len(observation.content_types)
    messages = event.get('messages')
    if not (event.get('@type') == 'messages' and type(event.get('total_count')) is int and
            event['total_count'] == count and type(messages) is list and len(messages) == count):
        return None
    temporary, album_ids = [], set()
    for value, content_type in zip(messages, observation.content_types):
        state = value.get('sending_state') if isinstance(value, dict) else None
        if (not _album_message(value, observation.chat_id, content_type) or not isinstance(state, dict)
                or set(state) != {'@type', 'sending_id'} or state.get('@type') != 'messageSendingStatePending'
                or type(state.get('sending_id')) is not int or state['sending_id'] != observation.sending_id):
            return None
        album_id = _wire_int64(value.get('media_album_id'))
        if album_id is None or album_id >= 0:
            return None
        temporary.append(value['id'])
        album_ids.add(album_id)
    if len(set(temporary)) != count or len(album_ids) != 1:
        return None
    return tuple(temporary), album_ids.pop()


def _reduce_album(old, event):
    if old.status in ('failed', 'conflict'):
        return old
    kind = event.get('@type')
    if event.get('@extra') == old.extra:
        if old.status != 'registered':
            return replace(old, status='conflict')  # One request has exactly one result.
        if kind == 'error' and type(event.get('code')) is int and 0 < event['code'] < 2**31:
            return replace(old, status='failed')
        binding = _album_binding(event, old)
        if binding is None:
            return replace(old, status='conflict')
        temporary, album_id = binding
        slots = [AlbumSlot(content_type, temporary_id) for content_type, temporary_id in zip(old.content_types, temporary)]
        for old_id, status, message_id, content_type in old.early:
            for index, slot in enumerate(slots):
                if slot.temporary_id == old_id and slot.status == 'pending':
                    slots[index] = (replace(slot, status=status, message_id=message_id)
                                    if content_type == slot.content_type else replace(slot, status='conflict'))
        return replace(old, status='bound', slots=tuple(slots), media_album_id=album_id, early=())
    if kind not in ('updateMessageSendSucceeded', 'updateMessageSendFailed') or not integer(event.get('old_message_id')):
        return old  # updateNewMessage never binds; deletion or absence never ends a slot.
    old_id = event['old_message_id']
    if old.status == 'registered':
        value = event.get('message')
        content_type = value.get('content', {}).get('@type') if isinstance(value, dict) and isinstance(
            value.get('content'), dict) else None
        if any(row[0] == old_id for row in old.early):  # Two ends for one message contradict each other.
            return replace(old, early=tuple(row if row[0] != old_id else (old_id, 'conflict', None, row[3])
                                            for row in old.early))
        if content_type not in old.content_types or len(old.early) >= MAX_EARLY_ALBUM_UPDATES:
            return old
        status, message_id = _album_terminal(event, old.chat_id, content_type)
        return replace(old, early=(*old.early, (old_id, status, message_id, content_type)))
    for index, slot in enumerate(old.slots):
        if slot.temporary_id == old_id:
            if slot.status != 'pending':
                return old  # Terminal is final.
            status, message_id = _album_terminal(event, old.chat_id, slot.content_type)
            slots = list(old.slots)
            slots[index] = replace(slot, status=status, message_id=message_id)
            return replace(old, slots=tuple(slots))
    return old


def integer(value, *, positive=False):
    return type(value) is int and -(2**53) < value < 2**53 and (value > 0 if positive else value != 0)


def _shape(value,kind,required,nullable=()):
    return (isinstance(value,dict) and value.get('@type')==kind
        and set(value)-{'@type',*required,*nullable}==set()
        and {'@type',*required}<=set(value))

def _number(value,maximum=2**53-1,minimum=0):
    return type(value) is int and minimum<=value<=maximum

def _string(value,maximum=65536):
    if type(value) is not str or len(value)>maximum:return False
    try:return len(value.encode('utf-8'))<=maximum
    except UnicodeError:return False

def _blob(value,maximum=65536):
    if not _string(value,maximum*2):return None
    try:
        raw=base64.b64decode(value,validate=True)
        return raw if len(raw)<=maximum else None
    except ValueError:return None

def _file(value):
    if not _shape(value,'file',('id','size','expected_size','local','remote')):return False
    if not (_number(value['id'],2**31-1,1) and _number(value['size']) and _number(value['expected_size'])):return False
    local=value['local'];remote=value['remote']
    if not _shape(local,'localFile',('path','can_be_downloaded','can_be_deleted','is_downloading_active','is_downloading_completed','download_offset','downloaded_prefix_size','downloaded_size')):return False
    if not (_string(local['path'],4096) and all(type(local[k]) is bool for k in ('can_be_downloaded','can_be_deleted','is_downloading_active','is_downloading_completed'))
        and all(_number(local[k]) for k in ('download_offset','downloaded_prefix_size','downloaded_size'))):return False
    return (_shape(remote,'remoteFile',('id','unique_id','is_uploading_active','is_uploading_completed','uploaded_size'))
        and _string(remote['id'],4096) and _string(remote['unique_id'],4096)
        and type(remote['is_uploading_active']) is bool and type(remote['is_uploading_completed']) is bool
        and _number(remote['uploaded_size']))

def _minithumbnail(value):
    return (value is None or (_shape(value,'minithumbnail',('width','height','data'))
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _blob(value['data']) is not None))

def _thumbnail(value):
    if value is None:return True
    if not _shape(value,'thumbnail',('format','width','height','file')):return False
    fmt=value['format']
    return (isinstance(fmt,dict) and set(fmt)=={'@type'} and type(fmt['@type']) is str and fmt['@type'] in {
        'thumbnailFormatJpeg','thumbnailFormatGif','thumbnailFormatMpeg4','thumbnailFormatPng','thumbnailFormatTgs','thumbnailFormatWebm','thumbnailFormatWebp'}
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _file(value['file']))

def _artifact_matches(content,observation):
    caption=content.get('caption')
    if not (_shape(caption,'formattedText',('text','entities')) and type(caption['text']) is str
        and len(caption['text'])<=1024 and caption['entities']==[]):return False
    try:
        if hashlib.sha256(caption['text'].encode('utf-8')).hexdigest()!=observation.caption_sha256:return False
    except UnicodeError:return False
    if observation.content_type=='messageDocument':
        if not _shape(content,'messageDocument',('document','caption')):return False
        doc=content['document']
        return (_shape(doc,'document',('file_name','mime_type','document'),('minithumbnail','thumbnail'))
            and _string(doc['file_name'],255) and _string(doc['mime_type'],127)
            and _minithumbnail(doc.get('minithumbnail')) and _thumbnail(doc.get('thumbnail')) and _file(doc['document']))
    if observation.content_type=='messagePhoto':
        if not (_shape(content,'messagePhoto',('photo','caption','show_caption_above_media','has_spoiler','is_secret'),('video',))
            and content.get('video') is None and all(content[k] is False for k in ('show_caption_above_media','has_spoiler','is_secret'))):return False
        photo=content['photo']
        if not (_shape(photo,'photo',('has_stickers','sizes'),('minithumbnail',)) and photo['has_stickers'] is False
            and _minithumbnail(photo.get('minithumbnail')) and type(photo['sizes']) is list and 1<=len(photo['sizes'])<=20):return False
        for size in photo['sizes']:
            if not (_shape(size,'photoSize',('type','photo','width','height','progressive_sizes'))
                and _string(size['type'],16) and _number(size['width'],16384,1) and _number(size['height'],16384,1)
                and type(size['progressive_sizes']) is list and len(size['progressive_sizes'])<=100
                and all(_number(n,2**31-1) for n in size['progressive_sizes']) and _file(size['photo'])):return False
        return True
    if not (_shape(content,'messageVoiceNote',('voice_note','caption','is_listened')) and type(content['is_listened']) is bool):return False
    voice=content['voice_note']
    if not (_shape(voice,'voiceNote',('duration','waveform','mime_type','voice'),('speech_recognition_result',))
        and _number(voice['duration'],600,1) and voice['duration']==observation.voice_duration
        and voice['mime_type']=='audio/ogg' and voice.get('speech_recognition_result') is None and _file(voice['voice'])):return False
    waveform=_blob(voice['waveform'],100)
    return (waveform is not None and len(waveform)==observation.waveform_size
        and hashlib.sha256(waveform).hexdigest()==observation.waveform_sha256)

def _video_matches(content):
    """Like document and photo sends, correlation (sending_id, chat, temporary ID) decides: the
    final message only has to be a messageVideo carrying a video object. The provider may
    re-encode, so its reported duration, dimensions and size are not compared with the draft."""
    video = content.get('video')
    return isinstance(video, dict) and video.get('@type') == 'video'


class SendObservations:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 capacity: int = MAX_OBSERVATIONS):
        if type(capacity) is not int or not 1 <= capacity <= MAX_OBSERVATIONS:
            raise ValueError("invalid observation capacity")
        self._clock = clock
        self._capacity = capacity
        self._entries: dict[str, SendObservation] = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = self._clock()
        self._entries = {key: value for key,value in self._entries.items()
                         if now - value.started_at < RETENTION_SECONDS}

    def invalidate(self):
        with self._lock:
            self._entries.clear()

    def register(self, attempt_id, extra, sending_id, chat_id, content_type, *, text_sha256=None, reply_anchor=None, caption_sha256=None, voice_duration=None, waveform_sha256=None, waveform_size=None, forum_topic_id=None, outgoing_entities: FrozenOutgoingEntities = (), saved_messages_topic_id=None, forward=False, forward_self=False, video=None):
        if type(outgoing_entities) is not tuple or len(outgoing_entities) > 32:
            raise ObservationUnavailable('invalid frozen outgoing expectation')
        # A native video correlates by sending_id, chat and returned temporary ID only; the
        # reviewed facts bound the registration but are not compared with the final message.
        # It is never a reply or a topic send.
        if video is not None:
            if (type(video) is not tuple or len(video) != 4 or content_type != 'messageVideo' or forward or
                    not _number(video[0], 3 * 3600, 1) or not _number(video[1], 8192, 16) or
                    not _number(video[2], 8192, 16) or not _number(video[3], 64 * 1024 * 1024, 1) or
                    any(value is not None for value in (text_sha256, reply_anchor, caption_sha256, voice_duration,
                        waveform_sha256, waveform_size, forum_topic_id, saved_messages_topic_id)) or outgoing_entities):
                raise ObservationUnavailable('invalid video observation')
        elif content_type == 'messageVideo' and not forward:
            raise ObservationUnavailable('video observation requires reviewed facts')
        # Forward content is provider-produced: correlate by sending_id, chat and
        # returned temporary ID only; no outgoing content hash can apply.
        if type(forward) is not bool or type(forward_self) is not bool or (forward_self and (
                not forward or not integer(chat_id, positive=True))) or (forward and (
                content_type not in FORWARD_CONTENT_TYPES or not integer(chat_id) or
                any(value is not None for value in (text_sha256, reply_anchor, caption_sha256, voice_duration,
                    waveform_sha256, waveform_size, forum_topic_id, saved_messages_topic_id)) or outgoing_entities)):
            raise ObservationUnavailable('invalid forward observation')
        if outgoing_entities:
            styles = {'textEntityTypeBold', 'textEntityTypeItalic', 'textEntityTypeUnderline',
                      'textEntityTypeStrikethrough', 'textEntityTypeSpoiler'}
            if (content_type != 'messageText' or caption_sha256 is not None or
                    type(text_sha256) is not str or len(text_sha256) != 64 or
                    any(c not in '0123456789abcdef' for c in text_sha256) or
                    any(type(row) is not tuple or len(row) != 3 or
                        type(row[0]) is not int or type(row[1]) is not int or
                        not 0 <= row[0] < 8192 or not 0 < row[1] <= 8192 or
                        row[0] + row[1] > 8192 or type(row[2]) is not str or row[2] not in styles
                        for row in outgoing_entities)):
                raise ObservationUnavailable('invalid formatted text expectation')
            if tuple(sorted(outgoing_entities, key=lambda row: (row[0], -row[1], row[2]))) != outgoing_entities:
                raise ObservationUnavailable('noncanonical outgoing expectation')
            for index, (offset, length, kind) in enumerate(outgoing_entities):
                for other_offset, other_length, other_kind in outgoing_entities[index + 1:]:
                    if ((kind == other_kind and other_offset <= offset + length) or
                            offset < other_offset < offset + length < other_offset + other_length):
                        raise ObservationUnavailable('invalid overlapping expectation')
        if forum_topic_id is not None and (not _number(forum_topic_id, 2**31-1, 1) or
                content_type != 'messageText' or caption_sha256 is not None or
                type(text_sha256) is not str or len(text_sha256) != 64 or
                any(c not in '0123456789abcdef' for c in text_sha256)):
            raise ObservationUnavailable('invalid topic observation')
        if reply_anchor is not None and (type(reply_anchor) is not tuple or len(reply_anchor)!=2 or
                not integer(reply_anchor[0]) or reply_anchor[0]!=chat_id or not integer(reply_anchor[1],positive=True)):
            raise ObservationUnavailable("invalid reply observation")
        if saved_messages_topic_id is not None and (
                not integer(saved_messages_topic_id, positive=True) or saved_messages_topic_id != chat_id or
                not outgoing_entities or content_type != 'messageText' or
                reply_anchor is not None or forum_topic_id is not None or caption_sha256 is not None):
            raise ObservationUnavailable('invalid saved messages observation')
        if caption_sha256 is not None:
            if (reply_anchor is None or content_type not in {'messageDocument','messagePhoto','messageVoiceNote'}
                or type(caption_sha256) is not str or len(caption_sha256)!=64
                or any(c not in '0123456789abcdef' for c in caption_sha256)):
                raise ObservationUnavailable('invalid artifact reply expectation')
            if content_type=='messageVoiceNote' and not (_number(voice_duration,600,1)
                and _number(waveform_size,100,1) and type(waveform_sha256) is str and len(waveform_sha256)==64
                and all(c in '0123456789abcdef' for c in waveform_sha256)):
                raise ObservationUnavailable('invalid voice reply expectation')
        with self._lock:
            self._prune()
            if (attempt_id in self._entries or len(self._entries) >= self._capacity or
                    any(v.sending_id == sending_id or v.extra == extra for v in self._entries.values())):
                raise ObservationUnavailable("send observation capacity or correlation is unavailable")
            self._entries[attempt_id] = SendObservation(attempt_id,extra,sending_id,chat_id,content_type,self._clock(),text_sha256,reply_anchor=reply_anchor,caption_sha256=caption_sha256,voice_duration=voice_duration,waveform_sha256=waveform_sha256,waveform_size=waveform_size,forum_topic_id=forum_topic_id,outgoing_entities=outgoing_entities,saved_messages_topic_id=saved_messages_topic_id,forward=forward,forward_self=forward_self,video=video)

    def register_album(self, attempt_id, extra, sending_id, chat_id, content_types):
        """One album attempt: its N content types in send order, correlated like a single send."""
        if (type(content_types) is not tuple or not 2 <= len(content_types) <= 10 or
                not any(set(content_types) <= group for group in ALBUM_CONTENT_GROUPS) or
                not integer(chat_id) or type(sending_id) is not int or not 0 < sending_id < 2**31 or
                type(attempt_id) is not str or not attempt_id or type(extra) is not str or not extra):
            raise ObservationUnavailable('invalid album observation')
        with self._lock:
            self._prune()
            if (attempt_id in self._entries or len(self._entries) >= self._capacity or
                    any(v.sending_id == sending_id or v.extra == extra for v in self._entries.values())):
                raise ObservationUnavailable("send observation capacity or correlation is unavailable")
            self._entries[attempt_id] = AlbumObservation(attempt_id, extra, sending_id, chat_id, content_types,
                self._clock(), slots=tuple(AlbumSlot(content_type) for content_type in content_types))

    def discard_unattempted(self, attempt_id):
        """Remove a registration refused before transport under the provider lock.

        Never remove pending or terminal provider evidence. This is only for the
        local gap between registering correlation and attempting rawsend.
        """
        with self._lock:
            observation=self._entries.get(attempt_id)
            if observation is not None and observation.status=="registered":
                self._entries.pop(attempt_id)

    def sending_id_available(self, sending_id):
        with self._lock:
            self._prune()
            return not any(v.sending_id == sending_id for v in self._entries.values())

    def snapshot(self, attempt_id):
        with self._lock:
            self._prune()
            return self._entries.get(attempt_id)

    @staticmethod
    def _text_matches(content, expected_sha256, outgoing_entities=()):
        if expected_sha256 is None:
            return True
        formatted = content.get("text")
        if not isinstance(formatted, dict) or formatted.get("@type") != "formattedText":
            return False
        text = formatted.get("text")
        if not isinstance(text, str) or len(text) > 4096:
            return False
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            return False
        if hashlib.sha256(encoded).hexdigest() != expected_sha256:
            return False
        if outgoing_entities:
            if set(formatted) != {'@type', 'text', 'entities'}:
                return False
            rows = formatted.get('entities')
            if type(rows) is not list or len(rows) > 32:
                return False
            caller = []
            for row in rows:
                if (type(row) is not dict or set(row) != {'@type', 'offset', 'length', 'type'} or
                        row.get('@type') != 'textEntity'):
                    return False
                caller.append({key: row[key] for key in ('offset', 'length', 'type')})
            try:
                return freeze_outgoing_entities(text, caller) == outgoing_entities
            except (ValueError, TypeError, UnicodeError):
                return False
        return True

    @staticmethod
    def _reply_matches(value, anchor, forum_topic_id=None):
        if anchor is None:
            return True
        reply=value.get("reply_to")
        return ((value.get("topic_id") is None if forum_topic_id is None else
                strict_shape(value.get('topic_id'), {'@type': 'messageTopicForum',
                    'forum_topic_id': forum_topic_id})) and isinstance(reply,dict)
            and not set(reply)-{"@type","chat_id","message_id","quote","checklist_task_id","poll_option_id","origin","origin_send_date","content"}
            and reply.get("@type")=="messageReplyToMessage"
            and type(reply.get("chat_id")) is int and reply["chat_id"]==anchor[0]
            and type(reply.get("message_id")) is int and reply["message_id"]==anchor[1]
            and type(reply.get("checklist_task_id")) is int and reply["checklist_task_id"]==0
            and type(reply.get("poll_option_id")) is str and reply["poll_option_id"]==""
            and type(reply.get("origin_send_date")) is int and reply["origin_send_date"]==0
            and all(reply.get(k) is None for k in ("quote","origin","content")))

    @staticmethod
    def _message(value, observation, *, final=False):
        return (isinstance(value,dict) and value.get("@type") == "message"
            and integer(value.get("chat_id")) and value["chat_id"] == observation.chat_id
            and integer(value.get("id"),positive=final)
            and (value.get("is_outgoing") is True or (observation.forward_self and value.get("is_outgoing") is False))
            and isinstance(value.get("content"),dict)
            and value["content"].get("@type") == observation.content_type
            and SendObservations._text_matches(value["content"], observation.text_sha256, observation.outgoing_entities)
            and SendObservations._reply_matches(value, observation.reply_anchor, observation.forum_topic_id)
            and (not observation.outgoing_entities or observation.reply_anchor is not None or value.get('reply_to') is None)
            and (not observation.outgoing_entities or observation.forum_topic_id is not None or value.get('topic_id') is None or (
                observation.saved_messages_topic_id is not None and strict_shape(value.get('topic_id'),
                    {'@type': 'messageTopicSavedMessages', 'saved_messages_topic_id': observation.saved_messages_topic_id})))
            and (observation.forum_topic_id is None or (
                strict_shape(value.get('topic_id'), {'@type': 'messageTopicForum',
                    'forum_topic_id': observation.forum_topic_id}) and
                (observation.reply_anchor is not None or value.get('reply_to') is None)))
            and (observation.caption_sha256 is None or _artifact_matches(value['content'],observation))
            and (not final or observation.video is None or _video_matches(value['content']))
            and ((observation.reply_anchor is None and observation.forum_topic_id is None and not observation.outgoing_entities) or observation.caption_sha256 is not None or (
                isinstance(value["content"].get("text"),dict)
                and set(value["content"]["text"])=={"@type","text","entities"}
                and (bool(observation.outgoing_entities) or value["content"]["text"].get("entities")==[])
                and value["content"].get("link_preview") is None
                and (value["content"].get("link_preview_options") is None or
                    strict_shape(value["content"]["link_preview_options"],LINK_OPTIONS))))
            and (not final or value.get("sending_state") is None))

    @staticmethod
    def forward_result_message(event, observation):
        """The forwardMessages result must be exactly one pending message in the recipient chat."""
        if not (observation.forward and isinstance(event, dict) and event.get('@type') == 'messages'
                and type(event.get('total_count')) is int and event['total_count'] == 1
                and type(event.get('messages')) is list and len(event['messages']) == 1):
            return None
        value = event['messages'][0]
        state = value.get('sending_state') if isinstance(value, dict) else None
        if (not SendObservations._message(value, observation) or not isinstance(state, dict)
                or state.get('@type') != 'messageSendingStatePending'
                or type(state.get('sending_id')) is not int or state['sending_id'] != observation.sending_id
                or (observation.temporary_id is not None and observation.temporary_id != value['id'])):
            return None
        return value

    def reduce(self, event):
        if not isinstance(event,dict):
            return
        with self._lock:
            self._prune()
            for key,old in list(self._entries.items()):
                if isinstance(old, AlbumObservation):
                    self._entries[key] = _reduce_album(old, event)
                    continue
                if old.forward and old.status == "sent" and event.get("@extra") == old.extra:
                    # A success that overtook the forwardMessages result stands only
                    # with an exact result; anything else makes the outcome a conflict.
                    if self.forward_result_message(event, old) is None:
                        self._entries[key] = replace(old,status="conflict",message_id=None)
                    continue
                if old.status in {"sent","failed","conflict"}:
                    continue
                updated = old
                kind = event.get("@type")
                if event.get("@extra") == old.extra:
                    if kind == "error" and type(event.get("code")) is int and 0 < event["code"] < 2**31:
                        updated = replace(old,status="failed")
                    elif old.forward:
                        value = self.forward_result_message(event, old)
                        # A malformed or mismatched result never later reconciles to sent.
                        updated = (replace(old,temporary_id=value["id"],status="pending") if value is not None
                                   else replace(old,status="conflict"))
                    elif self._message(event,old):
                        state = event.get("sending_state")
                        if (isinstance(state,dict) and state.get("@type") == "messageSendingStatePending"
                                and type(state.get("sending_id")) is int and state["sending_id"] == old.sending_id):
                            if old.temporary_id is None or old.temporary_id == event["id"]:
                                updated = replace(old,temporary_id=event["id"],status="pending")
                elif kind == "updateNewMessage":
                    value = event.get("message")
                    if self._message(value,old):
                        state = value.get("sending_state")
                        if (isinstance(state,dict) and state.get("@type") == "messageSendingStatePending"
                                and type(state.get("sending_id")) is int and state["sending_id"] == old.sending_id
                                and (old.temporary_id is None or old.temporary_id == value["id"])):
                            updated = replace(old,temporary_id=value["id"],status="pending")
                elif (old.temporary_id is not None and integer(event.get("old_message_id"))
                        and event["old_message_id"] == old.temporary_id):
                    value = event.get("message")
                    if kind == "updateMessageSendSucceeded" and self._message(value,old,final=True):
                        updated = replace(old,status="sent",message_id=value["id"])
                    elif kind == "updateMessageSendFailed" and self._message(value,old):
                        error = event.get("error")
                        state = value.get("sending_state")
                        inner = state.get("error") if isinstance(state,dict) else None
                        if (isinstance(error,dict) and error.get("@type") == "error"
                                and type(error.get("code")) is int and 0 < error["code"] < 2**31
                                and isinstance(state,dict) and state.get("@type") == "messageSendingStateFailed"
                                and isinstance(inner,dict) and inner.get("@type") == "error"
                                and type(inner.get("code")) is int and inner["code"] == error["code"]):
                            updated = replace(old,status="failed")
                self._entries[key] = updated
