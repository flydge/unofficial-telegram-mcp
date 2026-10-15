"""Safe launchd lifecycle for the single Unofficial Telegram MCP broker."""

from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .broker import Broker, BrokerStartupError
from .config import (
    BROKER_LOCK_PATH,
    BROKER_SOCKET_PATH,
    KEYCHAIN_SERVICE,
    LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_PLIST,
    validate_tdlib_runtime,
)
from .session_owner import OwnerLeaseError, open_private_directory

LAUNCHCTL = "/bin/launchctl"
# Transient QR connect helper: same per-user derivation as the broker label.
CONNECT_JOB_LABEL = f"{KEYCHAIN_SERVICE}.connect"


class LaunchAgentError(RuntimeError):
    """Raised when launchd installation or lifecycle checks fail safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_preflight() -> None:
    from .credential_identity import preflight_credentials
    validate_tdlib_runtime()
    preflight_credentials()


def launch_agent_payload(*, python_executable: Path) -> dict[str, Any]:
    from .bundle_runtime import broker_arguments
    bundled = broker_arguments()
    executable = python_executable.absolute()
    resolved_executable = executable.resolve(strict=True)
    if not resolved_executable.is_file():
        raise LaunchAgentError("Python executable is invalid")
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": bundled or [
            str(executable),
            "-m",
            "telegram_search_mcp.launch_agent",
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {} if bundled else {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def _managed_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected_keys = {
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "KeepAlive",
        "ProcessType",
        "LimitLoadToSessionType",
        "Umask",
        "StandardOutPath",
        "StandardErrorPath",
        "EnvironmentVariables",
    }
    arguments = value.get("ProgramArguments")
    from .bundle_runtime import broker_arguments, BundleError
    try:
        bundled = broker_arguments()
    except BundleError:
        return False
    legacy = (isinstance(arguments, list) and len(arguments) == 4
              and isinstance(arguments[0], str) and Path(arguments[0]).is_absolute()
              and arguments[1:] == ["-m", "telegram_search_mcp.launch_agent", "run"])
    selected = bundled is not None and arguments == bundled
    return (
        set(value) == expected_keys
        and value.get("Label") == LAUNCH_AGENT_LABEL
        and (selected or legacy)
        and value.get("RunAtLoad") is True
        and value.get("KeepAlive") is True
        and value.get("ProcessType") == "Background"
        and value.get("LimitLoadToSessionType") == "Aqua"
        and type(value.get("Umask")) is int
        and value.get("Umask") == 0o077
        and value.get("StandardOutPath") == "/dev/null"
        and value.get("StandardErrorPath") == "/dev/null"
        and value.get("EnvironmentVariables") == ({} if selected else {"PYTHONDONTWRITEBYTECODE": "1"})
    )


def _open_launch_agent_directory(plist_path: Path) -> int:
    parent = plist_path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise LaunchAgentError("LaunchAgent directory is unsafe") from error
    if parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise LaunchAgentError("LaunchAgent directory is unsafe")
    if (
        parent_metadata.st_uid != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise LaunchAgentError("LaunchAgent directory owner is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        raise LaunchAgentError("LaunchAgent directory is unsafe") from error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (
        parent_metadata.st_dev,
        parent_metadata.st_ino,
    ):
        os.close(descriptor)
        raise LaunchAgentError("LaunchAgent directory changed during validation")
    return descriptor


def _read_launch_agent(directory_descriptor: int, name: str) -> object | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LaunchAgentError("existing LaunchAgent plist is unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 1024 * 1024
        ):
            raise LaunchAgentError("existing LaunchAgent plist is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return plistlib.load(stream)
    except plistlib.InvalidFileException as error:
        raise LaunchAgentError("existing LaunchAgent plist is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_launch_agent(*, plist_path: Path, python_executable: Path) -> None:
    """Atomically write the fixed, credential-free LaunchAgent definition."""
    directory_descriptor = _open_launch_agent_directory(plist_path)
    temporary_name = f".{plist_path.name}.{secrets.token_hex(16)}.tmp"
    temporary_exists = False
    try:
        existing = _read_launch_agent(directory_descriptor, plist_path.name)
        if existing is not None and not _managed_payload(existing):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        payload = launch_agent_payload(python_executable=python_executable)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_exists = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            plist_path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_exists = False
        os.fsync(directory_descriptor)
        written = os.stat(
            plist_path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_uid != os.getuid()
            or stat.S_IMODE(written.st_mode) != 0o600
        ):
            raise LaunchAgentError("written LaunchAgent plist is unsafe")
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        try:
            os.close(directory_descriptor)
        except OSError:
            pass


def _run_launchctl(
    arguments: list[str],
    *,
    runner: Runner,
    required: bool,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        arguments,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if required and completed.returncode != 0:
        raise LaunchAgentError("launchctl operation failed")
    return completed


def install_launch_agent(
    *,
    plist_path: Path = LAUNCH_AGENT_PLIST,
    python_executable: Path = Path(sys.executable),
    runner: Runner = subprocess.run,
    preflight: Callable[[], None] = _default_preflight,
    uid: int | None = None,
) -> None:
    preflight()
    write_launch_agent(
        plist_path=plist_path,
        python_executable=python_executable,
    )
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    service = f"{domain}/{LAUNCH_AGENT_LABEL}"
    _run_launchctl(
        [LAUNCHCTL, "bootout", service],
        runner=runner,
        required=False,
    )
    # A successful bootout may return before launchd retires the registration.
    for _ in range(50):
        state = _run_launchctl(
            [LAUNCHCTL, "print", service], runner=runner, required=False, timeout=1,
        )
        if state.returncode == 113:  # launchd: service not found
            break
        if state.returncode != 0:
            raise LaunchAgentError("unable to verify previous service shutdown")
        time.sleep(0.1)
    else:
        raise LaunchAgentError("previous service did not shut down")
    _run_launchctl(
        [LAUNCHCTL, "bootstrap", domain, str(plist_path)],
        runner=runner,
        required=True,
    )
    # RunAtLoad starts the new broker; kickstart -k would terminate it again.


def launch_agent_status(
    *,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> bool:
    user_id = os.getuid() if uid is None else uid
    completed = _run_launchctl(
        [LAUNCHCTL, "print", f"gui/{user_id}/{LAUNCH_AGENT_LABEL}"],
        runner=runner,
        required=False,
    )
    return completed.returncode == 0


def _matching_live_registration(text: object, plist_path: Path, payload: dict) -> bool:
    """Check only bounded registration metadata, never print launchd diagnostics."""
    if type(text) is not str or len(text.encode('utf-8')) > 65536:
        return False
    lines = [line.strip() for line in text.splitlines()]
    if (lines.count('path = ' + str(plist_path.absolute())) != 1
            or lines.count('program = ' + payload['ProgramArguments'][0]) != 1
            or lines.count('arguments = {') != 1):
        return False
    start = lines.index('arguments = {') + 1
    try:
        end = lines.index('}', start)
    except ValueError:
        return False
    return lines[start:end] == payload['ProgramArguments']


def _require_enrolled_definition(parent: int, plist_path: Path, payload: dict) -> None:
    existing = _read_launch_agent(parent, plist_path.name)
    metadata = os.stat(plist_path.name, dir_fd=parent, follow_symlinks=False)
    if (existing != payload or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise LaunchAgentError('existing LaunchAgent does not match the installed broker')


def validate_enrolled_broker(
    *, plist_path: Path = LAUNCH_AGENT_PLIST,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    """Read-only intended-registration check for an existing competing owner.

    No directory/plist creation, bootstrap, stop, restart or provider operation.
    A compatible socket alone cannot substitute for the installed lifecycle.
    """
    if plist_path.parent.absolute() != plist_path.parent.resolve(strict=False):
        raise LaunchAgentError('LaunchAgent directory is unsafe')
    parent = _open_launch_agent_directory(plist_path)
    try:
        payload = launch_agent_payload(python_executable=python_executable)
        _require_enrolled_definition(parent, plist_path, payload)
        user_id = os.getuid() if uid is None else uid
        service = f'gui/{user_id}/{LAUNCH_AGENT_LABEL}'
        state = _run_launchctl([LAUNCHCTL, 'print', service], runner=runner, required=False, timeout=10)
        if state.returncode != 0 or not _matching_live_registration(state.stdout, plist_path, payload):
            raise LaunchAgentError('existing service does not match the installed broker')
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to verify installed broker registration') from None
    finally:
        os.close(parent)


def start_enrolled_broker(
    *, plist_path: Path = LAUNCH_AGENT_PLIST,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    """Start after auth teardown; no bootout, kill, restart or existing rewrite.

    A new installed user may have no plist. Publish the fixed credential-free
    definition without replacing any name, then bootstrap the absent service.
    An existing service must match its actual registration and installed path.
    Ready is checked separately through the compatible IPC boundary.
    """
    if plist_path.parent.absolute() != plist_path.parent.resolve(strict=False):
        raise LaunchAgentError('LaunchAgent directory is unsafe')
    plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = _open_launch_agent_directory(plist_path)
    temporary = '.enrolled-agent-' + secrets.token_hex(16)
    created = False
    try:
        payload = launch_agent_payload(python_executable=python_executable)
        existing = _read_launch_agent(parent, plist_path.name)
        if existing is None:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            created = True
            with os.fdopen(fd, 'wb') as stream:
                plistlib.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, plist_path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                # A competitor's registration is inspected, never overwritten.
                pass
            os.unlink(temporary, dir_fd=parent)
            created = False
            os.fsync(parent)
            existing = _read_launch_agent(parent, plist_path.name)
        _require_enrolled_definition(parent, plist_path, payload)
        user_id = os.getuid() if uid is None else uid
        domain = f'gui/{user_id}'
        service = f'{domain}/{LAUNCH_AGENT_LABEL}'
        state = _run_launchctl([LAUNCHCTL, 'print', service], runner=runner, required=False, timeout=10)
        if state.returncode == 0:
            if not _matching_live_registration(state.stdout, plist_path, payload):
                raise LaunchAgentError('existing service does not match the installed broker')
            return
        if state.returncode != 113:
            raise LaunchAgentError('unable to verify installed broker registration')
        _run_launchctl([LAUNCHCTL, 'bootstrap', domain, str(plist_path)],
                      runner=runner, required=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to start installed broker') from None
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def _await_retired(service: str, *, runner: Runner, sleeper: Callable[[float], None]) -> None:
    """A successful bootout may return before launchd retires the registration."""
    for _ in range(50):
        state = _run_launchctl([LAUNCHCTL, 'print', service], runner=runner, required=False, timeout=1)
        if state.returncode == 113:  # launchd: service not found
            return
        if state.returncode != 0:
            raise LaunchAgentError('unable to verify previous service shutdown')
        sleeper(0.1)
    raise LaunchAgentError('previous service did not shut down')


def stop_enrolled_broker(
    *, plist_path: Path = LAUNCH_AGENT_PLIST,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None, sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """QR-06 reconnect: stop the exact receipt-bound broker, nothing else.

    The caller has observed a compatible handshake whose check is not ready. This
    validates the installed definition and live registration first (any mismatch
    raises before launchctl can stop anything), then boots only that label out.
    The plist stays on disk so the next start reuses the same registration.
    """
    validate_enrolled_broker(plist_path=plist_path, python_executable=python_executable,
                             runner=runner, uid=uid)
    user_id = os.getuid() if uid is None else uid
    service = f'gui/{user_id}/{LAUNCH_AGENT_LABEL}'
    try:
        _run_launchctl([LAUNCHCTL, 'bootout', service], runner=runner, required=True, timeout=10)
        _await_retired(service, runner=runner, sleeper=sleeper)
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to stop installed broker') from None


def connect_job_arguments(*, python_executable: Path = Path(sys.executable)) -> list[str]:
    """Bundle mode: the receipt-bound launcher; source mode: this interpreter's module."""
    from . import bundle_runtime
    selection = bundle_runtime._SELECTION
    if selection is not None:
        try:
            return selection.arguments('connect-helper')
        except bundle_runtime.BundleError:
            raise LaunchAgentError('installed bundle binding is unsafe') from None
    executable = python_executable.absolute()
    try:
        if not executable.resolve(strict=True).is_file():
            raise LaunchAgentError('Python executable is invalid')
    except OSError:
        raise LaunchAgentError('Python executable is invalid') from None
    return [str(executable), '-m', 'telegram_search_mcp.setup_cli', 'connect-helper']


def connect_job_payload(*, python_executable: Path = Path(sys.executable)) -> dict[str, Any]:
    return {
        'Label': CONNECT_JOB_LABEL,
        'ProgramArguments': connect_job_arguments(python_executable=python_executable),
        'RunAtLoad': True,
        'KeepAlive': False,
        'LimitLoadToSessionType': 'Aqua',
        'ProcessType': 'Interactive',
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': '/dev/null',
        'Umask': 0o077,
        'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'},
    }


def write_connect_job(*, plist_path: Path, python_executable: Path = Path(sys.executable)) -> None:
    """Owner-only, exclusive, no-follow plist in the private run directory.

    Only a stale regular file of ours is replaced; the caller has established that
    no helper job is running. Anything else at the name (link, directory) refuses.
    """
    payload = connect_job_payload(python_executable=python_executable)
    try:
        parent = open_private_directory(plist_path.parent, create=True)
    except OwnerLeaseError:
        raise LaunchAgentError('connect job directory is unsafe') from None
    name, created = plist_path.name, False
    try:
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()
                    or existing.st_nlink != 1):
                raise LaunchAgentError('existing connect job file is unsafe')
            os.unlink(name, dir_fd=parent)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=parent)
        created = True
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                plistlib.dump(payload, stream, sort_keys=True)
                stream.flush()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent)
        created = False
    except OSError:
        raise LaunchAgentError('unable to write connect job') from None
    finally:
        if created:
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def remove_connect_job(plist_path: Path) -> bool:
    """Best effort, no-follow removal of our own regular plist; never raises."""
    try:
        parent = open_private_directory(plist_path.parent)
    except OwnerLeaseError:
        return False
    try:
        existing = os.stat(plist_path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
            return False
        os.unlink(plist_path.name, dir_fd=parent)
        return True
    except OSError:
        return False
    finally:
        os.close(parent)


def connect_job_state(*, runner: Runner = subprocess.run, uid: int | None = None) -> str:
    """'running', 'loaded' (registered, not running) or 'absent'; anything else raises."""
    user_id = os.getuid() if uid is None else uid
    try:
        state = _run_launchctl([LAUNCHCTL, 'print', f'gui/{user_id}/{CONNECT_JOB_LABEL}'],
                               runner=runner, required=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to verify connect job') from None
    if state.returncode == 113:  # launchd: service not found
        return 'absent'
    text = state.stdout
    if state.returncode != 0 or type(text) is not str or len(text.encode('utf-8')) > 65536:
        raise LaunchAgentError('unable to verify connect job')
    # The first state line belongs to the job itself; nested sections come later.
    first = next((line.strip() for line in text.splitlines() if line.strip().startswith('state = ')), None)
    return 'running' if first == 'state = running' else 'loaded'


def start_connect_job(
    *, plist_path: Path, python_executable: Path = Path(sys.executable),
    runner: Runner = subprocess.run, uid: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Bootstrap the transient helper; 'running' when one already is (nothing is touched).

    A registered but idle job is booted out first. The plist is removed again if the
    bootstrap fails, so a refused start leaves nothing behind.
    """
    user_id = os.getuid() if uid is None else uid
    domain = f'gui/{user_id}'
    service = f'{domain}/{CONNECT_JOB_LABEL}'
    try:
        state = connect_job_state(runner=runner, uid=user_id)
        if state == 'running':
            return 'running'
        if state == 'loaded':
            _run_launchctl([LAUNCHCTL, 'bootout', service], runner=runner, required=True, timeout=10)
            _await_retired(service, runner=runner, sleeper=sleeper)
        write_connect_job(plist_path=plist_path, python_executable=python_executable)
        try:
            _run_launchctl([LAUNCHCTL, 'bootstrap', domain, str(plist_path)],
                           runner=runner, required=True, timeout=10)
        except BaseException:
            remove_connect_job(plist_path)
            raise
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to start connect job') from None
    return 'started'


def restart_launch_agent(
    *,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    user_id = os.getuid() if uid is None else uid
    _run_launchctl(
        [
            LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{user_id}/{LAUNCH_AGENT_LABEL}",
        ],
        runner=runner,
        required=False,
    )


def uninstall_launch_agent(
    *,
    plist_path: Path = LAUNCH_AGENT_PLIST,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> None:
    directory_descriptor = _open_launch_agent_directory(plist_path)
    try:
        payload = _read_launch_agent(directory_descriptor, plist_path.name)
        if payload is not None and not _managed_payload(payload):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        user_id = os.getuid() if uid is None else uid
        _run_launchctl(
            [LAUNCHCTL, "bootout", f"gui/{user_id}/{LAUNCH_AGENT_LABEL}"],
            runner=runner,
            required=False,
        )
        if payload is not None:
            os.unlink(plist_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def run_broker() -> int:
    validate_tdlib_runtime()
    broker = Broker(socket_path=BROKER_SOCKET_PATH, lock_path=BROKER_LOCK_PATH)

    def stop(_signum: int, _frame: object) -> None:
        broker.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        broker.serve_forever()
    except BrokerStartupError:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-search-broker")
    parser.add_argument("command", choices=("run", "install", "status", "uninstall"))
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            return run_broker()
        if arguments.command == "install":
            install_launch_agent()
            print("INSTALLED")
            return 0
        if arguments.command == "status":
            active = launch_agent_status()
            print("RUNNING" if active else "NOT_RUNNING")
            return 0 if active else 1
        uninstall_launch_agent()
        print("UNINSTALLED")
        return 0
    except Exception:
        print("BROKER_LIFECYCLE_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
