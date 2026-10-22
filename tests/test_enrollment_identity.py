"""Enrollment-bound API identity across setup and the broker; all values synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_onboarding import AuthRaw, MemoryStore, Terminal

from telegram_search_mcp.app_identity import AppIdentityError
from telegram_search_mcp.credential_identity import (CredentialIdentityError, ResolvedIdentity,
                                                     identity_fingerprint, session_credential_loader)
from telegram_search_mcp.credentials import SecretValue
from telegram_search_mcp.enrollment import EnrollmentError, EnrollmentStore, SetupPaths
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.session_owner import SessionOwnerLease
from telegram_search_mcp.setup_wizard import run_setup

BUNDLED = ApiCredentials(777001, 'c' * 32)
OWN_PAIR = {'api_id': SecretValue(b'123456'), 'api_hash': SecretValue(b'a' * 32)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'app'
        self.paths = SetupPaths(self.root)
        self.store, self.terminal, self.events, self.raws = MemoryStore(), Terminal(), [], []
    def tearDown(self):
        self.temp.cleanup()
    def factory(self, states=None):
        raw = AuthRaw(states, events=self.events, paths=self.paths)
        self.raws.append(raw)
        self.events.append('create')
        return raw
    def handoff(self):
        with SessionOwnerLease(self.paths.lock):
            self.events.append('handoff')
        return 'ready'
    def setup(self, bundled=lambda: None, **kwargs):
        return run_setup(self.paths, self.terminal, self.store, kwargs.pop('provider_factory', self.factory),
                         handoff=self.handoff, bundled_loader=bundled, **kwargs)
    def parameters(self):
        return next(row for row in self.raws[-1].sent if row['@type'] == 'setTdlibParameters')


class SetupIdentityTests(Fixture):
    def test_bundled_identity_needs_no_api_prompts_and_is_bound(self):
        self.assertEqual(self.setup(bundled=lambda: BUNDLED).status, 'ready')
        self.assertEqual(self.terminal.prompts, ['phone', 'code', 'password'])
        self.assertEqual((self.parameters()['api_id'], self.parameters()['api_hash']), (777001, 'c' * 32))
        record = EnrollmentStore(self.paths).read()
        self.assertEqual(record['credential_source'], 'bundled')
        self.assertEqual(record['identity_fingerprint'], identity_fingerprint(BUNDLED))
        self.assertNotIn('api_id', self.store.puts)
        self.assertNotIn(b'c' * 32, self.paths.record.read_bytes())

    def test_owner_keychain_pair_overrides_bundled_identity(self):
        self.store.values.update(OWN_PAIR)
        self.assertEqual(self.setup(bundled=lambda: BUNDLED).status, 'ready')
        self.assertEqual(self.parameters()['api_id'], 123456)
        self.assertEqual(EnrollmentStore(self.paths).read()['credential_source'], 'keychain')

    def test_source_checkout_keeps_terminal_pair_prompts(self):
        self.assertEqual(self.setup().status, 'ready')
        self.assertEqual(self.terminal.prompts[:2], ['api_id', 'api_hash'])
        self.assertEqual(EnrollmentStore(self.paths).read()['credential_source'], 'keychain')

    def test_resume_of_bundled_enrollment_uses_bundled_even_if_keychain_appears(self):
        self.setup(bundled=lambda: BUNDLED, provider_factory=lambda: self.factory(
            ['authorizationStateWaitTdlibParameters', 'authorizationStateWaitPhoneNumber',
             'authorizationStateWaitOtherDeviceConfirmation']))
        self.store.values.update(OWN_PAIR)
        self.assertEqual(self.setup(bundled=lambda: BUNDLED, resume=True).status, 'ready')
        self.assertEqual(self.parameters()['api_id'], 777001)

    def test_changed_bundled_identity_refuses_before_native_client(self):
        self.setup(bundled=lambda: BUNDLED, provider_factory=lambda: self.factory(
            ['authorizationStateWaitTdlibParameters', 'authorizationStateWaitOtherDeviceConfirmation']))
        self.events.clear()
        result = self.setup(bundled=lambda: ApiCredentials(777002, 'd' * 32), resume=True)
        self.assertEqual(result.code, 'identity_changed')
        self.assertEqual(self.events, [])

    def test_wrong_typed_pair_for_bound_record_is_refused_before_any_keychain_write(self):
        self.store.values.update(OWN_PAIR)
        self.assertEqual(self.setup().status, 'ready')
        for name in ('api_id', 'api_hash'):
            del self.store.values[name]
        self.store.puts.clear()
        self.terminal.values.update(api_id=b'999999', api_hash=b'e' * 32)
        self.events.clear()
        self.assertEqual(self.setup(resume=True).code, 'identity_changed')
        self.assertEqual((self.store.puts, self.events), ([], []))
        self.assertNotIn('api_id', self.store.values)
        self.terminal.values.update(api_id=b'123456', api_hash=b'a' * 32)
        self.assertEqual(self.setup(resume=True).status, 'ready')
        self.assertEqual(sorted(self.store.puts), ['api_hash', 'api_id'])

    def test_invalid_bundled_identity_refuses_without_prompts(self):
        def broken():
            raise AppIdentityError()
        self.assertEqual(self.setup(bundled=broken).code, 'app_identity_invalid')
        self.assertEqual(self.terminal.prompts, [])
        self.assertFalse(self.paths.record.exists())


class RecordCompatibilityTests(Fixture):
    def enrolled_legacy(self):
        """A record exactly as written before 0.52: no identity fields."""
        self.store.values.update(OWN_PAIR)
        self.assertEqual(self.setup().status, 'ready')
        record = EnrollmentStore(self.paths).read()
        legacy = {k: v for k, v in record.items() if k not in {'credential_source', 'identity_fingerprint'}}
        self.paths.record.write_text(json.dumps(legacy, sort_keys=True, separators=(',', ':')))
        return legacy

    def test_pre_052_record_still_reads_and_state_changes_preserve_its_shape(self):
        legacy = self.enrolled_legacy()
        store = EnrollmentStore(self.paths)
        self.assertEqual(store.read(), legacy)
        store.set_state('pending')
        self.assertEqual(set(store.read()), set(legacy))

    def test_state_write_with_binding_adds_fields_once_and_never_rebinds(self):
        self.enrolled_legacy()
        store = EnrollmentStore(self.paths)
        own = ResolvedIdentity(ApiCredentials(123456, 'a' * 32), 'keychain')
        store.set_state('pending', bound=own)
        self.assertEqual(store.read()['identity_fingerprint'], own.fingerprint)
        store.set_state('ready', bound=own)
        with self.assertRaises(CredentialIdentityError) as caught:
            store.set_state('pending', bound=ResolvedIdentity(BUNDLED, 'bundled'))
        self.assertEqual(caught.exception.code, 'identity_changed')
        self.assertEqual(store.read()['credential_source'], 'keychain')

    def test_resume_binds_a_pre_052_record_on_its_state_write(self):
        self.enrolled_legacy()
        self.assertEqual(self.setup(resume=True).status, 'ready')
        record = EnrollmentStore(self.paths).read()
        self.assertEqual(record['credential_source'], 'keychain')
        self.assertEqual(record['identity_fingerprint'], identity_fingerprint(ApiCredentials(123456, 'a' * 32)))

    def test_malformed_identity_fields_refuse(self):
        self.store.values.update(OWN_PAIR)
        self.setup()
        good = EnrollmentStore(self.paths).read()
        for change in [{'credential_source': 'cloud'}, {'identity_fingerprint': 'x' * 64},
                       {'identity_fingerprint': None}]:
            with self.subTest(change=change):
                self.paths.record.write_text(json.dumps({**good, **change}))
                with self.assertRaises(EnrollmentError):
                    EnrollmentStore(self.paths).read()
        partial = {k: v for k, v in good.items() if k != 'identity_fingerprint'}
        self.paths.record.write_text(json.dumps(partial))
        with self.assertRaises(EnrollmentError):
            EnrollmentStore(self.paths).read()


class BrokerLoaderTests(Fixture):
    def test_no_enrollment_keeps_legacy_keychain_and_empty_key(self):
        keychain = mock.Mock(return_value=ApiCredentials(123456, 'a' * 32))
        key, loader = session_credential_loader(self.paths, self.store, keychain_loader=keychain,
                                                bundled_loader=lambda: BUNDLED)
        self.assertEqual(key, b'')
        self.assertEqual(loader().api_id, 123456)

    def test_bundled_enrollment_needs_no_keychain_pair(self):
        self.setup(bundled=lambda: BUNDLED)
        keychain = mock.Mock(side_effect=AssertionError('keychain pair consulted'))
        key, loader = session_credential_loader(self.paths, self.store, keychain_loader=keychain,
                                                bundled_loader=lambda: BUNDLED)
        credentials = loader()
        self.assertEqual((credentials.api_id, credentials.database_key), (777001, key))
        self.assertEqual(len(key), 32)
        keychain.assert_not_called()

    def test_broker_refuses_changed_identity(self):
        self.setup(bundled=lambda: BUNDLED)
        _, loader = session_credential_loader(self.paths, self.store, keychain_loader=mock.Mock(),
                                              bundled_loader=lambda: ApiCredentials(777002, 'd' * 32))
        with self.assertRaises(CredentialIdentityError) as caught:
            loader()
        self.assertEqual(caught.exception.code, 'identity_changed')


if __name__ == '__main__':
    unittest.main()


class BrokerWiringTests(Fixture):
    def test_from_defaults_uses_the_session_loader(self):
        from telegram_search_mcp import tdjson
        loader = mock.Mock(return_value=BUNDLED)
        with mock.patch('telegram_search_mcp.credential_identity.session_credential_loader',
                        return_value=(b'k' * 32, loader)), mock.patch.object(tdjson, 'LegacyTDJson'), \
                mock.patch.object(tdjson.os, 'umask') as umask:  # The real call would outlive this test.
            client = tdjson.TDLibClient.from_defaults()
        umask.assert_called_once_with(0o077)
        self.assertIs(client._credential_loader, loader)
        self.assertEqual(client._database_key_loader(), b'k' * 32)

    def test_preflight_resolves_pair_without_reading_the_database_key(self):
        from telegram_search_mcp.credential_identity import preflight_credentials
        self.setup(bundled=lambda: BUNDLED)
        self.store.gets.clear()
        resolved = preflight_credentials(self.paths, keychain_loader=mock.Mock(), bundled_loader=lambda: BUNDLED)
        self.assertEqual((resolved.api_id, resolved.database_key), (777001, b''))
        keychain = mock.Mock(return_value=ApiCredentials(1, 'f' * 32))
        empty = SetupPaths(Path(self.temp.name).resolve() / 'other')
        self.assertEqual(preflight_credentials(empty, keychain_loader=keychain, bundled_loader=lambda: None).api_id, 1)
