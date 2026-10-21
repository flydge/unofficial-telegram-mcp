"""The Homebrew/source native path answers the same synchronous version/commit probe as the bundle."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from telegram_search_mcp import config, tdjson
from telegram_search_mcp.native_runtime import COMMIT


class FakeLibrary:
    """A legacy tdjson stand-in: records calls, answers getOption from a table."""

    def __init__(self, answers):
        self.calls = []

        def execute(client, request):
            value = json.loads(request)
            self.calls.append(('execute', value['@type'], value.get('name')))
            if value['@type'] == 'getOption':
                answer = answers.get(value['name'])
                return None if answer is None else json.dumps({'@type': 'optionValueString', 'value': answer}).encode()
            return b'{"@type":"ok"}'

        def create():
            self.calls.append(('create',))
            return 1

        def noop(*args):
            self.calls.append(('other',))
            return None

        self.td_json_client_execute = execute
        self.td_json_client_create = create
        self.td_json_client_send = self.td_json_client_receive = self.td_json_client_destroy = noop


class HomebrewProbeTests(unittest.TestCase):
    def load(self, answers, expected=('1.8.67', COMMIT)):
        library = FakeLibrary(answers)
        with mock.patch.object(tdjson, 'tdlib_runtime', return_value=(Path('/synthetic/libtdjson.dylib'), expected)), \
                mock.patch.object(tdjson.ctypes, 'CDLL', return_value=library):
            try:
                tdjson.LegacyTDJson()
                return library, None
            except tdjson.TDLibError as error:
                return library, error

    def test_matching_version_and_commit_create_the_client(self):
        library, error = self.load({'version': '1.8.67', 'commit_hash': COMMIT})
        self.assertIsNone(error)
        self.assertEqual(library.calls[:2], [('execute', 'getOption', 'version'), ('execute', 'getOption', 'commit_hash')])
        self.assertIn(('create',), library.calls)

    def test_another_version_or_commit_refuses_before_any_client_exists(self):
        for label, answers in {'other version': {'version': '1.8.66', 'commit_hash': COMMIT},
                               'other commit': {'version': '1.8.67', 'commit_hash': '0' * 40},
                               'no answer': {}}.items():
            with self.subTest(label):
                library, error = self.load(answers)
                self.assertIsInstance(error, tdjson.TDLibError)
                self.assertNotIn(('create',), library.calls)
                self.assertNotIn('1.8.66', str(error))
                self.assertNotIn('0' * 40, str(error))

    def test_the_bundled_path_is_already_probed_by_validate_native(self):
        library, error = self.load({}, expected=None)
        self.assertIsNone(error)
        self.assertNotIn(('execute', 'getOption', 'version'), library.calls)
        self.assertIn(('create',), library.calls)

    def test_homebrew_runtime_names_the_approved_pin(self):
        with mock.patch('telegram_search_mcp.bundle_runtime.selected_native', return_value=None), \
                mock.patch.object(config, '_homebrew_tdlib', return_value=Path('/synthetic/libtdjson.dylib')):
            self.assertEqual(config.tdlib_runtime(), (Path('/synthetic/libtdjson.dylib'), ('1.8.67', COMMIT)))
            self.assertEqual(config.validate_tdlib_runtime(), Path('/synthetic/libtdjson.dylib'))
        with mock.patch('telegram_search_mcp.bundle_runtime.selected_native', return_value=Path('/release/native/libtdjson.dylib')):
            self.assertEqual(config.tdlib_runtime(), (Path('/release/native/libtdjson.dylib'), None))


class OpenSSLKegTests(unittest.TestCase):
    def test_reports_the_resolved_keg_or_a_fixed_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            keg = root / 'Cellar/openssl@3/3.6.4'
            keg.mkdir(parents=True)
            (root / 'opt').mkdir()
            os.symlink(keg, root / 'opt/openssl@3')
            odd = root / 'Cellar/other/9.9'
            odd.mkdir(parents=True)
            os.symlink(odd, root / 'opt/odd')
            for link, expected in ((root / 'opt/openssl@3', 'openssl@3 3.6.4'), (root / 'opt/missing', 'unavailable'),
                                   (root / 'opt/odd', 'unrecognized')):
                with self.subTest(expected), mock.patch.object(config, 'HOMEBREW_OPENSSL', link):
                    self.assertEqual(config.homebrew_openssl_keg(), expected)

    def test_this_host_answers_in_the_closed_shape(self):
        value = config.homebrew_openssl_keg()
        self.assertTrue(value in ('unavailable', 'unrecognized') or value.startswith('openssl@3 '))


if __name__ == '__main__':
    unittest.main()
