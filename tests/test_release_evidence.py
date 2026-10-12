from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


def file_pin(path: Path) -> dict:
    value = path.stat()
    return {"path": str(path), "bytes": value.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(value.st_mode), "uid": value.st_uid,
            "nlink": value.st_nlink}


def document(path: Path, value: object) -> dict:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o444)
    return file_pin(path)


class ReleaseEvidenceTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("telegram_search_mcp.release_evidence")
        self.assertIsNotNone(spec, "reusable report reader is missing")
        from telegram_search_mcp import release_evidence
        return release_evidence

    def fixture(self, root: Path) -> dict:
        source = root / "source"
        version = source / "src/telegram_search_mcp/__init__.py"
        version.parent.mkdir(parents=True)
        version.write_bytes(b'__version__ = "0.51.0"\n')
        (source / "sample.txt").write_bytes(b"source\n")
        rows = [{"path": "sample.txt", "bytes": 7,
                 "sha256": hashlib.sha256(b"source\n").hexdigest()},
                {"path": "src/telegram_search_mcp/__init__.py", "bytes": 23,
                 "sha256": hashlib.sha256(b'__version__ = "0.51.0"\n').hexdigest()}]
        manifest = document(root / "source-manifest.json", rows)
        native = []
        for i in range(3):
            cellar = root / f"cellar{i}"
            cellar.mkdir()
            library = cellar / "library.bin"
            library.write_bytes(b"native fixture\n")
            library.chmod(0o444)
            alias = root / f"native-alias{i}"
            alias.symlink_to(cellar.name)
            s = alias.lstat()
            native.append({"current_pin": file_pin(library),
                "opt_alias": {"path": str(alias), "target": cellar.name,
                    "mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "nlink": s.st_nlink},
                "load_command": str(alias / "library.bin")})
        protected = []
        for i in range(5):
            p = root / f"config{i}.toml"
            p.write_bytes(b"fixture configuration\n")
            p.chmod(0o600)
            protected.append(file_pin(p))
        config = protected[0]
        inert = {"absolute_path": config["path"], "byte_size": config["bytes"],
                 "digest_sha256": config["sha256"], "mode": config["mode"],
                 "uid": config["uid"], "nlink": config["nlink"]}
        fdir = root / "f12"
        fdir.mkdir()
        fplan = document(fdir / "fullsuite-plan.json", {
            "schema": "tg47-fullsuite-plan/v1", "source_files": rows,
            "method_ids": ["tests/test_sample.py::Sample::test_one"],
            "current_native_resolution": native, "protected_files": protected})
        approval = document(fdir / "fullsuite-f12-execution-approval.json",
                            {"protected_dispatch_config": config})
        phases = {}
        actuals = {}
        pins = {"source-manifest.json": manifest, "fullsuite-plan.json": fplan,
                "fullsuite-f12-execution-approval.json": approval}
        for phase in ("optimization-negative", "prepare", "supervise", "reconcile"):
            expected = 78 if phase == "optimization-negative" else 0
            phases[phase] = {"exit_code": 0, "helper_returncode": expected,
                             "initial": "first", "terminal": "last"}
            actual = {"helper_pid": 123, "helper_returncode": expected,
                      "actual_wait_statuses": [19968 if expected else 0],
                      "actual_direct_child_wait": True, "complete": True, "reason": None}
            actuals[phase] = actual
            pins[f"dispatch-f12-{phase}.json"] = document(fdir / f"dispatch-f12-{phase}.json", {
                **actual, "schema": "tg47-root-dispatch/v1", "phase": phase,
                "expected_helper_returncode": expected})
        root_receipt = document(fdir / "root-terminal-receipt.json", {
            "schema": "tgcmp-root-fullsuite-tool-terminals/v1",
            "all_root_tool_sessions_closed": True, "phases": phases})
        pins["root-terminal-receipt.json"] = root_receipt
        full = {
            "schema": "tgcmp-f12-independent-actual-closure-review/v1",
            "decision": "PASS", "blockers": [],
            "actual_terminal_witness": {"root_receipt": root_receipt,
                "root_tool_terminals": phases, "all_sessions_closed": True,
                "four_dispatch_actuals": actuals},
            "actual_counts": {"methods": 1, "phases": 3,
                "source_files": 2, "source_bytes": 30,
                "subtests": {"failed": 0, "passed": 1, "skipped": 0},
                "observed_launches": 1, "errors_total": 0, "policy_refusals": 0,
                "pytest_exit": 0, "suite_reaped": True, "reconciliation_complete": True},
            "source_and_input_closure": {"metadata_first_stream_hashes_passed": True,
                "original_source_input197_exact": True,
                "dependency_inputs_and_copies3023_exact": True,
                "trusted_current2923_exact": True,
                "same5_protected_paths_fixed4_exact_postcheck": True,
                "native_3_alias_targets_exact_afterrun": native,
                "dispatch_config_fingerprint_inert": inert, "root_approval_pin": approval},
            "current_process_closure": {
                "recorded_birth_parent_spawn_exit_identity_and_original_wait_statuses_verified": True,
                "actual_parent_reaps": 1, "suite_original_handle_reaped": True,
                "supervisor_cleanup_uncertainties": [], "unknown_outputs": [],
                "finite_postreconcile_census": {"recorded_process_parent_or_group_matches": 0,
                                                "performed_once": True}},
            "pins": pins, "limitations": ["historical fixture only"]}
        fp = document(root / "full-review.json", full)
        pdir = root / "p06"
        pdir.mkdir()
        pp = document(pdir / "package-plan.json", {
            "schema": "tg47-package-plan/v1", "source_files": rows,
            "source_manifest_sha256": manifest["sha256"],
            "current_native_resolution": native, "protected_files": protected})
        p_approval = document(pdir / "package-p06-execution-approval.json",
                              {"protected_dispatch_config": config})
        negative = {"schema": "tgcmp-root-package-terminal/v1", "exit_code": 78,
                    "negative_namespace_absent": True, "runtime": str(root / "runtime")}
        positive = {"schema": "tgcmp-root-package-terminal/v1", "exit_code": 0,
                    "all_reaped": True, "actual_child_pids": [1, 2, 3, 4],
                    "runtime": str(root / "runtime")}
        np = document(pdir / "negative-terminal-receipt.json", negative)
        posp = document(pdir / "positive-terminal-receipt.json", positive)
        package = {
            "schema": "tgcmp-p06-actual-closure-independent-review/v1",
            "decision": "PASS", "blockers": [], "actual_version": "0.51.0",
            "actual_terminals": {"negative": negative, "positive": positive},
            "counts": {"source_files": 2, "source_bytes": 30, "actual_children": 4,
                "actual_owned_waits": 4, "actual_groups_absent": 4,
                "actual_zero_exits": 4, "current_scoped_process_matches": 0},
            "phases": [{"phase": name, "pid": pid, "exit": 0, "reaped": True,
                        "process_group_absent": True}
                       for pid, name in enumerate(("sdist", "wheel", "install", "installed"), 1)],
            "pins": {p["path"]: p for p in (pp, np, posp, p_approval)},
            "native_resolution": native, "protected5": [inert, *protected[1:]],
            "limitations": ["historical fixture only"]}
        return {"full": full, "full_pin": fp, "package": package,
                "package_pin": document(root / "package-review.json", package),
                "manifest": manifest, "source": source}

    def read(self, module, pin, fixture):
        return module.read_report(pin, source_manifest=fixture["manifest"],
                                  source_root=fixture["source"], source_version="0.51.0")

    def read_valid(self, module, pin, fixture):
        try:
            return self.read(module, pin, fixture)
        except module.EvidenceError as error:
            self.fail(f"valid explicit report layout refused: {error}")

    def test_current_fullsuite_pins_and_terminal_witness_need_no_dispatch_actual_key(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            f = self.fixture(Path(directory).resolve())
            result = self.read_valid(module, f["full_pin"], f)
            self.assertEqual(result["kind"], "fullsuite")
            self.assertEqual(set(result["terminal_witnesses"]),
                             {"optimization-negative", "prepare", "supervise", "reconcile"})
            self.assertFalse(result["acceptance"])

    def test_package_adapter_retains_four_owned_phase_records(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            f = self.fixture(Path(directory).resolve())
            result = self.read_valid(module, f["package_pin"], f)
            self.assertEqual(result["kind"], "package")
            self.assertEqual([r["phase"] for r in result["process_records"]],
                             ["sdist", "wheel", "install", "installed"])
            self.assertFalse(result["acceptance"])

    def test_native_target_and_alias_drift_refuse_before_evidence_derivation(self):
        module = self.module()
        for change in ("bytes", "alias"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                f = self.fixture(root)
                self.read_valid(module, f["full_pin"], f)
                if change == "bytes":
                    library = root / "cellar0/library.bin"
                    library.chmod(0o600)
                    library.write_bytes(b"native changed\n")
                    library.chmod(0o444)
                else:
                    (root / "native-alias0").unlink()
                    (root / "native-alias0").symlink_to("cellar1")
                with self.assertRaises(module.EvidenceError):
                    self.read(module, f["full_pin"], f)

    def test_unknown_ambiguous_data_only_missing_or_false_terminal_refuse(self):
        module = self.module()
        for change in ("unknown", "ambiguous", "data_only", "missing", "bool_count", "unreaped"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                f = self.fixture(root)
                d = copy.deepcopy(f["full"])
                if change == "unknown": d["schema"] += "-future"
                elif change == "ambiguous": d["dispatch_actual"] = {}
                elif change == "data_only": d["decision"] = "PASS_DATA_ONLY"
                elif change == "missing": del d["actual_terminal_witness"]
                elif change == "bool_count": d["actual_counts"]["methods"] = True
                else: d["actual_terminal_witness"]["four_dispatch_actuals"]["prepare"]["actual_direct_child_wait"] = False
                p = document(root / "bad.json", d)
                with self.assertRaises(module.EvidenceError): self.read(module, p, f)

    def test_hash_and_source_drift_refuse_before_assembly_output(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            f = self.fixture(root)
            bad_pin = {**f["full_pin"], "sha256": "0" * 64}
            with self.assertRaisesRegex(module.EvidenceError, "pin_drift"):
                self.read(module, bad_pin, f)
            (f["source"] / "sample.txt").write_bytes(b"change\n")
            with self.assertRaisesRegex(module.EvidenceError, "source_drift"):
                self.read(module, f["full_pin"], f)

    def test_duplicate_pin_or_missing_source_binding_refuse(self):
        module = self.module()
        for change in ("duplicate", "missing_binding"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                f = self.fixture(root)
                d = copy.deepcopy(f["full"])
                if change == "duplicate":
                    d["pins"]["alias/source-manifest.json"] = d["pins"]["source-manifest.json"]
                else: del d["pins"]["source-manifest.json"]
                with self.assertRaises(module.EvidenceError):
                    self.read(module, document(root / "bad.json", d), f)

    def test_duplicate_json_keys_and_noncanonical_paths_refuse(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            f = self.fixture(root)
            p = root / "duplicate.json"
            p.write_text('{"schema":"bad","schema":"bad"}')
            p.chmod(0o444)
            with self.assertRaisesRegex(module.EvidenceError, "duplicate_key"):
                self.read(module, file_pin(p), f)
            pin = {**f["full_pin"], "path": str(root) + "/../" + root.name + "/full-review.json"}
            with self.assertRaises(module.EvidenceError): self.read(module, pin, f)

    def test_contradictory_unversioned_status_and_bool_integer_receipt_refuse(self):
        module = self.module()
        for change in ("status", "raw_type", "raw_phase"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                f = self.fixture(root)
                self.read_valid(module, f["full_pin"], f)
                data = copy.deepcopy(f["full"])
                if change == "status":
                    data["status"] = "DATA_ONLY_RUNTIME_ABSENT"
                else:
                    key = "dispatch-f12-prepare.json"
                    raw = json.loads(Path(data["pins"][key]["path"]).read_text())
                    if change == "raw_type": raw["actual_direct_child_wait"] = 1
                    else: raw["phase"] = "supervise"
                    target = root / "tampered"
                    target.mkdir()
                    data["pins"][key] = document(target / key, raw)
                with self.assertRaises(module.EvidenceError):
                    self.read(module, document(root / "bad.json", data), f)

    def test_static_correcting_additions_and_wrapped_inventory_are_not_lost(self):
        module = self.module()
        source = "ROWS = [{'id':'one'}]\nROWS += [{'id':'correction'}]\n"
        self.assertEqual(module.literal_inventory(source, "ROWS"),
                         [{"id": "one"}, {"id": "correction"}])
        self.assertEqual(module.inventory({"tools": [{"name": "one"}]}, wrapper="tools"),
                         [{"name": "one"}])
        with self.assertRaises(module.EvidenceError):
            module.literal_inventory("ROWS = []\nROWS += build_rows()", "ROWS")

    def test_json_parser_consumes_the_same_verified_bytes_under_transient_swap(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "input.json"
            pin = document(path, {"value": "good"})
            good = path.read_bytes()
            original = Path.read_text
            def transient_read(target, *args, **kwargs):
                if target != path:
                    return original(target, *args, **kwargs)
                path.chmod(0o600)
                path.write_text('{"value":"evil"}')
                value = original(path, *args, **kwargs)
                path.write_bytes(good)
                path.chmod(0o444)
                return value
            with patch.object(Path, "read_text", transient_read):
                self.assertEqual(module.load_json(pin, immutable=True), {"value": "good"})

    def test_static_inventory_refuses_subscript_mutation_and_alias_escape(self):
        module = self.module()
        for source in ("ROWS = [{'id':'one'}]\nROWS[0] = {'id':'lost'}\n",
                       "ROWS = [{'id':'one'}]\nALIAS = ROWS\nALIAS.append({'id':'lost'})\n"):
            with self.subTest(source=source), self.assertRaises(module.EvidenceError):
                module.literal_inventory(source, "ROWS")

    def test_package_terminal_pid_booleans_cannot_equal_integer_phase_pids(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            f = self.fixture(root)
            self.read_valid(module, f["package_pin"], f)
            data = copy.deepcopy(f["package"])
            data["actual_terminals"]["positive"]["actual_child_pids"][0] = True
            target = root / "tampered"
            target.mkdir()
            name = "positive-terminal-receipt.json"
            replacement = document(target / name, data["actual_terminals"]["positive"])
            old = next(k for k in data["pins"] if Path(k).name == name)
            del data["pins"][old]
            data["pins"][replacement["path"]] = replacement
            with self.assertRaises(module.EvidenceError):
                self.read(module, document(root / "bad-package.json", data), f)

    def test_wait_status_and_optional_parent_group_identity_require_integers(self):
        module = self.module()
        for change in ("wait_status", "parent", "group"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                f = self.fixture(root)
                data = copy.deepcopy(f["full"] if change == "wait_status" else f["package"])
                if change == "wait_status":
                    data["actual_terminal_witness"]["four_dispatch_actuals"]["prepare"]["actual_wait_statuses"] = [False]
                    key = "dispatch-f12-prepare.json"
                    raw = json.loads(Path(data["pins"][key]["path"]).read_text())
                    raw["actual_wait_statuses"] = [False]
                    target = root / "tampered"
                    target.mkdir()
                    data["pins"][key] = document(target / key, raw)
                else:
                    data["phases"][0]["ppid" if change == "parent" else "pgid"] = True
                with self.assertRaises(module.EvidenceError):
                    self.read(module, document(root / "bad-status.json", data), f)

    def test_assembly_fresh_namespace_and_recipe_pins_are_enforced(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            f = self.fixture(root)
            library = Path(module.__file__).resolve()
            request = {"schema": "tgcmp-evidence-assembly-request/v1",
                "source_manifest": f["manifest"], "source_root": str(f["source"]),
                "source_version": "0.51.0", "output_namespace": str(root / "fresh"),
                "tooling": {"version": 1, "entrypoint": file_pin(library), "libraries": []},
                "reports": [{"role": "fullsuite", "report": f["full_pin"]},
                            {"role": "package", "report": f["package_pin"]}]}
            request_pin = document(root / "request.json", request)
            try:
                result = module.assemble(request_pin)
            except module.EvidenceError as error:
                self.fail(f"valid historical assembly refused: {error}")
            packet = json.loads(Path(result["packet"]["path"]).read_text())
            self.assertEqual(packet["status"], "DATA_HISTORICAL_EVIDENCE_VERIFIED")
            self.assertFalse(packet["acceptance"])
            self.assertEqual(set(packet["bindings"]), {"fullsuite", "package"})
            self.assertEqual(packet["tooling"]["entrypoint"], file_pin(library))
            with self.assertRaisesRegex(module.EvidenceError, "namespace_exists"):
                module.assemble(request_pin)
            request["output_namespace"] = str(root / "stale")
            request["reports"][1]["report"]["sha256"] = "0" * 64
            bad = document(root / "bad-request.json", request)
            with self.assertRaisesRegex(module.EvidenceError, "pin_drift"):
                module.assemble(bad)
            self.assertFalse((root / "stale").exists())


if __name__ == "__main__":
    unittest.main()
