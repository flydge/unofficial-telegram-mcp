"""0.55 phase C: re-login hardening inside one broker (spec 3C).

A discovery cursor (`scan_`) is bound to the client lifecycle, the broker generation and the account that
started it: presented under another account or generation it is refused like an unknown cursor and dropped.
The draft registry's routing helpers (`attempt_outcome`, `is_owned_*`) take the DraftOwner: a call that names an
owner routes only that owner's draft, its client and its account. A call that lost the authority to name it (a
release, an account switch) passes None and its client alone decides, so a consumed claim never reads as expired
(the 0.54 rule). Every client is synthetic.
"""
from __future__ import annotations

import inspect
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from test_target_discovery import FakeClock, FakeDiscoveryClient, discover, registry

from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.discovery_state import DiscoveryCursorExpired, DiscoveryRegistry
from telegram_search_mcp.outgoing_drafts import (DraftOwner, OutgoingDraft, OutgoingDraftRegistry, SendReceipt,
                                                 _Entry)
from telegram_search_mcp.search_service import SearchService
from telegram_search_mcp.target_discovery import TargetDiscoveryService
from telegram_search_mcp.schemas import DiscoverTargetsRequest, SendPreparedArtifactRequest

CLIENT, FOREIGN = 'client_' + 'a' * 24, 'client_' + 'b' * 24
DIGEST = '0' * 64


class AccountClient(FakeDiscoveryClient):
    def __init__(self, account=7):
        super().__init__()
        self.account = account

    def ensure_ready(self):
        pass

    def get_account_id(self):
        self.calls.append(('get_account_id',))
        return self.account


class RegistryBindingTests(unittest.TestCase):
    def test_a_scan_binds_its_owner_and_another_owner_drops_it(self):
        scans = DiscoveryRegistry(clock=FakeClock(), ttl_seconds=300, capacity=4)
        owner = (CLIENT, 'broker_g1', 7)
        cursor = scans.start(hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=owner)
        self.assertEqual(scans.bind(cursor, hypothesis_digest=DIGEST, hypothesis_count=2, scope='main',
                                    owner=owner).cursor, cursor)
        # Other hypotheses keep the scan (a caller's mistake); another owner drops it for good.
        with self.assertRaises(DiscoveryCursorExpired):
            scans.bind(cursor, hypothesis_digest='1' * 64, hypothesis_count=2, scope='main', owner=owner)
        scans.bind(cursor, hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=owner)
        for other in ((CLIENT, 'broker_g1', 8), (CLIENT, 'broker_g2', 7), (FOREIGN, 'broker_g1', 7), ()):
            with self.subTest(owner=other):
                fresh = scans.start(hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=owner)
                with self.assertRaises(DiscoveryCursorExpired):
                    scans.bind(fresh, hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=other)
                with self.assertRaises(DiscoveryCursorExpired):  # Switching back does not resurrect it.
                    scans.bind(fresh, hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=owner)
        with self.assertRaises(ValueError):
            scans.start(hypothesis_digest=DIGEST, hypothesis_count=2, scope='main', owner=[CLIENT])

    def test_the_owner_defaults_to_none_for_unbound_callers(self):
        scans = DiscoveryRegistry(clock=FakeClock(), ttl_seconds=300, capacity=4)
        cursor = scans.start(hypothesis_digest=DIGEST, hypothesis_count=2, scope='main')
        scans.bind(cursor, hypothesis_digest=DIGEST, hypothesis_count=2, scope='main')
        self.assertEqual(inspect.signature(DiscoveryRegistry.start).parameters['owner'].default, ())


class DiscoveryCursorTests(unittest.TestCase):
    def test_a_cursor_from_another_account_is_refused_like_an_unknown_one(self):
        client = AccountClient(7)
        service = TargetDiscoveryService(client=client, registry=registry(), owner=(CLIENT, 'broker_g1'))
        first = discover(service, ['garden', 'seeds'])
        self.assertEqual(first.status, 'page')
        self.assertTrue(first.next_cursor.startswith('scan_'))
        client.account = 8
        switched = discover(service, ['garden', 'seeds'], cursor=first.next_cursor)
        unknown = discover(service, ['garden', 'seeds'], cursor='scan_' + 'x' * 32)
        self.assertEqual(switched.model_dump(), unknown.model_dump())
        self.assertEqual(switched.status, 'expired')
        client.account = 7  # The scan is gone, not paused.
        self.assertEqual(discover(service, ['garden', 'seeds'], cursor=first.next_cursor).status, 'expired')

    def test_a_switch_during_the_call_drops_the_page_like_an_unknown_cursor(self):
        scans = registry()
        client = AccountClient(7)
        service = TargetDiscoveryService(client=client, registry=scans, owner=(CLIENT, 'broker_g1'))
        first = discover(service, ['garden', 'seeds'])
        self.assertEqual(first.status, 'page')
        reads = iter([7])  # Authorized as 7 when the call binds, as 8 after its provider reads.
        client.get_account_id = lambda: next(reads, 8)
        switched = discover(service, ['garden', 'seeds'], cursor=first.next_cursor)
        unknown = discover(service, ['garden', 'seeds'], cursor='scan_' + 'x' * 32)
        self.assertEqual(switched.model_dump(), unknown.model_dump())
        client.get_account_id = lambda: 7  # The scan is gone with the page, not paused.
        self.assertEqual(discover(service, ['garden', 'seeds'], cursor=first.next_cursor).status, 'expired')
        self.assertEqual(scans._scans, {})
        reads = iter([7])  # A first page read across a switch is dropped too, and leaves no scan behind.
        client.get_account_id = lambda: next(reads, 8)
        fresh = discover(service, ['garden', 'seeds'])
        self.assertEqual((fresh.status, fresh.next_cursor, fresh.candidates), ('expired', None, []))
        self.assertEqual(scans._scans, {})

    def test_a_cursor_from_another_generation_is_refused(self):
        shared = registry()
        client = AccountClient(7)
        before = TargetDiscoveryService(client=client, registry=shared, owner=(CLIENT, 'broker_g1'))
        after = TargetDiscoveryService(client=client, registry=shared, owner=(CLIENT, 'broker_g2'))
        first = discover(before, ['garden', 'seeds'])
        self.assertEqual(discover(after, ['garden', 'seeds'], cursor=first.next_cursor).status, 'expired')

    def test_an_unreadable_account_is_an_error_and_a_blocked_one_blocked(self):
        from telegram_search_mcp.tdjson import AuthorizationBlocked, TDLibError
        for error, status in ((TDLibError('SYNTHETIC'), 'error'), (AuthorizationBlocked('SYNTHETIC'), 'blocked')):
            with self.subTest(status=status):
                client = AccountClient(7)
                client.get_account_id = lambda error=error: (_ for _ in ()).throw(error)
                response = discover(TargetDiscoveryService(client=client, registry=registry()), ['garden', 'seeds'])
                self.assertEqual((response.status, response.next_cursor), (status, None))

    def test_the_search_service_binds_its_client_and_generation(self):
        client = AccountClient(7)
        service = SearchService(client=client, discovery_registry=registry(), owns_client=False,
                                client_id=CLIENT, broker_generation='broker_g1')
        request = DiscoverTargetsRequest.model_validate({'hypotheses': ['garden', 'seeds'], 'scope': 'main'})
        first = service.discover(request)
        self.assertEqual(first.status, 'page')
        client.account = 8
        self.assertEqual(service.discover(request.model_copy(update={'cursor': first.next_cursor})).status,
                         'expired')


def entry(owner, *, kind='text', attempted=True, evidence='none', status='outcome_unknown', **draft):
    values = dict(draft_id='draft_' + '1' * 32, artifact_id='', sha256='0' * 64, size_bytes=1,
                  display_name='Text message', mime_type='text/plain', caption='', recipient=42,
                  expires_at=time.time() + 600, kind=kind)
    values.update(draft)
    receipt = SendReceipt(values['draft_id'], status, None, time.time(), evidence) if attempted else None
    return _Entry(draft=OutgoingDraft(**values), owner=owner, state='finished' if attempted else 'pending',
                  receipt=receipt, attempted_at=time.monotonic() if attempted else None)


class DraftOwnerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.drafts = OutgoingDraftRegistry(store=None)
        self.owner = DraftOwner(CLIENT, 7)
        self.other_account = DraftOwner(CLIENT, 8)
        self.foreign = DraftOwner(FOREIGN, 7)

    def put(self, value):
        self.drafts._entries[value.draft.draft_id] = value
        return value.draft.draft_id

    def test_is_owned_routes_only_for_the_drafts_own_client_and_account(self):
        topic = self.put(entry(self.owner, attempted=False, draft_id='draft_' + 'a' * 32, topic=object()))
        formatted = self.put(entry(self.owner, attempted=False, draft_id='draft_' + 'b' * 32,
                                   outgoing_entities=(('bold', 0, 1),)))
        reply = self.put(entry(self.owner, attempted=False, draft_id='draft_' + 'c' * 32, kind='document',
                               reply_source=object()))
        for check, draft_id in ((self.drafts.is_owned_topic, topic), (self.drafts.is_owned_formatted, formatted),
                                (self.drafts.is_owned_artifact_reply, reply)):
            with self.subTest(check=check.__name__):
                self.assertTrue(check(draft_id, owner=self.owner))
                self.assertFalse(check(draft_id, owner=self.other_account))
                self.assertFalse(check(draft_id, owner=self.foreign))
                self.assertFalse(check('draft_' + '9' * 32, owner=self.owner))
                with self.assertRaises(TypeError):
                    check(draft_id, client_id=CLIENT)

    def test_attempt_outcome_routes_a_named_owners_draft_only(self):
        local = self.put(entry(self.owner, evidence='local_failed', status='failed', draft_id='draft_' + 'd' * 32))
        sent = self.put(entry(self.owner, status='sent', evidence='provider_confirmed', draft_id='draft_' + 'e' * 32))
        pending = self.put(entry(self.owner, attempted=False, draft_id='draft_' + 'f' * 32))
        kinds = frozenset({'text'})
        outcome = lambda draft_id, owner, client=CLIENT: self.drafts.attempt_outcome(
            draft_id, client_id=client, owner=owner, kinds=kinds)
        self.assertEqual(outcome(local, self.owner), 'local_failed')
        self.assertEqual(outcome(sent, self.owner), 'attempted')
        # A call that lost its owner (release, account switch): its client decides; never expired.
        self.assertEqual(outcome(local, None), 'local_failed')
        self.assertEqual(outcome(sent, None), 'attempted')
        # A named owner of another account routes nothing of this account's.
        self.assertIsNone(outcome(local, self.other_account))
        self.assertIsNone(outcome(sent, self.other_account))
        # Another client, a mismatched owner, a draft never claimed or another kind: nothing.
        self.assertIsNone(outcome(local, self.foreign, FOREIGN))
        self.assertIsNone(outcome(local, self.foreign))
        self.assertIsNone(outcome(pending, self.owner))
        self.assertIsNone(self.drafts.attempt_outcome(local, client_id=CLIENT, owner=self.owner,
                                                      kinds=frozenset({'video'})))
        self.assertEqual(list(inspect.signature(OutgoingDraftRegistry.attempt_outcome).parameters),
                         ['self', 'draft_id', 'client_id', 'owner', 'kinds'])


class BudgetedProvider:
    def __init__(self):
        self.budgets = []

    @contextmanager
    def request_budget(self, deadline):
        self.budgets.append(deadline)
        yield


class BrokerRoutingTests(unittest.TestCase):
    """The broker routes a send budget by the DraftOwner of the account it last observed; no provider read."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.provider = BudgetedProvider()
        self.broker = Broker(socket_path=Path(temporary.name) / 'broker.sock', client_factory=lambda: self.provider,
                             policy=RuntimePolicy(enabled_capabilities=('send',)), verify_peer_uid=False)
        self.addCleanup(self.broker.shutdown)
        self.topic = 'draft_' + 'a' * 32
        self.broker._drafts._entries[self.topic] = entry(DraftOwner(CLIENT, 7), attempted=False,
                                                         draft_id=self.topic, topic=object())

    def route(self, observed):
        self.broker._artifact_authority.invalidate_account()
        if observed is not None:
            self.broker._artifact_authority.observe_account(observed)
        with mock.patch.object(self.broker, '_send_prepared_artifact_impl', return_value='done') as impl:
            self.assertEqual(self.broker._send_prepared_artifact(
                SendPreparedArtifactRequest(draft_id=self.topic, approved=True), client_id=CLIENT, text_only=True,
                deadline=None), 'done')
        return impl.call_args.kwargs['deadline']

    def test_only_the_observed_accounts_own_draft_is_budgeted(self):
        self.assertIsNotNone(self.route(7))
        self.assertEqual(len(self.provider.budgets), 1)
        self.assertIsNone(self.route(8))  # Another account's draft: no budget, and the send itself refuses it.
        self.assertIsNone(self.route(None))
        self.assertEqual(len(self.provider.budgets), 1)
        self.assertEqual(self.broker._observed_owner(CLIENT), None)
        self.broker._artifact_authority.observe_account(7)
        self.assertEqual(self.broker._observed_owner(CLIENT), DraftOwner(CLIENT, 7))
        self.assertIsNone(self.broker._observed_owner('not a client id'))


if __name__ == '__main__':
    unittest.main()
