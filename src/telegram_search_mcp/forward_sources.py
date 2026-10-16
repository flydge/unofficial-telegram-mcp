"""Closed, hash-pinned projection of one exact forward/copy source message.

Only stable, content-identifying facts enter the projection: never file IDs,
local paths, reactions, view counters or other volatile provider state.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

PROJECTION_VERSION = 1
MAX_PROJECTION_BYTES = 65536
MAX_TEXT = 4096
MAX_ENTITIES = 100
CONTENT_TYPES = {'messageText': 'text', 'messagePhoto': 'photo', 'messageDocument': 'document',
                 'messageVoiceNote': 'voice_note', 'messageVideo': 'video', 'messageAudio': 'audio',
                 'messageAnimation': 'animation'}
TDLIB_CONTENT_TYPES = {kind: name for name, kind in CONTENT_TYPES.items()}
REFUSALS = frozenset({'unsupported_source_content', 'unsupported_source_state',
                      'source_not_forwardable', 'protected_content'})
MODES = ('forward', 'copy')
_ENTITY_TYPE = re.compile(r'textEntityType[A-Za-z]{1,48}\Z')
_INT32 = 2**31 - 1
_INT53 = 2**53 - 1


class ForwardRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible source."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown forward refusal')
        self.code = code
        super().__init__(code)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _string(value: object, maximum: int) -> bool:
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def _content_refusal() -> ForwardRefusal:
    return ForwardRefusal('unsupported_source_content')


def _formatted(value: object) -> dict:
    if (type(value) is not dict or value.get('@type') != 'formattedText' or
            not _string(value.get('text'), MAX_TEXT) or type(value.get('entities')) is not list or
            len(value['entities']) > MAX_ENTITIES):
        raise _content_refusal()
    text = value['text']
    units = len(text.encode('utf-16-le')) // 2
    rows = []
    for entity in value['entities']:
        kind = entity.get('type') if type(entity) is dict else None
        if (type(entity) is not dict or entity.get('@type') != 'textEntity' or
                not _int(entity.get('offset'), 0, units) or not _int(entity.get('length'), 1, units) or
                entity['offset'] + entity['length'] > units or type(kind) is not dict or
                type(kind.get('@type')) is not str or _ENTITY_TYPE.fullmatch(kind['@type']) is None):
            raise _content_refusal()
        rows.append({'type': kind['@type'], 'offset': entity['offset'], 'length': entity['length']})
    return {'text': text, 'entities': rows}


def _file(value: object) -> dict:
    remote = value.get('remote') if type(value) is dict else None
    if (type(value) is not dict or value.get('@type') != 'file' or not _int(value.get('size'), 1, _INT53) or
            type(remote) is not dict or remote.get('@type') != 'remoteFile' or
            not _string(remote.get('unique_id'), 4096) or not remote['unique_id']):
        raise _content_refusal()
    return {'size': value['size'],
            'unique_id_sha256': hashlib.sha256(remote['unique_id'].encode('utf-8')).hexdigest()}


def _object(value: object, kind: str) -> dict:
    if type(value) is not dict or value.get('@type') != kind:
        raise _content_refusal()
    return value


def _named(value: dict) -> dict:
    if not _string(value.get('file_name'), 255) or not _string(value.get('mime_type'), 127):
        raise _content_refusal()
    return {'file_name': value['file_name'], 'mime_type': value['mime_type']}


def _measures(value: dict, *names: str) -> dict:
    if any(not _int(value.get(name), 0, _INT32) for name in names):
        raise _content_refusal()
    return {name: value[name] for name in names}


def _content(content: object) -> dict:
    kind = CONTENT_TYPES.get(content.get('@type')) if type(content) is dict else None
    if kind is None:
        raise _content_refusal()
    if kind == 'text':
        return {'kind': 'text', 'text': _formatted(content.get('text'))}
    if kind in ('photo', 'video', 'animation'):
        if content.get('is_secret') is True:
            raise ForwardRefusal('unsupported_source_state')
        if content.get('is_secret') is not False:
            raise _content_refusal()
    result = {'kind': kind, 'caption': _formatted(content.get('caption'))}
    if kind == 'photo':
        photo = _object(content.get('photo'), 'photo')
        sizes = photo.get('sizes')
        if content.get('video') is not None or type(sizes) is not list or not 1 <= len(sizes) <= 20:
            raise _content_refusal()
        variants = []
        for size in sizes:
            size = _object(size, 'photoSize')
            if not _string(size.get('type'), 16) or not _int(size.get('width'), 0, _INT32) or not _int(size.get('height'), 0, _INT32):
                raise _content_refusal()
            variants.append({'type': size['type'], 'width': size['width'], 'height': size['height'],
                             **_file(size.get('photo'))})
        variants.sort(key=lambda row: (row['type'], row['width'], row['height'], row['size'], row['unique_id_sha256']))
        result['variants'] = variants
    elif kind == 'document':
        document = _object(content.get('document'), 'document')
        result.update(_named(document), file=_file(document.get('document')))
    elif kind == 'voice_note':
        voice = _object(content.get('voice_note'), 'voiceNote')
        if not _string(voice.get('mime_type'), 127):
            raise _content_refusal()
        result.update(_measures(voice, 'duration'), mime_type=voice['mime_type'], file=_file(voice.get('voice')))
    elif kind == 'video':
        video = _object(content.get('video'), 'video')
        result.update(_measures(video, 'duration', 'width', 'height'), **_named(video), file=_file(video.get('video')))
    elif kind == 'audio':
        audio = _object(content.get('audio'), 'audio')
        result.update(_measures(audio, 'duration'), **_named(audio), file=_file(audio.get('audio')))
    else:
        animation = _object(content.get('animation'), 'animation')
        result.update(_measures(animation, 'duration', 'width', 'height'), **_named(animation),
                      file=_file(animation.get('animation')))
    return result


def project_message(raw: object, chat_id: int, message_id: int) -> dict:
    """Validate state before content; absence of a safety field is never safe."""
    if not _int(chat_id, -_INT53, _INT53) or chat_id == 0 or not _int(message_id, 1, _INT53):
        raise ValueError('forward source is unavailable')
    if (type(raw) is not dict or raw.get('@type') != 'message' or type(raw.get('chat_id')) is not int or
            raw['chat_id'] != chat_id or type(raw.get('id')) is not int or raw['id'] != message_id):
        raise ValueError('forward source is unavailable')
    if type(raw.get('self_destruct_in')) not in (int, float) or type(raw.get('ephemeral_message_id')) is not int:
        raise ValueError('forward source is unavailable')
    if (raw.get('sending_state') is not None or raw.get('scheduling_state') is not None or
            raw.get('self_destruct_type') is not None or raw['self_destruct_in'] != 0 or
            raw.get('ephemeral_content') is not None or raw['ephemeral_message_id'] != 0 or
            (type(raw.get('date')) is int and raw['date'] == 0)):
        raise ForwardRefusal('unsupported_source_state')
    if not _int(raw.get('date'), 1, _INT32) or not _int(raw.get('edit_date'), 0, _INT32):
        raise ValueError('forward source is unavailable')
    if raw.get('forward_info') is not None and type(raw['forward_info']) is not dict:
        raise ValueError('forward source is unavailable')
    sender = raw.get('sender_id')
    key = {'messageSenderUser': 'user_id', 'messageSenderChat': 'chat_id'}.get(
        sender.get('@type') if type(sender) is dict else None)
    if (key is None or set(sender) != {'@type', key} or
            not _int(sender[key], 1 if key == 'user_id' else -_INT53, _INT53) or sender[key] == 0):
        raise ValueError('forward source is unavailable')
    projection = {'version': PROJECTION_VERSION, 'chat_id': chat_id, 'message_id': message_id,
                  'sender': {'type': 'user' if key == 'user_id' else 'chat', 'id': sender[key]},
                  'date': raw['date'], 'edit_date': raw['edit_date'], 'content': _content(raw.get('content'))}
    if len(canonical(projection).encode('utf-8')) > MAX_PROJECTION_BYTES:
        raise _content_refusal()
    return projection


@dataclass(frozen=True)
class ForwardSource:
    """Canonical serialized projection; no mutable provider state escapes."""
    projection_json: str

    def __post_init__(self):
        if type(self.projection_json) is not str or len(self.projection_json.encode('utf-8')) > MAX_PROJECTION_BYTES:
            raise ValueError('invalid forward projection')
        value = json.loads(self.projection_json)
        if (type(value) is not dict or canonical(value) != self.projection_json or
                set(value) != {'version', 'chat_id', 'message_id', 'sender', 'date', 'edit_date', 'content'} or
                value['version'] != PROJECTION_VERSION or type(value['content']) is not dict or
                value['content'].get('kind') not in TDLIB_CONTENT_TYPES):
            raise ValueError('invalid forward projection')

    @classmethod
    def from_message(cls, raw: object, chat_id: int, message_id: int) -> 'ForwardSource':
        return cls(canonical(project_message(raw, chat_id, message_id)))

    @property
    def value(self) -> dict:
        return json.loads(self.projection_json)

    @property
    def anchor(self) -> tuple[int, int]:
        value = self.value
        return value['chat_id'], value['message_id']

    @property
    def content_type(self) -> str:
        return TDLIB_CONTENT_TYPES[self.value['content']['kind']]

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.projection_json.encode('utf-8')).hexdigest()


def chat_identity(chat: object, chat_id: int) -> tuple[tuple, str]:
    """Reuse the verified-target identity rules; secret chats are refused."""
    from .verified_targets import TargetGuardFailure, _identity
    try:
        identity, metadata = _identity(chat, chat_id)
    except TargetGuardFailure:
        raise ValueError('forward chat is unavailable') from None
    return identity, metadata.title


@dataclass(frozen=True)
class ForwardSnapshot:
    """One read of source chat identity, projection and current properties."""
    source: ForwardSource
    chat_identity: tuple
    chat_title: str
    chat_protected: bool
    can_be_forwarded: bool
    can_be_copied: bool
    protected_by_current_user: bool
    protected_by_other_user: bool

    def __post_init__(self):
        if (not isinstance(self.source, ForwardSource) or type(self.chat_identity) is not tuple or
                type(self.chat_title) is not str or not self.chat_title or
                any(type(getattr(self, name)) is not bool for name in (
                    'chat_protected', 'can_be_forwarded', 'can_be_copied',
                    'protected_by_current_user', 'protected_by_other_user'))):
            raise ValueError('invalid forward snapshot')

    @classmethod
    def create(cls, chat: object, chat_id: int, source: ForwardSource, properties: object) -> 'ForwardSnapshot':
        identity, title = chat_identity(chat, chat_id)
        names = ('can_be_forwarded', 'can_be_copied', 'has_protected_content_by_current_user',
                 'has_protected_content_by_other_user')
        if (not isinstance(source, ForwardSource) or source.anchor[0] != chat_id or
                type(chat.get('has_protected_content')) is not bool or type(properties) is not dict or
                properties.get('@type') != 'messageProperties' or
                any(type(properties.get(name)) is not bool for name in names)):
            raise ValueError('forward source properties are unavailable')
        return cls(source, identity, title, chat['has_protected_content'], *(properties[name] for name in names))

    def refusal(self, mode: str) -> str | None:
        if mode not in MODES:
            raise ValueError('forward mode is invalid')
        if self.chat_protected or self.protected_by_current_user or self.protected_by_other_user:
            return 'protected_content'
        if not (self.can_be_forwarded if mode == 'forward' else self.can_be_copied):
            return 'source_not_forwardable'
        return None
