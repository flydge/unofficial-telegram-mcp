"""Public-tree privacy guard: tracked text files carry no local paths, real ids or account data.

Scans the files `git ls-files` lists (a clean checkout is HEAD; a tree without `.git`, such as a
`git archive` extraction, is walked instead). Findings print as `path:line: rule`, never the match.
The rule patterns and the planted lines below are written so that this file does not match them.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SYSTEM_PREFIXES = ("/var/", "/usr/", "/opt/", "/nonexistent", "/dev/", "/bin/", "/sbin/")

RULES = {
    "user_path": re.compile(r"/Users/([A-Za-z0-9_.-]+)"),
    "home_path": re.compile(r"(?:/home/|[A-Za-z]:\\Users\\)([A-Za-z0-9_.-]+)"),
    "local_dir": re.compile(r"~/telegram-mcp-[0]|telegram-mcp-[0]\d\d-|\.codex/rese[a]rch|tg-de[v]-"),
    "message_id": re.compile(r"\bmsg \d{6,}", re.I),
    "chat_id": re.compile(r"(?<![0-9A-Za-z])-100\d{8,}"),
    "email": re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
}

# (rule, tracked path or None for any, exact matched text or None for any, reason). Keep it minimal.
# A path-scoped entry that no longer matches its file fails `test_allowlist_has_no_stale_entries`.
# The values are split so that this file does not match its own path-scoped entries.
ALLOW = (
    ("user_path", None, "/Users/" "example", "documentation placeholder account name"),
    ("user_path", "tests/test_connect_cli.py", "/Users/" "SYNTHETIC_SECRET_SENTINEL",
     "synthetic leak sentinel; the test asserts it is never echoed"),
    ("chat_id", "tests/test_schemas.py", "-100" "1234567890", "sequential-digit synthetic supergroup id"),
    ("chat_id", "tests/test_reply_relative_datetime_targets.py", "-100" "3456789012",
     "sequential-digit synthetic supergroup id"),
    ("email", "docs/licenses/Pillow.txt", None,
     "verbatim third-party license text quotes upstream maintainer addresses"),
    ("email", "tests/test_archive_edit.py", "mail@" "x.io", "synthetic address inside a styled-text fixture"),
)


def synthetic_email(value: str) -> bool:
    """noreply@ addresses and RFC 2606 example/reserved domains are not personal."""
    local, _, domain = value.lower().rpartition("@")
    labels = domain.split(".")
    return (local == "noreply" or labels[-1] in {"example", "invalid", "test", "localhost"}
            or labels[-2:] in (["example", "com"], ["example", "org"], ["example", "net"]))


def local_literals(root: Path = ROOT) -> tuple[str, ...]:
    """The running account's real home directory and this checkout's absolute path."""
    found = {str(root.resolve())}
    try:
        import pwd
        found.add(pwd.getpwuid(os.getuid()).pw_dir.rstrip("/"))
    except (ImportError, KeyError):
        pass
    return tuple(sorted(v for v in found if len(v) >= 8 and v.count("/") >= 2
                        and not (v + "/").startswith(SYSTEM_PREFIXES)))


def allowed(rule: str, path: str, value: str) -> int | None:
    """-1 for a synthetic address, the index of the admitting allowlist entry, else None."""
    if rule == "email" and synthetic_email(value):
        return -1
    for index, (name, where, text, _reason) in enumerate(ALLOW):
        if name == rule and where in (None, path) and text in (None, value):
            return index
    return None


def scan_text(path: str, text: str, literals: tuple[str, ...] = (),
              used: set | None = None) -> list[tuple[str, int, str]]:
    """Findings `(path, line, rule)` for one file's name (line 0) and text."""
    findings = []
    for number, line in [(0, path), *enumerate(text.splitlines(), 1)]:
        for rule, pattern in RULES.items():
            if rule == "email" and "@" not in line:
                continue
            for match in pattern.finditer(line):
                hit = allowed(rule, path, match.group(0))
                if hit is None:
                    findings.append((path, number, rule))
                elif used is not None and hit >= 0:
                    used.add(hit)
        findings += [(path, number, "home_literal") for literal in literals if literal in line]
    return findings


def tracked_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             timeout=60).stdout
        return sorted(name for name in out.decode("utf-8", "surrogateescape").split("\0") if name)
    names = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        names += [str((Path(directory) / name).relative_to(root)) for name in files]
    return sorted(names)


def scan_tree(root: Path, literals: tuple[str, ...] = (), used: set | None = None) -> tuple[list, set]:
    """Findings and the set of text files scanned; binary files, directories and gaps are skipped."""
    findings, scanned = [], set()
    for name in tracked_files(root):
        path = root / name
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif path.is_file():
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue
        else:
            continue
        scanned.add(name)
        findings += scan_text(name, data.decode("utf-8", "replace"), literals, used)
    return findings, scanned


def render(findings: list) -> str:
    return "\n".join(f"{path}:{line}: {rule}" for path, line, rule in findings[:50])


class PublicTreePrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.used: set = set()
        cls.findings, cls.scanned = scan_tree(ROOT, local_literals(), cls.used)

    def test_tracked_files_carry_no_private_data(self) -> None:
        self.assertGreater(len(self.scanned), 100, "tracked file listing is unexpectedly small")
        self.assertEqual([], self.findings, "private data in the public tree:\n" + render(self.findings))

    def test_allowlist_has_no_stale_entries(self) -> None:
        for index, (rule, where, _text, reason) in enumerate(ALLOW):
            self.assertTrue(reason.strip())
            if where in self.scanned:
                self.assertIn(index, self.used, f"stale allowlist entry: {rule} {where}")

    def test_planted_lines_fail_each_rule(self) -> None:
        literal = "/srv/" "private-home"
        planted = (
            ("user_path", "cd /Users/" "alice/project"),
            ("home_path", "cd /home/" "bob/project"),
            ("home_path", "cd C:\\Users\\" "carol\\project"),
            ("local_dir", "see ~/telegram-mcp-" "052-runtime/venv"),
            ("local_dir", "copy from ~/.codex/" "research/run"),
            ("local_dir", "clone tg-" "dev-7"),
            ("message_id", "sent msg " "631242752"),
            ("chat_id", "chat -100" "9876543210"),
            ("email", "mail bob@" "corp.com"),
            ("home_literal", "path " + literal + "/x"),
        )
        for rule, line in planted:
            with self.subTest(rule=rule, line=line):
                self.assertEqual([("docs/plan.md", 1, rule)], scan_text("docs/plan.md", line, (literal,)))
        named = "docs/tg-" "dev-7.md"
        self.assertEqual([(named, 0, "local_dir")], scan_text(named, "clean"))

    def test_synthetic_values_pass(self) -> None:
        clean = (
            ("docs/a.md", "cd /Users/" "example/project"),
            ("tests/test_connect_cli.py", "root /Users/" "SYNTHETIC_SECRET_SENTINEL/.codex"),
            ("tests/test_schemas.py", "id -100" "1234567890"),
            ("tests/test_reply_relative_datetime_targets.py", "id -100" "3456789012"),
            ("docs/licenses/Pillow.txt", "write to anyone@" "somewhere.org"),
            ("docs/a.md", "From: noreply@" "users.example.org, bob@" "example.com, a@" "b.invalid"),
            ("docs/a.md", "tomlkit==0.13.3 abc@1.2.3 @pytest.mark.skip"),
        )
        for path, line in clean:
            with self.subTest(line=line):
                self.assertEqual([], scan_text(path, line))
        # allowlist entries stay scoped to their file
        self.assertEqual([("tests/other.py", 1, "chat_id")], scan_text("tests/other.py", "id -100" "1234567890"))
        self.assertEqual([("tests/other.py", 1, "user_path")],
                         scan_text("tests/other.py", "root /Users/" "SYNTHETIC_SECRET_SENTINEL/.codex"))

    def test_planted_file_fails_a_tree_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "clean.md").write_text("nothing private here\n")
            (root / "docs" / "dirty.md").write_text("fine\nsent msg " "633339904 today\n")
            (root / "blob.bin").write_bytes(b"\0msg " b"633339904")
            (root / ".venv").mkdir()
            (root / ".venv" / "skipped.txt").write_text("cd /Users/" "alice/x\n")
            findings, scanned = scan_tree(root)
            self.assertEqual([("docs/dirty.md", 2, "message_id")], findings)
            self.assertEqual({"docs/clean.md", "docs/dirty.md"}, scanned)


if __name__ == "__main__":
    unittest.main()
