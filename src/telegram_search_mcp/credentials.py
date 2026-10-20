"""Lazy, add-only native Keychain access for local enrollment.

No import or construction touches Keychain. Missing differs from denied/locked;
no update, unlock, subprocess secret arguments or plaintext fallback exists.
"""
from __future__ import annotations
from contextlib import ExitStack
import ctypes
import platform
import re
import sys
from typing import Callable, NoReturn
from .config import KEYCHAIN_SERVICE
from .keychain import ApiCredentials
_CODES = frozenset({'denied', 'locked', 'unavailable', 'invalid_record', 'conflict'})
_MAX_BYTES = 4096

class CredentialStoreError(RuntimeError):
    """Closed safe error codes; no OS diagnostic, payload, or exception chain."""

    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _CODES else 'invalid_record'
        super().__init__('credential store: ' + self.code)


def _raise_safe(code: str) -> NoReturn:
    """Strip internal and ambient caller diagnostics before bare propagation.

    A new raise attaches the currently handled exception, even in the caller.
    Clearing after catching our safe error and then using bare raise preserves
    the cleared chain; `from None` alone would retain introspectable context.
    """
    try:
        raise CredentialStoreError(code)
    except CredentialStoreError as error:
        error.__context__ = None
        error.__cause__ = None
        raise


class SecretValue:
    """Bounded bytes with redacted display and explicit access at the boundary."""
    __slots__ = ('__value',)

    def __init__(self, value: bytes):
        if type(value) is not bytes or not 0 < len(value) <= _MAX_BYTES:
            _raise_safe('invalid_record')
        self.__value = value

    def reveal(self) -> bytes:
        return self.__value

    def __repr__(self) -> str:
        return 'SecretValue(<redacted>)'

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        _raise_safe('invalid_record')


def _validate_account(account):
    if type(account) is not str or (account not in {'api_id', 'api_hash'} and
            re.fullmatch(r'database_key_[0-9a-f]{32}', account) is None):
        _raise_safe('invalid_record')


def _validate_record(account: str, value: bytes) -> None:
    valid = False
    if type(value) is bytes and 0 < len(value) <= _MAX_BYTES:
        if account.startswith('database_key_'):
            valid = len(value) == 32
        elif account == 'api_hash':
            valid = len(value) == 32 and all(byte in b'0123456789abcdefABCDEF' for byte in value)
        elif account == 'api_id':
            # Bound before conversion, avoiding huge integers and raw conversion errors.
            valid = (len(value) <= 10 and all(byte in b'0123456789' for byte in value)
                     and 0 < int(value) <= 2147483647)
    if not valid:
        _raise_safe('invalid_record')


def _check_status(status: int) -> None:
    if status == 0:
        return
    code = {-128: 'denied', -25293: 'denied', -25308: 'locked',
            -25291: 'unavailable', -50: 'invalid_record', -26275: 'invalid_record'}.get(status, 'unavailable')
    _raise_safe(code)


class MacOSSecretStore:
    """Read the legacy user search list; add only to the default Keychain.

    Service/accounts retain the source's fixed namespace. The injected
    factory is the sole native boundary, allowing inert unit validation.
    Denied/locked operations never unlock, retry, or use a plaintext fallback.
    """

    def __init__(self, service: str = KEYCHAIN_SERVICE, *, backend_factory: Callable | None = None):
        if type(service) is not str or not service or len(service.encode('utf-8')) > 1024:
            _raise_safe('invalid_record')
        self._service = service
        self._backend_factory = _SecurityBackend if backend_factory is None else backend_factory
        self._backend = None

    def get(self, account: str) -> SecretValue | None:
        _validate_account(account)
        error = None
        try:
            return self._get(account)
        except CredentialStoreError as failure:
            error = failure.code
        except Exception:
            error = 'unavailable'
        _raise_safe(error)

    def put(self, account: str, value: SecretValue) -> None:
        _validate_account(account)
        if type(value) is not SecretValue:
            _raise_safe('invalid_record')
        _validate_record(account, value.reveal())
        error = None
        try:
            self._put(account, value.reveal())
            return
        except CredentialStoreError as failure:
            error = failure.code
        except Exception:
            error = 'unavailable'
        _raise_safe(error)

    def _native(self):
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend

    def _query(self, native, owned, account, *, insert=False):
        def retain(pointer):
            if not pointer:
                _raise_safe('unavailable')
            owned.callback(native.release, pointer)
            return pointer

        status, keychain = native.default_keychain() if insert else native.search_list()
        if keychain:
            retain(keychain)
        _check_status(status)
        if not keychain:
            _raise_safe('invalid_record')
        pairs = [(native.constant('kSecClass'), native.constant('kSecClassGenericPassword')),
                 (native.constant('kSecAttrService'), retain(native.string(self._service))),
                 (native.constant('kSecAttrAccount'), retain(native.string(account)))]
        if insert:
            pairs.append((native.constant('kSecUseKeychain'), keychain))
        else:
            pairs.append((native.constant('kSecMatchSearchList'), keychain))
        return pairs, retain, keychain

    def _get(self, account):
        """Count matches by attributes, then read the single item's data.

        File-based macOS keychains refuse kSecReturnData with kSecMatchLimitAll
        (errSecParam, -50), so duplicates are detected without returning data.
        """
        native = self._native()
        with ExitStack() as owned:
            pairs, retain, _ = self._query(native, owned, account)
            count = retain(native.dictionary(pairs + [
                (native.constant('kSecMatchLimit'), native.constant('kSecMatchLimitAll')),
                (native.constant('kSecReturnAttributes'), native.constant('kCFBooleanTrue'))]))
            status, result = native.copy_matching(count)
            if result:
                retain(result)
            if status == -25300:
                return None
            _check_status(status)
            if not result:
                _raise_safe('invalid_record')
            if len(native.array_values(result, 16)) != 1:
                _raise_safe('conflict')
            read = retain(native.dictionary(pairs + [
                (native.constant('kSecMatchLimit'), native.constant('kSecMatchLimitOne')),
                (native.constant('kSecReturnData'), native.constant('kCFBooleanTrue'))]))
            status, result = native.copy_matching(read)
            if result:
                retain(result)
            if status == -25300:
                _raise_safe('unavailable')  # Removed between the two queries; never read as absent.
            _check_status(status)
            if not result:
                _raise_safe('invalid_record')
            value = native.data_bytes(result, _MAX_BYTES)
            _validate_record(account, value)
            return SecretValue(value)

    def _put(self, account, value):
        if self._get(account) is not None:
            _raise_safe('conflict')
        native = self._native()
        with ExitStack() as owned:
            pairs, retain, keychain = self._query(native, owned, account, insert=True)
            data = retain(native.data(value))
            pairs.append((native.constant('kSecValueData'), data))
            status = native.add(retain(native.dictionary(pairs)))
            if status == -25299:
                _raise_safe('conflict')
            _check_status(status)


class _SecurityBackend:
    """ctypes binding; construction occurs only inside an explicit operation.

    CFIndex is signed long, CFTypeID unsigned long, OSStatus signed int32.
    All Create/Copy references are owned by the operation's ExitStack; borrowed
    exported constants remain framework-owned. CF collection callbacks retain
    their elements; collections are released before the source references.
    """

    def __init__(self):
        if sys.platform != 'darwin' or platform.machine() not in {'arm64', 'aarch64'}:
            _raise_safe('unavailable')
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        pointer, index, status = ctypes.c_void_p, ctypes.c_long, ctypes.c_int32
        pointer_ref = ctypes.POINTER(pointer)
        self._bind(self.security, 'SecKeychainCopyDefault', [pointer_ref], status)
        self._bind(self.security, 'SecKeychainCopySearchList', [pointer_ref], status)
        self._bind(self.security, 'SecItemCopyMatching', [pointer, pointer_ref], status)
        self._bind(self.security, 'SecItemAdd', [pointer, pointer_ref], status)
        self._bind(self.cf, 'CFStringCreateWithCString', [pointer, ctypes.c_char_p, ctypes.c_uint32], pointer)
        self._bind(self.cf, 'CFDataCreate', [pointer, pointer, index], pointer)
        self._bind(self.cf, 'CFArrayCreate', [pointer, pointer_ref, index, pointer], pointer)
        self._bind(self.cf, 'CFDictionaryCreate', [pointer, pointer_ref, pointer_ref, index, pointer, pointer], pointer)
        self._bind(self.cf, 'CFGetTypeID', [pointer], ctypes.c_ulong)
        self._bind(self.cf, 'CFDataGetTypeID', [], ctypes.c_ulong)
        self._bind(self.cf, 'CFArrayGetTypeID', [], ctypes.c_ulong)
        self._bind(self.cf, 'CFArrayGetCount', [pointer], index)
        self._bind(self.cf, 'CFArrayGetValueAtIndex', [pointer, index], pointer)
        self._bind(self.cf, 'CFDataGetLength', [pointer], index)
        self._bind(self.cf, 'CFDataGetBytePtr', [pointer], pointer)
        self._bind(self.cf, 'CFRelease', [pointer], None)
        # Address the exported callback structs directly (no guessed struct ABI).
        self._array_callbacks = ctypes.c_byte.in_dll(self.cf, 'kCFTypeArrayCallBacks')
        self._key_callbacks = ctypes.c_byte.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks')
        self._value_callbacks = ctypes.c_byte.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks')

    @staticmethod
    def _bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    def constant(self, name):
        library = self.cf if name == 'kCFBooleanTrue' else self.security
        return ctypes.c_void_p.in_dll(library, name).value

    def string(self, value):
        return self.cf.CFStringCreateWithCString(None, value.encode('utf-8'), 0x08000100)

    def data(self, value):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return self.cf.CFDataCreate(None, buffer, len(value))

    def array(self, values):
        buffer = (ctypes.c_void_p * len(values))(*values)
        return self.cf.CFArrayCreate(None, buffer, len(values), ctypes.byref(self._array_callbacks))

    def dictionary(self, pairs):
        keys = (ctypes.c_void_p * len(pairs))(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * len(pairs))(*(value for _, value in pairs))
        return self.cf.CFDictionaryCreate(None, keys, values, len(pairs),
                                         ctypes.byref(self._key_callbacks), ctypes.byref(self._value_callbacks))

    def default_keychain(self):
        result = ctypes.c_void_p()
        status = self.security.SecKeychainCopyDefault(ctypes.byref(result))
        return status, result.value

    def search_list(self):
        result = ctypes.c_void_p()
        status = self.security.SecKeychainCopySearchList(ctypes.byref(result))
        return status, result.value

    def copy_matching(self, query):
        result = ctypes.c_void_p()
        status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
        return status, result.value

    def add(self, query):
        return self.security.SecItemAdd(query, None)

    def array_values(self, pointer, maximum):
        if self.cf.CFGetTypeID(pointer) != self.cf.CFArrayGetTypeID():
            _raise_safe('invalid_record')
        count = self.cf.CFArrayGetCount(pointer)
        if not 0 < count <= maximum:
            _raise_safe('invalid_record')
        values = tuple(self.cf.CFArrayGetValueAtIndex(pointer, index) for index in range(count))
        if any(not value for value in values):
            _raise_safe('invalid_record')
        return values

    def data_bytes(self, pointer, max_bytes):
        if self.cf.CFGetTypeID(pointer) != self.cf.CFDataGetTypeID():
            _raise_safe('invalid_record')
        length = self.cf.CFDataGetLength(pointer)
        if not 0 < length <= max_bytes:
            _raise_safe('invalid_record')
        data = self.cf.CFDataGetBytePtr(pointer)
        if not data:
            _raise_safe('invalid_record')
        return ctypes.string_at(data, length)

    def release(self, pointer):
        self.cf.CFRelease(pointer)
