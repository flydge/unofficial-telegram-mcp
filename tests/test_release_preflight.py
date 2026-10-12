from __future__ import annotations

import hashlib
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telegram_search_mcp import release_preflight

SCRIPT = Path(release_preflight.__file__)


class ReleasePreflightTests(unittest.TestCase):
    def make_manifest(self, root: Path) -> tuple[Path, dict]:
        source = root / "source"
        source.mkdir()
        (source / "example.txt").write_bytes(b"synthetic source\n")
        return root / "manifest.json", {
            "schema": 1,
            "source_root": str(source),
            "source_files": [{"path": "example.txt", "bytes": 17,
                              "sha256": hashlib.sha256(b"synthetic source\n").hexdigest()}],
            "harness_files": [],
            "runtime": {"executable": sys.executable, "prefix": sys.prefix,
                        "packages": {}, "check_mcp_fields": False},
            "socket_path": "/tmp/preflight-fixture.sock",
        }

    def run_preflight(self, manifest: Path) -> SimpleNamespace:
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, "argv", [str(SCRIPT), str(manifest)]):
            status = release_preflight.main()
        return SimpleNamespace(returncode=status, stdout=output.getvalue(), stderr="")

    def test_matching_inputs_are_verified_without_creating_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, data = self.make_manifest(root)
            manifest.write_text(json.dumps(data))
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = self.run_preflight(manifest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "status": "preflight_passed", "source_files_checked": 1,
                "harness_files_checked": 0, "acceptance": False,
            })
            self.assertEqual(before, {p.relative_to(root): p.read_bytes()
                                      for p in root.rglob("*") if p.is_file()})

    def test_stale_or_duplicate_pins_are_rejected(self) -> None:
        for failure in ("source_drift", "duplicate_source", "harness_drift", "duplicate_harness"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                manifest, data = self.make_manifest(root)
                if failure == "source_drift":
                    (root / "source/example.txt").write_bytes(b"synthetic change\n")
                elif failure == "duplicate_source":
                    data["source_files"].append({**data["source_files"][0], "sha256": "0" * 64})
                else:
                    harness = root / "guard.py"
                    harness.write_bytes(b"guard")
                    data["harness_files"] = [{"path": str(harness), "bytes": 5,
                                              "sha256": hashlib.sha256(b"guard").hexdigest()}]
                    if failure == "harness_drift":
                        harness.write_bytes(b"other")
                    else:
                        data["harness_files"] *= 2
                manifest.write_text(json.dumps(data))
                result = self.run_preflight(manifest)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertEqual(json.loads(result.stdout)["reason"], failure)

    def test_duplicate_json_keys_cannot_hide_an_obsolete_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text('{"schema": 0, "schema": 1}')
            result = self.run_preflight(manifest)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "duplicate_key")

    def test_wrong_runtime_and_long_socket_fail_before_expensive_checks(self) -> None:
        cases = (("executable", "/nonexistent/python", "executable_mismatch"),
                 ("prefix", "/nonexistent/prefix", "prefix_mismatch"),
                 ("packages", {"codex-preflight-nonexistent-fixture": "0.0.0"}, "dependency_mismatch"))
        for key, value, reason in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                manifest, data = self.make_manifest(Path(directory).resolve())
                data["runtime"][key] = value
                manifest.write_text(json.dumps(data))
                result = self.run_preflight(manifest)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertEqual(json.loads(result.stdout)["reason"], reason)
        with tempfile.TemporaryDirectory() as directory:
            manifest, data = self.make_manifest(Path(directory).resolve())
            data["socket_path"] = "/tmp/" + "é" * 50
            manifest.write_text(json.dumps(data))
            result = self.run_preflight(manifest)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertEqual(json.loads(result.stdout)["reason"], "socket_path_too_long")

    def test_source_paths_cannot_escape_or_follow_symlinks(self) -> None:
        for name in ("../outside.txt", "link.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                manifest, data = self.make_manifest(root)
                (root / "outside.txt").write_bytes(b"synthetic source\n")
                (root / "source/link.txt").symlink_to(root / "outside.txt")
                data["source_files"][0]["path"] = name
                manifest.write_text(json.dumps(data))
                result = self.run_preflight(manifest)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertEqual(json.loads(result.stdout)["reason"], "invalid_source_path")

    def test_malformed_manifest_has_a_bounded_failure(self) -> None:
        cases = ([], {"schema": 2}, {"schema": 1, "runtime": {"executable": sys.executable,
                 "prefix": sys.prefix, "packages": [], "check_mcp_fields": False}})
        for data in cases:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "manifest.json"
                manifest.write_text(json.dumps(data))
                result = self.run_preflight(manifest)
                self.assertEqual(result.returncode, 78)
                self.assertEqual(json.loads(result.stdout)["status"], "preflight_failed")

    def test_sdk_wire_aliases_are_not_accepted_as_python_fields(self) -> None:
        runtime = {"executable": sys.executable, "prefix": sys.prefix,
                   "packages": {}, "check_mcp_fields": True}
        wrong = SimpleNamespace(InitializeResult=type("Init", (), {"model_fields": {"serverInfo": str}}),
                                CallToolResult=type("Call", (), {"model_fields": {
                                    "isError": bool, "structuredContent": dict}}))
        with patch.object(sys, "dont_write_bytecode", True), \
                patch.object(release_preflight.importlib, "import_module", return_value=wrong):
            with self.assertRaisesRegex(release_preflight.PreflightError, "mcp_fields_mismatch"):
                release_preflight.check_runtime(runtime)

    def test_harness_path_alias_does_not_count_as_a_second_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, data = self.make_manifest(root)
            guard = root / "guard.py"
            guard.write_bytes(b"guard")
            pin = {"path": str(guard), "bytes": 5, "sha256": hashlib.sha256(b"guard").hexdigest()}
            data["harness_files"] = [pin, {**pin, "path": str(root) + "//guard.py"}]
            manifest.write_text(json.dumps(data))
            result = self.run_preflight(manifest)
            self.assertEqual(result.returncode, 78)
            self.assertEqual(json.loads(result.stdout)["reason"], "duplicate_harness")

    def test_sdk_import_requires_bytecode_writes_disabled(self) -> None:
        runtime = {"executable": sys.executable, "prefix": sys.prefix,
                   "packages": {}, "check_mcp_fields": True}
        with patch.object(sys, "dont_write_bytecode", False):
            with self.assertRaisesRegex(release_preflight.PreflightError, "bytecode_writes_enabled"):
                release_preflight.check_runtime(runtime)

    def test_legacy_standalone_cli_keeps_its_existing_v1_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, data = self.make_manifest(Path(directory).resolve())
            manifest.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(manifest)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "preflight_passed")

    def test_v2_requires_explicit_environment_and_sdk_probe_before_packet(self) -> None:
        from test_release_environment import ReleaseEnvironmentTests
        from test_release_evidence import document
        from telegram_search_mcp import release_environment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, data = self.make_manifest(root)
            spec = ReleaseEnvironmentTests().fixture(root)
            baseline = release_environment.capture_environment(document(root / "spec.json", spec),
                                                               root / "baseline")["manifest"]
            data.update(schema=2, chain_environment=baseline, stage="fullsuite", moment="before",
                        sdk_probe_receipt=None, evidence_request=None)
            pin = document(manifest, data)
            try:
                value = release_preflight.preflight(manifest, expected_sha256=pin["sha256"])
            except (TypeError, release_preflight.PreflightError) as error:
                self.fail(f"version2 common environment path unavailable: {error}")
            self.assertEqual(value["environment"]["status"], "ENVIRONMENT_MATCHES_BASELINE")
            self.assertFalse(value["acceptance"])
            data["stage"] = "sdk"
            target = root / "sdk-manifest.json"
            pin = document(target, data)
            with self.assertRaisesRegex(release_preflight.PreflightError, "sdk_probe_required"):
                release_preflight.preflight(target, expected_sha256=pin["sha256"])

    def test_v2_drift_refuses_before_source_or_harness_preparation(self) -> None:
        from test_release_environment import ReleaseEnvironmentTests
        from test_release_evidence import document
        from telegram_search_mcp import release_environment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, data = self.make_manifest(root)
            spec = ReleaseEnvironmentTests().fixture(root)
            baseline = release_environment.capture_environment(document(root / "spec.json", spec),
                                                               root / "baseline")["manifest"]
            data.update(schema=2, chain_environment=baseline, stage="package", moment="before",
                        sdk_probe_receipt=None, evidence_request=None)
            pin = document(manifest, data)
            Path(spec["files"]["protected"][0]["path"]).write_bytes(b"changed configuration\n")
            try:
                with patch.object(release_preflight, "_check_files", side_effect=AssertionError("too late")):
                    with self.assertRaisesRegex(release_preflight.PreflightError, "pin_drift"):
                        release_preflight.preflight(manifest, expected_sha256=pin["sha256"])
            except TypeError as error:
                self.fail(f"version2 early drift check unavailable: {error}")

    def test_probe_receipt_unknown_layout_and_wrong_channels_fail_closed(self) -> None:
        from test_release_environment import ReleaseEnvironmentTests
        from test_release_evidence import document, file_pin
        from telegram_search_mcp import release_environment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            spec = ReleaseEnvironmentTests().fixture(root)
            baseline = release_environment.capture_environment(document(root / "spec.json", spec),
                                                               root / "baseline")["manifest"]
            libs = {name: file_pin(SCRIPT.with_name(name + ".py"))
                    for name in ("release_evidence", "release_environment")}
            request = document(root / "request.json", {
                "schema": "tgcmp-sdk-error-probe-request/v1", "runtime": spec["runtime"],
                "libraries": libs, "implementation_pins": spec["files"]["source"],
                "environment_manifest": baseline, "expected_tools": 43, "source_version": "0.51.0"})
            cases = []
            for route in ("plain", "topic", "reply", "topic_reply"):
                reply = route in {"reply", "topic_reply"}
                for kind in ("structural", "semantic", "revision"):
                    tool = (("update_reply_draft" if reply else "update_draft") if kind == "revision"
                            else ("prepare_reply_text_send" if reply else "prepare_text_send"))
                    errors = ([{"type": "list_type", "loc": ["outgoing_entities"], "msg": "Input should be a valid list"}]
                              if kind == "structural" else
                              [{"type": "value_error", "loc": [], "msg": "Value error, Invalid whole-scalar UTF-16 range"}]
                              if kind == "semantic" else
                              [{"type": "missing", "loc": ["text"], "msg": "Field required"}] if reply else
                              [{"type": "value_error", "loc": [], "msg": "Value error, exactly one of text or caption is required"}])
                    cases.append({"id": route + "/" + kind, "tool": tool,
                        "channel": "masked" if not reply and kind != "structural" else "detailed",
                        "model_errors": errors, "diagnostic_sha256": "a" * 64, "state_unchanged": True})
            receipt = {"schema": "tgcmp-installed-sdk-error-probe/v1", "status": "SDK_ERROR_PREFLIGHT_PASSED",
                "acceptance": False, "tool_version": 1, "sdk_calls": 12,
                "routes": ["plain", "topic", "reply", "topic_reply"], "cases": cases,
                **{key: [] for key in ("service_calls", "provider_calls", "hydration_calls", "owner_calls", "claims",
                                       "forbidden_effects", "loaded_origins")},
                "pending_and_approval_unchanged": True, "lazy_factory_calls": 0, "service_close_calls": 0,
                "limitation": "unit fixture", "entrypoint": file_pin(SCRIPT.with_name("release_sdk_probe.py")),
                "request": request}
            for moment in ("before", "after"):
                receipt["environment_" + moment] = release_environment.check_environment(baseline, stage="sdk", moment=moment)
            valid = document(root / "valid.json", receipt)
            self.assertEqual(release_preflight._sdk_probe(valid, baseline), valid)
            for mutation in ("layout", "case_layout", "channel", "reason", "success", "effects", "boolean_count"):
                import copy
                bad = copy.deepcopy(receipt)
                if mutation == "layout": bad["future_schema_extension"] = True
                elif mutation == "case_layout": bad["cases"][0]["future"] = True
                elif mutation == "channel": bad["cases"][0]["channel"] = "masked"
                elif mutation == "reason": bad["cases"][0]["model_errors"][0]["msg"] = "wrong reason"
                elif mutation == "success": bad["acceptance"] = True
                elif mutation == "effects": bad["provider_calls"] = ["attempted"]
                else: bad["lazy_factory_calls"] = False
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    release_preflight._sdk_probe(document(root / (mutation + ".json"), bad), baseline)


if __name__ == "__main__":
    unittest.main()
