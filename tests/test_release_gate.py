from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import re
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

import telegram_search_mcp

TESTS = Path(__file__).resolve().parent
SOURCE = Path(telegram_search_mcp.__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_gate_under_test", TESTS.parent / "packaging" / "release_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
VERSION = "1.2.3"
EPOCH = 1700000000
GUARD = (TESTS / "test_public_tree_privacy.py").read_text(encoding="utf-8")
ALLOWED_ID = "-100" "1234567890"  # allowlisted by the guard for tests/test_schemas.py only
COUNTS = {"tests": 2, "passed": 1, "failed": 0, "errors": 0, "skipped": 1}
FIRMLINK = "/System/Volumes/Data"  # macOS: /System/Volumes/Data/private/tmp is the directory /private/tmp


def admitting(root: Path):
    """These tests live in the OS temp dir: drop only the refused temp roots that hold `root`."""
    held = gate._folded(os.path.realpath(root))
    return mock.patch.object(gate, "TMP_ROOTS", tuple(
        r for r in gate.TMP_ROOTS if not gate._under(held, gate._folded(os.path.realpath(r)))))

PROBE = '''
import os
from pathlib import Path


def test_live_policy_is_never_read():
    planted = Path(os.environ["TGMCP_PLANTED_HOME"])
    from telegram_search_mcp import config
    from telegram_search_mcp.contract import contract_descriptor, require_current
    assert not Path.home().is_relative_to(planted)
    assert not config.RUNTIME_POLICY_PATH.is_relative_to(planted)
    policy = config.load_runtime_policy()
    assert policy.expected_package_version is None
    require_current(policy)
    contract_descriptor(policy)
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", gate.SYSTEM_PATH), "HOME": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic Author",
                           "-c", "user.email=author@example.invalid", "-c", "commit.gpgsign=false",
                           "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          env=env, capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def synthetic_repo(root: Path, first: dict[str, str], second: dict[str, str]) -> tuple[Path, str, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for files, message in ((first, "first"), (second, "second")):
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        if message == "first":
            git(repo, "tag", "v0.0.1")
            previous = git(repo, "rev-parse", "HEAD")
    return repo, previous, git(repo, "rev-parse", "HEAD")


def make_sdist(path: Path, members: dict[str, bytes], *, uid: int = 0) -> None:
    prefix = f"{gate.DIST}-{VERSION}"
    names = {f"{prefix}/{name}" for name in members}
    directories = sorted({str(parent) for name in names for parent in PurePosixPath(name).parents if str(parent) != "."})
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=EPOCH) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as tar:
            for directory in directories:
                info = tarfile.TarInfo(directory)
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, EPOCH
                tar.addfile(info)
            for name, data in sorted(members.items()):
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size, info.mtime, info.uid = len(data), EPOCH, uid
                tar.addfile(info, io.BytesIO(data))


def record_digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(path: Path, members: dict[str, bytes], *, record: dict[str, str] | None = None) -> None:
    info = f"{gate.DIST}-{VERSION}.dist-info/RECORD"
    rows = [f"{name},{(record or {}).get(name, record_digest(data))},{len(data)}" for name, data in members.items()]
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr(info, "\n".join(rows + [f"{info},,"]) + "\n")


class HermeticHomeTests(unittest.TestCase):
    def test_suite_ignores_a_mismatching_policy_in_the_original_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "home" / "Library" / "Application Support" / "TelegramSearchMCP"
            tree.mkdir(parents=True)
            os.chmod(tree, 0o700)
            policy = tree / "runtime.toml"
            policy.write_text('config_version = 1\nenabled_capabilities = ["read"]\n'
                              'expected_package_version = "0.0.0"\n')
            os.chmod(policy, 0o600)
            tests = root / "tests"
            tests.mkdir()
            shutil.copyfile(TESTS / "conftest.py", tests / "conftest.py")
            (tests / "test_probe.py").write_text(PROBE)
            env = {**os.environ, "HOME": str(root / "home"), "TGMCP_PLANTED_HOME": str(root / "home"),
                   "PYTHONPATH": str(SOURCE)}
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     "--rootdir", str(root), str(tests)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])
            self.assertIn("1 passed", result.stdout)
            self.assertEqual(sorted(p.name for p in tree.iterdir()), ["runtime.toml"])


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tgmcp-gate-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def refused(self, code: str):
        return self.assertRaisesRegex(gate.GateRefusal, f"^{code}$")

    def admit(self) -> None:
        patcher = admitting(self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class RunRefusalTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo, self.previous, self.head = synthetic_repo(self.root, {"README.md": "one\n"}, {"README.md": "two\n"})

    def test_unknown_commit_is_refused_without_echo_or_workdir(self) -> None:
        workdir = self.root / "work"
        missing = "0123456789abcdef0123456789abcdef01234567"
        result = self.main("run", "--commit", missing, "--previous-tag", "v0.0.1", "--workdir", str(workdir),
                           "--repo", str(self.repo))
        self.assertEqual(result, (1, "", "release gate refused: unknown_commit\n"))
        self.assertFalse(workdir.exists())
        for commit in ("HEAD", "-x" + "0" * 38, "g" * 40):
            self.assertEqual(self.main("run", "--commit=" + commit, "--previous-tag", "v0.0.1", "--workdir",
                                       str(workdir), "--repo", str(self.repo))[2], "release gate refused: invalid_commit\n")
        for tag, code in (("v9.9.9", "unknown_tag"), ("main", "invalid_tag")):
            self.assertEqual(self.main("run", "--commit", self.head, "--previous-tag", tag, "--workdir",
                                       str(workdir), "--repo", str(self.repo))[2], f"release gate refused: {code}\n")
        self.assertFalse(workdir.exists())

    def run_in(self, workdir: str) -> tuple[int, str, str]:
        return self.main("run", "--commit", self.head, "--previous-tag", "v0.0.1", "--workdir", workdir,
                         "--repo", str(self.repo))

    def test_workdir_under_any_temp_root_or_relative_is_refused(self) -> None:
        token = secrets.token_hex(8)
        refused = (f"/tmp/tgmcp-gate-{token}", f"/private/tmp/tgmcp-gate-{token}", f"/PRIVATE/TMP/tgmcp-gate-{token}",
                   f"/TMP/tgmcp-gate-{token}", f"//tmp/tgmcp-gate-{token}", f"/tmp/../tmp/tgmcp-gate-{token}",
                   f"/var/folders/zz/tgmcp-gate-{token}/T", f"/private/var/folders/zz/tgmcp-gate-{token}",
                   f"/Private/Var/Folders/tgmcp-gate-{token}")
        for workdir in refused:
            with self.subTest(workdir=workdir):
                self.assertEqual(self.run_in(workdir), (1, "", "release gate refused: workdir_under_tmp\n"))
                with self.refused("workdir_under_tmp"):
                    gate.outside_tmp(Path(workdir), None)
        for workdir in (f"tgmcp-gate-{token}", f"./tgmcp-gate-{token}/x"):
            with self.subTest(workdir=workdir):
                self.assertEqual(self.run_in(workdir), (1, "", "release gate refused: invalid_workdir\n"))
        for workdir in refused + (f"tgmcp-gate-{token}",):
            self.assertFalse(Path(workdir).exists())

    def firmlinked(self, directory: str) -> list[str]:
        """The existing `directory` through the macOS Data-volume firmlink, and its upper-case spelling
        where the volume is case-insensitive; skipped where the firmlink alias does not exist."""
        alias = FIRMLINK + directory
        if not (os.path.isdir(directory) and os.path.isdir(alias)):
            self.skipTest("no Data-volume firmlink for this directory on this machine")
        return [alias] + ([alias.upper()] if os.path.isdir(alias.upper()) else [])

    def test_firmlink_alias_of_a_temp_root_is_refused_by_identity(self) -> None:
        token = secrets.token_hex(8)
        refused = [alias + suffix for directory, suffix in (
            ("/private/tmp", f"/tgmcp-gate-{token}"), ("/private/var/folders", f"/tgmcp-gate-{token}/x"),
            (str(self.root), f"/work-{token}")) for alias in self.firmlinked(directory)]
        for workdir in refused:
            with self.subTest(workdir=workdir):
                self.assertFalse(any(gate._under(gate._folded(workdir), gate._folded(root)) for root in gate.TMP_ROOTS))
                self.assertEqual(self.run_in(workdir), (1, "", "release gate refused: workdir_under_tmp\n"))
                with self.refused("workdir_under_tmp"):
                    gate.outside_tmp(Path(workdir), None)
                self.assertFalse(os.path.lexists(workdir))
        evidence = self.firmlinked("/private/tmp")[0] + f"/tgmcp-{token}/evidence.json"
        self.assertEqual(self.main("verify", "--evidence", evidence), (1, "", "release gate refused: workdir_under_tmp\n"))

    def test_firmlink_alias_of_the_repository_is_refused_by_identity(self) -> None:
        self.admit()
        for alias in self.firmlinked(str(self.repo)):
            for workdir in (alias + "/evidence", alias + "/x/y"):
                with self.subTest(workdir=workdir):
                    self.assertFalse(gate._under(gate._folded(workdir), gate._folded(str(self.repo))))
                    self.assertEqual(self.run_in(workdir), (1, "", "release gate refused: workdir_inside_repo\n"))
        self.assertFalse((self.repo / "evidence").exists() or (self.repo / "x").exists())
        outside = self.root / "outside"
        self.assertEqual(gate.outside_tmp(outside, self.repo), outside)

    def test_workdir_inside_repo_or_not_empty_is_refused(self) -> None:
        self.admit()
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "stale.json").write_text("{}")
        cases = ((str(self.repo / "evidence"), "workdir_inside_repo"),
                 (str(self.repo / "evidence").upper(), "workdir_inside_repo"), (str(busy), "workdir_not_empty"))
        for workdir, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.run_in(workdir), (1, "", f"release gate refused: {code}\n"))
        self.assertFalse((self.repo / "evidence").exists())
        self.assertEqual(sorted(p.name for p in busy.iterdir()), ["stale.json"])

    def test_evidence_under_a_temp_root_or_relative_is_refused(self) -> None:
        token = secrets.token_hex(8)
        for path in (f"/tmp/tgmcp-{token}/evidence.json", f"/PRIVATE/TMP/tgmcp-{token}/evidence.json",
                     f"/var/folders/zz/tgmcp-{token}/evidence.json"):
            with self.subTest(path=path):
                self.assertEqual(self.main("verify", "--evidence", path),
                                 (1, "", "release gate refused: workdir_under_tmp\n"))
        self.assertEqual(self.main("verify", "--evidence", f"tgmcp-{token}/evidence.json"),
                         (1, "", "release gate refused: evidence_invalid\n"))

    def test_release_candidate_previous_tag_is_accepted(self) -> None:
        git(self.repo, "tag", "v0.0.2rc1", self.previous)
        self.assertEqual(gate.resolve_tag(gate.Git(self.repo), "v0.0.2rc1", self.head), self.previous)
        self.assertTrue(gate.TAG.fullmatch("v0.52.0rc12"))
        for tag in ("v0.0.2-rc1", "v0.0.2rc", "v0.0.2RC1", "v0.0.2rc1x", "v0.0.2a1"):
            with self.subTest(tag=tag), self.refused("invalid_tag"):
                gate.resolve_tag(gate.Git(self.repo), tag, self.head)

    def test_usage_errors_never_echo_arguments(self) -> None:
        secret = "SYNTHETIC_SECRET_SENTINEL"
        for argv in (("run",), ("run", "--commit", secret), ("verify", "--evidence"), ("publish", secret)):
            self.assertEqual(self.main(*argv), (1, "", "release gate refused: usage\n"))


class ArtifactRefusalTests(GateCase):
    def test_non_reproducible_build_is_refused(self) -> None:
        first, second = self.root / "a", self.root / "b"
        for directory, body in ((first, b"wheel-1"), (second, b"wheel-1")):
            directory.mkdir()
            (directory / "x.whl").write_bytes(body)
        gate.compare_builds(first, second)
        (second / "x.whl").write_bytes(b"wheel-2")
        with self.refused("build_not_reproducible"):
            gate.compare_builds(first, second)

    def sdist_inputs(self) -> tuple[dict[str, tuple[str, bool]], dict[str, bytes]]:
        tracked = {"README.md": b"readme\n", f"src/{gate.DIST}/__init__.py": b'__version__ = "1.2.3"\n',
                   "CLAUDE.md": b"internal\n", "docs/plans/next.md": b"internal\n",
                   "docs/plans/initial-roadmap.md": b"public plan\n", ".claude-plugin/marketplace.json": b"{}\n",
                   "plugins/telegram-local/bin/mcp-entry": b"#!/bin/sh -p\n"}
        sources = {name: (sha(data), False) for name, data in tracked.items()}
        members = {name: data for name, data in tracked.items() if gate.shipped(name)}
        members.update({name: b"generated\n" for name in gate.GENERATED})
        members["PKG-INFO"] = f"Metadata-Version: 2.4\nName: {gate.PROJECT}\nVersion: {VERSION}\n".encode()
        return sources, members

    def test_extra_sdist_member_is_refused(self) -> None:
        sources, members = self.sdist_inputs()
        path = self.root / "ok.tar.gz"
        make_sdist(path, members)
        self.assertEqual(gate.check_sdist(path, VERSION, EPOCH, sources), len(members) + 6)
        for extra in ("docs/plans/next.md", "notes.txt", "plugins/telegram-local/bin/mcp-entry",
                      ".claude-plugin/marketplace.json"):
            with self.subTest(extra=extra):
                path = self.root / f"extra-{secrets.token_hex(4)}.tar.gz"
                make_sdist(path, {**members, extra: b"unexpected\n"})
                with self.refused("sdist_member_set") as caught:
                    gate.check_sdist(path, VERSION, EPOCH, sources)
                member = f"{gate.DIST}-{VERSION}/{extra}"  # plus any parent directory only it needs
                self.assertEqual(caught.exception.detail["extra"][-1], member)
                self.assertTrue(all(member.startswith(name + "/") for name in caught.exception.detail["extra"][:-1]))

    def test_repository_only_plugin_files_are_never_expected_in_the_sdist(self) -> None:
        tracked = [path.relative_to(TESTS.parent).as_posix() for base in ("plugins", ".claude-plugin")
                   for path in (TESTS.parent / base).rglob("*") if path.is_file()]
        self.assertIn(".claude-plugin/marketplace.json", tracked)
        self.assertIn("plugins/telegram-local/bin/mcp-entry", tracked)
        self.assertFalse([name for name in tracked if gate.shipped(name)])
        for name in ("README.md", "skills/telegram-search/SKILL.md", "packaging/launcher.py",
                     "docs/plans/initial-roadmap.md"):
            self.assertTrue(gate.shipped(name), name)

    def test_sdist_owner_metadata_or_changed_content_is_refused(self) -> None:
        sources, members = self.sdist_inputs()
        path = self.root / "owner.tar.gz"
        make_sdist(path, members, uid=501)
        with self.refused("sdist_metadata"):
            gate.check_sdist(path, VERSION, EPOCH, sources)
        make_sdist(path := self.root / "content.tar.gz", {**members, "README.md": b"changed\n"})
        with self.refused("sdist_content"):
            gate.check_sdist(path, VERSION, EPOCH, sources)

    def wheel_inputs(self) -> tuple[dict, dict[str, tuple[str, bool]], dict[str, bytes]]:
        project = {"project": {"name": gate.PROJECT, "dependencies": ["mcp==2.2.0"], "license-files": ["LICENSE"],
                               "scripts": {"telegram-search-mcp": "telegram_search_mcp.server:main"}},
                   "tool": {"setuptools": {"data-files": {"share/telegram-search-mcp": ["README.md"]}}}}
        tracked = {f"src/{gate.DIST}/__init__.py": b"VALUE = 1\n", "README.md": b"readme\n", "LICENSE": b"license\n"}
        info = f"{gate.DIST}-{VERSION}.dist-info/"
        members = {f"{gate.DIST}/__init__.py": b"VALUE = 1\n",
                   f"{gate.DIST}-{VERSION}.data/data/share/telegram-search-mcp/README.md": b"readme\n",
                   info + "METADATA": f"Metadata-Version: 2.4\nName: {gate.PROJECT}\nVersion: {VERSION}\n"
                                      "Requires-Dist: mcp==2.2.0\n\n".encode(),
                   info + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                   info + "entry_points.txt": b"[console_scripts]\ntelegram-search-mcp = telegram_search_mcp.server:main\n",
                   info + "top_level.txt": b"telegram_search_mcp\n", info + "licenses/LICENSE": b"license\n"}
        return project, {name: (sha(data), False) for name, data in tracked.items()}, members

    def test_wheel_package_entry_point_and_record_mismatches_are_refused(self) -> None:
        project, sources, members = self.wheel_inputs()
        path = self.root / "ok.whl"
        make_wheel(path, members)
        self.assertEqual(gate.check_wheel(path, VERSION, sources, project), len(members) + 1)
        info = f"{gate.DIST}-{VERSION}.dist-info/"
        cases = (({**members, f"{gate.DIST}/extra.py": b"pass\n"}, None, "wheel_package_mismatch"),
                 ({**members, info + "entry_points.txt": b"[console_scripts]\nother = x:y\n"}, None,
                  "wheel_entry_points"),
                 (members, {f"{gate.DIST}/__init__.py": record_digest(b"other")}, "wheel_record"))
        for changed, record, code in cases:
            with self.subTest(code=code):
                make_wheel(path := self.root / f"{secrets.token_hex(4)}.whl", changed, record=record)
                with self.refused(code):
                    gate.check_wheel(path, VERSION, sources, project)

    def test_unsafe_archive_members_are_refused(self) -> None:
        for name, kind in (("../escape.txt", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                archive = self.root / f"{secrets.token_hex(4)}.tar"
                with tarfile.open(archive, "w") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    tar.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
                with self.refused("archive_member_unsafe"):
                    gate.extract(archive, self.root / f"tree-{secrets.token_hex(4)}", EPOCH)
        self.assertFalse((self.root / "escape.txt").exists())


class ConstraintsTests(unittest.TestCase):
    def test_tracked_constraints_pin_the_direct_dependencies_with_hashes(self) -> None:
        release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", telegram_search_mcp.__version__).group(0)  # as the gate names it
        target, pins = gate.parse_constraints(
            (TESTS.parent / "packaging" / f"constraints-{release}.txt").read_text(encoding="utf-8"))
        self.assertEqual(target, "3.14")
        self.assertTrue(all(len(digests) == 1 for _, digests in pins.values()))
        project = tomllib.loads((TESTS.parent / "pyproject.toml").read_text(encoding="utf-8"))
        for requirement in project["project"]["dependencies"]:
            name, _, version = requirement.partition("==")
            self.assertEqual(pins[gate.normalize(name)][0], version)
        self.assertNotIn(gate.PROJECT, pins)

    def test_malformed_constraints_are_refused(self) -> None:
        good = "# target: cpython-3.14 macosx-arm64\nmcp==2.2.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        self.assertEqual(gate.parse_constraints(good)[1]["mcp"][0], "2.2.0")
        for text in (good.replace(" \\\n    --hash=sha256:" + "a" * 64, ""), good + good.split("\n", 1)[1],
                     good + "# target: cpython-3.13 macosx-arm64\n", good.replace("==", ">="), "mcp==2.2.0 \\\n"):
            with self.subTest(text=text), self.assertRaisesRegex(gate.GateRefusal, "^constraints_invalid$"):
                gate.parse_constraints(text)


class PrivacyAndIdentityTests(GateCase):
    DATA_FILES = {"share/telegram-search-mcp/tests": ["tests/test_schemas.py"]}

    def scan(self, tracked: dict[str, str], wheel_members: dict[str, str] | None = None, *,
             guard: str | None = GUARD, message: str | None = None,
             steps: tuple = ()) -> tuple[dict, dict, str]:
        """The gate's privacy scan of a synthetic release whose sdist ships `tracked`; `steps` are later
        commits of (files, message), a None file being deleted, made before the release commit."""
        base = self.root / secrets.token_hex(4)
        base.mkdir()
        files = {**tracked, **({} if guard is None else {gate.PRIVACY_SOURCE: guard})}
        repo, previous, head = synthetic_repo(base, {"README.md": "public\n"}, files)
        for changes, text in steps + (() if message is None else (({}, message),)):
            for name, content in changes.items():
                if content is None:
                    (repo / name).unlink()
                else:
                    (repo / name).parent.mkdir(parents=True, exist_ok=True)
                    (repo / name).write_text(content)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "--allow-empty", "-m", text)
        head = git(repo, "rev-parse", "HEAD")
        self.repo = repo
        sdist, wheel = base / "s.tar.gz", base / "w.whl"
        make_sdist(sdist, {name: text.encode() for name, text in tracked.items()})
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, text in (wheel_members or {f"{gate.DIST}/__init__.py": "VALUE = 1\n"}).items():
                archive.writestr(name, text)
        sources = {name: (sha(text.encode()), False) for name, text in files.items()}
        report, history = gate.privacy_scan(gate.Git(repo), previous, head, repo, sources, sdist, wheel, VERSION,
                                            self.DATA_FILES)
        return report, history, head

    def test_allowlisted_value_passes_only_in_its_shipped_file(self) -> None:
        line = f"ID = {ALLOWED_ID}\n"
        data = f"{gate.DIST}-{VERSION}.data/data/share/telegram-search-mcp/tests/test_schemas.py"
        report, history, head = self.scan({"tests/test_schemas.py": line, "docs/clean.md": "public\n"}, {data: line})
        gate.check_privacy(report)
        self.assertEqual(history, {"commits": 1, "files": 3, "lines": len(GUARD.splitlines()) + 2, "total": 0,
                                   "findings": []})
        self.assertEqual((report["guard"], report["guard_sha256"], report["hits"]),
                         (gate.PRIVACY_SOURCE, sha(GUARD.encode()), []))
        self.assertIn("chat_id", report["rules"])
        self.assertGreater(report["allow"], 0)
        self.assertEqual(report["scanned"], {"sdist_members": 5, "wheel_members": 1, "diff_files": 3,
                                             "diff_lines": len(GUARD.splitlines()) + 2, "commits": 1})
        report, _, _ = self.scan({"tests/test_schemas.py": line, "docs/other.md": "fine\n" + line},
                                 {data: line, f"{gate.DIST}/leak.py": line})
        self.assertEqual(report["hits"], ["diff/docs/other.md:2:chat_id", "sdist/docs/other.md:2:chat_id",
                                          f"wheel/src/{gate.DIST}/leak.py:1:chat_id"])
        with self.refused("privacy_hit") as caught:
            gate.check_privacy(report)
        self.assertEqual(caught.exception.detail, {"hits": report["hits"], "total": 3})
        self.assertNotIn(ALLOWED_ID.removeprefix("-"), json.dumps(caught.exception.detail) + json.dumps(report))

    def test_commit_message_hit_refuses(self) -> None:
        report, _, head = self.scan({"docs/clean.md": "public\n"}, message="Fix the reply\n\nseen in msg 6312" "42752\n")
        self.assertEqual((report["scanned"]["commits"], report["hits"]), (2, [f"commit/{head}:3:message_id"]))
        with self.refused("privacy_hit"):
            gate.check_privacy(report)

    def test_paths_with_spaces_and_quotes_are_parsed(self) -> None:
        names = ("docs/a b.md", 'docs/q"uote.md', 'docs/q" b.md', "docs/tab\tname.md", "docs/\u00fc.md")
        report, history, _ = self.scan({name: "public\n" for name in names})
        gate.check_privacy(report)
        self.assertEqual((report["scanned"]["diff_files"], report["scanned"]["diff_lines"], history["total"]),
                         (len(names) + 1, len(names) + len(GUARD.splitlines()), 0))
        report, history, head = self.scan({name: f"fine\nID = {ALLOWED_ID}\n" for name in names})
        self.assertEqual(report["hits"], sorted(f"{where}/{name}:2:chat_id" for where in ("diff", "sdist")
                                                for name in names))
        self.assertEqual(history["findings"], sorted(f"{head[:12]}:{name}:2:chat_id" for name in names))

    def test_git_quoted_paths_decode_or_refuse(self) -> None:
        self.assertEqual(gate._git_path(b'"docs/q\\"b \\303\\274\\t\\\\.md"'), 'docs/q"b \u00fc\t\\.md')
        self.assertEqual(gate._git_path(b"docs/a b.md"), "docs/a b.md")
        for raw in (b'"docs/open', b'"bad \\x"', b'"short \\30"', b'"in"ner"', b"\xff.md"):
            with self.subTest(raw=raw), self.refused("privacy_scan_bounds"):
                gate._git_path(raw)

    def test_history_findings_are_a_report_not_a_refusal(self) -> None:
        named = "docs/tg-" "dev-7.md"  # its name is a local_dir finding
        hit = f"fine\nID = {ALLOWED_ID}\n"
        report, history, head = self.scan({"docs/clean.md": "public\n"}, steps=(
            ({"docs/old notes.md": hit, named: "x\n"}, "add notes"),
            ({"docs/old notes.md": None, named: None}, "drop notes")))
        gate.check_privacy(report)
        added = git(self.repo, "rev-parse", "HEAD~1")[:12]
        shown = "name-sha256-" + sha(named.encode())[:16]
        self.assertEqual(history, {"commits": 3, "files": 4, "lines": len(GUARD.splitlines()) + 4, "total": 2,
                                   "findings": sorted([f"{added}:docs/old notes.md:2:chat_id",
                                                       f"{added}:{shown}:0:local_dir"])})
        self.assertNotIn(ALLOWED_ID.removeprefix("-"), json.dumps(history))
        self.assertNotIn("dev-7", json.dumps(history))
        # at the tip, a path that is itself a finding refuses and is shown only by its digest
        report, _, _ = self.scan({named: "x\n"})
        self.assertEqual(report["hits"], [f"diff/{shown}:0:local_dir", f"sdist/{shown}:0:local_dir"])
        self.assertNotIn("dev-7", json.dumps(report))

    def test_history_scans_what_a_merge_resolution_added(self) -> None:
        tree = self.root / "guard"
        (tree / "tests").mkdir(parents=True)
        (tree / gate.PRIVACY_SOURCE).write_text(GUARD)
        guard = gate.load_guard(tree, {gate.PRIVACY_SOURCE: (sha(GUARD.encode()), False)})
        repo, previous, _ = synthetic_repo(self.root, {"README.md": "one\n"}, {"README.md": "one\nmain\n"})
        git(repo, "checkout", "-q", "-b", "side", previous)
        (repo / "README.md").write_text("one\nside\n")
        git(repo, "commit", "-qam", "side")
        git(repo, "checkout", "-q", "main")
        with contextlib.suppress(subprocess.CalledProcessError):
            git(repo, "merge", "-q", "side", "-m", "merge")
        (repo / "README.md").write_text(f"one\nID = {ALLOWED_ID}\n")
        git(repo, "commit", "-qam", "merge")
        merge = git(repo, "rev-parse", "HEAD")
        history = gate.history_scan(gate.Git(repo), previous, merge, guard, ())
        self.assertEqual((history["commits"], history["findings"]), (3, [f"{merge[:12]}:README.md:2:chat_id"]))

    def test_committed_attributes_cannot_hide_added_lines(self) -> None:
        hit = f"fine\nID = {ALLOWED_ID}\n"
        report, history, head = self.scan({".gitattributes": "*.md -diff binary\n", "docs/notes.md": hit})
        self.assertIn("diff/docs/notes.md:2:chat_id", report["hits"])
        self.assertIn(f"{head[:12]}:docs/notes.md:2:chat_id", history["findings"])

    def test_octopus_merge_refuses_the_history_scan(self) -> None:
        tree = self.root / "guard"
        (tree / "tests").mkdir(parents=True)
        (tree / gate.PRIVACY_SOURCE).write_text(GUARD)
        guard = gate.load_guard(tree, {gate.PRIVACY_SOURCE: (sha(GUARD.encode()), False)})
        repo, previous, _ = synthetic_repo(self.root, {"README.md": "one\n"}, {"README.md": "one\nmain\n"})
        for branch in ("x", "y"):
            git(repo, "checkout", "-q", "-b", branch, previous)
            (repo / f"{branch}.txt").write_text(branch + "\n")
            git(repo, "add", f"{branch}.txt")
            git(repo, "commit", "-qm", branch)
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-edit", "x", "y")
        with self.refused("privacy_scan_bounds"):
            gate.history_scan(gate.Git(repo), previous, git(repo, "rev-parse", "HEAD"), guard, ())

    def test_missing_or_broken_privacy_guard_refuses(self) -> None:
        guards = (None, "ALLOW = ()\n", "def scan_text(path, text, literals=()):\n    return []\n",
                  "raise SystemExit(3)\n", "ALLOW = ()\ndef scan_text(path, text, literals=()):\n    return None\n")
        for guard in guards:
            with self.subTest(guard=guard), self.refused("privacy_guard_missing"):
                self.scan({"docs/clean.md": "public\n"}, guard=guard)

    def test_identities_are_a_classified_report(self) -> None:
        repo, previous, head = synthetic_repo(self.root, {"a.txt": "1\n"}, {"a.txt": "2\n"})
        (repo / "a.txt").write_text("3\n")
        git(repo, "commit", "-qam", "third\n\nCo-Authored-By: Helper <noreply@example.invalid>")
        report = gate.identities(gate.Git(repo), previous, git(repo, "rev-parse", "HEAD"))
        self.assertEqual(report["commits"], 2)
        self.assertEqual(report["identities"], [
            {"role": "author", "name": "Synthetic Author", "email": "author@example.invalid", "class": "direct",
             "commits": 2},
            {"role": "co_author", "name": "Helper", "email": "noreply@example.invalid", "class": "noreply",
             "commits": 1},
            {"role": "committer", "name": "Synthetic Author", "email": "author@example.invalid", "class": "direct",
             "commits": 2}])


class SmokeAndSuiteTests(GateCase):
    def test_tool_count_mismatch_is_refused(self) -> None:
        source = {"version": VERSION, "tools": ["_manifest", "read", "send"], "capabilities": ["read"],
                  "schema_fingerprint": "f" * 64}
        observed = {"server_name": gate.PROJECT, "server_version": VERSION, "tools": ["_manifest", "read", "send"],
                    "manifest_error": False, "manifest_package_version": VERSION,
                    "manifest_schema_fingerprint": "f" * 64, "manifest_capabilities": ["read"]}
        self.assertEqual(gate.check_smoke(source, observed, VERSION)["tool_count"], 3)
        cases = (({"tools": ["_manifest", "read"]}, "tool_census_mismatch"),
                 ({"server_version": "0.0.0"}, "smoke_version_mismatch"),
                 ({"manifest_schema_fingerprint": "0" * 64}, "smoke_manifest_mismatch"),
                 ({"manifest_capabilities": []}, "smoke_policy_mismatch"))
        for change, code in cases:
            with self.subTest(code=code), self.refused(code):
                gate.check_smoke(source, {**observed, **change}, VERSION)

    def suite(self, body: str) -> tuple[dict, int]:
        tree = self.root / f"tree-{secrets.token_hex(4)}"
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "test_sample.py").write_text(body)
        junit, log = tree / "junit.xml", tree / "suite.log"
        returncode = gate.run_suite(Path(sys.executable), tree, junit, log, dict(os.environ))
        return gate.suite_report(junit, log), returncode

    def test_any_failed_test_is_refused_and_skip_reasons_are_recorded(self) -> None:
        body = ("import unittest\nclass Sample(unittest.TestCase):\n    def test_ok(self): pass\n"
                "    @unittest.skip('synthetic reason')\n    def test_skip(self): pass\n")
        report, returncode = self.suite(body)
        gate.check_tests(report, returncode)
        self.assertEqual({k: report[k] for k in ("tests", "passed", "failed", "errors", "skipped")},
                         {"tests": 2, "passed": 1, "failed": 0, "errors": 0, "skipped": 1})
        self.assertEqual(report["skips"][0]["reason"], "synthetic reason")
        report, returncode = self.suite(body + "    def test_fail(self): self.fail('synthetic failure')\n")
        self.assertEqual((report["failed"], returncode), (1, 1))
        with self.refused("tests_failed"):
            gate.check_tests(report, returncode)


class VerifyTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.admit()
        self.rerun = {**COUNTS, "returncode": 0}
        self.repo = synthetic_repo(self.root, {"README.md": "one\n"}, {"README.md": "two\n"})[0]
        self.base = self.root / "evidence"
        (self.base / "dist").mkdir(parents=True)
        self.sdist = self.base / "dist" / f"{gate.DIST}-{VERSION}.tar.gz"
        self.wheel = self.base / "dist" / f"{gate.DIST}-{VERSION}-py3-none-any.whl"
        self.sdist.write_bytes(b"sdist")
        self.wheel.write_bytes(b"wheel")
        (self.base / "junit.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="2">'
            '<testcase classname="tests.test_x.T" name="test_a"/><testcase classname="tests.test_x.T" name="test_b">'
            '<skipped type="pytest.skip" message="synthetic reason"/></testcase></testsuite></testsuites>')
        (self.base / "suite.log").write_text("1 passed, 1 skipped in 0.01s\n")
        self.derived = {
            "schema": gate.SCHEMA, "commit": "a" * 40, "tree": "b" * 40, "commit_time": EPOCH,
            "previous_tag": "v0.0.1", "previous_commit": "c" * 40, "version": VERSION,
            "gate": {"sha256": gate.own_sha256(), "at_commit_sha256": None},
            "toolchain": {"python": "3.14.0", "implementation": "cpython", "platform": "Darwin-arm64", "pip": "1",
                          "tools": [list(tool) for tool in gate.TOOLS]},
            "archive": {"sha256": "d" * 64, "files": 3},
            "sdist": {"file": f"dist/{self.sdist.name}", "sha256": sha(b"sdist"), "bytes": 5, "members": 9},
            "wheel": {"file": f"dist/{self.wheel.name}", "sha256": sha(b"wheel"), "bytes": 5, "members": 8},
            "constraints": {"file": "packaging/constraints-1.2.3.txt", "sha256": "e" * 64, "target": "3.14",
                            "distributions": 1, "wheels": [["mcp-2.2.0-py3-none-any.whl", "f" * 64]]},
            "install": {"freeze": ["mcp==2.2.0", f"{gate.PROJECT}=={VERSION}"], "pip_check": "clean",
                        "record_entries": 10},
            "smoke": {"server_version": VERSION, "tool_count": 55, "tools_sha256": "0" * 64,
                      "schema_fingerprint": "1" * 64, "capabilities": 33},
            "privacy": {"guard": gate.PRIVACY_SOURCE, "guard_sha256": "2" * 64, "rules": ["chat_id", "email"],
                        "allow": 1, "hits": [], "scanned": {"sdist_members": 9, "wheel_members": 8, "diff_files": 1,
                                                            "diff_lines": 1, "commits": 1}},
            "history_findings": {"commits": 1, "files": 1, "lines": 1, "total": 1,
                                 "findings": ["aaaaaaaaaaaa:docs/x.md:1:email"]},
            "identities": {"commits": 1, "identities": []}}
        self.evidence = {**copy.deepcopy(self.derived),
                         "tests": gate.suite_report(self.base / "junit.xml", self.base / "suite.log")}
        self.path = self.base / "evidence.json"

    def fake_derive(self, git, commit, tag, base, scratch, *, online):
        self.assertEqual((commit, tag, base, online), (self.derived["commit"], "v0.0.1", self.base, False))
        return {**copy.deepcopy(self.derived), "_artifacts": (self.sdist, self.wheel, None, None)}

    def fake_rerun(self, artifacts, evidence, base, scratch):
        self.assertEqual((artifacts[:2], evidence["commit"], base), ((self.sdist, self.wheel), "a" * 40, self.base))
        self.assertTrue(scratch.is_relative_to(self.base))
        return dict(self.rerun)

    def verify(self, evidence: dict) -> int:
        self.path.unlink(missing_ok=True)
        gate.write_evidence(self.path, evidence)
        args = argparse.Namespace(evidence=self.path, repo=self.repo, delegated=False)
        try:
            return gate.verify(args, derive_evidence=self.fake_derive, rerun=self.fake_rerun)
        finally:
            self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                             ["dist", "evidence.json", "junit.xml", "suite.log"])

    def test_untouched_evidence_verifies(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.verify(self.evidence), 0)
        self.assertEqual(out.getvalue(), "release gate verified\n")
        self.assertEqual(self.evidence["tests"]["skips"],
                         [{"test": "tests.test_x.T::test_b", "reason": "synthetic reason"}])

    def test_any_edit_to_the_evidence_is_refused(self) -> None:
        def edit(path: tuple, value):
            changed = copy.deepcopy(self.evidence)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return changed
        cases = [(edit(("sdist", "sha256"), "0" * 64), "evidence_mismatch"),
                 (edit(("wheel", "members"), 7), "evidence_mismatch"),
                 (edit(("tests", "passed"), 2), "evidence_mismatch"),
                 (edit(("tests", "skips"), []), "evidence_mismatch"),
                 (edit(("smoke", "tool_count"), 54), "evidence_mismatch"),
                 (edit(("privacy", "hits"), ["diff/x:1:email"]), "evidence_mismatch"),
                 (edit(("privacy", "guard_sha256"), "3" * 64), "evidence_mismatch"),
                 (edit(("install", "freeze"), ["mcp==2.2.1", f"{gate.PROJECT}=={VERSION}"]), "evidence_mismatch"),
                 (edit(("identities", "commits"), 2), "evidence_mismatch"),
                 (edit(("history_findings", "findings"), []), "evidence_mismatch"),
                 (edit(("tree",), "9" * 40), "evidence_mismatch"),
                 (edit(("toolchain", "python"), "3.14.1"), "toolchain_mismatch"),
                 (edit(("gate", "sha256"), "2" * 64), "gate_mismatch"),
                 (edit(("schema",), "tgmcp-release-gate/v0"), "evidence_invalid"),
                 ({**self.evidence, "note": "hand edited"}, "evidence_invalid")]
        for evidence, code in cases:
            with self.subTest(code=code), self.refused(code):
                self.verify(evidence)

    def test_formatting_only_edit_is_refused(self) -> None:
        gate.write_evidence(self.path, self.evidence)
        self.path.write_text(json.dumps(self.evidence, indent=2, sort_keys=True) + "\n")
        args = argparse.Namespace(evidence=self.path, repo=self.repo, delegated=False)
        with self.refused("evidence_invalid"):
            gate.verify(args, derive_evidence=self.fake_derive, rerun=self.fake_rerun)

    def test_rerun_suite_counts_must_match_the_evidence(self) -> None:
        for change in ({"passed": 0, "failed": 1}, {"passed": 2, "skipped": 0}, {"tests": 3, "passed": 2},
                       {"errors": 1}, {"returncode": 1}):
            self.rerun = {**COUNTS, "returncode": 0, **change}
            with self.subTest(change=change), self.refused("evidence_mismatch") as caught:
                self.verify(self.evidence)
            self.assertEqual(caught.exception.detail, "tests")
        self.rerun = {**COUNTS, "returncode": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.verify(self.evidence), 0)

    def test_changed_artifact_or_junit_on_disk_is_refused(self) -> None:
        self.wheel.write_bytes(b"other wheel")
        with self.refused("evidence_mismatch"):
            self.verify(self.evidence)
        self.wheel.write_bytes(b"wheel")
        (self.base / "junit.xml").write_text((self.base / "junit.xml").read_text().replace('message="synthetic reason"',
                                                                                         'message="edited"'))
        with self.refused("evidence_mismatch"):
            self.verify(self.evidence)
