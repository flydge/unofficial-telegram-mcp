#!/usr/bin/env python3
"""Consumer-matrix harness: every synthetic cell through the official SDK, the MCP Inspector CLI and real agent CLIs
(Codex, Claude Code) against an installed proxy, and the installed bundle installer's change paths.

  python packaging/release_consumer_matrix.py run --commit <sha> --receipt <path> [--wheelhouse <dir>]
      [--toolhouse <dir>] [--node <path> --inspector-cache <dir>] [--workdir <dir>] [--real-dialog]
      [--codex <path> --codex-home <dir> [--codex-model <model>]] [--claude <path> [--claude-model <model>]]
      [--agent-evidence <dir>]
  python packaging/release_consumer_matrix.py check --receipt <path> [--commit <sha>] [--wheelhouse <dir>]
      [--agent-evidence <dir>]
  python packaging/release_consumer_matrix.py inspector-fetch --node <path> --cache <dir>

Spec: docs/plans/final-consumer-matrix-spec.md (final acceptance F18; it extends the 0.54 matrix spec).
acceptance is always false: the harness runs only synthetic cells and never marks an owner or real-account cell
passed.

The agent columns (--codex, --claude) give a real agent CLI one fixed request per cell (AGENT_CELLS) with a private
configuration whose only MCP server is the installed proxy (Codex: a temporary HOME and CODEX_HOME holding its
config.toml and a copy of auth.json, nothing else; Claude Code: the default configuration location for its login
only, with no setting source, auto memory, skill, built-in tool or transcript, a temporary --mcp-config with
--strict-mcp-config, and the caller's Claude Code configuration verified unchanged), each cell against its own
synthetic broker, which records its own transcript of every frame, operation, payload, result and owner dialog. A
cell is decided on that transcript, the agent's exit, its completed turn and the tools it used; the answer's wording
is advisory. The evidence (the broker's transcript, the
agent's event stream, its answer and a run record bound to the commit, wheel and fingerprint) is kept in
--agent-evidence and bound into the receipt by digest; check re-reads it and judges every cell again. The private
homes are deleted afterwards; when Codex refreshed its tokens meanwhile, the refreshed auth.json goes back to the
caller's Codex home unless that file changed meanwhile. Without the CLI, a login or the Codex auth.json the column is
recorded unavailable with one fixed reason.

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
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
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

SCHEMA = "tgmcp-consumer-matrix/v4"
PROJECT = "telegram-search-mcp"
HARNESS = Path(__file__).resolve()
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# The schema fingerprint these cells were written for (the 0.58 surface, 64 tools, with the final acceptance's typed
# prepare_text_send and update_draft refusals). run refuses another census and check another receipt
# (schema_mismatch): a deliberate schema change must update the cells, their counts and this.
MATRIX_SCHEMA_FINGERPRINT = "8d8fc25c73c336b0ce32a8c965b59f57770b8aee9d971aacb0165d0162e00d28"
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
    ("refusal.text_options", "restricted", "a styled, or styled and topic, prepare_text_send returns "
     "capability_disabled with exactly its missing_capabilities, as the reply tools do"),
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
    ("search.boolean", "full", "search_messages with the Boolean form (all, any, none) returns exactly the rehydrated "
     "matches, each once"),
    ("search.filters", "full", "search_messages filtered by sender, direction and forum topic returns exactly the "
     "matching messages"),
    ("documents.xlsx", "full", "read_spreadsheet of a synthetic XLSX returns its sheet catalog and a selected range, "
     "formulas as data"),
    ("documents.pptx", "full", "read_presentation of a synthetic PPTX returns its slide catalog and selected slides "
     "with their notes"),
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
    ("prepare.styled_caption", "full", "a styled caption is prepared for a file and a file reply under a policy that "
     "allows it"),
    ("prepare.reply_sources_058", "full", "a reply to a media caption with a text link, to a custom emoji and to a "
     "voice note captioned with a media timestamp is prepared under a policy that allows them"),
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
    "refusal.reply_families": 22, "refusal.text_options": 5, "refusal.guard_every_tool": 65, "refusal.prompt_policy": 2,
    "refusal.config_stale": 3, "image.png": 6, "image.pdf_page": 5, "image.attachment_page": 5,
    "cursor.list_chats": 4, "cursor.read_history": 6, "cursor.search_messages": 6, "cursor.list_drafts": 5,
    "search.boolean": 6, "search.filters": 7, "documents.xlsx": 6, "documents.pptx": 5,
    "recovery.restart_listing": 4, "recovery.search_again": 2, "recovery.check_send_status": 8,
    "recovery.enable_capability": 5, "recovery.update_installation": 3, "recovery.check_connection": 4,
    "recovery.connect_account": 3, "caps.manifest": 8, "caps.input_bounds": 6, "caps.input_bounds_058": 13,
    "prepare.styled_caption": 4, "prepare.reply_sources_058": 7,
    "approval.declined_flag": 5, "approval.denial": 8, "approval.timeout": 6, "approval.denial_every_family": 34,
    "bundle.install": 8, "bundle.upgrade": 9, "bundle.rollback": 7, "bundle.uninstall": 8,
}
REAL_DIALOG_CHECKS = {"approval.denial": 1}
# Structured results every session validates in a passing run, and the tools they came from (every tool).
SURFACE_COUNTS = {"structured_results_validated": 183, "tools_with_validated_results": 64}
# Owner or real-account cells: listed so the receipt shows them, never run or passed by this harness. The Inspector
# CLI's synthetic cells (INSPECTOR_CELLS) and the Codex and Claude Code synthetic cells (AGENT_CELLS) are the
# harness's own columns since the final acceptance.
OWNER_CELLS = (
    ("official_sdk.real_account", "official SDK", "real account"),
    ("inspector_cli.real_account", "MCP Inspector CLI", "real account"),
    ("codex.real_account", "Codex (codex exec)", "real account"),
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
    "installer_failed", "inspector_invalid", "cache_invalid", "node_invalid", "fetch_failed", "agent_invalid",
    "agent_failed", "evidence_invalid", "evidence_missing", "evidence_mismatch", "client_config_changed"})
RECEIPT_KEYS = frozenset({"schema", "acceptance", "harness", "gate", "commit", "tree", "version", "wheel", "build",
                          "install", "installer", "sdk", "schema_fingerprint", "census", "surface", "approval",
                          "cells", "inspector", "agents", "owner_cells", "summary"})
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
TEXT_OPTIONS_DISABLED = "text draft needs capabilities the owner has not enabled: missing_capabilities"
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
# A document in the second chat whose caption holds a hidden link: the 0.58 media TextUrl reply source (v24).
MEDIA_LINK_MESSAGE, MEDIA_LINK_CAPTION, MEDIA_LINK_URL = 69, "Matrix caption link", "https://example.org/caption"
CUSTOM_EMOJI_SOURCE = 67  # the custom emoji reply source of FAMILY_SOURCES (v25)
# A voice note in the second chat whose caption marks a media timestamp within its duration (v28).
VOICE_TIMESTAMP_MESSAGE, VOICE_TIMESTAMP_CAPTION, VOICE_SECONDS = 73, "Matrix at 0:05", 30
# A forum supergroup outside the Main list (the topic filter): (message id, topic, text, outgoing).
FORUM_CHAT, FORUM_SUPERGROUP, FORUM_TOPIC = -91003, 91003, 41  # a synthetic id, not a real -100 one
FORUM_MESSAGES = ((70, FORUM_TOPIC, "Matrix forum topic note", False),
                  (71, FORUM_TOPIC + 1, "Matrix forum other note", False),
                  (72, FORUM_TOPIC, "Matrix forum own topic note", True))
PEER_USER = FIRST_CHAT + 1000  # the first chat's peer, the incoming messages' sender
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

# The agent columns: a real agent CLI (codex exec, claude -p) given one fixed request per cell, with a private
# configuration home whose only MCP server is the installed proxy, against a synthetic broker of its own. Each cell
# is decided on the broker's own transcript of what was asked and answered, the agent's end and the tools it used;
# the wording of its answer is advisory.
AGENT_COLUMNS = (("codex", "Codex (codex exec)"), ("claude_code", "Claude Code (claude -p)"))
AGENT_COLUMN_IDS = tuple(column for column, _ in AGENT_COLUMNS)
ALIAS = "work"  # the alias cell's account: a short token, so its broker socket fits the 104-byte limit
AGENT_PREAMBLE = ("You are helping the owner of a Telegram account through the {server} MCP server. Use only its "
                  "tools for Telegram; there is nothing to do in the shell or in the working directory.\n\n")
EDIT_TEXT, FAMILY_REPLY_TEXT = "Matrix synthetic edited text", "Matrix synthetic family reply"
REFUSAL_REPLY_TEXT = "Matrix synthetic refusal probe"
FAMILY_MESSAGE = 61  # the mention source: under the restricted policy it needs reply_identity_targets too
# (id, session, the fixed request after the preamble, what passing proves).
AGENT_CELLS = (
    ("agent.census", "full",
     "Check that the Telegram server is ready: call its _manifest tool once with check_broker set to true, then "
     "tell me the compatibility status, how many tools are available and the schema fingerprint. Do nothing else.",
     "the model checks the broker through _manifest(check_broker=true) and changes nothing"),
    ("agent.read", "full",
     "Read the latest messages of the chat “Matrix synthetic chat 01” (chat_id 91001) and quote the text or "
     "caption of the newest message. Do not send or change anything.",
     "the model reads the chat and the broker returns its newest message"),
    ("agent.search_cursor", "full",
     "Search the chat “Matrix synthetic chat 01” (chat_id 91001) for “Matrix synthetic” with "
     "search_messages, one result per page (limit 1), and follow next_cursor until there are no more pages. List "
     "every match. Do not send or change anything.",
     "the model pages a search one match at a time by its cursor to the end"),
    ("agent.refusal", "restricted",
     f"In the chat “Matrix synthetic chat 02” (chat_id 91002), prepare a reply to message {FAMILY_MESSAGE} "
     f"that says “{REFUSAL_REPLY_TEXT}”. Only prepare it; do not send it.",
     "a reply refused capability_disabled (next_action enable_capability) stops the model: no workaround, no draft"),
    ("agent.approval_timeout", "full",
     f"Send the text “{TIMEOUT_PROBE}” to the chat “Matrix synthetic chat 01” (chat_id 91001). I "
     "approve this send. The owner confirms each send in a local dialog; ask once, and do not retry if it is not "
     "confirmed.",
     "an approved send whose owner dialog times out returns timeout once and is never dispatched"),
    ("agent.alias_server", "alias",
     f"Use the {{server}} server, which serves the owner's account “{ALIAS}”. Read the latest "
     "messages of the chat “Matrix synthetic chat 01” (chat_id 91001) there and quote the text or caption of "
     "the newest message. Do not send or change anything.",
     "an alias server reaches only its own broker, which pins the account and returns the newest message"),
    ("agent.album_prepare", "full",
     "Create two small text files named matrix-a.txt and matrix-b.txt, each with one short line of text, and prepare "
     "one album of both files as documents for the chat “Matrix synthetic chat 01” (chat_id 91001). Only "
     "prepare it; do not send it.",
     "the model creates two artifacts and prepares one documents album of them (0.56) without sending"),
    ("agent.own_message_prepare", "full",
     f"My own message {OWN_TEXT_MESSAGE} in the chat “Matrix synthetic chat 01” (chat_id 91001) needs new "
     f"wording. Prepare an edit so that it reads “{EDIT_TEXT}”. Only prepare the edit; do not apply "
     "it.",
     "the model prepares an edit of the account's own message (0.57) without applying it"),
    ("agent.family_reply", "full",
     f"In the chat “Matrix synthetic chat 02” (chat_id 91002), prepare a reply to message {FAMILY_MESSAGE} "
     f"that says “{FAMILY_REPLY_TEXT}”. Only prepare it; do not send it.",
     "the model prepares a reply to a 0.58 reply-family source (a mention) without sending"),
)
AGENT_IDS = tuple(cell for cell, _, _, _ in AGENT_CELLS)
# Decisive checks of each passing agent cell (five common ones: the agent's clean exit, its completed turn, only
# the matrix server's tools, the broker of this wheel and an answer), frozen like CELL_CHECKS.
AGENT_CHECKS = {"agent.census": 7, "agent.read": 7, "agent.search_cursor": 9, "agent.refusal": 8,
                "agent.approval_timeout": 9, "agent.alias_server": 8, "agent.album_prepare": 8,
                "agent.own_message_prepare": 7, "agent.family_reply": 7}
# Checks on the answer's wording: reported, never decisive.
AGENT_ADVISORY = {"agent.census": ("answer_compatible", "answer_tool_count"), "agent.read": ("answer_newest",),
                  "agent.search_cursor": ("answer_matches",), "agent.refusal": ("answer_capability",),
                  "agent.approval_timeout": ("answer_timeout",), "agent.alias_server": ("answer_newest",),
                  "agent.album_prepare": ("answer_prepared",), "agent.own_message_prepare": ("answer_prepared",),
                  "agent.family_reply": ("answer_prepared",)}
# Why a column was not run, as the receipt records it.
AGENT_UNAVAILABLE = frozenset({"cli_not_given", "cli_unusable", "auth_missing", "not_logged_in"})
AGENT_SECONDS, AGENT_ATTEMPTS = 420, 3
AGENT_EVIDENCE = ("server.jsonl", "events.jsonl", "answer.txt", "run.json")
MAX_EVIDENCE_BYTES = 16 << 20
# A provider failure before any tool reached the broker says nothing about the agent: the cell is retried. Both marker
# sets are matched only against what the CLI itself reports as failing (provider_errors), never the model's words.
TRANSIENT_MARKERS = ("at capacity", "rate limit", "overloaded", "temporarily unavailable", "stream disconnected",
                     "internal server error", "503 service unavailable", "502 bad gateway")
LOGIN_MARKERS = ("401 unauthorized", "not logged in", "please run /login", "authentication_failed",
                 "invalid api key", "please sign in", "refresh token")
# Agent item types that are not tool use (Codex `--json` items).
CODEX_QUIET_ITEMS = frozenset({"agent_message", "reasoning", "error", "todo_list"})
# Only the network proxy settings reach an agent from the caller's environment, and a Claude credential the
# operator exports for the run; nothing else of the caller's environment does.
AGENT_NETWORK_ENV = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
CLAUDE_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
# Claude Code keeps the default configuration location only for its login (on macOS the Keychain, found through the
# caller's HOME and USER): no setting source, no auto memory, no skills or commands, only the --mcp-config server,
# no built-in tool, no transcript, no telemetry or auto-update; claude_configuration proves nothing of it changed.
CLAUDE_SETTINGS = '{"autoMemoryEnabled":false}'
CLAUDE_QUIET_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
CLAUDE_GUARDED = (".claude.json", ".claude/settings.json", ".claude/settings.local.json")


class MatrixRefusal(Exception):
    """A fixed refusal code; detail holds harness-derived facts, never caller input."""

    def __init__(self, code: str, detail: object = None):
        if code not in CODES:
            raise AssertionError("unknown refusal code")
        super().__init__(code)
        self.code = code
        self.detail = detail


class MatrixInterrupted(BaseException):
    """SIGTERM or SIGHUP during run or check, raised so that every finally block runs: agents killed, brokers and
    drivers stopped, the temporary homes and the copied Codex credential removed, refreshed credentials returned. A
    signal that lands during a cleanup block (a normal teardown included) raises only once that block has finished."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


_AGENT_GROUPS: set = set()  # the process groups (own sessions) of the agents running now
_INTERRUPT = {"deferred": 0, "pending": None, "raised": None}


def _interrupt(signum, frame) -> None:
    """Kill every running agent's session, then raise MatrixInterrupted, once: a repeat signal never cuts the
    cleanup short. While a child is being started and registered, and for the whole of every cleanup block, the
    Codex write-back included (deferred_interrupts), the raise waits until the block has finished."""
    if _INTERRUPT["raised"] is not None:
        return
    for group in list(_AGENT_GROUPS):
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if _INTERRUPT["deferred"]:
        _INTERRUPT["pending"] = signum
        return
    _INTERRUPT["raised"] = signum
    raise MatrixInterrupted(signum)


@contextlib.contextmanager
def deferred_interrupts():
    """Around starting a child and handing it to the code whose finally stops it, and around the whole of every
    cleanup block (finally), so that a first signal during a normal teardown never cuts it short: an interrupt raises
    afterwards."""
    _INTERRUPT["deferred"] += 1
    try:
        yield
    finally:
        _INTERRUPT["deferred"] -= 1
        if not _INTERRUPT["deferred"] and _INTERRUPT["pending"] is not None:
            signum, _INTERRUPT["pending"] = _INTERRUPT["pending"], None
            _interrupt(signum, None)


@contextlib.contextmanager
def interrupt_handlers():
    """SIGTERM and SIGHUP raise MatrixInterrupted for the duration (the main thread's run and check)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.signal(number, _interrupt) for number in (signal.SIGTERM, signal.SIGHUP)}
    _INTERRUPT.update(deferred=0, pending=None, raised=None)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


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


def media_evidence(text: str) -> tuple:
    """A reply target's media record (JSON, or for a hidden link one inert JSON layer): its kind and each caption
    entity as (type, covered text, payload: a text link's URL or a media timestamp's seconds); else (None, [])."""
    _, marker, record = text.partition("Media target: ")
    try:
        value = json.loads(record) if marker else {}
        rows = [(row.get("type"), row.get("text"), row.get("provider_url", row.get("media_timestamp")))
                for row in value["caption"]["entities"]]
        return value["kind"], rows
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, []


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


def _ooxml(parts: dict) -> bytes:
    """A deterministic OOXML package: each part at a fixed timestamp, deflated, in the given order."""
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)
    return buffer.getvalue()


_PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
XLSX_TEXT, XLSX_HIDDEN_TEXT = "Matrix synthetic cell", "Matrix hidden cell"
PPTX_SLIDES = (("Matrix synthetic slide one", "Matrix synthetic note one", False),
               ("Matrix synthetic slide two", None, True))  # (text, notes, hidden)


def synthetic_xlsx() -> bytes:
    """A workbook of two sheets: "Matrix" (visible: A1 an inline string, B1 the number 2, A2 the formula B1*2 with its
    cached value 4, B2 empty) and "Hidden" (hidden: A1 an inline string). Hand-authored, no spreadsheet library."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"

    def inline(address, text):
        return f'<c r="{address}" t="inlineStr"><is><t>{text}</t></is></c>'
    sheets = (("Matrix", "visible", f'<row r="1">{inline("A1", XLSX_TEXT)}<c r="B1"><v>2</v></c></row>'
               '<row r="2"><c r="A2"><f>B1*2</f><v>4</v></c></row>'),
              ("Hidden", "hidden", f'<row r="1">{inline("A1", XLSX_HIDDEN_TEXT)}</row>'))
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{_CT}"><Default Extension="rels" ContentType="application/'
                               'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType='
                               '"application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/'
                               'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                               + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{sheet}"/>'
                                         for i in (1, 2)) + "</Types>",
        "_rels/.rels": f'<Relationships xmlns="{_PKGREL}"><Relationship Id="main" Type="{_DOCREL}/officeDocument" '
                       'Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{main}" xmlns:r="{_DOCREL}"><sheets>'
                           + "".join(f'<sheet name="{name}" sheetId="{i}" state="{state}" r:id="rId{i}"/>'
                                     for i, (name, state, _) in enumerate(sheets, 1)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKGREL}">'
                                      + "".join(f'<Relationship Id="rId{i}" Type="{_DOCREL}/worksheet" '
                                                f'Target="worksheets/sheet{i}.xml"/>' for i in (1, 2))
                                      + "</Relationships>",
    }
    for i, (_, _, rows) in enumerate(sheets, 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = f'<worksheet xmlns="{main}"><sheetData>{rows}</sheetData></worksheet>'
    return _ooxml(parts)


def synthetic_pptx() -> bytes:
    """A presentation of PPTX_SLIDES: slide 1 with speaker notes, slide 2 hidden without. Hand-authored."""
    pml, dml = "http://schemas.openxmlformats.org/presentationml/2006/main", \
        "http://schemas.openxmlformats.org/drawingml/2006/main"
    office = "application/vnd.openxmlformats-officedocument.presentationml."

    def page(kind, text, hidden=False):
        return (f'<p:{kind} xmlns:p="{pml}" xmlns:a="{dml}" xmlns:r="{_DOCREL}"{" show=\"0\"" if hidden else ""}>'
                '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="synthetic"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                f'<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody>'
                f'</p:sp></p:spTree></p:cSld></p:{kind}>')
    parts = {"_rels/.rels": f'<Relationships xmlns="{_PKGREL}"><Relationship Id="main" Type="{_DOCREL}/'
                            'officeDocument" Target="ppt/presentation.xml"/></Relationships>'}
    ids, rels, overrides = [], [], []
    for index, (text, notes, hidden) in enumerate(PPTX_SLIDES, 1):
        parts[f"ppt/slides/slide{index}.xml"] = page("sld", text, hidden)
        ids.append(f'<p:sldId id="{255 + index}" r:id="slide{index}"/>')
        rels.append(f'<Relationship Id="slide{index}" Type="{_DOCREL}/slide" Target="slides/slide{index}.xml"/>')
        overrides.append(f'<Override PartName="/ppt/slides/slide{index}.xml" ContentType="{office}slide+xml"/>')
        if notes is not None:
            parts[f"ppt/notesSlides/notes{index}.xml"] = page("notes", notes)
            parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = (
                f'<Relationships xmlns="{_PKGREL}"><Relationship Id="notes" Type="{_DOCREL}/notesSlide" '
                f'Target="../notesSlides/notes{index}.xml"/></Relationships>')
            parts[f"ppt/notesSlides/_rels/notes{index}.xml.rels"] = (
                f'<Relationships xmlns="{_PKGREL}"><Relationship Id="slide" Type="{_DOCREL}/slide" '
                f'Target="../slides/slide{index}.xml"/></Relationships>')
            overrides.append(f'<Override PartName="/ppt/notesSlides/notes{index}.xml" '
                             f'ContentType="{office}notesSlide+xml"/>')
    parts["ppt/presentation.xml"] = (f'<p:presentation xmlns:p="{pml}" xmlns:r="{_DOCREL}"><p:sldIdLst>'
                                     + "".join(ids) + "</p:sldIdLst></p:presentation>")
    parts["ppt/_rels/presentation.xml.rels"] = f'<Relationships xmlns="{_PKGREL}">' + "".join(rels) + "</Relationships>"
    parts["[Content_Types].xml"] = (f'<Types xmlns="{_CT}"><Default Extension="xml" ContentType="application/xml"/>'
                                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                                    f'relationships+xml"/><Override PartName="/ppt/presentation.xml" ContentType='
                                    f'"{office}presentation.main+xml"/>' + "".join(overrides) + "</Types>")
    return _ooxml(parts)


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


def media_link_message() -> dict:
    """A document in the second chat whose caption holds a text link (the 0.58 media TextUrl reply source, v24), with
    the complete file shape a reply's media projection reads."""
    file = _remote_file(2, "matrix-synthetic-caption-link", 21)
    offset = MEDIA_LINK_CAPTION.index("link")
    message = _message(MEDIA_LINK_MESSAGE, {"@type": "messageDocument", "caption": _text(MEDIA_LINK_CAPTION, [
        {"@type": "textEntity", "offset": offset, "length": len(MEDIA_LINK_CAPTION) - offset,
         "type": {"@type": "textEntityTypeTextUrl", "url": MEDIA_LINK_URL}}]),
        "document": {"@type": "document", "file_name": "matrix-caption-link.txt", "mime_type": "text/plain",
                     "document": file}}, outgoing=False, date=1700000400)
    return {**message, "chat_id": FAMILY_CHAT,
            "sender_id": {"@type": "messageSenderUser", "user_id": FAMILY_CHAT + 1000}}


def _remote_file(file_id: int, unique: str, size: int) -> dict:
    """A complete TDLib file object, as a reply's media projection reads it (never downloaded)."""
    return {"@type": "file", "id": file_id, "size": size, "expected_size": size,
            "local": {"@type": "localFile", "path": "", "can_be_downloaded": True, "can_be_deleted": False,
                      "is_downloading_active": False, "is_downloading_completed": False, "download_offset": 0,
                      "downloaded_prefix_size": 0, "downloaded_size": 0},
            "remote": {"@type": "remoteFile", "id": unique, "unique_id": unique, "is_uploading_active": False,
                       "is_uploading_completed": True, "uploaded_size": size}}


def voice_timestamp_message() -> dict:
    """A voice note in the second chat whose caption marks 0:05 as a media timestamp (the 0.58 MediaTimestamp reply
    source, v28), within its 30 s duration."""
    offset = VOICE_TIMESTAMP_CAPTION.index("0:05")
    message = _message(VOICE_TIMESTAMP_MESSAGE, {"@type": "messageVoiceNote", "is_listened": False,
        "caption": _text(VOICE_TIMESTAMP_CAPTION, [
            {"@type": "textEntity", "offset": offset, "length": 4,
             "type": {"@type": "textEntityTypeMediaTimestamp", "media_timestamp": 5}}]),
        "voice_note": {"@type": "voiceNote", "duration": VOICE_SECONDS, "waveform": base64.b64encode(bytes(16)).decode(),
                       "mime_type": "audio/ogg", "voice": _remote_file(3, "matrix-synthetic-voice-note", 4096)}},
        outgoing=False, date=1700000410)
    return {**message, "chat_id": FAMILY_CHAT,
            "sender_id": {"@type": "messageSenderUser", "user_id": FAMILY_CHAT + 1000}}


def forum_message(message_id: int) -> dict:
    """A message of the synthetic forum in its topic; the account's own ones are outgoing."""
    _, topic, text, outgoing = next(row for row in FORUM_MESSAGES if row[0] == message_id)
    message = _message(message_id, {"@type": "messageText", "text": _text(text)}, outgoing=outgoing,
                       date=1700000500 + message_id)
    return {**message, "chat_id": FORUM_CHAT, "topic_id": {"@type": "messageTopicForum", "forum_topic_id": topic}}


def forum_chat() -> dict:
    return {"@type": "chat", "id": FORUM_CHAT, "title": "Matrix synthetic forum",
            "type": {"@type": "chatTypeSupergroup", "supergroup_id": FORUM_SUPERGROUP, "is_channel": False},
            "chat_lists": [], "positions": [], "unread_count": 0, "unread_mention_count": 0,
            "unread_reaction_count": 0, "is_marked_as_unread": False, "has_protected_content": False}


def forum_topic(topic: int) -> dict:
    return {"@type": "forumTopic", "info": {
        "@type": "forumTopicInfo", "chat_id": FORUM_CHAT, "forum_topic_id": topic, "name": f"Matrix topic {topic}",
        "creation_date": 1700000000, "creator_id": {"@type": "messageSenderUser", "user_id": PEER_USER},
        "is_general": False, "is_outgoing": False, "is_closed": False, "is_hidden": False}}


def synthetic_messages(chat_id: int = FIRST_CHAT) -> dict[int, dict]:
    if chat_id == FAMILY_CHAT:
        return {**{row[0]: family_message(row[0]) for row in FAMILY_SOURCES}, MEDIA_LINK_MESSAGE: media_link_message(),
                VOICE_TIMESTAMP_MESSAGE: voice_timestamp_message()}
    if chat_id == FORUM_CHAT:
        return {row[0]: forum_message(row[0]) for row in FORUM_MESSAGES}
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
            if chat_id == FORUM_CHAT:
                return forum_chat()
            return {"@type": "error", "code": 400, "message": "CHAT_NOT_FOUND"}
        if kind == "getSupergroup":
            if request.get("supergroup_id") == FORUM_SUPERGROUP:
                return {"@type": "supergroup", "id": FORUM_SUPERGROUP, "is_forum": True, "is_channel": False}
            return {"@type": "error", "code": 400, "message": "SUPERGROUP_NOT_FOUND"}
        if kind == "getForumTopic":
            topic = request.get("forum_topic_id")
            if request.get("chat_id") == FORUM_CHAT and topic in {row[1] for row in FORUM_MESSAGES}:
                return forum_topic(topic)
            return {"@type": "error", "code": 404, "message": "Not Found"}
        if kind == "searchPublicChat":
            if request.get("username") == PUBLIC_USERNAME:
                return chat_object(FIRST_CHAT)
            return {"@type": "error", "code": 400, "message": "USERNAME_NOT_OCCUPIED"}
        if kind in ("getMessage", "getMessageProperties"):
            chat_id = request.get("chat_id")
            messages = synthetic_messages(chat_id) if chat_id in (FIRST_CHAT, FAMILY_CHAT, FORUM_CHAT) else {}
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
        and answers in one page; a media filter finds nothing; searchChatMessages keeps only its sender_id and
        topic_id when given. The first chat and the forum have a history; searchMessages searches the first chat."""
        chat = request.get("chat_id")
        source = (synthetic_messages() if kind == "searchMessages"
                  else synthetic_messages(chat) if chat in (FIRST_CHAT, FORUM_CHAT) else {})
        messages = sorted(source.values(), key=lambda message: -message["id"])
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
            for key in ("sender_id", "topic_id") if kind == "searchChatMessages" else ():
                if request.get(key) is not None:
                    messages = [message for message in messages if message.get(key) == request[key]]
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


def record_calls(broker, record) -> None:
    """The broker's own transcript: each frame a connection received and each dispatched operation with its payload
    and its result (or its fixed error code), numbered by connection. A connection that received only its handshake
    was a diagnostic check (_manifest with check_broker=true)."""
    import itertools
    from telegram_search_mcp import broker as module
    numbers, local = itertools.count(1), threading.local()
    receive = module.receive_request

    def received(*args, **kwargs):
        request = receive(*args, **kwargs)
        operation = request.get("operation") if isinstance(request, dict) else None
        if operation == "handshake":
            local.connection = next(numbers)
        record({"frame": operation if isinstance(operation, str) else "invalid",
                "connection": getattr(local, "connection", 0)})
        return request

    dispatch = broker._dispatch

    def dispatched(request, *, publish=None):
        entry = {"operation": request.get("operation"), "connection": getattr(local, "connection", 0),
                 "payload": request.get("payload")}
        try:
            result = dispatch(request, publish=publish)
        except Exception as error:
            record({**entry, "error": broker._error_code(error)})
            raise
        record({**entry, "result": result})
        return result

    module.receive_request = received
    broker._dispatch = dispatched


def synthetic_alias(home: Path, alias: str):
    """Register `alias` in the synthetic home as `account-add` leaves it (the registry, an owner-only root) with the
    default account's policy; the alias's AccountPaths."""
    from telegram_search_mcp import account_registry, config
    account_registry.add_alias(alias, home)
    account = config.AccountPaths.for_alias(alias, home)
    account.root.mkdir(mode=0o700)
    fd = os.open(account.policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(config.RUNTIME_POLICY_PATH.read_bytes())
    return account


def broker_main(args: argparse.Namespace) -> int:
    home = Path(os.environ["HOME"])
    import telegram_search_mcp
    root = Path(args.package_root).resolve()
    if not Path(telegram_search_mcp.__file__).resolve().is_relative_to(root):
        print("BROKER_PACKAGE_MISMATCH", file=sys.stderr)
        return 3
    from telegram_search_mcp import config
    from telegram_search_mcp.broker import Broker
    from telegram_search_mcp.contract import schema_fingerprint
    from telegram_search_mcp.tdjson import TDLibClient

    record = JsonLog(home / "matrix-broker.jsonl")
    account = synthetic_alias(home, args.account) if args.account is not None else None
    record({"broker": {"version": telegram_search_mcp.__version__, "schema_fingerprint": schema_fingerprint(),
                       "alias": args.account or "default"}})

    def refuse_credentials():
        raise RuntimeError("the synthetic account has no credentials")

    client = TDLibClient(raw=SyntheticTDLib(authorized=args.profile != "unauthorized", record=record),
                         session_directory=home / "synthetic-session", credential_loader=refuse_credentials)
    broker = Broker(socket_path=config.BROKER_SOCKET_PATH if account is None else account.socket,
                    lock_path=config.BROKER_LOCK_PATH if account is None else account.broker_lock,
                    client_factory=lambda: client,
                    policy=config.load_runtime_policy(config.RUNTIME_POLICY_PATH if account is None else account.policy),
                    download_source_root=home / "synthetic-session" / "files", account=account,
                    approval_prompt=approval_prompt(record, real_dialog=args.approval == "real_dialog"))
    record_calls(broker, record)
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
                  "search.boolean", "search.filters", "documents.xlsx", "documents.pptx", "prepare.styled_caption",
                  "prepare.reply_sources_058", "approval.declined_flag", "approval.denial", "approval.timeout",
                  "recovery.check_send_status")

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

    # -- full session: the 0.51 forms and the 0.58 prepares an installed consumer had not called (final audit) --
    @staticmethod
    def anchors(page: dict) -> list:
        return [row.get("anchor", {}).get("message_id") for row in page.get("results", [])]

    @staticmethod
    def rehydrated(page: dict, *, topic: int | None = None) -> bool:
        """Every result a complete match with its current text (and, for a topic filter, that forum topic)."""
        rows = page.get("results", [])
        return bool(rows) and all(row.get("status") == "match" and row.get("coverage_complete") is True
                                  and isinstance((row.get("message") or {}).get("text"), dict)
                                  and (topic is None or (row.get("message") or {}).get("topic")
                                       == {"kind": "forum", "id": topic}) for row in rows)

    async def search(self, consumer, target: int, query, **filters) -> dict:
        return structured(await consumer.call("search_messages", {"target": target, "query": query, "mode": "latest",
                                                                  **filters}))

    async def cell_search_boolean(self, cell, consumer, tools) -> None:
        """F6: the Boolean form (seeded by TDLib, matched locally over the current full text) over the first chat's
        55 (incoming source), 56 (own text) and 57 (own caption)."""
        pages = {}
        for label, query, expected in (("all_none", {"all": ["synthetic", "own"], "none": ["caption"]},
                                        [OWN_TEXT_MESSAGE]),
                                       ("any", {"any": ["source", "caption"]}, [OWN_DOCUMENT_MESSAGE, SOURCE_MESSAGE]),
                                       ("dedup", {"any": ["matrix", "synthetic"]},
                                        [OWN_DOCUMENT_MESSAGE, OWN_TEXT_MESSAGE, SOURCE_MESSAGE])):
            pages[label] = await self.search(consumer, FIRST_CHAT, query)
            cell.expect(f"{label}:rows", self.anchors(pages[label]) == expected)
        cell.expect("semantics", all(page.get("status") == "partial" and page.get("stop_reason")
                                     == "provider_end_unverified" and (page.get("scope") or {}).get(
                                         "matching_semantics") == "local_nfkc_casefold_whitespace_substring"
                                     for page in pages.values()))
        cell.expect("rehydrated", all(self.rehydrated(page) for page in pages.values()))
        branches = pages["dedup"].get("branch_coverage") or []
        cell.expect("branches", [(b.get("query"), b.get("scanned_candidates")) for b in branches]
                    == [("matrix", 3), ("synthetic", 3)])

    async def cell_search_filters(self, cell, consumer, tools) -> None:
        """F6: the sender, direction and forum-topic filters, alone and together, and with the Boolean form."""
        own = {"kind": "user", "id": ACCOUNT_ID}
        sender = await self.search(consumer, FIRST_CHAT, "matrix", sender={"kind": "user", "id": PEER_USER})
        cell.expect("sender", self.anchors(sender) == [SOURCE_MESSAGE] and self.rehydrated(sender)
                    and (sender.get("scope") or {}).get("sender") == {"kind": "user", "id": PEER_USER})
        outgoing = await self.search(consumer, FIRST_CHAT, "matrix", direction="outgoing")
        cell.expect("outgoing", self.anchors(outgoing) == [OWN_DOCUMENT_MESSAGE, OWN_TEXT_MESSAGE]
                    and all((row.get("message") or {}).get("is_outgoing") is True
                            for row in outgoing.get("results", [])))
        incoming = await self.search(consumer, FIRST_CHAT, "matrix", direction="incoming")
        cell.expect("incoming", self.anchors(incoming) == [SOURCE_MESSAGE])
        topic = await self.search(consumer, FORUM_CHAT, "forum", topic={"id": FORUM_TOPIC})
        cell.expect("topic", self.anchors(topic) == [72, 70])
        cell.expect("topic_membership", self.rehydrated(topic, topic=FORUM_TOPIC)
                    and (topic.get("scope") or {}).get("topic") == {"kind": "forum", "id": FORUM_TOPIC})
        combined = await self.search(consumer, FORUM_CHAT, "forum", topic={"id": FORUM_TOPIC}, sender=own,
                                     direction="outgoing")
        cell.expect("combined", self.anchors(combined) == [72] and self.rehydrated(combined, topic=FORUM_TOPIC))
        boolean = await self.search(consumer, FORUM_CHAT, {"all": ["forum"], "none": ["own"]},
                                    topic={"id": FORUM_TOPIC}, direction="incoming")
        cell.expect("boolean_topic", self.anchors(boolean) == [70] and self.rehydrated(boolean, topic=FORUM_TOPIC))

    async def cell_documents_xlsx(self, cell, consumer, tools) -> None:
        """F9: a synthetic XLSX through read_spreadsheet: its catalog, then a range of the visible sheet (an inline
        string, a number, a formula as data with its cached value, an empty cell) and a cell of the hidden one."""
        artifact = await self.artifact(consumer, "matrix-synthetic.xlsx", synthetic_xlsx())
        catalog = structured(await consumer.call("read_spreadsheet", {"artifact_id": artifact}))
        cell.expect("catalog", catalog.get("status") == "catalog" and (catalog.get("scope") or {}).get("catalog") == [
            {"index": 1, "name": "Matrix", "state": "visible"}, {"index": 2, "name": "Hidden", "state": "hidden"}])
        page = structured(await consumer.call("read_spreadsheet", {
            "artifact_id": artifact, "selections": [{"sheet_index": 1, "range": "A1:B2"}]}))
        cells = {row.get("address"): row for row in page.get("cells", [])}
        cell.expect("range", page.get("status") == "complete" and page.get("scope_complete") is True
                    and [row.get("address") for row in page.get("cells", [])] == ["A1", "B1", "A2", "B2"])
        cell.expect("text", (cells.get("A1", {}).get("value_type"), cells.get("A1", {}).get("value"))
                    == ("inlineStr", XLSX_TEXT) and cells.get("B1", {}).get("value") == "2")
        cell.expect("formula", {key: cells.get("A2", {}).get(key) for key in ("value_type", "value", "formula",
                                                                              "formula_kind")}
                    == {"value_type": "n", "value": "4", "formula": "B1*2", "formula_kind": "normal"})
        cell.expect("empty", cells.get("B2", {}).get("value_type") == "blank"
                    and cells.get("B2", {}).get("value") is None)
        hidden = structured(await consumer.call("read_spreadsheet", {
            "artifact_id": artifact, "selections": [{"sheet_index": 2, "range": "A1"}]}))
        cell.expect("hidden_sheet", hidden.get("status") == "complete"
                    and [row.get("value") for row in hidden.get("cells", [])] == [XLSX_HIDDEN_TEXT])

    async def cell_documents_pptx(self, cell, consumer, tools) -> None:
        """F9: a synthetic PPTX through read_presentation: its catalog, slide 1 with its notes, slide 2 without."""
        artifact = await self.artifact(consumer, "matrix-synthetic.pptx", synthetic_pptx())
        catalog = structured(await consumer.call("read_presentation", {"artifact_id": artifact}))
        cell.expect("catalog", catalog.get("status") == "catalog" and (catalog.get("scope") or {}).get("catalog") == [
            {"index": 1, "hidden": False, "has_notes": True}, {"index": 2, "hidden": True, "has_notes": False}])
        first = structured(await consumer.call("read_presentation", {"artifact_id": artifact, "slides": [1],
                                                                     "include_notes": True}))
        slides = first.get("slides") or [{}]
        cell.expect("slide_text", first.get("status") == "complete" and len(slides) == 1 and slides[0].get("index") == 1
                    and PPTX_SLIDES[0][0] in (slides[0].get("text") or ""))
        cell.expect("notes", PPTX_SLIDES[0][1] in (slides[0].get("notes") or ""))
        cell.expect("coverage", first.get("selection_complete") is True and first.get("full_content_complete") is False)
        second = structured(await consumer.call("read_presentation", {"artifact_id": artifact, "slides": [2]}))
        rows = second.get("slides") or [{}]
        cell.expect("without_notes", second.get("status") == "complete"
                    and PPTX_SLIDES[1][0] in (rows[0].get("text") or "") and rows[0].get("notes") is None)

    async def cell_prepare_styled_caption(self, cell, consumer, tools) -> None:
        """F13: caption styles (formatted_caption_send) on a file and on a file reply, prepared, never sent."""
        prompts, png = len(self.owner_prompts()), await self.artifact(consumer, "matrix-styled.png", synthetic_png())
        image = {"recipient": FIRST_CHAT, "artifact_id": png, "display_name": "matrix-styled.png",
                 "mime_type": "image/png", "caption": "Matrix caption", "outgoing_entities": STYLED}
        draft = structured(await consumer.call("prepare_artifact_send", image))
        cell.expect("artifact", draft.get("status") == "prepared" and draft.get("caption") == "Matrix caption"
                    and draft.get("outgoing_entities") == STYLED
                    and re.fullmatch(r"[0-9a-f]{64}", str(draft.get("preview_sha256"))))
        pending = structured(await consumer.call("get_draft", {"draft_id": draft.get("draft_id")})).get("draft") or {}
        cell.expect("get_draft", pending.get("outgoing_entities") == STYLED
                    and pending.get("preview_sha256") == draft.get("preview_sha256"))
        reply = structured(await consumer.call("prepare_reply_artifact_send", {
            **image, "reply_to": {"chat_id": FIRST_CHAT, "message_id": OWN_TEXT_MESSAGE}}))
        cell.expect("reply_artifact", reply.get("status") == "prepared"
                    and ((reply.get("reply") or {}).get("draft") or {}).get("outgoing_entities") == STYLED)
        cell.expect("nothing_sent", self.dispatches() == 0 and len(self.owner_prompts()) == prompts)

    async def cell_prepare_reply_sources_058(self, cell, consumer, tools) -> None:
        """F13: a reply to the media caption with a text link (v24), to the custom emoji (v25) and to the voice note
        whose caption marks a media timestamp (v28), prepared under the full policy; the bound evidence carries each
        hidden payload."""
        prompts = len(self.owner_prompts())
        results = {}
        for label, message_id in (("media_text_url", MEDIA_LINK_MESSAGE), ("custom_emoji", CUSTOM_EMOJI_SOURCE),
                                  ("voice_timestamp", VOICE_TIMESTAMP_MESSAGE)):
            text = f"Matrix reply to {label}"
            results[label] = structured(await consumer.call("prepare_reply_text_send", {
                "recipient": FAMILY_CHAT, "text": text,
                "reply_to": {"chat_id": FAMILY_CHAT, "message_id": message_id}}))
            reply = results[label].get("reply") or {}
            cell.expect(label, results[label].get("status") == "prepared"
                        and (reply.get("reply_target") or {}).get("anchor") == {"chat_id": FAMILY_CHAT,
                                                                               "message_id": message_id}
                        and (reply.get("draft") or {}).get("text") == text)
        target = {label: ((value.get("reply") or {}).get("reply_target") or {}).get("text") or ""
                  for label, value in results.items()}
        cell.expect("media_evidence", media_evidence(target["media_text_url"])
                    == ("document", [("text_url", "link", MEDIA_LINK_URL)]))
        custom = next(row[2]["custom_emoji_id"] for row in FAMILY_SOURCES if row[0] == CUSTOM_EMOJI_SOURCE)
        cell.expect("custom_emoji_evidence", custom in target["custom_emoji"])
        cell.expect("voice_evidence", media_evidence(target["voice_timestamp"])
                    == ("voice_note", [("media_timestamp", "0:05", 5)]))
        cell.expect("nothing_sent", self.dispatches() == 0 and len(self.owner_prompts()) == prompts)

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

            async def text_options(cell):
                """A text send's own options refused as the reply tools refuse theirs (final acceptance)."""
                prompts = len(self.owner_prompts())
                for label, extra, missing in (("styled", {}, ["formatted_text_send"]),
                                              ("topic", {"topic": {"id": FORUM_TOPIC}},
                                               ["formatted_text_send", "topic_text_send"])):
                    result = structured(await consumer.call("prepare_text_send", {
                        "recipient": FIRST_CHAT, "text": "Matrix styled text", "outgoing_entities": STYLED, **extra}))
                    cell.expect(f"{label}:missing", result.get("status") == "capability_disabled"
                                and result.get("missing_capabilities") == missing
                                and result.get("detail") == TEXT_OPTIONS_DISABLED)
                    cell.expect(f"{label}:no_draft", result.get("next_action") == "enable_capability"
                                and find_draft(result) is None)
                cell.expect("no_dispatch", self.sends() == 0 and len(self.owner_prompts()) == prompts)

            await self.run_cell("refusal.capability_disabled", capability_disabled)
            await self.run_cell("refusal.guard_profiles", guard_profiles)
            await self.run_cell("refusal.reply_families", reply_families)
            await self.run_cell("refusal.text_options", text_options)
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


def agents_unavailable() -> dict:
    return {column: {"status": "unavailable", "reason": "cli_not_given"} for column in AGENT_COLUMN_IDS}


def agent_cells_passed(section: dict) -> int:
    return sum(cell["status"] == "pass" for cell in section.get("cells", []))


def build_receipt(*, commit: str, tree: str, version: str, wheel: dict, build: dict, install: dict, installer: dict,
                  census: dict, sessions: list[dict], inspector: dict, harness_sha256: str, gate_sha256: str,
                  agents: dict | None = None) -> dict:
    cells = merge_cells(sessions, census)
    agents = agents_unavailable() if agents is None else agents
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
        "agents": agents,
        "owner_cells": owner_cells(),
        "summary": {"synthetic": len(cells), "passed": sum(cell["status"] == "pass" for cell in cells),
                    "failed": sum(cell["status"] != "pass" for cell in cells), "inspector": inspector["status"],
                    "inspector_cells": sum(cell["status"] == "pass" for cell in inspector.get("cells", [])),
                    **{key: value for column in AGENT_COLUMN_IDS for key, value in (
                        (column, agents[column]["status"]), (f"{column}_cells", agent_cells_passed(agents[column])))},
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
    SURFACE_COUNTS, each agent column (check_agent_section) and the summary. Bindings: the commit, the wheel sha256,
    the census (version, fingerprint, tools and counts), and check_fields for every other field. The agent cells'
    evidence is verified by verify_agent_evidence."""
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
    agents = receipt["agents"]
    need(isinstance(agents, dict) and set(agents) == set(AGENT_COLUMN_IDS), "agent_invalid")
    for column in AGENT_COLUMN_IDS:
        check_agent_section(column, agents[column])
    surface = receipt["surface"]
    need(isinstance(surface, dict) and all(type(surface.get(key)) is int and surface[key] == count
                                           for key, count in SURFACE_COUNTS.items()), "field_mismatch")
    inspector = receipt["inspector"]
    summary = receipt["summary"]
    need(summary == {"synthetic": len(CELL_IDS), "passed": len(CELL_IDS), "failed": 0,
                     "inspector": inspector["status"],
                     "inspector_cells": len(INSPECTOR_IDS) if inspector["status"] == "pass" else 0,
                     **{key: value for column in AGENT_COLUMN_IDS for key, value in (
                         (column, agents[column]["status"]),
                         (f"{column}_cells", len(AGENT_IDS) if agents[column]["status"] == "pass" else 0))},
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

# --- the agent columns (the running interpreter: real agent CLIs, each cell against its own synthetic broker) ---

# The conversation runner's four, and the other tools Codex 0.162 offers a model beyond MCP servers (sub-agents,
# image generation, plugins); --strict-config refuses a name this Codex does not know.
CODEX_DISABLED_FEATURES = ("shell_tool", "browser_use", "computer_use", "apps", "multi_agent", "image_generation",
                           "plugins")
CLI_VERSIONS = {"codex": re.compile(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+"),
                "claude_code": re.compile(r"[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)")}
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
READ_OPERATIONS = ("read_history", "read_messages", "get_message_context", "search_messages", "search",
                   "read_target_messages", "read_reply_chain")
DRAFT_STATUSES = frozenset({"prepared", "retained", "revised"})  # a draft or a selection now exists
ARTIFACT_OPERATIONS = ("create_local_artifact", "finish_local_upload")
AGENT_RETRY_SECONDS = 30
ACCOUNTS_REGISTRY = ("Library", "Application Support", "TelegramSearchMCP-accounts", "registry.json")


def agent_cell(cell_id: str) -> tuple:
    return next(row for row in AGENT_CELLS if row[0] == cell_id)


def identity_server_name(tree: Path) -> str:
    """The MCP server name of a source tree, as its product identity spells it (read, never imported): the agent
    columns name their server, and the alias's `<server>_<alias>`, through it."""
    import ast
    try:
        module = ast.parse((tree / "src" / "telegram_search_mcp" / "product_identity.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        raise MatrixRefusal("census_failed") from None
    names = [node.value.value for node in module.body if isinstance(node, (ast.Assign, ast.AnnAssign))
             and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
             and [getattr(target, "id", None) for target in (node.targets if isinstance(node, ast.Assign)
                                                            else [node.target])] == ["MCP_SERVER_NAME"]]
    need(len(names) == 1 and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", names[0]), "census_failed")
    return names[0]


def agent_server(session: str, server_name: str) -> str:
    """The MCP server name a cell's agent sees: the default's, or the alias's `<server>_<alias>`."""
    return f"{server_name}_{ALIAS}" if session == "alias" else server_name


def agent_request(cell_id: str, server_name: str) -> str:
    _, session, request, _ = agent_cell(cell_id)
    server = agent_server(session, server_name)
    return AGENT_PREAMBLE.format(server=server) + request.format(server=server)


def bounded(command: list, *, cwd: Path, env: dict, timeout: float, stdout: Path, stderr: Path) -> tuple:
    """(exit code or None, timed out): the agent in its own session, its whole process group killed at the
    deadline or once it exits."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(stdout, flags, 0o600), "wb") as out, os.fdopen(os.open(stderr, flags, 0o600), "wb") as err:
        process = None
        try:
            with deferred_interrupts():
                try:
                    process = subprocess.Popen([str(part) for part in command], cwd=cwd, env=env,
                                               stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
                except OSError:
                    return None, False
                _AGENT_GROUPS.add(process.pid)
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            return None, True
        finally:
            with deferred_interrupts():
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    process.wait()
                    _AGENT_GROUPS.discard(process.pid)


def claude_login_home() -> Path:
    """The caller's home, through which Claude Code finds its login (the default configuration location)."""
    return Path.home()


def claude_configuration(home: Path) -> dict:
    """What the Claude Code column must leave as it found it in the caller's home: size, mtime and sha256 of the
    global configuration and the user's settings files (None when absent), and the names of the project folders,
    where a transcript or an auto-memory folder would appear. Held in memory only, never printed or kept."""
    state = {}
    for name in CLAUDE_GUARDED:
        try:
            metadata = (home / name).lstat()
            state[name] = [metadata.st_size, metadata.st_mtime_ns,
                           _sha((home / name).read_bytes()) if stat.S_ISREG(metadata.st_mode) else None]
        except OSError:
            state[name] = None
    try:
        state["projects"] = sorted(os.listdir(home / ".claude" / "projects"))
    except OSError:
        state["projects"] = None
    return state


def agent_environment(column: str, *, home: Path, config: Path, tmp: Path) -> dict:
    """Only what the agent needs: the system PATH, its TMPDIR, the network proxy settings and, for Codex, its private
    HOME and configuration home; for Claude Code the caller's HOME and USER (its login, nothing else: see
    CLAUDE_SETTINGS), no telemetry or auto-update, and a credential the operator exported for the run."""
    network = {name: os.environ[name] for name in AGENT_NETWORK_ENV if os.environ.get(name)}
    if column == "codex":
        return {"HOME": str(home), "PATH": SYSTEM_PATH, "TMPDIR": str(tmp), "LANG": "en_US.UTF-8",
                "CODEX_HOME": str(config), **network}
    return {"HOME": str(claude_login_home()), "USER": pwd.getpwuid(os.getuid()).pw_name, "PATH": SYSTEM_PATH,
            "TMPDIR": str(tmp), "LANG": "en_US.UTF-8", **CLAUDE_QUIET_ENV, **network,
            **{name: os.environ[name] for name in CLAUDE_CREDENTIAL_ENV if os.environ.get(name)}}


def agent_version(column: str, binary: Path, env: dict) -> str | None:
    try:
        done = subprocess.run([str(binary), "--version"], env=env, cwd=env["TMPDIR"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    version = done.stdout.decode("utf-8", "replace").strip()
    return version if done.returncode == 0 and CLI_VERSIONS[column].fullmatch(version) else None


def codex_config(server: str, command: list, env: dict, cwd: Path) -> str:
    """The private Codex home's config.toml: its only MCP server is the matrix proxy; no shell, browser, computer
    use or connectors; never an approval prompt (the owner's dialog decides); credentials only from auth.json."""
    toml = json.dumps  # JSON strings and arrays of strings are TOML basic strings and arrays
    return "\n".join([
        'approval_policy = "never"', 'sandbox_mode = "read-only"', 'cli_auth_credentials_store = "file"', "",
        "[features]", *(f"{name} = false" for name in CODEX_DISABLED_FEATURES), "",
        f"[mcp_servers.{server}]", f"command = {toml(str(command[0]))}",
        f"args = {toml([str(part) for part in command[1:]])}", f"cwd = {toml(str(cwd))}",
        "env = {" + ", ".join(f"{key} = {toml(value)}" for key, value in env.items()) + "}",
        "startup_timeout_sec = 60", "tool_timeout_sec = 120", 'default_tools_approval_mode = "approve"', ""])


def claude_config(server: str, command: list, env: dict) -> bytes:
    return canonical({"mcpServers": {server: {"command": str(command[0]), "args": [str(p) for p in command[1:]],
                                              "env": env}}})


def agent_command(column: str, binary: Path, model: str | None, request: str, *, server: str, workdir: Path,
                  answer: Path, mcp_config: Path | None) -> list:
    if column == "codex":
        return [binary, "exec", "--strict-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--cd", workdir, "--json", "--color", "never", "--output-last-message", answer,
                *(("--model", model) if model else ()), request]
    # The request goes first: --mcp-config, --tools and --allowedTools take several values.
    return [binary, "-p", request, "--output-format", "stream-json", "--verbose", "--max-turns", "40",
            "--setting-sources", "", "--settings", CLAUDE_SETTINGS, "--disable-slash-commands",
            "--strict-mcp-config", "--mcp-config", mcp_config, "--tools", "", "--allowedTools", f"mcp__{server}",
            "--no-session-persistence", *(("--model", model) if model else ())]


def _events(text: str):
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def agent_observations(column: str, events: str, server: str) -> dict:
    """The agent's own end and the tools it used: a completed turn (Codex turn.completed and no turn.failed; Claude
    Code results that all succeeded), its calls to the matrix server, and any other tool (a shell, another MCP
    server, a built-in tool)."""
    completed, failed, calls, foreign, results = False, False, 0, 0, []
    for event in _events(events):
        kind = event.get("type")
        if column == "codex":
            completed |= kind == "turn.completed"
            failed |= kind == "turn.failed"
            item = event.get("item") if kind in ("item.started", "item.updated", "item.completed") else None
            if isinstance(item, dict):
                if item.get("type") == "mcp_tool_call" and item.get("server") == server:
                    calls += kind == "item.completed"
                elif item.get("type") not in CODEX_QUIET_ITEMS:
                    foreign += 1
            continue
        if kind == "result":
            results.append(event.get("subtype") == "success" and event.get("is_error") is False)
        message = event.get("message") if kind == "assistant" else None
        for block in (message.get("content") or []) if isinstance(message, dict) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name.startswith(f"mcp__{server}__"):
                    calls += 1
                elif name != "ToolSearch":  # loading a deferred MCP tool's schema acts on nothing
                    foreign += 1
    turn = completed and not failed if column == "codex" else bool(results) and all(results)
    return {"turn_completed": turn, "calls": calls, "foreign_tools": foreign}


def provider_errors(column: str, events: str, stderr: str) -> str:
    """What the CLI itself reported as failing, casefolded, for the retry and login decisions: its stderr and its
    provider error events (Codex: `error` events, `turn.failed` errors and `error` items; Claude Code: API error
    messages and failed results), never the model's own messages or reasoning."""
    texts = [stderr]
    for event in _events(events):
        kind = event.get("type")
        if column == "codex":
            item = event.get("item") if kind in ("item.started", "item.updated", "item.completed") else None
            error = event.get("error") if kind == "turn.failed" else None
            texts += [event.get("message") if kind == "error" else None,
                      error.get("message") if isinstance(error, dict) else None,
                      item.get("message") if isinstance(item, dict) and item.get("type") == "error" else None]
        elif kind == "assistant" and (event.get("is_api_error_message") is True or isinstance(event.get("error"), str)):
            message = event.get("message")
            texts += [event.get("error"), *(block.get("text") for block in (
                message.get("content") or [] if isinstance(message, dict) else []) if isinstance(block, dict))]
        elif kind == "result" and event.get("is_error") is True:
            errors = event.get("errors")
            texts += [event.get("result"), event.get("api_error_status") and str(event["api_error_status"]),
                      *(errors if isinstance(errors, list) else [])]
    return "\n".join(text for text in texts if isinstance(text, str)).casefold()


def agent_answer(column: str, events: str) -> str:
    """Claude Code's answer: its last result text (Codex writes its own to answer.txt)."""
    answer = ""
    for event in _events(events):
        if column == "claude_code" and event.get("type") == "result" and isinstance(event.get("result"), str):
            answer = event["result"]
    return answer


def _anchors(value) -> set:
    """Every (chat_id, message_id) pair anywhere in a result."""
    found = set()
    if isinstance(value, dict):
        if type(value.get("chat_id")) is int and type(value.get("message_id")) is int:
            found.add((value["chat_id"], value["message_id"]))
        for item in value.values():
            found |= _anchors(item)
    elif isinstance(value, list):
        for item in value:
            found |= _anchors(item)
    return found


def _status(result) -> object:
    return result.get("status") if isinstance(result, dict) else None


def _rows(result) -> list:
    rows = result.get("results") if isinstance(result, dict) else None
    return [row.get("anchor", {}).get("message_id") for row in rows
            if isinstance(row, dict) and isinstance(row.get("anchor"), dict)] if isinstance(rows, list) else []


def _reply_to(payload: dict, chat: int, message: int) -> bool:
    anchor = payload.get("reply_to")
    return isinstance(anchor, dict) and anchor.get("chat_id") == chat and anchor.get("message_id") == message


class Transcript:
    """The broker's own record of one cell: its header, each connection's frames, each dispatched operation with its
    payload and result (or error), every provider request, every owner dialog and the harness's registry
    postscript."""

    def __init__(self, entries: list):
        entries = [entry for entry in entries if isinstance(entry, dict)]
        self.header = next((entry["broker"] for entry in entries if isinstance(entry.get("broker"), dict)), {})
        self.attempts = [entry for entry in entries if isinstance(entry.get("operation"), str)
                         and isinstance(entry.get("payload"), dict)]
        self.calls = [(entry["operation"], entry["payload"], entry["result"]) for entry in self.attempts
                      if isinstance(entry.get("result"), dict)]
        self.requests = [entry["request"] for entry in entries if "request" in entry]
        self.prompts = [entry for entry in entries if "prompt" in entry]
        self.registry = next((entry["registry"] for entry in reversed(entries)
                              if isinstance(entry.get("registry"), dict)), {})
        self.frames: dict = {}
        for entry in entries:
            if "frame" in entry:
                self.frames.setdefault(str(entry.get("connection")), []).append(entry["frame"])

    def results(self, *operations: str) -> list:
        return [(payload, result) for operation, payload, result in self.calls if operation in operations]

    def reached(self) -> bool:
        return bool(self.frames)

    def diagnostic_checks(self) -> int:
        return sum(frames == ["handshake"] for frames in self.frames.values())

    def dispatched(self) -> bool:
        return any(request in DISPATCH_REQUESTS for request in self.requests)

    def drafted(self) -> bool:
        return any(_status(result) in DRAFT_STATUSES for _, _, result in self.calls)

    def unchanged(self) -> bool:
        """Nothing dispatched, no owner dialog, and no draft or selection made."""
        return not self.dispatched() and not self.prompts and not self.drafted()

    def unsent(self) -> bool:
        """Nothing dispatched, no owner dialog and no approved send or apply attempted."""
        return not self.dispatched() and not self.prompts and not any(
            entry["operation"].startswith(("send_prepared_", "apply_prepared_"))
            and entry["payload"].get("approved") is True for entry in self.attempts)

    def read_newest(self) -> bool:
        return any((FIRST_CHAT, OWN_DOCUMENT_MESSAGE) in _anchors(result)
                   for _, result in self.results(*READ_OPERATIONS))

    def prepared(self, operation: str, match) -> list:
        return [result for payload, result in self.results(operation)
                if _status(result) == "prepared" and match(payload)]


def judge_agent_cell(cell_id: str, column: str, *, entries: list, events: str, answer: str, run: dict,
                     expected: dict) -> dict:
    """One agent cell from its evidence alone (run and check call this same function): its decisive checks in a
    fixed order, then the advisory checks on the answer's wording."""
    _, session, _, _ = agent_cell(cell_id)
    seen = Transcript(entries)
    observed = agent_observations(column, events, agent_server(session, expected["server_name"]))
    text = answer.casefold()
    cell = Cell(cell_id)
    cell.expect("agent_exited", run.get("exit") == 0 and run.get("timed_out") is False)
    cell.expect("turn_completed", observed["turn_completed"])
    cell.expect("only_matrix_tools", observed["foreign_tools"] == 0)
    cell.expect("this_wheel", seen.header == {"version": expected["version"],
                                              "schema_fingerprint": expected["schema_fingerprint"],
                                              "alias": ALIAS if session == "alias" else "default"})
    cell.expect("answer_present", bool(answer.strip()))
    prepared = "prepared" in text or "draft" in text
    if cell_id == "agent.census":
        cell.expect("broker_checked", seen.diagnostic_checks() >= 1)
        cell.expect("nothing_changed", seen.unchanged())
        advisory = {"answer_compatible": "compatible" in text,
                    "answer_tool_count": re.search(rf"\b{len(EVERY_TOOL)}\b", answer) is not None}
    elif cell_id in ("agent.read", "agent.alias_server"):
        if session == "alias":
            cell.expect("alias_served", seen.registry == {"aliases": [ALIAS], "pinned": [ALIAS]})
        cell.expect("read_newest", seen.read_newest())
        cell.expect("nothing_changed", seen.unchanged())
        advisory = {"answer_newest": OWN_CAPTION.casefold() in text}
    elif cell_id == "agent.search_cursor":
        searches = [(payload, result) for payload, result in seen.results("search_messages")
                    if payload.get("target") == FIRST_CHAT]
        followed, issued = False, set()
        for payload, result in searches:
            followed |= isinstance(payload.get("cursor"), str) and payload["cursor"] in issued
            if isinstance(result.get("next_cursor"), str):
                issued.add(result["next_cursor"])
        rows = {row for _, result in searches for row in _rows(result)}
        cell.expect("first_page", any(payload.get("limit") == 1 and len(_rows(result)) == 1
                                      and isinstance(result.get("next_cursor"), str) for payload, result in searches))
        cell.expect("cursor_followed", followed)
        cell.expect("to_the_end", {SOURCE_MESSAGE, OWN_TEXT_MESSAGE, OWN_DOCUMENT_MESSAGE} <= rows and any(
            isinstance(payload.get("cursor"), str) and result.get("next_cursor") is None
            and _status(result) != "invalid_cursor" for payload, result in searches))
        cell.expect("nothing_changed", seen.unchanged())
        advisory = {"answer_matches": all(value.casefold() in text for value in (SOURCE_TEXT, OWN_TEXT, OWN_CAPTION))}
    elif cell_id == "agent.refusal":
        missing = next(row[5] for row in FAMILY_SOURCES if row[0] == FAMILY_MESSAGE)
        cell.expect("refused", any(
            _status(result) == "capability_disabled" and result.get("missing_capabilities") == missing
            and result.get("next_action") == "enable_capability" and _reply_to(payload, FAMILY_CHAT, FAMILY_MESSAGE)
            for payload, result in seen.results("prepare_reply_text_send", "prepare_reply_artifact_send")))
        cell.expect("no_workaround", not seen.drafted())
        cell.expect("nothing_changed", not seen.dispatched() and not seen.prompts)
        advisory = {"answer_capability": "enable" in text or any(name in text for name in missing)}
    elif cell_id == "agent.approval_timeout":
        drafts = [find_draft(result) for result in seen.prepared(
            "prepare_text_send", lambda p: p.get("recipient") == FIRST_CHAT and p.get("text") == TIMEOUT_PROBE)]
        cell.expect("prepared", bool(drafts))
        cell.expect("timed_out", any(
            payload.get("draft_id") in drafts and payload.get("approved") is True and _status(result) == "timeout"
            and result.get("detail") == APPROVAL_DETAILS["timeout"]
            for payload, result in seen.results("send_prepared_text")))
        cell.expect("one_dialog", len(seen.prompts) == 1 and seen.prompts[0].get("prompt") == "timeout"
                    and seen.prompts[0].get("outcome") == "timeout" and seen.prompts[0].get("rendered") is True)
        cell.expect("no_dispatch", not seen.dispatched())
        advisory = {"answer_timeout": any(word in text for word in ("timed out", "timeout", "time out"))}
    elif cell_id == "agent.album_prepare":
        cell.expect("two_artifacts", sum(_status(result) == "complete" and isinstance(result.get("artifact_id"), str)
                                         for _, result in seen.results(*ARTIFACT_OPERATIONS)) >= 2)
        cell.expect("album_prepared", bool(seen.prepared(
            "prepare_album_send", lambda p: p.get("recipient") == FIRST_CHAT and isinstance(p.get("items"), list)
            and len(p["items"]) == 2)))
        cell.expect("not_sent", seen.unsent())
        advisory = {"answer_prepared": prepared}
    elif cell_id == "agent.own_message_prepare":
        cell.expect("edit_prepared", bool(seen.prepared(
            "prepare_text_edit", lambda p: p.get("chat") == FIRST_CHAT and p.get("message_id") == OWN_TEXT_MESSAGE
            and p.get("new_text") == EDIT_TEXT)))
        cell.expect("not_sent", seen.unsent())
        advisory = {"answer_prepared": prepared}
    else:  # agent.family_reply
        cell.expect("reply_prepared", bool(seen.prepared(
            "prepare_reply_text_send", lambda p: p.get("recipient") == FAMILY_CHAT
            and p.get("text") == FAMILY_REPLY_TEXT and _reply_to(p, FAMILY_CHAT, FAMILY_MESSAGE))))
        cell.expect("not_sent", seen.unsent())
        advisory = {"answer_prepared": prepared}
    return {**cell.result(), "advisory_failed": sorted(label for label, passed in advisory.items() if not passed)}


def _jsonl(data: bytes) -> list:
    entries = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def judge_evidence(column: str, cell_id: str, files: dict, expected: dict) -> dict:
    """judge_agent_cell over a cell's evidence files (AGENT_EVIDENCE, as bytes)."""
    try:
        run = json.loads(files["run.json"])
    except ValueError:
        run = {}
    return judge_agent_cell(cell_id, column, entries=_jsonl(files["server.jsonl"]),
                            events=files["events.jsonl"].decode("utf-8", "replace"),
                            answer=files["answer.txt"].decode("utf-8", "replace"),
                            run=run if isinstance(run, dict) else {}, expected=expected)


def run_record(files: dict) -> dict:
    try:
        run = json.loads(files["run.json"])
    except ValueError:
        raise MatrixRefusal("evidence_mismatch") from None
    need(isinstance(run, dict), "evidence_mismatch")
    return run


def evidence_digests(files: dict) -> dict:
    return {name.split(".")[0]: _sha(files[name]) for name in AGENT_EVIDENCE}


def read_evidence(folder: Path) -> dict:
    """A cell's evidence files: regular, not symlinked, at most MAX_EVIDENCE_BYTES each."""
    files = {}
    for name in AGENT_EVIDENCE:
        try:
            metadata = (folder / name).lstat()
            need(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_EVIDENCE_BYTES, "evidence_invalid")
            files[name] = (folder / name).read_bytes()
        except OSError:
            raise MatrixRefusal("evidence_invalid") from None
    return files


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(path, 0o600)


def registry_postscript(home: Path) -> dict:
    """The synthetic home's alias registry after the cell, by alias names only (a pin's user id is never kept)."""
    try:
        value = json.loads(home.joinpath(*ACCOUNTS_REGISTRY).read_bytes())
        aliases = value["aliases"]
        return {"registry": {"aliases": sorted(aliases), "pinned": sorted(
            alias for alias, entry in aliases.items() if isinstance(entry, dict) and type(entry.get("user_id")) is int)}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"registry": {}}


def codex_auth_source(codex_home: Path | None) -> Path | None:
    """The one file Codex needs to authenticate, auth.json of the caller's Codex home: regular and not symlinked."""
    if codex_home is None:
        return None
    try:
        metadata = (codex_home / "auth.json").lstat()
    except OSError:
        return None
    return codex_home / "auth.json" if stat.S_ISREG(metadata.st_mode) else None


def well_formed_refresh(current: bytes, refreshed: bytes) -> bool:
    """Whether a refreshed auth.json can replace the caller's: a JSON object with the same top-level keys as the
    caller's file (and the same keys in its `tokens` object), every field that is a non-empty string there still a
    non-empty string. A truncated or emptied file (an agent killed while writing it) never qualifies."""
    try:
        old, new = json.loads(current), json.loads(refreshed)
    except (ValueError, RecursionError):
        return False

    def filled(level: dict) -> set:
        return {key for key, value in level.items() if isinstance(value, str) and value}
    if not isinstance(old, dict) or not isinstance(new, dict) or set(old) != set(new) or not filled(old) <= filled(new):
        return False
    tokens, renewed = old.get("tokens"), new.get("tokens")
    return not isinstance(tokens, dict) or (isinstance(renewed, dict) and set(tokens) == set(renewed)
                                            and filled(tokens) <= filled(renewed))


def sync_codex_auth(source: Path, copied: str, private: Path) -> str:
    """When Codex refreshed its tokens in the private home during the run, the caller's auth.json still holds the
    refresh token it spent; the refreshed file goes back (atomically, owner-only: 'returned') so the caller's Codex
    stays signed in. The caller's file is kept as it is when it changed meanwhile ('kept'), when the refreshed file is
    not well formed ('invalid') and when the write fails ('failed', no temporary file left). Nothing is printed or kept
    elsewhere."""
    try:
        refreshed = private.read_bytes()
    except OSError:
        return "unchanged"
    if _sha(refreshed) == copied:
        return "unchanged"
    try:
        current = source.read_bytes() if stat.S_ISREG(source.lstat().st_mode) else b""
    except OSError:
        return "kept"
    if _sha(current) != copied:
        return "kept"
    if not well_formed_refresh(current, refreshed):
        return "invalid"
    temporary = source.with_name(f".auth.json.{os.getpid()}.matrix")
    try:
        write_private(temporary, refreshed)
        os.replace(temporary, source)
    except BaseException as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(error, OSError):
            return "failed"
        raise
    return "returned"


def run_agent_cell(column: str, cell_id: str, *, binary: Path, model: str | None, version: str, runtime: "Runtime",
                   census: dict, scratch: Path, env: dict, config: Path, folder: Path, bindings: dict) -> tuple:
    """One cell (retried when a provider failure stopped it before any tool reached the broker): every attempt's
    evidence in `folder`/attempt-<n>, then (its judged result with the evidence digests and the attempt count,
    whether a login failure stopped it before any tool reached the broker)."""
    _, session, _, _ = agent_cell(cell_id)
    server = agent_server(session, bindings["server_name"])
    account = ALIAS if session == "alias" else None
    _fresh(folder)
    for attempt in range(1, AGENT_ATTEMPTS + 1):
        here = _fresh(folder / f"attempt-{attempt}")
        tmp, workdir = _fresh(scratch / f"proxy-{cell_id}-{attempt}"), _fresh(scratch / f"work-{cell_id}-{attempt}")
        broker, home, started, code, timed_out = None, None, time.monotonic(), None, False
        try:
            with deferred_interrupts():
                home = Path(os.path.realpath(tempfile.mkdtemp(prefix="tga.", dir="/private/tmp")))
            write_policy(home, list(RESTRICTED_CAPABILITIES) if session == "restricted" else list(census["capabilities"]))
            with deferred_interrupts():
                broker = start_broker(runtime, home, tmp, "authorized", "scripted",
                                      scratch / f"broker-{cell_id}-{attempt}.log", account=account)
            proxy = [*runtime.proxy, *(("--account", account) if account else ())]
            mcp = scratch / f"mcp-{cell_id}-{attempt}.json"
            if column == "codex":
                write_private(config / "config.toml", codex_config(server, proxy, runtime.env(home, tmp), home).encode())
            else:
                write_private(mcp, claude_config(server, proxy, runtime.env(home, tmp)))
            code, timed_out = bounded(agent_command(column, binary, model, agent_request(cell_id, bindings["server_name"]),
                                                    server=server,
                                                    workdir=workdir, answer=here / "answer.txt", mcp_config=mcp),
                                      cwd=workdir, env=env, timeout=AGENT_SECONDS, stdout=here / "events.jsonl",
                                      stderr=here / "stderr.txt")
        finally:
            with deferred_interrupts():  # A signal meanwhile raises once the broker is stopped and the homes are gone.
                try:
                    if broker is not None:
                        stop_broker(*broker)
                    log = home / "matrix-broker.jsonl" if home is not None else None
                    server_log = log.read_bytes() if log is not None and log.is_file() else b""
                    if account and home is not None:
                        server_log += canonical(registry_postscript(home)).replace(b"\n", b"") + b"\n"
                finally:
                    for path in (home, tmp, workdir):
                        if path is not None:
                            shutil.rmtree(path, ignore_errors=True)
        write_private(here / "server.jsonl", server_log)
        events = (here / "events.jsonl").read_bytes().decode("utf-8", "replace")
        if (here / "answer.txt").is_file():
            os.chmod(here / "answer.txt", 0o600)
        if column == "claude_code" or not (here / "answer.txt").is_file():
            write_private(here / "answer.txt", agent_answer(column, events).encode())
        write_private(here / "run.json", canonical({
            **bindings, "column": column, "cell": cell_id, "cli": version, "model": model or "default",
            "exit": code, "timed_out": timed_out, "attempt": attempt, "seconds": round(time.monotonic() - started, 1)}))
        stream = provider_errors(column, events, (here / "stderr.txt").read_bytes().decode("utf-8", "replace"))
        login_failed = False
        if Transcript(_jsonl(server_log)).reached():
            break
        if any(marker in stream for marker in LOGIN_MARKERS):
            login_failed = True
            break
        if attempt == AGENT_ATTEMPTS or not any(marker in stream for marker in TRANSIENT_MARKERS):
            break
        time.sleep(AGENT_RETRY_SECONDS * attempt)
    files = read_evidence(here)
    return ({**judge_evidence(column, cell_id, files, bindings), "evidence": evidence_digests(files),
             "attempts": attempt}, login_failed)


def run_agent_column(column: str, binary: Path | None, model: str | None, runtime: "Runtime", census: dict,
                     scratch: Path, evidence: Path | None, bindings: dict, *, codex_home: Path | None = None,
                     cells: tuple = AGENT_IDS) -> dict:
    """Every agent cell of one column, in AGENT_IDS order, with private homes for the column, deleted afterwards
    (Codex: a HOME and a CODEX_HOME whose config.toml names only the matrix proxy, with a copy of auth.json, nothing
    else; Claude Code: the default configuration location for its login only, no setting source, a temporary
    --mcp-config with --strict-mcp-config, and the caller's Claude Code configuration unchanged after the version
    check and after every cell, or the run is refused with client_config_changed); or why the column is unavailable
    (AGENT_UNAVAILABLE)."""
    if binary is None or evidence is None:
        return {"status": "unavailable", "reason": "cli_not_given"}
    _fresh(scratch)
    auth = None
    try:
        home, config, tmp = (_fresh(scratch / name) for name in ("agent-home", "agent-config", "agent-tmp"))
        env = agent_environment(column, home=home, config=config, tmp=tmp)
        guarded = claude_configuration(claude_login_home()) if column == "claude_code" else None

        def unchanged() -> None:
            need(guarded is None or claude_configuration(claude_login_home()) == guarded, "client_config_changed")
        version = agent_version(column, binary, env)
        unchanged()
        if version is None:
            return {"status": "unavailable", "reason": "cli_unusable"}
        if column == "codex":
            source = codex_auth_source(codex_home)
            if source is None:
                return {"status": "unavailable", "reason": "auth_missing"}
            data = source.read_bytes()
            write_private(config / "auth.json", data)
            auth = (source, _sha(data))
            del data
        folder = _fresh(evidence / column)
        results, lost = [], False
        for cell_id in cells:
            result, login_failed = run_agent_cell(column, cell_id, binary=binary, model=model, version=version,
                                                  runtime=runtime, census=census, scratch=scratch, env=env,
                                                  config=config, folder=folder / cell_id, bindings=bindings)
            unchanged()
            if login_failed and not results:  # The column's first cell, before any tool reached the broker.
                return {"status": "unavailable", "reason": "not_logged_in"}
            results.append(result)
            if login_failed:  # A login lost once a cell ran: the column fails and keeps every result so far.
                print(f"{column}: login failed after the column's first cell", file=sys.stderr)
                lost = True
                break
        return {"status": "pass" if not lost and all(cell["status"] == "pass" for cell in results) else "fail",
                "cli": version, "model": model or "default", "cells": results}
    finally:
        with deferred_interrupts():  # The write-back and the removal run whole; a signal meanwhile raises afterwards.
            try:
                if auth is not None:
                    outcome = sync_codex_auth(*auth, config / "auth.json")
                    if outcome != "unchanged":  # A fixed line; never the file or its contents.
                        print(f"codex credentials refreshed during the run: {outcome}", file=sys.stderr)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)


def check_agent_section(column: str, section: object) -> None:
    """One agent column on its own: exactly the unavailable form with a fixed reason, or the CLI's version, the model
    and every cell in AGENT_IDS order passed at its frozen count, with known advisory labels, four digests and its
    attempt count."""
    need(isinstance(section, dict) and section.get("status") in ("pass", "unavailable"), "agent_invalid")
    if section["status"] == "unavailable":
        need(set(section) == {"status", "reason"} and section["reason"] in AGENT_UNAVAILABLE, "agent_invalid")
        return
    need(set(section) == {"status", "cli", "model", "cells"} and isinstance(section["cli"], str)
         and CLI_VERSIONS[column].fullmatch(section["cli"]) and isinstance(section["model"], str)
         and (section["model"] == "default" or MODEL_NAME.fullmatch(section["model"])), "agent_invalid")
    cells = section["cells"]
    need(isinstance(cells, list) and all(isinstance(item, dict) for item in cells)
         and [item.get("id") for item in cells] == list(AGENT_IDS), "cell_set_mismatch")
    need(all(item.get("status") == "pass" for item in cells), "cells_failed")
    hex64 = re.compile(r"[0-9a-f]{64}")
    for item in cells:
        advisory, evidence = item.get("advisory_failed"), item.get("evidence")
        need(set(item) == {"id", "status", "checks", "advisory_failed", "evidence", "attempts"}
             and type(item["checks"]) is int and item["checks"] == AGENT_CHECKS[item["id"]], "checks_mismatch")
        need(type(item["attempts"]) is int and 1 <= item["attempts"] <= AGENT_ATTEMPTS, "agent_invalid")
        need(isinstance(advisory, list) and advisory == sorted(set(advisory))
             and set(advisory) <= set(AGENT_ADVISORY[item["id"]]), "agent_invalid")
        need(isinstance(evidence, dict) and set(evidence) == {name.split(".")[0] for name in AGENT_EVIDENCE}
             and all(isinstance(value, str) and hex64.fullmatch(value) for value in evidence.values()), "agent_invalid")


def agents_passed(receipt: dict) -> list:
    return [column for column in AGENT_COLUMN_IDS if receipt["agents"][column]["status"] == "pass"]


def verify_agent_evidence(receipt: dict, directory: Path | None, server_name: str) -> None:
    """Every passed agent cell against its kept evidence (`<column>/<cell>/attempt-<n>`, n its attempt count): the
    four digests, the run record's bindings (commit, wheel, version, fingerprint, the commit's server name, column,
    cell, CLI version, model, attempt) and the same verdict when the cell is judged again; each earlier attempt must
    be kept, numbered, and show that nothing reached its broker (the only failure a cell is retried for)."""
    passed = agents_passed(receipt)
    if not passed:
        return
    need(directory is not None, "evidence_missing")
    bindings = {"commit": receipt["commit"], "wheel_sha256": receipt["wheel"]["sha256"],
                "version": receipt["version"], "schema_fingerprint": receipt["schema_fingerprint"],
                "server_name": server_name}
    for column in passed:
        section = receipt["agents"][column]
        for cell in section["cells"]:
            folder = directory / column / cell["id"]
            for earlier in range(1, cell["attempts"]):
                files = read_evidence(folder / f"attempt-{earlier}")
                need(run_record(files).get("attempt") == earlier
                     and not Transcript(_jsonl(files["server.jsonl"])).reached(), "evidence_mismatch")
            need(not (folder / f"attempt-{cell['attempts'] + 1}").exists(), "evidence_mismatch")
            files = read_evidence(folder / f"attempt-{cell['attempts']}")
            need(evidence_digests(files) == cell["evidence"], "evidence_mismatch")
            run = run_record(files)
            need({key: run.get(key) for key in (*bindings, "column", "cell", "cli", "model", "attempt")}
                 == {**bindings, "column": column, "cell": cell["id"], "cli": section["cli"],
                     "model": section["model"], "attempt": cell["attempts"]}, "evidence_mismatch")
            judged = judge_evidence(column, cell["id"], files, bindings)
            need({key: judged.get(key) for key in ("status", "checks", "advisory_failed")}
                 == {key: cell[key] for key in ("status", "checks", "advisory_failed")}, "evidence_mismatch")


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


def start_broker(runtime: Runtime, home: Path, tmp: Path, profile: str, approval: str, log: Path,
                 account: str | None = None):
    stream = open(log, "wb")
    try:
        process = subprocess.Popen(runtime.command("broker", "--profile", profile, "--approval", approval,
                                                   "--package-root", runtime.package_root,
                                                   *(("--account", account) if account else ())),
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
    base, brokers, drivers, outputs = None, [], [], []
    try:
        with deferred_interrupts():
            base = Path(os.path.realpath(tempfile.mkdtemp(prefix="tgcm.", dir="/private/tmp")))
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
                with deferred_interrupts():
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
                with deferred_interrupts():
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
        with deferred_interrupts():  # Every driver and broker stopped and the homes removed before a signal raises.
            try:
                for process, log in drivers:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    log.close()
            finally:
                try:
                    for broker in brokers:
                        stop_broker(*broker)
                finally:
                    if base is not None:
                        shutil.rmtree(base, ignore_errors=True)


# --- commands ------------------------------------------------------------------------------------------

def inside_work_tree(path: Path) -> bool:
    """Whether a path (while it does not exist, the existing directories above it) lies inside a git work tree: a
    `.git` entry in it or any parent. Receipts, work directories and agent evidence carry machine paths, and an
    agent's working directory there would load the checkout's instructions; none of them belongs in a checkout."""
    real = Path(os.path.realpath(path))
    return any(os.path.lexists(directory / ".git") for directory in (real, *real.parents))


def _workdir(value: str | None) -> tuple[Path, bool]:
    if value is None:
        return Path(tempfile.mkdtemp(prefix="tgmcp-matrix-")).resolve(), True
    path = Path(value)
    need(path.is_absolute() and not inside_work_tree(path), "workdir_invalid")
    if path.exists():
        need(path.is_dir() and not path.is_symlink() and not any(path.iterdir()), "workdir_invalid")
    else:
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError:
            raise MatrixRefusal("workdir_invalid") from None
    return path.resolve(), False


def evidence_directory(value: str | None) -> Path:
    """Where the agent cells' evidence is kept: an absolute, empty (or new) owner-only directory, never a symlink and
    never inside a git work tree."""
    need(value is not None, "evidence_invalid")
    path = Path(value)
    need(path.is_absolute() and not inside_work_tree(path), "evidence_invalid")
    try:
        if path.exists() or path.is_symlink():
            need(path.is_dir() and not path.is_symlink() and not any(path.iterdir()), "evidence_invalid")
        else:
            path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except OSError:
        raise MatrixRefusal("evidence_invalid") from None
    return path.resolve()


def summary_line(receipt: dict) -> str:
    summary, inspector = receipt["summary"], receipt["inspector"]
    columns = [f"Inspector {summary['inspector_cells']}/{len(INSPECTOR_IDS)} cells" if inspector["status"] == "pass"
               else f"Inspector unavailable ({inspector['reason']})"]
    for column, name in AGENT_COLUMNS:
        section, label = receipt["agents"][column], name.split(" (")[0]
        columns.append(f"{label} {summary[column + '_cells']}/{len(AGENT_IDS)} cells" if section["status"] == "pass"
                       else f"{label} unavailable ({section['reason']})")
    return (f"consumer matrix passed: {summary['passed']}/{summary['synthetic']} synthetic cells; "
            + "; ".join(columns) + f"; {summary['owner_pending']} owner cells pending")


def run(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    need(receipt_path.is_absolute() and not receipt_path.exists() and receipt_path.parent.is_dir()
         and not inside_work_tree(receipt_path.parent), "usage")
    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else None
    need(wheelhouse is None or wheelhouse.is_dir(), "wheelhouse_invalid")
    toolhouse = Path(args.toolhouse).resolve() if args.toolhouse else None
    need(toolhouse is None or toolhouse.is_dir(), "toolhouse_invalid")
    node = Path(args.node) if args.node else None
    need(node is None or node.is_absolute(), "node_invalid")
    cache = Path(args.inspector_cache) if args.inspector_cache else None
    need(cache is None or cache.is_absolute(), "cache_invalid")
    binaries = {column: Path(value) if value else None for column, value in (("codex", args.codex),
                                                                            ("claude_code", args.claude))}
    need(all(binary is None or binary.is_absolute() for binary in binaries.values()), "agent_invalid")
    need(all(model is None or MODEL_NAME.fullmatch(model) for model in (args.codex_model, args.claude_model)),
         "agent_invalid")
    # The Codex home whose auth.json is copied, and which gets a refreshed one back, is named, never derived.
    need(args.codex is None or args.codex_home is not None, "agent_invalid")
    codex_home = Path(args.codex_home) if args.codex_home else None
    need(codex_home is None or codex_home.is_absolute(), "agent_invalid")
    evidence = evidence_directory(args.agent_evidence) if any(binaries.values()) else None
    gate = load_gate()
    repo = _gate_call(gate, "repo_unavailable", gate.toplevel, args.repo)
    git = gate.Git(repo)
    commit = _gate_call(gate, "unknown_commit", gate.resolve_commit, git, args.commit)
    work, temporary = None, False
    try:
        with deferred_interrupts():
            work, temporary = _workdir(args.workdir)
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
        wheel_sha256 = gate.sha256_file(wheel)
        bindings = {"commit": commit, "wheel_sha256": wheel_sha256, "version": built["version"],
                    "schema_fingerprint": census["schema_fingerprint"],
                    "server_name": identity_server_name(built["tree"]) if evidence is not None else None}
        models = {"codex": args.codex_model, "claude_code": args.claude_model}
        agents = {name: run_agent_column(name, binaries[name], models[name], runtime, census, work / f"agent-{name}",
                                         evidence, bindings, codex_home=codex_home) for name in AGENT_COLUMN_IDS}
        receipt = build_receipt(
            commit=commit, tree=built["tree_id"], version=built["version"],
            wheel={"file": wheel.name, "sha256": wheel_sha256, "bytes": wheel.stat().st_size},
            build=built["build"], install=installed, installer=installer, census=census, sessions=sessions,
            inspector=column, harness_sha256=committed["harness_sha256"], gate_sha256=committed["gate_sha256"],
            agents=agents)
        for cell in receipt["cells"] + column.get("cells", []):
            if cell["status"] != "pass":  # Harness-defined labels only, never observed values.
                print(f"cell {cell['id']}: {cell['failed_check']}", file=sys.stderr)
        for name in AGENT_COLUMN_IDS:
            for cell in agents[name].get("cells", []):
                if cell["status"] != "pass":
                    print(f"{name} cell {cell['id']}: {cell['failed_check']}", file=sys.stderr)
        need(column["status"] != "fail", "cells_failed")
        need(all(agents[name]["status"] != "fail" for name in AGENT_COLUMN_IDS), "agent_failed")
        check_receipt(receipt, commit=commit, wheel_sha256=receipt["wheel"]["sha256"], census=census,
                      committed=committed, derived={"install": installed,
                                                    "listing_sha256": receipt["surface"]["listing_sha256"]})
        verify_agent_evidence(receipt, evidence, bindings["server_name"])
        write_receipt(receipt_path, receipt)
    finally:
        with deferred_interrupts():
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
    evidence = Path(args.agent_evidence) if args.agent_evidence else None
    need(evidence is None or evidence.is_absolute(), "evidence_invalid")
    need(evidence is not None or not agents_passed(receipt), "evidence_missing")
    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else None
    need(wheelhouse is None or wheelhouse.is_dir(), "wheelhouse_invalid")
    gate = load_gate()
    repo = _gate_call(gate, "repo_unavailable", gate.toplevel, args.repo)
    git = gate.Git(repo)
    commit = _gate_call(gate, "unknown_commit", gate.resolve_commit, git, receipt["commit"])
    if args.commit is not None:
        need(commit == _gate_call(gate, "unknown_commit", gate.resolve_commit, git, args.commit), "commit_mismatch")
    work = None
    try:
        with deferred_interrupts():
            work = Path(tempfile.mkdtemp(prefix="tgmcp-matrix-check-")).resolve()
        built = build_wheel(gate, git, commit, work / "build")
        committed = committed_facts(gate, built)
        # Re-derived with the receipt commit's own harness and gate, never a working-tree edit of them.
        need(harness_digest() == committed["harness_sha256"], "harness_mismatch")
        need(gate.matrix_loaded_sha256 == committed["gate_sha256"], "gate_mismatch")
        census = source_census(gate, Path(sys.executable), built["tree"] / "src", work)
        need(census.get("schema_fingerprint") == MATRIX_SCHEMA_FINGERPRINT, "schema_mismatch")
        if agents_passed(receipt):
            verify_agent_evidence(receipt, evidence, identity_server_name(built["tree"]))
        runtime, installed = installed_runtime(gate, built, work / "install", wheelhouse)
        listing = observed_listing(runtime, census, work / "listing")
        check_receipt(receipt, commit=commit, wheel_sha256=gate.sha256_file(built["wheel"]), census=census,
                      committed=committed, derived={"install": installed, "listing_sha256": listing})
    finally:
        with deferred_interrupts():
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
    print("consumer matrix receipt verified: " + summary_line(receipt).split(": ", 1)[1]
          + f" (commit {receipt['commit']}, wheel sha256 {receipt['wheel']['sha256']})")
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
    run_parser.add_argument("--codex")
    run_parser.add_argument("--codex-home")
    run_parser.add_argument("--codex-model")
    run_parser.add_argument("--claude")
    run_parser.add_argument("--claude-model")
    run_parser.add_argument("--agent-evidence")
    check_parser = commands.add_parser("check", add_help=False)
    check_parser.add_argument("--receipt", required=True)
    check_parser.add_argument("--repo", type=Path, default=Path.cwd())
    check_parser.add_argument("--commit")
    check_parser.add_argument("--wheelhouse")
    check_parser.add_argument("--agent-evidence")
    fetch_parser = commands.add_parser("inspector-fetch", add_help=False)
    fetch_parser.add_argument("--node", required=True)
    fetch_parser.add_argument("--cache", required=True)
    fetch_parser.add_argument("--repo", type=Path, default=Path.cwd())
    broker_parser = commands.add_parser("broker", add_help=False)
    broker_parser.add_argument("--profile", choices=("authorized", "unauthorized"), required=True)
    broker_parser.add_argument("--approval", choices=("scripted", "real_dialog"), required=True)
    broker_parser.add_argument("--package-root", required=True)
    broker_parser.add_argument("--account", choices=(ALIAS,))
    drive_parser = commands.add_parser("drive", add_help=False)
    drive_parser.add_argument("--plan", required=True)
    drive_parser.add_argument("--out", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        handler = {"run": run, "check": check, "inspector-fetch": inspector_fetch, "broker": broker_main,
                   "drive": drive_main}[args.command]
        if args.command not in ("run", "check"):
            return handler(args)
        with interrupt_handlers():
            return handler(args)
    except MatrixRefusal as refusal:
        print(refusal.code, file=sys.stderr)
        return 2
    except MatrixInterrupted as interrupted:  # Every finally has run; a fixed line, the shell's 128 + signal.
        print("interrupted", file=sys.stderr)
        return 128 + interrupted.signum


if __name__ == "__main__":
    raise SystemExit(main())
