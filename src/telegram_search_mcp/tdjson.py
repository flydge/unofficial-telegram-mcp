"""Narrow legacy tdjson ABI wrapper and read-only TDLib client."""

from __future__ import annotations

import ctypes
import base64
import binascii
import hashlib
import itertools
import json
import os
import platform
import secrets
import threading
import time
from contextlib import contextmanager, nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from . import __version__, product_identity
from .config import STAGING_ROOT, TDLIB_SESSION_DIRECTORY, AccountPaths, ensure_private_directory, tdlib_runtime
from .discovery_state import CatalogPosition, ChatListName
from .keychain import ApiCredentials, read_api_credentials
from .reply_drafts import ReplySource, source_from_message, LINK_OPTIONS, SEND_OPTIONS, strict_shape

_ALLOWED_REQUEST_TYPES = frozenset(
    {
        "getAuthorizationState",
        "setTdlibParameters",
        "checkDatabaseEncryptionKey",
        "getMe",
        "createPrivateChat",
        "searchPublicChat",
        "searchChats",
        "searchChatsOnServer",
        "getChats",
        "loadChats",
        "getChat",
        "getChatListsToAddChat",
        "getSupergroup",
        "getForumTopics",
        "getForumTopic",
        "getForumTopicHistory",
        "searchMessages",
        "searchChatMessages",
        "getMessage",
        "getMessageProperties",
        "getChatHistory",
        "getMessageLink",
        "getUser",
        "downloadFile",
        "getFile",
        "cancelDownloadFile",
        "sendMessage",
        "sendMessageAlbum",
        "forwardMessages",
        "addChatToList",
        "editMessageText",
    }
)

# State changes travel only through the dedicated one-attempt transport, never `_call`.
_CHANGE_REQUEST_TYPES = frozenset({"addChatToList", "editMessageText"})
# Only the dedicated forward and album transports may send a forward or an album; `_call` refuses them in any shape.
_CALL_REFUSED_TYPES = _CHANGE_REQUEST_TYPES | {"forwardMessages", "sendMessageAlbum"}

_AUTHORIZATION_CONTROL_TYPES = frozenset(
    {"getAuthorizationState", "setTdlibParameters", "checkDatabaseEncryptionKey"}
)
_ENDED_AUTHORIZATION_STATES = frozenset(
    {"authorizationStateLoggingOut", "authorizationStateClosing", "authorizationStateClosed"}
)
_CHAT_MEDIA_FILTERS = {
    "video": {"@type": "searchMessagesFilterVideo"},
    "video_note": {"@type": "searchMessagesFilterVideoNote"},
}


def _is_strict_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_nonzero_integer(value: object) -> bool:
    return _is_strict_integer(value) and value != 0


def _is_integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    return _is_strict_integer(value) and minimum <= value <= maximum


def _is_forum_chat_id(value: object) -> bool:
    return _is_integer_in_range(value, -(2**53 - 1), 2**53 - 1) and value != 0


def _valid_forum_offsets(date: object, message_id: object, topic_id: object) -> bool:
    return (
        _is_integer_in_range(date, 0, 2**31 - 1)
        and _is_integer_in_range(message_id, 0, 2**53 - 1)
        and _is_integer_in_range(topic_id, 0, 2**31 - 1)
    )


def _valid_voice_waveform(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 84:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 63
    except (binascii.Error, ValueError):
        return False


def _valid_staged_input_file(value: object, staging_root: Path) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"@type", "path"}
        and value.get("@type") == "inputFileLocal" and isinstance(value.get("path"), str)
        and Path(value["path"]).parent.parent == staging_root
        and Path(value["path"]).parent.name.startswith("draft_")
    )


def _parse_wire_int64(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    digits = value[1:] if value.startswith("-") else value
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    if len(digits) > 19 or (len(digits) > 1 and digits.startswith("0")):
        return None
    if value == "-0":
        return None
    parsed = int(value)
    return parsed if -(1 << 63) <= parsed <= (1 << 63) - 1 else None


def _chat_list_payload(chat_list: object) -> dict[str, str]:
    if chat_list == "main":
        return {"@type": "chatListMain"}
    if chat_list == "archive":
        return {"@type": "chatListArchive"}
    raise ValueError("chat_list must be main or archive")


def _is_search_sender(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value.get("@type") == "messageSenderUser":
        return set(value) == {"@type", "user_id"} and _is_integer_in_range(value.get("user_id"), 1, 2**53 - 1)
    if value.get("@type") == "messageSenderChat":
        return set(value) == {"@type", "chat_id"} and _is_forum_chat_id(value.get("chat_id"))
    return False


def _is_search_topic(value: object) -> bool:
    return value is None or (isinstance(value, dict) and set(value) == {"@type", "forum_topic_id"}
        and value.get("@type") == "messageTopicForum"
        and _is_integer_in_range(value.get("forum_topic_id"), 1, 2**31 - 1))


from .outgoing_formatting import FrozenOutgoingEntities, freeze_outgoing_entities, native_outgoing_entities


def _validated_frozen_outgoing_entities(text, entities):
    if (type(entities) is not tuple or len(entities) > 32 or
            any(type(row) is not tuple or len(row) != 3 for row in entities)):
        raise ValueError('invalid frozen outgoing entities')
    from .outgoing_drafts import _safe_message_text
    if _safe_message_text(text) != text:
        raise ValueError('outgoing text is not final normalized text')
    frozen = freeze_outgoing_entities(text, [
        {'offset': offset, 'length': length, 'type': {'@type': kind}}
        for offset, length, kind in entities])
    if frozen != entities:
        raise ValueError('outgoing entities are not canonical')
    return frozen


def _native_outgoing_entity_expectation(text, entities):
    if type(entities) is not list or len(entities) > 32:
        raise ValueError('invalid native outgoing entities')
    caller = []
    for entity in entities:
        if (type(entity) is not dict or set(entity) != {'@type', 'offset', 'length', 'type'}
                or entity.get('@type') != 'textEntity'):
            raise ValueError('invalid native outgoing entity')
        caller.append({key: entity[key] for key in ('offset', 'length', 'type')})
    return freeze_outgoing_entities(text, caller)


def _normalized_caption(value) -> bool:
    """A caption already in the shared normalized form, Telegram's own rewrite included."""
    try:
        from .outgoing_drafts import _safe_caption
        return type(value) is str and _safe_caption(value) == value
    except (ValueError, TypeError, UnicodeError):
        return False


def _bounded_text_entities(formatted):
    entities = formatted.get('entities')
    if entities == []:
        return _normalized_message_text(formatted.get('text'))
    try:
        from .outgoing_drafts import _safe_message_text
        return (_safe_message_text(formatted['text']) == formatted['text'] and
            native_outgoing_entities(_native_outgoing_entity_expectation(
                formatted['text'], entities)) == entities)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return False


def _normalized_message_text(value: str) -> bool:
    try:
        from .outgoing_drafts import _safe_message_text
        return _safe_message_text(value) == value
    except (ValueError, TypeError, UnicodeError):
        return False


def _exact_edit_text(value: str) -> bool:
    """No edge whitespace and no character the pinned TDLib rewrites: stored text equals the reviewed text."""
    from .edit_drafts import edge_whitespace, rewritten_text
    return not edge_whitespace(value) and not rewritten_text(value)


# Closed per-kind media contents, shared by sendMessage and sendMessageAlbum.
def _plain_caption(caption: object) -> bool:
    return (
        isinstance(caption, dict) and set(caption) == {"@type", "text", "entities"}
        and caption.get("@type") == "formattedText"
        and isinstance(caption.get("text"), str) and len(caption["text"]) <= 1024
        and _normalized_caption(caption["text"])
        and caption.get("entities") == []
    )


def _valid_document_content(content: object, staging: Path) -> bool:
    document = content.get("document") if isinstance(content, dict) else None
    return (
        isinstance(content, dict) and set(content) == {"@type", "document", "caption"}
        and content.get("@type") == "inputMessageDocument"
        and isinstance(document, dict)
        and set(document) == {"@type", "document", "thumbnail", "disable_content_type_detection"}
        and document.get("@type") == "inputDocument"
        and document.get("thumbnail") is None
        and document.get("disable_content_type_detection") is True
        and _valid_staged_input_file(document.get("document"), staging)
        and _plain_caption(content.get("caption"))
    )


def _valid_photo_content(content: object, staging: Path) -> bool:
    photo = content.get("photo") if isinstance(content, dict) else None
    return (
        isinstance(content, dict)
        and set(content) == {"@type", "photo", "caption", "show_caption_above_media", "self_destruct_type", "has_spoiler"}
        and content.get("@type") == "inputMessagePhoto"
        and isinstance(photo, dict)
        and set(photo) == {"@type", "photo", "thumbnail", "video", "added_sticker_file_ids", "width", "height"}
        and photo.get("@type") == "inputPhoto"
        and _valid_staged_input_file(photo.get("photo"), staging)
        and photo.get("thumbnail") is None and photo.get("video") is None
        and photo.get("added_sticker_file_ids") == []
        and photo.get("width") == 0 and photo.get("height") == 0
        and content.get("show_caption_above_media") is False
        and content.get("self_destruct_type") is None and content.get("has_spoiler") is False
        and _plain_caption(content.get("caption"))
    )


def _valid_voice_content(content: object, staging: Path) -> bool:
    voice = content.get("voice_note") if isinstance(content, dict) else None
    return (
        isinstance(content, dict)
        and set(content) == {"@type", "voice_note", "caption", "self_destruct_type"}
        and content.get("@type") == "inputMessageVoiceNote"
        and isinstance(voice, dict) and set(voice) == {"@type", "voice_note", "duration", "waveform"}
        and voice.get("@type") == "inputVoiceNote"
        and _valid_staged_input_file(voice.get("voice_note"), staging)
        and type(voice.get("duration")) is int and 1 <= voice["duration"] <= 600
        and _valid_voice_waveform(voice.get("waveform"))
        and content.get("self_destruct_type") is None
        and _plain_caption(content.get("caption"))
    )


def _valid_video_content(content: object, staging: Path) -> bool:
    video = content.get("video") if isinstance(content, dict) else None
    sides = (video.get("width"), video.get("height")) if isinstance(video, dict) else (None, None)
    return (
        isinstance(content, dict)
        and set(content) == {"@type", "video", "caption", "show_caption_above_media", "self_destruct_type", "has_spoiler"}
        and content.get("@type") == "inputMessageVideo"
        and isinstance(video, dict)
        and set(video) == {"@type", "video", "thumbnail", "cover", "start_timestamp", "added_sticker_file_ids",
                           "duration", "width", "height", "supports_streaming"}
        and video.get("@type") == "inputVideo"
        and _valid_staged_input_file(video.get("video"), staging)
        and video.get("thumbnail") is None and video.get("cover") is None
        and type(video.get("start_timestamp")) is int and video["start_timestamp"] == 0
        and type(video.get("added_sticker_file_ids")) is list and video["added_sticker_file_ids"] == []
        and _is_integer_in_range(video.get("duration"), 1, 3 * 3600)
        and all(_is_integer_in_range(side, 16, 8192) for side in sides)
        and max(sides) <= 20 * min(sides)
        and type(video.get("supports_streaming")) is bool
        and content.get("show_caption_above_media") is False
        and content.get("self_destruct_type") is None and content.get("has_spoiler") is False
        and _plain_caption(content.get("caption"))
    )


def _album_item_kind(content: object, staging: Path) -> str | None:
    """The kind of one album item from the shared per-kind shapes; voice notes and text are never album items."""
    if _valid_document_content(content, staging):
        return "document"
    if (_valid_photo_content(content, staging) and type(content["photo"]["width"]) is int
            and type(content["photo"]["height"]) is int):
        return "photo"
    if _valid_video_content(content, staging):
        return "video"
    return None


def _validate_bounded_request(payload: dict[str, Any], staging_root: Path | None = None) -> None:
    """`staging_root` None is the default alias's STAGING_ROOT, read at call time."""
    staging = STAGING_ROOT if staging_root is None else staging_root
    request_type = payload.get("@type")
    if request_type == "getForumTopicHistory" and (
        set(payload) != {"@type", "chat_id", "forum_topic_id", "from_message_id", "offset", "limit"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("forum_topic_id"), 1, 2**31 - 1)
        or not _is_integer_in_range(payload.get("from_message_id"), 0, 2**53 - 1)
        or type(payload.get("offset")) is not int or payload["offset"] != 0
        or not _is_integer_in_range(payload.get("limit"), 1, 20)
    ):
        raise ForbiddenTDLibRequest("TDLib forum history request shape is not allowed")
    if request_type == "getSupergroup" and (
        set(payload) != {"@type", "supergroup_id"}
        or not _is_integer_in_range(payload.get("supergroup_id"), 1, 2**53 - 1)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getForumTopic" and (
        set(payload) != {"@type", "chat_id", "forum_topic_id"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("forum_topic_id"), 1, 2**31 - 1)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getForumTopics" and (
        set(payload) != {
            "@type", "chat_id", "query", "offset_date", "offset_message_id",
            "offset_forum_topic_id", "limit",
        }
        or not _is_forum_chat_id(payload.get("chat_id"))
        or payload.get("query") != ""
        or not _valid_forum_offsets(
            payload.get("offset_date"), payload.get("offset_message_id"),
            payload.get("offset_forum_topic_id"),
        )
        or not _is_integer_in_range(payload.get("limit"), 1, 20)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getMessageProperties" and (
        set(payload)!={"@type","chat_id","message_id"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("message_id"),1,2**53-1)
    ):
        raise ForbiddenTDLibRequest("TDLib message properties request shape is not allowed")
    if request_type == "sendMessage":
        content = payload.get("input_message_content")
        options = payload.get("options")
        sending_id = options.get("sending_id") if isinstance(options, dict) else None
        expected_options = {
            "@type": "messageSendOptions", "suggested_post_info": None,
            "disable_notification": False, "from_background": False,
            "protect_content": False, "allow_paid_broadcast": False,
            "paid_message_star_count": 0,
            "update_order_of_installed_sticker_sets": False,
            "scheduling_state": None, "effect_id": 0,
            "sending_id": sending_id, "only_preview": False,
        }
        valid_text = (
            isinstance(content, dict)
            and set(content) == {"@type", "text", "link_preview_options", "clear_draft"}
            and content.get("@type") == "inputMessageText"
            and content.get("link_preview_options") == {
                "@type": "linkPreviewOptions", "is_disabled": True, "url": "",
                "force_small_media": False, "force_large_media": False,
                "show_above_text": False,
            }
            and content.get("clear_draft") is False
            and isinstance(content.get("text"), dict)
            and set(content["text"]) == {"@type", "text", "entities"}
            and content["text"].get("@type") == "formattedText"
            and isinstance(content["text"].get("text"), str)
            and 0 < len(content["text"]["text"]) <= 4096
            and _bounded_text_entities(content["text"])
        )
        reply_to=payload.get("reply_to")
        valid_reply=(reply_to is None or (isinstance(reply_to,dict)
            and strict_shape(reply_to,{"@type":"inputMessageReplyToMessage",
                "message_id":reply_to.get("message_id"),"quote":None,"checklist_task_id":0,"poll_option_id":""})
            and _is_integer_in_range(reply_to.get("message_id"),1,2**53-1)))
        valid_document = _valid_document_content(content, staging)
        photo = content.get("photo") if isinstance(content, dict) else None
        valid_photo = _valid_photo_content(content, staging)
        valid_voice = _valid_voice_content(content, staging)
        if reply_to is not None and valid_photo and (type(photo.get('width')) is not int or type(photo.get('height')) is not int):
            valid_photo=False
        valid_video = reply_to is None and _valid_video_content(content, staging)
        if (
            set(payload) != {"@type", "chat_id", "topic_id", "reply_to", "options", "reply_markup", "input_message_content"}
            or not _is_strict_nonzero_integer(payload.get("chat_id"))
            or not valid_reply
            or (payload.get('topic_id') is not None and not (
                type(payload['topic_id']) is dict and
                set(payload['topic_id']) == {'@type', 'forum_topic_id'} and
                payload['topic_id'].get('@type') == 'messageTopicForum' and
                _is_integer_in_range(payload['topic_id'].get('forum_topic_id'), 1, 2**31-1) and
                _is_forum_chat_id(payload.get('chat_id')) and valid_text and
                strict_shape(content['link_preview_options'], LINK_OPTIONS)))
            or payload.get("reply_markup") is not None
            or not isinstance(options, dict) or options != expected_options
            or ((reply_to is not None or payload.get('topic_id') is not None) and
                (not strict_shape(options,expected_options) or not _is_forum_chat_id(payload.get("chat_id"))))
            or type(sending_id) is not int or not 0 < sending_id <= (1 << 31) - 1
            or not (valid_text or valid_document or valid_photo or valid_voice or valid_video)
        ):
            raise ForbiddenTDLibRequest("TDLib send request shape is not allowed")
    if request_type == "sendMessageAlbum":
        # 2-10 items from the shared per-kind shapes: photos and videos, or documents only; each item in its own
        # staged directory; no topic, reply or option beyond the fixed ones plus the correlation sending_id.
        options = payload.get("options")
        sending_id = options.get("sending_id") if isinstance(options, dict) else None
        contents = payload.get("input_message_contents")
        kinds = [_album_item_kind(content, staging) for content in contents] if (
            type(contents) is list and 2 <= len(contents) <= 10) else [None]
        directories = {Path(content[kind][kind]["path"]).parent
                       for content, kind in zip(contents, kinds) if kind is not None} if None not in kinds else set()
        if (
            set(payload) != {"@type", "chat_id", "topic_id", "reply_to", "options", "input_message_contents"}
            or not _is_forum_chat_id(payload.get("chat_id"))
            or payload.get("topic_id") is not None or payload.get("reply_to") is not None
            or type(sending_id) is not int or not 0 < sending_id <= (1 << 31) - 1
            or not strict_shape(options, {**SEND_OPTIONS, "sending_id": sending_id})
            or None in kinds
            or not (set(kinds) == {"document"} or set(kinds) <= {"photo", "video"})
            or len(directories) != len(kinds)
        ):
            raise ForbiddenTDLibRequest("TDLib album request shape is not allowed")
    if request_type == "forwardMessages":
        options = payload.get("options")
        sending_id = options.get("sending_id") if isinstance(options, dict) else None
        message_ids = payload.get("message_ids")
        if (
            set(payload) != {"@type", "chat_id", "topic_id", "from_chat_id", "message_ids",
                             "options", "send_copy", "remove_caption"}
            or not _is_forum_chat_id(payload.get("chat_id"))
            or not _is_forum_chat_id(payload.get("from_chat_id"))
            or payload.get("topic_id") is not None
            or type(message_ids) is not list or len(message_ids) != 1
            or not _is_integer_in_range(message_ids[0], 1, 2**53 - 1)
            or type(sending_id) is not int or not 0 < sending_id <= (1 << 31) - 1
            or not strict_shape(options, {**SEND_OPTIONS, "sending_id": sending_id})
            or type(payload.get("send_copy")) is not bool
            or type(payload.get("remove_caption")) is not bool
            or (payload["remove_caption"] and not payload["send_copy"])
        ):
            raise ForbiddenTDLibRequest("TDLib forward request shape is not allowed")
    if request_type == "addChatToList" and (
        set(payload) != {"@type", "chat_id", "chat_list"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or type(payload.get("chat_list")) is not dict
        or payload["chat_list"] not in ({"@type": "chatListMain"}, {"@type": "chatListArchive"})
    ):
        raise ForbiddenTDLibRequest("TDLib chat list request shape is not allowed")
    if request_type == "editMessageText":
        content = payload.get("input_message_content")
        text = content.get("text") if isinstance(content, dict) else None
        if (
            set(payload) != {"@type", "chat_id", "message_id", "reply_markup", "input_message_content"}
            or not _is_forum_chat_id(payload.get("chat_id"))
            or not _is_integer_in_range(payload.get("message_id"), 1, 2**53 - 1)
            or payload.get("reply_markup") is not None
            or type(content) is not dict
            or set(content) != {"@type", "text", "link_preview_options", "clear_draft"}
            or content.get("@type") != "inputMessageText"
            or content.get("link_preview_options") is not None
            or content.get("clear_draft") is not False
            or type(text) is not dict or set(text) != {"@type", "text", "entities"}
            or text.get("@type") != "formattedText"
            or type(text.get("text")) is not str or not 0 < len(text["text"]) <= 4096
            or type(text.get("entities")) is not list or text["entities"] != []
            or not _normalized_message_text(text["text"]) or not _exact_edit_text(text["text"])
        ):
            raise ForbiddenTDLibRequest("TDLib edit request shape is not allowed")
    if request_type in {"downloadFile", "getFile", "cancelDownloadFile"}:
        file_id = payload.get("file_id")
        if not _is_strict_integer(file_id) or not 0 < file_id <= (1 << 31) - 1:
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "downloadFile" and (
            set(payload) != {"@type", "file_id", "priority", "offset", "limit", "synchronous"}
            or payload.get("priority") != 1
            or payload.get("offset") != 0
            or payload.get("limit") not in {64 * 1024 * 1024 + 1, 256 * 1024 * 1024 + 1}
            or payload.get("synchronous") is not False
        ):
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "getFile" and set(payload) != {"@type", "file_id"}:
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "cancelDownloadFile" and (
            set(payload) != {"@type", "file_id", "only_if_pending"}
            or payload.get("only_if_pending") is not False
        ):
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
    if request_type == "loadChats":
        # 50 advances a catalog lane; 100 is the one-shot load before an archive read of an unknown chat.
        if (
            set(payload) != {"@type", "chat_list", "limit"}
            or payload.get("chat_list")
            not in ({"@type": "chatListMain"}, {"@type": "chatListArchive"})
            or not _is_strict_integer(payload.get("limit"))
            or payload["limit"] not in (50, 100)
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")
    if request_type == "getChatListsToAddChat" and (
        set(payload) != {"@type", "chat_id"} or not _is_forum_chat_id(payload.get("chat_id"))
    ):
        raise ForbiddenTDLibRequest("TDLib chat lists request shape is not allowed")
    if request_type == "searchMessages":
        expected_keys = {
            "@type",
            "chat_list",
            "query",
            "offset",
            "limit",
            "filter",
            "chat_type_filter",
            "min_date",
            "max_date",
        }
        if (
            set(payload) != expected_keys
            or payload.get("chat_list")
            not in ({"@type": "chatListMain"}, {"@type": "chatListArchive"})
            or not isinstance(payload.get("query"), str)
            or not isinstance(payload.get("offset"), str)
            or payload.get("limit") != 10
            or payload.get("filter") is not None
            or payload.get("chat_type_filter") is not None
            or not _is_strict_integer(payload.get("min_date"))
            or payload.get("min_date") != 0
            or not _is_strict_integer(payload.get("max_date"))
            or payload.get("max_date") != 0
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")
    if request_type == "searchChatMessages":
        expected_keys = {
            "@type", "chat_id", "topic_id", "query", "sender_id",
            "from_message_id", "offset", "limit", "filter",
        }
        media_filter = payload.get("filter")
        if (
            set(payload) != expected_keys
            or not _is_strict_nonzero_integer(payload.get("chat_id"))
            or not _is_search_topic(payload.get("topic_id"))
            or not isinstance(payload.get("query"), str)
            or not _is_search_sender(payload.get("sender_id"))
            or not _is_strict_integer(payload.get("from_message_id"))
            or payload["from_message_id"] < 0
            or not _is_strict_integer(payload.get("offset"))
            or payload["offset"] != 0
            or not _is_strict_integer(payload.get("limit"))
            or not 1 <= payload["limit"] <= 100
            or (
                media_filter is not None
                and media_filter not in tuple(_CHAT_MEDIA_FILTERS.values())
            )
            or (media_filter is not None and (payload.get("topic_id") is not None or payload.get("sender_id") is not None))
            or ((payload["query"] == "") != (media_filter is not None))
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")


def _formatted_text_is_valid(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("text"), str)


_CAPTION_EVIDENCE_CONTENT_TYPES = frozenset(
    {
        "messageAnimation",
        "messageAudio",
        "messageDocument",
        "messagePaidMedia",
        "messagePhoto",
        "messageVideo",
        "messageVoiceNote",
    }
)
_KNOWN_NON_EVIDENCE_CONTENT_TYPES = frozenset(
    {
        "messageCall",
        "messageChatAddMembers",
        "messageChatBoost",
        "messageChatChangePhoto",
        "messageChatChangeTitle",
        "messageChatDeleteMember",
        "messageChatDeletePhoto",
        "messageChatHasProtectedContentDisableRequested",
        "messageChatHasProtectedContentToggled",
        "messageChatJoinByLink",
        "messageChatJoinByRequest",
        "messageChatOwnerChanged",
        "messageChatOwnerLeft",
        "messageChatSetBackground",
        "messageChatSetMessageAutoDeleteTime",
        "messageChatSetTheme",
        "messageChatShared",
        "messageChatUpgradeFrom",
        "messageChatUpgradeTo",
        "messageChecklist",
        "messageContact",
        "messageDice",
        "messageExpiredPhoto",
        "messageExpiredVideo",
        "messageGame",
        "messageInvoice",
        "messageLocation",
        "messagePoll",
        "messageSticker",
        "messageStory",
        "messageUnsupported",
        "messageVenue",
        "messageVideoNote",
    }
)


def _sender_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("@type") == "messageSenderUser":
        user_id = value.get("user_id")
        return _is_strict_integer(user_id) and user_id > 0
    if value.get("@type") == "messageSenderChat":
        return _is_strict_nonzero_integer(value.get("chat_id"))
    return False


def _validated_global_evidence_message(
    message: object,
) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(message, dict):
        return None, True
    if (
        message.get("@type") != "message"
        or not _is_strict_nonzero_integer(message.get("id"))
        or not _is_strict_nonzero_integer(message.get("chat_id"))
        or not _is_strict_integer(message.get("date"))
        or message["date"] < 0
        or not _sender_is_valid(message.get("sender_id"))
    ):
        return None, True
    content = message.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("@type"), str):
        return None, True
    content_type = content["@type"]
    if content_type == "messageText":
        return (
            (message, False)
            if _formatted_text_is_valid(content.get("text"))
            else (None, True)
        )
    if content_type in _CAPTION_EVIDENCE_CONTENT_TYPES:
        return (
            (message, False)
            if _formatted_text_is_valid(content.get("caption"))
            else (None, True)
        )
    if content_type in _KNOWN_NON_EVIDENCE_CONTENT_TYPES:
        return None, False
    return None, True


@dataclass(frozen=True)
class GlobalMessagePage:
    messages: list[dict[str, Any]]
    next_offset: str
    integrity_partial: bool


def _validate_global_message_page(response: dict[str, Any]) -> GlobalMessagePage:
    total_count = response.get("total_count")
    messages = response.get("messages")
    next_offset = response.get("next_offset")
    if (
        response.get("@type") != "foundMessages"
        or not _is_strict_integer(total_count)
        or total_count < -1
        or not isinstance(messages, list)
        or len(messages) > 10
        or not isinstance(next_offset, str)
    ):
        raise GlobalMessageEnvelopeError()
    valid_messages: list[dict[str, Any]] = []
    integrity_partial = False
    for message in messages:
        valid_message, invalid = _validated_global_evidence_message(message)
        integrity_partial = integrity_partial or invalid
        if valid_message is not None:
            valid_messages.append(valid_message)
    return GlobalMessagePage(
        messages=valid_messages,
        next_offset=next_offset,
        integrity_partial=integrity_partial,
    )


def _catalog_change_from_update(
    update: dict[str, Any],
) -> tuple[bool, int, dict[ChatListName, int], set[ChatListName]] | None:
    update_type = update.get("@type")
    replace_all = update_type in {
        "updateNewChat",
        "updateChatLastMessage",
        "updateChatDraftMessage",
    }
    if update_type == "updateNewChat":
        chat = update.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_type = chat.get("type")
        if isinstance(chat_type, dict) and chat_type.get("@type") == "chatTypeSecret":
            return None
        chat_id = chat.get("id")
        raw_positions = chat.get("positions")
    elif update_type in {"updateChatLastMessage", "updateChatDraftMessage"}:
        chat_id = update.get("chat_id")
        raw_positions = update.get("positions")
    elif update_type == "updateChatPosition":
        chat_id = update.get("chat_id")
        raw_positions = [update.get("position")]
    else:
        return None
    if not _is_strict_nonzero_integer(chat_id) or not isinstance(raw_positions, list):
        return None
    changes: dict[ChatListName, int] = {}
    protected_lists: set[ChatListName] = set()
    for raw_position in raw_positions:
        if not isinstance(raw_position, dict):
            protected_lists.update(("main", "archive"))
            continue
        raw_list = raw_position.get("list")
        if raw_list == {"@type": "chatListMain"}:
            chat_list: ChatListName = "main"
        elif raw_list == {"@type": "chatListArchive"}:
            chat_list = "archive"
        else:
            continue
        order = _parse_wire_int64(raw_position.get("order"))
        if raw_position.get("@type") != "chatPosition" or order is None or order < 0:
            if chat_list not in changes:
                protected_lists.add(chat_list)
            continue
        changes[chat_list] = order
        protected_lists.discard(chat_list)
    return replace_all, chat_id, changes, protected_lists


class TDLibError(RuntimeError):
    """A redacted TDLib failure."""

    # The provider's numeric error code when TDLib answered with an error object; never its text.
    provider_code: int | None = None


class ForumUnsupported(TDLibError):
    """The verified chat does not support forum topics."""


class TDLibDeadlineExceeded(TDLibError):
    """An aggregate request deadline expired before the provider replied."""


class DownloadTooLarge(TDLibError):
    """TDLib reported a file beyond the selected transfer limit."""


class GlobalMessageEnvelopeError(TDLibError):
    """The provider page cannot be continued without trusting malformed state."""

    def __init__(self) -> None:
        super().__init__("TDLib returned an invalid global message envelope")


class ForbiddenTDLibRequest(TDLibError):
    """A request outside the fixed read-only allowlist."""


class AuthorizationBlocked(TDLibError):
    """The dedicated TDLib session is not authorizationStateReady."""


class SecretChatRejected(TDLibError):
    """Secret chats are outside the product boundary."""


class MessageNotFound(TDLibError):
    """A previously discovered message no longer exists."""


class MessageSendFailed(TDLibError):
    """TDLib explicitly rejected or failed the single send attempt."""

    def __init__(self, message: str, *, provider_error: object = None) -> None:
        super().__init__(message)
        error = provider_error if isinstance(provider_error, dict) else {}
        code = error.get("code")
        self.provider_code = code if type(code) is int and 0 < code < 10000 else None
        # Provider text can contain private request data; never copy it.

    def public_detail(self) -> str:
        if self.provider_code is None:
            return "TDLib rejected the send"
        return f"TDLib rejected the send (code {self.provider_code})"


class MessageSendNotAttempted(TDLibError):
    """Local preparation refused before the raw provider send was invoked."""


class MessageSendOutcomeUnknown(TDLibError):
    """The provider attempt may have happened but delivery is unconfirmed."""


class RawTDJson(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class LegacyTDJson:
    """Own one client created through the installed legacy tdjson C ABI."""

    def __init__(self) -> None:
        library_path, expected = tdlib_runtime()
        try:
            library = ctypes.CDLL(str(library_path))
        except OSError as error:
            raise TDLibError("unable to load the pinned TDLib runtime") from error
        if expected is not None:
            # The Homebrew path's keg name is not proof: the loaded library must answer the pin
            # synchronously before any client exists, as the bundled path already requires.
            from .native_runtime import NativeRuntimeError, probe_identity
            try:
                probe_identity(library, *expected)
            except NativeRuntimeError:
                raise TDLibError("the loaded TDLib runtime does not answer the approved version and commit") from None

        library.td_json_client_create.argtypes = []
        library.td_json_client_create.restype = ctypes.c_void_p
        library.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.td_json_client_send.restype = None
        library.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        library.td_json_client_receive.restype = ctypes.c_char_p
        library.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.td_json_client_execute.restype = ctypes.c_char_p
        library.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        library.td_json_client_destroy.restype = None

        quiet_request = b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}'
        quiet_response = library.td_json_client_execute(None, quiet_request)
        if quiet_response is None:
            raise TDLibError("TDLib rejected its initialization log setting")
        client = library.td_json_client_create()
        if not client:
            raise TDLibError("TDLib did not create a client")
        self._library = library
        self._client: int | None = client

    def send(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise TDLibError("TDLib client is closed")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._library.td_json_client_send(self._client, encoded)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self._client is None:
            raise TDLibError("TDLib client is closed")
        encoded = self._library.td_json_client_receive(self._client, timeout)
        if encoded is None:
            return None
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TDLibError("TDLib returned an invalid JSON response") from error
        if not isinstance(value, dict):
            raise TDLibError("TDLib returned an invalid response object")
        return value

    def close(self) -> None:
        if self._client is not None:
            self._library.td_json_client_destroy(self._client)
            self._client = None

    def __enter__(self) -> "LegacyTDJson":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


from .send_observations import SendObservations, ObservationUnavailable, album_outcomes


class TDLibClient:
    """Typed read-only operations over a strictly allowlisted tdjson transport."""

    def __init__(
        self,
        *,
        raw: RawTDJson,
        session_directory: Path | None = None,
        credential_loader: Callable[[], ApiCredentials] = read_api_credentials,
        database_key_loader: Callable[[], bytes] | None = None,
        request_timeout: float = 30.0,
        staging_root: Path | None = None,
    ) -> None:
        self._raw = raw
        self._session_directory = TDLIB_SESSION_DIRECTORY if session_directory is None else session_directory
        self._staging_root = staging_root  # None: the default alias's STAGING_ROOT, read at each validation.
        self._credential_loader = credential_loader
        self._database_key_loader = database_key_loader
        self._request_timeout = request_timeout
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._serialization_wait_count = 0
        self._request_context = threading.local()
        self._ready = False
        # LoggingOut/Closing/Closed end this instance for good (a remote revoke goes all
        # three ways, then closed TDLib aborts every request): authorization is blocked.
        self._authorization_ended = False
        self._send_observations = SendObservations()
        self._send_account_id: int | None = None
        self._send_observation_epoch = object()
        self._catalog_positions: dict[ChatListName, dict[int, int]] = {
            "main": {},
            "archive": {},
        }

    @classmethod
    def from_defaults(cls, account: AccountPaths | None = None) -> "TDLibClient":
        os.umask(0o077)
        from .credential_identity import session_credential_loader
        # The broker already holds the exclusive session lease at this boundary.
        if account is None:
            key, credentials = session_credential_loader()
            return cls(raw=LegacyTDJson(), credential_loader=credentials, database_key_loader=lambda: key)
        from .enrollment import SetupPaths
        key, credentials = session_credential_loader(SetupPaths.from_account(account))
        return cls(raw=LegacyTDJson(), session_directory=account.session, staging_root=account.staging,
                   credential_loader=credentials, database_key_loader=lambda: key)

    def _apply_catalog_update(self, update: dict[str, Any]) -> None:
        change = _catalog_change_from_update(update)
        if change is None:
            return
        replace_all, chat_id, positions, protected_lists = change
        if replace_all:
            for chat_list, catalog in self._catalog_positions.items():
                if chat_list not in protected_lists:
                    catalog.pop(chat_id, None)
        for chat_list, order in positions.items():
            if order == 0:
                self._catalog_positions[chat_list].pop(chat_id, None)
            else:
                self._catalog_positions[chat_list][chat_id] = order

    @property
    def send_observation_epoch(self) -> object:
        return self._send_observation_epoch

    def _invalidate_send_observations(self) -> None:
        self._send_observations.invalidate()
        self._send_observation_epoch = object()
        self._send_account_id = None

    def _reduce_receive_event(self, response: dict[str, Any]) -> None:
        self._apply_catalog_update(response)
        if response.get("@type") == "updateAuthorizationState":
            state = response.get("authorization_state")
            self._ready = isinstance(state, dict) and state.get("@type") == "authorizationStateReady"
            if not self._ready:
                self._invalidate_send_observations()
            if isinstance(state, dict) and state.get("@type") in _ENDED_AUTHORIZATION_STATES:
                self._authorization_ended = True
        self._send_observations.reduce(response)

    def get_send_observation(self, attempt_id: str):
        """Drain at most 64 immediately available events; never wait for the lock.

        Authentication/account checks belong to the owning broker's bounded call.
        The fixed initial retention deadline is never renewed by reading.
        """
        if self._lock.acquire(blocking=False):
            try:
                deadline = min(time.monotonic() + 0.05,
                    getattr(self._request_context, "deadline", float("inf")))
                for _ in range(64):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        response = self._raw.receive(0.0)
                    except Exception:
                        break
                    if response is None:
                        break
                    self._reduce_receive_event(response)
            finally:
                self._lock.release()
        return self._send_observations.snapshot(attempt_id) if self._ready else None

    def _catalog_snapshot(self, chat_list: ChatListName) -> list[CatalogPosition]:
        with self._lock:
            return [
                CatalogPosition(chat_id=chat_id, order=order)
                for chat_id, order in sorted(
                    self._catalog_positions[chat_list].items(),
                    key=lambda item: (item[1], item[0]),
                    reverse=True,
                )
            ]

    @property
    def serialization_wait_count(self) -> int:
        with self._metrics_lock:
            return self._serialization_wait_count

    @contextmanager
    def request_budget(self, deadline: float):
        previous = getattr(self._request_context, "deadline", None)
        self._request_context.deadline = deadline
        try:
            yield
        finally:
            if previous is None:
                del self._request_context.deadline
            else:
                self._request_context.deadline = previous

    @contextmanager
    def _serialized_provider_call(self, deadline: float | None = None):
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            with self._metrics_lock:
                self._serialization_wait_count += 1
            if deadline is None:
                self._lock.acquire()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                acquired = self._lock.acquire(timeout=remaining)
                if not acquired:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
        try:
            yield
        finally:
            self._lock.release()

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_type = payload.get("@type")
        if (not isinstance(request_type, str) or request_type not in _ALLOWED_REQUEST_TYPES or
                request_type in _CALL_REFUSED_TYPES):
            raise ForbiddenTDLibRequest("TDLib request is not allowed")
        _validate_bounded_request(payload, self._staging_root)
        aggregate_deadline = getattr(self._request_context, "deadline", None)
        with self._serialized_provider_call(aggregate_deadline):
            if aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
            if request_type not in _AUTHORIZATION_CONTROL_TYPES and not self._ready:
                raise AuthorizationBlocked(
                    "the dedicated TDLib session is not authorizationStateReady"
                )
            per_call_deadline = time.monotonic() + self._request_timeout
            deadline = (
                min(per_call_deadline, aggregate_deadline)
                if aggregate_deadline is not None
                else per_call_deadline
            )
            if time.monotonic() >= deadline:
                if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                raise TDLibError("TDLib request timed out")
            extra = f"telegram-search-mcp-{next(self._ids)}"
            request = dict(payload)
            request["@extra"] = extra
            if time.monotonic() >= deadline:
                if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                raise TDLibError("TDLib request timed out")
            self._raw.send(request)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                        raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                    raise TDLibError("TDLib request timed out")
                response = self._raw.receive(min(1.0, remaining))
                if response is not None:
                    self._reduce_receive_event(response)
                if time.monotonic() >= deadline:
                    if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                        raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                    raise TDLibError("TDLib request timed out")
                if response is None:
                    continue
                if response.get("@type") == "updateAuthorizationState":
                    authorization_state = response.get("authorization_state")
                    state_type = (
                        authorization_state.get("@type")
                        if isinstance(authorization_state, dict)
                        else None
                    )
                    self._ready = state_type == "authorizationStateReady"
                    if not self._ready and request_type not in _AUTHORIZATION_CONTROL_TYPES:
                        raise AuthorizationBlocked(
                            "the dedicated TDLib session lost authorization"
                        )
                    continue
                if response.get("@extra") != extra:
                    continue
                if response.get("@type") == "error":
                    code = response.get("code")
                    if not _is_strict_integer(code):
                        raise TDLibError("TDLib returned an invalid error response")
                    if code == 404:
                        raise MessageNotFound("Telegram message is no longer available")
                    failure = TDLibError(f"TDLib request failed with code {code}")
                    failure.provider_code = code if 0 < code < 10000 else None
                    raise failure
                return response

    @staticmethod
    def _encoded_database_key(key: bytes) -> str:
        if type(key) is not bytes or len(key) not in {0, 32}:
            raise AuthorizationBlocked("the dedicated TDLib database key is unavailable")
        return base64.b64encode(key).decode("ascii")

    def ensure_ready(self) -> None:
        for _ in range(8):
            if self._authorization_ended:
                self._ready = False
                raise AuthorizationBlocked("the dedicated TDLib session was logged out or closed")
            try:
                state = self._call({"@type": "getAuthorizationState"}).get("@type")
            except AuthorizationBlocked:
                raise
            except TDLibError:
                if self._authorization_ended:  # Closed TDLib answers every request with an error.
                    raise AuthorizationBlocked("the dedicated TDLib session was logged out or closed") from None
                raise
            self._ready = state == "authorizationStateReady"
            if not self._ready:
                self._invalidate_send_observations()
            if state in _ENDED_AUTHORIZATION_STATES:
                self._authorization_ended = True
            if state == "authorizationStateReady":
                return
            if state == "authorizationStateWaitTdlibParameters":
                credentials = self._credential_loader()
                session = ensure_private_directory(self._session_directory)
                database = ensure_private_directory(session / "db")
                files = ensure_private_directory(session / "files")
                self._call(
                    {
                        "@type": "setTdlibParameters",
                        "use_test_dc": False,
                        "database_directory": str(database),
                        "files_directory": str(files),
                        "database_encryption_key": self._encoded_database_key(
                            self._database_key_loader() if self._database_key_loader else credentials.database_key),
                        "use_file_database": False,
                        "use_chat_info_database": True,
                        "use_message_database": True,
                        "use_secret_chats": False,
                        "api_id": credentials.api_id,
                        "api_hash": credentials.api_hash,
                        "system_language_code": "en",
                        "device_model": product_identity.DEVICE_MODEL,
                        "system_version": platform.mac_ver()[0] or "macOS",
                        "application_version": __version__,
                    }
                )
                continue
            if state == "authorizationStateWaitEncryptionKey":
                self._call({"@type": "checkDatabaseEncryptionKey",
                            "encryption_key": self._encoded_database_key(
                                self._database_key_loader() if self._database_key_loader else b"")})
                continue
            raise AuthorizationBlocked("the dedicated TDLib session is not authorizationStateReady")
        raise AuthorizationBlocked("the dedicated TDLib session did not become ready")

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        numeric_target = _is_strict_integer(target)
        if numeric_target:
            chat = self._call({"@type": "getChat", "chat_id": target})
        elif isinstance(target, str) and target.startswith("@"):
            chat = self._call({"@type": "searchPublicChat", "username": target[1:]})
        else:
            raise TDLibError("target must be a numeric chat ID or exact @username")
        chat_id = chat.get("id")
        if chat.get("@type") != "chat" or not _is_strict_nonzero_integer(chat_id):
            raise TDLibError("TDLib returned an invalid chat")
        if numeric_target and chat_id != target:
            raise TDLibError("TDLib returned a different chat")
        chat_type = chat.get("type")
        if chat_type is not None and not isinstance(chat_type, dict):
            raise TDLibError("TDLib returned an invalid chat type")
        if (chat_type or {}).get("@type") == "chatTypeSecret":
            raise SecretChatRejected("secret chats are not supported")
        return chat

    def resolve_forum_chat(self, chat_id: int) -> dict[str, Any]:
        """Verify exact chat identity and the native forum capability."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        chat = self._call({"@type": "getChat", "chat_id": chat_id})
        if (
            chat.get("@type") != "chat"
            or not _is_forum_chat_id(chat.get("id"))
            or chat["id"] != chat_id
            or not isinstance(chat.get("type"), dict)
        ):
            raise TDLibError("TDLib returned an invalid forum chat")
        chat_type = chat["type"]
        kind = chat_type.get("@type")
        if kind == "chatTypeSupergroup":
            identifier = chat_type.get("supergroup_id")
            if (
                not _is_integer_in_range(identifier, 1, 2**53 - 1)
                or type(chat_type.get("is_channel")) is not bool
            ):
                raise TDLibError("TDLib returned an invalid forum chat type")
            if chat_type["is_channel"]:
                raise ForumUnsupported("the chat does not support forum topics")
            supergroup = self._call({"@type": "getSupergroup", "supergroup_id": identifier})
            if (
                supergroup.get("@type") != "supergroup"
                or not _is_integer_in_range(supergroup.get("id"), 1, 2**53 - 1)
                or supergroup["id"] != identifier
                or type(supergroup.get("is_forum")) is not bool
            ):
                raise TDLibError("TDLib returned an invalid forum capability")
            if supergroup["is_forum"] is not True:
                raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypePrivate":
            identifier = chat_type.get("user_id")
            if not _is_integer_in_range(identifier, 1, 2**53 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            user = self._call({"@type": "getUser", "user_id": identifier})
            if (
                user.get("@type") != "user"
                or not _is_integer_in_range(user.get("id"), 1, 2**53 - 1)
                or user["id"] != identifier
                or not isinstance(user.get("type"), dict)
            ):
                raise TDLibError("TDLib returned an invalid forum capability")
            user_type = user["type"]
            user_kind = user_type.get("@type")
            if not isinstance(user_kind, str):
                raise TDLibError("TDLib returned an invalid forum capability")
            if user_kind in {"userTypeRegular", "userTypeDeleted", "userTypeUnknown"}:
                raise ForumUnsupported("the chat does not support forum topics")
            if user_kind != "userTypeBot" or type(user_type.get("has_topics")) is not bool:
                raise TDLibError("TDLib returned an invalid forum capability")
            if user_type["has_topics"] is not True:
                raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypeBasicGroup":
            if not _is_integer_in_range(chat_type.get("basic_group_id"), 1, 2**53 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypeSecret":
            if not _is_integer_in_range(chat_type.get("secret_chat_id"), 1, 2**31 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            raise ForumUnsupported("the chat does not support forum topics")
        else:
            raise TDLibError("TDLib returned an invalid forum chat type")
        return chat

    def get_forum_topics(
        self, chat_id: int, *, offset_date: int = 0, offset_message_id: int = 0,
        offset_forum_topic_id: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        """Return one bounded raw page and its entire native continuation triple."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _valid_forum_offsets(offset_date, offset_message_id, offset_forum_topic_id):
            raise ValueError("forum offsets are invalid")
        if not _is_integer_in_range(limit, 1, 20):
            raise ValueError("forum topic limit is invalid")
        response = self._call({
            "@type": "getForumTopics", "chat_id": chat_id, "query": "",
            "offset_date": offset_date, "offset_message_id": offset_message_id,
            "offset_forum_topic_id": offset_forum_topic_id, "limit": limit,
        })
        topics = response.get("topics")
        # TDLib can return more rows than requested. This is our local safety
        # bound, not a native guarantee; retain every accepted row and offset.
        if (
            response.get("@type") != "forumTopics"
            or not isinstance(topics, list)
            or len(topics) > 200
            or any(not isinstance(topic, dict) or topic.get("@type") != "forumTopic" for topic in topics)
            or not _valid_forum_offsets(
                response.get("next_offset_date"), response.get("next_offset_message_id"),
                response.get("next_offset_forum_topic_id"),
            )
        ):
            raise TDLibError("TDLib returned an invalid forum topic page")
        # total_count is approximate; neither it nor the continuation triple
        # proves that this observed page is a complete topic inventory.
        return response

    def get_forum_topic(self, chat_id: int, forum_topic_id: int) -> dict[str, Any] | None:
        """Return native metadata, or None for the pinned native null/404 result."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _is_integer_in_range(forum_topic_id, 1, 2**31 - 1):
            raise ValueError("forum_topic_id is invalid")
        try:
            response = self._call({
                "@type": "getForumTopic", "chat_id": chat_id, "forum_topic_id": forum_topic_id,
            })
        except MessageNotFound:
            # Td::send_result converts ForumTopicManager's nullptr to a
            # correlated error(404); no JSON null object is emitted.
            return None
        if response.get("@type") != "forumTopic":
            raise TDLibError("TDLib returned an invalid forum topic")
        return response

    def get_forum_topic_history(
        self, chat_id: int, forum_topic_id: int, *, from_message_id: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        """One observed native page, with no completeness inference or ID conversion."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _is_integer_in_range(forum_topic_id, 1, 2**31 - 1):
            raise ValueError("forum_topic_id is invalid")
        if not _is_integer_in_range(from_message_id, 0, 2**53 - 1):
            raise ValueError("history boundary is invalid")
        if not _is_integer_in_range(limit, 1, 20):
            raise ValueError("history limit is invalid")
        response = self._call({"@type": "getForumTopicHistory", "chat_id": chat_id,
            "forum_topic_id": forum_topic_id, "from_message_id": from_message_id,
            "offset": 0, "limit": limit})
        rows = response.get("messages")
        # The pinned native path slices to limit. The 200-row guard is local
        # defense against drift, not a promise that Telegram returns this size.
        if (response.get("@type") != "messages" or not isinstance(rows, list) or len(rows) > 200
                or not _is_integer_in_range(response.get("total_count"), -1, 2**31 - 1)
                or any(not isinstance(row, dict) or row.get("@type") != "message" for row in rows)):
            raise TDLibError("TDLib returned an invalid forum history page")
        return response

    def get_account_id(self) -> int:
        user = self._call({"@type": "getMe"})
        account_id = user.get("id")
        if user.get("@type") != "user" or type(account_id) is not int or not 0 < account_id < 2**53:
            raise TDLibError("TDLib returned an invalid account identity")
        if self._send_account_id is not None and self._send_account_id != account_id:
            self._invalidate_send_observations()
        self._send_account_id = account_id
        return account_id

    def get_self_chat(self) -> dict[str, Any]:
        user = self._call({"@type": "getMe"})
        user_id = user.get("id")
        if (
            user.get("@type") != "user"
            or not _is_strict_integer(user_id)
            or user_id <= 0
        ):
            raise TDLibError("TDLib returned an invalid account identity")
        chat = self._call({"@type": "createPrivateChat", "user_id": user_id, "force": True})
        chat_type = chat.get("type")
        chat_type_name = chat_type.get("@type") if isinstance(chat_type, dict) else None
        if chat_type_name == "chatTypeSecret":
            raise SecretChatRejected("secret chats are not supported")
        if (
            chat.get("@type") != "chat"
            or not _is_strict_nonzero_integer(chat.get("id"))
            or chat_type_name != "chatTypePrivate"
            or not _is_strict_integer(chat_type.get("user_id") if isinstance(chat_type, dict) else None)
            or chat_type["user_id"] != user_id
        ):
            raise TDLibError("TDLib returned an invalid self chat")
        return chat

    def search_known_chat_ids(self, query: str) -> tuple[list[int], bool]:
        return self._search_chat_ids(
            {"@type": "searchChats", "query": query, "limit": 20, "type_filter": None},
            cap=20,
            with_total_count=True,
        )

    def search_known_chat_ids_on_server(self, query: str) -> tuple[list[int], bool]:
        return self._search_chat_ids(
            {
                "@type": "searchChatsOnServer",
                "query": query,
                "limit": 20,
                "type_filter": None,
            },
            cap=20,
            with_total_count=True,
        )

    def get_recent_main_chat_ids(self) -> list[int]:
        chat_ids, _ = self._search_chat_ids(
            {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 50},
            cap=50,
            with_total_count=False,
        )
        return chat_ids

    def get_chat_list_snapshot(self, chat_list: ChatListName, *, limit: int) -> dict[str, Any]:
        """Return the raw bounded getChats envelope without catalog normalization."""
        if chat_list not in ("main", "archive") or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid chat listing request")
        return self._call({"@type": "getChats", "chat_list": _chat_list_payload(chat_list), "limit": limit})

    def get_chat_metadata(self, chat_id: int) -> dict[str, Any]:
        """Read exact local-state chat metadata; never open or mark a chat read."""
        if type(chat_id) is not int or chat_id == 0 or abs(chat_id) > 2**53 - 1:
            raise ValueError("invalid chat listing identity")
        return self._call({"@type": "getChat", "chat_id": chat_id})

    def get_chat_list_prefix(self, chat_list: ChatListName) -> list[int]:
        chat_ids, _ = self._search_chat_ids(
            {
                "@type": "getChats",
                "chat_list": _chat_list_payload(chat_list),
                "limit": 15,
            },
            cap=15,
            with_total_count=False,
        )
        return chat_ids

    def load_more_chats(
        self,
        chat_list: ChatListName,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool:
        list_payload = _chat_list_payload(chat_list)
        try:
            self._call({"@type": "loadChats", "chat_list": list_payload, "limit": 50})
        except MessageNotFound:
            on_positions(self._catalog_snapshot(chat_list))
            return True
        except AuthorizationBlocked:
            raise
        except TDLibError:
            on_positions(self._catalog_snapshot(chat_list))
            raise
        on_positions(self._catalog_snapshot(chat_list))
        return False

    def search_global_messages(
        self,
        chat_list: Literal["main", "archive"],
        query: str,
        *,
        offset: str,
    ) -> GlobalMessagePage:
        list_payload = _chat_list_payload(chat_list)
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not isinstance(offset, str):
            raise TypeError("offset must be a string")
        response = self._call(
            {
                "@type": "searchMessages",
                "chat_list": list_payload,
                "query": query,
                "offset": offset,
                "limit": 10,
                "filter": None,
                "chat_type_filter": None,
                "min_date": 0,
                "max_date": 0,
            }
        )
        return _validate_global_message_page(response)

    def _search_chat_ids(
        self, payload: dict[str, Any], *, cap: int, with_total_count: bool
    ) -> tuple[list[int], bool]:
        response = self._call(payload)
        chat_ids = response.get("chat_ids")
        total_count = response.get("total_count")
        if (
            response.get("@type") != "chats"
            or not isinstance(chat_ids, list)
            or (
                with_total_count
                and (not _is_strict_integer(total_count) or total_count < 0)
            )
        ):
            raise TDLibError("TDLib returned an invalid chat discovery response")
        if not all(_is_strict_nonzero_integer(chat_id) for chat_id in chat_ids):
            raise TDLibError("TDLib returned an invalid chat discovery response")
        unique_chat_ids = list(dict.fromkeys(chat_ids))[:cap]
        return unique_chat_ids, bool(with_total_count and total_count <= len(unique_chat_ids))

    def search_chat_messages(
        self,
        chat_id: int,
        query: str,
        *,
        from_message_id: int,
        limit: int,
        sender: Any = None,
        topic: Any = None,
    ) -> dict[str, Any]:
        from .schemas import SearchSenderReference, ForumTopicReference
        if sender is not None and not isinstance(sender, SearchSenderReference):
            raise ValueError("search sender must be a typed reference")
        if topic is not None and not isinstance(topic, ForumTopicReference):
            raise ValueError("search topic must be a typed forum reference")
        native_sender = None if sender is None else {
            "@type": "messageSenderUser" if sender.kind == "user" else "messageSenderChat",
            "user_id" if sender.kind == "user" else "chat_id": sender.id,
        }
        native_topic = None if topic is None else {"@type": "messageTopicForum", "forum_topic_id": topic.id}
        return self._call(
            {
                "@type": "searchChatMessages",
                "chat_id": chat_id,
                "topic_id": native_topic,
                "query": query,
                "sender_id": native_sender,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "filter": None,
            }
        )

    def search_chat_media(
        self,
        chat_id: int,
        media_type: str,
        *,
        from_message_id: int,
        limit: int,
    ) -> dict[str, Any]:
        media_filter = _CHAT_MEDIA_FILTERS.get(media_type)
        if media_filter is None:
            raise ValueError("media_type must be video or video_note")
        return self._call(
            {
                "@type": "searchChatMessages",
                "chat_id": chat_id,
                "topic_id": None,
                "query": "",
                "sender_id": None,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "filter": media_filter,
            }
        )

    def get_chat_history(
        self,
        chat_id: int,
        *,
        from_message_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        response = self._call(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "only_local": False,
            }
        )
        messages = response.get("messages")
        if (response.get("@type") != "messages" or not isinstance(messages, list)
                or len(messages) > min(max(limit, 1), 100) or any(not isinstance(item, dict) for item in messages)):
            raise TDLibError("TDLib returned an invalid chat history")
        return messages

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        message = self._call(
            {"@type": "getMessage", "chat_id": chat_id, "message_id": message_id}
        )
        if message.get("@type") != "message":
            raise TDLibError("TDLib returned an invalid message")
        return message

    def send_document_message(self, chat_id: int, path: Path, caption: str, *, attempt_id: str | None = None, pre_send_fence=None) -> int:
        """Make one provider attempt and wait for a matching delivery update."""
        content = {
            "@type": "inputMessageDocument",
            "document": {
                "@type": "inputDocument",
                "document": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None, "disable_content_type_detection": True,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
        }
        return self._send_message_content(chat_id, content, "messageDocument", attempt_id=attempt_id, pre_transport_fence=pre_send_fence)

    def send_text_message(self, chat_id: int, text: str, *, attempt_id: str | None = None,
                          outgoing_entities: FrozenOutgoingEntities = (),
                          expected_account_id=None, expected_recipient_title=None,
                          pre_send_guard=None) -> int:
        """One text attempt; nonempty styles require reviewed current authority."""
        if outgoing_entities != ():
            from .sanitize import sanitize_telegram_text
            with self._serialized_reply_call():
                def current_authority():
                    if (type(expected_account_id) is not int or expected_account_id <= 0 or
                            type(expected_recipient_title) is not str or
                            type(attempt_id) is not str or not attempt_id or
                            not callable(pre_send_guard) or pre_send_guard() is not True or
                            self.get_account_id() != expected_account_id):
                        return False
                    chat = self.resolve_target(chat_id)
                    title = sanitize_telegram_text(chat.get('title'), max_length=255) or str(chat_id)
                    return (title == expected_recipient_title and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
                try:
                    frozen = _validated_frozen_outgoing_entities(text, outgoing_entities)
                    if not current_authority():
                        raise ValueError('formatted text authority changed')
                    content = {'@type': 'inputMessageText',
                        'text': {'@type': 'formattedText', 'text': text,
                                 'entities': native_outgoing_entities(frozen)},
                        'link_preview_options': dict(LINK_OPTIONS), 'clear_draft': False}
                except Exception as error:
                    raise MessageSendNotAttempted('formatted text revalidation failed') from error
                return self._send_message_content(chat_id, content, 'messageText',
                    attempt_id=attempt_id, outgoing_entities=frozen,
                    # TDLib may project an ordinary self-send into the account's
                    # Saved Messages topic. This is receipt identity, not routing.
                    saved_messages_topic_id=expected_account_id if chat_id == expected_account_id else None,
                    pre_transport_guard=current_authority)
        content = {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
            "link_preview_options": {
                "@type": "linkPreviewOptions", "is_disabled": True, "url": "",
                "force_small_media": False, "force_large_media": False,
                "show_above_text": False,
            },
            "clear_draft": False,
        }
        return self._send_message_content(chat_id, content, "messageText", attempt_id=attempt_id)

    def read_reply_source(self, chat_id: int, message_id: int, *,
                          expected_forum_topic_id: int | None = None,
                          require_reply_eligibility: bool = False) -> ReplySource:
        if type(require_reply_eligibility) is not bool:
            raise ValueError('reply eligibility selector is invalid')
        if expected_forum_topic_id is None and not require_reply_eligibility:
            return source_from_message(self.get_message(chat_id,message_id),chat_id,message_id)
        if expected_forum_topic_id is not None and not _is_integer_in_range(expected_forum_topic_id, 1, 2**31 - 1):
            raise ValueError('reply topic is unavailable')
        with self._serialized_provider_call(getattr(self._request_context, 'deadline', None)):
            source = source_from_message(self.get_message(chat_id, message_id), chat_id, message_id,
                expected_forum_topic_id=expected_forum_topic_id)
            properties = self._call({'@type': 'getMessageProperties', 'chat_id': chat_id,
                'message_id': message_id})
            if (properties.get('@type') != 'messageProperties' or
                    properties.get('can_be_replied') is not True):
                raise TDLibError('reply eligibility is unavailable')
            return source

    def read_forward_source(self, chat_id: int, message_id: int):
        """One serialized read of source chat identity, projection and properties."""
        from .forward_sources import ForwardSnapshot, ForwardSource, chat_identity
        if not _is_forum_chat_id(chat_id) or not _is_integer_in_range(message_id, 1, 2**53 - 1):
            raise ValueError('forward source is unavailable')
        with self._serialized_provider_call(getattr(self._request_context, 'deadline', None)):
            chat = self.resolve_target(chat_id)
            chat_identity(chat, chat_id)
            source = ForwardSource.from_message(self.get_message(chat_id, message_id), chat_id, message_id)
            properties = self._call({'@type': 'getMessageProperties', 'chat_id': chat_id,
                'message_id': message_id})
            return ForwardSnapshot.create(chat, chat_id, source, properties)

    def forward_message(self, chat_id: int, *, snapshot, mode: str, remove_caption: bool,
                        expected_account_id: int, expected_recipient_title: str,
                        expected_recipient_identity: tuple, attempt_id: str,
                        pre_send_guard: Callable[[], bool]) -> int:
        """Revalidate the exact reviewed source and recipient, then make one attempt.

        Every refusal before registration/rawsend is no-attempt evidence. The
        provider forwards its own stored message; no content is reconstructed.
        """
        from .forward_sources import ForwardSnapshot, chat_identity
        with self._serialized_reply_call():
            def current_authority():
                if (not isinstance(snapshot, ForwardSnapshot) or mode not in ('forward', 'copy') or
                        type(remove_caption) is not bool or (remove_caption and mode != 'copy') or
                        type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                        type(expected_recipient_title) is not str or type(expected_recipient_identity) is not tuple or
                        type(attempt_id) is not str or not attempt_id or not callable(pre_send_guard) or
                        pre_send_guard() is not True or self.get_account_id() != expected_account_id):
                    return False
                identity, title = chat_identity(self.resolve_target(chat_id), chat_id)
                if identity != expected_recipient_identity or title != expected_recipient_title:
                    return False
                current = self.read_forward_source(*snapshot.source.anchor)
                return (current.source == snapshot.source and current.chat_identity == snapshot.chat_identity and
                        current.chat_title == snapshot.chat_title and current.refusal(mode) is None and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
            try:
                if not current_authority():
                    raise ValueError('forward authority changed')
            except Exception as error:
                raise MessageSendNotAttempted('forward revalidation failed') from error
            # The account's own Saved Messages chat has the account user ID.
            to_self = chat_id == expected_account_id and expected_recipient_identity == ('chatTypePrivate', chat_id)
            return self._forward_transport(chat_id, snapshot.source, send_copy=mode == 'copy',
                remove_caption=remove_caption, attempt_id=attempt_id, pre_transport_guard=pre_send_guard,
                to_self=to_self)

    def _forward_transport(self, chat_id: int, source, *, send_copy: bool, remove_caption: bool,
                           attempt_id: str, pre_transport_guard: Callable[[], bool], to_self: bool = False) -> int:
        sending_id = secrets.randbelow((1 << 31) - 1) + 1
        for _ in range(8):
            if self._send_observations.sending_id_available(sending_id):
                break
            sending_id = secrets.randbelow((1 << 31) - 1) + 1
        else:
            raise MessageSendNotAttempted("send correlation is unavailable")
        from_chat_id, message_id = source.anchor
        payload = {"@type": "forwardMessages", "chat_id": chat_id, "topic_id": None,
                   "from_chat_id": from_chat_id, "message_ids": [message_id],
                   "options": {**SEND_OPTIONS, "sending_id": sending_id},
                   "send_copy": send_copy, "remove_caption": remove_caption}
        try:
            _validate_bounded_request(payload, self._staging_root)
        except ForbiddenTDLibRequest as error:
            raise MessageSendNotAttempted("forward request shape is not allowed") from error
        with self._serialized_reply_call():
            extra = f"telegram-search-mcp-{next(self._ids)}"
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            if not self._ready or (aggregate_deadline is not None and time.monotonic() >= aggregate_deadline):
                raise MessageSendNotAttempted("forward provider is unavailable before send")
            try:
                self._send_observations.register(attempt_id, extra, sending_id, chat_id,
                                                 source.content_type, forward=True, forward_self=to_self is True)
            except ObservationUnavailable as error:
                raise MessageSendNotAttempted("send correlation is unavailable") from error
            try:
                if pre_transport_guard() is not True:
                    raise ValueError("forward scope changed")
                if aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                    raise ValueError("forward request deadline expired")
            except Exception as error:
                self._send_observations.discard_unattempted(attempt_id)
                raise MessageSendNotAttempted("forward scope changed before transport") from error
            try:
                self._raw.send({**payload, "@extra": extra})
            except Exception as error:
                raise MessageSendOutcomeUnknown("TDLib forward transport failed") from error
            deadline = time.monotonic() + min(max(self._request_timeout, 30), 300)
            if aggregate_deadline is not None:
                deadline = min(deadline, aggregate_deadline)
            result_valid = False  # `sent` needs the exact forwardMessages result, even after an early success.
            while time.monotonic() < deadline:
                try:
                    response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
                except Exception as error:
                    raise MessageSendOutcomeUnknown("TDLib confirmation transport was lost") from error
                if response is None:
                    continue
                self._reduce_receive_event(response)
                if not self._ready:
                    raise MessageSendOutcomeUnknown("authorization changed before confirmation")
                observation = self._send_observations.snapshot(attempt_id)
                if observation is not None and observation.status == "failed":
                    provider_error = response if response.get("@type") == "error" else response.get("error")
                    raise MessageSendFailed("TDLib reported forward failure", provider_error=provider_error)
                if observation is None or observation.status == "conflict":
                    raise MessageSendOutcomeUnknown("TDLib forward evidence is unavailable or conflicting")
                if response.get("@extra") == extra:
                    if SendObservations.forward_result_message(response, observation) is None:
                        raise MessageSendOutcomeUnknown("TDLib returned an invalid forward result")
                    result_valid = True
                if observation.status == "sent":
                    if result_valid:
                        return observation.message_id
                elif (response.get("@type") == "updateMessageSendSucceeded"
                        and type(response.get("old_message_id")) is int
                        and response["old_message_id"] == observation.temporary_id):
                    raise MessageSendOutcomeUnknown("TDLib returned an invalid final message")
            raise MessageSendOutcomeUnknown("TDLib forward confirmation timed out")

    # Bounded observation of the chat list after one addChatToList attempt.
    CHANGE_OBSERVATION_SECONDS = 10.0
    CHANGE_POLL_SECONDS = 0.25

    def read_chat_list_state(self, chat_id: int):
        """One serialized read of chat identity, title, kind and current Main/Archive list.

        A fresh session has not loaded its chats, and getChat then answers 400. Only that answer
        triggers one loadChats per list and exactly one more getChat; any other failure, and a
        second 400, propagate unchanged."""
        from .archive_drafts import ArchiveSnapshot
        if not _is_forum_chat_id(chat_id):
            raise ValueError("archive chat is unavailable")
        with self._serialized_provider_call(getattr(self._request_context, "deadline", None)):
            try:
                chat = self.resolve_target(chat_id)
            except TDLibError as error:
                if error.provider_code != 400:
                    raise
                for chat_list in ("main", "archive"):
                    try:
                        self._call({"@type": "loadChats", "chat_list": _chat_list_payload(chat_list), "limit": 100})
                    except MessageNotFound:
                        pass  # 404: TDLib has no more chats in this list.
                chat = self.resolve_target(chat_id)
            return ArchiveSnapshot.create(chat, chat_id)

    def chat_lists_to_add(self, chat_id: int) -> frozenset[str]:
        """The Main/Archive lists TDLib offers for adding this chat; folder lists are ignored."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("archive chat is unavailable")
        response = self._call({"@type": "getChatListsToAddChat", "chat_id": chat_id})
        lists = response.get("chat_lists")
        if (response.get("@type") != "chatLists" or set(response) - {"@type", "@extra", "chat_lists"} or
                type(lists) is not list or len(lists) > 64):
            raise TDLibError("TDLib returned an invalid chat lists result")
        offered = set()
        for item in lists:
            kind = item.get("@type") if type(item) is dict else None
            if kind in ("chatListMain", "chatListArchive") and set(item) == {"@type"}:
                offered.add("main" if kind == "chatListMain" else "archive")
            elif kind != "chatListFolder":
                raise TDLibError("TDLib returned an invalid chat lists result")
        return frozenset(offered)

    def move_chat_to_list(self, chat_id: int, *, target_list: str, expected, expected_account_id: int,
                          pre_send_guard: Callable[[], bool]) -> str:
        """Revalidate the reviewed chat under the provider lock, make one addChatToList attempt,
        then observe the chat's list with bounded getChat reads outside the lock.

        Every refusal before the raw send is no-attempt evidence; nothing is retried. Returns the
        observed list ('main', 'archive', 'none') or 'unknown' when it could not be read."""
        from .archive_drafts import LIST_TYPES, ArchiveSnapshot
        with self._serialized_reply_call():
            def current_authority():
                if (not isinstance(expected, ArchiveSnapshot) or expected.chat_id != chat_id or
                        target_list not in LIST_TYPES or expected.current_list not in LIST_TYPES or
                        expected.current_list == target_list or
                        type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                        not callable(pre_send_guard) or pre_send_guard() is not True or
                        self.get_account_id() != expected_account_id):
                    return False
                return (self.read_chat_list_state(chat_id) == expected and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
            try:
                if not current_authority():
                    raise ValueError("archive authority changed")
            except Exception as error:
                raise MessageSendNotAttempted("archive revalidation failed") from error
            response = self._change_transport({"@type": "addChatToList", "chat_id": chat_id,
                                               "chat_list": {"@type": LIST_TYPES[target_list]}},
                                              pre_transport_guard=pre_send_guard)
            if response.get("@type") != "ok" or set(response) - {"@type", "@extra"}:
                raise MessageSendOutcomeUnknown("TDLib returned an invalid chat list result")
        return self._observe_chat_list(chat_id, target_list)

    def _observe_chat_list(self, chat_id: int, target_list: str) -> str:
        observed = "unknown"
        deadline = time.monotonic() + self.CHANGE_OBSERVATION_SECONDS
        aggregate_deadline = getattr(self._request_context, "deadline", None)
        if aggregate_deadline is not None:
            deadline = min(deadline, aggregate_deadline)
        for _ in range(64):
            try:
                observed = self.read_chat_list_state(chat_id).current_list
            except Exception:
                observed = "unknown"
            if observed == target_list or time.monotonic() + self.CHANGE_POLL_SECONDS >= deadline:
                break
            time.sleep(self.CHANGE_POLL_SECONDS)
        return observed

    def read_own_text_message(self, chat_id: int, message_id: int):
        """One serialized read of chat identity, the own-text projection and edit permission."""
        from .edit_drafts import EditSnapshot
        if not _is_forum_chat_id(chat_id) or not _is_integer_in_range(message_id, 1, 2**53 - 1):
            raise ValueError("edit message is unavailable")
        with self._serialized_provider_call(getattr(self._request_context, "deadline", None)):
            chat = self.resolve_target(chat_id)
            message = self.get_message(chat_id, message_id)
            properties = self._call({"@type": "getMessageProperties", "chat_id": chat_id, "message_id": message_id})
            return EditSnapshot.create(chat, chat_id, message, message_id, properties)

    def edit_own_text(self, chat_id: int, message_id: int, *, new_text: str, expected, expected_account_id: int,
                      pre_send_guard: Callable[[], bool]) -> int:
        """Revalidate the reviewed projection and edit permission under the provider lock, make
        one editMessageText attempt and return the validated edit_date of the returned message.

        Every refusal before the raw send is no-attempt evidence; a result that is not exactly
        the edited message is an unknown outcome. Nothing is retried."""
        from .edit_drafts import EditSnapshot, edit_request, edited_text_result
        with self._serialized_reply_call():
            def current_authority():
                if (not isinstance(expected, EditSnapshot) or expected.anchor != (chat_id, message_id) or
                        not expected.can_be_edited or type(new_text) is not str or new_text == expected.raw_text or
                        type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                        not callable(pre_send_guard) or pre_send_guard() is not True or
                        self.get_account_id() != expected_account_id):
                    return False
                return (self.read_own_text_message(chat_id, message_id) == expected and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
            try:
                if not current_authority():
                    raise ValueError("edit authority changed")
            except Exception as error:
                raise MessageSendNotAttempted("edit revalidation failed") from error
            response = self._change_transport(edit_request(chat_id, message_id, new_text),
                                              pre_transport_guard=pre_send_guard)
        edit_date = edited_text_result(response, chat_id=chat_id, message_id=message_id, new_text=new_text,
                                       previous_edit_date=expected.edit_date)
        if edit_date is None:
            raise MessageSendOutcomeUnknown("TDLib returned an invalid edit result")
        return edit_date

    def _change_transport(self, payload: dict[str, Any], *, pre_transport_guard: Callable[[], bool]) -> dict[str, Any]:
        """One raw state-change request and its direct result; no send observation is involved."""
        try:
            _validate_bounded_request(payload, self._staging_root)
            if payload.get("@type") not in _CHANGE_REQUEST_TYPES:
                raise ForbiddenTDLibRequest("TDLib change request is not allowed")
        except ForbiddenTDLibRequest as error:
            raise MessageSendNotAttempted("change request shape is not allowed") from error
        with self._serialized_reply_call():
            extra = f"telegram-search-mcp-{next(self._ids)}"
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            if not self._ready or (aggregate_deadline is not None and time.monotonic() >= aggregate_deadline):
                raise MessageSendNotAttempted("change provider is unavailable before transport")
            try:
                if pre_transport_guard() is not True:
                    raise ValueError("change scope changed")
                if aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                    raise ValueError("change request deadline expired")
            except Exception as error:
                raise MessageSendNotAttempted("change scope changed before transport") from error
            try:
                self._raw.send({**payload, "@extra": extra})
            except Exception as error:
                raise MessageSendOutcomeUnknown("TDLib change transport failed") from error
            deadline = time.monotonic() + min(max(self._request_timeout, 30), 300)
            if aggregate_deadline is not None:
                deadline = min(deadline, aggregate_deadline)
            while time.monotonic() < deadline:
                try:
                    response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
                except Exception as error:
                    raise MessageSendOutcomeUnknown("TDLib change result transport was lost") from error
                if response is None:
                    continue
                self._reduce_receive_event(response)
                if not self._ready:
                    raise MessageSendOutcomeUnknown("authorization changed before the change result")
                if response.get("@extra") != extra:
                    continue
                if response.get("@type") == "error":
                    # Only a 4xx provider rejection proves no change; 5xx, negative or malformed
                    # codes may follow an applied change.
                    code = response.get("code")
                    if type(code) is int and 400 <= code <= 499:
                        raise MessageSendFailed("TDLib rejected the change", provider_error=response)
                    raise MessageSendOutcomeUnknown("TDLib change error does not prove rejection")
                return response
            raise MessageSendOutcomeUnknown("TDLib change result timed out")

    def read_topic_snapshot(self, chat_id: int, topic_id: int, *, expected_account_id: int,
                            expected_recipient_title: str):
        from .topic_models import snapshot_from_provider
        from .sanitize import sanitize_telegram_text
        with self._serialized_provider_call(getattr(self._request_context, 'deadline', None)):
            if type(expected_account_id) is not int or self.get_account_id() != expected_account_id:
                raise TDLibError('topic account is unavailable')
            chat = self.resolve_forum_chat(chat_id)
            title = sanitize_telegram_text(chat.get('title'), max_length=255) or str(chat_id)
            if title != expected_recipient_title:
                raise TDLibError('topic recipient changed')
            topic = snapshot_from_provider(expected_account_id, chat_id, topic_id,
                chat, self.get_forum_topic(chat_id, topic_id))
            if self.get_account_id() != expected_account_id:
                raise TDLibError('topic account changed')
            return topic

    def send_topic_text_message(self, chat_id: int, text: str, *, topic,
                               expected_account_id: int, expected_recipient_title: str,
                               attempt_id: str, pre_send_guard: Callable[[], bool],
                               outgoing_entities: FrozenOutgoingEntities = ()) -> int:
        """Recheck one exact reviewed topic and correlate its final native message."""
        from .topic_models import TopicSnapshot
        with self._serialized_reply_call():
            def current_authority():
                return (isinstance(topic, TopicSnapshot) and topic.account_id == expected_account_id and
                    topic.chat_id == chat_id and pre_send_guard() is True and
                    self.read_topic_snapshot(chat_id, topic.forum_topic_id,
                        expected_account_id=expected_account_id,
                        expected_recipient_title=expected_recipient_title) == topic and
                    pre_send_guard() is True)
            try:
                frozen = (_validated_frozen_outgoing_entities(text, outgoing_entities)
                          if outgoing_entities != () else ())
                if frozen and (type(attempt_id) is not str or not attempt_id or
                        type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                        type(expected_recipient_title) is not str or not callable(pre_send_guard)):
                    raise ValueError('formatted send authority is unavailable')
                if not current_authority():
                    raise ValueError('topic authority changed')
                content = {'@type': 'inputMessageText',
                    'text': {'@type': 'formattedText', 'text': text, 'entities': native_outgoing_entities(frozen)},
                    'link_preview_options': dict(LINK_OPTIONS), 'clear_draft': False}
            except Exception as error:
                raise MessageSendNotAttempted('topic revalidation failed') from error
            return self._send_message_content(chat_id, content, 'messageText', attempt_id=attempt_id,
                forum_topic_id=topic.forum_topic_id, pre_transport_guard=current_authority,
                **({'outgoing_entities': frozen} if frozen else {}))

    @contextmanager
    def _serialized_reply_call(self):
        # Convert only failed lock acquisition into no-attempt evidence. Errors
        # after yielding keep their delivery-uncertainty classification.
        scope=self._serialized_provider_call(getattr(self._request_context,"deadline",None))
        try:
            scope.__enter__()
        except Exception as error:
            raise MessageSendNotAttempted("reply provider lock is unavailable") from error
        try:
            yield
        finally:
            scope.__exit__(None,None,None)

    def send_reply_text_message(self, chat_id: int, text: str, *, reply_source: ReplySource,
                                expected_account_id: int, expected_recipient_title: str,
                                attempt_id: str, pre_send_guard: Callable[[], bool], topic=None,
                                outgoing_entities: FrozenOutgoingEntities = ()) -> int:
        """Revalidate cached/offline TDLib evidence under one local provider lock.

        Remote edits may still race these reads; exact output reply correlation is
        mandatory. No refusal before registration/rawsend creates a send attempt.
        """
        from .sanitize import sanitize_telegram_text
        with self._serialized_reply_call():
            if topic is not None:
                from .topic_models import TopicSnapshot
                def current_authority():
                    if (not isinstance(topic, TopicSnapshot) or
                            not isinstance(reply_source, ReplySource) or
                            type(expected_account_id) is not int or
                            topic.account_id != expected_account_id or topic.chat_id != chat_id or
                            reply_source.anchor[0] != chat_id or
                            reply_source.forum_topic_id != topic.forum_topic_id or
                            pre_send_guard() is not True):
                        return False
                    current_topic = self.read_topic_snapshot(chat_id, topic.forum_topic_id,
                        expected_account_id=expected_account_id,
                        expected_recipient_title=expected_recipient_title)
                    if current_topic != topic or pre_send_guard() is not True:
                        return False
                    current_source = self.read_reply_source(*reply_source.anchor,
                        expected_forum_topic_id=topic.forum_topic_id)
                    return (current_source.source_sha256 == reply_source.source_sha256 and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
                try:
                    frozen = (_validated_frozen_outgoing_entities(text, outgoing_entities)
                              if outgoing_entities != () else ())
                    if frozen and (type(attempt_id) is not str or not attempt_id or
                            type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                            type(expected_recipient_title) is not str or not callable(pre_send_guard)):
                        raise ValueError('formatted send authority is unavailable')
                    if not current_authority():
                        raise ValueError('topic reply authority changed')
                    content = {'@type': 'inputMessageText',
                        'text': {'@type': 'formattedText', 'text': text, 'entities': native_outgoing_entities(frozen)},
                        'link_preview_options': dict(LINK_OPTIONS), 'clear_draft': False}
                except Exception as error:
                    raise MessageSendNotAttempted('topic reply revalidation failed') from error
                return self._send_message_content(chat_id, content, 'messageText', attempt_id=attempt_id,
                    reply_anchor=reply_source.anchor, forum_topic_id=topic.forum_topic_id,
                    pre_transport_guard=current_authority,
                    **({'outgoing_entities': frozen} if frozen else {}))
            if outgoing_entities != ():
                def current_formatted_reply_authority():
                    if (not isinstance(reply_source, ReplySource) or reply_source.anchor[0] != chat_id or
                            reply_source.forum_topic_id is not None or type(expected_account_id) is not int or
                            pre_send_guard() is not True or self.get_account_id() != expected_account_id):
                        return False
                    chat = self.resolve_target(chat_id)
                    title = sanitize_telegram_text(chat.get('title'), max_length=255) or str(chat_id)
                    if title != expected_recipient_title:
                        return False
                    current = self.read_reply_source(*reply_source.anchor)
                    properties = self._call({'@type': 'getMessageProperties', 'chat_id': chat_id,
                        'message_id': reply_source.anchor[1]})
                    return (current.source_sha256 == reply_source.source_sha256 and
                        properties.get('@type') == 'messageProperties' and
                        properties.get('can_be_replied') is True and
                        self.get_account_id() == expected_account_id and pre_send_guard() is True)
                try:
                    frozen = _validated_frozen_outgoing_entities(text, outgoing_entities)
                    if (type(attempt_id) is not str or not attempt_id or
                            type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                            type(expected_recipient_title) is not str or not callable(pre_send_guard)):
                        raise ValueError('formatted send authority is unavailable')
                    if not current_formatted_reply_authority():
                        raise ValueError('formatted reply authority changed')
                    content = {'@type': 'inputMessageText',
                        'text': {'@type': 'formattedText', 'text': text,
                                 'entities': native_outgoing_entities(frozen)},
                        'link_preview_options': dict(LINK_OPTIONS), 'clear_draft': False}
                except Exception as error:
                    raise MessageSendNotAttempted('formatted reply revalidation failed') from error
                return self._send_message_content(chat_id, content, 'messageText', attempt_id=attempt_id,
                    reply_anchor=reply_source.anchor, outgoing_entities=frozen,
                    pre_transport_guard=current_formatted_reply_authority)
            try:
                if (not isinstance(reply_source,ReplySource) or reply_source.anchor[0]!=chat_id
                        or type(expected_account_id) is not int or self.get_account_id()!=expected_account_id):
                    raise ValueError('reply source is unavailable')
                chat=self.resolve_target(chat_id)
                title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(chat_id)
                if title!=expected_recipient_title:
                    raise ValueError('reply recipient is unavailable')
                current=self.read_reply_source(*reply_source.anchor)
                if current.source_sha256!=reply_source.source_sha256:
                    raise ValueError('reply source changed')
                properties=self._call({'@type':'getMessageProperties','chat_id':chat_id,'message_id':reply_source.anchor[1]})
                if properties.get('@type')!='messageProperties' or properties.get('can_be_replied') is not True:
                    raise ValueError('reply eligibility is unavailable')
                if self.get_account_id()!=expected_account_id or pre_send_guard() is not True:
                    raise ValueError('reply scope changed')
                content={'@type':'inputMessageText','text':{'@type':'formattedText','text':text,'entities':[]},
                         'link_preview_options':dict(LINK_OPTIONS),'clear_draft':False}
            except Exception as error:
                raise MessageSendNotAttempted('reply revalidation failed') from error
            return self._send_message_content(chat_id,content,'messageText',attempt_id=attempt_id,
                reply_anchor=reply_source.anchor,**({'pre_transport_guard':pre_send_guard} if reply_source.required_capabilities else {}))

    def send_reply_artifact_message(self,chat_id: int,path: Path,caption: str,*,kind: str,
            reply_source: ReplySource,expected_account_id: int,expected_recipient_title: str,
            attempt_id: str,pre_send_guard: Callable[[],bool],duration_seconds: int | None=None,
            waveform_base64: str | None=None,pre_send_fence=None) -> int:
        from .sanitize import sanitize_telegram_text
        self._request_context.artifact_transport_attempted=False
        try:
            with self._serialized_reply_call():
                if (not isinstance(reply_source,ReplySource) or reply_source.anchor[0]!=chat_id
                    or type(expected_account_id) is not int or self.get_account_id()!=expected_account_id):
                    raise ValueError('reply account changed')
                chat=self.resolve_target(chat_id)
                if (sanitize_telegram_text(chat.get('title'),max_length=255) or str(chat_id))!=expected_recipient_title:
                    raise ValueError('reply recipient changed')
                if self.read_reply_source(*reply_source.anchor).source_sha256!=reply_source.source_sha256:
                    raise ValueError('reply source changed')
                properties=self._call({'@type':'getMessageProperties','chat_id':chat_id,'message_id':reply_source.anchor[1]})
                if properties.get('@type')!='messageProperties' or properties.get('can_be_replied') is not True:
                    raise ValueError('reply eligibility changed')
                if self.get_account_id()!=expected_account_id or pre_send_guard() is not True:
                    raise ValueError('reply scope changed')
                formatted={'@type':'formattedText','text':caption,'entities':[]}
                local={'@type':'inputFileLocal','path':str(path)}
                if kind=='document':
                    content={'@type':'inputMessageDocument','document':{'@type':'inputDocument','document':local,
                        'thumbnail':None,'disable_content_type_detection':True},'caption':formatted}
                    expected='messageDocument'
                elif kind=='photo':
                    content={'@type':'inputMessagePhoto','photo':{'@type':'inputPhoto','photo':local,'thumbnail':None,
                        'video':None,'added_sticker_file_ids':[],'width':0,'height':0},'caption':formatted,
                        'show_caption_above_media':False,'self_destruct_type':None,'has_spoiler':False}
                    expected='messagePhoto'
                elif kind=='voice_note':
                    content={'@type':'inputMessageVoiceNote','voice_note':{'@type':'inputVoiceNote','voice_note':local,
                        'duration':duration_seconds,'waveform':waveform_base64},'caption':formatted,'self_destruct_type':None}
                    expected='messageVoiceNote'
                else:raise ValueError('unsupported reply kind')
                return self._send_message_content(chat_id,content,expected,attempt_id=attempt_id,
                    reply_anchor=reply_source.anchor,artifact_reply=True,pre_transport_guard=pre_send_guard,pre_transport_fence=pre_send_fence)
        except (MessageSendOutcomeUnknown,MessageSendFailed):
            raise
        except Exception as error:
            if self._request_context.artifact_transport_attempted:
                raise MessageSendOutcomeUnknown("artifact reply observation was lost") from error
            self._send_observations.discard_unattempted(attempt_id)
            raise MessageSendNotAttempted('artifact reply was refused before transport') from error

    def send_photo_message(self, chat_id: int, path: Path, caption: str, *, attempt_id: str | None = None, pre_send_fence=None) -> int:
        content = {
            "@type": "inputMessagePhoto",
            "photo": {
                "@type": "inputPhoto", "photo": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None, "video": None, "added_sticker_file_ids": [], "width": 0, "height": 0,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "show_caption_above_media": False, "self_destruct_type": None, "has_spoiler": False,
        }
        return self._send_message_content(chat_id, content, "messagePhoto", attempt_id=attempt_id, pre_transport_fence=pre_send_fence)

    def send_voice_note_message(self, chat_id: int, path: Path, caption: str,
                                duration_seconds: int, waveform_base64: str, *, attempt_id: str | None = None, pre_send_fence=None) -> int:
        content = {
            "@type": "inputMessageVoiceNote",
            "voice_note": {
                "@type": "inputVoiceNote",
                "voice_note": {"@type": "inputFileLocal", "path": str(path)},
                "duration": duration_seconds, "waveform": waveform_base64,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "self_destruct_type": None,
        }
        return self._send_message_content(chat_id, content, "messageVoiceNote", attempt_id=attempt_id, pre_transport_fence=pre_send_fence)

    @staticmethod
    def _video_content(path: Path, caption: str, *, duration_seconds: int, width: int, height: int,
                       supports_streaming: bool) -> dict[str, Any]:
        return {
            "@type": "inputMessageVideo",
            "video": {
                "@type": "inputVideo", "video": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None, "cover": None, "start_timestamp": 0, "added_sticker_file_ids": [],
                "duration": duration_seconds, "width": width, "height": height,
                "supports_streaming": supports_streaming,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "show_caption_above_media": False, "self_destruct_type": None, "has_spoiler": False,
        }

    def send_video_message(self, chat_id: int, path: Path, caption: str, *, duration_seconds: int, width: int,
                           height: int, supports_streaming: bool, size_bytes: int, expected_account_id: int,
                           expected_recipient_title: str, expected_recipient_identity: tuple, attempt_id: str,
                           pre_send_guard: Callable[[], bool], pre_send_fence=None) -> int:
        """Revalidate account and recipient under the provider lock, then make one native video attempt.

        Every refusal before the raw send is no-attempt evidence; nothing is ever retried.
        """
        from .forward_sources import chat_identity
        self._request_context.artifact_transport_attempted = False
        try:
            with self._serialized_reply_call():
                def current_authority():
                    if (type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                            type(expected_recipient_title) is not str or type(expected_recipient_identity) is not tuple or
                            type(attempt_id) is not str or not attempt_id or not callable(pre_send_guard) or
                            pre_send_guard() is not True or self.get_account_id() != expected_account_id):
                        return False
                    identity, title = chat_identity(self.resolve_target(chat_id), chat_id)
                    return (identity == expected_recipient_identity and title == expected_recipient_title and
                            self.get_account_id() == expected_account_id and pre_send_guard() is True)
                if not current_authority():
                    raise ValueError("video authority changed")
                content = self._video_content(path, caption, duration_seconds=duration_seconds, width=width,
                                              height=height, supports_streaming=supports_streaming)
                return self._send_message_content(chat_id, content, "messageVideo", attempt_id=attempt_id,
                    pre_transport_guard=pre_send_guard, pre_transport_fence=pre_send_fence,
                    video=(duration_seconds, width, height, size_bytes))
        except (MessageSendOutcomeUnknown, MessageSendFailed):
            raise
        except Exception as error:
            if getattr(self._request_context, "artifact_transport_attempted", False):
                raise MessageSendOutcomeUnknown("video send observation was lost") from error
            self._send_observations.discard_unattempted(attempt_id)
            raise MessageSendNotAttempted("video send was refused before transport") from error

    # A whole album may upload many files: wait up to 120 s, then the album status reconciles each slot.
    ALBUM_CONFIRMATION_SECONDS = 120.0

    @staticmethod
    def _album_contents(items) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """The closed content of every item, built exactly as the single photo, video and document sends build it."""
        if type(items) is not tuple or not 2 <= len(items) <= 10:
            raise ValueError("album items are unavailable")
        contents, types = [], []
        for item in items:
            if type(item) is not tuple or len(item) != 4:
                raise ValueError("album item is unavailable")
            kind, path, caption, video = item
            local = {"@type": "inputFileLocal", "path": str(path)}
            formatted = {"@type": "formattedText", "text": caption, "entities": []}
            if kind == "document" and video is None:
                contents.append({"@type": "inputMessageDocument", "document": {"@type": "inputDocument",
                    "document": local, "thumbnail": None, "disable_content_type_detection": True},
                    "caption": formatted})
            elif kind == "photo" and video is None:
                contents.append({"@type": "inputMessagePhoto", "photo": {"@type": "inputPhoto", "photo": local,
                    "thumbnail": None, "video": None, "added_sticker_file_ids": [], "width": 0, "height": 0},
                    "caption": formatted, "show_caption_above_media": False, "self_destruct_type": None,
                    "has_spoiler": False})
            elif kind == "video" and type(video) is tuple and len(video) == 4:
                duration, width, height, streaming = video
                contents.append(TDLibClient._video_content(path, caption, duration_seconds=duration, width=width,
                                                           height=height, supports_streaming=streaming))
            else:
                raise ValueError("album item kind is unavailable")  # Voice notes and audio are never album items.
            types.append({"document": "messageDocument", "photo": "messagePhoto", "video": "messageVideo"}[kind])
        return contents, tuple(types)

    def send_album_message(self, chat_id: int, items, *, expected_account_id: int, expected_recipient_title: str,
                           expected_recipient_identity: tuple, attempt_id: str, pre_send_guard: Callable[[], bool],
                           pre_send_fence=None) -> tuple[tuple[str, int | None], ...]:
        """Revalidate account and recipient under the provider lock, then make exactly one sendMessageAlbum attempt.

        `items` holds (kind, staged path, plain caption, video facts or None) per item in send order. Every refusal
        before the raw send is no-attempt evidence (MessageSendNotAttempted); after it the result is one
        (sent|failed|outcome_unknown, final message ID) per item from exact evidence only, and nothing is ever
        retried: an item without exact evidence stays outcome_unknown."""
        from .forward_sources import chat_identity
        self._request_context.artifact_transport_attempted = False
        count = len(items) if type(items) is tuple else 0
        try:
            with self._serialized_reply_call():
                def current_authority():
                    if (type(expected_account_id) is not int or not 0 < expected_account_id < 2**53 or
                            type(expected_recipient_title) is not str or type(expected_recipient_identity) is not tuple or
                            type(attempt_id) is not str or not attempt_id or not callable(pre_send_guard) or
                            pre_send_guard() is not True or self.get_account_id() != expected_account_id):
                        return False
                    identity, title = chat_identity(self.resolve_target(chat_id), chat_id)
                    return (identity == expected_recipient_identity and title == expected_recipient_title and
                            self.get_account_id() == expected_account_id and pre_send_guard() is True)
                if not current_authority():
                    raise ValueError("album authority changed")
                contents, types = self._album_contents(items)
                return self._album_transport(chat_id, contents, types, attempt_id=attempt_id,
                                             pre_transport_guard=pre_send_guard, pre_transport_fence=pre_send_fence)
        except Exception as error:
            if getattr(self._request_context, "artifact_transport_attempted", False):
                return album_outcomes(self._send_observations.snapshot(attempt_id), count)
            self._send_observations.discard_unattempted(attempt_id)
            if isinstance(error, MessageSendNotAttempted):
                raise
            raise MessageSendNotAttempted("album send was refused before transport") from error

    def _album_transport(self, chat_id: int, contents: list[dict[str, Any]], content_types: tuple[str, ...], *,
                         attempt_id: str, pre_transport_guard: Callable[[], bool],
                         pre_transport_fence=None) -> tuple[tuple[str, int | None], ...]:
        sending_id = secrets.randbelow((1 << 31) - 1) + 1
        for _ in range(8):
            if self._send_observations.sending_id_available(sending_id):
                break
            sending_id = secrets.randbelow((1 << 31) - 1) + 1
        else:
            raise MessageSendNotAttempted("send correlation is unavailable")
        payload = {"@type": "sendMessageAlbum", "chat_id": chat_id, "topic_id": None, "reply_to": None,
                   "options": {**SEND_OPTIONS, "sending_id": sending_id}, "input_message_contents": contents}
        try:
            _validate_bounded_request(payload, self._staging_root)
        except ForbiddenTDLibRequest as error:
            raise MessageSendNotAttempted("album request shape is not allowed") from error
        extra = f"telegram-search-mcp-{next(self._ids)}"
        aggregate_deadline = getattr(self._request_context, "deadline", None)
        if not self._ready or (aggregate_deadline is not None and time.monotonic() >= aggregate_deadline):
            raise MessageSendNotAttempted("album provider is unavailable before send")
        try:
            self._send_observations.register_album(attempt_id, extra, sending_id, chat_id, content_types)
        except ObservationUnavailable as error:
            raise MessageSendNotAttempted("send correlation is unavailable") from error
        try:
            if pre_transport_guard() is not True:
                raise ValueError("album scope changed")
            if aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                raise ValueError("album request deadline expired")
        except Exception as error:
            raise MessageSendNotAttempted("album scope changed before transport") from error
        # The one attempt. Once the raw send is invoked the outcome comes only from exact evidence.
        with pre_transport_fence() if pre_transport_fence is not None else nullcontext():
            self._request_context.artifact_transport_attempted = True
            self._raw.send({**payload, "@extra": extra})
        deadline = time.monotonic() + self.ALBUM_CONFIRMATION_SECONDS
        if aggregate_deadline is not None:
            deadline = min(deadline, aggregate_deadline)
        while time.monotonic() < deadline:
            try:
                response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
            except Exception:
                break  # Confirmation transport lost: whatever is exact so far stands, the rest stays unknown.
            if response is None:
                continue
            self._reduce_receive_event(response)
            if not self._ready:
                break
            observation = self._send_observations.snapshot(attempt_id)
            if (observation is None or observation.status in ("failed", "conflict") or
                    (observation.status == "bound" and all(slot.status != "pending" for slot in observation.slots))):
                break
        return album_outcomes(self._send_observations.snapshot(attempt_id), len(content_types))

    def _send_message_content(
        self, chat_id: int, content: dict[str, Any], expected_type: str,
        *, attempt_id: str | None = None, reply_anchor: tuple[int,int] | None = None, artifact_reply: bool = False, pre_transport_guard: Callable[[],bool] | None = None,
        forum_topic_id: int | None = None, outgoing_entities: FrozenOutgoingEntities = (),
        saved_messages_topic_id: int | None = None, pre_transport_fence=None,
        video: tuple[int, int, int, int] | None = None,
    ) -> int:
        sending_id = secrets.randbelow((1 << 31) - 1) + 1
        for _ in range(8):
            if self._send_observations.sending_id_available(sending_id):
                break
            sending_id = secrets.randbelow((1 << 31) - 1) + 1
        else:
            raise MessageSendNotAttempted("send correlation is unavailable")
        payload = {
            "@type": "sendMessage", "chat_id": chat_id,
            "topic_id": ({'@type': 'messageTopicForum', 'forum_topic_id': forum_topic_id}
                         if forum_topic_id is not None else None),
            "reply_to": ({"@type":"inputMessageReplyToMessage","message_id":reply_anchor[1],
                "quote":None,"checklist_task_id":0,"poll_option_id":""} if reply_anchor is not None else None), "reply_markup": None,
            "options": {**SEND_OPTIONS,"sending_id":sending_id},
            "input_message_content": content,
        }
        _validate_bounded_request(payload, self._staging_root)
        with self._serialized_provider_call():
            if not self._ready:
                raise AuthorizationBlocked("the dedicated TDLib session is not authorizationStateReady")
            extra = f"telegram-search-mcp-{next(self._ids)}"
            attempt_id = attempt_id if attempt_id is not None else extra
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            bounded = reply_anchor is not None or forum_topic_id is not None or bool(outgoing_entities) or video is not None
            if bounded and aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                raise MessageSendNotAttempted("reply request deadline expired before send")
            try:
                self._send_observations.register(attempt_id, extra, sending_id, chat_id, expected_type,
                    text_sha256=hashlib.sha256(content["text"]["text"].encode("utf-8")).hexdigest()
                    if expected_type == "messageText" else None, reply_anchor=reply_anchor,
                    **({'forum_topic_id': forum_topic_id} if forum_topic_id is not None else {}),
                    **({'outgoing_entities': outgoing_entities} if outgoing_entities else {}),
                    **({'saved_messages_topic_id': saved_messages_topic_id} if saved_messages_topic_id is not None else {}),
                    **({'video': video} if video is not None else {}),
                    **({'caption_sha256':hashlib.sha256(content['caption']['text'].encode('utf-8')).hexdigest(),
                        'voice_duration':content['voice_note']['duration'] if expected_type=='messageVoiceNote' else None,
                        'waveform_sha256':hashlib.sha256(base64.b64decode(content['voice_note']['waveform'],validate=True)).hexdigest() if expected_type=='messageVoiceNote' else None,
                        'waveform_size':len(base64.b64decode(content['voice_note']['waveform'],validate=True)) if expected_type=='messageVoiceNote' else None} if artifact_reply else {}))
            except ObservationUnavailable as error:
                raise MessageSendNotAttempted("send correlation is unavailable") from error
            if pre_transport_guard is not None:
                try:
                    if pre_transport_guard() is not True:
                        raise ValueError('reply scope changed')
                except Exception as error:
                    self._send_observations.discard_unattempted(attempt_id)
                    raise MessageSendNotAttempted("reply scope changed before transport") from error
            if bounded and aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                self._send_observations.discard_unattempted(attempt_id)
                raise MessageSendNotAttempted("reply request deadline expired before transport")
            try:
                # Pure lifecycle/epoch fence brackets raw dispatch only. Account
                # acquisition occurs on context entry before its mutex is held.
                with pre_transport_fence() if pre_transport_fence is not None else nullcontext():
                    if artifact_reply or video is not None:self._request_context.artifact_transport_attempted=True
                    self._raw.send({**payload, "@extra": extra})
            except MessageSendNotAttempted:
                self._send_observations.discard_unattempted(attempt_id)
                raise
            except Exception as error:
                raise MessageSendOutcomeUnknown("TDLib send transport failed") from error
            # A native video upload takes longer than text: wait up to 120 s, then reconcile by status.
            deadline = time.monotonic() + (120 if video is not None else min(max(self._request_timeout, 30), 300))
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            if aggregate_deadline is not None:
                deadline = min(deadline, aggregate_deadline)
            while time.monotonic() < deadline:
                try:
                    response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
                except Exception as error:
                    raise MessageSendOutcomeUnknown("TDLib confirmation transport was lost") from error
                if response is None:
                    continue
                self._reduce_receive_event(response)
                if not self._ready:
                    raise MessageSendOutcomeUnknown("authorization changed before confirmation")
                observation = self._send_observations.snapshot(attempt_id)
                if observation is not None and observation.status == "failed":
                    provider_error = response if response.get("@type") == "error" else response.get("error")
                    raise MessageSendFailed("TDLib reported send failure", provider_error=provider_error)
                if observation is not None and observation.status == "sent":
                    return observation.message_id
                if (observation is not None and response.get("@type") == "updateMessageSendSucceeded"
                        and type(response.get("old_message_id")) is int
                        and response["old_message_id"] == observation.temporary_id):
                    raise MessageSendOutcomeUnknown("TDLib returned an invalid final message")
                if response.get("@extra") == extra and observation is not None and observation.status != "pending":
                    raise MessageSendOutcomeUnknown("TDLib returned an invalid preliminary message")
            raise MessageSendOutcomeUnknown("TDLib delivery confirmation timed out")

    def download_file(
        self, file_id: int, *, max_bytes: int = 64 * 1024 * 1024,
        timeout: float = 300.0, poll_seconds: float = 0.2
    ) -> Path:
        """Download one internally selected file without holding the provider lock while waiting."""
        if not _is_strict_integer(file_id) or not 0 < file_id <= (1 << 31) - 1:
            raise ValueError("file_id is invalid")
        if not 0 <= timeout <= 540 or not 0 <= poll_seconds <= 2:
            raise ValueError("download timing is invalid")
        if max_bytes not in {64 * 1024 * 1024, 256 * 1024 * 1024}:
            raise ValueError("download size limit is invalid")
        deadline = time.monotonic() + timeout
        if not self._download_lock.acquire(timeout=timeout):
            raise TimeoutError("Telegram file download queue expired")
        completed = False
        started = False
        try:
            state = self._call({
                "@type": "downloadFile", "file_id": file_id, "priority": 1,
                "offset": 0, "limit": max_bytes + 1, "synchronous": False,
            })
            started = True
            while True:
                if state.get("@type") != "file" or state.get("id") != file_id:
                    raise TDLibError("TDLib returned an invalid file")
                local = state.get("local")
                if not isinstance(local, dict):
                    raise TDLibError("TDLib returned an invalid local file state")
                if any(type(value) is int and value > max_bytes for value in (
                    state.get("size"), state.get("expected_size"), local.get("downloaded_size"),
                )):
                    raise DownloadTooLarge("Telegram file exceeds the transfer limit")
                if local.get("is_downloading_completed") is True:
                    path = local.get("path")
                    if not isinstance(path, str) or not path or not Path(path).is_absolute():
                        raise TDLibError("TDLib returned an invalid completed path")
                    completed = True
                    return Path(path)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Telegram file download deadline expired")
                if poll_seconds:
                    time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
                state = self._call({"@type": "getFile", "file_id": file_id})
        finally:
            if started and not completed:
                try:
                    self._call({
                        "@type": "cancelDownloadFile", "file_id": file_id,
                        "only_if_pending": False,
                    })
                except TDLibError:
                    pass
            self._download_lock.release()

    def get_context_messages(
        self, chat_id: int, message_id: int, radius: int
    ) -> list[dict[str, Any]]:
        if not 1 <= radius <= 4:
            return []
        response = self._call(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": message_id,
                "offset": -radius,
                "limit": radius * 2 + 1,
                "only_local": False,
            }
        )
        messages = response.get("messages")
        if response.get("@type") != "messages" or not isinstance(messages, list):
            raise TDLibError("TDLib returned invalid message context")
        return [item for item in messages if isinstance(item, dict)]

    def get_message_link(self, chat_id: int, message_id: int) -> str | None:
        try:
            response = self._call(
                {
                    "@type": "getMessageLink",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media_timestamp": 0,
                    "checklist_task_id": 0,
                    "poll_option_id": "",
                    "for_album": False,
                    "in_message_thread": False,
                }
            )
        except AuthorizationBlocked:
            raise
        except TDLibError:
            return None
        link = response.get("link")
        return link if response.get("@type") == "messageLink" and isinstance(link, str) else None

    def get_chat_link(self, chat: dict[str, Any]) -> str | None:
        # TDLib does not return a canonical chat URL for this boundary. Do not
        # derive one from usernames; optional URL fields remain empty unless a
        # provider operation returns the complete link directly.
        del chat
        return None

    def get_sender_identity(self, sender: dict[str, Any]) -> dict[str, Any]:
        """Hydrate exactly one sender, verifying the provider's returned identity."""
        kind = sender.get("@type")
        if kind == "messageSenderUser":
            identifier = sender.get("user_id")
            if not _is_strict_integer(identifier) or not 0 < identifier < 2**53:
                raise TDLibError("invalid sender identity")
            user = self._call({"@type": "getUser", "user_id": identifier})
            if user.get("@type") != "user" or type(user.get("id")) is not int or user["id"] != identifier:
                raise TDLibError("invalid sender identity")
            first, last = user.get("first_name"), user.get("last_name")
            if not isinstance(first, str) or not isinstance(last, str):
                raise TDLibError("invalid sender name")
            return {"kind": "user", "id": identifier, "display_name": " ".join(p for p in (first, last) if p)}
        if kind == "messageSenderChat":
            identifier = sender.get("chat_id")
            if not _is_strict_nonzero_integer(identifier) or not -(2**53) < identifier < 2**53:
                raise TDLibError("invalid sender identity")
            chat = self._call({"@type": "getChat", "chat_id": identifier})
            if (chat.get("@type") != "chat" or type(chat.get("id")) is not int or
                    chat["id"] != identifier or not isinstance(chat.get("title"), str)):
                raise TDLibError("invalid sender identity")
            return {"kind": "chat", "id": identifier, "display_name": chat["title"]}
        raise TDLibError("unsupported sender identity")

    def get_sender_name(self, message: dict[str, Any]) -> str:
        sender = message.get("sender_id") or {}
        sender_type = sender.get("@type")
        if sender_type == "messageSenderUser" and isinstance(sender.get("user_id"), int):
            user = self._call({"@type": "getUser", "user_id": sender["user_id"]})
            pieces = [str(user.get("first_name") or ""), str(user.get("last_name") or "")]
            name = " ".join(piece for piece in pieces if piece).strip()
            usernames = (user.get("usernames") or {}).get("active_usernames") or []
            username = usernames[0] if usernames and isinstance(usernames[0], str) else None
            if username:
                return f"{name} (@{username})" if name else f"@{username}"
            return name or f"user:{sender['user_id']}"
        if sender_type == "messageSenderChat" and isinstance(sender.get("chat_id"), int):
            chat = self._call({"@type": "getChat", "chat_id": sender["chat_id"]})
            title = chat.get("title")
            return str(title) if title else f"chat:{sender['chat_id']}"
        return "unknown sender"

    def close(self) -> None:
        self._invalidate_send_observations()
        self._ready = False
        self._raw.close()
