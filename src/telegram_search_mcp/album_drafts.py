"""Closed contracts for one owner-approved ordered album (0.56, F15b).

An album is 2-10 of the caller's own artifacts sent by one `sendMessageAlbum`: photos and videos mixed
(group `media`) or documents only (group `documents`); audio is deferred. Every item carries its kind,
artifact id, sha256, size, delivered name, plain caption and facts, and one ordered digest
(`album_digest`) binds the preview, the owner dialog and the receipt. Each item ends `sent`, `failed` or
`outcome_unknown` on exact provider evidence only; the album's overall status is a closed reduction of its
slots (`album_overall`) with its own model, separate from the frozen `get_send_status`. Nothing here ever
resends: a repair is a new draft, or a single send for a lone item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .artifact_store import DOCUMENT_LIMIT
from .draft_models import _Closed, AccountId, DraftId, Hash
from .forward_drafts import _Frozen, _utc, escaped, unescaped
from .next_actions import NextAction, NextActionHint, next_action_field, optional_field
from .outgoing_media import PHOTO_FORMATS, PHOTO_LIMIT
from .reply_drafts import EVIDENCE_MARKER, SEND_OPTIONS, MissingCapabilities, digest
from .sanitize import sanitize_telegram_text
from .schemas import ArtifactId, ChatId
from .video_probe import MAX_ASPECT, MAX_FILE_BYTES, MAX_SECONDS, MAX_SIDE, MIN_SIDE, VideoFacts

PREPARED = 'immutable album draft prepared; inspect every item and obtain fresh approval'
REFUSED = 'an item is not eligible for this album; no draft was created'
UNAVAILABLE = 'album draft is unavailable'
DISABLED = 'album needs capabilities the owner has not enabled: missing_capabilities'
# Fixed send details: a provider outcome, a replay, and a lost response. Unconfirmed items point to the album's own
# status tool (get_send_status knows no album) and never to a resend.
SEND_DETAILS = {
    'sent': 'TDLib confirmed every item',
    'failed': 'TDLib reported a failure for every item; no resend is authorized',
    'partial': 'some items were confirmed and the others failed; never resend the album',
    'partial_unknown': ('some items are confirmed and some are unconfirmed: call get_album_send_status, '
                        'never resend the album'),
    'outcome_unknown': ('no item is confirmed yet; a large album can outlast the wait: call get_album_send_status, '
                        'never resend'),
    'replay': 'previous provider attempt receipt; no new send was attempted',
    'replay_unknown': ('previous provider attempt receipt; no new send was attempted; call get_album_send_status for '
                       'the unconfirmed items, never resend'),
    'lost': 'broker response is unavailable; call get_album_send_status, never resend',
    'authority': 'current receipt authority is unavailable; call get_album_send_status, never resend',
}
PREVIEW_VERSION = 1
MIN_ITEMS, MAX_ITEMS = 2, 10
# At most 256 MiB in total: headroom in the 512 MiB outgoing staging pool, quarantine included.
MAX_ALBUM_BYTES = 256 * 1024 * 1024
MAX_ITEM_BYTES = {'photo': PHOTO_LIMIT, 'video': MAX_FILE_BYTES, 'document': DOCUMENT_LIMIT}
MAX_NAME, MAX_CAPTION = 255, 1024
# The provider content type of each item kind, in the order the album is sent.
CONTENT_TYPES = {'photo': 'messagePhoto', 'video': 'messageVideo', 'document': 'messageDocument'}
GROUP_KINDS = {'media': frozenset({'photo', 'video'}), 'documents': frozenset({'document'})}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ARTIFACT = re.compile(r'artifact_[0-9a-f]{32}_([0-9a-f]{64})_([0-9]+)\Z')
_MIME = re.compile(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z')
# The delivered name of an item whose artifact has no recorded file name.
_PHOTO_NAMES = {'JPEG': 'photo.jpg', 'PNG': 'photo.png', 'WEBP': 'photo.webp'}

ItemKind = Literal['photo', 'video', 'document']
Group = Literal['media', 'documents']
REFUSALS = ('unsupported_item', 'item_too_large', 'album_too_large', 'display_name_required')
Refusal = Literal['unsupported_item', 'item_too_large', 'album_too_large', 'display_name_required']
SlotOutcome = Literal['sent', 'failed', 'outcome_unknown']
Overall = Literal['sent', 'failed', 'partial', 'outcome_unknown']
StrictBool = Annotated[bool, Field(strict=True)]
Title = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
Position = Annotated[int, Field(strict=True, ge=1, le=MAX_ITEMS)]
ItemBytes = Annotated[int, Field(strict=True, gt=0, le=max(MAX_ITEM_BYTES.values()))]
TotalBytes = Annotated[int, Field(strict=True, gt=0, le=MAX_ALBUM_BYTES)]
MessageId = Annotated[int, Field(strict=True, gt=0, lt=2**53)]
# prepare_artifact_send's display_name grammar (schemas.PrepareArtifactSendRequest), optional per item.
DisplayName = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
# One reversible JSON layer: an astral scalar costs 12 characters, plus the two quotes.
EscapedName = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 3,
                                   max_length=len(EVIDENCE_MARKER) + 12 * MAX_NAME + 2)]
EscapedCaption = Annotated[str, Field(strict=True, min_length=len(EVIDENCE_MARKER) + 3,
                                      max_length=len(EVIDENCE_MARKER) + 12 * MAX_CAPTION + 2)]


class AlbumRefusal(ValueError):
    """A closed, content-free refusal code for an ineligible album; it never names the item."""

    def __init__(self, code: str):
        if code not in REFUSALS:
            raise ValueError('unknown album refusal')
        self.code = code
        super().__init__(code)


def album_overall(statuses) -> str:
    """The closed reduction of an attempted album's slots: `sent` when every slot is sent, `failed` when every
    slot failed, `partial` when at least one is sent and at least one is not, otherwise `outcome_unknown`."""
    statuses = tuple(statuses)
    if (not MIN_ITEMS <= len(statuses) <= MAX_ITEMS or
            any(type(status) is not str or status not in ('sent', 'failed', 'outcome_unknown') for status in statuses)):
        raise ValueError('invalid album slots')
    if all(status == 'sent' for status in statuses):
        return 'sent'
    if all(status == 'failed' for status in statuses):
        return 'failed'
    return 'partial' if 'sent' in statuses else 'outcome_unknown'


def _valid_video_facts(facts: object) -> bool:
    return (isinstance(facts, VideoFacts) and type(facts.duration_seconds) is int and
            1 <= facts.duration_seconds <= MAX_SECONDS and type(facts.width) is int and type(facts.height) is int and
            MIN_SIDE <= min(facts.width, facts.height) and max(facts.width, facts.height) <= MAX_SIDE and
            max(facts.width, facts.height) <= MAX_ASPECT * min(facts.width, facts.height) and
            facts.codec in ('h264', 'hevc') and type(facts.has_audio) is bool and
            type(facts.supports_streaming) is bool)


def _valid_photo_facts(facts: object) -> bool:
    # validate_photo's own bounds: positive sides, at most 10,000 together, at most 20:1.
    return (type(facts) is tuple and len(facts) == 2 and all(type(side) is int for side in facts) and
            min(facts) >= 1 and sum(facts) <= 10_000 and max(facts) <= 20 * min(facts))


@dataclass(frozen=True)
class AlbumItemSpec:
    """One immutable album item bound to the exact artifact bytes its facts were read from."""
    kind: str
    artifact_id: str
    sha256: str
    size_bytes: int
    display_name: str
    mime_type: str
    caption: str
    facts: object = None  # VideoFacts for a video, (width, height) for a photo, None for a document

    def __post_init__(self):
        from .outgoing_drafts import _safe_caption, _safe_display_name
        match = _ARTIFACT.fullmatch(self.artifact_id) if type(self.artifact_id) is str else None
        if (self.kind not in MAX_ITEM_BYTES or match is None or type(self.sha256) is not str or
                _HASH.match(self.sha256) is None or match.group(1) != self.sha256 or
                type(self.size_bytes) is not int or not 0 < self.size_bytes <= MAX_ITEM_BYTES[self.kind] or
                int(match.group(2)) != self.size_bytes or type(self.display_name) is not str or
                _safe_display_name(self.display_name) != self.display_name or type(self.mime_type) is not str or
                len(self.mime_type) > 127 or _MIME.fullmatch(self.mime_type) is None or
                type(self.caption) is not str or _safe_caption(self.caption) != self.caption or
                not (_valid_video_facts(self.facts) if self.kind == 'video' else
                     _valid_photo_facts(self.facts) if self.kind == 'photo' else self.facts is None)):
            raise ValueError('invalid album item')

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES[self.kind]


@dataclass(frozen=True)
class AlbumSpec:
    """Immutable internal album payload; the registry stores it under kind 'album'."""
    group: str
    items: tuple
    recipient_identity: tuple

    def __post_init__(self):
        if (self.group not in GROUP_KINDS or type(self.items) is not tuple or
                not MIN_ITEMS <= len(self.items) <= MAX_ITEMS or
                any(type(item) is not AlbumItemSpec for item in self.items) or
                not {item.kind for item in self.items} <= GROUP_KINDS[self.group] or
                len({item.artifact_id for item in self.items}) != len(self.items) or
                sum(item.size_bytes for item in self.items) > MAX_ALBUM_BYTES or
                type(self.recipient_identity) is not tuple or not self.recipient_identity):
            raise ValueError('invalid album draft')

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(item.kind for item in self.items)

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        """The ordered item ids: with the owner and the recipient, the supersede key."""
        return tuple(item.artifact_id for item in self.items)

    @property
    def content_types(self) -> tuple[str, ...]:
        return tuple(item.content_type for item in self.items)

    @property
    def sha256(self) -> str:
        return album_digest(self.group, self.items)


def _item_facts(item: AlbumItemSpec) -> dict:
    if item.kind == 'video':
        facts = item.facts
        return {'duration_seconds': facts.duration_seconds, 'width': facts.width, 'height': facts.height,
                'codec': facts.codec, 'has_audio': facts.has_audio, 'supports_streaming': facts.supports_streaming}
    if item.kind == 'photo':
        return {'width': item.facts[0], 'height': item.facts[1]}
    return {}


def album_digest(group: str, items) -> str:
    """The one ordered digest over every item (position, kind, artifact, bytes, delivered name, caption and
    facts); the preview, the dialog and the receipt all carry it."""
    return digest({'domain': 'telegram-search-mcp.album-items', 'version': PREVIEW_VERSION, 'group': group,
                   'items': [{'position': position, 'kind': item.kind, 'artifact_id': item.artifact_id,
                              'sha256': item.sha256, 'size_bytes': item.size_bytes,
                              'display_name': item.display_name, 'caption': item.caption,
                              'facts': _item_facts(item)}
                             for position, item in enumerate(items, start=1)]})


def _photo_format(path: Path) -> str | None:
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as image:
            return image.format
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def _delivered_name(display_name: str | None, recorded: str | None, fixed: str) -> str:
    """The caller's display_name, else the recorded file name, else the fixed name, normalized as for
    prepare_artifact_send; a name over the 240-byte staging bound is `display_name_required`, never another name."""
    from .outgoing_drafts import _safe_display_name
    try:
        return _safe_display_name(display_name if display_name is not None else recorded or fixed)
    except ValueError:
        raise AlbumRefusal('display_name_required') from None


def album_item(*, group: str, artifact, recorded_name: object, recorded_mime: object, caption: str,
               display_name: str | None = None) -> AlbumItemSpec:
    """Classify one owned artifact for an album, locally and before any provider read.

    `media`: a PNG, JPEG or WebP photo that validate_photo accepts, else an MP4/MOV video that probe_video
    reads; anything else is `unsupported_item`. `documents`: any artifact of at most 64 MiB, sent with content
    type detection disabled. A per-kind size limit refuses with `item_too_large`. The delivered name is the
    caller's `display_name`, else the recorded file name (display evidence only: the bytes are bound by sha256),
    else a fixed name; one that cannot be staged is `display_name_required`.
    """
    from .outgoing_drafts import _safe_caption
    from .outgoing_media import validate_photo
    from .video_probe import VideoProbeError, probe_video
    if group not in GROUP_KINDS or type(caption) is not str or (display_name is not None and
                                                                 type(display_name) is not str):
        raise ValueError('invalid album item request')
    size = artifact.size_bytes
    name = recorded_name if type(recorded_name) is str and recorded_name else None
    if type(size) is not int or size <= 0:
        raise AlbumRefusal('unsupported_item')
    if group == 'documents':
        if size > MAX_ITEM_BYTES['document']:
            raise AlbumRefusal('item_too_large')
        mime = (recorded_mime if type(recorded_mime) is str and len(recorded_mime) <= 127 and
                _MIME.fullmatch(recorded_mime) else 'application/octet-stream')
        return AlbumItemSpec(kind='document', artifact_id=artifact.artifact_id, sha256=artifact.sha256,
                             size_bytes=size, display_name=_delivered_name(display_name, name, ''), mime_type=mime,
                             caption=_safe_caption(caption))
    if size > max(MAX_ITEM_BYTES['photo'], MAX_ITEM_BYTES['video']):
        raise AlbumRefusal('item_too_large')
    image_format = _photo_format(artifact.path)
    if image_format in PHOTO_FORMATS:
        if size > MAX_ITEM_BYTES['photo']:
            raise AlbumRefusal('item_too_large')
        delivered = _delivered_name(display_name, name, _PHOTO_NAMES[image_format])
        mime = PHOTO_FORMATS[image_format][0]
        try:
            sides = validate_photo(artifact.path, size_bytes=size, display_name=delivered, mime_type=mime)
        except ValueError:
            raise AlbumRefusal('unsupported_item') from None
        return AlbumItemSpec(kind='photo', artifact_id=artifact.artifact_id, sha256=artifact.sha256,
                             size_bytes=size, display_name=delivered, mime_type=mime,
                             caption=_safe_caption(caption), facts=tuple(sides))
    try:
        facts = probe_video(artifact.path, size_bytes=size)
    except VideoProbeError:
        raise AlbumRefusal('unsupported_item') from None
    delivered = _delivered_name(display_name, name, 'video.mp4')
    mime = 'video/quicktime' if delivered.casefold().endswith('.mov') else 'video/mp4'
    return AlbumItemSpec(kind='video', artifact_id=artifact.artifact_id, sha256=artifact.sha256, size_bytes=size,
                         display_name=delivered, mime_type=mime, caption=_safe_caption(caption), facts=facts)


# --- public contracts --------------------------------------------------------------------------------------

class AlbumRecipient(_Frozen):
    chat_id: ChatId
    title: Title

    @model_validator(mode='after')
    def sanitized(self):
        if sanitize_telegram_text(self.title, max_length=255) != self.title:
            raise ValueError('chat title is not sanitized evidence')
        return self


class AlbumPhotoFacts(_Frozen):
    width: Annotated[int, Field(strict=True, ge=1, le=9999)]
    height: Annotated[int, Field(strict=True, ge=1, le=9999)]

    @model_validator(mode='after')
    def bounded(self):
        if not _valid_photo_facts((self.width, self.height)):
            raise ValueError('photo facts are out of bounds')
        return self


class AlbumVideoFacts(_Frozen):
    """Facts declared by the file (not decoded), as its ISO-BMFF boxes state them."""
    duration_seconds: Annotated[int, Field(strict=True, ge=1, le=MAX_SECONDS)]
    width: Annotated[int, Field(strict=True, ge=MIN_SIDE, le=MAX_SIDE)]
    height: Annotated[int, Field(strict=True, ge=MIN_SIDE, le=MAX_SIDE)]
    codec: Literal['h264', 'hevc']
    has_audio: StrictBool
    supports_streaming: StrictBool

    @model_validator(mode='after')
    def bounded(self):
        if max(self.width, self.height) > MAX_ASPECT * min(self.width, self.height):
            raise ValueError('video facts are out of bounds')
        return self


class AlbumItemPreview(_Frozen):
    """One item: marked, reversibly escaped untrusted delivered name and caption."""
    position: Position
    kind: ItemKind
    artifact_id: ArtifactId
    display_name: EscapedName
    size_bytes: ItemBytes
    sha256: Hash
    caption: EscapedCaption | None = None
    photo: AlbumPhotoFacts | None = None
    video: AlbumVideoFacts | None = None

    @model_validator(mode='after')
    def exact(self):
        from .outgoing_drafts import _safe_caption, _safe_display_name
        name, caption = self.raw_display_name, self.raw_caption
        _, _, artifact_hash, artifact_size = self.artifact_id.split('_', 3)
        if (_safe_display_name(name) != name or
                (self.caption is not None and (not caption or _safe_caption(caption) != caption)) or
                artifact_hash != self.sha256 or int(artifact_size) != self.size_bytes or
                self.size_bytes > MAX_ITEM_BYTES[self.kind] or
                (self.kind == 'photo') != (self.photo is not None) or (self.kind == 'video') != (self.video is not None)):
            raise ValueError('album item facts disagree')
        return self

    @property
    def raw_display_name(self) -> str:
        return unescaped(self.display_name)

    @property
    def raw_caption(self) -> str:
        return unescaped(self.caption) if self.caption is not None else ''

    def spec(self) -> AlbumItemSpec:
        """The internal item these public facts state (the MIME type is not public evidence)."""
        facts = (VideoFacts(**self.video.model_dump()) if self.video is not None else
                 (self.photo.width, self.photo.height) if self.photo is not None else None)
        return AlbumItemSpec(kind=self.kind, artifact_id=self.artifact_id, sha256=self.sha256,
                             size_bytes=self.size_bytes, display_name=self.raw_display_name,
                             mime_type='application/octet-stream', caption=self.raw_caption, facts=facts)

    @classmethod
    def create(cls, position: int, item: AlbumItemSpec) -> 'AlbumItemPreview':
        facts = _item_facts(item)
        return cls(position=position, kind=item.kind, artifact_id=item.artifact_id,
                   display_name=escaped(item.display_name), size_bytes=item.size_bytes, sha256=item.sha256,
                   caption=escaped(item.caption) if item.caption else None,
                   photo=AlbumPhotoFacts(**facts) if item.kind == 'photo' else None,
                   video=AlbumVideoFacts(**facts) if item.kind == 'video' else None)


def _request_content(item: AlbumItemPreview) -> dict:
    """One item of the exact closed sendMessageAlbum request, without its private staged path."""
    caption = {'@type': 'formattedText', 'text': item.raw_caption, 'entities': []}
    if item.kind == 'document':
        return {'@type': 'inputMessageDocument', 'caption': caption,
                'document': {'@type': 'inputDocument', 'thumbnail': None, 'disable_content_type_detection': True}}
    media = {'caption': caption, 'show_caption_above_media': False, 'self_destruct_type': None, 'has_spoiler': False}
    if item.kind == 'photo':
        return {'@type': 'inputMessagePhoto', **media,
                'photo': {'@type': 'inputPhoto', 'thumbnail': None, 'video': None, 'added_sticker_file_ids': [],
                          'width': 0, 'height': 0}}
    video = item.video
    return {'@type': 'inputMessageVideo', **media,
            'video': {'@type': 'inputVideo', 'thumbnail': None, 'cover': None, 'start_timestamp': 0,
                      'added_sticker_file_ids': [], 'duration': video.duration_seconds, 'width': video.width,
                      'height': video.height, 'supports_streaming': video.supports_streaming}}


def preview_facts(*, draft_id: str, account_id: int, expires_at: datetime, recipient: AlbumRecipient, group: str,
                  items, total_size_bytes: int, album_sha256: str) -> dict:
    """Bind the displayed evidence to the exact closed sendMessageAlbum request."""
    return {'domain': 'telegram-search-mcp.album', 'version': PREVIEW_VERSION, 'draft_id': draft_id,
            'account_id': account_id, 'expires_at': _utc(expires_at).isoformat(), 'approval_required': True,
            'recipient': recipient.model_dump(mode='json'), 'group': group,
            'items': [item.model_dump(mode='json') for item in items], 'total_size_bytes': total_size_bytes,
            'album_sha256': album_sha256,
            'request': {'@type': 'sendMessageAlbum', 'chat_id': recipient.chat_id, 'topic_id': None,
                        'reply_to': None, 'options': dict(SEND_OPTIONS),
                        'input_message_contents': [_request_content(item) for item in items]}}


class AlbumDraftPreview(_Frozen):
    """Every item in send order, the recipient, the total size and the ordered album digest."""
    draft_id: DraftId
    account_id: AccountId
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True
    recipient: AlbumRecipient
    group: Group
    items: Annotated[list[AlbumItemPreview], Field(min_length=MIN_ITEMS, max_length=MAX_ITEMS)]
    total_size_bytes: TotalBytes
    album_sha256: Hash
    preview_sha256: Hash

    @field_validator('expires_at', mode='before')
    @classmethod
    def utc_expiry(cls, value):
        return _utc(value)

    @model_validator(mode='after')
    def exact(self):
        facts = preview_facts(**{key: getattr(self, key) for key in (
            'draft_id', 'account_id', 'expires_at', 'recipient', 'group', 'items', 'total_size_bytes',
            'album_sha256')})
        if (self.approval_required is not True or
                [item.position for item in self.items] != list(range(1, len(self.items) + 1)) or
                not {item.kind for item in self.items} <= GROUP_KINDS[self.group] or
                len({item.artifact_id for item in self.items}) != len(self.items) or
                sum(item.size_bytes for item in self.items) != self.total_size_bytes or
                album_digest(self.group, [item.spec() for item in self.items]) != self.album_sha256 or
                digest(facts) != self.preview_sha256):
            raise ValueError('album preview facts disagree')
        return self

    @classmethod
    def create(cls, *, draft_id: str, account_id: int, expires_at: datetime, recipient: AlbumRecipient,
               album: AlbumSpec) -> 'AlbumDraftPreview':
        if type(album) is not AlbumSpec:
            raise ValueError('album facts are unavailable')
        items = [AlbumItemPreview.create(position, item) for position, item in enumerate(album.items, start=1)]
        values = dict(draft_id=draft_id, account_id=account_id, expires_at=_utc(expires_at), recipient=recipient,
                      group=album.group, items=items, total_size_bytes=album.total_bytes, album_sha256=album.sha256)
        return cls(**values, preview_sha256=digest(preview_facts(**values)))


class AlbumItemRequest(_Closed):
    artifact_id: ArtifactId
    caption: Annotated[str, Field(strict=True, max_length=MAX_CAPTION)] = ''
    display_name: DisplayName | None = optional_field(description='File name the recipient sees; omit for the '
                                                                  'recorded file name.')


class PrepareAlbumSendRequest(_Closed):
    recipient: ChatId
    items: Annotated[list[AlbumItemRequest], Field(min_length=MIN_ITEMS, max_length=MAX_ITEMS)]
    group: Group = 'media'

    @model_validator(mode='after')
    def distinct(self):
        if len({item.artifact_id for item in self.items}) != len(self.items):
            raise ValueError('album items must be distinct artifacts')
        return self


class PrepareAlbumSendResponse(NextActionHint, _Closed):
    """`capability_disabled` names only the missing capabilities (video_send for an album with a video item), from
    the closed set and never from input; `unavailable` reveals nothing."""
    status: Literal['prepared', 'refused', 'capability_disabled', 'unavailable']
    album: AlbumDraftPreview | None = None
    refusal: Refusal | None = None
    missing_capabilities: MissingCapabilities | None = optional_field()
    detail: Literal['immutable album draft prepared; inspect every item and obtain fresh approval',
                    'an item is not eligible for this album; no draft was created', 'album draft is unavailable',
                    'album needs capabilities the owner has not enabled: missing_capabilities']
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def shape(self):
        expected = {'prepared': PREPARED, 'refused': REFUSED, 'unavailable': UNAVAILABLE,
                    'capability_disabled': DISABLED}[self.status]
        names = self.missing_capabilities
        if (self.detail != expected or (self.status == 'prepared') != (self.album is not None) or
                (self.status == 'refused') != (self.refusal is not None) or
                (self.status == 'capability_disabled') != (names is not None) or
                (names is not None and len(set(names)) != len(names))):
            raise ValueError('invalid album preparation response')
        return self


class AlbumSlot(_Frozen):
    """One item's outcome, in send order."""
    position: Position
    status: Literal['pending', 'sent', 'failed', 'outcome_unknown']
    message_id: MessageId | None = None

    @model_validator(mode='after')
    def exact(self):
        if (self.status == 'sent') != (self.message_id is not None):
            raise ValueError('only a confirmed item carries a final message ID')
        return self


Slots = Annotated[list[AlbumSlot], Field(max_length=MAX_ITEMS)]


def _ordered(slots) -> bool:
    return [slot.position for slot in slots] == list(range(1, len(slots) + 1))


def slot_list(outcomes) -> list[AlbumSlot]:
    """Public slots from internal (status, message_id) pairs, in send order."""
    return [AlbumSlot(position=position, status=status, message_id=message_id)
            for position, (status, message_id) in enumerate(outcomes, start=1)]


SendStatus = Literal['sent', 'failed', 'partial', 'outcome_unknown', 'local_failed', 'declined', 'timeout',
                     'dialog_unavailable', 'expired', 'recipient_changed', 'error']


class SendPreparedAlbumRequest(_Closed):
    draft_id: DraftId
    approved: StrictBool


class _UnconfirmedSlots(NextActionHint):
    """A `partial` album with an unconfirmed item carries check_send_status like outcome_unknown: its album status can
    still settle that item. A partial album whose items all ended carries no hint."""

    def next_action_condition(self):
        if self.status == 'partial' and any(slot.status == 'outcome_unknown' for slot in self.slots):
            return 'outcome_unknown'
        return super().next_action_condition()


class SendPreparedAlbumResponse(_UnconfirmedSlots, _Closed):
    status: SendStatus
    draft_id: DraftId
    recipient: ChatId | None = None
    album_sha256: Hash | None = None
    slots: Slots = Field(default_factory=list)
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def exact(self):
        if self.slots:
            if (not _ordered(self.slots) or len(self.slots) < MIN_ITEMS or self.album_sha256 is None or
                    any(slot.status == 'pending' for slot in self.slots) or
                    album_overall(slot.status for slot in self.slots) != self.status):
                raise ValueError('album slots disagree with the album status')
        elif self.status in ('sent', 'failed', 'partial') or self.album_sha256 is not None:
            raise ValueError('a provider outcome carries every slot')
        return self


StatusValue = Literal['pending', 'sent', 'failed', 'partial', 'outcome_unknown']
Evidence = Literal['none', 'local_pending', 'local_claimed', 'local_failed', 'provider']
STATUS_DETAILS = {
    'none': 'outcome is unknown; no resend is authorized',
    'local_pending': 'local album draft is pending; no provider acceptance or approval is asserted',
    'local_claimed': 'local album attempt is in flight; no terminal outcome is asserted',
    'local_failed': 'local preparation failed before provider send; no resend is authorized',
    'sent': 'exact provider confirmation of every item; recipient delivery and read status are not asserted',
    'failed': 'exact provider failure of every item; no resend is authorized',
    'partial': 'some items are confirmed and some are not; never resend the album',
    'outcome_unknown': 'no item is confirmed; outcome is unknown; no resend is authorized',
}
StatusDetail = Literal[
    'outcome is unknown; no resend is authorized',
    'local album draft is pending; no provider acceptance or approval is asserted',
    'local album attempt is in flight; no terminal outcome is asserted',
    'local preparation failed before provider send; no resend is authorized',
    'exact provider confirmation of every item; recipient delivery and read status are not asserted',
    'exact provider failure of every item; no resend is authorized',
    'some items are confirmed and some are not; never resend the album',
    'no item is confirmed; outcome is unknown; no resend is authorized',
]


class GetAlbumSendStatusRequest(_Closed):
    draft_id: DraftId


class GetAlbumSendStatusResponse(_UnconfirmedSlots, GetAlbumSendStatusRequest):
    """Closed, content-free album status, separate from the frozen get_send_status."""
    status: StatusValue
    evidence: Evidence
    detail: StatusDetail
    album_sha256: Hash | None = None
    slots: Slots = Field(default_factory=list)
    next_action: NextAction | None = next_action_field()

    @model_validator(mode='after')
    def exact(self):
        fixed = {'none': 'outcome_unknown', 'local_pending': 'pending', 'local_claimed': 'pending',
                 'local_failed': 'failed'}
        if self.evidence in fixed:
            claimed = self.evidence == 'local_claimed'
            if (self.status != fixed[self.evidence] or self.detail != STATUS_DETAILS[self.evidence] or
                    (self.album_sha256 is None) != (self.evidence == 'none') or
                    bool(self.slots) != claimed or (claimed and (not _ordered(self.slots) or
                                                                 len(self.slots) < MIN_ITEMS))):
                raise ValueError('album status evidence disagrees')
            return self
        if (not _ordered(self.slots) or len(self.slots) < MIN_ITEMS or self.album_sha256 is None or
                any(slot.status == 'pending' for slot in self.slots) or
                album_overall(slot.status for slot in self.slots) != self.status or
                self.detail != STATUS_DETAILS[self.status]):
            raise ValueError('album status evidence disagrees')
        return self


def album_status(draft_id: str, evidence: str = 'none', *, album_sha256: str | None = None,
                 outcomes=()) -> GetAlbumSendStatusResponse:
    """The one constructor: `none` is the content-free unknown shape; `provider` reduces the slots."""
    slots = slot_list(outcomes)
    if evidence == 'provider':
        status = album_overall(slot.status for slot in slots)
        detail = STATUS_DETAILS[status]
    else:
        status = {'none': 'outcome_unknown', 'local_pending': 'pending', 'local_claimed': 'pending',
                  'local_failed': 'failed'}[evidence]
        detail = STATUS_DETAILS[evidence]
    return GetAlbumSendStatusResponse(draft_id=draft_id, status=status, evidence=evidence, detail=detail,
                                      album_sha256=album_sha256, slots=slots)
