"""The consumer-matrix harness (0.54 item E, criterion 11; final acceptance F18): cell set, receipt rules,
fixtures, schema binding, the Inspector column, the agent columns and the bundle session.

The installed run (wheel from `git archive`, pinned closure, every session, the Inspector CLI, real Codex and Claude
Code) is recorded in the final acceptance tasks file; here the same synthetic sessions run once from this source tree,
the agent columns run with a stand-in agent (tests/matrix_fake_agent.py) that drives the proxy as an honest or a
misbehaving agent would, and a patched registration proves that a schema change fails both run and the receipt
check.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import telegram_search_mcp
from telegram_search_mcp.config import server_name as _identity_server_name

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SOURCE = Path(telegram_search_mcp.__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_consumer_matrix_under_test",
                                               ROOT / "packaging" / "release_consumer_matrix.py")
matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(matrix)
gate = matrix.load_gate()
COMMIT, OTHER = "a" * 40, "b" * 40
WHEEL = "c" * 64
GATE = "9" * 64
LOCK = "8" * 64
VERSION = telegram_search_mcp.__version__
INSTALLER = {"distribution": "tomlkit", "version": "0.13.3", "sha256": "7" * 64}

# Appended to a copy of server.py: a deliberate schema change in the registration itself.
PATCHES = {
    "input_bound": '''
_matrix_original_build_server = build_server
def build_server(*args, **kwargs):
    server = _matrix_original_build_server(*args, **kwargs)
    server._tool_manager.get_tool("list_chats").parameters["properties"]["limit"]["maximum"] = 21
    return server
''',
    "extra_tool": '''
_matrix_original_build_server = build_server
def build_server(*args, **kwargs):
    server = _matrix_original_build_server(*args, **kwargs)
    @server.tool(name="matrix_patched_tool", structured_output=True)
    def matrix_patched_tool() -> dict[str, object]:
        return {}
    return server
''',
}


def live_census() -> dict:
    from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
    from telegram_search_mcp.contract import schema_document, schema_fingerprint
    from telegram_search_mcp.server import build_server
    document = schema_document(build_server(policy=RuntimePolicy(enabled_capabilities=tuple(KNOWN_CAPABILITIES))))
    return {"version": telegram_search_mcp.__version__, "capabilities": sorted(KNOWN_CAPABILITIES),
            "tools": sorted(tool["name"] for tool in document["tools"]), "schema_fingerprint": schema_fingerprint()}


def live_listing() -> dict:
    """Every tool's listed input schema, as the full policy publishes it."""
    from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
    from telegram_search_mcp.contract import schema_document
    from telegram_search_mcp.server import build_server
    document = schema_document(build_server(policy=RuntimePolicy(enabled_capabilities=tuple(KNOWN_CAPABILITIES))))
    return {tool["name"]: tool["inputSchema"] for tool in document["tools"]}


def sessions(*, failing: str | None = None, validated: int | None = None, failures: tuple = (),
             approval: str = "scripted", tools: list | None = None) -> list[dict]:
    """Session results as a passing run reports them: each cell's frozen check count, the frozen surface, every
    census tool validated in the full session."""
    results, extra = [], {"approval.denial": 1} if approval == "real_dialog" else {}
    surface = matrix.SURFACE_COUNTS
    census_tools = live_census()["tools"] if tools is None else tools
    for name in matrix.SESSIONS:
        cells = [{"id": cell, "status": "fail" if cell == failing else "pass",
                  "checks": matrix.CELL_CHECKS[cell] + extra.get(cell, 0),
                  **({"failed_check": "synthetic"} if cell == failing else {})}
                 for cell, session, _ in matrix.CELLS if session == name]
        full = name == "full"
        count = surface["structured_results_validated"] - (len(matrix.SESSIONS) - 1) if full else 1
        results.append({"session": name, "cells": cells,
                        "observed": {"listing_sha256": "d" * 64,
                                     "approval": {"mode": approval,
                                                  "outcome": "timeout" if approval == "real_dialog" else "declined"}},
                        "schemas": {"validated": count if validated is None else validated, "failures": list(failures),
                                    "tools": list(census_tools) if full else census_tools[:1], "unschematized": []},
                        "sdk": {"distribution": "mcp", "version": "2.2.0"}})
    return results


INSPECTOR_PASS = {"status": "pass", "package": matrix.INSPECTOR_PACKAGE, "version": matrix.INSPECTOR_VERSION,
                  "integrity": matrix.INSPECTOR_INTEGRITY, "lock_sha256": LOCK, "node": "22.18.0",
                  "cells": [{"id": cell, "status": "pass", "checks": matrix.INSPECTOR_CHECKS[cell]}
                            for cell in matrix.INSPECTOR_IDS]}
INSPECTOR_UNAVAILABLE = {"status": "unavailable", "reason": "node_not_given"}

# What the recorded commit fixes (committed_facts) and what check re-derives; receipt() agrees with both.
FACTS = {"harness_sha256": "e" * 64, "gate_sha256": GATE, "tree_id": OTHER, "epoch": 1700000000,
         "constraints": "packaging/constraints-0.0.0.txt", "constraints_sha256": "f" * 64, "distributions": 2,
         "freeze": ["mcp==2.2.0", "pydantic==2.0", f"telegram-search-mcp=={VERSION}"], "sdk_version": "2.2.0",
         "wheel_bytes": 1, "python_target": "3.14", "build_tools": {"build": "1.3.0", "setuptools": "80.9.0"},
         "installer": dict(INSTALLER), "inspector_lock_sha256": LOCK}
INSTALLED = {"constraints": FACTS["constraints"], "constraints_sha256": FACTS["constraints_sha256"],
             "wheelhouse": "provided", "distributions": 2, "freeze": list(FACTS["freeze"]), "pip_check": "clean",
             "record_entries": 10}
DERIVED = {"install": {**INSTALLED, "wheelhouse": "downloaded"}, "listing_sha256": "d" * 64}


def agent_pass(cli: str = "codex-cli 0.162.0") -> dict:
    """A passed agent column as run records it (digests are only checked against kept evidence)."""
    return {"status": "pass", "cli": cli, "model": "default",
            "cells": [{"id": cell, "status": "pass", "checks": matrix.AGENT_CHECKS[cell], "advisory_failed": [],
                       "evidence": {name: "1" * 64 for name in ("server", "events", "answer", "run")}, "attempts": 1}
                      for cell in matrix.AGENT_IDS]}


AGENTS_PASS = {"codex": agent_pass(), "claude_code": {"status": "unavailable", "reason": "not_logged_in"}}


def receipt(census: dict, *, inspector: dict | None = None, agents: dict | None = None, **kwargs) -> dict:
    return matrix.build_receipt(
        commit=COMMIT, tree=OTHER, version=census["version"],
        wheel={"file": f"telegram_search_mcp-{census['version']}-py3-none-any.whl", "sha256": WHEEL, "bytes": 1},
        build={"build": "1.3.0", "python": "3.14.6", "setuptools": "80.9.0", "source_date_epoch": FACTS["epoch"]},
        install=dict(INSTALLED), installer={**INSTALLER, "toolhouse": "provided"}, census=census,
        sessions=sessions(tools=census["tools"], **kwargs),
        inspector=copy.deepcopy(INSPECTOR_PASS if inspector is None else inspector),
        harness_sha256=FACTS["harness_sha256"], gate_sha256=GATE, agents=copy.deepcopy(agents))


def refusal(function, *args, **kwargs) -> str:
    try:
        function(*args, **kwargs)
    except matrix.MatrixRefusal as error:
        return error.code
    raise AssertionError("no refusal")


class CellSetTests(unittest.TestCase):
    def test_cells_are_closed_unique_and_cover_every_category_and_session(self):
        self.assertEqual(len(matrix.CELL_IDS), len(set(matrix.CELL_IDS)))
        self.assertFalse(set(matrix.CELL_IDS) & set(matrix.OWNER_IDS))
        self.assertFalse(set(matrix.INSPECTOR_IDS) & (set(matrix.CELL_IDS) | set(matrix.OWNER_IDS)))
        self.assertEqual(len(matrix.OWNER_IDS), len(set(matrix.OWNER_IDS)))
        self.assertEqual({session for _, session, _ in matrix.CELLS}, set(matrix.SESSIONS) | {"all"})
        self.assertEqual({cell.split(".")[0] for cell in matrix.CELL_IDS},
                         {"handshake", "census", "output_schemas", "tools", "refusal", "image", "cursor", "recovery",
                          "caps", "approval", "bundle", "search", "documents", "prepare"})
        self.assertEqual([cell for cell in matrix.CELL_IDS if cell.startswith("handshake.")],
                         [f"handshake.{revision}" for revision in matrix.REVISIONS])
        self.assertEqual([cell for cell in matrix.CELL_IDS if cell.startswith("bundle.")],
                         ["bundle.install", "bundle.upgrade", "bundle.rollback", "bundle.uninstall"])
        self.assertNotIn("inspector_cli.synthetic", matrix.OWNER_IDS)  # The harness's own column now.

    def test_every_next_action_value_has_exactly_one_recovery_cell(self):
        from telegram_search_mcp.next_actions import NEXT_ACTIONS
        self.assertEqual({cell for cell in matrix.CELL_IDS if cell.startswith("recovery.")},
                         {f"recovery.{value}" for value in NEXT_ACTIONS.values()})

    def test_revisions_are_every_revision_of_the_installed_sdk(self):
        from mcp_types import version
        self.assertEqual(matrix.LEGACY_REVISIONS, tuple(version.HANDSHAKE_PROTOCOL_VERSIONS))
        self.assertEqual(matrix.MODERN_REVISIONS, tuple(version.MODERN_PROTOCOL_VERSIONS))

    def test_every_cell_has_a_frozen_check_count(self):
        self.assertEqual(list(matrix.CELL_CHECKS), list(matrix.CELL_IDS))
        self.assertTrue(all(type(count) is int and count > 0 for count in matrix.CELL_CHECKS.values()))
        self.assertEqual(list(matrix.INSPECTOR_CHECKS), list(matrix.INSPECTOR_IDS))
        self.assertEqual(set(matrix.SURFACE_COUNTS), {"structured_results_validated", "tools_with_validated_results"})
        # Table-driven cells: one check per row, plus their fixed extras.
        self.assertEqual(matrix.CELL_CHECKS["tools.every_tool"], len(matrix.EVERY_TOOL))
        self.assertEqual(matrix.CELL_CHECKS["refusal.invalid_arguments_every_tool"], len(matrix.TOOL_REQUIRED) + 1)
        self.assertEqual(matrix.CELL_CHECKS["refusal.guard_every_tool"],
                         2 + len(matrix.TOOL_GUARDS) + len(matrix.UPLOAD_AUTHORITY_TOOLS))
        self.assertEqual(matrix.CELL_CHECKS["refusal.guard_profiles"], len(matrix.RESTRICTED_GUARDS) + 2)
        self.assertEqual(matrix.CELL_CHECKS["refusal.reply_families"], 2 * (len(matrix.FAMILY_SOURCES) + 2))
        self.assertEqual(matrix.SURFACE_COUNTS["tools_with_validated_results"], len(matrix.TOOL_REQUIRED))

    def test_the_driver_runs_every_cell_of_its_session(self):
        driver = matrix.Driver
        full = set(driver.FULL_CELLS) | set(driver.ISOLATED_CELLS) | {f"handshake.{r}" for r in matrix.REVISIONS} | {
            "refusal.client_schema_mismatch", "recovery.restart_listing"}
        self.assertEqual(full, {cell for cell, session, _ in matrix.CELLS if session == "full"})
        for name in driver.FULL_CELLS + driver.ISOLATED_CELLS:
            self.assertTrue(callable(getattr(driver, "cell_" + name.replace(".", "_"), None)), name)
        for name in matrix.SESSIONS + ("inspector", "listing"):
            self.assertTrue(callable(getattr(driver, name, None)), name)


class FinalAuditCellTests(unittest.TestCase):
    """The cells that give the final audit's F6, F9 and F13 installed-consumer evidence, and the typed text refusal:
    their synthetic fixtures (the source sessions test drives the cells themselves)."""

    def test_the_cells_and_their_sessions(self):
        cells = {cell: session for cell, session, _ in matrix.CELLS}
        self.assertEqual({name: cells[name] for name in (
            "search.boolean", "search.filters", "documents.xlsx", "documents.pptx", "prepare.styled_caption",
            "prepare.reply_sources_058", "refusal.text_options")},
            {**dict.fromkeys(("search.boolean", "search.filters", "documents.xlsx", "documents.pptx",
                              "prepare.styled_caption", "prepare.reply_sources_058"), "full"),
             "refusal.text_options": "restricted"})
        self.assertEqual(len(matrix.CELL_IDS), 55)
        self.assertEqual(matrix.TEXT_OPTIONS_DISABLED, __import__(
            "telegram_search_mcp.schemas", fromlist=["TEXT_CAPABILITY_DISABLED"]).TEXT_CAPABILITY_DISABLED)

    def test_the_synthetic_documents_are_deterministic_ooxml(self):
        import io
        import zipfile
        for build, names in ((matrix.synthetic_xlsx, {"xl/workbook.xml", "xl/worksheets/sheet1.xml",
                                                      "xl/worksheets/sheet2.xml"}),
                             (matrix.synthetic_pptx, {"ppt/presentation.xml", "ppt/slides/slide1.xml",
                                                      "ppt/slides/slide2.xml", "ppt/notesSlides/notes1.xml"})):
            with self.subTest(build=build.__name__):
                self.assertEqual(build(), build())
                with zipfile.ZipFile(io.BytesIO(build())) as archive:
                    self.assertLessEqual(names, set(archive.namelist()))
                    self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_the_synthetic_account_filters_by_sender_and_topic_as_tdlib_does(self):
        tdlib = matrix.SyntheticTDLib()

        def found(**request):
            return [message["id"] for message in tdlib._answer({"@type": "searchChatMessages", "query": "matrix",
                                                                "from_message_id": 0, "offset": 0, "limit": 20,
                                                                "filter": None, **request})["messages"]]
        peer = {"@type": "messageSenderUser", "user_id": matrix.PEER_USER}
        topic = {"@type": "messageTopicForum", "forum_topic_id": matrix.FORUM_TOPIC}
        self.assertEqual(found(chat_id=matrix.FIRST_CHAT, sender_id=None, topic_id=None), [57, 56, 55])
        self.assertEqual(found(chat_id=matrix.FIRST_CHAT, sender_id=peer, topic_id=None), [55])
        self.assertEqual(found(chat_id=matrix.FORUM_CHAT, sender_id=None, topic_id=topic), [72, 70])
        self.assertEqual(found(chat_id=matrix.FAMILY_CHAT, sender_id=None, topic_id=None), [])  # as before
        self.assertEqual(tdlib._answer({"@type": "getChatHistory", "chat_id": matrix.FAMILY_CHAT, "from_message_id": 0,
                                        "offset": 0, "limit": 20})["messages"], [])
        supergroup = tdlib._answer({"@type": "getSupergroup", "supergroup_id": matrix.FORUM_SUPERGROUP})
        self.assertIs(supergroup["is_forum"], True)
        self.assertEqual(tdlib._answer({"@type": "getForumTopic", "chat_id": matrix.FORUM_CHAT,
                                        "forum_topic_id": matrix.FORUM_TOPIC})["info"]["forum_topic_id"], 41)
        # The Main list is still the 25 private chats: the forum is reachable by its id only.
        self.assertNotIn(matrix.FORUM_CHAT, tdlib._answer({"@type": "getChats", "chat_list": {"@type": "chatListMain"},
                                                           "limit": 100})["chat_ids"])

    def test_the_media_link_message_is_a_v24_reply_source(self):
        from telegram_search_mcp import reply_media_sources, reply_source_ladder
        content = matrix.synthetic_messages(matrix.FAMILY_CHAT)[matrix.MEDIA_LINK_MESSAGE]["content"]
        families = reply_source_ladder.present(content["caption"]["entities"])
        self.assertEqual(reply_source_ladder.version_for(families, media=True), 24)
        projected = reply_media_sources.media_content_for(content, reply_source_ladder.admission_for(24))
        self.assertEqual((projected["kind"], projected["media"]["file_name"]), ("document", "matrix-caption-link.txt"))
        rendered = reply_media_sources.render_content_for(projected, "", reply_source_ladder.admission_for(24))[0]
        self.assertTrue(matrix.media_link_evidence(rendered))
        self.assertFalse(matrix.media_link_evidence(rendered.replace("caption", "elsewhere")))


class ToolTableTests(unittest.TestCase):
    """The per-tool tables are the 0.58 contract the cells assert; they must equal what the source publishes."""

    def test_the_frozen_fingerprint_is_the_source_census(self):
        self.assertEqual(matrix.MATRIX_SCHEMA_FINGERPRINT, live_census()["schema_fingerprint"])

    def test_every_tool_is_probed_once_and_its_required_parameters_are_the_listed_ones(self):
        census = live_census()
        self.assertEqual(sorted(tool for tool, _ in matrix.EVERY_TOOL), census["tools"])
        self.assertEqual(len(matrix.EVERY_TOOL), len(set(tool for tool, _ in matrix.EVERY_TOOL)))
        self.assertEqual({name: tuple(sorted(schema.get("required", []))) for name, schema in live_listing().items()},
                         matrix.TOOL_REQUIRED)
        driver = object.__new__(matrix.Driver)
        files = {key: matrix.FAKE_ARTIFACT for key in ("png", "png2", "pdf", "mp4")}
        for tool, _ in matrix.EVERY_TOOL:  # Every probe has arguments; required ones are present.
            arguments = driver.every_tool_arguments(tool, {}, files)
            self.assertTrue(set(matrix.TOOL_REQUIRED[tool]) <= set(arguments), tool)

    def test_guard_tables_equal_the_guard(self):
        from telegram_search_mcp import server
        from telegram_search_mcp.local_upload_models import PUBLIC_MODELS
        hint = lambda profile: "owner enables profile " + profile if profile else "owner must enable in runtime.toml"
        gated = {name: needed for name, needed in server._TOOL_CAPABILITIES.items() if needed}
        self.assertEqual(set(matrix.UPLOAD_AUTHORITY_TOOLS), set(PUBLIC_MODELS))
        self.assertEqual(set(matrix.TOOL_GUARDS) | set(matrix.UPLOAD_AUTHORITY_TOOLS), set(gated))
        for name, (needed, profile) in matrix.TOOL_GUARDS.items():
            self.assertEqual(needed, tuple(gated[name]), name)
            self.assertEqual(hint(profile), server._enable_hint(list(needed)), name)
        restricted = set(matrix.RESTRICTED_CAPABILITIES)
        expected = {name: tuple(c for c in needed if c not in restricted) for name, needed in gated.items()
                    if not set(needed) <= restricted and name not in PUBLIC_MODELS}
        self.assertEqual({name: missing for name, (missing, _) in matrix.RESTRICTED_GUARDS.items()}, expected)
        for name, (missing, profile) in matrix.RESTRICTED_GUARDS.items():
            self.assertEqual(hint(profile), server._enable_hint(list(missing)), name)

    def test_guard_arguments_pass_every_input_schema(self):
        from jsonschema.validators import validator_for
        listing = live_listing()
        for name in list(matrix.TOOL_GUARDS) + list(matrix.UPLOAD_AUTHORITY_TOOLS):
            schema = listing[name]
            errors = list(validator_for(schema)(schema).iter_errors(matrix.guard_arguments(name)))
            self.assertEqual(errors, [], name)

    def test_dispatch_and_dialog_vocabularies_are_the_sources(self):
        from telegram_search_mcp import approval_prompt, tdjson
        self.assertTrue(matrix.DISPATCH_REQUESTS >= tdjson._CHANGE_REQUEST_TYPES | {"sendMessage", "sendMessageAlbum",
                                                                                     "forwardMessages"})
        self.assertTrue({key for key, _ in matrix.DIALOG_FAMILIES} <= set(approval_prompt._DEFAULTS))

    def test_argument_texts_follow_the_closed_grammar(self):
        self.assertEqual(matrix.argument_text("list_drafts"),
                         "Error executing tool list_drafts: invalid_arguments: 1 unexpected argument(s)")
        self.assertEqual(matrix.argument_text("prepare_pin_change"),
                         "Error executing tool prepare_pin_change: invalid_arguments: missing action, chat, "
                         "message_id; 1 unexpected argument(s)")


class ContractTextTests(unittest.TestCase):
    """The harness hard-codes the documented texts; the source and the reference must agree with it."""

    def test_refusal_texts_equal_the_source_constants(self):
        from telegram_search_mcp import approval_prompt, server
        prefix = "Error executing tool {}: "
        schema = {"properties": {"a": {}, "b": {}}, "required": ["a"]}
        self.assertEqual(server._argument_refusal("t", schema, {"zzz": 1}, None),
                         matrix.INVALID_ARGUMENTS.format("t", "missing a; 1 unexpected argument(s)"))
        self.assertEqual(matrix.UNKNOWN_TOOL, server._INPUT_REFUSALS["unknown_tool"])
        self.assertEqual(matrix.CAPABILITY_DISABLED.format("t", "c", "readers"),
                         prefix.format("t") + server._GUARD_REFUSALS["capability_disabled"].format(
                             "c", server._enable_hint(["read_history"])))
        self.assertEqual(matrix.CAPABILITY_DISABLED_POLICY.format("t", "c"),
                         prefix.format("t") + server._GUARD_REFUSALS["capability_disabled"].format(
                             "c", server._enable_hint(["archive_chats", "send", "read"])))
        self.assertEqual(matrix.CONFIG_STALE.format("t"), prefix.format("t") + server._GUARD_REFUSALS["config_stale"])
        self.assertEqual(matrix.CLIENT_SCHEMA_MISMATCH.format("t"),
                         prefix.format("t") + server._GUARD_REFUSALS["client_schema_mismatch"])
        self.assertEqual(matrix.APPROVAL_NOT_GIVEN, approval_prompt.NOT_GIVEN)
        self.assertEqual(matrix.APPROVAL_DETAILS, {key: approval_prompt.DETAILS[key] for key in ("declined", "timeout")})
        self.assertEqual(matrix.PROMPTS, tuple(sorted(server._PROMPTS)))
        self.assertEqual(matrix.REAL_DIALOG_SECONDS, 5)

    def test_refusal_texts_are_the_documented_ones(self):
        reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        for text in (matrix.INVALID_ARGUMENTS.format("<tool>", "missing <names>; invalid <names>; <n> unexpected "
                                                     "argument(s)"), matrix.UNKNOWN_TOOL,
                     matrix.CAPABILITY_DISABLED.format("<tool>", "<capabilities>", "<profile>"),
                     matrix.CONFIG_STALE.format("<tool>"), matrix.CLIENT_SCHEMA_MISMATCH.format("<tool>")):
            self.assertIn(text, reference)

    def test_manifest_caps_equal_the_served_manifest(self):
        from mcp import Client
        from telegram_search_mcp.server import build_server

        async def manifest():
            async with Client(build_server(service_factory=lambda: self.fail("provider opened"))) as client:
                return (await client.call_tool("_manifest", {})).structured_content
        served = asyncio.run(manifest())
        self.assertEqual({key: served[key] for key in matrix.MANIFEST_CAPS}, matrix.MANIFEST_CAPS)
        self.assertEqual({key: served["retained_selections"][key] for key in matrix.SELECTION_CAPS},
                         matrix.SELECTION_CAPS)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.census = live_census()
        self.receipt = receipt(self.census)

    def test_a_passing_receipt_is_bound_to_commit_wheel_and_census(self):
        matrix.check_receipt(self.receipt, commit=COMMIT, wheel_sha256=WHEEL, census=self.census)
        self.assertIs(self.receipt["acceptance"], False)
        self.assertEqual(self.receipt["schema"], "tgmcp-consumer-matrix/v4")
        self.assertEqual([cell["id"] for cell in self.receipt["cells"]], list(matrix.CELL_IDS))
        self.assertEqual({cell["status"] for cell in self.receipt["owner_cells"]}, {"pending"})
        self.assertEqual(self.receipt["summary"], {"synthetic": len(matrix.CELL_IDS), "passed": len(matrix.CELL_IDS),
                                                   "failed": 0, "inspector": "pass",
                                                   "inspector_cells": len(matrix.INSPECTOR_IDS),
                                                   "codex": "unavailable", "codex_cells": 0,
                                                   "claude_code": "unavailable", "claude_code_cells": 0,
                                                   "owner_pending": len(matrix.OWNER_IDS)})
        self.assertEqual(self.receipt["census"]["tools_sha256"], matrix.tools_sha256(self.census["tools"]))
        self.assertEqual(set(self.receipt), matrix.RECEIPT_KEYS)
        self.assertEqual(matrix.summary_line(self.receipt),
                         f"consumer matrix passed: {len(matrix.CELL_IDS)}/{len(matrix.CELL_IDS)} synthetic cells; "
                         "Inspector 4/4 cells; Codex unavailable (cli_not_given); Claude Code unavailable "
                         f"(cli_not_given); {len(matrix.OWNER_IDS)} owner cells pending")
        document = receipt(self.census, agents=AGENTS_PASS)
        matrix.check_receipt(document, commit=COMMIT, wheel_sha256=WHEEL, census=self.census)
        self.assertEqual({key: document["summary"][key] for key in ("codex", "codex_cells", "claude_code",
                                                                    "claude_code_cells")},
                         {"codex": "pass", "codex_cells": len(matrix.AGENT_IDS), "claude_code": "unavailable",
                          "claude_code_cells": 0})
        self.assertIn(f"Codex {len(matrix.AGENT_IDS)}/{len(matrix.AGENT_IDS)} cells; Claude Code unavailable "
                      "(not_logged_in)", matrix.summary_line(document))

    def test_an_unavailable_inspector_is_recorded_with_its_fixed_reason(self):
        for reason in sorted(matrix.INSPECTOR_UNAVAILABLE):
            with self.subTest(reason=reason):
                document = receipt(self.census, inspector={"status": "unavailable", "reason": reason})
                matrix.check_receipt(document, census=self.census)
                self.assertEqual((document["summary"]["inspector"], document["summary"]["inspector_cells"]),
                                 ("unavailable", 0))
                self.assertIn(f"Inspector unavailable ({reason})", matrix.summary_line(document))
        for bad in ({"status": "unavailable", "reason": "skipped"}, {"status": "unavailable"},
                    {"status": "skipped"}, {"status": "unavailable", "reason": "node_not_given", "cells": []}):
            with self.subTest(bad=bad):
                document = receipt(self.census, inspector=INSPECTOR_UNAVAILABLE)
                document["inspector"] = bad
                self.assertEqual(refusal(matrix.check_receipt, document), "inspector_invalid")

    def test_each_rule_refuses_with_its_fixed_code(self):
        def changed(update):
            value = copy.deepcopy(self.receipt)
            update(value)
            return value
        cases = {
            "acceptance_claimed": changed(lambda r: r.update(acceptance=True)),
            "owner_cell_passed": changed(lambda r: r["owner_cells"][0].update(status="pass")),
            "cell_set_mismatch": changed(lambda r: r["cells"].pop()),
            "receipt_invalid": changed(lambda r: r.update(extra=1)),
            "schema_mismatch": changed(lambda r: r.update(schema_fingerprint="0" * 64)),
        }
        cases["cells_failed"] = receipt(self.census, failing="approval.denial")
        tampered = changed(lambda r: r["summary"].update(passed=1))
        for code, value in cases.items():
            with self.subTest(code=code):
                self.assertEqual(refusal(matrix.check_receipt, value), code)
        self.assertEqual(refusal(matrix.check_receipt, tampered), "receipt_invalid")
        for key in ("cells", "owner_cells"):
            with self.subTest(appended_to=key):
                self.assertEqual(refusal(matrix.check_receipt, changed(lambda r: r[key].append("pass"))),
                                 "cell_set_mismatch")
        self.assertEqual(refusal(matrix.check_receipt, ["not", "a", "receipt"]), "receipt_invalid")
        self.assertEqual(refusal(matrix.check_receipt, self.receipt, commit=OTHER), "commit_mismatch")
        self.assertEqual(refusal(matrix.check_receipt, self.receipt, wheel_sha256="0" * 64), "wheel_mismatch")
        for key, value, code in (("schema_fingerprint", "0" * 64, "schema_mismatch"),
                                 ("tools", self.census["tools"][1:], "tools_mismatch"),
                                 ("version", "0.0.0", "version_mismatch")):
            with self.subTest(code=code):
                self.assertEqual(refusal(matrix.check_receipt, self.receipt, census={**self.census, key: value}), code)

    def test_output_schema_cell_needs_validated_results_from_every_tool_and_no_failure(self):
        index = matrix.CELL_IDS.index("output_schemas")
        self.assertEqual(matrix.merge_cells(sessions(), self.census)[index]["status"], "pass")
        for kwargs in ({"validated": 0}, {"failures": ("list_chats:invalid",)}, {"tools": self.census["tools"][1:]}):
            with self.subTest(kwargs=sorted(kwargs)):
                cell = matrix.merge_cells(sessions(**kwargs), self.census)[index]
                self.assertEqual(cell["status"], "fail")

    def test_merge_refuses_a_missing_repeated_or_unknown_cell(self):
        for mutate in (lambda s: s[0]["cells"].pop(), lambda s: s[1]["cells"].append(dict(s[0]["cells"][0])),
                       lambda s: s[0]["cells"].append({"id": "matrix.unknown", "status": "pass", "checks": 1})):
            value = sessions()
            mutate(value)
            with self.subTest(mutate=mutate):
                self.assertEqual(refusal(matrix.merge_cells, value, self.census), "cell_set_mismatch")

    def test_the_inspector_section_is_built_in_its_closed_order(self):
        prepared = {"status": "ready", "node": "22.18.0", "lock_sha256": LOCK}
        cells = [{"id": cell, "status": "pass", "checks": matrix.INSPECTOR_CHECKS[cell]}
                 for cell in reversed(matrix.INSPECTOR_IDS)]
        section = matrix.inspector_section(prepared, {"cells": cells})
        self.assertEqual(section, INSPECTOR_PASS)
        cells[0] = {**cells[0], "status": "fail", "failed_check": "text"}
        self.assertEqual(matrix.inspector_section(prepared, {"cells": cells})["status"], "fail")
        self.assertEqual(refusal(matrix.inspector_section, prepared, {"cells": cells[1:]}), "cell_set_mismatch")
        self.assertEqual(matrix.inspector_section({"status": "unavailable", "reason": "node_unsupported"}, None),
                         {"status": "unavailable", "reason": "node_unsupported"})

    def test_receipt_files_are_canonical_owner_only_and_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            matrix.write_receipt(path, self.receipt)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(matrix.load_receipt(path), self.receipt)
            self.assertEqual(path.read_bytes(), matrix.canonical(self.receipt))
            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text('{"schema": 1, "schema": 2}')
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            for bad in (duplicate, link, Path(directory) / "missing.json"):
                with self.subTest(bad=bad.name):
                    self.assertEqual(refusal(matrix.load_receipt, bad), "receipt_invalid")


def run_command(fake, directory, *, census=None, extra=(), **patches):
    """`run` with the build, install and sessions mocked: what it refuses before any session starts."""
    with mock.patch.object(matrix, "load_gate", return_value=fake), \
            mock.patch.object(matrix, "build_wheel", return_value={"tree": Path(directory), "version": VERSION}), \
            mock.patch.object(matrix, "committed_facts", return_value=FACTS), \
            mock.patch.object(matrix, "installed_runtime",
                              return_value=(SimpleNamespace(python=Path("/v/bin/python")), DERIVED["install"])), \
            mock.patch.object(matrix, "source_census", return_value=census or live_census()), \
            mock.patch.object(matrix, "run_sessions", side_effect=AssertionError("sessions")) as sessions_run, \
            mock.patch.object(matrix, "installer_dependency", side_effect=AssertionError("installer")), \
            contextlib.ExitStack() as stack, contextlib.redirect_stderr(io.StringIO()) as err:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(matrix, name, return_value=value))
        code = matrix.main(["run", "--commit", COMMIT, "--receipt", str(Path(directory) / "r.json"), "--repo",
                            directory, "--workdir", str(Path(directory) / "work"), *extra])
    return code, err.getvalue().strip(), sessions_run


class SchemaChangeTests(unittest.TestCase):
    """Criterion 2: a deliberate schema change (a patched registration) fails run and the receipt check."""

    def census_of(self, directory: Path, patch: str | None) -> dict:
        tree = directory / (patch or "unpatched") / "src"
        shutil.copytree(SOURCE / "telegram_search_mcp", tree / "telegram_search_mcp",
                        ignore=shutil.ignore_patterns("__pycache__"))
        if patch is not None:
            with open(tree / "telegram_search_mcp" / "server.py", "a", encoding="utf-8") as stream:
                stream.write(PATCHES[patch])
        return matrix.source_census(gate, Path(sys.executable), tree, directory)

    def fake_gate(self, directory):
        return SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: Path(directory),
                               Git=lambda repo: object(), resolve_commit=lambda git, value: COMMIT,
                               sha256_file=lambda file, digest=WHEEL: digest, matrix_loaded_sha256=GATE)

    def test_patched_registration_changes_the_census_and_fails_the_receipt_check_and_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = self.census_of(root, None)
            self.assertEqual(baseline, live_census())
            recorded = receipt(baseline)
            matrix.check_receipt(recorded, census=baseline)
            bound = self.census_of(root, "input_bound")
            self.assertEqual(bound["tools"], baseline["tools"])
            self.assertNotEqual(bound["schema_fingerprint"], baseline["schema_fingerprint"])
            self.assertEqual(refusal(matrix.check_receipt, recorded, census=bound), "schema_mismatch")
            extra = self.census_of(root, "extra_tool")
            self.assertEqual(sorted(set(extra["tools"]) - set(baseline["tools"])), ["matrix_patched_tool"])
            self.assertEqual(refusal(matrix.check_receipt, recorded, census=extra), "schema_mismatch")
            self.assertEqual(refusal(matrix.check_receipt, recorded,
                                     census={**extra, "schema_fingerprint": baseline["schema_fingerprint"]}),
                             "tools_mismatch")
            # A receipt taken over the changed schema fails on its own: the cells were written for another one, and
            # passed agent cells change nothing.
            self.assertEqual(refusal(matrix.check_receipt, receipt(bound)), "schema_mismatch")
            self.assertEqual(refusal(matrix.check_receipt, receipt(bound, agents=AGENTS_PASS)), "schema_mismatch")
            # run refuses before any session or install of the installer dependency.
            for patched in (bound, extra):
                with tempfile.TemporaryDirectory() as work:
                    code, err, sessions_run = run_command(self.fake_gate(work), work, census=patched,
                                                          harness_digest=FACTS["harness_sha256"])
                    self.assertEqual((code, err), (2, "schema_mismatch"))
                    sessions_run.assert_not_called()

    def test_check_command_rebuilds_reinstalls_and_refuses_any_difference(self):
        """check rebuilds the wheel, reinstalls the closure and lists the tools again; it refuses another wheel,
        census, install or listing, and a working-tree harness or gate that is not the receipt commit's."""
        census = live_census()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            matrix.write_receipt(path, receipt(census))
            wheel = Path(directory) / "synthetic.whl"
            wheel.write_bytes(b"synthetic wheel")
            built = {"tree": Path(directory), "wheel": wheel}
            patched = {**census, "schema_fingerprint": "f" * 64}
            cases = (({}, None),
                     ({"wheel": "0" * 64}, "wheel_mismatch"),
                     ({"census": patched}, "schema_mismatch"),
                     ({"installed": {**DERIVED["install"], "record_entries": 11}}, "field_mismatch"),
                     ({"installed": {**DERIVED["install"], "freeze": ["mcp==2.2.0", "pydantic==2.1",
                                                                       f"telegram-search-mcp=={VERSION}"]}},
                      "field_mismatch"),
                     ({"listing": "0" * 64}, "field_mismatch"),
                     ({"facts": {**FACTS, "inspector_lock_sha256": "0" * 64}}, "field_mismatch"),
                     ({"facts": {**FACTS, "installer": {**INSTALLER, "sha256": "0" * 64}}}, "field_mismatch"),
                     ({"harness": "1" * 64}, "harness_mismatch"),
                     ({"gate": "2" * 64}, "gate_mismatch"))
            for change, expected in cases:
                fake = SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: Path(directory),
                                       Git=lambda repo: object(), resolve_commit=lambda git, value: COMMIT,
                                       sha256_file=lambda file, digest=change.get("wheel", WHEEL): digest,
                                       matrix_loaded_sha256=change.get("gate", GATE))
                runtime = object()
                with self.subTest(change=sorted(change)), mock.patch.object(matrix, "load_gate", return_value=fake), \
                        mock.patch.object(matrix, "build_wheel", return_value=built), \
                        mock.patch.object(matrix, "committed_facts", return_value=change.get("facts", FACTS)), \
                        mock.patch.object(matrix, "harness_digest",
                                          return_value=change.get("harness", FACTS["harness_sha256"])), \
                        mock.patch.object(matrix, "source_census", return_value=change.get("census", census)), \
                        mock.patch.object(matrix, "installed_runtime",
                                          return_value=(runtime, change.get("installed", DERIVED["install"]))) \
                        as installed, \
                        mock.patch.object(matrix, "observed_listing",
                                          return_value=change.get("listing", DERIVED["listing_sha256"])) as listed, \
                        contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    code = matrix.main(["check", "--receipt", str(path), "--repo", directory])
                self.assertEqual((code, err.getvalue().strip()), (0, "") if expected is None else (2, expected))
                if expected is None:
                    self.assertTrue(out.getvalue().startswith("consumer matrix receipt verified: "))
                    installed.assert_called_once()
                    self.assertIs(listed.call_args.args[0], runtime)
                if expected in ("harness_mismatch", "gate_mismatch", "schema_mismatch"):
                    installed.assert_not_called()


def edit(path: str, value):
    """An update that sets one dotted receipt field ("install.freeze", "cells.3.checks")."""
    def update(document):
        *parents, last = path.split(".")
        target = document
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[int(last) if isinstance(target, list) else last] = value
    return update


def drop(path: str, key: str):
    def update(document):
        target = document
        for part in path.split("."):
            target = target[int(part)] if isinstance(target, list) else target[part]
        del target[key]
    return update


class ReceiptFieldTests(unittest.TestCase):
    def setUp(self):
        self.census = live_census()

    def check(self, document, **bindings):
        return matrix.check_receipt(document, **{"commit": COMMIT, "wheel_sha256": WHEEL, "census": self.census,
                                                 "committed": FACTS, "derived": DERIVED, **bindings})

    def test_a_valid_receipt_passes_every_field_check(self):
        self.check(receipt(self.census))
        self.check(receipt(self.census, approval="real_dialog"))
        self.check(receipt(self.census, inspector=INSPECTOR_UNAVAILABLE))
        self.check(receipt(self.census, agents=AGENTS_PASS))
        self.check(receipt(self.census, agents={"codex": agent_pass(),
                                                "claude_code": agent_pass("2.1.304 (Claude Code)")}))

    def test_each_edited_field_refuses(self):
        """Every field the check_receipt, check_inspector and check_fields docstrings name refuses when edited."""
        cells = {cell: index for index, cell in enumerate(matrix.CELL_IDS)}
        other_closure = ["mcp==2.2.0", "evil==6.6.6", f"telegram-search-mcp=={VERSION}"]
        cases = [
            ("schema", edit("schema", "tgmcp-consumer-matrix/v2"), "receipt_invalid"),
            ("acceptance", edit("acceptance", True), "acceptance_claimed"),
            ("harness.sha256", edit("harness.sha256", "0" * 64), "harness_mismatch"),
            ("harness.path", edit("harness.path", "packaging/other.py"), "harness_mismatch"),
            ("gate.sha256", edit("gate.sha256", "0" * 64), "gate_mismatch"),
            ("gate.path", edit("gate.path", "packaging/other.py"), "gate_mismatch"),
            ("gate.extra", edit("gate.extra", 1), "gate_mismatch"),
            ("tree", edit("tree", "0" * 40), "tree_mismatch"),
            ("version", edit("version", "0.0.0"), "version_mismatch"),
            ("wheel.sha256", edit("wheel.sha256", "0" * 64), "wheel_mismatch"),
            ("wheel.bytes", edit("wheel.bytes", 123456789), "field_mismatch"),
            ("wheel.file", edit("wheel.file", "other-0.0.0-py3-none-any.whl"), "field_mismatch"),
            ("wheel.extra", edit("wheel.extra", 1), "field_mismatch"),
            ("build.python", edit("build.python", "2.7.0"), "field_mismatch"),
            ("build.python_list", edit("build.python", ["3.14.6"]), "field_mismatch"),
            ("build.python_minor", edit("build.python", "3.13.9"), "field_mismatch"),
            ("build.build", edit("build.build", "0.1"), "field_mismatch"),
            ("build.setuptools", edit("build.setuptools", "1"), "field_mismatch"),
            ("build.source_date_epoch", edit("build.source_date_epoch", 1), "field_mismatch"),
            ("build.extra", edit("build.extra", 1), "field_mismatch"),
            ("install.constraints", edit("install.constraints", "packaging/constraints-9.9.9.txt"), "field_mismatch"),
            ("install.constraints_sha256", edit("install.constraints_sha256", "0" * 64), "field_mismatch"),
            ("install.wheelhouse", edit("install.wheelhouse", "elsewhere"), "field_mismatch"),
            ("install.distributions", edit("install.distributions", 3), "field_mismatch"),
            ("install.freeze_closure", edit("install.freeze", other_closure), "field_mismatch"),
            ("install.freeze_types", edit("install.freeze", [1, None, "x"]), "field_mismatch"),
            ("install.pip_check", edit("install.pip_check", "broken"), "field_mismatch"),
            ("install.record_entries", edit("install.record_entries", 1), "field_mismatch"),
            ("install.record_entries_zero", edit("install.record_entries", 0), "field_mismatch"),
            ("install.extra", edit("install.extra", 1), "field_mismatch"),
            ("installer.distribution", edit("installer.distribution", "evil"), "field_mismatch"),
            ("installer.version", edit("installer.version", "0.13.4"), "field_mismatch"),
            ("installer.sha256", edit("installer.sha256", "0" * 64), "field_mismatch"),
            ("installer.toolhouse", edit("installer.toolhouse", "elsewhere"), "field_mismatch"),
            ("installer.extra", edit("installer.extra", 1), "field_mismatch"),
            ("installer.missing", drop("installer", "sha256"), "field_mismatch"),
            ("sdk.version", edit("sdk.version", "9.9.9"), "field_mismatch"),
            ("sdk.version_and_freeze", lambda d: (edit("sdk.version", "9.9.9")(d), edit(
                "install.freeze", ["mcp==9.9.9", "pydantic==2.0", f"telegram-search-mcp=={VERSION}"])(d)),
             "field_mismatch"),
            ("sdk.distribution", edit("sdk.distribution", "other"), "field_mismatch"),
            ("sdk.revisions", edit("sdk.revisions", []), "field_mismatch"),
            ("schema_fingerprint", edit("schema_fingerprint", "0" * 64), "schema_mismatch"),
            ("census.tool_count", edit("census.tool_count", 1), "field_mismatch"),
            ("census.capabilities", edit("census.capabilities", 1), "field_mismatch"),
            ("census.tools_sha256", edit("census.tools_sha256", "0" * 64), "tools_mismatch"),
            ("surface.listing_sha256", edit("surface.listing_sha256", "0" * 64), "field_mismatch"),
            ("surface.listing_form", edit("surface.listing_sha256", "x"), "field_mismatch"),
            ("surface.structured_results_validated", edit("surface.structured_results_validated", 999),
             "field_mismatch"),
            ("surface.tools_with_validated_results", edit("surface.tools_with_validated_results", 999),
             "field_mismatch"),
            ("surface.unschematized_tools", edit("surface.unschematized_tools", ["read_history"]), "field_mismatch"),
            ("surface.extra", edit("surface.extra", 1), "field_mismatch"),
            ("approval.outcome", edit("approval.outcome", "sent"), "field_mismatch"),
            ("approval.mode", edit("approval.mode", "real_dialog"), "field_mismatch"),
            ("cells.passing_with_failed_check", edit(f"cells.{cells['approval.denial']}.failed_check", "x"),
             "checks_mismatch"),
            ("cells.extra", edit("cells.0.evidence", "owner saw it pass"), "checks_mismatch"),
            ("cells.all_one", lambda d: [cell.update(checks=1) for cell in d["cells"]], "checks_mismatch"),
            ("inspector.status", edit("inspector.status", "fail"), "inspector_invalid"),
            ("inspector.package", edit("inspector.package", "@evil/inspector-cli"), "inspector_invalid"),
            ("inspector.version", edit("inspector.version", "1.0.3"), "inspector_invalid"),
            ("inspector.integrity", edit("inspector.integrity", "sha512-0"), "inspector_invalid"),
            ("inspector.node", edit("inspector.node", "18.20.2"), "inspector_invalid"),
            ("inspector.node_form", edit("inspector.node", "v22"), "inspector_invalid"),
            ("inspector.lock_form", edit("inspector.lock_sha256", "x"), "inspector_invalid"),
            ("inspector.lock_sha256", edit("inspector.lock_sha256", "0" * 64), "field_mismatch"),
            ("inspector.extra", edit("inspector.extra", 1), "inspector_invalid"),
            ("inspector.cells_order", lambda d: d["inspector"]["cells"].reverse(), "cell_set_mismatch"),
            ("inspector.cells_missing", lambda d: d["inspector"]["cells"].pop(), "cell_set_mismatch"),
            ("inspector.cell_failed", edit("inspector.cells.0.status", "fail"), "cells_failed"),
            ("inspector.cell_extra", edit("inspector.cells.0.evidence", "x"), "checks_mismatch"),
            ("inspector.unavailable_with_cells", lambda d: d.update(inspector={**INSPECTOR_UNAVAILABLE, "cells": []}),
             "inspector_invalid"),
            ("owner_cells.status", edit("owner_cells.0.status", "pass"), "owner_cell_passed"),
            ("owner_cells.extra", edit("owner_cells.0.evidence", "owner saw it pass"), "field_mismatch"),
            ("owner_cells.client", edit("owner_cells.0.client", "someone"), "field_mismatch"),
            ("owner_cells.provider", drop("owner_cells.0", "provider"), "field_mismatch"),
            ("summary", edit("summary.passed", 1), "receipt_invalid"),
            ("summary.inspector", edit("summary.inspector", "unavailable"), "receipt_invalid"),
            ("summary.inspector_cells", edit("summary.inspector_cells", 0), "receipt_invalid"),
        ]
        for cell, index in cells.items():  # every cell's frozen count, lowered by one
            cases.append((f"cells.{cell}", edit(f"cells.{index}.checks", matrix.CELL_CHECKS[cell] - 1),
                          "checks_mismatch"))
        for index, cell in enumerate(matrix.INSPECTOR_IDS):
            cases.append((f"inspector.{cell}", edit(f"inspector.cells.{index}.checks",
                                                    matrix.INSPECTOR_CHECKS[cell] - 1), "checks_mismatch"))
        for label, update, code in cases:
            with self.subTest(field=label):
                document = receipt(self.census)
                update(document)
                self.assertEqual(refusal(self.check, document), code)
        agent_cases = [
            ("agents.codex.status", edit("agents.codex.status", "fail"), "agent_invalid"),
            ("agents.codex.cli", edit("agents.codex.cli", "codex 1"), "agent_invalid"),
            ("agents.codex.cli_of_claude", edit("agents.codex.cli", "2.1.304 (Claude Code)"), "agent_invalid"),
            ("agents.codex.model", edit("agents.codex.model", "a model; rm"), "agent_invalid"),
            ("agents.codex.extra", edit("agents.codex.extra", 1), "agent_invalid"),
            ("agents.codex.cells_order", lambda d: d["agents"]["codex"]["cells"].reverse(), "cell_set_mismatch"),
            ("agents.codex.cells_missing", lambda d: d["agents"]["codex"]["cells"].pop(), "cell_set_mismatch"),
            ("agents.codex.cell_failed", edit("agents.codex.cells.0.status", "fail"), "cells_failed"),
            ("agents.codex.cell_extra", edit("agents.codex.cells.0.transcript", "x"), "checks_mismatch"),
            ("agents.codex.advisory_unknown", edit("agents.codex.cells.0.advisory_failed", ["answer_unknown"]),
             "agent_invalid"),
            ("agents.codex.advisory_unsorted", edit("agents.codex.cells.0.advisory_failed",
                                                     ["answer_tool_count", "answer_compatible"]), "agent_invalid"),
            ("agents.codex.evidence_form", edit("agents.codex.cells.0.evidence.server", "x"), "agent_invalid"),
            ("agents.codex.evidence_missing", drop("agents.codex.cells.0.evidence", "run"), "agent_invalid"),
            ("agents.claude_code.reason", edit("agents.claude_code.reason", "skipped"), "agent_invalid"),
            ("agents.claude_code.unavailable_extra", edit("agents.claude_code.cli", "2.1.304 (Claude Code)"),
             "agent_invalid"),
            ("agents.missing_column", drop("agents", "claude_code"), "agent_invalid"),
            ("summary.codex", edit("summary.codex", "unavailable"), "receipt_invalid"),
            ("summary.codex_cells", edit("summary.codex_cells", 0), "receipt_invalid"),
        ]
        for index, cell in enumerate(matrix.AGENT_IDS):  # every agent cell's frozen count, lowered by one
            agent_cases.append((f"agents.codex.{cell}", edit(f"agents.codex.cells.{index}.checks",
                                                             matrix.AGENT_CHECKS[cell] - 1), "checks_mismatch"))
        for label, update, code in agent_cases:
            with self.subTest(field=label):
                document = receipt(self.census, agents=AGENTS_PASS)
                update(document)
                self.assertEqual(refusal(self.check, document), code)

    def test_counts_are_frozen_without_bindings_and_follow_the_approval_mode(self):
        document = receipt(self.census, approval="real_dialog")
        matrix.check_receipt(document)
        denial = matrix.CELL_IDS.index("approval.denial")
        document["cells"][denial]["checks"] -= 1  # the scripted count under a real-dialog approval
        self.assertEqual(refusal(matrix.check_receipt, document), "checks_mismatch")
        document = receipt(self.census)
        document["surface"]["structured_results_validated"] -= 1
        self.assertEqual(refusal(matrix.check_receipt, document), "field_mismatch")

    def test_run_refuses_a_working_tree_harness_or_gate_that_differs_from_the_commits(self):
        fake = SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: Path("/"), Git=lambda repo: object(),
                               resolve_commit=lambda git, value: COMMIT, sha256_file=lambda file: "1" * 64,
                               matrix_loaded_sha256=GATE)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(matrix, "installed_runtime", side_effect=AssertionError("installed")):
            self.assertEqual(run_command(fake, directory)[:2], (2, "harness_mismatch"))
        fake.matrix_loaded_sha256 = "2" * 64
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(run_command(fake, directory, harness_digest=FACTS["harness_sha256"])[:2],
                             (2, "gate_mismatch"))

    def test_run_refuses_unusable_inspector_and_toolhouse_arguments_before_building(self):
        with tempfile.TemporaryDirectory() as directory:
            for extra, code in ((["--node", "relative/node"], "node_invalid"),
                                (["--inspector-cache", "relative/cache"], "cache_invalid"),
                                (["--toolhouse", str(Path(directory) / "missing")], "toolhouse_invalid")):
                with self.subTest(extra=extra):  # Refused before the gate is even loaded (fake gate: None).
                    code_seen, err, _ = run_command(None, directory, extra=extra)
                    self.assertEqual((code_seen, err), (2, code))

    def test_the_gate_is_hashed_from_the_bytes_it_runs(self):
        loaded = matrix.load_gate()
        source = (matrix.HARNESS.parent / "release_gate.py").read_bytes()
        self.assertEqual(loaded.matrix_loaded_sha256, matrix._sha(source))
        self.assertEqual(loaded.GATE_PATH, matrix.GATE_PATH)
        self.assertEqual(matrix.harness_digest(), matrix._sha(matrix.HARNESS.read_bytes()))
        self.assertEqual(matrix.installer_pin(loaded)["distribution"], matrix.INSTALLER_DISTRIBUTION)


def fake_node(directory: Path, *, version: str = "v22.18.0", npm_exit: int = 1) -> Path:
    """A stand-in `node` that only reports a version, beside an `npm` that exits with `npm_exit`."""
    bin_dir = directory / "node-bin"
    bin_dir.mkdir()
    (bin_dir / "node").write_text(f"#!/bin/sh\necho {version}\n")
    (bin_dir / "npm").write_text(f"#!/bin/sh\nexit {npm_exit}\n")
    for name in ("node", "npm"):
        (bin_dir / name).chmod(0o700)
    return bin_dir / "node"


class InspectorTests(unittest.TestCase):
    """The Inspector column's pins and its fixed unavailable reasons; the run itself is recorded evidence."""

    def lock(self) -> bytes:
        return (ROOT / matrix.INSPECTOR_DIR / "package-lock.json").read_bytes()

    def test_the_committed_lock_pins_the_whole_tree(self):
        lock = matrix.check_inspector_lock(self.lock())
        manifest = json.loads((ROOT / matrix.INSPECTOR_DIR / "package.json").read_text())
        self.assertEqual(manifest["dependencies"], {matrix.INSPECTOR_PACKAGE: matrix.INSPECTOR_VERSION})
        self.assertIn(matrix.INSPECTOR_SDK_CLIENT.split("/dist/")[0], lock["packages"])

    def test_an_edited_lock_is_refused(self):
        def edited(update):
            value = json.loads(self.lock())
            update(value["packages"])
            return json.dumps(value).encode()
        top = matrix.INSPECTOR_CLI_PATH
        for label, update in (
                ("version", lambda p: p[top].update(version="1.0.3")),
                ("integrity", lambda p: p[top].update(integrity="sha512-AAAA")),
                ("registry", lambda p: p["node_modules/commander"].update(
                    resolved="https://registry.example.test/commander.tgz")),
                ("sha1", lambda p: p["node_modules/commander"].update(integrity="sha1-AAAA")),
                ("script", lambda p: p["node_modules/commander"].update(hasInstallScript=True)),
                ("root", lambda p: p[""].update(dependencies={matrix.INSPECTOR_PACKAGE: "^1.0.2"}))):
            with self.subTest(label=label):
                self.assertEqual(refusal(matrix.check_inspector_lock, edited(update)), "inspector_invalid")
        self.assertEqual(refusal(matrix.check_inspector_lock, b"not json"), "inspector_invalid")

    def test_every_unavailable_reason_is_fixed_and_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tree = base / "tree"
            shutil.copytree(ROOT / matrix.INSPECTOR_DIR, tree / matrix.INSPECTOR_DIR)
            cache = base / "cache"
            cache.mkdir()
            reasons = {}
            reasons["node_not_given"] = matrix.prepare_inspector(tree, None, cache, base / "s1")
            for name in ("old", "new"):
                (base / name).mkdir()
            old = fake_node(base / "old", version="v18.20.2")
            reasons["node_unsupported"] = matrix.prepare_inspector(tree, old, cache, base / "s2")
            current = fake_node(base / "new")
            reasons["inspector_cache_missing"] = matrix.prepare_inspector(tree, current, base / "absent", base / "s3")
            reasons["inspector_install_failed"] = matrix.prepare_inspector(tree, current, cache, base / "s4")
            (tree / matrix.INSPECTOR_DIR / "package-lock.json").write_text("{}")
            reasons["inspector_lock_invalid"] = matrix.prepare_inspector(tree, current, cache, base / "s5")
            self.assertEqual({reason: value for reason, value in reasons.items()},
                             {reason: {"status": "unavailable", "reason": reason} for reason in reasons})
            self.assertEqual(set(reasons), set(matrix.INSPECTOR_UNAVAILABLE))

    def test_the_cache_fetch_refuses_unusable_arguments_with_fixed_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            node = fake_node(Path(directory))
            for argv, code in ((["inspector-fetch", "--node", "node", "--cache", directory], "node_invalid"),
                               (["inspector-fetch", "--node", str(node), "--cache", "cache"], "cache_invalid"),
                               (["inspector-fetch", "--node", str(node)], "usage")):
                with self.subTest(code=code), contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(matrix.main(argv), 2)
                self.assertEqual(err.getvalue().strip(), code)
            # The fake npm fails the online fetch; the tree need not be a git checkout (the gate runs the sdist's).
            fake = SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: ROOT)
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                    mock.patch.object(matrix, "load_gate", return_value=fake):
                self.assertEqual(matrix.main(["inspector-fetch", "--node", str(node), "--cache",
                                              str(Path(directory) / "cache")]), 2)
            self.assertEqual(err.getvalue().strip(), "fetch_failed")
            self.assertTrue((Path(directory) / "cache").is_dir())

    def test_the_session_script_loads_only_the_request_paths(self):
        imports = [line for line in matrix.INSPECTOR_SESSION_SCRIPT.splitlines() if "import" in line]
        self.assertEqual([line.split(" from ")[-1] for line in imports if line.startswith("import ")],
                         ['"node:fs";', '"node:url";'])
        self.assertIn("request.cli + \"/build/client/index.js\"", matrix.INSPECTOR_SESSION_SCRIPT)
        self.assertNotIn("http", matrix.INSPECTOR_SESSION_SCRIPT)


class InstallerDependencyTests(unittest.TestCase):
    """The bundle session's tomlkit: the gate's pinned wheel from --toolhouse or a hash-checked download."""

    def wheel(self, directory: Path) -> tuple[Path, dict]:
        house = directory / "toolhouse"
        house.mkdir()
        path = house / "tomlkit-0.13.3-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("tomlkit/__init__.py", "__version__ = '0.13.3'\n")
            archive.writestr("tomlkit-0.13.3.dist-info/RECORD", "")
        return house, {"distribution": "tomlkit", "version": "0.13.3", "sha256": matrix._sha(path.read_bytes())}

    def test_a_provided_wheel_is_hash_checked_and_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            house, pin = self.wheel(Path(directory))
            with mock.patch.object(matrix, "installer_pin", return_value=pin):
                path, facts = matrix.installer_dependency(gate, house, Path("/unused"), Path(directory) / "installer")
            self.assertEqual(facts, {**pin, "toolhouse": "provided"})
            self.assertEqual(sorted(p.name for p in path.iterdir()), ["tomlkit"])
            with mock.patch.object(matrix, "installer_pin", return_value={**pin, "sha256": "0" * 64}):
                self.assertEqual(refusal(matrix.installer_dependency, gate, house, Path("/unused"),
                                         Path(directory) / "other"), "toolhouse_invalid")
            self.assertEqual(refusal(matrix.installer_dependency, gate, Path(directory), Path("/unused"),
                                     Path(directory) / "third"), "toolhouse_invalid")

    def test_the_pin_is_the_release_gates(self):
        self.assertEqual(matrix.installer_pin(gate), {"distribution": "tomlkit", "version": "0.13.3",
                                                      "sha256": dict((n, d) for n, _, d in gate.TOOLS)["tomlkit"]})


class SyntheticAccountTests(unittest.TestCase):
    def client(self, **kwargs):
        from telegram_search_mcp.tdjson import TDLibClient
        log = []
        raw = matrix.SyntheticTDLib(record=log.append, **kwargs)
        return TDLibClient(raw=raw, request_timeout=5), log

    def test_the_synthetic_account_answers_through_the_real_client(self):
        from telegram_search_mcp.tdjson import TDLibError
        client, log = self.client()
        client.ensure_ready()
        self.assertEqual(client.get_account_id(), matrix.ACCOUNT_ID)
        self.assertEqual(client.resolve_target("@" + matrix.PUBLIC_USERNAME)["id"], matrix.FIRST_CHAT)
        snapshot = client.get_chat_list_snapshot("main", limit=200)
        self.assertEqual(snapshot["chat_ids"], list(range(matrix.FIRST_CHAT, matrix.FIRST_CHAT + matrix.CHAT_COUNT)))
        self.assertEqual(client.get_chat_list_snapshot("archive", limit=200)["chat_ids"], [])
        self.assertEqual(client.get_message(matrix.FIRST_CHAT, matrix.SOURCE_MESSAGE)["content"]["text"]["text"],
                         matrix.SOURCE_TEXT)
        for call in (lambda: client.resolve_target("@nobody_here"), lambda: client.get_chat_metadata(5),
                     lambda: client.get_message(matrix.FIRST_CHAT, 1)):
            with self.assertRaises(TDLibError):
                call()
        self.assertNotIn({"request": "sendMessage"}, log)

    def test_history_follows_the_documented_offsets(self):
        client, _ = self.client()
        client.ensure_ready()
        ids = lambda messages: [message["id"] for message in messages]
        own, document, source = matrix.OWN_TEXT_MESSAGE, matrix.OWN_DOCUMENT_MESSAGE, matrix.SOURCE_MESSAGE
        self.assertEqual(ids(client.get_chat_history(matrix.FIRST_CHAT, from_message_id=0, limit=10)),
                         [document, own, source])
        self.assertEqual(ids(client.get_chat_history(matrix.FIRST_CHAT, from_message_id=own, limit=10)), [own, source])
        self.assertEqual(ids(client.get_context_messages(matrix.FIRST_CHAT, own, 1)), [document, own, source])
        found = client.search_chat_messages(matrix.FIRST_CHAT, "own", from_message_id=0, limit=10)
        self.assertEqual(ids(found["messages"]), [document, own])

    def test_own_messages_and_reply_family_sources_have_their_permissions(self):
        client, _ = self.client()
        client.ensure_ready()
        self.assertTrue(client.read_own_text_message(matrix.FIRST_CHAT, matrix.OWN_TEXT_MESSAGE).can_be_edited)
        self.assertTrue(client.read_own_caption_message(matrix.FIRST_CHAT, matrix.OWN_DOCUMENT_MESSAGE).can_be_edited)
        for message_id, text, entity, offset, length, _ in matrix.FAMILY_SOURCES:
            with self.subTest(message_id=message_id):
                message = client.get_message(matrix.FAMILY_CHAT, message_id)
                self.assertEqual(message["content"]["text"]["entities"][0]["type"], entity)
                self.assertEqual(len(text[offset:].encode("utf-16-le")) // 2, length)

    def test_a_send_is_logged_then_lost_so_its_outcome_is_unknown(self):
        from telegram_search_mcp.tdjson import MessageSendOutcomeUnknown
        client, log = self.client()
        client.ensure_ready()
        with self.assertRaises(MessageSendOutcomeUnknown):
            client.send_text_message(matrix.FIRST_CHAT, "Synthetic text")
        self.assertEqual(log.count({"request": "sendMessage"}), 1)

    def test_an_unauthorized_account_blocks(self):
        from telegram_search_mcp.tdjson import AuthorizationBlocked
        client, _ = self.client(authorized=False)
        with self.assertRaises(AuthorizationBlocked):
            client.ensure_ready()

    def test_png_fixture_is_a_valid_deterministic_image(self):
        from PIL import Image
        data = matrix.synthetic_png()
        self.assertEqual(data, matrix.synthetic_png())
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            self.assertEqual((image.size, image.mode), ((8, 8), "RGB"))

    def test_mp4_fixture_is_an_eligible_native_video(self):
        from telegram_search_mcp import video_probe
        self.assertEqual(matrix.synthetic_mp4(), matrix.synthetic_mp4())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.mp4"
            path.write_bytes(matrix.synthetic_mp4())
            probe = video_probe.probe_video(path, size_bytes=path.stat().st_size)
        self.assertEqual((probe.width, probe.height, probe.duration_seconds, probe.codec, probe.supports_streaming),
                         (1280, 720, 5, "h264", True))


class ScriptedOwnerTests(unittest.TestCase):
    def prompt(self, *, real_dialog=False, rendered=("Send text\n│ body", "send"), decision=None):
        from telegram_search_mcp import approval_prompt
        log = []
        dialog = mock.Mock(return_value=decision or approval_prompt.TIMEOUT)
        patches = (mock.patch.object(approval_prompt, "render_approval", return_value=rendered),
                   mock.patch.object(approval_prompt, "decide_approved_send", dialog))
        return log, dialog, patches, matrix.approval_prompt(log.append, real_dialog=real_dialog)

    def test_scripted_owner_declines_denials_approves_the_outcome_probe_and_times_out_the_timeout_probe(self):
        from telegram_search_mcp import approval_prompt
        log, dialog, patches, prompt = self.prompt()
        with patches[0], patches[1]:
            self.assertIs(prompt(text=matrix.DENIAL_PROBE, recipient=1, kind="text"), approval_prompt.DECLINED)
            self.assertIs(prompt(text=matrix.OUTCOME_PROBE, recipient=1, kind="text"), approval_prompt.APPROVED)
            self.assertIs(prompt(text=matrix.TIMEOUT_PROBE, recipient=1, kind="text"), approval_prompt.TIMEOUT)
            self.assertIs(prompt(text="", recipient=1, forward_preview={"x": 1}), approval_prompt.DECLINED)
            self.assertIs(prompt(text="", recipient=1, kind="photo"), approval_prompt.DECLINED)
        dialog.assert_not_called()
        self.assertEqual([(entry["prompt"], entry["outcome"], entry["rows"], entry["family"], entry["verb"])
                          for entry in log],
                         [("denial", "declined", 2, "text", "send"), ("outcome", "approved", 2, "text", "send"),
                          ("timeout", "timeout", 2, "text", "send"), ("other", "declined", 2, "forward", "send"),
                          ("other", "declined", 2, "artifact", "send")])

    def test_real_dialog_mode_opens_the_dialog_with_a_five_second_timer(self):
        from telegram_search_mcp import approval_prompt
        log, dialog, patches, prompt = self.prompt(real_dialog=True)
        with patches[0], patches[1]:
            self.assertIs(prompt(text=matrix.DENIAL_PROBE, recipient=1), approval_prompt.TIMEOUT)
            self.assertIs(prompt(text=matrix.OUTCOME_PROBE, recipient=1), approval_prompt.APPROVED)
        dialog.assert_called_once_with(text=matrix.DENIAL_PROBE, recipient=1, timeout_seconds=5)
        self.assertEqual((log[0]["mode"], log[0]["outcome"]), ("real_dialog", "timeout"))

    def test_an_unrenderable_draft_is_dialog_unavailable(self):
        from telegram_search_mcp import approval_prompt
        log, dialog, patches, prompt = self.prompt(rendered=None)
        with patches[0], patches[1]:
            self.assertIs(prompt(text=matrix.OUTCOME_PROBE, recipient=1), approval_prompt.UNAVAILABLE)
        self.assertEqual(log, [{"prompt": "outcome", "family": "artifact", "rendered": False, "rows": 0, "verb": None,
                                "outcome": "dialog_unavailable"}])


class LayoutTests(unittest.TestCase):
    def test_session_policies_load_as_the_runtime_reads_them(self):
        from telegram_search_mcp.config import load_runtime_policy
        census = live_census()
        with tempfile.TemporaryDirectory() as directory:
            for name in matrix.SESSIONS + ("inspector", "listing"):
                capabilities, stale, _ = matrix.session_layout(name, census)
                home = Path(directory) / name
                home.mkdir()
                matrix.write_policy(home, capabilities, stale=stale)
                root = home / "Library" / "Application Support" / "TelegramSearchMCP"
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((root / "runtime.toml").stat().st_mode), 0o600)
                policy = load_runtime_policy(root / "runtime.toml")
                self.assertEqual(policy.enabled_capabilities, tuple(sorted(capabilities)))
                self.assertEqual(policy.expected_package_version, matrix.STALE_PIN if stale else None)
        self.assertEqual(matrix.session_layout("full", census)[2], "authorized")
        self.assertEqual(matrix.session_layout("inspector", census)[2], "authorized")
        self.assertEqual(matrix.session_layout("unauthorized", census)[2], "unauthorized")
        for name in ("offline", "closed", "bundle"):
            self.assertIsNone(matrix.session_layout(name, census)[2])
        self.assertEqual(matrix.session_layout("closed", census)[0], [])

    def test_installed_runs_are_isolated_and_source_runs_use_the_tree(self):
        installed = matrix.Runtime(python=Path("/v/bin/python"), proxy=["/v/bin/p"], package_root=Path("/v"),
                                   sdk_root=Path("/v"))
        self.assertEqual(installed.command("drive")[:3], ["/v/bin/python", "-I", str(matrix.HARNESS)])
        self.assertNotIn("PYTHONPATH", installed.env(Path("/h"), Path("/t")))
        source = matrix.Runtime(python=Path("/p"), proxy=["/p"], package_root=SOURCE, sdk_root=None,
                                pythonpath=str(SOURCE))
        self.assertEqual(source.command("drive")[:2], ["/p", str(matrix.HARNESS)])
        self.assertEqual(source.env(Path("/h"), Path("/t"))["PYTHONPATH"], str(SOURCE))

    def test_cli_refuses_with_fixed_codes_without_echoing_input(self):
        for argv, code in ((["bogus"], "usage"), (["run", "--commit", "abc1234", "--receipt", "relative.json"], "usage"),
                           (["check", "--receipt", "/nonexistent/receipt.json"], "receipt_invalid")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(matrix.main(argv), 2)
            self.assertEqual(err.getvalue().strip(), code)
        with self.assertRaises(AssertionError):
            matrix.MatrixRefusal("not_a_code")


class BundleWorldTests(unittest.TestCase):
    def test_the_fake_launchctl_serves_only_its_label_and_never_starts_a_process(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            plist = Path(directory) / "job.plist"
            plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/r/python3", "-I", "/r/launcher.py"]}))
            fake = matrix.FakeLaunchctl(plist)
            with matrix.no_other_process():
                self.assertEqual(fake(["/bin/launchctl", "print", "gui/1/job"]).returncode, 113)
                self.assertEqual(fake(["/bin/launchctl", "bootstrap", "gui/1", str(plist)]).returncode, 0)
                self.assertIn("program = /r/python3", fake(["/bin/launchctl", "print", "gui/1/job"]).stdout)
                self.assertEqual(fake(["/bin/launchctl", "kickstart", "-k", "gui/1/job"]).returncode, 64)
                self.assertEqual(fake(["/bin/launchctl", "bootout", "gui/1/job"]).returncode, 0)
                with self.assertRaises(AssertionError):
                    __import__("subprocess").run(["/usr/bin/true"])
            self.assertEqual(fake.verbs, ["print", "bootstrap", "print", "kickstart", "bootout"])
            self.assertIsNone(fake.loaded)


class SourceSessionsTests(unittest.TestCase):
    """Every synthetic session from this tree (proxy, broker and driver as subprocesses): every cell passes."""

    def test_every_synthetic_cell_passes_from_source(self):
        """Every cell passes with exactly its frozen check count and the frozen surface counts; the listing
        session (no broker, what check re-derives) sees the same tool listing as the full session."""
        census = live_census()
        runtime = matrix.Runtime(python=Path(sys.executable), proxy=[sys.executable, "-m", "telegram_search_mcp.server"],
                                 package_root=SOURCE, sdk_root=None, pythonpath=str(SOURCE))
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "sessions"
            work.mkdir()
            results = matrix.run_sessions(runtime, census, work, approval="scripted",
                                          sessions=matrix.SESSIONS + ("listing",))
            logs = {path.name: path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    for path in work.glob("*.log")}
        *results, listing = results
        built = matrix.build_receipt(commit=COMMIT, tree=OTHER, version=census["version"],
                                     wheel={"file": "source", "sha256": WHEEL, "bytes": 0}, build={},
                                     install={"source": "tree"}, installer={"source": "tree"}, census=census,
                                     sessions=results, inspector=dict(INSPECTOR_UNAVAILABLE),
                                     harness_sha256="e" * 64, gate_sha256=GATE)
        failed = {cell["id"]: cell["failed_check"] for cell in built["cells"] if cell["status"] != "pass"}
        self.assertEqual(failed, {}, json.dumps(logs, indent=1)[-6000:])
        self.assertEqual({cell["id"]: cell["checks"] for cell in built["cells"]}, matrix.CELL_CHECKS)
        matrix.check_receipt(built, census=census)
        self.assertEqual(built["approval"], {"mode": "scripted", "outcome": "declined"})
        self.assertEqual({key: built["surface"][key] for key in matrix.SURFACE_COUNTS}, matrix.SURFACE_COUNTS)
        self.assertEqual(built["sdk"]["revisions"], list(matrix.REVISIONS))
        self.assertEqual((listing["session"], listing["cells"]), ("listing", []))
        self.assertRegex(built["surface"]["listing_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(listing["observed"]["listing_sha256"], built["surface"]["listing_sha256"])
        full = next(result for result in results if result["session"] == "full")
        self.assertEqual(full["observed"]["every_tool_dispatches"], 0)


# --- the agent columns ------------------------------------------------------------------------------------------

FAKE_AGENT = TESTS / "matrix_fake_agent.py"
SERVER = _identity_server_name()  # the client entries' name, read through the product identity
SHELL_VARIABLES = {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}  # what /bin/sh and macOS add on their own


def source_runtime():
    return matrix.Runtime(python=Path(sys.executable), proxy=[sys.executable, "-m", "telegram_search_mcp.server"],
                          package_root=SOURCE, sdk_root=None, pythonpath=str(SOURCE))


def fake_cli(directory: Path, role: str, behaviour: str = "honest", *, report: Path | None = None) -> Path:
    """A wrapper named like the CLI that runs the stand-in agent with this interpreter."""
    path = directory / f"{role}-{behaviour}"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_AGENT}" --as {role} --behaviour {behaviour} '
                    f'--harness "{matrix.HARNESS}"' + (f' --report "{report}"' if report else "") + ' -- "$@"\n')
    path.chmod(0o700)
    return path


# A Codex auth.json of the shape Codex writes (synthetic values only).
CODEX_AUTH = (b'{"OPENAI_API_KEY": null, "last_refresh": "2026-10-25T00:00:00Z", "tokens": {"access_token": "synthetic-a", '
              b'"account_id": "synthetic-account", "id_token": "synthetic-i", "refresh_token": "synthetic-r"}}\n')


def bindings_for(census: dict) -> dict:
    return {"commit": COMMIT, "wheel_sha256": WHEEL, "version": census["version"],
            "schema_fingerprint": census["schema_fingerprint"], "server_name": SERVER}


def run_column(base: Path, column: str, behaviour: str = "honest", *, cells=matrix.AGENT_IDS, auth: bytes | None = b"",
               binary: Path | None = None):
    """One agent column from this source tree with the stand-in agent; (section, evidence directory, Codex home)."""
    census = live_census()
    codex_home = base / "codex-home"
    codex_home.mkdir(exist_ok=True)
    if auth is not None:
        (codex_home / "auth.json").write_bytes(auth or b'{"synthetic": "credentials"}\n')
    evidence = base / f"evidence-{column}-{behaviour}"
    evidence.mkdir()
    role = "codex" if column == "codex" else "claude"
    with mock.patch.object(matrix, "AGENT_RETRY_SECONDS", 0):
        section = matrix.run_agent_column(column, binary or fake_cli(base, role, behaviour), None, source_runtime(),
                                          census, base / f"scratch-{column}-{behaviour}", evidence,
                                          bindings_for(census), codex_home=codex_home, cells=cells)
    return section, evidence, codex_home


def isolation(evidence: Path, column: str, cell: str) -> dict:
    return json.loads((evidence / column / cell / "attempt-1" / "events.jsonl").read_text().splitlines()[0])


class AgentTableTests(unittest.TestCase):
    def test_the_agent_cells_are_closed_and_cover_the_requested_set(self):
        self.assertEqual(len(set(matrix.AGENT_IDS)), len(matrix.AGENT_IDS))
        self.assertEqual(set(matrix.AGENT_CHECKS), set(matrix.AGENT_IDS))
        self.assertEqual(set(matrix.AGENT_ADVISORY), set(matrix.AGENT_IDS))
        self.assertEqual({session for _, session, _, _ in matrix.AGENT_CELLS}, {"full", "restricted", "alias"})
        requests = {cell: matrix.agent_request(cell, SERVER) for cell in matrix.AGENT_IDS}
        self.assertIn("check_broker set to true", requests["agent.census"])
        self.assertIn("limit 1", requests["agent.search_cursor"])
        self.assertIn(matrix.TIMEOUT_PROBE, requests["agent.approval_timeout"])
        self.assertIn(f"Use the {SERVER}_{matrix.ALIAS} server", requests["agent.alias_server"])
        self.assertTrue(all("{" not in request for request in requests.values()))
        self.assertIn(matrix.EDIT_TEXT, requests["agent.own_message_prepare"])
        self.assertIn(matrix.FAMILY_REPLY_TEXT, requests["agent.family_reply"])
        self.assertTrue(all(request.startswith("You are helping the owner") for request in requests.values()))
        self.assertNotIn("codex.synthetic", matrix.OWNER_IDS)
        self.assertNotIn("claude_code.synthetic", matrix.OWNER_IDS)
        # The family source names more than the base reply capability under the restricted policy.
        missing = next(row[5] for row in matrix.FAMILY_SOURCES if row[0] == matrix.FAMILY_MESSAGE)
        self.assertGreater(len(missing), 1)
        self.assertTrue(set(missing).isdisjoint(matrix.RESTRICTED_CAPABILITIES))

    def test_the_server_name_is_read_from_the_trees_product_identity(self):
        self.assertEqual(matrix.identity_server_name(ROOT), SERVER)
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "src" / "telegram_search_mcp" / "product_identity.py"
            identity.parent.mkdir(parents=True)
            for text in ("", "MCP_SERVER_NAME = 'Bad Name'\n", "MCP_SERVER_NAME = 7\n",
                         "MCP_SERVER_NAME = 'a'\nMCP_SERVER_NAME = 'b'\n"):
                with self.subTest(text=text):
                    identity.write_text(text)
                    self.assertEqual(refusal(matrix.identity_server_name, Path(directory)), "census_failed")
            identity.write_text("MCP_SERVER_NAME: Final = 'renamed_server'\n")
            self.assertEqual(matrix.identity_server_name(Path(directory)), "renamed_server")
            self.assertIn("Use the renamed_server_work server", matrix.agent_request("agent.alias_server",
                                                                                      "renamed_server"))

    def test_the_alias_fits_the_socket_and_tool_name_budgets(self):
        from telegram_search_mcp import account_registry
        home = Path("/private/tmp/tga.XXXXXXXX")  # what run_agent_cell's mkdtemp makes
        account_registry.check_budgets(matrix.ALIAS, home)

    def test_the_codex_home_names_only_the_matrix_proxy(self):
        import tomllib
        env = {"HOME": "/h", "PATH": matrix.SYSTEM_PATH, "LC_ALL": "C", "TMPDIR": "/t"}
        document = tomllib.loads(matrix.codex_config("telegram_search_work", ["/v/bin/p", "--account", "work"], env,
                                                     Path("/h")))
        self.assertEqual(document, {
            "approval_policy": "never", "sandbox_mode": "read-only", "cli_auth_credentials_store": "file",
            "features": {name: False for name in matrix.CODEX_DISABLED_FEATURES},
            "mcp_servers": {"telegram_search_work": {
                "command": "/v/bin/p", "args": ["--account", "work"], "cwd": "/h", "env": env,
                "startup_timeout_sec": 60, "tool_timeout_sec": 120, "default_tools_approval_mode": "approve"}}})
        self.assertEqual(json.loads(matrix.claude_config("telegram_search", ["/v/bin/p"], env)),
                         {"mcpServers": {"telegram_search": {"command": "/v/bin/p", "args": [], "env": env}}})

    def test_the_commands_keep_the_conversation_runners_isolation(self):
        codex = [str(part) for part in matrix.agent_command(
            "codex", Path("/c"), "m", "request", server="telegram_search", workdir=Path("/w"), answer=Path("/a"),
            mcp_config=None)]
        self.assertEqual(codex, ["/c", "exec", "--strict-config", "--ephemeral", "--skip-git-repo-check", "--sandbox",
                                 "read-only", "--cd", "/w", "--json", "--color", "never", "--output-last-message", "/a",
                                 "--model", "m", "request"])
        self.assertNotIn("--ignore-user-config", codex)  # the private home's config.toml is the whole configuration
        claude = [str(part) for part in matrix.agent_command(
            "claude_code", Path("/c"), None, "request", server="telegram_search_work", workdir=Path("/w"),
            answer=Path("/a"), mcp_config=Path("/m.json"))]
        self.assertEqual(claude, ["/c", "-p", "request", "--output-format", "stream-json", "--verbose", "--max-turns",
                                  "40", "--setting-sources", "", "--settings", '{"autoMemoryEnabled":false}',
                                  "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", "/m.json",
                                  "--tools", "", "--allowedTools", "mcp__telegram_search_work",
                                  "--no-session-persistence"])

    def test_the_agent_environment_passes_nothing_else_of_the_callers(self):
        host = {"CLAUDE_CODE_SESSION_ID": "s", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "OPENAI_API_KEY": "k",
                "ANTHROPIC_API_KEY": "k", "HTTPS_PROXY": "http://proxy.test:3128", "CODEX_HOME": "/elsewhere"}
        with mock.patch.dict(os.environ, host):
            codex = matrix.agent_environment("codex", home=Path("/h"), config=Path("/c"), tmp=Path("/t"))
            claude = matrix.agent_environment("claude_code", home=Path("/h"), config=Path("/c"), tmp=Path("/t"))
        self.assertEqual(codex, {"HOME": "/h", "PATH": matrix.SYSTEM_PATH, "TMPDIR": "/t", "LANG": "en_US.UTF-8",
                                 "CODEX_HOME": "/c", "HTTPS_PROXY": "http://proxy.test:3128"})
        self.assertEqual(claude, {"HOME": str(Path.home()), "USER": pwd.getpwuid(os.getuid()).pw_name,
                                  "PATH": matrix.SYSTEM_PATH, "TMPDIR": "/t", "LANG": "en_US.UTF-8",
                                  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                                  "HTTPS_PROXY": "http://proxy.test:3128", "ANTHROPIC_API_KEY": "k"})

    def test_refreshed_codex_credentials_go_back_only_onto_an_unchanged_file(self):
        refreshed = CODEX_AUTH.replace(b"synthetic-r", b"synthetic-r2").replace(b"synthetic-a", b"synthetic-a2")
        with tempfile.TemporaryDirectory() as directory:
            source, private = Path(directory) / "auth.json", Path(directory) / "private.json"
            source.write_bytes(CODEX_AUTH)
            private.write_bytes(CODEX_AUTH)
            copied = matrix._sha(CODEX_AUTH)
            self.assertEqual(matrix.sync_codex_auth(source, copied, private), "unchanged")
            private.write_bytes(refreshed)
            source.write_bytes(b"changed meanwhile")
            self.assertEqual(matrix.sync_codex_auth(source, copied, private), "kept")
            self.assertEqual(source.read_bytes(), b"changed meanwhile")
            source.write_bytes(CODEX_AUTH)
            self.assertEqual(matrix.sync_codex_auth(source, copied, private), "returned")
            self.assertEqual((source.read_bytes(), stat.S_IMODE(source.stat().st_mode)), (refreshed, 0o600))
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["auth.json", "private.json"])

    def test_only_a_well_formed_refreshed_credential_goes_back(self):
        value = json.loads(CODEX_AUTH)
        bad = [b"", CODEX_AUTH[:40], b"[]", b"null", json.dumps({**value, "extra": 1}).encode(),
               json.dumps({key: item for key, item in value.items() if key != "last_refresh"}).encode(),
               json.dumps({**value, "tokens": {**value["tokens"], "refresh_token": ""}}).encode(),
               json.dumps({**value, "tokens": {**value["tokens"], "access_token": None}}).encode(),
               json.dumps({**value, "tokens": {"refresh_token": "x"}}).encode(),
               json.dumps({**value, "tokens": "x"}).encode()]
        with tempfile.TemporaryDirectory() as directory:
            source, private = Path(directory) / "auth.json", Path(directory) / "private.json"
            for data in bad:
                with self.subTest(data=data[:60]):
                    source.write_bytes(CODEX_AUTH)
                    private.write_bytes(data)
                    self.assertEqual(matrix.sync_codex_auth(source, matrix._sha(CODEX_AUTH), private), "invalid")
                    self.assertEqual(source.read_bytes(), CODEX_AUTH)
            api_key = b'{"OPENAI_API_KEY": "synthetic-key"}\n'
            source.write_bytes(api_key)
            private.write_bytes(b'{"OPENAI_API_KEY": ""}\n')
            self.assertEqual(matrix.sync_codex_auth(source, matrix._sha(api_key), private), "invalid")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["auth.json", "private.json"])

    def test_a_failed_write_back_keeps_the_callers_file_and_leaves_no_temporary_file(self):
        refreshed = CODEX_AUTH.replace(b"synthetic-r", b"synthetic-r2")
        with tempfile.TemporaryDirectory() as directory:
            caller = Path(directory) / "caller"
            caller.mkdir()
            source, private = caller / "auth.json", Path(directory) / "private.json"
            source.write_bytes(CODEX_AUTH)
            private.write_bytes(refreshed)
            copied = matrix._sha(CODEX_AUTH)
            caller.chmod(0o500)  # the temporary file cannot be created
            try:
                self.assertEqual(matrix.sync_codex_auth(source, copied, private), "failed")
            finally:
                caller.chmod(0o700)
            for error in (OSError(28, "No space left on device"), matrix.MatrixInterrupted(signal.SIGTERM)):
                with self.subTest(error=type(error).__name__), mock.patch.object(matrix.os, "replace",
                                                                                 side_effect=error):
                    if isinstance(error, OSError):
                        self.assertEqual(matrix.sync_codex_auth(source, copied, private), "failed")
                    else:
                        with self.assertRaises(matrix.MatrixInterrupted):
                            matrix.sync_codex_auth(source, copied, private)
                self.assertEqual(sorted(path.name for path in caller.iterdir()), ["auth.json"])
                self.assertEqual(source.read_bytes(), CODEX_AUTH)

    def test_the_column_scratch_is_removed_even_when_the_write_back_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(matrix, "sync_codex_auth", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                run_column(Path(directory), "codex", cells=("agent.census",), auth=CODEX_AUTH)
            self.assertFalse((Path(directory) / "scratch-codex-honest").exists())

    def test_an_agent_cell_records_its_attempts(self):
        for attempts in (0, matrix.AGENT_ATTEMPTS + 1, "1", True, None):
            with self.subTest(attempts=attempts):
                section = agent_pass()
                section["cells"][0]["attempts"] = attempts
                self.assertEqual(refusal(matrix.check_agent_section, "codex", section), "agent_invalid")
        section = agent_pass()
        del section["cells"][0]["attempts"]
        self.assertEqual(refusal(matrix.check_agent_section, "codex", section), "checks_mismatch")
        section = agent_pass()
        section["cells"][0]["attempts"] = matrix.AGENT_ATTEMPTS
        matrix.check_agent_section("codex", section)

    def test_run_refuses_unusable_agent_arguments_before_building(self):
        with tempfile.TemporaryDirectory() as directory:
            full = Path(directory) / "full"
            full.mkdir()
            (full / "kept").write_text("x")
            linked = Path(directory) / "linked-work-tree"
            linked.mkdir()
            (linked / ".git").write_text("gitdir: /elsewhere\n")  # what `git worktree add` leaves
            codex = ["--codex", "/bin/codex", "--codex-home", "/h"]
            for extra, code in ((["--codex", "relative/codex"], "agent_invalid"),
                                (["--claude", "/bin/claude", "--claude-model", "a model"], "agent_invalid"),
                                (["--codex", "/bin/codex"], "agent_invalid"),  # the Codex home is named explicitly
                                (["--codex", "/bin/codex", "--codex-home", "relative"], "agent_invalid"),
                                (codex, "evidence_invalid"),
                                ([*codex, "--agent-evidence", "relative"], "evidence_invalid"),
                                ([*codex, "--agent-evidence", str(full)], "evidence_invalid"),
                                ([*codex, "--agent-evidence", str(ROOT / "matrix-evidence")], "evidence_invalid"),
                                ([*codex, "--agent-evidence", str(linked / "a" / "evidence")], "evidence_invalid")):
                with self.subTest(extra=extra):  # Refused before the gate is even loaded (fake gate: None).
                    code_seen, err, _ = run_command(None, directory, extra=extra)
                    self.assertEqual((code_seen, err), (2, code))
            self.assertFalse((ROOT / "matrix-evidence").exists())
            self.assertEqual(sorted(path.name for path in linked.iterdir()), [".git"])

    def test_run_keeps_its_receipt_work_directory_and_evidence_out_of_every_git_work_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            linked = Path(directory) / "linked-work-tree"
            linked.mkdir()
            (linked / ".git").write_text("gitdir: /elsewhere\n")
            for parent in (ROOT, linked, ROOT / "docs"):
                with self.subTest(parent=str(parent)), contextlib.redirect_stderr(io.StringIO()) as err, \
                        mock.patch.object(matrix, "load_gate", side_effect=AssertionError("gate")):
                    code = matrix.main(["run", "--commit", COMMIT, "--receipt", str(parent / "r.json"),
                                        "--repo", directory])
                    self.assertEqual((code, err.getvalue().strip()), (2, "usage"))
                    for path, function, expected in ((parent / "matrix-work", matrix._workdir, "workdir_invalid"),
                                                     (parent / "matrix-evidence", matrix.evidence_directory,
                                                      "evidence_invalid")):
                        self.assertEqual(refusal(function, str(path)), expected)
                        self.assertFalse(path.exists())
            self.assertTrue(matrix.inside_work_tree(ROOT / "missing" / "deeper"))
            self.assertFalse(matrix.inside_work_tree(Path(directory) / "outside"))


class AgentColumnTests(unittest.TestCase):
    """The Codex column from this source tree with an honest stand-in agent: every cell's evidence, its verdict, the
    receipt binding and check's second judgement."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="tgmcp-agents-")).resolve()
        cls.census = live_census()
        cls.section, cls.evidence, cls.codex_home = run_column(cls.base, "codex")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def files(self, cell: str) -> dict:
        return matrix.read_evidence(self.evidence / "codex" / cell / "attempt-1")

    def judged(self, cell: str, files: dict) -> dict:
        return matrix.judge_evidence("codex", cell, files, bindings_for(self.census))

    def test_every_cell_passes_with_its_frozen_count(self):
        failed = {cell["id"]: cell.get("failed_check") for cell in self.section.get("cells", [])
                  if cell["status"] != "pass"}
        self.assertEqual(failed, {}, json.dumps(self.section)[-3000:])
        self.assertEqual((self.section["status"], self.section["cli"], self.section["model"]),
                         ("pass", "codex-cli 0.162.0", "default"))
        self.assertEqual({cell["id"]: cell["checks"] for cell in self.section["cells"]}, matrix.AGENT_CHECKS)
        self.assertEqual({cell["id"]: cell["advisory_failed"] for cell in self.section["cells"]},
                         {cell: [] for cell in matrix.AGENT_IDS})
        matrix.check_agent_section("codex", self.section)

    def test_the_private_homes_hold_only_what_the_agent_needs_and_are_deleted(self):
        auth = (self.codex_home / "auth.json").read_bytes()
        for cell in matrix.AGENT_IDS:
            with self.subTest(cell=cell):
                seen = isolation(self.evidence, "codex", cell)
                self.assertEqual(seen["config"], ["auth.json", "config.toml"])
                self.assertEqual((seen["home"], seen["cwd"]), ([], []))
                self.assertEqual(seen["servers"], [matrix.agent_server(matrix.agent_cell(cell)[1], SERVER)])
                self.assertEqual(seen["auth_sha256"], matrix._sha(auth))
                self.assertLessEqual(set(seen["environment"]) - SHELL_VARIABLES,
                                     {"HOME", "PATH", "TMPDIR", "LANG", "CODEX_HOME"})
        self.assertFalse((self.base / "scratch-codex-honest").exists())
        self.assertEqual(sorted(path.name for path in self.codex_home.iterdir()), ["auth.json"])
        for folder in (self.evidence / "codex").iterdir():
            self.assertEqual(sorted(path.name for path in folder.iterdir()), ["attempt-1"])
            for directory in (folder, folder / "attempt-1"):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for name in matrix.AGENT_EVIDENCE:
                self.assertEqual(stat.S_IMODE((folder / "attempt-1" / name).stat().st_mode), 0o600)

    def test_each_verdict_is_the_evidence_judged_again(self):
        for cell in self.section["cells"]:
            with self.subTest(cell=cell["id"]):
                files = self.files(cell["id"])
                self.assertEqual(matrix.evidence_digests(files), cell["evidence"])
                judged = self.judged(cell["id"], files)
                self.assertEqual({key: judged[key] for key in ("id", "status", "checks", "advisory_failed")},
                                 {key: cell[key] for key in ("id", "status", "checks", "advisory_failed")})
                run = json.loads(files["run.json"])
                self.assertEqual({key: run[key] for key in ("commit", "wheel_sha256", "version", "schema_fingerprint",
                                                            "server_name", "column", "cell", "cli", "model", "exit",
                                                            "timed_out")},
                                 {**bindings_for(self.census), "column": "codex", "cell": cell["id"],
                                  "cli": "codex-cli 0.162.0", "model": "default", "exit": 0, "timed_out": False})

    def mutated(self, cell: str, *, server=None, events=None, answer=None, run=None) -> dict:
        files = dict(self.files(cell))
        entries = [json.loads(line) for line in files["server.jsonl"].decode().splitlines()]
        if server is not None:
            entries = server(entries)
            files["server.jsonl"] = b"".join(json.dumps(entry).encode() + b"\n" for entry in entries)
        if events is not None:
            files["events.jsonl"] = events(files["events.jsonl"].decode()).encode()
        if answer is not None:
            files["answer.txt"] = answer.encode()
        if run is not None:
            files["run.json"] = json.dumps({**json.loads(files["run.json"]), **run}).encode()
        return self.judged(cell, files)

    def test_each_decisive_check_fails_on_its_own_misbehaviour(self):
        def header(entries):
            return [{"broker": {**entry["broker"], "version": "0.0.0"}} if "broker" in entry else entry
                    for entry in entries]

        def dispatch(entries):
            return entries + [{"request": "sendMessage"}]

        def second_dialog(entries):
            return entries + [dict(next(entry for entry in entries if "prompt" in entry))]

        def drafted(entries):
            return entries + [{"operation": "prepare_text_send", "connection": 99, "payload": {"recipient": 91002},
                               "result": {"status": "prepared", "draft_id": "draft_" + "1" * 32}}]

        def without(operation):
            return lambda entries: [entry for entry in entries if entry.get("operation") != operation]

        def no_diagnostic(entries):
            return [entry for entry in entries if not ("frame" in entry and entry.get("connection") == 1)]

        def unfollowed(entries):
            return [{**entry, "payload": {**entry["payload"], "cursor": None}} if entry.get("operation")
                    == "search_messages" else entry for entry in entries]

        def foreign(events):
            return events + json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "other",
                                                                           "tool": "x"}}) + "\n"

        def failed_turn(events):
            return events + json.dumps({"type": "turn.failed", "error": {"message": "x"}}) + "\n"

        cases = [("agent.census", {"run": {"exit": 1}}, "agent_exited"),
                 ("agent.census", {"run": {"timed_out": True}}, "agent_exited"),
                 ("agent.census", {"events": failed_turn}, "turn_completed"),
                 ("agent.read", {"events": foreign}, "only_matrix_tools"),
                 ("agent.read", {"server": header}, "this_wheel"),
                 ("agent.read", {"answer": " \n"}, "answer_present"),
                 ("agent.census", {"server": no_diagnostic}, "broker_checked"),
                 ("agent.census", {"server": drafted}, "nothing_changed"),
                 ("agent.read", {"server": without("read_history")}, "read_newest"),
                 ("agent.read", {"server": dispatch}, "nothing_changed"),
                 ("agent.search_cursor", {"server": unfollowed}, "cursor_followed"),
                 ("agent.refusal", {"server": without("prepare_reply_text_send")}, "refused"),
                 ("agent.refusal", {"server": drafted}, "no_workaround"),
                 ("agent.approval_timeout", {"server": second_dialog}, "one_dialog"),
                 ("agent.approval_timeout", {"server": dispatch}, "no_dispatch"),
                 ("agent.alias_server", {"server": lambda entries: entries[:-1]}, "alias_served"),
                 ("agent.album_prepare", {"server": without("prepare_album_send")}, "album_prepared"),
                 ("agent.album_prepare", {"server": dispatch}, "not_sent"),
                 ("agent.own_message_prepare", {"server": without("prepare_text_edit")}, "edit_prepared"),
                 ("agent.family_reply", {"server": without("prepare_reply_text_send")}, "reply_prepared"),
                 ("agent.family_reply", {"server": second_dialog if False else dispatch}, "not_sent")]
        for cell, change, label in cases:
            with self.subTest(cell=cell, check=label):
                judged = self.mutated(cell, **change)
                self.assertEqual((judged["status"], judged.get("failed_check"), judged["checks"]),
                                 ("fail", label, matrix.AGENT_CHECKS[cell]))

    def test_the_answers_wording_is_advisory(self):
        for cell in matrix.AGENT_IDS:
            with self.subTest(cell=cell):
                judged = self.mutated(cell, answer="Done.")
                self.assertEqual((judged["status"], judged["advisory_failed"]),
                                 ("pass", sorted(matrix.AGENT_ADVISORY[cell])))

    def test_the_receipt_binds_the_column_and_check_judges_the_kept_evidence_again(self):
        agents = {"codex": self.section, "claude_code": {"status": "unavailable", "reason": "not_logged_in"}}
        document = receipt(self.census, agents=agents)
        matrix.check_receipt(document, commit=COMMIT, wheel_sha256=WHEEL, census=self.census)
        matrix.verify_agent_evidence(document, self.evidence, SERVER)
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, None, SERVER), "evidence_missing")
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, self.evidence, "renamed_server"),
                         "evidence_mismatch")  # another commit's server name
        matrix.verify_agent_evidence(receipt(self.census), None, SERVER)  # nothing passed, nothing to verify
        read = matrix.AGENT_IDS.index("agent.read")

        def rewritten(name: str, change) -> tuple:
            """A copy of the evidence with one file of agent.read changed, and a receipt carrying its new digest."""
            copy_dir = self.base / f"tampered-{name}-{len(list(self.base.iterdir()))}"
            shutil.copytree(self.evidence, copy_dir)
            path = copy_dir / "codex" / "agent.read" / "attempt-1" / name
            path.write_bytes(change(path.read_bytes()))
            changed = copy.deepcopy(document)
            changed["agents"]["codex"]["cells"][read]["evidence"] = matrix.evidence_digests(
                matrix.read_evidence(copy_dir / "codex" / "agent.read" / "attempt-1"))
            return copy_dir, changed

        def no_read(data: bytes) -> bytes:
            return b"".join(line + b"\n" for line in data.splitlines() if b'"operation": "read_history"' not in line)

        stale, _ = rewritten("server.jsonl", no_read)
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, stale, SERVER), "evidence_mismatch")
        for name, change in (("server.jsonl", no_read),
                             ("run.json", lambda data: data.replace(COMMIT.encode(), OTHER.encode())),
                             ("run.json", lambda data: data.replace(b"codex-cli 0.162.0", b"codex-cli 0.0.1")),
                             ("events.jsonl", lambda data: data.replace(b'"turn.completed"', b'"turn.failed"'))):
            with self.subTest(file=name):
                directory, changed = rewritten(name, change)
                self.assertEqual(refusal(matrix.verify_agent_evidence, changed, directory, SERVER),
                                 "evidence_mismatch")
        missing, _ = rewritten("answer.txt", lambda data: data)
        (missing / "codex" / "agent.read" / "attempt-1" / "answer.txt").unlink()
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, missing, SERVER), "evidence_invalid")
        linked, _ = rewritten("answer.txt", lambda data: data)
        target = linked / "codex" / "agent.read" / "attempt-1" / "answer.txt"
        target.unlink()
        target.symlink_to(self.evidence / "codex" / "agent.read" / "attempt-1" / "answer.txt")
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, linked, SERVER), "evidence_invalid")

    def test_the_check_command_requires_the_evidence_of_passed_agent_cells(self):
        document = receipt(self.census, agents={"codex": self.section,
                                                "claude_code": {"status": "unavailable", "reason": "not_logged_in"}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            matrix.write_receipt(path, document)
            wheel = Path(directory) / "synthetic.whl"
            wheel.write_bytes(b"synthetic wheel")
            fake = SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: Path(directory),
                                   Git=lambda repo: object(), resolve_commit=lambda git, value: COMMIT,
                                   sha256_file=lambda file: WHEEL, matrix_loaded_sha256=GATE)
            for extra, expected in (([], (2, "evidence_missing")),
                                    (["--agent-evidence", "relative"], (2, "evidence_invalid")),
                                    (["--agent-evidence", str(self.evidence)], (0, ""))):
                with self.subTest(extra=extra), mock.patch.object(matrix, "load_gate", return_value=fake), \
                        mock.patch.object(matrix, "build_wheel", return_value={"tree": Path(directory), "wheel": wheel}), \
                        mock.patch.object(matrix, "committed_facts", return_value=FACTS), \
                        mock.patch.object(matrix, "harness_digest", return_value=FACTS["harness_sha256"]), \
                        mock.patch.object(matrix, "source_census", return_value=self.census), \
                        mock.patch.object(matrix, "identity_server_name", return_value=SERVER), \
                        mock.patch.object(matrix, "installed_runtime", return_value=(object(), DERIVED["install"])), \
                        mock.patch.object(matrix, "observed_listing", return_value=DERIVED["listing_sha256"]), \
                        contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    code = matrix.main(["check", "--receipt", str(path), "--repo", directory, *extra])
                self.assertEqual((code, err.getvalue().strip()), expected)
                if code == 0:
                    self.assertIn(f"Codex {len(matrix.AGENT_IDS)}/{len(matrix.AGENT_IDS)} cells", out.getvalue())
                    self.assertTrue(out.getvalue().startswith("consumer matrix receipt verified: "))
                    self.assertTrue(out.getvalue().endswith(f" owner cells pending (commit {COMMIT}, wheel sha256 "
                                                            f"{WHEEL})\n"), out.getvalue())


class InterruptTests(unittest.TestCase):
    def test_an_interrupt_waits_for_a_child_handover_and_a_repeat_never_cuts_the_cleanup_short(self):
        previous = signal.getsignal(signal.SIGTERM)
        events = []
        with self.assertRaises(matrix.MatrixInterrupted) as caught:
            with matrix.interrupt_handlers():
                with matrix.deferred_interrupts():
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.05)
                    events.append("handed over")
                events.append("not reached")
        self.assertEqual((caught.exception.signum, events), (signal.SIGTERM, ["handed over"]))
        with self.assertRaises(matrix.MatrixInterrupted) as caught:
            with matrix.interrupt_handlers():
                try:
                    os.kill(os.getpid(), signal.SIGHUP)
                    time.sleep(0.05)
                finally:
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.05)
                    events.append("cleaned up")
        self.assertEqual((caught.exception.signum, events[-1]), (signal.SIGHUP, "cleaned up"))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_main_answers_an_interrupt_with_a_fixed_line_and_the_shells_status(self):
        for command in ("run", "check"):
            with self.subTest(command=command), \
                    mock.patch.object(matrix, command, side_effect=matrix.MatrixInterrupted(signal.SIGHUP)), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                arguments = ["--commit", COMMIT] if command == "run" else []
                code = matrix.main([command, "--receipt", "/r.json", *arguments])
            self.assertEqual((code, err.getvalue()), (128 + signal.SIGHUP, "interrupted\n"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_quietly(*pids: int) -> None:
    for pid in pids:
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class AgentBehaviourTests(unittest.TestCase):
    """Misbehaving or unavailable agents, one or two cells each."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tgmcp-agent-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_a_workaround_after_the_refusal_fails_the_cell(self):
        section, _, _ = run_column(self.base, "codex", "workaround", cells=("agent.refusal",))
        self.assertEqual((section["status"], section["cells"][0]["failed_check"]), ("fail", "no_workaround"))

    def test_a_shell_command_fails_the_cell(self):
        section, _, _ = run_column(self.base, "codex", "shell", cells=("agent.read",))
        self.assertEqual((section["status"], section["cells"][0]["failed_check"]), ("fail", "only_matrix_tools"))

    def test_a_missing_login_makes_the_column_unavailable_after_its_first_cell(self):
        for column in ("codex", "claude_code"):
            with self.subTest(column=column):
                section, evidence, _ = run_column(self.base, column, "not_logged_in",
                                                  cells=("agent.census", "agent.read"))
                self.assertEqual(section, {"status": "unavailable", "reason": "not_logged_in"})
                self.assertEqual(sorted(path.name for path in (evidence / column).iterdir()), ["agent.census"])

    def test_a_login_failure_after_the_first_cell_fails_the_column_and_keeps_every_result(self):
        expected = {"codex": [("agent.census", "fail", "only_matrix_tools"), ("agent.read", "fail", "agent_exited")],
                    "claude_code": [("agent.census", "pass", None), ("agent.read", "fail", "agent_exited")]}
        for column, cells in expected.items():
            with self.subTest(column=column), contextlib.redirect_stderr(io.StringIO()) as err:
                section, evidence, _ = run_column(self.base, column, "logout_after_census",
                                                  cells=("agent.census", "agent.read"))
                self.assertEqual(section["status"], "fail")
                self.assertEqual([(cell["id"], cell["status"], cell.get("failed_check")) for cell in section["cells"]],
                                 cells)
                self.assertEqual(sorted(path.name for path in (evidence / column).iterdir()),
                                 ["agent.census", "agent.read"])
                self.assertEqual(err.getvalue().strip(), f"{column}: login failed after the column's first cell")
                self.assertEqual(refusal(matrix.check_agent_section, column, section), "agent_invalid")

    def test_a_provider_failure_before_any_tool_is_retried_and_every_attempt_is_kept(self):
        section, evidence, _ = run_column(self.base, "codex", "transient", cells=("agent.census",))
        self.assertEqual((section["status"], section["cells"][0]["attempts"]), ("pass", 2))
        folder = evidence / "codex" / "agent.census"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["attempt-1", "attempt-2"])
        first, second = (matrix.read_evidence(folder / f"attempt-{n}") for n in (1, 2))
        self.assertEqual([json.loads(files["run.json"])["attempt"] for files in (first, second)], [1, 2])
        self.assertIn(b"at capacity", first["events.jsonl"])
        self.assertFalse(matrix.Transcript(matrix._jsonl(first["server.jsonl"])).reached())
        self.assertEqual(section["cells"][0]["evidence"], matrix.evidence_digests(second))
        census = live_census()
        document = {"commit": COMMIT, "wheel": {"sha256": WHEEL}, "version": census["version"],
                    "schema_fingerprint": census["schema_fingerprint"],
                    "agents": {"codex": section, "claude_code": {"status": "unavailable", "reason": "cli_not_given"}}}
        matrix.verify_agent_evidence(document, evidence, SERVER)
        claimed = copy.deepcopy(document)
        claimed["agents"]["codex"]["cells"][0]["attempts"] = 1
        self.assertEqual(refusal(matrix.verify_agent_evidence, claimed, evidence, SERVER), "evidence_mismatch")
        shutil.rmtree(folder / "attempt-1")
        self.assertEqual(refusal(matrix.verify_agent_evidence, document, evidence, SERVER), "evidence_invalid")

    def test_markers_in_the_models_own_words_are_not_provider_failures(self):
        for column in ("codex", "claude_code"):
            with self.subTest(column=column):
                section, evidence, _ = run_column(self.base, column, "prose", cells=("agent.census",))
                self.assertEqual((section["status"], section["cells"][0]["failed_check"],
                                  section["cells"][0]["attempts"]), ("fail", "broker_checked", 1))
                self.assertEqual(sorted(path.name for path in (evidence / column / "agent.census").iterdir()),
                                 ["attempt-1"])
        self.assertEqual(matrix.provider_errors("codex", json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "rate limit"}}) + "\n", ""), "")
        self.assertIn("401 unauthorized", matrix.provider_errors("codex", json.dumps(
            {"type": "turn.failed", "error": {"message": "401 Unauthorized"}}) + "\n", ""))
        self.assertIn("overloaded", matrix.provider_errors("claude_code", "", "API Error: Overloaded"))

    def test_refreshed_codex_credentials_return_to_the_callers_codex_home(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            section, _, codex_home = run_column(self.base, "codex", "refresh", cells=("agent.census",),
                                                auth=CODEX_AUTH)
        self.assertEqual(section["status"], "pass")
        returned = json.loads((codex_home / "auth.json").read_text())
        self.assertEqual(returned["tokens"]["refresh_token"], "synthetic-r-refreshed")
        self.assertEqual(err.getvalue().strip(), "codex credentials refreshed during the run: returned")

    def test_sigterm_or_sighup_during_an_agent_cell_leaves_nothing_behind(self):
        """A real harness process is interrupted while a real child agent (which would sleep far past the test)
        runs: the agent and the cell's broker are gone, the temporary homes and the copied credential are removed
        and the refreshed credential went back."""
        script = self.base / "interrupted.py"
        script.write_text(textwrap.dedent(f"""\
            import contextlib, importlib.util, sys
            from pathlib import Path
            spec = importlib.util.spec_from_file_location("matrix_tests", {str(Path(__file__).resolve())!r})
            tests = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tests)
            matrix, base, report = tests.matrix, Path(sys.argv[1]), Path(sys.argv[2])
            original = matrix.start_broker
            def recording(*args, **kwargs):
                broker = original(*args, **kwargs)
                (base / "broker.pid").write_text(str(broker[0].pid))
                return broker
            matrix.start_broker = recording
            binary = tests.fake_cli(base, "codex", "hang", report=report)
            try:
                with getattr(matrix, "interrupt_handlers", contextlib.nullcontext)():
                    tests.run_column(base, "codex", "hang", cells=("agent.census",), auth=tests.CODEX_AUTH,
                                     binary=binary)
            except getattr(matrix, "MatrixInterrupted", ()) as interrupted:
                sys.exit(128 + interrupted.signum)
            """))
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                base = self.base / number.name
                base.mkdir()
                report = base / "agent.json"
                env = {**os.environ, "PYTHONPATH": str(SOURCE)}
                with open(base / "child.log", "wb") as log:
                    child = subprocess.Popen([sys.executable, str(script), str(base), str(report)], env=env,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
                deadline = time.monotonic() + 90
                while not report.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(report.exists(), (base / "child.log").read_text()[-2000:])
                agent = json.loads(report.read_text())
                broker = int((base / "broker.pid").read_text())
                self.addCleanup(_kill_quietly, agent["pid"], broker)
                child.send_signal(number)
                code = child.wait(timeout=60)
                time.sleep(0.5)
                self.assertEqual([_alive(agent["pid"]), _alive(broker), os.path.lexists(agent["broker_home"]),
                                  (base / "scratch-codex-hang").exists()], [False, False, False, False])
                self.assertEqual(code, 128 + number, (base / "child.log").read_text()[-2000:])
                returned = json.loads((base / "codex-home" / "auth.json").read_text())
                self.assertEqual(returned["tokens"]["refresh_token"], "synthetic-r-refreshed")
                self.assertEqual(sorted(path.name for path in (base / "codex-home").iterdir()), ["auth.json"])

    def test_each_unavailable_reason_is_fixed(self):
        cases = (("cli_unusable", {"binary": fake_cli(self.base, "codex", "bad_version")}),
                 ("cli_unusable", {"binary": self.base / "missing-codex"}),
                 ("auth_missing", {"auth": None}))
        for reason, kwargs in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory(dir=self.base) as directory:
                section, _, _ = run_column(Path(directory), "codex", cells=("agent.census",), **kwargs)
                self.assertEqual(section, {"status": "unavailable", "reason": reason})
        census = live_census()
        self.assertEqual(matrix.run_agent_column("codex", None, None, source_runtime(), census, self.base / "none",
                                                 self.base, bindings_for(census)),
                         {"status": "unavailable", "reason": "cli_not_given"})

    @staticmethod
    def caller_home(base: Path) -> Path:
        """A synthetic caller home with a Claude Code configuration, as claude_login_home answers it."""
        home = base / "caller-home"
        (home / ".claude" / "projects" / "-some-project").mkdir(parents=True)
        (home / ".claude.json").write_text('{"synthetic": "global configuration"}\n')
        (home / ".claude" / "settings.json").write_text('{"enabledPlugins": {"synthetic@market": true}}\n')
        return home

    def test_claude_code_logs_in_through_the_default_configuration_and_loads_nothing_else_of_it(self):
        home = self.caller_home(self.base)
        before = matrix.claude_configuration(home)
        with mock.patch.object(matrix, "claude_login_home", return_value=home):
            section, evidence, _ = run_column(self.base, "claude_code", cells=("agent.census", "agent.alias_server"))
        self.assertEqual([cell["status"] for cell in section["cells"]], ["pass", "pass"])
        self.assertEqual(section["cli"], "2.1.304 (Claude Code)")
        seen = isolation(evidence, "claude_code", "agent.alias_server")
        self.assertEqual((seen["config"], seen["home_path"], seen["cwd"], seen["auth_sha256"]),
                         (None, str(home), [], None))
        self.assertEqual(seen["servers"], [f"{SERVER}_{matrix.ALIAS}"])
        argv = seen["argv"]
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"autoMemoryEnabled": False})
        self.assertEqual(argv[argv.index("--allowedTools") + 1], f"mcp__{SERVER}_{matrix.ALIAS}")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertLessEqual(set(seen["environment"]) - SHELL_VARIABLES,
                             {"HOME", "USER", "PATH", "TMPDIR", "LANG", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                              "DISABLE_AUTOUPDATER"})
        self.assertEqual(matrix.claude_configuration(home), before)

    def test_a_claude_code_run_that_changes_the_callers_configuration_is_refused(self):
        for behaviour in ("touch_config", "new_project"):
            with self.subTest(behaviour=behaviour), tempfile.TemporaryDirectory(dir=self.base) as directory:
                base = Path(directory)
                with mock.patch.object(matrix, "claude_login_home", return_value=self.caller_home(base)):
                    self.assertEqual(refusal(run_column, base, "claude_code", behaviour, cells=("agent.census",)),
                                     "client_config_changed")
                self.assertFalse((base / f"scratch-claude_code-{behaviour}").exists())
        snapshot = matrix.claude_configuration(self.base / "nowhere")
        self.assertEqual(snapshot, {".claude.json": None, ".claude/settings.json": None,
                                    ".claude/settings.local.json": None, "projects": None})


if __name__ == "__main__":
    unittest.main()
