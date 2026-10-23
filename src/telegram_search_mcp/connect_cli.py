"""Agent-facing connect/wait/cancel commands, the transient helper job and Terminal-only api-credentials.

`connect`, `wait` and `cancel` are safe in a captured agent shell: they take no secrets, print
fixed JSON and never show the loopback URL or its token. The helper runs outside the agent's
process tree (launchd), opens the page through osascript's stdin (never argv) and owns the status file.
A dedicated run/connect.lock (never the session lease) keeps it the only helper, and a reconnect
that stopped the enrolled broker puts it back whenever no helper reaches Ready.
Every launchd, broker, terminal and Keychain boundary is injectable for inert tests.
"""
from __future__ import annotations
import calendar
import errno
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from . import account_registry
from .broker_client import BrokerCompatibilityError, BrokerUnavailable
from .config import DEFAULT_ALIAS, AccountPathsError
from .connect_flow import (_TERMINAL, ConnectResult, PHASES, STATUS_CODES, connect_status_path, next_action,
                           run_connect, valid_retry_after, write_connect_status)
from .credentials import CredentialStoreError
from .launch_agent import (LaunchAgentError, account_options, connect_job_state, remove_connect_job,
                           restore_enrolled_broker, start_connect_job, stop_connect_job, stop_enrolled_broker,
                           validate_enrolled_broker)
from .secure_terminal import TerminalError
from .session_owner import OwnerLeaseError, SessionOwnerLease, acquire_owner_lock, open_private_directory
from .setup_wizard import _api_credentials, setup_status

WAIT_SECONDS = 540  # Closed grammar: no duration argument.
HELPER_LIFETIME = 600  # Whole helper run, then expired (spec §3).
HELPER_LINGER = 10  # Seconds the page stays up after a terminal phase.
LINGER_WAIT = 15  # connect waits this long for a finished helper's linger before a fresh start.
HELPER_LOCK_WAIT = 10  # A helper waits this long for connect.lock, else exits connect_in_progress.
# A non-terminal status this far past its expiry belongs to a dead or hung helper. The tail after
# an observed Ready (teardown, a handoff of up to 30 s, the linger) can reach 60 s past expiry.
HUNG_GRACE = 120
PROBE_HANDSHAKE = 3  # Seconds for the broker handshake: a live broker answers at once.
PROBE_CHECK = 25  # Seconds for check after a compatible handshake: the first one starts TDLib and re-verifies the bundle.
_STEP = 0.1
_POLL = 1.0
_MAX_STATUS = 1024
_STATUS_KEYS = frozenset({'phase', 'code', 'updated_at', 'expires_at', 'retry_after'})
_URL = re.compile(r'http://127\.0\.0\.1:[0-9]{1,5}/#t=[A-Za-z0-9_-]{43}')  # Our own server's shape.
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_IN_PROGRESS = {'status': 'connect_in_progress', 'next_action': 'wait'}
_AGAIN = 'run_api_credentials_in_native_terminal'
# api-credentials keeps these codes' table actions; any other refusal there means "run it again".
_API_KEPT = frozenset({'secure_terminal_required', 'enrollment_exists', 'legacy_unmanaged',
                       'enrollment_inconsistent', 'owner_busy', 'credential_invalid_record', 'alias_unavailable'})
_STORE_CODES = frozenset({'denied', 'locked', 'unavailable', 'invalid_record', 'conflict'})
# The OS refused a write (an agent sandbox, a read-only parent): not unsafe metadata.
_DENIED = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def job_plist_path(paths):
    return paths.account.connect_plist


def connect_lock_path(paths):
    """Single-helper lock, separate from the session lease (broker.lock)."""
    return paths.account.connect_lock


def _jobs(paths):
    """Keyword arguments naming this account's launchd jobs; none for the default alias (its 0.54 calls)."""
    return account_options(paths.account)


def _page(paths):
    """Keyword arguments naming the account alias on the connect page; none for the default alias."""
    alias = paths.account.alias
    return {} if alias == DEFAULT_ALIAS else {'alias': alias}


def _denied(error):
    """An OwnerLeaseError raised because the OS refused to create or open a file, not for unsafe metadata."""
    context = error.__cause__ or error.__context__
    return isinstance(context, OSError) and context.errno in _DENIED


def _take_connect_lock(paths):
    """Descriptor holding connect.lock, or None while another process holds it; unsafe raises.

    A named alias's is taken only inside its existing root, and only while the registry still lists it
    (account_registry.login_lock, review fix 055d3): a login never creates the alias root; a missing root or an
    alias no longer listed raises AccountPathsError (`alias_unavailable`), nothing created. The default's is 0.54's.
    """
    try:
        if paths.account.alias != DEFAULT_ALIAS:
            return account_registry.login_lock(paths.account)
        return acquire_owner_lock(connect_lock_path(paths))
    except OwnerLeaseError as error:
        if error.code == 'owner_busy':
            return None
        raise


def _connect_lock_held(paths):
    """Probe without creating anything: True only while some process holds connect.lock."""
    try:
        parent = open_private_directory(paths.account.run)
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


def _refused(code, action=None):
    return {'status': 'refused', 'code': code, 'next_action': action or next_action('failed', code)}, 2


def _report(phase, code, retry_after=None):
    value = {'status': phase, 'code': code, 'next_action': next_action(phase, code)}
    if code == 'flood_wait':
        value['retry_after'] = retry_after  # Seconds Telegram asked to wait; None when it gave none.
    return value, 0 if phase == 'ready' else 2


# connect -----------------------------------------------------------------------------------

def _probe_broker(paths, client_factory, *, check_timeout=PROBE_CHECK, monotonic=time.monotonic):
    """'none' | 'ready' | 'blocked' | 'starting' | 'other'. Handshake and check only; nothing is started or stopped.

    The handshake has PROBE_HANDSHAKE seconds. Only a compatible broker gets check_timeout for its
    check; a check that runs that budget out is 'starting'. Anything else is 'other'.
    """
    if not paths.account.socket.exists():
        return 'none'
    def ask(timeout, request):
        client = client_factory(paths, timeout)
        try:
            return request(client)
        finally:
            try:
                client.close()
            except Exception:
                pass
    try:
        ask(PROBE_HANDSHAKE, lambda client: client.handshake())
    except Exception:
        return 'other'  # Incompatible, unreachable or unreadable policy: never a stop candidate.
    started = monotonic()
    try:
        status = ask(check_timeout, lambda client: client.check())
    except BrokerCompatibilityError:
        return 'other'
    except BrokerUnavailable:
        return 'starting' if monotonic() - started >= check_timeout - 1 else 'other'
    except Exception:
        return 'other'
    return status if status in ('ready', 'blocked') else 'other'


def _lease(paths):
    """'free' | 'busy' | 'denied' | 'unsafe'. Takes and immediately releases the owner lease."""
    try:
        with SessionOwnerLease(paths.lock):
            return 'free'
    except OwnerLeaseError as error:
        if error.code == 'owner_busy':
            return 'busy'
        return 'denied' if _denied(error) else 'unsafe'


def _await_free(paths, sleeper):
    for _ in range(50):
        state = _lease(paths)
        if state != 'busy':
            return state
        sleeper(0.1)
    return 'busy'


def _clear_status(paths):
    """'ok' | 'denied' | 'unsafe'. Remove a previous run's status so `wait` can never report a stale outcome."""
    try:
        parent = open_private_directory(paths.account.run, create=True)
    except OwnerLeaseError as error:
        return 'denied' if _denied(error) else 'unsafe'
    try:
        try:
            os.unlink(connect_status_path(paths).name, dir_fd=parent)
        except FileNotFoundError:
            pass
        return 'ok'
    except OSError as error:
        return 'denied' if error.errno in _DENIED else 'unsafe'
    finally:
        os.close(parent)


def _forget_finished_run(paths):
    """With a Ready broker and no helper holding connect.lock, a previous outcome must not reach `wait`.

    connect.lock is held by every helper from before its first status write through its linger,
    so taking it proves nothing will write the status meanwhile. Best effort; never raises.
    """
    try:
        lock = _take_connect_lock(paths)
    except Exception:
        return
    if lock is None:
        return  # A live helper owns the status.
    try:
        _clear_status(paths)
    finally:
        os.close(lock)


def _native(terminal):
    try:
        terminal.require_secure()
        return True
    except (Exception, KeyboardInterrupt):
        return False


def _write_denied(terminal):
    """A write the OS refused before launchd was tried: an agent sandbox, unless this is native Terminal."""
    return 'owner_unsafe' if _native(terminal) else 'connect_launch_failed'


def connect(paths, *, client_factory, terminal, inline, runner=subprocess.run, uid=None,
            sleeper=time.sleep, python_executable=Path(sys.executable), broker_plist=None,
            clock=time.time, monotonic=time.monotonic):
    """(JSON-able dict, exit code). Returns once the screen is requested, never waiting on the owner.

    At most about 10 s, or about 40 s when a compatible broker's first check is still starting.
    """
    try:
        broker_plist = paths.account.broker_plist if broker_plist is None else broker_plist
        return _connect(paths, client_factory=client_factory, terminal=terminal, inline=inline,
                        runner=runner, uid=uid, sleeper=sleeper, python_executable=python_executable,
                        broker_plist=broker_plist, clock=clock, monotonic=monotonic)
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
            job = connect_job_state(runner=runner, uid=uid, **_jobs(paths))
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
            # Our transient job; its run is over.
            stop_connect_job(runner=runner, uid=uid, sleeper=sleeper, **_jobs(paths))
            booted = True
            continue
        if terminal and polls < LINGER_WAIT / _STEP:
            polls += 1
            sleeper(_STEP)
            continue
        break
    return None, (dict(_IN_PROGRESS), 0)


def _starting(state, runner, uid, python_executable, broker_plist, jobs):
    """A compatible broker whose check outlived PROBE_CHECK: only our enrolled, validated one is starting.

    Read-only: nothing is stopped or started, whatever the validation finds.
    """
    if state == 'managed':
        try:
            validate_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                     runner=runner, uid=uid, **jobs)
        except LaunchAgentError:
            pass
        else:
            return _refused('broker_starting')
    return _refused('owner_busy')


def _connect(paths, *, client_factory, terminal, inline, runner, uid, sleeper, python_executable, broker_plist,
             clock, monotonic):
    state = setup_status(paths)['state']
    if state == 'legacy_unmanaged':
        return _refused('legacy_unmanaged')
    if state == 'inconsistent':
        return _refused('enrollment_inconsistent')
    probe = _probe_broker(paths, client_factory, monotonic=monotonic)
    if probe == 'ready':
        _forget_finished_run(paths)
        return {'status': 'already_connected'}, 0  # Repeat use never asks for a new login.
    if probe == 'starting':
        return _starting(state, runner, uid, python_executable, broker_plist, _jobs(paths))
    refused = account_registry.login_admission(paths.account)  # Asked before the lock too: then nothing is created.
    if refused is not None:
        return _refused(refused)
    try:
        lock, response = _enter(paths, runner, uid, sleeper, clock)
    except LaunchAgentError:
        return _refused('connect_launch_failed')
    except AccountPathsError:  # A named alias's root is gone, or the registry no longer lists it: nothing created.
        return _refused('alias_unavailable')
    except OwnerLeaseError as error:
        return _refused(_write_denied(terminal) if _denied(error) else 'owner_unsafe')
    if lock is None:
        return response
    try:  # Released right after the bootstrap; an inline run holds it throughout.
        # A named alias run from an installed release, with the lock held: no installer transaction may be pending
        # (review fix 055d3); refused before any stop, job or status change.
        refused = account_registry.login_admission(paths.account)
        if refused is not None:
            return _refused(refused)
        return _locked(paths, state, probe, client_factory=client_factory, terminal=terminal, inline=inline,
                       runner=runner, uid=uid, sleeper=sleeper, python_executable=python_executable,
                       broker_plist=broker_plist)
    finally:
        os.close(lock)


def _locked(paths, state, probe, *, client_factory, terminal, inline, runner, uid, sleeper, python_executable,
            broker_plist):
    stopped, jobs = False, _jobs(paths)
    if probe == 'blocked':
        # The narrow reconnect exception: only an enrolled, receipt-bound, compatible broker whose
        # authorization is still not ready right now. Validation precedes the stop; any doubt keeps it.
        if state != 'managed':
            return _refused('owner_busy')
        # TDLib already answered once: the re-check gets the short budget.
        probe = _probe_broker(paths, client_factory, check_timeout=PROBE_HANDSHAKE)
        if probe == 'ready':
            return {'status': 'already_connected'}, 0
    if probe == 'blocked':
        try:
            validate_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                     runner=runner, uid=uid, **jobs)
        except LaunchAgentError:
            return _refused('owner_busy')  # Nothing was stopped.
        stopped = True  # From here on a failure restores the broker before it reports.
        try:
            stop_enrolled_broker(plist_path=broker_plist, python_executable=python_executable,
                                 runner=runner, uid=uid, sleeper=sleeper, **jobs)
        except LaunchAgentError:
            return _restore('owner_busy', broker_plist, python_executable, runner, uid, jobs)
    def refuse(code):
        return _restore(code, broker_plist, python_executable, runner, uid, jobs) if stopped else _refused(code)
    held = _await_free(paths, sleeper) if stopped else _lease(paths)
    if held != 'free':
        return refuse({'busy': 'owner_busy', 'unsafe': 'owner_unsafe'}.get(held) or _write_denied(terminal))
    cleared = _clear_status(paths)  # Only now: no helper can be running (connect.lock, idle job).
    if cleared != 'ok':
        return refuse('owner_unsafe' if cleared == 'unsafe' else _write_denied(terminal))
    if _native(terminal):  # Foreground in Terminal: nothing to hand to launchd; it restores on failure.
        try:
            result = inline()
        except BaseException:
            if stopped:  # Also when the inline helper could not even be built.
                _restore_quietly(lambda: restore_enrolled_broker(
                    plist_path=broker_plist, python_executable=python_executable, runner=runner, uid=uid, **jobs))
            raise
        value, code = _report(result.phase, result.code, result.retry_after)
        if stopped and value['code'] == 'owner_busy':  # This run stopped (and restored) the broker.
            value['next_action'] = 'run_connect_again'
        return value, code
    try:
        started = start_connect_job(plist_path=job_plist_path(paths), python_executable=python_executable,
                                    runner=runner, uid=uid, sleeper=sleeper, **jobs)
    except LaunchAgentError:
        return refuse('connect_launch_failed')
    if started == 'running':
        if stopped:
            return refuse('connect_in_progress')
        return dict(_IN_PROGRESS), 0
    return {'status': 'screen_opened', 'next_action': 'wait'}, 0


def _restore(code, broker_plist, python_executable, runner, uid, jobs):
    """After our stop, no helper started: put the enrolled broker back, then report truthfully."""
    try:
        restore_enrolled_broker(plist_path=broker_plist, python_executable=python_executable, runner=runner, uid=uid,
                                **jobs)
    except Exception:
        return _refused('broker_start_failed')
    if code == 'connect_in_progress':
        return dict(_IN_PROGRESS), 0
    # Never 'check_existing_broker_without_stopping_it': this run did stop (and restart) it.
    return _refused(code, 'run_connect_again' if code == 'owner_busy' else None)


# wait --------------------------------------------------------------------------------------

def read_connect_status(paths):
    """(phase, code) from a strictly valid status file, else None. Reporting only, never trusted further."""
    record = _read_status(paths)
    return None if record is None else record[:2]


def _read_status(paths):
    """(phase, code, expires_at epoch seconds, retry_after) from a strictly valid status file, else None."""
    try:
        parent = open_private_directory(paths.account.run)
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
                or not valid_retry_after(record['code'], record['retry_after'])
                or any(type(record[name]) is not str or _TIME.fullmatch(record[name]) is None
                       for name in ('updated_at', 'expires_at'))):
            return None
        expires_at = calendar.timegm(time.strptime(record['expires_at'], '%Y-%m-%dT%H:%M:%SZ'))
        return record['phase'], record['code'], expires_at, record['retry_after']
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
        if connect_job_state(runner=runner, uid=uid, **_jobs(paths)) == 'busy':
            return True
    except LaunchAgentError:
        pass  # launchd is not queryable here; the connect lock still tells.
    return _connect_lock_held(paths)


def _exited():
    # Never "connect again" from here: a helper that keeps dying would loop the agent.
    return {'status': 'failed', 'code': 'helper_exited', 'next_action': next_action('failed', 'helper_exited')}, 2


def _in_progress(phase, persisted=False):
    """A non-terminal phase; a QR still unscanned after a whole wait may be on a screen nobody sees."""
    action = 'scan_qr_on_this_mac_screen' if persisted and phase == 'waiting_for_scan' else next_action(phase, None)
    return {'status': phase, 'next_action': action}, 0


def wait(paths, *, runner=subprocess.run, uid=None, sleeper=time.sleep, monotonic=time.monotonic,
         clock=time.time, timeout=WAIT_SECONDS, poll=_POLL):
    """(JSON-able dict, exit code). Returns on the next phase change, a terminal phase, a vanished or
    expired helper or the deadline. The phase it started from is the one it does not report again,
    so a fresh run's QR is announced within seconds and each later wait returns on the next step."""
    try:
        return _wait(paths, runner, uid, sleeper, monotonic, clock, timeout, poll)
    except KeyboardInterrupt:
        return _refused('cancelled')
    except Exception:
        return {'status': 'failed', 'code': 'dependency_unavailable',
                'next_action': next_action('failed', 'dependency_unavailable')}, 2


def _wait(paths, runner, uid, sleeper, monotonic, clock, timeout, poll):
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(poll) not in (int, float) or not math.isfinite(poll) or poll <= 0):
        raise ValueError()
    deadline = monotonic() + timeout
    start, phase, gone = None, 'starting', 0  # No status yet reads as starting.
    while True:
        record = _read_status(paths)
        if record is not None:
            phase = record[0]
            if phase in _TERMINAL:
                return _report(phase, record[1], record[3])
            if clock() > record[2] + HUNG_GRACE:  # Past its own lifetime: no live helper writes this.
                return _exited()
        if start is None:
            start = phase
        elif phase != start:
            return _in_progress(phase)
        if _helper_alive(paths, runner, uid):
            gone = 0
        else:
            gone += 1
            if gone >= 2:  # Two looks, so a helper still starting up is not mistaken for a dead one.
                record = _read_status(paths)  # It may have finished between the look and the check.
                if record is not None and record[0] in _TERMINAL:
                    return _report(record[0], record[1], record[3])
                return _exited()
        left = deadline - monotonic()
        if left <= 0:
            return _in_progress(phase, persisted=True)
        sleeper(min(poll, left))


# cancel ------------------------------------------------------------------------------------

def cancel(paths, *, runner=subprocess.run, uid=None, sleeper=time.sleep, clock=time.time,
           restore_broker=None):
    """(JSON-able dict, exit code). Boots out our own running helper job, nothing else.

    launchd's SIGTERM makes the helper cancel its run, which tears the auth client down and
    writes `cancelled` itself. A helper that died without a terminal status gets one here, under
    connect.lock, and an enrolled broker it may have stopped is put back (never created).
    An inline run in the user's Terminal is not ours to stop: nothing_to_cancel.
    """
    try:
        jobs = _jobs(paths)
        if restore_broker is None:
            restore_broker = functools.partial(restore_enrolled_broker, **jobs)
        if connect_job_state(runner=runner, uid=uid, **jobs) != 'busy':
            return {'status': 'nothing_to_cancel'}, 0
        stop_connect_job(runner=runner, uid=uid, sleeper=sleeper, **jobs)
    except (Exception, KeyboardInterrupt):  # launchd unusable here, or interrupted: the run expires by itself.
        return _refused('cancel_failed')
    try:
        lock = _take_connect_lock(paths)
    except Exception:
        lock = None
    if lock is not None:
        try:
            record = _read_status(paths)
            if record is not None and record[0] not in _TERMINAL:
                write_connect_status(paths, 'cancelled', 'cancelled', updated_at=clock(), expires_at=record[2])
                _restore_quietly(restore_broker)
        except Exception:
            pass
        finally:
            os.close(lock)
    return {'status': 'cancelled'}, 0


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


class _Terminated(Exception):
    """SIGTERM during the linger: the run already ended and was reported; leave at once."""


def _on_sigterm(ui, stopping):
    """Install the helper's SIGTERM handler (launchd bootout); returns the previous disposition."""
    def handler(signum, frame):
        stopping['terminated'] = True
        cancel_page = getattr(ui, 'request_cancel', None)
        if cancel_page is not None:
            # Never take the UI's lock inside the handler: the interrupted main thread may hold it.
            threading.Thread(target=cancel_page, name='connect-cancel', daemon=True).start()
        if stopping['lingering']:
            raise _Terminated()
    return signal.signal(signal.SIGTERM, handler)


def _record_failure(paths, clock, expires_at, code):
    """A helper that fails before run_connect reports must not leave `wait` to watch it vanish."""
    try:
        record = _read_status(paths)
        if record is not None and record[0] in _TERMINAL:
            return
        write_connect_status(paths, 'cancelled' if code == 'cancelled' else 'failed', code,
                             updated_at=clock(), expires_at=expires_at)
    except Exception:
        pass


def connect_helper(paths, *, store, provider_factory, handoff, own_plist=None, ui_factory=None,
                   server_factory=None, run=run_connect, opener=open_url, clock=time.time,
                   sleeper=time.sleep, linger=HELPER_LINGER, lifetime=HELPER_LIFETIME, lock_held=False,
                   restore_broker=None, handle_sigterm=False):
    """Serve the loopback screen and drive one run_connect; returns its ConnectResult.

    The single helper: it holds connect.lock for its whole run including the linger
    (inline, the calling connect already holds it), else ends connect_in_progress without
    touching the status or the screen. The URL exists only between the server and the
    opener: it is never printed, logged or kept. One deadline, taken once here, is both the
    page's countdown and run_connect's expiry. run_connect owns the status file, except for
    an owner_busy run, which writes none and is recorded here, and a failure before or
    around the run, recorded here as a terminal status unless one exists. A run that does
    not end ready puts back an existing enrolled broker that is no longer loaded
    (restore_broker never creates one). With handle_sigterm (the launchd job), SIGTERM
    cancels the run through the UI so the auth client is torn down normally, and skips the
    linger. The page stays up briefly after a terminal phase, then the server closes and
    our own launchd plist is removed. A named alias's helper run from an installed release
    asks account_registry.login_admission once it holds the lock (a refusal, such as
    `transaction_pending`, is written as its terminal status; no screen, run or restore)
    and again right before its handoff (review fix 055d3).
    """
    if restore_broker is None:
        restore_broker = functools.partial(restore_enrolled_broker, **_jobs(paths))
    if ui_factory is None or server_factory is None:
        from .connect_server import ConnectServer, LoopbackConnectUI
        ui_factory = ui_factory or LoopbackConnectUI
        server_factory = server_factory or ConnectServer
    server, lock, result, ran, expires_at = None, None, None, False, None
    stopping, previous, installed = {'terminated': False, 'lingering': False}, None, False
    failure = 'dependency_unavailable'
    try:
        if not lock_held:
            try:
                lock = _await_connect_lock(paths, sleeper)
            except AccountPathsError:  # A named alias's root is gone or no longer listed: nothing to own or put back.
                return ConnectResult('failed', 'alias_unavailable')
            except OwnerLeaseError:
                ran = True  # Nobody else can own this run: restore below.
                return ConnectResult('failed', 'owner_unsafe')
            if lock is None:
                return ConnectResult('failed', 'connect_in_progress')  # The lock holder owns the status.
            refused = account_registry.login_admission(paths.account)
            if refused is not None:
                # A pending installer transaction owns the services (review fix 055d3): only the status is ours.
                _record_failure(paths, clock, clock() + lifetime, refused)
                return ConnectResult('failed', refused)
        ran = True
        expires_at = clock() + lifetime  # The one deadline of this run.
        ui = ui_factory()
        if handle_sigterm:
            previous, installed = _on_sigterm(ui, stopping), True
        # A named alias's page names the alias (never the account); the default's call is unchanged.
        server = server_factory(ui, expires_at, **_page(paths))
        url = server.start()
        try:
            opener(url)
        except Exception:
            pass  # Headless or no browser: the run goes on and `wait` keeps reporting its phase.
        finally:
            url = None
        result = run(paths, store, provider_factory, ui, handoff=lambda: _handoff(paths, handoff),
                     expires_at=expires_at)
        if result.phase == 'failed' and result.code == 'owner_busy':
            try:  # We hold connect.lock, so this status is ours to write.
                write_connect_status(paths, 'failed', 'owner_busy', updated_at=clock(), expires_at=expires_at)
            except Exception:
                pass
        try:  # A run that never took the lease did not tell the page.
            ui.notify(result.phase, result.code, retry_after=result.retry_after)
        except Exception:
            pass
        if result.phase != 'ready':
            _restore_quietly(restore_broker)
        if not stopping['terminated']:
            stopping['lingering'] = True
            sleeper(linger)
        return result
    except (KeyboardInterrupt, _Terminated):
        failure = 'cancelled'
        raise
    finally:
        stopping['lingering'] = False  # A later SIGTERM must not interrupt the cleanup below.
        try:
            if ran and result is None:  # Raised or refused before a result: still never leave it stopped.
                if expires_at is not None and (lock is not None or lock_held):
                    _record_failure(paths, clock, expires_at, failure)
                _restore_quietly(restore_broker)
            if server is not None:
                server.close()
            if own_plist is not None:
                remove_connect_job(own_plist)
        finally:
            if installed:
                signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
            if lock is not None:
                os.close(lock)


def _handoff(paths, handoff):
    """The handoff, after the admission is asked again for a named alias (review fix 055d3): a refusal is the run's
    code and no broker is started."""
    refused = account_registry.login_admission(paths.account)
    return handoff(paths=paths) if refused is None else refused


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
    refuse = lambda code: _refused(code, None if code in _API_KEPT else _AGAIN)
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
    login = None
    try:
        if paths.account.alias != DEFAULT_ALIAS:  # Inside the alias's existing root only (review fix 055d3).
            login = account_registry.login_lock(paths.account)
        with SessionOwnerLease(paths.lock):  # No connect run may create an enrollment meanwhile.
            state = setup_status(paths)['state']
            if state != 'new':
                return refuse('enrollment_exists' if state == 'managed' else 'enrollment_inconsistent')
            store = _WriteCounter(store_factory())
            _api_credentials(terminal, store, remaining)
    except AccountPathsError:
        return refuse('alias_unavailable')
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
    finally:
        if login is not None:
            os.close(login)
    return ({'status': 'stored'} if store.writes else {'status': 'already_present'}), 0
