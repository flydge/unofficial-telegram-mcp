"""0.55 phase C: the connect helper's account pin at login, and the QR page that names the alias (spec 3C).

With an alias registry, the authorized user is checked against the pins before the broker is handed the session:
a user pinned under another alias, the default included, is refused with `account_duplicate`, a user other
than the alias's own with `account_mismatch`. Without a registry nothing is read (0.54). The user id is never
written to a result, the status file or the page. Every provider is synthetic; nothing reaches Telegram.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_connect_flow import PARAMETERS, PHONE, READY, BUNDLED, ConnectHarness, FakeUI, QrRaw, other_device
from test_onboarding import MemoryStore

from telegram_search_mcp import (account_registry, bundle_install, connect_cli, connect_flow, connect_page,
                                 connect_server)
from telegram_search_mcp.config import AccountPaths
from telegram_search_mcp.connect_flow import ConnectResult, connect_status_path, run_connect
from telegram_search_mcp.enrollment import EnrollmentStore, SetupPaths
from telegram_search_mcp.session_owner import SessionOwnerLease

USER_A, USER_B, USER_C = 7401234561, 7401234562, 7401234563
# The default page's assets as 0.55 phase B (3d251c9) serves them: a named alias's page never changes them.
DEFAULT_ASSETS = {'/': '2eb5b38ce52639b2608ad1cc161f6164b10861abe4aec0ef14bdad657273d8ef',
                  '/app.js': '86e76e90ac567456f09f54423ab00c412d6484b19f60963ca077c45a2a0b0b72',
                  '/app.css': 'a818f012bee2f1c121b358e7082ec2233b8a53b159247561e57534c41966c5ea'}


def added_root(account):
    """What account-add leaves at a named alias's place (review fix 055d3): its root, owner-only and empty but for its
    run directory. A login takes its connect.lock only inside it and never creates it."""
    for path in (account.root, account.run):
        path.mkdir(mode=0o700)
    return account


class AccountRaw(QrRaw):
    """QrRaw that answers getMe with one user, like an authorized TDLib session."""

    def __init__(self, flow, *, user_id, **kwargs):
        super().__init__(flow, **kwargs)
        self.user_id = user_id

    def send(self, payload):
        if payload['@type'] == 'getMe':
            self.sent.append(dict(payload))
            user = ({'@type': 'user', 'id': self.user_id} if type(self.user_id) is int else
                    {'@type': 'error', 'code': 400, 'message': 'USER PRIVATE'} if self.user_id is None else
                    self.user_id)
            self.queue.append({**user, '@extra': payload['@extra']})
            return
        super().send(payload)


class AliasHarness(ConnectHarness):
    """run_connect on a registered alias's own paths in a short synthetic home."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='c')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.work = AccountPaths.for_alias('work', self.home)
        self.accounts = self.work.accounts_root
        self.paths = SetupPaths.from_account(self.work)
        self.store, self.events, self.raws, self.handoffs = MemoryStore(), [], [], 0
        self.bundled = BUNDLED

    def tearDown(self):
        pass

    def account_raw(self, flow, user_id):
        def factory():
            raw = AccountRaw(flow, user_id=user_id, events=self.events, paths=self.paths)
            self.raws.append(raw)
            self.events.append('create')
            return raw
        return factory

    def login(self, user_id, flow=(PARAMETERS, PHONE, other_device(), READY)):
        return self.connect(list(flow), raw=self.account_raw(list(flow), user_id))

    def pins(self):
        registry = account_registry.load(self.accounts)
        return dict(registry.entries), registry.default_user_id

    def assert_never_reported(self, result, ui):
        reported = (json.dumps(result.to_dict()) + repr(result) + repr(ui.notices)
                    + connect_status_path(self.paths).read_text())
        for user in (USER_A, USER_B, USER_C):
            self.assertNotIn(str(user), reported)


class LoginPinTests(AliasHarness):
    def test_an_account_pinned_under_the_default_is_refused_before_the_handoff(self):
        account_registry.pin_account(self.accounts, 'default', USER_A)
        result, ui = self.login(USER_A)
        self.assertEqual((result.phase, result.code), ('failed', 'account_duplicate'))
        self.assertEqual(result.to_dict()['next_action'], 'review_enrollment_binding_without_resetting')
        self.assert_torn_down_pending(result)
        self.assertEqual(self.sent('getMe'), 1)
        self.assertEqual(self.pins(), ({'work': None}, USER_A))
        self.assertNotIn('handoff', self.events)
        self.assert_never_reported(result, ui)

    def test_an_account_pinned_under_another_alias_is_refused(self):
        account_registry.add_alias('play', self.home, longest_tool='t')
        account_registry.pin_account(self.accounts, 'play', USER_B)
        result, ui = self.login(USER_B)
        self.assertEqual((result.phase, result.code), ('failed', 'account_duplicate'))
        self.assertEqual(self.handoffs, 0)
        self.assertEqual(self.pins(), ({'play': USER_B, 'work': None}, None))
        self.assert_never_reported(result, ui)

    def test_another_user_than_the_aliases_own_is_refused(self):
        account_registry.pin_account(self.accounts, 'work', USER_B)
        result, ui = self.login(USER_C)
        self.assertEqual((result.phase, result.code), ('failed', 'account_mismatch'))
        self.assertEqual(result.to_dict()['next_action'], 'review_enrollment_binding_without_resetting')
        self.assert_torn_down_pending(result)
        self.assertEqual(self.pins(), ({'work': USER_B}, None))
        self.assert_never_reported(result, ui)

    def test_a_new_account_is_pinned_at_login_and_handed_off(self):
        account_registry.pin_account(self.accounts, 'default', USER_A)
        result, ui = self.login(USER_B)
        self.assertEqual((result.phase, result.code), ('ready', None))
        self.assertEqual(self.handoffs, 1)
        self.assertEqual(self.pins(), ({'work': USER_B}, USER_A))
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'ready')
        # getMe is asked of the authorized session, before its teardown and the handoff.
        self.assertEqual([row['@type'] for row in self.raws[-1].sent][-2:], ['getMe', 'close'])
        self.assert_never_reported(result, ui)
        # The pinned account logs in again: same user, nothing changes.
        result, _ = self.login(USER_B, flow=(READY,))
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(self.pins(), ({'work': USER_B}, USER_A))

    def test_an_alias_removed_meanwhile_or_an_unreadable_account_never_hands_off(self):
        result, _ = self.login({'@type': 'user', 'id': 'seven'})
        self.assertEqual((result.phase, result.code), ('failed', 'auth_request_failed'))
        result, _ = self.login(None)
        self.assertEqual((result.phase, result.code), ('failed', 'auth_request_failed'))
        account_registry.remove_alias('work', self.home)
        result, _ = self.login(USER_B, flow=(READY,))
        self.assertEqual((result.phase, result.code), ('failed', 'alias_unavailable'))
        self.assertEqual(result.to_dict()['next_action'], 'stop_and_report')
        self.assertEqual(self.handoffs, 0)
        self.assertEqual(self.pins(), ({}, None))

    def test_a_busy_registry_is_a_retry_and_an_unsafe_one_stops(self):
        with mock.patch.object(account_registry, 'pin_account',
                               side_effect=account_registry.RegistryError('registry_busy')):
            result, _ = self.login(USER_B)
        self.assertEqual((result.phase, result.code), ('failed', 'dependency_unavailable'))
        with mock.patch.object(account_registry, 'pin_account',
                               side_effect=account_registry.RegistryError('registry_unsafe')):
            result, _ = self.login(USER_B, flow=(READY,))
        self.assertEqual((result.phase, result.code), ('failed', 'alias_unavailable'))
        self.assertEqual(self.handoffs, 0)


class DefaultLoginTests(ConnectHarness):
    def test_without_a_registry_the_default_asks_no_account(self):
        result, _ = self.connect([PARAMETERS, PHONE, other_device(), READY])
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(self.sent('getMe'), 0)


class DefaultRegistryLoginTests(ConnectHarness):
    """The default alias in a synthetic home's own layout, beside a registry that lists `work`."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='d')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        self.paths = SetupPaths.from_account(AccountPaths.for_alias('default', self.home))
        account_registry.add_alias('work', self.home, longest_tool='t')

    def test_with_a_registry_the_default_is_pinned_at_login_and_refused_as_a_duplicate(self):
        accounts = self.paths.account.accounts_root
        self.assertEqual(accounts, account_registry.accounts_root(self.home))
        flow = [PARAMETERS, PHONE, other_device(), READY]

        def raw(user):
            def factory():
                self.raws.append(AccountRaw(flow, user_id=user, events=self.events, paths=self.paths))
                self.events.append('create')
                return self.raws[-1]
            return factory
        account_registry.pin_account(accounts, 'work', USER_B)
        result, _ = self.connect(flow, raw=raw(USER_B))
        self.assertEqual((result.phase, result.code), ('failed', 'account_duplicate'))
        self.assertEqual(self.handoffs, 0)
        result, _ = self.connect(flow, raw=raw(USER_A))
        self.assertEqual((result.phase, result.code), ('ready', None))
        self.assertEqual(account_registry.load(accounts).default_user_id, USER_A)


class CodeTableTests(unittest.TestCase):
    def test_the_pin_codes_are_closed_status_codes_with_their_next_action(self):
        for code, action in (('account_mismatch', 'review_enrollment_binding_without_resetting'),
                             ('account_duplicate', 'review_enrollment_binding_without_resetting'),
                             ('alias_unavailable', 'stop_and_report'),
                             # Review fix 055d3: a named alias's login from an installed release.
                             ('transaction_pending', 'finish_pending_installer_action_then_retry'),
                             ('newer_state_kept', 'upgrade_installation_or_remove_account'),
                             # Critic fix 055f: an older line, before account-add's repair.
                             ('older_state_kept', 'run_account_add_for_alias_then_retry')):
            self.assertIn(code, connect_flow.STATUS_CODES)
            self.assertEqual(connect_flow.next_action('failed', code), action)
            self.assertEqual(connect_cli._report('failed', code), ({'status': 'failed', 'code': code,
                                                                   'next_action': action}, 2))


class AliasPageTests(unittest.TestCase):
    def test_the_default_page_is_unchanged(self):
        self.assertIs(connect_page.assets(None), connect_page.ASSETS)
        self.assertEqual({path: hashlib.sha256(body).hexdigest() for path, (_, body) in connect_page.ASSETS.items()},
                         DEFAULT_ASSETS)

    def test_a_named_aliases_page_names_the_alias_in_both_languages(self):
        assets = connect_page.assets('work')
        self.assertEqual(set(assets), set(connect_page.ASSETS))
        self.assertEqual(assets['/app.css'], connect_page.ASSETS['/app.css'])
        page, script = assets['/'][1].decode(), assets['/app.js'][1].decode()
        self.assertIn('<p class="muted" data-i18n="account">Account alias: work</p>\n</header>', page)
        self.assertIn('"account": "Account alias: work"', script)
        self.assertIn('"account": "Псевдоним аккаунта: work"', script)
        self.assertEqual(page.replace('<p class="muted" data-i18n="account">Account alias: work</p>\n', ''),
                         connect_page.ASSETS['/'][1].decode().replace(connect_page.PRODUCT,
                                                                      connect_page.PRODUCT + ' · work'))
        self.assertIs(connect_page.assets('work'), assets)  # Built once per alias.
        for refused in ('default', 'Work', 'a' * 13, '', 'w!rk', 7):
            with self.subTest(alias=refused), self.assertRaises(ValueError):
                connect_page.assets(refused)

    def test_the_server_serves_the_aliases_page_and_its_state_names_no_account(self):
        ui = connect_server.LoopbackConnectUI()
        with connect_server.ConnectServer(ui, 4102444800, alias='work') as server:
            self.assertIs(server.assets, connect_page.assets('work'))
            self.assertEqual(set(server.state()), {'phase', 'code', 'expires_in', 'password_error',
                                                   'password_pending'})
        with connect_server.ConnectServer(connect_server.LoopbackConnectUI(), 4102444800) as server:
            self.assertIs(server.assets, connect_page.ASSETS)
        with self.assertRaises(ValueError):
            connect_server.ConnectServer(connect_server.LoopbackConnectUI(), 4102444800, alias='default')

    def test_the_helper_opens_the_aliases_page_and_the_default_keeps_its_call(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='p')
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        account_registry.add_alias('work', home, longest_tool='t')
        added_root(AccountPaths.for_alias('work', home))
        seen = []

        class Server:
            def __init__(self, ui, expires_at, **options):
                seen.append(options)

            def start(self):
                return 'http://127.0.0.1:1/#t=' + 'x' * 43

            def close(self):
                pass

        def run(paths, store, provider_factory, ui, **options):
            return ConnectResult('ready', None, True, True)
        for paths in (SetupPaths.from_account(AccountPaths.for_alias('work', home)), SetupPaths(home / 'default')):
            connect_cli.connect_helper(paths, store=None, provider_factory=None, handoff=None, run=run,
                                       ui_factory=lambda: FakeUI(paths), server_factory=Server, opener=lambda _: None,
                                       sleeper=lambda _: None, restore_broker=lambda: None)
        self.assertEqual(seen, [{'alias': 'work'}, {}])


class InstallerTransactionLockTests(unittest.TestCase):
    """Review fix (055d2, lead decision A): while an installer transaction over the aliases runs it holds each alias's
    connect.lock (`bundle_install._LoginLocks`), the one every helper holds for its whole run. The launchd connect
    helper and `connect --account` (the agent's, or inline in the owner's Terminal) then end with their existing code,
    `connect_in_progress`, and change nothing: no status file, screen, run, handoff, broker stop or restore, no job."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='l')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.paths = SetupPaths.from_account(added_root(AccountPaths.for_alias('work', self.home)))
        self.calls = []

        def launchctl(argv, **kwargs):  # No job loaded.
            self.calls.append(argv[1])
            return subprocess.CompletedProcess(argv, 113, '', '')
        self.launchctl = launchctl
        locks = bundle_install._LoginLocks(launchctl, self.home, ['work']).__enter__()
        self.addCleanup(locks.__exit__)
        locks.complete()
        self.assertEqual(locks.held, {'work'})
        self.assertEqual(connect_cli.connect_lock_path(self.paths), self.paths.account.connect_lock)
        self.assertTrue(connect_cli._connect_lock_held(self.paths))
        self.calls.clear()

    def tree(self):
        return sorted(str(p.relative_to(self.paths.account.root)) for p in self.paths.account.root.rglob('*'))

    def test_the_connect_helper_ends_connect_in_progress_and_touches_nothing(self):
        log, before = [], self.tree()
        result = connect_cli.connect_helper(
            self.paths, store=None, provider_factory=None, handoff=lambda **_: log.append('handoff'),
            run=lambda *args, **kwargs: log.append('run'), ui_factory=lambda: log.append('ui'),
            server_factory=lambda *args, **kwargs: log.append('server'), opener=lambda _: log.append('open'),
            sleeper=lambda _: None, restore_broker=lambda: log.append('restore'))
        self.assertEqual((result.phase, result.code), ('failed', 'connect_in_progress'))
        self.assertEqual(log, [])
        self.assertFalse(connect_status_path(self.paths).exists())
        self.assertEqual(self.tree(), before)

    def test_connect_answers_connect_in_progress_and_starts_nothing(self):
        before = self.tree()

        def never(*_args, **_kwargs):
            raise AssertionError('nothing may run')
        value, code = connect_cli.connect(self.paths, client_factory=never, terminal=None, inline=never,
                                          runner=self.launchctl, uid=os.getuid(), sleeper=lambda _: None,
                                          broker_plist=self.paths.account.broker_plist)
        self.assertEqual((code, value), (0, {'status': 'connect_in_progress', 'next_action': 'wait'}))
        self.assertEqual(set(self.calls), {'print'})  # Only looked: no bootout, no bootstrap.
        self.assertFalse(self.paths.account.broker_plist.exists())
        self.assertEqual(self.tree(), before)


class RootlessLoginTests(unittest.TestCase):
    """Review fix (055d3, medium, R1): a login never creates a named alias's root. `connect` (the agent's and inline),
    the connect helper, `cancel` and `api-credentials --account` take its connect.lock only inside an existing root and
    only while the registry lists the alias (before and once held); a missing root or an alias no longer listed
    refuses `alias_unavailable` with nothing created: no root, run directory, lock, status, job or broker change."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='r')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.account = AccountPaths.for_alias('work', self.home)  # Admitted, as by the launcher; no root yet.
        self.paths = SetupPaths.from_account(self.account)
        self.calls = []

        def launchctl(argv, **kwargs):  # No job loaded.
            self.calls.append(argv[1])
            return subprocess.CompletedProcess(argv, 113, '', '')
        self.launchctl = launchctl

    def accounts_tree(self):
        return sorted(str(p.relative_to(self.account.accounts_root)) for p in self.account.accounts_root.rglob('*'))

    def never(self, *_args, **_kwargs):
        raise AssertionError('nothing may run')

    def connect(self, terminal=None):
        return connect_cli.connect(self.paths, client_factory=self.never, terminal=terminal, inline=self.never,
                                   runner=self.launchctl, uid=os.getuid(), sleeper=lambda _: None,
                                   broker_plist=self.account.broker_plist)

    def helper(self):
        log = []
        result = connect_cli.connect_helper(
            self.paths, store=None, provider_factory=None, handoff=lambda **_: log.append('handoff'),
            run=lambda *args, **kwargs: log.append('run'), ui_factory=lambda: log.append('ui'),
            server_factory=lambda *args, **kwargs: log.append('server'), opener=lambda _: log.append('open'),
            sleeper=lambda _: None, restore_broker=lambda: log.append('restore'))
        self.assertEqual(log, [])
        return result

    def assert_every_entry_refuses(self):
        from test_onboarding import Terminal
        before = self.accounts_tree()
        refused = ({'status': 'refused', 'code': 'alias_unavailable', 'next_action': 'stop_and_report'}, 2)
        self.assertEqual(self.connect(), refused)
        self.assertEqual(self.connect(Terminal()), refused)  # Inline in native Terminal.
        result = self.helper()
        self.assertEqual((result.phase, result.code), ('failed', 'alias_unavailable'))
        self.assertEqual(connect_cli.api_credentials(self.paths, terminal=Terminal(), store_factory=self.never),
                         refused)
        self.assertLessEqual(set(self.calls), {'print'})  # Nothing booted out or bootstrapped.
        self.assertEqual(self.accounts_tree(), before)

    def test_a_missing_root_refuses_every_login_and_creates_nothing(self):
        self.assertFalse(self.account.root.exists())
        self.assert_every_entry_refuses()
        self.assertFalse(self.account.root.exists())
        with self.assertRaises(connect_cli.AccountPathsError):
            account_registry.login_lock(self.account)
        self.assertFalse(self.account.root.exists())

    def test_an_alias_no_longer_listed_refuses_with_its_root_in_place(self):
        added_root(self.account)
        account_registry.remove_alias('work', self.home)
        self.assert_every_entry_refuses()
        self.assertFalse(self.account.connect_lock.exists())

    def test_a_listed_alias_with_its_root_takes_only_its_run_directory_and_lock(self):
        self.account.root.mkdir(mode=0o700)  # Its run directory missing too: created inside the root.
        descriptor = account_registry.login_lock(self.account)
        try:
            self.assertEqual(sorted(str(p.relative_to(self.account.root)) for p in self.account.root.rglob('*')),
                             ['run', 'run/connect.lock'])
            self.assertTrue(connect_cli._connect_lock_held(self.paths))
            with self.assertRaises(connect_cli.OwnerLeaseError) as caught:
                account_registry.login_lock(self.account)
            self.assertEqual(caught.exception.code, 'owner_busy')
        finally:
            os.close(descriptor)

    def test_a_lock_taken_while_the_root_moved_is_refused_and_released(self):
        added_root(self.account)
        moved = self.account.root.with_name('removed-work-' + '0' * 32)
        real = connect_cli.account_registry.acquire_owner_lock

        def move_after(path, **kwargs):  # account-remove's move, right after the lock was taken in the old tree.
            descriptor = real(path, **kwargs)
            os.rename(self.account.root, moved)
            return descriptor
        with mock.patch.object(account_registry, 'acquire_owner_lock', side_effect=move_after):
            with self.assertRaises(connect_cli.AccountPathsError):
                account_registry.login_lock(self.account)
        self.assertFalse(self.account.root.exists())
        os.close(real(moved / 'run' / 'connect.lock'))  # Released: free again.

    def test_cancel_never_creates_the_root(self):
        jobs = []

        def busy(argv, **kwargs):  # A busy connect job of the alias, booted out by cancel.
            jobs.append(argv[1])
            loaded = argv[1] == 'print' and 'bootout' not in jobs
            return subprocess.CompletedProcess(argv, 0 if loaded or argv[1] == 'bootout' else 113,
                                               '\tstate = running\n' if loaded else '', '')
        value, code = connect_cli.cancel(self.paths, runner=busy, uid=os.getuid(), sleeper=lambda _: None,
                                         restore_broker=self.never)
        self.assertEqual((value, code), ({'status': 'cancelled'}, 0))
        self.assertFalse(self.account.root.exists())


if __name__ == '__main__':
    unittest.main()
