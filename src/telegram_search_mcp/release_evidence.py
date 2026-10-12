"""Versioned, fail-closed readers and immutable DATA assembly, never acceptance.

Only explicitly supported historical layouts are adapted. Every input pin is
verified before deriving bindings; original reports and inventories are retained.
No report is rewritten, relocated, imported as code, or promoted to a fresh PASS.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

TOOL_VERSION = 1
PIN_KEYS = {"path", "bytes", "sha256", "mode", "uid", "nlink"}
JSON_MAX = 8 * 1024 * 1024
HASH_MAX = 3 * 1024 * 1024 * 1024
ADAPTERS = {
    "tgcmp-f12-independent-actual-closure-review/v1": "fullsuite",
    "tgcmp-p06-actual-closure-independent-review/v1": "package",
}
REPORT_KEYS = {
    "fullsuite": {"schema", "decision", "blockers", "reviewed_at", "fullsuite_acceptance_emitted",
        "actual_terminal_witness", "reviewed_preexecution", "reviewed_author", "analyzer",
        "actual_counts", "source_and_input_closure", "current_process_closure",
        "aggregate_artifacts", "returncode_counts", "effect_counts", "repair_outcomes",
        "supervisor_elapsed_seconds", "pins", "limitations"},
    "package": {"actual_schema_fingerprint", "actual_terminals", "actual_version",
        "analysis_reader_compatibility_adjustments", "blockers", "checks", "counts",
        "decision", "installed_manifest", "limitations", "namespace", "native_resolution",
        "phases", "pins", "protected5", "reviewed_at", "root_runtime_review", "schema", "scope"},
}


class EvidenceError(ValueError):
    """A bounded reason code; never includes input/configuration contents."""


def need(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceError(reason)


def same_json(left: Any, right: Any) -> bool:
    # Python == equates bool/int; wire/receipt equality must retain JSON types.
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":"))


def integer(value: Any, *, minimum: int = 0) -> int:
    need(type(value) is int and value >= minimum, "invalid_integer")
    return value


def digest(value: Any) -> str:
    need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "invalid_digest")
    return value


def canonical_path(value: Any) -> Path:
    need(type(value) is str and "\0" not in value, "invalid_path")
    p = Path(value)
    need(p.is_absolute() and str(p) == value and ".." not in p.parts
         and p.resolve() == p, "invalid_path")
    return p


def validate_pin(row: Any) -> Path:
    need(type(row) is dict and set(row) == PIN_KEYS, "invalid_pin")
    path = canonical_path(row["path"])
    integer(row["bytes"])
    digest(row["sha256"])
    need(type(row["mode"]) is int and 0 <= row["mode"] <= 0o777, "invalid_pin")
    integer(row["uid"])
    need(type(row["nlink"]) is int and row["nlink"] == 1, "invalid_pin")
    return path


def metadata(row: dict) -> os.stat_result:
    path = validate_pin(row)
    try:
        value = path.lstat()
    except OSError as error:
        raise EvidenceError("pin_drift") from error
    need(stat.S_ISREG(value.st_mode) and value.st_size == row["bytes"]
         and stat.S_IMODE(value.st_mode) == row["mode"] and value.st_uid == row["uid"]
         and value.st_nlink == row["nlink"], "pin_drift")
    return value


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
            value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _verified_read(row: dict, *, collect: bool = False) -> bytes | None:
    before = metadata(row)
    path = Path(row["path"])
    h = hashlib.sha256()
    chunks = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            need(_identity(os.fstat(stream.fileno())) == _identity(before), "pin_drift")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
                if collect:
                    chunks.append(chunk)
            need(_identity(os.fstat(stream.fileno())) == _identity(before), "pin_drift")
        need(_identity(path.lstat()) == _identity(before)
             and h.hexdigest() == row["sha256"], "pin_drift")
    except OSError as error:
        raise EvidenceError("pin_drift") from error
    return b"".join(chunks) if collect else None


def verify_pin(row: dict) -> dict:
    _verified_read(row)
    return dict(row)


def current_pin(path: Path) -> dict:
    path = canonical_path(str(path))
    s = path.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, "invalid_pin")
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        need(_identity(os.fstat(stream.fileno())) == _identity(s), "pin_drift")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        need(_identity(os.fstat(stream.fileno())) == _identity(s), "pin_drift")
    row = {"path": str(path), "bytes": s.st_size, "sha256": h.hexdigest(),
           "mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "nlink": s.st_nlink}
    need(_identity(path.lstat()) == _identity(s), "pin_drift")
    return row


def verify_pins(rows: list[dict]) -> list[dict]:
    need(type(rows) is list and len(rows) <= 20000, "invalid_pins")
    seen = set()
    total = 0
    for row in rows:
        name = str(validate_pin(row))
        need(name not in seen, "duplicate_pin")
        seen.add(name)
        metadata(row)  # All metadata, including large files, precedes all hashes.
        total += row["bytes"]
    need(total <= HASH_MAX, "input_budget")
    return [verify_pin(row) for row in rows]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate_key")
        result[key] = value
    return result


def load_json(row: dict, *, immutable: bool = False) -> Any:
    need(row["bytes"] <= JSON_MAX, "json_budget")
    if immutable:
        need(row["mode"] & 0o222 == 0, "mutable_input")
    try:
        verified = _verified_read(row, collect=True)
        value = json.loads(verified.decode("utf-8"),
                           object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("invalid_json")))
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("invalid_json") from error
    return value


def inventory(value: Any, *, wrapper: str | None = None) -> list:
    if type(value) is dict:
        need(wrapper in {"tools", "scenarios", "methods"}, "inventory_layout")
        allowed = {wrapper}
        if wrapper == "tools":
            cursor = set(value) - allowed
            need(cursor in (set(), {"nextCursor"}, {"next_cursor"}), "inventory_layout")
            need(not cursor or value[next(iter(cursor))] is None, "incomplete_inventory")
        else:
            need(set(value) == allowed, "inventory_layout")
        value = value.get(wrapper)
    need(type(value) is list and len(value) <= 20000, "inventory_layout")
    return value


def literal_inventory(source: str, variable: str) -> list:
    """Retain literal += corrections; reject dynamic or conditional mutations."""
    try:
        tree = ast.parse(source)
        result = None
        accepted_stores = set()
        for node in tree.body:
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                target = node.target
            if isinstance(target, ast.Name) and target.id == variable:
                accepted_stores.add(id(target))
                rows = inventory(ast.literal_eval(node.value))
                if isinstance(node, ast.AugAssign):
                    need(isinstance(node.op, ast.Add) and result is not None, "inventory_layout")
                    result += rows
                else:
                    need(result is None, "ambiguous_inventory")
                    result = rows
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == variable:
                # The supported grammar is literal declarations and += only.
                # Any load can escape/alias, and subscript stores are mutations.
                need(id(node) in accepted_stores, "dynamic_inventory")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                need(not isinstance(node.func.value, ast.Name) or node.func.value.id != variable,
                     "dynamic_inventory")
        need(result is not None, "missing_inventory")
        return result
    except (SyntaxError, ValueError, TypeError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("dynamic_inventory") from error


def _source(manifest: dict, root: Path, version: str) -> list:
    rows = inventory(load_json(manifest, immutable=True))
    root = canonical_path(str(root))
    need(root.is_dir() and type(version) is str and re.fullmatch(r"\d+\.\d+\.\d+", version),
         "source_binding")
    seen, checked = set(), []
    for row in rows:
        need(type(row) is dict and set(row) == {"path", "bytes", "sha256"}, "source_binding")
        name = row["path"]
        need(type(name) is str and str(Path(name)) == name and not Path(name).is_absolute()
             and ".." not in Path(name).parts and name not in seen, "source_binding")
        seen.add(name)
        path = canonical_path(str(root / name))
        need(path.is_relative_to(root), "source_binding")
        integer(row["bytes"])
        digest(row["sha256"])
        try:
            s = path.lstat()
            need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size == row["bytes"], "source_drift")
        except OSError as error:
            raise EvidenceError("source_drift") from error
        checked.append({**row, "path": str(path), "mode": stat.S_IMODE(s.st_mode),
                        "uid": s.st_uid, "nlink": s.st_nlink})
    try:
        verify_pins(checked)
    except EvidenceError as error:
        raise EvidenceError("source_drift") from error
    try:
        tree = ast.parse((root / "src/telegram_search_mcp/__init__.py").read_text())
        versions = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and n.targets[0].id == "__version__"]
        need(versions == [version], "source_version")
    except (OSError, SyntaxError, ValueError) as error:
        raise EvidenceError("source_version") from error
    return rows


def _pins(data: dict, kind: str) -> tuple[list, dict]:
    mapping = data["pins"]
    need(type(mapping) is dict and mapping, "missing_pins")
    rows = list(mapping.values())
    for key, row in mapping.items():
        path = validate_pin(row)
        need(type(key) is str and (key == str(path) if kind == "package"
                                 else Path(key).name == path.name), "pin_binding")
    verify_pins(rows)
    return rows, {Path(r["path"]).name: r for r in rows}


def _binding(rows: list, name: str) -> dict:
    selected = [row for row in rows if Path(row["path"]).name == name]
    need(len(selected) == 1, "missing_or_ambiguous_binding")
    return selected[0]


def _source_plan(plan: dict, rows: list, schema: str) -> None:
    need(plan["schema"] == schema, "plan_schema")
    original = inventory(plan["source_files"])
    need(len(original) == len(rows), "source_binding")
    projection = [{k: row[k] for k in ("path", "bytes", "sha256")} for row in original]
    need(same_json(projection, rows), "source_binding")


def _lexical_absolute(value: Any) -> Path:
    need(type(value) is str and "\0" not in value, "invalid_path")
    path = Path(value)
    need(path.is_absolute() and str(path) == value and ".." not in path.parts, "invalid_path")
    return path


def check_native_rows(rows: list, *, count: int | None = None) -> None:
    """The only allowed symlinks are literal alias/target pairs in these rows."""
    need(type(rows) is list and 1 <= len(rows) <= 16
         and (count is None or len(rows) == count), "native_layout")
    aliases = set()
    targets = []
    try:
        for row in rows:
            need(type(row) is dict and {"current_pin", "opt_alias", "load_command"} <= set(row)
                 and set(row) <= {"current_pin", "opt_alias", "load_command", "historical_fingerprint"},
                 "native_layout")
            alias = row["opt_alias"]
            need(type(alias) is dict and set(alias) == {"path", "target", "mode", "uid", "nlink"},
                 "native_layout")
            path = _lexical_absolute(alias["path"])
            need(str(path) not in aliases and type(alias["target"]) is str
                 and "\0" not in alias["target"], "native_layout")
            aliases.add(str(path))
            s = path.lstat()
            need(stat.S_ISLNK(s.st_mode) and stat.S_IMODE(s.st_mode) == integer(alias["mode"])
                 and s.st_uid == integer(alias["uid"]) and s.st_nlink == integer(alias["nlink"], minimum=1)
                 and os.readlink(path) == alias["target"], "native_drift")
            load = _lexical_absolute(row["load_command"])
            need(str(load.resolve(strict=True)) == row["current_pin"]["path"], "native_drift")
            need(_identity(path.lstat()) == _identity(s), "native_drift")
            targets.append(row["current_pin"])
        verify_pins(targets)
    except OSError as error:
        raise EvidenceError("native_drift") from error


def _recorded_protected(approval: dict, rows: list) -> None:
    """Old global-config fingerprints stay inert; fixed protected pins stay exact."""
    need(type(rows) is list and len(rows) == 5, "protected_layout")
    inert = rows[0]
    fields = {"absolute_path", "byte_size", "digest_sha256", "mode", "uid", "nlink"}
    need(type(inert) is dict and fields <= set(inert)
         and set(inert) <= fields | {"classification"}, "protected_layout")
    recorded = {"path": inert["absolute_path"], "bytes": inert["byte_size"],
                "sha256": inert["digest_sha256"], "mode": inert["mode"],
                "uid": inert["uid"], "nlink": inert["nlink"]}
    validate_pin(recorded)  # Shape only: do not rehash an old config as current authority.
    value = load_json(approval, immutable=True)
    need(value["protected_dispatch_config"] == recorded, "protected_binding")
    verify_pins(rows[1:])


def _fullsuite(data: dict, pins: list, source: dict, rows: list) -> tuple[dict, list, dict]:
    need("dispatch_actual" not in data, "ambiguous_report")
    plan = load_json(_binding(pins, "fullsuite-plan.json"), immutable=True)
    _source_plan(plan, rows, "tg47-fullsuite-plan/v1")
    need(_binding(pins, "source-manifest.json") == source, "source_binding")
    counts = data["actual_counts"]
    for key, value in counts.items():
        if key not in {"subtests", "suite_reaped", "reconciliation_complete"}:
            integer(value)
    for key in ("methods", "phases", "source_files", "source_bytes", "observed_launches",
                "errors_total", "policy_refusals", "pytest_exit"):
        integer(counts[key])
    methods = inventory(plan["method_ids"])
    need(all(type(x) is str and x for x in methods) and len(set(methods)) == len(methods)
         and len(methods) == counts["methods"] and counts["methods"] > 0
         and counts["phases"] == counts["methods"] * 3, "inventory_census")
    need(counts["source_files"] == len(rows) and counts["source_bytes"] == sum(r["bytes"] for r in rows),
         "source_binding")
    need(all(counts[k] == 0 for k in ("errors_total", "policy_refusals", "pytest_exit"))
         and counts["suite_reaped"] is True and counts["reconciliation_complete"] is True,
         "report_status")
    subtests = counts["subtests"]
    need(all(integer(subtests[k]) == 0 for k in ("failed", "skipped")), "report_status")
    integer(subtests["passed"], minimum=1)
    closure = data["source_and_input_closure"]
    need(all(closure[k] is True for k in ("metadata_first_stream_hashes_passed",
         "original_source_input197_exact", "dependency_inputs_and_copies3023_exact",
         "trusted_current2923_exact", "same5_protected_paths_fixed4_exact_postcheck")),
         "report_hash_closure")
    native = closure["native_3_alias_targets_exact_afterrun"]
    need(same_json(native, plan["current_native_resolution"]), "native_binding")
    check_native_rows(native, count=3)
    approval = _binding(pins, "fullsuite-f12-execution-approval.json")
    need(approval == closure["root_approval_pin"], "protected_binding")
    _recorded_protected(approval, [closure["dispatch_config_fingerprint_inert"],
                                 *plan["protected_files"][1:]])
    process = data["current_process_closure"]
    if "guarded_claims" in process:
        integer(process["guarded_claims"])
    if "rows" in process["finite_postreconcile_census"]:
        integer(process["finite_postreconcile_census"]["rows"])
    need(process["recorded_birth_parent_spawn_exit_identity_and_original_wait_statuses_verified"] is True
         and process["suite_original_handle_reaped"] is True
         and integer(process["actual_parent_reaps"]) == counts["observed_launches"]
         and process["supervisor_cleanup_uncertainties"] == [] and process["unknown_outputs"] == []
         and process["finite_postreconcile_census"]["performed_once"] is True
         and integer(process["finite_postreconcile_census"]["recorded_process_parent_or_group_matches"]) == 0,
         "report_process_closure")
    witness = data["actual_terminal_witness"]
    need(witness["all_sessions_closed"] is True, "missing_terminal_witness")
    expected = {"optimization-negative": 78, "prepare": 0, "supervise": 0, "reconcile": 0}
    actuals = witness["four_dispatch_actuals"]
    tools = witness["root_tool_terminals"]
    need(set(actuals) == set(tools) == set(expected), "missing_terminal_witness")
    root_pin = _binding(pins, "root-terminal-receipt.json")
    need(root_pin == witness["root_receipt"], "terminal_binding")
    root = load_json(root_pin, immutable=True)
    need(root["schema"] == "tgcmp-root-fullsuite-tool-terminals/v1"
         and root["all_root_tool_sessions_closed"] is True and same_json(root["phases"], tools),
         "terminal_binding")
    for phase, exitcode in expected.items():
        item = actuals[phase]
        integer(item["helper_pid"], minimum=1)
        waits = inventory(item["actual_wait_statuses"])
        need(len(waits) == 1, "terminal_binding")
        for status in waits:
            integer(status)
        need(integer(item["helper_returncode"]) == exitcode and item["complete"] is True
             and item["actual_direct_child_wait"] is True and item["reason"] is None
             and same_json(waits, [exitcode << 8])
             and integer(tools[phase]["exit_code"]) == 0
             and integer(tools[phase]["helper_returncode"]) == exitcode, "terminal_binding")
        if "session" in tools[phase]:
            integer(tools[phase]["session"], minimum=1)
        raw = load_json(_binding(pins, f"dispatch-f12-{phase}.json"))
        need(raw["schema"] == "tg47-root-dispatch/v1" and raw["phase"] == phase
             and all(same_json(raw[k], v) for k, v in item.items()), "terminal_binding")
    return actuals, [], {"methods": methods, "source": rows}


def _package(data: dict, pins: list, source: dict, rows: list, version: str) -> tuple[dict, list, dict]:
    plan = load_json(_binding(pins, "package-plan.json"), immutable=True)
    _source_plan(plan, rows, "tg47-package-plan/v1")
    need(digest(plan["source_manifest_sha256"]) == source["sha256"]
         and data["actual_version"] == version, "source_binding")
    need(same_json(data["native_resolution"], plan["current_native_resolution"]), "native_binding")
    check_native_rows(data["native_resolution"], count=3)
    _recorded_protected(_binding(pins, "package-p06-execution-approval.json"), data["protected5"])
    counts = data["counts"]
    need(integer(counts["source_files"]) == len(rows)
         and integer(counts["source_bytes"]) == sum(r["bytes"] for r in rows), "source_binding")
    need(all(integer(counts[k]) == 4 for k in ("actual_children", "actual_owned_waits",
         "actual_groups_absent", "actual_zero_exits"))
         and integer(counts["current_scoped_process_matches"]) == 0, "report_process_closure")
    phases = inventory(data["phases"])
    need(len(phases) == 4 and {x["phase"] for x in phases} == {"sdist", "wheel", "install", "installed"},
         "inventory_census")
    pids = [integer(x["pid"], minimum=1) for x in phases]
    for record in phases:
        for key in ("ppid", "pgid"):
            if key in record:
                integer(record[key], minimum=1)
    need(len(set(pids)) == 4 and all(integer(x["exit"]) == 0 and x["reaped"] is True
         and x["process_group_absent"] is True for x in phases), "report_process_closure")
    terminals = data["actual_terminals"]
    need(set(terminals) == {"negative", "positive"}, "missing_terminal_witness")
    for phase, expected in (("negative", 78), ("positive", 0)):
        raw = load_json(_binding(pins, f"{phase}-terminal-receipt.json"), immutable=True)
        need(same_json(raw, terminals[phase]) and raw["schema"] == "tgcmp-root-package-terminal/v1"
             and integer(raw["exit_code"]) == expected, "terminal_binding")
    need(terminals["negative"]["negative_namespace_absent"] is True
         and terminals["positive"]["all_reaped"] is True, "report_process_closure")
    child_pids = inventory(terminals["positive"]["actual_child_pids"])
    need(len(child_pids) == 4, "report_process_closure")
    for pid in child_pids:
        integer(pid, minimum=1)
    need(same_json(child_pids, pids), "report_process_closure")
    return terminals, phases, {"source": rows}


def read_report(report: dict, *, source_manifest: dict, source_root: Path,
                source_version: str) -> dict:
    """Verify recorded proof; returns historical DATA, not live/current acceptance."""
    try:
        data = load_json(report, immutable=True)
        need(type(data) is dict and data.get("schema") in ADAPTERS, "unknown_report_schema")
        kind = ADAPTERS[data["schema"]]
        need(set(data) <= REPORT_KEYS[kind], "unknown_report_layout")
        need(data["decision"] == "PASS" and data["blockers"] == [], "report_status")
        need(not any(k in data for k in ("runtime_absent", "data_only", "dispatch_actual")),
             "ambiguous_report")
        rows = _source(source_manifest, source_root, source_version)
        pins, _ = _pins(data, kind)
        if kind == "fullsuite":
            terminals, processes, inventories = _fullsuite(data, pins, source_manifest, rows)
            counts = data["actual_counts"]
        else:
            terminals, processes, inventories = _package(data, pins, source_manifest, rows, source_version)
            counts = data["counts"]
        return {"schema": "tgcmp-normalized-release-report/v1", "adapter": data["schema"],
                "kind": kind, "status": "HISTORICAL_RECORDED_EVIDENCE_VERIFIED",
                "acceptance": False, "report": report, "source_manifest": source_manifest,
                "source_root": str(source_root), "source_version": source_version,
                "reported_counts": counts, "terminal_witnesses": terminals,
                "process_records": processes, "inventories": inventories,
                "evidence_pins": pins, "recorded_report": data}
    except EvidenceError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise EvidenceError("malformed_report") from error


def assemble(request_pin: dict) -> dict:
    """All verification finishes before creating the exclusive output namespace."""
    try:
        request = load_json(request_pin, immutable=True)
        expected_keys = {"schema", "source_manifest", "source_root", "source_version",
                         "output_namespace", "tooling", "reports"}
        need(type(request) is dict and set(request) == expected_keys
             and request["schema"] == "tgcmp-evidence-assembly-request/v1", "assembly_schema")
        output = canonical_path(request["output_namespace"])
        need(not os.path.lexists(output), "namespace_exists")
        source_root = canonical_path(request["source_root"])
        need(not output.is_relative_to(source_root) and output.parent.is_dir(), "output_namespace")
        tooling = request["tooling"]
        need(set(tooling) == {"version", "entrypoint", "libraries"}
             and type(tooling["version"]) is int and tooling["version"] == TOOL_VERSION,
             "tooling_version")
        need(tooling["entrypoint"]["path"] == str(Path(__file__).resolve()), "entrypoint_binding")
        verify_pins([tooling["entrypoint"], *tooling["libraries"]])
        reports = inventory(request["reports"])
        need(1 <= len(reports) <= len(ADAPTERS), "report_count")
        need(all(type(r) is dict and set(r) == {"role", "report"} for r in reports), "report_binding")
        need(len({r["role"] for r in reports}) == len(reports), "duplicate_report")
        verify_pins([r["report"] for r in reports])
        normalized = []
        for row in reports:
            value = read_report(row["report"], source_manifest=request["source_manifest"],
                                source_root=source_root, source_version=request["source_version"])
            need(row["role"] == value["kind"], "report_binding")
            normalized.append(value)
        # Recheck recipe/report inputs after reads, before any derived output.
        verify_pins([request_pin, tooling["entrypoint"], *tooling["libraries"],
                     *[r["report"] for r in reports]])
        packet = {"schema": "tgcmp-release-evidence-packet/v1", "tooling": tooling,
                  "request": request_pin, "status": "DATA_HISTORICAL_EVIDENCE_VERIFIED",
                  "acceptance": False, "gate_execution": False,
                  "source_manifest": request["source_manifest"], "reports": normalized,
                  "bindings": {r["kind"]: {"report": r["report"],
                               "source_manifest": r["source_manifest"],
                               "terminal_witnesses": r["terminal_witnesses"]} for r in normalized}}
        payload = (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode()
        need(len(payload) <= JSON_MAX, "json_budget")
        output.mkdir(mode=0o700)
        path = output / "evidence.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        return {"status": packet["status"], "acceptance": False,
                "reports": len(normalized), "packet": current_pin(path)}
    except EvidenceError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise EvidenceError("malformed_assembly") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        need(args.request.is_absolute(), "invalid_path")
        row = current_pin(args.request)
        need(row["sha256"] == digest(args.request_sha256), "pin_drift")
        result = assemble(row)
    except EvidenceError as error:
        print(json.dumps({"status": "evidence_preparation_refused", "reason": str(error), "acceptance": False}))
        return 78
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
