"""0.55 phase C: one broker LaunchAgent and one transient connect job per alias (spec 3C).

A named alias's jobs are written, bootstrapped and booted out only under that alias's own label; the default
alias keeps today's label, plist and bytes. Every launchd call goes to a fake runner and every home is
synthetic; nothing reaches the real launchd or Telegram.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_search_mcp import account_registry, bundle_runtime, config, connect_cli, launch_agent, setup_cli
from telegram_search_mcp.config import AccountPaths
from telegram_search_mcp.enrollment import SetupPaths

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'packaging' / 'launcher.py'
FIXTURE_HOME = '/fixture-home/sample-owner'
BUNDLE_LAUNCHER = '/fixture/bundle/bin/telegram-search-mcp'
UID = 501


class FakeLaunchd:
    """launchctl for one gui domain: bootstrap loads the plist it is given, print answers like launchd."""

    def __init__(self):
        self.calls, self.loaded = [], {}

    def __call__(self, arguments, **_):
        arguments = list(arguments)
        self.calls.append(arguments)
        verb = arguments[1]
        if verb == 'bootstrap':
            payload = plistlib.loads(Path(arguments[3]).read_bytes())
            self.loaded[payload['Label']] = (Path(arguments[3]), payload['ProgramArguments'])
            return subprocess.CompletedProcess(arguments, 0, '', '')
        label = arguments[2].rpartition('/')[2]
        if verb == 'bootout':
            return subprocess.CompletedProcess(arguments, 0 if self.loaded.pop(label, None) else 113, '', '')
        if verb == 'print':
            if label not in self.loaded:
                return subprocess.CompletedProcess(arguments, 113, '', '')
            plist, program = self.loaded[label]
            text = '\n'.join([f'{label} = {{', f'\tpath = {plist.absolute()}', f'\tprogram = {program[0]}',
                              '\targuments = {', *(f'\t\t{value}' for value in program), '\t}', '\tstate = running',
                              '}'])
            return subprocess.CompletedProcess(arguments, 0, text, '')
        return subprocess.CompletedProcess(arguments, 0, '', '')

    def labels(self):
        """Every label a call named (a bootstrap names its plist; the plist's name is its label)."""
        named = set()
        for arguments in self.calls:
            named.add(Path(arguments[3]).stem if arguments[1] == 'bootstrap' else arguments[2].rpartition('/')[2])
        return named


class Home(unittest.TestCase):
    """A short synthetic home whose registry lists `work` and `play`; Path.home() answers it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='j')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        for alias in ('work', 'play'):
            account_registry.add_alias(alias, self.home, longest_tool='t')
        patcher = mock.patch.object(Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = AccountPaths.for_alias('work', self.home)
        self.play = AccountPaths.for_alias('play', self.home)
        self.default = AccountPaths.for_alias('default', self.home)
        self.agents = self.home / 'Library' / 'LaunchAgents'
        self.agents.mkdir(parents=True, mode=0o700)
        self.python = Path(sys.executable)
        # A default broker plist beside the alias's: it must never be read, rewritten or named.
        self.default.broker_plist.write_bytes(b'default broker plist bytes')
        os.chmod(self.default.broker_plist, 0o600)

    def assert_default_untouched(self, launchd):
        self.assertEqual(self.default.broker_plist.read_bytes(), b'default broker plist bytes')
        self.assertTrue(launchd.labels().isdisjoint({self.default.broker_label, self.default.connect_label,
                                                     config.LAUNCH_AGENT_LABEL, config.CONNECT_JOB_LABEL}))


def run(main, args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            code = main(args)
        except SystemExit as exit:
            code = exit.code
    return code, out.getvalue(), err.getvalue()


class BrokerAgentTests(Home):
    def test_start_writes_and_bootstraps_only_the_alias_label(self):
        launchd = FakeLaunchd()
        launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.work)
        payload = plistlib.loads(self.work.broker_plist.read_bytes())
        self.assertEqual(payload, launch_agent.launch_agent_payload(python_executable=self.python, account=self.work))
        self.assertEqual(payload['Label'], f'com.{self.home.name}.telegram-search-mcp.a.work.broker')
        self.assertEqual(payload['ProgramArguments'][-2:], ['--account', 'work'])
        self.assertEqual(self.work.broker_plist, self.agents / f'{payload["Label"]}.plist')
        metadata = self.work.broker_plist.lstat()
        self.assertEqual((stat.S_IMODE(metadata.st_mode), metadata.st_nlink), (0o600, 1))
        self.assertEqual(launchd.calls, [
            [launch_agent.LAUNCHCTL, 'print', f'gui/{UID}/{self.work.broker_label}'],
            [launch_agent.LAUNCHCTL, 'bootstrap', f'gui/{UID}', str(self.work.broker_plist)]])
        # A second start finds its own live registration and touches nothing.
        launchd.calls.clear()
        launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.work)
        self.assertEqual(launchd.calls, [[launch_agent.LAUNCHCTL, 'print', f'gui/{UID}/{self.work.broker_label}']])
        self.assert_default_untouched(launchd)
        self.assertFalse(self.play.broker_plist.exists())

    def test_install_restore_stop_and_uninstall_name_only_the_alias_label(self):
        launchd = FakeLaunchd()
        launch_agent.install_launch_agent(python_executable=self.python, runner=launchd, uid=UID, account=self.work,
                                          preflight=lambda: None)
        service = f'gui/{UID}/{self.work.broker_label}'
        self.assertEqual(launchd.calls, [[launch_agent.LAUNCHCTL, 'bootout', service],
                                         [launch_agent.LAUNCHCTL, 'print', service],
                                         [launch_agent.LAUNCHCTL, 'bootstrap', f'gui/{UID}', str(self.work.broker_plist)]])
        launch_agent.validate_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.work)
        launch_agent.stop_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.work,
                                          sleeper=lambda _: None)
        self.assertNotIn(self.work.broker_label, launchd.loaded)
        self.assertTrue(self.work.broker_plist.exists())  # A stop keeps the definition for the restart.
        launch_agent.restore_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.work)
        self.assertIn(self.work.broker_label, launchd.loaded)
        self.assertTrue(launch_agent.launch_agent_status(runner=launchd, uid=UID, account=self.work))
        launch_agent.uninstall_launch_agent(runner=launchd, uid=UID, account=self.work)
        self.assertFalse(self.work.broker_plist.exists())
        self.assertFalse(launch_agent.launch_agent_status(runner=launchd, uid=UID, account=self.work))
        self.assertEqual(launchd.labels(), {self.work.broker_label})
        self.assert_default_untouched(launchd)

    def test_restore_never_creates_an_alias_definition(self):
        launchd = FakeLaunchd()
        with self.assertRaises(launch_agent.LaunchAgentError):
            launch_agent.restore_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID,
                                                 account=self.work)
        self.assertEqual(launchd.calls, [])
        self.assertFalse(self.work.broker_plist.exists())

    def test_a_definition_of_another_account_at_the_alias_path_is_refused(self):
        launchd = FakeLaunchd()
        for foreign in (launch_agent.launch_agent_payload(python_executable=self.python),
                        launch_agent.launch_agent_payload(python_executable=self.python, account=self.play)):
            with self.subTest(label=foreign['Label']):
                self.work.broker_plist.write_bytes(plistlib.dumps(foreign, sort_keys=True))
                os.chmod(self.work.broker_plist, 0o600)
                for operation in (
                        lambda: launch_agent.write_launch_agent(plist_path=self.work.broker_plist,
                                                                python_executable=self.python, account=self.work),
                        lambda: launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd,
                                                                   uid=UID, account=self.work),
                        lambda: launch_agent.uninstall_launch_agent(runner=launchd, uid=UID, account=self.work)):
                    with self.assertRaises(launch_agent.LaunchAgentError):
                        operation()
                self.assertEqual(plistlib.loads(self.work.broker_plist.read_bytes()), foreign)
        self.assertEqual(launchd.calls, [])

    def test_two_aliases_in_one_domain_never_name_each_others_jobs(self):
        launchd = FakeLaunchd()
        for account in (self.work, self.play):
            launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=account)
        self.assertEqual(set(launchd.loaded), {self.work.broker_label, self.play.broker_label})
        launchd.calls.clear()
        launch_agent.uninstall_launch_agent(runner=launchd, uid=UID, account=self.play)
        self.assertEqual(launchd.labels(), {self.play.broker_label})
        self.assertEqual(set(launchd.loaded), {self.work.broker_label})
        self.assertTrue(self.work.broker_plist.exists())
        labels = {account.broker_label for account in (self.default, self.work, self.play)}
        labels |= {account.connect_label for account in (self.default, self.work, self.play)}
        self.assertEqual(len(labels), 6)
        self.assert_default_untouched(launchd)

    def test_broker_lifecycle_cli_takes_the_alias(self):
        with mock.patch.object(launch_agent, 'install_launch_agent') as install, \
                mock.patch.object(launch_agent, 'launch_agent_status', return_value=True) as status, \
                mock.patch.object(launch_agent, 'uninstall_launch_agent') as uninstall:
            self.assertEqual(run(launch_agent.main, ['install', '--account', 'work']), (0, 'INSTALLED\n', ''))
            self.assertEqual(run(launch_agent.main, ['status', '--account', 'work']), (0, 'RUNNING\n', ''))
            self.assertEqual(run(launch_agent.main, ['uninstall', '--account', 'work']), (0, 'UNINSTALLED\n', ''))
            self.assertEqual(run(launch_agent.main, ['status']), (0, 'RUNNING\n', ''))
        install.assert_called_once_with(account=self.work)
        uninstall.assert_called_once_with(account=self.work)
        self.assertEqual(status.call_args_list, [mock.call(account=self.work), mock.call()])
        account_registry.remove_alias('work', self.home)
        with mock.patch.object(launch_agent, 'install_launch_agent') as install:
            self.assertEqual(run(launch_agent.main, ['install', '--account', 'work']),
                             (1, '', 'BROKER_LIFECYCLE_FAILED\n'))
        install.assert_not_called()


class AliasStatusTests(Home):
    """`status --account <alias>`: loaded and authorized for that alias's own broker, never the account."""

    def status(self, args, *, broker='not_running', check=None, loaded=False):
        client = mock.Mock()
        client.check.side_effect = check if isinstance(check, Exception) else None
        client.check.return_value = check
        with mock.patch.object(setup_cli, 'existing_broker_status', return_value=broker), \
                mock.patch.object(setup_cli, '_client', return_value=client) as made, \
                mock.patch.object(setup_cli, 'launch_agent_status', return_value=loaded) as agent:
            code, out, err = run(setup_cli.main, args)
        return code, json.loads(out), made, agent

    def test_a_named_alias_reports_loaded_and_authorized(self):
        code, value, made, agent = self.status(['status', '--account', 'work'])
        self.assertEqual(code, 0)
        self.assertEqual(value, {'state': 'new', 'broker': 'not_running', 'alias': 'work', 'loaded': False,
                                 'authorized': None})
        agent.assert_called_once_with(account=self.work)
        made.assert_not_called()  # No broker: nothing is asked.
        for check, authorized in (('ready', True), ('blocked', False), (TimeoutError(), None), ('other', None)):
            with self.subTest(check=check):
                _, value, made, _ = self.status(['status', '--account', 'work'], broker='compatible_running',
                                                check=check, loaded=True)
                self.assertEqual((value['loaded'], value['authorized']), (True, authorized))
                self.assertEqual(made.call_args.args[0], SetupPaths.from_account(self.work))
                made.return_value.close.assert_called_once_with()

    def test_the_default_status_keeps_its_054_keys(self):
        code, value, made, agent = self.status(['status'], broker='compatible_running', check='ready')
        self.assertEqual((code, set(value)), (0, {'state', 'broker'}))
        agent.assert_not_called()
        made.assert_not_called()

    def test_a_launchd_failure_reports_unknown(self):
        with mock.patch.object(setup_cli, 'existing_broker_status', return_value='not_running'), \
                mock.patch.object(setup_cli, 'launch_agent_status', side_effect=OSError('SYNTHETIC')):
            code, out, err = run(setup_cli.main, ['status', '--account', 'work'])
        self.assertEqual((code, json.loads(out)['loaded']), (0, None))
        self.assertNotIn('SYNTHETIC', out + err)

    def test_the_loaded_fact_is_the_aliases_own_label(self):
        launchd = FakeLaunchd()
        launch_agent.start_enrolled_broker(python_executable=self.python, runner=launchd, uid=UID, account=self.play)
        self.assertFalse(launch_agent.launch_agent_status(runner=launchd, account=self.work, uid=UID))
        self.assertTrue(launch_agent.launch_agent_status(runner=launchd, account=self.play, uid=UID))
        self.assertEqual(launchd.calls[-2:], [[launch_agent.LAUNCHCTL, 'print', f'gui/{UID}/{self.work.broker_label}'],
                                              [launch_agent.LAUNCHCTL, 'print', f'gui/{UID}/{self.play.broker_label}']])

    def test_the_launcher_reports_no_default_clients_for_an_alias(self):
        spec = importlib.util.spec_from_file_location('launcher_alias_status', LAUNCHER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(setup_cli, 'main', side_effect=lambda args: print('{"state": "new"}') or 0), \
                mock.patch('telegram_search_mcp.bundle_install.client_status', return_value={'claude_desktop': 'ok'}):
            named = run(lambda _: module.status(Path('/r/releases/x'), 'work'), None)
            default = run(lambda _: module.status(Path('/r/releases/x')), None)
        self.assertEqual(json.loads(named[1]), {'state': 'new'})
        self.assertEqual(json.loads(default[1]), {'state': 'new', 'clients': {'claude_desktop': 'ok'}})


class Selection:
    def arguments(self, kind):
        return ['/fixture/bundle/python3', '-I', '-S', '-B', '/fixture/bundle/launcher.py', kind]


class ConnectJobTests(Home):
    def test_the_connect_job_runs_the_aliases_helper_under_its_own_label(self):
        payload = launch_agent.connect_job_payload(python_executable=self.python, account=self.work)
        default = launch_agent.connect_job_payload(python_executable=self.python)
        self.assertEqual(payload['Label'], f'com.{self.home.name}.telegram-search-mcp.a.work.connect')
        self.assertEqual(payload['ProgramArguments'], [str(self.python.absolute()), '-m', 'telegram_search_mcp.setup_cli',
                                                       'connect-helper', '--account', 'work'])
        self.assertEqual({k: v for k, v in payload.items() if k not in ('Label', 'ProgramArguments')},
                         {k: v for k, v in default.items() if k not in ('Label', 'ProgramArguments')})
        with mock.patch.object(bundle_runtime, '_SELECTION', Selection()):
            bundled = launch_agent.connect_job_payload(python_executable=self.python, account=self.work)
        self.assertEqual(bundled['ProgramArguments'], Selection().arguments('connect-helper') + ['--account', 'work'])

    def test_plist_status_and_lock_live_under_the_alias_root(self):
        paths = SetupPaths.from_account(self.work)
        for path in (connect_cli.job_plist_path(paths), connect_cli.connect_lock_path(paths),
                     connect_cli.connect_status_path(paths)):
            self.assertEqual(path.parent, self.work.run)
            self.assertTrue(path.is_relative_to(self.home / 'Library' / 'Application Support' /
                                                config.ACCOUNTS_DIRECTORY / 'work'))
        launchd = FakeLaunchd()
        self.assertEqual(launch_agent.start_connect_job(plist_path=self.work.connect_plist,
                                                        python_executable=self.python, runner=launchd, uid=UID,
                                                        account=self.work), 'started')
        self.assertEqual(launchd.calls, [
            [launch_agent.LAUNCHCTL, 'print', f'gui/{UID}/{self.work.connect_label}'],
            [launch_agent.LAUNCHCTL, 'bootstrap', f'gui/{UID}', str(self.work.connect_plist)]])
        written = plistlib.loads(self.work.connect_plist.read_bytes())
        self.assertEqual(written, launch_agent.connect_job_payload(python_executable=self.python, account=self.work))
        self.assertEqual(stat.S_IMODE(self.work.connect_plist.lstat().st_mode), 0o600)
        # A second connect sees the alias's own busy job; another alias's job is independent.
        self.assertEqual(launch_agent.start_connect_job(plist_path=self.work.connect_plist,
                                                        python_executable=self.python, runner=launchd, uid=UID,
                                                        account=self.work), 'running')
        self.assertEqual(launch_agent.connect_job_state(runner=launchd, uid=UID, account=self.play), 'absent')
        launch_agent.stop_connect_job(runner=launchd, uid=UID, account=self.work, sleeper=lambda _: None)
        self.assertEqual(launchd.labels(), {self.work.connect_label, self.play.connect_label})
        self.assert_default_untouched(launchd)

    def test_connect_helper_takes_the_alias_and_requires_its_own_job(self):
        self.assertEqual(setup_cli.account_argument(['connect-helper', '--account', 'work']),
                         (['connect-helper'], 'work'))
        self.assertIn('connect-helper', setup_cli.ACCOUNT_COMMANDS)
        ran = []
        with mock.patch.object(connect_cli, 'connect_helper', side_effect=lambda paths, **kw: ran.append(paths) or
                               mock.Mock(exit_code=0)):
            for service in (None, config.CONNECT_JOB_LABEL, self.play.connect_label):
                with self.subTest(service=service), mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop('XPC_SERVICE_NAME', None)
                    if service is not None:
                        os.environ['XPC_SERVICE_NAME'] = service
                    code, out, _ = run(setup_cli.main, ['connect-helper', '--account', 'work'])
                    self.assertEqual((code, json.loads(out)['code']), (2, 'connect_helper_requires_launchd'))
            with mock.patch.dict(os.environ, {'XPC_SERVICE_NAME': self.work.connect_label}):
                self.assertEqual(run(setup_cli.main, ['connect-helper', '--account', 'work']), (0, '', ''))
        self.assertEqual(ran, [SetupPaths.from_account(self.work)])
        self.assertFalse(self.work.run.exists())  # Refused helpers touched nothing.

    def test_the_launcher_admits_the_aliases_connect_helper(self):
        spec = importlib.util.spec_from_file_location('launcher_account_jobs', LAUNCHER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(set(module.ACCOUNT_ACTIONS), set(module.ACTIONS))
        self.assertEqual(module.account_action(['connect-helper', '--account', 'work']), (['connect-helper'], 'work'))


# Renders the named alias's payloads in a fresh process under the fixture HOME (the explicit test hook admits it).
RENDER = '''
import json, plistlib, sys
from pathlib import Path
from unittest import mock
from telegram_search_mcp import bundle_runtime, config, launch_agent


class Selection:
    def arguments(self, kind):
        return [sys.argv[2], kind]


with config.named_aliases_for_tests():
    work = config.AccountPaths._derive("work", Path.home())


def render(suffix):
    python = Path(sys.argv[1])
    return {"broker" + suffix: launch_agent.launch_agent_payload(python_executable=python, account=work),
            "connect" + suffix: launch_agent.connect_job_payload(python_executable=python, account=work)}


payloads = render("")
with mock.patch.object(bundle_runtime, "_SELECTION", Selection()):
    payloads.update(render("_bundled"))
payloads["paths"] = [str(work.broker_plist), str(work.connect_plist)]
print(json.dumps({name: value if name == "paths" else plistlib.dumps(value, sort_keys=True).decode()
                  for name, value in payloads.items()}))
'''


class NamedPayloadGoldenTests(unittest.TestCase):
    def test_the_named_alias_payloads_are_the_literal_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory).resolve() / 'python3'
            python.write_bytes(b'')
            rendered = subprocess.run(
                [sys.executable, '-B', '-c', RENDER, str(python), BUNDLE_LAUNCHER], capture_output=True, text=True,
                timeout=60, check=True, cwd=directory,
                env={'HOME': FIXTURE_HOME, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
        written = json.loads(rendered.stdout)
        python = str(python)
        common = {'LimitLoadToSessionType': 'Aqua', 'RunAtLoad': True, 'StandardErrorPath': '/dev/null',
                  'StandardOutPath': '/dev/null', 'Umask': 63}
        broker = {**common, 'KeepAlive': True, 'Label': 'com.sample-owner.telegram-search-mcp.a.work.broker',
                  'ProcessType': 'Background'}
        connect = {**common, 'KeepAlive': False, 'Label': 'com.sample-owner.telegram-search-mcp.a.work.connect',
                   'ProcessType': 'Interactive', 'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}}
        golden = {
            'broker': {**broker, 'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'},
                       'ProgramArguments': [python, '-m', 'telegram_search_mcp.launch_agent', 'run', '--account', 'work']},
            'connect': {**connect, 'ProgramArguments': [python, '-m', 'telegram_search_mcp.setup_cli', 'connect-helper',
                                                        '--account', 'work']},
            'broker_bundled': {**broker, 'EnvironmentVariables': {},
                               'ProgramArguments': [BUNDLE_LAUNCHER, 'broker', '--account', 'work']},
            'connect_bundled': {**connect, 'ProgramArguments': [BUNDLE_LAUNCHER, 'connect-helper', '--account', 'work']},
        }
        self.assertEqual(written.pop('paths'), [
            f'{FIXTURE_HOME}/Library/LaunchAgents/com.sample-owner.telegram-search-mcp.a.work.broker.plist',
            f'{FIXTURE_HOME}/Library/Application Support/TelegramSearchMCP-accounts/work/run/'
            'com.sample-owner.telegram-search-mcp.a.work.connect.plist'])
        self.assertEqual(set(written), set(golden))
        for name, payload in golden.items():
            with self.subTest(payload=name):
                self.assertEqual(written[name], plistlib.dumps(payload, sort_keys=True).decode())


if __name__ == '__main__':
    unittest.main()
