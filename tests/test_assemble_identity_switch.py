"""The assembler's identity switch (CLIENTS-16 prep): none by default, the owner's service, or a named one.

Synthetic Keychain values behind a fake `security`; no Keychain is read and nothing is printed.
"""
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from bundle_payload import make_payload
from telegram_search_mcp.config import KEYCHAIN_SERVICE
from telegram_search_mcp.release_archive import load_archive_index

ROOT = Path(__file__).parents[1]
API_ID = 2468013
API_HASH = 'ab' * 16  # synthetic
SERVICE = 'io.example.synthetic.app-identity'


def load():
    spec = importlib.util.spec_from_file_location('assembly_switch_under_test', ROOT / 'packaging/assemble_bundle.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AB = load()


def runner(calls, *, fail=False):
    def run(args, **options):
        calls.append(list(args))
        if fail:
            return subprocess.CompletedProcess(args, 44, 'private-keychain-output', 'private-keychain-output')
        return subprocess.CompletedProcess(args, 0, {'api_id': str(API_ID), 'api_hash': API_HASH}[args[-1]] + '\n', '')
    return run


class IdentitySwitchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.payload = make_payload(self.root)

    def services(self, calls):
        return [c[c.index('-s') + 1] for c in calls]

    def test_a_named_service_is_read_and_bundled_silently(self):
        calls, out = [], io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = AB.assemble_bundle(self.payload, self.root / 'output', app_identity_service=SERVICE,
                                        keychain_runner=runner(calls))
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(self.services(calls), [SERVICE, SERVICE])
        self.assertEqual([c[-1] for c in calls], ['api_id', 'api_hash'])
        self.assertEqual(calls[0][:3], ['/usr/bin/security', 'find-generic-password', '-w'])
        index = load_archive_index(result.index.read_bytes())
        self.assertIn('app-identity.json', [f.path for f in index.files])
        for generated in (result.audit, result.index, result.provenance):
            self.assertNotIn(API_HASH.encode(), generated.read_bytes())
            self.assertNotIn(SERVICE.encode(), generated.read_bytes())

    def test_the_existing_flag_still_reads_the_owner_service_and_the_default_reads_nothing(self):
        calls = []
        AB.assemble_bundle(self.payload, self.root / 'flag', app_identity_from_keychain=True,
                           keychain_runner=runner(calls))
        self.assertEqual(self.services(calls), [KEYCHAIN_SERVICE, KEYCHAIN_SERVICE])
        calls = []
        result = AB.assemble_bundle(self.payload, self.root / 'none', keychain_runner=runner(calls))
        self.assertEqual(calls, [])
        self.assertNotIn('app-identity.json', [f.path for f in load_archive_index(result.index.read_bytes()).files])

    def test_invalid_switches_refuse_before_any_read(self):
        for options in (dict(app_identity_service=SERVICE, app_identity_from_keychain=True),
                        dict(app_identity_service=''), dict(app_identity_service='a b'),
                        dict(app_identity_service='../x'), dict(app_identity_service='x' * 129),
                        dict(app_identity_service=b'bytes')):
            calls = []
            with self.subTest(options=options), self.assertRaises(AB.ReleasePackageError) as caught:
                AB.assemble_bundle(self.payload, self.root / 'output', keychain_runner=runner(calls), **options)
            self.assertEqual(str(caught.exception), 'app_identity_switch_invalid')
            self.assertEqual(calls, [])
            self.assertFalse((self.root / 'output').exists())

    def test_an_unavailable_named_service_is_one_fixed_code(self):
        calls = []
        with self.assertRaises(AB.ReleasePackageError) as caught:
            AB.assemble_bundle(self.payload, self.root / 'output', app_identity_service=SERVICE,
                               keychain_runner=runner(calls, fail=True))
        self.assertEqual(str(caught.exception), 'app_identity_unavailable')
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn('private', repr(caught.exception.args))
        self.assertFalse((self.root / 'output').exists())

    def test_command_line_has_the_switch_once(self):
        text = (ROOT / 'packaging/assemble_bundle.py').read_text()
        self.assertEqual(len(re.findall(r"add_argument\('--app-identity-service',", text)), 1)


if __name__ == '__main__':
    unittest.main()
