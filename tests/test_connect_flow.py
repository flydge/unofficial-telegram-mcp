"""QR-first connect orchestration; every provider, link and password is synthetic."""
from contextlib import contextmanager
import io
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_onboarding import MemoryStore

from telegram_search_mcp import connect_flow, session_owner
from telegram_search_mcp.connect_flow import (PASSWORD_ATTEMPTS, ConnectUI, connect_status_path, run_connect,
                                              write_connect_status)
from telegram_search_mcp.credential_identity import identity_fingerprint
from telegram_search_mcp.credentials import SecretValue
from telegram_search_mcp.enrollment import EnrollmentStore, SetupPaths
from telegram_search_mcp.keychain import ApiCredentials
from telegram_search_mcp.session_owner import OwnerLeaseError, SessionOwnerLease

LINK = 'tg://login?token=' + 'Q' * 40 + 'LEAKCANARYLINK'
NEXT_LINK = 'tg://login?token=' + 'R' * 43
PASSWORD = 'LEAKCANARYPASSWORD-7f3'
BUNDLED = ApiCredentials(424242, 'b' * 32)

PARAMETERS = {'@type': 'authorizationStateWaitTdlibParameters'}
PHONE = {'@type': 'authorizationStateWaitPhoneNumber'}
READY = {'@type': 'authorizationStateReady'}
CODE = {'@type': 'authorizationStateWaitCode', 'code_info': {}}


def other_device(link=LINK):
    return {'@type': 'authorizationStateWaitOtherDeviceConfirmation', 'link': link}


def password_state(hint='cat'):
    return {'@type': 'authorizationStateWaitPassword', 'password_hint': hint,
            'has_recovery_email_address': False, 'has_passport_data': False,
            'recovery_email_address_pattern': ''}


class QrRaw:
    """Advances along `flow` on accepted requests; a scan advances after quiet receives."""
    def __init__(self, flow, *, outcomes=(), scan_after=2, events=None, paths=None):
        self.flow, self.index = list(flow), 0
        self.outcomes = list(outcomes)
        self.scan_after, self.quiet = scan_after, 0
        self.events, self.paths = events if events is not None else [], paths
        self.queue, self.sent = [], []
        self.close_error = self.destroy_error = self.closed = False

    @property
    def state(self):
        return self.flow[self.index]

    def advance(self):
        self.index = min(len(self.flow) - 1, self.index + 1)
        self.queue.append({'@type': 'updateAuthorizationState', 'authorization_state': self.state})

    def send(self, payload):
        self.sent.append(dict(payload))
        kind, extra = payload['@type'], payload['@extra']
        if kind == 'getAuthorizationState':
            self.queue.append({**self.state, '@extra': extra})
        elif kind == 'close':
            self.events.append('close')
            if self.close_error:
                self.queue.append({'@type': 'error', '@extra': extra, 'code': 400})
            else:
                self.queue.extend([{'@type': 'ok', '@extra': extra}, {'@type': 'updateAuthorizationState',
                    'authorization_state': {'@type': 'authorizationStateClosed'}}])
        elif kind == 'checkAuthenticationPassword':
            outcome = self.outcomes.pop(0)
            if outcome == 'raise':
                raise RuntimeError('provider failed for ' + payload['password'])
            if outcome == 'silent':
                return
            if outcome == 'malformed':
                self.queue.append({'@type': 'session', '@extra': extra})
                return
            if outcome == 'invalid':
                self.queue.append({'@type': 'error', '@extra': extra, 'code': 400,
                                   'message': 'PASSWORD_HASH_INVALID'})
            elif outcome == 'flood':
                self.queue.append({'@type': 'error', '@extra': extra, 'code': 420, 'message': 'FLOOD_WAIT_30'})
            else:
                self.queue.append({'@type': 'ok', '@extra': extra})
                self.advance()
        else:
            self.queue.append({'@type': 'ok', '@extra': extra})
            self.advance()

    def receive(self, timeout):
        if self.queue:
            return self.queue.pop(0)
        if self.state['@type'] == 'authorizationStateWaitOtherDeviceConfirmation' and self.scan_after is not None:
            self.quiet += 1
            if self.quiet >= self.scan_after:
                self.quiet = 0
                self.advance()
                return self.queue.pop(0)
        time.sleep(min(timeout, .005))
        return None

    def close(self):
        if self.paths:
            try:
                with SessionOwnerLease(self.paths.lock):
                    raise AssertionError('destroy occurred without the owner lease')
            except OwnerLeaseError as error:
                assert error.code == 'owner_busy'
        self.events.append('destroy')
        if self.destroy_error:
            raise RuntimeError('native destroy did not return')
        self.closed = True


class FakeUI(ConnectUI):
    def __init__(self, paths, passwords=(), *, cancel_on=None):
        self.paths, self.passwords, self.cancel_on = paths, list(passwords), cancel_on
        self.published, self.hints, self.notices, self.statuses = [], [], [], []
        self.is_cancelled = False

    def publish_qr(self, link):
        assert type(link) is SecretValue
        self.published.append(link.reveal().decode('ascii'))
        if self.cancel_on == 'publish':
            self.is_cancelled = True

    def request_password(self, hint, timeout):
        assert 0 < timeout <= 1
        self.hints.append(hint)
        if self.cancel_on == 'password':
            self.is_cancelled = True
            return None
        if not self.passwords:
            time.sleep(min(timeout, .005))
            return None
        value = self.passwords.pop(0)
        return None if value is None else SecretValue(value.encode('utf-8'))

    def cancelled(self):
        return self.is_cancelled

    def notify(self, phase, code):
        self.notices.append((phase, code))
        self.statuses.append(connect_status_path(self.paths).read_bytes())


class ConnectHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'app'
        self.paths = SetupPaths(self.root)
        self.store, self.events, self.raws, self.handoffs = MemoryStore(), [], [], 0
        self.bundled = BUNDLED

    def tearDown(self):
        self.temp.cleanup()

    def raw(self, flow, **kwargs):
        def factory():
            raw = QrRaw(flow, events=self.events, paths=self.paths, **kwargs)
            self.raws.append(raw)
            self.events.append('create')
            return raw
        return factory

    def handoff(self):
        self.assertEqual(self.events[-1], 'destroy')
        with SessionOwnerLease(self.paths.lock):
            self.events.append('handoff')
        self.handoffs += 1
        return 'ready'

    def connect(self, flow, ui=None, *, raw=None, **kwargs):
        ui = FakeUI(self.paths) if ui is None else ui
        result = run_connect(self.paths, self.store, raw or self.raw(flow, **kwargs.pop('raw_options', {})), ui,
                             handoff=kwargs.pop('handoff', self.handoff),
                             bundled_loader=kwargs.pop('bundled_loader', lambda: self.bundled), **kwargs)
        return result, ui

    def status(self):
        path = connect_status_path(self.paths)
        metadata = path.lstat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        record = json.loads(path.read_bytes())
        self.assertEqual(set(record), {'phase', 'code', 'updated_at', 'expires_at'})
        return record

    def sent(self, kind, raw=None):
        return sum(row['@type'] == kind for row in (raw or self.raws[-1]).sent)

    def assert_torn_down_pending(self, result):
        self.assertTrue(result.auth_client_closed and result.auth_client_destroyed)
        self.assertTrue(self.raws[-1].closed)
        self.assertEqual(self.handoffs, 0)
        self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')
        self.assertEqual(self.status()['phase'], result.phase)
        self.assertEqual(self.status()['code'], result.code)


class ConnectFlowTests(ConnectHarness):
    # Happy paths -------------------------------------------------------------------------

    def test_qr_scan_reaches_ready_with_bundled_identity_then_hands_off(self):
        result, ui = self.connect([PARAMETERS, PHONE, other_device(), READY])
        self.assertEqual((result.phase, result.code, result.exit_code), ('ready', None, 0))
        self.assertEqual(result.to_dict()['next_action'], 'none')
        self.assertEqual(self.events, ['create', 'close', 'destroy', 'handoff'])
        self.assertEqual(self.sent('requestQrCodeAuthentication'), 1)
        parameters = next(row for row in self.raws[-1].sent if row['@type'] == 'setTdlibParameters')
        self.assertEqual((parameters['api_id'], parameters['api_hash']), (BUNDLED.api_id, BUNDLED.api_hash))
        record = EnrollmentStore(self.paths).read()
        self.assertEqual(record['state'], 'ready')
        self.assertEqual(record['credential_source'], 'bundled')
        self.assertEqual(record['identity_fingerprint'], identity_fingerprint(BUNDLED))
        self.assertEqual(ui.published, [LINK])
        self.assertEqual(ui.notices, [('starting', None), ('waiting_for_scan', None), ('handing_off', None),
                                      ('ready', None)])
        record = self.status()
        self.assertEqual((record['phase'], record['code']), ('ready', None))
        self.assertRegex(record['expires_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_complete_keychain_pair_overrides_bundled_identity(self):
        self.store.values.update(api_id=SecretValue(b'777'), api_hash=SecretValue(b'c' * 32))
        result, _ = self.connect([PARAMETERS, PHONE, other_device(), READY])
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(EnrollmentStore(self.paths).read()['credential_source'], 'keychain')
        parameters = next(row for row in self.raws[-1].sent if row['@type'] == 'setTdlibParameters')
        self.assertEqual(parameters['api_id'], 777)

    def test_replaced_link_is_republished_without_a_second_qr_request(self):
        result, ui = self.connect([PHONE, other_device(), other_device(NEXT_LINK), READY])
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(ui.published, [LINK, NEXT_LINK])
        self.assertEqual(self.sent('requestQrCodeAuthentication'), 1)

    def test_password_after_scan_reaches_ready_with_sanitized_hint(self):
        ui = FakeUI(self.paths, [None, PASSWORD])
        result, _ = self.connect([PHONE, other_device(), password_state('my\ncat'), READY], ui,
                                 raw_options={'outcomes': ['ok']})
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(ui.hints, ['my cat', 'my cat'])
        self.assertEqual(self.sent('checkAuthenticationPassword'), 1)
        self.assertEqual(ui.notices[:5], [('starting', None), ('waiting_for_scan', None),
            ('waiting_for_password', None), ('authorizing', None), ('handing_off', None)])

    def test_existing_enrollment_is_reused_without_new_key_or_identity(self):
        self.assertEqual(self.connect([PHONE, other_device(), READY])[0].phase, 'ready')
        first, puts = EnrollmentStore(self.paths).read(), list(self.store.puts)
        result, _ = self.connect([READY])
        self.assertEqual(result.phase, 'ready')
        second = EnrollmentStore(self.paths).read()
        self.assertEqual(second['enrollment_id'], first['enrollment_id'])
        self.assertEqual(self.store.puts, puts)

    # Password bounds ---------------------------------------------------------------------

    def test_definitive_invalid_password_allows_re_entry(self):
        ui = FakeUI(self.paths, ['one', 'two', PASSWORD])
        result, _ = self.connect([password_state(), READY], ui, raw_options={'outcomes': ['invalid', 'invalid', 'ok']})
        self.assertEqual(result.phase, 'ready')
        self.assertEqual(self.sent('checkAuthenticationPassword'), 3)
        self.assertIn(('waiting_for_password', 'invalid_password'), ui.notices)

    def test_password_attempts_are_bounded_per_run(self):
        ui = FakeUI(self.paths, ['wrong'] * (PASSWORD_ATTEMPTS + 3))
        result, _ = self.connect([password_state(), READY], ui,
                                 raw_options={'outcomes': ['invalid'] * (PASSWORD_ATTEMPTS + 3)})
        self.assertEqual((result.phase, result.code), ('failed', 'invalid_password'))
        self.assertEqual(self.sent('checkAuthenticationPassword'), PASSWORD_ATTEMPTS)
        self.assertEqual(len(ui.passwords), 3)
        self.assert_torn_down_pending(result)

    def test_uncertain_password_submission_is_never_resubmitted(self):
        for outcome, lifetime in [('raise', 30), ('silent', .4), ('malformed', 30)]:
            with self.subTest(outcome=outcome), mock.patch.object(connect_flow, '_MIN_SUBMIT', .1):
                ui = FakeUI(self.paths, [PASSWORD, PASSWORD])
                result, _ = self.connect([password_state(), READY], ui, lifetime=lifetime,
                                         raw_options={'outcomes': [outcome, 'ok']})
                self.assertEqual((result.phase, result.code), ('failed', 'password_submission_uncertain'))
                self.assertEqual(result.to_dict()['next_action'], 'run_connect_again')
                self.assertEqual(self.sent('checkAuthenticationPassword'), 1)
                self.assertEqual(len(ui.passwords), 1)
                self.assert_torn_down_pending(result)

    def test_flood_wait_on_password_stops_with_retry_after(self):
        ui = FakeUI(self.paths, [PASSWORD, PASSWORD])
        result, _ = self.connect([password_state(), READY], ui, raw_options={'outcomes': ['flood', 'ok']})
        self.assertEqual((result.phase, result.code, result.retry_after), ('failed', 'flood_wait', 30))
        self.assertEqual(self.sent('checkAuthenticationPassword'), 1)
        self.assert_torn_down_pending(result)

    # Unsupported, expiry, cancel ---------------------------------------------------------

    def test_code_and_other_states_point_to_terminal_fallback(self):
        flows = [[CODE], [PHONE, CODE], [{'@type': 'authorizationStateWaitRegistration'}],
                 [{'@type': 'authorizationStateWaitEmailAddress'}], [other_device('http://evil')],
                 [PHONE, other_device(), PHONE]]
        for flow in flows:
            with self.subTest(flow=flow[-1]['@type']):
                result, ui = self.connect(flow)
                self.assertEqual((result.phase, result.code), ('failed', 'unsupported_auth_state'))
                self.assertEqual(result.to_dict()['next_action'], 'use_terminal_setup_fallback')
                self.assertLessEqual(self.sent('requestQrCodeAuthentication'), 1)
                self.assertEqual(ui.published, [LINK] if len(flow) == 3 else [])
                self.assert_torn_down_pending(result)

    def test_lifetime_expiry_tears_down_without_handoff(self):
        started = time.monotonic()
        result, ui = self.connect([PHONE, other_device(), READY], lifetime=.4, raw_options={'scan_after': None})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.phase, result.code), ('expired', 'expired'))
        self.assertEqual(ui.published, [LINK])
        self.assertEqual(ui.notices[-1], ('expired', 'expired'))
        self.assert_torn_down_pending(result)

    def test_expiry_while_waiting_for_password(self):
        result, _ = self.connect([password_state(), READY], lifetime=.4)
        self.assertEqual(result.phase, 'expired')
        self.assertEqual(self.sent('checkAuthenticationPassword'), 0)
        self.assert_torn_down_pending(result)

    def test_cancel_from_ui_tears_down_during_scan_and_password(self):
        for cancel_on, flow in [('publish', [other_device(), READY]), ('password', [password_state(), READY])]:
            with self.subTest(cancel_on=cancel_on):
                ui = FakeUI(self.paths, [PASSWORD], cancel_on=cancel_on)
                result, _ = self.connect(flow, ui, raw_options={'scan_after': None})
                self.assertEqual((result.phase, result.code), ('cancelled', 'cancelled'))
                self.assertEqual(self.sent('checkAuthenticationPassword'), 0)
                self.assert_torn_down_pending(result)

    def test_observed_ready_wins_over_a_cancel_in_the_same_slice(self):
        harness = self
        class CancelAtReady(FakeUI):
            def cancelled(self):  # Pressed during the slice in which the provider reached Ready.
                return harness.raws[-1].state == READY
        ui = CancelAtReady(self.paths)
        result, _ = self.connect([other_device(), READY], ui, raw_options={'scan_after': 1})
        self.assertEqual((result.phase, result.code), ('ready', None))
        self.assertEqual(self.handoffs, 1)

    def test_password_is_not_sent_without_a_round_trip_budget(self):
        ui = FakeUI(self.paths, [PASSWORD])
        result, _ = self.connect([password_state(), READY], ui, lifetime=2, raw_options={'outcomes': ['ok']})
        self.assertEqual((result.phase, result.code), ('expired', 'expired'))
        self.assertEqual(self.sent('checkAuthenticationPassword'), 0)
        self.assert_torn_down_pending(result)

    def test_status_is_finalized_when_the_lease_check_fails_after_teardown(self):
        checks = []
        original = SessionOwnerLease.check
        def failing(lease):
            checks.append(1)
            if len(checks) >= 3:
                raise OwnerLeaseError('owner_unsafe')
            return original(lease)
        with mock.patch.object(SessionOwnerLease, 'check', failing):
            result, _ = self.connect([other_device(), READY])
        self.assertEqual((result.phase, result.code), ('failed', 'owner_unsafe'))
        self.assertEqual(self.status()['code'], 'owner_unsafe')
        self.assertEqual(self.handoffs, 0)

    # Teardown and handoff ----------------------------------------------------------------

    def test_failed_close_destroys_under_lease_and_never_hands_off(self):
        def factory():
            raw = self.raw([READY])()
            raw.close_error = True
            return raw
        result, _ = self.connect(None, raw=factory)
        self.assertEqual((result.phase, result.code), ('failed', 'auth_close_failed'))
        self.assertEqual(self.events, ['create', 'close', 'destroy'])
        self.assertEqual(self.status()['code'], 'auth_close_failed')

    def test_uncertain_destroy_retains_the_lease_and_never_hands_off(self):
        def factory():
            raw = self.raw([READY])()
            raw.destroy_error = True
            return raw
        retained = dict(session_owner._PROCESS_LIFETIME_OWNERS)
        try:
            result, _ = self.connect(None, raw=factory)
            self.assertEqual((result.phase, result.code), ('failed', 'auth_destroy_failed'))
            self.assertFalse(result.auth_client_destroyed)
            self.assertEqual(result.to_dict()['next_action'], 'exit_connect_process_then_run_connect_again')
            self.assertEqual(self.handoffs, 0)
            with self.assertRaises(OwnerLeaseError) as busy:
                with SessionOwnerLease(self.paths.lock):
                    pass
            self.assertEqual(busy.exception.code, 'owner_busy')
            self.assertEqual(self.status()['code'], 'auth_destroy_failed')
        finally:
            for descriptor in set(session_owner._PROCESS_LIFETIME_OWNERS) - set(retained):
                session_owner._PROCESS_LIFETIME_OWNERS.pop(descriptor)
                os.close(descriptor)

    def test_handoff_failures_never_claim_ready(self):
        for boundary in ['broker_start_failed', 'broker_incompatible', 'broker_readiness_timeout', 'owner_busy']:
            with self.subTest(boundary=boundary):
                result, _ = self.connect([READY], handoff=lambda: boundary)
                self.assertEqual((result.phase, result.code), ('failed', boundary))
                self.assertEqual(EnrollmentStore(self.paths).read()['state'], 'pending')
                self.assertEqual(self.status()['phase'], 'failed')

    # Admission ---------------------------------------------------------------------------

    def test_contended_owner_writes_no_status_and_constructs_nothing(self):
        with SessionOwnerLease(self.paths.lock):
            result, _ = self.connect([READY])
        self.assertEqual((result.phase, result.code), ('failed', 'owner_busy'))
        self.assertEqual(self.raws, [])
        self.assertFalse(connect_status_path(self.paths).exists())

    def test_missing_identity_refuses_before_enrollment_or_native(self):
        result, _ = self.connect([READY], bundled_loader=lambda: None)
        self.assertEqual((result.phase, result.code), ('failed', 'app_identity_missing'))
        self.assertEqual(result.to_dict()['next_action'], 'use_terminal_setup_fallback')
        self.assertEqual(self.raws, [])
        self.assertIsNone(EnrollmentStore(self.paths).read())
        self.assertEqual(self.store.puts, [])
        self.assertEqual(self.status()['code'], 'app_identity_missing')

    def test_partial_keychain_pair_is_a_conflict(self):
        self.store.values['api_id'] = SecretValue(b'777')
        result, _ = self.connect([READY])
        self.assertEqual(result.code, 'credential_conflict')
        self.assertEqual(self.raws, [])

    def test_changed_bundled_identity_refuses_existing_enrollment(self):
        self.assertEqual(self.connect([READY])[0].phase, 'ready')
        self.bundled = ApiCredentials(424243, 'b' * 32)
        result, _ = self.connect([READY])
        self.assertEqual(result.code, 'identity_changed')
        self.assertEqual(len(self.raws), 1)

    def test_legacy_session_is_preserved(self):
        os.makedirs(self.paths.session, mode=0o700)
        os.chmod(self.paths.root, 0o700)
        (self.paths.session / 'td.binlog').write_bytes(b'legacy')
        result, _ = self.connect([READY])
        self.assertEqual(result.code, 'legacy_unmanaged')
        self.assertEqual((self.paths.session / 'td.binlog').read_bytes(), b'legacy')
        self.assertEqual(self.raws, [])

    def test_invalid_arguments_refuse_without_effects(self):
        class NotUI:
            pass
        for kwargs in [dict(ui=NotUI()), dict(lifetime=0), dict(lifetime=float('nan')), dict(lifetime=3601),
                       dict(close_timeout=0)]:
            with self.subTest(kwargs=list(kwargs)):
                ui = kwargs.pop('ui', FakeUI(self.paths))
                result = run_connect(self.paths, self.store, self.raw([READY]), ui, handoff=self.handoff,
                                     bundled_loader=lambda: BUNDLED, **kwargs)
                self.assertEqual(result.code, 'invalid_input')
        self.assertFalse(self.root.exists())

    # Status file -------------------------------------------------------------------------

    def test_status_write_is_closed_atomic_and_replaces_a_planted_link(self):
        run = self.paths.lock.parent
        os.makedirs(run, mode=0o700)
        os.chmod(self.root, 0o700)
        outside = Path(self.temp.name) / 'outside'
        outside.write_bytes(b'untouched')
        connect_status_path(self.paths).symlink_to(outside)
        write_connect_status(self.paths, 'waiting_for_scan', None, updated_at=0, expires_at=600)
        self.assertEqual(outside.read_bytes(), b'untouched')
        self.assertEqual(self.status(), {'phase': 'waiting_for_scan', 'code': None,
            'updated_at': '1970-01-01T00:00:00Z', 'expires_at': '1970-01-01T00:10:00Z'})
        for phase, code in [('scanning', None), ('failed', 'PASSWORD_HASH_INVALID'), ('failed', LINK)]:
            with self.assertRaises(ValueError):
                write_connect_status(self.paths, phase, code, updated_at=0, expires_at=0)
        self.assertEqual(sorted(os.listdir(run)), ['connect-status.json'])


@contextmanager
def captured_process_output():
    """File-descriptor level, so writes that bypass sys.stdout are caught too."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    sinks = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    try:
        os.dup2(sinks[0].fileno(), 1)
        os.dup2(sinks[1].fileno(), 2)
        sys.stdout, sys.stderr = out, err
        captured = []
        yield captured
    finally:
        sys.stdout, sys.stderr = old
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for descriptor in saved:
            os.close(descriptor)
        for sink in sinks:
            sink.seek(0)
            captured.append(sink.read())
            sink.close()
        captured.extend([out.getvalue().encode(), err.getvalue().encode()])


class ConnectLeakageTests(ConnectHarness):
    """Criterion 5, flow part: a known link and password reach no output, file or error."""
    def test_known_link_and_password_never_leave_the_transport_boundary(self):
        canaries = [LINK.encode(), NEXT_LINK.encode(), PASSWORD.encode(), b'LEAKCANARY']
        scenarios = [
            ([PHONE, other_device(), other_device(NEXT_LINK), password_state(), READY], ['invalid', 'ok'], {}),
            ([other_device(), password_state(), READY], ['raise'], {}),
            ([other_device(), password_state(), READY], ['silent'], {'lifetime': .5}),
            ([other_device(), password_state(), READY], ['invalid'] * PASSWORD_ATTEMPTS, {}),
            ([other_device(), password_state(), READY], ['flood'], {}),
            ([other_device(), READY], [], {'lifetime': .3, 'raw_options': {'scan_after': None}}),
        ]
        records, logger = [], logging.getLogger()
        class Keep(logging.Handler):
            def emit(self, record):
                records.append(self.format(record).encode('utf-8', 'replace'))
        handler, level = Keep(level=logging.DEBUG), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        budget = mock.patch.object(connect_flow, '_MIN_SUBMIT', .1)
        budget.start()
        self.addCleanup(budget.stop)
        try:
            for flow, outcomes, kwargs in scenarios:
                with self.subTest(outcomes=outcomes):
                    ui = FakeUI(self.paths, [PASSWORD] * (PASSWORD_ATTEMPTS + 1))
                    options = dict(kwargs.pop('raw_options', {}), outcomes=outcomes)
                    with captured_process_output() as output:
                        result, _ = self.connect(flow, ui, raw_options=options, **kwargs)
                    raw = self.raws[-1]
                    if outcomes:
                        # The secret did reach the provider boundary, and only there.
                        self.assertIn(PASSWORD, [row.get('password') for row in raw.sent])
                    self.assertIn(LINK, ui.published)
                    surfaces = list(output) + list(ui.statuses) + records + [
                        repr(result).encode(), json.dumps(result.to_dict()).encode(), repr(ui.notices).encode()]
                    for path in self.root.rglob('*'):
                        if path.is_file() and not path.is_symlink():
                            surfaces.append(path.read_bytes())
                    for surface in surfaces:
                        for canary in canaries:
                            self.assertNotIn(canary, surface)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)

    def test_exception_text_from_ui_and_provider_is_not_carried(self):
        class LeakyUI(FakeUI):
            def publish_qr(self, link):
                raise RuntimeError('cannot render ' + link.reveal().decode())
        ui = LeakyUI(self.paths)
        with captured_process_output() as output:
            result, _ = self.connect([other_device(), READY], ui)
        self.assertEqual((result.phase, result.code), ('failed', 'dependency_unavailable'))
        for surface in list(output) + ui.statuses + [repr(result).encode(), repr(result.to_dict()).encode()]:
            self.assertNotIn(b'LEAKCANARY', surface)


if __name__ == '__main__':
    unittest.main()
