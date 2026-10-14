"""Application identity and credential source resolution; all values synthetic."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from telegram_search_mcp import bundle_runtime
from telegram_search_mcp.app_identity import AppIdentityError, bundled_app_identity, parse_app_identity
from telegram_search_mcp.credential_identity import (CredentialIdentityError, identity_fingerprint,
                                                     resolve_enrolled, resolve_new)
from telegram_search_mcp.credentials import CredentialStoreError, SecretValue
from telegram_search_mcp.keychain import ApiCredentials

BUNDLED = ApiCredentials(777001, 'c' * 32)
OWN = ApiCredentials(123456, 'a' * 32)


def identity_bytes(**overrides):
    return json.dumps({'version': 1, 'api_id': 777001, 'api_hash': 'c' * 32, **overrides}).encode()


class Store:
    def __init__(self, values=None, error=None):
        self.values, self.error, self.puts = dict(values or {}), error, []
    def get(self, account):
        if self.error:
            raise CredentialStoreError(self.error)
        value = self.values.get(account)
        return None if value is None else SecretValue(value)
    def put(self, account, value):
        self.puts.append(account)


class AppIdentityParseTests(unittest.TestCase):
    def test_exact_document_parses(self):
        parsed = parse_app_identity(identity_bytes())
        self.assertEqual((parsed.api_id, parsed.api_hash), (777001, 'c' * 32))
        self.assertNotIn('c' * 32, repr(parsed))

    def test_invalid_documents_refuse_without_echo(self):
        cases = [b'not json', b'[]', identity_bytes(version=2), identity_bytes(api_id=True),
                 identity_bytes(api_id=0), identity_bytes(api_id=2 ** 31), identity_bytes(api_id='777001'),
                 identity_bytes(api_hash='c' * 31), identity_bytes(api_hash='z' * 32),
                 identity_bytes(extra=1), b'{"version":1,"version":1,"api_id":1,"api_hash":"' + b'c' * 32 + b'"}',
                 b'x' * 5000]
        for data in cases:
            with self.subTest(data=data[:40]), self.assertRaises(AppIdentityError) as caught:
                parse_app_identity(data)
            self.assertEqual(caught.exception.code, 'app_identity_invalid')
            self.assertNotIn('c' * 31, str(caught.exception))
            self.assertIsNone(caught.exception.__context__)


class BundledLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'release'
        self.root.mkdir(mode=0o700)
        self.selection = mock.Mock(root=self.root)
    def tearDown(self):
        self.temp.cleanup()
    def write(self, data, mode=0o400):
        path = self.root / 'app-identity.json'
        path.write_bytes(data)
        path.chmod(mode)

    def test_source_checkout_has_no_bundled_identity(self):
        with mock.patch.object(bundle_runtime, '_SELECTION', None):
            self.assertIsNone(bundled_app_identity())

    def test_admitted_bundle_without_file_has_none_and_still_rechecks(self):
        with mock.patch.object(bundle_runtime, '_SELECTION', self.selection):
            self.assertIsNone(bundled_app_identity())
        self.selection.check.assert_called_once()

    def test_admitted_bundle_file_is_read_after_recheck(self):
        self.write(identity_bytes())
        with mock.patch.object(bundle_runtime, '_SELECTION', self.selection):
            self.assertEqual(bundled_app_identity().api_id, 777001)
        self.selection.check.assert_called_once()

    def test_writable_or_symlinked_file_refuses(self):
        self.write(identity_bytes(), mode=0o600)
        with mock.patch.object(bundle_runtime, '_SELECTION', self.selection), \
                self.assertRaises(AppIdentityError):
            bundled_app_identity()
        (self.root / 'app-identity.json').unlink()
        target = Path(self.temp.name) / 'elsewhere.json'
        target.write_bytes(identity_bytes())
        os.symlink(target, self.root / 'app-identity.json')
        with mock.patch.object(bundle_runtime, '_SELECTION', self.selection), \
                self.assertRaises(AppIdentityError):
            bundled_app_identity()

    def test_failed_bundle_recheck_never_reads_identity(self):
        self.write(identity_bytes())
        self.selection.check.side_effect = bundle_runtime.BundleError('receipt_mismatch')
        with mock.patch.object(bundle_runtime, '_SELECTION', self.selection), \
                self.assertRaises(bundle_runtime.BundleError):
            bundled_app_identity()


class ResolveNewTests(unittest.TestCase):
    def test_complete_keychain_pair_wins_over_bundled(self):
        resolved = resolve_new(Store({'api_id': b'123456', 'api_hash': b'a' * 32}), lambda: BUNDLED)
        self.assertEqual((resolved.source, resolved.credentials.api_id), ('keychain', 123456))
        self.assertEqual(resolved.fingerprint, identity_fingerprint(OWN))

    def test_empty_keychain_uses_bundled(self):
        resolved = resolve_new(Store(), lambda: BUNDLED)
        self.assertEqual((resolved.source, resolved.credentials), ('bundled', BUNDLED))

    def test_no_source_refuses(self):
        with self.assertRaises(CredentialIdentityError) as caught:
            resolve_new(Store(), lambda: None)
        self.assertEqual(caught.exception.code, 'app_identity_missing')

    def test_partial_pair_refuses_even_with_bundled(self):
        for values in [{'api_id': b'123456'}, {'api_hash': b'a' * 32}]:
            with self.subTest(values=values), self.assertRaises(CredentialIdentityError) as caught:
                resolve_new(Store(values), lambda: BUNDLED)
            self.assertEqual(caught.exception.code, 'credential_conflict')

    def test_invalid_record_and_store_failures_keep_distinct_codes(self):
        with self.assertRaises(CredentialIdentityError) as caught:
            resolve_new(Store({'api_id': b'12x', 'api_hash': b'a' * 32}), lambda: BUNDLED)
        self.assertEqual(caught.exception.code, 'credential_invalid_record')
        for error in ['denied', 'locked', 'unavailable']:
            with self.subTest(error=error), self.assertRaises(CredentialIdentityError) as caught:
                resolve_new(Store(error=error), lambda: BUNDLED)
            self.assertEqual(caught.exception.code, 'credential_' + error)

    def test_resolution_never_writes_keychain(self):
        store = Store()
        resolve_new(store, lambda: BUNDLED)
        self.assertEqual(store.puts, [])


class ResolveEnrolledTests(unittest.TestCase):
    def record(self, **fields):
        return {'state': 'ready', **fields}

    def test_legacy_record_without_fields_uses_keychain_unverified(self):
        bundled = mock.Mock(side_effect=AssertionError('bundled identity consulted'))
        self.assertEqual(resolve_enrolled(self.record(), keychain_loader=lambda: OWN, bundled_loader=bundled), OWN)

    def test_bound_sources_use_their_own_loader(self):
        keychain = self.record(credential_source='keychain', identity_fingerprint=identity_fingerprint(OWN))
        bundled = self.record(credential_source='bundled', identity_fingerprint=identity_fingerprint(BUNDLED))
        self.assertEqual(resolve_enrolled(keychain, keychain_loader=lambda: OWN, bundled_loader=lambda: BUNDLED), OWN)
        self.assertEqual(resolve_enrolled(bundled, keychain_loader=lambda: OWN, bundled_loader=lambda: BUNDLED), BUNDLED)

    def test_changed_identity_refuses_instead_of_rebinding(self):
        record = self.record(credential_source='bundled', identity_fingerprint=identity_fingerprint(BUNDLED))
        with self.assertRaises(CredentialIdentityError) as caught:
            resolve_enrolled(record, keychain_loader=lambda: OWN,
                             bundled_loader=lambda: ApiCredentials(777002, 'd' * 32))
        self.assertEqual(caught.exception.code, 'identity_changed')

    def test_bundled_record_without_bundle_refuses(self):
        record = self.record(credential_source='bundled', identity_fingerprint=identity_fingerprint(BUNDLED))
        with self.assertRaises(CredentialIdentityError) as caught:
            resolve_enrolled(record, keychain_loader=lambda: OWN, bundled_loader=lambda: None)
        self.assertEqual(caught.exception.code, 'app_identity_missing')

    def test_database_key_is_carried_through(self):
        record = self.record(credential_source='keychain', identity_fingerprint=identity_fingerprint(OWN))
        resolved = resolve_enrolled(record, keychain_loader=lambda: OWN, bundled_loader=lambda: None,
                                    database_key=b'k' * 32)
        self.assertEqual(resolved.database_key, b'k' * 32)
        self.assertEqual(identity_fingerprint(resolved), identity_fingerprint(OWN))


if __name__ == '__main__':
    unittest.main()
