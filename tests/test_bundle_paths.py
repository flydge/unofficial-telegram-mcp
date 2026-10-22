"""The owned-path ACL check under unrelated churn, and the uninstall sweep's answer when a check is not one.

An unrelated child created or removed in an ancestor (another app writing into ~/Library/Application Support, a
test suite in $TMPDIR) changes that directory's timestamps; it is not a security change and must neither refuse a
path nor make the installer's own retired root look foreign. Disposable directories only.
"""
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

from test_adopt_legacy import AdoptionHarness
from telegram_search_mcp import bundle_install, bundle_paths


class _ChurningApi:
    """The real libSystem ACL calls, with an unrelated child created and removed in `directory` during every ACL
    read of a descriptor bound to it (the moment the old retry loop sampled the timestamps)."""

    def __init__(self, real, directory, mutate=None):
        self.real, self.directory, self.mutate, self.reads = real, Path(directory), mutate, 0
        self.bound = os.stat(directory)

    def __getattr__(self, name):
        return getattr(self.real, name)

    def acl_get_fd_np(self, descriptor, kind):
        value = self.real.acl_get_fd_np(descriptor, kind)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (self.bound.st_dev, self.bound.st_ino):
            self.reads += 1
            if self.mutate is not None:
                self.mutate(self.directory)
            else:
                child = self.directory / ('.unrelated-%d' % self.reads)
                child.write_bytes(b'')
                child.unlink()
        return value


@unittest.skipUnless(sys.platform == 'darwin', 'the ACL check reads Darwin extended ACLs')
class ChurnTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.parent = self.base / 'parent'
        self.leaf = self.parent / 'leaf'
        self.leaf.mkdir(parents=True, mode=0o700)
        self.parent.chmod(0o700)

    def churning(self, directory, mutate=None):
        api = _ChurningApi(bundle_paths._darwin_acl_api(), directory, mutate)
        return api, mock.patch.object(bundle_paths, '_darwin_acl_api', lambda: api)

    def test_unrelated_children_churning_in_an_ancestor_never_refuse_a_path(self):
        api, patched = self.churning(self.parent)
        with patched:
            for _ in range(5):
                with bundle_paths.open_owned_directory(self.leaf, private=True):
                    pass
                self.assertEqual(bundle_paths.object_binding(self.leaf, directory=True)['kind'], 'directory')
                bundle_paths.validate_owned_path(self.leaf, private=True)
        self.assertGreater(api.reads, 10)

    def test_a_security_change_during_the_read_still_refuses(self):
        def chmod(directory):
            directory.chmod(0o750 if (directory.stat().st_mode & 0o777) == 0o700 else 0o700)
        _, patched = self.churning(self.parent, chmod)
        with patched, self.assertRaises(bundle_paths.RuntimePathError):
            bundle_paths.object_binding(self.leaf, directory=True)

    def test_a_regular_file_changing_during_the_read_still_refuses(self):
        path = self.leaf / 'data'
        path.write_bytes(b'x')
        path.chmod(0o600)
        real = bundle_paths._darwin_acl_api()

        class Growing:
            def __getattr__(self, name):
                return getattr(real, name)

            def acl_get_fd_np(self, descriptor, kind):
                value = real.acl_get_fd_np(descriptor, kind)
                if os.fstat(descriptor).st_ino == path.stat().st_ino:
                    with open(path, 'ab') as stream:
                        stream.write(b'y')
                return value
        with mock.patch.object(bundle_paths, '_darwin_acl_api', lambda: Growing()):
            with self.assertRaises(bundle_paths.RuntimePathError):
                bundle_paths.read_owned_file(path, max_bytes=1024)


class RetiredRootTests(AdoptionHarness, unittest.TestCase):
    """A check that changed under it is no answer: the closing sweep never takes the retired root for a lookalike."""

    def test_a_changing_binding_check_keeps_the_proof_for_the_retry(self):
        self.full()
        real = bundle_install.require_object_binding
        failed = []

        def flaky(path, binding):
            if re.search(r'/\.runtime-removed-[0-9a-f]{32}\Z', str(path)) and not failed:
                failed.append(path)
                raise bundle_paths.ChangedRuntimePath('portable storage security metadata changed')
            return real(path, binding)
        with mock.patch.object(bundle_install, 'require_object_binding', flaky):
            with self.assertRaises(bundle_install.InstallError) as caught:
                self.change('uninstall')
        self.assertEqual(str(caught.exception), 'installation_refused')
        self.assertEqual(len(failed), 1)
        left = sorted(re.sub('[0-9a-f]{32}', '*', p.name) for p in self.home.iterdir() if p.name.startswith('.runtime'))
        self.assertEqual(left, ['.runtime-removed-*', '.runtime.install-intent.json'])
        self.assertEqual(self.change('uninstall'), {'status': 'uninstalled', 'installation': 'absent'})
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith('.runtime')], [])


if __name__ == '__main__':
    unittest.main()
