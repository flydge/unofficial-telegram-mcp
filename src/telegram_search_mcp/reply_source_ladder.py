"""The reply-source family ladder: the one table of provider entity families a reply source may carry.

Each family names its shim keyword, the default-off owner capability that admits it, the TDLib entity types it
covers with their display labels, its rank, and the projection version it selects as the highest present family:
odd for messageText, even for a media caption, so parity alone decides `is_media`. A family without a version for a
source kind is refused there. The two datetime levels refine DateTime by formatting type and share its capability.

Everything else derives from the table: the version of a parsed source, its capability tuple (media, formatted,
the top family's, then every other present family in LISTING_ORDER), the families a stored version admits for
validation and rendering, and the keyword admission of the public shims. No other module lists versions.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

MEDIA_CAPABILITY = 'reply_media_targets'
PLAIN_TEXT_VERSION, PLAIN_MEDIA_VERSION = 1, 2
DATETIME_TYPE = 'textEntityTypeDateTime'


@dataclass(frozen=True, eq=False)
class Family:
    """One closed ladder row; identity equality, so rows are unique members of frozensets."""
    name: str
    kwarg: str
    capability: str
    entities: Mapping[str, str]
    rank: int
    text_version: int | None
    media_version: int | None
    formatting: str | None = None  # a datetime level: present by this formatting type of a DateTime entity
    exclusive: bool = False  # may not overlap another exclusive entity or a block quote
    inert: bool = False  # its presence renders the whole display record inertly escaped

    def version(self, *, media: bool) -> int | None:
        return self.media_version if media else self.text_version

    def marks(self, entity: dict) -> bool:
        """Whether one validated canonical entity belongs to this family."""
        kind = entity['type']['@type']
        if self.formatting is None:
            return kind in self.entities
        formatting = entity['type']['formatting_type'] if kind == DATETIME_TYPE else None
        return formatting is not None and formatting['@type'] == self.formatting


def _map(**labels: str) -> Mapping[str, str]:
    return MappingProxyType({'textEntityType' + kind: label for kind, label in labels.items()})


STYLED = Family('styled', 'styled', 'reply_formatted_targets', _map(
    Bold='bold', Italic='italic', Underline='underline', Strikethrough='strikethrough', Spoiler='spoiler',
    Code='code', Pre='pre', PreCode='pre_code', BlockQuote='block_quote',
    ExpandableBlockQuote='expandable_block_quote'), 1, 3, 4)
LEXICAL = Family('lexical', 'lexical', 'reply_lexical_targets', _map(
    Hashtag='hashtag', Cashtag='cashtag', BotCommand='bot_command'), 2, 5, 6, exclusive=True)
IDENTITY = Family('identity', 'identity', 'reply_identity_targets', _map(
    Mention='mention', MentionName='mention_name'), 3, 7, 8, exclusive=True)
DATETIME = Family('datetime', 'datetime', 'reply_datetime_targets', _map(DateTime='date_time'), 4, 9, 10)
DATETIME_RELATIVE = Family('datetime_relative', 'datetime_relative', 'reply_datetime_targets', _map(), 5, 11, 12,
                           formatting='dateTimeFormattingTypeRelative')
DATETIME_ABSOLUTE = Family('datetime_absolute', 'datetime_absolute', 'reply_datetime_targets', _map(), 6, 13, 14,
                           formatting='dateTimeFormattingTypeAbsolute')
URL = Family('url', 'url', 'reply_url_targets', _map(Url='url'), 7, 15, 16, exclusive=True)
EMAIL = Family('email', 'email', 'reply_email_targets', _map(EmailAddress='email_address'), 8, 17, 18, exclusive=True)
PHONE = Family('phone', 'phone', 'reply_phone_targets', _map(PhoneNumber='phone_number'), 9, 19, 20, exclusive=True)
BANK_CARD = Family('bank_card', 'bank_card', 'reply_bank_card_targets', _map(BankCardNumber='bank_card_number'),
                   10, 21, 22, exclusive=True)
TEXT_URL = Family('text_url', 'text_url', 'reply_text_url_targets', _map(TextUrl='text_url'), 11, 23, None,
                  exclusive=True, inert=True)

LADDER: tuple[Family, ...] = (STYLED, LEXICAL, IDENTITY, DATETIME, DATETIME_RELATIVE, DATETIME_ABSOLUTE, URL, EMAIL,
                              PHONE, BANK_CARD, TEXT_URL)
# After `formatted` and the top family's capability, the other present families are listed in this fixed order
# (not by rank: identity precedes lexical only as a top). A new family extends it at the front.
LISTING_ORDER: tuple[Family, ...] = (TEXT_URL, BANK_CARD, PHONE, EMAIL, URL, DATETIME, LEXICAL, IDENTITY)
TEXT_VERSIONS = frozenset((PLAIN_TEXT_VERSION, *(f.text_version for f in LADDER if f.text_version is not None)))
MEDIA_VERSIONS = frozenset((PLAIN_MEDIA_VERSION, *(f.media_version for f in LADDER if f.media_version is not None)))
EXCLUSIVE_TYPES = frozenset(kind for f in LADDER if f.exclusive for kind in f.entities)
_TOPS: Mapping[int, tuple[Family | None, bool]] = MappingProxyType({
    PLAIN_TEXT_VERSION: (None, False), PLAIN_MEDIA_VERSION: (None, True),
    **{f.text_version: (f, False) for f in LADDER if f.text_version is not None},
    **{f.media_version: (f, True) for f in LADDER if f.media_version is not None}})


def _known(version: object) -> tuple[Family | None, bool]:
    if type(version) is not int or version not in _TOPS:
        raise ValueError('invalid reply projection')
    return _TOPS[version]


def is_media_version(version: object) -> bool:
    return version in MEDIA_VERSIONS


def is_text_version(version: object) -> bool:
    return version in TEXT_VERSIONS


@dataclass(frozen=True)
class Admission:
    """The families a validation accepts; styles are always admitted."""
    families: frozenset[Family]
    entities: Mapping[str, str] = field(init=False, repr=False, compare=False)
    relative: bool = field(init=False, repr=False, compare=False)
    absolute: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.families) is not frozenset or STYLED not in self.families or
                not all(any(family is row for row in LADDER) for family in self.families)):
            raise ValueError('invalid reply family admission')
        object.__setattr__(self, 'entities', MappingProxyType(
            {kind: label for f in LADDER if f in self.families for kind, label in f.entities.items()}))
        object.__setattr__(self, 'relative', DATETIME_RELATIVE in self.families)
        object.__setattr__(self, 'absolute', DATETIME_ABSOLUTE in self.families)

    @classmethod
    def of(cls, **flags: object) -> 'Admission':
        """The public shims' keyword contract: styles plus each truthy family keyword."""
        if set(flags) - {f.kwarg for f in LADDER}:
            raise TypeError('unknown reply family keyword')
        return cls(frozenset((STYLED, *(f for f in LADDER if flags.get(f.kwarg)))))


TEXT_ADMISSION = Admission(frozenset((STYLED, *(f for f in LADDER if f.text_version is not None))))
MEDIA_ADMISSION = Admission(frozenset((STYLED, *(f for f in LADDER if f.media_version is not None))))
_STORED: Mapping[int, Admission] = MappingProxyType({
    version: Admission(frozenset((STYLED, *(f for f in LADDER if f.version(media=media) is not None and
                                            f.rank <= (0 if top is None else top.rank)))))
    for version, (top, media) in _TOPS.items()})


def kind_admission(*, media: bool) -> Admission:
    """Every family that has a version for the source kind: what a fresh provider message may carry."""
    return MEDIA_ADMISSION if media else TEXT_ADMISSION


def admission_for(version: object) -> Admission:
    """What a stored projection version validates and renders with: its top family and every family below it."""
    _known(version)
    return _STORED[version]


def present(entities: Iterable[dict]) -> tuple[Family, ...]:
    """The families present in validated canonical entities, in rank order."""
    rows = tuple(entities)
    return tuple(f for f in LADDER if any(f.marks(entity) for entity in rows))


def _top(families: Iterable[Family]) -> Family | None:
    return max(families, key=lambda f: f.rank, default=None)


def version_for(families: Iterable[Family], *, media: bool) -> int:
    """The projection version selected by the highest present family; refused where it has none for the kind."""
    top = _top(families)
    if top is None:
        return PLAIN_MEDIA_VERSION if media else PLAIN_TEXT_VERSION
    version = top.version(media=media)
    if version is None:
        raise ValueError('reply source family is unavailable for this kind')
    return version


def capabilities_for(families: Iterable[Family], *, media: bool) -> tuple[str, ...]:
    """Media, formatted, the top family's capability, then every other present family in LISTING_ORDER."""
    chosen = tuple(families)
    top = _top(chosen)
    names = [MEDIA_CAPABILITY] if media else []
    if top is not None:
        for name in (STYLED.capability, top.capability, *(f.capability for f in LISTING_ORDER if f in chosen)):
            if name not in names:
                names.append(name)
    return tuple(names)
