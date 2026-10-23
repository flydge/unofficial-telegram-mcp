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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_connect_flow import PARAMETERS, PHONE, READY, BUNDLED, ConnectHarness, FakeUI, QrRaw, other_device
from test_onboarding import MemoryStore

from telegram_search_mcp import account_registry, connect_cli, connect_flow, connect_page, connect_server
from telegram_search_mcp.config import AccountPaths
from telegram_search_mcp.connect_flow import ConnectResult, connect_status_path, run_connect
from telegram_search_mcp.enrollment import EnrollmentStore, SetupPaths
from telegram_search_mcp.session_owner import SessionOwnerLease

USER_A, USER_B, USER_C = 7401234561, 7401234562, 7401234563
# The default page's assets as 0.55 phase B (3d251c9) serves them: a named alias's page never changes them.
DEFAULT_ASSETS = {'/': '2eb5b38ce52639b2608ad1cc161f6164b10861abe4aec0ef14bdad657273d8ef',
                  '/app.js': '86e76e90ac567456f09f54423ab00c412d6484b19f60963ca077c45a2a0b0b72',
                  '/app.css': 'a818f012bee2f1c121b358e7082ec2233b8a53b159247561e57534c41966c5ea'}


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
                             ('alias_unavailable', 'stop_and_report')):
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


if __name__ == '__main__':
    unittest.main()
