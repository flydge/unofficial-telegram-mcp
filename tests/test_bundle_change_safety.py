"""Change-transaction safety (CLIENTS-13 review), over disposable homes only.

A refused change leaves no transaction behind; an older kit goes back only through rollback; the
pending-send gate precedes every client write; a real kill (a forked child that hard-exits, no cleanup)
between a temporary's creation and its journaled inode leaves nothing behind after the resume;
adoption records outlive a rollback to an older receipt; an uninstall killed in a write-ahead window
keeps its records until the owned entry is gone. launchctl, the claude CLI and the pending-send probe
are fakes; no test reads or writes the real ~/Library or a real client configuration.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from test_adopt_legacy import AdoptionHarness
from test_bundle_upgrade import ChangeHarness, NEIGHBOUR_CODEX, OLD_VERSION
from telegram_search_mcp import claude_desktop_registration as desktop, codex_registration

TEMPORARY = '.telegram-search-'
FOREIGN_CODEX = NEIGHBOUR_CODEX + b'\n[mcp_servers.telegram_search]\ncommand = "/legacy/051"\n'


class SafetyHarness(ChangeHarness):

    def temporaries(self):
        """Every temporary left anywhere under the disposable base: homes, session, roots and transactions."""
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob(TEMPORARY + '*'))

    def changes(self):
        return sorted(p.name for p in (self.runtime / 'transactions').iterdir() if p.name.startswith('change_'))

    def client_files(self):
        """Every object a change may write, by path: its bytes, or None when absent."""
        paths = [self.codex / 'config.toml', self.desktop_config, self.claude().path, self.plist,
                 self.session_root / 'runtime.toml', self.session_root / 'enrollment.json',
                 self.runtime / 'receipt.json', self.runtime / 'current.json']
        paths += [base / name / 'SKILL.md' for base in (self.skills, self.claude_skills)
                  for name in ('telegram-search', 'telegram-setup')]
        return {str(p): p.read_bytes() if p.exists() else None for p in paths}

    def die(self, action, *, at=None, after=None, skip=0, directory=None):
        """Run `action(checkpoint)` in a forked child that hard-exits with 9 (os._exit: no cleanup runs).

        `at`: at that checkpoint. `directory`: right after our temporary there is created (its first
        syscall after O_CREAT, before its inode can be journaled). `after`: at the (skip+1)-th of our
        temporaries created anywhere once that checkpoint passed (journal updates included).
        """
        pid = os.fork()
        if pid == 0:
            try:
                armed, seen, real = [False], [], os.fchmod
                def fchmod(fd, mode):
                    if directory is not None or armed[0]:
                        path = Path(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0').decode())
                        if path.name.startswith(TEMPORARY) and (directory is None or path.parent == Path(directory)):
                            seen.append(path)
                            if len(seen) > skip: os._exit(9)
                    return real(fd, mode)
                def checkpoint(value):
                    if value == at: os._exit(9)
                    if value == after: armed[0] = True
                with mock.patch.object(codex_registration.os, 'fchmod', side_effect=fchmod):
                    action(checkpoint)
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)


class RefusedChangeTests(SafetyHarness, unittest.TestCase):
    """Every refusal, the pending-send gate included, comes before the transaction exists."""

    def test_a_refused_uninstall_leaves_no_transaction_and_a_later_upgrade_succeeds(self):
        for cause in ('pending', 'codex'):
            with self.subTest(cause=cause):
                self.setUp()
                self.full()
                if cause == 'pending':
                    self.answers = ['pending']
                else:
                    self.codex_present(b'[mcp_servers\n')  # A config no removal can be planned over.
                before = self.client_files()
                with self.assertRaises(self.module().InstallError) as caught:
                    self.change('uninstall')
                self.assertEqual(str(caught.exception),
                                 'pending_send_unresolved' if cause == 'pending' else 'installation_refused')
                self.assertEqual(self.changes(), [])
                self.assertEqual(self.client_files(), before)
                self.assertNotIn('bootout', self.launchctl.verbs())
                if cause == 'codex':
                    self.codex_present()
                result = self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual(result['status'], 'upgraded')
                self.assertEqual(self.changes(), ['change_1'])
                self.selection_check()

    def test_a_refused_rollback_or_adoption_leaves_no_transaction(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.install(new)
        self.answers = ['pending']
        with self.assertRaises(self.module().InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual(self.changes(), ['change_1'])
        self.assertEqual(self.change('rollback')['status'], 'rolled_back')
        # A foreign Codex entry adopted on the next upgrade: a pending send refuses before change_3 exists.
        self.codex_present(FOREIGN_CODEX)
        self.answers = ['pending']
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(new, adopt_legacy=True)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual(self.changes(), ['change_1', 'change_2'])
        self.assertFalse((self.runtime / 'adopted.json').exists())
        self.assertEqual(self.install(new, adopt_legacy=True)['adopted'], ['codex'])


class DowngradeTests(SafetyHarness, unittest.TestCase):

    def test_installing_an_older_kit_is_refused_and_only_rollback_goes_back(self):
        old_packet, _ = self.full()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        self.install(new)
        new_root = str(self.runtime / 'releases' / new[3])
        # The newer release wrote session state the older one would refuse.
        (self.session_root / 'enrollment.json').write_bytes(b'{"written": "by the newer release"}\n')
        with self.assertRaises(self.module().InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'rollback_refused')
        before, probes, verbs = self.client_files(), len(self.probes), len(self.launchctl.verbs())
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            with self.assertRaises(self.module().InstallError) as caught:
                self.install(old_packet)
        self.assertEqual(str(caught.exception), 'downgrade_refused')
        self.assertEqual(self.client_files(), before)
        self.assertEqual(self.changes(), ['change_1'])
        self.assertEqual(self.pointer()['release_root'], new_root)
        self.assertEqual((len(self.probes), len(self.launchctl.verbs())), (probes, verbs))  # Refused before any probe.
        self.selection_check()

    def test_the_version_order_is_pep440_and_anything_else_refuses(self):
        key = self.module()._version_key
        ordered = ['0.52.9', '0.53.0.dev0', '0.53.0.dev1', '0.53.0a1', '0.53.0b2', '0.53.0rc1', '0.53.0',
                   '0.53.0.post1.dev0', '0.53.0.post1', '0.53.1', '1.0']
        self.assertEqual(sorted(reversed(ordered), key=key), ordered)
        self.assertEqual(key('0.53'), key('0.53.0'))
        for value in ('', 'v0.53.0', '0.53.0+local', '01.2', '0.53.0-1', '0.53..0', None, 53, '0.53.0.dev'):
            with self.subTest(value=value):
                with self.assertRaises(self.module().InstallError) as caught:
                    key(value)
                self.assertEqual(str(caught.exception), 'downgrade_refused')


class PendingGateTests(SafetyHarness, unittest.TestCase):

    def test_a_late_pending_send_refuses_before_any_client_write(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        before = self.client_files()
        calls = len(self.claude().calls)
        self.answers = ['none', 'pending']  # Clear before the transaction, pending at its first gate.
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(new)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual(self.client_files(), before)
        self.assertEqual(self.claude().calls[calls:], [])
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertEqual(self.install(new)['status'], 'upgraded')
        self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
        self.selection_check()

    def test_the_stop_is_gated_again_and_the_transaction_resumes(self):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.answers = ['none', 'none', 'pending']
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(new)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertEqual(self.pointer()['release_root'], old['release_root'])
        self.assertEqual(self.install(new)['service'], 'restarted')
        self.assertEqual(self.launchctl.verbs().count('bootout'), 1)


class PendingActionTests(SafetyHarness, unittest.TestCase):
    """An unfinished change blocks every other action until that same action is retried; the refusal names it."""

    def assertPending(self, action, pending):
        with self.assertRaises(self.module().InstallError) as caught:
            action()
        self.assertEqual((str(caught.exception), caught.exception.pending_action), ('transaction_pending', pending))

    def test_each_unfinished_change_is_named_until_it_is_retried(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.answers = ['none', 'pending']  # The upgrade journal exists; its first gate refuses.
        with self.assertRaises(self.module().InstallError):
            self.install(new)
        for action in (lambda: self.change('rollback'), lambda: self.change('uninstall'),
                       lambda: self.install(self.build('c', launcher=b'# third release\n'))):
            self.assertPending(action, 'upgrade')
        self.assertEqual(self.install(new)['status'], 'upgraded')
        self.answers = ['none', 'pending']
        with self.assertRaises(self.module().InstallError):
            self.change('rollback')
        for action in (lambda: self.change('uninstall'), lambda: self.install(self.build('d', launcher=b'# fourth release\n'))):
            self.assertPending(action, 'rollback')
        self.assertEqual(self.change('rollback')['status'], 'rolled_back')
        self.launchctl.fail = {'bootout'}
        with self.assertRaises(self.module().InstallError):
            self.change('uninstall')
        self.launchctl.fail = set()
        for action in (lambda: self.change('rollback'), lambda: self.install(new)):
            self.assertPending(action, 'uninstall')
        self.assertEqual(self.change('uninstall')['status'], 'uninstalled')

    def test_only_a_fixed_pending_action_is_carried(self):
        error = self.module().InstallError
        self.assertEqual(error('transaction_pending', pending_action='uninstall').pending_action, 'uninstall')
        for value in (error('transaction_pending', pending_action='/Users/example/secret'), error('transaction_pending'),
                      error('recovery_limit', pending_action='upgrade'), error('transaction_pending', 'x', pending_action='upgrade')):
            self.assertIsNone(value.pending_action)


class KilledTemporaryTests(SafetyHarness, unittest.TestCase):
    """A real kill right after one of our temporaries is created: the resume leaves no temporary anywhere."""

    def upgrade_killed(self, **kill):
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.claude()
        self.die(lambda checkpoint: self.install(new, checkpoint=checkpoint), **kill)
        result = self.install(new)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(result['claude_desktop'], 'registered')  # An unchanged file is written once now.
        self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])
        self.assertEqual(self.temporaries(), [])
        self.selection_check()

    def test_a_kill_at_the_creation_of_each_external_temporary(self):
        targets = {'claude_desktop': lambda: desktop.directory(self.home), 'codex': lambda: self.codex,
                   'policy': lambda: self.session_root, 'plist': lambda: self.plist.parent}
        for name, directory in targets.items():
            with self.subTest(name=name):
                self.setUp()
                self.upgrade_killed(directory=directory())

    def test_a_kill_at_every_temporary_between_the_record_and_the_write(self):
        # The review repros: after each checkpoint come the journal's temporary, the external file's, then the
        # journal's again (Claude Desktop; runtime.toml after the service step; Codex after the release).
        for stage, count in (('recorded:claude_desktop_add', 3), ('service', 4), ('release', 5)):
            for skip in range(count):
                with self.subTest(stage=stage, skip=skip):
                    self.setUp()
                    self.upgrade_killed(after=stage, skip=skip)

    def test_a_first_install_killed_at_its_codex_or_desktop_temporary(self):
        for name in ('codex', 'claude_desktop'):
            with self.subTest(name=name):
                self.setUp()
                self.codex_present()
                self.desktop_present()
                self.claude()
                packet = self.build('a')
                directory = self.codex if name == 'codex' else desktop.directory(self.home)
                self.die(lambda checkpoint: self.install(packet, checkpoint=checkpoint), directory=directory)
                result = self.install(packet)
                self.assertEqual((result['status'], result['claude_desktop']), ('activation_required', 'registered'))
                self.assertIn(b'[mcp_servers.telegram_search]', (self.codex / 'config.toml').read_bytes())
                self.assertEqual(self.temporaries(), [])
                self.selection_check()


class AdoptionRecordTests(SafetyHarness, unittest.TestCase):

    def adopted_on_upgrade(self):
        self.full()
        self.as_v1()
        self.codex_present(FOREIGN_CODEX)
        result = self.install(self.build('b', launcher=b'# second release\n'), adopt_legacy=True)
        self.assertEqual(result['adopted'], ['codex'])
        return (self.runtime / 'transactions/change_1/adopted-codex').read_bytes()

    def test_adoption_backups_survive_a_rollback_to_a_v1_receipt_and_uninstall_keeps_them(self):
        data = self.adopted_on_upgrade()
        sidecar = self.runtime / 'adopted.json'
        self.assertEqual(json.loads(sidecar.read_text()), {'schema_version': 1, 'backups': [
            {'client': 'codex', 'transaction': 'change_1', 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}]})
        self.assertEqual(oct(sidecar.stat().st_mode & 0o777), '0o600')
        recorded = sidecar.read_bytes()
        self.change('rollback')
        self.assertEqual((self.receipt()['schema_version'], 'adopted' in self.receipt()), (1, False))
        self.assertEqual(sidecar.read_bytes(), recorded)  # No receipt schema touches it.
        removed = self.change('uninstall')
        kept = Path(removed['legacy_backups'])
        self.assertEqual((kept / 'change_1-codex').read_bytes(), data)
        self.assertEqual(oct(kept.stat().st_mode & 0o777), '0o700')
        self.assertFalse(self.runtime.exists())

    def test_the_sidecar_shape_is_closed_and_a_bad_one_refuses_before_any_transaction(self):
        from telegram_search_mcp.bundle_runtime import BundleError, check_adopted
        good = {'client': 'codex', 'transaction': 'change_1', 'sha256': 'a' * 64, 'bytes': 1}
        self.assertEqual(check_adopted({'schema_version': 1, 'backups': [good]}), [good])
        for value in ({'schema_version': 1, 'backups': []}, {'schema_version': 2, 'backups': [good]},
                      {'schema_version': 1, 'backups': [good], 'extra': 1}, {'schema_version': 1, 'backups': [good, good]},
                      {'schema_version': 1, 'backups': [{**good, 'client': 'other'}]},
                      {'schema_version': 1, 'backups': [{**good, 'transaction': 'change_0'}]},
                      {'schema_version': 1, 'backups': [{**good, 'bytes': 0}]},
                      {'schema_version': 1, 'backups': [{**good, 'path': '/x'}]}, [good]):
            with self.subTest(value=value):
                with self.assertRaises(BundleError):
                    check_adopted(value)
        self.adopted_on_upgrade()
        (self.runtime / 'adopted.json').write_text('{"schema_version": 1, "backups": []}')
        with self.assertRaises(self.module().InstallError) as caught:
            self.change('uninstall')
        self.assertEqual(str(caught.exception), 'metadata_conflict')
        self.assertEqual(self.changes(), ['change_1'])


class LegacyAdoptionSafetyTests(AdoptionHarness, SafetyHarness, unittest.TestCase):

    def test_adopting_a_legacy_broker_gates_before_any_client_write(self):
        self.legacy()
        before = {p: p.read_bytes() for p in (self.codex / 'config.toml', self.desktop_config, self.claude().path, self.plist)}
        self.answers = ['none', 'pending']
        packet = self.build('a')
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(packet, adopt_legacy=True)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.claude().calls, [])
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertEqual(self.install(packet, adopt_legacy=True)['service'], 'restarted')

    def test_a_first_install_adoption_is_listed_in_the_sidecar(self):
        backups = self.legacy()
        packet = self.build('a')
        self.install(packet, adopt_legacy=True)
        tx = 'install_' + packet[3][:24]
        self.assertEqual(json.loads((self.runtime / 'adopted.json').read_text())['backups'],
                         [{'client': c, 'transaction': tx, 'sha256': hashlib.sha256(d).hexdigest(), 'bytes': len(d)}
                          for c, d in sorted(backups.items())])
        self.assertEqual(self.install(packet)['installation'], 'reused')
        removed = self.change('uninstall')
        self.assertEqual(sorted(p.name for p in Path(removed['legacy_backups']).iterdir()),
                         sorted(tx + '-' + c for c in backups))


class KilledUninstallTests(SafetyHarness, unittest.TestCase):
    """An uninstall killed in a write-ahead window keeps the receipt and root until the owned entry is gone."""

    def installed(self):
        self.full()
        self.claude()

    def assertGone(self, result):
        self.assertEqual((result['installation'], result['claude_code'], result['claude_desktop'], result['codex']),
                         ('removed', 'removed', 'removed', 'removed'))
        self.assertFalse(self.runtime.exists())
        self.assertNotIn('telegram_search', self.claude().document()['mcpServers'])
        self.assertNotIn('telegram_search', json.loads(self.desktop_config.read_text())['mcpServers'])
        self.assertEqual((self.codex / 'config.toml').read_bytes().rstrip(b'\n'), NEIGHBOUR_CODEX.rstrip(b'\n'))
        self.assertEqual(self.temporaries(), [])

    def test_each_killed_window_is_finished_by_the_retry(self):
        kills = [{'at': 'recorded:claude_remove'}, {'at': 'recorded:claude_desktop_add'},
                 {'at': 'recorded:claude_desktop_write'}, {'directory': 'desktop'}, {'directory': 'codex'}]
        for kill in kills:
            with self.subTest(**kill):
                self.setUp()
                self.installed()
                if 'directory' in kill:
                    kill = {'directory': desktop.directory(self.home) if kill['directory'] == 'desktop' else self.codex}
                self.die(lambda checkpoint: self.change('uninstall', checkpoint=checkpoint), **kill)
                self.assertTrue((self.runtime / 'receipt.json').is_file())
                self.assertGone(self.change('uninstall'))

    def test_an_unconfirmed_removal_keeps_every_record_until_a_later_retry(self):
        for window in ('recorded:claude_remove', 'recorded:claude_desktop_add'):
            with self.subTest(window=window):
                self.setUp()
                self.installed()
                self.die(lambda checkpoint: self.change('uninstall', checkpoint=checkpoint), at=window)
                good = self.desktop_config.read_text()
                if window == 'recorded:claude_remove':
                    self.claude().fail = {'remove'}  # The CLI cannot remove our still exact entry now.
                else:
                    self.desktop_present('{')  # Unreadable: our entry might still be there.
                with self.assertRaises(self.module().InstallError) as caught:
                    self.change('uninstall')
                self.assertEqual(str(caught.exception), 'uninstall_incomplete')
                self.assertTrue((self.runtime / 'receipt.json').is_file() and (self.runtime / 'current.json').is_file())
                journal = json.loads((self.runtime / 'transactions' / self.changes()[-1] / 'journal.json').read_text())
                self.assertEqual(journal['kind'], 'uninstall')
                self.assertNotIn('claude_desktop' if window != 'recorded:claude_remove' else 'claude_code',
                                 journal['publications'])
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual(str(caught.exception), 'transaction_pending')
                self.claude().fail = set()
                self.desktop_present(good)
                self.assertGone(self.change('uninstall'))


if __name__ == '__main__':
    unittest.main()
