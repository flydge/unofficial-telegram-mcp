"""Explicit immutable environment baseline for a four-gate release chain.

Capture requires an already pinned spec, never silently blesses current drift.
Every check is diagnostic only; per-gate startup, before/after and independent
closure checks remain required. Configuration bytes are hashed, never reported.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys

from . import release_evidence as ev
from .release_evidence import EvidenceError

VERSION = 1
STAGES = {"fullsuite", "package", "sdk", "clients"}
SPEC_KEYS = {"schema", "runtime", "files", "native", "interpreter", "links", "trees", "absent_paths"}
KINDS = {"source", "harness", "dependencies", "protected"}


def _dependency_metadata(row):
    # System programs may have multiple declared hardlinks. This exception is
    # confined to dependencies; their exact link count remains part of the pin.
    ev.need(type(row) is dict and set(row) == ev.PIN_KEYS, "invalid_pin")
    path = ev.canonical_path(row["path"])
    ev.integer(row["bytes"])
    ev.digest(row["sha256"])
    ev.need(type(row["mode"]) is int and 0 <= row["mode"] <= 0o777, "invalid_pin")
    ev.integer(row["uid"])
    ev.integer(row["nlink"], minimum=1)
    try:
        value = path.lstat()
    except OSError as error:
        raise EvidenceError("pin_drift") from error
    ev.need(stat.S_ISREG(value.st_mode) and value.st_size == row["bytes"]
            and stat.S_IMODE(value.st_mode) == row["mode"] and value.st_uid == row["uid"]
            and value.st_nlink == row["nlink"], "pin_drift")
    return value


def _verify_dependency(row):
    before = _dependency_metadata(row)
    path = Path(row["path"])
    digest = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            ev.need(ev._identity(os.fstat(stream.fileno())) == ev._identity(before), "pin_drift")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            ev.need(ev._identity(os.fstat(stream.fileno())) == ev._identity(before), "pin_drift")
        ev.need(ev._identity(path.lstat()) == ev._identity(before)
                and digest.hexdigest() == row["sha256"], "pin_drift")
    except OSError as error:
        raise EvidenceError("pin_drift") from error


def _runtime(value):
    ev.need(type(value) is dict and set(value) == {"executable", "prefix", "site", "python_version", "packages"},
            "runtime_layout")
    ev.need(os.path.abspath(sys.executable) == value["executable"]
            and os.path.abspath(sys.prefix) == value["prefix"], "runtime_identity")
    ev._lexical_absolute(value["executable"])
    prefix = ev.canonical_path(value["prefix"])
    site = ev.canonical_path(value["site"])
    ev.need(site.is_dir() and site.is_relative_to(prefix) and sys.dont_write_bytecode,
            "runtime_identity")
    version = ev.inventory(value["python_version"])
    ev.need(len(version) == 3, "runtime_layout")
    for part in version:
        ev.integer(part)
    ev.need(version == list(sys.version_info[:3]), "python_version")
    packages = value["packages"]
    ev.need(type(packages) is dict and len(packages) <= 128, "runtime_layout")
    for name, expected in packages.items():
        ev.need(type(name) is str and name and type(expected) is str and expected, "runtime_layout")
        try:
            ev.need(importlib.metadata.version(name) == expected, "dependency_version")
        except importlib.metadata.PackageNotFoundError as error:
            raise EvidenceError("dependency_version") from error


def _links(rows):
    ev.need(type(rows) is list and len(rows) <= 4096, "link_layout")
    seen = set()
    try:
        for row in rows:
            ev.need(type(row) is dict and set(row) == {"path", "target", "resolved_target", "mode", "uid", "nlink"},
                    "link_layout")
            path = ev._lexical_absolute(row["path"])
            ev.need(str(path) not in seen and type(row["target"]) is str
                    and "\0" not in row["target"], "link_layout")
            seen.add(str(path))
            target = ev.canonical_path(row["resolved_target"])
            before = path.lstat()
            ev.need(stat.S_ISLNK(before.st_mode) and stat.S_IMODE(before.st_mode) == ev.integer(row["mode"])
                    and before.st_uid == ev.integer(row["uid"])
                    and before.st_nlink == ev.integer(row["nlink"], minimum=1)
                    and os.readlink(path) == row["target"] and path.resolve(strict=True) == target,
                    "link_drift")
            ev.need(ev._identity(path.lstat()) == ev._identity(before), "link_drift")
    except OSError as error:
        raise EvidenceError("link_drift") from error


def _trees(trees, rows, links):
    ev.need(type(trees) is list and len(trees) <= 16, "tree_layout")
    seen = set()
    for tree in trees:
        ev.need(type(tree) is dict and set(tree) == {"path", "directories"}, "tree_layout")
        root = ev.canonical_path(tree["path"])
        ev.need(root.is_dir() and str(root) not in seen, "tree_layout")
        seen.add(str(root))
        expected_dirs = ev.inventory(tree["directories"])
        ev.need(all(type(x) is str and (x == "." or (str(Path(x)) == x and not Path(x).is_absolute()
                and ".." not in Path(x).parts)) for x in expected_dirs)
                and len(set(expected_dirs)) == len(expected_dirs), "tree_layout")
        actual_dirs, actual_files, actual_links = set(), set(), set()
        def traversal_error(error):
            raise EvidenceError("tree_unreadable") from error
        for parent, directories, filenames in os.walk(root, followlinks=False, onerror=traversal_error):
            actual_dirs.add(str(Path(parent).relative_to(root)))
            for name in directories[:]:
                path = Path(parent) / name
                if path.is_symlink():
                    actual_links.add(str(path))
                    directories.remove(name)
                else:
                    ev.need(stat.S_ISDIR(path.lstat().st_mode), "tree_drift")
                    # Record the discovered entry before descending: an unreadable
                    # child must not disappear from the census.
                    actual_dirs.add(str(path.relative_to(root)))
            for name in filenames:
                path = Path(parent) / name
                if path.is_symlink(): actual_links.add(str(path))
                else:
                    ev.need(stat.S_ISREG(path.lstat().st_mode), "tree_drift")
                    actual_files.add(str(path))
            ev.need(len(actual_dirs) + len(actual_files) + len(actual_links) <= 25000, "tree_budget")
        expected_files = {r["path"] for r in rows if Path(r["path"]).is_relative_to(root)}
        expected_links = {r["path"] for r in links if Path(r["path"]).is_relative_to(root)}
        ev.need(actual_dirs == set(expected_dirs) and actual_files == expected_files
                and actual_links == expected_links, "tree_drift")


def _validate(data):
    _runtime(data["runtime"])
    files = data["files"]
    ev.need(type(files) is dict and set(files) == KINDS, "file_families")
    ev.need(all(type(value) is list and value for value in files.values())
            and len(files["protected"]) == 5, "file_families")
    rows = [row for kind in ("protected", "source", "harness", "dependencies") for row in files[kind]]
    ev.need(len(rows) <= 20000, "input_budget")
    seen, total = {}, 0
    for kind in ("protected", "source", "harness", "dependencies"):
        for row in files[kind]:
            # All large-file metadata before any input-file hash.
            (_dependency_metadata if kind == "dependencies" else ev.metadata)(row)
            path = row["path"]
            ev.need(path not in seen, "duplicate_pin")
            seen[path] = row
            total += row["bytes"]
    ev.need(total <= ev.HASH_MAX, "input_budget")
    native = data["native"]
    ev.need(type(native) is list and len(native) == 3, "native_layout")
    for row in native:
        ev.need(seen.get(row["current_pin"]["path"]) == row["current_pin"], "native_binding")
    ev.check_native_rows(native, count=3)
    interpreter = data["interpreter"]
    ev.need(type(interpreter) is dict and set(interpreter) == {"kind", "row"}, "interpreter_layout")
    if interpreter["kind"] == "symlink":
        row = interpreter["row"]
        ev.need(row["load_command"] == data["runtime"]["executable"]
                and seen.get(row["current_pin"]["path"]) == row["current_pin"], "interpreter_binding")
        ev.check_native_rows([row], count=1)
    else:
        ev.need(interpreter["kind"] == "regular"
                and interpreter["row"]["path"] == data["runtime"]["executable"]
                and seen.get(interpreter["row"]["path"]) == interpreter["row"], "interpreter_binding")
    _links(data["links"])
    absent = ev.inventory(data["absent_paths"])
    ev.need(len(set(absent)) == len(absent), "absent_layout")
    for name in absent:
        ev._lexical_absolute(name)
        ev.need(not os.path.lexists(name), "absent_path_drift")
    _trees(data["trees"], rows, data["links"])
    for kind in ("protected", "source", "harness", "dependencies"):
        for row in files[kind]:
            (_verify_dependency if kind == "dependencies" else ev.verify_pin)(row)
    return {"files": len(rows), "bytes": total,
            "families": {kind: len(value) for kind, value in files.items()},
            "native_aliases": 3, "explicit_links": len(data["links"]),
            "closed_trees": len(data["trees"])}


def _write(path, value):
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    ev.need(len(payload) <= ev.JSON_MAX, "json_budget")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    return ev.current_pin(path)


def capture_environment(spec_pin, output):
    try:
        spec = ev.load_json(spec_pin, immutable=True)
        ev.need(type(spec) is dict and set(spec) == SPEC_KEYS
                and spec["schema"] == "tgcmp-chain-environment-spec/v1", "environment_schema")
        output = ev.canonical_path(str(output))
        ev.need(not os.path.lexists(output) and output.parent.is_dir(), "namespace_exists")
        counts = _validate(spec)
        tooling = {"version": VERSION, "entrypoint": ev.current_pin(Path(__file__).resolve()),
                   "reader": ev.current_pin(Path(ev.__file__).resolve())}
        value = {**spec, "schema": "tgcmp-chain-environment/v1", "spec": spec_pin,
                 "tooling": tooling, "acceptance": False}
        ev.need(len(json.dumps(value).encode()) <= ev.JSON_MAX, "json_budget")
        ev.verify_pin(spec_pin)
        output.mkdir(mode=0o700)
        row = _write(output / "environment.json", value)
        return {"status": "ENVIRONMENT_BASELINE_CAPTURED", "acceptance": False,
                "manifest": row, "counts": counts}
    except EvidenceError:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise EvidenceError("malformed_environment") from error


def check_environment(manifest_pin, *, stage, moment):
    try:
        ev.need(stage in STAGES and moment in {"before", "after"}, "stage_identity")
        data = ev.load_json(manifest_pin, immutable=True)
        ev.need(type(data) is dict and set(data) == SPEC_KEYS | {"spec", "tooling", "acceptance"}
                and data["schema"] == "tgcmp-chain-environment/v1" and data["acceptance"] is False,
                "environment_schema")
        tooling = data["tooling"]
        ev.need(set(tooling) == {"version", "entrypoint", "reader"}
                and type(tooling["version"]) is int and tooling["version"] == VERSION
                and tooling["entrypoint"]["path"] == str(Path(__file__).resolve())
                and tooling["reader"]["path"] == str(Path(ev.__file__).resolve()), "tooling_binding")
        ev.verify_pins([tooling["entrypoint"], tooling["reader"], data["spec"]])
        counts = _validate(data)
        ev.verify_pin(manifest_pin)
        return {"schema": "tgcmp-chain-environment-observation/v1",
                "status": "ENVIRONMENT_MATCHES_BASELINE", "acceptance": False,
                "manifest": manifest_pin, "stage": stage, "moment": moment, "counts": counts,
                "limitation": "Finite observation; unchanged gate startup/postchecks and independent closure still required."}
    except EvidenceError:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise EvidenceError("malformed_environment") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capture", "check"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stage", choices=sorted(STAGES))
    parser.add_argument("--moment", choices=("before", "after"))
    args = parser.parse_args()
    try:
        row = ev.current_pin(args.input)
        ev.need(row["sha256"] == ev.digest(args.input_sha256), "pin_drift")
        if args.operation == "capture":
            ev.need(args.output is not None and args.stage is None and args.moment is None, "operation_arguments")
            result = capture_environment(row, args.output)
        else:
            ev.need(args.output is None, "operation_arguments")
            result = check_environment(row, stage=args.stage, moment=args.moment)
    except EvidenceError as error:
        print(json.dumps({"status": "ENVIRONMENT_PREFLIGHT_REFUSED", "reason": str(error), "acceptance": False}))
        return 78
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
