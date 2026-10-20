from __future__ import annotations

import importlib.metadata
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# The source builds below run `python -m build --no-isolation`, so both the frontend and the backend
# must be installed; a missing one skips the class instead of failing it.
RELEASE_BUILD = ("build", "setuptools")


def distributions_available(names: tuple[str, ...] = RELEASE_BUILD) -> bool:
    # A stray ./build directory imports as a namespace package; only installed metadata counts.
    for name in names:
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            return False
    return True


class BuildDependencyProbeTests(unittest.TestCase):
    def test_stray_build_directory_is_not_the_build_distribution(self) -> None:
        code = ("import importlib.util, sys; sys.path[:0] = sys.argv[1:3]; "
                "from test_release_package import distributions_available as probe; "
                "print(importlib.util.find_spec('build') is not None, probe(('build',)))")
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "build").mkdir()
            result = subprocess.run([sys.executable, "-I", "-S", "-c", code, directory, str(Path(__file__).parent)],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.split()), (0, ["True", "False"]), result.stderr[-2000:])

    def test_a_missing_backend_or_frontend_skips_the_build_tests(self) -> None:
        real = importlib.metadata.distribution
        for missing in RELEASE_BUILD:
            def distribution(name: str, missing: str = missing):
                if name == missing:
                    raise importlib.metadata.PackageNotFoundError(name)
                return real(name)
            with self.subTest(missing=missing), patch("importlib.metadata.distribution", distribution):
                self.assertFalse(distributions_available())


@unittest.skipUnless(distributions_available(), "release build dependencies (build, setuptools) required")
class SourceDistributionTests(unittest.TestCase):
    def test_failed_atomic_publish_never_leaves_raw_owner_metadata(self) -> None:
        from setuptools import Distribution

        root = Path(__file__).resolve().parents[1]
        with patch("setuptools.setup"):
            namespace = runpy.run_path(str(root / "setup.py"))
        command = namespace["PublicSourceDistribution"](Distribution())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            (source / "example.txt").write_text("synthetic release fixture")
            destination = Path(directory) / "release"
            with patch("os.replace", side_effect=OSError("synthetic atomic publish failure")):
                with self.assertRaises(OSError):
                    command.make_archive(str(destination), "gztar", root_dir=directory, base_dir="input")
            self.assertFalse(destination.with_suffix(".tar.gz").exists())

    def test_failed_source_build_does_not_leave_an_unfiltered_archive(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", directory, str(root)],
                env={**os.environ, "SOURCE_DATE_EPOCH": "invalid"},
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(Path(directory).glob("*.tar.gz")), [])

    def test_source_archive_does_not_disclose_local_owner_metadata(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", directory, str(root)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            archives = list(Path(directory).glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0]) as archive:
                members = archive.getmembers()
                self.assertTrue(members)
                self.assertEqual({(m.uid, m.gid, m.uname, m.gname) for m in members}, {(0, 0, "", "")})
                self.assertTrue(all(not (m.issym() or m.islnk()) for m in members))
                self.assertTrue(any(m.name.endswith("/skills/telegram-search/SKILL.md") for m in members))
