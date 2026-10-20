from __future__ import annotations

import importlib
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from pathlib import Path
from typing import Any
from unittest.mock import patch

from telegram_search_mcp.config import LAUNCH_AGENT_LABEL


class LaunchAgentTests(unittest.TestCase):
    def test_install_waits_for_old_service_retirement_before_bootstrap(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        checks = 0
        actions = []

        def runner(arguments, **kwargs):
            nonlocal checks
            action = arguments[1]
            actions.append(action)
            if action == "print":
                checks += 1
                return subprocess.CompletedProcess(arguments, 113 if checks >= 3 else 0, "", "")
            if action == "bootstrap" and checks < 3:
                return subprocess.CompletedProcess(arguments, 5, "", "service still retiring")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, patch("time.sleep"):
            launch_agent.install_launch_agent(plist_path=Path(directory) / "broker.plist", runner=runner, preflight=lambda: None)
        self.assertEqual(actions, ["bootout", "print", "print", "print", "bootstrap"])

    def test_install_does_not_bootstrap_when_previous_service_never_retires(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        actions = []

        def runner(arguments, **kwargs):
            actions.append(arguments[1])
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, patch("time.sleep"):
            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.install_launch_agent(plist_path=Path(directory) / "broker.plist", runner=runner, preflight=lambda: None)
        self.assertNotIn("bootstrap", actions)

    def test_pyproject_exposes_the_broker_lifecycle_command(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with (repository / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)

        self.assertEqual(
            project["project"]["scripts"]["telegram-search-broker"],
            "telegram_search_mcp.launch_agent:main",
        )

    def test_generated_plist_is_valid_private_and_contains_no_sensitive_inputs(self) -> None:
        try:
            launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        except ModuleNotFoundError:
            self.fail("launch agent support is not implemented")

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            plist_path = root / f"{LAUNCH_AGENT_LABEL}.plist"
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(Path(sys.executable))
            (root / ".venv" / "pyvenv.cfg").write_text(
                "\n".join(
                    (
                        f"home = {Path(sys.base_prefix) / 'bin'}",
                        "include-system-site-packages = false",
                        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    )
                ),
                encoding="utf-8",
            )
            package = (
                root
                / ".venv"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
                / "telegram_search_mcp"
            )
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            launch_agent.write_launch_agent(
                plist_path=plist_path,
                python_executable=venv_python,
            )
            payload = plistlib.loads(plist_path.read_bytes())
            lint = subprocess.run(
                ["plutil", "-lint", str(plist_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            mode = stat.S_IMODE(plist_path.stat().st_mode)
            import_check = subprocess.run(
                [
                    payload["ProgramArguments"][0],
                    "-c",
                    "import telegram_search_mcp",
                ],
                cwd=root,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(lint.returncode, 0, lint.stderr)
        self.assertEqual(import_check.returncode, 0, import_check.stderr)
        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(venv_python.absolute()), "-m", "telegram_search_mcp.launch_agent", "run"],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["ProcessType"], "Background")
        self.assertEqual(payload["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(payload["Umask"], 0o077)
        self.assertEqual(mode, 0o600)
        serialized = plistlib.dumps(payload).decode("utf-8")
        for forbidden in (
            "api_id",
            "api_hash",
            "target",
            "query",
            "message",
            "snippet",
            "cursor",
        ):
            self.assertNotIn(forbidden, serialized.casefold())

    def test_install_preflights_then_uses_argument_list_launchctl_calls(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        calls: list[list[str]] = []
        preflights: list[str] = []

        def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 113 if arguments[1] == "print" else 0, "", "")

        with tempfile.TemporaryDirectory() as parent:
            plist_path = Path(parent) / "broker.plist"
            launch_agent.install_launch_agent(
                plist_path=plist_path,
                python_executable=Path(sys.executable),
                runner=runner,
                preflight=lambda: preflights.append("available"),
                uid=501,
            )

        self.assertEqual(preflights, ["available"])
        self.assertEqual(
            calls,
            [
                ["/bin/launchctl", "bootout", f"gui/501/{LAUNCH_AGENT_LABEL}"],
                ["/bin/launchctl", "print", f"gui/501/{LAUNCH_AGENT_LABEL}"],
                ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)],
            ],
        )

    def test_existing_symlink_plist_is_rejected_without_touching_target(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            target = root / "sentinel"
            target.write_text("keep", encoding="utf-8")
            plist_path = root / "broker.plist"
            plist_path.symlink_to(target)

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertTrue(plist_path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_group_or_world_writable_launch_agent_directory_is_rejected(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            unsafe = Path(parent) / "unsafe"
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o777)
            plist_path = unsafe / "broker.plist"

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertFalse(plist_path.exists())

    def test_existing_foreign_regular_plist_is_not_overwritten(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        with tempfile.TemporaryDirectory() as parent:
            plist_path = Path(parent) / "broker.plist"
            original = plistlib.dumps(
                {"Label": "com.example.unrelated", "ProgramArguments": ["/bin/true"]}
            )
            plist_path.write_bytes(original)

            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.write_launch_agent(
                    plist_path=plist_path,
                    python_executable=Path(sys.executable),
                )

            self.assertEqual(plist_path.read_bytes(), original)

    def test_uninstall_removes_only_a_plist_with_the_complete_managed_shape(self) -> None:
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        calls: list[list[str]] = []

        def runner(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            managed = root / "managed.plist"
            launch_agent.write_launch_agent(
                plist_path=managed,
                python_executable=Path(sys.executable),
            )
            launch_agent.uninstall_launch_agent(
                plist_path=managed,
                runner=runner,
                uid=501,
            )
            self.assertFalse(managed.exists())

            foreign = root / "foreign.plist"
            foreign.write_bytes(
                plistlib.dumps(
                    {
                        "Label": LAUNCH_AGENT_LABEL,
                        "ProgramArguments": ["/bin/echo", "not-owned"],
                    }
                )
            )
            with self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.uninstall_launch_agent(
                    plist_path=foreign,
                    runner=runner,
                    uid=501,
                )

            self.assertTrue(foreign.exists())
            self.assertEqual(len(calls), 1)


class ConnectJobLifecycleTests(unittest.TestCase):
    """Phase D launchd surface: stopping the enrolled broker and the transient connect job."""

    def setUp(self) -> None:
        self.launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.plist = self.root / "broker.plist"
        self.calls: list[list[str]] = []

    def installed(self) -> bytes:
        value = self.launch_agent.launch_agent_payload(python_executable=Path(sys.executable))
        self.plist.write_bytes(plistlib.dumps(value))
        self.plist.chmod(0o600)
        arguments = value["ProgramArguments"]
        self.registration = ("path = " + str(self.plist) + "\nprogram = " + arguments[0]
                             + "\narguments = {\n" + "\n".join(arguments) + "\n}\n")
        return self.plist.read_bytes()

    def runner(self, *, bootout=0, retired_after=1, matching=True):
        prints = 0

        def run(arguments, **kwargs):
            nonlocal prints
            self.calls.append(list(arguments))
            if arguments[1] == "print":
                prints += 1
                if prints > retired_after:
                    return subprocess.CompletedProcess(arguments, 113, "", "")
                return subprocess.CompletedProcess(arguments, 0, self.registration if matching else "program = /x", "")
            if arguments[1] == "bootout":
                return subprocess.CompletedProcess(arguments, bootout, "", "")
            raise AssertionError(arguments)

        return run

    def test_label_is_derived_like_the_broker_label(self) -> None:
        from telegram_search_mcp.config import KEYCHAIN_SERVICE
        self.assertEqual(self.launch_agent.CONNECT_JOB_LABEL, KEYCHAIN_SERVICE + ".connect")
        self.assertEqual(LAUNCH_AGENT_LABEL, KEYCHAIN_SERVICE + ".broker")

    def test_stop_validates_then_boots_out_only_the_broker_label_and_keeps_the_plist(self) -> None:
        before = self.installed()
        sleeps: list[float] = []
        self.launch_agent.stop_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
            runner=self.runner(retired_after=2), uid=501, sleeper=sleeps.append)
        service = "gui/501/" + LAUNCH_AGENT_LABEL
        self.assertEqual(self.calls, [["/bin/launchctl", "print", service], ["/bin/launchctl", "bootout", service],
                                      ["/bin/launchctl", "print", service], ["/bin/launchctl", "print", service]])
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(self.plist.read_bytes(), before)

    def test_stop_refuses_before_any_bootout_on_every_validation_failure(self) -> None:
        self.installed()
        cases = {
            "live registration differs": dict(matching=False),
            "registration reported absent": dict(retired_after=0),
        }
        for name, options in cases.items():
            with self.subTest(name), self.assertRaises(self.launch_agent.LaunchAgentError):
                self.calls.clear()
                try:
                    self.launch_agent.stop_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                        runner=self.runner(**options), uid=501, sleeper=lambda _: None)
                finally:
                    self.assertNotIn("bootout", [call[1] for call in self.calls])
        self.plist.write_bytes(plistlib.dumps({"Label": "foreign"}))
        self.plist.chmod(0o600)
        foreign = self.plist.read_bytes()
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.calls.clear()
            self.launch_agent.stop_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                runner=self.runner(), uid=501, sleeper=lambda _: None)
        self.assertEqual((self.calls, self.plist.read_bytes()), ([], foreign))
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.stop_enrolled_broker(plist_path=self.root / "missing" / "x.plist",
                python_executable=Path(sys.executable), runner=self.runner(), uid=501, sleeper=lambda _: None)

    def test_stop_reports_a_failed_bootout_or_a_service_that_never_retires(self) -> None:
        self.installed()
        for options in [dict(bootout=5), dict(retired_after=10 ** 6)]:
            with self.subTest(options), self.assertRaises(self.launch_agent.LaunchAgentError):
                self.launch_agent.stop_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                    runner=self.runner(**options), uid=501, sleeper=lambda _: None)

    def test_connect_job_state_reads_the_jobs_own_state_line_only(self) -> None:
        def state(code, text):
            return self.launch_agent.connect_job_state(
                runner=lambda a, **k: subprocess.CompletedProcess(a, code, text, ""), uid=501)
        self.assertEqual(state(113, ""), "absent")
        self.assertEqual(state(0, "x = {\n\tstate = running\n}"), "busy")
        self.assertEqual(state(0, "x = {\n\tstate = not running\n\tendpoints = {\n\t\tstate = running\n\t}\n}"), "idle")
        self.assertEqual(state(0, "x = {\n\tstate = exited\n}"), "idle")
        # Only an explicit idle state is idle: anything else may still be or become a live helper.
        for text in ["x = {\n\tstate = spawn scheduled\n}", "x = {\n\tstate = waiting\n}",
                     "x = {\n\tstate = spawning\n\tendpoints = {\n\t\tstate = not running\n\t}\n}",
                     "x = {\n\tactive count = 0\n}", "x = {\n\tstate = not running yet\n}"]:
            with self.subTest(text=text):
                self.assertEqual(state(0, text), "busy")
        for code, text in [(5, ""), (0, None), (0, "state = running\n" + "x" * 70000)]:
            with self.subTest(code=code), self.assertRaises(self.launch_agent.LaunchAgentError):
                state(code, text)
        for error in (PermissionError(), subprocess.TimeoutExpired("launchctl", 10)):
            with self.subTest(error=error), self.assertRaises(self.launch_agent.LaunchAgentError):
                self.launch_agent.connect_job_state(runner=lambda a, **k: (_ for _ in ()).throw(error), uid=501)

    def test_connect_job_plist_is_a_valid_exact_owner_only_definition(self) -> None:
        run = self.root / "run"
        path = run / (self.launch_agent.CONNECT_JOB_LABEL + ".plist")
        self.launch_agent.write_connect_job(plist_path=path, python_executable=Path(sys.executable))
        value = plistlib.loads(path.read_bytes())
        self.assertEqual(value, {
            "Label": self.launch_agent.CONNECT_JOB_LABEL,
            "ProgramArguments": [str(Path(sys.executable).absolute()), "-m", "telegram_search_mcp.setup_cli", "connect-helper"],
            "RunAtLoad": True, "KeepAlive": False, "LimitLoadToSessionType": "Aqua", "ProcessType": "Interactive",
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null", "Umask": 63,
            "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        lint = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_connect_job_plist_creation_refuses_unsafe_directories_and_names(self) -> None:
        run = self.root / "loose"
        run.mkdir()
        run.chmod(0o755)  # The process umask may be restrictive; set the mode explicitly.
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.write_connect_job(plist_path=run / "job.plist", python_executable=Path(sys.executable))
        self.assertEqual(list(run.iterdir()), [])
        private = self.root / "private"
        private.mkdir(mode=0o700)
        victim = self.root / "victim.txt"
        victim.write_bytes(b"precious")
        (private / "link.plist").symlink_to(victim)
        (private / "dir.plist").mkdir()
        (private / "multi.plist").write_bytes(b"x")
        os.link(private / "multi.plist", self.root / "second-name")
        for name in ["link.plist", "dir.plist", "multi.plist"]:
            with self.subTest(name), self.assertRaises(self.launch_agent.LaunchAgentError):
                self.launch_agent.write_connect_job(plist_path=private / name, python_executable=Path(sys.executable))
        self.assertEqual(victim.read_bytes(), b"precious")
        self.assertEqual((self.root / "second-name").read_bytes(), b"x")

    def test_connect_job_plist_is_created_exclusively_never_opening_a_name_that_appears_late(self) -> None:
        private = self.root / "private"
        private.mkdir(mode=0o700)
        path = private / "job.plist"
        path.write_bytes(b"planted between the check and the create")
        planted = path.read_bytes()
        real_stat = os.stat
        def late_stat(name, *args, **kwargs):  # The name is absent at the check and present at the create.
            if kwargs.get("dir_fd") is not None and name == "job.plist":
                raise FileNotFoundError(name)
            return real_stat(name, *args, **kwargs)
        with unittest.mock.patch.object(self.launch_agent.os, "unlink"), \
             unittest.mock.patch.object(self.launch_agent.os, "stat", side_effect=late_stat):
            with self.assertRaises(self.launch_agent.LaunchAgentError):
                self.launch_agent.write_connect_job(plist_path=path, python_executable=Path(sys.executable))
        self.assertEqual(path.read_bytes(), planted)

    def test_remove_connect_job_only_unlinks_its_own_regular_file(self) -> None:
        private = self.root / "private"
        private.mkdir(mode=0o700)
        victim = self.root / "victim.txt"
        victim.write_bytes(b"precious")
        (private / "link.plist").symlink_to(victim)
        (private / "dir.plist").mkdir()
        (private / "job.plist").write_bytes(b"x")
        self.assertFalse(self.launch_agent.remove_connect_job(private / "link.plist"))
        self.assertFalse(self.launch_agent.remove_connect_job(private / "dir.plist"))
        self.assertFalse(self.launch_agent.remove_connect_job(private / "missing.plist"))
        self.assertFalse(self.launch_agent.remove_connect_job(self.root / "nowhere" / "job.plist"))
        self.assertTrue(self.launch_agent.remove_connect_job(private / "job.plist"))
        self.assertEqual(victim.read_bytes(), b"precious")

    def test_start_leaves_a_running_job_untouched_and_cleans_up_a_failed_bootstrap(self) -> None:
        private = self.root / "private"
        private.mkdir(mode=0o700)
        path = private / "job.plist"
        label = self.launch_agent.CONNECT_JOB_LABEL
        def running(arguments, **kwargs):
            self.calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, "state = running\n", "")
        self.assertEqual(self.launch_agent.start_connect_job(plist_path=path, runner=running, uid=501), "running")
        self.assertEqual([call[1] for call in self.calls], ["print"])
        self.assertFalse(path.exists())
        def spawning(arguments, **kwargs):
            self.calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, "state = spawn scheduled\n", "")
        self.calls.clear()
        self.assertEqual(self.launch_agent.start_connect_job(plist_path=path, runner=spawning, uid=501), "running")
        self.assertEqual([call[1] for call in self.calls], ["print"])
        def refusing(arguments, **kwargs):
            if arguments[1] == "print":
                return subprocess.CompletedProcess(arguments, 113, "", "")
            return subprocess.CompletedProcess(arguments, 5, "", "")
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.start_connect_job(plist_path=path, runner=refusing, uid=501)
        self.assertFalse(path.exists())
        self.assertIn(label, self.launch_agent.connect_job_payload()["Label"])

    def test_stop_connect_job_boots_out_only_the_connect_label_and_tolerates_an_already_gone_job(self) -> None:
        label = "gui/501/" + self.launch_agent.CONNECT_JOB_LABEL
        def runner(bootout_code, print_codes):
            codes = list(print_codes)
            def run(arguments, **kwargs):
                self.calls.append(list(arguments))
                if arguments[1] == "bootout":
                    return subprocess.CompletedProcess(arguments, bootout_code, "", "")
                return subprocess.CompletedProcess(arguments, codes.pop(0) if len(codes) > 1 else codes[0], "", "")
            return run
        sleeps: list[float] = []
        self.launch_agent.stop_connect_job(runner=runner(0, [0, 113]), uid=501, sleeper=sleeps.append)
        self.assertEqual(self.calls, [["/bin/launchctl", "bootout", label], ["/bin/launchctl", "print", label],
                                      ["/bin/launchctl", "print", label]])
        self.assertEqual(sleeps, [0.1])
        self.launch_agent.stop_connect_job(runner=runner(113, [113]), uid=501, sleeper=sleeps.append)
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.stop_connect_job(runner=runner(5, [0]), uid=501, sleeper=lambda _: None)
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.stop_connect_job(runner=runner(0, [0]), uid=501, sleeper=lambda _: None)
        self.assertNotIn(LAUNCH_AGENT_LABEL, " ".join(" ".join(call) for call in self.calls))

    def test_restore_never_creates_a_broker_definition_and_starts_only_an_unloaded_matching_one(self) -> None:
        agents = self.root / "LaunchAgents"
        plist = agents / "broker.plist"
        def runner(loaded, registration=None):
            def run(arguments, **kwargs):
                self.calls.append(list(arguments))
                if arguments[1] == "print":
                    return subprocess.CompletedProcess(arguments, 0 if loaded else 113, registration or "", "")
                if arguments[1] == "bootstrap":
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                raise AssertionError(arguments)
            return run
        for missing in [plist, self.plist]:  # No directory at all, then a directory without a plist.
            with self.subTest(missing=str(missing)), self.assertRaises(self.launch_agent.LaunchAgentError):
                self.launch_agent.restore_enrolled_broker(plist_path=missing, python_executable=Path(sys.executable),
                                                          runner=runner(False), uid=501)
        self.assertFalse(agents.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])
        self.assertEqual(self.calls, [])
        before = self.installed()
        self.launch_agent.restore_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                                                  runner=runner(False), uid=501)
        self.assertEqual([call[1:3] for call in self.calls], [["print", "gui/501/" + LAUNCH_AGENT_LABEL],
                                                             ["bootstrap", "gui/501"]])
        self.calls.clear()
        self.launch_agent.restore_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                                                  runner=runner(True, self.registration), uid=501)
        self.assertEqual([call[1] for call in self.calls], ["print"])
        self.assertEqual(self.plist.read_bytes(), before)
        self.plist.write_bytes(plistlib.dumps({"Label": "foreign"}))
        self.plist.chmod(0o600)
        self.calls.clear()
        with self.assertRaises(self.launch_agent.LaunchAgentError):
            self.launch_agent.restore_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                                                      runner=runner(False), uid=501)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()


class StartIfStoppedTests(unittest.TestCase):
    def test_starting_the_broker_never_kills_a_running_one(self):
        launch_agent = importlib.import_module("telegram_search_mcp.launch_agent")
        calls = []
        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        launch_agent.start_launch_agent_if_stopped(runner=runner, uid=501)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], [launch_agent.LAUNCHCTL, "kickstart"])
        self.assertNotIn("-k", calls[0])
        self.assertEqual(calls[0][-1], "gui/501/" + launch_agent.LAUNCH_AGENT_LABEL)
