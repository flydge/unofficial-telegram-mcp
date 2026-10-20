#!/usr/bin/env python3
"""Deterministic release gate that writes machine-checkable evidence (stdlib only).

  python packaging/release_gate.py run --commit <sha> --previous-tag v0.51.0 --workdir <dir>
  python packaging/release_gate.py verify --evidence <dir>/evidence.json

run builds only from `git archive <commit>`, never from a working tree, into a
fresh workdir outside /tmp and outside the repository, and writes
<workdir>/evidence.json beside the artifacts. Its only network use is pip
downloading the pinned, hash-checked wheels (TOOLS below and
packaging/constraints-<release>.txt at the commit) into the workdir.

verify binds to the recorded commit, never to HEAD: it rebuilds from a fresh
`git archive <commit>` offline from the recorded wheelhouses, re-derives every
recorded field (tests from the junit and log files on disk) and fails on any
difference. Evidence made by another gate version is verified by the gate
stored at the recorded commit when its hash matches.

Both commands take --repo (default: the repository of the current directory).
Refusals print one fixed code and never echo inputs; run keeps the details in
<workdir>/refusal.json.
"""
from __future__ import annotations

import argparse
import ast
import base64
import configparser
import csv
import email.parser
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
import zipfile

SCHEMA = "tgmcp-release-gate/v1"
PROJECT = "telegram-search-mcp"
DIST = "telegram_search_mcp"
GATE_PATH = "packaging/release_gate.py"
PRIVACY_SOURCE = "tests/test_public_tree_privacy.py"
# Pure-Python wheels: (normalized name, version, sha256). The build venv uses BUILD_TOOLS.
TOOLS = (
    ("build", "1.3.0", "7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4"),
    ("iniconfig", "2.3.1", "9121e2c1fdb355232495be3194c8dfe87ccc2d5dee45947b78e68f499790d7a7"),
    ("packaging", "26.3", "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"),
    ("pluggy", "1.6.0", "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746"),
    ("pygments", "2.21.0", "2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9"),
    ("pyproject-hooks", "1.2.0", "9e5c6bfa8dcc30091c74b0cf803c81fdd29d94f01992a7707bc97babb1141913"),
    ("pytest", "9.1.1", "37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c"),
    ("setuptools", "80.9.0", "062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922"),
    ("tomlkit", "0.13.3", "c89c649d79ee40629a9fda55f8ace8c6a1b42deb912b2a8fd8d942ddadb606b0"),
)
BUILD_TOOLS = frozenset({"build", "packaging", "pyproject-hooks", "setuptools"})
# Tracked but never shipped: the sdist must hold exactly the other tracked files plus GENERATED.
INTERNAL = ("CLAUDE.md", "docs/plans/*")
SHIPPED_INTERNAL = frozenset({"docs/plans/initial-roadmap.md"})
GENERATED = ("PKG-INFO", "setup.cfg") + tuple(f"src/{DIST}.egg-info/{name}" for name in (
    "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt"))
DIST_INFO = ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")
TMP_ROOTS = (Path("/tmp"), Path("/private/tmp"))
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Downloads are hash-checked, so a proxy or CA bundle cannot change what is installed.
NETWORK_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE")
MAX_MEMBERS = 5000
MAX_MEMBER_BYTES = 64 << 20
MAX_FILE_BYTES = 1 << 30
MAX_OUTPUT_BYTES = 64 << 20
MAX_EVIDENCE_BYTES = 4 << 20
MAX_COMMITS = 100000
GIT_TIMEOUT, PIP_TIMEOUT, BUILD_TIMEOUT, SMOKE_TIMEOUT, SUITE_TIMEOUT = 120, 900, 900, 180, 3600
COMMIT = re.compile(r"[0-9a-f]{7,40}")
TAG = re.compile(r"v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
VERSION = re.compile(r'^__version__ = "([0-9]+(?:\.[0-9]+){1,3}(?:(?:a|b|rc)[0-9]+)?'
                     r'(?:\.post[0-9]+)?(?:\.dev[0-9]+)?)"$', re.M)
PIN = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)==([0-9A-Za-z.+!]+)((?: --hash=sha256:[0-9a-f]{64})+)")
TARGET = re.compile(r"# target: cpython-(3\.[0-9]{1,2}) macosx-arm64")
EVIDENCE_KEYS = frozenset({"schema", "gate", "commit", "tree", "commit_time", "previous_tag", "previous_commit",
    "version", "toolchain", "archive", "sdist", "wheel", "constraints", "install", "smoke", "privacy",
    "identities", "tests"})
CODES = frozenset({
    "usage", "repo_unavailable", "git_failed", "invalid_commit", "unknown_commit", "invalid_tag", "unknown_tag",
    "tag_not_ancestor", "invalid_workdir", "workdir_under_tmp", "workdir_inside_repo", "workdir_not_empty",
    "archive_not_reproducible", "archive_member_unsafe", "archive_incomplete", "version_unreadable",
    "project_unreadable", "constraints_missing", "constraints_invalid", "interpreter_mismatch",
    "toolchain_failed", "download_failed", "wheelhouse_mismatch", "build_failed", "build_not_reproducible",
    "sdist_metadata", "sdist_member_set", "sdist_content", "wheel_member_set", "wheel_package_mismatch",
    "wheel_data_mismatch", "wheel_metadata", "wheel_entry_points", "wheel_record", "install_failed",
    "pip_check_failed", "freeze_mismatch", "record_mismatch", "census_failed", "smoke_failed",
    "smoke_version_mismatch", "tool_census_mismatch", "smoke_manifest_mismatch", "smoke_policy_mismatch",
    "suite_failed", "suite_unreadable", "tests_failed", "privacy_patterns_invalid", "privacy_scan_bounds",
    "privacy_hit", "evidence_invalid", "evidence_mismatch", "toolchain_mismatch", "gate_mismatch"})

CENSUS = r'''
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import telegram_search_mcp
from telegram_search_mcp.config import KNOWN_CAPABILITIES, RuntimePolicy
from telegram_search_mcp.contract import schema_document, schema_fingerprint
from telegram_search_mcp.server import build_server
if not Path(telegram_search_mcp.__file__).resolve().is_relative_to(root):
    raise SystemExit(3)
capabilities = sorted(KNOWN_CAPABILITIES)
document = schema_document(build_server(policy=RuntimePolicy(enabled_capabilities=tuple(capabilities))))
print(json.dumps({"version": telegram_search_mcp.__version__, "capabilities": capabilities,
                  "tools": sorted(tool["name"] for tool in document["tools"]),
                  "schema_fingerprint": schema_fingerprint()}))
'''

SMOKE = r'''
import asyncio, json, sys
from mcp import Client, StdioServerParameters


async def main():
    params = StdioServerParameters(command=sys.argv[1], args=[], env={"HOME": sys.argv[2]}, cwd=sys.argv[2])
    async with Client(params, read_timeout_seconds=60) as client:
        info = client.server_info
        names, cursor = [], None
        for _ in range(64):
            page = await client.list_tools(cursor=cursor)
            names.extend(tool.name for tool in page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
        else:
            raise SystemExit(4)
        manifest = await client.call_tool("_manifest", {}, read_timeout_seconds=60)
        content = manifest.structured_content if isinstance(manifest.structured_content, dict) else {}
        return {"server_name": getattr(info, "name", None), "server_version": getattr(info, "version", None),
                "tools": sorted(names), "manifest_error": manifest.is_error,
                "manifest_package_version": content.get("package_version"),
                "manifest_schema_fingerprint": content.get("schema_fingerprint"),
                "manifest_capabilities": content.get("enabled_capabilities")}

print(json.dumps(asyncio.run(asyncio.wait_for(main(), 120))))
'''


class GateRefusal(Exception):
    """A fixed refusal code; detail holds repository-derived facts, never caller input."""

    def __init__(self, code: str, detail: object = None):
        if code not in CODES:
            raise AssertionError("unknown refusal code")
        super().__init__(code)
        self.code = code
        self.detail = detail


def need(condition: object, code: str, detail: object = None) -> None:
    if not condition:
        raise GateRefusal(code, detail)


def _tail(data: bytes) -> str:
    return data[-4000:].decode("utf-8", "replace")


def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def sha256_file(path: Path, code: str = "evidence_mismatch") -> str:
    try:
        metadata = path.lstat()
        need(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_FILE_BYTES, code)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        raise GateRefusal(code) from None
    return digest.hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _run(command: list, code: str, *, timeout: int, env: dict, cwd: Path | None = None,
         stdout: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        if stdout is None:
            result = subprocess.run([str(part) for part in command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=timeout)
        else:
            fd = os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                result = subprocess.run([str(part) for part in command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=stream, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise GateRefusal(code, {"failure": "spawn_or_timeout"}) from None
    if check:
        need(result.returncode == 0, code, {"returncode": result.returncode, "stdout": _tail(result.stdout or b""),
                                            "stderr": _tail(result.stderr or b"")})
    need(len(result.stdout or b"") <= MAX_OUTPUT_BYTES, code)
    return result


class Git:
    """Git with system/global config and attributes neutralized and a fixed tar umask."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.executable = shutil.which("git")
        need(self.executable, "repo_unavailable")
        self.env = {"PATH": SYSTEM_PATH, "HOME": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                    "LC_ALL": "C"}

    def __call__(self, *args: str, code: str = "git_failed", check: bool = True, stdout: Path | None = None):
        return _run([self.executable, "-C", self.repo, "-c", "core.quotepath=false", "-c", "tar.umask=0022",
                     "-c", "core.attributesFile=" + os.devnull, *args], code, timeout=GIT_TIMEOUT, env=self.env,
                    stdout=stdout, check=check)

    def text(self, *args: str, code: str = "git_failed") -> str:
        try:
            return self(*args, code=code).stdout.decode("utf-8").strip()
        except UnicodeError:
            raise GateRefusal(code) from None


def toplevel(path: Path) -> Path:
    try:
        git = Git(Path(os.path.abspath(path)))
        result = git("rev-parse", "--show-toplevel", code="repo_unavailable")
        return Path(result.stdout.decode("utf-8").strip()).resolve()
    except UnicodeError:
        raise GateRefusal("repo_unavailable") from None


def resolve_commit(git: Git, value: str) -> str:
    need(COMMIT.fullmatch(value), "invalid_commit")
    result = git("rev-parse", "--verify", "--quiet", value + "^{commit}", code="unknown_commit", check=False)
    full = (result.stdout or b"").decode("ascii", "replace").strip()
    need(result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", full) and full.startswith(value), "unknown_commit")
    return full


def resolve_tag(git: Git, tag: str, commit: str) -> str:
    need(TAG.fullmatch(tag), "invalid_tag")
    result = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", code="unknown_tag", check=False)
    previous = (result.stdout or b"").decode("ascii", "replace").strip()
    need(result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", previous), "unknown_tag")
    ancestor = git("merge-base", "--is-ancestor", previous, commit, code="tag_not_ancestor", check=False)
    need(ancestor.returncode == 0, "tag_not_ancestor")
    return previous


def outside_tmp(path: Path, repo: Path | None, code: str = "invalid_workdir") -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = absolute.resolve()
    except (OSError, RuntimeError, ValueError):
        raise GateRefusal(code) from None
    for candidate in (absolute, resolved):
        need(not any(candidate.is_relative_to(root) for root in TMP_ROOTS), "workdir_under_tmp")
    need(repo is None or not resolved.is_relative_to(repo), "workdir_inside_repo")
    return resolved


def prepare_workdir(path: Path, repo: Path) -> Path:
    workdir = outside_tmp(path, repo)
    if workdir.exists() or workdir.is_symlink():
        need(workdir.is_dir() and not workdir.is_symlink() and not any(workdir.iterdir()), "workdir_not_empty")
        os.chmod(workdir, 0o700)
    else:
        try:
            workdir.mkdir(mode=0o700, parents=True)
        except OSError:
            raise GateRefusal("invalid_workdir") from None
    return workdir


def tracked_files(git: Git, commit: str) -> dict[str, bool]:
    """Tracked path -> executable bit; only regular blobs are releasable."""
    files = {}
    for entry in git("ls-tree", "-r", "-z", "--full-tree", commit).stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, raw = entry.partition(b"\t")
        mode, kind, _ = meta.decode("ascii").split(" ")
        try:
            path = raw.decode("utf-8")
        except UnicodeError:
            raise GateRefusal("archive_member_unsafe") from None
        need(kind == "blob" and mode in ("100644", "100755"), "archive_member_unsafe", {"path": path})
        files[path] = mode == "100755"
        need(len(files) <= MAX_MEMBERS, "archive_member_unsafe")
    return files


def _safe_name(name: str) -> bool:
    pure = PurePosixPath(name)
    return bool(name) and str(pure) == name and not pure.is_absolute() and ".." not in pure.parts and "\\" not in name


def extract(archive: Path, target: Path, epoch: int) -> dict[str, tuple[str, bool]]:
    """Extract only regular files and directories with fixed modes; return path -> (sha256, executable)."""
    _private_dir(target)
    files: dict[str, tuple[str, bool]] = {}
    total = 0
    try:
        with tarfile.open(archive, "r:") as tar:
            for count, member in enumerate(tar, 1):
                need(count <= MAX_MEMBERS and _safe_name(member.name), "archive_member_unsafe")
                pure = PurePosixPath(member.name)
                if member.isdir():
                    (target / pure).mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                need(member.isfile() and member.size <= MAX_MEMBER_BYTES and member.name not in files,
                     "archive_member_unsafe", {"path": member.name})
                total += member.size
                need(total <= MAX_FILE_BYTES, "archive_member_unsafe")
                with tar.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                need(len(data) == member.size, "archive_member_unsafe")
                path = target / pure
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                executable = bool(member.mode & 0o100)
                _write_new(path, data, 0o755 if executable else 0o644)
                os.chmod(path, 0o755 if executable else 0o644)
                os.utime(path, (epoch, epoch))
                files[member.name] = (_sha(data), executable)
    except (OSError, tarfile.TarError):
        raise GateRefusal("archive_member_unsafe") from None
    return files


def project_metadata(tree: Path) -> tuple[str, dict]:
    try:
        matches = VERSION.findall((tree / "src" / DIST / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise GateRefusal("version_unreadable") from None
    need(len(matches) == 1, "version_unreadable")
    try:
        project = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
        need(project["project"]["name"] == PROJECT and isinstance(project["project"]["dependencies"], list)
             and isinstance(project["project"]["scripts"], dict)
             and isinstance(project["tool"]["setuptools"]["data-files"], dict), "project_unreadable")
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError):
        raise GateRefusal("project_unreadable") from None
    return matches[0], project


def parse_constraints(text: str) -> tuple[str, dict[str, tuple[str, frozenset[str]]]]:
    """Closed grammar: one `# target:` line, comments, and `name==version --hash=sha256:...` pins."""
    targets, logical, current = [], [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            match = TARGET.fullmatch(stripped)
            if match:
                targets.append(match.group(1))
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        logical.append(" ".join((current + stripped).split()))
        current = ""
    need(not current and len(targets) == 1 and 1 <= len(logical) <= 500, "constraints_invalid")
    pins: dict[str, tuple[str, frozenset[str]]] = {}
    for item in logical:
        match = PIN.fullmatch(item)
        need(match and match.group(1) not in pins, "constraints_invalid")
        digests = frozenset(value.removeprefix("--hash=sha256:") for value in match.group(3).split())
        pins[match.group(1)] = (match.group(2), digests)
    return targets[0], pins


def check_interpreter(target: str) -> None:
    need(sys.implementation.name == "cpython" and f"{sys.version_info[0]}.{sys.version_info[1]}" == target
         and platform.system() == "Darwin" and platform.machine() == "arm64", "interpreter_mismatch")


def _wheel_identity(path: Path) -> tuple[str, str]:
    parts = path.name.removesuffix(".whl").split("-")
    need(path.suffix == ".whl" and len(parts) in (5, 6), "wheelhouse_mismatch")
    return normalize(parts[0]), parts[1]


def check_wheelhouse(directory: Path, pins: dict[str, tuple[str, frozenset[str]]]) -> list[list[str]]:
    seen: dict[str, list[str]] = {}
    for path in sorted(directory.iterdir()):
        need(path.is_file() and not path.is_symlink(), "wheelhouse_mismatch")
        name, version = _wheel_identity(path)
        digest = sha256_file(path, "wheelhouse_mismatch")
        need(name in pins and name not in seen and pins[name][0] == version and digest in pins[name][1],
             "wheelhouse_mismatch", {"wheel": path.name})
        seen[name] = [path.name, digest]
    need(set(seen) == set(pins), "wheelhouse_mismatch", {"missing": sorted(set(pins) - set(seen))})
    return [seen[name] for name in sorted(seen)]


def _requirements(path: Path, pins: dict[str, tuple[str, frozenset[str]]]) -> Path:
    lines = [f"{name}=={version} " + " ".join(f"--hash=sha256:{d}" for d in sorted(digests))
             for name, (version, digests) in sorted(pins.items())]
    _write_new(path, ("\n".join(lines) + "\n").encode())
    return path


class Toolchain:
    """Fresh venvs from the running interpreter; pip in isolated, cache-free mode."""

    def __init__(self, scratch: Path):
        self.scratch = scratch
        self.tmp = _private_dir(scratch / "tmp")
        self.home = _private_dir(scratch / "home")

    def env(self, venv: Path | None = None, **extra: str) -> dict[str, str]:
        path = (str(venv / "bin") + ":" if venv else "") + SYSTEM_PATH
        network = {name: os.environ[name] for name in NETWORK_ENV if name in os.environ}
        return {"HOME": str(self.home), "TMPDIR": str(self.tmp), "PATH": path, "LC_ALL": "C", "TZ": "UTC",
                "PYTHONUTF8": "1", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1", **network, **extra}

    def venv(self, name: str) -> Path:
        root = self.scratch / name
        _run([sys.executable, "-I", "-m", "venv", root], "toolchain_failed", timeout=PIP_TIMEOUT, env=self.env())
        need((root / "bin" / "python").is_file(), "toolchain_failed")
        return root

    def pip(self, venv: Path, *args: object, code: str) -> subprocess.CompletedProcess:
        return _run([venv / "bin" / "python", "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                     "--no-input", "--no-cache-dir", *args], code, timeout=PIP_TIMEOUT, env=self.env(venv))

    def pip_version(self, venv: Path) -> str:
        output = self.pip(venv, "--version", code="toolchain_failed").stdout.decode("utf-8", "replace")
        match = re.match(r"pip ([0-9][0-9A-Za-z.]*) ", output)
        need(match, "toolchain_failed")
        return match.group(1)


def build_twice(chain: Toolchain, venv: Path, trees: tuple[Path, Path], version: str, epoch: int) -> tuple[Path, Path]:
    names = sorted([f"{DIST}-{version}.tar.gz", f"{DIST}-{version}-py3-none-any.whl"])
    outputs = []
    for index, tree in enumerate(trees):
        out = _private_dir(chain.scratch / f"build-{index}")
        _run([venv / "bin" / "python", "-I", "-m", "build", "--no-isolation", "--outdir", out, tree], "build_failed",
             timeout=BUILD_TIMEOUT, env=chain.env(venv, SOURCE_DATE_EPOCH=str(epoch)), cwd=chain.tmp)
        need(sorted(path.name for path in out.iterdir()) == names, "build_failed")
        outputs.append(out)
    compare_builds(*outputs)
    return outputs[0] / f"{DIST}-{version}.tar.gz", outputs[0] / f"{DIST}-{version}-py3-none-any.whl"


def compare_builds(first: Path, second: Path) -> None:
    names = sorted(path.name for path in first.iterdir())
    need(names == sorted(path.name for path in second.iterdir()), "build_not_reproducible")
    for name in names:
        need((first / name).read_bytes() == (second / name).read_bytes(), "build_not_reproducible", {"artifact": name})


def shipped(path: str) -> bool:
    return path in SHIPPED_INTERNAL or not any(fnmatch.fnmatchcase(path, pattern) for pattern in INTERNAL)


def check_sdist(path: Path, version: str, epoch: int, sources: dict[str, tuple[str, bool]]) -> int:
    prefix = f"{DIST}-{version}"
    expected = {f"{prefix}/{name}" for name in sources if shipped(name)} | {f"{prefix}/{name}" for name in GENERATED}
    directories = {prefix} | {str(parent) for name in expected for parent in PurePosixPath(name).parents
                              if str(parent).startswith(prefix)}
    try:
        with path.open("rb") as stream:
            header = stream.read(10)
        need(header[:3] == b"\x1f\x8b\x08" and int.from_bytes(header[4:8], "little") == epoch, "sdist_metadata")
        files: dict[str, bytes] = {}
        found: set[str] = set()
        with tarfile.open(path, "r:gz") as tar:
            for count, member in enumerate(tar, 1):
                need(count <= MAX_MEMBERS, "sdist_member_set")
                need((member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
                     and member.mtime == epoch and (member.isfile() or member.isdir()), "sdist_metadata",
                     {"member": member.name})
                need(member.name not in found and member.size <= MAX_MEMBER_BYTES, "sdist_member_set")
                found.add(member.name)
                if member.isfile():
                    with tar.extractfile(member) as content:
                        files[member.name] = content.read(member.size + 1)
    except (OSError, tarfile.TarError, EOFError):
        raise GateRefusal("sdist_metadata") from None
    actual_directories = found - set(files)
    need(set(files) == expected and actual_directories == directories, "sdist_member_set",
         {"extra": sorted((set(files) - expected) | (actual_directories - directories)),
          "missing": sorted((expected - set(files)) | (directories - actual_directories))})
    for name, (digest, _) in sources.items():
        if shipped(name):
            need(_sha(files[f"{prefix}/{name}"]) == digest, "sdist_content", {"member": name})
    info = email.parser.Parser().parsestr(files[f"{prefix}/PKG-INFO"].decode("utf-8", "replace"))
    need(info["Name"] == PROJECT and info["Version"] == version, "sdist_metadata")
    return len(found)


def _record_digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def check_wheel(path: Path, version: str, sources: dict[str, tuple[str, bool]], project: dict) -> int:
    info_dir, data_dir = f"{DIST}-{version}.dist-info/", f"{DIST}-{version}.data/data/"
    members: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            need(len(entries) <= MAX_MEMBERS, "wheel_member_set")
            for entry in entries:
                name = entry.filename
                kind = stat.S_IFMT(entry.external_attr >> 16)
                need(_safe_name(name) and name not in members and kind in (0, stat.S_IFREG)
                     and entry.file_size <= MAX_MEMBER_BYTES, "wheel_member_set", {"member": name})
                members[name] = archive.read(entry)
    except (OSError, zipfile.BadZipFile):
        raise GateRefusal("wheel_member_set") from None
    package = {name: _sha(data) for name, data in members.items() if name.startswith(DIST + "/")}
    expected_package = {name.removeprefix("src/"): digest for name, (digest, _) in sources.items()
                        if name.startswith(f"src/{DIST}/")}
    need(package == expected_package, "wheel_package_mismatch",
         {"extra": sorted(set(package) - set(expected_package)), "missing": sorted(set(expected_package) - set(package))})
    expected_data = {}
    for target, names in project["tool"]["setuptools"]["data-files"].items():
        for name in names:
            need(name in sources, "wheel_data_mismatch", {"missing": name})
            expected_data[f"{data_dir}{target}/{PurePosixPath(name).name}"] = sources[name][0]
    data = {name: _sha(content) for name, content in members.items() if name.startswith(data_dir)}
    need(data == expected_data, "wheel_data_mismatch",
         {"extra": sorted(set(data) - set(expected_data)), "missing": sorted(set(expected_data) - set(data))})
    expected_info = {info_dir + name for name in DIST_INFO} | {
        f"{info_dir}licenses/{name}" for name in project["project"].get("license-files", [])}
    need(set(members) == set(package) | set(data) | expected_info, "wheel_member_set",
         {"extra": sorted(set(members) - set(package) - set(data) - expected_info)})
    try:
        metadata = email.parser.Parser().parsestr(members[info_dir + "METADATA"].decode("utf-8"))
        wheel = email.parser.Parser().parsestr(members[info_dir + "WHEEL"].decode("utf-8"))
        scripts = configparser.ConfigParser(interpolation=None)
        scripts.optionxform = str
        scripts.read_string(members[info_dir + "entry_points.txt"].decode("utf-8"))
        rows = list(csv.reader(members[info_dir + "RECORD"].decode("utf-8").splitlines()))
    except (UnicodeError, configparser.Error, csv.Error):
        raise GateRefusal("wheel_metadata") from None
    requires = sorted(value for value in metadata.get_all("Requires-Dist") or [] if "extra ==" not in value)
    need(metadata["Name"] == PROJECT and metadata["Version"] == version
         and requires == sorted(project["project"]["dependencies"])
         and wheel.get_all("Tag") == ["py3-none-any"] and wheel["Root-Is-Purelib"] == "true", "wheel_metadata")
    need(scripts.sections() == ["console_scripts"]
         and dict(scripts["console_scripts"]) == project["project"]["scripts"], "wheel_entry_points")
    listed = {}
    for row in rows:
        need(len(row) == 3 and row[0] in members and row[0] not in listed, "wheel_record")
        listed[row[0]] = row
    need(set(listed) == set(members), "wheel_record")
    for name, (_, digest, size) in listed.items():
        if name == info_dir + "RECORD":
            need(digest == "" and size == "", "wheel_record")
        else:
            need(digest == _record_digest(members[name]) and size == str(len(members[name])), "wheel_record",
                 {"member": name})
    return len(members)


def privacy_patterns(tree: Path, sources: dict[str, tuple[str, bool]]) -> tuple[str, list[str]]:
    """Static PRIVACY_PATTERNS from the commit's own privacy test; never executes it."""
    if PRIVACY_SOURCE not in sources:
        return "not_available", []
    try:
        module = ast.parse((tree / PRIVACY_SOURCE).read_text(encoding="utf-8"))
        values = [node.value for node in module.body
                  if (isinstance(node, ast.Assign) and len(node.targets) == 1
                      and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "PRIVACY_PATTERNS")
                  or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                      and node.target.id == "PRIVACY_PATTERNS" and node.value is not None)]
        need(len(values) == 1, "privacy_patterns_invalid")
        patterns = ast.literal_eval(values[0])
        need(isinstance(patterns, (tuple, list)) and 1 <= len(patterns) <= 256
             and all(type(p) is str and 0 < len(p) <= 1024 for p in patterns), "privacy_patterns_invalid")
        for pattern in patterns:
            re.compile(pattern)
    except (OSError, UnicodeError, SyntaxError, ValueError, re.error):
        raise GateRefusal("privacy_patterns_invalid") from None
    return PRIVACY_SOURCE, list(patterns)


def privacy_scan(git: Git, previous: str, commit: str, source: str, patterns: list[str],
                 sdist: Path, wheel: Path) -> dict:
    """Hits name the source, member path and pattern index only; matched text is never recorded."""
    compiled = [re.compile(pattern) for pattern in patterns]
    hits: set[tuple[str, str, int]] = set()
    scanned = {"diff_lines": 0, "sdist_members": 0, "wheel_members": 0}

    def scan(kind: str, path: str, text: str) -> None:
        for index, pattern in enumerate(compiled):
            if pattern.search(text):
                hits.add((kind, path, index))

    diff = git("diff", "--no-color", "--no-ext-diff", "--no-renames", "--text", "-U0", previous, commit, "--",
               code="privacy_scan_bounds").stdout
    path = ""
    for raw in diff.split(b"\n"):
        line = raw.decode("utf-8", "replace")
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            scan("diff", path, path)
        elif line.startswith("+"):
            scanned["diff_lines"] += 1
            scan("diff", path, line[1:])
    try:
        with tarfile.open(sdist, "r:gz") as tar:
            for member in tar:
                scanned["sdist_members"] += 1
                scan("sdist", member.name, member.name)
                if member.isfile():
                    with tar.extractfile(member) as content:
                        scan("sdist", member.name, content.read(MAX_MEMBER_BYTES).decode("utf-8", "replace"))
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                scanned["wheel_members"] += 1
                scan("wheel", entry.filename, entry.filename)
                scan("wheel", entry.filename, archive.read(entry).decode("utf-8", "replace"))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
        raise GateRefusal("privacy_scan_bounds") from None
    return {"pattern_source": source, "patterns": len(patterns), "scanned": scanned,
            "hits": [{"source": kind, "path": member, "pattern": index} for kind, member, index in sorted(hits)]}


def check_privacy(report: dict) -> None:
    need(not report["hits"], "privacy_hit", {"hits": report["hits"][:100]})


def _identity_class(address: str) -> str:
    local, _, domain = address.lower().rpartition("@")
    return "noreply" if domain.endswith("users.noreply.github.com") or local in ("noreply", "no-reply") else "direct"


def identities(git: Git, previous: str, commit: str) -> dict:
    """A classified report of who appears in previous..commit; not a pass/fail check."""
    output = git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00"
                 "%(trailers:key=Co-authored-by,valueonly,separator=%x1f)%x1e", f"{previous}..{commit}").stdout
    counts: dict[tuple[str, str, str], int] = {}
    commits = 0
    for record in output.split(b"\x1e"):
        record = record.strip(b"\n")
        if not record:
            continue
        commits += 1
        need(commits <= MAX_COMMITS, "git_failed")
        fields = record.decode("utf-8", "replace").split("\x00")
        need(len(fields) == 6, "git_failed")
        people = {("author", fields[1], fields[2]), ("committer", fields[3], fields[4])}
        for trailer in filter(None, fields[5].strip().split("\x1f")):
            match = re.fullmatch(r"\s*(.*?)\s*<([^<>]*)>\s*", trailer)
            people.add(("co_author", match.group(1), match.group(2)) if match else ("co_author", trailer.strip(), ""))
        for person in people:
            counts[person] = counts.get(person, 0) + 1
    return {"commits": commits, "identities": [
        {"role": role, "name": name, "email": address, "class": _identity_class(address), "commits": total}
        for (role, name, address), total in sorted(counts.items())]}


def install_closure(chain: Toolchain, venv: Path, wheel: Path, constraints: Path, wheelhouse: Path,
                    version: str, pins: dict[str, tuple[str, frozenset[str]]]) -> dict:
    requirement = chain.scratch / "wheel-requirement.txt"
    _write_new(requirement, f"{PROJECT} @ {wheel.as_uri()} --hash=sha256:{sha256_file(wheel)}\n".encode())
    chain.pip(venv, "install", "--no-index", "--find-links", wheelhouse, "--only-binary=:all:", "--require-hashes",
              "-c", constraints, "-r", requirement, code="install_failed")
    chain.pip(venv, "check", code="pip_check_failed")
    listing = chain.pip(venv, "list", "--format=freeze", "--exclude", "pip", code="install_failed").stdout
    freeze = []
    for line in listing.decode("utf-8", "replace").splitlines():
        name, separator, installed = line.strip().partition("==")
        need(separator and installed, "freeze_mismatch")
        freeze.append(f"{normalize(name)}=={installed}")
    expected = sorted([f"{name}=={pinned}" for name, (pinned, _) in pins.items()] + [f"{PROJECT}=={version}"])
    need(sorted(freeze) == expected, "freeze_mismatch",
         {"extra": sorted(set(freeze) - set(expected)), "missing": sorted(set(expected) - set(freeze))})
    return {"freeze": expected, "pip_check": "clean", "record_entries": verify_records(chain, venv)}


def verify_records(chain: Toolchain, venv: Path) -> int:
    """Every hashed RECORD row of every installed distribution matches the file on disk."""
    code = "import json, sysconfig; print(json.dumps(sorted({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')})))"
    output = _run([venv / "bin" / "python", "-I", "-c", code], "record_mismatch", timeout=PIP_TIMEOUT,
                  env=chain.env(venv)).stdout
    root, checked = venv.resolve(), 0
    for site in json.loads(output):
        site = Path(site).resolve()
        need(site.is_relative_to(root), "record_mismatch")
        records = sorted(site.glob("*.dist-info/RECORD"))
        need(records and len(records) <= 1000, "record_mismatch")
        for record in records:
            for row in csv.reader(record.read_text(encoding="utf-8").splitlines()):
                need(len(row) == 3, "record_mismatch", {"record": record.parent.name})
                target = (site / row[0]).resolve()
                need(target.is_relative_to(root) and target.is_file(), "record_mismatch", {"entry": row[0]})
                if not row[1]:
                    need(target == record.resolve() or target.suffix == ".pyc", "record_mismatch", {"entry": row[0]})
                    continue
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        digest.update(chunk)
                encoded = "sha256=" + base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode()
                need(row[1] == encoded and (not row[2] or int(row[2]) == target.stat().st_size), "record_mismatch",
                     {"entry": row[0]})
                checked += 1
    return checked


def _json_output(result: subprocess.CompletedProcess, code: str) -> dict:
    try:
        value = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeError, ValueError, IndexError):
        raise GateRefusal(code) from None
    need(type(value) is dict, code)
    return value


def write_policy(home: Path, capabilities: list[str]) -> None:
    directory = home
    for part in ("Library", "Application Support", "TelegramSearchMCP"):
        directory = _private_dir(directory / part)
    body = "config_version = 1\nenabled_capabilities = [" + ", ".join(json.dumps(c) for c in capabilities) + "]\n"
    _write_new(directory / "runtime.toml", body.encode())


def smoke(chain: Toolchain, venv: Path, tree: Path) -> tuple[dict, dict]:
    """Source census of the full registration, then the installed server over stdio with an isolated HOME."""
    census_script, smoke_script = chain.scratch / "census.py", chain.scratch / "smoke.py"
    _write_new(census_script, CENSUS.encode())
    _write_new(smoke_script, SMOKE.encode())
    census_home = _private_dir(chain.scratch / "census-home")
    source = _json_output(_run([venv / "bin" / "python", "-I", census_script, tree / "src"], "census_failed",
                               timeout=SMOKE_TIMEOUT, env={**chain.env(venv), "HOME": str(census_home)},
                               cwd=census_home), "census_failed")
    need(isinstance(source.get("tools"), list) and isinstance(source.get("capabilities"), list), "census_failed")
    smoke_home = _private_dir(chain.scratch / "smoke-home")
    write_policy(smoke_home, source["capabilities"])
    observed = _json_output(_run([venv / "bin" / "python", "-I", smoke_script, venv / "bin" / PROJECT, smoke_home],
                                 "smoke_failed", timeout=SMOKE_TIMEOUT,
                                 env={**chain.env(venv), "HOME": str(smoke_home)}, cwd=smoke_home), "smoke_failed")
    return source, observed


def check_smoke(source: dict, observed: dict, version: str) -> dict:
    need(source.get("version") == version and observed.get("server_version") == version
         and observed.get("server_name") == PROJECT, "smoke_version_mismatch")
    need(observed.get("tools") == source["tools"], "tool_census_mismatch",
         {"source": len(source["tools"]), "stdio": len(observed.get("tools") or [])})
    need(observed.get("manifest_error") is False and observed.get("manifest_package_version") == version
         and observed.get("manifest_schema_fingerprint") == source.get("schema_fingerprint"), "smoke_manifest_mismatch")
    need(observed.get("manifest_capabilities") == source["capabilities"], "smoke_policy_mismatch")
    return {"server_version": version, "tool_count": len(source["tools"]),
            "tools_sha256": _sha("\n".join(source["tools"]).encode()),
            "schema_fingerprint": source["schema_fingerprint"], "capabilities": len(source["capabilities"])}


def run_suite(python: Path, tree: Path, junit: Path, log: Path, env: dict) -> int:
    result = _run([python, "-m", "pytest", "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning", "-rs",
                   f"--junitxml={junit}", "-o", "junit_family=xunit2"], "suite_failed", timeout=SUITE_TIMEOUT,
                  env=env, cwd=tree, stdout=log, check=False)
    return result.returncode


def suite_report(junit: Path, log: Path) -> dict:
    """Counts and skip reasons re-derived from the junit and log files alone."""
    try:
        need(junit.lstat().st_size <= MAX_OUTPUT_BYTES and log.lstat().st_size <= MAX_OUTPUT_BYTES, "suite_unreadable")
        root = ElementTree.fromstring(junit.read_bytes())
        lines = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except (OSError, ElementTree.ParseError):
        raise GateRefusal("suite_unreadable") from None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    need(suites and lines, "suite_unreadable")
    counts = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    skips = []
    for case in (case for suite in suites for case in suite.iter("testcase")):
        counts["tests"] += 1
        tags = [child.tag for child in case]
        counts["failed"] += tags.count("failure")
        counts["errors"] += tags.count("error")
        if "skipped" in tags and not {"failure", "error"} & set(tags):
            counts["skipped"] += 1
            skips.append({"test": f"{case.get('classname', '')}::{case.get('name', '')}",
                          "reason": (case.find("skipped").get("message") or "")[:500]})
        elif not {"failure", "error", "skipped"} & set(tags):
            counts["passed"] += 1
    return {**counts, "skips": sorted(skips, key=lambda item: item["test"]), "summary": lines[-1][:500],
            "junit": junit.name, "junit_sha256": sha256_file(junit), "log": log.name, "log_sha256": sha256_file(log)}


def check_tests(report: dict, returncode: int) -> None:
    need(returncode == 0 and report["failed"] == 0 and report["errors"] == 0 and report["passed"] > 0,
         "tests_failed", {key: report[key] for key in ("tests", "passed", "failed", "errors", "skipped")})


def own_sha256() -> str:
    return sha256_file(Path(__file__).resolve(), "gate_mismatch")


def derive(git: Git, commit: str, tag: str, base: Path, scratch: Path, *, online: bool) -> dict:
    """Every evidence field except tests, from `git archive <commit>` and the pinned wheels."""
    commit = resolve_commit(git, commit)
    previous = resolve_tag(git, tag, commit)
    tree_id = git.text("rev-parse", "--verify", commit + "^{tree}")
    epoch = int(git.text("show", "-s", "--format=%ct", commit))
    chain = Toolchain(scratch)
    archives = [scratch / f"archive-{index}.tar" for index in range(2)]
    for archive in archives:
        git("archive", "--format=tar", commit, stdout=archive)
    need(archives[0].read_bytes() == archives[1].read_bytes(), "archive_not_reproducible")
    trees = (scratch / "tree-0", scratch / "tree-1")
    contents = [extract(archive, tree, epoch) for archive, tree in zip(archives, trees)]
    need(contents[0] == contents[1], "archive_not_reproducible")
    sources = contents[0]
    tracked = tracked_files(git, commit)
    need({name: executable for name, (_, executable) in sources.items()} == tracked, "archive_incomplete",
         {"missing": sorted(set(tracked) - set(sources)), "extra": sorted(set(sources) - set(tracked))})
    version, project = project_metadata(trees[0])
    release = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    need(release, "version_unreadable")
    constraints_name = f"packaging/constraints-{release.group(0)}.txt"
    need(constraints_name in sources, "constraints_missing")
    constraints = trees[0] / constraints_name
    try:
        target, pins = parse_constraints(constraints.read_text(encoding="utf-8"))
    except UnicodeError:
        raise GateRefusal("constraints_invalid") from None
    check_interpreter(target)
    tools = {name: (pinned, frozenset({digest})) for name, pinned, digest in TOOLS}
    toolhouse, wheelhouse = base / "toolhouse", base / "wheelhouse"
    build_venv = chain.venv("venv-build")
    tool_requirements = _requirements(scratch / "tools.txt", tools)
    if online:
        _private_dir(toolhouse)
        chain.pip(build_venv, "download", "--only-binary=:all:", "--no-deps", "--require-hashes",
                  "-r", tool_requirements, "-d", toolhouse, code="download_failed")
    check_wheelhouse(toolhouse, tools)
    build_requirements = _requirements(scratch / "build-tools.txt",
                                       {name: pin for name, pin in tools.items() if name in BUILD_TOOLS})
    chain.pip(build_venv, "install", "--no-index", "--find-links", toolhouse, "--only-binary=:all:", "--no-deps",
              "--require-hashes", "-r", build_requirements, code="toolchain_failed")
    sdist, wheel = build_twice(chain, build_venv, trees, version, epoch)
    sdist_members = check_sdist(sdist, version, epoch, sources)
    wheel_members = check_wheel(wheel, version, sources, project)
    pattern_source, patterns = privacy_patterns(trees[0], sources)
    privacy = privacy_scan(git, previous, commit, pattern_source, patterns, sdist, wheel)
    check_privacy(privacy)
    if online:
        _private_dir(wheelhouse)
        chain.pip(build_venv, "download", "--only-binary=:all:", "--no-deps", "--require-hashes",
                  "-r", constraints, "-d", wheelhouse, code="download_failed")
    closure = check_wheelhouse(wheelhouse, pins)
    install_venv = chain.venv("venv-install")
    install = install_closure(chain, install_venv, wheel, constraints, wheelhouse, version, pins)
    source, observed = smoke(chain, install_venv, trees[0])
    gate_at_commit = trees[0] / GATE_PATH
    return {
        "schema": SCHEMA, "commit": commit, "tree": tree_id, "commit_time": epoch, "previous_tag": tag,
        "previous_commit": previous, "version": version,
        "gate": {"sha256": own_sha256(),
                 "at_commit_sha256": sha256_file(gate_at_commit) if GATE_PATH in sources else None},
        "toolchain": {"python": platform.python_version(), "implementation": sys.implementation.name,
                      "platform": f"{platform.system()}-{platform.machine()}", "pip": chain.pip_version(build_venv),
                      "tools": [list(tool) for tool in TOOLS]},
        "archive": {"sha256": sha256_file(archives[0]), "files": len(sources)},
        "sdist": {"file": f"dist/{sdist.name}", "sha256": sha256_file(sdist), "bytes": sdist.stat().st_size,
                  "members": sdist_members},
        "wheel": {"file": f"dist/{wheel.name}", "sha256": sha256_file(wheel), "bytes": wheel.stat().st_size,
                  "members": wheel_members},
        "constraints": {"file": constraints_name, "sha256": sha256_file(constraints), "target": target,
                        "distributions": len(pins), "wheels": closure},
        "install": install,
        "smoke": check_smoke(source, observed, version),
        "privacy": privacy,
        "identities": identities(git, previous, commit),
        "_artifacts": (sdist, wheel, archives[0], chain),
    }


def run_tests(evidence: dict, base: Path, scratch: Path) -> dict:
    """The full suite from a fresh archive extraction, on the pinned closure, with a hermetic HOME."""
    archive, chain = evidence["_artifacts"][2:]
    tree = scratch / "suite-tree"
    extract(archive, tree, evidence["commit_time"])
    venv = chain.venv("venv-test")
    tools = {name: (pinned, frozenset({digest})) for name, pinned, digest in TOOLS}
    chain.pip(venv, "install", "--no-index", "--find-links", base / "toolhouse", "--find-links", base / "wheelhouse",
              "--only-binary=:all:", "--require-hashes", "-r", _requirements(scratch / "test-tools.txt", tools),
              "-r", tree / evidence["constraints"]["file"], code="toolchain_failed")
    chain.pip(venv, "check", code="pip_check_failed")
    home = _private_dir(scratch / "suite-home")
    env = {**chain.env(venv), "HOME": str(home), "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": tempfile.gettempdir(),
           "PATH": str(venv / "bin") + ":" + os.environ.get("PATH", SYSTEM_PATH)}
    returncode = run_suite(venv / "bin" / "python", tree, base / "junit.xml", base / "suite.log", env)
    report = suite_report(base / "junit.xml", base / "suite.log")
    check_tests(report, returncode)
    return report


def canonical(evidence: dict) -> bytes:
    return (json.dumps(evidence, indent=1, sort_keys=True) + "\n").encode()


def write_evidence(path: Path, evidence: dict) -> None:
    temporary = path.with_name(path.name + ".partial")
    _write_new(temporary, canonical(evidence))
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> int:
    repo = toplevel(args.repo)
    git = Git(repo)
    commit = resolve_commit(git, args.commit)
    resolve_tag(git, args.previous_tag, commit)
    workdir = prepare_workdir(args.workdir, repo)
    try:
        scratch = _private_dir(workdir / "work")
        evidence = derive(git, commit, args.previous_tag, workdir, scratch, online=True)
        sdist, wheel = evidence["_artifacts"][:2]
        dist = _private_dir(workdir / "dist")
        for artifact in (sdist, wheel):
            shutil.copyfile(artifact, dist / artifact.name)
            os.chmod(dist / artifact.name, 0o600)
            need(sha256_file(dist / artifact.name) == sha256_file(artifact), "build_failed")
        evidence["tests"] = run_tests(evidence, workdir, scratch)
        del evidence["_artifacts"]
        for kind in ("sdist", "wheel"):
            evidence[kind]["sha256"] = sha256_file(workdir / evidence[kind]["file"])
        need(set(evidence) == EVIDENCE_KEYS, "evidence_invalid")
        write_evidence(workdir / "evidence.json", evidence)
        shutil.rmtree(scratch)
    except GateRefusal as refusal:
        try:
            _write_new(workdir / "refusal.json", (json.dumps({"code": refusal.code, "detail": refusal.detail},
                                                             indent=1, sort_keys=True, default=str) + "\n").encode())
        except OSError:
            pass
        raise
    print("release gate passed")
    return 0


def _unique(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        need(key not in value, "evidence_invalid")
        value[key] = item
    return value


def load_evidence(path: Path) -> dict:
    try:
        metadata = path.lstat()
        need(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_EVIDENCE_BYTES, "evidence_invalid")
        raw = path.read_bytes()
        evidence = json.loads(raw, object_pairs_hook=_unique)
    except (OSError, ValueError):
        raise GateRefusal("evidence_invalid") from None
    # Only the exact bytes the gate writes are accepted, so even a formatting edit fails.
    need(type(evidence) is dict and canonical(evidence) == raw, "evidence_invalid")
    need(set(evidence) == EVIDENCE_KEYS and evidence["schema"] == SCHEMA
         and type(evidence["commit"]) is str and type(evidence["previous_tag"]) is str
         and type(evidence["gate"]) is dict and type(evidence["tests"]) is dict
         and type(evidence["sdist"]) is dict and type(evidence["wheel"]) is dict, "evidence_invalid")
    return evidence


def delegate(git: Git, evidence: dict, path: Path, scratch: Path) -> int | None:
    """Evidence from another gate version is verified by the gate stored at the recorded commit."""
    recorded = evidence["gate"].get("sha256")
    if recorded == own_sha256():
        return None
    need(COMMIT.fullmatch(evidence["commit"]) and len(evidence["commit"]) == 40, "gate_mismatch")
    blob = git("show", f"{evidence['commit']}:{GATE_PATH}", code="gate_mismatch", check=False)
    need(blob.returncode == 0 and _sha(blob.stdout) == recorded, "gate_mismatch")
    gate = scratch / "release_gate.py"
    _write_new(gate, blob.stdout)
    try:
        return subprocess.run([sys.executable, "-I", gate, "verify", "--evidence", path, "--repo", git.repo,
                               "--delegated"], stdin=subprocess.DEVNULL, timeout=SUITE_TIMEOUT).returncode
    except (OSError, subprocess.SubprocessError):
        raise GateRefusal("gate_mismatch") from None


def verify(args: argparse.Namespace, derive_evidence=derive) -> int:
    path = outside_tmp(args.evidence, None, "evidence_invalid")
    evidence = load_evidence(path)
    base = path.parent
    git = Git(toplevel(args.repo))
    scratch = _private_dir(base / f"verify-{secrets.token_hex(8)}")
    try:
        if not args.delegated:
            delegated = delegate(git, evidence, path, scratch)
            if delegated is not None:
                return delegated
        version_files = (f"dist/{DIST}-{evidence.get('version')}.tar.gz",
                         f"dist/{DIST}-{evidence.get('version')}-py3-none-any.whl")
        need((evidence["sdist"].get("file"), evidence["wheel"].get("file")) == version_files, "evidence_invalid")
        for kind in ("sdist", "wheel"):
            need(sha256_file(base / evidence[kind]["file"]) == evidence[kind].get("sha256"), "evidence_mismatch")
        need(evidence["tests"].get("junit") == "junit.xml" and evidence["tests"].get("log") == "suite.log",
             "evidence_invalid")
        tests = suite_report(base / "junit.xml", base / "suite.log")
        need(tests == evidence["tests"], "evidence_mismatch", "tests")
        check_tests(tests, 0)
        derived = derive_evidence(git, evidence["commit"], evidence["previous_tag"], base, scratch, online=False)
        sdist, wheel = derived.pop("_artifacts")[:2]
        need(derived["toolchain"] == evidence["toolchain"], "toolchain_mismatch")
        for kind, artifact in (("sdist", sdist), ("wheel", wheel)):
            need(sha256_file(artifact) == sha256_file(base / evidence[kind]["file"]), "evidence_mismatch", kind)
        derived["tests"] = tests
        for key in sorted(EVIDENCE_KEYS):
            need(derived[key] == evidence[key], "evidence_mismatch", key)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("release gate verified")
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise GateRefusal("usage")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="release_gate.py", description="Deterministic release gate.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--commit", required=True)
    run_parser.add_argument("--previous-tag", required=True)
    run_parser.add_argument("--workdir", required=True, type=Path)
    run_parser.add_argument("--repo", default=Path.cwd(), type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--evidence", required=True, type=Path)
    verify_parser.add_argument("--repo", default=Path.cwd(), type=Path)
    verify_parser.add_argument("--delegated", action="store_true", help=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
        return run(args) if args.command == "run" else verify(args)
    except GateRefusal as refusal:
        section = refusal.detail if refusal.code == "evidence_mismatch" and isinstance(refusal.detail, str) else None
        print(f"release gate refused: {refusal.code}" + (f" ({section})" if section in EVIDENCE_KEYS else ""),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
