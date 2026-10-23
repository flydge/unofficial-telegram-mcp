"""The consumer-matrix harness (0.54 item E, criterion 11): cell set, receipt rules, fixtures, schema binding.

The installed run (wheel from `git archive`, pinned closure, five sessions) is recorded in the 0.54 tasks
file; here the same sessions run once from this source tree, and a patched registration proves that a
schema change fails the receipt check.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
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
VERSION = telegram_search_mcp.__version__

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


def sessions(*, failing: str | None = None, validated: int | None = None, failures: tuple = (),
             approval: str = "scripted") -> list[dict]:
    """Session results as a passing run reports them: each cell's frozen check count, the frozen surface."""
    results, extra = [], {"approval.denial": 1} if approval == "real_dialog" else {}
    surface = matrix.SURFACE_COUNTS
    for name in matrix.SESSIONS:
        cells = [{"id": cell, "status": "fail" if cell == failing else "pass",
                  "checks": matrix.CELL_CHECKS[cell] + extra.get(cell, 0),
                  **({"failed_check": "synthetic"} if cell == failing else {})}
                 for cell, session, _ in matrix.CELLS if session == name]
        full = name == "full"
        count = surface["structured_results_validated"] - (len(matrix.SESSIONS) - 1) if full else 1
        tools = [f"tool_{index}" for index in range(surface["tools_with_validated_results"])] if full else ["tool_0"]
        results.append({"session": name, "cells": cells,
                        "observed": {"listing_sha256": "d" * 64,
                                     "approval": {"mode": approval,
                                                  "outcome": "timeout" if approval == "real_dialog" else "declined"}},
                        "schemas": {"validated": count if validated is None else validated, "failures": list(failures),
                                    "tools": tools, "unschematized": []},
                        "sdk": {"distribution": "mcp", "version": "2.2.0"}})
    return results


# What the recorded commit fixes (committed_facts) and what check re-derives; receipt() agrees with both.
FACTS = {"harness_sha256": "e" * 64, "gate_sha256": GATE, "tree_id": OTHER, "epoch": 1700000000,
         "constraints": "packaging/constraints-0.0.0.txt", "constraints_sha256": "f" * 64, "distributions": 2,
         "freeze": ["mcp==2.2.0", "pydantic==2.0", f"telegram-search-mcp=={VERSION}"], "sdk_version": "2.2.0",
         "wheel_bytes": 1, "python_target": "3.14", "build_tools": {"build": "1.3.0", "setuptools": "80.9.0"}}
INSTALLED = {"constraints": FACTS["constraints"], "constraints_sha256": FACTS["constraints_sha256"],
             "wheelhouse": "provided", "distributions": 2, "freeze": list(FACTS["freeze"]), "pip_check": "clean",
             "record_entries": 10}
DERIVED = {"install": {**INSTALLED, "wheelhouse": "downloaded"}, "listing_sha256": "d" * 64}


def receipt(census: dict, **kwargs) -> dict:
    return matrix.build_receipt(
        commit=COMMIT, tree=OTHER, version=census["version"],
        wheel={"file": f"telegram_search_mcp-{census['version']}-py3-none-any.whl", "sha256": WHEEL, "bytes": 1},
        build={"build": "1.3.0", "python": "3.14.6", "setuptools": "80.9.0", "source_date_epoch": FACTS["epoch"]},
        install=dict(INSTALLED), census=census, sessions=sessions(**kwargs), harness_sha256=FACTS["harness_sha256"],
        gate_sha256=GATE)


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
        self.assertEqual(len(matrix.OWNER_IDS), len(set(matrix.OWNER_IDS)))
        self.assertEqual({session for _, session, _ in matrix.CELLS}, set(matrix.SESSIONS) | {"all"})
        self.assertEqual({cell.split(".")[0] for cell in matrix.CELL_IDS},
                         {"handshake", "census", "output_schemas", "refusal", "image", "cursor", "recovery", "caps",
                          "approval"})
        self.assertEqual([cell for cell in matrix.CELL_IDS if cell.startswith("handshake.")],
                         [f"handshake.{revision}" for revision in matrix.REVISIONS])

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
        self.assertEqual(set(matrix.SURFACE_COUNTS), {"structured_results_validated", "tools_with_validated_results"})

    def test_the_driver_runs_every_cell_of_its_session(self):
        driver = matrix.Driver
        full = set(driver.FULL_CELLS) | {f"handshake.{r}" for r in matrix.REVISIONS} | {
            "refusal.client_schema_mismatch", "recovery.restart_listing"}
        self.assertEqual(full, {cell for cell, session, _ in matrix.CELLS if session == "full"})
        for name in driver.FULL_CELLS:
            self.assertTrue(callable(getattr(driver, "cell_" + name.replace(".", "_"), None)), name)
        for name in matrix.SESSIONS:
            self.assertTrue(callable(getattr(driver, name, None)), name)


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
        self.assertEqual([cell["id"] for cell in self.receipt["cells"]], list(matrix.CELL_IDS))
        self.assertEqual({cell["status"] for cell in self.receipt["owner_cells"]}, {"pending"})
        self.assertEqual(self.receipt["summary"], {"synthetic": len(matrix.CELL_IDS), "passed": len(matrix.CELL_IDS),
                                                   "failed": 0, "owner_pending": len(matrix.OWNER_IDS)})
        self.assertEqual(self.receipt["census"]["tools_sha256"], matrix.tools_sha256(self.census["tools"]))
        self.assertEqual(set(self.receipt), matrix.RECEIPT_KEYS)

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

    def test_output_schema_cell_needs_validated_results_and_no_failure(self):
        self.assertEqual(matrix.merge_cells(sessions())[matrix.CELL_IDS.index("output_schemas")]["status"], "pass")
        for kwargs in ({"validated": 0}, {"failures": ("list_chats:invalid",)}):
            with self.subTest(kwargs=kwargs):
                cell = matrix.merge_cells(sessions(**kwargs))[matrix.CELL_IDS.index("output_schemas")]
                self.assertEqual(cell["status"], "fail")

    def test_merge_refuses_a_missing_repeated_or_unknown_cell(self):
        for mutate in (lambda s: s[0]["cells"].pop(), lambda s: s[1]["cells"].append(dict(s[0]["cells"][0])),
                       lambda s: s[0]["cells"].append({"id": "matrix.unknown", "status": "pass", "checks": 1})):
            value = sessions()
            mutate(value)
            with self.subTest(mutate=mutate):
                self.assertEqual(refusal(matrix.merge_cells, value), "cell_set_mismatch")

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


class SchemaChangeTests(unittest.TestCase):
    """Criterion 11: a deliberate schema change (a patched registration) fails the receipt check."""

    def census_of(self, directory: Path, patch: str | None) -> dict:
        tree = directory / (patch or "unpatched") / "src"
        shutil.copytree(SOURCE / "telegram_search_mcp", tree / "telegram_search_mcp",
                        ignore=shutil.ignore_patterns("__pycache__"))
        if patch is not None:
            with open(tree / "telegram_search_mcp" / "server.py", "a", encoding="utf-8") as stream:
                stream.write(PATCHES[patch])
        return matrix.source_census(gate, Path(sys.executable), tree, directory)

    def test_patched_registration_changes_the_census_and_fails_the_receipt_check(self):
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
            cases = (({}, None),
                     ({"wheel": "0" * 64}, "wheel_mismatch"),
                     ({"census": {**census, "schema_fingerprint": "f" * 64}}, "schema_mismatch"),
                     ({"installed": {**DERIVED["install"], "record_entries": 11}}, "field_mismatch"),
                     ({"installed": {**DERIVED["install"], "freeze": ["mcp==2.2.0", "pydantic==2.1",
                                                                       f"telegram-search-mcp=={VERSION}"]}},
                      "field_mismatch"),
                     ({"listing": "0" * 64}, "field_mismatch"),
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
                        mock.patch.object(matrix, "committed_facts", return_value=FACTS), \
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
                    self.assertEqual(out.getvalue().strip(), "consumer matrix receipt verified")
                    installed.assert_called_once()
                    self.assertIs(listed.call_args.args[0], runtime)
                if expected in ("harness_mismatch", "gate_mismatch"):
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

    def test_each_edited_field_refuses(self):
        """Every field the check_receipt and check_fields docstrings name refuses when it is edited."""
        cells = {cell: index for index, cell in enumerate(matrix.CELL_IDS)}
        other_closure = ["mcp==2.2.0", "evil==6.6.6", f"telegram-search-mcp=={VERSION}"]
        cases = [
            ("schema", edit("schema", "tgmcp-consumer-matrix/v1"), "receipt_invalid"),
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
            ("owner_cells.status", edit("owner_cells.0.status", "pass"), "owner_cell_passed"),
            ("owner_cells.extra", edit("owner_cells.0.evidence", "owner saw it pass"), "field_mismatch"),
            ("owner_cells.client", edit("owner_cells.0.client", "someone"), "field_mismatch"),
            ("owner_cells.provider", drop("owner_cells.0", "provider"), "field_mismatch"),
            ("summary", edit("summary.passed", 1), "receipt_invalid"),
        ]
        for cell, index in cells.items():  # every cell's frozen count, lowered by one
            cases.append((f"cells.{cell}", edit(f"cells.{index}.checks", matrix.CELL_CHECKS[cell] - 1),
                          "checks_mismatch"))
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

    def run_command(self, fake, directory, **patches):
        with mock.patch.object(matrix, "load_gate", return_value=fake), \
                mock.patch.object(matrix, "build_wheel", return_value={"tree": Path(directory), "version": "0.0.0"}), \
                mock.patch.object(matrix, "committed_facts", return_value=FACTS), \
                mock.patch.object(matrix, "installed_runtime", side_effect=AssertionError("installed")), \
                contextlib.ExitStack() as stack, contextlib.redirect_stderr(io.StringIO()) as err:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(matrix, name, return_value=value))
            code = matrix.main(["run", "--commit", COMMIT, "--receipt", str(Path(directory) / "r.json"), "--repo",
                                directory, "--workdir", str(Path(directory) / "work")])
        return code, err.getvalue().strip()

    def test_run_refuses_a_working_tree_harness_or_gate_that_differs_from_the_commits(self):
        fake = SimpleNamespace(GateRefusal=gate.GateRefusal, toplevel=lambda repo: Path("/"), Git=lambda repo: object(),
                               resolve_commit=lambda git, value: COMMIT, sha256_file=lambda file: "1" * 64,
                               matrix_loaded_sha256=GATE)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_command(fake, directory), (2, "harness_mismatch"))
        fake.matrix_loaded_sha256 = "2" * 64
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_command(fake, directory, harness_digest=FACTS["harness_sha256"]),
                             (2, "gate_mismatch"))

    def test_the_gate_is_hashed_from_the_bytes_it_runs(self):
        loaded = matrix.load_gate()
        source = (matrix.HARNESS.parent / "release_gate.py").read_bytes()
        self.assertEqual(loaded.matrix_loaded_sha256, matrix._sha(source))
        self.assertEqual(loaded.GATE_PATH, matrix.GATE_PATH)
        self.assertEqual(matrix.harness_digest(), matrix._sha(matrix.HARNESS.read_bytes()))


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


class ScriptedOwnerTests(unittest.TestCase):
    def prompt(self, *, real_dialog=False, rendered=("Send text\n│ body", False), decision=None):
        from telegram_search_mcp import approval_prompt
        log = []
        dialog = mock.Mock(return_value=decision or approval_prompt.TIMEOUT)
        patches = (mock.patch.object(approval_prompt, "render_approval", return_value=rendered),
                   mock.patch.object(approval_prompt, "decide_approved_send", dialog))
        return log, dialog, patches, matrix.approval_prompt(log.append, real_dialog=real_dialog)

    def test_scripted_owner_declines_denials_and_approves_only_the_outcome_probe(self):
        from telegram_search_mcp import approval_prompt
        log, dialog, patches, prompt = self.prompt()
        with patches[0], patches[1]:
            self.assertIs(prompt(text=matrix.DENIAL_PROBE, recipient=1), approval_prompt.DECLINED)
            self.assertIs(prompt(text=matrix.OUTCOME_PROBE, recipient=1), approval_prompt.APPROVED)
            self.assertIs(prompt(text="anything else", recipient=1), approval_prompt.DECLINED)
        dialog.assert_not_called()
        self.assertEqual([(entry["prompt"], entry["outcome"], entry["rows"]) for entry in log],
                         [("denial", "declined", 2), ("outcome", "approved", 2), ("other", "declined", 2)])

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
        self.assertEqual(log, [{"prompt": "outcome", "rendered": False, "rows": 0, "outcome": "dialog_unavailable"}])


class LayoutTests(unittest.TestCase):
    def test_session_policies_load_as_the_runtime_reads_them(self):
        from telegram_search_mcp.config import load_runtime_policy
        census = live_census()
        with tempfile.TemporaryDirectory() as directory:
            for name in matrix.SESSIONS:
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
        self.assertEqual(matrix.session_layout("unauthorized", census)[2], "unauthorized")
        self.assertIsNone(matrix.session_layout("offline", census)[2])

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


class SourceSessionsTests(unittest.TestCase):
    """The five sessions from this tree (proxy, broker and driver as subprocesses): every cell passes."""

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
                                     install={"source": "tree"}, census=census, sessions=results,
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


if __name__ == "__main__":
    unittest.main()
