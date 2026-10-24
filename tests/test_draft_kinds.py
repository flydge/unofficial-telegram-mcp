"""0.57 work item 1: the one draft-kind table, behaviour-identical to the sets and dispatch it replaced.

One test per flag (change, unbacked, dedicated, status policy) against the registry and the broker, plus the rows
themselves pinned to the 0.56 sets. Synthetic registry entries only; no provider, no dialog.
"""
from __future__ import annotations

import tempfile
import time
import typing
import unittest
import uuid
from pathlib import Path

from telegram_search_mcp import draft_kinds
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CompatibilityError, required_capabilities
from telegram_search_mcp.draft_kinds import DRAFT_KINDS, DraftKind, change_kind_of
from telegram_search_mcp.draft_models import DraftSummary
from telegram_search_mcp.outgoing_drafts import (DraftError, DraftOwner, OutgoingDraft, OutgoingDraftRegistry,
                                                 _Entry)

CLIENT = 'client_' + 'k' * 24
OWNER = DraftOwner(client_id=CLIENT, account_id=7)
CHAT = -1001234
# The 0.56 sets this table replaced (outgoing_drafts._CHANGE_KINDS, _UNBACKED_KINDS, _DEDICATED_KINDS and the
# broker's kind tuples), restated independently.
CHANGE_056 = {'archive', 'text_edit'}
UNBACKED_056 = {'text', 'forward', 'archive', 'text_edit'}
DEDICATED_056 = {'forward', 'video', 'album', 'archive', 'text_edit'}
STATUS_056 = {'forward': 'forward_send', 'video': 'video_send', 'archive': 'archive_chats',
              'text_edit': 'edit_own_text', 'album': 'refused'}
# 0.57 adds three rows, each a dedicated unbacked change: the caption edit (part 1, bound to edit_own_caption), the
# pin change (part 2, bound to pin_messages) and the message deletion (part 3, bound to delete_own_message). A styled
# text edit is a text_edit row whose payload names edit_own_styled_text as an extra capability.
NEW_057 = {'caption_edit', 'pin_change', 'message_delete'}
CHANGE_057, UNBACKED_057, DEDICATED_057 = CHANGE_056 | NEW_057, UNBACKED_056 | NEW_057, DEDICATED_056 | NEW_057
STATUS_057 = {**STATUS_056, 'caption_edit': 'edit_own_caption', 'pin_change': 'pin_messages',
              'message_delete': 'delete_own_message'}
ALL = ('document', 'photo', 'voice_note', 'text', 'forward', 'video', 'archive', 'text_edit', 'caption_edit',
       'pin_change', 'message_delete', 'album')


def insert(registry, kind, owner=OWNER, state='pending', payload=False):
    """A synthetic entry of any kind, bypassing the typed prepare paths (white-box); `payload` fills the field named
    by the kind, when OutgoingDraft has one, with an opaque object."""
    extra = {kind: object()} if payload and kind in OutgoingDraft.__dataclass_fields__ else {}
    draft = OutgoingDraft(draft_id=f'draft_{uuid.uuid4().hex}', artifact_id='artifact_x', sha256='0' * 64,
                          size_bytes=1, display_name='x', mime_type='application/json', caption='', recipient=CHAT,
                          expires_at=time.time() + 600, recipient_title='Team', kind=kind, **extra)
    registry._entries[draft.draft_id] = _Entry(draft=draft, owner=owner, state=state)
    return draft.draft_id


class Store:
    """Every artifact lookup is recorded and finds nothing."""
    def __init__(self):
        self.lookups = []

    def lookup(self, artifact_id):
        self.lookups.append(artifact_id)
        return None


class TableTests(unittest.TestCase):
    def test_rows_reproduce_the_replaced_sets(self):
        self.assertEqual(tuple(DRAFT_KINDS), ALL)
        self.assertEqual(draft_kinds.CHANGE_KINDS, CHANGE_057)
        self.assertEqual(draft_kinds.UNBACKED_KINDS, UNBACKED_057)
        self.assertEqual(draft_kinds.DEDICATED_KINDS, DEDICATED_057)
        self.assertEqual({name: row.status for name, row in DRAFT_KINDS.items() if row.status is not None}, STATUS_057)
        self.assertEqual(draft_kinds.GENERIC_ARTIFACT_KINDS, {'document', 'photo', 'voice_note'})
        self.assertEqual(draft_kinds.CHANGE_CAPABILITIES, {'archive_chats', 'edit_own_text', 'edit_own_caption',
                                                           'pin_messages', 'delete_own_message'})

    def test_every_kind_literal_follows_the_table(self):
        self.assertEqual(set(typing.get_args(OutgoingDraft.__dataclass_fields__['kind'].type
                                             if not isinstance(OutgoingDraft.__dataclass_fields__['kind'].type, str)
                                             else typing.get_type_hints(OutgoingDraft)['kind'])), set(DRAFT_KINDS))
        # The frozen 0.52 summary lists only the generic kinds; dedicated kinds never reach it.
        self.assertEqual(set(typing.get_args(DraftSummary.model_fields['kind'].annotation)),
                         set(DRAFT_KINDS) - draft_kinds.DEDICATED_KINDS)

    def test_change_rows_name_their_payload_field_class_and_display_name(self):
        from telegram_search_mcp.archive_drafts import ArchiveSpec
        from telegram_search_mcp.caption_edit_drafts import CaptionEditSpec
        from telegram_search_mcp.edit_drafts import TextEditSpec
        self.assertIs(DRAFT_KINDS['archive'].spec_type(), ArchiveSpec)
        self.assertIs(DRAFT_KINDS['text_edit'].spec_type(), TextEditSpec)
        self.assertIs(DRAFT_KINDS['caption_edit'].spec_type(), CaptionEditSpec)
        self.assertEqual((DRAFT_KINDS['archive'].display_name, DRAFT_KINDS['text_edit'].display_name),
                         ('Chat list move', 'Text edit'))
        for name in draft_kinds.CHANGE_KINDS:
            self.assertIn(name, OutgoingDraft.__dataclass_fields__)
        self.assertIsNone(change_kind_of(object()))
        with self.assertRaises(ValueError):
            DRAFT_KINDS['text'].spec_type()

    def test_inconsistent_rows_are_refused(self):
        for row in (dict(name='x', change=True), dict(name='x', change=True, unbacked=True, dedicated=True),
                    dict(name='x', change=True, unbacked=True, dedicated=True, status='refused',
                         spec='edit_drafts:TextEditSpec', display_name='x'),
                    dict(name='x', spec='edit_drafts:TextEditSpec'), dict(name='x', display_name='x'),
                    dict(name='x', status='video_send'), dict(name='x', dedicated=1), dict(name='')):
            with self.subTest(row=row), self.assertRaises(ValueError):
                DraftKind(**row)


class ChangeFlagTests(unittest.TestCase):
    """change: only a change kind finishes `applied`, never `sent`, and never reconciles from a send observation."""

    def test_applied_receipts_and_change_claims_follow_the_change_flag(self):
        for kind in ALL:
            if kind == 'album':
                continue  # An album finishes only with every slot (finish_album).
            with self.subTest(kind=kind):
                registry = OutgoingDraftRegistry(Store())
                did = insert(registry, kind, state='claimed', payload=True)
                registry._entries[did].attempted_at = time.monotonic()
                change = kind in CHANGE_057
                self.assertEqual(registry.change_claim_current(did, owner=OWNER, kind=kind, provider=None,
                                                               provider_epoch=None), change)
                with self.assertRaises(ValueError):
                    registry.finish(did, owner=OWNER, status='sent' if change else 'applied',
                                    **({'message_id': 5} if change else {'observed': 1}))
                with self.assertRaises(DraftError) if change else _no_error():
                    registry.reconcile(did, owner=OWNER, provider=None, provider_epoch=None,
                                       attempted_at=registry._entries[did].attempted_at, status='failed')
                if change:
                    receipt = registry.finish(did, owner=OWNER, status='applied', observed=1)
                    self.assertEqual((receipt.status, receipt.observed), ('applied', 1))


class _no_error:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UnbackedFlagTests(unittest.TestCase):
    """unbacked: a pending unbacked draft never looks up an artifact; a backed one is re-verified against it."""

    def test_pending_reads_artifacts_only_for_backed_kinds(self):
        for kind in ALL:
            with self.subTest(kind=kind):
                store = Store()
                registry = OutgoingDraftRegistry(store)
                did = insert(registry, kind)
                if kind in UNBACKED_057:
                    self.assertIsNone(registry._pending_locked(registry._entries[did]))
                    self.assertEqual(store.lookups, [])
                else:
                    if kind == 'album':
                        continue  # An album has no single artifact; its items are re-hashed (tests/test_album_send.py).
                    with self.assertRaises(DraftError):
                        registry._pending_locked(registry._entries[did])
                    self.assertEqual(store.lookups, ['artifact_x'])
                    self.assertEqual(registry._entries[did].state, 'invalid')


class DedicatedFlagTests(unittest.TestCase):
    """dedicated: the generic draft tools never list, revise or cancel the kind; the generic send tools refuse it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                             client_factory=lambda: self.fail('provider opened'),
                             policy=RuntimePolicy(enabled_capabilities=('send', 'read', 'artifacts')),
                             approval_prompt=lambda **kw: self.fail('dialog shown'), verify_peer_uid=False)
        self.addCleanup(self.broker.shutdown)

    def test_generic_tools_skip_and_refuse_dedicated_kinds(self):
        registry = OutgoingDraftRegistry(Store())
        ids = {kind: insert(registry, kind) for kind in ('text', 'forward', 'video', 'archive', 'text_edit',
                                                         'caption_edit', 'album')}
        listed, _ = registry.list_pending(owner=OWNER, limit=50)
        self.assertEqual({draft.kind for draft in listed}, {'text'})
        for kind, did in ids.items():
            with self.subTest(kind=kind):
                dedicated = kind in DEDICATED_057
                with self.assertRaises(DraftError) if dedicated else _no_error():
                    registry.revise(did, owner=OWNER, recipient_title='Team', text='new' if kind == 'text' else None)
                with self.assertRaises(DraftError) if dedicated else _no_error():
                    registry.cancel(insert(registry, kind), owner=OWNER)

    def test_broker_preview_and_generic_send_refuse_dedicated_kinds(self):
        for kind in ALL:
            with self.subTest(kind=kind):
                did = insert(self.broker._drafts, kind)
                draft = self.broker._drafts._entries[did].draft
                if kind in DEDICATED_057:
                    with self.assertRaises(DraftError):
                        self.broker._draft_preview(draft, OWNER)
                    for text_only in (True, False):
                        with self.subTest(text_only=text_only):
                            self.broker._draft_owner = lambda client_id: OWNER
                            from telegram_search_mcp.schemas import SendPreparedArtifactRequest
                            result = self.broker._send_prepared_artifact_impl(
                                SendPreparedArtifactRequest(draft_id=did, approved=True), client_id=CLIENT,
                                text_only=text_only)
                            self.assertEqual((result.status, result.detail),
                                             ('error', 'draft kind does not match send tool'))


class StatusPolicyTests(unittest.TestCase):
    """status: a dedicated kind's status stays bound to its own capability; an album's generic status is refused."""

    def broker(self, enabled):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        broker = Broker(socket_path=root / 'broker.sock', artifact_store=ArtifactStore(cache_dir=root / 'cache'),
                        client_factory=lambda: self.fail('provider opened'),
                        policy=RuntimePolicy(enabled_capabilities=enabled), verify_peer_uid=False)
        self.addCleanup(broker.shutdown)
        return broker

    def test_each_kind_checks_exactly_its_status_capability(self):
        for kind in ALL:
            status = STATUS_057.get(kind)
            with self.subTest(kind=kind):
                base = self.broker(('send', 'read', 'artifacts'))
                did = insert(base._drafts, kind)
                if status == 'refused':
                    with self.assertRaises(DraftError):
                        base._require_topic_policy(did, OWNER)
                    continue
                if status is None:
                    self.assertIsNone(base._require_topic_policy(did, OWNER))
                    continue
                with self.assertRaises(CompatibilityError):
                    base._require_topic_policy(did, OWNER)
                needed = required_capabilities(status)
                for missing in needed:
                    partial = self.broker(tuple(name for name in ('artifacts', *needed) if name != missing))
                    other = insert(partial._drafts, kind)
                    with self.subTest(missing=missing), self.assertRaises(CompatibilityError):
                        partial._require_topic_policy(other, OWNER)
                full = self.broker(('artifacts', *needed))
                self.assertIsNone(full._require_topic_policy(insert(full._drafts, kind), OWNER))

    def test_change_policy_admits_only_change_capabilities(self):
        broker = self.broker(tuple(sorted({'send', 'read', 'archive_chats', 'edit_own_text', 'edit_own_caption',
                                           'forward_send', 'video_send'})))
        for capability in ('archive_chats', 'edit_own_text', 'edit_own_caption'):
            self.assertIsNone(broker._require_change_policy(capability))
        for capability in ('forward_send', 'video_send', 'send', 'read'):
            with self.subTest(capability=capability), self.assertRaises(CompatibilityError):
                broker._require_change_policy(capability)


if __name__ == '__main__':
    unittest.main()
