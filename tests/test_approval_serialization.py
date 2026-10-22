"""One owner dialog at a time (0.54 safety finding): while one approval dialog is open, another send or apply
answers dialog_unavailable at once and its draft stays pending, so a click on the visible dialog can never
approve a different draft. Nothing is queued behind the open dialog."""
from __future__ import annotations

import inspect
import re
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import telegram_search_mcp.broker as broker_module
from telegram_search_mcp.approval_prompt import DETAILS
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.schemas import PrepareArtifactSendRequest, SendPreparedArtifactRequest

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
        self.assertIn("self._prompt_approval(", inspect.getsource(broker_module.Broker._owner_dialog))


if __name__ == "__main__":
    unittest.main()
