"""Out-of-band owner confirmation for a prepared Telegram send or change.

One renderer turns every bound preview into one document, in a fixed order:
- a plain-language summary ("Send text to «Title» (chat 42)", account, expiry);
- the exact payload: untrusted strings under `forward_drafts.READABLE_RULE`, real line breaks kept, and a
  fixed gutter on every payload line;
- a technical block with the draft id and hashes.

One native dialog shows it: an NSAlert through `osascript -l JavaScript` with a fixed-size scrolling view,
Cancel as the default and a timer that ends it as `timeout`. Only an explicit click on Send/Apply
approves; anything else is `declined`, `timeout` or `dialog_unavailable`, and never a send or change.

Once an alias registry exists (0.55 spec 3C) the broker names its account alias: the heading says which alias
asks, and the summary's account line carries the alias and a masked account (the last two digits of the user
id) instead of the user id, so the user id never reaches the dialog: the owner's own chat (Saved Messages, whose
chat id is the user id) is named "Saved Messages", the owner as a message sender or a topic's creator is "you".
Without one, every argument and byte is 0.54's but for two substitutions that hold in both modes: wherever a chat
is named (recipient, forward source, reply anchor, archived or edited chat) the own chat is "Saved Messages", and
the owner as a forwarded message's sender is "you"; 0.54 printed the user id in both places.

An album (0.56) is one document too: one entry per item (position, kind, delivered name, bytes and facts, then its
caption as an exact payload), the total size and every item's digest. It has no fallback: the scrolling dialog shows
the whole list or nothing, and a dialog that cannot be shown is `dialog_unavailable` with no second attempt.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args

from . import product_identity as identity
from .config import ALIAS_GRAMMAR, DEFAULT_ALIAS


Outcome = Literal['approved', 'declined', 'timeout', 'dialog_unavailable']
OUTCOMES = frozenset(get_args(Outcome))
DIALOG_SECONDS = 120


@dataclass(frozen=True)
class ApprovalDecision:
    """Closed owner decision. Only `approved` is truthy, so `if not decision` stays fail-closed."""
    outcome: Outcome

    def __post_init__(self):
        if type(self.outcome) is not str or self.outcome not in OUTCOMES:
            raise ValueError('unknown approval outcome')

    def __bool__(self) -> bool:
        return self.outcome == 'approved'


APPROVED, DECLINED = ApprovalDecision('approved'), ApprovalDecision('declined')
TIMEOUT, UNAVAILABLE = ApprovalDecision('timeout'), ApprovalDecision('dialog_unavailable')

# Fixed response details; never an input, a dialog text or a provider message.
NOT_GIVEN = 'explicit approval was not given'
DETAILS = MappingProxyType({
    'declined': 'the owner declined in the local dialog; no attempt was made',
    'timeout': 'the local dialog timed out without a choice; no attempt was made',
    'dialog_unavailable': 'the local dialog could not be shown; no attempt was made',
})


def approval_outcome(value: object) -> Outcome:
    """Fail-closed reading of an approval prompt result: only `True` or an approved decision approve.

    A bare `False` (the compatibility path and test fakes) is a decline; any other value, such as a
    string or another truthy object, means no dialog decision exists."""
    if value is True:
        return 'approved'
    if type(value) is ApprovalDecision:
        return value.outcome
    return 'declined' if value is False else 'dialog_unavailable'


# --- native dialog --------------------------------------------------------------------------------------

# JXA; the document and the timer arrive only as argv (never interpolated). Cancel is the default button
# only with an explicit Return key equivalent (AppKit gives a "Cancel" button Escape instead); Send/Apply
# has none. Response codes are AppKit's NSAlertFirstButtonReturn (Cancel), NSAlertSecondButtonReturn and
# NSModalResponseAbort, which the timer's abortModal produces. Any other code or any exception prints no
# approval token. The window joins every Space and full-screen apps (canJoinAllSpaces |
# fullScreenAuxiliary): without it the alert opened off the owner's Space, inactive, occluded and at
# alpha 0 (dialog proof, 21.10.2026). Its appear animation is off (NSWindowAnimationBehaviorNone), so a
# stalled animation cannot hide it either.
_TEMPLATE = '''ObjC.import('AppKit');
function run(argv) {
    try {
        if (argv.length !== 2 || !/^[1-9][0-9]{0,2}$/.test(argv[0]) || Number(argv[0]) > 120) return 'UNAVAILABLE';
        var seconds = Number(argv[0]);
        var app = $.NSApplication.sharedApplication;
        app.setActivationPolicy(1);
        var alert = $.NSAlert.alloc.init;
        alert.messageText = $("@@NAME@@ — approve @@ACTION@@@@ACCOUNT@@");
        alert.informativeText = $('Cancel is the default. Without a choice this dialog closes after ' + seconds +
            ' seconds and nothing is @@EFFECT@@.');
        alert.addButtonWithTitle($('Cancel')).keyEquivalent = $('\\r');
        alert.addButtonWithTitle($('@@VERB@@')).keyEquivalent = $('');
        var scroll = $.NSScrollView.alloc.initWithFrame($.NSMakeRect(0, 0, 740, 400));
        scroll.hasVerticalScroller = true;
        scroll.hasHorizontalScroller = true;
        scroll.borderType = 2;
        var view = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, 740, 400));
        view.editable = false;
        view.selectable = true;
        view.richText = false;
        view.font = $.NSFont.monospacedSystemFontOfSizeWeight(12, 0);
        view.horizontallyResizable = true;
        view.verticallyResizable = true;
        view.maxSize = $.NSMakeSize(1e7, 1e7);
        view.textContainer.widthTracksTextView = false;
        view.textContainer.containerSize = $.NSMakeSize(1e7, 1e7);
        view.string = $(argv[1]);
        scroll.documentView = view;
        alert.accessoryView = scroll;
        alert.layout;
        alert.window.collectionBehavior = 257;
        alert.window.animationBehavior = 2;
        var timer = $.NSTimer.timerWithTimeIntervalTargetSelectorUserInfoRepeats(seconds, app, 'abortModal', $(), false);
        $.NSRunLoop.currentRunLoop.addTimerForMode(timer, $.NSModalPanelRunLoopMode);
        app.activateIgnoringOtherApps(true);
        var code = Number(alert.runModal);
        timer.invalidate;
        return code === 1001 ? 'APPROVED' : code === 1000 ? 'DECLINED' : code === -1001 ? 'TIMEOUT' : 'UNAVAILABLE';
    } catch (error) {
        return 'UNAVAILABLE';
    }
}'''
_SCRIPT = (_TEMPLATE.replace('@@ACTION@@', 'send').replace('@@VERB@@', 'Send').replace('@@EFFECT@@', 'sent'))
# Archive moves and own text edits change Telegram state without sending: same default and timer.
_CHANGE_SCRIPT = (_TEMPLATE.replace('@@ACTION@@', 'change').replace('@@VERB@@', 'Apply')
                  .replace('@@EFFECT@@', 'changed'))
_TOKENS = {b'APPROVED': APPROVED, b'DECLINED': DECLINED, b'TIMEOUT': TIMEOUT}


def _dialog_script(template, alias=None):
    # Resolved per call so the heading follows `product_identity.DISPLAY_NAME`, escaped as a JS string. A named
    # account (0.55) adds the alias to the heading; without one the script is the 0.54 bytes.
    account = '' if alias is None else json.dumps(f' for account «{alias}»', ensure_ascii=True)[1:-1]
    return (template.replace('@@NAME@@', json.dumps(identity.DISPLAY_NAME, ensure_ascii=True)[1:-1])
            .replace('@@ACCOUNT@@', account))


def _dialog(document: str, *, change: bool, seconds: int, alias: str | None = None) -> ApprovalDecision:
    if (type(seconds) is not int or not 1 <= seconds <= DIALOG_SECONDS or type(document) is not str
            or not _valid_alias(alias)):
        return UNAVAILABLE
    try:
        result = subprocess.run(
            ['/usr/bin/osascript', '-l', 'JavaScript', '-e',
             _dialog_script(_CHANGE_SCRIPT if change else _SCRIPT, alias),
             '--', str(seconds), document],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=seconds + 5, check=False,
        )
    except Exception:  # OSError, SubprocessError, ValueError (NUL or unencodable argv) or anything else: fail closed
        return UNAVAILABLE
    if result.returncode != 0 or type(result.stdout) is not bytes:
        return UNAVAILABLE
    return _TOKENS.get(result.stdout.strip(), UNAVAILABLE)


# --- one renderer ---------------------------------------------------------------------------------------

WIDTH = 96  # columns per visual row; the view never wraps, so a row is a line
GUTTER, CONTINUED = '│ ', '┆ '
_UNIT = re.compile(r'⟦U\+[0-9A-F]{4,6}⟧|.', re.S)
_MARK = '[untrusted Telegram evidence]'
_LIST_NAMES = {'main': 'Main', 'archive': 'Archive'}
_EDIT_CHAT_KINDS = {'private': 'private', 'basic_group': 'group', 'supergroup': 'supergroup',
                    'channel': 'channel; visible to all subscribers'}
_KIND_WORDS = {'document': 'document', 'photo': 'photo', 'voice_note': 'voice note'}


class _Refused(Exception):
    """The arguments do not describe one bound preview; no dialog is shown."""


def _require(condition: bool) -> None:
    if not condition:
        raise _Refused


def _width(unit: str) -> int:
    return sum(0 if unicodedata.category(char) in ('Mn', 'Me') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in unit)


def _rows(line: str, prefix: str = '') -> list[str]:
    """One logical line as visual rows of at most WIDTH columns: the first row carries `prefix`, every
    further row starts with CONTINUED. A ⟦U+XXXX⟧ mark is never split. A row breaks before its last
    space when that space lies in the row's latter two thirds, so the space opens the next row and stays
    visible ("┆  word" continues after a space, "┆ word" directly); otherwise it breaks at the width."""
    rows, current, used, space = [], [], 0, 0
    budget = WIDTH - _width(prefix)
    for unit in _UNIT.findall(line):
        size = _width(unit)
        if current and size and used + size > budget:
            cut = space if space and 3 * space >= len(current) else len(current)
            rows.append(''.join(current[:cut]))
            current = current[cut:]
            used = sum(map(_width, current))
            space = max((index for index, item in enumerate(current) if item == ' ' and index), default=0)
            budget = WIDTH - _width(CONTINUED)
        if unit == ' ' and current:
            space = len(current)
        current.append(unit)
        used += size
    rows.append(''.join(current))
    return [prefix + rows[0]] + [CONTINUED + row for row in rows[1:]]


def _span(raw: str) -> str:
    """An untrusted string inline: « » around its readable rendering (no line break can survive)."""
    from .forward_drafts import readable
    return '«' + readable(raw) + '»'


@dataclass(frozen=True)
class _Payload:
    """Exact multi-line payload: every real line on its own gutter row, « » around the whole."""
    raw: str

    def rows(self) -> list[str]:
        from .forward_drafts import readable
        lines = [readable(line) for line in self.raw.split('\n')]
        lines[0] = '«' + lines[0]
        lines[-1] += '»'
        return [row for line in lines for row in _rows(line, GUTTER)]


def _legend() -> str:
    from .forward_drafts import READABLE_RULE
    return (f'Reading rule: text in « » is exact unless its label says rendering; {READABLE_RULE}. A row starting '
            'with │ is one payload line; a row starting with ┆ continues the row above: the marker is "┆ " (┆ and '
            'one space), and any further space after it belongs to the text.')


def _document(summary, body, technical, closing) -> str:
    lines = [*summary, '', *body, '', _legend(), '', 'Technical details:', *technical, '', closing]
    return '\n'.join(row for line in lines
                     for row in (line.rows() if isinstance(line, _Payload) else _rows(line)))


def _valid_alias(alias) -> bool:
    """None (no registry: 0.54) or an account alias: `default` or a named alias's closed token."""
    return alias is None or (type(alias) is str and (alias == DEFAULT_ALIAS or ALIAS_GRAMMAR.fullmatch(alias) is not None))


def _account_line(account_id: int, alias: str | None) -> str:
    """The summary's account. Without a registry (alias None) the 0.54 line with the user id; with one, the alias
    and a masked account, the last two digits of the user id, so the user id itself never reaches the dialog."""
    if alias is None:
        return f'Account: {account_id}'
    _require(type(account_id) is int and account_id > 0)
    return f'Account: {alias} (Telegram account ••{str(account_id)[-2:]})'


def _chat(chat_id: int, account_id: int) -> str:
    """A chat as the dialog names it: the owner's own chat (Saved Messages) has the user id as its chat id, so it is
    named, never numbered; every other chat keeps its id (a private chat's id is the other user's)."""
    return 'Saved Messages' if chat_id == account_id else f'chat {chat_id}'


def _sender(kind: str, sender_id: int, account_id: int) -> str:
    """A message's sender or a topic's creator: the owner is `you`, never the user id."""
    return 'you' if kind == 'user' and sender_id == account_id else f'{kind} {sender_id}'


def _head(action: str, account_id: int, expires_at, alias: str | None) -> list[str]:
    return [action, _account_line(account_id, alias), f'Expires: {expires_at.isoformat()}']


def _evidence(value: str) -> str:
    # Topic names and reply evidence carry the marker prefix; the label states it instead.
    _require(value.startswith(_MARK + ' '))
    return value[len(_MARK) + 1:]


def _styles(draft) -> list[str]:
    if not draft.outgoing_entities:
        return []
    from .outgoing_entity_models import caller_outgoing_entities
    from .outgoing_formatting import freeze_outgoing_entities, outgoing_entity_facts
    frozen = freeze_outgoing_entities(draft.text, caller_outgoing_entities(draft.outgoing_entities))
    return ['Outgoing styles (final normalized text; UTF-16 code units):'] + [
        f'{kind}: offset {offset}, length {length}; covered {_span(covered)}'
        for offset, length, kind, covered in outgoing_entity_facts(draft.text, frozen)]


def _topic_lines(topic, account_id, alias) -> tuple[list[str], list[str]]:
    """(summary line, technical lines) for a bound forum topic. With a registry a topic the owner created names its
    creator `you`; without one the line is 0.54's (its account line prints the user id anyway)."""
    if topic is None:
        return [], []
    creator = (f'{topic.creator_kind} {topic.creator_id}' if alias is None
               else _sender(topic.creator_kind, topic.creator_id, account_id))
    return ([f'Topic {_MARK}: {_span(_evidence(topic.name))} (topic {topic.forum_topic_id})'],
            [f'Topic type: {topic.chat_type}; peer: {topic.peer_id}',
             f'Topic created: {topic.creation_date}; creator: {creator}',
             f'Topic general: {topic.is_general}; outgoing: {topic.is_outgoing}; closed: {topic.is_closed}; '
             f'hidden: {topic.is_hidden}',
             f'Topic SHA-256: {topic.source_sha256}'])


def _entity_rows(heading: str, rows) -> list[str]:
    return ([f'{heading} (raw text UTF-16 code units):'] +
            [f'{kind}: offset {offset}, length {length}; covered {_span(json.loads(covered))}'
             for kind, offset, length, covered in rows]) if rows else []


def _plain_document(draft, alias) -> str:
    """Ordinary text or file send without a topic, styles or reply."""
    where = f'{_span(draft.recipient_title)} ({_chat(draft.recipient, draft.account_id)})'
    if draft.kind == 'text':
        return _document(
            _head(f'Send text to {where}', draft.account_id, draft.expires_at, alias),
            ['Text (exact):', _Payload(draft.text)],
            [f'Draft: {draft.draft_id}', f'Bytes: {draft.size_bytes}', f'Text SHA-256: {draft.sha256}'],
            f'{identity.DISPLAY_NAME} will make one send attempt.')
    voice = ([f'Source file: {_span(draft.source_display_name)}', f'Duration: {draft.duration_seconds} seconds',
              f'Converted to OGG/Opus mono: {draft.converted}'] if draft.kind == 'voice_note' else [])
    voice_technical = ([f'Source SHA-256: {draft.source_sha256}'] if draft.kind == 'voice_note' else [])
    return _document(
        _head(f'Send a {_KIND_WORDS[draft.kind]} to {where}', draft.account_id, draft.expires_at, alias),
        [f'File: {_span(draft.display_name)}', f'MIME: {draft.mime_type}', f'Bytes: {draft.size_bytes}', *voice,
         *(['Caption (exact):', _Payload(draft.caption)] if draft.caption else ['Caption: (none)'])],
        [f'Draft: {draft.draft_id}', f'SHA-256: {draft.sha256}', *voice_technical],
        f'{identity.DISPLAY_NAME} will make one upload/send attempt.')


def _topic_document(draft, alias) -> str:
    """Text into a forum topic, formatted text, or both (one outer preview digest)."""
    where = f'{_span(draft.recipient_title)} ({_chat(draft.recipient, draft.account_id)})'
    topic, topic_technical = _topic_lines(draft.topic, draft.account_id, alias)
    if draft.outgoing_entities:
        action = f'Send formatted text to {where}'
        rule = 'Normal notifications; no reply, link preview, schedule or draft clearing.'
        closing = (f'{identity.DISPLAY_NAME} will revalidate this account and recipient, including any selected '
                   'topic, then make one send attempt.')
    else:
        action = f'Send text to a topic in {where}'
        rule = 'Plain text; normal notifications; no reply, formatting, link preview, schedule or draft clearing.'
        closing = f'{identity.DISPLAY_NAME} will revalidate this account and topic, then make one send attempt.'
    return _document(
        [*_head(action, draft.account_id, draft.expires_at, alias), *topic],
        ['Text (exact):', _Payload(draft.text), *_styles(draft), '', rule],
        [f'Draft: {draft.draft_id}', *topic_technical, f'Bytes: {draft.size_bytes}',
         f'Text SHA-256: {draft.sha256}', f'Preview SHA-256: {draft.preview_sha256}'],
        closing)


def _reply_target(target, account_id) -> tuple[list, list[str]]:
    """(payload lines, technical lines) for the message replied to; its text is untrusted evidence. The
    evidence is always the bound rendering (render_content / sanitize_telegram_text; styled and media
    targets are JSON renderings even when nothing was cleaned), so the block is labelled a rendering, a
    sanitized one also "cleaned, not exact", and its sanitized flag stays directly under it."""
    anchor = target.anchor
    label = ' (rendering, cleaned, not exact)' if target.sanitized else ' (rendering)'
    return ([f'In reply to message {anchor.message_id} in {_chat(anchor.chat_id, account_id)} {_MARK}{label}:',
             _Payload(_evidence(target.text)),
             f'Target sanitized: {target.sanitized}; truncated: {target.truncated}'],
            [f'Target source SHA-256: {target.source_sha256}'])


def _reply_document(validated, alias) -> str:
    draft = validated.draft
    topic, topic_technical = _topic_lines(draft.topic, draft.account_id, alias)
    target, target_technical = _reply_target(validated.reply_target, draft.account_id)
    return _document(
        [*_head(f'Send a reply to {_span(draft.recipient_title)} ({_chat(draft.recipient, draft.account_id)})',
                draft.account_id, draft.expires_at, alias), *topic],
        [*target, '', 'Reply text (exact):', _Payload(draft.text), *_styles(draft)],
        [f'Draft: {draft.draft_id}', *topic_technical, f'Bytes: {draft.size_bytes}', f'Text SHA-256: {draft.sha256}',
         *target_technical, f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate cached target evidence and make one reply attempt.')


def _reply_artifact_document(validated, alias) -> str:
    draft = validated.draft
    target, target_technical = _reply_target(validated.reply_target, draft.account_id)
    voice = ([f'Source file: {_span(draft.source_display_name)}', f'Duration: {draft.duration_seconds} seconds',
              f'Converted to OGG/Opus mono: {draft.converted}'] if draft.kind == 'voice_note' else [])
    voice_technical = ([f'Source SHA-256: {draft.source_sha256}', f'Waveform (base64): {draft.waveform_base64}']
                       if draft.kind == 'voice_note' else [])
    return _document(
        _head(f'Send a {_KIND_WORDS[draft.kind]} reply to {_span(draft.recipient_title)} '
              f'({_chat(draft.recipient, draft.account_id)})', draft.account_id, draft.expires_at, alias),
        [*target, '', f'File: {_span(draft.display_name)}', f'MIME: {draft.mime_type}',
         f'Bytes: {draft.size_bytes}', *voice,
         *(['Caption (exact):', _Payload(draft.caption)] if draft.caption else ['Caption: (none)'])],
        [f'Draft: {draft.draft_id}', f'SHA-256: {draft.sha256}', *voice_technical, *target_technical,
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate cached target evidence and make one reply attempt.')


def _message_lines(source, account_id: int, *, remove_caption: bool = False,
                   forward_note: str = 'Source is itself a forward: the copy carries no original attribution') -> list:
    """One projected message (a forward source; 0.57: a message to pin or unpin): the text or caption is an exact
    gutter payload; entity covered spans, link-preview URLs, file facts and the author signature are « » spans, all
    under the readable rule."""
    from .forward_drafts import unescaped
    label = 'Text' if source.content_kind == 'text' else 'Caption'

    def name(value):  # marked sanitized names, not escapes
        return _span(_evidence(value))
    if remove_caption:
        content = ['Caption: removed — not sent']
    elif source.text is None:
        content = [f'{label}: (none)']
    else:
        content = ([f'{label} {_MARK} (exact):', _Payload(unescaped(source.text))] +
                   _entity_rows(f'{label} entities', source.entity_rows()))
    link = []
    if source.link_preview is not None:
        item = source.link_preview
        url = _span(unescaped(item.url)) if item.url is not None else '(none)'
        link.append(f'Link preview options {_MARK}: disabled {item.is_disabled}; URL {url}; '
                    f'small media {item.force_small_media}; large media {item.force_large_media}; '
                    f'above text {item.show_above_text}')
    if source.link_preview_card_url is not None:
        link.append(f'Link preview card {_MARK}: URL {_span(unescaped(source.link_preview_card_url))}; '
                    'its title, description and media are not shown')
    media = []
    if source.media is not None:
        item = source.media
        parts = ([f'{key} {name(value)}' for key, value in (('file', item.file_name), ('MIME', item.mime_type),
                  ('title', item.title), ('performer', item.performer)) if value is not None] +
                 ([f'{item.duration_seconds} seconds'] if item.duration_seconds is not None else []) +
                 ([f'{item.width}x{item.height}'] if item.width is not None and item.height is not None else []) +
                 ([f'starts at {item.start_timestamp_seconds} seconds'] if item.start_timestamp_seconds else []) +
                 [f'{item.size_bytes} bytes'] + ([f'{item.variants} variants'] if item.variants > 1 else []) +
                 ([f'spoiler {item.has_spoiler}; caption above media {item.caption_above_media}']
                  if item.has_spoiler is not None else []))
        media.append(f'Media {_MARK}: ' + '; '.join(parts))
    attribution = (([f'Author signature {_MARK}: {name(source.author_signature)}']
                    if source.author_signature is not None else []) +
                   ([f'Via bot {_MARK}: user {source.via_bot_user_id}'] if source.via_bot_user_id is not None else []) +
                   ([forward_note] if source.is_forward else []))
    return [f'Message ID: {source.message_id}',
            f'Sender {_MARK}: {_sender(source.sender.kind, source.sender.id, account_id)}', *attribution,
            f'Date: {source.date.isoformat()}', f'Content: {source.content_kind}', *content, *link, *media]


def _forward_document(validated, alias) -> str:
    """The source message under the readable rule (`_message_lines`), its chat, the mode and the recipient."""
    source, recipient = validated.source, validated.recipient
    mode = ('Mode: FORWARD — recipients see the original sender' if validated.mode == 'forward' else
            'Mode: COPY — sent as your own message, no attribution' +
            ('; caption removed' if validated.remove_caption else ''))
    return _document(
        [f'Forward a message to {_span(recipient.title)} ({_chat(recipient.chat_id, validated.account_id)})', mode,
         _account_line(validated.account_id, alias), f'Expires: {validated.expires_at.isoformat()}'],
        [f'Source chat: {_span(source.chat.title)} ({_chat(source.chat.chat_id, validated.account_id)})',
         *_message_lines(source, validated.account_id, remove_caption=validated.remove_caption)],
        [f'Draft: {validated.draft_id}', f'Source SHA-256: {source.source_sha256}',
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the source, its permissions and the recipient, '
        'then make one forward attempt.')


def _video_document(validated, alias) -> str:
    """The probed facts are what the container boxes declare; nothing is decoded."""
    recipient = validated.recipient
    caption = (['Caption [untrusted] (exact):', _Payload(validated.raw_caption)] if validated.caption is not None
               else ['Caption: (none)'])
    return _document(
        _head(f'Send a native video to {_span(recipient.title)} ({_chat(recipient.chat_id, validated.account_id)})',
              validated.account_id, validated.expires_at, alias),
        [f'File name [untrusted] (exact): {_span(validated.raw_display_name)}', f'Bytes: {validated.size_bytes}',
         'Declared by the file (not decoded):', f'Duration: {validated.duration_seconds} seconds',
         f'Dimensions: {validated.width}x{validated.height}', f'Codec: {validated.codec}',
         f"Audio: {'yes' if validated.has_audio else 'no'}",
         f"Streaming: {'yes' if validated.supports_streaming else 'no'}", *caption],
        [f'Draft: {validated.draft_id}', f'SHA-256: {validated.sha256}',
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the account, the recipient and the file bytes, '
        'then make one upload/send attempt as a native video.')


def _archive_document(validated, alias) -> str:
    chat = validated.chat
    current, target = _LIST_NAMES[validated.current_list], _LIST_NAMES[validated.target_list]
    # Telegram behaviour of the move itself: the chat leaves its list (and a pin there); folders
    # that exclude archived chats hide an archived chat.
    notes = [f'Note: the chat leaves {current} and loses any pin there; it is not pinned in {target} '
             '(Telegram behaviour).']
    if validated.target_list == 'archive':
        notes.append('Note: chat folders that exclude archived chats will hide it (Telegram behaviour).')
    if validated.note is not None:
        notes.append(f'Note: {validated.note} (Telegram behaviour).')
    return _document(
        _head(f'Move chat {_span(chat.title)} ({_chat(chat.chat_id, validated.account_id)}, {chat.kind}) '
              f'from {current} to {target}',
              validated.account_id, validated.expires_at, alias),
        [f'Current list: {current}', f'Target list: {target}', *notes],
        [f'Draft: {validated.draft_id}', f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the account, the chat and its current list, then make one '
        'move attempt and report the list it observes right after.')


def _new_styles(heading: str, text: str, styles) -> list[str]:
    """A styled edit's caller styles with their covered text (0.57); nothing for a plain edit."""
    from .outgoing_formatting import outgoing_entity_facts
    return ([heading] + [f'{kind[len("textEntityType"):]}: offset {offset}, length {length}; covered {_span(covered)}'
                         for offset, length, kind, covered in outgoing_entity_facts(text, styles)]) if styles else []


def _text_edit_document(validated, alias) -> str:
    """Only formatting Telegram does not detect again is listed as removed (plain) or replaced (styled, 0.57, whose
    caller styles are listed with their covered text). A plain edit's document is 0.52's."""
    from datetime import datetime, timezone
    from .edit_drafts import AUTO_DETECTED
    chat = validated.chat
    styles = validated.styles()
    rows = list(zip(validated.current_entities, validated.entity_rows()))
    formatting = (_entity_rows('Current formatting, replaced by this edit' if styles else
                               'Current formatting, removed by this plain-text edit',
                               [row for entity, row in rows if entity.type not in AUTO_DETECTED]) +
                  _entity_rows('Current auto-detected entities, auto-detected again by Telegram wherever the new '
                               'text contains them', [row for entity, row in rows if entity.type in AUTO_DETECTED]))
    edited = (datetime.fromtimestamp(validated.current_edit_date, tz=timezone.utc).isoformat()
              if validated.current_edit_date else 'never')
    kind = ('The new text carries only the styles listed here' if styles else
            'The new text is plain text without formatting')
    return _document(
        [*_head(f'Edit your message {validated.message_id} in {_span(chat.title)} '
                f'({_chat(chat.chat_id, validated.account_id)}, {_EDIT_CHAT_KINDS[chat.kind]})', validated.account_id,
                validated.expires_at, alias),
         f'Last edited: {edited}'],
        [f'Current text {_MARK} (exact):', _Payload(validated.raw_current_text), *formatting,
         'New text (exact):', _Payload(validated.raw_new_text),
         *_new_styles('New text styles (final text; UTF-16 code units):', validated.raw_new_text, styles), '',
         f'{kind}; Telegram may detect links in it and show a link preview. '
         'Link preview settings return to Telegram\'s default. Recipients see the message marked "edited".'],
        [f'Draft: {validated.draft_id}', f'Source SHA-256: {validated.source_sha256}',
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the account, the chat, the message and its edit permission, '
        'then make one edit attempt.')


_MEDIA_WORDS = {'photo': 'photo', 'video': 'video', 'animation': 'animation', 'document': 'document', 'audio': 'audio',
                'voice_note': 'voice note'}


def _caption_edit_document(validated, alias) -> str:
    """The old and new caption as exact payloads, or "(none)" and "Remove caption". Only formatting Telegram does not
    detect again is listed as replaced; the caller's styles are listed with their covered text (0.57)."""
    from datetime import datetime, timezone
    from .caption_edit_drafts import ABOVE_MEDIA_KINDS
    from .edit_drafts import AUTO_DETECTED
    from .outgoing_formatting import outgoing_entity_facts
    chat = validated.chat
    rows = list(zip(validated.current_entities, validated.entity_rows()))
    formatting = (_entity_rows('Current formatting, replaced by this edit',
                               [row for entity, row in rows if entity.type not in AUTO_DETECTED]) +
                  _entity_rows('Current auto-detected entities, auto-detected again by Telegram wherever the new '
                               'caption contains them', [row for entity, row in rows if entity.type in AUTO_DETECTED]))
    current = ([f'Current caption {_MARK} (exact):', _Payload(validated.raw_current_caption), *formatting]
               if validated.current_caption is not None else ['Current caption: (none)'])
    if validated.new_caption is None:
        new = ['New caption: Remove caption']
        note = 'Only the caption is removed; the media stays. Recipients see the message marked "edited".'
    else:
        styles = validated.styles()
        new = ['New caption (exact):', _Payload(validated.raw_new_caption)] + (
            ['New caption styles (final caption; UTF-16 code units):'] +
            [f'{kind[len("textEntityType"):]}: offset {offset}, length {length}; covered {_span(covered)}'
             for offset, length, kind, covered in outgoing_entity_facts(validated.raw_new_caption, styles)]
            if styles else [])
        note = ('The new caption carries only the styles listed here; Telegram may detect links in it again. '
                'Recipients see the message marked "edited".')
    position = ([f"Caption position: {'above' if validated.show_caption_above_media else 'below'} the media "
                 '(unchanged)'] if validated.content_kind in ABOVE_MEDIA_KINDS else [])
    edited = (datetime.fromtimestamp(validated.current_edit_date, tz=timezone.utc).isoformat()
              if validated.current_edit_date else 'never')
    return _document(
        [*_head(f'Edit the caption of your {_MEDIA_WORDS[validated.content_kind]} message {validated.message_id} in '
                f'{_span(chat.title)} ({_chat(chat.chat_id, validated.account_id)}, {_EDIT_CHAT_KINDS[chat.kind]})',
                validated.account_id, validated.expires_at, alias),
         f'Last edited: {edited}'],
        [*current, *new, *position, '', note],
        [f'Draft: {validated.draft_id}', f'Source SHA-256: {validated.source_sha256}',
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the account, the chat, the message and its edit permission, '
        'then make one caption edit attempt.')


def _pin_change_document(validated, alias) -> str:
    """The message to pin or unpin (it may be another member's; its content is untrusted evidence) and the side
    effects stated plainly: a pin is silent, for everyone, and posts a visible service message (0.57)."""
    chat, message, account = validated.chat, validated.message, validated.account_id
    if validated.action == 'pin':
        effects = ['Telegram posts a visible service message about the pin that every participant of the chat sees.',
                   'The message is pinned for everyone in the chat, not only for you, and silently: members get no '
                   'notification.',
                   'Messages pinned earlier stay pinned.']
    else:
        effects = ['The message is unpinned for everyone in the chat; no service message is posted.']
    verb = 'Pin' if validated.action == 'pin' else 'Unpin'
    return _document(
        _head(f'{verb} message {message.message_id} in {_span(chat.title)} ({_chat(chat.chat_id, account)}, '
              f'{_EDIT_CHAT_KINDS[chat.kind]})', account, validated.expires_at, alias),
        [*_message_lines(message, account, forward_note='The message is itself a forward; its original sender is '
                                                        'not shown'), '', *effects],
        [f'Draft: {validated.draft_id}', f'Source SHA-256: {message.source_sha256}',
         f'Preview SHA-256: {validated.preview_sha256}'],
        f'{identity.DISPLAY_NAME} will revalidate the account, the chat, the message and its pin permission, then make '
        f'one {validated.action} attempt and report the pin state it observes right after.')


_ALBUM_GROUPS = {'media': 'media (photos and videos)', 'documents': 'documents'}


def _album_entry(item, count: int) -> list:
    """One item: its row (position, kind, delivered name, bytes, facts), then its caption label or exact payload."""
    facts = ([f'{item.photo.width}x{item.photo.height}'] if item.photo is not None else
             [f'{item.video.duration_seconds} seconds', f'{item.video.width}x{item.video.height}', item.video.codec,
              f"audio {'yes' if item.video.has_audio else 'no'}",
              f"streaming {'yes' if item.video.supports_streaming else 'no'}"] if item.video is not None else [])
    row = '; '.join([f'Item {item.position} of {count}: {item.kind} {_span(item.raw_display_name)}',
                     f'{item.size_bytes} bytes', *facts])
    return [row, *(['Caption (exact):', _Payload(item.raw_caption)] if item.caption is not None else
                   ['Caption: (none)'])]


def _album_document(validated, alias) -> str:
    """Every item in send order; names and captions under the readable rule, video facts as the file declares."""
    recipient, count = validated.recipient, len(validated.items)
    entries = [line for item in validated.items for line in ('', *_album_entry(item, count))]
    return _document(
        _head(f'Send an album of {count} items to {_span(recipient.title)} '
              f'({_chat(recipient.chat_id, validated.account_id)})', validated.account_id, validated.expires_at, alias),
        [f'Group: {_ALBUM_GROUPS[validated.group]}', f'Total: {count} items, {validated.total_size_bytes} bytes',
         'Each item is sent once, in this order, as one album. File names and captions are untrusted; captions are '
         'plain text. Video facts are declared by the file, not decoded.', *entries],
        [f'Draft: {validated.draft_id}', f'Album SHA-256: {validated.album_sha256}',
         *(f'Item {item.position} SHA-256: {item.sha256}' for item in validated.items),
         f'Preview SHA-256: {validated.preview_sha256}'],
        f"{identity.DISPLAY_NAME} will revalidate the account, the recipient and every item's bytes, then make one "
        'album send attempt; the album is never sent again.')


def _text_only(display_name, caption, duration_seconds, source_sha256, source_display_name) -> None:
    _require(display_name in ('Text message', 'text', '', None) and caption in ('', None) and
             duration_seconds is None and source_sha256 is None and source_display_name is None)


def _render(*, recipient_title, recipient, display_name, size_bytes, sha256, caption, kind, text,
            duration_seconds, source_sha256, source_display_name, reply_preview, reply_artifact_preview,
            topic_preview, forward_preview, video_preview, archive_preview, text_edit_preview,
            draft_preview, account_alias, album_preview, caption_edit_preview, pin_change_preview) -> tuple[str, bool]:
    """(document, is a change) for exactly one preview bound to the broker's own arguments."""
    _require(_valid_alias(account_alias))
    alias = account_alias
    previews = (topic_preview, reply_preview, reply_artifact_preview, forward_preview, video_preview,
                archive_preview, text_edit_preview, draft_preview, album_preview, caption_edit_preview,
                pin_change_preview)
    _require(sum(value is not None for value in previews) == 1)
    _require((kind == 'forward') == (forward_preview is not None) and (kind == 'video') == (video_preview is not None)
             and (kind == 'archive') == (archive_preview is not None) and
             (kind == 'text_edit') == (text_edit_preview is not None) and (kind == 'album') == (album_preview is not None)
             and (kind == 'caption_edit') == (caption_edit_preview is not None)
             and (kind == 'pin_change') == (pin_change_preview is not None))
    if pin_change_preview is not None:
        # A change bound to the chat, its title and the hashed message projection (0.57).
        from .pin_drafts import PinChangeDraftPreview
        validated = PinChangeDraftPreview.model_validate(pin_change_preview)
        _require(validated.chat.chat_id == recipient and validated.chat.title == recipient_title and
                 sha256 == validated.message.source_sha256 and text in ('', None) and caption in ('', None) and
                 duration_seconds is None and source_sha256 is None and source_display_name is None)
        return _pin_change_document(validated, alias), True
    if caption_edit_preview is not None:
        # A change bound like a text edit: the chat, its title and the hashed source projection (0.57).
        from .caption_edit_drafts import CaptionEditDraftPreview
        validated = CaptionEditDraftPreview.model_validate(caption_edit_preview)
        _require(validated.chat.chat_id == recipient and validated.chat.title == recipient_title and
                 sha256 == validated.source_sha256 and text in ('', None) and caption in ('', None) and
                 duration_seconds is None and source_sha256 is None and source_display_name is None)
        return _caption_edit_document(validated, alias), True
    if album_preview is not None:
        # The broker passes the album digest as sha256, the total as size_bytes and the fixed name "Album".
        from .album_drafts import AlbumDraftPreview
        validated = AlbumDraftPreview.model_validate(album_preview)
        _require(validated.recipient.chat_id == recipient and validated.recipient.title == recipient_title and
                 validated.album_sha256 == sha256 and validated.total_size_bytes == size_bytes and
                 display_name == 'Album' and caption in ('', None) and text in ('', None) and
                 duration_seconds is None and source_sha256 is None and source_display_name is None)
        return _album_document(validated, alias), False
    if archive_preview is not None or text_edit_preview is not None:
        if archive_preview is not None:
            from .archive_drafts import ArchiveDraftPreview, archive_digest
            validated = ArchiveDraftPreview.model_validate(archive_preview)
            chat = validated.chat
            expected_sha256 = archive_digest(chat.chat_id, chat.kind, validated.current_list, validated.target_list)
        else:
            from .edit_drafts import TextEditDraftPreview
            validated = TextEditDraftPreview.model_validate(text_edit_preview)
            chat = validated.chat
            expected_sha256 = validated.source_sha256
        _require(chat.chat_id == recipient and chat.title == recipient_title and sha256 == expected_sha256 and
                 text in ('', None) and caption in ('', None) and duration_seconds is None and
                 source_sha256 is None and source_display_name is None)
        return (_archive_document(validated, alias) if archive_preview is not None
                else _text_edit_document(validated, alias)), True
    if video_preview is not None:
        from .video_drafts import VideoDraftPreview
        validated = VideoDraftPreview.model_validate(video_preview)
        _require(validated.recipient.chat_id == recipient and validated.recipient.title == recipient_title and
                 validated.sha256 == sha256 and validated.size_bytes == size_bytes and
                 validated.raw_display_name == display_name and validated.raw_caption == (caption or '') and
                 text in ('', None) and duration_seconds is None and source_sha256 is None and
                 source_display_name is None)
        return _video_document(validated, alias), False
    if forward_preview is not None:
        from .forward_drafts import ForwardDraftPreview
        validated = ForwardDraftPreview.model_validate(forward_preview)
        _require(validated.recipient.chat_id == recipient and validated.recipient.title == recipient_title and
                 validated.source.source_sha256 == sha256 and text in ('', None) and caption in ('', None) and
                 duration_seconds is None and source_sha256 is None and source_display_name is None)
        return _forward_document(validated, alias), False
    if topic_preview is not None:
        from .draft_models import DraftPreview
        validated = DraftPreview.model_validate(topic_preview)
        _require((validated.topic is not None or validated.outgoing_entities) and kind == 'text' and
                 validated.recipient == recipient and validated.recipient_title == recipient_title and
                 validated.text == text and validated.sha256 == sha256 and validated.size_bytes == size_bytes)
        if validated.outgoing_entities:
            _text_only(display_name, caption, duration_seconds, source_sha256, source_display_name)
        return _topic_document(validated, alias), False
    if reply_artifact_preview is not None:
        from .reply_artifact_drafts import ReplyArtifactDraftPreview
        validated = ReplyArtifactDraftPreview.model_validate(reply_artifact_preview)
        draft = validated.draft
        _require(kind == draft.kind and draft.recipient == recipient and draft.recipient_title == recipient_title and
                 draft.sha256 == sha256 and draft.size_bytes == size_bytes and draft.display_name == display_name and
                 draft.caption == (caption or '') and text in ('', None))
        return _reply_artifact_document(validated, alias), False
    if reply_preview is not None:
        from .reply_drafts import ReplyDraftPreview
        validated = ReplyDraftPreview.model_validate(reply_preview)
        draft = validated.draft
        _require(kind == 'text' and draft.recipient == recipient and draft.recipient_title == recipient_title and
                 draft.text == text and draft.sha256 == sha256 and draft.size_bytes == size_bytes)
        _text_only(display_name, caption, duration_seconds, source_sha256, source_display_name)
        return _reply_document(validated, alias), False
    from .draft_models import DraftPreview
    validated = DraftPreview.model_validate(draft_preview)
    _require(validated.topic is None and not validated.outgoing_entities and validated.kind == kind and
             validated.recipient == recipient and validated.recipient_title == recipient_title and
             validated.sha256 == sha256 and validated.size_bytes == size_bytes)
    if kind == 'text':
        _require(validated.text == text)
        _text_only(display_name, caption, duration_seconds, source_sha256, source_display_name)
    else:
        _require(validated.display_name == display_name and validated.caption == (caption or '') and
                 text in ('', None) and validated.duration_seconds == duration_seconds and
                 validated.source_sha256 == source_sha256 and validated.source_display_name == source_display_name)
    return _plain_document(validated, alias), False


def render_approval(**arguments) -> tuple[str, bool] | None:
    """The exact dialog document and whether it is a change, or None when no dialog may be shown."""
    try:
        return _render(**{**_DEFAULTS, **arguments})
    except (_Refused, ValueError, TypeError, KeyError, AttributeError):
        return None


_DEFAULTS = dict(kind='document', text='', duration_seconds=None, source_sha256=None, source_display_name=None,
                 reply_preview=None, reply_artifact_preview=None, topic_preview=None, forward_preview=None,
                 video_preview=None, archive_preview=None, text_edit_preview=None, draft_preview=None,
                 account_alias=None, album_preview=None, caption_edit_preview=None, pin_change_preview=None)


def decide_approved_send(
    *, recipient_title: str, recipient: int, display_name: str,
    size_bytes: int, sha256: str, caption: str,
    kind: str = "document", text: str = "",
    duration_seconds: int | None = None, source_sha256: str | None = None,
    source_display_name: str | None = None, reply_preview: dict | None = None,
    reply_artifact_preview: dict | None = None,
    topic_preview: dict | None = None,
    forward_preview: dict | None = None,
    video_preview: dict | None = None,
    archive_preview: dict | None = None,
    text_edit_preview: dict | None = None,
    draft_preview: dict | None = None,
    timeout_seconds: int = DIALOG_SECONDS,
    account_alias: str | None = None,
    album_preview: dict | None = None,
    caption_edit_preview: dict | None = None,
    pin_change_preview: dict | None = None,
) -> ApprovalDecision:
    """Ask the signed-in macOS user. Only an explicit Send/Apply click approves; a decline, the
    timer, or any failure to render or show the dialog means no send or change. `account_alias` (only once an
    alias registry exists) names the asking account in the heading and the masked account line."""
    rendered = render_approval(
        recipient_title=recipient_title, recipient=recipient, display_name=display_name, size_bytes=size_bytes,
        sha256=sha256, caption=caption, kind=kind, text=text, duration_seconds=duration_seconds,
        source_sha256=source_sha256, source_display_name=source_display_name, reply_preview=reply_preview,
        reply_artifact_preview=reply_artifact_preview, topic_preview=topic_preview, forward_preview=forward_preview,
        video_preview=video_preview, archive_preview=archive_preview, text_edit_preview=text_edit_preview,
        draft_preview=draft_preview, account_alias=account_alias, album_preview=album_preview,
        caption_edit_preview=caption_edit_preview, pin_change_preview=pin_change_preview)
    if rendered is None:
        return UNAVAILABLE
    document, change = rendered
    return _dialog(document, change=change, seconds=timeout_seconds, alias=account_alias)


def confirm_approved_send(**arguments) -> bool:
    """Compatibility path: True only for an explicit approval in the local dialog."""
    return decide_approved_send(**arguments).outcome == 'approved'
