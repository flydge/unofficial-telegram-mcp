import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import tomllib
from telegram_search_mcp import setup_cli
from telegram_search_mcp import launch_agent
from telegram_search_mcp.enrollment import SetupPaths
from telegram_search_mcp.broker_client import BrokerCompatibilityError, BrokerUnavailable


class SetupCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = SetupPaths(Path(self.temp.name).resolve() / 'app')
        patcher = mock.patch.object(setup_cli, 'SetupPaths', return_value=self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
    def test_cli_entrypoint_exists(self):
        config = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text())
        self.assertEqual(config['project']['scripts']['telegram-search-setup'], 'telegram_search_mcp.setup_cli:main')
    def test_help_and_status_never_create_secret_store_or_native_or_launch(self):
        for args in [['--help'], ['status']]:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                paths = SetupPaths(Path(tmp).resolve() / 'app')
                with mock.patch.object(setup_cli, 'SetupPaths', return_value=paths), \
                     mock.patch.object(setup_cli, 'MacOSSecretStore') as store, \
                     mock.patch.object(setup_cli, 'LegacyTDJson') as raw, \
                     mock.patch.object(setup_cli, 'handoff_broker') as handoff, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(setup_cli.main(args), 0)
                store.assert_not_called()
                raw.assert_not_called()
                handoff.assert_not_called()
                self.assertFalse(paths.root.exists())
    def test_invalid_secret_bearing_arguments_are_never_echoed(self):
        sentinel = 'SYNTHETIC_SECRET_SENTINEL'
        for args in [['setup', '--api-hash', sentinel], [sentinel], ['resume', sentinel], ['--help', sentinel]]:
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output, \
                 contextlib.redirect_stderr(io.StringIO()) as errors, mock.patch.object(setup_cli, 'run_setup') as run:
                self.assertEqual(setup_cli.main(args), 2)
            self.assertNotIn(sentinel, output.getvalue() + errors.getvalue())
            run.assert_not_called()
    def test_setup_and_resume_refuse_actual_non_tty_before_credential_construction(self):
        for action in ['setup', 'resume']:
            with self.subTest(action=action), mock.patch.object(setup_cli, 'MacOSSecretStore') as store, \
                 mock.patch.object(setup_cli, 'LegacyTDJson') as raw, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(setup_cli.main([action]), 2)
                self.assertEqual(json.loads(output.getvalue())['code'], 'secure_terminal_required')
            store.assert_not_called()
            raw.assert_not_called()


    def test_documented_resume_command_recovers_add_only_partial_provisioning(self):
        from test_onboarding import MemoryStore, Terminal, AuthRaw
        store, terminal = MemoryStore(), Terminal()
        store.fail_account = 'api_hash'
        provider = mock.Mock(side_effect=lambda: AuthRaw(['authorizationStateReady'], paths=self.paths))
        with mock.patch.object(setup_cli, 'NativeTerminal', return_value=terminal), \
             mock.patch.object(setup_cli, 'MacOSSecretStore', return_value=store), \
             mock.patch.object(setup_cli, 'LegacyTDJson', provider), \
             mock.patch.object(setup_cli, 'handoff_broker', return_value='ready'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup_cli.main(['setup']), 2)
            first = json.loads(output.getvalue())
            self.assertEqual(first['next_action'], 'resume_in_foreground_native_terminal')
            self.assertEqual(first['code'], 'credential_denied')
            self.assertEqual(provider.call_count, 0)
            store.fail_account = None
            output.truncate(0)
            output.seek(0)
            self.assertEqual(setup_cli.main(['resume']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'ready')
        self.assertEqual(store.puts.count('api_id'), 1)
        self.assertEqual(store.puts.count('api_hash'), 2)
        self.assertEqual(provider.call_count, 1)


class InstalledHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.plist = self.root / 'agent.plist'
        self.paths = SetupPaths(self.root / 'app')
        self.calls = []
        # A forgotten synthetic path must fail before any home lock/socket call.
        patcher = mock.patch.object(setup_cli, 'SetupPaths', side_effect=AssertionError('explicit synthetic paths required'))
        patcher.start()
        self.addCleanup(patcher.stop)
    def tearDown(self): self.temp.cleanup()
    def runner(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 113 if args[1] == 'print' else 0, '', '')
    def test_absent_plist_can_bootstrap_without_bootout_or_kill(self):
        launch_agent.start_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                                           runner=self.runner)
        value = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(value, launch_agent.launch_agent_payload(python_executable=Path(sys.executable)))
        self.assertEqual([call[1] for call in self.calls], ['print', 'bootstrap'])
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)
    def test_different_existing_plist_is_preserved_without_launch_actions(self):
        self.plist.write_bytes(plistlib.dumps({'Label': 'foreign'}))
        before = self.plist.read_bytes()
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.start_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                                               runner=self.runner)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual(self.calls, [])
    def test_exact_existing_registration_is_reused_without_rewriting_or_restart(self):
        value = launch_agent.launch_agent_payload(python_executable=Path(sys.executable))
        self.plist.write_bytes(plistlib.dumps(value))
        self.plist.chmod(0o600)
        before = self.plist.stat().st_ino, self.plist.read_bytes()
        def runner(args, **kwargs):
            self.calls.append(args)
            text = ('path = ' + str(self.plist) + '\nprogram = ' + str(Path(sys.executable).absolute())
                    + '\narguments = {\n' + '\n'.join(value['ProgramArguments']) + '\n}\n')
            return subprocess.CompletedProcess(args, 0, text, '')
        launch_agent.start_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable), runner=runner)
        self.assertEqual((self.plist.stat().st_ino, self.plist.read_bytes()), before)
        self.assertEqual([call[1] for call in self.calls], ['print'])
    def test_exact_plist_with_different_live_registration_refuses_without_restart(self):
        self.plist.write_bytes(plistlib.dumps(launch_agent.launch_agent_payload(python_executable=Path(sys.executable))))
        self.plist.chmod(0o600)
        def runner(args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, 'program = /foreign/python', '')
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.start_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable), runner=runner)
        self.assertEqual([call[1] for call in self.calls], ['print'])
    def test_unknown_registration_or_bootstrap_failure_refuses_honestly(self):
        for status in [5, 113]:
            with self.subTest(status=status):
                def runner(args, **kwargs): return subprocess.CompletedProcess(args, status, '', '')
                with self.assertRaises(launch_agent.LaunchAgentError):
                    launch_agent.start_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable), runner=runner)
    def test_handoff_does_not_turn_launch_success_into_ready(self):
        for check, expected in [('blocked', 'broker_readiness_timeout'), ('error', 'broker_readiness_timeout'),
                                ('ready', 'ready')]:
            with self.subTest(check=check):
                client = mock.Mock()
                client.check.return_value = check
                with mock.patch.object(setup_cli, 'start_enrolled_broker'), \
                     mock.patch.object(setup_cli, 'BrokerClient', return_value=client), \
                     mock.patch.object(setup_cli, 'load_runtime_policy'):
                    self.assertEqual(setup_cli.handoff_broker(paths=self.paths, timeout=.001, sleeper=lambda _: None), expected)
                client.handshake.assert_called()
                self.assertTrue(client.close.called)
    def test_competing_owner_is_checked_without_starting_or_stopping_it(self):
        from telegram_search_mcp.session_owner import SessionOwnerLease
        client = mock.Mock()
        client.check.return_value = 'blocked'
        with SessionOwnerLease(self.paths.lock), mock.patch.object(setup_cli, 'start_enrolled_broker') as start, \
             mock.patch.object(setup_cli, 'BrokerClient', return_value=client), mock.patch.object(setup_cli, 'load_runtime_policy'), \
             mock.patch.object(setup_cli, 'validate_enrolled_broker', create=True):
            self.assertEqual(setup_cli.handoff_broker(paths=self.paths, timeout=.01), 'owner_busy')
        start.assert_not_called()
        client.handshake.assert_called_once()
    def test_competing_ready_requires_intended_registration_before_handshake(self):
        from telegram_search_mcp.session_owner import SessionOwnerLease
        for matching in [True, False]:
            with self.subTest(matching=matching):
                client, events = mock.Mock(), []
                client.check.return_value = 'ready'
                client.handshake.side_effect = lambda: events.append('handshake')
                def validate():
                    events.append('registration')
                    if not matching:
                        raise launch_agent.LaunchAgentError('foreign_registration')
                with SessionOwnerLease(self.paths.lock), mock.patch.object(setup_cli, 'start_enrolled_broker') as start, \
                     mock.patch.object(setup_cli, '_client', return_value=client), \
                     mock.patch.object(setup_cli, 'validate_enrolled_broker', side_effect=validate, create=True) as validated:
                    self.assertEqual(setup_cli.handoff_broker(paths=self.paths, timeout=.1),
                                     'ready' if matching else 'broker_incompatible')
                start.assert_not_called()
                validated.assert_called_once()
                self.assertEqual(events, ['registration', 'handshake'] if matching else ['registration'])
                if not matching: client.check.assert_not_called()

    def test_readonly_registration_validation_preserves_exact_definition_and_checks_live_metadata(self):
        value = launch_agent.launch_agent_payload(python_executable=Path(sys.executable))
        self.plist.write_bytes(plistlib.dumps(value))
        self.plist.chmod(0o600)
        before = self.plist.stat().st_ino, self.plist.read_bytes()
        text = ('path = ' + str(self.plist) + '\nprogram = ' + value['ProgramArguments'][0]
                + '\narguments = {\n' + '\n'.join(value['ProgramArguments']) + '\n}\n')
        def runner(args, **kwargs):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, text, '')
        launch_agent.validate_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable), runner=runner)
        self.assertEqual([call[1] for call in self.calls], ['print'])
        self.assertEqual((self.plist.stat().st_ino, self.plist.read_bytes()), before)
        for status, body in [(113, ''), (5, ''), (0, 'program = /foreign/python')]:
            with self.subTest(status=status, body=body), self.assertRaises(launch_agent.LaunchAgentError):
                launch_agent.validate_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable),
                    runner=lambda args, **kwargs: subprocess.CompletedProcess(args, status, body, ''))
        self.assertEqual((self.plist.stat().st_ino, self.plist.read_bytes()), before)

    def test_readonly_registration_validation_never_creates_missing_or_replaces_foreign_plist(self):
        absent = self.root / 'missing-directory' / 'agent.plist'
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.validate_enrolled_broker(plist_path=absent, python_executable=Path(sys.executable), runner=self.runner)
        self.assertFalse(absent.parent.exists())
        self.plist.write_bytes(plistlib.dumps({'Label': 'foreign'}))
        self.plist.chmod(0o600)
        before = self.plist.read_bytes()
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.validate_enrolled_broker(plist_path=self.plist, python_executable=Path(sys.executable), runner=self.runner)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual(self.calls, [])
    def test_incompatible_handshake_and_launch_failure_are_distinct(self):
        client = mock.Mock()
        client.handshake.side_effect = BrokerCompatibilityError('schema_mismatch')
        with mock.patch.object(setup_cli, 'start_enrolled_broker'), \
             mock.patch.object(setup_cli, 'BrokerClient', return_value=client), mock.patch.object(setup_cli, 'load_runtime_policy'):
            self.assertEqual(setup_cli.handoff_broker(paths=self.paths, timeout=.001), 'broker_incompatible')
        with mock.patch.object(setup_cli, 'start_enrolled_broker', side_effect=launch_agent.LaunchAgentError()):
            self.assertEqual(setup_cli.handoff_broker(paths=self.paths, timeout=.001), 'broker_start_failed')


class ClosedGrammarTests(unittest.TestCase):
    def test_new_commands_are_part_of_the_closed_grammar_and_nothing_else_is(self):
        self.assertEqual(setup_cli.COMMANDS, ('setup', 'resume', 'status', 'connect', 'wait', 'cancel',
                                              'api-credentials', 'connect-helper'))
        sentinel = 'SYNTHETIC_SECRET_SENTINEL'
        for args in [['connect', sentinel], ['wait', '5'], ['api-credentials', '--api-hash', sentinel],
                     ['connect-helper', sentinel], ['connect', 'wait'], ['CONNECT'], ['cancel', sentinel]]:
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output, \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(setup_cli.main(args), 2)
            self.assertEqual(json.loads(output.getvalue())['code'], 'invalid_arguments')
            self.assertNotIn(sentinel, output.getvalue() + errors.getvalue())
    def test_help_describes_connect_wait_and_api_credentials_without_the_internal_helper_contract(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup_cli.main(['--help']), 0)
        for word in ['connect', 'wait', 'cancel', 'api-credentials', '540']:
            self.assertIn(word, output.getvalue())
        self.assertNotIn('osascript', output.getvalue())
