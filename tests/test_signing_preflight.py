"""signing_preflight.sh (CLIENTS-6) against mocked `security` and `xcrun notarytool`.

A copy of the script has exactly its tool block rewritten to mock paths; no Keychain, certificate or
notary service is used. The output carries only the identity name, the Team ID and `notary: ok`.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / 'packaging/signing_preflight.sh'
TOOL_BLOCK = 'tool_security=/usr/bin/security\ntool_xcrun=/usr/bin/xcrun\n'
CERT_HASH = '0123456789ABCDEF0123456789ABCDEF01234567'
DEVELOPER_ID = 'Developer ID Application: Synthetic Example, Ltd. (ABCDE12345)'
OTHER_TEAM = 'Developer ID Application: Other Example (ZYXWV98765)'
HISTORY = '{"history":[{"id":"aaaaaaaa-0000-0000-0000-000000000000","name":"secret-name.zip"}]}'


def listing(*names):
    rows = [f'  {i + 1}) {CERT_HASH[:-1]}{i} "{name}"' for i, name in enumerate(names)]
    return '\n'.join(rows + [f'     {len(names)} valid identities found']) + '\n'


class PreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.control = self.root / 'control'
        self.control.mkdir()
        (self.control / 'identities').write_text(listing('Apple Development: Synthetic Example (QQQQQ11111)',
                                                         DEVELOPER_ID))
        (self.control / 'security-exit').write_text('0')
        (self.control / 'history-exit').write_text('0')
        security, xcrun = self.root / 'security', self.root / 'xcrun'
        security.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.control}/calls"\n'
                            f'[ "$*" = "find-identity -v -p codesigning" ] || exit 64\n'
                            f'cat "{self.control}/identities"\nexit "$(cat "{self.control}/security-exit")"\n')
        xcrun.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.control}/calls"\n'
                         f"printf '%s\\n' '{HISTORY}'\nexit \"$(cat \"{self.control}/history-exit\")\"\n")
        security.chmod(0o700)
        xcrun.chmod(0o700)
        text = SCRIPT.read_text()
        self.assertEqual(text.count(TOOL_BLOCK), 1)
        self.script = self.root / 'signing_preflight.sh'
        self.script.write_text(text.replace(TOOL_BLOCK, f'tool_security={security}\ntool_xcrun={xcrun}\n'))

    def run_script(self, *args):
        return subprocess.run(['/bin/sh', '-p', str(self.script), *args], capture_output=True, text=True, timeout=60,
                              env={'PATH': '/usr/bin:/bin'})

    def test_one_developer_id_identity_and_a_working_profile(self):
        result = self.run_script('--profile', 'release-notary')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'schema_version': 1, 'identity': DEVELOPER_ID,
                                                     'team_id': 'ABCDE12345', 'notary': 'ok'})
        self.assertEqual(result.stderr, '')
        for leaked in (CERT_HASH[:20], 'secret-name', 'aaaaaaaa-0000'):
            self.assertNotIn(leaked, result.stdout)
        self.assertEqual((self.control / 'calls').read_text().splitlines(), [
            'find-identity -v -p codesigning',
            'notarytool history --keychain-profile release-notary --output-format json'])

    def test_team_filter_picks_one_of_several(self):
        (self.control / 'identities').write_text(listing(OTHER_TEAM, DEVELOPER_ID))
        self.assertEqual(json.loads(self.run_script('--profile', 'p', '--team-id', 'ZYXWV98765').stdout)['identity'],
                         OTHER_TEAM)
        result = self.run_script('--profile', 'p')
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (4, '{"schema_version":1,"error_code":"identity_ambiguous"}'))

    def test_refusals_are_fixed_codes(self):
        cases = [(listing('Apple Development: Synthetic Example (QQQQQ11111)'), '0', '0', 'identity_missing', 4),
                 (listing('Developer ID Application: Bad "Quote" (ABCDE12345)'), '0', '0', 'identity_unavailable', 3),
                 (listing('Developer ID Application: Bad $(x) (ABCDE12345)'), '0', '0', 'identity_unavailable', 3),
                 (listing(DEVELOPER_ID), '1', '0', 'identity_unavailable', 3),
                 (listing(DEVELOPER_ID), '0', '69', 'notary_profile_unavailable', 5)]
        for identities, security, history, code, status in cases:
            with self.subTest(code=code, identities=identities):
                (self.control / 'identities').write_text(identities)
                (self.control / 'security-exit').write_text(security)
                (self.control / 'history-exit').write_text(history)
                result = self.run_script('--profile', 'release-notary')
                self.assertEqual((result.returncode, result.stdout, result.stderr.strip()),
                                 (status, '', f'{{"schema_version":1,"error_code":"{code}"}}'))

    def test_usage_never_echoes_and_no_secret_flag_exists(self):
        for args in ((), ('--profile',), ('--profile', 'a b'), ('--profile', '-p'), ('--profile', 'x' * 65),
                     ('--profile', 'p', '--team-id', 'abcde12345'), ('--profile', 'p', '--password', 'Hunter2'),
                     ('--profile', 'p', '--profile', 'q')):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual((result.returncode, result.stderr.strip()), (2, '{"schema_version":1,"error_code":"usage"}'))
                self.assertNotIn('Hunter2', result.stdout + result.stderr)
        self.assertFalse((self.control / 'calls').exists())
        text = SCRIPT.read_text()
        for flag in ('--password', '--apple-id', '--key ', '--key-id', '--issuer', 'store-credentials'):
            self.assertNotIn(flag, text)


if __name__ == '__main__':
    unittest.main()
