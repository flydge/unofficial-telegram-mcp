"""0.55 phase C review fix: `setup` and `resume` apply the connect helper's login pin (spec 3C).

`run_setup` (`setup --account` and `resume --account`) asks the authorized session's getMe after Ready and applies
the broker's pin before the teardown and handoff, with the connect codes and next actions: `account_duplicate`,
`account_mismatch` (review_enrollment_binding_without_resetting) and `alias_unavailable` (stop_and_report).
Without a registry the default asks no getMe (0.54). The user id never reaches a result. Synthetic providers only.
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

from test_onboarding import AuthRaw, MemoryStore, Terminal

from telegram_search_mcp import account_registry, bundle_install, connect_cli
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
        """The alias's SetupPaths; a named alias's root as account-add leaves it (review fix 055d3: owner-only, empty
        but for its run directory), since a login never creates it."""
        account = AccountPaths.for_alias(alias, self.home)
        if alias != 'default':
            for path in (account.root, account.run):
                path.mkdir(mode=0o700, exist_ok=True)
        self.paths = SetupPaths.from_account(account)

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
        # Removed while the run holds its connect.lock (here, as the provider is built): the pin refuses it.
        create = self.factory(USER_B, ['authorizationStateReady'])

        def removed_meanwhile():
            account_registry.remove_alias('work', self.home)
            return create()
        result = run_setup(self.paths, self.terminal, self.store, removed_meanwhile, handoff=self.handoff, resume=True,
                           bundled_loader=lambda: None)
        self.assert_refused(result, 'alias_unavailable', 'stop_and_report')
        self.assertEqual(self.pins(), ({}, None))
        # Removed before: the lock is refused with it (review fix 055d3), before any provider.
        raws = len(self.raws)
        result = self.resume(USER_B)
        self.assertEqual((result.status, result.code, result.to_dict()['next_action'], len(self.raws)),
                         ('pending', 'alias_unavailable', 'stop_and_report', raws))

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


def tree(path):
    out = {}
    for parent, dirs, files in os.walk(path):
        for name in dirs:
            out[str((Path(parent) / name).relative_to(path))] = 'dir'
        for name in files:
            child = Path(parent) / name
            out[str(child.relative_to(path))] = hashlib.sha256(child.read_bytes()).hexdigest()
    return out


class ConnectLockTests(SetupHarness):
    """Review fix (055d2, lead decision A): `setup --account` and `resume --account` hold the alias's connect.lock for
    their whole run, handoff included, as the connect helper does, so an installer transaction over the aliases (which
    holds every alias's connect.lock, `bundle_install._LoginLocks`) and a login never run at once. Held, they refuse
    with their existing code, `owner_busy`, before anything else. The default's setup keeps 0.54's behaviour."""

    def installer_holds_the_locks(self):
        def launchctl(argv, **kwargs):  # No connect job loaded.
            return subprocess.CompletedProcess(argv, 113, '', '')
        locks = bundle_install._LoginLocks(launchctl, self.home, ['work']).__enter__()
        self.addCleanup(locks.__exit__)
        locks.complete()
        self.assertEqual(locks.held, {'work'})
        self.assertTrue(self.paths.account.connect_lock.exists())  # The very lock setup takes.

    def test_setup_and_resume_refuse_owner_busy_while_an_installer_transaction_runs_and_touch_nothing(self):
        self.home_with('work')
        self.use('work')
        self.installer_holds_the_locks()
        before = tree(self.paths.account.root)
        for resume in (False, True):
            with self.subTest(resume=resume):
                result = self.setup(USER_B, resume=resume)
                self.assertEqual((result.status, result.code), ('pending', 'owner_busy'))
                self.assertEqual(result.to_dict()['next_action'], 'check_existing_broker_without_stopping_it')
                # No provider, no secret prompt, no enrollment, no handoff: nothing at all.
                self.assertEqual((self.events, self.raws, self.terminal.prompts, self.handoffs), ([], [], [], 0))
                self.assertIsNone(EnrollmentStore(self.paths).read())
                self.assertEqual(tree(self.paths.account.root), before)
                self.assertEqual(self.pins(), ({'work': None}, None))

    def test_a_named_aliases_setup_holds_its_connect_lock_through_the_handoff(self):
        self.home_with('work')
        self.use('work')
        seen = []
        handoff = self.handoff

        def probing():
            seen.append(connect_cli._connect_lock_held(self.paths))
            return handoff()
        self.handoff = probing
        result = self.setup(USER_B)
        self.assertEqual((result.status, result.code, seen), ('ready', None, [True]))
        self.assertFalse(connect_cli._connect_lock_held(self.paths))  # Released when the run ends.

    def test_the_default_setup_takes_no_connect_lock(self):
        self.home_with()
        self.use('default')
        seen = []
        handoff = self.handoff

        def probing():
            seen.append(connect_cli._connect_lock_held(self.paths))
            return handoff()
        self.handoff = probing
        result = self.setup(USER_A)
        self.assertEqual((result.status, result.code, seen), ('ready', None, [False]))
        self.assertFalse(self.paths.account.connect_lock.exists())


class RootlessSetupTests(SetupHarness):
    """Review fix (055d3, medium, R1): `setup --account` and `resume --account` take the alias's connect.lock only inside
    its existing root, which account-add creates and a login never does: a missing root, or an alias the registry no
    longer lists, refuses `alias_unavailable` before any provider, prompt or enrollment, with nothing created."""

    def test_a_missing_root_or_an_alias_no_longer_listed_refuses_and_creates_nothing(self):
        self.home_with('work')
        self.paths = SetupPaths.from_account(AccountPaths.for_alias('work', self.home))  # Admitted; no root.
        for case in ('missing_root', 'no_longer_listed'):
            if case == 'no_longer_listed':
                for path in (self.paths.account.root, self.paths.account.run):
                    path.mkdir(mode=0o700)
                account_registry.remove_alias('work', self.home)
            before = tree(self.accounts)
            for resume in (False, True):
                with self.subTest(case=case, resume=resume):
                    result = self.setup(USER_B, resume=resume)
                    self.assertEqual((result.status, result.code, result.to_dict()['next_action']),
                                     ('pending', 'alias_unavailable', 'stop_and_report'))
                    self.assertEqual((self.events, self.raws, self.terminal.prompts, self.handoffs), ([], [], [], 0))
                    self.assertEqual(tree(self.accounts), before)


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
