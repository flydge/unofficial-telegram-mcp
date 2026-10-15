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
from . import __version__
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
    'broker_start_failed', 'broker_incompatible', 'broker_readiness_timeout'})
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
    def notify(self, phase: str, code: str | None) -> None: ...


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
        actions = {'owner_busy': 'check_existing_broker_without_stopping_it',
            'legacy_unmanaged': 'preserve_existing_session_and_use_legacy_install_path',
            'enrollment_inconsistent': 'review_enrollment_binding_without_resetting',
            'managed_key_missing': 'restore_exact_managed_key_without_regenerating',
            'unsupported_auth_state': 'use_terminal_setup_fallback',
            'app_identity_missing': 'use_terminal_setup_fallback',
            'identity_changed': 'review_enrollment_binding_without_resetting',
            'auth_destroy_failed': 'exit_connect_process_then_run_connect_again',
            'broker_incompatible': 'use_matching_installed_package_and_policy'}
        return dict(phase=self.phase, code=self.code,
            next_action='none' if self.phase == 'ready' else actions.get(self.code, 'run_connect_again'),
            auth_client_closed=self.auth_client_closed, auth_client_destroyed=self.auth_client_destroyed,
            retry_after=self.retry_after)


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
    return paths.lock.parent / 'connect-status.json'


def write_connect_status(paths, phase, code, *, updated_at, expires_at):
    """Atomic 0600 replace in the private run directory; fixed fields only."""
    if phase not in PHASES or (code is not None and code not in _CODES):
        raise ValueError('invalid_status')
    data = json.dumps({'phase': phase, 'code': code, 'updated_at': _iso(updated_at),
                       'expires_at': _iso(expires_at)}, sort_keys=True, separators=(',', ':')).encode('ascii')
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

    def __call__(self, phase, code=None):
        if (phase, code) == self.current:
            return
        self.current = (phase, code)
        try:
            write_connect_status(self.paths, phase, code, updated_at=self.wall(), expires_at=self.expires_at)
        finally:
            self.ui.notify(phase, code)  # The page must learn the phase even if the file write fails.

    def final(self, phase, code):
        try:
            self(phase, code)
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
            transport.set_parameters(credentials, key, database_directory=str(paths.session / 'db'),
                files_directory=str(paths.session / 'files'), application_version=__version__,
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
                bundled_loader=bundled_app_identity, _clock=time.monotonic, _wall=time.time) -> ConnectResult:
    """No terminal: secrets come only through the UI port, credentials only from the bound identity.

    A run that cannot take the owner lease writes no status: the lease holder owns it.
    Handing-off and its outcome are written after the lease is released (the broker
    takes it); the connect CLI guarantees a single helper and clears stale status.
    """
    if (type(paths) is not SetupPaths or not isinstance(ui, ConnectUI)
            or type(lifetime) not in (int, float) or not math.isfinite(lifetime) or not 0 < lifetime <= 3600
            or type(close_timeout) not in (int, float) or not math.isfinite(close_timeout)
            or not 0 < close_timeout <= 60):
        return _failed('invalid_input')
    deadline = _clock() + lifetime
    progress = _Progress(paths, ui, _wall, _wall() + lifetime)
    def remaining(cap):
        left = deadline - _clock()
        if left <= 0: raise TimeoutError()
        return min(cap, left)
    def expired_or(code):
        return 'expired' if code == 'timeout' and deadline - _clock() <= 0 else code
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
                for directory in (paths.session / 'db', paths.session / 'files'):
                    fd = open_private_directory(directory, create=True)
                    os.close(fd)
                lease.check()
                remaining(_STEP)
                transport = AuthTransport(provider_factory())
                _drive(transport, ui, progress, credentials, key, paths, remaining)
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
                    progress.final(result.phase, result.code)
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
