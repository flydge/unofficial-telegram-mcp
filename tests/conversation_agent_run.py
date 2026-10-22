"""Run the conversation scenarios through a real agent and score them with the same rubrics (0.54 item E).

Not collected by pytest, and never run by the suite: it starts a real model. Synthetic data only: the
agent's only Telegram server is tests/conversation_demo_server.py, which never reaches Telegram.

    python tests/conversation_agent_run.py --agent codex [--scenario NAME ...] [--timeout SECONDS]
        [--model MODEL] [--out DIRECTORY]
    python tests/conversation_agent_run.py --agent claude [--claude-bin PATH] ...

codex: `codex exec --ignore-user-config` (documented in `codex exec --help`: the user's config.toml
is not loaded; authentication still comes from CODEX_HOME) plus `-c mcp_servers.telegram_search.*`
overrides that point the server at the demo, with an ephemeral session, a read-only sandbox and an
empty working directory. Nothing is written to the Codex configuration. The override uses the
usual server name on purpose: had a user entry been loaded anyway, it would be replaced, never run
beside the demo. With --skills none (the default) Codex runs with HOME set to an empty private
directory and CODEX_HOME at the existing Codex home, so user skills under HOME (such as an installed
telegram-search skill) are not offered and the run measures the MCP surface alone; --skills user
keeps the caller's HOME.

claude: `claude -p --mcp-config <temporary JSON> --strict-mcp-config`, with CLAUDE_CONFIG_DIR set
to a fresh temporary directory so the user's Claude configuration is neither read nor written.
Without a usable login there, the run is reported as unavailable.

Each scenario gets its own owner-only directory under --out (default: a new temporary directory)
holding the demo transcript, the agent's event stream and its final answer. The report (JSON on
stdout and report.json) carries the commit, agent version, date and, per scenario, the rubric
result. Exit status: 0 when every scenario passed, 1 when one failed, 2 when the agent is unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import conversation_rubrics as rubrics  # noqa: E402

SERVER = HERE / "conversation_demo_server.py"
SERVER_NAME = "telegram_search"
UNAVAILABLE_MARKERS = ("invalid api key", "please run /login", "not logged in", "authentication_error",
                       "oauth token", "login required", "credit balance")


def toml(value) -> str:
    """A TOML value for `codex -c`: JSON strings and arrays are valid TOML basic strings and arrays."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {toml(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return json.dumps(value)


def server_environment() -> dict[str, str]:
    environment = {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("TMPDIR"):
        environment["TMPDIR"] = os.environ["TMPDIR"]
    return environment


def run_bounded(command: list[str], *, cwd: Path, env: dict, timeout: float, stdout: Path) -> tuple[int | None, bool]:
    """Run with a hard deadline; the whole process group goes on timeout."""
    with stdout.open("wb") as sink:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=sink,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return None, True


def git_commit() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=30,
                                check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Codex:
    name = "codex"

    def __init__(self, binary: str | None, model: str | None, home: Path | None):
        self.binary = binary or shutil.which("codex")
        self.model = model
        self.home = home  # None keeps the caller's HOME (user skills visible)

    def environment(self) -> dict:
        if self.home is None:
            return dict(os.environ)
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        return {**os.environ, "HOME": str(self.home), "CODEX_HOME": codex_home}

    def version(self) -> str | None:
        if not self.binary:
            return None
        try:
            done = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=60,
                                  env=self.environment())
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() or None if done.returncode == 0 else None

    def command(self, scenario, transcript: Path, workdir: Path) -> tuple[list[str], dict]:
        server = f"mcp_servers.{SERVER_NAME}"
        overrides = {
            f"{server}.command": sys.executable,
            f"{server}.args": [str(SERVER), *rubrics.server_arguments(scenario, transcript)],
            f"{server}.env": server_environment(),
            f"{server}.startup_timeout_sec": 60,
            f"{server}.tool_timeout_sec": 120,
            # Codex's own per-call prompt has nobody to answer it; the owner dialog (scripted) still decides.
            f"{server}.default_tools_approval_mode": "approve",
            "approval_policy": "never",
        }
        command = [self.binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--cd", str(workdir), "--json", "--color", "never",
                   "--output-last-message", str(workdir / "answer.txt")]
        for key, value in overrides.items():
            command += ["-c", f"{key}={toml(value)}"]
        if self.model:
            command += ["--model", self.model]
        return command + [rubrics.PREAMBLE + scenario.prompt], self.environment()

    @staticmethod
    def answer(workdir: Path, events: Path) -> str | None:
        path = workdir / "answer.txt"
        return path.read_text("utf-8") if path.exists() and path.stat().st_size else None

    @staticmethod
    def observations(events: Path) -> dict:
        shell, other_mcp, errors = 0, set(), []
        for line in events.read_text("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if item.get("type") == "command_execution":
                shell += 1
            if item.get("type") == "mcp_tool_call" and item.get("server") not in (None, SERVER_NAME):
                other_mcp.add(item.get("server"))
            if event.get("type") in {"error", "turn.failed"}:
                errors.append(str(event.get("message") or event.get("error"))[:300])
        return {"shell_commands": shell, "other_mcp_servers": sorted(other_mcp), "errors": errors[:5]}


class ClaudeCode:
    name = "claude"

    def __init__(self, binary: str | None, model: str | None, config_dir: Path):
        self.binary = binary or shutil.which("claude")
        self.model = model
        self.config_dir = config_dir

    def environment(self) -> dict:
        return {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config_dir)}

    def version(self) -> str | None:
        if not self.binary:
            return None
        try:
            done = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=60,
                                  env=self.environment())
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() or None if done.returncode == 0 else None

    def command(self, scenario, transcript: Path, workdir: Path) -> tuple[list[str], dict]:
        config = workdir / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {SERVER_NAME: {
            "command": sys.executable, "args": [str(SERVER), *rubrics.server_arguments(scenario, transcript)],
            "env": server_environment()}}}), "utf-8")
        command = [self.binary, "-p", "--mcp-config", str(config), "--strict-mcp-config", "--output-format",
                   "stream-json", "--verbose", "--max-turns", "40", "--allowedTools", f"mcp__{SERVER_NAME}"]
        if self.model:
            command += ["--model", self.model]
        return command + [rubrics.PREAMBLE + scenario.prompt], self.environment()

    @staticmethod
    def answer(workdir: Path, events: Path) -> str | None:
        result = None
        for line in events.read_text("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                result = event["result"]
        return result

    @staticmethod
    def observations(events: Path) -> dict:
        shell = 0
        for line in events.read_text("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for block in ((event.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    shell += 1
        return {"shell_commands": shell}


def run_scenario(agent, scenario, base: Path, timeout: float) -> dict:
    workdir = base / f"{agent.name}-{scenario.name}"
    workdir.mkdir(mode=0o700)
    transcript = workdir / "transcript.jsonl"
    events = workdir / "events.jsonl"
    command, environment = agent.command(scenario, transcript, workdir)
    started = time.monotonic()
    code, timed_out = run_bounded(command, cwd=workdir, env=environment, timeout=timeout, stdout=events)
    answer = agent.answer(workdir, events)
    run = rubrics.Run.load(transcript)
    result = rubrics.score(scenario, run, answer)
    observed = agent.observations(events)
    result["checks"].append({"name": "no_shell", "passed": observed["shell_commands"] == 0, "required": False,
                             "detail": f"{observed['shell_commands']} shell command(s)"})
    if observed.get("other_mcp_servers"):
        result["checks"].append({"name": "only_demo_server", "passed": False, "required": True,
                                 "detail": ", ".join(observed["other_mcp_servers"])})
        result["passed"] = False
    stream = events.read_text("utf-8", errors="replace").casefold()
    unavailable = not run.calls and any(marker in stream for marker in UNAVAILABLE_MARKERS)
    return {**result, "agent": agent.name, "exit": code, "timed_out": timed_out, "unavailable": unavailable,
            "seconds": round(time.monotonic() - started, 1), "answer": (answer or "")[:2000],
            "observed": observed, "directory": str(workdir)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the conversation scenarios through a real agent.")
    parser.add_argument("--agent", choices=("codex", "claude"), required=True)
    parser.add_argument("--scenario", action="append", choices=sorted(rubrics.SCENARIOS) + ["all"],
                        help="repeatable; default: the five core scenarios; all adds the approval variants")
    parser.add_argument("--timeout", type=float, default=420.0, help="seconds per scenario (default 420)")
    parser.add_argument("--model")
    parser.add_argument("--skills", choices=("none", "user"), default="none",
                        help="codex: none hides skills under HOME (default); user keeps the caller's HOME")
    parser.add_argument("--codex-bin")
    parser.add_argument("--claude-bin")
    parser.add_argument("--out", type=Path, help="report directory (default: a new temporary directory)")
    arguments = parser.parse_args(argv)
    if not 30 <= arguments.timeout <= 1800:
        parser.error("--timeout must be 30..1800 seconds")
    base = arguments.out or Path(tempfile.mkdtemp(prefix="tgmcp-agent-run-"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    if arguments.agent == "codex":
        home = None
        if arguments.skills == "none":
            home = base / "agent-home"
            home.mkdir(mode=0o700, exist_ok=True)
        agent = Codex(arguments.codex_bin, arguments.model, home)
    else:
        config = base / "claude-config"
        config.mkdir(mode=0o700, exist_ok=True)
        agent = ClaudeCode(arguments.claude_bin, arguments.model, config)
    report = {"agent": agent.name, "version": agent.version(), "commit": git_commit(), "skills": arguments.skills,
              "date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "directory": str(base),
              "scenarios": []}
    if report["version"] is None:
        report["unavailable"] = f"{agent.name} --version failed or the binary is missing"
    else:
        names = arguments.scenario or list(rubrics.CORE)
        names = list(rubrics.SCENARIOS) if "all" in names else names
        for name in names:
            result = run_scenario(agent, rubrics.SCENARIOS[name], base, arguments.timeout)
            report["scenarios"].append(result)
            print(f"{name}: {'PASS' if result['passed'] else 'FAIL'} ({result['seconds']} s)", file=sys.stderr)
            if result["unavailable"]:
                report["unavailable"] = f"{agent.name} could not run non-interactively (no usable login)"
                break
    (base / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("unavailable"):
        return 2
    return 0 if all(result["passed"] for result in report["scenarios"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
