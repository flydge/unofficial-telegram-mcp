"""Read-only diagnostics before a release gate; passing is not acceptance.

Run with the intended verification interpreter, -B, and an external JSON manifest.
The manifest separates source_files, harness_files, and runtime requirements;
save each invocation's JSON output separately from those immutable inputs.
This module never launches a gate or changes its policy.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

if __package__:
    from . import release_evidence as ev
else:
    ev = None  # Preserve the original standalone v1 script invocation.


class PreflightError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError("duplicate_key")
        result[key] = value
    return result


def _check_files(root: Path | None, rows: list[dict], kind: str) -> int:
    seen = set()
    for row in rows:
        name = str(Path(row["path"]))
        if name in seen:
            raise PreflightError(f"duplicate_{kind}")
        seen.add(name)
    for row in rows:
        path = Path(row["path"])
        if root is not None:
            if path.is_absolute() or ".." in path.parts or str(path) != row["path"]:
                raise PreflightError("invalid_source_path")
            path = root / path
            if path.resolve() != path or not path.is_relative_to(root):
                raise PreflightError("invalid_source_path")
        elif not path.is_absolute() or path.resolve() != path:
            raise PreflightError("invalid_harness_path")
        try:
            if not path.is_file() or path.stat().st_size != row["bytes"]:
                raise PreflightError(f"{kind}_drift")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise PreflightError(f"{kind}_drift")
        except OSError as error:
            raise PreflightError(f"{kind}_drift") from error
    return len(rows)


def check_runtime(runtime: dict) -> None:
    # Preserve the virtual-environment executable spelling: resolving its symlink
    # would wrongly equate distinct environments using the same base interpreter.
    if os.path.abspath(sys.executable) != runtime["executable"]:
        raise PreflightError("executable_mismatch")
    if os.path.abspath(sys.prefix) != runtime["prefix"]:
        raise PreflightError("prefix_mismatch")
    if not isinstance(runtime["packages"], dict) or type(runtime["check_mcp_fields"]) is not bool:
        raise PreflightError("invalid_manifest")
    for name, expected in runtime["packages"].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise PreflightError("dependency_mismatch") from error
        if actual != expected:
            raise PreflightError("dependency_mismatch")
    if runtime["check_mcp_fields"]:
        # Imports execute trusted dependency initialization. Its origin still
        # belongs to the enclosing gate; do not create dependency bytecode here.
        if not sys.dont_write_bytecode:
            raise PreflightError("bytecode_writes_enabled")
        try:
            types = importlib.import_module("mcp.types")
            required = ((types.InitializeResult, {"server_info"}),
                        (types.CallToolResult, {"is_error", "structured_content"}))
            if any(not fields <= set(model.model_fields) for model, fields in required):
                raise PreflightError("mcp_fields_mismatch")
        except (ImportError, AttributeError) as error:
            raise PreflightError("mcp_fields_mismatch") from error


def _sdk_probe(row: dict, environment: dict) -> dict:
    from . import release_evidence as ev
    ev.need(row is not None, "sdk_probe_required")
    value = ev.load_json(row, immutable=True)
    ev.need(type(value) is dict and set(value) == {"schema", "tool_version", "status", "acceptance",
            "sdk_calls", "routes", "cases", "service_calls", "provider_calls", "hydration_calls",
            "owner_calls", "claims", "pending_and_approval_unchanged", "forbidden_effects",
            "lazy_factory_calls", "service_close_calls", "limitation", "environment_before",
            "environment_after", "request", "entrypoint", "loaded_origins"}, "sdk_probe_layout")
    ev.need(value["schema"] == "tgcmp-installed-sdk-error-probe/v1"
            and value["status"] == "SDK_ERROR_PREFLIGHT_PASSED" and value["acceptance"] is False
            and type(value["tool_version"]) is int and value["tool_version"] == 1
            and ev.integer(value["sdk_calls"]) == 12, "sdk_probe_contract")
    routes = ["plain", "topic", "reply", "topic_reply"]
    expected = [route + "/" + kind for route in routes for kind in ("structural", "semantic", "revision")]
    cases = ev.inventory(value["cases"])
    ev.need(value["routes"] == routes and [case["id"] for case in cases] == expected,
            "sdk_probe_census")
    for case in cases:
        ev.need(type(case) is dict and set(case) == {"id", "tool", "channel", "model_errors",
                "diagnostic_sha256", "state_unchanged"}, "sdk_probe_layout")
        route, kind = case["id"].split("/")
        reply = route in {"reply", "topic_reply"}
        tool = ("update_reply_draft" if reply else "update_draft") if kind == "revision" else (
            "prepare_reply_text_send" if reply else "prepare_text_send")
        channel = "masked" if not reply and kind != "structural" else "detailed"
        if kind == "structural":
            errors = [{"type": "list_type", "loc": ["outgoing_entities"], "msg": "Input should be a valid list"}]
        elif kind == "semantic":
            errors = [{"type": "value_error", "loc": [], "msg": "Value error, Invalid whole-scalar UTF-16 range"}]
        elif reply:
            errors = [{"type": "missing", "loc": ["text"], "msg": "Field required"}]
        else:
            errors = [{"type": "value_error", "loc": [], "msg": "Value error, exactly one of text or caption is required"}]
        ev.need(case["tool"] == tool and case["channel"] == channel
                and case["state_unchanged"] is True and ev.same_json(case["model_errors"], errors),
                "sdk_probe_contract")
        ev.digest(case["diagnostic_sha256"])
    ev.need(all(value[key] == [] for key in ("service_calls", "provider_calls", "hydration_calls",
            "owner_calls", "claims", "forbidden_effects"))
            and value["pending_and_approval_unchanged"] is True, "sdk_probe_effects")
    factories = ev.integer(value["lazy_factory_calls"])
    ev.need(factories <= 1 and ev.integer(value["service_close_calls"]) <= factories,
            "sdk_probe_effects")
    for moment in ("before", "after"):
        observation = value["environment_" + moment]
        ev.need(observation["status"] == "ENVIRONMENT_MATCHES_BASELINE"
                and observation["acceptance"] is False and observation["stage"] == "sdk"
                and observation["moment"] == moment and ev.same_json(observation["manifest"], environment),
                "sdk_probe_environment")
    entrypoint = value["entrypoint"]
    ev.need(entrypoint["path"] == str(Path(__file__).resolve().with_name("release_sdk_probe.py")),
            "sdk_probe_recipe")
    ev.verify_pin(entrypoint)
    request = ev.load_json(value["request"], immutable=True)
    ev.need(type(request) is dict and set(request) == {"schema", "runtime", "libraries",
            "implementation_pins", "environment_manifest", "expected_tools", "source_version"}
            and request["schema"] == "tgcmp-sdk-error-probe-request/v1"
            and type(request["expected_tools"]) is int and request["expected_tools"] == 43
            and type(request["source_version"]) is str and bool(request["source_version"])
            and type(request["libraries"]) is dict
            and set(request["libraries"]) == {"release_evidence", "release_environment"}
            and ev.same_json(request["environment_manifest"], environment), "sdk_probe_environment")
    baseline = ev.load_json(environment, immutable=True)
    ev.need(ev.same_json(request["runtime"], baseline["runtime"]), "sdk_probe_environment")
    allowed = {pin["path"] for rows in baseline["files"].values() for pin in rows}
    for origin in ev.inventory(value["loaded_origins"]):
        ev.need(type(origin) is dict and set(origin) == {"module", "path"}
                and type(origin["module"]) is str and origin["module"]
                and origin["path"] in allowed, "sdk_probe_origins")
    ev.verify_pins(list(request["libraries"].values()) + request["implementation_pins"])
    return row


def preflight(manifest: Path, *, expected_sha256: str | None = None) -> dict:
    try:
        if expected_sha256 is None:
            data = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        else:
            if ev is None:
                raise PreflightError("module_context_required_for_pinned_manifest")
            row = ev.current_pin(manifest)
            ev.need(row["sha256"] == ev.digest(expected_sha256), "pin_drift")
            data = ev.load_json(row, immutable=True)
        if type(data["schema"]) is not int or data["schema"] not in {1, 2}:
            raise PreflightError("manifest_schema")
        extras = {}
        if data["schema"] == 2:
            if ev is None:
                raise PreflightError("module_context_required_for_v2")
            from . import release_environment
            ev.need(expected_sha256 is not None, "manifest_pin_required")
            ev.need(set(data) == {"schema", "source_root", "source_files", "harness_files", "runtime",
                "socket_path", "chain_environment", "stage", "moment", "sdk_probe_receipt",
                "evidence_request"}, "manifest_layout")
            extras["environment"] = release_environment.check_environment(data["chain_environment"],
                stage=data["stage"], moment=data["moment"])
            if data["stage"] == "sdk":
                extras["sdk_probe"] = _sdk_probe(data["sdk_probe_receipt"], data["chain_environment"])
            else:
                ev.need(data["sdk_probe_receipt"] is None, "sdk_probe_stage")
            ev.need(data["moment"] == "before" or data["evidence_request"] is None,
                    "assembly_stage")
        check_runtime(data["runtime"])
        socket_path = data["socket_path"]
        if not Path(socket_path).is_absolute() or "\0" in socket_path:
            raise PreflightError("invalid_socket_path")
        # Conservative macOS sockaddr_un.sun_path bound, measured in bytes.
        if len(os.fsencode(socket_path)) > 103:
            raise PreflightError("socket_path_too_long")
        root = Path(data["source_root"])
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise PreflightError("invalid_source_path")
        source_count = _check_files(root, data["source_files"], "source")
        harness_count = _check_files(None, data["harness_files"], "harness")
        if data["schema"] == 2 and data["evidence_request"] is not None:
            extras["evidence_packet"] = ev.assemble(data["evidence_request"])
        return {"status": "preflight_passed", "source_files_checked": source_count,
                "harness_files_checked": harness_count, "acceptance": False, **extras}
    except PreflightError:
        raise
    except (ev.EvidenceError if ev is not None else ()) as error:
        raise PreflightError(str(error)) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PreflightError("invalid_manifest") from error


def main() -> int:
    if len(sys.argv) not in {2, 4} or (len(sys.argv) == 4 and sys.argv[2] != "--sha256"):
        print(json.dumps({"status": "preflight_failed", "reason": "manifest_argument_required"}))
        return 78
    try:
        result = preflight(Path(sys.argv[1]), expected_sha256=sys.argv[3] if len(sys.argv) == 4 else None)
    except PreflightError as error:
        print(json.dumps({"status": "preflight_failed", "reason": str(error)}))
        return 78
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
