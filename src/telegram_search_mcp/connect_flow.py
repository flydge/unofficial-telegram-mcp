"""QR-first connect: the run_setup structure driven by a UI port instead of a terminal.

One run owns the session lease, resolves the bound API identity, drives the pinned
QR/2FA auth states, tears the auth client down and hands off to the broker. The QR
link and the 2FA password exist only as SecretValue between the UI port and the
transport: never in results, status, logs or exception text. Uncertain password
submission stops resumable and is never retried. The terminal path is unchanged.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import math
import os
import secrets
import time
from . import __version__, account_registry
from .app_identity import AppIdentityError, bundled_app_identity
from .auth_transport import AuthTransport, AuthTransportError, AuthorizationState
from .config import ConfigurationError, load_runtime_policy
from .credential_identity import CredentialIdentityError, ResolvedIdentity, resolve_enrolled, resolve_new
from .credentials import CredentialStoreError, SecretValue
from .enrollment import SetupPaths, EnrollmentStore, EnrollmentError, managed_key
from .session_owner import SessionOwnerLease, OwnerLeaseError, open_private_directory
from .setup_wizard import _ensure_policy

PHASES = ('starting', 'waiting_for_scan', 'waiting_for_password', 'authorizing', 'handing_off',
          'ready', 'failed', 'expired', 'cancelled')
_TERMINAL = frozenset({'ready', 'failed', 'expired', 'cancelled'})
_CODES = frozenset({'owner_busy', 'owner_unsafe', 'legacy_unmanaged', 'enrollment_missing',
    'enrollment_inconsistent', 'managed_key_missing', 'credential_denied', 'credential_locked',
    'credential_unavailable', 'credential_invalid_record', 'credential_conflict', 'app_identity_missing',
    'app_identity_invalid', 'identity_changed', 'policy_unsafe', 'unsupported_auth_state', 'invalid_password',
    'invalid_input', 'flood_wait', 'dependency_unavailable', 'auth_request_failed', 'timeout',
    'password_submission_uncertain', 'expired', 'cancelled', 'auth_close_failed', 'auth_destroy_failed',
    'broker_start_failed', 'broker_incompatible', 'broker_readiness_timeout',
    # The account pin at login (0.55 spec 3C): never a user id, only these fixed codes.
    'account_mismatch', 'account_duplicate', 'alias_unavailable',
    # A named alias's login run from an installed release (review fix 055d3, account_registry.login_admission).
    'transaction_pending', 'newer_state_kept'})
STATUS_CODES = _CODES  # Public: the fixed codes a UI may display.
MAX_RETRY_AFTER = 86400  # Seconds; a flood_wait retry time outside 1..this is never written.
# The one next_action table: ConnectResult.to_dict, connect's inline result and every `wait` and
# refusal of the connect CLI read it, so a code means the same thing wherever it is reported.
# The CLI-only codes at the end never appear in the status file.
NEXT_ACTIONS = {
    'legacy_unmanaged': 'preserve_existing_session_and_use_legacy_install_path',
    'enrollment_inconsistent': 'review_enrollment_binding_without_resetting',
    'identity_changed': 'review_enrollment_binding_without_resetting',
    'account_mismatch': 'review_enrollment_binding_without_resetting',
    'account_duplicate': 'review_enrollment_binding_without_resetting',
    'alias_unavailable': 'stop_and_report',
    'transaction_pending': 'finish_pending_installer_action_then_retry',
    'newer_state_kept': 'upgrade_installation_or_remove_account',
    'managed_key_missing': 'restore_exact_managed_key_without_regenerating',
    'enrollment_missing': 'stop_and_report', 'app_identity_invalid': 'stop_and_report',
    'policy_unsafe': 'stop_and_report', 'owner_unsafe': 'stop_and_report',
    'credential_unavailable': 'stop_and_report',
    'credential_invalid_record': 'repair_keychain_record_manually',
    'owner_busy': 'check_existing_broker_without_stopping_it',
    'credential_conflict': 'run_api_credentials_in_native_terminal',
    'credential_denied': 'unlock_keychain_then_connect_again',
    'credential_locked': 'unlock_keychain_then_connect_again',
    'app_identity_missing': 'use_terminal_setup_fallback',
    'unsupported_auth_state': 'use_terminal_setup_fallback',
    'broker_start_failed': 'run_connect_in_native_terminal',
    'broker_readiness_timeout': 'run_connect_in_native_terminal',
    'broker_incompatible': 'run_connect_in_native_terminal',
    'flood_wait': 'wait_retry_after_then_connect_again',
    'auth_destroy_failed': 'exit_connect_process_then_run_connect_again',
    'invalid_password': 'run_connect_again', 'invalid_input': 'run_connect_again',
    'password_submission_uncertain': 'run_connect_again', 'auth_request_failed': 'run_connect_again',
    'timeout': 'run_connect_again', 'dependency_unavailable': 'run_connect_again',
    'auth_close_failed': 'run_connect_again', 'expired': 'run_connect_again', 'cancelled': 'run_connect_again',
    # CLI only.
    'secure_terminal_required': 'run_in_foreground_native_terminal',
    'connect_launch_failed': 'run_connect_in_native_terminal',
    'helper_exited': 'run_connect_in_native_terminal',  # Inline, the run shows its real refusal.
    'broker_starting': 'wait_then_connect_again',
    'connect_in_progress': 'wait', 'cancel_failed': 'wait',
    'enrollment_exists': 'run_connect', 'connect_helper_requires_launchd': 'run_connect',
}
# A run still in progress: what the agent does with a non-terminal phase that `wait` reports.
PHASE_ACTIONS = {'waiting_for_scan': 'tell_user_to_scan_qr_then_wait',
                 'waiting_for_password': 'tell_user_to_enter_password_on_page_then_wait'}
PASSWORD_ATTEMPTS = 5  # Submissions per run; Telegram's flood control is the security bound.
_SLICE = 1.0  # Longest wait between cancel/expiry checks.
_STEP = 30  # Cap for one provider request inside the run lifetime.
_DEFINITIVE_PASSWORD = frozenset({'invalid_password', 'invalid_input'})
# A submission needs a provider round trip; with less left its outcome would be
# uncertain by construction, so the run expires instead of sending.
_MIN_SUBMIT = 5.0


class ConnectUI(ABC):
    """What phase C's loopback screen implements. Calls come from the run's thread."""

    @abstractmethod
    def publish_qr(self, link: SecretValue) -> None:
        """Show this link as the current QR; a later call replaces it."""

    @abstractmethod
    def request_password(self, hint: str, timeout: float) -> SecretValue | None:
        """Block up to timeout for one password; None when nothing was entered."""

    @abstractmethod
    def cancelled(self) -> bool: ...

    @abstractmethod
    def notify(self, phase: str, code: str | None, retry_after: int | None = None) -> None:
        """retry_after: seconds Telegram asked to wait, only with flood_wait."""


@dataclass(frozen=True)
class ConnectResult:
    phase: str
    code: str | None
    auth_client_closed: bool = False
    auth_client_destroyed: bool = False
    retry_after: int | None = None

    @property
    def exit_code(self): return 0 if self.phase == 'ready' else 2

    def to_dict(self):
        return dict(phase=self.phase, code=self.code, next_action=next_action(self.phase, self.code),
            auth_client_closed=self.auth_client_closed, auth_client_destroyed=self.auth_client_destroyed,
            retry_after=self.retry_after)


def next_action(phase, code):
    """From NEXT_ACTIONS; an unmapped terminal code stops instead of inviting a retry loop."""
    if phase == 'ready':
        return 'none'
    if phase in _TERMINAL:
        return NEXT_ACTIONS.get(code, 'stop_and_report')
    return PHASE_ACTIONS.get(phase, 'wait')


def _failed(code, *, closed=False, destroyed=None, retry_after=None):
    code = code if code in _CODES else 'dependency_unavailable'
    phase = code if code in ('expired', 'cancelled') else 'failed'
    return ConnectResult(phase, code, closed, closed if destroyed is None else destroyed, retry_after)


class _Stop(Exception):
    """Internal: the run ends with this code; carries no input or provider text."""
    def __init__(self, code, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


def _iso(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(seconds)))


def connect_status_path(paths):
    return paths.account.connect_status


def valid_retry_after(code, retry_after):
    """None, or whole seconds in 1..MAX_RETRY_AFTER carried only by flood_wait."""
    return retry_after is None or (type(retry_after) is int and 1 <= retry_after <= MAX_RETRY_AFTER
                                   and code == 'flood_wait')


def write_connect_status(paths, phase, code, *, updated_at, expires_at, retry_after=None):
    """Atomic 0600 replace in the private run directory; fixed fields only."""
    if phase not in PHASES or (code is not None and code not in _CODES) or not valid_retry_after(code, retry_after):
        raise ValueError('invalid_status')
    data = json.dumps({'phase': phase, 'code': code, 'updated_at': _iso(updated_at),
                       'expires_at': _iso(expires_at), 'retry_after': retry_after},
                      sort_keys=True, separators=(',', ':')).encode('ascii')
    target = connect_status_path(paths)
    parent = open_private_directory(target.parent, create=True)
    temporary = '.connect-status-' + secrets.token_hex(16)
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                             dir_fd=parent)
        created = True
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(data)
                stream.flush()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


class _Progress:
    """Status file and UI see the same phase; the final write is best effort."""
    def __init__(self, paths, ui, wall, expires_at):
        self.paths, self.ui, self.wall, self.expires_at = paths, ui, wall, expires_at
        self.current = None

    def __call__(self, phase, code=None, retry_after=None):
        if (phase, code, retry_after) == self.current:
            return
        self.current = (phase, code, retry_after)
        try:
            write_connect_status(self.paths, phase, code, updated_at=self.wall(), expires_at=self.expires_at,
                                 retry_after=retry_after)
        finally:
            # The page must learn the phase even if the file write fails.
            self.ui.notify(phase, code, retry_after=retry_after)

    def final(self, phase, code, retry_after=None):
        try:
            self(phase, code, retry_after)
        except Exception:
            pass


def _keychain_pair(store):
    def no_bundled():
        raise CredentialIdentityError('credential_unavailable')
    return resolve_new(store, no_bundled).credentials


def _drive(transport, ui, progress, credentials, key, paths, remaining):
    """Returns once the provider reports Ready; any other end raises _Stop/transport errors."""
    qr_requested, attempts = False, 0
    transport.get_authorization_state(timeout=remaining(_STEP))
    for _ in range(4096):
        observation = transport.observation()
        state = observation.state
        if state is AuthorizationState.READY:
            return  # An observed Ready wins over a late cancel: the device is already authorized.
        if ui.cancelled():
            raise _Stop('cancelled')
        if state is AuthorizationState.PARAMETERS:
            transport.set_parameters(credentials, key, database_directory=str(paths.account.database),
                files_directory=str(paths.account.files), application_version=__version__,
                timeout=remaining(_STEP))
        elif state is AuthorizationState.ENCRYPTION:
            transport.check_database_key(key, timeout=remaining(_STEP))
        elif state is AuthorizationState.PHONE:
            if qr_requested:
                raise _Stop('unsupported_auth_state')
            qr_requested = True
            transport.request_qr_code(timeout=remaining(_STEP))
        elif state is AuthorizationState.OTHER_DEVICE:
            ui.publish_qr(observation.qr_link)
            progress('waiting_for_scan')
        elif state is AuthorizationState.PASSWORD:
            attempts = _password(transport, ui, progress, observation, attempts, remaining)
        else:
            raise _Stop('unsupported_auth_state')
        _await_change(transport, ui, observation, remaining)
    raise _Stop('timeout')


def _pin_account(transport, paths, remaining):
    """The account pin at login, before the broker is handed the session (0.55 spec 3C): the rule `setup` and
    `resume` apply too (`account_registry.login_refusal`). The user id never reaches a result, the status or the
    page."""
    code = account_registry.login_refusal(paths.account,
                                          lambda: transport.get_account_id(timeout=remaining(_STEP)))
    if code is not None:
        raise _Stop(code)


def _password(transport, ui, progress, observation, attempts, remaining):
    """One request in flight; only a definitive refusal allows re-entry."""
    code = None
    while attempts < PASSWORD_ATTEMPTS:
        progress('waiting_for_password', code)
        secret = None
        try:
            for _ in range(100000):
                if ui.cancelled():
                    raise _Stop('cancelled')
                secret = ui.request_password(observation.password_hint or '', remaining(_SLICE))
                if secret is not None:
                    break
            if type(secret) is not SecretValue:
                raise _Stop('invalid_input')
            if remaining(_STEP) < _MIN_SUBMIT:
                raise _Stop('expired')
            attempts += 1
            progress('authorizing')
            # Raise outside the handler: the transport error's frames hold the password,
            # so it must not stay reachable as __context__.
            failure = None
            try:
                transport.check_password(secret, timeout=remaining(_STEP))
            except AuthTransportError as error:
                failure = (error.code, error.retry_after)
            except TimeoutError:
                failure = ('expired', None)
            if failure is None:
                return attempts
            if failure[0] in _DEFINITIVE_PASSWORD:
                code = failure[0]
                continue
            if failure[0] in ('timeout', 'dependency_unavailable', 'auth_request_failed'):
                raise _Stop('password_submission_uncertain')
            raise _Stop(*failure)
        finally:
            secret = None
    raise _Stop(code or 'invalid_password')


def _await_change(transport, ui, previous, remaining):
    """Slices keep cancel and expiry responsive while the provider is quiet."""
    for _ in range(1000000):
        current = transport.observation()
        if current is not None and not current.same_as(previous):
            return current  # Already changed (possibly to Ready): report it before a cancel.
        if ui.cancelled():
            raise _Stop('cancelled')
        try:
            return transport.wait_for_change(previous, timeout=remaining(_SLICE))
        except AuthTransportError as error:
            if error.code != 'timeout':
                raise
    raise _Stop('timeout')


def run_connect(paths, store, provider_factory, ui, *, handoff, lifetime=600, close_timeout=10,
                bundled_loader=bundled_app_identity, expires_at=None, _clock=time.monotonic,
                _wall=time.time) -> ConnectResult:
    """No terminal: secrets come only through the UI port, credentials only from the bound identity.

    A run that cannot take the owner lease writes no status: the lease holder owns it.
    Handing-off and its outcome are written after the lease is released (the broker
    takes it); the connect CLI guarantees a single helper and clears stale status.
    With expires_at (epoch seconds, the helper's one deadline) the lifetime is what is left
    of it now, and the status carries exactly that expiry. The run also expires once the wall
    clock passes it, so a sleeping Mac cannot keep it alive past the time the status shows.
    """
    if expires_at is not None and type(expires_at) in (int, float) and math.isfinite(expires_at):
        lifetime = expires_at - _wall()
    elif expires_at is not None:
        lifetime = None
    if (type(paths) is not SetupPaths or not isinstance(ui, ConnectUI)
            or type(lifetime) not in (int, float) or not math.isfinite(lifetime) or not 0 < lifetime <= 3600
            or type(close_timeout) not in (int, float) or not math.isfinite(close_timeout)
            or not 0 < close_timeout <= 60):
        return _failed('invalid_input')
    deadline = _clock() + lifetime
    wall_deadline = _wall() + lifetime if expires_at is None else expires_at
    progress = _Progress(paths, ui, _wall, wall_deadline)
    def left():
        return min(deadline - _clock(), wall_deadline - _wall())
    def remaining(cap):
        value = left()
        if value <= 0: raise TimeoutError()
        return min(cap, value)
    def expired_or(code):
        return 'expired' if code == 'timeout' and left() <= 0 else code
    transport, result = None, _failed('dependency_unavailable')
    enrollment = EnrollmentStore(paths)
    acquired = False
    try:
        with SessionOwnerLease(paths.lock) as lease:
            acquired = True
            try:
                progress('starting')
                record = enrollment.read()
                if record is None:
                    enrollment.require_new()  # Before credential resolution/native construction.
                load_runtime_policy(paths.policy)
                lease.check()
                if record is not None:
                    key = managed_key(record, store)  # Never regenerate a managed key.
                    credentials = resolve_enrolled(record, bundled_loader=bundled_loader,
                                                   keychain_loader=lambda: _keychain_pair(store))
                    # A pre-0.52 record gains its binding with this write (spec §2).
                    enrollment.set_state('pending', bound=ResolvedIdentity(credentials, 'keychain')
                                         if 'credential_source' not in record else None)
                else:
                    identity = resolve_new(store, bundled_loader)
                    credentials = identity.credentials
                    record, key = enrollment.create(store, bound=identity)
                _ensure_policy(paths)
                for directory in (paths.account.database, paths.account.files):
                    fd = open_private_directory(directory, create=True)
                    os.close(fd)
                lease.check()
                remaining(_STEP)
                transport = AuthTransport(provider_factory())
                _drive(transport, ui, progress, credentials, key, paths, remaining)
                _pin_account(transport, paths, remaining)
                result = ConnectResult('authorizing', None)
            except _Stop as stop:
                result = _failed(expired_or(stop.code), retry_after=stop.retry_after)
            except EnrollmentError as error:
                result = _failed(error.code)
            except CredentialStoreError as error:
                result = _failed('credential_' + error.code)
            except (CredentialIdentityError, AppIdentityError) as error:
                result = _failed(error.code)
            except AuthTransportError as error:
                result = _failed(expired_or(error.code), retry_after=error.retry_after)
            except ConfigurationError:
                result = _failed('policy_unsafe')
            except TimeoutError:
                result = _failed('expired')
            except KeyboardInterrupt:
                result = _failed('cancelled')
            except OwnerLeaseError as error:
                result = _failed(error.code)
            except Exception:
                result = _failed('dependency_unavailable')
            finally:
                if transport is not None:
                    teardown = transport.close(timeout=close_timeout)
                    if not teardown.native_destroyed:
                        lease.retain_until_process_exit(transport)
                        result = _failed('auth_destroy_failed', closed=teardown.provider_closed, destroyed=False)
                    elif not teardown.provider_closed:
                        result = _failed('auth_close_failed', destroyed=True)
                    else:
                        result = ConnectResult(result.phase, result.code, True, True, result.retry_after)
                if result.phase in _TERMINAL:
                    progress.final(result.phase, result.code, result.retry_after)
                lease.check()
    except OwnerLeaseError as error:
        if not acquired:
            return _failed(error.code)  # Contended: the lease holder owns the status file.
        result = _failed(error.code, closed=result.auth_client_closed, destroyed=result.auth_client_destroyed)
        progress.final(result.phase, result.code)
        return result
    except Exception:
        result = _failed('dependency_unavailable')
        if acquired:
            progress.final(result.phase, result.code)
        return result
    if result.phase == 'authorizing' and result.auth_client_closed and result.auth_client_destroyed:
        try:
            progress.final('handing_off', None)
            boundary = handoff()
            if boundary != 'ready':
                result = _failed(boundary, closed=True)
            else:
                enrollment.set_state('ready')
                result = ConnectResult('ready', None, True, True)
        except (Exception, KeyboardInterrupt):
            result = _failed('broker_start_failed', closed=True)
        progress.final(result.phase, result.code)
        return result
    if result.phase not in _TERMINAL:
        result = _failed('dependency_unavailable', closed=result.auth_client_closed,
                         destroyed=result.auth_client_destroyed)
        progress.final(result.phase, result.code)
    return result
