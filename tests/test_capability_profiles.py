"""Capability profiles (0.54 item D): the closed table, the one-line policy edit, the gate and the restart."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

from telegram_search_mcp import bundle_install, capability_profiles as profiles
from telegram_search_mcp.config import CAPABILITY_PROFILES, _owner_paths, load_runtime_policy

POLICY = (b'# owner policy, written by connect\n'
          b'config_version = 1\n'
          b'enabled_capabilities = ["read", "artifacts"] # the default\n'
          b'expected_package_version = "0.56.0.dev0"\n'
          b'max_draft_ttl_seconds = 600\n')


class PlanTests(unittest.TestCase):
    def test_enable_adds_the_profile_and_what_it_depends_on(self):
        self.assertEqual(profiles.planned(('artifacts', 'read'), 'enable', 'replies'),
                         tuple(sorted({'artifacts', 'read', *CAPABILITY_PROFILES['replies'],
                                       *CAPABILITY_PROFILES['send']})))
        self.assertEqual(profiles.planned(('artifacts', 'read'), 'enable', 'readers'),
                         tuple(sorted({'artifacts', 'read', *CAPABILITY_PROFILES['readers']})))

    def test_disable_removes_the_profile_and_its_dependents_only(self):
        everything = tuple(sorted({'artifacts', 'read', *(c for m in CAPABILITY_PROFILES.values() for c in m)}))
        after = set(profiles.planned(everything, 'disable', 'send'))
        for name in ('send', 'replies', 'forward', 'video', 'archive_edit', 'albums', 'pin', 'delete'):
            self.assertFalse(after & set(CAPABILITY_PROFILES[name]), name)
        self.assertTrue(set(CAPABILITY_PROFILES['readers']) <= after and {'read', 'artifacts'} <= after)
        self.assertEqual(set(profiles.planned(everything, 'disable', 'video')),
                         set(everything) - set(CAPABILITY_PROFILES['video']))

    def test_unknown_profiles_and_actions_refuse(self):
        for action, profile in (('enable', 'everything'), ('grant', 'send'), ('enable', 'Send')):
            with self.subTest(action=action, profile=profile), self.assertRaises(profiles.ProfileError) as caught:
                profiles.planned(('read',), action, profile)
            self.assertEqual(caught.exception.code, 'policy_unsupported')

    def test_states_report_on_off_and_partial(self):
        self.assertEqual(profiles.states(('artifacts', 'read', 'send'))['send'], 'partial')
        self.assertEqual(profiles.states(('artifacts', 'read', *CAPABILITY_PROFILES['video']))['video'], 'on')
        self.assertEqual(profiles.states(('artifacts', 'read'))['readers'], 'off')


class RewriteTests(unittest.TestCase):
    def test_only_the_capability_line_changes_and_its_comment_stays(self):
        changed = profiles.rewrite(POLICY, ('artifacts', 'read', 'send'))
        self.assertEqual(changed, POLICY.replace(b'enabled_capabilities = ["read", "artifacts"] # the default',
                                                 b'enabled_capabilities = ["artifacts", "read", "send"] # the default'))

    def test_a_missing_repeated_or_multi_line_array_refuses(self):
        for data in (b'config_version = 1\n',
                     POLICY + b'enabled_capabilities = ["read"]\n',
                     b'config_version = 1\nenabled_capabilities = [\n  "read",\n]\n'):
            with self.subTest(data=data[-40:]), self.assertRaises(profiles.ProfileError):
                profiles.rewrite(data, ('read',))
        with self.assertRaises(profiles.ProfileError):
            profiles.rewrite(POLICY, ('read', 'not_a_capability'))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        root = _owner_paths(self.home)[0]
        root.mkdir(parents=True)
        for path in (root, *root.parents):
            if path == self.home:
                break
            path.chmod(0o700)
        root.chmod(0o700)
        self.path = root / 'runtime.toml'
        self.path.write_bytes(POLICY)
        self.path.chmod(0o600)
        self.calls = []

    def pending(self, answer):
        def probe():
            self.calls.append('pending')
            return answer
        return probe

    def restart(self, answer='restarted'):
        def restart():
            self.calls.append('restart')
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return restart

    def restartable(self, answer='loaded'):
        def check():
            self.calls.append('restartable')
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return check

    def apply(self, action, profile, pending='none', restartable='loaded', restart='restarted'):
        return profiles.apply(self.home, action, profile, pending=self.pending(pending),
                              restartable=self.restartable(restartable), restart=self.restart(restart))

    def test_enabling_changes_one_line_then_restarts(self):
        result = self.apply('enable', 'forward')
        self.assertEqual((result['status'], result['broker'], result['clients']), ('changed', 'restarted', 'reconnect'))
        self.assertEqual(self.calls, ['restartable', 'pending', 'restart'])
        self.assertNotIn('restart_command', result)
        self.assertEqual(result['profiles']['forward'], 'on')
        self.assertEqual(load_runtime_policy(self.path).enabled_capabilities,
                         ('artifacts', 'formatted_text_send', 'forward_send', 'read', 'send', 'topic_text_send'))
        data = self.path.read_bytes()
        self.assertEqual(data.replace(data.splitlines()[2], b''), POLICY.replace(POLICY.splitlines()[2], b''))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['runtime.toml'])

    def test_a_pending_or_unknown_send_refuses_and_changes_nothing(self):
        for answer in ('pending', 'unknown'):
            with self.subTest(answer=answer), self.assertRaises(profiles.ProfileError) as caught:
                self.apply('enable', 'video', pending=answer)
            self.assertEqual(caught.exception.code, 'pending_send_unresolved')
            self.assertEqual(self.path.read_bytes(), POLICY)
        self.assertNotIn('restart', self.calls)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['runtime.toml'])

    def test_an_edit_made_meanwhile_wins_and_refuses(self):
        def edit_meanwhile():
            self.path.write_bytes(POLICY.replace(b'600', b'700'))
            return 'none'
        with self.assertRaises(profiles.ProfileError) as caught:
            profiles.apply(self.home, 'enable', 'video', pending=edit_meanwhile, restartable=self.restartable(),
                           restart=self.restart())
        self.assertEqual(caught.exception.code, 'policy_conflict')
        self.assertEqual(self.path.read_bytes(), POLICY.replace(b'600', b'700'))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['runtime.toml'])

    def test_no_change_needs_neither_the_gate_nor_a_restart(self):
        result = self.apply('disable', 'video', pending='pending')
        self.assertEqual((result['status'], result['broker']), ('unchanged', 'unchanged'))
        self.assertEqual(self.calls, [])

    def test_a_broker_that_cannot_be_restarted_refuses_before_any_write(self):
        """service_conflict (another loaded or written definition) comes from the check before the write."""
        for answer in (bundle_install.InstallError('service_conflict'), bundle_install.InstallError('service_restart_failed'),
                       'odd', None):
            with self.subTest(answer=answer):
                self.calls.clear()
                with self.assertRaises((profiles.ProfileError, bundle_install.InstallError)) as caught:
                    self.apply('enable', 'video', restartable=answer)
                if isinstance(caught.exception, profiles.ProfileError):
                    self.assertEqual(caught.exception.code, 'service_conflict')
                self.assertEqual(self.path.read_bytes(), POLICY)
                self.assertEqual(self.calls, ['restartable'])
                self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['runtime.toml'])

    def test_a_stopped_broker_is_not_restarted_but_the_policy_changes(self):
        result = self.apply('enable', 'video', restartable='not_running', restart='not_running')
        self.assertEqual((result['status'], result['broker']), ('changed', 'not_running'))
        self.assertIn('video_send', load_runtime_policy(self.path).enabled_capabilities)

    def test_a_failed_restart_after_the_write_reports_the_change_and_the_manual_command(self):
        """Never a refusal once runtime.toml was replaced: status changed, broker restart_failed, the command."""
        label = _owner_paths(self.home)[2]
        for answer in ('odd', bundle_install.InstallError('service_conflict'),
                       bundle_install.InstallError('service_restart_failed'), OSError('launchctl'), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                self.path.write_bytes(POLICY)
                self.calls.clear()
                result = self.apply('enable', 'video', restart=answer)
                self.assertEqual((result['status'], result['broker'], result['clients']),
                                 ('changed', 'restart_failed', 'reconnect'))
                self.assertEqual(result['restart_command'],
                                 ['/bin/launchctl', 'kickstart', '-k', 'gui/%d/%s' % (os.getuid(), label)])
                self.assertEqual(result['profiles']['video'], 'on')
                self.assertIn('video_send', load_runtime_policy(self.path).enabled_capabilities)
                self.assertEqual(self.calls, ['restartable', 'pending', 'restart'])
                # A rerun finds nothing to change, so the manual command is the way to restart.
                self.assertEqual(self.apply('enable', 'video')['status'], 'unchanged')

    def test_the_manual_command_names_the_launchctl_the_installer_uses(self):
        from telegram_search_mcp.launch_agent import LAUNCHCTL
        self.assertEqual(profiles.LAUNCHCTL, LAUNCHCTL)

    def test_an_unsafe_or_foreign_policy_refuses(self):
        self.path.chmod(0o644)
        with self.assertRaises(profiles.ProfileError):
            self.apply('enable', 'video')
        self.path.chmod(0o600)
        self.path.write_bytes(POLICY + b'unknown_key = 1\n')
        with self.assertRaises(profiles.ProfileError):
            self.apply('enable', 'video')
        self.assertEqual(self.calls, [])

    def test_show_reports_profiles_without_writing(self):
        result = profiles.show(self.home)
        self.assertEqual((result['status'], result['enabled_capabilities']), ('ok', ['artifacts', 'read']))
        self.assertEqual(set(result['profiles'].values()), {'off'})
        self.assertEqual(self.path.read_bytes(), POLICY)


class RestartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        _, _, self.label, self.plist = _owner_paths(self.home)
        self.plist.parent.mkdir(parents=True)
        self.release = self.home / 'TelegramSearch' / 'releases' / ('a' * 64)
        for release in (self.release, self.release.parent / ('b' * 64)):
            (release / 'python' / 'bin').mkdir(parents=True)
            (release / 'python' / 'bin' / 'python3').write_bytes(b'')
        self.payload = bundle_install._service_payload(self.release, self.label)
        self.plist.write_bytes(plistlib.dumps(self.payload, sort_keys=True))
        self.plist.chmod(0o600)
        self.commands = []

    def runner(self, loaded=True, program=None):
        arguments = self.payload['ProgramArguments']
        text = '\n'.join([f'path = {self.plist}', f'program = {program or arguments[0]}', 'arguments = {',
                          *arguments, '}'])

        def run(argv, **options):
            self.commands.append(argv[1:3])
            if argv[1] == 'print':
                return subprocess.CompletedProcess(argv, 0 if loaded else 113, text if loaded else '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        return run

    def test_a_loaded_exact_broker_is_kickstarted(self):
        self.assertEqual(bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner()),
                         'restarted')
        self.assertEqual(self.commands[-1], ['kickstart', '-k'])

    def test_a_stopped_broker_is_left_alone(self):
        self.assertEqual(bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner(False)),
                         'not_running')
        self.assertEqual([c[0] for c in self.commands], ['print'])

    def test_the_check_before_the_write_never_kickstarts(self):
        self.assertEqual(bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner(),
                                                               kickstart=False), 'loaded')
        self.assertEqual(bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner(False),
                                                               kickstart=False), 'not_running')
        with self.assertRaises(bundle_install.InstallError) as caught:
            bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner(program='/usr/bin/python3'),
                                                  kickstart=False)
        self.assertEqual(str(caught.exception), 'service_conflict')
        self.assertEqual({c[0] for c in self.commands}, {'print'})

    def test_another_loaded_or_written_definition_refuses(self):
        with self.assertRaises(bundle_install.InstallError) as caught:
            bundle_install.restart_receipt_broker(self.release, self.home, runner=self.runner(program='/usr/bin/python3'))
        self.assertEqual(str(caught.exception), 'service_conflict')
        other = self.home / 'TelegramSearch' / 'releases' / ('b' * 64)
        with self.assertRaises(bundle_install.InstallError):
            bundle_install.restart_receipt_broker(other, self.home, runner=self.runner())
        self.assertNotIn(['kickstart', '-k'], self.commands)


def _launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location('launcher_profiles', Path(__file__).parents[1] / 'packaging/launcher.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


class LauncherCapabilitiesTests(unittest.TestCase):
    """launcher.capabilities: the check before the write and the restart after it reach apply; only a refusal
    raised before the write prints `refused`."""

    def run_capabilities(self, outcome):
        import contextlib, io, json
        from unittest import mock
        launcher, seen = _launcher(), {}

        def apply(home, action, profile, *, pending, restartable, restart):
            seen.update(action=action, profile=profile, restartable=restartable, restart=restart)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(profiles, 'apply', apply), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = launcher.capabilities(Path('/nonexistent/releases/' + 'a' * 64), ['enable', 'video'])
        return code, json.loads(out.getvalue() or err.getvalue()), seen

    def test_a_change_prints_its_result_even_when_the_restart_failed(self):
        result = {'status': 'changed', 'broker': 'restart_failed', 'restart_command': ['/bin/launchctl'],
                  'clients': 'reconnect', 'profiles': {}, 'enabled_capabilities': []}
        code, printed, seen = self.run_capabilities(result)
        self.assertEqual((code, printed), (0, result))
        self.assertEqual((seen['action'], seen['profile']), ('enable', 'video'))
        self.assertTrue(callable(seen['restartable']) and callable(seen['restart']))

    def test_a_refusal_before_the_write_prints_a_fixed_code(self):
        for error, code in ((bundle_install.InstallError('service_conflict'), 'service_conflict'),
                            (bundle_install.InstallError('service_restart_failed'), 'service_conflict'),
                            (bundle_install.InstallError('anything', 'else'), 'service_conflict'),
                            (profiles.ProfileError('pending_send_unresolved'), 'pending_send_unresolved')):
            with self.subTest(error=error):
                self.assertEqual(self.run_capabilities(error)[:2], (2, {'status': 'refused', 'code': code}))


class ManifestProfilesTests(unittest.TestCase):
    def test_the_manifest_lists_the_profiles_but_not_the_switch_command(self):
        import asyncio
        from telegram_search_mcp.config import PROFILE_DEPENDENCIES, RuntimePolicy
        from telegram_search_mcp.server import build_server
        server = build_server(policy=RuntimePolicy(), service_factory=lambda: None)
        result = asyncio.run(server.call_tool('_manifest', {}))
        manifest = result.structured_content if hasattr(result, 'structured_content') else result[1]
        table = manifest['capability_profiles']
        self.assertEqual(set(table), {'profiles', 'dependencies'})
        self.assertEqual(table['profiles'], {name: list(members) for name, members in CAPABILITY_PROFILES.items()})
        self.assertEqual(table['dependencies'], {name: list(needs) for name, needs in PROFILE_DEPENDENCIES.items()})
        self.assertNotIn('launcher', repr(manifest))
        self.assertNotIn('capabilities enable', repr(manifest))


class LauncherGrammarTests(unittest.TestCase):
    def test_the_launcher_spells_the_closed_profile_table(self):
        launcher = _launcher()
        self.assertEqual(launcher.PROFILES, tuple(CAPABILITY_PROFILES))
        for argv in (['capabilities'], ['capabilities', 'enable'], ['capabilities', 'enable', 'everything'],
                     ['capabilities', 'show', 'extra'], ['capabilities', 'grant', 'send']):
            with self.subTest(argv=argv):
                self.assertEqual(launcher.main(argv), 2)


if __name__ == '__main__':
    unittest.main()
