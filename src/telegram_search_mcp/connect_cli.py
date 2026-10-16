"""Agent-facing connect/wait commands, the transient helper job and Terminal-only api-credentials.

`connect` and `wait` are safe in a captured agent shell: they take no secrets, print fixed
JSON and never show the loopback URL or its token. The helper runs outside the agent's
process tree (launchd), opens the page through osascript's stdin (never argv) and owns the status file.
A dedicated run/connect.lock (never the session lease) keeps it the only helper, and a reconnect
that stopped the enrolled broker puts it back whenever no helper reaches Ready.
Every launchd, broker, terminal and Keychain boundary is injectable for inert tests.
"""
from __future__ import annotations
import calendar
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from .connect_flow import (_TERMINAL, ConnectResult, PHASES, STATUS_CODES, connect_status_path, run_connect,
                           write_connect_status)
from .credentials import CredentialStoreError
from .launch_agent import (CONNECT_JOB_LABEL, LAUNCH_AGENT_PLIST, LaunchAgentError, connect_job_state,
                           remove_connect_job, restore_enrolled_broker, start_connect_job, stop_connect_job,
                           stop_enrolled_broker, validate_enrolled_broker)
from .secure_terminal import TerminalError
from .session_owner import OwnerLeaseError, SessionOwnerLease, acquire_owner_lock, open_private_directory
from .setup_wizard import _api_credentials, setup_status

WAIT_SECONDS = 540  # Closed grammar: no duration argument.
HELPER_LIFETIME = 600  # Whole helper run, then expired (spec §3).
HELPER_LINGER = 10  # Seconds the page stays up after a terminal phase.
LINGER_WAIT = 15  # connect waits this long for a finished helper's linger before a fresh start.
HELPER_LOCK_WAIT = 10  # A helper waits this long for connect.lock, else exits connect_in_progress.
HUNG_GRACE = 60  # A non-terminal status this far past its expiry belongs to a dead or hung helper.
_STEP = 0.1
_POLL = 1.0
_MAX_STATUS = 1024
_STATUS_KEYS = frozenset({'phase', 'code', 'updated_at', 'expires_at'})
_URL = re.compile(r'http://127\.0\.0\.1:[0-9]{1,5}/#t=[A-Za-z0-9_-]{43}')  # Our own server's shape.
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_ACTIONS = {'secure_terminal_required': 'run_in_foreground_native_terminal',
            'connect_launch_failed': 'run_connect_in_native_terminal',
            'enrollment_exists': 'run_connect',
            'credential_invalid_record': 'repair_keychain_record_manually'}
_IN_PROGRESS = {'status': 'connect_in_progress', 'next_action': 'wait'}
_GENERIC = ConnectResult('failed', None).to_dict()['next_action']  # ConnectResult's fallback.
_AGAIN = 'run_api_credentials_in_native_terminal'
_STORE_CODES = frozenset({'denied', 'locked', 'unavailable', 'invalid_record', 'conflict'})


def job_plist_path(paths):
    return paths.lock.parent / (CONNECT_JOB_LABEL + '.plist')


def connect_lock_path(paths):
    """Single-helper lock, separate from the session lease (broker.lock)."""
    return paths.lock.parent / 'connect.lock'


def _take_connect_lock(paths):
    """Descriptor holding connect.lock, or None while another process holds it; unsafe raises."""
    try:
        return acquire_owner_lock(connect_lock_path(paths))
    except OwnerLeaseError as error:
        if error.code == 'owner_busy':
            return None
        raise


def _connect_lock_held(paths):
    """Probe without creating anything: True only while some process holds connect.lock."""
    try:
        parent = open_private_directory(paths.lock.parent)
    except OwnerLeaseError:
        return False
    descriptor = -1
    try:
        descriptor = os.open(connect_lock_path(paths).name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0),
                             dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            return error.errno in (errno.EAGAIN, errno.EACCES)
        return False  # Closing the descriptor releases the probe at once.
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _next_action(code, fallback):
    # ConnectResult's mapping stays the single source; only CLI-only codes are added here.
    mapped = ConnectResult('failed', code).to_dict()['next_action']
    return _ACTIONS.get(code) or (fallback if mapped == _GENERIC else mapped)


def _refused(code, fallback=_GENERIC):
    return {'status': 'refused', 'code': code, 'next_action': _next_action(code, fallback)}, 2


def _report(phase, code):
    result = ConnectResult(phase, code)
    action = result.to_dict()['next_action']
    if phase != 'ready' and code == 'credential_invalid_record':
        action = _ACTIONS[code]  # A corrupt Keychain record is never fixed by connecting again.
    return {'status': phase, 'code': code, 'next_action': action}, result.exit_code


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
            sleeper=time.sleep, python_executable=Path(sys.executable), broker_plist=LAUNCH_AGENT_PLIST,
            clock=time.time):
    """(JSON-able dict, exit code). Returns once the screen is requested, never waiting on the owner."""
    try:
        return _connect(paths, client_factory=client_factory, terminal=terminal, inline=inline,
                        runner=runner, uid=uid, sleeper=sleeper, python_executable=python_executable,
                        broker_plist=broker_plist, clock=clock)
    except KeyboardInterrupt:
        return _refused('cancelled')
    except Exception:
        return _refused('dependency_unavailable')


def _enter(paths, runner, uid, sleeper, clock):
    """(connect.lock descriptor, None) once no helper can be live; else (None, response).

    A finished helper keeps the lock and its job through a short linger: with a terminal
    status this waits up to LINGER_WAIT, then boots out a job that still has not exited.
    A busy job whose status is HUNG_GRACE past its expiry is our own hung helper and is
    booted out at once. Anything else holding the lock or the job is a live run.
    """
    polls, booted = 0, False
    for _ in range(int(LINGER_WAIT / _STEP) + 3):  # Every pass sleeps once or boots out once.
        lock = _take_connect_lock(paths)
        try:
            job = connect_job_state(runner=runner, uid=uid)
            record = _read_status(paths)  # After the job: a fresh helper's start cleared it first.
            if lock is not None and job != 'busy':
                lock, held = None, lock
                return held, None
        finally:
            if lock is not None:
                os.close(lock)
        terminal = record is not None and record[0] in _TERMINAL
        hung = record is not None and clock() > record[2] + HUNG_GRACE
        if job == 'busy' and not booted and (hung or (terminal and polls >= LINGER_WAIT / _STEP)):
            stop_connect_job(runner=runner, uid=uid, sleeper=sleeper)  # Our transient job; its run is over.
            booted = True
            continue
        if terminal and polls < LINGER_WAIT / _STEP:
            polls += 1
            sleeper(_STEP)
            continue
        break
    return None, (dict(_IN_PROGRESS), 0)


def _connect(paths, *, client_factory, terminal, inline, runner, uid, sleeper, python_executable, broker_plist,
             clock):
    state = setup_status(paths)['state']
    if state == 'legacy_unmanaged':
        return _refused('legacy_unmanaged')
    if state == 'inconsistent':
        return _refused('enrollment_inconsistent')
    probe = _probe_broker(paths, client_factory)
    if probe == 'ready':
        return {'status': 'already_connected'}, 0  # Repeat use never asks for a new login.
    try:
        lock, response = _enter(paths, runner, uid, sleeper, clock)
    except LaunchAgentError:
        return _refused('connect_launch_failed')
    except OwnerLeaseError:
        return _refused('owner_unsafe')
    if lock is None:
        return response
    try:  # Released right after the bootstrap; an inline run holds it throughout.
        return _locked(paths, state, probe, client_factory=client_factory, terminal=terminal, inline=inline,
                       runner=runner, uid=uid, sleeper=sleeper, python_executable=python_executable,
                       broker_plist=broker_plist)
    finally:
        os.close(lock)


def _locked(paths, state, probe, *, client_factory, terminal, inline, runner, uid, sleeper, python_executable,
            broker_plist):
    stopped = False
    if probe == 'blocked':
        # The narrow reconnect exception: only an enrolled, receipt-bound, compatible broker whose
        # authorization is still not ready right now. Validation precedes the stop; any doubt keeps it.
        if state != 'managed':
            return _refused('owner_busy')
        probe = _probe_broker(paths, client_factory)
        if probe == 'ready':
            return {'status': 'already_connected'}, 0
    if probe == 'blocked':
        try:
            validate_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                     runner=runner, uid=uid)
        except LaunchAgentError:
            return _refused('owner_busy')  # Nothing was stopped.
        stopped = True  # From here on a failure restores the broker before it reports.
        try:
            stop_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                 runner=runner, uid=uid, sleeper=sleeper)
        except LaunchAgentError:
            return _restore('owner_busy', broker_plist, python_executable, runner, uid)
    def refuse(code):
        return _restore(code, broker_plist, python_executable, runner, uid) if stopped else _refused(code)
    held = _await_free(paths, sleeper) if stopped else _lease(paths)
    if held != 'free':
        return refuse('owner_busy' if held == 'busy' else 'owner_unsafe')
    if not _clear_status(paths):  # Only now: no helper can be running (connect.lock, idle job).
        return refuse('owner_unsafe')
    if _native(terminal):  # Foreground in Terminal: nothing to hand to launchd; it restores on failure.
        try:
            result = inline()
        except BaseException:
            if stopped:  # Also when the inline helper could not even be built.
                _restore_quietly(lambda: restore_enrolled_broker(
                    plist_path=broker_plist, python_executable=python_executable, runner=runner, uid=uid))
            raise
        value, code = _report(result.phase, result.code)
        if stopped and value['code'] == 'owner_busy':  # This run stopped (and restored) the broker.
            value['next_action'] = 'run_connect_again'
        return value, code
    try:
        started = start_connect_job(plist_path=job_plist_path(paths), python_executable=python_executable,
                                    runner=runner, uid=uid, sleeper=sleeper)
    except LaunchAgentError:
        return refuse('connect_launch_failed')
    if started == 'running':
        if stopped:
            return refuse('connect_in_progress')
        return dict(_IN_PROGRESS), 0
    return {'status': 'screen_opened', 'next_action': 'wait'}, 0


def _restore(code, broker_plist, python_executable, runner, uid):
    """After our stop, no helper started: put the enrolled broker back, then report truthfully."""
    try:
        restore_enrolled_broker(plist_path=broker_plist, python_executable=python_executable, runner=runner, uid=uid)
    except Exception:
        return {'status': 'refused', 'code': 'broker_start_failed',
                'next_action': 'run_connect_in_native_terminal'}, 2
    if code == 'connect_in_progress':
        return dict(_IN_PROGRESS), 0
    # Never 'check_existing_broker_without_stopping_it': this run did stop (and restart) it.
    action = 'run_connect_in_native_terminal' if code == 'connect_launch_failed' else 'run_connect_again'
    return {'status': 'refused', 'code': code, 'next_action': action}, 2


# wait --------------------------------------------------------------------------------------

def read_connect_status(paths):
    """(phase, code) from a strictly valid status file, else None. Reporting only, never trusted further."""
    record = _read_status(paths)
    return None if record is None else record[:2]


def _read_status(paths):
    """(phase, code, expires_at epoch seconds) from a strictly valid status file, else None."""
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
        expires_at = calendar.timegm(time.strptime(record['expires_at'], '%Y-%m-%dT%H:%M:%SZ'))
        return record['phase'], record['code'], expires_at
    except (OSError, ValueError, OverflowError, UnicodeError, RecursionError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _helper_alive(paths, runner, uid):
    """Our connect job in any state but an explicit idle one, or a connect.lock holder (inline helper).

    The session lease is deliberately not probed: the broker holds it, and taking it,
    even briefly, could collide with a handoff.
    """
    try:
        if connect_job_state(runner=runner, uid=uid) == 'busy':
            return True
    except LaunchAgentError:
        pass  # launchd is not queryable here; the connect lock still tells.
    return _connect_lock_held(paths)


_EXITED = {'status': 'failed', 'code': 'helper_exited', 'next_action': 'run_connect_again'}


def wait(paths, *, runner=subprocess.run, uid=None, sleeper=time.sleep, monotonic=time.monotonic,
         clock=time.time, timeout=WAIT_SECONDS, poll=_POLL):
    """(JSON-able dict, exit code). Returns on a terminal phase, a vanished or expired helper or the deadline."""
    try:
        return _wait(paths, runner, uid, sleeper, monotonic, clock, timeout, poll)
    except KeyboardInterrupt:
        return _refused('cancelled')
    except Exception:
        return {'status': 'failed', 'code': 'dependency_unavailable', 'next_action': 'run_connect_again'}, 2


def _wait(paths, runner, uid, sleeper, monotonic, clock, timeout, poll):
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0):
        raise ValueError()
    deadline = monotonic() + timeout
    phase, gone = 'starting', 0
    while True:
        record = _read_status(paths)
        if record is not None:
            phase = record[0]
            if phase in _TERMINAL:
                return _report(*record[:2])
            if clock() > record[2] + HUNG_GRACE:  # Past its own lifetime: no live helper writes this.
                return dict(_EXITED), 2
        if _helper_alive(paths, runner, uid):
            gone = 0
        else:
            gone += 1
            if gone >= 2:  # Two looks, so a helper still starting up is not mistaken for a dead one.
                record = _read_status(paths)  # It may have finished between the look and the check.
                if record is not None and record[0] in _TERMINAL:
                    return _report(*record[:2])
                return dict(_EXITED), 2
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


def _await_connect_lock(paths, sleeper):
    for attempt in range(int(HELPER_LOCK_WAIT / _STEP) + 1):
        lock = _take_connect_lock(paths)
        if lock is not None:
            return lock
        if attempt < HELPER_LOCK_WAIT / _STEP:
            sleeper(_STEP)
    return None


def connect_helper(paths, *, store, provider_factory, handoff, own_plist=None, ui_factory=None,
                   server_factory=None, run=run_connect, opener=open_url, clock=time.time,
                   sleeper=time.sleep, linger=HELPER_LINGER, lifetime=HELPER_LIFETIME, lock_held=False,
                   restore_broker=restore_enrolled_broker):
    """Serve the loopback screen and drive one run_connect; returns its ConnectResult.

    The single helper: it holds connect.lock for its whole run including the linger
    (inline, the calling connect already holds it), else ends connect_in_progress without
    touching the status or the screen. The URL exists only between the server and the
    opener: it is never printed, logged or kept. run_connect owns the status file, except
    for an owner_busy run, which writes none and is recorded here. A run that does not end
    ready puts back an existing enrolled broker that is no longer loaded (restore_broker
    never creates one). The page stays up briefly after a terminal phase, then the server
    closes and our own launchd plist is removed.
    """
    if ui_factory is None or server_factory is None:
        from .connect_server import ConnectServer, LoopbackConnectUI
        ui_factory = ui_factory or LoopbackConnectUI
        server_factory = server_factory or ConnectServer
    server, lock, result, ran = None, None, None, False
    try:
        if not lock_held:
            try:
                lock = _await_connect_lock(paths, sleeper)
            except OwnerLeaseError:
                ran = True  # Nobody else can own this run: restore below.
                return ConnectResult('failed', 'owner_unsafe')
            if lock is None:
                return ConnectResult('failed', 'connect_in_progress')  # The lock holder owns the status.
        ran = True
        expires_at = clock() + lifetime
        ui = ui_factory()
        server = server_factory(ui, expires_at)
        url = server.start()
        try:
            opener(url)
        except Exception:
            pass  # Headless or no browser: the run goes on and `wait` keeps reporting its phase.
        finally:
            url = None
        result = run(paths, store, provider_factory, ui, handoff=lambda: handoff(paths=paths),
                     lifetime=lifetime)
        if result.phase == 'failed' and result.code == 'owner_busy':
            try:  # We hold connect.lock, so this status is ours to write.
                write_connect_status(paths, 'failed', 'owner_busy', updated_at=clock(), expires_at=expires_at)
            except Exception:
                pass
        try:
            ui.notify(result.phase, result.code)  # A run that never took the lease did not tell the page.
        except Exception:
            pass
        if result.phase != 'ready':
            _restore_quietly(restore_broker)
        sleeper(linger)
        return result
    finally:
        try:
            if ran and result is None:  # Raised or refused before a result: still never leave it stopped.
                _restore_quietly(restore_broker)
            if server is not None:
                server.close()
            if own_plist is not None:
                remove_connect_job(own_plist)
        finally:
            if lock is not None:
                os.close(lock)


def _restore_quietly(restore_broker):
    try:
        restore_broker()  # Raises when there is no enrolled broker definition: nothing to put back.
    except Exception:
        pass


# api-credentials ---------------------------------------------------------------------------

class _WriteCounter:
    """The only store operations _api_credentials uses; counts the add-only writes."""
    def __init__(self, store):
        self._store, self.writes = store, 0

    def get(self, account):
        return self._store.get(account)

    def put(self, account, value):
        self.writes += 1
        return self._store.put(account, value)


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
            store = _WriteCounter(store_factory())
            _api_credentials(terminal, store, remaining)
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
    return ({'status': 'stored'} if store.writes else {'status': 'already_present'}), 0
