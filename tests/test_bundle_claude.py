"""Claude Code registration inside the install transaction, against a behavioural fake CLI only.

No test here runs the real `claude`, reads any real client configuration or uses a real home.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest import mock

from claude_fake import FakeClaude, entry
from test_bundle_install import InstallHarness
from telegram_search_mcp import claude_registration


class ClaudeInstallTests(InstallHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.packet = self.fixture()
        self.bundle = self.packet[0]

    def expected(self):
        release = self.runtime / 'releases' / self.packet[3]
        return str(release / 'python/bin/python3'), ['-I', '-S', '-B', str(release / 'launcher.py'), 'mcp']

    def journal(self):
        return json.loads((self.runtime / 'transactions' / ('install_' + self.packet[3][:24]) / 'journal.json').read_text())

    def receipt(self):
        return json.loads((self.runtime / 'receipt.json').read_text())

    def assertNothingCreated(self):
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.codex / 'config.toml').exists())
        self.assertFalse(self.skills.exists())
        self.assertFalse((self.home / '.claude').exists())

    # --- the three outcomes ---------------------------------------------------------------

    def test_absent_entry_is_added_once_in_user_scope_then_confirmed(self):
        fake = self.claude()
        result = self.install(self.packet)
        command, args = self.expected()
        self.assertEqual(result['claude_code'], 'registered')
        self.assertEqual(fake.entry, entry(command, args))
        adds = [c for c in fake.calls if c[1] == 'add-json']
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0][2:5], ['--scope', 'user', 'telegram_search'])
        self.assertEqual(json.loads(adds[0][5]), {'type': 'stdio', 'command': command, 'args': args})
        self.assertEqual(adds[0][5], json.dumps({'type': 'stdio', 'command': command, 'args': args}, separators=(',', ':')))
        verbs = fake.verbs()
        self.assertEqual(verbs[verbs.index('add-json') + 1], 'get')  # confirmed after the add
        self.assertNotIn('remove', verbs)
        self.assertEqual(self.receipt()['claude_code'], 'registered')
        self.assertEqual(self.journal()['publications']['claude_code'], [{'status': 'registered'}])

    def test_every_cli_call_is_an_argv_list_with_bounded_time_private_cwd_and_no_stdin(self):
        fake = self.claude()
        self.install(self.packet)
        self.assertTrue(fake.calls)
        for argv, kwargs in zip(fake.calls, fake.kwargs):
            self.assertEqual(argv[0], 'mcp')
            self.assertTrue(0 < kwargs['timeout'] <= 60)
            self.assertEqual(kwargs['cwd'], str(self.home))
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['capture_output'])
            self.assertNotIn('shell', kwargs)
            self.assertNotIn('env', kwargs)

    def test_exact_existing_entry_is_reused_and_never_touched(self):
        fake = self.claude()
        command, args = self.expected()
        fake.entry = entry(command, args)
        result = self.install(self.packet)
        self.assertEqual(result['claude_code'], 'reused')
        self.assertEqual(fake.entry, entry(command, args))
        self.assertTrue(set(fake.verbs()) <= {'get'})
        self.assertEqual(self.receipt()['claude_code'], 'reused')
        self.assertNotIn('claude_add', self.journal()['publications'])

    def test_without_a_cli_the_install_succeeds_recording_not_found_and_calls_nothing(self):
        fake = self.claude()
        result = self.install(self.packet, claude_candidates=(self.base / 'missing/claude',))
        self.assertEqual(result['claude_code'], 'not_found')
        self.assertEqual(result['status'], 'activation_required')
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.receipt()['claude_code'], 'not_found')
        self.assertTrue((self.claude_skills / 'telegram-setup' / 'SKILL.md').is_file())  # skills are still copied

    def test_repeat_install_reuses_the_recorded_outcome_without_consulting_the_cli(self):
        fake = self.claude()
        self.assertEqual(self.install(self.packet)['claude_code'], 'registered')
        before = list(fake.calls)
        fake.fail.update({'get', 'add-json', 'remove'})  # The CLI would now refuse everything.
        again = self.install(self.packet)
        self.assertEqual(again['installation'], 'reused')
        self.assertEqual(again['claude_code'], 'registered')
        self.assertEqual(fake.calls, before)

    # --- foreign entries -----------------------------------------------------------------

    def foreign_entries(self):
        command, args = self.expected()
        return {
            'other command': entry('/foreign/python3', args),
            'other args': entry(command, args + ['--extra']),
            'fewer args': entry(command, args[:-1]),
            'http server': entry(None, [], type='http', url='https://example.invalid/mcp'),
            'extra environment': entry(command, args, env={'EXAMPLE': 'value'}),
            'same command in local scope': entry(command, args, scope='local'),
            'same command in project scope': entry(command, args, scope='project'),
        }

    def test_foreign_entry_refuses_before_anything_is_created_and_is_never_modified(self):
        for label, foreign in self.foreign_entries().items():
            with self.subTest(label):
                self.setUp()
                fake = self.claude()
                fake.entry = foreign
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'claude_registration_conflict')
                self.assertEqual(fake.entry, foreign)
                self.assertTrue(set(fake.verbs()) <= {'get'})
                self.assertNothingCreated()
                for text in ('/foreign', 'example.invalid', 'EXAMPLE'):  # nothing from the entry is echoed
                    self.assertNotIn(text, repr(caught.exception.args) + str(caught.exception))

    def test_foreign_entry_appearing_mid_transaction_refuses_without_publishing_or_removing(self):
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        def appear(stage):
            if stage == 'claude-telegram-setup': fake.entry = foreign
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet, checkpoint=appear)
        self.assertEqual(str(caught.exception), 'claude_registration_conflict')
        self.assertEqual(fake.entry, foreign)
        self.assertNotIn('remove', fake.verbs())
        self.assertNotIn('add-json', fake.verbs())
        self.assertFalse((self.runtime / 'current.json').exists())
        with self.assertRaises(self.bundle.InstallError):  # still refused while the foreign entry stays
            self.install(self.packet)
        fake.entry = None  # The owner removes it themselves; the retained transaction then completes.
        self.assertEqual(self.install(self.packet)['claude_code'], 'registered')

    def test_entry_replaced_by_a_foreign_one_during_our_add_is_never_removed(self):
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        fake.mutate_after_add = lambda f: setattr(f, 'entry', foreign)
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet)
        self.assertEqual(str(caught.exception), 'claude_registration_conflict')
        self.assertEqual(fake.entry, foreign)
        self.assertNotIn('remove', fake.verbs())
        self.assertFalse((self.runtime / 'current.json').exists())

    # --- CLI failures --------------------------------------------------------------------

    def test_failed_lost_or_timed_out_cli_calls_are_refused_with_a_fixed_redacted_code(self):
        cases = {'add fails': lambda f: f.fail.add('add-json'),
                 'add lost': lambda f: setattr(f, 'lose_add', True),
                 'get times out': lambda f: f.timeout.add('get'),
                 'add times out': lambda f: f.timeout.add('add-json')}
        for label, arrange in cases.items():
            with self.subTest(label):
                self.setUp()
                fake = self.claude()
                arrange(fake)
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'claude_registration_failed')
                self.assertIsNone(caught.exception.__cause__)
                self.assertNotIn('synthetic', repr(caught.exception.args))
                self.assertNotIn('remove', fake.verbs())
                self.assertFalse((self.runtime / 'current.json').exists())
                self.assertIsNone(fake.entry)

    def test_retry_after_a_cli_failure_completes_the_retained_transaction(self):
        fake = self.claude()
        fake.fail.add('add-json')
        with self.assertRaises(self.bundle.InstallError):
            self.install(self.packet)
        fake.fail.clear()
        result = self.install(self.packet)
        self.assertEqual((result['installation'], result['claude_code']), ('installed', 'registered'))
        self.assertEqual(len(self.journal()['publications']['claude_add']), 2)

    # --- journal-backed ownership and rollback --------------------------------------------

    def test_crash_after_our_add_is_still_ours_on_retry_and_not_added_twice(self):
        fake = self.claude()
        def crash(stage):
            if stage == 'published:claude_add': raise RuntimeError('simulated crash after add-json')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
        self.assertIsNotNone(fake.entry)
        self.assertNotIn('remove', fake.verbs())  # A crash performs no rollback.
        result = self.install(self.packet)
        self.assertEqual(result['claude_code'], 'registered')  # Journaled attempt: ours, not "pre-existing".
        self.assertEqual(fake.verbs().count('add-json'), 1)

    def test_crash_between_the_journaled_attempt_and_the_add_retries_cleanly(self):
        fake = self.claude()
        def crash(stage):
            if stage == 'recorded:claude_add': raise RuntimeError('simulated crash before add-json')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
        self.assertIsNone(fake.entry)
        self.assertEqual(self.install(self.packet)['claude_code'], 'registered')
        self.assertEqual(len(self.journal()['publications']['claude_add']), 2)

    def test_refusal_after_our_add_removes_only_our_entry_and_a_retry_registers_it_again(self):
        fake = self.claude()
        def fail(stage):
            if stage == 'claude_code': raise OSError('simulated disk failure after registration')
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet, checkpoint=fail)
        self.assertEqual(str(caught.exception), 'installation_refused')
        self.assertEqual(fake.verbs().count('remove'), 1)
        self.assertIsNone(fake.entry)
        self.assertFalse((self.runtime / 'current.json').exists())
        result = self.install(self.packet)
        self.assertEqual(result['claude_code'], 'registered')
        self.assertIsNotNone(fake.entry)

    def test_rollback_never_removes_an_entry_it_did_not_add_or_that_is_no_longer_exact(self):
        command, args = self.expected()
        def fail(stage):
            if stage == 'claude_code': raise OSError('simulated disk failure after registration')
        fake = self.claude()
        fake.entry = entry(command, args)  # Pre-existing and exact: reused, not ours.
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=fail)
        self.assertEqual(fake.entry, entry(command, args))
        self.assertNotIn('remove', fake.verbs())
        self.setUp()
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        def replace_then_fail(stage):
            if stage == 'claude_code':
                fake.entry = foreign  # Ours was added, then someone replaced it.
                raise OSError('simulated disk failure after registration')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=replace_then_fail)
        self.assertEqual(fake.entry, foreign)
        self.assertNotIn('remove', fake.verbs())

    def test_cli_failure_during_rollback_cannot_hide_the_original_refusal(self):
        fake = self.claude()
        def fail(stage):
            if stage == 'claude_code':
                fake.fail.add('remove')
                raise OSError('simulated disk failure after registration')
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet, checkpoint=fail)
        self.assertEqual(str(caught.exception), 'installation_refused')
        self.assertIsNotNone(fake.entry)  # Left in place, never forced; a retry reuses it as ours.
        fake.fail.clear()
        self.assertEqual(self.install(self.packet)['claude_code'], 'registered')

    def test_receipt_is_published_only_after_the_registration_outcome_is_recorded(self):
        self.claude()
        order = []
        def watch(stage):
            order.append(stage)
            if stage == 'receipt':
                self.assertEqual(self.journal()['publications']['claude_code'], [{'status': 'registered'}])
        self.install(self.packet, checkpoint=watch)
        self.assertLess(order.index('claude_code'), order.index('receipt'))
        self.assertLess(order.index('claude-telegram-setup'), order.index('claude_code'))


class ClaudeCliAdapterTests(unittest.TestCase):
    """Fixed locations, executable checks and parsing, with nothing outside a disposable directory."""

    def make(self, root, name='claude', mode=0o700):
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(b'#!/bin/sh\n')
        path.chmod(mode)
        return path

    def test_the_fixed_candidate_list_is_exactly_the_four_documented_locations_in_order(self):
        self.assertEqual(claude_registration.candidate_paths(Path('/Users/example')),
                         (Path('/Users/example/.local/bin/claude'), Path('/Users/example/.claude/local/claude'),
                          Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude')))

    def test_locate_accepts_user_owned_executables_and_links_to_them_and_picks_the_first(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first = self.make(root, 'a/claude')
            second = self.make(root, 'b/claude')
            self.assertEqual(claude_registration.locate((first, second)), first)
            link = root / 'link/claude'
            link.parent.mkdir(mode=0o700)
            link.symlink_to(second)
            self.assertEqual(claude_registration.locate((root / 'missing', link)), link)

    def test_locate_rejects_missing_non_regular_unexecutable_writable_and_foreign_owned_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'dir').mkdir()
            cases = {'missing': root / 'nothing', 'directory': root / 'dir',
                     'not executable': self.make(root, 'plain/claude', 0o600),
                     'group writable': self.make(root, 'gw/claude', 0o770),
                     'world writable': self.make(root, 'ww/claude', 0o707),
                     'dangling link': root / 'dangling'}
            cases['dangling link'].symlink_to(root / 'nowhere')
            for label, path in cases.items():
                with self.subTest(label):
                    self.assertIsNone(claude_registration.locate((path,)))
            owned = self.make(root, 'owned/claude')
            with mock.patch.object(claude_registration.os, 'getuid', return_value=os.getuid() + 1):
                self.assertIsNone(claude_registration.locate((owned,)))  # Neither the caller's nor root's.

    def test_locate_never_searches_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.make(root, 'onpath/claude')
            with mock.patch.dict(os.environ, {'PATH': str(root / 'onpath')}):
                self.assertIsNone(claude_registration.locate((root / 'home/.local/bin/claude', root / 'home/.claude/local/claude')))

    def test_get_output_is_matched_on_fields_not_substrings(self):
        command, args = '/r/python/bin/python3', ['-I', '-S', '-B', '/r/launcher.py', 'mcp']
        fake = FakeClaude(entry(command, args))
        cli = Path('/synthetic/claude')
        self.assertEqual(claude_registration.state(fake, cli, Path('/h'), command, args), 'exact')
        for label, other in {'command': entry(command + 'x', args), 'args': entry(command, args + ['x']),
                             'scope': entry(command, args, scope='local'), 'env': entry(command, args, env={'A': 'b'})}.items():
            with self.subTest(label):
                fake.entry = other
                self.assertEqual(claude_registration.state(fake, cli, Path('/h'), command, args), 'different')
        fake.entry = None
        self.assertEqual(claude_registration.state(fake, cli, Path('/h'), command, args), 'absent')
        for garbage in ('', 'telegram_search:\n', 'another:\n  Scope: User config\n  Type: stdio\n  Command: %s\n  Args: %s\n'
                        % (command, ' '.join(args)), 'x' * 200000):
            with self.subTest(garbage=garbage[:20]):
                runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, garbage, '')
                self.assertEqual(claude_registration.state(runner, cli, Path('/h'), command, args), 'different')

    def test_runner_errors_become_one_fixed_code(self):
        cli = Path('/synthetic/claude')
        for error in (OSError('private'), subprocess.TimeoutExpired('claude', 1), ValueError('private')):
            with self.subTest(type(error).__name__):
                def runner(argv, **kw): raise error
                with self.assertRaises(claude_registration.ClaudeCliError) as caught:
                    claude_registration.state(runner, cli, Path('/h'), '/c', [])
                self.assertEqual(str(caught.exception), 'claude_registration_failed')
                self.assertNotIn('private', str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)


if __name__ == '__main__': unittest.main()
