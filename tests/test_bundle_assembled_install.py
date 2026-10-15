"""One synthetic end-to-end run: assemble -> authenticated extraction -> install -> admitted identity.

Everything is inert and disposable: synthetic payload bytes, a fake Keychain runner with
synthetic values, a fake `claude` CLI and a temporary home. No real service, client or secret.
"""
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from bundle_payload import keychain_runner, load_assembler, make_payload
from test_bundle_install import InstallHarness
from telegram_search_mcp.release_archive import extract_verified_archive, load_archive_index

API_ID, API_HASH = 2468013, 'fedcba9876543210fedcba9876543210'


class AssembledBundleInstallTests(InstallHarness, unittest.TestCase):
    def assembled(self, *, flag):
        assembler = load_assembler()
        payload = make_payload(self.base)
        result = assembler.assemble_bundle(payload, self.base / 'output', app_identity_from_keychain=flag,
                                           keychain_runner=keychain_runner(API_ID, API_HASH))
        extract_verified_archive(result.archive, load_archive_index(result.index.read_bytes()), self.base / 'admitted')
        return self.module_(), self.base / 'admitted', result.index, result.index_sha256

    def module_(self):
        import importlib
        return importlib.import_module('telegram_search_mcp.bundle_install')

    def installed_selection(self, packet):
        from telegram_search_mcp import bundle_runtime
        result = self.install(packet)
        selection = bundle_runtime.Selection(Path(result['release_root']), packet[3])
        patcher = mock.patch.object(bundle_runtime, '_SELECTION', selection)
        patcher.start()
        self.addCleanup(patcher.stop)
        return result, selection

    def test_assembled_identity_installs_read_only_and_is_the_only_copy_of_the_pair(self):
        from telegram_search_mcp.app_identity import bundled_app_identity
        packet = self.assembled(flag=True)
        result, selection = self.installed_selection(packet)
        self.assertEqual(result['claude_code'], 'registered')
        selection.check()
        credentials = bundled_app_identity()
        self.assertEqual((credentials.api_id, credentials.api_hash), (API_ID, API_HASH))
        installed = Path(result['release_root']) / 'app-identity.json'
        self.assertEqual(oct(installed.stat().st_mode & 0o777), '0o400')
        index = load_archive_index((Path(result['release_root']) / 'bundle-index.json').read_bytes())
        record = next(f for f in index.files if f.path == 'app-identity.json')
        self.assertEqual(record.sha256, hashlib.sha256(installed.read_bytes()).hexdigest())
        # Receipts, journal, registration and every skill/config file stay free of the pair.
        copies = [p for p in self.home.rglob('*') if p.is_file() and API_HASH.encode() in p.read_bytes()]
        self.assertEqual(copies, [installed])

    def test_bundle_assembled_without_the_flag_has_no_identity_to_serve(self):
        from telegram_search_mcp.app_identity import bundled_app_identity
        packet = self.assembled(flag=False)
        result, selection = self.installed_selection(packet)
        self.assertFalse((Path(result['release_root']) / 'app-identity.json').exists())
        self.assertIsNone(bundled_app_identity())

    def test_a_tampered_installed_identity_fails_admission_instead_of_being_served(self):
        from telegram_search_mcp import bundle_runtime
        from telegram_search_mcp.app_identity import bundled_app_identity
        packet = self.assembled(flag=True)
        result, selection = self.installed_selection(packet)
        target = Path(result['release_root']) / 'app-identity.json'
        target.chmod(0o600)
        target.write_bytes(b'{"api_hash":"00000000000000000000000000000000","api_id":1,"version":1}\n')
        target.chmod(0o400)
        with self.assertRaises(bundle_runtime.BundleError): bundled_app_identity()


if __name__ == '__main__': unittest.main()
