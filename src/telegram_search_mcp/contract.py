"""Loaded-code identity and a closed mapping of capabilities to operations."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Literal

from . import __version__
from .config import KNOWN_CAPABILITIES, RuntimePolicy, ConfigurationError

CONTRACT_VERSION = 1
OPERATION_CAPABILITIES = {
    **{name: 'artifacts' for name in ('issue_local_upload_authority', 'consume_local_upload_result',
                                    'cancel_local_upload_authority')},
    "retain_selection": "retained_selections",
    **{name: "forward_send" for name in ("prepare_forward_send", "send_prepared_forward")},
    **{name: "video_send" for name in ("prepare_video_send", "send_prepared_video")},
    # 0.56: an album with a video item also needs video_send; that per-draft conjunction is the broker's.
    **{name: "album_send" for name in ("prepare_album_send", "send_prepared_album", "get_album_send_status")},
    **{name: "archive_chats" for name in ("prepare_chat_archive", "apply_prepared_archive")},
    **{name: "edit_own_text" for name in ("prepare_text_edit", "apply_prepared_text_edit")},
    **{name: "reply_artifact_send" for name in ("prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft")},
    **{name: "reply_text_send" for name in ("prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft")},
    "read_presentation": "presentations",
    "read_spreadsheet": "spreadsheets",
    "read_attachment_page": "attachment_pages",
    "verify_target": "verified_targets",
    "read_target_messages": "verified_targets",
    "search_chats": "search_chats",
    "list_chats": "list_chats",
    "search_messages": "search_messages",
    "read_topic_history": "read_topic_history",
    "list_topics": "list_topics",
    "read_reply_chain": "read_reply_chain",
    "read_history": "read_history",
    "read_messages": "read_messages",
    "resolve": "read", "discover": "read", "search": "read", "get_message_context": "read",
    "get_attachment": "artifacts", "read_attachment": "artifacts", "analyze_media": "artifacts",
    "create_local_artifact": "artifacts", "begin_local_upload": "artifacts",
    "append_local_upload": "artifacts", "finish_local_upload": "artifacts",
    "get_send_status": "send", "list_drafts": "send", "get_draft": "send", "cancel_draft": "send",
    "update_draft": "send", "refresh_draft": "send",
    "prepare_text_send": "send", "send_prepared_text": "send",
    "prepare_artifact_send": "send", "send_prepared_artifact": "send",
}
# Default-off capabilities that change Telegram state; each also requires `send`.
# The 0.57 own-message actions (edit_own_caption, edit_own_styled_text, pin_messages, delete_own_message) too.
OWN_MESSAGE_ACTIONS = frozenset({"edit_own_caption", "edit_own_styled_text", "pin_messages", "delete_own_message"})
SEND_DEPENDENT_CAPABILITIES = frozenset({"reply_text_send", "reply_artifact_send", "forward_send", "video_send",
                                         "archive_chats", "edit_own_text", "album_send"}) | OWN_MESSAGE_ACTIONS
# Their prepare step reveals content (an own message's text, a chat's title and list); owner decision 20.10.2026.
# 0.57: an own message's caption and text, or a message to pin, are revealed the same way.
READ_DEPENDENT_CAPABILITIES = frozenset({"archive_chats", "edit_own_text"}) | OWN_MESSAGE_ACTIONS


def required_capabilities(capability: str) -> tuple[str, ...]:
    """The capability, then `send` and `read` when it depends on them, in that order."""
    return ((capability,) + (("send",) if capability in SEND_DEPENDENT_CAPABILITIES else ())
            + (("read",) if capability in READ_DEPENDENT_CAPABILITIES else ()))
TOOL_OPERATIONS = {"resolve_target": "resolve", "discover_targets": "discover", "search_correspondence": "search",
                   **{name: name for name in OPERATION_CAPABILITIES if name not in {"resolve", "discover", "search"}}}
# Supplier transport is capability-checked but deliberately absent from MCP tools.
HELPER_OPERATION_CAPABILITIES = {name: 'artifacts' for name in (
    'redeem_local_upload_authority', 'append_authorized_local_upload', 'finish_authorized_local_upload')}
OPERATION_CAPABILITIES.update(HELPER_OPERATION_CAPABILITIES)


class CompatibilityError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# The closed capability set as a schema type, sorted so the published schema is stable.
CapabilityName = Literal[tuple(sorted(KNOWN_CAPABILITIES))]


def missing_capabilities(policy: RuntimePolicy, *required: str) -> tuple[str, ...]:
    """Only the missing names, once each, in the order they are required (the guard's rule)."""
    return tuple(dict.fromkeys(name for name in required if name not in policy.enabled_capabilities))


class CapabilityDisabled(CompatibilityError):
    """`capability_disabled` carrying the missing names: closed, ordered, never derived from input."""
    def __init__(self, missing: tuple[str, ...]):
        if (type(missing) is not tuple or not missing or len(set(missing)) != len(missing)
                or not set(missing) <= KNOWN_CAPABILITIES):
            raise ValueError("missing capabilities must be distinct known names")
        self.missing = missing
        super().__init__("capability_disabled")


COMPATIBILITY_CODES = frozenset({"package_mismatch", "contract_mismatch", "schema_mismatch", "config_mismatch",
    "config_stale", "generation_stale", "capability_disabled", "handshake_required", "handshake_invalid"})


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def schema_document(server) -> dict[str, object]:
    tools = server._tool_manager.list_tools()
    return {"contract_version": CONTRACT_VERSION,
            "operations": OPERATION_CAPABILITIES, "tool_operations": TOOL_OPERATIONS,
            "tools": [{"name": t.name, "inputSchema": t.parameters, "outputSchema": t.output_schema,
                       "annotations": t.annotations.model_dump(mode="json", exclude_none=True) if t.annotations else None}
                      for t in sorted(tools, key=lambda t: t.name)]}


@lru_cache(maxsize=1)
def schema_fingerprint() -> str:
    # Use the finalized SDK schemas, not a second hand-maintained schema catalogue.
    # Building the registration performs no broker, provider, or policy I/O.
    from .server import build_server
    return fingerprint(schema_document(build_server(policy=RuntimePolicy())))


def contract_descriptor(policy: RuntimePolicy) -> dict[str, object]:
    descriptor = {"package_version": __version__, "contract_version": CONTRACT_VERSION,
                  "schema_fingerprint": schema_fingerprint(), "config_version": policy.config_version,
                  "config_fingerprint": fingerprint(policy.public_settings()),
                  "enabled_capabilities": list(policy.enabled_capabilities)}
    for name in ("package_version", "contract_version", "schema_fingerprint"):
        expected = getattr(policy, "expected_" + name)
        if expected is not None and expected != descriptor[name]:
            raise CompatibilityError("config_stale")
    return descriptor


def require_current(policy: RuntimePolicy) -> None:
    try:
        policy.require_current()
    except ConfigurationError:
        raise CompatibilityError("config_stale") from None


def require_compatible(remote: object, local: dict[str, object], *, broker: bool = False) -> None:
    keys = set(local) | ({"broker_generation"} if broker else set())
    if not isinstance(remote, dict) or set(remote) != keys:
        raise CompatibilityError("handshake_invalid")
    for name in local:
        if type(remote[name]) is not type(local[name]):
            raise CompatibilityError("handshake_invalid")
    for name, code in (("package_version", "package_mismatch"), ("contract_version", "contract_mismatch"),
                       ("schema_fingerprint", "schema_mismatch"), ("config_version", "config_mismatch"),
                       ("config_fingerprint", "config_mismatch"), ("enabled_capabilities", "config_mismatch")):
        if remote[name] != local[name]:
            raise CompatibilityError(code)
    if broker:
        generation = remote["broker_generation"]
        if (type(generation) is not str or len(generation) != 39 or not generation.startswith("broker_")
                or any(c not in "0123456789abcdef" for c in generation[7:])):
            raise CompatibilityError("handshake_invalid")
