"""0.55 phase A: every constructor and call site that defaulted to an import-time path, label or socket
takes an AccountPaths. None stays the default alias (byte-identical 0.54 values); an explicit account
reaches every place it names. Named accounts come only from the explicit test hook.
"""
from __future__ import annotations

import ast
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_search_mcp import (artifact_store, broker_client, config, connect_cli, connect_flow, launch_agent,
                                 local_upload, media_analyzer, outgoing_stage, server, setup_cli, tdjson,
                                 transfer_cli)
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import DEFAULT_ACCOUNT_PATHS, AccountPaths, RuntimePolicy
from telegram_search_mcp.enrollment import SetupPaths
from telegram_search_mcp.local_upload import LocalUploadRegistry

DEFAULT = DEFAULT_ACCOUNT_PATHS
POLICY = RuntimePolicy(enabled_capabilities=('artifacts', 'read'))
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HOME = '/fixture-home/sample-owner'
BUNDLE_LAUNCHER = '/fixture/bundle/bin/telegram-search-mcp'


def payloads_054(python):
    """The launchd payloads 0.54 (7f67cba) writes for HOME=/fixture-home/sample-owner, recorded from that tree:
    source mode with `python` as the interpreter, and bundle mode with the fixture launcher."""
    common = {'LimitLoadToSessionType': 'Aqua', 'RunAtLoad': True, 'StandardErrorPath': '/dev/null',
              'StandardOutPath': '/dev/null', 'Umask': 63}
    broker = {**common, 'KeepAlive': True, 'Label': 'com.sample-owner.telegram-search-mcp.broker',
              'ProcessType': 'Background'}
    connect = {**common, 'KeepAlive': False, 'Label': 'com.sample-owner.telegram-search-mcp.connect',
               'ProcessType': 'Interactive', 'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}}
    return {
        'broker': {**broker, 'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'},
                   'ProgramArguments': [python, '-m', 'telegram_search_mcp.launch_agent', 'run']},
        'connect': {**connect, 'ProgramArguments': [python, '-m', 'telegram_search_mcp.setup_cli', 'connect-helper']},
        'broker_bundled': {**broker, 'EnvironmentVariables': {}, 'ProgramArguments': [BUNDLE_LAUNCHER, 'broker']},
        'connect_bundled': {**connect, 'ProgramArguments': [BUNDLE_LAUNCHER, 'connect-helper']},
    }


# Renders the payloads in a fresh process, so config derives every name from the fixture HOME at import.
RENDER = '''
import json, plistlib, sys
from pathlib import Path
from unittest import mock
from telegram_search_mcp import bundle_runtime, launch_agent


class Selection:
    def arguments(self, kind):
        return [sys.argv[2], kind]


def render(suffix):
    python = Path(sys.argv[1])
    return {"broker" + suffix: launch_agent.launch_agent_payload(python_executable=python),
            "connect" + suffix: launch_agent.connect_job_payload(python_executable=python)}


payloads = render("")
with mock.patch.object(bundle_runtime, "_SELECTION", Selection()):
    payloads.update(render("_bundled"))
print(json.dumps({name: plistlib.dumps(payload, sort_keys=True).decode() for name, payload in payloads.items()}))
'''


def launchctl(calls, code=113):
    def run(arguments, **kwargs):
        calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, code, '', '')
    return run


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / 'home'
        with config.named_aliases_for_tests():
            self.work = AccountPaths._derive('work', self.home)


class DefaultsStayTests(Fixture):
    """account None keeps the 0.54 values everywhere."""

    def test_re_exported_constants_are_the_default_alias(self):
        self.assertIs(artifact_store.CACHE_DIRECTORY, config.CACHE_DIRECTORY)
        self.assertIs(local_upload.UPLOAD_ROOT, config.UPLOAD_ROOT)
        self.assertIs(outgoing_stage.STAGING_ROOT, config.STAGING_ROOT)
        self.assertIs(tdjson.STAGING_ROOT, config.STAGING_ROOT)
        self.assertIs(launch_agent.CONNECT_JOB_LABEL, config.CONNECT_JOB_LABEL)
        self.assertIs(transfer_cli.CACHE_DIRECTORY, config.CACHE_DIRECTORY)
        self.assertIs(transfer_cli.BROKER_SOCKET_PATH, config.BROKER_SOCKET_PATH)
        self.assertIs(server.BROKER_SOCKET_PATH, config.BROKER_SOCKET_PATH)
        self.assertEqual((config.CACHE_DIRECTORY, config.UPLOAD_ROOT, config.STAGING_ROOT, config.CONNECT_JOB_LABEL),
                         (DEFAULT.artifacts, DEFAULT.uploads, DEFAULT.staging, DEFAULT.connect_label))
        # One speech model for every account, where 0.54 kept it.
        self.assertEqual(media_analyzer.DEFAULT_MODEL_PATH, config.APP_SUPPORT_ROOT / 'models' / 'ggml-small.bin')

    def test_stores_default_to_the_default_alias_and_derive_its_quarantine_siblings(self):
        store = ArtifactStore()
        self.assertEqual((store.cache_dir, store.quarantine_dir), (DEFAULT.artifacts, DEFAULT.artifacts_quarantine))
        self.assertEqual(LocalUploadRegistry(store)._root, DEFAULT.uploads)
        moved = ArtifactStore(cache_dir=self.work.artifacts)
        self.assertEqual(moved.quarantine_dir, self.work.artifacts_quarantine)
        uploads = self.work.uploads
        self.assertEqual(uploads.with_name(uploads.name + '-quarantine'), self.work.uploads_quarantine)
        staging = self.work.staging
        self.assertEqual(staging.with_name(staging.name + '-quarantine'), self.work.staging_quarantine)

    def test_default_broker_keeps_every_054_default(self):
        broker = Broker(socket_path=self.home / 'broker.sock', policy=POLICY, verify_peer_uid=False)
        self.addCleanup(broker.shutdown)
        self.assertEqual(broker._artifact_store.cache_dir, config.CACHE_DIRECTORY)
        self.assertEqual(broker._uploads._root, config.UPLOAD_ROOT)
        self.assertEqual(broker._download_source_root, config.TDLIB_SESSION_DIRECTORY / 'files')
        self.assertEqual(broker._staging, {})
        self.assertEqual(broker._client_factory, tdjson.TDLibClient.from_defaults)

    def test_default_launchd_payloads_are_the_literal_054_bytes(self):
        python = self.home.parent / 'python3'
        python.write_bytes(b'')
        rendered = subprocess.run(
            [sys.executable, '-B', '-c', RENDER, str(python), BUNDLE_LAUNCHER], capture_output=True, text=True,
            timeout=60, check=True, cwd=self.home.parent,
            env={'HOME': FIXTURE_HOME, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
        written = json.loads(rendered.stdout)
        golden = payloads_054(str(python))
        self.assertEqual(set(written), set(golden))
        for name, payload in golden.items():
            with self.subTest(payload=name):  # The bytes plistlib.dump(..., sort_keys=True) writes to the plist.
                self.assertEqual(written[name], plistlib.dumps(payload, sort_keys=True).decode())

    def test_default_launchd_payloads_are_identical_with_or_without_the_default_account(self):
        python = Path(sys.executable)
        self.assertEqual(plistlib.dumps(launch_agent.launch_agent_payload(python_executable=python)),
                         plistlib.dumps(launch_agent.launch_agent_payload(python_executable=python, account=DEFAULT)))
        self.assertEqual(plistlib.dumps(launch_agent.connect_job_payload(python_executable=python)),
                         plistlib.dumps(launch_agent.connect_job_payload(python_executable=python, account=DEFAULT)))
        self.assertEqual(launch_agent.launch_agent_payload(python_executable=python)['Label'], config.LAUNCH_AGENT_LABEL)
        self.assertEqual(launch_agent.account_options(DEFAULT), {})
        self.assertEqual(launch_agent.account_options(DEFAULT.relocated(self.home)), {})
        self.assertEqual(launch_agent.account_options(self.work), {'account': self.work})

    def test_tdlib_client_defaults(self):
        client = tdjson.TDLibClient(raw=mock.Mock())
        self.assertEqual((client._session_directory, client._staging_root), (config.TDLIB_SESSION_DIRECTORY, None))


class SetupPathsTests(Fixture):
    def test_setup_paths_derive_from_the_default_alias(self):
        paths = SetupPaths()
        self.assertEqual(paths, SetupPaths(config.APP_SUPPORT_ROOT))
        self.assertEqual(paths, SetupPaths.from_account(DEFAULT))
        self.assertIs(type(SetupPaths.from_account(DEFAULT)), SetupPaths)
        self.assertEqual((paths.root, paths.session, paths.lock, paths.policy, paths.record),
                         (config.APP_SUPPORT_ROOT, config.TDLIB_SESSION_DIRECTORY, config.BROKER_LOCK_PATH,
                          config.RUNTIME_POLICY_PATH, config.APP_SUPPORT_ROOT / 'enrollment.json'))
        self.assertEqual(paths.account, DEFAULT)
        self.assertEqual(repr(paths), f'SetupPaths(root={config.APP_SUPPORT_ROOT!r})')

    def test_an_explicit_root_is_the_inert_test_boundary_with_default_names(self):
        root = self.home / 'app'
        paths = SetupPaths(root)
        self.assertEqual((paths.session, paths.lock, paths.policy, paths.record),
                         (root / 'tdlib', root / 'run' / 'broker.lock', root / 'runtime.toml', root / 'enrollment.json'))
        self.assertEqual(paths.account, DEFAULT.relocated(root))
        self.assertEqual((paths.account.broker_label, paths.account.connect_label, paths.account.broker_plist),
                         (config.LAUNCH_AGENT_LABEL, config.CONNECT_JOB_LABEL, config.LAUNCH_AGENT_PLIST))
        # The names every 0.54 caller rebuilt from paths.lock.parent.
        self.assertEqual(connect_cli.job_plist_path(paths), paths.lock.parent / (config.CONNECT_JOB_LABEL + '.plist'))
        self.assertEqual(connect_cli.connect_lock_path(paths), paths.lock.parent / 'connect.lock')
        self.assertEqual(connect_flow.connect_status_path(paths), paths.lock.parent / 'connect-status.json')
        self.assertEqual(paths.account.socket, paths.lock.parent / 'broker.sock')

    def test_a_named_account_derives_its_own_setup_paths(self):
        paths = SetupPaths.from_account(self.work)
        self.assertIs(type(paths), SetupPaths)
        self.assertEqual((paths.root, paths.session, paths.lock, paths.policy, paths.record),
                         (self.work.root, self.work.session, self.work.broker_lock, self.work.policy,
                          self.work.enrollment_record))
        self.assertEqual(connect_cli.job_plist_path(paths), self.work.connect_plist)
        self.assertEqual(connect_cli.connect_lock_path(paths), self.work.connect_lock)
        self.assertEqual(connect_flow.connect_status_path(paths), self.work.connect_status)

    def test_only_genuine_account_paths_are_carried(self):
        for impostor in (object(), {'root': self.home}, mock.Mock(spec=AccountPaths)):
            with self.subTest(impostor=type(impostor).__name__), self.assertRaises(TypeError):
                SetupPaths(self.home, impostor)

    def test_exact_type_checks_accept_the_derived_form_and_still_refuse_a_subclass(self):
        from telegram_search_mcp.setup_wizard import run_setup

        class Refusing:
            def require_secure(self):
                raise RuntimeError('not native')

        class Imitation(SetupPaths):
            pass

        def code(paths):
            return run_setup(paths, Refusing(), mock.Mock(), mock.Mock(side_effect=AssertionError), handoff=None).code
        self.assertEqual(code(SetupPaths.from_account(self.work)), 'secure_terminal_required')
        self.assertEqual(code(SetupPaths.from_account(DEFAULT)), 'secure_terminal_required')
        self.assertEqual(code(Imitation(self.home)), 'invalid_input')


class BrokerThreadingTests(Fixture):
    def test_an_explicit_account_reaches_every_broker_root(self):
        broker = Broker(socket_path=self.work.socket, lock_path=self.work.broker_lock, account=self.work,
                        verify_peer_uid=False)
        self.addCleanup(broker.shutdown)
        self.assertEqual(broker._policy.source_path, self.work.policy)  # Absent file: the default policy, bound here.
        self.assertEqual(broker._artifact_store.cache_dir, self.work.artifacts)
        self.assertEqual(broker._uploads._root, self.work.uploads)
        self.assertEqual(broker._download_source_root, self.work.files)
        self.assertEqual(broker._staging, {'root': self.work.staging})
        with mock.patch.object(tdjson.TDLibClient, 'from_defaults', return_value='client') as factory:
            self.assertEqual(broker._client_factory(), 'client')
        factory.assert_called_once_with(self.work)

    def test_every_broker_staging_call_carries_the_account_root(self):
        from telegram_search_mcp import broker as broker_module
        tree = ast.parse(Path(broker_module.__file__).read_text(encoding='utf-8'))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id in {'stage_approved_document', 'retire_staged_document'}]
        self.assertEqual(len(calls), 4)
        for call in calls:  # Empty for the default alias, so its 0.54 one-argument call stays.
            self.assertEqual([ast.unparse(keyword) for keyword in call.keywords], ['**self._staging'])

    def test_run_broker_binds_the_account_socket_lock_and_roots(self):
        with mock.patch.object(launch_agent, 'Broker') as constructed, \
                mock.patch.object(launch_agent, 'validate_tdlib_runtime'), \
                mock.patch.object(launch_agent.signal, 'signal'):
            self.assertEqual(launch_agent.run_broker(self.work), 0)
            launch_agent.run_broker()
        self.assertEqual(constructed.call_args_list, [
            mock.call(socket_path=self.work.socket, lock_path=self.work.broker_lock, account=self.work),
            mock.call(socket_path=config.BROKER_SOCKET_PATH, lock_path=config.BROKER_LOCK_PATH)])

    def test_tdlib_client_from_an_account_uses_its_session_staging_and_enrollment(self):
        loader = mock.Mock(return_value=(b'k' * 32, lambda: None))
        # from_defaults sets the broker's process umask; the real call would leak it into every later test.
        with mock.patch('telegram_search_mcp.credential_identity.session_credential_loader', loader), \
                mock.patch.object(tdjson, 'LegacyTDJson'), mock.patch.object(tdjson.os, 'umask') as umask:
            client = tdjson.TDLibClient.from_defaults(self.work)
            default = tdjson.TDLibClient.from_defaults()
        self.assertEqual(umask.call_args_list, [mock.call(0o077)] * 2)
        self.assertEqual(loader.call_args_list, [mock.call(SetupPaths.from_account(self.work)), mock.call()])
        self.assertEqual((client._session_directory, client._staging_root), (self.work.session, self.work.staging))
        self.assertEqual((default._session_directory, default._staging_root), (config.TDLIB_SESSION_DIRECTORY, None))

    def test_staged_input_files_must_lie_in_the_clients_own_staging_root(self):
        staged = {'@type': 'inputFileLocal', 'path': str(self.work.staging / ('draft_' + 'a' * 32) / 'report.txt')}
        self.assertTrue(tdjson._valid_staged_input_file(staged, self.work.staging))
        self.assertFalse(tdjson._valid_staged_input_file(staged, config.STAGING_ROOT))
        default = {**staged, 'path': str(config.STAGING_ROOT / ('draft_' + 'a' * 32) / 'report.txt')}
        self.assertFalse(tdjson._valid_staged_input_file(default, self.work.staging))


class ProxyThreadingTests(Fixture):
    def test_broker_client_takes_the_account_policy_and_restarts_only_its_broker(self):
        client = broker_client.BrokerClient(socket_path=self.work.socket, account=self.work)
        self.assertEqual(client._policy.source_path, self.work.policy)
        default = broker_client.BrokerClient(socket_path=config.BROKER_SOCKET_PATH, policy=POLICY)
        self.assertIs(default._restart_callback, broker_client._request_launchd_restart)
        with mock.patch.object(launch_agent, 'start_launch_agent_if_stopped') as restart:
            client._restart_callback()
            default._restart_callback()
        self.assertEqual(restart.call_args_list, [mock.call(account=self.work), mock.call(account=None)])
        calls = []
        launch_agent.start_launch_agent_if_stopped(runner=launchctl(calls, 0), uid=501, account=self.work)
        launch_agent.start_launch_agent_if_stopped(runner=launchctl(calls, 0), uid=501)
        self.assertEqual(calls, [[launch_agent.LAUNCHCTL, 'kickstart', 'gui/501/' + self.work.broker_label],
                                 [launch_agent.LAUNCHCTL, 'kickstart', 'gui/501/' + config.LAUNCH_AGENT_LABEL]])

    def test_server_check_and_policy_follow_the_account(self):
        built = server.build_server(account=self.work, service_factory=lambda: self.fail('provider opened'))
        self.assertEqual(built.policy.source_path, self.work.policy)
        made = []

        class Ready:
            def __init__(self, **kwargs):
                made.append(kwargs)

            def check(self):
                return 'ready'

            def close(self):
                pass

        with mock.patch.object(server, 'BrokerClient', Ready), \
                mock.patch.object(server, 'bundled_runtime_pin', return_value='bundled'), \
                mock.patch('sys.stderr'):
            self.assertEqual(server._run_check(self.work), 0)
            self.assertEqual(server._run_check(), 0)
        self.assertEqual(made, [{'socket_path': self.work.socket, 'account': self.work},
                                {'socket_path': config.BROKER_SOCKET_PATH}])

    def test_transfer_client_and_download_cache_follow_the_account(self):
        client = transfer_cli._make_client(self.work)
        self.assertEqual((client._socket_path, client._policy.source_path), (self.work.socket, self.work.policy))
        opened = []

        class Stop(Exception):
            pass

        def pinned(path, code):
            opened.append(path)
            raise Stop()
        with mock.patch.object(transfer_cli, '_PinnedDirectory', side_effect=pinned):
            for cache in (self.work.artifacts, None):
                with self.assertRaises(Stop):
                    transfer_cli._PinnedArtifact('artifact_x', 1, cache)
        self.assertEqual(opened, [str(self.work.artifacts), str(config.CACHE_DIRECTORY)])


class LaunchdThreadingTests(Fixture):
    def test_named_account_jobs_use_their_own_labels_and_plists(self):
        self.assertEqual(launch_agent._broker_job(self.work), (self.work.broker_label, self.work.broker_plist))
        self.assertEqual(launch_agent._connect_label(self.work), self.work.connect_label)
        self.assertEqual(launch_agent._broker_job(None), (config.LAUNCH_AGENT_LABEL, config.LAUNCH_AGENT_PLIST))
        self.assertEqual(launch_agent._connect_label(None), config.CONNECT_JOB_LABEL)
        calls = []
        self.assertEqual(launch_agent.connect_job_state(runner=launchctl(calls), uid=501, account=self.work), 'absent')
        self.assertFalse(launch_agent.launch_agent_status(runner=launchctl(calls), uid=501, account=self.work))
        self.assertEqual(calls, [[launch_agent.LAUNCHCTL, 'print', 'gui/501/' + self.work.connect_label],
                                 [launch_agent.LAUNCHCTL, 'print', 'gui/501/' + self.work.broker_label]])
        # validate reads the account's own plist (absent here) before any launchctl call.
        self.work.broker_plist.parent.mkdir(parents=True, mode=0o700)
        calls.clear()
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.validate_enrolled_broker(runner=launchctl(calls), uid=501, account=self.work)
        self.assertEqual(calls, [])

    def test_no_job_is_written_for_a_named_account_before_phase_c(self):
        python = Path(sys.executable)
        self.work.broker_plist.parent.mkdir(parents=True, mode=0o700)
        # Phase B: the broker argv names the alias; writing and starting its jobs stay phase C's.
        self.assertEqual(launch_agent.launch_agent_payload(python_executable=python, account=self.work)
                         ['ProgramArguments'][-2:], ['--account', 'work'])
        for build in (lambda: launch_agent.connect_job_payload(python_executable=python, account=self.work),
                      lambda: launch_agent.start_enrolled_broker(python_executable=python, account=self.work,
                                                                 runner=launchctl([])),
                      lambda: launch_agent.write_connect_job(plist_path=self.work.connect_plist, account=self.work)):
            with self.assertRaises(launch_agent.LaunchAgentError):
                build()
        self.assertEqual(list(self.work.broker_plist.parent.iterdir()), [])
        self.assertFalse(self.work.connect_plist.exists())

    def test_cancel_and_handoff_address_the_account_jobs(self):
        paths = SetupPaths.from_account(self.work)
        calls = []
        self.assertEqual(connect_cli.cancel(paths, runner=launchctl(calls), uid=501), ({'status': 'nothing_to_cancel'}, 0))
        self.assertEqual(calls, [[launch_agent.LAUNCHCTL, 'print', 'gui/501/' + self.work.connect_label]])
        client = mock.Mock()
        client.check.return_value = 'ready'
        with mock.patch.object(setup_cli, 'start_enrolled_broker') as start, \
                mock.patch.object(setup_cli, 'BrokerClient', return_value=client) as constructed, \
                mock.patch.object(setup_cli, 'load_runtime_policy'):
            self.assertEqual(setup_cli.handoff_broker(paths=paths, timeout=5), 'ready')
        start.assert_called_once_with(account=self.work)
        self.assertEqual(constructed.call_args.kwargs['socket_path'], self.work.socket)


if __name__ == '__main__':
    unittest.main()
