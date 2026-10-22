#!/usr/bin/env python3
"""Consumer-matrix harness: every synthetic cell through the official SDK against an installed proxy.

  python packaging/release_consumer_matrix.py run --commit <sha> --receipt <path> [--wheelhouse <dir>]
                                                  [--workdir <dir>] [--real-dialog]
  python packaging/release_consumer_matrix.py check --receipt <path> [--commit <sha>] [--wheelhouse <dir>]

Spec: docs/plans/0.54-consumer-matrix-spec.md (0.54 item E, criterion 11). acceptance is always false:
the harness runs only synthetic cells and never marks an owner or real-account cell passed.

run builds the sdist and wheel from `git archive <commit>` with the running interpreter's build backend
(packaging/release_gate.py helpers, SOURCE_DATE_EPOCH = commit time), installs the wheel with the commit's
pinned, hash-checked dependency closure in a fresh venv (pip downloads it into the workdir unless
--wheelhouse names an existing one), takes the source census of the extracted tree, then drives every
synthetic cell with the installed official SDK against the installed `telegram-search-mcp` proxy. Each
session has its own short owner-only HOME under /private/tmp (a Unix socket path holds at most 104
bytes) and, where the cell needs one, a synthetic broker: the installed Broker class over a fake raw
tdjson transport serving one fixed synthetic account. Nothing reaches Telegram, the Keychain, launchd
state or the live runtime tree. Approval denial is scripted by default (the bound dialog document is
rendered, then declined); --real-dialog shows the real JXA dialog with a 5-second timer and expects
`timeout`. The receipt is bound to the commit, the wheel sha256 and the schema fingerprint. run refuses
when this file or packaging/release_gate.py (which builds, installs and takes the census) differs from the
commit's copy, and records both hashes.

check re-derives the bindings with the receipt commit's own harness and gate (a working-tree copy that
differs refuses): it rebuilds the wheel from `git archive <receipt commit>`, takes the census of that tree
with the running interpreter, reinstalls the pinned closure (downloaded unless --wheelhouse names one) and
lists the tools through the reinstalled proxy. Any difference refuses: another wheel or size, schema, tool
set, closure, install facts or listing, or a field check_receipt and check_fields name. Per-cell check
counts and surface counts are frozen in this file (CELL_CHECKS, SURFACE_COUNTS), so a weakened cell must
change them too. Refusals print one fixed code and never echo inputs.

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

SCHEMA = "tgmcp-consumer-matrix/v2"
PROJECT = "telegram-search-mcp"
HARNESS = Path(__file__).resolve()
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
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
    ("output_schemas", "all", "every structured result of every session validates against its listed outputSchema"),
    ("refusal.invalid_arguments", "full", "schema violations return one fixed input-free text"),
    ("refusal.invalid_arguments_hook", "full", "a strict pre-parse hook refusal returns the same fixed text"),
    ("refusal.unknown_tool", "full", "an unknown tool returns the fixed unknown_tool text"),
    ("refusal.prompt_unavailable", "full", "an unknown prompt is refused with prompt_unavailable"),
    ("refusal.prompt_arguments", "full", "an unknown prompt argument is refused with invalid_arguments"),
    ("refusal.client_schema_mismatch", "full", "a consumer fingerprint mismatch blocks tools until a matching recheck"),
    ("refusal.capability_disabled", "restricted", "a disabled tool is unlisted and refused naming the capability "
     "and next_action"),
    ("refusal.prompt_policy", "restricted", "a prompt needing a disabled tool is unlisted and refused"),
    ("refusal.config_stale", "stale", "a stale pin lists only _manifest and refuses tools with config_stale"),
    ("image.png", "full", "a PNG artifact returns its exact bytes as one image/png block"),
    ("image.pdf_page", "full", "a PDF artifact returns its rendered page as an image/png block"),
    ("cursor.list_chats", "full", "list_chats pages at its cap and follows its cursor to the end"),
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
    ("approval.declined_flag", "full", "approved=false declines without a dialog and without dispatch"),
    ("approval.denial", "full", "approved=true with a denying owner (scripted decline or real dialog timeout) never "
     "dispatches"),
)
CELL_IDS = tuple(cell for cell, _, _ in CELLS)
# How many checks each cell makes when it passes, in CELLS order; check refuses any other count, so a cell
# weakened in the driver also has to be weakened here. The real dialog adds the approval timer check.
CELL_CHECKS = {
    **{f"handshake.{revision}": 6 for revision in REVISIONS},
    "census.tools": 4, "census.fingerprint": 7, "census.prompts": 12, "output_schemas": 2,
    "refusal.invalid_arguments": 10, "refusal.invalid_arguments_hook": 2, "refusal.unknown_tool": 1,
    "refusal.prompt_unavailable": 1, "refusal.prompt_arguments": 2, "refusal.client_schema_mismatch": 4,
    "refusal.capability_disabled": 5, "refusal.prompt_policy": 2, "refusal.config_stale": 3,
    "image.png": 6, "image.pdf_page": 5, "cursor.list_chats": 4,
    "recovery.restart_listing": 4, "recovery.search_again": 2, "recovery.check_send_status": 8,
    "recovery.enable_capability": 5, "recovery.update_installation": 3, "recovery.check_connection": 4,
    "recovery.connect_account": 3, "caps.manifest": 8, "caps.input_bounds": 6,
    "approval.declined_flag": 5, "approval.denial": 8,
}
REAL_DIALOG_CHECKS = {"approval.denial": 1}
# Structured results every session validates in a passing run, and the tools they came from.
SURFACE_COUNTS = {"structured_results_validated": 34, "tools_with_validated_results": 10}
# Owner or real-account cells: listed so the receipt shows them, never run or passed by this harness.
OWNER_CELLS = (
    ("official_sdk.real_account", "official SDK", "real account"),
    ("inspector_cli.synthetic", "MCP Inspector CLI", "synthetic broker"),
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
SESSIONS = ("full", "restricted", "stale", "offline", "unauthorized")
# The restricted policy: send and text replies on, the read_history/list_chats tools and every reply
# source family off.
RESTRICTED_CAPABILITIES = ("artifacts", "read", "reply_text_send", "send")
STALE_PIN = "0.0.0+matrix.stale"

CODES = frozenset({
    "usage", "repo_unavailable", "git_failed", "unknown_commit", "build_failed", "build_tools_missing",
    "install_failed", "census_failed", "broker_failed", "drive_failed", "cells_failed", "receipt_invalid",
    "acceptance_claimed", "owner_cell_passed", "cell_set_mismatch", "commit_mismatch", "wheel_mismatch",
    "schema_mismatch", "tools_mismatch", "version_mismatch", "workdir_invalid", "wheelhouse_invalid",
    "harness_mismatch", "gate_mismatch", "tree_mismatch", "field_mismatch", "checks_mismatch"})
RECEIPT_KEYS = frozenset({"schema", "acceptance", "harness", "gate", "commit", "tree", "version", "wheel", "build",
                          "install", "sdk", "schema_fingerprint", "census", "surface", "approval", "cells",
                          "owner_cells", "summary"})
HARNESS_PATH, GATE_PATH = "packaging/release_consumer_matrix.py", "packaging/release_gate.py"
APPROVALS = ({"mode": "scripted", "outcome": "declined"}, {"mode": "real_dialog", "outcome": "timeout"})
MAX_RECEIPT_BYTES = 1 << 20
BROKER_START_SECONDS, DRIVE_SECONDS, CENSUS_SECONDS, BUILD_SECONDS = 30, 900, 180, 900
REAL_DIALOG_SECONDS = 5

# Fixed texts a consumer sees (docs/reference.md); a deliberate text change updates these.
# Argument refusals name only schema parameters (missing, invalid) and count undeclared keys.
INVALID_ARGUMENTS = "Error executing tool {}: invalid_arguments: {}"
UNKNOWN_TOOL = "unknown_tool (refresh tools/list)"
# The guard names the profile that unlocks the missing capabilities.
CAPABILITY_DISABLED = ("Error executing tool {}: capability_disabled: {} (owner enables profile {}); "
                       "next_action: enable_capability")
CONFIG_STALE = "Error executing tool {}: config_stale (restart matching components); next_action: update_installation"
CLIENT_SCHEMA_MISMATCH = "Error executing tool {}: client_schema_mismatch (refresh tools/list, then recheck _manifest)"
APPROVAL_NOT_GIVEN = "explicit approval was not given"
APPROVAL_DETAILS = {"declined": "the owner declined in the local dialog; no attempt was made",
                    "timeout": "the local dialog timed out without a choice; no attempt was made"}
PROMPTS = ("check_connection", "find_and_summarize", "reply_with_file", "unread_overview")
MANIFEST_CAPS = {"catalog_page_size": 15, "global_message_page_size": 10, "scan_ttl_seconds": 300,
                 "scan_capacity": 4}
SELECTION_CAPS = {"ttl_seconds": 900, "live_capacity_per_owner": 16, "active_capacity_per_owner": 4,
                  "message_projection_bytes": 65536}

# The synthetic account: one account, 25 private chats in Main, one public username, one source message.
ACCOUNT_ID = 9001
FIRST_CHAT, CHAT_COUNT = 91001, 25
PUBLIC_USERNAME = "matrix_synthetic"
SOURCE_MESSAGE = 55
SOURCE_TEXT = "Matrix synthetic source #matrix"
SOURCE_MISSING = ["reply_formatted_targets", "reply_lexical_targets"]  # a hashtag: lexical text
DENIAL_PROBE = "Matrix synthetic denial probe"
OUTCOME_PROBE = "Matrix synthetic outcome probe"
PDF_TEXT = "Matrix synthetic page"
REQUEST_TEXT = "Matrix synthetic request"
LIST_PAGE = 20  # list_chats limit cap


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

def synthetic_png() -> bytes:
    """An 8x8 RGB PNG, built byte for byte so the expected bytes never depend on an encoder."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"".join(bytes((x * 32, y * 32, 128)) for x in range(8)) for y in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def chat_object(chat_id: int) -> dict:
    index = chat_id - FIRST_CHAT + 1
    return {"@type": "chat", "id": chat_id, "title": f"Matrix synthetic chat {index:02d}",
            "type": {"@type": "chatTypePrivate", "user_id": chat_id + 1000},
            "chat_lists": [{"@type": "chatListMain"}],
            "positions": [{"@type": "chatPosition", "list": {"@type": "chatListMain"},
                           "order": str(9_000_000 - index), "is_pinned": False}],
            "unread_count": index % 3, "unread_mention_count": 0, "unread_reaction_count": 0,
            "is_marked_as_unread": False}


def source_message() -> dict:
    offset = SOURCE_TEXT.index("#")
    return {"@type": "message", "chat_id": FIRST_CHAT, "id": SOURCE_MESSAGE,
            "sender_id": {"@type": "messageSenderUser", "user_id": FIRST_CHAT + 1000},
            "is_outgoing": False, "is_from_offline": False, "ephemeral_message_id": 0, "date": 1700000000,
            "edit_date": 0, "self_destruct_in": 0.0, "auto_delete_in": 0.0,
            "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": SOURCE_TEXT, "entities": [
                {"@type": "textEntity", "offset": offset, "length": len(SOURCE_TEXT) - offset,
                 "type": {"@type": "textEntityTypeHashtag"}}]}}}


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
        if kind == "getMessage":
            if (request.get("chat_id"), request.get("message_id")) == (FIRST_CHAT, SOURCE_MESSAGE):
                return source_message()
            return {"@type": "error", "code": 404, "message": "Not Found"}
        if kind == "getMessageProperties":
            return {"@type": "messageProperties", "can_be_replied": True}
        return {"@type": "error", "code": 400, "message": "unsupported by the synthetic account"}

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
    """Scripted owner: renders the bound dialog document, then declines; approves only the outcome probe.

    With real_dialog the denial probe opens the real JXA dialog with a 5-second timer instead."""
    from telegram_search_mcp import approval_prompt as approval

    def prompt(**arguments):
        text = arguments.get("text")
        probe = "outcome" if text == OUTCOME_PROBE else "denial" if text == DENIAL_PROBE else "other"
        rendered = approval.render_approval(**arguments)
        entry = {"prompt": probe, "rendered": rendered is not None,
                 "rows": len(rendered[0].splitlines()) if rendered else 0}
        if rendered is None:
            record({**entry, "outcome": "dialog_unavailable"})
            return approval.UNAVAILABLE
        if probe == "outcome":
            record({**entry, "outcome": "approved", "mode": "scripted"})
            return approval.APPROVED
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
    # The full session's cells on one legacy proxy, in this order: the denial cells run before the one
    # approved send, so "no dispatch" means zero sendMessage requests in the broker log.
    FULL_CELLS = ("census.tools", "census.fingerprint", "census.prompts", "refusal.invalid_arguments",
                  "refusal.invalid_arguments_hook", "refusal.unknown_tool", "refusal.prompt_unavailable",
                  "refusal.prompt_arguments", "image.png", "image.pdf_page", "cursor.list_chats",
                  "recovery.search_again", "caps.manifest", "caps.input_bounds", "approval.declined_flag",
                  "approval.denial", "recovery.check_send_status")

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

            await self.run_cell("refusal.capability_disabled", capability_disabled)
            await self.run_cell("refusal.prompt_policy", prompt_policy)
            await self.run_cell("recovery.enable_capability", enable_capability)

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

def output_schema_cell(sessions: list[dict]) -> dict:
    validated = sum(session["schemas"]["validated"] for session in sessions)
    failures = sorted({failure for session in sessions for failure in session["schemas"]["failures"]})
    cell = Cell("output_schemas")
    cell.expect("validated", validated > 0)
    cell.expect("conforming", not failures)
    return cell.result()


def merge_cells(sessions: list[dict]) -> list[dict]:
    """Session results in the closed CELLS order; a missing or repeated cell refuses."""
    found: dict[str, dict] = {}
    for session in sessions:
        for cell in session["cells"]:
            need(cell["id"] in CELL_IDS and cell["id"] not in found, "cell_set_mismatch")
            found[cell["id"]] = cell
    found["output_schemas"] = output_schema_cell(sessions)
    need(set(found) == set(CELL_IDS), "cell_set_mismatch", {"missing": sorted(set(CELL_IDS) - set(found))})
    return [found[cell_id] for cell_id in CELL_IDS]


def owner_cells() -> list[dict]:
    return [{"id": cell, "client": client, "provider": provider, "status": "pending"}
            for cell, client, provider in OWNER_CELLS]


def build_receipt(*, commit: str, tree: str, version: str, wheel: dict, build: dict, install: dict,
                  census: dict, sessions: list[dict], harness_sha256: str, gate_sha256: str) -> dict:
    cells = merge_cells(sessions)
    full = next(session for session in sessions if session["session"] == "full")
    schemas = [session["schemas"] for session in sessions]
    sdk = {"distribution": SDK_DISTRIBUTION, "version": full["sdk"]["version"], "revisions": list(REVISIONS)}
    return {
        "schema": SCHEMA, "acceptance": False,
        "harness": {"path": HARNESS_PATH, "sha256": harness_sha256}, "gate": {"path": GATE_PATH, "sha256": gate_sha256},
        "commit": commit, "tree": tree, "version": version, "wheel": wheel, "build": build, "install": install,
        "sdk": sdk, "schema_fingerprint": census["schema_fingerprint"],
        "census": {"tool_count": len(census["tools"]), "tools_sha256": tools_sha256(census["tools"]),
                   "capabilities": len(census["capabilities"])},
        "surface": {"listing_sha256": full["observed"].get("listing_sha256"),
                    "structured_results_validated": sum(item["validated"] for item in schemas),
                    "tools_with_validated_results": len({tool for item in schemas for tool in item["tools"]}),
                    "unschematized_tools": sorted({tool for item in schemas for tool in item["unschematized"]})},
        "approval": full["observed"].get("approval"),
        "cells": cells,
        "owner_cells": owner_cells(),
        "summary": {"synthetic": len(cells), "passed": sum(cell["status"] == "pass" for cell in cells),
                    "failed": sum(cell["status"] != "pass" for cell in cells), "owner_pending": len(OWNER_CELLS)},
    }


def check_receipt(receipt: object, *, commit: str | None = None, wheel_sha256: str | None = None,
                  census: dict | None = None, committed: dict | None = None, derived: dict | None = None) -> None:
    """Every rule the harness fixes on its own, then each binding that is given; refuses with one fixed code.

    On its own: the closed keys and schema, no acceptance, the closed cell and owner-cell sets, owner cells
    exactly as OWNER_CELLS lists them and pending, every cell passed with exactly {id, status, checks} and its
    CELL_CHECKS count (REAL_DIALOG_CHECKS added under the real dialog), the approval one of APPROVALS, the
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
    surface = receipt["surface"]
    need(isinstance(surface, dict) and all(type(surface.get(key)) is int and surface[key] == count
                                           for key, count in SURFACE_COUNTS.items()), "field_mismatch")
    summary = receipt["summary"]
    need(summary == {"synthetic": len(CELL_IDS), "passed": len(CELL_IDS), "failed": 0,
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
    - sdk: exactly the distribution, the version the constraints pin and every revision;
    - surface: exactly its four keys, a sha256 listing (with `derived`, the fresh listing's), no tool without
      an output schema.
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
    need(sdk == {"distribution": SDK_DISTRIBUTION, "version": committed["sdk_version"], "revisions": list(REVISIONS)},
         "field_mismatch")
    need(set(surface) == {"listing_sha256", "structured_results_validated", "tools_with_validated_results",
                          "unschematized_tools"}
         and isinstance(surface["listing_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", surface["listing_sha256"])
         and surface["unschematized_tools"] == [], "field_mismatch")
    if derived is not None:
        def facts(value: dict) -> dict:
            return {key: item for key, item in value.items() if key != "wheelhouse"}
        need(facts(install) == facts(derived["install"]), "field_mismatch")
        need(surface["listing_sha256"] == derived["listing_sha256"], "field_mismatch")


def committed_facts(gate, built: dict) -> dict:
    """What rebuilding the recorded commit fixes: its own harness and gate, tree id and epoch, the rebuilt
    wheel's size and the build tools that made it, and its pinned closure (target, freeze, SDK version)."""
    tree = built["tree"]
    release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", built["version"])
    need(release, "build_failed")
    constraints = f"packaging/constraints-{release.group(0)}.txt"
    target, pins = _gate_call(gate, "install_failed", gate.parse_constraints,
                              (tree / constraints).read_text(encoding="utf-8"))
    return {"harness_sha256": _sha((tree / HARNESS_PATH).read_bytes()),
            "gate_sha256": _sha((tree / GATE_PATH).read_bytes()),
            "tree_id": built["tree_id"], "epoch": built["epoch"], "wheel_bytes": built["wheel"].stat().st_size,
            "build_tools": {name: built["build"][name] for name in ("build", "setuptools")},
            "python_target": target, "constraints": constraints,
            "constraints_sha256": gate.sha256_file(tree / constraints), "distributions": len(pins),
            # install_closure's own expectation: every pin, plus the project at the built version.
            "freeze": sorted([f"{name}=={pinned}" for name, (pinned, _) in pins.items()]
                             + [f"{PROJECT}=={built['version']}"]),
            "sdk_version": pins[SDK_DISTRIBUTION][0] if SDK_DISTRIBUTION in pins else None}


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
                 sessions: tuple[str, ...] = SESSIONS) -> list[dict]:
    """Every session at once, each with its own short HOME (a socket path holds at most 104 bytes), its
    own broker where it needs one and its own driver; results in `sessions` order."""
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
            if profile is not None:
                brokers.append(start_broker(runtime, home, tmp, profile, approval, work / f"broker-{name}.log"))
            plan = {"session": name, "home": str(home), "tmp": str(tmp), "proxy": [str(p) for p in runtime.proxy],
                    "pythonpath": runtime.pythonpath, "census": census, "approval": approval,
                    "broker_log": str(home / "matrix-broker.jsonl") if profile is not None else None,
                    "sdk_root": str(runtime.sdk_root) if runtime.sdk_root else None,
                    "debug": os.environ.get("TGMCP_MATRIX_DEBUG") == "1"}
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


def run(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    need(receipt_path.is_absolute() and not receipt_path.exists() and receipt_path.parent.is_dir(), "usage")
    wheelhouse = Path(args.wheelhouse).resolve() if args.wheelhouse else None
    need(wheelhouse is None or wheelhouse.is_dir(), "wheelhouse_invalid")
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
        sessions = run_sessions(runtime, census, _fresh(work / "sessions"),
                                approval="real_dialog" if args.real_dialog else "scripted")
        wheel = built["wheel"]
        receipt = build_receipt(
            commit=commit, tree=built["tree_id"], version=built["version"],
            wheel={"file": wheel.name, "sha256": gate.sha256_file(wheel), "bytes": wheel.stat().st_size},
            build=built["build"], install=installed, census=census, sessions=sessions,
            harness_sha256=committed["harness_sha256"], gate_sha256=committed["gate_sha256"])
        for cell in receipt["cells"]:
            if cell["status"] != "pass":  # Harness-defined labels only, never observed values.
                print(f"cell {cell['id']}: {cell['failed_check']}", file=sys.stderr)
        check_receipt(receipt, commit=commit, wheel_sha256=receipt["wheel"]["sha256"], census=census,
                      committed=committed, derived={"install": installed,
                                                    "listing_sha256": receipt["surface"]["listing_sha256"]})
        write_receipt(receipt_path, receipt)
    finally:
        if temporary:
            shutil.rmtree(work, ignore_errors=True)
    print(f"consumer matrix passed: {receipt['summary']['passed']}/{receipt['summary']['synthetic']} synthetic cells; "
          f"{len(OWNER_CELLS)} owner cells pending")
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
        runtime, installed = installed_runtime(gate, built, work / "install", wheelhouse)
        listing = observed_listing(runtime, census, work / "listing")
        check_receipt(receipt, commit=commit, wheel_sha256=gate.sha256_file(built["wheel"]), census=census,
                      committed=committed, derived={"install": installed, "listing_sha256": listing})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("consumer matrix receipt verified")
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
    run_parser.add_argument("--workdir")
    run_parser.add_argument("--real-dialog", action="store_true")
    check_parser = commands.add_parser("check", add_help=False)
    check_parser.add_argument("--receipt", required=True)
    check_parser.add_argument("--repo", type=Path, default=Path.cwd())
    check_parser.add_argument("--commit")
    check_parser.add_argument("--wheelhouse")
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
        handler = {"run": run, "check": check, "broker": broker_main, "drive": drive_main}[args.command]
        return handler(args)
    except MatrixRefusal as refusal:
        print(refusal.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
