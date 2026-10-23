"""0.55 phase B: a trailing `--account <alias>` on the launcher, setup, proxy, transfer and broker argv.

The alias is a closed token, never `default`, and the registry admits it before any directory is created;
without the flag every argv and behaviour is the default alias's. Refusals are fixed codes and never echo.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_search_mcp import (account_registry, bundle_runtime, contract, launch_agent, server, setup_cli,
                                 transfer_cli)
from telegram_search_mcp.config import AccountPaths, RuntimePolicy
from telegram_search_mcp.enrollment import SetupPaths

SENTINEL = 'SYNTHETICSENTINEL'
LAUNCHER = Path(__file__).resolve().parents[1] / 'packaging' / 'launcher.py'
# Grammar violations of the trailing flag: each must be refused before any admission or registry read.
OUTSIDE = [['status', '--account', 'default'], ['status', '--account', 'Work'], ['status', '--account', SENTINEL],
           ['status', '--account', 'a' * 13], ['status', '--account', '1work'], ['status', '--account', 'w-rk'],
           ['status', '--account', 'work\n'], ['status', '--account', ''], ['status', '--account'],
           ['status', '--account', 'work', 'x'], ['status', 'work'], ['--account', 'work', 'status'],
           ['status', '--account=work'], ['status', '--acc', 'work'], ['status', '-a', 'work']]


class Home(unittest.TestCase):
    """A short home whose registry lists `work`; Path.home() answers it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='g')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        account_registry.add_alias('work', self.home, longest_tool='t')
        patcher = mock.patch.object(Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = AccountPaths.for_alias('work', self.home)

    def snapshot(self):
        return sorted(str(path.relative_to(self.home)) for path in self.home.rglob('*'))

    def unregistered(self):
        """The same home with an empty registry: `work` is no longer admitted."""
        account_registry.remove_alias('work', self.home)


def run(main, args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            code = main(args)
        except SystemExit as exit:
            code = exit.code
    return code, out.getvalue(), err.getvalue()


class SetupGrammarTests(Home):
    def test_each_account_command_takes_the_flag_and_resolves_the_registered_alias(self):
        self.assertEqual(setup_cli.ACCOUNT_COMMANDS, ('setup', 'resume', 'status', 'connect', 'wait', 'cancel',
                                                      'api-credentials', 'connect-helper'))
        for command in setup_cli.ACCOUNT_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(setup_cli.account_argument([command, '--account', 'work']), ([command], 'work'))
                self.assertEqual(setup_cli.account_argument([command]), ([command], None))
        self.assertEqual(setup_cli.account_paths('work'), SetupPaths.from_account(self.work))

    def test_status_and_connect_commands_run_on_the_aliases_paths(self):
        seen = []
        with mock.patch.object(setup_cli, 'setup_status', side_effect=lambda paths: seen.append(paths) or
                               {'state': 'not_enrolled'}), \
                mock.patch.object(setup_cli, 'existing_broker_status', return_value='not_running'), \
                mock.patch.object(setup_cli, 'launch_agent_status', return_value=False) as loaded:
            code, out, _ = run(setup_cli.main, ['status', '--account', 'work'])
        self.assertEqual((code, json.loads(out)), (0, {'state': 'not_enrolled', 'broker': 'not_running',
                                                       'alias': 'work', 'loaded': False, 'authorized': None}))
        loaded.assert_called_once_with(account=self.work)
        self.assertEqual(seen, [SetupPaths.from_account(self.work)])
        for command in ('connect', 'wait', 'cancel', 'api-credentials'):
            with self.subTest(command=command), \
                    mock.patch.object(setup_cli, '_connect_command', return_value=0) as connect:
                self.assertEqual(run(setup_cli.main, [command, '--account', 'work'])[0], 0)
            connect.assert_called_once_with(command, SetupPaths.from_account(self.work))

    def test_setup_and_resume_resolve_the_alias_before_the_terminal_check(self):
        for command in ('setup', 'resume'):
            terminal = mock.Mock()
            terminal.require_secure.side_effect = RuntimeError('not a terminal')
            with self.subTest(command=command), mock.patch.object(setup_cli, 'NativeTerminal', return_value=terminal), \
                    mock.patch.object(setup_cli, 'run_setup') as setup:
                code, out, _ = run(setup_cli.main, [command, '--account', 'work'])
            self.assertEqual((code, json.loads(out)['code']), (2, 'secure_terminal_required'))
            setup.assert_not_called()

    def test_flag_grammar_violations_are_invalid_arguments_and_never_echo(self):
        for args in [*OUTSIDE, ['connect-helper', '--account', 'default'], ['connect-helper', '--account']]:
            with self.subTest(args=args), mock.patch.object(setup_cli, 'SetupPaths') as paths:
                code, out, err = run(setup_cli.main, args)
            self.assertEqual((code, json.loads(out)['code']), (2, 'invalid_arguments'))
            self.assertNotIn(SENTINEL, out + err)
            paths.assert_not_called()

    def test_an_unregistered_alias_refuses_before_any_directory_exists(self):
        self.unregistered()
        before = self.snapshot()
        for command in setup_cli.ACCOUNT_COMMANDS:
            with self.subTest(command=command), mock.patch.object(setup_cli, 'setup_status') as status, \
                    mock.patch.object(setup_cli, '_connect_command') as connect:
                code, out, _ = run(setup_cli.main, [command, '--account', 'home'])
            self.assertEqual((code, json.loads(out)), (2, {'status': 'refused', 'code': 'alias_unavailable'}))
            status.assert_not_called()
            connect.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_an_unreadable_registry_refuses_with_its_own_code(self):
        (self.work.accounts_root / account_registry.REGISTRY_NAME).chmod(0o644)
        code, out, _ = run(setup_cli.main, ['status', '--account', 'work'])
        self.assertEqual((code, json.loads(out)), (2, {'status': 'refused', 'code': 'registry_unsafe'}))

    def test_without_the_flag_the_default_paths_are_built_exactly_as_before(self):
        with mock.patch.object(setup_cli, 'SetupPaths') as paths, \
                mock.patch.object(setup_cli, 'setup_status', return_value={'state': 'not_enrolled'}), \
                mock.patch.object(setup_cli, 'existing_broker_status', return_value='not_running'):
            self.assertEqual(run(setup_cli.main, ['status'])[0], 0)
        paths.assert_called_once_with()


def launcher_module(name):
    spec = importlib.util.spec_from_file_location(name, LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherGrammarTests(Home):
    def test_the_account_actions_are_exactly_the_documented_ones(self):
        module = launcher_module('account_launcher_grammar')
        self.assertEqual(module.ACCOUNT_ACTIONS, ('mcp', 'broker', 'setup', 'resume', 'connect', 'wait', 'cancel',
                                                  'api-credentials', 'status', 'connect-helper'))
        self.assertEqual(set(module.ACCOUNT_ACTIONS), set(module.ACTIONS))
        for action in module.ACCOUNT_ACTIONS:
            self.assertEqual(module.account_action([action, '--account', 'work']), ([action], 'work'))
        for alias in ('a', 'w0rk', 'abcdefghijkl'):
            self.assertEqual(module.account_action(['mcp', '--account', alias]), (['mcp'], alias))

    def test_flag_grammar_violations_stop_before_any_admission(self):
        module = launcher_module('account_launcher_refusals')

        class Reached(Exception):
            pass
        refused = [*OUTSIDE, ['connect-helper', '--account', 'default'], ['capabilities', 'show', '--account', 'work'],
                   ['rollback', '--account', 'work'], ['mcp', '--account', 'ｗork']]
        for args in refused:
            with self.subTest(args=args), mock.patch.object(module, '_read', side_effect=Reached()) as reader:
                code, out, err = run(module.main, args)
            self.assertEqual(code, 2)
            reader.assert_not_called()
            self.assertNotIn(SENTINEL, out + err)
        for action in module.ACCOUNT_ACTIONS:  # Inside the grammar: only receipt admission stops it here.
            with self.subTest(action=action), mock.patch.object(module, '_read', side_effect=Reached()) as reader:
                self.assertEqual(run(module.main, [action, '--account', 'work'])[0], 2)
            reader.assert_called_once()

    def installed(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        pin = 'b' * 64
        release = Path(temporary.name).resolve() / 'releases' / pin
        release.mkdir(parents=True)
        shutil.copy(LAUNCHER, release / 'launcher.py')
        spec = importlib.util.spec_from_file_location('account_launcher_dispatch', release / 'launcher.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        receipt = json.dumps({'release_root': str(release), 'index_sha256': pin}).encode()
        return module, release, receipt

    def dispatch(self, module, receipt, args):
        saved = list(sys.path)
        try:
            with mock.patch.object(module, '_read', return_value=receipt), mock.patch.object(module, 'admit'), \
                    mock.patch.object(bundle_runtime, 'activate'), \
                    mock.patch.object(module, 'dispatch_mcp', return_value=11) as mcp, \
                    mock.patch.object(launch_agent, 'run_broker', return_value=12) as broker, \
                    mock.patch.object(module, 'status', return_value=14) as status, \
                    mock.patch.object(setup_cli, 'main', return_value=13) as setup:
                code, out, err = run(module.main, args)
        finally:
            sys.path[:] = saved
        return code, err, mcp, broker, status, setup

    def test_every_account_action_reaches_its_command_with_the_registered_alias(self):
        module, release, receipt = self.installed()
        for action in module.ACCOUNT_ACTIONS:
            with self.subTest(action=action):
                code, _, mcp, broker, status, setup = self.dispatch(module, receipt, [action, '--account', 'work'])
                self.assertEqual(code, {'mcp': 11, 'broker': 12, 'status': 14}.get(action, 13))
                if action == 'mcp':
                    mcp.assert_called_once_with('work')
                elif action == 'broker':
                    broker.assert_called_once_with(self.work)
                elif action == 'status':
                    status.assert_called_once_with(release, 'work')
                else:
                    setup.assert_called_once_with([action, '--account', 'work'])

    def test_without_the_flag_every_dispatch_is_unchanged(self):
        module, release, receipt = self.installed()
        for action in module.ACCOUNT_ACTIONS:
            with self.subTest(action=action):
                _, _, mcp, broker, status, setup = self.dispatch(module, receipt, [action])
                called = {'mcp': mcp, 'broker': broker, 'status': status}.get(action, setup)
                self.assertEqual(called.call_args, mock.call(*{'mcp': (), 'broker': (), 'status': (release,)}.get(
                    action, ([action],))))

    def test_an_unregistered_alias_refuses_with_a_fixed_code_before_dispatch(self):
        module, _, receipt = self.installed()
        self.unregistered()
        before = self.snapshot()
        for action in module.ACCOUNT_ACTIONS:
            with self.subTest(action=action):
                code, err, mcp, broker, status, setup = self.dispatch(module, receipt, [action, '--account', 'work'])
                self.assertEqual((code, json.loads(err)), (2, {'status': 'refused', 'code': 'alias_unavailable'}))
                for dispatched in (mcp, broker, status, setup):
                    dispatched.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_the_mcp_and_status_helpers_hand_the_flag_on(self):
        module = launcher_module('account_launcher_helpers')
        with mock.patch.object(server, 'main', side_effect=lambda: list(sys.argv[1:])), \
                mock.patch.object(sys, 'argv', ['launcher']):
            self.assertEqual(module.dispatch_mcp('work'), ['--account', 'work'])
            self.assertEqual(module.dispatch_mcp(), [])
        with mock.patch.object(setup_cli, 'main', side_effect=lambda args: print('{}') or 0) as main, \
                mock.patch('telegram_search_mcp.bundle_install.client_status', return_value={}):
            run(lambda args: module.status(Path('/r/releases/x'), *args), ['work'])
            run(lambda args: module.status(Path('/r/releases/x')), [])
        self.assertEqual(main.call_args_list, [mock.call(['status', '--account', 'work']), mock.call(['status'])])

    def test_help_names_the_flag(self):
        code, out, _ = run(launcher_module('account_launcher_help').main, ['--help'])
        self.assertEqual(code, 0)
        self.assertIn('--account <alias>', out)


class ProxyArgumentTests(Home):
    def main(self, args):
        with mock.patch.object(sys, 'argv', ['telegram-search-mcp', *args]):
            return run(lambda _: server.main(), None)

    def test_the_registered_alias_builds_its_own_server_and_check(self):
        built = mock.Mock()
        with mock.patch.object(server, 'build_server', return_value=built) as build:
            self.assertEqual(self.main(['--account', 'work'])[0], 0)
        build.assert_called_once_with(account=self.work)
        built.run.assert_called_once_with(transport='stdio')
        with mock.patch.object(server, '_run_check', return_value=0) as check:
            self.assertEqual(self.main(['--check', '--account', 'work'])[0], 0)
        check.assert_called_once_with(self.work)

    def test_without_the_flag_the_calls_are_unchanged(self):
        with mock.patch.object(server, 'build_server') as build:
            self.main([])
        build.assert_called_once_with()
        with mock.patch.object(server, '_run_check', return_value=0) as check:
            self.main(['--check'])
        check.assert_called_once_with()

    def test_refusals_are_fixed_and_never_echo(self):
        self.unregistered()
        with mock.patch.object(server, 'build_server') as build:
            code, out, err = self.main(['--account', 'work'])
        self.assertEqual((code, out, err), (1, '', 'ACCOUNT_ALIAS_UNAVAILABLE\n'))
        for args in (['--account', SENTINEL.lower() + 'x'], ['--account', SENTINEL], ['--account', 'default'],
                     ['--account', 'a' * 13]):
            with self.subTest(args=args), mock.patch.object(server, 'build_server') as build:
                code, out, err = self.main(args)
            self.assertNotEqual(code, 0)
            self.assertNotIn(SENTINEL.lower(), (out + err).lower())
            self.assertNotIn('a' * 13, out + err)
            build.assert_not_called()

    def test_a_named_server_carries_the_alias_in_its_name_and_one_instructions_line_only(self):
        policy = RuntimePolicy()
        default = server.build_server(policy=policy)
        named = server.build_server(policy=policy, account=self.work)
        self.assertEqual(default.name, 'telegram-search-mcp')
        self.assertEqual(named.name, 'telegram-search-mcp-work')
        self.assertEqual(named.instructions, default.instructions + '\nThis server serves the account alias «work».')
        self.assertEqual(server.build_server(policy=policy, account=AccountPaths.for_alias('default', self.home))
                         .instructions, default.instructions)
        self.assertEqual(contract.schema_document(named), contract.schema_document(default))


class ProxyServerInfoTests(unittest.IsolatedAsyncioTestCase, Home):
    """The official SDK client sees the alias in serverInfo.name and the instructions, and nothing else changes."""

    async def test_the_sdk_client_sees_the_alias_where_the_spec_puts_it(self):
        from mcp import Client
        policy = RuntimePolicy()
        async with Client(server.build_server(policy=policy)) as client:
            default = (client.server_info.name, client.instructions, (await client.list_tools()).tools)
        async with Client(server.build_server(policy=policy, account=self.work)) as client:
            named = (client.server_info.name, client.instructions, (await client.list_tools()).tools)
        self.assertEqual((default[0], named[0]), ('telegram-search-mcp', 'telegram-search-mcp-work'))
        self.assertEqual(named[1], default[1] + '\nThis server serves the account alias «work».')
        self.assertEqual(named[2], default[2])


class TransferArgumentTests(Home):
    def test_download_and_upload_use_the_registered_aliases_broker_and_cache(self):
        client = mock.Mock()
        with mock.patch.object(transfer_cli, '_make_client', return_value=client) as make, \
                mock.patch.object(transfer_cli, 'download_artifact', return_value={'status': 'complete'}) as download:
            code, out, _ = run(transfer_cli.main, ['download', '--artifact-id', 'artifact_x', '--destination', '/tmp/x',
                                                   '--account', 'work'])
        self.assertEqual(code, 0)
        make.assert_called_once_with(self.work)
        self.assertEqual(download.call_args.kwargs['account'], self.work)
        self.assertIs(download.call_args.kwargs['client'], client)
        with mock.patch.object(transfer_cli, 'upload_source', return_value={'status': 'complete'}) as upload:
            self.assertEqual(run(transfer_cli.main, ['upload', '--source', '/tmp/x', '--kind', 'document',
                                                     '--account', 'work'])[0], 0)
        self.assertEqual(upload.call_args.kwargs['account'], self.work)

    def test_without_the_flag_the_calls_are_unchanged(self):
        with mock.patch.object(transfer_cli, '_make_client') as make, \
                mock.patch.object(transfer_cli, 'download_artifact', return_value={}) as download:
            run(transfer_cli.main, ['download', '--artifact-id', 'artifact_x', '--destination', '/tmp/x'])
        make.assert_called_once_with()
        self.assertNotIn('account', download.call_args.kwargs)
        with mock.patch.object(transfer_cli, 'upload_source', return_value={}) as upload:
            run(transfer_cli.main, ['upload', '--source', '/tmp/x', '--kind', 'document'])
        self.assertNotIn('account', upload.call_args.kwargs)

    def test_refusals_are_fixed_and_never_echo(self):
        for args in (['download', '--artifact-id', 'artifact_x', '--destination', '/tmp/x', '--account', SENTINEL],
                     ['download', '--artifact-id', 'artifact_x', '--destination', '/tmp/x', '--account', 'default'],
                     ['manifest', '--source', '/tmp/x', '--kind', 'document', '--account', 'work']):
            with self.subTest(args=args), mock.patch.object(transfer_cli, '_make_client') as make:
                code, out, err = run(transfer_cli.main, args)
            self.assertEqual((code, json.loads(err)['code']), (1, 'invalid_arguments'))
            self.assertNotIn(SENTINEL, out + err)
            make.assert_not_called()
        self.unregistered()
        with mock.patch.object(transfer_cli, '_make_client') as make:
            code, out, err = run(transfer_cli.main, ['download', '--artifact-id', 'artifact_x', '--destination',
                                                     '/tmp/x', '--account', 'work'])
        self.assertEqual((code, out, json.loads(err)['code']), (1, '', 'alias_unavailable'))
        make.assert_not_called()


class Selection:
    def arguments(self, kind):
        return ['/fixture/bundle/python3', '-I', '-S', '-B', '/fixture/bundle/launcher.py', kind]


class BrokerArgumentTests(Home):
    def test_a_named_broker_payload_ends_with_its_alias_and_is_recognised_as_its_own(self):
        python = Path(sys.executable)
        default = launch_agent.launch_agent_payload(python_executable=python)
        work = launch_agent.launch_agent_payload(python_executable=python, account=self.work)
        self.assertEqual(work['ProgramArguments'], default['ProgramArguments'] + ['--account', 'work'])
        self.assertEqual(work['Label'], self.work.broker_label)
        self.assertEqual({k: v for k, v in work.items() if k not in ('ProgramArguments', 'Label')},
                         {k: v for k, v in default.items() if k not in ('ProgramArguments', 'Label')})
        self.assertTrue(launch_agent._managed_payload(work, self.work))
        self.assertTrue(launch_agent._managed_payload(default))
        self.assertFalse(launch_agent._managed_payload(work))
        self.assertFalse(launch_agent._managed_payload(default, self.work))
        self.assertFalse(launch_agent._managed_payload({**work, 'ProgramArguments': default['ProgramArguments']},
                                                       self.work))
        with mock.patch.object(bundle_runtime, '_SELECTION', Selection()):
            bundled = launch_agent.launch_agent_payload(python_executable=python, account=self.work)
            self.assertEqual(bundled['ProgramArguments'], Selection().arguments('broker') + ['--account', 'work'])
            self.assertTrue(launch_agent._managed_payload(bundled, self.work))
            self.assertFalse(launch_agent._managed_payload(bundled))

    def test_the_named_broker_argv_runs_that_aliases_broker(self):
        with mock.patch.object(launch_agent, 'run_broker', return_value=0) as broker:
            self.assertEqual(run(launch_agent.main, ['run', '--account', 'work'])[0], 0)
            self.assertEqual(run(launch_agent.main, ['run'])[0], 0)
        self.assertEqual(broker.call_args_list, [mock.call(account=self.work), mock.call()])

    def test_broker_argv_refusals_are_fixed(self):
        with mock.patch.object(launch_agent, 'run_broker') as broker, \
                mock.patch.object(launch_agent, 'install_launch_agent') as install:
            code, out, err = run(launch_agent.main, ['run', '--account', SENTINEL])
            self.assertEqual(code, 2)
            self.assertNotIn(SENTINEL, out + err)
            self.unregistered()
            self.assertEqual(run(launch_agent.main, ['run', '--account', 'work']), (1, '', 'BROKER_LIFECYCLE_FAILED\n'))
        broker.assert_not_called()
        install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
