"""Out-of-band owner confirmation for a prepared Telegram document send."""

from __future__ import annotations

import json
import subprocess


_SCRIPT = '''on run argv
    set previewText to item 1 of argv
    try
        set choice to display dialog previewText with title "Unofficial Telegram MCP — approve send" buttons {"Cancel", "Send"} default button "Cancel" cancel button "Cancel" giving up after 120
        if (gave up of choice) is false and (button returned of choice) is "Send" then
            return "APPROVED"
        end if
    on error
        return "DENIED"
    end try
    return "DENIED"
end run'''


def _outgoing_style_detail(draft):
    if not draft.outgoing_entities:
        return ''
    from .outgoing_entity_models import caller_outgoing_entities
    from .outgoing_formatting import freeze_outgoing_entities, outgoing_entity_facts
    frozen = freeze_outgoing_entities(draft.text, caller_outgoing_entities(draft.outgoing_entities))
    rows = outgoing_entity_facts(draft.text, frozen)
    return ('Outgoing styles (FINAL normalized text; UTF-16 code units):\n' +
        ''.join(f'{kind}: offset {offset}, length {length}; covered text: {covered!r}\n'
                for offset, length, kind, covered in rows) + '\n')


def _forward_detail(validated) -> str:
    """Untrusted Telegram strings stay marked and JSON-escaped on their own lines."""
    source, recipient = validated.source, validated.recipient
    mode = ('FORWARD — recipients see the original sender' if validated.mode == 'forward' else
            'COPY — sent as your own message, no attribution' + ('; caption removed' if validated.remove_caption else ''))
    label = 'Text' if source.content_kind == 'text' else 'Caption'
    marker = '[untrusted Telegram evidence]'
    if source.text is None:
        text = f'{label}: (none)\n'
    else:
        from .reply_drafts import EVIDENCE_MARKER
        body = source.text[len(EVIDENCE_MARKER):]
        text = f'{label} {marker}: {json.dumps(body, ensure_ascii=False)}\n'
    media = ''
    if source.media is not None:
        from .reply_drafts import EVIDENCE_MARKER
        item = source.media
        def name(value):
            return json.dumps(value[len(EVIDENCE_MARKER):], ensure_ascii=False)
        parts = ([f'file {name(item.file_name)}'] if item.file_name is not None else []) + \
            ([f'MIME {name(item.mime_type)}'] if item.mime_type is not None else []) + \
            ([f'{item.duration_seconds} seconds'] if item.duration_seconds is not None else []) + \
            ([f'{item.width}x{item.height}'] if item.width is not None and item.height is not None else []) + \
            [f'{item.size_bytes} bytes'] + ([f'{item.variants} variants'] if item.variants > 1 else [])
        media = f'Media {marker}: ' + '; '.join(parts) + '\n'
    return (f"Forward this message to {recipient.title} (chat {recipient.chat_id})?\n{mode}\n\n"
            f"Source chat: {source.chat.title} (chat {source.chat.chat_id})\n"
            f"Message ID: {source.message_id}\n"
            f"Sender {marker}: {source.sender.kind} {source.sender.id}\n"
            f"Date: {source.date.isoformat()}\nContent: {source.content_kind}\n"
            f"{text}Text sanitized: {source.text_sanitized}\n{media}"
            f"Source SHA-256: {source.source_sha256}\n"
            f"Recipient: {recipient.title} (chat {recipient.chat_id})\n"
            f"Preview SHA-256: {validated.preview_sha256}\n\n"
            "Unofficial Telegram MCP will revalidate the source, its permissions and the recipient, "
            "then make one forward attempt.")


def confirm_approved_send(
    *, recipient_title: str, recipient: int, display_name: str,
    size_bytes: int, sha256: str, caption: str,
    kind: str = "document", text: str = "",
    duration_seconds: int | None = None, source_sha256: str | None = None,
    source_display_name: str | None = None, reply_preview: dict | None = None,
    reply_artifact_preview: dict | None = None,
    topic_preview: dict | None = None,
    forward_preview: dict | None = None,
) -> bool:
    """Ask the signed-in macOS user; failure or timeout means no send."""
    if sum(value is not None for value in (topic_preview, reply_preview, reply_artifact_preview, forward_preview)) > 1:
        return False
    if (kind == 'forward') != (forward_preview is not None):
        return False
    if forward_preview is not None:
        from .forward_drafts import ForwardDraftPreview
        try:
            validated = ForwardDraftPreview.model_validate(forward_preview)
            if (validated.recipient.chat_id != recipient or validated.recipient.title != recipient_title or
                    validated.source.source_sha256 != sha256 or text not in ('', None) or caption not in ('', None) or
                    duration_seconds is not None or source_sha256 is not None or source_display_name is not None):
                return False
        except (ValueError, TypeError):
            return False
        preview = _forward_detail(validated)
    elif topic_preview is not None:
        from .draft_models import DraftPreview
        try:
            validated = DraftPreview.model_validate(topic_preview)
            if ((validated.topic is None and not validated.outgoing_entities) or kind != 'text' or reply_preview is not None or
                    reply_artifact_preview is not None or validated.recipient != recipient or
                    validated.recipient_title != recipient_title or validated.text != text or
                    validated.sha256 != sha256 or validated.size_bytes != size_bytes):
                return False
        except (ValueError, TypeError):
            return False
        topic = validated.topic
        if validated.outgoing_entities:
            if (display_name not in ('Text message', 'text', '', None) or caption not in ('', None) or
                    duration_seconds is not None or source_sha256 is not None or source_display_name is not None):
                return False
            topic_detail = (f"Topic: {topic.name}\nTopic ID: {topic.forum_topic_id}\n"
                f"Topic type: {topic.chat_type}; peer: {topic.peer_id}\n"
                f"Created: {topic.creation_date}; creator: {topic.creator_kind} {topic.creator_id}\n"
                f"General: {topic.is_general}; outgoing: {topic.is_outgoing}; closed: {topic.is_closed}; hidden: {topic.is_hidden}\n"
                f"Topic SHA-256: {topic.source_sha256}\n" if topic is not None else '')
            preview = (f"Send this text to {validated.recipient_title} (chat {validated.recipient})?\n"
                f"Account: {validated.account_id}\n{topic_detail}Draft: {validated.draft_id}\n"
                f"Expires: {validated.expires_at.isoformat()}\n\n{validated.text}\n\n"
                f"{_outgoing_style_detail(validated)}Bytes: {validated.size_bytes}\n"
                f"Text SHA-256: {validated.sha256}\nPreview SHA-256: {validated.preview_sha256}\n\n"
                "Normal notifications; no reply, link preview, schedule or draft clearing.\n"
                "Unofficial Telegram MCP will revalidate this account and recipient, including any selected topic, then make one send attempt.")
        else:
            preview = (f"Send this plain text to {validated.recipient_title} (chat {validated.recipient})?\n"
                f"Account: {validated.account_id}\nTopic: {topic.name}\nTopic ID: {topic.forum_topic_id}\n"
                f"Topic type: {topic.chat_type}; peer: {topic.peer_id}\n"
                f"Created: {topic.creation_date}; creator: {topic.creator_kind} {topic.creator_id}\n"
                f"General: {topic.is_general}; outgoing: {topic.is_outgoing}; closed: {topic.is_closed}; hidden: {topic.is_hidden}\n"
                f"Draft: {validated.draft_id}\nExpires: {validated.expires_at.isoformat()}\n\n"
                f"{validated.text}\n\nBytes: {validated.size_bytes}\nText SHA-256: {validated.sha256}\n"
                f"Topic SHA-256: {topic.source_sha256}\nPreview SHA-256: {validated.preview_sha256}\n\n"
                "Plain text; normal notifications; no reply, formatting, link preview, schedule or draft clearing.\n"
                "Unofficial Telegram MCP will revalidate this account and topic, then make one send attempt.")
    elif reply_artifact_preview is not None:
        from .reply_artifact_drafts import ReplyArtifactDraftPreview
        try:
            validated=ReplyArtifactDraftPreview.model_validate(reply_artifact_preview)
        except (ValueError,TypeError):return False
        draft=validated.draft;target=validated.reply_target;anchor=target.anchor
        voice=(f"Source: {draft.source_display_name}\nSource SHA-256: {draft.source_sha256}\n"
            f"Converted: {draft.converted}\nDuration: {draft.duration_seconds} seconds\nWaveform (base64): {draft.waveform_base64}\n"
            if draft.kind=='voice_note' else '')
        preview=(f"Send this {draft.kind} reply to {draft.recipient_title} (chat {draft.recipient})?\n"
            f"Account: {draft.account_id}\nReply anchor: chat {anchor.chat_id}, message {anchor.message_id}\n\n"
            f"{target.text}\n\nTarget sanitized: {target.sanitized}; truncated: {target.truncated}\n"
            f"Source SHA-256: {target.source_sha256}\nPreview SHA-256: {validated.preview_sha256}\n\n"
            f"{voice}File: {draft.display_name}\nMIME: {draft.mime_type}\nBytes: {draft.size_bytes}\nSHA-256: {draft.sha256}\n"
            f"Caption:\n{draft.caption}\n\nUnofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.")
    elif reply_preview is not None:
        from .reply_drafts import ReplyDraftPreview
        try:
            validated = ReplyDraftPreview.model_validate(reply_preview)
            draft = validated.draft
            if (kind != 'text' or draft.recipient != recipient or draft.recipient_title != recipient_title or
                    draft.text != text or draft.sha256 != sha256 or draft.size_bytes != size_bytes or
                    display_name not in ('Text message', 'text', '', None) or caption not in ('', None) or duration_seconds is not None or
                    source_sha256 is not None or source_display_name is not None):
                return False
        except (ValueError, TypeError):
            return False
        target = validated.reply_target; anchor = target.anchor
        topic = draft.topic
        topic_detail = (f"Topic: {topic.name}\nTopic ID: {topic.forum_topic_id}\n"
            f"Topic type: {topic.chat_type}; peer: {topic.peer_id}\n"
            f"Created: {topic.creation_date}; creator: {topic.creator_kind} {topic.creator_id}\n"
            f"General: {topic.is_general}; outgoing: {topic.is_outgoing}; closed: {topic.is_closed}; hidden: {topic.is_hidden}\n"
            f"Topic SHA-256: {topic.source_sha256}\n" if topic is not None else '')
        preview=(f"Send this reply to {draft.recipient_title} (chat {draft.recipient})?\n"
            f"Account: {draft.account_id}\n{topic_detail}Reply anchor: chat {anchor.chat_id}, message {anchor.message_id}\n"
            f"Draft: {draft.draft_id}\nExpires: {draft.expires_at.isoformat()}\n\n"
            f"{target.text}\n\nTarget sanitized: {target.sanitized}; truncated: {target.truncated}\n"
            f"Source SHA-256: {target.source_sha256}\nPreview SHA-256: {validated.preview_sha256}\n\n"
            f"Reply text:\n{draft.text}\n\n{_outgoing_style_detail(draft)}Bytes: {draft.size_bytes}\nText SHA-256: {draft.sha256}\n\n"
            "Unofficial Telegram MCP will revalidate cached target evidence and make one reply attempt.")
    elif kind == "text":
        preview = (f"Send this text to {recipient_title} (chat {recipient})?\n\n"
                   f"{text}\n\nSHA-256: {sha256}\n\n"
                   "Unofficial Telegram MCP will make one send attempt.")
    else:
        voice_detail = (f"Source: {source_display_name}\nSource SHA-256: {source_sha256}\n"
                        f"Duration: {duration_seconds} seconds\nConverted to OGG/Opus mono\n"
                        if kind == "voice_note" else "")
        preview = (
            f"Send this {kind} to {recipient_title} (chat {recipient})?\n\n"
            f"{voice_detail}"
            f"File: {display_name}\nBytes: {size_bytes}\nSHA-256: {sha256}\n"
            f"Caption: {caption or '(none)'}\n\n"
            "Unofficial Telegram MCP will make one upload/send attempt."
        )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _SCRIPT, "--", preview],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=125, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b"APPROVED"
