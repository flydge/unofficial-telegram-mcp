"""0.55 phase B: each broker pins its Telegram user at the first authorized check (spec 2, criterion 4).

Fake TDLib clients answer different user ids. Without a registry nothing is read or pinned (0.54). With one,
the first authorized user is pinned for the broker's alias; a different user later blocks with
account_mismatch, and a user pinned under another alias, the default included, with account_duplicate. The
check runs at authorization and before every operation that can grant a Telegram-sourced handle. The user
id never leaves the broker.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_broker import SerializedFakeTDLib

from telegram_search_mcp import account_registry
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import ACCOUNT_CHECK_EXEMPT, AccountBlocked, Broker
from telegram_search_mcp.broker_client import BrokerClient
from telegram_search_mcp.config import KNOWN_CAPABILITIES, AccountPaths, RuntimePolicy, accounts_root
from telegram_search_mcp.contract import OPERATION_CAPABILITIES
from telegram_search_mcp.local_upload_models import UPLOAD_MODELS
from telegram_search_mcp.schemas import ResolveTargetRequest
from telegram_search_mcp.tdjson import AuthorizationBlocked

USER, OTHER, THIRD = 4_000_000_017, 4_000_000_029, 4_000_000_031
POLICY = RuntimePolicy(enabled_capabilities=tuple(sorted(KNOWN_CAPABILITIES)))


class Account(SerializedFakeTDLib):
    """A fake TDLib session authorized as `user_id`, with the real client's authorization epoch."""

    def __init__(self, user_id, *, ready=True):
        super().__init__()
        self.user_id, self.ready = user_id, ready
        self.identity_reads = 0
        self.send_observation_epoch = object()

    def ensure_ready(self):
        super().ensure_ready()
        if not self.ready:
            raise AuthorizationBlocked('the dedicated TDLib session is not authorizationStateReady')

    def get_account_id(self):
        self.identity_reads += 1
        return self.user_id


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='p')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = accounts_root(self.home)

    def broker(self, client, alias='default'):
        account = AccountPaths.for_alias(alias, self.home)
        broker = Broker(socket_path=account.socket, client_factory=lambda: client, verify_peer_uid=False,
                        policy=POLICY, account=account,
                        artifact_store=ArtifactStore(cache_dir=account.artifacts))
        self.addCleanup(broker.shutdown)
        return broker

    def request(self, broker, operation, payload=None):
        return broker._dispatch({'operation': operation, 'payload': {} if payload is None else payload,
                                 'client_id': 'client_' + 'a' * 32, 'request_id': 'request_1',
                                 'deadline': time.monotonic() + 30, 'broker_generation': broker._generation})

    def resolve(self, broker):
        return self.request(broker, 'resolve', ResolveTargetRequest(target='@known_user').model_dump(mode='json'))

    def pins(self):
        value = json.loads((self.root / account_registry.REGISTRY_NAME).read_bytes())
        return {alias: entry['user_id'] for alias, entry in value['aliases'].items()}, value['default_user_id']

    def registry_bytes(self):
        return (self.root / account_registry.REGISTRY_NAME).read_bytes()

    def blocked(self, code, call):
        with self.assertRaises(AccountBlocked) as caught:
            call()
        self.assertEqual((caught.exception.code, str(caught.exception)), (code, code))
        self.assertIsInstance(caught.exception, AuthorizationBlocked)
        for user_id in (USER, OTHER, THIRD):
            self.assertNotIn(str(user_id), repr(caught.exception.args))


class WithoutRegistryTests(Fixture):
    def test_nothing_is_read_pinned_or_created_and_every_operation_behaves_as_before(self):
        client = Account(USER)
        broker = self.broker(client)
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertNotEqual(self.resolve(broker)['status'], 'error')
        self.assertEqual(client.identity_reads, 0)  # The pin never asks who the session is.
        self.assertFalse(self.root.exists())


class DefaultPinTests(Fixture):
    def setUp(self):
        super().setUp()
        account_registry.add_alias('work', self.home, longest_tool='t')

    def test_the_first_authorized_check_pins_the_default_user(self):
        client = Account(USER)
        broker = self.broker(client)
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertEqual(self.pins(), ({'work': None}, USER))
        self.assertNotEqual(self.resolve(broker)['status'], 'error')
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertEqual(client.identity_reads, 1)  # Within one authorization epoch the pinned user is reused.
        client.send_observation_epoch = object()  # A new authorization epoch asks again.
        self.resolve(broker)
        self.assertEqual(client.identity_reads, 2)

    def test_the_first_telegram_operation_pins_when_no_check_came_first(self):
        broker = self.broker(Account(USER))
        self.assertNotEqual(self.resolve(broker)['status'], 'error')
        self.assertEqual(self.pins(), ({'work': None}, USER))

    def test_another_user_later_blocks_with_account_mismatch(self):
        account_registry.pin_account(self.root, 'default', OTHER)
        before = self.registry_bytes()
        broker = self.broker(Account(USER))
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})
        self.blocked('account_mismatch', lambda: self.resolve(broker))
        self.assertEqual(self.registry_bytes(), before)

    def test_a_user_pinned_under_an_alias_blocks_the_default_with_account_duplicate(self):
        account_registry.pin_account(self.root, 'work', USER)
        broker = self.broker(Account(USER))
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})
        self.blocked('account_duplicate', lambda: self.resolve(broker))
        self.assertEqual(self.pins(), ({'work': USER}, None))

    def test_a_session_that_is_not_authorized_pins_nothing_and_reports_its_own_state(self):
        client = Account(USER, ready=False)
        broker = self.broker(client)
        before = self.registry_bytes()
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})
        self.assertEqual(self.resolve(broker)['status'], 'blocked')
        self.assertEqual((client.identity_reads, self.registry_bytes()), (0, before))

    def test_a_registry_created_while_the_broker_runs_takes_effect_at_the_next_operation(self):
        (self.root / account_registry.REGISTRY_NAME).unlink()  # No registry: 0.54.
        client = Account(USER)
        broker = self.broker(client)
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertEqual(client.identity_reads, 0)
        account_registry.add_alias('home', self.home, longest_tool='t')
        account_registry.pin_account(self.root, 'home', USER)
        self.blocked('account_duplicate', lambda: self.resolve(broker))

    def test_an_unreadable_registry_blocks_every_checked_operation(self):
        (self.root / account_registry.REGISTRY_NAME).chmod(0o644)
        broker = self.broker(Account(USER))
        self.blocked('registry_unsafe', lambda: self.resolve(broker))
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})


class AliasPinTests(Fixture):
    def setUp(self):
        super().setUp()
        account_registry.add_alias('work', self.home, longest_tool='t')

    def test_a_named_broker_pins_its_own_alias(self):
        broker = self.broker(Account(OTHER), 'work')
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertEqual(self.pins(), ({'work': OTHER}, None))
        later = self.broker(Account(THIRD), 'work')
        self.assertEqual(self.request(later, 'check'), {'status': 'blocked'})
        self.blocked('account_mismatch', lambda: self.resolve(later))

    def test_the_default_user_under_a_named_alias_is_a_duplicate(self):
        account_registry.pin_account(self.root, 'default', USER)
        broker = self.broker(Account(USER), 'work')
        self.blocked('account_duplicate', lambda: self.resolve(broker))
        self.assertEqual(self.pins(), ({'work': None}, USER))

    def test_two_brokers_with_different_users_pin_side_by_side(self):
        default, work = self.broker(Account(USER)), self.broker(Account(OTHER), 'work')
        self.assertEqual((self.request(default, 'check'), self.request(work, 'check')),
                         ({'status': 'ready'}, {'status': 'ready'}))
        self.assertEqual(self.pins(), ({'work': OTHER}, USER))

    def test_a_removed_alias_blocks_its_running_broker(self):
        broker = self.broker(Account(OTHER), 'work')
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        account_registry.remove_alias('work', self.home)
        self.blocked('alias_unavailable', lambda: self.resolve(broker))

    def test_the_artifact_account_read_runs_the_same_pin(self):
        broker = self.broker(Account(OTHER), 'work')
        self.assertEqual(broker._artifact_account(), OTHER)
        self.assertEqual(self.pins(), ({'work': OTHER}, None))
        later = self.broker(Account(THIRD), 'work')
        self.blocked('account_mismatch', later._artifact_account)

    def test_an_unidentifiable_session_is_never_admitted_once_a_registry_exists(self):
        client = SerializedFakeTDLib()  # No get_account_id at all.
        broker = self.broker(client, 'work')
        self.blocked('account_mismatch', lambda: self.resolve(broker))
        self.blocked('account_mismatch', broker._artifact_account)


class CoverageTests(Fixture):
    def test_every_operation_but_lifecycle_and_local_bytes_is_checked_first(self):
        self.assertEqual(ACCOUNT_CHECK_EXEMPT, {'health', 'release_client', 'check', 'create_local_artifact',
                                                'begin_local_upload', 'append_local_upload', 'finish_local_upload'})
        account_registry.add_alias('work', self.home, longest_tool='t')
        account_registry.pin_account(self.root, 'default', OTHER)
        broker = self.broker(Account(USER))
        checked = sorted(set(OPERATION_CAPABILITIES) - ACCOUNT_CHECK_EXEMPT - set(UPLOAD_MODELS))
        self.assertGreater(len(checked), 40)
        for operation in checked:
            with self.subTest(operation=operation):  # Refused before the payload is even read.
                self.blocked('account_mismatch', lambda: self.request(broker, operation))
        self.assertIn('pending_sends', self.request(broker, 'health'))


class SocketTests(Fixture):
    """Through the real socket: the refusal is the existing blocked or unavailable shape, with no user id."""

    def serve(self, broker):
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(broker.shutdown)  # Cleanups run last-in first: stop, then join.
        self.assertTrue(broker.wait_until_ready(timeout=5))

    def proxy(self, alias='default'):
        account = AccountPaths.for_alias(alias, self.home)
        client = BrokerClient(socket_path=account.socket, policy=POLICY, restart_callback=lambda: None,
                              account=account)
        self.addCleanup(client.close)
        return client

    def test_a_mismatched_account_answers_blocked_and_unavailable_only(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        account_registry.pin_account(self.root, 'work', OTHER)
        self.serve(self.broker(Account(THIRD), 'work'))
        client = self.proxy('work')
        self.assertEqual(client.check(), 'blocked')
        response = client.resolve(ResolveTargetRequest(target='@known_user'))
        self.assertEqual(response.status, 'error')
        self.assertNotIn(str(THIRD), response.model_dump_json())
        self.assertNotIn(str(OTHER), response.model_dump_json())

    def test_the_pinned_account_answers_ready(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        self.serve(self.broker(Account(OTHER), 'work'))
        client = self.proxy('work')
        self.assertEqual(client.check(), 'ready')
        self.assertNotEqual(client.resolve(ResolveTargetRequest(target='@known_user')).status, 'error')
        self.assertEqual(self.pins(), ({'work': OTHER}, None))


if __name__ == '__main__':
    unittest.main()
