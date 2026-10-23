"""Local-only default-session auth; checkpoints never stand in for Ready."""
from __future__ import annotations
from dataclasses import dataclass
import math
import os
import time
from . import __version__, account_registry
from .app_identity import AppIdentityError, bundled_app_identity
from .auth_transport import AuthTransport, AuthTransportError, AuthorizationState
from .config import DEFAULT_ALIAS, ConfigurationError, load_runtime_policy
from .credential_identity import CredentialIdentityError, ResolvedIdentity, resolve_enrolled, valid_record
from .credentials import CredentialStoreError
from .enrollment import SetupPaths, EnrollmentStore, EnrollmentError, managed_key
from .keychain import ApiCredentials
from .secure_terminal import TerminalError
from .session_owner import SessionOwnerLease, OwnerLeaseError, acquire_owner_lock, open_private_directory

_CODES = frozenset({'secure_terminal_required', 'owner_busy', 'owner_unsafe', 'legacy_unmanaged',
    'enrollment_missing', 'enrollment_inconsistent', 'managed_key_missing', 'credential_denied',
    'credential_locked', 'credential_unavailable', 'credential_invalid_record', 'credential_conflict',
    'unsupported_auth_state', 'invalid_code', 'code_expired', 'invalid_password', 'flood_wait',
    'dependency_unavailable', 'auth_request_failed', 'timeout', 'cancelled', 'invalid_input',
    'auth_close_failed', 'auth_destroy_failed', 'broker_start_failed', 'broker_incompatible', 'broker_readiness_timeout',
    'policy_unsafe', 'app_identity_missing', 'app_identity_invalid', 'identity_changed',
    # The account pin at login, as connect reports it (0.55 spec 3C): never a user id, only these fixed codes.
    'account_mismatch', 'account_duplicate', 'alias_unavailable'})


@dataclass(frozen=True)
class SetupResult:
    status: str
    code: str | None
    auth_client_closed: bool = False
    retry_after: int | None = None
    auth_client_destroyed: bool = False
    @property
    def exit_code(self): return 0 if self.status == 'ready' else 2
    def to_dict(self):
        actions = {'secure_terminal_required': 'run_in_foreground_native_terminal',
            'owner_busy': 'check_existing_broker_without_stopping_it',
            'legacy_unmanaged': 'preserve_existing_session_and_use_legacy_install_path',
            'enrollment_inconsistent': 'review_enrollment_binding_without_resetting',
            'account_mismatch': 'review_enrollment_binding_without_resetting',
            'account_duplicate': 'review_enrollment_binding_without_resetting',
            'alias_unavailable': 'stop_and_report',
            'managed_key_missing': 'restore_exact_managed_key_without_regenerating',
            'auth_destroy_failed': 'exit_setup_process_then_resume_in_foreground_native_terminal',
            'unsupported_auth_state': 'complete_supported_auth_with_official_telegram_guidance',
            'broker_incompatible': 'use_matching_installed_package_and_policy'}
        return dict(status=self.status, code=self.code,
            next_action='none' if self.status == 'ready' else actions.get(self.code, 'resume_in_foreground_native_terminal'),
            auth_client_closed=self.auth_client_closed, auth_client_destroyed=self.auth_client_destroyed,
            retry_after=self.retry_after)


def _result(code, *, closed=False, destroyed=None, retry_after=None):
    code = code if code in _CODES else 'dependency_unavailable'
    return SetupResult('pending', code, closed, retry_after, closed if destroyed is None else destroyed)


def _api_credentials(terminal, store, remaining, bundled_loader=lambda: None, expected_fingerprint=None):
    """Terminal path. Keeps add-only partial-pair resume (pre-0.52 behaviour, spec §2).

    For a bound enrollment the typed pair is checked against its fingerprint before
    any Keychain write, so a wrong pair is refused instead of persisted.
    """
    values = {name: store.get(name) for name in ('api_id', 'api_hash')}
    missing = [name for name, value in values.items() if value is None]
    valid = valid_record
    if any(not valid(name, value) for name, value in values.items() if value is not None):
        raise CredentialStoreError('invalid_record')
    if len(missing) == 2:
        # No owner pair at all: an admitted bundle supplies the application identity.
        bundled = bundled_loader()
        if bundled is not None:
            return ResolvedIdentity(bundled, 'bundled')
    for name in missing:
        values[name] = terminal.read_secret(name, timeout=remaining())
        if not valid(name, values[name]):
            raise TerminalError('invalid_input')
    identity = ResolvedIdentity(ApiCredentials(int(values['api_id'].reveal()),
                                               values['api_hash'].reveal().decode('ascii')), 'keychain')
    if expected_fingerprint is not None and identity.fingerprint != expected_fingerprint:
        raise CredentialIdentityError('identity_changed')
    # Validate both first. Add-only writes leave partial progress recoverable.
    for name in missing:
        store.put(name, values[name])
    return identity


def _ensure_policy(paths):
    from .contract import CONTRACT_VERSION
    parent = open_private_directory(paths.root)
    try:
        try:
            descriptor = os.open(paths.policy.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
        except FileExistsError:
            load_runtime_policy(paths.policy)
            return
        try:
            data = (f'config_version = 1\nexpected_package_version = "{__version__}"\n'
                    f'expected_contract_version = {CONTRACT_VERSION}\n'
                    'enabled_capabilities = ["read", "artifacts"]\n').encode('ascii')
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(data)
                stream.flush()
            os.fsync(descriptor)
            os.fsync(parent)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def setup_status(paths=None):
    """Read metadata only; no native client, Keychain or launchd operation."""
    paths = SetupPaths() if paths is None else paths
    try:
        store = EnrollmentStore(paths)
        record = store.read()
        if record is None:
            store.require_new()
            return {'state': 'new', 'broker': 'not_checked'}
        return {'state': 'managed', 'checkpoint': record['state'], 'authorization': 'not_checked',
                'broker': 'not_checked'}
    except EnrollmentError as error:
        return {'state': 'legacy_unmanaged' if error.code == 'legacy_unmanaged' else 'inconsistent',
                'broker': 'not_checked'}


def run_setup(paths, terminal, store, provider_factory, *, handoff, resume=False,
              timeout=120, close_timeout=10, bundled_loader=bundled_app_identity) -> SetupResult:
    """All secret inputs come from the verified native terminal, never CLI/env.

    One submission per provider state; uncertainty returns a resumable failure.
    Resume always opens a fresh client and asks TDLib for its current state.
    Before enrollment, resume uses the same genuinely-new-session admission as
    setup so interrupted/partial API provisioning can continue add-only.

    A named alias's run holds its connect.lock from here through the handoff,
    as the connect helper does (review fix 055d2): held by another login or by
    an installer transaction over the aliases, it refuses `owner_busy` before
    anything else; the default's keeps 0.54's behaviour.
    """
    if (type(paths) is not SetupPaths or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or not 0 < timeout <= 3600
            or type(close_timeout) not in (int, float) or not math.isfinite(close_timeout)
            or not 0 < close_timeout <= 60):
        return _result('invalid_input')
    try:
        terminal.require_secure()
    except (Exception, KeyboardInterrupt):
        return _result('secure_terminal_required')
    login = None
    if paths.account.alias != DEFAULT_ALIAS:
        try:
            login = acquire_owner_lock(paths.account.connect_lock)
        except OwnerLeaseError as error:
            return _result(error.code)
    try:
        return _run_setup(paths, terminal, store, provider_factory, handoff, timeout, close_timeout, bundled_loader)
    finally:
        if login is not None:
            os.close(login)


def _run_setup(paths, terminal, store, provider_factory, handoff, timeout, close_timeout, bundled_loader):
    deadline = time.monotonic() + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0: raise TimeoutError()
        return left
    transport, result = None, _result('dependency_unavailable')
    enrollment = EnrollmentStore(paths)
    try:
        with SessionOwnerLease(paths.lock) as lease:
            try:
                record = enrollment.read()
                if record is None:
                    enrollment.require_new()  # Before credential provisioning/native construction.
                # Validate existing policy without changing any byte.
                load_runtime_policy(paths.policy)
                terminal.require_secure()
                lease.check()
                if record is not None:
                    key = managed_key(record, store)  # Never regenerate a managed key.
                    credentials = resolve_enrolled(record, bundled_loader=bundled_loader,
                        keychain_loader=lambda: _api_credentials(terminal, store, remaining,
                            expected_fingerprint=record.get('identity_fingerprint')).credentials)
                    # A pre-0.52 record gains its binding with this write (spec §2).
                    enrollment.set_state('pending', bound=ResolvedIdentity(credentials, 'keychain')
                                         if 'credential_source' not in record else None)
                else:
                    identity = _api_credentials(terminal, store, remaining, bundled_loader)
                    credentials = identity.credentials
                    record, key = enrollment.create(store, identity)
                _ensure_policy(paths)
                for directory in (paths.account.database, paths.account.files):
                    fd = open_private_directory(directory, create=True)
                    os.close(fd)
                terminal.require_secure()
                lease.check()
                transport = AuthTransport(provider_factory())
                for _ in range(16):
                    state = transport.get_authorization_state(timeout=remaining())
                    if state is AuthorizationState.READY:
                        # The connect helper's login pin, before the teardown and handoff (0.55 spec 3C).
                        refused = account_registry.login_refusal(
                            paths.account, lambda: transport.get_account_id(timeout=remaining()))
                        result = SetupResult('authorized', None) if refused is None else _result(refused)
                        break
                    if state is AuthorizationState.PARAMETERS:
                        transport.set_parameters(credentials, key, database_directory=str(paths.account.database),
                            files_directory=str(paths.account.files), application_version=__version__,
                            timeout=remaining())
                    elif state is AuthorizationState.ENCRYPTION:
                        transport.check_database_key(key, timeout=remaining())
                    elif state in {AuthorizationState.PHONE, AuthorizationState.CODE, AuthorizationState.PASSWORD}:
                        kind, submit = {AuthorizationState.PHONE: ('phone', transport.set_phone_number),
                            AuthorizationState.CODE: ('code', transport.check_code),
                            AuthorizationState.PASSWORD: ('password', transport.check_password)}[state]
                        secret = terminal.read_secret(kind, timeout=remaining())
                        try:
                            submit(secret, timeout=remaining())
                        finally:
                            secret = None
                    else:
                        result = _result('unsupported_auth_state')
                        break
                    transport.wait_for_state_change(state, timeout=remaining())
                else:
                    result = _result('timeout')
            except EnrollmentError as error:
                result = _result(error.code)
            except CredentialStoreError as error:
                result = _result('credential_' + error.code)
            except (CredentialIdentityError, AppIdentityError) as error:
                result = _result(error.code)
            except (AuthTransportError, TerminalError) as error:
                result = _result(error.code, retry_after=getattr(error, 'retry_after', None))
            except ConfigurationError:
                result = _result('policy_unsafe')
            except (KeyboardInterrupt, EOFError):
                result = _result('cancelled')
            except TimeoutError:
                result = _result('timeout')
            except OwnerLeaseError as error:
                result = _result(error.code)
            except Exception:
                result = _result('dependency_unavailable')
            finally:
                if transport is not None:
                    teardown = transport.close(timeout=close_timeout)
                    if not teardown.native_destroyed:
                        lease.retain_until_process_exit(transport)
                        result = _result('auth_destroy_failed', closed=teardown.provider_closed, destroyed=False)
                    elif not teardown.provider_closed:
                        result = _result('auth_close_failed', destroyed=True)
                    else:
                        result = SetupResult(result.status, result.code, True, result.retry_after, True)
                lease.check()
    except OwnerLeaseError as error:
        return _result(error.code)
    except Exception:
        return _result('dependency_unavailable')
    if result.status == 'authorized' and result.auth_client_closed and result.auth_client_destroyed:
        try:
            boundary = handoff()
            if boundary != 'ready':
                return _result(boundary, closed=True)
            enrollment.set_state('ready')
            return SetupResult('ready', None, True, auth_client_destroyed=True)
        except (Exception, KeyboardInterrupt):
            return _result('broker_start_failed', closed=True)
    return result
