"""QR and password observations over the pinned auth schema; all values synthetic."""
import unittest

from telegram_search_mcp.auth_transport import (AuthObservation, AuthTransport, AuthTransportError,
                                                AuthorizationState)
from telegram_search_mcp.credentials import SecretValue

LINK = 'tg://login?token=' + 'A' * 40 + '_-x'
NEXT_LINK = 'tg://login?token=' + 'B' * 43


class StateRaw:
    """Answers getAuthorizationState with a full state object; queues updates."""
    def __init__(self, state, *, fail=None):
        self.state, self.fail = state, fail
        self.sent, self.queue, self.closed = [], [], False
    def send(self, payload):
        self.sent.append(dict(payload))
        if payload['@type'] == self.fail:
            self.queue.append({'@type': 'error', '@extra': payload['@extra'], 'code': 429,
                               'message': 'Too Many Requests: retry after 17'})
        elif payload['@type'] == 'getAuthorizationState':
            self.queue.append({**self.state, '@extra': payload['@extra']})
        else:
            self.queue.append({'@type': 'ok', '@extra': payload['@extra']})
    def update(self, state):
        self.queue.append({'@type': 'updateAuthorizationState', 'authorization_state': state})
    def receive(self, timeout):
        return self.queue.pop(0) if self.queue else None
    def close(self):
        self.closed = True


def other_device(link):
    return {'@type': 'authorizationStateWaitOtherDeviceConfirmation', 'link': link}


def password(hint):
    return {'@type': 'authorizationStateWaitPassword', 'password_hint': hint,
            'has_recovery_email_address': False, 'has_passport_data': False,
            'recovery_email_address_pattern': ''}


class QrObservationTests(unittest.TestCase):
    def test_valid_link_is_observed_as_redacted_secret(self):
        transport = AuthTransport(StateRaw(other_device(LINK)))
        self.assertIs(transport.get_authorization_state(), AuthorizationState.OTHER_DEVICE)
        observation = transport.observation()
        self.assertIsInstance(observation, AuthObservation)
        self.assertIs(observation.state, AuthorizationState.OTHER_DEVICE)
        self.assertEqual(observation.qr_link.reveal(), LINK.encode('ascii'))
        self.assertNotIn('AAAA', repr(observation))
        self.assertIsNone(observation.password_hint)

    def test_malformed_links_fail_closed_without_retaining_value(self):
        for link in ['http://login?token=' + 'A' * 40, 'tg://login?token=', 'tg://login?token=' + 'A' * 300,
                     'tg://login?token=AAAA\nAAAAAAAAAAAAAAAA', 'tg://join?invite=' + 'A' * 40,
                     'tg://login?token=' + 'A' * 40 + '&x=1', 17, None]:
            with self.subTest(link=link):
                transport = AuthTransport(StateRaw(other_device(link)))
                self.assertIs(transport.get_authorization_state(), AuthorizationState.UNSUPPORTED)
                self.assertIsNone(transport.observation().qr_link)

    def test_request_qr_code_sends_exact_pinned_request_once(self):
        raw = StateRaw({'@type': 'authorizationStateWaitPhoneNumber'})
        AuthTransport(raw).request_qr_code()
        self.assertEqual([row['@type'] for row in raw.sent], ['requestQrCodeAuthentication'])
        self.assertEqual(raw.sent[0]['other_user_ids'], [])
        self.assertEqual(set(raw.sent[0]), {'@type', '@extra', 'other_user_ids'})

    def test_request_qr_code_maps_flood_wait_without_provider_text(self):
        raw = StateRaw({'@type': 'authorizationStateWaitPhoneNumber'}, fail='requestQrCodeAuthentication')
        with self.assertRaises(AuthTransportError) as caught:
            AuthTransport(raw).request_qr_code()
        self.assertEqual((caught.exception.code, caught.exception.retry_after), ('flood_wait', 17))
        self.assertNotIn('Too Many', str(caught.exception))

    def test_link_only_change_is_a_change(self):
        raw = StateRaw(other_device(LINK))
        transport = AuthTransport(raw)
        transport.get_authorization_state()
        first = transport.observation()
        raw.update(other_device(LINK))
        raw.update(other_device(NEXT_LINK))
        changed = transport.wait_for_change(first, timeout=5)
        self.assertIs(changed.state, AuthorizationState.OTHER_DEVICE)
        self.assertEqual(changed.qr_link.reveal(), NEXT_LINK.encode('ascii'))

    def test_state_change_after_confirmation(self):
        raw = StateRaw(other_device(LINK))
        transport = AuthTransport(raw)
        transport.get_authorization_state()
        first = transport.observation()
        raw.update({'@type': 'authorizationStateReady'})
        self.assertIs(transport.wait_for_change(first, timeout=5).state, AuthorizationState.READY)

    def test_unchanged_observation_times_out(self):
        ticks = iter([0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1, 2, 3])
        raw = StateRaw(other_device(LINK))
        transport = AuthTransport(raw, _clock=lambda: next(ticks))
        transport.get_authorization_state(timeout=5)
        first = transport.observation()
        raw.update(other_device(LINK))
        with self.assertRaises(AuthTransportError) as caught:
            transport.wait_for_change(first, timeout=.5)
        self.assertEqual(caught.exception.code, 'timeout')

    def test_password_hint_is_sanitized_and_bounded(self):
        for hint, expected in [('', ''), ('my‮cat\x00', 'mycat'), ('x' * 300, 'x' * 127 + '…'),
                               ('  two\nlines ', 'two lines'), (None, '')]:
            with self.subTest(hint=hint):
                transport = AuthTransport(StateRaw(password(hint)))
                self.assertIs(transport.get_authorization_state(), AuthorizationState.PASSWORD)
                observation = transport.observation()
                self.assertEqual(observation.password_hint, expected)
                self.assertIsNone(observation.qr_link)

    def test_request_after_teardown_sends_nothing(self):
        raw = StateRaw({'@type': 'authorizationStateWaitPhoneNumber'})
        transport = AuthTransport(raw)
        transport.close(timeout=.01)
        sent = list(raw.sent)
        with self.assertRaises(AuthTransportError):
            transport.request_qr_code()
        self.assertEqual(raw.sent, sent)

    def test_legacy_enum_api_is_unchanged_for_terminal_path(self):
        raw = StateRaw({'@type': 'authorizationStateWaitCode', 'code_info': {}})
        transport = AuthTransport(raw)
        self.assertIs(transport.get_authorization_state(), AuthorizationState.CODE)
        raw.update({'@type': 'authorizationStateWaitPassword', 'password_hint': 'h'})
        self.assertIs(transport.wait_for_state_change(AuthorizationState.CODE, timeout=5),
                      AuthorizationState.PASSWORD)


if __name__ == '__main__':
    unittest.main()
