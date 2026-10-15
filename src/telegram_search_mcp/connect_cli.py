"""Agent-facing connect/wait commands, the transient helper job and Terminal-only api-credentials.

`connect` and `wait` are safe in a captured agent shell: they take no secrets, print fixed
JSON and never show the loopback URL or its token. The helper runs outside the agent's
process tree (launchd), opens the page through osascript's stdin (never argv) and owns the status file.
Every launchd, broker, terminal and Keychain boundary is injectable for inert tests.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from .connect_flow import _TERMINAL, ConnectResult, PHASES, STATUS_CODES, connect_status_path, run_connect
from .credentials import CredentialStoreError
from .launch_agent import (CONNECT_JOB_LABEL, LAUNCH_AGENT_PLIST, LaunchAgentError, connect_job_state,
                           remove_connect_job, start_connect_job, stop_enrolled_broker)
from .secure_terminal import TerminalError
from .session_owner import OwnerLeaseError, SessionOwnerLease, open_private_directory
from .setup_wizard import _api_credentials, setup_status

WAIT_SECONDS = 540  # Closed grammar: no duration argument.
HELPER_LIFETIME = 600  # Whole helper run, then expired (spec §3).
HELPER_LINGER = 10  # Seconds the page stays up after a terminal phase.
_POLL = 1.0
_MAX_STATUS = 1024
_STATUS_KEYS = frozenset({'phase', 'code', 'updated_at', 'expires_at'})
_URL = re.compile(r'http://127\.0\.0\.1:[0-9]{1,5}/#t=[A-Za-z0-9_-]{43}')  # Our own server's shape.
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_ACTIONS = {'secure_terminal_required': 'run_in_foreground_native_terminal',
            'connect_launch_failed': 'run_connect_in_native_terminal',
            'enrollment_exists': 'run_connect'}
_GENERIC = ConnectResult('failed', None).to_dict()['next_action']  # ConnectResult's fallback.
_AGAIN = 'run_api_credentials_in_native_terminal'
_STORE_CODES = frozenset({'denied', 'locked', 'unavailable', 'invalid_record', 'conflict'})


def job_plist_path(paths):
    return paths.lock.parent / (CONNECT_JOB_LABEL + '.plist')


def _next_action(code, fallback):
    # ConnectResult's mapping stays the single source; only CLI-only codes are added here.
    mapped = ConnectResult('failed', code).to_dict()['next_action']
    return _ACTIONS.get(code) or (fallback if mapped == _GENERIC else mapped)


def _refused(code, fallback=_GENERIC):
    return {'status': 'refused', 'code': code, 'next_action': _next_action(code, fallback)}, 2


def _report(phase, code):
    result = ConnectResult(phase, code)
    return {'status': phase, 'code': code, 'next_action': result.to_dict()['next_action']}, result.exit_code


# connect -----------------------------------------------------------------------------------

def _probe_broker(paths, client_factory):
    """'none' | 'ready' | 'blocked' | 'other'. Handshake and check only; nothing is started or stopped."""
    if not (paths.lock.parent / 'broker.sock').exists():
        return 'none'
    client = None
    try:
        client = client_factory(paths, 3)
        client.handshake()
        status = client.check()
    except Exception:
        return 'other'  # Incompatible, unreachable or unreadable policy: never a stop candidate.
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return status if status in ('ready', 'blocked') else 'other'


def _lease(paths):
    """'free' | 'busy' | 'unsafe'. Takes and immediately releases the owner lease."""
    try:
        with SessionOwnerLease(paths.lock):
            return 'free'
    except OwnerLeaseError as error:
        return 'busy' if error.code == 'owner_busy' else 'unsafe'


def _await_free(paths, sleeper):
    for _ in range(50):
        state = _lease(paths)
        if state != 'busy':
            return state
        sleeper(0.1)
    return 'busy'


def _clear_status(paths):
    """Remove a previous run's status so `wait` can never report a stale outcome."""
    try:
        parent = open_private_directory(paths.lock.parent, create=True)
    except OwnerLeaseError:
        return False
    try:
        try:
            os.unlink(connect_status_path(paths).name, dir_fd=parent)
        except FileNotFoundError:
            pass
        return True
    except OSError:
        return False
    finally:
        os.close(parent)


def _native(terminal):
    try:
        terminal.require_secure()
        return True
    except (Exception, KeyboardInterrupt):
        return False


def connect(paths, *, client_factory, terminal, inline, runner=subprocess.run, uid=None,
            sleeper=time.sleep, python_executable=Path(sys.executable), broker_plist=LAUNCH_AGENT_PLIST):
    """(JSON-able dict, exit code). Returns once the screen is requested, never waiting on the owner."""
    try:
        return _connect(paths, client_factory=client_factory, terminal=terminal, inline=inline,
                        runner=runner, uid=uid, sleeper=sleeper, python_executable=python_executable,
                        broker_plist=broker_plist)
    except KeyboardInterrupt:
        return _refused('cancelled')
    except Exception:
        return _refused('dependency_unavailable')


def _connect(paths, *, client_factory, terminal, inline, runner, uid, sleeper, python_executable, broker_plist):
    state = setup_status(paths)['state']
    if state == 'legacy_unmanaged':
        return _refused('legacy_unmanaged')
    if state == 'inconsistent':
        return _refused('enrollment_inconsistent')
    probe = _probe_broker(paths, client_factory)
    if probe == 'ready':
        return {'status': 'already_connected'}, 0  # Repeat use never asks for a new login.
    try:
        job = connect_job_state(runner=runner, uid=uid)
    except LaunchAgentError:
        return _refused('connect_launch_failed')
    if job == 'running':
        return {'status': 'connect_in_progress', 'next_action': 'wait'}, 0
    stopped = False
    if probe == 'blocked':
        # The narrow reconnect exception: only an enrolled, receipt-bound, compatible broker whose
        # authorization is not ready. Validation precedes the stop; any doubt keeps it running.
        if state != 'managed':
            return _refused('owner_busy')
        try:
            stop_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                 runner=runner, uid=uid, sleeper=sleeper)
        except LaunchAgentError:
            return _refused('owner_busy')
        stopped = True
    held = _await_free(paths, sleeper) if stopped else _lease(paths)
    if held != 'free':
        return _refused('owner_busy' if held == 'busy' else 'owner_unsafe')
    if not _clear_status(paths):  # Only now: no helper is running (single-helper guarantee).
        return _refused('owner_unsafe')
    if _native(terminal):  # Foreground in Terminal: nothing to hand to launchd.
        result = inline()
        return _report(result.phase, result.code)
    try:
        started = start_connect_job(plist_path=job_plist_path(paths), python_executable=python_executable,
                                    runner=runner, uid=uid, sleeper=sleeper)
    except LaunchAgentError:
        return _refused('connect_launch_failed')
    if started == 'running':
        return {'status': 'connect_in_progress', 'next_action': 'wait'}, 0
    return {'status': 'screen_opened', 'next_action': 'wait'}, 0


# wait --------------------------------------------------------------------------------------

def read_connect_status(paths):
    """(phase, code) from a strictly valid status file, else None. Reporting only, never trusted further."""
    try:
        parent = open_private_directory(paths.lock.parent)
    except OwnerLeaseError:
        return None
    descriptor = -1
    try:
        descriptor = os.open(connect_status_path(paths).name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0),
                             dir_fd=parent)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600 or not 0 < metadata.st_size <= _MAX_STATUS):
            return None
        data = os.read(descriptor, _MAX_STATUS + 1)
        if len(data) != metadata.st_size:
            return None
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        def reject(_):
            raise ValueError()
        record = json.loads(data.decode('ascii'), object_pairs_hook=unique, parse_constant=reject)
        if (type(record) is not dict or set(record) != _STATUS_KEYS or type(record['phase']) is not str
                or record['phase'] not in PHASES
                or (record['code'] is not None and (type(record['code']) is not str
                                                    or record['code'] not in STATUS_CODES))
                or any(type(record[name]) is not str or _TIME.fullmatch(record[name]) is None
                       for name in ('updated_at', 'expires_at'))):
            return None
        return record['phase'], record['code']
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _helper_alive(paths, runner, uid):
    """A running launchd job, or any holder of the owner lease (an inline helper in Terminal)."""
    try:
        if connect_job_state(runner=runner, uid=uid) == 'running':
            return True
    except LaunchAgentError:
        pass  # launchd is not queryable here; the lease still tells.
    return _lease(paths) == 'busy'


def wait(paths, *, runner=subprocess.run, uid=None, sleeper=time.sleep, monotonic=time.monotonic,
         timeout=WAIT_SECONDS, poll=_POLL):
    """(JSON-able dict, exit code). Returns on a terminal phase, a vanished helper or the deadline."""
    try:
        return _wait(paths, runner, uid, sleeper, monotonic, timeout, poll)
    except KeyboardInterrupt:
        return _refused('cancelled')
    except Exception:
        return {'status': 'failed', 'code': 'dependency_unavailable', 'next_action': 'run_connect_again'}, 2


def _wait(paths, runner, uid, sleeper, monotonic, timeout, poll):
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0):
        raise ValueError()
    deadline = monotonic() + timeout
    phase, gone = 'starting', 0
    while True:
        record = read_connect_status(paths)
        if record is not None:
            phase = record[0]
            if phase in _TERMINAL:
                return _report(*record)
        if _helper_alive(paths, runner, uid):
            gone = 0
        else:
            gone += 1
            if gone >= 2:  # Two looks, so a helper still starting up is not mistaken for a dead one.
                record = read_connect_status(paths)  # It may have finished between the look and the check.
                if record is not None and record[0] in _TERMINAL:
                    return _report(*record)
                return {'status': 'failed', 'code': 'helper_exited', 'next_action': 'run_connect_again'}, 2
        left = deadline - monotonic()
        if left <= 0:
            action = 'scan_qr_on_this_mac_screen' if phase == 'waiting_for_scan' else 'wait'
            return {'status': phase, 'next_action': action}, 0
        sleeper(min(poll, left))


# connect-helper ----------------------------------------------------------------------------

def open_url(url, *, runner=subprocess.run):
    """Default browser via AppleScript on stdin: the token never appears in any process's argv.

    The URL is checked against our server's exact shape before it is placed in the script,
    so it cannot carry quoting or script text. No shell, output discarded, short timeout.
    """
    if type(url) is not str or _URL.fullmatch(url) is None:
        raise ValueError('invalid_url')
    runner(['/usr/bin/osascript', '-'], input='open location "%s"\n' % url, text=True,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)


def connect_helper(paths, *, store, provider_factory, handoff, own_plist=None, ui_factory=None,
                   server_factory=None, run=run_connect, opener=open_url, clock=time.time,
                   sleeper=time.sleep, linger=HELPER_LINGER, lifetime=HELPER_LIFETIME):
    """Serve the loopback screen and drive one run_connect; returns its ConnectResult.

    The URL exists only between the server and the opener: it is never printed, logged or
    kept. run_connect owns the status file; this adds none. The page stays up briefly after
    a terminal phase, then the server closes and our own launchd plist is removed.
    """
    if ui_factory is None or server_factory is None:
        from .connect_server import ConnectServer, LoopbackConnectUI
        ui_factory = ui_factory or LoopbackConnectUI
        server_factory = server_factory or ConnectServer
    server = None
    try:
        ui = ui_factory()
        server = server_factory(ui, clock() + lifetime)
        url = server.start()
        try:
            opener(url)
        except Exception:
            pass  # Headless or no browser: the run goes on and `wait` keeps reporting its phase.
        finally:
            url = None
        result = run(paths, store, provider_factory, ui, handoff=lambda: handoff(paths=paths),
                     lifetime=lifetime)
        try:
            ui.notify(result.phase, result.code)  # A run that never took the lease did not tell the page.
        except Exception:
            pass
        sleeper(linger)
        return result
    finally:
        if server is not None:
            server.close()
        if own_plist is not None:
            remove_connect_job(own_plist)


# api-credentials ---------------------------------------------------------------------------

def api_credentials(paths, *, terminal, store_factory, timeout=120):
    """Owner override: store the Telegram API pair add-only. Native Terminal only, before first connect."""
    refuse = lambda code: _refused(code, _AGAIN)
    try:
        terminal.require_secure()
    except (Exception, KeyboardInterrupt):
        return refuse('secure_terminal_required')
    state = setup_status(paths)['state']
    if state != 'new':
        return refuse({'managed': 'enrollment_exists', 'legacy_unmanaged': 'legacy_unmanaged'}
                      .get(state, 'enrollment_inconsistent'))
    deadline = time.monotonic() + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        return left
    try:
        with SessionOwnerLease(paths.lock):  # No connect run may create an enrollment meanwhile.
            state = setup_status(paths)['state']
            if state != 'new':
                return refuse('enrollment_exists' if state == 'managed' else 'enrollment_inconsistent')
            _api_credentials(terminal, store_factory(), remaining)
    except OwnerLeaseError as error:
        return refuse(error.code)
    except CredentialStoreError as error:
        return refuse('credential_' + (error.code if error.code in _STORE_CODES else 'unavailable'))
    except TerminalError as error:
        return refuse(error.code)
    except (KeyboardInterrupt, EOFError):
        return refuse('cancelled')
    except TimeoutError:
        return refuse('timeout')
    except Exception:
        return refuse('dependency_unavailable')
    return {'status': 'stored'}, 0
