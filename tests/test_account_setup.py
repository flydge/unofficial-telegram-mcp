"""0.55 phase C review fix: `setup` and `resume` apply the connect helper's login pin (spec 3C).

`run_setup` (`setup --account` and `resume --account`) asks the authorized session's getMe after Ready and applies
the broker's pin before the teardown and handoff, with the connect codes and next actions: `account_duplicate`,
`account_mismatch` (review_enrollment_binding_without_resetting) and `alias_unavailable` (stop_and_report).
Without a registry the default asks no getMe (0.54). The user id never reaches a result. Synthetic providers only.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_onboarding import AuthRaw, MemoryStore, Terminal

from telegram_search_mcp import account_registry
from telegram_search_mcp.config import AccountPaths
from telegram_search_mcp.enrollment import EnrollmentStore, SetupPaths
from telegram_search_mcp.session_owner import SessionOwnerLease
from telegram_search_mcp.setup_wizard import run_setup

USER_A, USER_B, USER_C = 7401234561, 7401234562, 7401234563
REVIEW = 'review_enrollment_binding_without_resetting'


class AccountAuthRaw(AuthRaw):
    """AuthRaw that answers getMe with one user, like an authorized TDLib session."""

    def __init__(self, states=None, *, user_id, **kwargs):
        super().__init__(states, **kwargs)
        self.user_id = user_id

    def send(self, payload):
        if payload['@type'] == 'getMe':
            self.sent.append(dict(payload))
            user = ({'@type': 'user', 'id': self.user_id} if type(self.user_id) is int else
                    {'@type': 'error', 'code': 400, 'message': 'USER PRIVATE'})
            self.queue.append({**user, '@extra': payload['@extra']})
            return
        super().send(payload)


class SetupHarness(unittest.TestCase):
    def home_with(self, *aliases):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='s')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        os.chmod(self.home, 0o700)
        for alias in aliases:
            account_registry.add_alias(alias, self.home, longest_tool='t')
        self.accounts = account_registry.accounts_root(self.home)
        self.store, self.terminal, self.events, self.raws, self.handoffs = MemoryStore(), Terminal(), [], [], 0

    def use(self, alias):
        self.paths = SetupPaths.from_account(AccountPaths.for_alias(alias, self.home))

    def factory(self, user_id, states=None):
        def create():
            raw = AccountAuthRaw(states, user_id=user_id, events=self.events, paths=self.paths)
            self.raws.append(raw)
            self.events.append('create')
            return raw
        return create

    def handoff(self):
        self.assertEqual(self.events[-1], 'destroy')
        with SessionOwnerLease(self.paths.lock):
            self.events.append('handoff')
        self.handoffs += 1
        return 'ready'

    def setup(self, user_id, *, resume=False, states=None):
        return run_setup(self.paths, self.terminal, self.store, self.factory(user_id, states), handoff=self.handoff,
                         resume=resume, bundled_loader=lambda: None)

    def resume(self, user_id):
        return self.setup(user_id, resume=True, states=['authorizationStateReady'])

    def sent(self, kind):
        return sum(row['@type'] == kind for row in self.raws[-1].sent)

    def pins(self):
        registry = account_registry.load(self.accounts)
        return dict(registry.entries), registry.default_user_id

    def assert_refused(self, result, code, action=REVIEW):
        self.assertEqual((result.status, result.code), ('pending', code))
        self.assertEqual(result.to_dict()['next_action'], action)
        self.assertTrue(result.auth_client_closed and result.auth_client_destroyed)
        self.assertTrue(self.raws[-1].closed)
        self.assertNotIn('handoff', self.events)
        self.assertEqual(self.handoffs, 0)
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')
        self.assertTrue(self.paths.session.is_dir())  # The authorized session stays; nothing is logged out.
        reported = json.dumps(result.to_dict()) + repr(result)
        for user in (USER_A, USER_B, USER_C):
            self.assertNotIn(str(user), reported)


class SetupPinTests(SetupHarness):
    def setUp(self):
        self.home_with('work')
        self.use('work')

    def test_setup_refuses_an_account_pinned_under_the_default_before_the_handoff(self):
        account_registry.pin_account(self.accounts, 'default', USER_A)
        result = self.setup(USER_A)
        self.assert_refused(result, 'account_duplicate')
        self.assertEqual(self.sent('getMe'), 1)
        # getMe is asked of the authorized session, before its teardown.
        self.assertEqual([row['@type'] for row in self.raws[-1].sent][-2:], ['getMe', 'close'])
        self.assertEqual(self.pins(), ({'work': None}, USER_A))

    def test_setup_refuses_an_account_pinned_under_another_alias(self):
        account_registry.add_alias('play', self.home, longest_tool='t')
        account_registry.pin_account(self.accounts, 'play', USER_B)
        self.assert_refused(self.setup(USER_B), 'account_duplicate')
        self.assertEqual(self.pins(), ({'play': USER_B, 'work': None}, None))

    def test_resume_refuses_another_user_than_the_aliases_own_and_admits_its_own(self):
        account_registry.pin_account(self.accounts, 'work', USER_B)
        self.assert_refused(self.setup(USER_C), 'account_mismatch')
        self.assert_refused(self.resume(USER_C), 'account_mismatch')
        self.assertEqual(self.sent('getMe'), 1)
        self.assertEqual(self.pins(), ({'work': USER_B}, None))
        result = self.resume(USER_B)
        self.assertEqual((result.status, result.code), ('ready', None))
        self.assertEqual(self.handoffs, 1)
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'ready')

    def test_a_new_account_is_pinned_at_setup_and_handed_off(self):
        account_registry.pin_account(self.accounts, 'default', USER_A)
        result = self.setup(USER_B)
        self.assertEqual((result.status, result.code), ('ready', None))
        self.assertEqual(self.events, ['create', 'close', 'destroy', 'handoff'])
        self.assertEqual(self.pins(), ({'work': USER_B}, USER_A))
        self.assertNotIn(str(USER_B), json.dumps(result.to_dict()) + repr(result))

    def test_an_alias_removed_meanwhile_stops_and_an_unreadable_account_never_hands_off(self):
        self.assert_refused(self.setup(None), 'auth_request_failed', 'resume_in_foreground_native_terminal')
        account_registry.remove_alias('work', self.home)
        self.assert_refused(self.resume(USER_B), 'alias_unavailable', 'stop_and_report')
        self.assertEqual(self.pins(), ({}, None))

    def test_a_busy_registry_is_a_retry_and_an_unsafe_one_stops(self):
        with mock.patch.object(account_registry, 'pin_account',
                               side_effect=account_registry.RegistryError('registry_busy')):
            self.assert_refused(self.setup(USER_B), 'dependency_unavailable', 'resume_in_foreground_native_terminal')
        with mock.patch.object(account_registry, 'pin_account',
                               side_effect=account_registry.RegistryError('registry_unsafe')):
            self.assert_refused(self.resume(USER_B), 'alias_unavailable', 'stop_and_report')


class DefaultSetupPinTests(SetupHarness):
    def test_without_a_registry_the_default_asks_no_account(self):
        self.home_with()
        self.use('default')
        self.assertFalse(account_registry.present(self.accounts))
        result = self.setup(USER_A)
        self.assertEqual((result.status, result.code), ('ready', None))
        self.assertEqual(self.sent('getMe'), 0)
        self.assertFalse(account_registry.present(self.accounts))

    def test_with_a_registry_the_default_is_pinned_at_setup_and_refused_as_a_duplicate(self):
        self.home_with('work')
        self.use('default')
        account_registry.pin_account(self.accounts, 'work', USER_B)
        self.assert_refused(self.setup(USER_B), 'account_duplicate')
        result = self.resume(USER_A)
        self.assertEqual((result.status, result.code), ('ready', None))
        self.assertEqual(self.pins(), ({'work': USER_B}, USER_A))


class CodeTableTests(unittest.TestCase):
    def test_setup_reports_the_connect_pin_codes_and_next_actions(self):
        from telegram_search_mcp import connect_flow
        from telegram_search_mcp.setup_wizard import SetupResult, _result
        for code in ('account_mismatch', 'account_duplicate', 'alias_unavailable'):
            with self.subTest(code=code):
                result = _result(code, closed=True)
                self.assertIsInstance(result, SetupResult)
                self.assertEqual(result.code, code)
                self.assertEqual(result.to_dict()['next_action'], connect_flow.next_action('failed', code))


if __name__ == '__main__':
    unittest.main()
