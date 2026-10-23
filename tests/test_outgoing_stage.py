from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.outgoing_stage import retire_staged_document, stage_approved_document


OWNER = DraftOwner(client_id="client_" + "a" * 24, account_id=7)

class OutgoingStageTests(unittest.TestCase):
    def test_large_finished_copy_moves_to_private_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source"
            source.write_bytes(b"12345")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            registry = OutgoingDraftRegistry(store)
            draft = registry.prepare(owner=OWNER,
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="large.pdf", mime_type="application/pdf", caption="",
            )
            with patch("telegram_search_mcp.outgoing_stage.LARGE_FILE_THRESHOLD", 4):
                staged = stage_approved_document(registry.claim(draft.draft_id, owner=OWNER, approved=True),
                                                 root=root / "outgoing")
                retire_staged_document(staged, root=root / "outgoing")
            self.assertFalse(staged.exists())
            quarantined = list((root / "outgoing-quarantine").iterdir())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].stat().st_size, 5)

    def test_staged_copy_has_reviewed_name_and_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source"
            source.write_bytes(b"local generated content")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            registry = OutgoingDraftRegistry(store)
            draft = registry.prepare(owner=OWNER,
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="report.txt", mime_type="text/plain", caption="caption",
            )
            claim = registry.claim(draft.draft_id, owner=OWNER, approved=True)
            staged = stage_approved_document(claim, root=root / "outgoing")
            self.assertEqual(staged.name, "report.txt")
            self.assertEqual(staged.read_bytes(), b"local generated content")
            self.assertNotEqual(staged, artifact.path)
            retire_staged_document(staged, root=root / "outgoing")
            self.assertFalse(staged.exists())



class RetirementRaceTests(unittest.TestCase):
    """Retirement and the staging sweep are serialized: a retirement paused between removing a copy and its
    directory never makes a concurrent approved send fail locally, for a single send and an album alike."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "outgoing"
        self.store = ArtifactStore(cache_dir=self.root / "artifacts")
        self.registry = OutgoingDraftRegistry(self.store)
        self.counter = 0

    def claim(self, data: bytes = b"document"):
        self.counter += 1
        source = self.root / f"source-{self.counter}"
        source.write_bytes(data + bytes([self.counter]))
        artifact = self.store.store(source)
        draft = self.registry.prepare(owner=OWNER, artifact_id=artifact.artifact_id, recipient=123,
                                      display_name=f"file-{self.counter}.pdf", mime_type="application/pdf", caption="")
        return self.registry.claim(draft.draft_id, owner=OWNER, approved=True)

    def staged_album(self):
        from test_album_send import MOVIE, PNG
        from telegram_search_mcp.album_drafts import AlbumSpec, album_item
        from telegram_search_mcp.outgoing_stage import stage_approved_album
        items = []
        for data, name in ((PNG, "a.png"), (MOVIE, None)):
            self.counter += 1
            source = self.root / f"source-{self.counter}"
            source.write_bytes(data)
            items.append(album_item(group="media", artifact=self.store.store(source, kind="media"),
                                    recorded_name=name, recorded_mime=None, caption=""))
        album = AlbumSpec(group="media", items=tuple(items), recipient_identity=("chatTypePrivate", 123))
        draft = self.registry.prepare_album(owner=OWNER, recipient=123, recipient_title="Recipient", album=album)
        return stage_approved_album(self.registry.claim(draft.draft_id, owner=OWNER, approved=True), root=self.stage)

    def race(self, retire) -> None:
        """Pause `retire` right after it removed its first copy, stage another approved send meanwhile."""
        import threading
        paused, release, outcome = threading.Event(), threading.Event(), {}
        unlink = Path.unlink

        def pausing_unlink(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if threading.current_thread() is retirer and not paused.is_set():
                paused.set()
                self.assertTrue(release.wait(10))

        def stage() -> None:
            try:
                outcome["staged"] = stage_approved_document(self.claim(b"concurrent"), root=self.stage)
            except Exception as error:  # Recorded: a local failure after approval is the defect.
                outcome["error"] = error

        retirer = threading.Thread(target=retire)
        stager = threading.Thread(target=stage)
        with patch.object(Path, "unlink", pausing_unlink):
            retirer.start()
            self.assertTrue(paused.wait(10))
            stager.start()
            stager.join(0.3)
            blocked = stager.is_alive()
            release.set()
            retirer.join(10)
            stager.join(10)
        self.assertFalse(retirer.is_alive() or stager.is_alive())
        self.assertNotIn("error", outcome)
        self.assertTrue(blocked)  # It waited for the retirement instead of sweeping a half-retired directory.
        self.assertEqual(outcome["staged"].read_bytes(), b"concurrent" + bytes([self.counter]))

    def test_single_send_retirement_never_fails_a_concurrent_staging(self) -> None:
        staged = stage_approved_document(self.claim(), root=self.stage)
        self.race(lambda: retire_staged_document(staged, root=self.stage))
        self.assertFalse(staged.parent.exists())

    def test_album_retirement_never_fails_a_concurrent_staging(self) -> None:
        from telegram_search_mcp.outgoing_stage import retire_staged_album
        paths = self.staged_album()
        self.race(lambda: retire_staged_album(paths, root=self.stage))
        self.assertFalse(any(path.parent.exists() for path in paths))

    def test_the_sweep_skips_entries_that_vanish_while_it_reads_them(self) -> None:
        """Another process may retire an entry between the listing and the read; genuine refusals stand."""
        import shutil
        lstat, iterdir = Path.lstat, Path.iterdir
        cases = ("directory lstat", "directory listing", "file lstat", "quarantine lstat")
        for case in cases:
            with self.subTest(case=case):
                shutil.rmtree(self.stage, ignore_errors=True)
                shutil.rmtree(self.stage.with_name("outgoing-quarantine"), ignore_errors=True)
                with patch("telegram_search_mcp.outgoing_stage.LARGE_FILE_THRESHOLD", 4):
                    gone = stage_approved_document(self.claim(), root=self.stage)
                    if case == "quarantine lstat":
                        retire_staged_document(gone, root=self.stage)
                        [gone] = list(self.stage.with_name("outgoing-quarantine").iterdir())
                target = {"directory lstat": gone.parent, "directory listing": gone.parent,
                          "file lstat": gone, "quarantine lstat": gone}[case]
                fired = []

                def vanish(path) -> None:
                    if path == target and not fired:
                        fired.append(path)
                        if case == "quarantine lstat":
                            gone.unlink()
                        else:
                            shutil.rmtree(gone.parent)

                def vanishing_lstat(path, *args, **kwargs):
                    if case != "directory listing":
                        vanish(path)
                    return lstat(path, *args, **kwargs)

                def vanishing_iterdir(path):
                    if case == "directory listing":
                        vanish(path)
                    return iterdir(path)

                with patch.object(Path, "lstat", vanishing_lstat), patch.object(Path, "iterdir", vanishing_iterdir):
                    staged = stage_approved_document(self.claim(b"next"), root=self.stage)
                self.assertEqual(fired, [target])
                self.assertEqual(list(self.stage.iterdir()), [staged.parent])
        # Genuinely unsafe contents are still refused.
        (staged.parent / "second").write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "unsafe outgoing staging contents"):
            stage_approved_document(self.claim(b"after"), root=self.stage)
        (staged.parent / "second").unlink()
        staged.unlink()  # A fresh empty directory is still not a retired one.
        with self.assertRaisesRegex(ValueError, "unsafe outgoing staging contents"):
            stage_approved_document(self.claim(b"after"), root=self.stage)
        staged.parent.rmdir()
        (self.stage / "not-a-draft").mkdir()
        with self.assertRaisesRegex(ValueError, "unsafe outgoing staging entry"):
            stage_approved_document(self.claim(b"after"), root=self.stage)


if __name__ == "__main__":
    unittest.main()
