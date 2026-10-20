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
    "other_home": re.compile(r"(?<![\w~])~[A-Za-z_][A-Za-z0-9_.-]*/"),
    "claude_project": re.compile(r"-Users-[A-Za-z0-9_.]+-"),
    "local_dir": re.compile(r"~/telegram-mcp-[0]|telegram-mcp-[0]\d\d-|\.codex/rese[a]rch|tg-de[v]-"),
    # Up to 6 non-word characters between key and value admit `"message_id": "1..."` and `message_id = '1...'`.
    "message_id": re.compile(r"(?<![A-Za-z])(?:msg|message)(?:[ _-]?ids?)?\W{1,6}\d{6,}", re.I),
    "chat_id": re.compile(r"(?<![0-9A-Za-z])-100\d{8,}"),
    # A candidate after "+": digit groups joined by 1-2 spaces, dots or hyphens, or a bracketed area code.
    # phone_value() flags the shortest prefix of groups that reaches 10-15 digits.
    "phone": re.compile(r"(?<![\w+])\+\d+(?:(?:[ .-]{1,2}|\s?\(\d{1,4}\)\s?)\d+){0,7}"),
    # TOML `api_hash = "..."` and JSON `"api_hash": "..."` put 4 non-word characters before the value.
    "api_hash": re.compile(r"api_hash\W{0,6}[0-9A-Fa-f]{32}", re.I),
    "email": re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
}
# The only noreply addresses accepted; any other address on a real domain is a finding.
NOREPLY = frozenset({"noreply@users.noreply.github.com", "noreply@anthropic.com", "noreply@github.com"})

# (rule, tracked path or None for any, exact matched text or None for any, reason). Keep it minimal.
# Every entry must admit at least one finding of the scanned tree, and a scoped entry must name a
# scanned file; otherwise `test_allowlist_has_no_stale_entries` fails. The values are split inside the
# digits so that this file does not match its own path-scoped entries.
ALLOW = (
    ("user_path", None, "/Users/" "example", "documentation placeholder account name"),
    ("user_path", "tests/test_connect_cli.py", "/Users/" "SYNTHETIC_SECRET_SENTINEL",
     "synthetic leak sentinel; the test asserts it is never echoed"),
    ("chat_id", "tests/test_schemas.py", "-100" "1234567890", "sequential-digit synthetic supergroup id"),
    ("chat_id", "tests/test_reply_relative_datetime_targets.py", "-100" "3456789012",
     "sequential-digit synthetic supergroup id"),
    ("message_id", "tests/test_reply_relative_datetime_targets.py", "MESSAGE_ID = 104" "8576",
     "synthetic message id constant (2**20)"),
    ("message_id", "tests/test_forum_tdjson.py", "message_id': 104" "8576", "synthetic TDLib message id (2**20)"),
    ("message_id", "tests/test_topic_history_tdjson.py", "message_id=125" "82912",
     "synthetic TDLib message id (12 * 2**20)"),
    ("message_id", "tests/test_topic_history_tdjson.py", "message_id':125" "82912",
     "the same synthetic id in the expected request"),
    ("phone", "tests/test_archive_edit.py", "+1202555" "0123", "reserved fictional 555-01xx number in a fixture"),
    ("phone", "tests/test_onboarding.py", "+1234" "567890", "sequential-digit synthetic phone number"),
    ("api_hash", "tests/test_bundle_assembled_install.py", "api_hash" '":"' + "0" * 32,
     "all-zero synthetic api_hash in a bundle fixture"),
    ("api_hash", "tests/test_bundle_archive.py", "API_HASH = '" "0123456789abcdef" "0123456789abcdef",
     "sequential-hex synthetic api_hash in a bundle fixture"),
    ("api_hash", "tests/test_bundle_install.py", "api_hash = '" "fedcba9876543210" "fedcba9876543210",
     "reversed sequential-hex synthetic api_hash in a bundle fixture"),
    ("email", "docs/licenses/Pillow.txt", None,
     "verbatim third-party license text quotes upstream maintainer addresses"),
    ("email", "tests/test_archive_edit.py", "mail@" "x.io", "synthetic address inside a styled-text fixture"),
)


def synthetic_email(value: str) -> bool:
    """The listed noreply addresses and RFC 2606 example/reserved domains are not personal."""
    _local, _, domain = value.lower().rpartition("@")
    labels = domain.split(".")
    return (value.lower() in NOREPLY or labels[-1] in {"example", "invalid", "test", "localhost"}
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


def phone_value(candidate: str) -> str | None:
    """The shortest prefix of the candidate's digit groups with 10-15 digits, or None."""
    digits = 0
    for group in re.finditer(r"\d+", candidate):
        digits += len(group.group(0))
        if digits >= 10:
            return candidate[:group.end()] if digits <= 15 else None
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
                value = phone_value(match.group(0)) if rule == "phone" else match.group(0)
                if value is None:
                    continue
                hit = allowed(rule, path, value)
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


def stale_allowlist(scanned: set, used: set) -> list[str]:
    """Entries that admitted no finding of the scanned tree, or name a file it does not hold."""
    return [f"{rule} {where}" for index, (rule, where, _text, _reason) in enumerate(ALLOW)
            if index not in used or (where is not None and where not in scanned)]


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
        for rule, _where, _text, reason in ALLOW:
            self.assertIn(rule, RULES)
            self.assertTrue(reason.strip())
        self.assertEqual([], stale_allowlist(self.scanned, self.used), "stale allowlist entries")

    def test_stale_or_misplaced_allowlist_entries_fail(self) -> None:
        everything = set(range(len(ALLOW)))
        scoped = {where for _rule, where, _text, _reason in ALLOW if where is not None}
        self.assertEqual([f"{rule} {where}" for rule, where, _text, _reason in ALLOW],
                         stale_allowlist(scoped, set()))
        # a deleted or renamed scoped file fails even if its entry was used elsewhere
        gone = "tests/test_onboarding.py"
        self.assertEqual(["phone " + gone], stale_allowlist(scoped - {gone}, everything))
        self.assertEqual([], stale_allowlist(scoped, everything))

    def test_planted_lines_fail_each_rule(self) -> None:
        literal = "/srv/" "private-home"
        planted = (
            ("user_path", "cd /Users/" "alice/project"),
            ("home_path", "cd /home/" "bob/project"),
            ("home_path", "cd C:\\Users\\" "carol\\project"),
            ("other_home", "cd ~" "alice/project"),
            ("other_home", "see (~" "bob_2/notes)"),
            ("claude_project", "see .claude/projects/" "-Users-" "alice-telegram-mcp/memory"),
            ("local_dir", "see ~/telegram-mcp-" "052-runtime/venv"),
            ("local_dir", "copy from ~/.codex/" "research/run"),
            ("local_dir", "clone tg-" "dev-7"),
            ("message_id", "sent msg 6312" "42752"),
            ("message_id", "sent message 6312" "42752"),
            ("message_id", "sent msg  6312" "42752"),
            ("message_id", "reply message_id=6312" "42752"),
            ("message_id", "reply Message_ID: 6312" "42752"),
            ("message_id", 'json {"msg_id": 6312' "42752}"),
            ("message_id", 'json {"message_id": "6312' '42752"}'),
            ("message_id", "toml message_id = '6312" "42752'"),
            ("chat_id", "chat -100" "9876543210"),
            ("phone", "call +4915" "112345678"),
            ("phone", "call (+7999" "1234567)"),
            ("phone", "max +12345" "6789012345"),
            ("phone", "call +7 999 1" "23-45-67"),
            ("phone", "call +49 151 1" "2345678"),
            ("phone", "call +1234 5" "67 890"),
            ("phone", "call +1-202-5" "55-0199"),
            ("phone", "call +7999" "1234567 2026-10-21"),
            ("phone", "call +4915" "112345678 123"),
            ("phone", "call +7 (999) 1" "23-45-67"),
            ("phone", "call +7.999.1" "23.45.67"),
            ("phone", "call +7  999 1" "23 45 67"),
            ("api_hash", "api_hash=" "0123456789abcdef" "0123456789ABCDEF"),
            ("api_hash", 'API_HASH: "' "0123456789abcdef" '0123456789abcdef"'),
            ("api_hash", 'toml api_hash = "' "0123456789abcdef" '0123456789abcdef"'),
            ("api_hash", 'json {"api_hash": "' "0123456789abcdef" '0123456789abcdef"}'),
            ("email", "mail bob@" "corp.com"),
            ("email", "mail noreply@" "corp.com"),
            ("email", "mail 12345+bob@" "users.noreply.github.com"),
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
            ("docs/a.md", "By: noreply@" "users.noreply.github.com, noreply@" "anthropic.com, noreply@" "github.com"),
            ("docs/a.md", "tomlkit==0.13.3 abc@1.2.3 @pytest.mark.skip"),
            ("docs/a.md", "cd ~/project; ~~struck~~/x; a~b/c; message 12345; messages: 6312427; 1+1234567890"),
            ("docs/a.md", "call +12 345 678; +1 2 3; ref -Users/x; api_hash=" "0123456789abcdef"),
            ("docs/a.md", "1+1234 567 890; v1.2.3+4567-8901; message_id = '12345'; api_hash = 12"),
            ("tests/test_reply_relative_datetime_targets.py", "MESSAGE_ID = 104" "8576"),
            ("tests/test_bundle_archive.py", "    API_HASH = '" "0123456789abcdef" "0123456789abcdef'"),
            ("tests/test_bundle_install.py", "api_hash = '" "fedcba9876543210" "fedcba9876543210'"),
            ("tests/test_archive_edit.py", "phone +1202555" "0123"),
            ("tests/test_onboarding.py", "phone +1234" "567890"),
            ("tests/test_bundle_assembled_install.py", '{"api_hash' '":"' + "0" * 32 + '"}'),
        )
        for path, line in clean:
            with self.subTest(line=line):
                self.assertEqual([], scan_text(path, line))
        # allowlist entries stay scoped to their file
        self.assertEqual([("tests/other.py", 1, "chat_id")], scan_text("tests/other.py", "id -100" "1234567890"))
        self.assertEqual([("tests/other.py", 1, "user_path")],
                         scan_text("tests/other.py", "root /Users/" "SYNTHETIC_SECRET_SENTINEL/.codex"))
        self.assertEqual([("tests/other.py", 1, "phone")], scan_text("tests/other.py", "phone +1234" "567890"))
        self.assertEqual([("tests/test_onboarding.py", 1, "phone")],
                         scan_text("tests/test_onboarding.py", "phone +1234" "567891"))

    def test_planted_file_fails_a_tree_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "clean.md").write_text("nothing private here\n")
            (root / "docs" / "dirty.md").write_text("fine\nsent msg 6333" "39904 today\n")
            (root / "blob.bin").write_bytes(b"\0msg 6333" b"39904")
            (root / ".venv").mkdir()
            (root / ".venv" / "skipped.txt").write_text("cd /Users/" "alice/x\n")
            findings, scanned = scan_tree(root)
            self.assertEqual([("docs/dirty.md", 2, "message_id")], findings)
            self.assertEqual({"docs/clean.md", "docs/dirty.md"}, scanned)


if __name__ == "__main__":
    unittest.main()
