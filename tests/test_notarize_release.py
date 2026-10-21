"""notarize_release.sh (CLIENTS-8) against mocked xcrun/notarytool/stapler, hdiutil, ditto, codesign, spctl.

The tools are injected without PATH: a copy of the script has exactly its tool block rewritten to
absolute mock paths. plutil and openssl stay the stock ones. No notary service, Keychain profile,
signing identity or disk image is touched. The dry run builds the payload zip and signs the DMG ad
hoc; only notarization and stapling are skipped.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from telegram_search_mcp.native_runtime import designated_requirement

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / 'packaging/notarize_release.sh'
TOOL_BLOCK = ('tool_xcrun=/usr/bin/xcrun\ntool_hdiutil=/usr/bin/hdiutil\ntool_ditto=/usr/bin/ditto\n'
              'tool_codesign=/usr/bin/codesign\ntool_spctl=/usr/sbin/spctl\ntool_plutil=/usr/bin/plutil\n'
              'tool_openssl=/usr/bin/openssl\n')
TEAM = 'ABCDE12345'
IDENTITY = f'Developer ID Application: Synthetic Example ({TEAM})'
REQUIREMENT = designated_requirement(TEAM)
PAYLOAD_ID, DMG_ID = '11111111-2222-3333-4444-555555555555', '66666666-7777-8888-9999-aaaaaaaaaaaa'
LOG = 'printf "%s" "$(basename "$0")" >> "$control/calls.log"; for a in "$@"; do printf " [%s]" "$a" >> "$control/calls.log"; done; echo >> "$control/calls.log"\n'
MOCKS = {
    'xcrun': r'''
case "$1 $2" in
    "notarytool submit")
        case "$3" in *.zip) id=PAYLOAD_ID;; *) id=DMG_ID;; esac
        printf '{"id":"%s","status":"%s","message":"done"}\n' "$id" "$(cat "$control/submit-status")";;
    "notarytool log")
        for last in "$@"; do :; done
        printf '{"status":"Accepted","issues":%s,"jobId":"%s"}\n' "$(cat "$control/issues")" "$3" > "$last";;
    "stapler staple") : > "$control/stapled";;
    "stapler validate") exit "$(cat "$control/validate-exit")";;
    *) exit 64;;
esac
''',
    'hdiutil': r'''
case "$1" in
    create)
        for last in "$@"; do :; done
        while [ "$#" -gt 0 ]; do [ "$1" = -srcfolder ] && kit=$2; shift; done
        printf '%s' "$kit" > "$last";;
    attach)
        for last in "$@"; do :; done
        while [ "$#" -gt 0 ]; do [ "$1" = -mountpoint ] && mount=$2; shift; done
        /bin/cp -p "$(cat "$last")"/* "$mount"/
        if [ -f "$control/tamper" ]; then chmod u+w "$mount/README.txt"; echo changed > "$mount/README.txt"; fi;;
    detach) for last in "$@"; do :; done; /bin/rm -f "$last"/*;;
    *) exit 64;;
esac
''',
    'ditto': 'for last in "$@"; do :; done\n[ -f "$control/ditto-fail" ] && exit 1\nprintf zip > "$last"\n',
    'codesign': r'''
case "$1" in
    --verify)
        [ -f "$control/verify-fail" ] && exit 1
        if [ -f "$control/verify-fail-stapled" ] && [ -f "$control/stapled" ]; then exit 1; fi;;
    -d) printf 'Executable=%s\nSignature=%s\nTeamIdentifier=not set\n' "$3" "$(cat "$control/signature")" >&2;;
esac
''',
    'spctl': '''
if [ -f "$control/spctl-bad" ]; then printf '%s: rejected\\nsource=Unnotarized Developer ID\\n' "$7" >&2; exit 3; fi
printf '%s: accepted\\nsource=Notarized Developer ID\\n' "$7" >&2
''',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class NotarizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.control = self.root / 'control'
        self.control.mkdir()
        for name, value in (('submit-status', 'Accepted'), ('issues', 'null'), ('validate-exit', '0'),
                            ('signature', 'adhoc')):
            (self.control / name).write_text(value)
        tools = self.root / 'tools'
        tools.mkdir()
        block = ''
        for name, body in MOCKS.items():
            path = tools / name
            body = body.replace('PAYLOAD_ID', PAYLOAD_ID).replace('DMG_ID', DMG_ID)
            path.write_text(f'#!/bin/sh\ncontrol="{self.control}"\n{LOG}{body}')
            path.chmod(0o700)
        text = SCRIPT.read_text()
        self.assertEqual(text.count(TOOL_BLOCK), 1)
        mocked = TOOL_BLOCK
        for name in MOCKS:
            mocked = mocked.replace(f'tool_{name}=/usr/bin/{name}\n', f'tool_{name}={tools / name}\n')
            mocked = mocked.replace(f'tool_{name}=/usr/sbin/{name}\n', f'tool_{name}={tools / name}\n')
        self.script = self.root / 'notarize_release.sh'
        self.script.write_text(text.replace(TOOL_BLOCK, mocked))
        self.kit = self.root / 'kit'
        self.kit.mkdir()
        for name in ('Install.command', 'bootstrap.sh', 'bootstrap-verifier', 'release.tar.gz', 'release-index.json',
                     'release-pins.json', 'README.txt'):
            (self.kit / name).write_bytes(name.encode() + b' synthetic')
            (self.kit / name).chmod(0o444)
        self.sums()
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.dmg = self.assets / 'product-1.2.3-macos-arm64.dmg'
        self.evidence = self.root / 'notary.json'
        self.payload = self.root / 'signed'
        self.payload.mkdir()

    def sums(self):
        rows = ''.join(f'{sha(p.read_bytes())}  {p.name}\n' for p in sorted(self.kit.iterdir()) if p.name != 'SHA256SUMS')
        (self.kit / 'SHA256SUMS').unlink(missing_ok=True)
        (self.kit / 'SHA256SUMS').write_text(rows)

    def run_script(self, *extra, dry=True):
        base = ['--kit', str(self.kit), '--dmg', str(self.dmg), '--volume-name', 'Synthetic Product',
                '--evidence', str(self.evidence), '--payload', str(self.payload)]
        if dry is None:  # Real mode with the caller's --identity.
            mode = ['--team-id', TEAM, '--profile', 'release-notary']
        else:
            mode = ['--dry-run'] if dry else ['--identity', IDENTITY, '--team-id', TEAM, '--profile', 'release-notary']
        return subprocess.run(['/bin/sh', '-p', str(self.script), *base, *mode, *extra], capture_output=True, text=True,
                              timeout=60, env={'PATH': '/usr/bin:/bin', 'HOME': str(self.root)})

    def calls(self):
        path = self.control / 'calls.log'
        return path.read_text().splitlines() if path.exists() else []

    def test_dry_run_builds_the_zip_and_an_ad_hoc_signed_dmg_and_touches_no_notary_tool(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        work = self.root / 'notary-notary'
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual(evidence, {'schema_version': 1, 'mode': 'dry-run', 'notarization': 'skipped',
                                    'stapling': 'skipped',
                                    'payload': {'zip_sha256': sha(b'zip'), 'zip_bytes': 3, 'notarization': 'skipped'},
                                    'dmg': {'file': self.dmg.name, 'sha256': sha(self.dmg.read_bytes()),
                                            'bytes': self.dmg.stat().st_size, 'contents': 'matches SHA256SUMS',
                                            'signature': 'adhoc', 'verified': True}})
        self.assertEqual(json.loads(result.stdout), evidence)
        self.assertEqual(self.calls(), [
            f'ditto [-c] [-k] [--keepParent] [{self.payload}] [{work}/payload.zip]',
            f'hdiutil [create] [-volname] [Synthetic Product] [-srcfolder] [{self.kit}] [-fs] [HFS+] [-format] [UDZO] '
            f'[{self.dmg}]',
            f'hdiutil [attach] [-readonly] [-nobrowse] [-noautoopen] [-mountpoint] [{work}/mount] [{self.dmg}]',
            f'hdiutil [detach] [{work}/mount]',
            f'codesign [--force] [--sign] [-] [{self.dmg}]',
            f'codesign [--verify] [--strict] [{self.dmg}]',
            f'codesign [-d] [--verbose=4] [{self.dmg}]'])
        self.assertFalse((work / 'mount').exists())
        self.assertFalse((work / 'payload.zip').exists())

    def test_dry_run_refuses_a_failed_zip_or_a_dmg_that_is_not_ad_hoc(self):
        def fresh():
            self.dmg.unlink(missing_ok=True)
            self.evidence.unlink(missing_ok=True)
            shutil.rmtree(self.root / 'notary-notary', ignore_errors=True)
            (self.control / 'calls.log').unlink(missing_ok=True)
        (self.control / 'ditto-fail').write_text('')
        self.refused('payload_zip_failed', 4)
        self.assertFalse(any(c.startswith('hdiutil') for c in self.calls()))
        (self.control / 'ditto-fail').unlink()
        for name, value in (('verify-fail', ''), ('signature', 'size=9000')):
            with self.subTest(name=name):
                fresh()
                previous = (self.control / name).read_text() if (self.control / name).exists() else None
                (self.control / name).write_text(value)
                self.refused('dmg_sign_failed', 5)
                self.assertFalse(self.evidence.exists())
                if previous is None:
                    (self.control / name).unlink()
                else:
                    (self.control / name).write_text(previous)

    def test_real_run_notarizes_payload_and_dmg_staples_and_checks_gatekeeper(self):
        result = self.run_script(dry=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        work = self.root / 'notary-notary'
        self.assertEqual(self.calls(), [
            f'ditto [-c] [-k] [--keepParent] [{self.payload}] [{work}/payload.zip]',
            f'xcrun [notarytool] [submit] [{work}/payload.zip] [--keychain-profile] [release-notary] [--wait] '
            '[--output-format] [json]',
            f'xcrun [notarytool] [log] [{PAYLOAD_ID}] [--keychain-profile] [release-notary] [{work}/payload-log.json]',
            f'hdiutil [create] [-volname] [Synthetic Product] [-srcfolder] [{self.kit}] [-fs] [HFS+] [-format] [UDZO] '
            f'[{self.dmg}]',
            f'hdiutil [attach] [-readonly] [-nobrowse] [-noautoopen] [-mountpoint] [{work}/mount] [{self.dmg}]',
            f'hdiutil [detach] [{work}/mount]',
            f'codesign [--force] [--timestamp] [--sign] [{IDENTITY}] [{self.dmg}]',
            f'codesign [--verify] [--strict] [-R] [{REQUIREMENT}] [{self.dmg}]',
            f'xcrun [notarytool] [submit] [{self.dmg}] [--keychain-profile] [release-notary] [--wait] '
            '[--output-format] [json]',
            f'xcrun [notarytool] [log] [{DMG_ID}] [--keychain-profile] [release-notary] [{work}/dmg-log.json]',
            f'xcrun [stapler] [staple] [{self.dmg}]',
            f'xcrun [stapler] [validate] [{self.dmg}]',
            f'codesign [--verify] [--strict] [-R] [{REQUIREMENT}] [{self.dmg}]',
            f'spctl [-a] [-vv] [-t] [open] [--context] [context:primary-signature] [{self.dmg}]'])
        self.assertFalse((work / 'payload.zip').exists())
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual(evidence['payload'], {'zip_sha256': sha(b'zip'), 'zip_bytes': 3, 'submission_id': PAYLOAD_ID,
                                               'status': 'Accepted', 'issues': 0})
        self.assertEqual(evidence['dmg']['submission_id'], DMG_ID)
        self.assertEqual((evidence['dmg']['team_id'], evidence['dmg']['requirement']), (TEAM, 'developer-id-team'))
        self.assertEqual(evidence['dmg']['sha256'], sha(self.dmg.read_bytes()))
        self.assertEqual((evidence['mode'], evidence['stapler_validate'], evidence['spctl']),
                         ('notarized', 'ok', 'accepted: Notarized Developer ID'))
        (self.control / 'issues').write_text('[]')
        self.evidence.unlink()
        self.dmg.unlink()
        shutil.rmtree(work)
        self.assertEqual(self.run_script(dry=False).returncode, 0)

    def test_a_certificate_sha1_identity_is_accepted_with_its_team(self):
        result = self.run_script('--identity', '0123456789ABCDEF0123456789ABCDEF01234567', dry=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'codesign [--force] [--timestamp] [--sign] [0123456789ABCDEF0123456789ABCDEF01234567] '
                      f'[{self.dmg}]', self.calls())

    def test_only_a_keychain_profile_ever_reaches_notarytool(self):
        text = SCRIPT.read_text()
        for flag in ('--password', '--apple-id', '--key ', '--key-id', '--issuer', 'store-credentials', 'NOTARY_'):
            self.assertNotIn(flag, text)
        self.run_script(dry=False)
        for call in self.calls():
            if call.startswith('xcrun [notarytool]'):
                self.assertIn('[--keychain-profile] [release-notary]', call)

    def refused(self, code, status, *extra, dry=True):
        result = self.run_script(*extra, dry=dry)
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (status, f'{{"schema_version":1,"error_code":"{code}"}}'))
        return result

    def test_notary_and_gatekeeper_refusals(self):
        def fresh():
            self.dmg.unlink(missing_ok=True)
            shutil.rmtree(self.root / 'notary-notary', ignore_errors=True)
            (self.control / 'calls.log').unlink(missing_ok=True)
            (self.control / 'stapled').unlink(missing_ok=True)
        fresh()
        (self.control / 'verify-fail').write_text('')  # Another team or no Developer ID: before any DMG submission.
        self.refused('dmg_sign_failed', 5, dry=False)
        self.assertFalse(any('notarytool] [submit] [' + str(self.dmg) in c for c in self.calls()))
        (self.control / 'verify-fail').unlink()
        fresh()
        (self.control / 'verify-fail-stapled').write_text('')  # The stapled DMG is checked again before spctl.
        self.refused('gatekeeper_refused', 8, dry=False)
        self.assertFalse(any(c.startswith('spctl') for c in self.calls()))
        (self.control / 'verify-fail-stapled').unlink()
        cases = [('submit-status', 'Invalid', 'notarization_rejected', 6),
                 ('issues', '[{"severity":"error","path":"x"}]', 'notarization_rejected', 6),
                 ('validate-exit', '65', 'staple_failed', 7)]
        for name, value, code, status in cases:
            with self.subTest(code=code, name=name):
                fresh()
                previous = (self.control / name).read_text()
                (self.control / name).write_text(value)
                self.refused(code, status, dry=False)
                (self.control / name).write_text(previous)
                self.assertFalse(self.evidence.exists())
        fresh()
        (self.control / 'spctl-bad').write_text('')
        self.refused('gatekeeper_refused', 8, dry=False)
        (self.control / 'spctl-bad').unlink()
        fresh()
        (self.control / 'tamper').write_text('')
        self.refused('dmg_mismatch', 4, dry=False)
        self.assertFalse(any('notarytool] [submit] [' + str(self.dmg) in c for c in self.calls()))
        self.assertTrue(self.calls()[-1].startswith('hdiutil [detach] [-force]'))  # the trap detaches

    def test_kit_refusals(self):
        (self.kit / 'extra.txt').write_text('unlisted')
        self.refused('kit_mismatch', 3)
        (self.kit / 'extra.txt').unlink()
        os.chmod(self.kit / 'README.txt', 0o644)
        (self.kit / 'README.txt').write_text('edited')
        self.refused('kit_mismatch', 3)
        self.sums()
        (self.kit / 'release-pins.json').unlink()
        self.sums()
        self.refused('kit_incomplete', 3)
        self.assertEqual(self.calls(), [])

    def test_a_path_with_a_newline_is_refused_like_any_control_byte(self):
        # grep reads lines, so each half of 'kit\nnext' alone holds no control byte: the case pattern refuses it.
        for name in ('kit\nnext', 'kit\tnext'):
            with self.subTest(name=name):
                folder = self.root / name
                shutil.copytree(self.kit, folder)
                self.kit = folder
                self.refused('usage', 2)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.dmg.exists())

    def test_usage_refusals_never_echo_arguments(self):
        secretish = 'Hunter2-' + 'x' * 8
        real = {'--kit': str(self.kit), '--dmg': str(self.dmg), '--volume-name': 'Synthetic Product',
                '--evidence': str(self.evidence), '--payload': str(self.payload), '--identity': IDENTITY,
                '--team-id': TEAM, '--profile': 'release-notary'}
        cases = [({'--profile': secretish + ' y'}, []), ({'--profile': '-x'}, []),
                 ({'--identity': 'Apple Development: X (ABCDE12345)'}, []),
                 ({'--identity': 'Developer ID Application: X (abcde12345)'}, []),
                 ({'--identity': 'Developer ID Application: X (ZYXWV98765)'}, []),  # not the --team-id
                 ({'--team-id': None}, []), ({'--team-id': 'abcde12345'}, []), ({'--team-id': 'ABCDE1234'}, []),
                 ({'--identity': '0123456789abcdef0123456789abcdef01234567'}, []),
                 ({'--identity': secretish + ' $(id)'}, []), ({'--dmg': 'relative.dmg'}, []),
                 ({'--dmg': str(self.assets / 'x.zip')}, []), ({'--volume-name': ' leading'}, []),
                 ({'--evidence': str(self.root / 'notary.txt')}, []), ({'--kit': '/tmp/kit'}, []),
                 ({'--payload': None}, []), ({}, ['--unknown', secretish]), ({}, ['--kit', str(self.kit)])]
        for override, extra in cases:
            options = {**real, **override}
            argv = [x for key, value in options.items() if value is not None for x in (key, value)]
            with self.subTest(override=override, extra=extra):
                result = subprocess.run(['/bin/sh', '-p', str(self.script), *argv, *extra], capture_output=True,
                                        text=True, timeout=60, env={'PATH': '/usr/bin:/bin'})
                self.assertEqual((result.returncode, result.stderr.strip()),
                                 (2, '{"schema_version":1,"error_code":"usage"}'))
                self.assertNotIn(secretish, result.stdout + result.stderr)
        self.dmg.write_bytes(b'exists')
        self.refused('usage', 2)
        self.dmg.unlink()
        self.refused('usage', 2, '--profile', 'release-notary')  # --dry-run takes no notary input
        self.refused('usage', 2, '--team-id', TEAM)
        dry = ['--kit', str(self.kit), '--dmg', str(self.dmg), '--volume-name', 'Synthetic Product',
               '--evidence', str(self.evidence), '--dry-run']  # The dry run needs the payload too.
        result = subprocess.run(['/bin/sh', '-p', str(self.script), *dry], capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin'})
        self.assertEqual((result.returncode, result.stderr.strip()), (2, '{"schema_version":1,"error_code":"usage"}'))
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
