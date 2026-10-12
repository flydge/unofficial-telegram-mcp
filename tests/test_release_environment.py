from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from test_release_evidence import document, file_pin


class ReleaseEnvironmentTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("telegram_search_mcp.release_environment"),
                             "shared environment baseline is missing")
        from telegram_search_mcp import release_environment
        return release_environment

    def fixture(self, root):
        files = {"source": [], "harness": [], "dependencies": [], "protected": []}
        for kind in files:
            for i in range(5 if kind == "protected" else 1):
                p = root / f"{kind}{i}.bin"
                p.write_bytes(b"fixture bytes\n")
                files[kind].append(file_pin(p))
        native = []
        for i in range(3):
            target = root / f"native{i}"
            target.mkdir()
            library = target / "library.bin"
            library.write_bytes(b"native bytes\n")
            row = file_pin(library)
            files["dependencies"].append(row)
            alias = root / f"alias{i}"
            alias.symlink_to(target.name)
            s = alias.lstat()
            native.append({"current_pin": row, "load_command": str(alias / "library.bin"),
                "opt_alias": {"path": str(alias), "target": target.name,
                              "mode": stat.S_IMODE(s.st_mode), "uid": s.st_uid, "nlink": s.st_nlink}})
        executable = Path(sys.executable)
        target = executable.resolve()
        p = file_pin(target)
        files["dependencies"].append(p)
        if executable.is_symlink():
            s = executable.lstat()
            interpreter = {"kind": "symlink", "row": {"current_pin": p,
                "load_command": str(executable), "opt_alias": {"path": str(executable),
                "target": os.readlink(executable), "mode": stat.S_IMODE(s.st_mode),
                "uid": s.st_uid, "nlink": s.st_nlink}}}
        else:
            interpreter = {"kind": "regular", "row": p}
        return {"schema": "tgcmp-chain-environment-spec/v1",
            "runtime": {"executable": sys.executable, "prefix": sys.prefix,
                "site": str(Path(sys.prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"),
                "python_version": list(sys.version_info[:3]), "packages": {}},
            "files": files, "native": native, "interpreter": interpreter,
            "links": [], "trees": [], "absent_paths": [str(root / "absent.zip")]}

    def test_explicit_capture_and_each_stage_observation_are_immutable_diagnostics(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            spec = document(root / "spec.json", self.fixture(root))
            receipt = module.capture_environment(spec, root / "baseline")
            baseline = receipt["manifest"]
            original = Path(baseline["path"]).read_bytes()
            for stage in ("fullsuite", "package", "sdk", "clients"):
                for moment in ("before", "after"):
                    result = module.check_environment(baseline, stage=stage, moment=moment)
                    self.assertEqual(result["status"], "ENVIRONMENT_MATCHES_BASELINE")
                    self.assertEqual((result["stage"], result["moment"]), (stage, moment))
                    self.assertFalse(result["acceptance"])
            self.assertEqual(Path(baseline["path"]).read_bytes(), original)
            with self.assertRaises(module.EvidenceError):
                module.capture_environment(spec, root / "baseline")

    def test_source_dependency_config_missing_library_and_link_drift_refuse_without_rebaseline(self):
        module = self.module()
        for kind in ("source", "dependencies", "protected", "missing", "alias"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                spec = self.fixture(root)
                result = module.capture_environment(document(root / "spec.json", spec), root / "baseline")
                baseline = result["manifest"]
                before = Path(baseline["path"]).read_bytes()
                if kind in spec["files"]:
                    Path(spec["files"][kind][0]["path"]).write_bytes(b"changed bytes\n")
                elif kind == "missing":
                    Path(spec["native"][0]["current_pin"]["path"]).unlink()
                else:
                    link = root / "alias0"
                    link.unlink()
                    link.symlink_to("native1")
                with self.assertRaises(module.EvidenceError):
                    module.check_environment(baseline, stage="sdk", moment="before")
                self.assertEqual(Path(baseline["path"]).read_bytes(), before)

    def test_wrong_runtime_duplicate_pin_or_new_absent_file_refuse(self):
        module = self.module()
        for kind in ("runtime", "duplicate", "absent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                spec = self.fixture(root)
                if kind == "runtime": spec["runtime"]["prefix"] = str(root)
                elif kind == "duplicate": spec["files"]["source"].append(spec["files"]["source"][0])
                else: (root / "absent.zip").write_bytes(b"unexpected")
                with self.assertRaises(module.EvidenceError):
                    module.capture_environment(document(root / "spec.json", spec), root / "refused")
                self.assertFalse((root / "refused").exists())

    def test_closed_dependency_tree_rejects_new_files_and_empty_directories(self):
        module = self.module()
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                spec = self.fixture(root)
                tree = root / "dependency-tree"
                tree.mkdir()
                p = tree / "module.py"
                p.write_bytes(b"pass\n")
                spec["files"]["dependencies"].append(file_pin(p))
                spec["trees"] = [{"path": str(tree), "directories": ["."]}]
                baseline = module.capture_environment(document(root / "spec.json", spec), root / "baseline")["manifest"]
                if kind == "file": (tree / "new.py").write_bytes(b"pass\n")
                else: (tree / "empty").mkdir()
                with self.assertRaises(module.EvidenceError):
                    module.check_environment(baseline, stage="clients", moment="before")

    def test_unreadable_new_directory_cannot_disappear_from_tree_census(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            spec = self.fixture(root)
            tree = root / "dependency-tree"
            tree.mkdir()
            spec["trees"] = [{"path": str(tree), "directories": ["."]}]
            baseline = module.capture_environment(document(root / "spec.json", spec), root / "baseline")["manifest"]
            blocked = tree / "unreadable"
            blocked.mkdir()
            (blocked / "new.py").write_bytes(b"pass\n")
            blocked.chmod(0o000)
            try:
                with self.assertRaises(module.EvidenceError):
                    module.check_environment(baseline, stage="clients", moment="before")
            finally:
                blocked.chmod(0o700)

    def test_declared_dependency_hardlink_count_stays_exact(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            spec = self.fixture(root)
            shared = root / "trusted-program"
            shared.write_bytes(b"trusted dependency\n")
            other = root / "other-link"
            os.link(shared, other)
            spec["files"]["dependencies"].append(file_pin(shared))
            try:
                baseline = module.capture_environment(document(root / "spec.json", spec), root / "baseline")["manifest"]
            except module.EvidenceError as error:
                self.fail(f"declared exact dependency hardlink count refused: {error}")
            other.unlink()
            with self.assertRaises(module.EvidenceError):
                module.check_environment(baseline, stage="fullsuite", moment="before")


if __name__ == "__main__":
    unittest.main()
