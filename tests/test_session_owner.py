"""One stable lease protects both auth setup and the production broker."""
import os
import tempfile
import unittest
from pathlib import Path
from telegram_search_mcp.session_owner import SessionOwnerLease, OwnerLeaseError


class SessionOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / 'broker.lock'

    def tearDown(self):
        self.temp.cleanup()

    def test_competing_owner_is_preserved_and_inode_is_stable(self):
        with SessionOwnerLease(self.path) as first:
            identity = self.path.stat().st_ino
            with self.assertRaises(OwnerLeaseError) as caught:
                with SessionOwnerLease(self.path):
                    self.fail('contended lease admitted')
            self.assertEqual(caught.exception.code, 'owner_busy')
            first.check()
        with SessionOwnerLease(self.path):
            self.assertEqual(self.path.stat().st_ino, identity)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_symlink_and_nonprivate_existing_lock_are_refused_unchanged(self):
        target = self.root / 'foreign'
        target.write_bytes(b'foreign')
        self.path.symlink_to(target)
        with self.assertRaises(OwnerLeaseError):
            with SessionOwnerLease(self.path):
                pass
        self.assertEqual(target.read_bytes(), b'foreign')
        self.path.unlink()
        self.path.write_bytes(b'preserve')
        self.path.chmod(0o644)
        with self.assertRaises(OwnerLeaseError):
            with SessionOwnerLease(self.path):
                pass
        self.assertEqual(self.path.read_bytes(), b'preserve')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_substituted_visible_lock_is_detected(self):
        with SessionOwnerLease(self.path) as lease:
            self.path.rename(self.root / 'old')
            self.path.write_bytes(b'')
            self.path.chmod(0o600)
            with self.assertRaises(OwnerLeaseError):
                lease.check()

    def test_parent_symlink_and_hardlink_lock_are_refused(self):
        other = self.root / 'other'
        other.mkdir(mode=0o700)
        alias = self.root / 'alias'
        alias.symlink_to(other)
        with self.assertRaises(OwnerLeaseError):
            with SessionOwnerLease(alias / 'broker.lock'):
                pass
        self.path.touch(mode=0o600)
        os.link(self.path, self.root / 'link')
        with self.assertRaises(OwnerLeaseError):
            with SessionOwnerLease(self.path):
                pass
