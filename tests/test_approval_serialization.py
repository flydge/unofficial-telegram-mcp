"""One owner dialog at a time (0.54 safety finding): while one approval dialog is open, another send or apply
answers dialog_unavailable at once and its draft stays pending, so a click on the visible dialog can never
approve a different draft. Nothing is queued behind the open dialog. Once an alias registry exists the rule holds
across every broker of the user (critic fix 055f, high)."""
from __future__ import annotations

import inspect
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram_search_mcp.broker as broker_module
from telegram_search_mcp import account_registry
from telegram_search_mcp.approval_prompt import DETAILS
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.config import AccountPaths, accounts_root
from telegram_search_mcp.schemas import PrepareArtifactSendRequest, SendPreparedArtifactRequest
from telegram_search_mcp.session_owner import OwnerLeaseError

from test_broker_outgoing import FakeSender

CLIENT = "client_" + "a" * 24


class SerializedDialogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        source = self.root / "source.txt"
        source.write_bytes(b"reviewed artifact")
        store = ArtifactStore(cache_dir=self.root / "artifacts")
        self.artifact = store.store(source)
        self.sender = FakeSender()
        self.opened, self.release = threading.Event(), threading.Event()
        self.dialogs: list[str] = []
        self.answers: list[object] = []
        self.broker = broker_module.Broker(socket_path=self.root / "broker.sock", artifact_store=store,
                                           client_factory=lambda: self.sender, verify_peer_uid=False,
                                           approval_prompt=self.prompt)
        self.addCleanup(self.broker.shutdown)
        for name, target in (("stage_approved_document", broker_module.stage_approved_document),
                             ("retire_staged_document", broker_module.retire_staged_document)):
            patcher = patch.object(broker_module, name, side_effect=partial(target, root=self.root / "outgoing"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def prompt(self, **facts):
        """The first dialog stays open until the test releases it; later ones answer at once."""
        self.dialogs.append(facts["caption"])
        if len(self.dialogs) == 1:
            self.opened.set()
            if not self.release.wait(10):
                raise AssertionError("the open dialog was never released")
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def prepare(self, caption):
        return self.broker._prepare_artifact_send(PrepareArtifactSendRequest(
            artifact_id=self.artifact.artifact_id, recipient=123, display_name="report.txt",
            mime_type="text/plain", caption=caption), client_id=CLIENT).draft_id

    def send(self, draft_id):
        return self.broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=draft_id, approved=True),
                                                   client_id=CLIENT)

    def test_a_second_send_while_a_dialog_is_open_is_dialog_unavailable_and_stays_pending(self):
        first, second = self.prepare("first"), self.prepare("second")
        results = {}
        worker = threading.Thread(target=lambda: results.setdefault("first", self.send(first)))
        worker.start()
        self.assertTrue(self.opened.wait(10))
        refused = self.send(second)
        self.assertEqual((refused.status, refused.detail), ("dialog_unavailable", DETAILS["dialog_unavailable"]))
        self.assertEqual(self.dialogs, ["first"])  # no second dialog was shown or queued
        self.assertEqual(self.sender.sent, [])
        self.release.set()
        worker.join(10)
        self.assertEqual(results["first"].status, "sent")
        self.assertEqual([caption for _, _, caption in self.sender.sent], ["first"])
        # The refused draft stayed pending: once the dialog closed, its own dialog can open.
        self.assertEqual(self.send(second).status, "sent")
        self.assertEqual(self.dialogs, ["first", "second"])
        self.assertEqual([caption for _, _, caption in self.sender.sent], ["first", "second"])

    def test_a_repeat_on_the_same_draft_while_its_dialog_is_open_shows_no_second_dialog(self):
        draft, results = self.prepare("only"), {}
        worker = threading.Thread(target=lambda: results.setdefault("first", self.send(draft)))
        worker.start()
        self.assertTrue(self.opened.wait(10))
        self.assertEqual(self.send(draft).status, "dialog_unavailable")
        self.release.set()
        worker.join(10)
        self.assertEqual(results["first"].status, "sent")
        replay = self.send(draft)  # The receipt is replayed; no second dialog and no second attempt.
        self.assertEqual(replay.status, "sent")
        self.assertIn("no new send", replay.detail)
        self.assertEqual((self.dialogs, len(self.sender.sent)), (["only"], 1))

    def test_the_open_dialog_is_counted_once_for_the_installers_gate(self):
        first, second = self.prepare("first"), self.prepare("second")
        worker = threading.Thread(target=lambda: self.send(first))
        worker.start()
        self.assertTrue(self.opened.wait(10))
        self.assertEqual(self.broker._health()["pending_sends"]["approval_dialogs"], 1)
        self.assertEqual(self.send(second).status, "dialog_unavailable")  # refused before any dialog: not counted
        self.assertEqual(self.broker._health()["pending_sends"]["approval_dialogs"], 1)
        self.release.set()
        worker.join(10)
        self.assertEqual(self.broker._health()["pending_sends"]["approval_dialogs"], 0)

    def test_a_dialog_that_fails_frees_the_next_one(self):
        self.release.set()
        self.answers = [RuntimeError("osascript"), True]
        draft = self.prepare("only")
        with self.assertRaises(RuntimeError):
            self.send(draft)
        self.assertEqual(self.send(draft).status, "sent")
        self.assertEqual(self.dialogs, ["only", "only"])

    def test_every_send_and_apply_opens_the_dialog_through_the_one_serialized_gate(self):
        source = inspect.getsource(broker_module.Broker)
        self.assertEqual(len(re.findall(r"approval_outcome\(self\._owner_dialog\(", source)), 5)
        # The one raw dialog call sits in _prompt_approval (counted as an open dialog for the installer's gate),
        # and only the serialized _owner_dialog calls that.
        self.assertEqual(re.findall(r"self\._approval_prompt\(", source), ["self._approval_prompt("])
        self.assertIn("self._approval_prompt(", inspect.getsource(broker_module.Broker._prompt_approval))
        self.assertEqual(len(re.findall(r"self\._prompt_approval\(", source)), 1)
        gate = inspect.getsource(broker_module.Broker._owner_dialog)
        self.assertIn("self._prompt_approval(", gate)
        # Critic fix (055f, high): the same gate takes the user's one cross-broker dialog lock, after the thread lock
        # and before the dialog, and it is taken nowhere else.
        self.assertEqual(re.findall(r"account_registry\.dialog_lock\(", source), ["account_registry.dialog_lock("])
        self.assertLess(gate.index("self._dialog_lock.acquire(blocking=False)"),
                        gate.index("account_registry.dialog_lock("))
        self.assertLess(gate.index("account_registry.dialog_lock("), gate.index("self._prompt_approval("))

    def test_without_a_registry_the_gate_reads_and_creates_nothing_in_the_accounts_folder(self):
        """0.54's bytes: no registry, so no lock file is made, and the dialog opens as before."""
        accounts = self.root / "home" / "TelegramSearchMCP-accounts"
        self.broker._accounts_root = accounts
        self.release.set()
        self.assertEqual(self.send(self.prepare("absent")).status, "sent")
        self.assertFalse(accounts.exists())
        accounts.mkdir(parents=True, mode=0o755)  # A folder without a registry is not judged (0.54).
        self.assertEqual(self.send(self.prepare("folder")).status, "sent")
        self.assertEqual(list(accounts.iterdir()), [])
        self.assertEqual(self.dialogs, ["absent", "folder"])


def _side(test, alias, home, prompt):
    """One real broker of `alias` under `home` (its own root, artifact store and fake sender), with an owner dialog
    that records which account asked."""
    account = AccountPaths.for_alias(alias, home)
    source = test.root / f"{alias}-source.txt"
    source.write_bytes(b"reviewed artifact")
    store = ArtifactStore(cache_dir=test.root / f"{alias}-artifacts")
    sender = FakeSender()
    user = 7 if alias == "default" else 8  # one Telegram user per account: each broker's pin admits its own
    sender.get_account_id = lambda: user
    broker = broker_module.Broker(socket_path=test.root / f"{alias}.sock", artifact_store=store,
                                  client_factory=lambda: sender, verify_peer_uid=False,
                                  approval_prompt=partial(prompt, alias), account=account)
    test.addCleanup(broker.shutdown)
    return SimpleNamespace(alias=alias, broker=broker, sender=sender, artifact=store.store(source))


class CrossBrokerDialogTests(unittest.TestCase):
    """Critic fix (055f, high): once an alias registry exists, one owner dialog at a time holds across every broker of
    the user (one process per alias in production), through a non-blocking flock on the owner-only
    `TelegramSearchMCP-accounts/dialog.lock`. While another broker's dialog is open a send answers dialog_unavailable
    exactly as the 0.54 in-broker rule: no dialog, no attempt, the draft pending, not counted as an open dialog."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        os.chmod(self.root, 0o700)
        home = self.root / "home"
        home.mkdir(mode=0o700)
        limit = patch.object(account_registry, "SOCKET_PATH_LIMIT", 4096)  # a long disposable home
        limit.start()
        self.addCleanup(limit.stop)
        account_registry.add_alias("work", home, longest_tool="t")
        self.accounts = accounts_root(home)
        self.lock = self.accounts / "dialog.lock"
        self.opened, self.release = threading.Event(), threading.Event()
        self.dialogs: list[tuple[str, str]] = []
        self.sides = {alias: _side(self, alias, home, self.prompt) for alias in ("default", "work")}

    def prompt(self, alias, **facts):
        """The first dialog stays open until the test releases it; later ones answer at once."""
        self.dialogs.append((alias, facts["caption"]))
        if len(self.dialogs) == 1:
            self.opened.set()
            if not self.release.wait(10):
                raise AssertionError("the open dialog was never released")
        return True

    def prepare(self, alias, caption):
        side = self.sides[alias]
        return side.broker._prepare_artifact_send(PrepareArtifactSendRequest(
            artifact_id=side.artifact.artifact_id, recipient=123, display_name="report.txt",
            mime_type="text/plain", caption=caption), client_id=CLIENT).draft_id

    def send(self, alias, draft_id):
        return self.sides[alias].broker._send_prepared_artifact(
            SendPreparedArtifactRequest(draft_id=draft_id, approved=True), client_id=CLIENT)

    def open_dialogs(self, alias):
        return self.sides[alias].broker._health()["pending_sends"]["approval_dialogs"]

    def test_a_second_brokers_send_while_another_brokers_dialog_is_open_is_dialog_unavailable(self):
        for first, second in (("work", "default"), ("default", "work")):
            with self.subTest(first=first):
                self.opened.clear(), self.release.clear(), self.dialogs.clear()
                held, waiting = self.prepare(first, "held"), self.prepare(second, "waiting")
                results = {}
                worker = threading.Thread(target=lambda: results.setdefault("held", self.send(first, held)))
                worker.start()
                self.assertTrue(self.opened.wait(10))
                refused = self.send(second, waiting)
                self.assertEqual((refused.status, refused.detail),
                                 ("dialog_unavailable", DETAILS["dialog_unavailable"]))
                self.assertEqual(self.dialogs, [(first, "held")])  # no second dialog was shown or queued
                self.assertEqual((self.open_dialogs(first), self.open_dialogs(second)), (1, 0))
                self.assertNotIn("waiting", [caption for _, _, caption in self.sides[second].sender.sent])
                self.release.set()
                worker.join(10)
                self.assertEqual(results["held"].status, "sent")
                # The refused draft stayed pending: once the other dialog closed, its own dialog opens.
                self.assertEqual(self.send(second, waiting).status, "sent")
                self.assertEqual(self.dialogs, [(first, "held"), (second, "waiting")])
                self.assertEqual((self.open_dialogs(first), self.open_dialogs(second)), (0, 0))
        metadata = self.lock.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual((stat.S_IMODE(metadata.st_mode), metadata.st_nlink, metadata.st_uid), (0o600, 1, os.getuid()))
        self.assertEqual(self.lock.read_bytes(), b"")

    def test_a_dialog_held_by_another_process_is_dialog_unavailable_until_it_ends(self):
        self.release.set()
        self.assertEqual(self.send("work", self.prepare("work", "first")).status, "sent")  # creates the lock
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c", "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()\n", str(self.lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline(), "held\n")
        waiting = self.prepare("default", "waiting")
        self.assertEqual(self.send("default", waiting).status, "dialog_unavailable")
        self.assertEqual(self.dialogs, [("work", "first")])
        holder.stdin.close()
        self.assertEqual(holder.wait(10), 0)
        self.assertEqual(self.send("default", waiting).status, "sent")
        self.assertEqual(self.dialogs, [("work", "first"), ("default", "waiting")])

    def test_a_dialog_that_fails_releases_the_lock_for_every_broker(self):
        self.release.set()
        failing = self.sides["work"].broker
        failing._approval_prompt = lambda **_: (_ for _ in ()).throw(RuntimeError("osascript"))
        with self.assertRaises(RuntimeError):
            self.send("work", self.prepare("work", "fails"))
        self.assertEqual(self.send("default", self.prepare("default", "next")).status, "sent")

    def test_an_unsafe_lock_or_accounts_folder_is_dialog_unavailable_and_left_as_it_is(self):
        self.release.set()

        def check(label, unsafe, restore):
            drafts = {alias: self.prepare(alias, label) for alias in ("default", "work")}
            unsafe()
            before = self.lock.lstat() if os.path.lexists(self.lock) else None
            for alias, draft in drafts.items():
                with self.subTest(case=label, alias=alias):
                    shown = len(self.dialogs)
                    self.assertEqual(self.send(alias, draft).status, "dialog_unavailable")
                    self.assertEqual(len(self.dialogs), shown)
                    self.assertEqual(self.open_dialogs(alias), 0)
            after = self.lock.lstat() if os.path.lexists(self.lock) else None
            self.assertEqual(None if before is None else (before.st_ino, before.st_mode, before.st_nlink),
                             None if after is None else (after.st_ino, after.st_mode, after.st_nlink))
            restore()
            for alias, draft in drafts.items():  # each draft stayed pending
                self.assertEqual(self.send(alias, draft).status, "sent")

        self.lock.write_bytes(b"")
        self.lock.chmod(0o600)
        for mode in (0o644, 0o400, 0o700):
            check(f"mode {mode:o}", lambda: self.lock.chmod(mode), lambda: self.lock.chmod(0o600))
        link = self.root / "second-link"
        check("second link", lambda: os.link(self.lock, link), link.unlink)
        target = self.root / "target"
        target.write_bytes(b"")
        target.chmod(0o600)
        check("symlink", lambda: (self.lock.unlink(), self.lock.symlink_to(target)),
              lambda: (self.lock.unlink(), self.lock.write_bytes(b""), self.lock.chmod(0o600)))
        check("directory", lambda: (self.lock.unlink(), self.lock.mkdir(mode=0o700)),
              lambda: (self.lock.rmdir(), self.lock.write_bytes(b""), self.lock.chmod(0o600)))

        # A folder that is not owner-only: the broker's own pin already refuses before any dialog, and the lock
        # itself refuses with nothing created in it.
        self.lock.unlink()
        self.accounts.chmod(0o755)
        with self.assertRaises(OwnerLeaseError) as caught:
            account_registry.dialog_lock(self.accounts)
        self.assertEqual(caught.exception.code, "owner_unsafe")
        self.assertFalse(os.path.lexists(self.lock))
        self.accounts.chmod(0o700)
        self.assertEqual(self.send("work", self.prepare("work", "safe again")).status, "sent")
        self.assertEqual(self.lock.lstat().st_mode & 0o777, 0o600)  # created again, owner-only
