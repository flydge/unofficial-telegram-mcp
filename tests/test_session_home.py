"""The conftest session HOME: every run removes the one directory it made, and nothing else (0.58 review fix).

A run that pytest never configures (a usage error) and an import outside pytest used to leave an empty
tgmcp-test-home-* behind in $TMPDIR. The runs here are hermetic subprocesses with their own $TMPDIR.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from conftest import HOME_PREFIX, TEMP_ROOT, TEST_HOME, remove_session_home

TESTS = Path(__file__).resolve().parent
PROBE = '''
import os
from pathlib import Path


def test_home_is_a_private_session_directory_in_tmpdir():
    home = Path(os.environ["HOME"])
    assert home.parent == Path(os.environ["TGMCP_PROBE_TMPDIR"]).resolve()
    assert home.name.startswith("tgmcp-test-home-") and home.stat().st_mode & 0o777 == 0o700
    (home / "Library").mkdir()  # a HOME the run wrote into is removed as a whole
'''
OUTSIDE_PYTEST = '''
import os
import sys
sys.path.insert(0, sys.argv[1])
import conftest
home = conftest.TEST_HOME
(home / "written").write_text("x")
sys.stdout.flush()
child = os.fork()
if child == 0:
    sys.exit(0)  # the forked child runs the exit handlers it inherited
os.waitpid(child, 0)
print(home, home.is_dir())
'''


class SessionHomeRunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(mode=0o700)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        shutil.copyfile(TESTS / "conftest.py", self.tests / "conftest.py")
        (self.tests / "test_probe.py").write_text(PROBE)
        (self.root / "pytest.ini").write_text("[pytest]\n")
        self.env = {**{name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")},
                    "TMPDIR": str(self.tmp), "TGMCP_PROBE_TMPDIR": str(self.tmp)}

    def pytest(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(self.root),
                               *arguments, str(self.tests)], cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=120)

    def homes_left(self) -> list[str]:
        return sorted(path.name for path in self.tmp.iterdir() if path.name.startswith(HOME_PREFIX))

    def test_a_passing_run_leaves_no_session_home(self) -> None:
        result = self.pytest()
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])
        self.assertIn("1 passed", result.stdout)
        self.assertEqual(self.homes_left(), [])

    def test_a_run_that_ends_in_a_usage_error_leaves_no_session_home(self) -> None:
        result = self.pytest("--no-such-option")
        self.assertEqual(result.returncode, 4, result.stdout[-3000:] + result.stderr[-2000:])
        self.assertEqual(self.homes_left(), [])

    def test_an_import_outside_pytest_removes_its_home_at_exit_and_a_forked_child_never_does(self) -> None:
        result = subprocess.run([sys.executable, "-c", OUTSIDE_PYTEST, str(self.tests)], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        home, alive = result.stdout.split()
        self.assertEqual(alive, "True")  # the child's exit left its parent's HOME alone
        self.assertEqual(Path(home).parent, self.tmp)
        self.assertFalse(os.path.lexists(home))
        self.assertEqual(self.homes_left(), [])


class RemoveSessionHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.outside = self.root / "outside"
        self.outside.mkdir(mode=0o700)
        (self.outside / "kept").write_text("x")

    def home(self, name: str = HOME_PREFIX + "abc", mode: int = 0o700, parent: Path | None = None) -> Path:
        path = (parent or self.root) / name
        path.mkdir()
        os.chmod(path, mode)
        return path

    def assert_kept(self, path: Path) -> None:
        self.assertFalse(remove_session_home(path, self.root))
        self.assertTrue(os.path.lexists(path))
        self.assertEqual((self.outside / "kept").read_text(), "x")

    def test_this_session_home_is_owner_only_inside_the_temporary_root(self) -> None:
        self.assertEqual((TEST_HOME.parent, TEST_HOME.stat().st_mode & 0o777), (TEMP_ROOT, 0o700))
        self.assertEqual(Path(os.environ["HOME"]), TEST_HOME)

    def test_an_owner_only_session_home_is_removed_without_following_a_link_inside_it(self) -> None:
        home = self.home()
        (home / "Library").mkdir()
        (home / "Library" / "file").write_text("x")
        (home / "Library" / "link").symlink_to(self.outside, target_is_directory=True)
        self.assertTrue(remove_session_home(home, self.root))
        self.assertFalse(os.path.lexists(home))
        self.assertEqual((self.outside / "kept").read_text(), "x")
        self.assertFalse(remove_session_home(home, self.root))  # already gone

    def test_a_link_in_place_of_the_home_is_never_followed(self) -> None:
        link = self.root / (HOME_PREFIX + "link")
        link.symlink_to(self.outside, target_is_directory=True)
        os.chmod(self.outside, 0o700)
        self.assert_kept(link)

    def test_a_home_that_is_not_owner_only_or_not_directly_in_the_root_or_misnamed_is_kept(self) -> None:
        self.assert_kept(self.home(mode=0o750))
        self.assert_kept(self.home(parent=self.outside))
        self.assert_kept(self.home(name="tgmcp-other-home-abc"))
        file = self.root / (HOME_PREFIX + "file")
        file.write_text("x")
        os.chmod(file, 0o600)
        self.assert_kept(file)


if __name__ == "__main__":
    unittest.main()
