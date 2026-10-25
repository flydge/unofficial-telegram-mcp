"""The consumer-matrix harness (0.54 item E, criterion 11; final acceptance F18): cell set, receipt rules,
fixtures, schema binding, the Inspector column and the bundle session.

The installed run (wheel from `git archive`, pinned closure, every session, the Inspector CLI) is recorded in
the final acceptance tasks file; here the same synthetic sessions run once from this source tree, and a patched
registration proves that a schema change fails both run and the receipt check.
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
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import telegram_search_mcp

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


def receipt(census: dict, *, inspector: dict | None = None, **kwargs) -> dict:
    return matrix.build_receipt(
        commit=COMMIT, tree=OTHER, version=census["version"],
        wheel={"file": f"telegram_search_mcp-{census['version']}-py3-none-any.whl", "sha256": WHEEL, "bytes": 1},
        build={"build": "1.3.0", "python": "3.14.6", "setuptools": "80.9.0", "source_date_epoch": FACTS["epoch"]},
        install=dict(INSTALLED), installer={**INSTALLER, "toolhouse": "provided"}, census=census,
        sessions=sessions(tools=census["tools"], **kwargs),
        inspector=copy.deepcopy(INSPECTOR_PASS if inspector is None else inspector),
        harness_sha256=FACTS["harness_sha256"], gate_sha256=GATE)


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
                          "caps", "approval", "bundle"})
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
        self.assertEqual(self.receipt["schema"], "tgmcp-consumer-matrix/v3")
        self.assertEqual([cell["id"] for cell in self.receipt["cells"]], list(matrix.CELL_IDS))
        self.assertEqual({cell["status"] for cell in self.receipt["owner_cells"]}, {"pending"})
        self.assertEqual(self.receipt["summary"], {"synthetic": len(matrix.CELL_IDS), "passed": len(matrix.CELL_IDS),
                                                   "failed": 0, "inspector": "pass",
                                                   "inspector_cells": len(matrix.INSPECTOR_IDS),
                                                   "owner_pending": len(matrix.OWNER_IDS)})
        self.assertEqual(self.receipt["census"]["tools_sha256"], matrix.tools_sha256(self.census["tools"]))
        self.assertEqual(set(self.receipt), matrix.RECEIPT_KEYS)
        self.assertEqual(matrix.summary_line(self.receipt),
                         f"consumer matrix passed: {len(matrix.CELL_IDS)}/{len(matrix.CELL_IDS)} synthetic cells; "
                         f"Inspector 4/4 cells; {len(matrix.OWNER_IDS)} owner cells pending")

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
            # A receipt taken over the changed schema fails on its own: the cells were written for another one.
            self.assertEqual(refusal(matrix.check_receipt, receipt(bound)), "schema_mismatch")
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
            with contextlib.redirect_stderr(io.StringIO()) as err:  # The fake npm fails the online fetch.
                self.assertEqual(matrix.main(["inspector-fetch", "--node", str(node), "--cache",
                                              str(Path(directory) / "cache"), "--repo", str(ROOT)]), 2)
            self.assertEqual(err.getvalue().strip(), "fetch_failed")

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


if __name__ == "__main__":
    unittest.main()
