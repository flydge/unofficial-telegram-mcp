"""Hidden input only from a foreground human shell in native Apple Terminal.

TTY status alone does not prove an uncaptured input channel. The additional
bounded ancestry check rejects agent shells and capture intermediaries. Missing
or ambiguous evidence fails closed. Injected boundaries are for inert Python
tests only; no CLI, MCP, environment or config overrides exist.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import select
import stat
import subprocess
import sys
import termios
import time
from typing import NoReturn, Protocol

from .credentials import SecretValue

NATIVE_TERMINAL = '/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal'
PROMPTS = {
    'api_id': 'Telegram API ID (hidden): ',
    'api_hash': 'Telegram API hash (hidden): ',
    'phone': 'Your Telegram phone number with country code (hidden): ',
    'code': 'The code delivered by Telegram (hidden): ',
    'password': 'Your Telegram two-step verification password (hidden): ',
}
_NOTICES = {'invalid_code': 'Telegram rejected the code. Enter a new code.\n',
            'invalid_password': 'Telegram rejected the password. Enter it again.\n'}
_SHELLS = frozenset(('/bin/zsh', '/bin/bash', '/bin/sh', '/usr/bin/login'))


class TerminalError(RuntimeError):
    def __init__(self, code='secure_terminal_required'):
        self.code = code if code in {'secure_terminal_required', 'cancelled', 'timeout', 'invalid_input'} else 'secure_terminal_required'
        super().__init__(self.code)


def _fail(code: str) -> NoReturn:
    try:
        raise TerminalError(code)
    except TerminalError as failure:
        failure.__context__ = failure.__cause__ = None
        raise


class SecureTerminal(Protocol):
    def require_secure(self) -> None: ...
    def read_secret(self, prompt_id: str, *, timeout: float) -> SecretValue: ...
    def notice(self, kind: str) -> None: ...


@dataclass(frozen=True, repr=False)
class ProcessIdentity:
    pid: int
    ppid: int
    uid: int
    executable: str


@dataclass(frozen=True, repr=False)
class TerminalInspection:
    uid: int
    tty_owner_uid: int
    interactive: bool
    controlling: bool
    foreground: bool
    processes: tuple[ProcessIdentity, ...]


def _validate_inspection(evidence: TerminalInspection) -> None:
    if (type(evidence) is not TerminalInspection or type(evidence.uid) is not int
            or evidence.uid < 0 or evidence.tty_owner_uid != evidence.uid
            or evidence.interactive is not True or evidence.controlling is not True
            or evidence.foreground is not True or type(evidence.processes) is not tuple
            or not 2 <= len(evidence.processes) <= 12):
        _fail('secure_terminal_required')
    seen, terminal_seen = set(), False
    for index, process in enumerate(evidence.processes):
        if (type(process) is not ProcessIdentity or type(process.pid) is not int
                or type(process.ppid) is not int or process.pid <= 1
                or process.ppid < 1 or process.pid in seen
                or type(process.uid) is not int or process.uid != evidence.uid
                or type(process.executable) is not str or len(process.executable) > 1024):
            _fail('secure_terminal_required')
        seen.add(process.pid)
        if index and evidence.processes[index-1].ppid != process.pid:
            _fail('secure_terminal_required')
        lowered = process.executable.lower()
        executable_name = lowered.rsplit('/', 1)[-1]
        if '/codex.app/' in lowered or any(word in executable_name for word in ('codex', 'agent', 'script', 'ssh', 'tmux', 'screen')):
            _fail('secure_terminal_required')
        if process.executable == NATIVE_TERMINAL:
            terminal_seen = True
        elif index and (terminal_seen or process.executable not in _SHELLS):
            _fail('secure_terminal_required')
    if not terminal_seen or evidence.processes[-1].executable != NATIVE_TERMINAL or evidence.processes[-1].ppid != 1:
        _fail('secure_terminal_required')


def _inspect_processes() -> tuple[ProcessIdentity, ...]:
    """Inspect one ancestor at a time, never command arguments or environment."""
    pid, records = os.getpid(), []
    for _ in range(12):
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,ppid=,uid=,comm='],
                                capture_output=True, timeout=1, check=False)
        if result.returncode != 0 or type(result.stdout) is not bytes or len(result.stdout) > 2048:
            _fail('secure_terminal_required')
        fields = result.stdout.decode('utf-8', errors='strict').strip().split(None, 3)
        if len(fields) != 4 or any(not item.isascii() or not item.isdecimal() or len(item) > 10 for item in fields[:3]):
            _fail('secure_terminal_required')
        record = ProcessIdentity(*(int(item) for item in fields[:3]), fields[3])
        if record.pid != pid or record.ppid == pid or record.ppid < 1:
            _fail('secure_terminal_required')
        records.append(record)
        if record.ppid == 1:
            return tuple(records)
        pid = record.ppid
    _fail('secure_terminal_required')


def _inspect_fd(fd: int) -> TerminalInspection:
    if sys.platform != 'darwin':
        _fail('secure_terminal_required')
    uid = os.getuid()
    interactive = all(os.isatty(item) for item in (0, 1, 2, fd))
    tty_owner = -1
    controlling = foreground = False
    if interactive:
        names = tuple(os.ttyname(item) for item in (0, 1, 2))
        metadata = os.stat(names[0], follow_symlinks=False)
        # /dev/tty is a root-owned alias. Inspect the actual stdin device's owner.
        interactive = (len(set(names)) == 1 and stat.S_ISCHR(metadata.st_mode)
                       and metadata.st_uid == uid and metadata.st_mode & 0o047 == 0
                       and os.fstat(0).st_rdev == metadata.st_rdev)
        tty_owner = metadata.st_uid
        controlling = os.tcgetpgrp(fd) == os.tcgetpgrp(0)
        foreground = os.tcgetpgrp(fd) == os.getpgrp()
    return TerminalInspection(uid, tty_owner, bool(interactive), controlling, foreground, _inspect_processes())


def _open_tty() -> int:
    # There is deliberately no stdin/getpass fallback on any failure.
    return os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY | getattr(os, 'O_CLOEXEC', 0))


def _inspect_native() -> TerminalInspection:
    fd = _open_tty()
    try:
        return _inspect_fd(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, value: bytes) -> None:
    """Never accept consent after a short write omitted part of its scope."""
    remaining = memoryview(value)
    while remaining:
        count = os.write(fd, remaining)
        if type(count) is not int or not 0 < count <= len(remaining):
            _fail('secure_terminal_required')
        remaining = remaining[count:]


def _read_hidden(fd: int, prompt: str, timeout: float) -> bytes:
    """Keep canonical input, disable echo, and restore exact attributes in finally."""
    original = termios.tcgetattr(fd)
    hidden = [*original[:6], list(original[6])]
    hidden[3] &= ~(termios.ECHO | termios.ECHONL)
    deadline = time.monotonic() + timeout
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
        _write_all(fd, prompt.encode('ascii'))
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            raise TimeoutError()
        value = os.read(fd, 4097)
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if not value:
            raise EOFError()
        if len(value) > 4096 or not value.endswith(b'\n'):
            _fail('invalid_input')
        value = value[:-1]
        if value.endswith(b'\r'):
            value = value[:-1]
        if not value or b'\0' in value:
            _fail('invalid_input')
        return value
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)
        _write_all(fd, b'\n')


def _native_read(prompt: str, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    fd = _open_tty()
    try:
        _validate_inspection(_inspect_fd(fd))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return _read_hidden(fd, prompt, remaining)
    finally:
        os.close(fd)


class NativeTerminal:
    """Production uses default native boundaries; private injections are test-only."""
    def __init__(self, *, _inspector=None, _reader=None):
        self._inspector = _inspect_native if _inspector is None else _inspector
        self._reader = _native_read if _reader is None else _reader

    def require_secure(self) -> None:
        code = None
        try:
            _validate_inspection(self._inspector())
            return
        except BaseException:
            code = 'secure_terminal_required'
        _fail(code)

    def read_secret(self, prompt_id: str, *, timeout: float = 120) -> SecretValue:
        if (type(prompt_id) is not str or prompt_id not in PROMPTS
                or type(timeout) not in (int, float) or not 0 < timeout <= 3600):
            _fail('invalid_input')
        deadline = time.monotonic() + timeout
        self.require_secure()
        code = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            value = self._reader(PROMPTS[prompt_id], remaining)
            if time.monotonic() >= deadline:
                raise TimeoutError()
            return SecretValue(value)
        except (KeyboardInterrupt, EOFError):
            code = 'cancelled'
        except TimeoutError:
            code = 'timeout'
        except TerminalError as failure:
            code = failure.code
        except Exception:
            code = 'secure_terminal_required'
        _fail(code)

    def notice(self, kind: str) -> None:
        if type(kind) is not str or kind not in _NOTICES:
            _fail('invalid_input')
        self.require_secure()
        fd = None
        code = None
        try:
            fd = _open_tty()
            _validate_inspection(_inspect_fd(fd))
            os.write(fd, _NOTICES[kind].encode('ascii'))
            return
        except Exception:
            code = 'secure_terminal_required'
        finally:
            if fd is not None:
                os.close(fd)
        _fail(code)
