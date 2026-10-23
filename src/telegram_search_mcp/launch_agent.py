"""Safe launchd lifecycle for the single broker."""

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
    # Transient QR connect helper: same per-user derivation as the broker label.
    CONNECT_JOB_LABEL,
    ALIAS_GRAMMAR,
    DEFAULT_ALIAS,
    LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_PLIST,
    AccountPaths,
    validate_tdlib_runtime,
)
from .session_owner import OwnerLeaseError, open_private_directory

LAUNCHCTL = "/bin/launchctl"


class LaunchAgentError(RuntimeError):
    """Raised when launchd installation or lifecycle checks fail safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def account_options(account: AccountPaths) -> dict[str, AccountPaths]:
    """Keyword arguments naming one account's launchd jobs: none for the default alias, so its 0.54 calls stay."""
    return {} if account.alias == DEFAULT_ALIAS else {"account": account}


def _broker_job(account: AccountPaths | None) -> tuple[str, Path]:
    """The broker's launchd label and plist: the module constants for the default alias (account None)."""
    return (LAUNCH_AGENT_LABEL, LAUNCH_AGENT_PLIST) if account is None else (account.broker_label, account.broker_plist)


def _connect_label(account: AccountPaths | None) -> str:
    return CONNECT_JOB_LABEL if account is None else account.connect_label


def _account_arguments(account: AccountPaths | None) -> list[str]:
    """The trailing argv naming a named alias (spec 3B); none for the default alias, so its 0.54 argv stays."""
    return [] if account is None or account.alias == DEFAULT_ALIAS else ["--account", account.alias]


def _require_default_job(account: AccountPaths | None) -> None:
    """Writing or starting a named alias's broker job, and its connect helper's argv, are phase C's (spec 3C).

    Until then a named alias's broker payload exists (its argv ends with `--account <alias>`) and is recognised,
    but no job is written or bootstrapped for it.
    """
    if account is not None and account.alias != DEFAULT_ALIAS:
        raise LaunchAgentError("account jobs are unavailable")


def _default_preflight(account: AccountPaths | None = None) -> None:
    from .credential_identity import preflight_credentials
    validate_tdlib_runtime()
    if account is None:
        preflight_credentials()
    else:
        from .enrollment import SetupPaths
        preflight_credentials(SetupPaths.from_account(account))


def launch_agent_payload(*, python_executable: Path, account: AccountPaths | None = None) -> dict[str, Any]:
    """The broker's launchd definition; a named alias's argv ends with `--account <alias>` (spec 3B)."""
    from .bundle_runtime import broker_arguments
    bundled = broker_arguments()
    executable = python_executable.absolute()
    resolved_executable = executable.resolve(strict=True)
    if not resolved_executable.is_file():
        raise LaunchAgentError("Python executable is invalid")
    return {
        "Label": _broker_job(account)[0],
        "ProgramArguments": (bundled or [
            str(executable),
            "-m",
            "telegram_search_mcp.launch_agent",
            "run",
        ]) + _account_arguments(account),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {} if bundled else {"PYTHONDONTWRITEBYTECODE": "1"},
    }


def _managed_payload(value: object, account: AccountPaths | None = None) -> bool:
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
    named = _account_arguments(account)
    legacy = (isinstance(arguments, list) and len(arguments) == 4 + len(named)
              and isinstance(arguments[0], str) and Path(arguments[0]).is_absolute()
              and arguments[1:] == ["-m", "telegram_search_mcp.launch_agent", "run", *named])
    selected = bundled is not None and arguments == bundled + named
    return (
        set(value) == expected_keys
        and value.get("Label") == _broker_job(account)[0]
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


def write_launch_agent(*, plist_path: Path, python_executable: Path, account: AccountPaths | None = None) -> None:
    """Atomically write the fixed, credential-free LaunchAgent definition."""
    _require_default_job(account)
    directory_descriptor = _open_launch_agent_directory(plist_path)
    temporary_name = f".{plist_path.name}.{secrets.token_hex(16)}.tmp"
    temporary_exists = False
    try:
        existing = _read_launch_agent(directory_descriptor, plist_path.name)
        if existing is not None and not _managed_payload(existing, account):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        payload = launch_agent_payload(python_executable=python_executable, account=account)
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
    plist_path: Path | None = None,
    python_executable: Path = Path(sys.executable),
    runner: Runner = subprocess.run,
    preflight: Callable[[], None] | None = None,
    uid: int | None = None,
    account: AccountPaths | None = None,
) -> None:
    _require_default_job(account)
    label, default_plist = _broker_job(account)
    plist_path = default_plist if plist_path is None else plist_path
    if preflight is None:
        _default_preflight(account)
    else:
        preflight()
    write_launch_agent(
        plist_path=plist_path,
        python_executable=python_executable,
        account=account,
    )
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    service = f"{domain}/{label}"
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
    account: AccountPaths | None = None,
) -> bool:
    user_id = os.getuid() if uid is None else uid
    completed = _run_launchctl(
        [LAUNCHCTL, "print", f"gui/{user_id}/{_broker_job(account)[0]}"],
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
    *, plist_path: Path | None = None,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None, account: AccountPaths | None = None,
) -> None:
    """Read-only intended-registration check for an existing competing owner.

    No directory/plist creation, bootstrap, stop, restart or provider operation.
    A compatible socket alone cannot substitute for the installed lifecycle.
    """
    label, default_plist = _broker_job(account)
    plist_path = default_plist if plist_path is None else plist_path
    if plist_path.parent.absolute() != plist_path.parent.resolve(strict=False):
        raise LaunchAgentError('LaunchAgent directory is unsafe')
    parent = _open_launch_agent_directory(plist_path)
    try:
        payload = launch_agent_payload(python_executable=python_executable, account=account)
        _require_enrolled_definition(parent, plist_path, payload)
        user_id = os.getuid() if uid is None else uid
        service = f'gui/{user_id}/{label}'
        state = _run_launchctl([LAUNCHCTL, 'print', service], runner=runner, required=False, timeout=10)
        if state.returncode != 0 or not _matching_live_registration(state.stdout, plist_path, payload):
            raise LaunchAgentError('existing service does not match the installed broker')
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to verify installed broker registration') from None
    finally:
        os.close(parent)


def start_enrolled_broker(
    *, plist_path: Path | None = None,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None, create: bool = True, account: AccountPaths | None = None,
) -> None:
    """Start after auth teardown; no bootout, kill, restart or existing rewrite.

    A new installed user may have no plist. Publish the fixed credential-free
    definition without replacing any name, then bootstrap the absent service.
    An existing service must match its actual registration and installed path.
    Ready is checked separately through the compatible IPC boundary. With
    create=False nothing is ever written: a missing definition refuses.
    """
    _require_default_job(account)
    label, default_plist = _broker_job(account)
    plist_path = default_plist if plist_path is None else plist_path
    if plist_path.parent.absolute() != plist_path.parent.resolve(strict=False):
        raise LaunchAgentError('LaunchAgent directory is unsafe')
    if create:
        plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = _open_launch_agent_directory(plist_path)
    temporary = '.enrolled-agent-' + secrets.token_hex(16)
    created = False
    try:
        payload = launch_agent_payload(python_executable=python_executable, account=account)
        existing = _read_launch_agent(parent, plist_path.name)
        if existing is None and not create:
            raise LaunchAgentError('no installed broker to restore')
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
        service = f'{domain}/{label}'
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


def restore_enrolled_broker(
    *, plist_path: Path | None = None,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None, account: AccountPaths | None = None,
) -> None:
    """Put back a broker that existed before a failed reconnect; never create one.

    Only an existing, exactly matching definition is started, and only when its
    service is not loaded (a loaded matching service is left as it is).
    """
    start_enrolled_broker(plist_path=plist_path, python_executable=python_executable,
                          runner=runner, uid=uid, create=False, account=account)


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
    *, plist_path: Path | None = None,
    python_executable: Path = Path(sys.executable), runner: Runner = subprocess.run,
    uid: int | None = None, sleeper: Callable[[float], None] = time.sleep, account: AccountPaths | None = None,
) -> None:
    """QR-06 reconnect: stop the exact receipt-bound broker, nothing else.

    The caller has observed a compatible handshake whose check is not ready. This
    validates the installed definition and live registration first (any mismatch
    raises before launchctl can stop anything), then boots only that label out.
    The plist stays on disk so the next start reuses the same registration.
    """
    validate_enrolled_broker(plist_path=plist_path, python_executable=python_executable,
                             runner=runner, uid=uid, account=account)
    user_id = os.getuid() if uid is None else uid
    service = f'gui/{user_id}/{_broker_job(account)[0]}'
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


def connect_job_payload(*, python_executable: Path = Path(sys.executable),
                        account: AccountPaths | None = None) -> dict[str, Any]:
    _require_default_job(account)
    return {
        'Label': _connect_label(account),
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


def write_connect_job(*, plist_path: Path, python_executable: Path = Path(sys.executable),
                      account: AccountPaths | None = None) -> None:
    """Owner-only, exclusive, no-follow plist in the private run directory.

    Only a stale regular file of ours is replaced; the caller has established that
    no helper job is running. Anything else at the name (link, directory) refuses.
    """
    payload = connect_job_payload(python_executable=python_executable, account=account)
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


# Only these explicit job states are idle; any other loaded state (running, spawn
# scheduled, waiting, a missing state line) may still become or be a live helper.
_IDLE_JOB_STATES = frozenset({'state = not running', 'state = exited'})


def connect_job_state(*, runner: Runner = subprocess.run, uid: int | None = None,
                      account: AccountPaths | None = None) -> str:
    """'busy', 'idle' (registered, explicitly not running) or 'absent'; anything else raises."""
    user_id = os.getuid() if uid is None else uid
    try:
        state = _run_launchctl([LAUNCHCTL, 'print', f'gui/{user_id}/{_connect_label(account)}'],
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
    return 'idle' if first in _IDLE_JOB_STATES else 'busy'


def stop_connect_job(
    *, runner: Runner = subprocess.run, uid: int | None = None,
    sleeper: Callable[[float], None] = time.sleep, account: AccountPaths | None = None,
) -> None:
    """Boot out our own transient helper job (idle, finished or hung) and await retirement."""
    user_id = os.getuid() if uid is None else uid
    service = f'gui/{user_id}/{_connect_label(account)}'
    try:
        stopped = _run_launchctl([LAUNCHCTL, 'bootout', service], runner=runner, required=False, timeout=10)
        if stopped.returncode != 0:
            state = _run_launchctl([LAUNCHCTL, 'print', service], runner=runner, required=False, timeout=1)
            if state.returncode == 113:  # Already gone (a concurrent connect retired it).
                return
            raise LaunchAgentError('unable to stop connect job')
        _await_retired(service, runner=runner, sleeper=sleeper)
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to stop connect job') from None


def start_connect_job(
    *, plist_path: Path, python_executable: Path = Path(sys.executable),
    runner: Runner = subprocess.run, uid: int | None = None,
    sleeper: Callable[[float], None] = time.sleep, account: AccountPaths | None = None,
) -> str:
    """Bootstrap the transient helper; 'running' when a busy one exists (nothing is touched).

    A registered but idle job is booted out first. The plist is removed again if the
    bootstrap fails, so a refused start leaves nothing behind.
    """
    user_id = os.getuid() if uid is None else uid
    domain = f'gui/{user_id}'
    try:
        state = connect_job_state(runner=runner, uid=user_id, account=account)
        if state == 'busy':
            return 'running'
        if state == 'idle':
            stop_connect_job(runner=runner, uid=user_id, sleeper=sleeper, account=account)
        write_connect_job(plist_path=plist_path, python_executable=python_executable, account=account)
        try:
            _run_launchctl([LAUNCHCTL, 'bootstrap', domain, str(plist_path)],
                           runner=runner, required=True, timeout=10)
        except BaseException:
            remove_connect_job(plist_path)
            raise
    except (OSError, subprocess.SubprocessError):
        raise LaunchAgentError('unable to start connect job') from None
    return 'started'


def start_launch_agent_if_stopped(
    *,
    runner: Runner = subprocess.run,
    uid: int | None = None,
    account: AccountPaths | None = None,
) -> None:
    """Start the broker service if it is not running; never kill a running one.

    A broker that is still starting (Keychain key, encrypted database) has no
    socket yet; killing it with `kickstart -k` would only restart that wait.
    """
    user_id = os.getuid() if uid is None else uid
    _run_launchctl(
        [
            LAUNCHCTL,
            "kickstart",
            f"gui/{user_id}/{_broker_job(account)[0]}",
        ],
        runner=runner,
        required=False,
    )


def uninstall_launch_agent(
    *,
    plist_path: Path | None = None,
    runner: Runner = subprocess.run,
    uid: int | None = None,
    account: AccountPaths | None = None,
) -> None:
    label, default_plist = _broker_job(account)
    plist_path = default_plist if plist_path is None else plist_path
    directory_descriptor = _open_launch_agent_directory(plist_path)
    try:
        payload = _read_launch_agent(directory_descriptor, plist_path.name)
        if payload is not None and not _managed_payload(payload, account):
            raise LaunchAgentError("existing LaunchAgent plist is not owned by this service")
        user_id = os.getuid() if uid is None else uid
        _run_launchctl(
            [LAUNCHCTL, "bootout", f"gui/{user_id}/{label}"],
            runner=runner,
            required=False,
        )
        if payload is not None:
            os.unlink(plist_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def run_broker(account: AccountPaths | None = None) -> int:
    validate_tdlib_runtime()
    broker = (Broker(socket_path=BROKER_SOCKET_PATH, lock_path=BROKER_LOCK_PATH) if account is None else
              Broker(socket_path=account.socket, lock_path=account.broker_lock, account=account))

    def stop(_signum: int, _frame: object) -> None:
        broker.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        broker.serve_forever()
    except BrokerStartupError:
        return 1
    return 0


def _alias_argument(value: str) -> str:
    """A closed alias token; the refusal never echoes it, and `default` is spelled by omitting the flag."""
    if type(value) is not str or value == DEFAULT_ALIAS or ALIAS_GRAMMAR.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a registered account alias")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-search-broker")
    parser.add_argument("command", choices=("run", "install", "status", "uninstall"))
    # `run --account <alias>` is a named alias's broker argv (spec 3B); its install and lifecycle are phase C's.
    parser.add_argument("--account", type=_alias_argument, metavar="ALIAS", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.account is not None:
            if arguments.command != "run":
                raise LaunchAgentError("account lifecycle is unavailable")
            # The registry admits the alias before the broker creates any directory.
            return run_broker(AccountPaths.for_alias(arguments.account, Path.home()))
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
