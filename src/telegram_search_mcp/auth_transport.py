"""Private typed transport for the pinned d1085f9 TDLib auth schema.

One raw client, correlated requests, bounded receives and discarded provider
metadata. It has no arbitrary dispatch, no broker/MCP interface, no bot flow,
and never expands the search client's authorization allowlist.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import math
import re
import time
from typing import NoReturn, Protocol

from . import product_identity
from .credentials import ApiCredentials, SecretValue
from .sanitize import sanitize_telegram_text


class AuthorizationState(Enum):
    PARAMETERS = 'authorizationStateWaitTdlibParameters'
    ENCRYPTION = 'authorizationStateWaitEncryptionKey'
    PHONE = 'authorizationStateWaitPhoneNumber'
    OTHER_DEVICE = 'authorizationStateWaitOtherDeviceConfirmation'
    CODE = 'authorizationStateWaitCode'
    PASSWORD = 'authorizationStateWaitPassword'
    READY = 'authorizationStateReady'
    CLOSING = 'authorizationStateClosing'
    CLOSED = 'authorizationStateClosed'
    UNSUPPORTED = 'unsupported_auth_state'


_ERRORS = frozenset(('invalid_code', 'code_expired', 'invalid_password', 'flood_wait',
                     'dependency_unavailable', 'auth_request_failed', 'timeout', 'invalid_input'))


class AuthTransportError(RuntimeError):
    def __init__(self, code='auth_request_failed', retry_after=None):
        self.code = code if type(code) is str and code in _ERRORS else 'auth_request_failed'
        self.retry_after = min(86400, max(1, retry_after)) if self.code == 'flood_wait' and type(retry_after) is int else None
        super().__init__(self.code)


def _fail(code: str, retry_after=None) -> NoReturn:
    try:
        raise AuthTransportError(code, retry_after)
    except AuthTransportError as failure:
        failure.__context__ = failure.__cause__ = None
        raise


class RawAuthClient(Protocol):
    def send(self, payload: dict) -> None: ...
    def receive(self, timeout: float) -> dict | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class AuthTeardown:
    provider_closed: bool
    native_destroyed: bool


@dataclass(frozen=True, repr=False)
class AuthObservation:
    """One provider state. The QR link is a secret: it never enters repr or logs."""
    state: AuthorizationState
    qr_link: SecretValue | None = None
    password_hint: str | None = None

    def __repr__(self):
        return f'AuthObservation({self.state.name})'

    def same_as(self, other) -> bool:
        def link(value):
            return None if value.qr_link is None else value.qr_link.reveal()
        return (type(other) is AuthObservation and self.state is other.state
                and link(self) == link(other) and self.password_hint == other.password_hint)


# Pinned TDLib renders tg://login?token=<base64url>; anything else fails closed.
_QR_LINK = re.compile(r'tg://login\?token=[A-Za-z0-9_-]{16,200}={0,2}')


def _state(value: object) -> AuthorizationState:
    kind = value.get('@type') if type(value) is dict else None
    if type(kind) is str:
        for state in AuthorizationState:
            if kind == state.value:
                return state
    return AuthorizationState.UNSUPPORTED


def _observe(value: object) -> AuthObservation:
    state = _state(value)
    if state is AuthorizationState.OTHER_DEVICE:
        link = value.get('link')
        if type(link) is not str or len(link) > 256 or _QR_LINK.fullmatch(link) is None:
            return AuthObservation(AuthorizationState.UNSUPPORTED)
        return AuthObservation(state, qr_link=SecretValue(link.encode('ascii')))
    if state is AuthorizationState.PASSWORD:
        hint = value.get('password_hint')
        return AuthObservation(state, password_hint=sanitize_telegram_text(
            hint if type(hint) is str and len(hint) <= 4096 else '', max_length=128))
    return AuthObservation(state)


def _provider_error(value: dict) -> tuple[str, int | None]:
    message = value.get('message')
    if type(message) is str and len(message) <= 256:
        mapped = {'PHONE_CODE_INVALID': 'invalid_code', 'PHONE_CODE_EXPIRED': 'code_expired',
                  'PASSWORD_HASH_INVALID': 'invalid_password'}
        if message in mapped:
            return mapped[message], None
        match = re.fullmatch(r'(?:FLOOD_WAIT_|Too Many Requests: retry after )(\d{1,9})', message)
        if match:
            return 'flood_wait', min(86400, max(1, int(match.group(1))))
    if type(value.get('code')) is int and value['code'] >= 500:
        return 'dependency_unavailable', None
    if value.get('code') == 429:
        return 'flood_wait', 60
    return 'auth_request_failed', None


def _text(secret: SecretValue, *, max_bytes=4096) -> str:
    if type(secret) is not SecretValue or len(secret.reveal()) > max_bytes:
        _fail('invalid_input')
    failed = False
    try:
        value = secret.reveal().decode('utf-8')
    except UnicodeError:
        failed = True
    if failed:
        _fail('invalid_input')
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail('invalid_input')
    return value


class AuthTransport:
    def __init__(self, raw: RawAuthClient, *, _clock=time.monotonic):
        self._raw, self._clock = raw, _clock
        self._counter = 0
        self._last_state = None
        self._last_observation = None
        self._destroyed = False
        self._teardown = None

    def __repr__(self):
        return 'AuthTransport(<private>)'

    def _deadline(self, timeout):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600
                or self._destroyed or self._teardown is not None):
            _fail('timeout')
        return self._clock() + timeout

    def _receive(self, deadline):
        remaining = deadline - self._clock()
        if remaining <= 0:
            _fail('timeout')
        failed = False
        try:
            result = self._raw.receive(min(.25, remaining))
        except Exception:
            failed = True
        if failed:
            _fail('dependency_unavailable')
        if self._clock() >= deadline:
            _fail('timeout')
        if result is not None and type(result) is not dict:
            _fail('auth_request_failed')
        if result is not None and result.get('@type') == 'updateAuthorizationState':
            self._remember(_observe(result.get('authorization_state')))
        return result

    def _remember(self, observation):
        self._last_observation, self._last_state = observation, observation.state

    def _request(self, kind, fields, timeout):
        deadline = self._deadline(timeout)
        self._counter += 1
        correlation = f'auth-{self._counter}'
        failed = False
        try:
            self._raw.send({'@type': kind, **fields, '@extra': correlation})
        except Exception:
            failed = True
        if failed:
            _fail('dependency_unavailable')
        # Bounds also protect tests/adapters that return instantly without waiting.
        for _ in range(100000):
            value = self._receive(deadline)
            if value is None or value.get('@extra') != correlation:
                continue
            if value.get('@type') == 'error':
                code, retry_after = _provider_error(value)
                _fail(code, retry_after)
            return value
        _fail('timeout')

    def _ok(self, kind, fields, timeout):
        if self._request(kind, fields, timeout).get('@type') != 'ok':
            _fail('auth_request_failed')

    def get_authorization_state(self, *, timeout=10) -> AuthorizationState:
        value = self._request('getAuthorizationState', {}, timeout)
        self._remember(_observe(value))
        return self._last_state

    def observation(self) -> AuthObservation | None:
        return self._last_observation

    def set_parameters(self, credentials: ApiCredentials, key: SecretValue, *, database_directory: str,
                       files_directory: str, application_version: str, timeout=10) -> None:
        if type(credentials) is not ApiCredentials or type(key) is not SecretValue or len(key.reveal()) != 32:
            _fail('invalid_input')
        self._ok('setTdlibParameters', dict(use_test_dc=False, database_directory=database_directory,
            files_directory=files_directory, database_encryption_key=base64.b64encode(key.reveal()).decode('ascii'),
            use_file_database=False, use_chat_info_database=True, use_message_database=True, use_secret_chats=False,
            api_id=credentials.api_id, api_hash=credentials.api_hash, system_language_code='en',
            device_model=product_identity.DEVICE_MODEL, system_version='macOS', application_version=application_version), timeout)

    def check_database_key(self, key: SecretValue, *, timeout=10) -> None:
        if type(key) is not SecretValue or len(key.reveal()) != 32:
            _fail('invalid_input')
        self._ok('checkDatabaseEncryptionKey',
                 {'encryption_key': base64.b64encode(key.reveal()).decode('ascii')}, timeout)

    def set_phone_number(self, phone: SecretValue, *, timeout=10) -> None:
        value = _text(phone, max_bytes=32)
        if re.fullmatch(r'\+[0-9]{5,30}', value) is None:
            _fail('invalid_input')
        self._ok('setAuthenticationPhoneNumber', {'phone_number': value,
            'settings': {'@type': 'phoneNumberAuthenticationSettings', 'allow_flash_call': False,
                'allow_missed_call': False, 'is_current_phone_number': False, 'has_unknown_phone_number': False,
                'allow_sms_retriever_api': False, 'firebase_authentication_settings': None,
                'authentication_tokens': []}}, timeout)

    def request_qr_code(self, *, timeout=10) -> None:
        self._ok('requestQrCodeAuthentication', {'other_user_ids': []}, timeout)

    def check_code(self, code: SecretValue, *, timeout=10) -> None:
        self._ok('checkAuthenticationCode', {'code': _text(code, max_bytes=128)}, timeout)

    def check_password(self, password: SecretValue, *, timeout=10) -> None:
        self._ok('checkAuthenticationPassword', {'password': _text(password)}, timeout)

    def wait_for_state_change(self, previous: AuthorizationState, *, timeout=10) -> AuthorizationState:
        deadline = self._deadline(timeout)
        for _ in range(100000):
            if self._last_state is not None and self._last_state is not previous:
                return self._last_state
            self._receive(deadline)
        _fail('timeout')

    def wait_for_change(self, previous: AuthObservation, *, timeout=10) -> AuthObservation:
        """Like wait_for_state_change, but a replaced QR link also counts."""
        deadline = self._deadline(timeout)
        for _ in range(100000):
            current = self._last_observation
            if current is not None and not current.same_as(previous):
                return current
            self._receive(deadline)
        _fail('timeout')

    def close(self, *, timeout=10) -> AuthTeardown:
        """Keep provider Closed distinct from successful native destruction.

        Never retry uncertain destruction: the owning setup process must retain
        its lease and raw owner until process exit if destroy does not return.
        """
        if self._teardown is not None:
            return self._teardown
        closed = False
        try:
            deadline = self._deadline(timeout)
            self._ok('close', {}, max(.000001, deadline - self._clock()))
            for _ in range(100000):
                if self._last_state is AuthorizationState.CLOSED:
                    closed = True
                    break
                self._receive(deadline)
        except BaseException:
            closed = False
        finally:
            try:
                self._raw.close()
            except BaseException:
                self._destroyed = False
            else:
                self._destroyed = True
            self._teardown = AuthTeardown(closed, self._destroyed)
        return self._teardown
