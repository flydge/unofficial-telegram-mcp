"""Behavioral safety contract; all providers and secrets are synthetic."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import select
import subprocess
import sys
import unittest
from unittest import mock

from telegram_search_mcp.credentials import SecretValue, MacOSSecretStore, CredentialStoreError
from telegram_search_mcp.enrollment import SetupPaths, EnrollmentStore, EnrollmentError, load_database_key
from telegram_search_mcp.setup_wizard import run_setup, setup_status
from telegram_search_mcp.secure_terminal import NativeTerminal, TerminalError, TerminalInspection, ProcessIdentity, NATIVE_TERMINAL
from telegram_search_mcp.session_owner import SessionOwnerLease, OwnerLeaseError
from telegram_search_mcp.auth_transport import AuthTransport, AuthorizationState, AuthTransportError
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.tdjson import TDLibClient, ForbiddenTDLibRequest


class MemoryStore:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.gets, self.puts = [], []
        self.fail_account = None
    def get(self, account):
        self.gets.append(account)
        return self.values.get(account)
    def put(self, account, value):
        self.puts.append(account)
        if account == self.fail_account:
            raise CredentialStoreError('denied')
        if account in self.values:
            raise CredentialStoreError('conflict')
        self.values[account] = value


class Terminal:
    def __init__(self, values=None, refused=False):
        self.values = dict(values or {'api_id': b'123456', 'api_hash': b'a' * 32,
                                     'phone': b'+1234567890', 'code': b'12345', 'password': b'pass'})
        self.prompts, self.refused = [], refused
    def require_secure(self):
        if self.refused:
            raise TerminalError()
    def read_secret(self, prompt_id, *, timeout):
        self.require_secure()
        self.prompts.append(prompt_id)
        value = self.values[prompt_id]
        if isinstance(value, BaseException):
            raise value
        return SecretValue(value)
    def notice(self, kind):
        pass


class AuthRaw:
    def __init__(self, states=None, *, events=None, paths=None):
        self.states = list(states or ['authorizationStateWaitTdlibParameters',
            'authorizationStateWaitPhoneNumber', 'authorizationStateWaitCode',
            'authorizationStateWaitPassword', 'authorizationStateReady'])
        self.index, self.queue, self.sent = 0, [], []
        self.events, self.paths = events if events is not None else [], paths
        self.failure_kind, self.failure_message, self.closed = None, None, False
        self.close_error = False
    def send(self, payload):
        self.sent.append(dict(payload))
        kind = payload['@type']
        if kind == 'setTdlibParameters' and self.paths:
            self.assert_pending = EnrollmentStore(self.paths).read()['state'] == 'pending'
        if kind == self.failure_kind:
            if self.failure_message == 'raise':
                raise RuntimeError('SYNTHETIC_SECRET_SENTINEL')
            self.queue.append({'@type': 'error', '@extra': payload['@extra'],
                               'code': 400, 'message': self.failure_message})
            return
        if kind == 'getAuthorizationState':
            self.queue.append({'@type': self.states[self.index], '@extra': payload['@extra']})
        elif kind == 'close':
            if self.close_error:
                self.queue.append({'@type': 'error', '@extra': payload['@extra'], 'code': 400})
            else:
                self.queue.extend([{'@type': 'ok', '@extra': payload['@extra']},
                    {'@type': 'updateAuthorizationState', 'authorization_state': {'@type': 'authorizationStateClosed'}}])
            self.events.append('close')
        else:
            self.index = min(len(self.states) - 1, self.index + 1)
            self.queue.extend([{'@type': 'ok', '@extra': payload['@extra']},
                {'@type': 'updateAuthorizationState', 'authorization_state': {'@type': self.states[self.index]}}])
    def receive(self, timeout):
        return self.queue.pop(0) if self.queue else None
    def close(self):
        if self.paths:
            try:
                with SessionOwnerLease(self.paths.lock):
                    raise AssertionError('destroy occurred without the owner lease')
            except OwnerLeaseError as error:
                assert error.code == 'owner_busy'
        self.events.append('destroy')
        self.closed = True


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'app'
        self.paths = SetupPaths(self.root)
        self.store, self.terminal, self.events = MemoryStore(), Terminal(), []
        self.raws = []
    def tearDown(self):
        self.temp.cleanup()
    def factory(self, states=None):
        raw = AuthRaw(states, events=self.events, paths=self.paths)
        self.raws.append(raw)
        self.events.append('create')
        return raw
    def handoff(self):
        self.assertEqual(self.events[-1], 'destroy')
        with SessionOwnerLease(self.paths.lock):
            self.events.append('handoff')
        return 'ready'
    def setup(self, **kwargs):
        return run_setup(self.paths, self.terminal, self.store, kwargs.pop('provider_factory', self.factory),
                         handoff=kwargs.pop('handoff', self.handoff), **kwargs)
    def enrolled(self):
        result = self.setup()
        self.assertEqual(result.status, 'ready')
        return EnrollmentStore(self.paths).read()

    def test_refused_terminal_has_no_files_credentials_native_or_service_effects(self):
        self.terminal.refused = True
        self.assertEqual(self.setup().code, 'secure_terminal_required')
        self.assertFalse(self.root.exists())
        self.assertEqual(self.store.gets + self.store.puts + self.events, [])

    def test_supported_auth_is_encrypted_pending_then_closed_destroyed_released_before_handoff(self):
        self.enrolled()
        raw = self.raws[0]
        self.assertTrue(raw.assert_pending)
        parameters = next(row for row in raw.sent if row['@type'] == 'setTdlibParameters')
        key = base64.b64decode(parameters['database_encryption_key'], validate=True)
        self.assertEqual(len(key), 32)
        self.assertEqual(self.events, ['create', 'close', 'destroy', 'handoff'])
        self.assertEqual(self.terminal.prompts, ['api_id', 'api_hash', 'phone', 'code', 'password'])
        self.assertEqual(self.paths.policy.stat().st_mode & 0o777, 0o600)
        self.assertIn(b'enabled_capabilities = ["read", "artifacts"]', self.paths.policy.read_bytes())
        self.assertNotIn(b'pass', self.paths.record.read_bytes())

    def test_existing_legacy_session_and_policy_and_credentials_stay_identical(self):
        self.paths.session.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        legacy = self.paths.session / 'legacy'
        legacy.write_bytes(b'untouched')
        self.paths.policy.write_bytes(b'existing-policy')
        self.store.values = {'api_id': SecretValue(b'456'), 'api_hash': SecretValue(b'b' * 32)}
        self.assertEqual(self.setup().code, 'legacy_unmanaged')
        self.assertEqual(legacy.read_bytes(), b'untouched')
        self.assertEqual(self.paths.policy.read_bytes(), b'existing-policy')
        self.assertEqual(self.store.gets + self.store.puts + self.events, [])
        self.assertEqual(self.setup(resume=True).code, 'legacy_unmanaged')
        self.assertEqual(self.store.gets + self.store.puts + self.events, [])

    def test_owner_contention_never_touches_socket_or_starts_native(self):
        with SessionOwnerLease(self.paths.lock):
            socket = self.paths.lock.parent / 'broker.sock'
            socket.write_bytes(b'foreign-owner')
            self.assertEqual(self.setup().code, 'owner_busy')
            self.assertEqual(socket.read_bytes(), b'foreign-owner')
        self.assertEqual(self.store.gets + self.store.puts + self.events, [])

    def test_complete_pair_and_existing_policy_are_reused_without_overwrite(self):
        self.root.mkdir(mode=0o700)
        original = b'config_version = 1\nenabled_capabilities = ["read"]\n'
        self.paths.policy.write_bytes(original)
        self.paths.policy.chmod(0o600)
        self.store.values = {'api_id': SecretValue(b'456'), 'api_hash': SecretValue(b'b' * 32)}
        self.enrolled()
        self.assertNotIn('api_id', self.terminal.prompts)
        self.assertNotIn('api_hash', self.terminal.prompts)
        self.assertEqual(self.paths.policy.read_bytes(), original)
        self.assertEqual(self.store.values['api_id'].reveal(), b'456')
        self.assertEqual(len(self.store.puts), 1)

    def test_partial_pair_write_failure_can_resume_missing_record_without_overwrite(self):
        self.store.fail_account = 'api_hash'
        self.assertEqual(self.setup().code, 'credential_denied')
        self.assertIn('api_id', self.store.values)
        self.assertFalse(self.paths.record.exists())
        self.assertEqual(self.events, [])
        self.store.fail_account = None
        self.terminal.prompts.clear()
        self.assertEqual(self.setup(resume=True).status, 'ready')
        self.assertNotIn('api_id', self.terminal.prompts)
        self.assertEqual(self.store.puts.count('api_id'), 1)

    def test_early_credential_cancellation_recovers_through_resume_without_adoption(self):
        self.terminal.values['api_hash'] = KeyboardInterrupt()
        first = self.setup()
        self.assertEqual(first.code, 'cancelled')
        self.assertEqual(first.to_dict()['next_action'], 'resume_in_foreground_native_terminal')
        self.assertFalse(self.paths.record.exists())
        self.assertFalse(self.paths.session.exists())
        self.assertEqual(self.store.puts + self.events, [])
        self.terminal.values['api_hash'] = b'a' * 32
        self.assertEqual(self.setup(resume=True).status, 'ready')
        self.assertEqual(self.store.puts.count('api_id'), 1)
        self.assertEqual(self.store.puts.count('api_hash'), 1)

    def test_existing_invalid_credential_refuses_before_prompts_or_database_calls(self):
        self.store.values['api_id'] = SecretValue(b'bad-SYNTHETIC_SECRET_SENTINEL')
        self.assertEqual(self.setup().code, 'credential_invalid_record')
        self.assertEqual(self.terminal.prompts + self.events, [])
        self.assertEqual(self.store.puts, [])

    def test_cancel_preserves_pending_key_and_resume_queries_fresh_state(self):
        self.terminal.values['code'] = KeyboardInterrupt()
        result = self.setup()
        self.assertEqual(result.code, 'cancelled')
        pending = EnrollmentStore(self.paths).read()
        key = self.store.values[pending['key_account']].reveal()
        self.assertEqual(pending['state'], 'pending')
        self.assertTrue(self.raws[0].closed)
        self.terminal.values['code'] = b'12345'
        self.assertEqual(self.setup(provider_factory=lambda: self.factory(['authorizationStateWaitCode', 'authorizationStateReady']), resume=True).status, 'ready')
        self.assertEqual(self.raws[1].sent[0]['@type'], 'getAuthorizationState')
        self.assertEqual(self.store.values[pending['key_account']].reveal(), key)

    def test_saved_ready_never_bypasses_provider_authorization(self):
        self.enrolled()
        self.assertEqual(self.setup(provider_factory=lambda: self.factory(['authorizationStateWaitEmailAddress']), resume=True).code,
                         'unsupported_auth_state')
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')

    def test_resumed_encryption_state_uses_same_base64_key(self):
        record = self.enrolled()
        self.assertEqual(self.setup(provider_factory=lambda: self.factory(['authorizationStateWaitEncryptionKey', 'authorizationStateReady']), resume=True).status, 'ready')
        request = next(row for row in self.raws[-1].sent if row['@type'] == 'checkDatabaseEncryptionKey')
        self.assertEqual(base64.b64decode(request['encryption_key'], validate=True), self.store.values[record['key_account']].reveal())

    def test_corruption_and_rebinding_refuse_without_native_or_key_regeneration(self):
        record = self.enrolled()
        for field, value in [('uid', os.getuid() + 1), ('session', str(self.root / 'other')),
                             ('key_account', 'api_hash'), ('session_inode', record['session_inode'] + 1)]:
            with self.subTest(field=field):
                altered = dict(record, **{field: value})
                self.paths.record.write_text(json.dumps(altered))
                self.events.clear()
                writes = list(self.store.puts)
                self.assertEqual(self.setup(resume=True).code, 'enrollment_inconsistent')
                self.assertEqual(self.events, [])
                self.assertEqual(self.store.puts, writes)
        self.paths.record.write_bytes(b'broken')
        self.assertEqual(setup_status(self.paths)['state'], 'inconsistent')

    def test_missing_and_malformed_managed_key_never_fall_back_to_empty(self):
        record = self.enrolled()
        for key in [None, SecretValue(b'short')]:
            if key is None:
                self.store.values.pop(record['key_account'], None)
            else:
                self.store.values[record['key_account']] = key
            self.events.clear()
            writes = list(self.store.puts)
            self.assertEqual(self.setup(resume=True).code, 'managed_key_missing' if key is None else 'credential_invalid_record')
            self.assertEqual(self.events, [])
            self.assertEqual(self.store.puts, writes)

    def test_record_symlink_and_directory_substitution_are_refused(self):
        self.enrolled()
        self.paths.session.rename(self.root / 'old-session')
        self.paths.session.mkdir(mode=0o700)
        self.assertEqual(self.setup(resume=True).code, 'enrollment_inconsistent')
        self.paths.record.rename(self.root / 'old-record')
        self.paths.record.symlink_to(self.root / 'old-record')
        self.assertEqual(self.setup(resume=True).code, 'enrollment_inconsistent')

    def test_auth_error_and_uncertain_submission_never_replay_or_leak(self):
        for message, code in [('PHONE_CODE_INVALID', 'invalid_code'), ('raise', 'dependency_unavailable')]:
            with self.subTest(message=message):
                def factory():
                    raw = self.factory(['authorizationStateWaitCode', 'authorizationStateReady'])
                    raw.failure_kind, raw.failure_message = 'checkAuthenticationCode', message
                    return raw
                result = self.setup(provider_factory=factory)
                self.assertEqual(result.code, code)
                self.assertEqual(sum(row['@type'] == 'checkAuthenticationCode' for row in self.raws[-1].sent), 1)
                self.assertNotIn('SYNTHETIC_SECRET_SENTINEL', repr(result))
                self.assertTrue(self.raws[-1].closed)
                self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')

    def test_unsupported_states_stop_and_remain_resumable(self):
        for state in ['authorizationStateWaitOtherDeviceConfirmation', 'authorizationStateWaitRegistration',
                      'authorizationStateWaitEmailAddress', 'authorizationStateWaitEmailCode', 'authorizationStateWaitPremiumPurchase']:
            with self.subTest(state=state):
                self.assertEqual(self.setup(provider_factory=lambda: self.factory([state])).code, 'unsupported_auth_state')
                self.assertTrue(self.raws[-1].closed)

    def test_failed_close_destroys_under_lease_and_never_starts_broker(self):
        def factory():
            raw = self.factory(['authorizationStateReady'])
            raw.close_error = True
            return raw
        self.assertEqual(self.setup(provider_factory=factory).code, 'auth_close_failed')
        self.assertEqual(self.events, ['create', 'close', 'destroy'])
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')

    def test_uncertain_destroy_retains_ownership_until_owning_process_exits(self):
        fixture = Path(__file__).parent / 'fixtures' / 'setup_destroy_owner.py'
        for mode in ['exception', 'interrupt', 'system_exit']:
            for provider_state in ['closed', 'not_closed']:
                with self.subTest(mode=mode, provider_state=provider_state), tempfile.TemporaryDirectory(prefix='b1a-destroy-') as tmp:
                    paths = SetupPaths(Path(tmp).resolve() / 'app')
                    receipt = None
                    process = subprocess.Popen([sys.executable, str(fixture), str(paths.root), mode, provider_state],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    try:
                        self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'synthetic owner did not report')
                        receipt = json.loads(process.stdout.readline())
                        self.assertIsNone(process.poll())
                        with self.assertRaises(OwnerLeaseError) as busy:
                            with SessionOwnerLease(paths.lock):
                                pass
                        self.assertEqual(busy.exception.code, 'owner_busy')
                        self.assertEqual(receipt['first']['code'], 'auth_destroy_failed')
                        self.assertEqual(receipt['first']['auth_client_closed'], provider_state == 'closed')
                        self.assertFalse(receipt['first']['auth_client_destroyed'])
                        self.assertEqual(receipt['first']['next_action'], 'exit_setup_process_then_resume_in_foreground_native_terminal')
                        self.assertEqual(receipt['again']['code'], 'owner_busy')
                        self.assertEqual(receipt['destroy_calls'], 1)
                        self.assertEqual(receipt['provider_constructions'], 1)
                        self.assertEqual(receipt['handoff_calls'], 0)
                        self.assertTrue(receipt['native_alive'])
                    finally:
                        if process.poll() is None:
                            process.stdin.write('exit\n')
                            process.stdin.flush()
                        output, errors = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertEqual(output + errors, '')
                    with SessionOwnerLease(paths.lock):
                        pass
                    evidence = os.environ.get('TG_BUILD_TEST_DESTROY_EVIDENCE')
                    if evidence:
                        with Path(evidence).open('a', encoding='utf-8') as stream:
                            stream.write(json.dumps({'lock': str(paths.lock), 'owner_pid': process.pid,
                                'mode': mode, 'provider_state': provider_state, 'receipt': receipt,
                                'returncode': process.returncode, 'contended_while_owner_alive': True,
                                'acquired_after_owner_exit': True}) + '\n')

    def test_launch_and_readiness_failures_never_claim_ready(self):
        for result in ['broker_start_failed', 'broker_incompatible', 'broker_readiness_timeout', 'owner_busy']:
            with self.subTest(result=result):
                self.assertEqual(self.setup(handoff=lambda: result).code, result)
                self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')

    def test_absent_status_is_inert_and_resume_applies_the_same_new_session_admission(self):
        self.assertEqual(setup_status(self.paths), {'state': 'new', 'broker': 'not_checked'})
        self.assertEqual(self.store.gets + self.store.puts + self.events, [])
        self.assertFalse(self.paths.session.exists())
        self.assertEqual(self.setup(resume=True).status, 'ready')

    def test_production_broker_managed_key_is_bound_and_legacy_key_stays_empty(self):
        self.assertEqual(load_database_key(self.paths, self.store), b'')
        record = self.enrolled()
        self.assertEqual(load_database_key(self.paths, self.store), self.store.values[record['key_account']].reveal())
        self.paths.record.write_bytes(b'corrupt')
        with self.assertRaises(EnrollmentError):
            load_database_key(self.paths, self.store)
    def test_missing_managed_record_never_downgrades_to_legacy_empty_key(self):
        self.enrolled()
        self.paths.record.unlink()
        self.events.clear()
        with self.assertRaises(EnrollmentError) as caught:
            load_database_key(self.paths, self.store)
        self.assertEqual(caught.exception.code, 'enrollment_missing')
        self.assertEqual(self.setup(resume=True).code, 'enrollment_missing')
        self.assertEqual(self.events, [])
    def test_session_marker_missing_or_corrupt_refuses_record_binding(self):
        self.enrolled()
        marker = self.paths.session / '.managed-enrollment'
        marker.unlink()
        self.assertEqual(self.setup(resume=True).code, 'enrollment_inconsistent')
        marker.write_bytes(b'foreign-marker')
        marker.chmod(0o600)
        self.assertEqual(self.setup(resume=True).code, 'enrollment_inconsistent')
    def test_interrupted_record_persistence_never_opens_database_or_regenerates_key(self):
        with mock.patch.object(EnrollmentStore, '_write', side_effect=EnrollmentError()):
            self.assertEqual(self.setup().code, 'enrollment_inconsistent')
        self.assertFalse(self.paths.record.exists())
        self.assertTrue((self.paths.session / '.managed-enrollment').exists())
        self.assertFalse((self.paths.session / 'db').exists())
        writes = list(self.store.puts)
        self.assertEqual(self.setup().code, 'enrollment_missing')
        self.assertEqual(self.store.puts, writes)
        self.assertEqual(self.events, [])

    def test_secrets_have_redacted_repr(self):
        sentinel = b'SYNTHETIC_SECRET_SENTINEL'
        self.assertNotIn(sentinel.decode(), repr(SecretValue(sentinel)))
        self.assertNotIn(sentinel.decode(), repr(ApiCredentials(123, sentinel.decode(), sentinel)))


class TerminalBoundaryTests(unittest.TestCase):
    def evidence(self, intermediary=None):
        uid = os.getuid()
        processes = [ProcessIdentity(10, 11, uid, '/python'), ProcessIdentity(11, 12, uid, '/bin/zsh'),
                     ProcessIdentity(12, 1, uid, NATIVE_TERMINAL)]
        if intermediary:
            processes[1] = ProcessIdentity(11, 12, uid, intermediary)
        return TerminalInspection(uid, uid, True, True, True, tuple(processes))
    def test_native_foreground_is_accepted_but_capture_ancestry_is_refused(self):
        NativeTerminal(_inspector=lambda: self.evidence()).require_secure()
        for name in ['/usr/bin/ssh', '/usr/bin/tmux', '/usr/bin/script', '/app/Codex.app/Contents/MacOS/Codex', '/usr/bin/agent']:
            with self.subTest(name=name), self.assertRaises(TerminalError):
                NativeTerminal(_inspector=lambda: self.evidence(name)).require_secure()
    def test_non_tty_or_background_or_wrong_owner_is_refused_before_reader(self):
        for field, value in [('interactive', False), ('foreground', False), ('controlling', False), ('tty_owner_uid', -1)]:
            evidence = self.evidence()
            evidence = TerminalInspection(**{**evidence.__dict__, field: value})
            reader = mock.Mock()
            with self.subTest(field=field), self.assertRaises(TerminalError):
                NativeTerminal(_inspector=lambda: evidence, _reader=reader).read_secret('code')
            reader.assert_not_called()


class Backend:
    def __init__(self):
        self.values, self.status, self.calls = {}, None, []
    def default_keychain(self): return 0, 'keychain'
    def search_list(self): return 0, ('keychain', 'other-keychain')
    def constant(self, value): return value
    def string(self, value): return value
    def array(self, values): return tuple(values)
    def dictionary(self, pairs): return dict(pairs)
    def data(self, value): return value
    def release(self, value): pass
    def data_bytes(self, value, maximum): return value
    def array_values(self, value, maximum): return tuple(value)
    def copy_matching(self, query):
        """Mirrors file-based macOS keychains, which refuse data for many items (-50)."""
        if self.status: return self.status, None
        if query.get('kSecReturnData') and query.get('kSecMatchLimit') != 'kSecMatchLimitOne':
            return -50, None
        value = self.values.get(query['kSecAttrAccount'])
        if value is None:
            return -25300, None
        if query.get('kSecReturnData'):
            return 0, value
        return 0, ({'acct': query['kSecAttrAccount']},)
    def add(self, query):
        self.calls.append(query)
        name = query['kSecAttrAccount']
        if name in self.values: return -25299
        self.values[name] = query['kSecValueData']
        return 0


class CredentialBoundaryTests(unittest.TestCase):
    def test_reads_legacy_search_list_and_refuses_ambiguous_duplicate_account(self):
        backend = Backend()
        backend.values['api_id'] = b'123'
        queries = []
        real = backend.copy_matching
        def copy(query):
            queries.append(query)
            return real(query)
        backend.copy_matching = copy
        store = MacOSSecretStore(backend_factory=lambda: backend)
        self.assertEqual(store.get('api_id').reveal(), b'123')
        self.assertEqual(queries[0]['kSecMatchSearchList'], ('keychain', 'other-keychain'))
        backend.copy_matching = lambda query: (0, ({'acct': 1}, {'acct': 2}))
        with self.assertRaises(CredentialStoreError) as caught:
            store.get('api_id')
        self.assertEqual(caught.exception.code, 'conflict')
    def test_never_asks_for_data_of_many_items_which_real_keychains_refuse(self):
        # Found on a real Mac: kSecReturnData with kSecMatchLimitAll returns errSecParam (-50),
        # which made every stored pair read as invalid_record and blocked setup and connect.
        backend = Backend()
        backend.values.update(api_id=b'12345678', api_hash=b'a' * 32)
        queries = []
        real = backend.copy_matching
        def copy(query):
            queries.append(dict(query))
            return real(query)
        backend.copy_matching = copy
        store = MacOSSecretStore(backend_factory=lambda: backend)
        self.assertEqual(store.get('api_id').reveal(), b'12345678')
        self.assertEqual(store.get('api_hash').reveal(), b'a' * 32)
        for query in queries:
            self.assertFalse(query.get('kSecReturnData') and query.get('kSecMatchLimit') == 'kSecMatchLimitAll')
        self.assertTrue(any(q.get('kSecReturnAttributes') and q.get('kSecMatchLimit') == 'kSecMatchLimitAll'
                            for q in queries))
    def test_item_removed_between_count_and_read_is_unavailable_not_absent(self):
        backend = Backend()
        backend.values['api_id'] = b'123'
        real = backend.copy_matching
        def copy(query):
            if query.get('kSecReturnData'):
                return -25300, None
            return real(query)
        backend.copy_matching = copy
        with self.assertRaises(CredentialStoreError) as caught:
            MacOSSecretStore(backend_factory=lambda: backend).get('api_id')
        self.assertEqual(caught.exception.code, 'unavailable')
    def test_construction_is_inert_and_get_distinguishes_absent_from_denied(self):
        backend = Backend()
        factory = mock.Mock(return_value=backend)
        store = MacOSSecretStore(backend_factory=factory)
        factory.assert_not_called()
        self.assertIsNone(store.get('api_id'))
        for status, code in [(-128, 'denied'), (-25308, 'locked'), (-25291, 'unavailable')]:
            backend.status = status
            with self.subTest(status=status), self.assertRaises(CredentialStoreError) as caught:
                store.get('api_id')
            self.assertEqual(caught.exception.code, code)
    def test_add_only_preserves_conflicting_record_and_never_updates(self):
        backend = Backend()
        backend.values['api_hash'] = b'a' * 32
        store = MacOSSecretStore(backend_factory=lambda: backend)
        with self.assertRaises(CredentialStoreError) as caught:
            store.put('api_hash', SecretValue(b'b' * 32))
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(backend.values['api_hash'], b'a' * 32)
    def test_exception_chain_and_secret_input_are_redacted(self):
        def factory(): raise RuntimeError('SYNTHETIC_SECRET_SENTINEL')
        store = MacOSSecretStore(backend_factory=factory)
        with self.assertRaises(CredentialStoreError) as caught:
            store.get('api_id')
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn('SYNTHETIC_SECRET_SENTINEL', str(caught.exception))


class ClientEncryptionTests(unittest.TestCase):
    def test_parameters_and_wait_encryption_key_encode_binary_bytes(self):
        for initial in ['authorizationStateWaitTdlibParameters', 'authorizationStateWaitEncryptionKey']:
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                raw = AuthRaw([initial, 'authorizationStateReady'])
                key = bytes(range(32))
                client = TDLibClient(raw=raw, session_directory=Path(tmp),
                    credential_loader=lambda: ApiCredentials(123, 'legacy-hash', key),
                    database_key_loader=lambda: key)
                client.ensure_ready()
                payload = next(row for row in raw.sent if row['@type'] != 'getAuthorizationState')
                field = 'database_encryption_key' if initial.endswith('Parameters') else 'encryption_key'
                self.assertEqual(base64.b64decode(payload[field], validate=True), key)
    def test_legacy_encryption_branch_needs_no_api_credential_lookup(self):
        raw = AuthRaw(['authorizationStateWaitEncryptionKey', 'authorizationStateReady'])
        loader = mock.Mock(side_effect=AssertionError('legacy credential lookup changed'))
        TDLibClient(raw=raw, credential_loader=loader).ensure_ready()
        loader.assert_not_called()
        payload = next(row for row in raw.sent if row['@type'] == 'checkDatabaseEncryptionKey')
        self.assertEqual(payload['encryption_key'], '')
    def test_data_transport_does_not_allow_auth_submission(self):
        raw = AuthRaw(['authorizationStateReady'])
        client = TDLibClient(raw=raw)
        for kind in ['setAuthenticationPhoneNumber', 'checkAuthenticationCode', 'checkAuthenticationPassword', 'close']:
            with self.subTest(kind=kind), self.assertRaises(ForbiddenTDLibRequest):
                client._call({'@type': kind})
        self.assertEqual(raw.sent, [])


class AuthTransportBoundaryTests(unittest.TestCase):
    def test_uncertain_teardown_is_not_retried_and_blocks_further_submission(self):
        raw = AuthRaw(['authorizationStateReady'])
        destroy = mock.Mock(side_effect=RuntimeError('SYNTHETIC_DESTROY_SENTINEL'))
        raw.close = destroy
        transport = AuthTransport(raw)
        teardown = transport.close()
        self.assertTrue(teardown.provider_closed)
        self.assertFalse(teardown.native_destroyed)
        sent = list(raw.sent)
        self.assertEqual(transport.close(), teardown)
        destroy.assert_called_once()
        with self.assertRaises(AuthTransportError):
            transport.check_code(SecretValue(b'SYNTHETIC_CODE_SENTINEL'))
        self.assertEqual(raw.sent, sent)

    def test_correlation_ignores_unrelated_ready_response(self):
        raw = AuthRaw(['authorizationStateWaitPhoneNumber'])
        raw.queue.append({'@type': 'authorizationStateReady', '@extra': 'another-request'})
        self.assertIs(AuthTransport(raw).get_authorization_state(), AuthorizationState.PHONE)
        self.assertEqual(raw.sent[0]['@type'], 'getAuthorizationState')

    def test_uncertain_timeout_submits_once_and_redacts_error(self):
        class SilentRaw:
            def __init__(self): self.sent = []
            def send(self, value): self.sent.append(value)
            def receive(self, timeout): return None
            def close(self): pass
        ticks = iter([0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1, 2])
        raw = SilentRaw()
        transport = AuthTransport(raw, _clock=lambda: next(ticks))
        with self.assertRaises(AuthTransportError) as caught:
            transport.check_code(SecretValue(b'SYNTHETIC_SECRET_SENTINEL'), timeout=.5)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(len(raw.sent), 1)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn('SYNTHETIC_SECRET_SENTINEL', str(caught.exception))

    def test_invalid_phone_is_rejected_before_provider_submission(self):
        raw = AuthRaw()
        with self.assertRaises(AuthTransportError) as caught:
            AuthTransport(raw).set_phone_number(SecretValue(b'invalid-secret-phone'))
        self.assertEqual(caught.exception.code, 'invalid_input')
        self.assertEqual(raw.sent, [])
