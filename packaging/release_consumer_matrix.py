#!/usr/bin/env python3
"""Consumer-matrix harness: every synthetic cell through the official SDK and the MCP Inspector CLI against an
installed proxy, and the installed bundle installer's change paths.

  python packaging/release_consumer_matrix.py run --commit <sha> --receipt <path> [--wheelhouse <dir>]
      [--toolhouse <dir>] [--node <path> --inspector-cache <dir>] [--workdir <dir>] [--real-dialog]
  python packaging/release_consumer_matrix.py check --receipt <path> [--commit <sha>] [--wheelhouse <dir>]
  python packaging/release_consumer_matrix.py inspector-fetch --node <path> --cache <dir>

Spec: docs/plans/final-consumer-matrix-spec.md (final acceptance F18; it extends the 0.54 matrix spec).
acceptance is always false: the harness runs only synthetic cells and never marks an owner, agent or
real-account cell passed.

run builds the sdist and wheel from `git archive <commit>` with the running interpreter's build backend
(packaging/release_gate.py helpers, SOURCE_DATE_EPOCH = commit time), installs the wheel with the commit's
pinned, hash-checked dependency closure in a fresh venv (pip downloads it into the workdir unless
--wheelhouse names an existing one), takes the source census of the extracted tree and refuses a schema the
cells were not written for (MATRIX_SCHEMA_FINGERPRINT), then drives every synthetic cell with the installed
official SDK against the installed `telegram-search-mcp` proxy. Each session has its own short owner-only HOME
under /private/tmp (a Unix socket path holds at most 104 bytes) and, where the cell needs one, a synthetic
broker: the installed Broker class over a fake raw tdjson transport serving one fixed synthetic account. The
bundle session runs the installed bundle_install module over a synthetic home with a fake launchctl and the
release gate's pinned tomlkit (from --toolhouse, else a hash-checked download), as the bundle's launcher
provides it. The Inspector column runs the pinned Inspector CLI (packaging/inspector, its whole npm tree
hash-pinned) installed offline from a cache that `inspector-fetch` filled; without --node or the cache, or
when it cannot install offline, the receipt records it unavailable with one fixed reason. Nothing reaches
Telegram, the Keychain, launchd state, a client configuration or the live runtime tree. Approval denial is
scripted by default (the bound dialog document is rendered, then declined; the timeout probe's dialog times
out); --real-dialog shows the real JXA dialog with a 5-second timer and expects `timeout`. The receipt is bound
to the commit, the wheel sha256 and the schema fingerprint. run refuses when this file or
packaging/release_gate.py (which builds, installs and takes the census) differs from the commit's copy, and
records both hashes.

check re-derives the bindings with the receipt commit's own harness and gate (a working-tree copy that
differs refuses): it rebuilds the wheel from `git archive <receipt commit>`, takes the census of that tree
with the running interpreter, reinstalls the pinned closure (downloaded unless --wheelhouse names one) and
lists the tools through the reinstalled proxy. Any difference refuses: another wheel or size, schema, tool
set, closure, install facts or listing, or a field check_receipt and check_fields name. Per-cell check
counts and surface counts are frozen in this file (CELL_CHECKS, INSPECTOR_CHECKS, SURFACE_COUNTS), so a
weakened cell must change them too. Refusals print one fixed code and never echo inputs.

The `broker` and `drive` subcommands are internal: run starts them inside the temporary venv.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

SCHEMA = "tgmcp-consumer-matrix/v3"
PROJECT = "telegram-search-mcp"
HARNESS = Path(__file__).resolve()
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# The schema fingerprint these cells were written for (the 0.58 surface, 64 tools). run refuses another census and
# check another receipt (schema_mismatch): a deliberate schema change must update the cells, their counts and this.
MATRIX_SCHEMA_FINGERPRINT = "6a0b0971d74d988c60399ed4019db7d8ec97c9997ea5713d9074e9713b1bfe7d"
# Every revision of the official SDK this harness drives: the four initialize-handshake revisions and the
# per-request (discover) revision. A newer SDK with another revision fails `sdk_revisions` until listed.
LEGACY_REVISIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_REVISIONS = ("2026-07-28",)
REVISIONS = LEGACY_REVISIONS + MODERN_REVISIONS
SDK_DISTRIBUTION = "mcp"

# The closed synthetic cell set, in receipt order: (id, session, what it proves).
CELLS = (
    *((f"handshake.{revision}", "full", f"initialize at {revision}: negotiated revision, server identity, census, "
       "compatible broker") for revision in LEGACY_REVISIONS),
    *((f"handshake.{revision}", "full", f"server/discover at {revision}: negotiated revision, census, compatible "
       "broker") for revision in MODERN_REVISIONS),
    ("census.tools", "full", "tools/list names equal the source census; every tool has a title"),
    ("census.fingerprint", "full", "_manifest fingerprint, version, capabilities and tools equal the source census"),
    ("census.prompts", "full", "prompts/list is the closed prompt set; each renders and quotes the request"),
    ("output_schemas", "all", "every structured result of every session validates against its listed outputSchema; "
     "every tool of the census returned at least one"),
    ("tools.every_tool", "full", "every tool of the census answers its probe with its expected structured status"),
    ("refusal.invalid_arguments", "full", "schema violations return one fixed input-free text"),
    ("refusal.invalid_arguments_hook", "full", "a strict pre-parse hook refusal returns the same fixed text"),
    ("refusal.invalid_arguments_every_tool", "full", "every tool names exactly its listed required parameters and "
     "counts an undeclared key"),
    ("refusal.unknown_tool", "full", "an unknown tool returns the fixed unknown_tool text"),
    ("refusal.prompt_unavailable", "full", "an unknown prompt is refused with prompt_unavailable"),
    ("refusal.prompt_arguments", "full", "an unknown prompt argument is refused with invalid_arguments"),
    ("refusal.client_schema_mismatch", "full", "a consumer fingerprint mismatch blocks tools until a matching recheck"),
    ("refusal.capability_disabled", "restricted", "a disabled tool is unlisted and refused naming the capability "
     "and next_action"),
    ("refusal.guard_profiles", "restricted", "every tool the restricted policy disables is unlisted and refused naming "
     "exactly its missing capabilities and the profile that unlocks them"),
    ("refusal.reply_families", "restricted", "a reply to a source of each 0.58 reply family, or with styles, returns "
     "capability_disabled with exactly its missing_capabilities"),
    ("refusal.guard_every_tool", "closed", "with no capability every tool but _manifest is unlisted and refused with "
     "its full requirement and hint; the upload-authority tools answer unavailable"),
    ("refusal.prompt_policy", "restricted", "a prompt needing a disabled tool is unlisted and refused"),
    ("refusal.config_stale", "stale", "a stale pin lists only _manifest and refuses tools with config_stale"),
    ("image.png", "full", "a PNG artifact returns its exact bytes as one image/png block"),
    ("image.pdf_page", "full", "a PDF artifact returns its rendered page as an image/png block"),
    ("image.attachment_page", "full", "read_attachment_page renders a selected PDF page as an image/png block"),
    ("cursor.list_chats", "full", "list_chats pages at its cap and follows its cursor to the end"),
    ("cursor.read_history", "full", "read_history pages one message at a time and follows its cursor to the end"),
    ("cursor.search_messages", "full", "search_messages pages one match at a time and follows its cursor to the end"),
    ("cursor.list_drafts", "full", "list_drafts pages owned drafts with after_draft_id"),
    ("recovery.restart_listing", "full", "a replayed cursor returns invalid_cursor with next_action restart_listing"),
    ("recovery.search_again", "full", "an unknown selection returns selection_unavailable with next_action "
     "search_again"),
    ("recovery.check_send_status", "full", "an approved send whose transport fails returns outcome_unknown with "
     "next_action check_send_status, once"),
    ("recovery.enable_capability", "restricted", "a reply source needing disabled families returns "
     "capability_disabled naming exactly them"),
    ("recovery.update_installation", "stale", "_manifest reports config_stale with next_action update_installation"),
    ("recovery.check_connection", "offline", "without a broker, _manifest and list_chats return next_action "
     "check_connection"),
    ("recovery.connect_account", "unauthorized", "an unauthorized account returns blocked with next_action "
     "connect_account"),
    ("caps.manifest", "full", "_manifest declares the documented page sizes, scan and selection caps"),
    ("caps.input_bounds", "full", "inputs one past each cap are refused with the fixed invalid_arguments text"),
    ("caps.input_bounds_058", "full", "inputs one past each cap of the tools through 0.58 are refused with the fixed "
     "invalid_arguments text"),
    ("approval.declined_flag", "full", "approved=false declines without a dialog and without dispatch"),
    ("approval.denial", "full", "approved=true with a denying owner (scripted decline or real dialog timeout) never "
     "dispatches"),
    ("approval.timeout", "full", "approved=true whose owner dialog times out returns timeout and never dispatches"),
    ("approval.denial_every_family", "full", "every send and apply family renders its owner dialog, is declined and "
     "never dispatches"),
    ("bundle.install", "bundle", "the installed bundle installer's first install in a synthetic home registers the "
     "clients and the broker definition"),
    ("bundle.upgrade", "bundle", "an upgrade restarts the broker on the new release, moves the pin and keeps the "
     "session and the old release"),
    ("bundle.rollback", "bundle", "a rollback restores the previous release, broker definition and pin"),
    ("bundle.uninstall", "bundle", "an uninstall removes only owned objects and keeps the session"),
)
CELL_IDS = tuple(cell for cell, _, _ in CELLS)
# How many checks each cell makes when it passes, in CELLS order; check refuses any other count, so a cell
# weakened in the driver also has to be weakened here. The real dialog adds the approval timer check.
CELL_CHECKS = {
    **{f"handshake.{revision}": 6 for revision in REVISIONS},
    "census.tools": 4, "census.fingerprint": 7, "census.prompts": 12, "output_schemas": 3, "tools.every_tool": 64,
    "refusal.invalid_arguments": 10, "refusal.invalid_arguments_hook": 2, "refusal.invalid_arguments_every_tool": 65,
    "refusal.unknown_tool": 1, "refusal.prompt_unavailable": 1, "refusal.prompt_arguments": 2,
    "refusal.client_schema_mismatch": 4, "refusal.capability_disabled": 5, "refusal.guard_profiles": 33,
    "refusal.reply_families": 22, "refusal.guard_every_tool": 65, "refusal.prompt_policy": 2,
    "refusal.config_stale": 3, "image.png": 6, "image.pdf_page": 5, "image.attachment_page": 5,
    "cursor.list_chats": 4, "cursor.read_history": 6, "cursor.search_messages": 6, "cursor.list_drafts": 5,
    "recovery.restart_listing": 4, "recovery.search_again": 2, "recovery.check_send_status": 8,
    "recovery.enable_capability": 5, "recovery.update_installation": 3, "recovery.check_connection": 4,
    "recovery.connect_account": 3, "caps.manifest": 8, "caps.input_bounds": 6, "caps.input_bounds_058": 13,
    "approval.declined_flag": 5, "approval.denial": 8, "approval.timeout": 6, "approval.denial_every_family": 34,
    "bundle.install": 8, "bundle.upgrade": 9, "bundle.rollback": 7, "bundle.uninstall": 8,
}
REAL_DIALOG_CHECKS = {"approval.denial": 1}
# Structured results every session validates in a passing run, and the tools they came from (every tool).
SURFACE_COUNTS = {"structured_results_validated": 157, "tools_with_validated_results": 64}
# Owner or real-account cells: listed so the receipt shows them, never run or passed by this harness. The Inspector
# CLI's synthetic cells are the harness's own column (INSPECTOR_CELLS) since the final acceptance.
OWNER_CELLS = (
    ("official_sdk.real_account", "official SDK", "real account"),
    ("inspector_cli.real_account", "MCP Inspector CLI", "real account"),
    ("codex.synthetic", "Codex (codex exec)", "synthetic broker"),
    ("codex.real_account", "Codex (codex exec)", "real account"),
    ("claude_code.synthetic", "Claude Code (claude -p)", "synthetic broker"),
    ("claude_code.real_account", "Claude Code", "real account"),
    ("claude_desktop.real_account", "Claude Desktop", "real account"),
    ("plugin.real_account", "Claude Code plugin", "real account"),
    ("dialog.owner_clicks", "native approval dialog", "real account"),
)
OWNER_IDS = tuple(cell for cell, _, _ in OWNER_CELLS)
SESSIONS = ("full", "restricted", "stale", "offline", "unauthorized", "closed", "bundle")
# The restricted policy: send, text and artifact replies on; the readers, every family beyond send and every reply
# source family off.
RESTRICTED_CAPABILITIES = ("artifacts", "read", "reply_artifact_send", "reply_text_send", "send")
STALE_PIN = "0.0.0+matrix.stale"

# The MCP Inspector column: the Inspector's scripted CLI mode (the pinned inspector-cli package, its whole tree
# hash-pinned by packaging/inspector/package-lock.json and installed offline from a fetched npm cache) against an
# installed proxy and its own synthetic broker.
INSPECTOR_CELLS = (
    ("inspector.census", "tools/list through the Inspector CLI lists the census with the official SDK's schemas; "
     "_manifest is compatible with the census fingerprint"),
    ("inspector.read", "a list_chats call through the Inspector CLI returns its first page, valid under the listed "
     "outputSchema"),
    ("inspector.refusal", "an argument refusal through the Inspector CLI is the fixed input-free text"),
    ("inspector.approval_denial", "prepare then send through the Inspector's client in one session is declined by the "
     "owner without dispatch; a later CLI invocation cannot send that draft"),
)
INSPECTOR_IDS = tuple(cell for cell, _ in INSPECTOR_CELLS)
INSPECTOR_CHECKS = {"inspector.census": 5, "inspector.read": 5, "inspector.refusal": 2, "inspector.approval_denial": 8}
INSPECTOR_PACKAGE, INSPECTOR_VERSION = "@modelcontextprotocol/inspector-cli", "1.0.2"
INSPECTOR_INTEGRITY = "sha512-i8a+wWb6THQBb9Ie2+dlp4qGY0EzUe75heiTFmMSY4pUQczhtj3bgwYMCLjHLyFarrp3x1LoKmY0ug/+gUA6vQ=="
INSPECTOR_DIR = "packaging/inspector"
INSPECTOR_NODE_MAJOR = 22  # the oldest Node line this column is recorded on
# Why the column is not run, as the receipt records it.
INSPECTOR_UNAVAILABLE = frozenset({"node_not_given", "node_unsupported", "inspector_cache_missing",
                                   "inspector_lock_invalid", "inspector_install_failed"})
INSPECTOR_SECONDS = 120
# The bundle session's installer dependency: the bundle vendors tomlkit (installer/tomlkit) beside the app; the
# release gate pins the same wheel (TOOLS).
INSTALLER_DISTRIBUTION = "tomlkit"

CODES = frozenset({
    "usage", "repo_unavailable", "git_failed", "unknown_commit", "build_failed", "build_tools_missing",
    "install_failed", "census_failed", "broker_failed", "drive_failed", "cells_failed", "receipt_invalid",
    "acceptance_claimed", "owner_cell_passed", "cell_set_mismatch", "commit_mismatch", "wheel_mismatch",
    "schema_mismatch", "tools_mismatch", "version_mismatch", "workdir_invalid", "wheelhouse_invalid",
    "harness_mismatch", "gate_mismatch", "tree_mismatch", "field_mismatch", "checks_mismatch", "toolhouse_invalid",
    "installer_failed", "inspector_invalid", "cache_invalid", "node_invalid", "fetch_failed"})
RECEIPT_KEYS = frozenset({"schema", "acceptance", "harness", "gate", "commit", "tree", "version", "wheel", "build",
                          "install", "installer", "sdk", "schema_fingerprint", "census", "surface", "approval",
                          "cells", "inspector", "owner_cells", "summary"})
HARNESS_PATH, GATE_PATH = "packaging/release_consumer_matrix.py", "packaging/release_gate.py"
APPROVALS = ({"mode": "scripted", "outcome": "declined"}, {"mode": "real_dialog", "outcome": "timeout"})
MAX_RECEIPT_BYTES = 1 << 20
BROKER_START_SECONDS, DRIVE_SECONDS, CENSUS_SECONDS, BUILD_SECONDS = 30, 900, 180, 900
REAL_DIALOG_SECONDS = 5

# Fixed texts a consumer sees (docs/reference.md); a deliberate text change updates these.
# Argument refusals name only schema parameters (missing, invalid) and count undeclared keys.
INVALID_ARGUMENTS = "Error executing tool {}: invalid_arguments: {}"
UNKNOWN_TOOL = "unknown_tool (refresh tools/list)"
# The guard names the profile, or the profiles, that unlock the missing capabilities, else runtime.toml.
CAPABILITY_DISABLED = ("Error executing tool {}: capability_disabled: {} (owner enables profile {}); "
                       "next_action: enable_capability")
CAPABILITY_DISABLED_POLICY = ("Error executing tool {}: capability_disabled: {} (owner must enable in runtime.toml); "
                              "next_action: enable_capability")
CONFIG_STALE = "Error executing tool {}: config_stale (restart matching components); next_action: update_installation"
CLIENT_SCHEMA_MISMATCH = ("Error executing tool {}: client_schema_mismatch (refresh tools/list, then call _manifest with "
                          "expected_schema_fingerprint set to the current schema_fingerprint)")
APPROVAL_NOT_GIVEN = "explicit approval was not given"
APPROVAL_DETAILS = {"declined": "the owner declined in the local dialog; no attempt was made",
                    "timeout": "the local dialog timed out without a choice; no attempt was made"}
PROMPTS = ("check_connection", "find_and_summarize", "reply_with_file", "unread_overview")
MANIFEST_CAPS = {"catalog_page_size": 15, "global_message_page_size": 10, "scan_ttl_seconds": 300,
                 "scan_capacity": 4}
SELECTION_CAPS = {"ttl_seconds": 900, "live_capacity_per_owner": 16, "active_capacity_per_owner": 4,
                  "message_projection_bytes": 65536}

# Every tool's listed required parameters (the 0.58 surface): the argument refusal names exactly these.
TOOL_REQUIRED = {
    "_manifest": (), "analyze_media": (), "append_local_upload": ("content_base64", "index", "sha256", "upload_id"),
    "apply_prepared_archive": ("approved", "draft_id"), "apply_prepared_caption_edit": ("approved", "draft_id"),
    "apply_prepared_message_delete": ("approved", "draft_id"), "apply_prepared_pin_change": ("approved", "draft_id"),
    "apply_prepared_text_edit": ("approved", "draft_id"),
    "begin_local_upload": ("file_name", "kind", "sha256", "size_bytes"), "cancel_draft": ("draft_id",),
    "cancel_local_upload_authority": ("result_ref",), "consume_local_upload_result": ("result_ref",),
    "create_local_artifact": ("file_name",), "discover_targets": ("hypotheses",), "finish_local_upload": ("upload_id",),
    "get_album_send_status": ("draft_id",), "get_attachment": (), "get_draft": ("draft_id",),
    "get_message_context": (), "get_reply_artifact_draft": ("draft_id",), "get_reply_draft": ("draft_id",),
    "get_send_status": ("draft_id",), "issue_local_upload_authority": ("manifest",), "list_chats": ("scope",),
    "list_drafts": (), "list_topics": ("target",), "prepare_album_send": ("items", "recipient"),
    "prepare_artifact_send": ("display_name", "mime_type", "recipient"),
    "prepare_caption_edit": ("chat", "message_id", "new_caption"), "prepare_chat_archive": ("chat", "target_list"),
    "prepare_forward_send": ("mode", "recipient", "source_chat", "source_message_id"),
    "prepare_message_delete": ("chat", "message_id"), "prepare_pin_change": ("action", "chat", "message_id"),
    "prepare_reply_artifact_send": ("display_name", "mime_type", "recipient"),
    "prepare_reply_text_send": ("recipient", "text"), "prepare_text_edit": ("chat", "message_id", "new_text"),
    "prepare_text_send": ("recipient", "text"), "prepare_video_send": ("artifact_id", "recipient"),
    "read_attachment": (), "read_attachment_page": (), "read_history": ("mode", "target"), "read_messages": (),
    "read_presentation": (), "read_reply_chain": (), "read_spreadsheet": (),
    "read_target_messages": ("message_ids", "target_handle"), "read_topic_history": ("mode", "target", "topic"),
    "refresh_draft": ("draft_id",), "refresh_reply_artifact_draft": ("draft_id",), "refresh_reply_draft": ("draft_id",),
    "resolve_target": ("target",), "retain_selection": ("source",), "search_chats": ("mode", "query", "targets"),
    "search_correspondence": ("query", "target"), "search_messages": ("mode", "query", "target"),
    "send_prepared_album": ("approved", "draft_id"), "send_prepared_artifact": ("approved", "draft_id"),
    "send_prepared_forward": ("approved", "draft_id"), "send_prepared_text": ("approved", "draft_id"),
    "send_prepared_video": ("approved", "draft_id"), "update_draft": ("draft_id",),
    "update_reply_artifact_draft": ("caption", "draft_id"), "update_reply_draft": ("draft_id", "text"),
    "verify_target": ("target",),
}
# The guard's requirement of every gated tool and the profile that unlocks it whole (None: runtime.toml).
_READERS, _SEND, _REPLIES = "readers", "send", "replies"
TOOL_GUARDS = {
    **{name: (("read",), None) for name in ("resolve_target", "discover_targets", "search_correspondence",
                                            "get_message_context")},
    **{name: (("artifacts",), None) for name in ("get_attachment", "read_attachment", "analyze_media",
                                                 "create_local_artifact", "begin_local_upload", "append_local_upload",
                                                 "finish_local_upload")},
    **{name: (("reply_artifact_send", "send"), _REPLIES) for name in (
        "prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft",
        "refresh_reply_artifact_draft")},
    **{name: (("reply_text_send", "send"), _REPLIES) for name in (
        "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft")},
    **{name: (("send",), _SEND) for name in ("prepare_text_send", "send_prepared_text", "prepare_artifact_send",
                                             "send_prepared_artifact", "list_drafts", "get_draft", "get_send_status",
                                             "cancel_draft", "update_draft", "refresh_draft")},
    "read_messages": (("read_messages",), _READERS), "read_history": (("read_history",), _READERS),
    "read_reply_chain": (("read_reply_chain",), _READERS), "list_topics": (("list_topics",), _READERS),
    "read_topic_history": (("read_topic_history",), _READERS), "search_messages": (("search_messages",), _READERS),
    "list_chats": (("list_chats",), _READERS), "search_chats": (("search_chats",), _READERS),
    "verify_target": (("verified_targets",), _READERS), "read_target_messages": (("verified_targets",), _READERS),
    "read_attachment_page": (("attachment_pages",), _READERS), "read_spreadsheet": (("spreadsheets",), _READERS),
    "read_presentation": (("presentations",), _READERS), "retain_selection": (("retained_selections",), _READERS),
    **{name: (("forward_send", "send"), "forward") for name in ("prepare_forward_send", "send_prepared_forward")},
    **{name: (("video_send", "send"), "video") for name in ("prepare_video_send", "send_prepared_video")},
    **{name: (("album_send", "send"), "albums") for name in ("prepare_album_send", "send_prepared_album",
                                                              "get_album_send_status")},
    **{name: ((capability, "send", "read"), None) for capability, names in (
        ("archive_chats", ("prepare_chat_archive", "apply_prepared_archive")),
        ("edit_own_text", ("prepare_text_edit", "apply_prepared_text_edit")),
        ("edit_own_caption", ("prepare_caption_edit", "apply_prepared_caption_edit")),
        ("pin_messages", ("prepare_pin_change", "apply_prepared_pin_change")),
        ("delete_own_message", ("prepare_message_delete", "apply_prepared_message_delete"))) for name in names},
}
# The local-upload authority tools answer the structured `unavailable` instead of a guard refusal.
UPLOAD_AUTHORITY_TOOLS = ("issue_local_upload_authority", "consume_local_upload_result", "cancel_local_upload_authority")
# Under the restricted policy: what each disabled tool is missing and the one profile that unlocks it.
RESTRICTED_GUARDS = {
    **{name: (TOOL_GUARDS[name][0], _READERS) for name in (
        "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages",
        "list_chats", "search_chats", "verify_target", "read_target_messages", "read_attachment_page",
        "read_spreadsheet", "read_presentation", "retain_selection")},
    **{name: (("forward_send",), "forward") for name in ("prepare_forward_send", "send_prepared_forward")},
    **{name: (("video_send",), "video") for name in ("prepare_video_send", "send_prepared_video")},
    **{name: (("album_send",), "albums") for name in ("prepare_album_send", "send_prepared_album",
                                                       "get_album_send_status")},
    **{name: ((capability,), profile) for capability, profile, names in (
        ("archive_chats", "archive_edit", ("prepare_chat_archive", "apply_prepared_archive")),
        ("edit_own_text", "archive_edit", ("prepare_text_edit", "apply_prepared_text_edit")),
        ("edit_own_caption", "archive_edit", ("prepare_caption_edit", "apply_prepared_caption_edit")),
        ("pin_messages", "pin", ("prepare_pin_change", "apply_prepared_pin_change")),
        ("delete_own_message", "delete", ("prepare_message_delete", "apply_prepared_message_delete"))) for name in names},
}

# Every census tool once, in call order, with the status its probe returns over the synthetic account
# (_manifest: its compatibility). Probes after the first name what earlier ones returned.
EVERY_TOOL = (
    ("_manifest", "compatible"), ("resolve_target", "resolved"), ("discover_targets", "partial"),
    ("search_correspondence", "matches"), ("get_attachment", "unsupported"), ("get_message_context", "complete"),
    ("create_local_artifact", "complete"), ("read_attachment", "complete"), ("analyze_media", "unsupported"),
    ("issue_local_upload_authority", "issued"), ("consume_local_upload_result", "pending"),
    ("cancel_local_upload_authority", "cancelled"), ("begin_local_upload", "ready"),
    ("append_local_upload", "accepted"), ("finish_local_upload", "complete"), ("prepare_text_send", "prepared"),
    ("send_prepared_text", "declined"), ("prepare_artifact_send", "prepared"), ("send_prepared_artifact", "declined"),
    ("read_messages", "complete"), ("read_history", "partial"), ("read_reply_chain", "complete"),
    ("list_topics", "unsupported"), ("read_topic_history", "unsupported"), ("search_messages", "partial"),
    ("list_chats", "page"), ("search_chats", "partial"), ("verify_target", "verified"),
    ("read_target_messages", "complete"), ("read_attachment_page", "complete"), ("read_spreadsheet", "unsupported"),
    ("read_presentation", "unsupported"), ("prepare_reply_text_send", "prepared"), ("get_reply_draft", "pending"),
    ("update_reply_draft", "revised"), ("refresh_reply_draft", "revised"), ("prepare_reply_artifact_send", "prepared"),
    ("get_reply_artifact_draft", "pending"), ("update_reply_artifact_draft", "revised"),
    ("refresh_reply_artifact_draft", "revised"), ("list_drafts", "listed"), ("get_draft", "pending"),
    ("get_send_status", "pending"), ("update_draft", "revised"), ("refresh_draft", "revised"),
    ("cancel_draft", "cancelled"), ("retain_selection", "retained"), ("prepare_forward_send", "prepared"),
    ("send_prepared_forward", "declined"), ("prepare_video_send", "prepared"), ("send_prepared_video", "declined"),
    ("prepare_chat_archive", "prepared"), ("apply_prepared_archive", "declined"), ("prepare_text_edit", "prepared"),
    ("apply_prepared_text_edit", "declined"), ("prepare_album_send", "prepared"), ("send_prepared_album", "declined"),
    ("get_album_send_status", "pending"), ("prepare_caption_edit", "prepared"),
    ("apply_prepared_caption_edit", "declined"), ("prepare_pin_change", "prepared"),
    ("apply_prepared_pin_change", "declined"), ("prepare_message_delete", "prepared"),
    ("apply_prepared_message_delete", "declined"),
)
UPLOAD_BYTES = b"Matrix synthetic upload"

# The synthetic account: one account, 25 private chats in Main, one public username, one source message.
ACCOUNT_ID = 9001
FIRST_CHAT, CHAT_COUNT = 91001, 25
PUBLIC_USERNAME = "matrix_synthetic"
SOURCE_MESSAGE = 55
SOURCE_TEXT = "Matrix synthetic source #matrix"
OWN_TEXT_MESSAGE, OWN_DOCUMENT_MESSAGE = 56, 57
OWN_TEXT = "Matrix synthetic own text"
OWN_CAPTION = "Matrix synthetic own caption"
# Reply sources of the 0.58 reply families, in the second chat: (message id, text, entity type, offset, length,
# the missing_capabilities a reply to it names under the restricted policy).
FAMILY_CHAT = FIRST_CHAT + 1
FAMILY_SOURCES = (
    (60, "Matrix bold", {"@type": "textEntityTypeBold"}, 7, 4, ["reply_formatted_targets"]),
    (61, "Matrix @matrix_synthetic", {"@type": "textEntityTypeMention"}, 7, 17,
     ["reply_formatted_targets", "reply_identity_targets"]),
    (62, "Matrix https://example.org", {"@type": "textEntityTypeUrl"}, 7, 19,
     ["reply_formatted_targets", "reply_url_targets"]),
    (63, "Matrix matrix@example.org", {"@type": "textEntityTypeEmailAddress"}, 7, 18,
     ["reply_formatted_targets", "reply_email_targets"]),
    (64, "Matrix +15550100", {"@type": "textEntityTypePhoneNumber"}, 7, 9,
     ["reply_formatted_targets", "reply_phone_targets"]),
    (65, "Matrix 4111111111111111", {"@type": "textEntityTypeBankCardNumber"}, 7, 16,
     ["reply_formatted_targets", "reply_bank_card_targets"]),
    (66, "Matrix link", {"@type": "textEntityTypeTextUrl", "url": "https://example.org/"}, 7, 4,
     ["reply_formatted_targets", "reply_text_url_targets"]),
    (67, "Matrix \U0001F642", {"@type": "textEntityTypeCustomEmoji", "custom_emoji_id": "5368324170671202286"}, 7, 2,
     ["reply_formatted_targets", "reply_custom_emoji_targets"]),
    (68, "Matrix $USD", {"@type": "textEntityTypeCashtag"}, 7, 4,
     ["reply_formatted_targets", "reply_lexical_targets"]),
)
SOURCE_MISSING = ["reply_formatted_targets", "reply_lexical_targets"]  # a hashtag: lexical text
STYLED = [{"offset": 0, "length": 6, "type": {"@type": "textEntityTypeBold"}}]  # "Matrix" in bold
DENIAL_PROBE = "Matrix synthetic denial probe"
OUTCOME_PROBE = "Matrix synthetic outcome probe"
TIMEOUT_PROBE = "Matrix synthetic timeout probe"
PDF_TEXT = "Matrix synthetic page"
REQUEST_TEXT = "Matrix synthetic request"
LIST_PAGE = 20  # list_chats limit cap
# Every request that changes provider state; no denied, declined or timed-out approval may log one.
DISPATCH_REQUESTS = frozenset({"sendMessage", "sendMessageAlbum", "forwardMessages", "addChatToList", "editMessageText",
                               "editMessageCaption", "pinChatMessage", "unpinChatMessage", "deleteMessages"})
# Which dialog a prompt is, from the preview the broker passes; plain text and file drafts carry none.
DIALOG_FAMILIES = (("album_preview", "album"), ("forward_preview", "forward"), ("video_preview", "video"),
                   ("archive_preview", "archive"), ("text_edit_preview", "text_edit"),
                   ("caption_edit_preview", "caption_edit"), ("pin_change_preview", "pin"),
                   ("message_delete_preview", "delete"), ("reply_artifact_preview", "reply_artifact"),
                   ("reply_preview", "reply_text"))
# Syntactically valid stand-ins: a guard refuses after argument validation, before any lookup.
FAKE_ARTIFACT = "artifact_" + "0" * 32 + "_" + "0" * 64 + "_1"
FAKE_ARTIFACT_2 = "artifact_" + "1" * 32 + "_" + "0" * 64 + "_1"
FAKE_DRAFT, FAKE_SELECTION = "draft_" + "0" * 32, "selection_" + "0" * 64
FAKE_TARGET, FAKE_UPLOAD, FAKE_RESULT = "target_" + "0" * 64, "upload_" + "0" * 32, "upload_result_" + "0" * 64
ZERO_SHA = "0" * 64


class MatrixRefusal(Exception):
    """A fixed refusal code; detail holds harness-derived facts, never caller input."""

    def __init__(self, code: str, detail: object = None):
        if code not in CODES:
            raise AssertionError("unknown refusal code")
        super().__init__(code)
        self.code = code
        self.detail = detail


def need(condition: object, code: str, detail: object = None) -> None:
    if not condition:
        raise MatrixRefusal(code, detail)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def tools_sha256(tools: list[str]) -> str:
    return _sha("\n".join(tools).encode("utf-8"))


# --- synthetic fixtures (pure stdlib, deterministic) ----------------------------------------------------

def guard_arguments(tool: str) -> dict:
    """Arguments that pass every gated tool's input schema without naming anything the account holds."""
    anchor = {"chat_id": FIRST_CHAT, "message_id": SOURCE_MESSAGE}
    artifact = {"recipient": FIRST_CHAT, "artifact_id": FAKE_ARTIFACT, "display_name": "matrix.png",
                "mime_type": "image/png"}
    draft, decision = {"draft_id": FAKE_DRAFT}, {"draft_id": FAKE_DRAFT, "approved": False}
    upload = {"file_name": "matrix.txt", "kind": "document", "size_bytes": 6, "sha256": ZERO_SHA}
    table = {
        "resolve_target": {"target": "@" + PUBLIC_USERNAME},
        "discover_targets": {"hypotheses": ["Matrix synthetic chat 01", "Matrix synthetic chat 02"]},
        "search_correspondence": {"target": FIRST_CHAT, "query": {"text": "matrix"}},
        "get_attachment": {"anchor": anchor}, "get_message_context": {"anchor": anchor},
        "read_attachment": {"artifact_id": FAKE_ARTIFACT}, "analyze_media": {"artifact_id": FAKE_ARTIFACT},
        "create_local_artifact": {"file_name": "matrix.txt", "content": "matrix"},
        "begin_local_upload": upload, "finish_local_upload": {"upload_id": FAKE_UPLOAD},
        "append_local_upload": {"upload_id": FAKE_UPLOAD, "index": 0, "content_base64": "bWF0cml4", "sha256": ZERO_SHA},
        "issue_local_upload_authority": {"manifest": upload},
        "consume_local_upload_result": {"result_ref": FAKE_RESULT},
        "cancel_local_upload_authority": {"result_ref": FAKE_RESULT},
        "prepare_reply_artifact_send": {**artifact, "reply_to": anchor},
        "update_reply_artifact_draft": {**draft, "caption": "matrix"},
        "prepare_reply_text_send": {"recipient": FIRST_CHAT, "text": "matrix", "reply_to": anchor},
        "update_reply_draft": {**draft, "text": "matrix"}, "update_draft": {**draft, "text": "matrix"},
        "prepare_text_send": {"recipient": FIRST_CHAT, "text": "matrix"}, "prepare_artifact_send": artifact,
        "read_messages": {"anchors": [anchor]}, "read_history": {"target": FIRST_CHAT, "mode": "latest"},
        "read_reply_chain": {"anchor": anchor}, "list_topics": {"target": FIRST_CHAT},
        "read_topic_history": {"target": FIRST_CHAT, "topic": {"id": 1}, "mode": "latest"},
        "search_messages": {"target": FIRST_CHAT, "query": "matrix", "mode": "latest"},
        "list_chats": {"scope": "main", "limit": 1},
        "search_chats": {"targets": [FIRST_CHAT], "query": "matrix", "mode": "latest"},
        "verify_target": {"target": FIRST_CHAT},
        "read_target_messages": {"target_handle": FAKE_TARGET, "message_ids": [SOURCE_MESSAGE]},
        "read_attachment_page": {"artifact_id": FAKE_ARTIFACT}, "read_spreadsheet": {"artifact_id": FAKE_ARTIFACT},
        "read_presentation": {"artifact_id": FAKE_ARTIFACT}, "list_drafts": {},
        "retain_selection": {"source": {"kind": "messages", "anchors": [anchor]}},
        "prepare_forward_send": {"source_chat": FIRST_CHAT, "source_message_id": SOURCE_MESSAGE,
                                 "recipient": FAMILY_CHAT, "mode": "copy"},
        "prepare_video_send": {"artifact_id": FAKE_ARTIFACT, "recipient": FIRST_CHAT},
        "prepare_chat_archive": {"chat": FIRST_CHAT, "target_list": "archive"},
        "prepare_text_edit": {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE, "new_text": "matrix"},
        "prepare_album_send": {"recipient": FIRST_CHAT, "items": [{"artifact_id": FAKE_ARTIFACT},
                                                                  {"artifact_id": FAKE_ARTIFACT_2}]},
        "prepare_caption_edit": {"chat": FIRST_CHAT, "message_id": OWN_DOCUMENT_MESSAGE, "new_caption": "matrix"},
        "prepare_pin_change": {"chat": FIRST_CHAT, "message_id": SOURCE_MESSAGE, "action": "pin"},
        "prepare_message_delete": {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE},
    }
    if tool in table:
        return table[tool]
    if tool.startswith(("send_prepared_", "apply_prepared_")):
        return decision
    return draft


def synthetic_pdf(*pages: str) -> bytes:
    """A PDF with one text line per page (PyMuPDF from the pinned closure; driver side only)."""
    import fitz
    document = fitz.open()
    for text in pages:
        document.new_page().insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def find_draft(value: object) -> str | None:
    """The draft_id of a prepare or revise result, wherever its family nests it."""
    if isinstance(value, dict):
        if isinstance(value.get("draft_id"), str):
            return value["draft_id"]
        for item in value.values():
            found = find_draft(item)
            if found:
                return found
    return None


def guard_text(tool: str, capabilities: tuple, profile: str | None) -> str:
    names = ", ".join(capabilities)
    return (CAPABILITY_DISABLED.format(tool, names, profile) if profile is not None
            else CAPABILITY_DISABLED_POLICY.format(tool, names))


def argument_text(tool: str) -> str:
    """The refusal of a call that gives one undeclared key and nothing else."""
    required = TOOL_REQUIRED[tool]
    return INVALID_ARGUMENTS.format(tool, (f"missing {', '.join(required)}; " if required else "")
                                    + "1 unexpected argument(s)")

def synthetic_png() -> bytes:
    """An 8x8 RGB PNG, built byte for byte so the expected bytes never depend on an encoder."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"".join(bytes((x * 32, y * 32, 128)) for x in range(8)) for y in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def synthetic_mp4() -> bytes:
    """A faststart ISO-BMFF movie (ftyp, moov with one 1280x720 avc1 track of 5 s, mdat): boxes only, no media,
    which is what the native-video probe reads."""
    def box(kind: bytes, *parts: bytes) -> bytes:
        body = b"".join(parts)
        return struct.pack(">I4s", 8 + len(body), kind) + body

    def full(kind: bytes, body: bytes, flags: int = 0) -> bytes:
        return box(kind, b"\x00" + flags.to_bytes(3, "big"), body)
    unity = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    header = full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, 0) + bytes(16) + unity
                  + struct.pack(">II", 1280 << 16, 720 << 16), flags=3)
    sample = full(b"stsd", struct.pack(">I", 1) + box(b"avc1", bytes(78)))
    media = box(b"mdia", full(b"hdlr", struct.pack(">I4s", 0, b"vide") + bytes(12) + b"\x00"),
                box(b"minf", box(b"stbl", sample)))
    movie = box(b"moov", full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 5000) + bytes(80)),
                box(b"trak", header, media))
    return box(b"ftyp", b"isom", struct.pack(">I", 0), b"isom", b"mp42") + movie + box(b"mdat", bytes(64))


def chat_object(chat_id: int) -> dict:
    index = chat_id - FIRST_CHAT + 1
    return {"@type": "chat", "id": chat_id, "title": f"Matrix synthetic chat {index:02d}",
            "type": {"@type": "chatTypePrivate", "user_id": chat_id + 1000},
            "chat_lists": [{"@type": "chatListMain"}],
            "positions": [{"@type": "chatPosition", "list": {"@type": "chatListMain"},
                           "order": str(9_000_000 - index), "is_pinned": False}],
            "unread_count": index % 3, "unread_mention_count": 0, "unread_reaction_count": 0,
            "is_marked_as_unread": False, "has_protected_content": False}


def _message(message_id: int, content: dict, *, outgoing: bool, date: int) -> dict:
    """One message of the first synthetic chat, with every state field the projections require."""
    return {"@type": "message", "chat_id": FIRST_CHAT, "id": message_id,
            "sender_id": {"@type": "messageSenderUser", "user_id": ACCOUNT_ID if outgoing else FIRST_CHAT + 1000},
            "is_outgoing": outgoing, "is_from_offline": False, "is_pinned": False, "ephemeral_message_id": 0,
            "date": date, "edit_date": 0, "self_destruct_in": 0.0, "auto_delete_in": 0.0, "author_signature": "",
            "via_bot_user_id": 0, "media_album_id": "0", "content": content}


def _text(text: str, entities: list | None = None) -> dict:
    return {"@type": "formattedText", "text": text, "entities": entities or []}


def source_message() -> dict:
    offset = SOURCE_TEXT.index("#")
    return _message(SOURCE_MESSAGE, {"@type": "messageText", "text": _text(SOURCE_TEXT, [
        {"@type": "textEntity", "offset": offset, "length": len(SOURCE_TEXT) - offset,
         "type": {"@type": "textEntityTypeHashtag"}}])}, outgoing=False, date=1700000000)


def own_text_message() -> dict:
    """The account's own text message: the edit and delete source."""
    return _message(OWN_TEXT_MESSAGE, {"@type": "messageText", "text": _text(OWN_TEXT)}, outgoing=True,
                    date=1700000100)


def own_document_message() -> dict:
    """The account's own captioned document: the caption edit source."""
    return _message(OWN_DOCUMENT_MESSAGE, {"@type": "messageDocument", "caption": _text(OWN_CAPTION), "document": {
        "@type": "document", "file_name": "matrix-synthetic.txt", "mime_type": "text/plain",
        "document": {"@type": "file", "id": 1, "size": 21,
                     "remote": {"@type": "remoteFile", "unique_id": "matrix-synthetic-document"}}}},
        outgoing=True, date=1700000200)


def family_message(message_id: int) -> dict:
    """A reply source in the second synthetic chat whose one entity belongs to one reply family (0.58)."""
    _, text, entity, offset, length, _ = next(row for row in FAMILY_SOURCES if row[0] == message_id)
    message = _message(message_id, {"@type": "messageText", "text": _text(text, [
        {"@type": "textEntity", "offset": offset, "length": length, "type": entity}])}, outgoing=False,
        date=1700000300 + message_id)
    return {**message, "chat_id": FAMILY_CHAT, "sender_id": {"@type": "messageSenderUser", "user_id": FAMILY_CHAT + 1000}}


def synthetic_messages(chat_id: int = FIRST_CHAT) -> dict[int, dict]:
    if chat_id == FAMILY_CHAT:
        return {row[0]: family_message(row[0]) for row in FAMILY_SOURCES}
    if chat_id != FIRST_CHAT:
        return {}
    return {message["id"]: message for message in (source_message(), own_text_message(), own_document_message())}


def message_properties(message_id: int) -> dict:
    own = message_id != SOURCE_MESSAGE
    return {"@type": "messageProperties", "can_be_replied": True, "can_be_edited": own, "can_be_forwarded": True,
            "can_be_copied": True, "can_be_saved": True, "can_be_pinned": True,
            "can_be_deleted_for_all_users": own, "can_be_deleted_only_for_self": True,
            "has_protected_content_by_current_user": False, "has_protected_content_by_other_user": False}


class SyntheticTDLib:
    """The raw tdjson transport of one fixed synthetic account: answers from fixtures, never a network.

    sendMessage raises after it is logged, which the client reads as a lost transport (outcome unknown);
    an unauthorized account answers authorizationStateWaitPhoneNumber. Unknown requests get a TDLib error.
    """

    def __init__(self, *, authorized: bool = True, record=None):
        self._authorized = authorized
        self._record = record or (lambda entry: None)
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    def _answer(self, request: dict) -> dict:
        kind = request.get("@type")
        if kind == "getAuthorizationState":
            return {"@type": "authorizationStateReady" if self._authorized else "authorizationStateWaitPhoneNumber"}
        if kind == "getMe":
            return {"@type": "user", "id": ACCOUNT_ID, "first_name": "Matrix", "last_name": "",
                    "usernames": None, "type": {"@type": "userTypeRegular"}}
        if kind == "getChats":
            main = request.get("chat_list", {}).get("@type") == "chatListMain"
            ids = list(range(FIRST_CHAT, FIRST_CHAT + CHAT_COUNT)) if main else []
            return {"@type": "chats", "total_count": len(ids), "chat_ids": ids[:request.get("limit", 0)]}
        if kind == "getChat":
            chat_id = request.get("chat_id")
            if type(chat_id) is int and FIRST_CHAT <= chat_id < FIRST_CHAT + CHAT_COUNT:
                return chat_object(chat_id)
            return {"@type": "error", "code": 400, "message": "CHAT_NOT_FOUND"}
        if kind == "searchPublicChat":
            if request.get("username") == PUBLIC_USERNAME:
                return chat_object(FIRST_CHAT)
            return {"@type": "error", "code": 400, "message": "USERNAME_NOT_OCCUPIED"}
        if kind in ("getMessage", "getMessageProperties"):
            chat_id = request.get("chat_id")
            messages = synthetic_messages(chat_id) if chat_id in (FIRST_CHAT, FAMILY_CHAT) else {}
            message_id = request.get("message_id")
            if type(message_id) is int and message_id in messages:
                return messages[message_id] if kind == "getMessage" else message_properties(message_id)
            return {"@type": "error", "code": 404, "message": "Not Found"}
        if kind == "getUser":
            user_id = request.get("user_id")
            if user_id == ACCOUNT_ID or (type(user_id) is int and FIRST_CHAT + 1000 <= user_id
                                         < FIRST_CHAT + 1000 + CHAT_COUNT):
                return {"@type": "user", "id": user_id, "first_name": "Matrix", "last_name": "Peer",
                        "usernames": None, "type": {"@type": "userTypeRegular"}}
            return {"@type": "error", "code": 400, "message": "USER_NOT_FOUND"}
        if kind == "getChatListsToAddChat":
            return {"@type": "chatLists", "chat_lists": [{"@type": "chatListArchive"}]}
        if kind in ("getChatHistory", "searchChatMessages", "searchMessages"):
            return self._history(kind, request)
        return {"@type": "error", "code": 400, "message": "unsupported by the synthetic account"}

    @staticmethod
    def _history(kind: str, request: dict) -> dict:
        """Newest first, as td_api.tl documents getChatHistory: from exactly from_message_id (0: the last message),
        with -offset newer messages before it. A search matches the query case-insensitively in a text or caption
        and answers in one page; a media filter finds nothing."""
        chat = request.get("chat_id")
        messages = sorted(synthetic_messages().values(), key=lambda message: -message["id"])
        if kind != "searchMessages" and chat != FIRST_CHAT:
            messages = []
        start, offset = request.get("from_message_id"), request.get("offset")
        if type(start) is int and start > 0:
            first = next((index for index, message in enumerate(messages) if message["id"] <= start), len(messages))
            back = -offset if type(offset) is int and offset < 0 else 0
            messages = messages[max(0, first - back):]
        if kind != "getChatHistory":
            query = request.get("query") if isinstance(request.get("query"), str) else ""
            content = lambda message: message["content"].get("text") or message["content"].get("caption") or {}
            messages = [] if request.get("filter") is not None or not query else [
                message for message in messages if query.casefold() in content(message).get("text", "").casefold()]
        limit = request.get("limit") if type(request.get("limit")) is int else 0
        messages = messages[:max(limit, 0)]
        if kind == "getChatHistory":
            return {"@type": "messages", "total_count": len(messages), "messages": messages}
        if kind == "searchChatMessages":
            return {"@type": "foundChatMessages", "total_count": len(messages), "messages": messages,
                    "next_from_message_id": 0}
        return {"@type": "foundMessages", "total_count": len(messages), "messages": messages, "next_offset": ""}

    def send(self, request: dict) -> None:
        kind = request.get("@type")
        self._record({"request": kind if type(kind) is str else "invalid"})
        if kind == "sendMessage":
            raise ConnectionError("synthetic transport loss after dispatch")
        response = dict(self._answer(request))
        response["@extra"] = request.get("@extra")
        with self._lock:
            self._pending.append(response)

    def receive(self, timeout: float):
        with self._lock:
            if self._pending:
                return self._pending.pop(0)
        time.sleep(min(max(timeout, 0.0), 0.01))
        return None

    def close(self) -> None:
        pass


# --- the synthetic broker (runs inside the temporary venv) ----------------------------------------------

class JsonLog:
    def __init__(self, path: Path):
        self._path, self._lock = path, threading.Lock()

    def __call__(self, entry: dict) -> None:
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._lock, open(self._path, "a", encoding="utf-8") as stream:
            stream.write(line)


def read_log(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def approval_prompt(record, *, real_dialog: bool):
    """Scripted owner: renders the bound dialog document, then declines; approves only the outcome probe and lets
    the timeout probe's dialog time out. Each entry names the dialog's family and verb.

    With real_dialog the denial probe opens the real JXA dialog with a 5-second timer instead."""
    from telegram_search_mcp import approval_prompt as approval

    def prompt(**arguments):
        text = arguments.get("text")
        probe = {OUTCOME_PROBE: "outcome", DENIAL_PROBE: "denial", TIMEOUT_PROBE: "timeout"}.get(text, "other")
        family = next((name for key, name in DIALOG_FAMILIES if arguments.get(key) is not None),
                      "text" if arguments.get("kind") == "text" else "artifact")
        rendered = approval.render_approval(**arguments)
        entry = {"prompt": probe, "family": family, "rendered": rendered is not None,
                 "rows": len(rendered[0].splitlines()) if rendered else 0,
                 "verb": rendered[1] if rendered and rendered[1] in ("send", "change", "delete") else None}
        if rendered is None:
            record({**entry, "outcome": "dialog_unavailable"})
            return approval.UNAVAILABLE
        if probe == "outcome":
            record({**entry, "outcome": "approved", "mode": "scripted"})
            return approval.APPROVED
        if probe == "timeout":
            record({**entry, "outcome": "timeout", "mode": "scripted"})
            return approval.TIMEOUT
        if probe == "denial" and real_dialog:
            started = time.monotonic()
            decision = approval.decide_approved_send(**arguments, timeout_seconds=REAL_DIALOG_SECONDS)
            record({**entry, "outcome": decision.outcome, "mode": "real_dialog",
                    "seconds": round(time.monotonic() - started, 1)})
            return decision
        record({**entry, "outcome": "declined", "mode": "scripted"})
        return approval.DECLINED
    return prompt


def broker_main(args: argparse.Namespace) -> int:
    home = Path(os.environ["HOME"])
    import telegram_search_mcp
    root = Path(args.package_root).resolve()
    if not Path(telegram_search_mcp.__file__).resolve().is_relative_to(root):
        print("BROKER_PACKAGE_MISMATCH", file=sys.stderr)
        return 3
    from telegram_search_mcp import config
    from telegram_search_mcp.broker import Broker
    from telegram_search_mcp.tdjson import TDLibClient

    record = JsonLog(home / "matrix-broker.jsonl")

    def refuse_credentials():
        raise RuntimeError("the synthetic account has no credentials")

    client = TDLibClient(raw=SyntheticTDLib(authorized=args.profile != "unauthorized", record=record),
                         session_directory=home / "synthetic-session", credential_loader=refuse_credentials)
    broker = Broker(socket_path=config.BROKER_SOCKET_PATH, lock_path=config.BROKER_LOCK_PATH,
                    client_factory=lambda: client, policy=config.load_runtime_policy(),
                    download_source_root=home / "synthetic-session" / "files",
                    approval_prompt=approval_prompt(record, real_dialog=args.approval == "real_dialog"))
    signal.signal(signal.SIGTERM, lambda *_: broker.shutdown())
    signal.signal(signal.SIGINT, lambda *_: broker.shutdown())
    thread = threading.Thread(target=broker.serve_forever, daemon=True)
    thread.start()
    if not broker.wait_until_ready(timeout=BROKER_START_SECONDS):
        print("BROKER_NOT_READY", file=sys.stderr)
        return 4
    (home / "matrix-broker.ready").write_text("ready\n")
    while thread.is_alive():
        thread.join(0.2)
    return 0


# --- the driver (runs inside the temporary venv, official SDK only) -------------------------------------

class Cell:
    def __init__(self, cell_id: str):
        self.id, self.checks, self.failed = cell_id, 0, None

    def expect(self, label: str, condition: object) -> bool:
        self.checks += 1
        if not condition and self.failed is None:
            self.failed = label
        return bool(condition)

    def result(self) -> dict:
        passed = self.failed is None and self.checks > 0
        return {"id": self.id, "status": "pass" if passed else "fail", "checks": self.checks,
                **({} if passed else {"failed_check": self.failed or "no_checks"})}


class SchemaLedger:
    """Validates every structured result against the outputSchema its session listed."""

    def __init__(self):
        self.validated, self.failures, self.unschematized, self.tools = 0, [], set(), set()
        self._validators = {}

    def listing(self, tools) -> None:
        from jsonschema.validators import validator_for
        for tool in tools:
            if tool.output_schema is not None:
                cls = validator_for(tool.output_schema)
                cls.check_schema(tool.output_schema)
                self._validators[tool.name] = cls(tool.output_schema)
            else:
                self._validators[tool.name] = None

    def check(self, name: str, result) -> None:
        structured = getattr(result, "structured_content", None)
        if structured is None:
            return
        if name not in self._validators:
            self.failures.append(f"{name}:unlisted")
            return
        validator = self._validators[name]
        if validator is None:
            self.unschematized.add(name)
            return
        if any(True for _ in validator.iter_errors(structured)):
            self.failures.append(f"{name}:invalid")
            return
        self.validated += 1
        self.tools.add(name)

    def summary(self) -> dict:
        return {"validated": self.validated, "failures": sorted(set(self.failures)), "tools": sorted(self.tools),
                "unschematized": sorted(self.unschematized)}


class Consumer:
    """One official-SDK client over STDIO to the proxy, with output-schema accounting on every call."""

    def __init__(self, client, ledger: SchemaLedger):
        self.client, self.ledger = client, ledger

    async def call(self, name: str, arguments: dict):
        try:
            result = await self.client.call_tool(name, arguments)
        except RuntimeError:
            # The SDK raises when structured content does not match the listed output schema.
            self.ledger.failures.append(f"{name}:sdk_rejected")
            raise
        self.ledger.check(name, result)
        return result

    async def _listing(self, method: str, field: str) -> list:
        """Every page; `Client` takes a cursor, the bare `ClientSession` of a pinned handshake takes params."""
        import mcp_types as types
        from mcp.client.session import ClientSession
        listed, cursor = [], None
        for _ in range(64):
            if isinstance(self.client, ClientSession):
                page = await getattr(self.client, method)(params=types.PaginatedRequestParams(cursor=cursor))
            else:
                page = await getattr(self.client, method)(cursor=cursor)
            listed.extend(getattr(page, field))
            cursor = page.next_cursor
            if cursor is None:
                return listed
        raise RuntimeError("listing did not end")

    async def tools(self) -> list:
        listed = await self._listing("list_tools", "tools")
        self.ledger.listing(listed)
        return listed

    async def prompts(self) -> list:
        return await self._listing("list_prompts", "prompts")


def listing_digest(tools) -> str:
    """sha256 of the consumer-observed tool listing: every field a client reads, in name order."""
    return _sha(json.dumps([
        {"name": tool.name, "title": tool.title, "description": tool.description,
         "inputSchema": tool.input_schema, "outputSchema": tool.output_schema,
         "annotations": tool.annotations.model_dump(mode="json", exclude_none=True) if tool.annotations else None}
        for tool in sorted(tools, key=lambda t: t.name)], sort_keys=True, ensure_ascii=False).encode("utf-8"))


def error_text(result) -> str | None:
    """The single text block of an error result, else None."""
    content = getattr(result, "content", None) or []
    if (getattr(result, "is_error", False) is not True or getattr(result, "structured_content", None) is not None
            or len(content) != 1 or getattr(content[0], "type", None) != "text"):
        return None
    return content[0].text


def structured(result) -> dict:
    """The structured content of a success result; a union return type arrives wrapped as {"result": ...}."""
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict) or getattr(result, "is_error", True) is not False:
        return {}
    if set(value) == {"result"} and isinstance(value["result"], dict):
        return value["result"]
    return value


async def prompt_error(client, name: str, arguments: dict | None) -> str | None:
    from mcp.shared.exceptions import MCPError
    try:
        await client.get_prompt(name, arguments)
    except MCPError as error:
        data = getattr(error, "error", None)
        return getattr(data, "message", None)
    return None


def proxy_parameters(plan: dict):
    from mcp import StdioServerParameters
    env = {"HOME": plan["home"], "PATH": SYSTEM_PATH, "LC_ALL": "C", "TMPDIR": plan["tmp"],
           **({"PYTHONPATH": plan["pythonpath"]} if plan.get("pythonpath") else {})}
    return StdioServerParameters(command=plan["proxy"][0], args=list(plan["proxy"][1:]), env=env, cwd=plan["home"])


async def legacy_session(stack, params, revision: str):
    """An initialize handshake that offers exactly `revision`, through the official SDK's own session."""
    import mcp_types as types
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(dispatcher=JSONRPCDispatcher(read, write),
                                                            read_timeout_seconds=120))
    result = await session.send_request(types.InitializeRequest(params=types.InitializeRequestParams(
        protocol_version=revision, capabilities=types.ClientCapabilities(),
        client_info=types.Implementation(name="consumer-matrix", version="1"))), types.InitializeResult)
    session.adopt(result)
    await session.send_notification(types.InitializedNotification())
    return session, result


class Driver:
    def __init__(self, plan: dict):
        self.plan, self.census = plan, plan["census"]
        self.ledger, self.cells, self.observed = SchemaLedger(), [], {}
        self.params = proxy_parameters(plan)
        self.log = Path(plan["broker_log"]) if plan.get("broker_log") else None

    def broker_entries(self) -> list[dict]:
        return read_log(self.log) if self.log else []

    def sends(self) -> int:
        return sum(entry.get("request") == "sendMessage" for entry in self.broker_entries())

    def dispatches(self) -> int:
        """Every provider-changing request the broker made, whatever its family."""
        return sum(entry.get("request") in DISPATCH_REQUESTS for entry in self.broker_entries())

    def owner_prompts(self, **match) -> list[dict]:
        return [entry for entry in self.broker_entries()
                if "prompt" in entry and all(entry.get(key) == value for key, value in match.items())]

    async def run_cell(self, cell_id: str, body) -> None:
        cell = Cell(cell_id)
        try:
            await asyncio.wait_for(body(cell), 300)
        except Exception as error:  # A crash is a failed cell, recorded by its type only.
            cell.expect("exception:" + type(error).__name__, False)
            if self.plan.get("debug"):
                import traceback
                traceback.print_exc()
        self.cells.append(cell.result())

    def client(self, **kwargs):
        from mcp import Client
        return Client(self.params, read_timeout_seconds=120, **kwargs)

    async def census_checks(self, cell: Cell, consumer: Consumer, *, server_info) -> None:
        cell.expect("server_name", server_info is not None and server_info.name == PROJECT)
        cell.expect("server_version", server_info is not None and server_info.version == self.census["version"])
        tools = await consumer.tools()
        cell.expect("tools", sorted(tool.name for tool in tools) == self.census["tools"])
        manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
        cell.expect("compatible", manifest.get("compatibility", {}).get("status") == "compatible")

    # -- full session ---------------------------------------------------------------------------------
    # Cells with drafts of their own run first, each on its own proxy (its own broker client and draft space).
    # Then the full session's cells on one legacy proxy, in this order: every denial runs before the one
    # approved send, so "no dispatch" means no provider-changing request in the broker log.
    ISOLATED_CELLS = ("tools.every_tool", "approval.denial_every_family", "cursor.list_drafts")
    FULL_CELLS = ("census.tools", "census.fingerprint", "census.prompts", "refusal.invalid_arguments",
                  "refusal.invalid_arguments_hook", "refusal.invalid_arguments_every_tool", "refusal.unknown_tool",
                  "refusal.prompt_unavailable", "refusal.prompt_arguments", "image.png", "image.pdf_page",
                  "image.attachment_page", "cursor.list_chats", "cursor.read_history", "cursor.search_messages",
                  "recovery.search_again", "caps.manifest", "caps.input_bounds", "caps.input_bounds_058",
                  "approval.declined_flag", "approval.denial", "approval.timeout", "recovery.check_send_status")

    async def full(self) -> None:
        from contextlib import AsyncExitStack
        import mcp_types
        sdk_revisions = (tuple(mcp_types.version.HANDSHAKE_PROTOCOL_VERSIONS) == LEGACY_REVISIONS
                         and tuple(mcp_types.version.MODERN_PROTOCOL_VERSIONS) == MODERN_REVISIONS)

        async def legacy(cell, revision):
            cell.expect("sdk_revisions", sdk_revisions)
            async with AsyncExitStack() as stack:
                session, result = await legacy_session(stack, self.params, revision)
                cell.expect("negotiated", result.protocol_version == revision and session.protocol_version == revision)
                await self.census_checks(cell, Consumer(session, self.ledger), server_info=result.server_info)

        async def modern(cell, revision):
            cell.expect("sdk_revisions", sdk_revisions)
            async with self.client(mode="auto") as client:
                cell.expect("negotiated", client.protocol_version == revision)
                await self.census_checks(cell, Consumer(client, self.ledger), server_info=client.server_info)

        for revision in REVISIONS:
            body = legacy if revision in LEGACY_REVISIONS else modern
            await self.run_cell(f"handshake.{revision}", lambda cell, body=body, revision=revision: body(cell, revision))
        for name in self.ISOLATED_CELLS:
            await self.run_cell(name, getattr(self, "cell_" + name.replace(".", "_")))
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            tools = await consumer.tools()
            for name in self.FULL_CELLS:
                body = getattr(self, "cell_" + name.replace(".", "_"))
                await self.run_cell(name, lambda cell, body=body: body(cell, consumer, tools))
        await self.run_cell("refusal.client_schema_mismatch", self.cell_client_schema_mismatch)

    async def cell_census_tools(self, cell, consumer, tools) -> None:
        names = [tool.name for tool in tools]
        cell.expect("unique", len(names) == len(set(names)))
        cell.expect("names", sorted(names) == self.census["tools"])
        cell.expect("titles", all(isinstance(tool.title, str) and tool.title.strip() for tool in tools))
        cell.expect("input_schemas", all(tool.input_schema.get("type") == "object" for tool in tools))
        self.observed["listing_sha256"] = listing_digest(tools)

    async def cell_census_fingerprint(self, cell, consumer, tools) -> None:
        manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
        compatibility = manifest.get("compatibility", {})
        cell.expect("compatible", compatibility.get("status") == "compatible")
        cell.expect("fingerprint", manifest.get("schema_fingerprint") == self.census["schema_fingerprint"])
        cell.expect("version", manifest.get("package_version") == self.census["version"]
                    and manifest.get("version") == self.census["version"])
        cell.expect("generation", re.fullmatch(r"broker_[0-9a-f]{32}", str(manifest.get("broker_generation"))))
        cell.expect("capabilities", sorted(manifest.get("enabled_capabilities") or []) == self.census["capabilities"])
        cell.expect("registered_tools", sorted(manifest.get("tools") or []) == self.census["tools"])
        cell.expect("available_tools", sorted(manifest.get("available_tools") or [])
                    == sorted(tool.name for tool in tools))
        self.observed["schema_fingerprint"] = manifest.get("schema_fingerprint")

    async def cell_census_prompts(self, cell, consumer, tools) -> None:
        prompts = await consumer.prompts()
        cell.expect("names", sorted(prompt.name for prompt in prompts) == list(PROMPTS))
        for prompt in prompts:
            takes_request = any(argument.name == "request" for argument in (prompt.arguments or []))
            result = await consumer.client.get_prompt(prompt.name,
                                                      {"request": REQUEST_TEXT} if takes_request else None)
            texts = [message.content.text for message in result.messages
                     if getattr(message.content, "type", "") == "text"]
            cell.expect(f"{prompt.name}:rendered", texts and all(text.strip() for text in texts))
            if takes_request:
                cell.expect(f"{prompt.name}:quoted", any(json.dumps(REQUEST_TEXT) in text for text in texts))
            cell.expect(f"{prompt.name}:grants_nothing", any("grants no capability" in text for text in texts))

    async def cell_refusal_invalid_arguments(self, cell, consumer, tools) -> None:
        for name, arguments, clause in (("list_chats", {"scope": "everywhere", "limit": 1}, "invalid scope"),
                                        ("list_chats", {"scope": "main", "limit": 1, "path": "/nonexistent"},
                                         "1 unexpected argument(s)"),
                                        ("read_attachment", {"artifact_id": 7}, "invalid artifact_id"),
                                        ("prepare_text_send", {"recipient": True, "text": "x"}, "invalid recipient"),
                                        ("prepare_text_send", {"text": "x"}, "missing recipient")):
            text = error_text(await consumer.call(name, arguments))
            cell.expect(f"{name}:text", text == INVALID_ARGUMENTS.format(name, clause))
            cell.expect(f"{name}:no_detail", text is not None and "pydantic" not in text and "/nonexistent" not in text)

    async def cell_refusal_invalid_arguments_hook(self, cell, consumer, tools) -> None:
        for arguments, clause in (({"target": FIRST_CHAT, "query": "matrix"}, "invalid query"),
                                  ({"target": FIRST_CHAT, "query": {"text": "matrix"}, "date_from": "yesterday"},
                                   "invalid date_from")):
            text = error_text(await consumer.call("search_correspondence", arguments))
            cell.expect("text", text == INVALID_ARGUMENTS.format("search_correspondence", clause))

    async def cell_refusal_unknown_tool(self, cell, consumer, tools) -> None:
        text = error_text(await consumer.call("matrix_unknown_tool", {"probe": "matrix"}))
        cell.expect("text", text == UNKNOWN_TOOL)

    async def cell_refusal_prompt_unavailable(self, cell, consumer, tools) -> None:
        cell.expect("unknown", await prompt_error(consumer.client, "matrix_unknown_prompt", None)
                    == "prompt_unavailable")

    async def cell_refusal_prompt_arguments(self, cell, consumer, tools) -> None:
        cell.expect("unknown_argument", await prompt_error(consumer.client, "find_and_summarize",
                                                           {"matrix": "probe"}) == "invalid_arguments")
        cell.expect("no_arguments_prompt", await prompt_error(consumer.client, "check_connection",
                                                              {"request": "probe"}) == "invalid_arguments")

    async def created(self, cell, consumer, name: str, data: bytes) -> str | None:
        created = structured(await consumer.call("create_local_artifact", {
            "file_name": name, "content_base64": base64.b64encode(data).decode("ascii")}))
        cell.expect("created", created.get("status") == "complete" and created.get("artifact_id"))
        return created.get("artifact_id")

    async def cell_image_png(self, cell, consumer, tools) -> None:
        png = synthetic_png()
        artifact = await self.created(cell, consumer, "matrix-synthetic.png", png)
        result = await consumer.call("read_attachment", {"artifact_id": artifact})
        images = [block for block in result.content if block.type == "image"]
        cell.expect("not_error", result.is_error is False)
        cell.expect("text_first", result.content and result.content[0].type == "text"
                    and json.loads(result.content[0].text) == result.structured_content)
        cell.expect("one_image", len(images) == 1)
        cell.expect("mime", images and images[0].mime_type == "image/png")
        cell.expect("bytes", images and base64.b64decode(images[0].data) == png)

    async def cell_image_pdf_page(self, cell, consumer, tools) -> None:
        import fitz
        document = fitz.open()
        document.new_page().insert_text((72, 72), PDF_TEXT)
        pdf = document.tobytes()
        document.close()
        artifact = await self.created(cell, consumer, "matrix-synthetic.pdf", pdf)
        result = await consumer.call("read_attachment", {"artifact_id": artifact})
        images = [block for block in result.content if block.type == "image"]
        cell.expect("not_error", result.is_error is False)
        cell.expect("text", PDF_TEXT in (result.structured_content or {}).get("text", ""))
        cell.expect("image", len(images) >= 1 and all(block.mime_type == "image/png" for block in images))
        cell.expect("png", images and base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n"))

    async def cell_cursor_list_chats(self, cell, consumer, tools) -> None:
        self.replay_cursor = None
        try:
            await self._cursor_pages(cell, consumer)
        finally:  # The paired cell is recorded either way, failing when no cursor came back.
            await self.run_cell("recovery.restart_listing", self.cell_restart_listing(consumer))

    async def _cursor_pages(self, cell, consumer) -> None:
        first = structured(await consumer.call("list_chats", {"scope": "main", "limit": LIST_PAGE}))
        ids = [row.get("chat_id") for row in first.get("results", [])]
        cell.expect("page", first.get("status") == "page" and first.get("next_cursor"))
        cell.expect("cap", ids == list(range(FIRST_CHAT, FIRST_CHAT + LIST_PAGE)))
        second = structured(await consumer.call("list_chats", {"scope": "main", "limit": LIST_PAGE,
                                                                "cursor": first.get("next_cursor")}))
        rest = [row.get("chat_id") for row in second.get("results", [])]
        cell.expect("rest", rest == list(range(FIRST_CHAT + LIST_PAGE, FIRST_CHAT + CHAT_COUNT)))
        cell.expect("end", second.get("next_cursor") is None and "next_action" not in second)
        self.replay_cursor = first.get("next_cursor")

    def cell_restart_listing(self, consumer):
        async def body(cell):
            if not cell.expect("cursor", isinstance(self.replay_cursor, str)):
                return
            replay = structured(await consumer.call("list_chats", {"scope": "main", "limit": LIST_PAGE,
                                                                    "cursor": self.replay_cursor}))
            cell.expect("invalid_cursor", replay.get("status") == "invalid_cursor")
            cell.expect("next_action", replay.get("next_action") == "restart_listing")
            cell.expect("no_rows", replay.get("results") == [])
        return body

    async def cell_recovery_search_again(self, cell, consumer, tools) -> None:
        result = structured(await consumer.call("get_message_context", {"selection_id": "selection_" + "0" * 64}))
        cell.expect("status", result.get("status") == "selection_unavailable")
        cell.expect("next_action", result.get("next_action") == "search_again")

    async def cell_caps_manifest(self, cell, consumer, tools) -> None:
        manifest = structured(await consumer.call("_manifest", {}))
        for key, value in MANIFEST_CAPS.items():
            cell.expect(key, manifest.get(key) == value)
        selections = manifest.get("retained_selections") or {}
        for key, value in SELECTION_CAPS.items():
            cell.expect("retained_selections." + key, selections.get(key) == value)

    async def cell_caps_input_bounds(self, cell, consumer, tools) -> None:
        for name, arguments, parameter in (("list_chats", {"scope": "main", "limit": LIST_PAGE + 1}, "limit"),
                                           ("read_attachment", {"artifact_id": None, "max_chars": 20_001}, "max_chars"),
                                           ("read_attachment", {"artifact_id": None, "max_pages": 6}, "max_pages"),
                                           ("get_message_context", {"anchor": {"chat_id": FIRST_CHAT, "message_id": 1},
                                                                    "radius": 5}, "radius"),
                                           ("create_local_artifact", {"file_name": "a" * 129, "content": "x"},
                                            "file_name"),
                                           ("prepare_text_send", {"recipient": FIRST_CHAT, "text": "x" * 4097},
                                            "text")):
            cell.expect(f"{name}:refused", error_text(await consumer.call(name, arguments))
                        == INVALID_ARGUMENTS.format(name, "invalid " + parameter))

    async def prepared(self, cell, consumer, text: str) -> str | None:
        draft = structured(await consumer.call("prepare_text_send", {"recipient": FIRST_CHAT, "text": text}))
        cell.expect("prepared", draft.get("status") == "prepared" and draft.get("approval_required") is True
                    and draft.get("recipient") == FIRST_CHAT
                    and draft.get("recipient_title") == chat_object(FIRST_CHAT)["title"]
                    and draft.get("sha256") == _sha(text.encode("utf-8")))
        return draft.get("draft_id")

    async def cell_approval_declined_flag(self, cell, consumer, tools) -> None:
        draft_id = await self.prepared(cell, consumer, DENIAL_PROBE)
        prompts = len([e for e in self.broker_entries() if "prompt" in e])
        result = structured(await consumer.call("send_prepared_text", {"draft_id": draft_id, "approved": False}))
        cell.expect("declined", result.get("status") == "declined")
        cell.expect("detail", result.get("detail") == APPROVAL_NOT_GIVEN)
        cell.expect("no_dialog", len([e for e in self.broker_entries() if "prompt" in e]) == prompts)
        cell.expect("no_dispatch", self.sends() == 0)

    async def cell_approval_denial(self, cell, consumer, tools) -> None:
        real = self.plan["approval"] == "real_dialog"
        expected = "timeout" if real else "declined"
        draft_id = await self.prepared(cell, consumer, DENIAL_PROBE)
        started = time.monotonic()
        result = structured(await consumer.call("send_prepared_text", {"draft_id": draft_id, "approved": True}))
        elapsed = time.monotonic() - started
        cell.expect("status", result.get("status") == expected)
        cell.expect("detail", result.get("detail") == APPROVAL_DETAILS[expected])
        prompts = [e for e in self.broker_entries() if e.get("prompt") == "denial"]
        cell.expect("one_dialog", len(prompts) == 1)
        cell.expect("rendered", prompts and prompts[-1].get("rendered") is True and prompts[-1].get("rows", 0) > 0)
        cell.expect("mode", prompts and prompts[-1].get("mode") == ("real_dialog" if real else "scripted")
                    and prompts[-1].get("outcome") == expected)
        if real:
            cell.expect("timer", elapsed >= REAL_DIALOG_SECONDS)
        cell.expect("no_dispatch", self.sends() == 0)
        status = structured(await consumer.call("get_send_status", {"draft_id": draft_id}))
        cell.expect("still_pending", status.get("status") == "pending" and status.get("evidence") == "local_pending")
        self.observed["approval"] = {"mode": "real_dialog" if real else "scripted", "outcome": result.get("status")}

    async def cell_recovery_check_send_status(self, cell, consumer, tools) -> None:
        draft_id = await self.prepared(cell, consumer, OUTCOME_PROBE)
        result = structured(await consumer.call("send_prepared_text", {"draft_id": draft_id, "approved": True}))
        cell.expect("outcome_unknown", result.get("status") == "outcome_unknown")
        cell.expect("next_action", result.get("next_action") == "check_send_status")
        status = structured(await consumer.call("get_send_status", {"draft_id": draft_id}))
        cell.expect("status_unknown", status.get("status") == "outcome_unknown")
        cell.expect("status_next_action", status.get("next_action") == "check_send_status")
        again = structured(await consumer.call("send_prepared_text", {"draft_id": draft_id, "approved": True}))
        cell.expect("no_resend", again.get("status") == "outcome_unknown")
        cell.expect("one_dispatch", self.sends() == 1)
        cell.expect("one_owner_prompt", len([e for e in self.broker_entries() if e.get("prompt") == "outcome"]) == 1)

    async def cell_client_schema_mismatch(self, cell) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()
            first = structured(await consumer.call("_manifest", {"expected_schema_fingerprint": "0" * 64}))
            cell.expect("detected", first.get("compatibility", {}).get("status") == "client_schema_mismatch")
            text = error_text(await consumer.call("list_chats", {"scope": "main", "limit": 1}))
            cell.expect("blocked", text == CLIENT_SCHEMA_MISMATCH.format("list_chats"))
            again = structured(await consumer.call("_manifest", {
                "expected_schema_fingerprint": self.census["schema_fingerprint"], "check_broker": True}))
            cell.expect("recheck", again.get("compatibility", {}).get("status") == "compatible")
            listing = await consumer.call("list_chats", {"scope": "main", "limit": 1})
            cell.expect("unblocked", listing.is_error is False and structured(listing).get("status") == "page")

    # -- full session, tools through 0.58 ------------------------------------------------------------
    async def artifact(self, consumer, name: str, data: bytes) -> str | None:
        """A local artifact for later probes; its result is validated, never a check of its own."""
        created = structured(await consumer.call("create_local_artifact", {
            "file_name": name, "content_base64": base64.b64encode(data).decode("ascii")}))
        return created.get("artifact_id") if created.get("status") == "complete" else None

    def every_tool_arguments(self, tool: str, state: dict, files: dict) -> dict:
        """Each probe's arguments; a later probe names what an earlier one returned (a draft, a handle, a ref)."""
        anchor = {"chat_id": FIRST_CHAT, "message_id": SOURCE_MESSAGE}
        own = {"chat_id": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE}
        image = {"recipient": FIRST_CHAT, "artifact_id": files["png"], "display_name": "matrix-every.png",
                 "mime_type": "image/png"}
        upload = {"file_name": "matrix-upload.txt", "kind": "document", "size_bytes": len(UPLOAD_BYTES),
                  "sha256": _sha(UPLOAD_BYTES)}
        draft = lambda name: {"draft_id": find_draft(state.get(name))}
        decision = lambda name: {**draft(name), "approved": False}
        table = {
            "_manifest": {"check_broker": True}, "resolve_target": {"target": "@" + PUBLIC_USERNAME},
            "discover_targets": {"hypotheses": ["Matrix synthetic chat 01", "Matrix synthetic chat 02"]},
            "search_correspondence": {"target": FIRST_CHAT, "query": {"text": "matrix"}},
            "get_attachment": {"anchor": anchor}, "get_message_context": {"anchor": own, "radius": 1},
            "create_local_artifact": {"file_name": "matrix-every.csv", "content": "a,b\n1,2\n"},
            "read_attachment": {"artifact_id": files["png"]}, "analyze_media": {"artifact_id": files["png"]},
            "issue_local_upload_authority": {"manifest": upload},
            "consume_local_upload_result": {"result_ref": state.get("issue_local_upload_authority", {}).get("result_ref")},
            "cancel_local_upload_authority": {"result_ref": state.get("issue_local_upload_authority", {}).get("result_ref")},
            "begin_local_upload": upload,
            "append_local_upload": {"upload_id": state.get("begin_local_upload", {}).get("upload_id"), "index": 0,
                                    "content_base64": base64.b64encode(UPLOAD_BYTES).decode("ascii"),
                                    "sha256": _sha(UPLOAD_BYTES)},
            "finish_local_upload": {"upload_id": state.get("begin_local_upload", {}).get("upload_id")},
            "prepare_text_send": {"recipient": FIRST_CHAT, "text": "Matrix every text"},
            "send_prepared_text": decision("prepare_text_send"), "prepare_artifact_send": image,
            "send_prepared_artifact": decision("prepare_artifact_send"), "read_messages": {"anchors": [anchor]},
            "read_history": {"target": FIRST_CHAT, "mode": "latest"}, "read_reply_chain": {"anchor": anchor},
            "list_topics": {"target": FIRST_CHAT},
            "read_topic_history": {"target": FIRST_CHAT, "topic": {"id": 1}, "mode": "latest"},
            "search_messages": {"target": FIRST_CHAT, "query": "matrix", "mode": "latest"},
            "list_chats": {"scope": "main", "limit": 3},
            "search_chats": {"targets": [FIRST_CHAT], "query": "matrix", "mode": "latest"},
            "verify_target": {"target": FIRST_CHAT},
            "read_target_messages": {"target_handle": state.get("verify_target", {}).get("target_handle"),
                                     "message_ids": [SOURCE_MESSAGE]},
            "read_attachment_page": {"artifact_id": files["pdf"]},
            "read_spreadsheet": {"artifact_id": state.get("create_local_artifact", {}).get("artifact_id")},
            "read_presentation": {"artifact_id": state.get("create_local_artifact", {}).get("artifact_id")},
            "prepare_reply_text_send": {"recipient": FIRST_CHAT, "text": "Matrix every reply", "reply_to": own},
            "get_reply_draft": draft("prepare_reply_text_send"),
            "update_reply_draft": {**draft("prepare_reply_text_send"), "text": "Matrix every reply, revised"},
            "refresh_reply_draft": draft("update_reply_draft"),
            "prepare_reply_artifact_send": {**image, "reply_to": own},
            "get_reply_artifact_draft": draft("prepare_reply_artifact_send"),
            "update_reply_artifact_draft": {**draft("prepare_reply_artifact_send"), "caption": "Matrix every caption"},
            "refresh_reply_artifact_draft": draft("update_reply_artifact_draft"),
            "list_drafts": {}, "get_draft": draft("prepare_text_send"), "get_send_status": draft("prepare_text_send"),
            "update_draft": {**draft("prepare_text_send"), "text": "Matrix every text, revised"},
            "refresh_draft": draft("update_draft"), "cancel_draft": draft("refresh_draft"),
            "retain_selection": {"source": {"kind": "messages", "anchors": [own]}},
            "prepare_forward_send": {"source_chat": FIRST_CHAT, "source_message_id": SOURCE_MESSAGE,
                                     "recipient": FAMILY_CHAT, "mode": "copy"},
            "send_prepared_forward": decision("prepare_forward_send"),
            "prepare_video_send": {"artifact_id": files["mp4"], "recipient": FIRST_CHAT},
            "send_prepared_video": decision("prepare_video_send"),
            "prepare_chat_archive": {"chat": FIRST_CHAT, "target_list": "archive"},
            "apply_prepared_archive": decision("prepare_chat_archive"),
            "prepare_text_edit": {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE, "new_text": "Matrix every edit"},
            "apply_prepared_text_edit": decision("prepare_text_edit"),
            "prepare_album_send": {"recipient": FIRST_CHAT, "items": [{"artifact_id": files["png"]},
                                                                      {"artifact_id": files["png2"]}]},
            "send_prepared_album": decision("prepare_album_send"), "get_album_send_status": draft("prepare_album_send"),
            "prepare_caption_edit": {"chat": FIRST_CHAT, "message_id": OWN_DOCUMENT_MESSAGE,
                                     "new_caption": "Matrix every caption edit"},
            "apply_prepared_caption_edit": decision("prepare_caption_edit"),
            "prepare_pin_change": {"chat": FIRST_CHAT, "message_id": SOURCE_MESSAGE, "action": "pin"},
            "apply_prepared_pin_change": decision("prepare_pin_change"),
            "prepare_message_delete": {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE},
            "apply_prepared_message_delete": decision("prepare_message_delete"),
        }
        return table[tool]

    async def cell_tools_every_tool(self, cell) -> None:
        """Every census tool once, in EVERY_TOOL order: its structured status (_manifest: its compatibility)."""
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()
            files = {"png": await self.artifact(consumer, "matrix-every.png", synthetic_png()),
                     "png2": await self.artifact(consumer, "matrix-every-2.png", synthetic_png()),
                     "pdf": await self.artifact(consumer, "matrix-every.pdf", synthetic_pdf(PDF_TEXT)),
                     "mp4": await self.artifact(consumer, "matrix-every.mp4", synthetic_mp4())}
            state: dict = {}
            for tool, expected in EVERY_TOOL:
                value = structured(await consumer.call(tool, self.every_tool_arguments(tool, state, files)))
                state[tool] = value
                observed = value.get("compatibility", {}).get("status") if tool == "_manifest" else value.get("status")
                cell.expect(tool, observed == expected)
                if observed != expected and self.plan.get("debug"):
                    print("every_tool", tool, observed, file=sys.stderr)
            self.observed["every_tool_dispatches"] = self.dispatches()

    async def cell_approval_denial_every_family(self, cell) -> None:
        """Each send and apply family on its own drafts: prepared, approved by the caller, declined by the owner's
        rendered dialog of that family and verb; nothing is dispatched."""
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()
            png = await self.artifact(consumer, "matrix-family.png", synthetic_png())
            png2 = await self.artifact(consumer, "matrix-family-2.png", synthetic_png())
            mp4 = await self.artifact(consumer, "matrix-family.mp4", synthetic_mp4())
            own = {"chat_id": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE}
            image = {"recipient": FIRST_CHAT, "artifact_id": png, "display_name": "matrix-family.png",
                     "mime_type": "image/png"}
            families = (
                ("artifact", "prepare_artifact_send", image, "send_prepared_artifact", "send"),
                ("reply_text", "prepare_reply_text_send", {"recipient": FIRST_CHAT, "text": "Matrix family reply",
                                                           "reply_to": own}, "send_prepared_text", "send"),
                ("reply_artifact", "prepare_reply_artifact_send", {**image, "reply_to": own}, "send_prepared_artifact",
                 "send"),
                ("forward", "prepare_forward_send", {"source_chat": FIRST_CHAT, "source_message_id": SOURCE_MESSAGE,
                                                     "recipient": FAMILY_CHAT, "mode": "copy"},
                 "send_prepared_forward", "send"),
                ("video", "prepare_video_send", {"artifact_id": mp4, "recipient": FIRST_CHAT}, "send_prepared_video",
                 "send"),
                ("album", "prepare_album_send", {"recipient": FIRST_CHAT, "items": [{"artifact_id": png},
                                                                                    {"artifact_id": png2}]},
                 "send_prepared_album", "send"),
                ("archive", "prepare_chat_archive", {"chat": FIRST_CHAT, "target_list": "archive"},
                 "apply_prepared_archive", "change"),
                ("text_edit", "prepare_text_edit", {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE,
                                                    "new_text": "Matrix family edit"}, "apply_prepared_text_edit",
                 "change"),
                ("caption_edit", "prepare_caption_edit", {"chat": FIRST_CHAT, "message_id": OWN_DOCUMENT_MESSAGE,
                                                          "new_caption": "Matrix family caption"},
                 "apply_prepared_caption_edit", "change"),
                ("pin", "prepare_pin_change", {"chat": FIRST_CHAT, "message_id": SOURCE_MESSAGE, "action": "pin"},
                 "apply_prepared_pin_change", "change"),
                ("delete", "prepare_message_delete", {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE},
                 "apply_prepared_message_delete", "delete"),
            )
            for family, prepare, arguments, send, verb in families:
                before = len(self.owner_prompts(family=family))
                draft = structured(await consumer.call(prepare, arguments))
                draft_id = find_draft(draft)
                cell.expect(f"{family}:prepared", draft.get("status") == "prepared" and draft_id is not None)
                result = structured(await consumer.call(send, {"draft_id": draft_id, "approved": True}))
                cell.expect(f"{family}:declined", result.get("status") == "declined"
                            and result.get("detail") == APPROVAL_DETAILS["declined"])
                prompts = self.owner_prompts(family=family)[before:]
                cell.expect(f"{family}:dialog", len(prompts) == 1 and prompts[0].get("rendered") is True
                            and prompts[0].get("rows", 0) > 0 and prompts[0].get("verb") == verb
                            and prompts[0].get("outcome") == "declined" and prompts[0].get("prompt") == "other")
            cell.expect("no_dispatch", self.dispatches() == 0)

    async def cell_cursor_list_drafts(self, cell) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()
            drafts = [find_draft(structured(await consumer.call("prepare_text_send", {
                "recipient": FIRST_CHAT, "text": f"Matrix listed draft {index}"}))) for index in (1, 2)]
            cell.expect("prepared", all(drafts) and len(set(drafts)) == 2)
            first = structured(await consumer.call("list_drafts", {"limit": 1}))
            rows = [row.get("draft_id") for row in first.get("drafts", [])]
            cell.expect("first_page", first.get("status") == "listed" and len(rows) == 1 and first.get("has_more") is True)
            second = structured(await consumer.call("list_drafts", {"limit": 1, "after_draft_id": rows[0] if rows else None}))
            more = [row.get("draft_id") for row in second.get("drafts", [])]
            cell.expect("second_page", second.get("status") == "listed" and len(more) == 1)
            cell.expect("both", sorted(rows + more) == sorted(drafts))
            cell.expect("end", second.get("has_more") is False)

    async def cell_refusal_invalid_arguments_every_tool(self, cell, consumer, tools) -> None:
        listed = {tool.name: tuple(sorted(tool.input_schema.get("required", []))) for tool in tools}
        cell.expect("listed_required", listed == TOOL_REQUIRED)
        for name in sorted(TOOL_REQUIRED):
            cell.expect(name, error_text(await consumer.call(name, {"matrix_probe": 1})) == argument_text(name))

    async def cell_image_attachment_page(self, cell, consumer, tools) -> None:
        artifact = await self.artifact(consumer, "matrix-pages.pdf", synthetic_pdf("Matrix synthetic page one", PDF_TEXT))
        result = await consumer.call("read_attachment_page", {"artifact_id": artifact, "pages": [2],
                                                              "render_pages": True})
        images = [block for block in result.content if block.type == "image"]
        cell.expect("not_error", result.is_error is False)
        cell.expect("text_first", result.content and result.content[0].type == "text"
                    and json.loads(result.content[0].text) == result.structured_content)
        text = json.dumps(result.structured_content or {}, ensure_ascii=False)
        cell.expect("page_two", PDF_TEXT in text and "Matrix synthetic page one" not in text)
        cell.expect("one_image", len(images) == 1 and images[0].mime_type == "image/png")
        cell.expect("png", images and base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n"))

    async def paged(self, consumer, tool: str, arguments: dict, rows) -> tuple[list, list, str | None, dict]:
        """Follow next_cursor (at most 8 pages): every row, each page's size, the first cursor, the last page."""
        collected, sizes, first_cursor, page, cursor = [], [], None, {}, None
        for _ in range(8):
            page = structured(await consumer.call(tool, {**arguments, **({"cursor": cursor} if cursor else {})}))
            found = rows(page)
            collected += found
            sizes.append(len(found))
            cursor = page.get("next_cursor")
            first_cursor = first_cursor or cursor
            if not cursor:
                break
        return collected, sizes, first_cursor, page

    async def cursor_cell(self, cell, consumer, tool: str, arguments: dict, rows) -> None:
        collected, sizes, first_cursor, last = await self.paged(consumer, tool, arguments, rows)
        cell.expect("rows", collected == [OWN_DOCUMENT_MESSAGE, OWN_TEXT_MESSAGE, SOURCE_MESSAGE])
        cell.expect("page_size", sizes and max(sizes) == 1)
        cell.expect("cursor", isinstance(first_cursor, str) and len(sizes) >= 3)
        cell.expect("end", last.get("next_cursor") is None)
        replay = structured(await consumer.call(tool, {**arguments, "cursor": first_cursor}))
        cell.expect("replay", replay.get("status") == "invalid_cursor" and not rows(replay))
        cell.expect("next_action", replay.get("next_action") == "restart_listing")

    async def cell_cursor_read_history(self, cell, consumer, tools) -> None:
        await self.cursor_cell(cell, consumer, "read_history", {"target": FIRST_CHAT, "mode": "latest", "limit": 1},
                               lambda page: [row.get("anchor", {}).get("message_id") for row in page.get("results", [])])

    async def cell_cursor_search_messages(self, cell, consumer, tools) -> None:
        await self.cursor_cell(cell, consumer, "search_messages", {"target": FIRST_CHAT, "query": "matrix",
                                                                   "mode": "latest", "limit": 1},
                               lambda page: [row.get("anchor", {}).get("message_id") for row in page.get("results", [])])

    async def cell_caps_input_bounds_058(self, cell, consumer, tools) -> None:
        anchor = {"chat_id": FIRST_CHAT, "message_id": SOURCE_MESSAGE}
        item = {"artifact_id": FAKE_ARTIFACT}
        entities = [{"offset": 0, "length": 1, "type": {"@type": "textEntityTypeBold"}}] * 33
        for name, arguments, parameter in (
                ("discover_targets", {"hypotheses": [f"Matrix {index}" for index in range(6)]}, "hypotheses"),
                ("read_target_messages", {"target_handle": FAKE_TARGET, "message_ids": list(range(1, 22))},
                 "message_ids"),
                ("prepare_album_send", {"recipient": FIRST_CHAT, "items": [item] * 11}, "items"),
                ("prepare_album_send", {"recipient": FIRST_CHAT, "items": [item]}, "items"),
                ("prepare_album_send", {"recipient": FIRST_CHAT, "items": [{**item, "caption": "x" * 1025},
                                                                           {"artifact_id": FAKE_ARTIFACT_2}]}, "items"),
                ("prepare_text_edit", {"chat": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE, "new_text": "x" * 4097},
                 "new_text"),
                ("prepare_caption_edit", {"chat": FIRST_CHAT, "message_id": OWN_DOCUMENT_MESSAGE,
                                          "new_caption": "x" * 1025}, "new_caption"),
                ("prepare_text_send", {"recipient": FIRST_CHAT, "text": "x" * 40, "outgoing_entities": entities},
                 "outgoing_entities"),
                ("list_drafts", {"limit": 51}, "limit"),
                ("read_reply_chain", {"anchor": anchor, "max_depth": 11}, "max_depth"),
                ("retain_selection", {"source": {"kind": "messages", "anchors": [anchor] * 21}}, "source"),
                ("search_chats", {"targets": [FIRST_CHAT + index for index in range(6)], "query": "matrix",
                                  "mode": "latest"}, "targets"),
                ("read_spreadsheet", {"artifact_id": FAKE_ARTIFACT, "max_cells": 201}, "max_cells")):
            cell.expect(f"{name}:{parameter}", error_text(await consumer.call(name, arguments))
                        == INVALID_ARGUMENTS.format(name, "invalid " + parameter))

    async def cell_approval_timeout(self, cell, consumer, tools) -> None:
        draft_id = await self.prepared(cell, consumer, TIMEOUT_PROBE)
        result = structured(await consumer.call("send_prepared_text", {"draft_id": draft_id, "approved": True}))
        cell.expect("status", result.get("status") == "timeout")
        cell.expect("detail", result.get("detail") == APPROVAL_DETAILS["timeout"])
        prompts = self.owner_prompts(prompt="timeout")
        cell.expect("one_dialog", len(prompts) == 1 and prompts[0].get("rendered") is True
                    and prompts[0].get("outcome") == "timeout" and prompts[0].get("family") == "text")
        cell.expect("no_dispatch", self.dispatches() == 0)
        status = structured(await consumer.call("get_send_status", {"draft_id": draft_id}))
        cell.expect("still_pending", status.get("status") == "pending" and status.get("evidence") == "local_pending")

    # -- other sessions -------------------------------------------------------------------------------
    async def restricted(self) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            tools = await consumer.tools()

            async def capability_disabled(cell):
                names = {tool.name for tool in tools}
                cell.expect("unlisted", "read_history" not in names and "list_chats" not in names)
                manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
                cell.expect("compatible", manifest.get("compatibility", {}).get("status") == "compatible")
                for name, arguments, capability, profile in (
                        ("read_history", {"target": FIRST_CHAT, "mode": "latest"}, "read_history", "readers"),
                        ("list_chats", {"scope": "main", "limit": 1}, "list_chats", "readers"),
                        ("prepare_forward_send", {"source_chat": FIRST_CHAT, "source_message_id": SOURCE_MESSAGE,
                                                  "recipient": FIRST_CHAT, "mode": "copy"}, "forward_send", "forward")):
                    text = error_text(await consumer.call(name, arguments))
                    cell.expect(f"{name}:text", text == CAPABILITY_DISABLED.format(name, capability, profile))

            async def prompt_policy(cell):
                prompts = await consumer.prompts()
                cell.expect("unlisted", "unread_overview" not in {prompt.name for prompt in prompts})
                cell.expect("refused", await prompt_error(client, "unread_overview", None) == "prompt_unavailable")

            async def enable_capability(cell):
                result = structured(await consumer.call("prepare_reply_text_send", {
                    "recipient": FIRST_CHAT, "text": "Matrix synthetic reply",
                    "reply_to": {"chat_id": FIRST_CHAT, "message_id": SOURCE_MESSAGE}}))
                cell.expect("status", result.get("status") == "capability_disabled")
                cell.expect("missing", result.get("missing_capabilities") == SOURCE_MISSING)
                cell.expect("next_action", result.get("next_action") == "enable_capability")
                cell.expect("no_draft", result.get("reply") is None)
                cell.expect("no_dispatch", self.sends() == 0)

            async def guard_profiles(cell):
                names = {tool.name for tool in tools}
                cell.expect("unlisted", names == set(self.census["tools"]) - set(RESTRICTED_GUARDS))
                for name, (missing, profile) in RESTRICTED_GUARDS.items():
                    text = error_text(await consumer.call(name, guard_arguments(name)))
                    cell.expect(name, text == guard_text(name, missing, profile))
                # A selection adds retained_selections to a listed tool's requirement.
                text = error_text(await consumer.call("read_attachment", {"selection_id": FAKE_SELECTION}))
                cell.expect("selection", text == guard_text("read_attachment", ("retained_selections",), _READERS))

            async def reply_families(cell):
                png = await self.artifact(consumer, "matrix-restricted.png", synthetic_png())
                own = {"chat_id": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE}
                probes = [(f"family_{message_id}", "prepare_reply_text_send", {
                    "recipient": FAMILY_CHAT, "text": "Matrix synthetic reply",
                    "reply_to": {"chat_id": FAMILY_CHAT, "message_id": message_id}}, missing)
                    for message_id, _, _, _, _, missing in FAMILY_SOURCES]
                probes += [("styled_text", "prepare_reply_text_send", {"recipient": FIRST_CHAT, "text": "Matrix reply",
                                                                       "reply_to": own, "outgoing_entities": STYLED},
                            ["formatted_text_send"]),
                           ("styled_caption", "prepare_reply_artifact_send", {
                               "recipient": FIRST_CHAT, "artifact_id": png, "display_name": "matrix-restricted.png",
                               "mime_type": "image/png", "caption": "Matrix caption", "outgoing_entities": STYLED,
                               "reply_to": own}, ["formatted_caption_send"])]
                for label, tool, arguments, missing in probes:
                    result = structured(await consumer.call(tool, arguments))
                    cell.expect(f"{label}:missing", result.get("status") == "capability_disabled"
                                and result.get("missing_capabilities") == missing)
                    cell.expect(f"{label}:next_action", result.get("next_action") == "enable_capability"
                                and result.get("reply") is None and find_draft(result) is None)

            await self.run_cell("refusal.capability_disabled", capability_disabled)
            await self.run_cell("refusal.guard_profiles", guard_profiles)
            await self.run_cell("refusal.reply_families", reply_families)
            await self.run_cell("refusal.prompt_policy", prompt_policy)
            await self.run_cell("recovery.enable_capability", enable_capability)

    async def closed(self) -> None:
        """No capability at all (and no broker: every guard refuses before the provider)."""
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)

            async def guard_every_tool(cell):
                tools = await consumer.tools()
                cell.expect("listing", [tool.name for tool in tools] == ["_manifest"])
                cell.expect("prompts", [prompt.name for prompt in await consumer.prompts()] == ["check_connection"])
                for name in sorted(TOOL_GUARDS):
                    text = error_text(await consumer.call(name, guard_arguments(name)))
                    cell.expect(name, text == guard_text(name, *TOOL_GUARDS[name]))
                for name in UPLOAD_AUTHORITY_TOOLS:  # Unlisted, so no listed schema: outside the ledger.
                    result = structured(await client.call_tool(name, guard_arguments(name)))
                    cell.expect(name, result.get("status") == "unavailable")

            await self.run_cell("refusal.guard_every_tool", guard_every_tool)

    async def stale(self) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)

            async def config_stale(cell):
                tools = await consumer.tools()
                cell.expect("listing", [tool.name for tool in tools] == ["_manifest"])
                cell.expect("prompts", [prompt.name for prompt in await consumer.prompts()] == ["check_connection"])
                text = error_text(await consumer.call("list_chats", {"scope": "main", "limit": 1}))
                cell.expect("text", text == CONFIG_STALE.format("list_chats"))

            async def update_installation(cell):
                manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
                compatibility = manifest.get("compatibility", {})
                cell.expect("status", compatibility.get("status") == "config_stale")
                cell.expect("next_action", compatibility.get("next_action") == "update_installation")
                cell.expect("nothing_available", manifest.get("available_tools") == ["_manifest"])

            await self.run_cell("refusal.config_stale", config_stale)
            await self.run_cell("recovery.update_installation", update_installation)

    async def offline(self) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()

            async def check_connection(cell):
                manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
                compatibility = manifest.get("compatibility", {})
                cell.expect("manifest_status", compatibility.get("status") == "broker_unavailable")
                cell.expect("manifest_next_action", compatibility.get("next_action") == "check_connection")
                listing = structured(await consumer.call("list_chats", {"scope": "main", "limit": 1}))
                cell.expect("listing_status", listing.get("status") == "error"
                            and listing.get("stop_reason") == "broker_unavailable")
                cell.expect("listing_next_action", listing.get("next_action") == "check_connection")

            await self.run_cell("recovery.check_connection", check_connection)

    async def unauthorized(self) -> None:
        async with self.client(mode="legacy") as client:
            consumer = Consumer(client, self.ledger)
            await consumer.tools()

            async def connect_account(cell):
                manifest = structured(await consumer.call("_manifest", {"check_broker": True}))
                cell.expect("compatible", manifest.get("compatibility", {}).get("status") == "compatible")
                result = structured(await consumer.call("resolve_target", {"target": "@" + PUBLIC_USERNAME}))
                cell.expect("blocked", result.get("status") == "blocked")
                cell.expect("next_action", result.get("next_action") == "connect_account")

            await self.run_cell("recovery.connect_account", connect_account)

    async def bundle(self) -> None:
        """The installed bundle installer over a synthetic home: first install of an older release, upgrade to this
        package's release, rollback, uninstall. No proxy and no broker; no process starts."""
        if self.plan.get("installer_path"):
            sys.path.insert(1, self.plan["installer_path"])
        from unittest import mock
        import telegram_search_mcp
        current = telegram_search_mcp.__version__
        world = BundleWorld(Path(os.path.realpath(self.plan["tmp"])) / "bundle")  # no symlinked ancestor
        packets: dict = {}
        state: dict = {}
        world.clients()
        world.session(BUNDLE_OLD_VERSION)
        platform = mock.patch("telegram_search_mcp.bundle_install.require_platform")

        async def install(cell):
            packets["old"] = world.release("release-old", BUNDLE_OLD_VERSION)
            before = world.session_hashes(policy=True)
            with platform, no_other_process():
                result = world.install(packets["old"], BUNDLE_OLD_VERSION)
            old_root = str(world.runtime / "releases" / packets["old"][2])
            state["old_root"] = old_root
            command = str(Path(old_root) / "python/bin/python3")
            cell.expect("status", (result.get("status"), result.get("installation")) == ("activation_required",
                                                                                          "installed"))
            cell.expect("pointer", world.pointer() == old_root == result.get("release_root"))
            cell.expect("receipt", world.receipt().get("schema_version") == 2
                        and world.receipt().get("package_version") == BUNDLE_OLD_VERSION)
            cell.expect("codex", world.codex_command() == command)
            cell.expect("desktop", world.entry().get("command") == command)
            cell.expect("neighbours", world.neighbours_kept())
            cell.expect("skills", world.skill() == b"search release-old" and "bootstrap" not in world.launchctl.verbs)
            cell.expect("session", world.session_hashes(policy=True) == before)
            world.service(old_root)  # The activation step's broker definition, loaded.

        async def upgrade(cell):
            packets["new"] = world.release("release-new", current)
            before = world.session_hashes(policy=False)
            world.launchctl.verbs.clear()
            with platform, no_other_process():
                result = world.install(packets["new"], current)
            new_root = str(world.runtime / "releases" / packets["new"][2])
            state["new_root"] = new_root
            command = str(Path(new_root) / "python/bin/python3")
            cell.expect("status", (result.get("status"), result.get("installation"), result.get("service"))
                        == ("upgraded", "upgraded", "restarted"))
            cell.expect("pointer", world.pointer() == new_root)
            cell.expect("definition", world.program() == str(Path(new_root) / "launcher.py"))
            cell.expect("restarted", world.launchctl.loaded is not None and world.launchctl.loaded[0] == command
                        and world.launchctl.verbs.index("bootout") < world.launchctl.verbs.index("bootstrap"))
            cell.expect("clients", world.codex_command() == command
                        and world.entry().get("command") == command
                        and world.neighbours_kept())
            cell.expect("pin", world.pin() == current)
            cell.expect("session", world.session_hashes(policy=False) == before)
            cell.expect("kept", Path(state.get("old_root", "/nonexistent")).is_dir())
            previous = world.receipt().get("previous") or {}
            cell.expect("previous", previous.get("package_version") == BUNDLE_OLD_VERSION
                        and previous.get("release_root") == state.get("old_root"))

        async def rollback(cell):
            before = world.session_hashes(policy=False)
            world.launchctl.verbs.clear()
            with platform, no_other_process():
                result = world.change("rollback")
            old_root = state.get("old_root", "/nonexistent")
            command = str(Path(old_root) / "python/bin/python3")
            cell.expect("status", (result.get("status"), result.get("service")) == ("rolled_back", "restarted"))
            cell.expect("pointer", world.pointer() == old_root)
            cell.expect("definition", world.program() == str(Path(old_root) / "launcher.py")
                        and world.launchctl.loaded is not None and world.launchctl.loaded[0] == command)
            cell.expect("clients", world.codex_command() == command
                        and world.entry().get("command") == command
                        and world.neighbours_kept() and world.skill() == b"search release-old")
            cell.expect("pin", world.pin() == BUNDLE_OLD_VERSION)
            cell.expect("session", world.session_hashes(policy=False) == before)
            cell.expect("kept", Path(state.get("new_root", "/nonexistent")).is_dir())

        async def uninstall(cell):
            before = world.session_hashes(policy=True)
            world.launchctl.verbs.clear()
            with platform, no_other_process():
                result = world.change("uninstall")
            cell.expect("status", (result.get("status"), result.get("installation"), result.get("service"),
                                   result.get("codex"), result.get("claude_desktop"))
                        == ("uninstalled", "removed", "removed", "removed", "removed"))
            cell.expect("root", not world.runtime.exists())
            cell.expect("service", not world.plist.exists() and world.launchctl.loaded is None
                        and "bootout" in world.launchctl.verbs)
            cell.expect("codex", (world.codex / "config.toml").read_bytes().rstrip(b"\n")
                        == BUNDLE_NEIGHBOUR_CODEX.rstrip(b"\n"))
            cell.expect("desktop", world.server not in world.desktop() and world.neighbours_kept())
            cell.expect("skills", not world.skills.exists() or not any(world.skills.iterdir()))
            cell.expect("session", world.session_hashes(policy=True) == before)
            with platform, no_other_process():
                again = world.change("uninstall")
            cell.expect("repeat", again == {"status": "uninstalled", "installation": "absent"})

        for name, body in (("bundle.install", install), ("bundle.upgrade", upgrade), ("bundle.rollback", rollback),
                           ("bundle.uninstall", uninstall)):
            await self.run_cell(name, body)

    # -- the Inspector column -----------------------------------------------------------------------------
    def inspector_env(self) -> dict:
        node = Path(self.plan["inspector"]["node"])
        return {"HOME": self.plan["home"], "PATH": f"{node.parent}:{SYSTEM_PATH}", "LC_ALL": "C",
                "TMPDIR": self.plan["tmp"], **({"PYTHONPATH": self.plan["pythonpath"]} if self.plan.get("pythonpath")
                                               else {})}

    def inspector_cli(self, *arguments: str) -> object:
        """One scripted CLI invocation (its own MCP session and proxy): the parsed JSON it printed, else None. It runs
        from the package's build directory, where the CLI finds its own package.json."""
        spec, proxy = self.plan["inspector"], self.plan["proxy"]
        command = [spec["node"], spec["cli"], proxy[0], *arguments, *(("--", *proxy[1:]) if proxy[1:] else ())]
        try:
            done = subprocess.run(command, cwd=str(Path(spec["cli"]).parent), env=self.inspector_env(),
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=INSPECTOR_SECONDS)
            return json.loads(done.stdout.decode("utf-8")) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            return None

    def inspector_call(self, tool: str, **arguments) -> dict:
        """tools/call through the CLI; each argument as the CLI takes it, key=JSON."""
        pairs = [f"{key}={json.dumps(value)}" for key, value in arguments.items()]
        value = self.inspector_cli("--method", "tools/call", "--tool-name", tool,
                                   *(("--tool-arg", *pairs) if pairs else ()))
        return value if isinstance(value, dict) else {}

    def inspector_session(self, calls: list) -> list:
        """Several calls in one MCP session through the Inspector CLI's own client module (connect, callTool,
        disconnect and createTransport, with the SDK Client the CLI uses): the CLI binary is one call per process,
        and a draft belongs to the session that prepared it."""
        spec, proxy = self.plan["inspector"], self.plan["proxy"]
        script = Path(self.plan["tmp"]) / "inspector-session.mjs"
        script.write_text(INSPECTOR_SESSION_SCRIPT, encoding="utf-8")
        request = Path(self.plan["tmp"]) / "inspector-session.json"
        request.write_text(json.dumps({"cli": str(Path(spec["cli"]).parents[1]), "sdk_client": spec["sdk_client"],
                                       "version": spec["version"], "command": proxy[0], "args": proxy[1:],
                                       "calls": calls}), encoding="utf-8")
        try:
            done = subprocess.run([spec["node"], str(script), str(request)], cwd=self.plan["tmp"],
                                  env=self.inspector_env(), stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=INSPECTOR_SECONDS)
            value = json.loads(done.stdout.decode("utf-8")) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            value = None
        return value if isinstance(value, list) else []

    async def inspector(self) -> None:
        from jsonschema.validators import validator_for
        async with self.client(mode="legacy") as client:
            sdk_tools = {tool.name: tool for tool in await Consumer(client, self.ledger).tools()}
        listed: dict = {}

        async def census(cell):
            value = self.inspector_cli("--method", "tools/list")
            tools = value.get("tools", []) if isinstance(value, dict) else []
            listed.update({tool.get("name"): tool for tool in tools if isinstance(tool, dict)})
            cell.expect("names", sorted(listed) == self.census["tools"] and len(tools) == len(listed))
            cell.expect("schemas", all(name in sdk_tools and tool.get("inputSchema") == sdk_tools[name].input_schema
                                       and tool.get("outputSchema") == sdk_tools[name].output_schema
                                       for name, tool in listed.items()) and bool(listed))
            cell.expect("titles", all(tool.get("title") == sdk_tools[name].title for name, tool in listed.items()
                                      if name in sdk_tools) and bool(listed))
            manifest = self.inspector_call("_manifest", check_broker=True).get("structuredContent") or {}
            cell.expect("compatible", manifest.get("compatibility", {}).get("status") == "compatible")
            cell.expect("fingerprint", manifest.get("schema_fingerprint") == self.census["schema_fingerprint"])

        async def read(cell):
            result = self.inspector_call("list_chats", scope="main", limit=LIST_PAGE)
            page = result.get("structuredContent") or {}
            cell.expect("not_error", result.get("isError") is False)
            cell.expect("rows", page.get("status") == "page" and [row.get("chat_id") for row in page.get("results", [])]
                        == list(range(FIRST_CHAT, FIRST_CHAT + LIST_PAGE)))
            cell.expect("cursor", isinstance(page.get("next_cursor"), str))
            schema = (listed.get("list_chats") or {}).get("outputSchema")
            cell.expect("schema", isinstance(schema, dict) and not any(True for _ in validator_for(schema)(schema)
                                                                       .iter_errors(page)))
            content = result.get("content") or [{}]
            cell.expect("text_first", content[0].get("type") == "text"
                        and json.loads(content[0].get("text") or "null") == page)

        async def refusal(cell):
            result = self.inspector_call("list_chats", scope="everywhere", limit=1)
            content = result.get("content") or [{}]
            cell.expect("text", result.get("isError") is True and len(content) == 1
                        and content[0].get("text") == INVALID_ARGUMENTS.format("list_chats", "invalid scope"))
            cell.expect("no_structured", "structuredContent" not in result)

        async def approval_denial(cell):
            prepared, sent, status = (self.inspector_session([
                ["prepare_text_send", {"recipient": FIRST_CHAT, "text": DENIAL_PROBE}],
                ["send_prepared_text", {"approved": True}], ["get_send_status", {}]]) + [{}, {}, {}])[:3]
            draft = (prepared.get("structuredContent") or {})
            result = sent.get("structuredContent") or {}
            cell.expect("prepared", draft.get("status") == "prepared" and isinstance(draft.get("draft_id"), str))
            cell.expect("declined", result.get("status") == "declined")
            cell.expect("detail", result.get("detail") == APPROVAL_DETAILS["declined"])
            prompts = self.owner_prompts(prompt="denial")
            cell.expect("one_dialog", len(prompts) == 1 and prompts[0].get("rendered") is True)
            cell.expect("still_pending", (status.get("structuredContent") or {}).get("status") == "pending")
            later = self.inspector_call("send_prepared_text", draft_id=draft.get("draft_id"), approved=True)
            cell.expect("other_session", (later.get("structuredContent") or {}).get("status") == "expired")
            cell.expect("no_other_dialog", len(self.owner_prompts()) == 1)
            cell.expect("no_dispatch", self.dispatches() == 0)

        for name, body in (("inspector.census", census), ("inspector.read", read), ("inspector.refusal", refusal),
                           ("inspector.approval_denial", approval_denial)):
            await self.run_cell(name, body)

    async def listing(self) -> None:
        """No cell: the full policy's tool listing without a broker, which check re-derives from its own
        reinstall and compares with the full session's recorded listing_sha256."""
        async with self.client(mode="legacy") as client:
            self.observed["listing_sha256"] = listing_digest(await Consumer(client, self.ledger).tools())

    async def run(self) -> dict:
        await getattr(self, self.plan["session"])()
        import importlib.metadata
        return {"session": self.plan["session"], "cells": self.cells, "schemas": self.ledger.summary(),
                "observed": self.observed, "sdk": {"distribution": SDK_DISTRIBUTION,
                                                   "version": importlib.metadata.version(SDK_DISTRIBUTION)}}



# --- the bundle session (inside the temporary venv: the installed bundle installer, synthetic homes) --------

BUNDLE_OLD_VERSION = "0.57.9"
BUNDLE_NEIGHBOUR_CODEX = b'# a neighbour the installer keeps\nmodel = "example"\n'
BUNDLE_NEIGHBOUR_DESKTOP = {"mcpServers": {"weather": {"command": "/opt/weather", "args": []}}}


class FakeLaunchctl:
    """`launchctl print|bootout|bootstrap` over the one broker label of a synthetic home, as the installer tests
    drive it; the loaded job is the plist read at bootstrap. Any other verb answers an error."""

    def __init__(self, plist: Path):
        self.plist, self.loaded, self.verbs = plist, None, []

    def load(self) -> None:
        import plistlib
        self.loaded = plistlib.loads(self.plist.read_bytes())["ProgramArguments"]

    def __call__(self, argv, **kwargs):
        verb = argv[1] if len(argv) > 1 and argv[0] == "/bin/launchctl" else "invalid"
        self.verbs.append(verb)
        if verb == "print":
            if self.loaded is None:
                return subprocess.CompletedProcess(argv, 113, "", "")
            text = "\n".join(["%s = {" % argv[2], "\tpath = %s" % self.plist, "\tprogram = %s" % self.loaded[0],
                              "\targuments = {", *("\t\t" + part for part in self.loaded), "\t}", "}"])
            return subprocess.CompletedProcess(argv, 0, text, "")
        if verb == "bootout":
            if self.loaded is None:
                return subprocess.CompletedProcess(argv, 3, "", "")
            self.loaded = None
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "bootstrap" and argv[3:4] == [str(self.plist)]:
            self.load()
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 64, "", "")


class BundleWorld:
    """One synthetic user home, two synthetic admitted releases (an older one and this package's version) and fake
    service and pending-send boundaries; the installed bundle_install module does everything else."""

    def __init__(self, base: Path):
        from telegram_search_mcp import bundle_install
        from telegram_search_mcp.config import _owner_paths, server_name
        self.module, self.base = bundle_install, base
        self.server = server_name()  # The client entries' name, read through the product identity.
        self.home = base / "user"
        self.home.mkdir(mode=0o700, parents=True)
        self.runtime, self.codex = self.home / "runtime", self.home / ".codex"
        self.skills = self.home / ".agents" / "skills"
        self.session_root, _, self.label, self.plist = _owner_paths(self.home)
        self.launchctl = FakeLaunchctl(self.plist)

    @property
    def desktop_config(self) -> Path:
        return self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"

    def clients(self) -> None:
        """A Codex config and a Claude Desktop config, each with a neighbour the installer must keep."""
        self.codex.mkdir(mode=0o700)
        (self.codex / "config.toml").write_bytes(BUNDLE_NEIGHBOUR_CODEX)
        (self.codex / "config.toml").chmod(0o600)
        self.desktop_config.parent.mkdir(mode=0o700, parents=True)
        self.desktop_config.write_text(json.dumps(BUNDLE_NEIGHBOUR_DESKTOP, indent=2) + "\n")
        self.desktop_config.chmod(0o644)

    def session(self, pin: str) -> None:
        """The account's session tree: never part of any install, upgrade, rollback or uninstall but its pin."""
        files = {"enrollment.json": b'{"synthetic": "enrollment"}\n',
                 "runtime.toml": ('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                                  'enabled_capabilities = ["read", "artifacts", "send"]\n' % pin).encode(),
                 "tdlib/td.binlog": b"synthetic-session-bytes"}
        for name, data in files.items():
            path = self.session_root / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)

    def session_hashes(self, *, policy: bool) -> dict:
        return {str(path.relative_to(self.session_root)): _sha(path.read_bytes())
                for path in sorted(self.session_root.rglob("*"))
                if path.is_file() and (policy or path.name != "runtime.toml")}

    def pin(self) -> str | None:
        match = re.search(r'expected_package_version = "([^"]+)"', (self.session_root / "runtime.toml").read_text())
        return match.group(1) if match else None

    def release(self, name: str, version: str) -> tuple:
        """An admitted release tree and its index, as the installer tests build them (inert bytes only)."""
        from telegram_search_mcp.native_runtime import COMMIT, NAMES, REQUIRED_SYMBOLS, SCHEMA_SHA256
        release = self.base / name
        release.mkdir(mode=0o700)
        natives = {native: (native.encode() + b"-inert-native-bytes-padding-0123456789")[:48].ljust(48, b".")
                   for native in NAMES}
        lock = dict(manifest_version=1, target="macos-arm64", platform="Darwin", architecture="arm64",
                    minimum_os="26.0", source_commit=COMMIT, tdlib_version="1.8.67", schema_sha256=SCHEMA_SHA256,
                    abi="legacy-td_json_client", required_symbols=list(REQUIRED_SYMBOLS),
                    signature="adhoc-local-integrity-only",
                    artifacts=[dict(name=native, bytes=len(natives[native]), sha256=_sha(natives[native]),
                                    dependencies=[]) for native in NAMES],
                    build=dict(method="homebrew-receipt-relocated", reproducibility="unverified", compiler="clang",
                               build_os="synthetic", xcode="synthetic", clt="synthetic", used_options=[],
                               known_formula_flags=[], implicit_flags="homebrew-std_cmake_args-not-captured",
                               openssl_version="3.6.4"),
                    notices=dict(status="pending-public-distribution-review", tdlib_license="BSL-1.0",
                                 tdlib_source="https://github.com/tdlib/td",
                                 tdlib_license_source="https://github.com/tdlib/td/blob/"
                                                      "d1085f9cebc5a62379991ae1652673954f229c1f/LICENSE_1_0.txt",
                                 openssl_license="Apache-2.0", source_rebuild="pending",
                                 publisher_signature="unavailable"))
        files = {"launcher.py": f"# inert launcher {name}\n".encode(), "python/bin/python3": b"inert-runtime",
                 "app/telegram_search_mcp/__init__.py": f'__version__ = "{version}"\n'.encode(),
                 "native/libtdjson.dylib": b"inert-native", "skills/telegram-search/SKILL.md": f"search {name}".encode(),
                 "skills/telegram-setup/SKILL.md": f"setup {name}".encode(), "tdlib-lock.json": json.dumps(lock).encode()}
        manifest = dict(schema_version=1, package_version=version, target="macos-arm64", minimum_os="26.0",
                        contract_version=1, schema_fingerprint="a" * 64, tools=["_manifest", "search_correspondence"],
                        native_lock_sha256=_sha(files["tdlib-lock.json"]),
                        skills=[dict(id=skill, sha256=_sha(files["skills/" + skill + "/SKILL.md"]))
                                for skill in ("telegram-search", "telegram-setup")],
                        source_completeness="incomplete", publication="unreleased")
        files["manifest/release.json"] = json.dumps(manifest).encode()
        records = []
        for path_name, data in sorted(files.items()):
            path = release / path_name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            executable = path_name == "python/bin/python3"
            path.chmod(0o500 if executable else 0o400)
            records.append(dict(path=path_name, bytes=len(data), sha256=_sha(data), executable=executable))
        for parent, _, _ in os.walk(release):
            Path(parent).chmod(0o700)
        index = dict(schema_version=1, target="macos-arm64", release_version=version, archive_bytes=1,
                     archive_sha256="b" * 64, unpacked_bytes=sum(len(data) for data in files.values()), files=records)
        index_path = self.base / (name + "-index.json")
        index_path.write_bytes(json.dumps(index).encode())
        index_path.chmod(0o400)
        return release, index_path, _sha(index_path.read_bytes())

    def boundaries(self) -> dict:
        return {"launchctl": self.launchctl, "pending_sends": lambda home, python, app: "none"}

    def install(self, packet: tuple, version: str) -> dict:
        from unittest import mock
        release, index, pin = packet
        with mock.patch("telegram_search_mcp.__version__", version):
            return self.module.install(release, index, pin, installation_root=self.runtime, codex_home=self.codex,
                                       skills_root=self.skills, user_home=self.home, claude_code="skip",
                                       **self.boundaries())

    def change(self, action: str) -> dict:
        return getattr(self.module, action)(self.runtime, user_home=self.home, **self.boundaries())

    def service(self, release_root: str) -> None:
        """The broker definition the activation step writes, loaded as launchd would hold it."""
        import plistlib
        payload = self.module._service_payload(Path(release_root), self.label)
        self.plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps(payload))
        self.plist.chmod(0o600)
        self.launchctl.load()

    def pointer(self) -> str | None:
        try:
            return json.loads((self.runtime / "current.json").read_text())["release_root"]
        except (OSError, ValueError, KeyError):
            return None

    def receipt(self) -> dict:
        try:
            return json.loads((self.runtime / "receipt.json").read_text())
        except (OSError, ValueError):
            return {}

    def program(self) -> str | None:
        """The launcher the broker definition names."""
        import plistlib
        try:
            return plistlib.loads(self.plist.read_bytes())["ProgramArguments"][4]
        except (OSError, ValueError, KeyError, IndexError):
            return None

    def codex_command(self) -> str | None:
        text = (self.codex / "config.toml").read_text()
        match = re.search(r"\[mcp_servers\." + re.escape(self.server) + r'\][^\[]*?command = "([^"]+)"', text, re.S)
        return match.group(1) if match else None

    def desktop(self) -> dict:
        return json.loads(self.desktop_config.read_text()).get("mcpServers", {})

    def entry(self) -> dict:
        """This server's Claude Desktop entry, or {}."""
        return self.desktop().get(self.server) or {}

    def neighbours_kept(self) -> bool:
        return ((self.codex / "config.toml").read_bytes().startswith(BUNDLE_NEIGHBOUR_CODEX)
                and self.desktop().get("weather") == BUNDLE_NEIGHBOUR_DESKTOP["mcpServers"]["weather"]
                and stat.S_IMODE(self.desktop_config.stat().st_mode) == 0o644)

    def skill(self) -> bytes | None:
        path = self.skills / "telegram-search" / "SKILL.md"
        return path.read_bytes() if path.is_file() else None


def no_other_process():
    """Every process an installer step could start is refused: launchd and pending sends go through the fakes."""
    from unittest import mock
    from contextlib import ExitStack
    stack = ExitStack()
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        stack.enter_context(mock.patch(f"subprocess.{name}", side_effect=AssertionError("unexpected process")))
    return stack


def drive_main(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    import mcp
    if plan.get("sdk_root") and not Path(mcp.__file__).resolve().is_relative_to(Path(plan["sdk_root"]).resolve()):
        print("DRIVER_SDK_MISMATCH", file=sys.stderr)
        return 3
    result = asyncio.run(asyncio.wait_for(Driver(plan).run(), DRIVE_SECONDS))
    Path(args.out).write_bytes(canonical(result))
    return 0


# --- receipt ----------------------------------------------------------------------------------------------

def output_schema_cell(sessions: list[dict], census: dict) -> dict:
    validated = sum(session["schemas"]["validated"] for session in sessions)
    failures = sorted({failure for session in sessions for failure in session["schemas"]["failures"]})
    tools = {tool for session in sessions for tool in session["schemas"]["tools"]}
    cell = Cell("output_schemas")
    cell.expect("validated", validated > 0)
    cell.expect("conforming", not failures)
    cell.expect("every_tool", sorted(tools) == sorted(census["tools"]))
    return cell.result()


def merge_cells(sessions: list[dict], census: dict) -> list[dict]:
    """Session results in the closed CELLS order; a missing or repeated cell refuses."""
    found: dict[str, dict] = {}
    for session in sessions:
        for cell in session["cells"]:
            need(cell["id"] in CELL_IDS and cell["id"] not in found, "cell_set_mismatch")
            found[cell["id"]] = cell
    found["output_schemas"] = output_schema_cell(sessions, census)
    need(set(found) == set(CELL_IDS), "cell_set_mismatch", {"missing": sorted(set(CELL_IDS) - set(found))})
    return [found[cell_id] for cell_id in CELL_IDS]


def owner_cells() -> list[dict]:
    return [{"id": cell, "client": client, "provider": provider, "status": "pending"}
            for cell, client, provider in OWNER_CELLS]


def inspector_section(prepared: dict, session: dict | None) -> dict:
    """The receipt's Inspector column: the pinned package, its lock, the Node version and the four cells in
    INSPECTOR_IDS order; or `unavailable` with its fixed reason."""
    if prepared.get("status") != "ready":
        return {"status": "unavailable", "reason": prepared.get("reason")}
    found: dict[str, dict] = {}
    for cell in (session or {}).get("cells", []):
        need(cell["id"] in INSPECTOR_IDS and cell["id"] not in found, "cell_set_mismatch")
        found[cell["id"]] = cell
    need(set(found) == set(INSPECTOR_IDS), "cell_set_mismatch", {"missing": sorted(set(INSPECTOR_IDS) - set(found))})
    return {"status": "pass" if all(found[cell]["status"] == "pass" for cell in INSPECTOR_IDS) else "fail",
            "package": INSPECTOR_PACKAGE, "version": INSPECTOR_VERSION, "integrity": INSPECTOR_INTEGRITY,
            "lock_sha256": prepared["lock_sha256"], "node": prepared["node"],
            "cells": [found[cell] for cell in INSPECTOR_IDS]}


def build_receipt(*, commit: str, tree: str, version: str, wheel: dict, build: dict, install: dict, installer: dict,
                  census: dict, sessions: list[dict], inspector: dict, harness_sha256: str, gate_sha256: str) -> dict:
    cells = merge_cells(sessions, census)
    full = next(session for session in sessions if session["session"] == "full")
    schemas = [session["schemas"] for session in sessions]
    sdk = {"distribution": SDK_DISTRIBUTION, "version": full["sdk"]["version"], "revisions": list(REVISIONS)}
    return {
        "schema": SCHEMA, "acceptance": False,
        "harness": {"path": HARNESS_PATH, "sha256": harness_sha256}, "gate": {"path": GATE_PATH, "sha256": gate_sha256},
        "commit": commit, "tree": tree, "version": version, "wheel": wheel, "build": build, "install": install,
        "installer": installer, "sdk": sdk, "schema_fingerprint": census["schema_fingerprint"],
        "census": {"tool_count": len(census["tools"]), "tools_sha256": tools_sha256(census["tools"]),
                   "capabilities": len(census["capabilities"])},
        "surface": {"listing_sha256": full["observed"].get("listing_sha256"),
                    "structured_results_validated": sum(item["validated"] for item in schemas),
                    "tools_with_validated_results": len({tool for item in schemas for tool in item["tools"]}),
                    "unschematized_tools": sorted({tool for item in schemas for tool in item["unschematized"]})},
        "approval": full["observed"].get("approval"),
        "cells": cells,
        "inspector": inspector,
        "owner_cells": owner_cells(),
        "summary": {"synthetic": len(cells), "passed": sum(cell["status"] == "pass" for cell in cells),
                    "failed": sum(cell["status"] != "pass" for cell in cells), "inspector": inspector["status"],
                    "inspector_cells": sum(cell["status"] == "pass" for cell in inspector.get("cells", [])),
                    "owner_pending": len(OWNER_CELLS)},
    }


def check_inspector(inspector: object) -> None:
    """The Inspector column on its own: exactly the unavailable form with a fixed reason, or the pinned package,
    integrity and a Node of a recorded line with its four cells passed at their frozen counts."""
    need(isinstance(inspector, dict) and inspector.get("status") in ("pass", "unavailable"), "inspector_invalid")
    if inspector["status"] == "unavailable":
        need(set(inspector) == {"status", "reason"} and inspector["reason"] in INSPECTOR_UNAVAILABLE,
             "inspector_invalid")
        return
    node = inspector.get("node")
    need(set(inspector) == {"status", "package", "version", "integrity", "lock_sha256", "node", "cells"}
         and inspector["package"] == INSPECTOR_PACKAGE and inspector["version"] == INSPECTOR_VERSION
         and inspector["integrity"] == INSPECTOR_INTEGRITY
         and isinstance(inspector["lock_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", inspector["lock_sha256"])
         and isinstance(node, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", node)
         and int(node.split(".")[0]) >= INSPECTOR_NODE_MAJOR, "inspector_invalid")
    cells = inspector["cells"]
    need(isinstance(cells, list) and all(isinstance(item, dict) for item in cells)
         and [item.get("id") for item in cells] == list(INSPECTOR_IDS), "cell_set_mismatch")
    need(all(item.get("status") == "pass" for item in cells), "cells_failed")
    need(all(set(item) == {"id", "status", "checks"} and type(item["checks"]) is int
             and item["checks"] == INSPECTOR_CHECKS[item["id"]] for item in cells), "checks_mismatch")


def check_receipt(receipt: object, *, commit: str | None = None, wheel_sha256: str | None = None,
                  census: dict | None = None, committed: dict | None = None, derived: dict | None = None) -> None:
    """Every rule the harness fixes on its own, then each binding that is given; refuses with one fixed code.

    On its own: the closed keys and schema, no acceptance, the schema fingerprint these cells were written for
    (MATRIX_SCHEMA_FINGERPRINT), the closed cell and owner-cell sets, owner cells exactly as OWNER_CELLS lists them
    and pending, every cell passed with exactly {id, status, checks} and its CELL_CHECKS count (REAL_DIALOG_CHECKS
    added under the real dialog), the Inspector column (check_inspector), the approval one of APPROVALS, the
    SURFACE_COUNTS and the summary. Bindings: the commit, the wheel sha256, the census (version, fingerprint,
    tools and counts), and check_fields for every other field."""
    need(isinstance(receipt, dict) and set(receipt) == RECEIPT_KEYS and receipt["schema"] == SCHEMA,
         "receipt_invalid")
    need(receipt["acceptance"] is False, "acceptance_claimed")
    hex40, hex64 = re.compile(r"[0-9a-f]{40}"), re.compile(r"[0-9a-f]{64}")
    wheel = receipt["wheel"]
    need(isinstance(receipt["commit"], str) and hex40.fullmatch(receipt["commit"])
         and isinstance(wheel, dict) and isinstance(wheel.get("sha256"), str) and hex64.fullmatch(wheel["sha256"])
         and isinstance(receipt["schema_fingerprint"], str) and hex64.fullmatch(receipt["schema_fingerprint"])
         and isinstance(receipt["census"], dict) and isinstance(receipt["census"].get("tools_sha256"), str),
         "receipt_invalid")
    need(receipt["schema_fingerprint"] == MATRIX_SCHEMA_FINGERPRINT, "schema_mismatch")
    owner, cells = receipt["owner_cells"], receipt["cells"]
    for items, ids in ((owner, OWNER_IDS), (cells, CELL_IDS)):
        need(isinstance(items, list) and all(isinstance(item, dict) for item in items)
             and [item.get("id") for item in items] == list(ids), "cell_set_mismatch")
    need(all(item.get("status") == "pending" for item in owner), "owner_cell_passed")
    need(owner == owner_cells(), "field_mismatch")
    need(all(item.get("status") == "pass" for item in cells), "cells_failed",
         {"failed": [item.get("id") for item in cells if item.get("status") != "pass"]})
    approval = receipt["approval"]
    need(approval in APPROVALS, "field_mismatch")
    extra = REAL_DIALOG_CHECKS if approval["mode"] == "real_dialog" else {}
    need(all(set(item) == {"id", "status", "checks"} and type(item["checks"]) is int
             and item["checks"] == CELL_CHECKS[item["id"]] + extra.get(item["id"], 0) for item in cells),
         "checks_mismatch")
    check_inspector(receipt["inspector"])
    surface = receipt["surface"]
    need(isinstance(surface, dict) and all(type(surface.get(key)) is int and surface[key] == count
                                           for key, count in SURFACE_COUNTS.items()), "field_mismatch")
    inspector = receipt["inspector"]
    summary = receipt["summary"]
    need(summary == {"synthetic": len(CELL_IDS), "passed": len(CELL_IDS), "failed": 0,
                     "inspector": inspector["status"],
                     "inspector_cells": len(INSPECTOR_IDS) if inspector["status"] == "pass" else 0,
                     "owner_pending": len(OWNER_IDS)}, "receipt_invalid")
    if commit is not None:
        need(receipt["commit"] == commit, "commit_mismatch")
    if wheel_sha256 is not None:
        need(wheel["sha256"] == wheel_sha256, "wheel_mismatch")
    if census is not None:
        need(receipt["version"] == census.get("version"), "version_mismatch")
        need(receipt["schema_fingerprint"] == census.get("schema_fingerprint"), "schema_mismatch")
        need(receipt["census"]["tools_sha256"] == tools_sha256(census.get("tools") or []), "tools_mismatch")
        need(receipt["census"] == {"tool_count": len(census.get("tools") or []),
                                   "tools_sha256": tools_sha256(census.get("tools") or []),
                                   "capabilities": len(census.get("capabilities") or [])}, "field_mismatch")
    if committed is not None:
        check_fields(receipt, committed, derived)


INSTALL_KEYS = frozenset({"constraints", "constraints_sha256", "wheelhouse", "distributions", "freeze", "pip_check",
                          "record_entries"})


def check_fields(receipt: dict, committed: dict, derived: dict | None = None) -> None:
    """Every other field against what the recorded commit fixes (committed_facts) and what check re-derived
    from it (`derived`: a reinstall's facts and a fresh listing); an edited field refuses:

    - harness and gate: path and sha256 of the commit's own files (harness_mismatch, gate_mismatch);
    - tree: the commit's tree id (tree_mismatch);
    - wheel: exactly file, sha256 and bytes, the file named for the version, the size of the rebuilt wheel;
    - build: exactly build, python, setuptools and source_date_epoch; the build and setuptools that rebuilt
      the same wheel, an X.Y.Z interpreter of the constraints' target minor version, the commit time;
    - install: exactly its seven keys; the commit's constraints file and sha256, its distribution count, the
      exact freeze its pins give, pip check clean, wheelhouse provided or downloaded, a positive RECORD count;
      with `derived`, every install fact but the wheelhouse equal to the reinstall's;
    - installer: exactly the bundle installer's dependency as the commit's release gate pins it (distribution,
      version, sha256) and where it came from (toolhouse provided or downloaded);
    - sdk: exactly the distribution, the version the constraints pin and every revision;
    - surface: exactly its four keys, a sha256 listing (with `derived`, the fresh listing's), no tool without
      an output schema;
    - inspector: a passed column's lock_sha256 is the commit's packaging/inspector/package-lock.json.
    Every other field is fixed by the harness or bound to the census (check_receipt); anything else refuses
    field_mismatch."""
    need(receipt["harness"] == {"path": HARNESS_PATH, "sha256": committed["harness_sha256"]}, "harness_mismatch")
    need(receipt["gate"] == {"path": GATE_PATH, "sha256": committed["gate_sha256"]}, "gate_mismatch")
    need(receipt["tree"] == committed["tree_id"], "tree_mismatch")
    version = receipt["version"]
    wheel, build, install, sdk, surface = (receipt[key] for key in ("wheel", "build", "install", "sdk", "surface"))
    need(set(wheel) == {"file", "sha256", "bytes"} and wheel["file"] == f"{PROJECT.replace('-', '_')}-{version}"
         f"-py3-none-any.whl" and type(wheel["bytes"]) is int and wheel["bytes"] == committed["wheel_bytes"],
         "field_mismatch")
    python = build.get("python") if isinstance(build, dict) else None
    need(isinstance(build, dict) and set(build) == {"build", "python", "setuptools", "source_date_epoch"}
         and build["build"] == committed["build_tools"]["build"]
         and build["setuptools"] == committed["build_tools"]["setuptools"]
         and isinstance(python, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", python)
         and python.rsplit(".", 1)[0] == committed["python_target"]
         and type(build["source_date_epoch"]) is int and build["source_date_epoch"] == committed["epoch"],
         "field_mismatch")
    need(isinstance(install, dict) and set(install) == INSTALL_KEYS
         and install["constraints"] == committed["constraints"]
         and install["constraints_sha256"] == committed["constraints_sha256"]
         and install["wheelhouse"] in ("provided", "downloaded") and install["pip_check"] == "clean"
         and type(install["distributions"]) is int and install["distributions"] == committed["distributions"]
         and install["freeze"] == committed["freeze"]
         and type(install["record_entries"]) is int and install["record_entries"] > 0, "field_mismatch")
    installer = receipt["installer"]
    need(isinstance(installer, dict) and set(installer) == {"distribution", "version", "sha256", "toolhouse"}
         and {key: installer[key] for key in ("distribution", "version", "sha256")} == committed["installer"]
         and installer["toolhouse"] in ("provided", "downloaded"), "field_mismatch")
    need(sdk == {"distribution": SDK_DISTRIBUTION, "version": committed["sdk_version"], "revisions": list(REVISIONS)},
         "field_mismatch")
    need(set(surface) == {"listing_sha256", "structured_results_validated", "tools_with_validated_results",
                          "unschematized_tools"}
         and isinstance(surface["listing_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", surface["listing_sha256"])
         and surface["unschematized_tools"] == [], "field_mismatch")
    if receipt["inspector"].get("status") == "pass":
        need(receipt["inspector"]["lock_sha256"] == committed["inspector_lock_sha256"], "field_mismatch")
    if derived is not None:
        def facts(value: dict) -> dict:
            return {key: item for key, item in value.items() if key != "wheelhouse"}
        need(facts(install) == facts(derived["install"]), "field_mismatch")
        need(surface["listing_sha256"] == derived["listing_sha256"], "field_mismatch")


def installer_pin(gate) -> dict:
    """The bundle installer's dependency as the release gate pins it (its TOOLS row)."""
    rows = [row for row in gate.TOOLS if row[0] == INSTALLER_DISTRIBUTION]
    need(len(rows) == 1, "toolhouse_invalid")
    return {"distribution": rows[0][0], "version": rows[0][1], "sha256": rows[0][2]}


def committed_facts(gate, built: dict) -> dict:
    """What rebuilding the recorded commit fixes: its own harness and gate, tree id and epoch, the rebuilt
    wheel's size and the build tools that made it, its pinned closure (target, freeze, SDK version), the
    installer dependency its gate pins and its Inspector lock."""
    tree = built["tree"]
    release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", built["version"])
    need(release, "build_failed")
    constraints = f"packaging/constraints-{release.group(0)}.txt"
    target, pins = _gate_call(gate, "install_failed", gate.parse_constraints,
                              (tree / constraints).read_text(encoding="utf-8"))
    lock = tree / INSPECTOR_DIR / "package-lock.json"
    return {"harness_sha256": _sha((tree / HARNESS_PATH).read_bytes()),
            "gate_sha256": _sha((tree / GATE_PATH).read_bytes()),
            "tree_id": built["tree_id"], "epoch": built["epoch"], "wheel_bytes": built["wheel"].stat().st_size,
            "build_tools": {name: built["build"][name] for name in ("build", "setuptools")},
            "python_target": target, "constraints": constraints,
            "constraints_sha256": gate.sha256_file(tree / constraints), "distributions": len(pins),
            # install_closure's own expectation: every pin, plus the project at the built version.
            "freeze": sorted([f"{name}=={pinned}" for name, (pinned, _) in pins.items()]
                             + [f"{PROJECT}=={built['version']}"]),
            "sdk_version": pins[SDK_DISTRIBUTION][0] if SDK_DISTRIBUTION in pins else None,
            "installer": installer_pin(gate),
            "inspector_lock_sha256": _sha(lock.read_bytes()) if lock.is_file() else None}


def load_receipt(path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            need(key not in value, "receipt_invalid")
            value[key] = item
        return value
    try:
        metadata = path.lstat()
        need(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_RECEIPT_BYTES, "receipt_invalid")
        return json.loads(path.read_bytes(), object_pairs_hook=unique)
    except (OSError, ValueError, RecursionError):
        raise MatrixRefusal("receipt_invalid") from None


def write_receipt(path: Path, receipt: dict) -> None:
    temporary = path.with_name(path.name + ".partial")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(receipt))
    os.replace(temporary, path)


# --- build, install, census (the running interpreter; release_gate helpers) ----------------------------

def load_gate():
    """The working tree's release_gate.py, executed from exactly the bytes whose sha256 it carries as
    `matrix_loaded_sha256`; run and check refuse (gate_mismatch) when that is not the recorded commit's."""
    path = HARNESS.parent / "release_gate.py"
    source = path.read_bytes()
    spec = importlib.util.spec_from_file_location("release_gate_for_matrix", path)
    gate = importlib.util.module_from_spec(spec)
    exec(compile(source, str(path), "exec"), gate.__dict__)
    gate.matrix_loaded_sha256 = _sha(source)
    return gate


def harness_digest() -> str:
    """sha256 of this harness file; run and check refuse (harness_mismatch) unless it is the commit's."""
    return _sha(HARNESS.read_bytes())


def _gate_call(gate, matrix_code: str, function, *args, **kwargs):
    """A release_gate helper whose refusal becomes this harness's fixed code (the gate's code as detail)."""
    try:
        return function(*args, **kwargs)
    except gate.GateRefusal as refusal:
        raise MatrixRefusal(matrix_code, {"gate": refusal.code}) from None


def build_tools() -> dict:
    import importlib.metadata
    versions = {}
    for name in ("build", "setuptools"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise MatrixRefusal("build_tools_missing") from None
    return versions


def build_wheel(gate, git, commit: str, scratch: Path) -> dict:
    """sdist and wheel from `git archive <commit>`, as the release gate builds them (wheel from the sdist)."""
    _fresh(scratch)
    tree_id = _gate_call(gate, "git_failed", git.text, "rev-parse", "--verify", commit + "^{tree}")
    epoch = int(_gate_call(gate, "git_failed", git.text, "show", "-s", "--format=%ct", commit))
    archive = scratch / "archive.tar"
    _gate_call(gate, "git_failed", git, "archive", "--format=tar", commit, stdout=archive)
    tree = scratch / "tree"
    sources = _gate_call(gate, "build_failed", gate.extract, archive, tree, epoch)
    version, project = _gate_call(gate, "build_failed", gate.project_metadata, tree)
    tools = build_tools()
    out = scratch / "dist"
    out.mkdir(mode=0o700)
    env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": SYSTEM_PATH, "LC_ALL": "C", "TZ": "UTC",
           "PYTHONUTF8": "1", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1", "SOURCE_DATE_EPOCH": str(epoch)}
    _run([sys.executable, "-I", "-m", "build", "--no-isolation", "--outdir", out, tree], "build_failed",
         timeout=BUILD_SECONDS, env=env, cwd=scratch)
    wheel = out / f"{gate.DIST}-{version}-py3-none-any.whl"
    need(wheel.is_file(), "build_failed")
    _gate_call(gate, "build_failed", gate.check_wheel, wheel, version, sources, project)
    return {"tree": tree, "tree_id": tree_id, "epoch": epoch, "version": version, "wheel": wheel,
            "build": {"python": ".".join(map(str, sys.version_info[:3])), **tools, "source_date_epoch": epoch}}


def _run(command: list, code: str, *, timeout: int, env: dict, cwd: Path | None = None,
         stdout=subprocess.PIPE, stderr=subprocess.PIPE) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run([str(part) for part in command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise MatrixRefusal(code, {"failure": "spawn_or_timeout"}) from None
    tail = (result.stderr or b"")[-4000:].decode("utf-8", "replace") if isinstance(result.stderr, bytes) else ""
    need(result.returncode == 0, code, {"returncode": result.returncode, "stderr": tail})
    return result


def install(gate, built: dict, scratch: Path, wheelhouse: Path | None) -> tuple[Path, dict]:
    """A fresh venv with the wheel and the commit's pinned closure, hash-checked, offline once downloaded."""
    version = built["version"]
    release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    need(release, "install_failed")
    constraints = built["tree"] / f"packaging/constraints-{release.group(0)}.txt"
    need(constraints.is_file(), "install_failed", {"constraints": "missing"})
    target, pins = _gate_call(gate, "install_failed", gate.parse_constraints, constraints.read_text(encoding="utf-8"))
    _gate_call(gate, "install_failed", gate.check_interpreter, target)
    chain = gate.Toolchain(_fresh(scratch))
    venv = _gate_call(gate, "install_failed", chain.venv, "venv")
    if wheelhouse is None:
        wheelhouse = scratch / "wheelhouse"
        wheelhouse.mkdir(mode=0o700)
        _gate_call(gate, "install_failed", chain.pip, venv, "download", "--only-binary=:all:", "--no-deps",
                   "--require-hashes", "-r", constraints, "-d", wheelhouse, code="download_failed")
        source = "downloaded"
    else:
        source = "provided"
    closure = _gate_call(gate, "wheelhouse_invalid", gate.check_wheelhouse, wheelhouse, pins)
    record = _gate_call(gate, "install_failed", gate.install_closure, chain, venv, built["wheel"], constraints,
                        wheelhouse, version, pins)
    return venv, {"constraints": constraints.relative_to(built["tree"]).as_posix(),
                  "constraints_sha256": gate.sha256_file(constraints), "wheelhouse": source,
                  "distributions": len(closure), "freeze": record["freeze"], "pip_check": record["pip_check"],
                  "record_entries": record["record_entries"]}


def installed_runtime(gate, built: dict, scratch: Path, wheelhouse: Path | None) -> tuple["Runtime", dict]:
    """install(), then the installed proxy, broker and driver as a Runtime, with the install facts."""
    venv, installed = install(gate, built, scratch, wheelhouse)
    python = venv / "bin" / "python"
    site = Path(_run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                     "install_failed", timeout=60, env={"PATH": SYSTEM_PATH}).stdout.decode().strip())
    return Runtime(python=python, proxy=[venv / "bin" / PROJECT], package_root=site, sdk_root=site), installed


def observed_listing(runtime: "Runtime", census: dict, work: Path) -> str:
    """The listing session against `runtime`: the sha256 of the tool listing a consumer sees under the full
    policy (no broker is needed to list), as check re-derives the receipt's surface.listing_sha256."""
    (result,) = run_sessions(runtime, census, _fresh(work), approval="scripted", sessions=("listing",))
    digest = result.get("observed", {}).get("listing_sha256")
    need(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "drive_failed")
    return digest


def source_census(gate, python: Path, src: Path, scratch: Path) -> dict:
    """The release gate's census of a source tree: version, capabilities, tools and schema fingerprint."""
    home = Path(tempfile.mkdtemp(prefix="census-home-", dir=scratch))
    script = home / "census.py"
    script.write_text(gate.CENSUS, encoding="utf-8")
    result = _run([python, "-I", script, src], "census_failed", timeout=CENSUS_SECONDS,
                  env={"HOME": str(home), "PATH": SYSTEM_PATH, "LC_ALL": "C", "TMPDIR": str(home)}, cwd=home)
    try:
        census = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeError, ValueError, IndexError):
        raise MatrixRefusal("census_failed") from None
    need(isinstance(census, dict) and isinstance(census.get("tools"), list)
         and isinstance(census.get("capabilities"), list) and isinstance(census.get("schema_fingerprint"), str),
         "census_failed")
    census["tools"] = sorted(census["tools"])
    census["capabilities"] = sorted(census["capabilities"])
    return census


# --- the Inspector column and the installer dependency (the running interpreter) --------------------------

# Several tools/call in one MCP session through the Inspector CLI's own client module; a call without a draft_id
# gets the draft the first call prepared. Written by the harness beside the session, never into the package.
INSPECTOR_SESSION_SCRIPT = """\
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const request = JSON.parse(readFileSync(process.argv[2], "utf8"));
const load = (path) => import(pathToFileURL(path).href);
const client = await load(request.cli + "/build/client/index.js");
const transport = await load(request.cli + "/build/transport.js");
const { Client } = await load(request.sdk_client);
const session = transport.createTransport({ transportType: "stdio", command: request.command, args: request.args });
const mcp = new Client({ name: "inspector-cli", version: request.version });
const results = [];
await client.connect(mcp, session);
try {
  let draft = null;
  for (const [name, args] of request.calls) {
    const result = await client.callTool(mcp, name, draft !== null && !("recipient" in args) ? { draft_id: draft, ...args } : args);
    results.push(result);
    if (draft === null && result && result.structuredContent && typeof result.structuredContent.draft_id === "string") {
      draft = result.structuredContent.draft_id;
    }
  }
} finally {
  await client.disconnect(session);
}
process.stdout.write(JSON.stringify(results));
process.exit(0);
"""
INSPECTOR_CLI_PATH = "node_modules/@modelcontextprotocol/inspector-cli"
INSPECTOR_SDK_CLIENT = "node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js"
NPM_SECONDS = 600


def check_inspector_lock(data: bytes) -> dict:
    """The committed lock pins the Inspector CLI at INSPECTOR_VERSION and INSPECTOR_INTEGRITY, and every package of
    its tree to an npm registry tarball with a sha512 integrity and no install script."""
    try:
        lock = json.loads(data)
    except (ValueError, UnicodeError):
        raise MatrixRefusal("inspector_invalid") from None
    packages = lock.get("packages") if isinstance(lock, dict) else None
    need(isinstance(packages, dict) and lock.get("lockfileVersion") == 3
         and (packages.get("") or {}).get("dependencies") == {INSPECTOR_PACKAGE: INSPECTOR_VERSION}, "inspector_invalid")
    top = packages.get(INSPECTOR_CLI_PATH) or {}
    need(top.get("version") == INSPECTOR_VERSION and top.get("integrity") == INSPECTOR_INTEGRITY, "inspector_invalid")
    for path, entry in packages.items():
        if path == "":
            continue
        need(isinstance(entry, dict) and path.startswith("node_modules/")
             and isinstance(entry.get("resolved"), str) and entry["resolved"].startswith("https://registry.npmjs.org/")
             and isinstance(entry.get("integrity"), str) and entry["integrity"].startswith("sha512-")
             and not entry.get("hasInstallScript"), "inspector_invalid")
    return lock


def node_version(node: Path) -> str | None:
    try:
        done = subprocess.run([str(node), "--version"], env={"PATH": f"{node.parent}:{SYSTEM_PATH}"},
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)\s*", done.stdout.decode("ascii", "replace"))
    return match.group(1) if done.returncode == 0 and match else None


def npm_ci(node: Path, project: Path, cache: Path, *, offline: bool, home: Path) -> bool:
    """`npm ci` of the committed lock with the node's own npm, scripts off, a private HOME and empty npm configs."""
    user, system = home / "user-npmrc", home / "global-npmrc"  # npm refuses one file loaded twice
    for config in (user, system):
        config.write_text("", encoding="utf-8")
    npm = node.parent / "npm"
    command = [str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", str(cache),
               "--userconfig", str(user), "--globalconfig", str(system), *(("--offline",) if offline else ())]
    env = {"HOME": str(home), "PATH": f"{node.parent}:{SYSTEM_PATH}", "LC_ALL": "C", "TMPDIR": str(home),
           **{name: os.environ[name] for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
              if name in os.environ and not offline}}
    try:
        done = subprocess.run(command, cwd=str(project), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=NPM_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def inspector_project(source: Path, scratch: Path) -> Path:
    project = _fresh(scratch / "project")
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(source / name, project / name)
    return project


def prepare_inspector(tree: Path, node: Path | None, cache: Path | None, scratch: Path) -> dict:
    """The Inspector column's runtime, installed offline from the fetched cache with the commit's lock, or why it is
    unavailable (one fixed reason)."""
    if node is None:
        return {"status": "unavailable", "reason": "node_not_given"}
    version = node_version(node)
    if version is None or int(version.split(".")[0]) < INSPECTOR_NODE_MAJOR:
        return {"status": "unavailable", "reason": "node_unsupported"}
    if cache is None or not cache.is_dir():
        return {"status": "unavailable", "reason": "inspector_cache_missing"}
    lock = tree / INSPECTOR_DIR / "package-lock.json"
    try:
        check_inspector_lock(lock.read_bytes())
    except (OSError, MatrixRefusal):
        return {"status": "unavailable", "reason": "inspector_lock_invalid"}
    _fresh(scratch)
    project = inspector_project(tree / INSPECTOR_DIR, scratch)
    if not npm_ci(node, project, cache, offline=True, home=_fresh(scratch / "home")):
        return {"status": "unavailable", "reason": "inspector_install_failed"}
    cli, sdk_client = project / INSPECTOR_CLI_PATH, project / INSPECTOR_SDK_CLIENT
    try:
        installed = json.loads((cli / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        installed = {}
    if (installed.get("name"), installed.get("version")) != (INSPECTOR_PACKAGE, INSPECTOR_VERSION) or not (
            (cli / "build" / "index.js").is_file() and sdk_client.is_file()):
        return {"status": "unavailable", "reason": "inspector_install_failed"}
    return {"status": "ready", "node": version, "lock_sha256": _sha(lock.read_bytes()),
            "plan": {"node": str(node), "cli": str(cli / "build" / "index.js"), "sdk_client": str(sdk_client),
                     "version": INSPECTOR_VERSION}}


def inspector_fetch(args: argparse.Namespace) -> int:
    """Fill an npm cache with the pinned Inspector tree (online, integrity-checked by npm against the committed
    lock); later runs install it offline from that cache."""
    node, cache = Path(args.node), Path(args.cache)
    need(node.is_absolute() and node_version(node) is not None, "node_invalid")
    need(cache.is_absolute() and (not cache.exists() or (cache.is_dir() and not cache.is_symlink())), "cache_invalid")
    gate = load_gate()
    repo = _gate_call(gate, "repo_unavailable", gate.toplevel, args.repo)
    source = Path(repo) / INSPECTOR_DIR
    try:
        check_inspector_lock((source / "package-lock.json").read_bytes())
    except OSError:
        raise MatrixRefusal("inspector_invalid") from None
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="tgmcp-inspector-")).resolve()
    try:
        project = inspector_project(source, scratch)
        need(npm_ci(node, project, cache, offline=False, home=_fresh(scratch / "home")), "fetch_failed")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"inspector cache ready: {INSPECTOR_PACKAGE} {INSPECTOR_VERSION}")
    return 0


def installer_dependency(gate, toolhouse: Path | None, venv: Path, scratch: Path) -> tuple[Path, dict]:
    """The bundle installer's tomlkit, as the release gate pins it: the wheel from --toolhouse (the gate's) or a
    hash-checked download, extracted into a private directory the bundle session puts on its path, as the bundle's
    launcher puts its installer/ directory."""
    pin = installer_pin(gate)
    name = f"{pin['distribution']}-{pin['version']}-py3-none-any.whl"
    _fresh(scratch)
    if toolhouse is None:
        download = _fresh(scratch / "toolhouse")
        requirement = scratch / "installer-requirement.txt"
        requirement.write_text(f"{pin['distribution']}=={pin['version']} --hash=sha256:{pin['sha256']}\n",
                               encoding="utf-8")
        chain = gate.Toolchain(_fresh(scratch / "chain"))
        _gate_call(gate, "installer_failed", chain.pip, venv, "download", "--only-binary=:all:", "--no-deps",
                   "--require-hashes", "-r", requirement, "-d", download, code="download_failed")
        wheel, source = download / name, "downloaded"
    else:
        wheel, source = toolhouse / name, "provided"
    need(wheel.is_file() and not wheel.is_symlink(), "toolhouse_invalid")
    need(_sha(wheel.read_bytes()) == pin["sha256"], "toolhouse_invalid")
    target = _fresh(scratch / "installer")
    import zipfile
    try:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                parts = Path(member.filename).parts
                if not parts or parts[0] != pin["distribution"] or member.is_dir():
                    continue
                need(not member.filename.startswith("/") and ".." not in parts, "toolhouse_invalid")
                path = target.joinpath(*parts)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(archive.read(member))
    except (OSError, zipfile.BadZipFile):
        raise MatrixRefusal("installer_failed") from None
    need((target / pin["distribution"] / "__init__.py").is_file(), "installer_failed")
    return target, {**pin, "toolhouse": source}

# --- sessions ------------------------------------------------------------------------------------------

def write_policy(home: Path, capabilities: list[str], *, stale: bool = False) -> None:
    directory = home
    for part in ("Library", "Application Support", "TelegramSearchMCP"):
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    body = ("config_version = 1\nenabled_capabilities = [" + ", ".join(json.dumps(c) for c in capabilities) + "]\n"
            + (f"expected_package_version = {json.dumps(STALE_PIN)}\n" if stale else ""))
    path = directory / "runtime.toml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(body)


def session_layout(name: str, census: dict) -> tuple[list[str], bool, str | None]:
    """(capabilities, stale pin, broker profile or None) of each session in the closed SESSIONS set, and of
    the cell-less listing session check runs."""
    everything = list(census["capabilities"])
    return {"full": (everything, False, "authorized"),
            "restricted": (list(RESTRICTED_CAPABILITIES), False, "authorized"),
            "stale": (everything, True, None),
            "offline": (everything, False, None),
            "unauthorized": (everything, False, "unauthorized"),
            "closed": ([], False, None),
            "bundle": ([], False, None),
            "inspector": (everything, False, "authorized"),
            "listing": (everything, False, None)}[name]


class Runtime:
    """Where the proxy, broker and driver come from: an installed venv, or a source tree (tests only)."""

    def __init__(self, *, python: Path, proxy: list, package_root: Path, sdk_root: Path | None,
                 pythonpath: str | None = None):
        self.python, self.proxy, self.package_root, self.sdk_root = python, proxy, package_root, sdk_root
        self.pythonpath = pythonpath

    def env(self, home: Path, tmp: Path) -> dict:
        return {"HOME": str(home), "PATH": SYSTEM_PATH, "LC_ALL": "C", "TMPDIR": str(tmp),
                **({"PYTHONPATH": self.pythonpath} if self.pythonpath else {})}

    def command(self, *args) -> list:
        return [str(self.python), *(() if self.pythonpath else ("-I",)), str(HARNESS), *map(str, args)]


def start_broker(runtime: Runtime, home: Path, tmp: Path, profile: str, approval: str, log: Path):
    stream = open(log, "wb")
    try:
        process = subprocess.Popen(runtime.command("broker", "--profile", profile, "--approval", approval,
                                                   "--package-root", runtime.package_root),
                                   env=runtime.env(home, tmp), cwd=home, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=stream)
    except OSError:
        stream.close()
        raise MatrixRefusal("broker_failed") from None
    deadline = time.monotonic() + BROKER_START_SECONDS
    while time.monotonic() < deadline:
        if (home / "matrix-broker.ready").exists():
            return process, stream
        if process.poll() is not None:
            break
        time.sleep(0.05)
    stop_broker(process, stream)
    raise MatrixRefusal("broker_failed", {"profile": profile})


def stop_broker(process, stream) -> None:
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    finally:
        stream.close()


def run_sessions(runtime: Runtime, census: dict, work: Path, *, approval: str,
                 sessions: tuple[str, ...] = SESSIONS, extra: dict | None = None) -> list[dict]:
    """Every session at once, each with its own short HOME (a socket path holds at most 104 bytes), its
    own broker where it needs one and its own driver; results in `sessions` order. `extra` adds plan keys:
    the bundle session's installer_path and the Inspector session's inspector."""
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="tgcm.", dir="/private/tmp")))
    brokers, drivers, outputs = [], [], []
    try:
        for index, name in enumerate(sessions):
            # The name feeds the proxy's derived LaunchAgent label: distinctive, so the offline session's
            # restart attempt (`launchctl kickstart` of that label) can only find nothing.
            home = base / f"tgcm{index}"
            home.mkdir(mode=0o700)
            tmp = work / f"tmp-{name}"
            tmp.mkdir(mode=0o700)
            capabilities, stale, profile = session_layout(name, census)
            write_policy(home, capabilities, stale=stale)
            mode = "scripted" if name == "inspector" else approval  # The real dialog is the SDK denial probe's.
            if profile is not None:
                brokers.append(start_broker(runtime, home, tmp, profile, mode, work / f"broker-{name}.log"))
            plan = {"session": name, "home": str(home), "tmp": str(tmp), "proxy": [str(p) for p in runtime.proxy],
                    "pythonpath": runtime.pythonpath, "census": census, "approval": mode,
                    "broker_log": str(home / "matrix-broker.jsonl") if profile is not None else None,
                    "sdk_root": str(runtime.sdk_root) if runtime.sdk_root else None,
                    "debug": os.environ.get("TGMCP_MATRIX_DEBUG") == "1", **(extra or {})}
            plan_path, out = work / f"plan-{name}.json", work / f"result-{name}.json"
            plan_path.write_bytes(canonical(plan))
            outputs.append(out)
            log = open(work / f"drive-{name}.log", "wb")
            try:
                drivers.append((subprocess.Popen(runtime.command("drive", "--plan", plan_path, "--out", out),
                                                 env=runtime.env(tmp, tmp), cwd=tmp, stdin=subprocess.DEVNULL,
                                                 stdout=log, stderr=log), log))
            except OSError:
                log.close()
                raise MatrixRefusal("drive_failed") from None
        deadline = time.monotonic() + DRIVE_SECONDS + 60
        for process, _ in drivers:
            try:
                returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise MatrixRefusal("drive_failed", {"failure": "timeout"}) from None
            need(returncode == 0, "drive_failed", {"returncode": returncode})
        try:
            return [json.loads(out.read_bytes()) for out in outputs]
        except (OSError, ValueError):
            raise MatrixRefusal("drive_failed") from None
    finally:
        for process, log in drivers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            log.close()
        for broker in brokers:
            stop_broker(*broker)
        shutil.rmtree(base, ignore_errors=True)


# --- commands ------------------------------------------------------------------------------------------

def _workdir(value: str | None) -> tuple[Path, bool]:
    if value is None:
        return Path(tempfile.mkdtemp(prefix="tgmcp-matrix-")).resolve(), True
    path = Path(value)
    need(path.is_absolute(), "workdir_invalid")
    if path.exists():
        need(path.is_dir() and not path.is_symlink() and not any(path.iterdir()), "workdir_invalid")
    else:
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError:
            raise MatrixRefusal("workdir_invalid") from None
    return path.resolve(), False


def summary_line(receipt: dict) -> str:
    summary, inspector = receipt["summary"], receipt["inspector"]
    column = (f"Inspector {summary['inspector_cells']}/{len(INSPECTOR_IDS)} cells" if inspector["status"] == "pass"
              else f"Inspector unavailable ({inspector['reason']})")
    return (f"consumer matrix passed: {summary['passed']}/{summary['synthetic']} synthetic cells; {column}; "
            f"{summary['owner_pending']} owner cells pending")


def run(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    need(receipt_path.is_absolute() and not receipt_path.exists() and receipt_path.parent.is_dir(), "usage")
    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else None
    need(wheelhouse is None or wheelhouse.is_dir(), "wheelhouse_invalid")
    toolhouse = Path(args.toolhouse).resolve() if args.toolhouse else None
    need(toolhouse is None or toolhouse.is_dir(), "toolhouse_invalid")
    node = Path(args.node) if args.node else None
    need(node is None or node.is_absolute(), "node_invalid")
    cache = Path(args.inspector_cache) if args.inspector_cache else None
    need(cache is None or cache.is_absolute(), "cache_invalid")
    gate = load_gate()
    repo = _gate_call(gate, "repo_unavailable", gate.toplevel, args.repo)
    git = gate.Git(repo)
    commit = _gate_call(gate, "unknown_commit", gate.resolve_commit, git, args.commit)
    work, temporary = _workdir(args.workdir)
    try:
        built = build_wheel(gate, git, commit, work / "build")
        committed = committed_facts(gate, built)
        # The receipt is the commit's harness and gate at work: a working-tree edit of either refuses.
        need(harness_digest() == committed["harness_sha256"], "harness_mismatch")
        need(gate.matrix_loaded_sha256 == committed["gate_sha256"], "gate_mismatch")
        runtime, installed = installed_runtime(gate, built, work / "install", wheelhouse)
        census = source_census(gate, runtime.python, built["tree"] / "src", work)
        need(census.get("version") == built["version"], "version_mismatch")
        # The cells are written for one schema: a deliberate schema change refuses before any session runs.
        need(census.get("schema_fingerprint") == MATRIX_SCHEMA_FINGERPRINT, "schema_mismatch")
        installer_path, installer = installer_dependency(gate, toolhouse, runtime.python.parent.parent,
                                                         work / "installer")
        inspector = prepare_inspector(built["tree"], node, cache, work / "inspector")
        names = SESSIONS + (("inspector",) if inspector["status"] == "ready" else ())
        results = run_sessions(runtime, census, _fresh(work / "sessions"),
                               approval="real_dialog" if args.real_dialog else "scripted", sessions=names,
                               extra={"installer_path": str(installer_path), "inspector": inspector.get("plan")})
        sessions = [result for result in results if result["session"] != "inspector"]
        column = inspector_section(inspector, next((result for result in results
                                                    if result["session"] == "inspector"), None))
        wheel = built["wheel"]
        receipt = build_receipt(
            commit=commit, tree=built["tree_id"], version=built["version"],
            wheel={"file": wheel.name, "sha256": gate.sha256_file(wheel), "bytes": wheel.stat().st_size},
            build=built["build"], install=installed, installer=installer, census=census, sessions=sessions,
            inspector=column, harness_sha256=committed["harness_sha256"], gate_sha256=committed["gate_sha256"])
        for cell in receipt["cells"] + column.get("cells", []):
            if cell["status"] != "pass":  # Harness-defined labels only, never observed values.
                print(f"cell {cell['id']}: {cell['failed_check']}", file=sys.stderr)
        need(column["status"] != "fail", "cells_failed")
        check_receipt(receipt, commit=commit, wheel_sha256=receipt["wheel"]["sha256"], census=census,
                      committed=committed, derived={"install": installed,
                                                    "listing_sha256": receipt["surface"]["listing_sha256"]})
        write_receipt(receipt_path, receipt)
    finally:
        if temporary:
            shutil.rmtree(work, ignore_errors=True)
    print(summary_line(receipt))
    return 0


def _fresh(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def check(args: argparse.Namespace) -> int:
    receipt = load_receipt(Path(args.receipt))
    check_receipt(receipt)
    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else None
    need(wheelhouse is None or wheelhouse.is_dir(), "wheelhouse_invalid")
    gate = load_gate()
    repo = _gate_call(gate, "repo_unavailable", gate.toplevel, args.repo)
    git = gate.Git(repo)
    commit = _gate_call(gate, "unknown_commit", gate.resolve_commit, git, receipt["commit"])
    if args.commit is not None:
        need(commit == _gate_call(gate, "unknown_commit", gate.resolve_commit, git, args.commit), "commit_mismatch")
    work = Path(tempfile.mkdtemp(prefix="tgmcp-matrix-check-")).resolve()
    try:
        built = build_wheel(gate, git, commit, work / "build")
        committed = committed_facts(gate, built)
        # Re-derived with the receipt commit's own harness and gate, never a working-tree edit of them.
        need(harness_digest() == committed["harness_sha256"], "harness_mismatch")
        need(gate.matrix_loaded_sha256 == committed["gate_sha256"], "gate_mismatch")
        census = source_census(gate, Path(sys.executable), built["tree"] / "src", work)
        need(census.get("schema_fingerprint") == MATRIX_SCHEMA_FINGERPRINT, "schema_mismatch")
        runtime, installed = installed_runtime(gate, built, work / "install", wheelhouse)
        listing = observed_listing(runtime, census, work / "listing")
        check_receipt(receipt, commit=commit, wheel_sha256=gate.sha256_file(built["wheel"]), census=census,
                      committed=committed, derived={"install": installed, "listing_sha256": listing})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("consumer matrix receipt verified: " + summary_line(receipt).split(": ", 1)[1])
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise MatrixRefusal("usage")


def parser() -> argparse.ArgumentParser:
    root = _Parser(prog="release_consumer_matrix.py", add_help=False)
    commands = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run_parser = commands.add_parser("run", add_help=False)
    run_parser.add_argument("--commit", required=True)
    run_parser.add_argument("--receipt", required=True)
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--wheelhouse")
    run_parser.add_argument("--toolhouse")
    run_parser.add_argument("--node")
    run_parser.add_argument("--inspector-cache")
    run_parser.add_argument("--workdir")
    run_parser.add_argument("--real-dialog", action="store_true")
    check_parser = commands.add_parser("check", add_help=False)
    check_parser.add_argument("--receipt", required=True)
    check_parser.add_argument("--repo", type=Path, default=Path.cwd())
    check_parser.add_argument("--commit")
    check_parser.add_argument("--wheelhouse")
    fetch_parser = commands.add_parser("inspector-fetch", add_help=False)
    fetch_parser.add_argument("--node", required=True)
    fetch_parser.add_argument("--cache", required=True)
    fetch_parser.add_argument("--repo", type=Path, default=Path.cwd())
    broker_parser = commands.add_parser("broker", add_help=False)
    broker_parser.add_argument("--profile", choices=("authorized", "unauthorized"), required=True)
    broker_parser.add_argument("--approval", choices=("scripted", "real_dialog"), required=True)
    broker_parser.add_argument("--package-root", required=True)
    drive_parser = commands.add_parser("drive", add_help=False)
    drive_parser.add_argument("--plan", required=True)
    drive_parser.add_argument("--out", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        handler = {"run": run, "check": check, "inspector-fetch": inspector_fetch, "broker": broker_main,
                   "drive": drive_main}[args.command]
        return handler(args)
    except MatrixRefusal as refusal:
        print(refusal.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
