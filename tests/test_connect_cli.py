"""Agent-facing connect/wait, the transient helper job, api-credentials and the launcher grammar.

Everything is inert: launchctl is a recording fake, the broker is a fake client, the terminal and
Keychain are fakes, the browser opener is a recorder. Nothing here reaches launchd, open, Keychain
or TDLib, and the "URL" and password-like strings are synthetic canaries.
"""
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

from test_onboarding import MemoryStore, Terminal

from telegram_search_mcp import bundle_runtime, connect_cli, launch_agent, setup_cli
from telegram_search_mcp.broker_client import BrokerCompatibilityError, BrokerUnavailable
from telegram_search_mcp.config import LAUNCH_AGENT_LABEL
from telegram_search_mcp.connect_flow import (PHASES, STATUS_CODES, ConnectResult, connect_status_path,
                                              write_connect_status)
from telegram_search_mcp.credential_identity import ResolvedIdentity
from telegram_search_mcp.credentials import SecretValue
from telegram_search_mcp.enrollment import EnrollmentStore, SetupPaths
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.launch_agent import CONNECT_JOB_LABEL
from telegram_search_mcp.session_owner import SessionOwnerLease, acquire_owner_lock, open_private_directory

UID = 501
URL_CANARY = 'http://127.0.0.1:50999/#t=' + 'LEAKCANARYTOKEN-9d41' + 'x' * 23  # 43 token characters
PASSWORD_CANARY = 'LEAKCANARYPASSWORD-5c2'
ENROLLED = ResolvedIdentity(ApiCredentials(424242, 'b' * 32), 'keychain')


def completed(args, code=0, out=''):
    return subprocess.CompletedProcess(args, code, out, '')


class FakeLaunchd:
    """Recording launchctl. Models the enrolled broker registration and the transient connect job.

    `job` is the connect job's literal launchd state ('running', 'not running', 'exited',
    'spawn scheduled', 'waiting', ...), 'absent' when unregistered, or 'no state line'.
    """
    def __init__(self, paths, broker_plist, *, broker_loaded=False, job='absent', bootstrap_code=0,
                 broker_bootstrap_code=0, print_code=None, raises=None, exact_registration=True,
                 bootstrap_state='running'):
        self.paths, self.broker_plist, self.calls = paths, broker_plist, []
        self.broker_loaded, self.job = broker_loaded, job
        self.bootstrap_code, self.print_code, self.raises = bootstrap_code, print_code, raises
        self.broker_bootstrap_code, self.bootstrap_state = broker_bootstrap_code, bootstrap_state
        arguments = launch_agent.launch_agent_payload(python_executable=Path(sys.executable))['ProgramArguments']
        program = arguments[0] if exact_registration else '/foreign/python'
        self.broker_text = ('path = ' + str(broker_plist) + '\nprogram = ' + program
                            + '\narguments = {\n' + '\n'.join(arguments) + '\n}\n')
        self.bootstrapped = []  # (plist bytes, status file existed) at the moment of each job bootstrap
        self.broker_bootstraps = 0
        self.on_print = self.on_bootout = self.on_bootstrap = None
        self._lock = threading.RLock()

    def verbs(self, *names):
        return [call[1:] for call in self.calls if call[1] in names]

    def __call__(self, args, **kwargs):
        with self._lock:
            return self._call(args, **kwargs)

    def _call(self, args, **kwargs):
        assert args[0] == '/bin/launchctl' and kwargs.get('check') is False
        self.calls.append(list(args))
        if self.raises is not None:
            raise self.raises
        verb = args[1]
        if verb == 'print':
            label = args[2].rsplit('/', 1)[1]
            if self.on_print is not None:
                self.on_print()
            if label == LAUNCH_AGENT_LABEL:
                return completed(args, 0 if self.broker_loaded else 113, self.broker_text if self.broker_loaded else '')
            assert label == CONNECT_JOB_LABEL
            if self.print_code is not None:
                return completed(args, self.print_code)
            if self.job == 'absent':
                return completed(args, 113)
            state = '' if self.job == 'no state line' else '\tstate = %s\n' % self.job
            return completed(args, 0, '%s = {\n\tactive count = 1\n%s\n\tprogram = /x\n}\n' % (args[2], state))
        if verb == 'bootout':
            if args[2] == 'gui/%d/%s' % (UID, LAUNCH_AGENT_LABEL):
                self.broker_loaded = False
            else:
                assert args[2] == 'gui/%d/%s' % (UID, CONNECT_JOB_LABEL)
                if self.job == 'absent':
                    return completed(args, 113)
                self.job = 'absent'
                if self.on_bootout is not None:
                    self.on_bootout()
            return completed(args)
        if verb == 'bootstrap':
            assert args[2] == 'gui/%d' % UID
            if args[3] == str(self.broker_plist):
                self.broker_bootstraps += 1
                if self.broker_bootstrap_code:
                    return completed(args, self.broker_bootstrap_code)
                self.broker_loaded = True
                return completed(args)
            self.bootstrapped.append((Path(args[3]).read_bytes(), connect_status_path(self.paths).exists()))
            if self.on_bootstrap is not None:
                self.on_bootstrap()
            if self.bootstrap_code:
                return completed(args, self.bootstrap_code)
            self.job = self.bootstrap_state
            return completed(args)
        raise AssertionError(args)


class FakeBroker:
    """status may be a list: one answer per check, the last one repeating."""
    def __init__(self, status='ready', handshake_error=None):
        self.status, self.handshake_error, self.events = status, handshake_error, []
        self.answers = list(status) if isinstance(status, list) else None

    def handshake(self):
        self.events.append('handshake')
        if self.handshake_error is not None:
            raise self.handshake_error
        return {}

    def check(self):
        self.events.append('check')
        status = self.status
        if self.answers is not None:
            status = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(status, Exception):
            raise status
        return status

    def close(self):
        self.events.append('close')


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = SetupPaths(self.root / 'app')
        self.broker_plist = self.root / 'agent.plist'
        self.sleeps = []

    def run_dir(self):
        os.close(open_private_directory(self.paths.lock.parent, create=True))
        return self.paths.lock.parent

    def socket(self):
        (self.run_dir() / 'broker.sock').touch()

    def legacy_session(self):
        self.paths.root.mkdir(mode=0o700)
        self.paths.session.mkdir(mode=0o700)

    def enroll(self):
        EnrollmentStore(self.paths).create(MemoryStore(), bound=ENROLLED)

    def install_broker_plist(self):
        value = launch_agent.launch_agent_payload(python_executable=Path(sys.executable))
        self.broker_plist.write_bytes(plistlib.dumps(value))
        self.broker_plist.chmod(0o600)
        return self.broker_plist.read_bytes()

    def launchd(self, **options):
        return FakeLaunchd(self.paths, self.broker_plist, **options)

    def status_file(self, phase='ready', code=None, mode=0o600, expires_at=None):
        """A live run's status by default: it expires 600 s from now."""
        now = time.time()
        write_connect_status(self.paths, phase, code, updated_at=now,
                             expires_at=now + 600 if expires_at is None else expires_at)
        connect_status_path(self.paths).chmod(mode)

    def connect(self, launchd, *, broker=None, terminal=None, inline=None, sleeper=None):
        def no_inline():
            raise AssertionError('inline helper must not run')
        return connect_cli.connect(self.paths, client_factory=lambda paths, timeout: broker,
            terminal=terminal if terminal is not None else Terminal(refused=True), inline=inline or no_inline,
            runner=launchd, uid=UID, sleeper=sleeper or self.sleeps.append, broker_plist=self.broker_plist)

    def hold_connect_lock(self):
        """Stands in for another helper or connect: a separate open file description of connect.lock."""
        os.close(open_private_directory(self.paths.lock.parent, create=True))
        descriptor = acquire_owner_lock(connect_cli.connect_lock_path(self.paths))
        holder = {'fd': descriptor}
        def release():
            if holder['fd'] is not None:
                os.close(holder['fd'])
                holder['fd'] = None
        self.addCleanup(release)
        return release

    def connect_lock_free(self):
        descriptor = connect_cli._take_connect_lock(self.paths)
        if descriptor is None:
            return False
        os.close(descriptor)
        return True


class ConnectTests(Harness):
    def test_legacy_unmanaged_is_refused_before_any_broker_or_launchd_action(self):
        session = self.paths.session
        self.legacy_session()
        (session / 'td.binlog').write_bytes(b'legacy')
        launchd, broker = self.launchd(), FakeBroker()
        value, code = self.connect(launchd, broker=broker)
        self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'legacy_unmanaged',
            'next_action': 'preserve_existing_session_and_use_legacy_install_path'}))
        self.assertEqual((launchd.calls, broker.events), ([], []))
        self.assertFalse(job_plist(self.paths).exists())

    def test_inconsistent_enrollment_is_refused_without_reset(self):
        self.enroll()
        record = self.paths.record
        record.write_bytes(b'{"version":1}')
        launchd = self.launchd()
        value, code = self.connect(launchd, broker=FakeBroker())
        self.assertEqual((code, value['code'], value['next_action']),
                         (2, 'enrollment_inconsistent', 'review_enrollment_binding_without_resetting'))
        self.assertEqual(launchd.calls, [])
        self.assertEqual(record.read_bytes(), b'{"version":1}')

    def test_ready_broker_is_already_connected_and_nothing_else_is_touched(self):
        self.enroll()
        self.socket()
        self.status_file('failed', 'owner_busy')
        before = connect_status_path(self.paths).read_bytes()
        launchd, broker = self.launchd(broker_loaded=True), FakeBroker('ready')
        value, code = self.connect(launchd, broker=broker)
        self.assertEqual((code, value), (0, {'status': 'already_connected'}))
        self.assertEqual(launchd.calls, [])  # Not even a read-only launchctl print.
        self.assertEqual(broker.events, ['handshake', 'check', 'close'])
        self.assertEqual(connect_status_path(self.paths).read_bytes(), before)
        self.assertFalse(job_plist(self.paths).exists())

    def test_ready_is_never_stopped_even_with_a_matching_registration(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        self.connect(launchd, broker=FakeBroker('ready'))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_blocked_enrolled_validated_broker_is_stopped_then_the_screen_job_starts(self):
        self.enroll()
        self.socket()
        plist = self.install_broker_plist()
        self.status_file('failed', 'owner_busy')  # A previous run's outcome must not survive.
        launchd = self.launchd(broker_loaded=True)
        broker = FakeBroker('blocked')
        value, code = self.connect(launchd, broker=broker)
        self.assertEqual((code, value), (0, {'status': 'screen_opened', 'next_action': 'wait'}))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [
            ['bootout', 'gui/%d/%s' % (UID, LAUNCH_AGENT_LABEL)],
            ['bootstrap', 'gui/%d' % UID, str(job_plist(self.paths))]])
        self.assertEqual(self.broker_plist.read_bytes(), plist)  # The broker definition is never unlinked.
        # The check is re-run right before the stop, and validation (read-only prints) precedes it.
        self.assertEqual(broker.events, ['handshake', 'check', 'close'] * 2)
        broker_calls = [call[1] for call in launchd.calls if call[2].endswith(LAUNCH_AGENT_LABEL)]
        self.assertEqual(broker_calls[:3], ['print', 'print', 'bootout'])
        self.assertFalse(launchd.bootstrapped[0][1], 'stale status must be gone before the helper starts')
        self.assertFalse(connect_status_path(self.paths).exists())

    def test_blocked_broker_failing_validation_is_owner_busy_and_never_stopped(self):
        self.enroll()
        self.socket()
        cases = {
            'live registration differs': dict(setup=self.install_broker_plist, exact_registration=False),
            'no broker plist': dict(setup=lambda: None, exact_registration=True),
            'foreign plist': dict(setup=lambda: (self.broker_plist.write_bytes(plistlib.dumps({'Label': 'foreign'})),
                                                 self.broker_plist.chmod(0o600)), exact_registration=True),
            'service not loaded': dict(setup=self.install_broker_plist, exact_registration=True, loaded=False),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.broker_plist.unlink(missing_ok=True)
                self.status_file('failed', 'owner_busy')
                case['setup']()
                launchd = self.launchd(broker_loaded=case.get('loaded', True),
                                       exact_registration=case['exact_registration'])
                value, code = self.connect(launchd, broker=FakeBroker('blocked'))
                self.assertEqual((code, value['code'], value['next_action']),
                                 (2, 'owner_busy', 'check_existing_broker_without_stopping_it'))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
                self.assertTrue(connect_status_path(self.paths).exists())
                self.assertFalse(job_plist(self.paths).exists())

    def test_blocked_broker_without_an_enrollment_record_is_not_a_stop_candidate(self):
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        value, code = self.connect(launchd, broker=FakeBroker('blocked'))
        self.assertEqual((code, value['code']), (2, 'owner_busy'))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_incompatible_or_erroring_owner_holding_the_lease_is_owner_busy_and_never_stopped(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        for broker in [FakeBroker(handshake_error=BrokerCompatibilityError('schema_mismatch')),
                       FakeBroker(handshake_error=BrokerUnavailable('down')), FakeBroker('error'),
                       FakeBroker(BrokerUnavailable('bad response'))]:
            with self.subTest(events=broker.status), SessionOwnerLease(self.paths.lock):
                launchd = self.launchd(broker_loaded=True)
                value, code = self.connect(launchd, broker=broker)
                self.assertEqual((code, value['code']), (2, 'owner_busy'))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_stale_socket_with_no_owner_does_not_block_a_new_connect(self):
        self.enroll()
        self.socket()
        launchd = self.launchd()
        value, code = self.connect(launchd, broker=FakeBroker(handshake_error=BrokerUnavailable('stale')))
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        self.assertEqual(len(launchd.verbs('bootstrap')), 1)
        self.assertEqual(launchd.verbs('bootout'), [])

    def test_a_stopped_broker_whose_lease_stays_held_is_restarted_and_reported_truthfully(self):
        self.enroll()
        self.socket()
        plist = self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        with SessionOwnerLease(self.paths.lock):  # Some other owner keeps the session after our stop.
            value, code = self.connect(launchd, broker=FakeBroker('blocked'))
        # The broker was stopped by this run, so the advice is not "check it without stopping it".
        self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'owner_busy',
                                             'next_action': 'run_connect_again'}))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [
            ['bootout', 'gui/%d/%s' % (UID, LAUNCH_AGENT_LABEL)],
            ['bootstrap', 'gui/%d' % UID, str(self.broker_plist)]])
        self.assertTrue(launchd.broker_loaded, 'the enrolled broker is never left stopped')
        self.assertEqual((launchd.bootstrapped, self.broker_plist.read_bytes()), ([], plist))
        self.assertGreater(len(self.sleeps), 10)  # Bounded polling, not a hang.
        self.assertTrue(self.connect_lock_free())

    def test_a_stopped_broker_is_restarted_when_the_helper_job_cannot_be_bootstrapped(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True, bootstrap_code=5)
        value, code = self.connect(launchd, broker=FakeBroker('blocked'))
        self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'connect_launch_failed',
                                             'next_action': 'run_connect_in_native_terminal'}))
        self.assertEqual(len(launchd.bootstrapped), 1)
        self.assertEqual((launchd.broker_bootstraps, launchd.broker_loaded), (1, True))
        self.assertFalse(job_plist(self.paths).exists())

    def test_a_broker_that_cannot_be_restarted_after_our_stop_says_so(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        for options in [dict(bootstrap_code=5), dict()]:
            with self.subTest(options), SessionOwnerLease(self.paths.lock) if not options else contextlib.nullcontext():
                launchd = self.launchd(broker_loaded=True, broker_bootstrap_code=5, **options)
                value, code = self.connect(launchd, broker=FakeBroker('blocked'))
                self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'broker_start_failed',
                                                     'next_action': 'run_connect_in_native_terminal'}))
                self.assertEqual(launchd.broker_bootstraps, 1)

    def test_the_check_is_rerun_right_before_the_stop_and_only_still_blocked_is_stopped(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        value, code = self.connect(launchd, broker=FakeBroker(['blocked', 'ready']))
        self.assertEqual((code, value), (0, {'status': 'already_connected'}))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
        for later in ['error', BrokerUnavailable('restarting')]:
            with self.subTest(later=later), SessionOwnerLease(self.paths.lock):  # The broker still owns it.
                launchd = self.launchd(broker_loaded=True)
                value, code = self.connect(launchd, broker=FakeBroker(['blocked', later]))
                self.assertEqual((code, value['code'], value['next_action']),
                                 (2, 'owner_busy', 'check_existing_broker_without_stopping_it'))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_a_failed_stop_keeps_the_broker_and_refuses(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        real = launchd.__call__
        def failing(args, **kwargs):
            if args[1] == 'bootout':
                launchd.calls.append(list(args))
                return completed(args, 5)
            return real(args, **kwargs)
        value, code = self.connect(failing, broker=FakeBroker('blocked'))
        # A stop was attempted: the restore finds the broker still loaded and touches nothing more.
        self.assertEqual((code, value['code'], value['next_action']), (2, 'owner_busy', 'run_connect_again'))
        self.assertEqual(launchd.verbs('bootstrap'), [])
        self.assertTrue(launchd.broker_loaded)
        self.assertEqual([call[1] for call in launchd.calls if call[2].endswith(LAUNCH_AGENT_LABEL)][-1], 'print')

    def test_running_helper_means_in_progress_and_blocks_both_stop_and_status_reset(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        self.status_file('waiting_for_scan')
        for broker in [None, FakeBroker('blocked')]:  # A starting broker mid-handoff must not be stopped.
            with self.subTest(broker=broker):
                launchd = self.launchd(job='running', broker_loaded=True)
                value, code = self.connect(launchd, broker=broker)
                self.assertEqual((code, value), (0, {'status': 'connect_in_progress', 'next_action': 'wait'}))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
                self.assertTrue(connect_status_path(self.paths).exists())

    def test_loaded_but_idle_job_is_booted_out_then_bootstrapped(self):
        for idle in ['not running', 'exited']:
            with self.subTest(idle):
                launchd = self.launchd(job=idle)
                value, code = self.connect(launchd)
                self.assertEqual((code, value['status']), (0, 'screen_opened'))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [
                    ['bootout', 'gui/%d/%s' % (UID, CONNECT_JOB_LABEL)],
                    ['bootstrap', 'gui/%d' % UID, str(job_plist(self.paths))]])

    def test_any_loaded_job_state_but_an_explicit_idle_one_is_a_live_helper(self):
        self.status_file('waiting_for_scan')
        for busy in ['running', 'spawn scheduled', 'waiting', 'spawning', 'no state line']:
            with self.subTest(busy):
                launchd = self.launchd(job=busy)
                value, code = self.connect(launchd)
                self.assertEqual((code, value), (0, {'status': 'connect_in_progress', 'next_action': 'wait'}))
                self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
                self.assertTrue(connect_status_path(self.paths).exists())

    def test_launchctl_failures_are_connect_launch_failed_with_the_terminal_fallback(self):
        cases = {'bootstrap refused': dict(bootstrap_code=5), 'launchctl unusable': dict(raises=PermissionError()),
                 'timeout': dict(raises=subprocess.TimeoutExpired('launchctl', 10)),
                 'unverifiable state': dict(print_code=5)}
        for name, options in cases.items():
            with self.subTest(name):
                launchd = self.launchd(**options)
                value, code = self.connect(launchd)
                self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'connect_launch_failed',
                                                     'next_action': 'run_connect_in_native_terminal'}))
                self.assertFalse(job_plist(self.paths).exists(), 'a refused start leaves no plist behind')

    def test_job_plist_is_exact_owner_only_and_replaces_only_a_stale_regular_file(self):
        run = self.run_dir()
        stale = job_plist(self.paths)
        stale.write_bytes(b'stale leftover of a killed helper')
        launchd = self.launchd()
        value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        written, existed = launchd.bootstrapped[0]
        self.assertEqual(plistlib.loads(written), {
            'Label': CONNECT_JOB_LABEL,
            'ProgramArguments': [str(Path(sys.executable).absolute()), '-m', 'telegram_search_mcp.setup_cli',
                                 'connect-helper'],
            'RunAtLoad': True, 'KeepAlive': False, 'LimitLoadToSessionType': 'Aqua', 'ProcessType': 'Interactive',
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null', 'Umask': 63,
            'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}})
        metadata = stale.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual((stat.S_IMODE(metadata.st_mode), metadata.st_nlink, metadata.st_uid), (0o600, 1, os.getuid()))
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        self.assertEqual(stale.parent, self.paths.lock.parent)
        self.assertNotIn(b'broker', written)

    def test_a_symlink_or_directory_at_the_plist_name_is_refused_and_not_followed(self):
        run = self.run_dir()
        target = self.root / 'victim.txt'
        target.write_bytes(b'precious')
        for make in [lambda p: p.symlink_to(target), lambda p: p.mkdir()]:
            with self.subTest(make=make):
                plist = job_plist(self.paths)
                make(plist)
                launchd = self.launchd()
                value, code = self.connect(launchd)
                self.assertEqual((code, value['code']), (2, 'connect_launch_failed'))
                self.assertEqual(launchd.verbs('bootstrap'), [])
                self.assertEqual(target.read_bytes(), b'precious')
                if plist.is_dir() and not plist.is_symlink():
                    plist.rmdir()
                else:
                    plist.unlink()
        self.assertEqual(sorted(p.name for p in run.iterdir()), ['broker.lock', 'connect.lock'])

    def test_bundle_mode_uses_the_receipt_bound_selection_for_the_helper(self):
        selection = mock.Mock()
        selection.arguments.side_effect = lambda action: ['/bundle/python3', '-I', '-S', '-B', '/bundle/launcher.py', action]
        launchd = self.launchd()
        with mock.patch.object(bundle_runtime, '_SELECTION', selection):
            value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        selection.arguments.assert_called_once_with('connect-helper')
        self.assertEqual(plistlib.loads(launchd.bootstrapped[0][0])['ProgramArguments'],
                         ['/bundle/python3', '-I', '-S', '-B', '/bundle/launcher.py', 'connect-helper'])

    def test_a_broken_bundle_binding_refuses_instead_of_starting_a_helper(self):
        selection = mock.Mock()
        selection.arguments.side_effect = bundle_runtime.BundleError('receipt_mismatch')
        launchd = self.launchd()
        with mock.patch.object(bundle_runtime, '_SELECTION', selection):
            value, code = self.connect(launchd)
        self.assertEqual((code, value['code']), (2, 'connect_launch_failed'))
        self.assertEqual(launchd.verbs('bootstrap'), [])

    def test_native_terminal_runs_the_helper_inline_without_launchd_start(self):
        self.status_file('failed', 'owner_busy')
        seen = []
        def inline():
            seen.append(connect_status_path(self.paths).exists())
            return ConnectResult('ready', None, True, True)
        launchd = self.launchd()
        value, code = self.connect(launchd, terminal=Terminal(), inline=inline)
        self.assertEqual((code, value), (0, {'status': 'ready', 'code': None, 'next_action': 'none'}))
        self.assertEqual(seen, [False])  # The stale outcome is cleared first.
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
        for result, expected in [(ConnectResult('failed', 'unsupported_auth_state'), 'use_terminal_setup_fallback'),
                                 (ConnectResult('expired', 'expired'), 'run_connect_again')]:
            value, code = self.connect(self.launchd(), terminal=Terminal(), inline=lambda r=result: r)
            self.assertEqual((code, value['next_action']), (2, expected))

    def test_inline_run_is_still_refused_while_a_helper_job_runs(self):
        value, code = self.connect(self.launchd(job='running'), terminal=Terminal())
        self.assertEqual((code, value['status']), (0, 'connect_in_progress'))

    def test_connect_never_prints_or_returns_a_url_or_secret_and_swallows_unexpected_errors(self):
        launchd = self.launchd()
        launchd.on_print = mock.Mock(side_effect=ValueError(URL_CANARY))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            value, code = self.connect(launchd)
        self.assertEqual((code, value['code']), (2, 'dependency_unavailable'))
        self.assertNotIn(URL_CANARY, json.dumps(value) + out.getvalue() + err.getvalue())

    def test_a_finished_helper_in_its_linger_is_awaited_then_a_fresh_start_follows(self):
        self.status_file('failed', 'auth_request_failed')  # The previous run ended; its page lingers.
        release = self.hold_connect_lock()  # The lingering helper still holds connect.lock ...
        launchd = self.launchd(job='running')  # ... and its job still runs.
        def sleeper(seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) == 30:  # Linger over: the helper exits.
                release()
                launchd.job = 'not running'
        value, code = self.connect(launchd, sleeper=sleeper)
        self.assertEqual((code, value), (0, {'status': 'screen_opened', 'next_action': 'wait'}))
        self.assertEqual(self.sleeps[:30], [0.1] * 30)
        self.assertEqual(len(launchd.bootstrapped), 1)
        self.assertFalse(launchd.bootstrapped[0][1], 'the old outcome is cleared before the fresh helper starts')

    def test_a_finished_helper_whose_job_never_exits_is_booted_out_after_the_linger_window(self):
        self.status_file('cancelled', 'cancelled')
        launchd = self.launchd(job='running')
        value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        self.assertEqual(len(self.sleeps), connect_cli.LINGER_WAIT / 0.1)
        self.assertEqual(connect_cli.LINGER_WAIT, 15)
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [
            ['bootout', 'gui/%d/%s' % (UID, CONNECT_JOB_LABEL)],
            ['bootstrap', 'gui/%d' % UID, str(job_plist(self.paths))]])

    def test_a_lock_still_held_past_the_linger_window_without_a_job_stays_in_progress(self):
        self.status_file('ready')
        self.hold_connect_lock()  # An inline run in Terminal, not our job: never booted out.
        launchd = self.launchd()
        value, code = self.connect(launchd)
        self.assertEqual((code, value), (0, {'status': 'connect_in_progress', 'next_action': 'wait'}))
        self.assertEqual(len(self.sleeps), 150)
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_a_hung_helper_past_its_expiry_is_booted_out_and_replaced(self):
        self.status_file('waiting_for_scan', expires_at=time.time() - 61)
        release = self.hold_connect_lock()  # The hung helper holds connect.lock until it is killed.
        launchd = self.launchd(job='running')
        launchd.on_bootout = release
        value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [
            ['bootout', 'gui/%d/%s' % (UID, CONNECT_JOB_LABEL)],
            ['bootstrap', 'gui/%d' % UID, str(job_plist(self.paths))]])
        self.assertEqual(self.sleeps, [])  # No linger wait: an expired run is not lingering.

    def test_a_helper_within_its_grace_is_not_hung(self):
        self.status_file('waiting_for_scan', expires_at=time.time() - 30)
        self.hold_connect_lock()
        launchd = self.launchd(job='running')
        value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'connect_in_progress'))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])

    def test_connect_holds_its_lock_only_until_the_job_is_bootstrapped(self):
        launchd = self.launchd()
        seen = []
        launchd.on_bootstrap = lambda: seen.append(self.connect_lock_free())
        value, code = self.connect(launchd)
        self.assertEqual((code, value['status']), (0, 'screen_opened'))
        self.assertEqual(seen, [False])
        self.assertTrue(self.connect_lock_free())
        self.assertEqual(stat.S_IMODE(connect_cli.connect_lock_path(self.paths).stat().st_mode), 0o600)

    def test_a_held_connect_lock_means_in_progress_before_any_stop_or_reset(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        self.status_file('waiting_for_scan')
        self.hold_connect_lock()
        launchd = self.launchd(broker_loaded=True)
        value, code = self.connect(launchd, broker=FakeBroker('blocked'))
        self.assertEqual((code, value), (0, {'status': 'connect_in_progress', 'next_action': 'wait'}))
        self.assertEqual(launchd.verbs('bootout', 'bootstrap'), [])
        self.assertTrue(connect_status_path(self.paths).exists())

    def test_two_concurrent_connects_start_exactly_one_helper(self):
        launchd = self.launchd()
        second = []
        def race():  # The second connect arrives while the first is inside its critical section.
            if not second:
                second.append(self.connect(launchd))
        launchd.on_bootstrap = race
        first = self.connect(launchd)
        self.assertEqual(first, ({'status': 'screen_opened', 'next_action': 'wait'}, 0))
        self.assertEqual(second, [({'status': 'connect_in_progress', 'next_action': 'wait'}, 0)])
        self.assertEqual(len(launchd.bootstrapped), 1)

    def test_two_threaded_connects_start_exactly_one_helper(self):
        for _ in range(20):
            connect_status_path(self.paths).unlink(missing_ok=True)
            launchd = self.launchd(bootstrap_state='spawn scheduled')
            barrier, results = threading.Barrier(2), []
            def run():
                barrier.wait()
                results.append(self.connect(launchd, sleeper=lambda _: None))
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(sorted(value['status'] for value, _ in results), ['connect_in_progress', 'screen_opened'])
            self.assertEqual(len(launchd.bootstrapped), 1)

    def test_inline_run_holds_the_connect_lock_for_its_whole_run(self):
        seen = []
        def inline():
            seen.append(self.connect_lock_free())
            return ConnectResult('failed', 'cancelled')
        value, code = self.connect(self.launchd(), terminal=Terminal(), inline=inline)
        self.assertEqual((code, value['code']), (2, 'cancelled'))
        self.assertEqual(seen, [False])
        self.assertTrue(self.connect_lock_free())

    def test_an_inline_run_that_raises_after_our_stop_still_restores_the_broker(self):
        self.enroll()
        self.socket()
        self.install_broker_plist()
        launchd = self.launchd(broker_loaded=True)
        def inline():
            raise RuntimeError('helper could not be built')
        value, code = self.connect(launchd, broker=FakeBroker('blocked'), terminal=Terminal(), inline=inline)
        self.assertEqual((code, value['code']), (2, 'dependency_unavailable'))
        self.assertEqual((launchd.broker_bootstraps, launchd.broker_loaded), (1, True))
        self.assertTrue(self.connect_lock_free())

    def test_lease_busy_after_no_stop_refuses_before_clearing_status(self):
        self.status_file('waiting_for_scan')
        launchd = self.launchd()
        with SessionOwnerLease(self.paths.lock):
            value, code = self.connect(launchd)
        self.assertEqual((code, value['code']), (2, 'owner_busy'))
        self.assertTrue(connect_status_path(self.paths).exists())
        self.assertEqual(launchd.verbs('bootstrap'), [])


def job_plist(paths):
    return paths.lock.parent / (CONNECT_JOB_LABEL + '.plist')


class WaitTests(Harness):
    class Clock:
        def __init__(self):
            self.now, self.hooks = 0.0, []
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.now += seconds
            if self.hooks:
                self.hooks.pop(0)()

    def wait(self, launchd, clock=None, **options):
        clock = clock or self.Clock()
        self.clock = clock
        return connect_cli.wait(self.paths, runner=launchd, uid=UID, sleeper=clock.sleep,
                                monotonic=clock.monotonic, **options)

    def test_each_terminal_phase_is_reported_at_once_with_the_connect_result_mapping(self):
        cases = [('ready', None, 0, 'none'), ('failed', 'owner_busy', 2, 'check_existing_broker_without_stopping_it'),
                 ('failed', 'unsupported_auth_state', 2, 'use_terminal_setup_fallback'),
                 ('failed', 'broker_incompatible', 2, 'use_matching_installed_package_and_policy'),
                 ('failed', 'password_submission_uncertain', 2, 'run_connect_again'),
                 ('expired', 'expired', 2, 'run_connect_again'), ('cancelled', 'cancelled', 2, 'run_connect_again')]
        for phase, error, exit_code, action in cases:
            with self.subTest(phase=phase, code=error):
                self.status_file(phase, error)
                launchd = self.launchd(job='absent')  # A finished helper has already gone: still reported.
                value, code = self.wait(launchd)
                self.assertEqual((code, value), (exit_code, {'status': phase, 'code': error, 'next_action': action}))
                self.assertEqual(self.clock.now, 0.0)
        self.assertEqual(set(p for p, _, _, _ in cases) | {'starting', 'waiting_for_scan', 'waiting_for_password',
                                                           'authorizing', 'handing_off'}, set(PHASES))

    def test_phases_progress_until_a_terminal_one_arrives(self):
        self.status_file('starting')
        clock = self.Clock()
        clock.hooks = [lambda: self.status_file('waiting_for_scan'), lambda: self.status_file('authorizing'),
                       lambda: self.status_file('handing_off'), lambda: self.status_file('ready')]
        value, code = self.wait(self.launchd(job='running'), clock)
        self.assertEqual((code, value['status']), (0, 'ready'))
        self.assertEqual(clock.now, 4.0)

    def test_deadline_returns_the_current_phase_so_the_agent_can_wait_again(self):
        self.status_file('waiting_for_scan')
        value, code = self.wait(self.launchd(job='running'))
        self.assertEqual((code, value), (0, {'status': 'waiting_for_scan', 'next_action': 'scan_qr_on_this_mac_screen'}))
        self.assertEqual(self.clock.now, connect_cli.WAIT_SECONDS)
        self.assertEqual(connect_cli.WAIT_SECONDS, 540)
        for phase, expected in [('waiting_for_password', 'wait'), ('authorizing', 'wait'), ('handing_off', 'wait')]:
            with self.subTest(phase=phase):
                self.status_file(phase)
                value, code = self.wait(self.launchd(job='running'), timeout=5)
                self.assertEqual((code, value), (0, {'status': phase, 'next_action': expected}))
        connect_status_path(self.paths).unlink()
        value, code = self.wait(self.launchd(job='running'), timeout=5)  # Helper still starting up.
        self.assertEqual((code, value), (0, {'status': 'starting', 'next_action': 'wait'}))

    def test_a_vanished_helper_is_helper_exited_unless_it_finished_in_between(self):
        for states in [None, ('starting', None), ('waiting_for_scan', None), ('authorizing', None),
                       ('waiting_for_password', 'invalid_password')]:
            for job in ['absent', 'not running', 'exited']:
                with self.subTest(status=states, job=job):
                    if states is None:
                        connect_status_path(self.paths).unlink(missing_ok=True)
                    else:
                        self.status_file(*states)
                    value, code = self.wait(self.launchd(job=job))
                    self.assertEqual((code, value), (2, {'status': 'failed', 'code': 'helper_exited',
                                                         'next_action': 'run_connect_again'}))
        connect_status_path(self.paths).unlink()
        launchd = self.launchd(job='absent')
        prints = []
        def finish_on_second_look():
            prints.append(1)
            if len(prints) == 2:
                self.status_file('ready')
        launchd.on_print = finish_on_second_look
        value, code = self.wait(launchd)
        self.assertEqual((code, value['status']), (0, 'ready'))

    def test_a_helper_still_spawning_is_not_declared_dead_after_one_look(self):
        self.status_file('starting')
        launchd = self.launchd(job='absent')
        clock = self.Clock()
        clock.hooks = [lambda: setattr(launchd, 'job', 'spawn scheduled')]
        value, code = self.wait(launchd, clock, timeout=3)
        self.assertEqual((code, value['status']), (0, 'starting'))

    def test_any_loaded_job_state_but_an_explicit_idle_one_counts_as_alive(self):
        self.status_file('waiting_for_scan')
        for state in ['running', 'spawn scheduled', 'waiting', 'no state line']:
            with self.subTest(state):
                value, code = self.wait(self.launchd(job=state), timeout=5)
                self.assertEqual((code, value['status']), (0, 'waiting_for_scan'))

    def test_only_our_job_or_the_connect_lock_mean_alive_never_the_session_lease(self):
        self.status_file('waiting_for_scan')
        with SessionOwnerLease(self.paths.lock):  # A broker holding the session is not a helper.
            value, code = self.wait(self.launchd(job='absent'), timeout=5)
        self.assertEqual((code, value['code']), (2, 'helper_exited'))
        release = self.hold_connect_lock()  # An inline helper in Terminal holds connect.lock.
        for launchd in [self.launchd(job='absent'), self.launchd(raises=PermissionError())]:
            value, code = self.wait(launchd, timeout=5)
            self.assertEqual((code, value['status']), (0, 'waiting_for_scan'))
        release()
        value, code = self.wait(self.launchd(raises=PermissionError()), timeout=5)
        self.assertEqual((code, value['code']), (2, 'helper_exited'))

    def test_wait_never_takes_the_session_lease(self):
        self.status_file('handing_off')
        def forbidden(*args, **kwargs):
            raise AssertionError('wait must not probe the owner lease')
        with mock.patch.object(connect_cli, 'SessionOwnerLease', side_effect=forbidden), \
             mock.patch('telegram_search_mcp.session_owner.SessionOwnerLease', side_effect=forbidden):
            for job in ['running', 'absent']:
                value, code = self.wait(self.launchd(job=job), timeout=3)
                self.assertNotEqual(value.get('code'), 'dependency_unavailable')

    def test_a_status_past_its_expiry_is_helper_exited_whatever_still_looks_alive(self):
        # A helper killed mid-handoff (or long ago) while a broker now holds the session.
        for phase in ('handing_off', 'waiting_for_scan', 'starting', 'authorizing'):
            with self.subTest(phase):
                self.status_file(phase, expires_at=time.time() - 61)
                release = self.hold_connect_lock()
                with SessionOwnerLease(self.paths.lock):
                    value, code = self.wait(self.launchd(job='running'))
                release()
                self.assertEqual((code, value), (2, {'status': 'failed', 'code': 'helper_exited',
                                                     'next_action': 'run_connect_again'}))
                self.assertEqual(self.clock.now, 0.0)
        self.status_file('failed', 'owner_busy', expires_at=time.time() - 3600)  # Terminal always reports.
        self.assertEqual(self.wait(self.launchd(job='absent'))[0]['code'], 'owner_busy')

    def test_a_status_within_its_grace_is_still_waited_on(self):
        self.status_file('handing_off', expires_at=time.time() - 30)
        value, code = self.wait(self.launchd(job='running'), timeout=5)
        self.assertEqual((code, value), (0, {'status': 'handing_off', 'next_action': 'wait'}))

    def test_wait_creates_nothing_in_a_missing_run_directory(self):
        value, code = self.wait(self.launchd(job='absent'))
        self.assertEqual((code, value['code']), (2, 'helper_exited'))
        self.assertFalse(self.paths.lock.parent.exists())
        self.run_dir()
        self.wait(self.launchd(job='absent'))
        self.assertEqual(list(self.paths.lock.parent.iterdir()), [])

    def test_a_corrupt_keychain_record_is_not_answered_with_connect_again(self):
        self.status_file('failed', 'credential_invalid_record')
        value, code = self.wait(self.launchd(job='absent'))
        self.assertEqual((code, value), (2, {'status': 'failed', 'code': 'credential_invalid_record',
                                             'next_action': 'repair_keychain_record_manually'}))

    def test_untrusted_status_files_are_ignored_and_never_reported_as_ready(self):
        run = self.run_dir()
        target = connect_status_path(self.paths)
        good = json.dumps({'phase': 'ready', 'code': None, 'updated_at': '2026-10-16T10:00:00Z',
                           'expires_at': '2026-10-16T10:10:00Z'}).encode()
        def bad(**changes):
            record = json.loads(good)
            record.update(changes)
            return json.dumps(record).encode()
        cases = {
            'not json': b'ready', 'empty': b'', 'list': b'[]', 'extra key': bad(extra=1),
            'missing key': json.dumps({'phase': 'ready'}).encode(),
            'duplicate key': good[:-1] + b',"phase":"failed"}', 'unknown phase': bad(phase='done'),
            'phase type': bad(phase=['ready']), 'unknown code': bad(phase='failed', code='made_up'),
            'code type': bad(code=7), 'time shape': bad(updated_at='yesterday'), 'time type': bad(expires_at=5),
            'nan': good.replace(b'null', b'NaN'), 'non ascii': good.replace(b'ready', 'reaédy'.encode()),
            'oversized': good + b' ' * 2048, 'huge nested': b'[' * 100000,
        }
        for name, data in cases.items():
            with self.subTest(name):
                target.unlink(missing_ok=True)
                target.write_bytes(data)
                target.chmod(0o600)
                self.assertIsNone(connect_cli.read_connect_status(self.paths))
                value, code = self.wait(self.launchd(job='running'), timeout=2)
                self.assertEqual((code, value), (0, {'status': 'starting', 'next_action': 'wait'}))
        target.unlink()
        target.write_bytes(good)
        for mode in [0o644, 0o640, 0o400, 0o666]:
            with self.subTest(mode=oct(mode)):
                target.chmod(mode)
                self.assertIsNone(connect_cli.read_connect_status(self.paths))
        target.chmod(0o600)
        self.assertEqual(connect_cli.read_connect_status(self.paths), ('ready', None))
        link = run / 'second-name'
        os.link(target, link)  # Hard-linked file.
        self.assertIsNone(connect_cli.read_connect_status(self.paths))
        link.unlink()
        moved = self.root / 'elsewhere.json'
        moved.write_bytes(good)
        moved.chmod(0o600)
        target.unlink()
        target.symlink_to(moved)  # Symlink to an otherwise perfect file.
        self.assertIsNone(connect_cli.read_connect_status(self.paths))
        value, code = self.wait(self.launchd(job='running'), timeout=2)
        self.assertEqual(value['status'], 'starting')

    def test_every_documented_code_and_phase_of_the_status_file_is_accepted(self):
        for phase in PHASES:
            for code in [None, *sorted(STATUS_CODES)[:3]]:
                self.status_file(phase, code)
                self.assertEqual(connect_cli.read_connect_status(self.paths), (phase, code))

    def test_missing_run_directory_is_just_no_status(self):
        self.assertIsNone(connect_cli.read_connect_status(self.paths))

    def test_unexpected_errors_and_bad_options_become_a_redacted_failure(self):
        launchd = self.launchd(job='running')
        value, code = connect_cli.wait(self.paths, runner=launchd, uid=UID, timeout=-1)
        self.assertEqual((code, value['code']), (2, 'dependency_unavailable'))


class ApiCredentialsTests(Harness):
    def run_command(self, terminal, store=None, **options):
        store = MemoryStore() if store is None else store
        built = []
        def factory():
            built.append(store)
            return store
        return connect_cli.api_credentials(self.paths, terminal=terminal, store_factory=factory, **options), store, built

    def test_anything_but_a_secure_native_terminal_is_refused_before_the_store_exists(self):
        terminal = Terminal(refused=True)
        (value, code), store, built = self.run_command(terminal)
        self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'secure_terminal_required',
                                            'next_action': 'run_in_foreground_native_terminal'}))
        self.assertEqual((built, terminal.prompts, store.gets, store.puts), ([], [], [], []))

    def test_an_existing_enrollment_refuses_before_any_prompt_or_keychain_access(self):
        self.enroll()
        terminal = Terminal()
        (value, code), store, built = self.run_command(terminal)
        self.assertEqual((code, value['code'], value['next_action']), (2, 'enrollment_exists', 'run_connect'))
        self.assertEqual((built, terminal.prompts, store.puts), ([], [], []))

    def test_legacy_session_is_refused_not_overwritten(self):
        self.legacy_session()
        (self.paths.session / 'td.binlog').write_bytes(b'x')
        (value, code), store, built = self.run_command(Terminal())
        self.assertEqual((code, value['code']), (2, 'legacy_unmanaged'))
        self.assertEqual((built, store.puts), ([], []))

    def test_invalid_typed_input_is_refused_and_nothing_is_written(self):
        for values in [{'api_id': b'not-a-number', 'api_hash': b'a' * 32}, {'api_id': b'123456', 'api_hash': b'short'},
                       {'api_id': b'0', 'api_hash': b'a' * 32}, {'api_id': b'123456', 'api_hash': b'z' * 32}]:
            with self.subTest(values=values):
                terminal = Terminal(values)
                (value, code), store, _ = self.run_command(terminal)
                self.assertEqual((code, value['code']), (2, 'invalid_input'))
                self.assertEqual(store.puts, [])  # Both validated before the first write.
                self.assertNotIn(values['api_hash'].decode(), json.dumps(value))

    def test_add_only_success_stores_both_values_and_prints_no_secret(self):
        terminal = Terminal({'api_id': b'123456', 'api_hash': b'c' * 32})
        (value, code), store, _ = self.run_command(terminal)
        self.assertEqual((code, value), (0, {'status': 'stored'}))
        self.assertEqual(terminal.prompts, ['api_id', 'api_hash'])
        self.assertEqual(store.puts, ['api_id', 'api_hash'])
        self.assertEqual((store.values['api_id'].reveal(), store.values['api_hash'].reveal()), (b'123456', b'c' * 32))
        self.assertFalse(EnrollmentStore(self.paths).read(), 'no enrollment is created by the override')

    def test_partial_pair_resumes_add_only_and_never_overwrites(self):
        store = MemoryStore({'api_id': SecretValue(b'777')})
        terminal = Terminal({'api_hash': b'd' * 32})
        (value, code), _, _ = self.run_command(terminal, store)
        self.assertEqual((code, value['status']), (0, 'stored'))
        self.assertEqual((terminal.prompts, store.puts), (['api_hash'], ['api_hash']))
        self.assertEqual(store.values['api_id'].reveal(), b'777')

    def test_a_denied_write_is_a_redacted_credential_refusal(self):
        store = MemoryStore()
        store.fail_account = 'api_hash'
        (value, code), _, _ = self.run_command(Terminal({'api_id': b'123456', 'api_hash': b'e' * 32}), store)
        self.assertEqual((code, value['code'], value['next_action']),
                         (2, 'credential_denied', 'run_api_credentials_in_native_terminal'))
        self.assertEqual(sorted(store.values), ['api_id'])

    def test_a_corrupt_existing_record_is_refused(self):
        store = MemoryStore({'api_id': SecretValue(b'abc')})
        (value, code), _, _ = self.run_command(Terminal(), store)
        self.assertEqual((code, value), (2, {'status': 'refused', 'code': 'credential_invalid_record',
                                            'next_action': 'repair_keychain_record_manually'}))
        self.assertEqual(store.puts, [])

    def test_a_complete_existing_pair_is_already_present_and_nothing_is_written(self):
        store = MemoryStore({'api_id': SecretValue(b'123456'), 'api_hash': SecretValue(b'c' * 32)})
        terminal = Terminal()
        (value, code), _, _ = self.run_command(terminal, store)
        self.assertEqual((code, value), (0, {'status': 'already_present'}))
        self.assertEqual((terminal.prompts, store.puts), ([], []))

    def test_cancel_and_timeout_are_redacted_refusals(self):
        from telegram_search_mcp.secure_terminal import TerminalError
        for error, expected in [(KeyboardInterrupt(), 'cancelled'), (EOFError(), 'cancelled'),
                                (TerminalError('timeout'), 'timeout'), (TimeoutError(), 'timeout')]:
            with self.subTest(expected=expected):
                (value, code), store, _ = self.run_command(Terminal({'api_id': error, 'api_hash': b'a' * 32}))
                self.assertEqual((code, value['code']), (2, expected))
                self.assertEqual(store.puts, [])

    def test_a_running_owner_blocks_the_override_before_prompting(self):
        terminal = Terminal()
        with SessionOwnerLease(self.paths.lock):
            (value, code), store, built = self.run_command(terminal)
        self.assertEqual((code, value['code']), (2, 'owner_busy'))
        self.assertEqual((built, terminal.prompts), ([], []))

    def test_the_bundled_identity_is_never_used_to_skip_the_prompts(self):
        # Even with an identity reachable from every import site, the owner's typed pair is stored.
        terminal = Terminal({'api_id': b'123456', 'api_hash': b'a' * 32})
        bundled = ApiCredentials(1, 'f' * 32)
        with mock.patch('telegram_search_mcp.app_identity.bundled_app_identity', return_value=bundled), \
             mock.patch('telegram_search_mcp.setup_wizard.bundled_app_identity', return_value=bundled), \
             mock.patch('telegram_search_mcp.connect_flow.bundled_app_identity', return_value=bundled):
            (value, code), store, _ = self.run_command(terminal)
        self.assertEqual((code, value), (0, {'status': 'stored'}))
        self.assertEqual((terminal.prompts, store.puts), (['api_id', 'api_hash'], ['api_id', 'api_hash']))
        self.assertEqual((store.values['api_id'].reveal(), store.values['api_hash'].reveal()), (b'123456', b'a' * 32))


class FakeUI:
    def __init__(self, log):
        self.log, self.notices = log, []
    def notify(self, phase, code):
        self.log.append('notify')
        self.notices.append((phase, code))


class FakeServer:
    def __init__(self, ui, expires_at, log, *, start_error=None, url=URL_CANARY):
        self.ui, self.expires_at, self.log, self.start_error, self.url = ui, expires_at, log, start_error, url
    def start(self):
        self.log.append('start')
        if self.start_error:
            raise self.start_error
        return self.url
    def close(self):
        self.log.append('close')


class HelperTests(Harness):
    def helper(self, *, run=None, opener=None, own_plist=None, start_error=None, result=None, sleeper=None,
               **options):
        self.log, self.opened, self.run_args, self.restored = [], [], [], []
        self.ui = None
        def ui_factory():
            self.ui = FakeUI(self.log)
            return self.ui
        def server_factory(ui, expires_at):
            self.server = FakeServer(ui, expires_at, self.log, start_error=start_error)
            return self.server
        def default_run(paths, store, provider_factory, ui, **options):
            self.log.append('run')
            self.run_args.append((paths, store, provider_factory, ui, options))
            return result or ConnectResult('ready', None, True, True)
        def default_opener(url):
            self.log.append('open')
            self.opened.append(url)
        def default_sleeper(seconds):
            self.log.append('linger:%s' % seconds)
        handoff = mock.Mock(return_value='ready')
        self.handoff = handoff
        options.setdefault('restore_broker', lambda: (self.log.append('restore'), self.restored.append(1)))
        return connect_cli.connect_helper(self.paths, store='store', provider_factory='provider', handoff=handoff,
            own_plist=own_plist, ui_factory=ui_factory, server_factory=server_factory, run=run or default_run,
            opener=opener or default_opener, clock=lambda: 1000.0, sleeper=sleeper or default_sleeper, **options)

    def test_url_goes_only_to_the_opener_and_nothing_is_printed(self):
        plist = job_plist(self.paths)
        self.run_dir()
        plist.write_bytes(plistlib.dumps({'Label': CONNECT_JOB_LABEL}))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.helper(own_plist=plist)
        self.assertEqual(self.opened, [URL_CANARY])
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual((result.phase, result.exit_code), ('ready', 0))
        self.assertEqual(self.log, ['start', 'open', 'run', 'notify', 'linger:10', 'close'])
        self.assertEqual(self.server.expires_at, 1600.0)
        paths, store, provider, ui, options = self.run_args[0]
        self.assertEqual((paths, store, provider, ui), (self.paths, 'store', 'provider', self.ui))
        self.assertEqual(options['lifetime'], 600)
        self.assertEqual(self.ui.notices, [('ready', None)])
        options['handoff']()
        self.handoff.assert_called_once_with(paths=self.paths)
        self.assertFalse(plist.exists(), 'the helper removes its own plist')
        self.assertNotIn(URL_CANARY, repr(result) + repr(options) + repr(self.server))

    def test_an_opener_failure_naming_the_url_is_swallowed_and_the_run_continues(self):
        def failing(url):
            raise subprocess.TimeoutExpired(['/usr/bin/open', url], 10)
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.helper(opener=failing)
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertIn('run', self.log)

    def test_a_failed_run_is_shown_on_the_page_then_reported_through_the_exit_code(self):
        failed = ConnectResult('failed', 'owner_busy')
        result = self.helper(result=failed)
        self.assertEqual((result.exit_code, self.ui.notices), (2, [('failed', 'owner_busy')]))
        self.assertEqual(self.log[-3:], ['restore', 'linger:10', 'close'])

    def test_an_owner_busy_run_that_wrote_no_status_is_recorded_by_the_helper(self):
        result = self.helper(result=ConnectResult('failed', 'owner_busy'))  # run_connect wrote nothing.
        self.assertEqual(result.code, 'owner_busy')
        self.assertEqual(connect_cli.read_connect_status(self.paths), ('failed', 'owner_busy'))
        value, code = connect_cli.wait(self.paths, runner=self.launchd(job='absent'), uid=UID, sleeper=lambda _: None)
        self.assertEqual((code, value['code']), (2, 'owner_busy'))
        self.helper(result=ConnectResult('failed', 'cancelled'))  # Other codes stay run_connect's to write.
        self.assertEqual(connect_cli.read_connect_status(self.paths), ('failed', 'owner_busy'))

    def test_the_helper_holds_the_connect_lock_for_its_whole_run_including_the_linger(self):
        seen = {}
        def run(paths, store, provider_factory, ui, **options):
            seen['run'] = self.connect_lock_free()
            return ConnectResult('failed', 'expired')
        def sleeper(seconds):
            seen['linger'] = self.connect_lock_free()
        self.helper(run=run, sleeper=sleeper)
        self.assertEqual((seen, self.connect_lock_free()), ({'run': False, 'linger': False}, True))

    def test_a_second_helper_never_runs_while_the_lock_is_held_and_leaves_the_status_alone(self):
        self.status_file('waiting_for_scan')
        before = connect_status_path(self.paths).read_bytes()
        self.hold_connect_lock()
        sleeps = []
        result = self.helper(sleeper=sleeps.append)
        self.assertEqual((result.phase, result.code), ('failed', 'connect_in_progress'))
        self.assertEqual(self.log, [])  # No server, opener, run, notice or restore.
        self.assertEqual(sleeps, [0.1] * 100)
        self.assertEqual(connect_status_path(self.paths).read_bytes(), before)

    def test_a_helper_takes_the_lock_once_it_is_released_within_ten_seconds(self):
        release = self.hold_connect_lock()
        sleeps = []
        def sleeper(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 20:
                release()
        result = self.helper(sleeper=sleeper)
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(sleeps[:20], [0.1] * 20)

    def test_inline_mode_relies_on_the_lock_its_connect_already_holds(self):
        self.hold_connect_lock()  # Held by the calling connect.
        result = self.helper(lock_held=True, sleeper=lambda _: None)
        self.assertEqual(result.phase, 'ready')

    def test_a_run_that_does_not_end_ready_restores_the_broker_and_a_ready_one_does_not(self):
        for result, restored in [(ConnectResult('ready', None, True, True), []),
                                 (ConnectResult('failed', 'auth_request_failed'), [1]),
                                 (ConnectResult('expired', 'expired'), [1]), (ConnectResult('cancelled', 'cancelled'), [1])]:
            with self.subTest(result.phase):
                self.helper(result=result)
                self.assertEqual(self.restored, restored)
        def boom(*args, **kwargs):
            raise RuntimeError('provider failed')
        with self.assertRaises(RuntimeError):
            self.helper(run=boom)
        self.assertEqual(self.restored, [1])
        def failing_restore():
            raise launch_agent.LaunchAgentError('no installed broker to restore')
        self.assertEqual(self.helper(result=ConnectResult('failed', 'timeout'), restore_broker=failing_restore).code,
                         'timeout')

    def test_the_real_restore_puts_back_only_a_broker_that_existed_and_is_not_loaded(self):
        import functools
        def restore(launchd):
            return functools.partial(launch_agent.restore_enrolled_broker, plist_path=self.broker_plist,
                                     python_executable=Path(sys.executable), runner=launchd, uid=UID)
        failed = ConnectResult('failed', 'auth_request_failed')
        launchd = self.launchd()  # A new user: no broker plist was ever installed.
        self.helper(result=failed, restore_broker=restore(launchd))
        self.assertFalse(self.broker_plist.exists(), 'never create a broker for a user who had none')
        self.assertEqual((launchd.verbs('bootstrap', 'bootout'), launchd.broker_bootstraps), ([], 0))
        plist = self.install_broker_plist()
        launchd = self.launchd(broker_loaded=False)  # Stopped by connect's reconnect.
        self.helper(result=failed, restore_broker=restore(launchd))
        self.assertEqual((launchd.broker_bootstraps, launchd.broker_loaded), (1, True))
        self.assertEqual(self.broker_plist.read_bytes(), plist)
        launchd = self.launchd(broker_loaded=True)  # Still loaded: nothing to do.
        self.helper(result=failed, restore_broker=restore(launchd))
        self.assertEqual(launchd.verbs('bootstrap', 'bootout'), [])

    def test_cleanup_happens_when_the_run_raises_and_the_error_is_not_swallowed(self):
        plist = job_plist(self.paths)
        self.run_dir()
        plist.write_bytes(b'x')
        def boom(*args, **kwargs):
            raise RuntimeError('provider failed')
        with self.assertRaises(RuntimeError):
            self.helper(run=boom, own_plist=plist)
        self.assertEqual(self.log[-1], 'close')
        self.assertFalse(plist.exists())

    def test_a_server_that_cannot_start_never_runs_and_still_cleans_up(self):
        plist = job_plist(self.paths)
        self.run_dir()
        plist.write_bytes(b'x')
        with self.assertRaises(OSError):
            self.helper(start_error=OSError('bind'), own_plist=plist)
        self.assertEqual(self.log, ['start', 'restore', 'close'])  # Never leaves a stopped broker behind.
        self.assertFalse(plist.exists())

    def test_inline_mode_leaves_any_plist_alone(self):
        plist = job_plist(self.paths)
        self.run_dir()
        plist.write_bytes(b'belongs to a running launchd job')
        self.helper()
        self.assertTrue(plist.exists())

    def test_plist_removal_never_follows_a_link(self):
        victim = self.root / 'victim'
        victim.write_bytes(b'precious')
        plist = job_plist(self.paths)
        self.run_dir()
        plist.symlink_to(victim)
        self.helper(own_plist=plist)
        self.assertEqual(victim.read_bytes(), b'precious')
        self.assertTrue(plist.is_symlink())

    def test_default_opener_sends_the_url_on_stdin_never_in_argv(self):
        self.assertEqual(len(URL_CANARY.partition('#t=')[2]), 43)
        runner = mock.Mock()
        connect_cli.open_url(URL_CANARY, runner=runner)
        runner.assert_called_once_with(['/usr/bin/osascript', '-'], input='open location "%s"\n' % URL_CANARY,
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
        (argv,), options = runner.call_args
        self.assertNotIn('shell', options)
        self.assertEqual(argv, ['/usr/bin/osascript', '-'])
        self.assertFalse(any('127.0.0.1' in part or 'LEAKCANARY' in part for part in argv))
        self.assertNotIn(URL_CANARY, repr({key: value for key, value in options.items() if key != 'input'}))

    def test_default_opener_refuses_anything_but_our_servers_exact_url_shape(self):
        token = 'A' * 43
        good = 'http://127.0.0.1:1/#t=' + token
        for url in ['http://127.0.0.1:50999/#t=' + token + '\n', good + '"', good + '" & do shell script "x',
                    'http://127.0.0.1:50999/#t=' + token[:-1], 'http://127.0.0.1:50999/#t=' + token + 'A',
                    'http://127.0.0.1:123456/#t=' + token, 'http://localhost:1/#t=' + token,
                    'https://127.0.0.1:1/#t=' + token, 'http://127.0.0.1.evil.test:1/#t=' + token,
                    'http://127.0.0.1:1/x#t=' + token, 'http://127.0.0.1:1/?t=' + token, 'http://127.0.0.1/#t=' + token,
                    'http://127.0.0.1:1/#t=' + token[:-3] + '"\n;', 'file:///etc/passwd', '', None, b'x', ['x']]:
            with self.subTest(url=url):
                runner = mock.Mock()
                with self.assertRaises(ValueError) as caught:
                    connect_cli.open_url(url, runner=runner)
                runner.assert_not_called()
                if url:
                    self.assertNotIn(str(url), str(caught.exception))
        runner = mock.Mock()
        connect_cli.open_url(good, runner=runner)
        runner.assert_called_once()

    def test_a_malformed_url_from_the_server_does_not_stop_the_run(self):
        calls = []
        def run(paths, store, provider_factory, ui, **options):
            calls.append('run')
            return ConnectResult('ready', None, True, True)
        system = mock.Mock(side_effect=AssertionError('no process may be started for a refused URL'))
        with mock.patch.dict(connect_cli.open_url.__kwdefaults__, {'runner': system}):
            result = connect_cli.connect_helper(self.paths, store=None, provider_factory=None, handoff=None, run=run,
                ui_factory=lambda: FakeUI([]), sleeper=lambda _: None, restore_broker=mock.Mock(),
                server_factory=lambda ui, expires: FakeServer(ui, expires, [], url='http://127.0.0.1:1/#t=short'))
        self.assertEqual((result.phase, calls), ('ready', ['run']))
        system.assert_not_called()

    def test_real_loopback_server_is_wired_to_the_ui_and_url_reaches_only_the_opener(self):
        seen = {}
        def opener(url):
            seen['url'] = url
        def run(paths, store, provider_factory, ui, **options):
            base, _, token = seen['url'].partition('/#t=')
            request = urllib.request.Request(base + '/api/state', headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request, timeout=5) as response:
                seen['state'] = json.loads(response.read())
            ui.notify('waiting_for_scan', None)
            seen['snapshot'] = ui.snapshot()
            return ConnectResult('ready', None, True, True)
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            result = connect_cli.connect_helper(self.paths, store=None, provider_factory=None, handoff=None,
                                                run=run, opener=opener, sleeper=lambda _: None,
                                                restore_broker=mock.Mock())
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(seen['state']['phase'], 'starting')
        self.assertTrue(0 < seen['state']['expires_in'] <= 600)
        self.assertEqual(seen['snapshot']['phase'], 'waiting_for_scan')
        self.assertTrue(seen['url'].startswith('http://127.0.0.1:'))
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        with self.assertRaises(OSError):  # The server is closed afterwards.
            urllib.request.urlopen(seen['url'].partition('/#')[0] + '/api/state', timeout=2)


class SetupCliDispatchTests(Harness):
    """setup_cli.main is the only entry; its closed grammar routes the new commands and leaks nothing."""
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(setup_cli, 'SetupPaths', return_value=self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = setup_cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_unknown_or_argument_bearing_forms_are_refused_without_echo(self):
        sentinel = 'SYNTHETIC_SECRET_SENTINEL'
        for args in [['connect', sentinel], ['wait', '600'], ['wait', sentinel], ['api-credentials', sentinel],
                     ['connect-helper', sentinel], ['connect', '--url', sentinel], ['connect-helper', 'connect'],
                     [sentinel], ['Connect'], ['connect '], ['--connect'], ['status', 'connect']]:
            with self.subTest(args=args), mock.patch.object(connect_cli, 'connect') as connect, \
                 mock.patch.object(connect_cli, 'wait') as wait, \
                 mock.patch.object(connect_cli, 'connect_helper') as helper, \
                 mock.patch.object(connect_cli, 'api_credentials') as credentials:
                code, out, err = self.main(args)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['code'], 'invalid_arguments')
            self.assertNotIn(sentinel, out + err)
            for fake in (connect, wait, helper, credentials):
                fake.assert_not_called()

    def test_connect_and_wait_print_the_fixed_json_and_never_touch_keychain_or_tdlib(self):
        with mock.patch.object(setup_cli, 'MacOSSecretStore') as store, mock.patch.object(setup_cli, 'LegacyTDJson') as raw, \
             mock.patch.object(connect_cli, 'connect', return_value=({'status': 'screen_opened', 'next_action': 'wait'}, 0)) as connect, \
             mock.patch.object(connect_cli, 'wait', return_value=({'status': 'ready', 'code': None, 'next_action': 'none'}, 0)) as wait:
            self.assertEqual(self.main(['connect']), (0, '{"next_action": "wait", "status": "screen_opened"}\n', ''))
            self.assertEqual(self.main(['wait'])[0], 0)
        store.assert_not_called()
        raw.assert_not_called()
        self.assertIs(connect.call_args.kwargs['client_factory'], setup_cli._client)
        wait.assert_called_once_with(self.paths)

    def test_inline_helper_gets_the_real_store_provider_and_handoff_only_when_it_runs(self):
        result = ConnectResult('ready', None, True, True)
        with mock.patch.object(setup_cli, 'MacOSSecretStore') as store, mock.patch.object(setup_cli, 'LegacyTDJson') as raw, \
             mock.patch.object(connect_cli, 'connect_helper', return_value=result) as helper, \
             mock.patch.object(connect_cli, 'connect', side_effect=lambda paths, **options: (
                 connect_cli._report(*[(r := options['inline']()).phase, r.code]))):
            code, out, _ = self.main(['connect'])
        self.assertEqual((code, json.loads(out)['status']), (0, 'ready'))
        store.assert_called_once_with()
        # Inline, the calling connect already holds connect.lock for the whole run.
        helper.assert_called_once_with(self.paths, store=store.return_value, provider_factory=raw,
                                       handoff=setup_cli.handoff_broker, lock_held=True)

    def test_connect_helper_is_silent_exits_with_the_result_and_removes_only_its_plist(self):
        launchd_env = mock.patch.dict(os.environ, {'XPC_SERVICE_NAME': CONNECT_JOB_LABEL})
        for result, expected in [(ConnectResult('ready', None, True, True), 0), (ConnectResult('failed', 'owner_busy'), 2)]:
            with self.subTest(phase=result.phase), launchd_env, mock.patch.object(setup_cli, 'MacOSSecretStore'), \
                 mock.patch.object(setup_cli, 'LegacyTDJson'), \
                 mock.patch.object(connect_cli, 'connect_helper', return_value=result) as helper:
                code, out, err = self.main(['connect-helper'])
            self.assertEqual((code, out, err), (expected, '', ''))
            self.assertEqual(helper.call_args.kwargs['own_plist'], job_plist(self.paths))
            self.assertNotIn('lock_held', helper.call_args.kwargs)  # Launchd mode takes the lock itself.
        with launchd_env, mock.patch.object(setup_cli, 'MacOSSecretStore'), mock.patch.object(setup_cli, 'LegacyTDJson'), \
             mock.patch.object(connect_cli, 'connect_helper', side_effect=RuntimeError(URL_CANARY)):
            self.assertEqual(self.main(['connect-helper']), (2, '', ''))

    def test_connect_helper_refuses_unless_launched_by_launchd_as_our_job(self):
        self.run_dir()
        plist = job_plist(self.paths)
        plist.write_bytes(b'belongs to whoever launched it')
        for value in [None, '', '0', 'application.com.apple.Terminal.1234', LAUNCH_AGENT_LABEL, CONNECT_JOB_LABEL + 'x']:
            environment = {key: item for key, item in os.environ.items() if key != 'XPC_SERVICE_NAME'}
            if value is not None:
                environment['XPC_SERVICE_NAME'] = value
            with self.subTest(value=value), mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(setup_cli, 'MacOSSecretStore') as store, \
                 mock.patch.object(connect_cli, 'connect_helper') as helper:
                code, out, err = self.main(['connect-helper'])
            self.assertEqual((code, json.loads(out), err), (2, {'status': 'refused',
                'code': 'connect_helper_requires_launchd', 'next_action': 'run_connect'}, ''))
            helper.assert_not_called()
            store.assert_not_called()
        self.assertEqual(plist.read_bytes(), b'belongs to whoever launched it')
        self.assertEqual(sorted(p.name for p in self.paths.lock.parent.iterdir()), [plist.name])

    def test_api_credentials_gets_a_lazy_store_factory(self):
        with mock.patch.object(setup_cli, 'MacOSSecretStore') as store, \
             mock.patch.object(setup_cli, 'NativeTerminal') as terminal, \
             mock.patch.object(connect_cli, 'api_credentials', return_value=({'status': 'stored'}, 0)) as command:
            self.assertEqual(self.main(['api-credentials']), (0, '{"status": "stored"}\n', ''))
        store.assert_not_called()  # Constructed by the command only after the terminal check.
        self.assertIs(command.call_args.kwargs['store_factory'], store)
        self.assertIs(command.call_args.kwargs['terminal'], terminal.return_value)

    def test_real_api_credentials_refuses_without_terminal_and_never_builds_the_store(self):
        with mock.patch.object(setup_cli, 'MacOSSecretStore') as store, \
             mock.patch.object(setup_cli, 'NativeTerminal', return_value=Terminal(refused=True)):
            code, out, _ = self.main(['api-credentials'])
        self.assertEqual((code, json.loads(out)['code']), (2, 'secure_terminal_required'))
        store.assert_not_called()

    def test_help_documents_the_new_commands_and_still_prints_no_state(self):
        code, out, _ = self.main(['--help'])
        self.assertEqual(code, 0)
        for word in ['connect', 'wait', 'api-credentials', 'setup', 'resume', 'status']:
            self.assertIn(word, out)
        self.assertNotIn('127.0.0.1', out)


class LauncherGrammarTests(unittest.TestCase):
    def launcher(self):
        path = Path(__file__).parents[1] / 'packaging/launcher.py'
        spec = importlib.util.spec_from_file_location('connect_cli_launcher_grammar', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_closed_grammar_lists_exactly_the_documented_actions(self):
        self.assertEqual(self.launcher().ACTIONS, ('mcp', 'broker', 'setup', 'resume', 'status', 'connect', 'wait',
                                                   'api-credentials', 'connect-helper'))
        self.assertEqual(set(self.launcher().ACTIONS), {'mcp', 'broker'} | set(setup_cli.COMMANDS))

    def test_new_actions_pass_the_grammar_and_everything_else_stops_before_any_admission(self):
        module = self.launcher()
        sentinel = 'SYNTHETIC_SECRET_SENTINEL'
        class Reached(Exception):
            pass
        for args, admitted in [(['connect'], True), (['wait'], True), (['api-credentials'], True),
                               (['connect-helper'], True), (['setup'], True),
                               (['connect', sentinel], False), (['wait', '10'], False), (['connect-helper', 'x'], False),
                               (['api-credentials', sentinel], False), (['Connect'], False), ([sentinel], False)]:
            with self.subTest(args=args), mock.patch.object(module, '_read', side_effect=Reached()) as reader, \
                 contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(module.main(args), 2)
            self.assertEqual(reader.called, admitted)  # Only a known action reaches receipt admission.
            self.assertNotIn(sentinel, out.getvalue() + err.getvalue())
            self.assertEqual(json.loads(err.getvalue()), {'status': 'refused', 'code': 'bundle_admission_or_install_failed'})

    def test_help_names_the_new_actions_without_credentials_wording(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.launcher().main(['--help']), 0)
        for word in ['connect', 'wait', 'api-credentials']:
            self.assertIn(word, out.getvalue())
        self.assertNotIn('secret', out.getvalue().lower())

    def test_launcher_dispatch_hands_every_non_mcp_action_to_setup_cli(self):
        # Behavioural: a copy laid out as an installed release, admission stubbed, dispatch recorded.
        import shutil
        from telegram_search_mcp import launch_agent as agent
        pin = 'a' * 64
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve() / 'releases' / pin
            release.mkdir(parents=True)
            shutil.copy(Path(__file__).parents[1] / 'packaging/launcher.py', release / 'launcher.py')
            spec = importlib.util.spec_from_file_location('connect_cli_launcher_dispatch', release / 'launcher.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            receipt = json.dumps({'release_root': str(release), 'index_sha256': pin}).encode()
            saved_path = list(sys.path)
            try:
                for action in module.ACTIONS:
                    with self.subTest(action=action), \
                         mock.patch.object(module, '_read', return_value=receipt), \
                         mock.patch.object(module, 'admit') as admit, \
                         mock.patch.object(bundle_runtime, 'activate') as activate, \
                         mock.patch.object(module, 'dispatch_mcp', return_value=11) as mcp, \
                         mock.patch.object(agent, 'run_broker', return_value=12) as broker, \
                         mock.patch.object(setup_cli, 'main', return_value=13) as setup:
                        code = module.main([action])
                        sys.path[:] = saved_path
                    self.assertEqual(code, {'mcp': 11, 'broker': 12}.get(action, 13))
                    admit.assert_called_once_with(release, release / 'bundle-index.json', pin, True)
                    activate.assert_called_once_with(release, pin)
                    self.assertEqual((mcp.called, broker.called), (action == 'mcp', action == 'broker'))
                    if action in ('mcp', 'broker'):
                        setup.assert_not_called()
                    else:
                        setup.assert_called_once_with([action])
            finally:
                sys.path[:] = saved_path


if __name__ == '__main__':
    unittest.main()
