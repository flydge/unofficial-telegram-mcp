"""Claude Code registration inside the install transaction, against a behavioural fake CLI only.

State comes only from `<home>/.claude.json` of a disposable home; no test here runs the real
`claude`, reads any real client configuration or uses a real home.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from claude_fake import entry
from test_bundle_install import InstallHarness
from telegram_search_mcp import claude_registration

SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


class ClaudeInstallTests(InstallHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.packet = self.fixture()
        self.bundle = self.packet[0]

    def expected(self):
        release = self.runtime / 'releases' / self.packet[3]
        return str(release / 'python/bin/python3'), ['-I', '-S', '-B', str(release / 'launcher.py'), 'mcp']

    def manual(self):
        command, args = self.expected()
        return ['claude', 'mcp', 'add-json', '--scope', 'user', 'telegram_search',
                json.dumps({'type': 'stdio', 'command': command, 'args': args}, separators=(',', ':'))]

    def journal(self):
        return json.loads((self.runtime / 'transactions' / ('install_' + self.packet[3][:24]) / 'journal.json').read_text())

    def receipt(self):
        return json.loads((self.runtime / 'receipt.json').read_text())

    def assertNothingCreated(self):
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.codex / 'config.toml').exists())
        self.assertFalse(self.skills.exists())
        self.assertFalse((self.home / '.claude').exists())

    def assertCompletedWith(self, result, status):
        self.assertEqual(result['status'], 'activation_required')
        self.assertTrue((self.runtime / 'current.json').is_file())
        self.assertEqual(result['claude_code'], status)
        self.assertEqual(self.receipt()['claude_code'], status)
        if status in ('failed', 'unavailable', 'not_found'):
            self.assertEqual(result['claude_registration_command'], self.manual())
        else:
            self.assertNotIn('claude_registration_command', result)

    # --- outcomes --------------------------------------------------------------------------

    def test_absent_entry_is_added_once_in_user_scope_and_confirmed_from_the_file(self):
        fake = self.claude()
        result = self.install(self.packet)
        command, args = self.expected()
        self.assertCompletedWith(result, 'registered')
        self.assertEqual(fake.entry, {'type': 'stdio', 'command': command, 'args': args})  # The CLI's own shape.
        self.assertEqual(fake.verbs(), ['add-json'])  # Neither the pre-check nor the confirmation runs the CLI.
        add = fake.calls[0]
        self.assertEqual(add[2:5], ['--scope', 'user', 'telegram_search'])
        self.assertEqual(add[5], json.dumps({'type': 'stdio', 'command': command, 'args': args}, separators=(',', ':')))
        self.assertEqual(self.journal()['publications']['claude_code'], [{'status': 'registered'}])
        self.assertEqual(self.journal()['publications']['claude_add'], [{'attempt': 1}])

    def test_every_cli_call_runs_the_resolved_target_with_exactly_the_minimal_environment(self):
        fake = self.claude()
        link = self.base / 'links' / 'claude'
        link.parent.mkdir(mode=0o700)
        link.symlink_to(self.claude_cli)
        noisy = {'FORCE_COLOR': '1', 'CLAUDECODE': '1', 'NODE_OPTIONS': '--require=/foreign.js', 'PATH': '/foreign/bin'}
        with mock.patch.dict(os.environ, noisy):
            self.install(self.packet, claude_candidates=(link,))
        self.assertTrue(fake.calls)
        for argv0, argv, kwargs in zip(fake.argv0, fake.calls, fake.kwargs):
            self.assertEqual(argv0, str(self.claude_cli))  # The resolved target, not the link.
            self.assertEqual(argv[0], 'mcp')
            self.assertEqual(kwargs['env'], {'HOME': str(self.home), 'PATH': str(self.claude_cli.parent) + ':' + SYSTEM_PATH,
                                             'LC_ALL': 'C'})
            self.assertTrue(0 < kwargs['timeout'] <= 60)
            self.assertEqual(kwargs['cwd'], str(self.home))
            for stream in ('stdin', 'stdout', 'stderr'):
                self.assertEqual(kwargs[stream], subprocess.DEVNULL)
            self.assertNotIn('shell', kwargs)
            self.assertNotIn('capture_output', kwargs)

    def test_exact_existing_entry_is_reused_without_any_cli_process(self):
        for label in ('cli shape', 'empty env', 'no type'):
            with self.subTest(label):
                self.setUp()
                command, args = self.expected()
                value = {'cli shape': {'type': 'stdio', 'command': command, 'args': args},
                         'empty env': {'type': 'stdio', 'command': command, 'args': args, 'env': {}},
                         'no type': {'command': command, 'args': args}}[label]
                fake = self.claude()
                fake.entry = value
                result = self.install(self.packet)
                self.assertCompletedWith(result, 'reused')
                self.assertEqual(fake.entry, value)
                self.assertEqual(fake.calls, [])
                self.assertNotIn('claude_add', self.journal()['publications'])

    def test_without_a_cli_the_install_records_not_found_and_offers_the_manual_command(self):
        fake = self.claude()
        result = self.install(self.packet, claude_candidates=(self.base / 'missing/claude',))
        self.assertCompletedWith(result, 'not_found')
        self.assertEqual(fake.calls, [])
        self.assertIsNone(fake.entry)
        self.assertEqual(json.loads(result['claude_registration_command'][-1])['args'][-1], 'mcp')

    def test_unreadable_or_invalid_claude_config_completes_as_unavailable_without_running_the_cli(self):
        command, args = self.expected()
        valid = json.dumps({'mcpServers': {}})
        def symlink(path):
            (self.base / 'elsewhere.json').write_text(valid); path.symlink_to(self.base / 'elsewhere.json')
        def hardlink(path):
            path.write_text(valid); os.link(path, self.base / 'alias.json')
        cases = {'not json': lambda p: p.write_text('{"mcpServers": {'),
                 'duplicate keys': lambda p: p.write_text('{"mcpServers": {}, "mcpServers": {}}'),
                 'duplicate server': lambda p: p.write_text('{"mcpServers": {"telegram_search": {}, "telegram_search": %s}}'
                                                            % json.dumps({'command': command, 'args': args})),
                 'non-standard constant': lambda p: p.write_text('{"mcpServers": {}, "x": NaN}'),
                 'top-level array': lambda p: p.write_text('[]'),
                 'servers not an object': lambda p: p.write_text('{"mcpServers": []}'),
                 'symlink': symlink, 'hard link': hardlink,
                 'group writable': lambda p: (p.write_text(valid), p.chmod(0o620)),
                 'unreadable': lambda p: (p.write_text(valid), p.chmod(0o000))}
        for label, arrange in cases.items():
            with self.subTest(label):
                self.setUp()
                fake = self.claude()
                path = self.home / '.claude.json'
                arrange(path)
                if not path.is_symlink() and label not in ('group writable', 'unreadable'): path.chmod(0o600)
                before = os.readlink(path) if path.is_symlink() else (path.stat().st_mode, path.stat().st_ino)
                result = self.install(self.packet)
                self.assertCompletedWith(result, 'unavailable')
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.readlink(path) if path.is_symlink() else (path.stat().st_mode, path.stat().st_ino), before)

    def test_oversized_claude_config_is_unavailable(self):
        fake = self.claude()
        fake.entry = None
        with mock.patch.object(claude_registration, 'MAX_CONFIG_BYTES', 8):
            self.assertCompletedWith(self.install(self.packet), 'unavailable')
        self.assertEqual(fake.calls, [])

    def test_project_local_entries_do_not_count_and_are_left_untouched(self):
        fake = self.claude()
        local = {'type': 'stdio', 'command': '/foreign/python3', 'args': ['mcp']}
        fake.write_document({'projects': {str(self.home): {'mcpServers': {'telegram_search': local}}}})
        self.assertCompletedWith(self.install(self.packet), 'registered')
        self.assertEqual(fake.document()['projects'][str(self.home)]['mcpServers']['telegram_search'], local)

    def test_repeat_install_reuses_the_recorded_outcome_without_reading_claude_state(self):
        fake = self.claude()
        self.assertEqual(self.install(self.packet)['claude_code'], 'registered')
        fake.fail.update({'add-json', 'remove'})
        fake.write_document({'mcpServers': {'telegram_search': entry('/foreign/python3', ['mcp'])}})  # Even foreign now.
        before = list(fake.calls)
        again = self.install(self.packet)
        self.assertEqual((again['installation'], again['claude_code']), ('reused', 'registered'))
        self.assertEqual(fake.calls, before)

    # --- different entries ------------------------------------------------------------------

    def different_entries(self):
        command, args = self.expected()
        split = [part for arg in args for part in arg.split(' ')]
        return {
            'other command': entry('/foreign/python3', args),
            'other args': entry(command, args + ['--extra']),
            'fewer args': entry(command, args[:-1]),
            'args joined into one string': {'type': 'stdio', 'command': command, 'args': ' '.join(args)},
            'args re-split at the space in the path': entry(command, split),
            'http server': entry(type='http', url='https://example.invalid/mcp'),
            'url beside a matching command': {**entry(command, args), 'url': 'https://example.invalid/mcp'},
            'sse type': entry(command, args, type='sse'),
            'extra environment': entry(command, args, env={'EXAMPLE': 'value'}),
            'environment not an object': {**entry(command, args), 'env': []},
            'unknown extra key': {**entry(command, args), 'cwd': '/foreign'},
            'not an object': 'telegram_search',
            'null': None,
        }

    def test_different_entry_refuses_before_anything_is_created_and_is_never_modified(self):
        for label in list(self.different_entries()):
            with self.subTest(label):
                self.setUp()
                self.base.joinpath('with space').mkdir()
                self.runtime = self.base / 'with space' / 'runtime'  # The real default root has a space too.
                foreign = self.different_entries()[label]
                if label == 'args re-split at the space in the path': self.assertNotEqual(foreign['args'], self.expected()[1])
                fake = self.claude()
                fake.write_document({'mcpServers': {'telegram_search': foreign, 'neighbour': entry('/n', [])}})
                before = fake.path.read_bytes()
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'claude_registration_conflict')
                self.assertEqual(fake.path.read_bytes(), before)
                self.assertEqual(fake.calls, [])
                self.assertNothingCreated()
                for text in ('/foreign', 'example.invalid', 'EXAMPLE'):  # nothing from the entry is echoed
                    self.assertNotIn(text, repr(caught.exception.args) + str(caught.exception))

    def test_different_entry_appearing_mid_transaction_completes_as_failed_and_is_never_touched(self):
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        def appear(stage):
            if stage == 'claude-telegram-setup': fake.entry = foreign
        result = self.install(self.packet, checkpoint=appear)
        self.assertCompletedWith(result, 'failed')
        self.assertEqual(fake.entry, foreign)
        self.assertEqual(fake.calls, [])

    def test_entry_replaced_by_a_foreign_one_during_our_add_is_failed_and_never_removed(self):
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        fake.mutate_after_add = lambda f: setattr(f, 'entry', foreign)
        self.assertCompletedWith(self.install(self.packet), 'failed')
        self.assertEqual(fake.entry, foreign)
        self.assertNotIn('remove', fake.verbs())

    # --- CLI failures never block ---------------------------------------------------------

    def test_npm_style_cli_exiting_127_completes_as_failed_and_never_locks_up(self):
        fake = self.claude()
        fake.fail.add('add-json')
        fake.returncode = 127  # `#!/usr/bin/env node` with node outside the minimal PATH.
        self.assertCompletedWith(self.install(self.packet), 'failed')
        self.assertEqual(fake.verbs(), ['add-json'])
        for _ in range(10):
            again = self.install(self.packet)
            self.assertEqual((again['installation'], again['claude_code']), ('reused', 'failed'))
            self.assertEqual(again['claude_registration_command'], self.manual())
        self.assertEqual(fake.verbs(), ['add-json'])  # No retries, so nothing approaches a recovery cap.
        self.assertEqual(self.journal()['publications']['claude_add'], [{'attempt': 1}])
        self.assertIsNone(fake.entry)

    def test_failed_lost_or_timed_out_add_completes_as_failed_with_a_redacted_result(self):
        cases = {'add fails': lambda f: f.fail.add('add-json'),
                 'add lost': lambda f: setattr(f, 'lose_add', True),
                 'add times out': lambda f: f.timeout.add('add-json')}
        for label, arrange in cases.items():
            with self.subTest(label):
                self.setUp()
                fake = self.claude()
                arrange(fake)
                result = self.install(self.packet)
                self.assertCompletedWith(result, 'failed')
                self.assertNotIn('synthetic', json.dumps(result))
                self.assertNotIn('remove', fake.verbs())
                self.assertIsNone(fake.entry)

    def test_real_npm_style_script_exits_127_under_the_minimal_path(self):
        if shutil.which('node', path=SYSTEM_PATH):
            self.skipTest('node is on the minimal system PATH here')
        self.claude()
        script = self.base / 'npm' / 'cli.js'
        script.parent.mkdir(mode=0o700)
        script.write_text('#!/usr/bin/env node\nprocess.exit(0)\n')
        script.chmod(0o700)
        link = self.base / 'npm-bin' / 'claude'
        link.parent.mkdir(mode=0o700)
        link.symlink_to(script)
        result = self.install(self.packet, claude_candidates=(link,), claude_runner=subprocess.run)
        self.assertCompletedWith(result, 'failed')
        self.assertFalse((self.home / '.claude.json').exists())

    def test_real_process_receives_only_the_minimal_environment(self):
        self.claude()
        record = self.base / 'env.txt'
        script = self.base / 'envbin' / 'claude'
        script.parent.mkdir(mode=0o700)
        script.write_text('#!/bin/sh\n/usr/bin/env > %s\nexit 1\n' % record)
        script.chmod(0o700)
        with mock.patch.dict(os.environ, {'FORCE_COLOR': '1', 'CLAUDECODE': '1', 'NODE_OPTIONS': '--x'}):
            result = self.install(self.packet, claude_candidates=(script,), claude_runner=subprocess.run)
        self.assertCompletedWith(result, 'failed')
        seen = dict(line.split('=', 1) for line in record.read_text().splitlines() if '=' in line)
        self.assertEqual((seen['HOME'], seen['PATH'], seen['LC_ALL']),
                         (str(self.home), str(script.parent) + ':' + SYSTEM_PATH, 'C'))
        for name in ('FORCE_COLOR', 'CLAUDECODE', 'NODE_OPTIONS', 'USER', 'TMPDIR'):
            self.assertNotIn(name, seen)
        self.assertTrue(set(seen) <= {'HOME', 'PATH', 'LC_ALL', 'PWD', 'SHLVL', '_', 'OLDPWD'}, sorted(seen))

    # --- resume: the recorded outcome is final -----------------------------------------------

    def test_crash_after_our_add_confirms_on_resume_without_adding_again(self):
        fake = self.claude()
        def crash(stage):
            if stage == 'published:claude_add': raise RuntimeError('simulated crash after add-json')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
        self.assertIsNotNone(fake.entry)
        self.assertNotIn('remove', fake.verbs())  # A crash performs no rollback.
        self.assertCompletedWith(self.install(self.packet), 'registered')
        self.assertEqual(fake.verbs(), ['add-json'])

    def test_crash_between_the_journaled_attempt_and_the_add_never_re_registers(self):
        fake = self.claude()
        def crash(stage):
            if stage == 'recorded:claude_add': raise RuntimeError('simulated crash before add-json')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
        self.assertIsNone(fake.entry)
        self.assertCompletedWith(self.install(self.packet), 'failed')
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.journal()['publications']['claude_add'], [{'attempt': 1}])

    def test_resume_after_the_outcome_is_recorded_never_consults_claude_again(self):
        for stage in ('claude_code', 'created:packet:receipt', 'recorded:receipt', 'receipt'):
            with self.subTest(stage=stage):
                self.setUp()
                fake = self.claude()
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
                calls = list(fake.calls)
                fake.entry = None  # The environment changes between crash and resume.
                fake.fail.add('add-json')
                self.assertCompletedWith(self.install(self.packet), 'registered')
                self.assertEqual(fake.calls, calls)

    def test_receipt_published_then_environment_changes_cause_no_metadata_conflict(self):
        def crash(stage):
            if stage == 'receipt': raise RuntimeError('crash after the receipt')
        changes = {
            'cli vanishes': (None, dict(claude_candidates=(Path('/nonexistent/claude'),))),
            'cli appears': (dict(claude_candidates=(Path('/nonexistent/claude'),)), {}),
            'config unreadable': (None, 'unreadable'),
            'foreign entry appears': (None, 'foreign'),
        }
        for label, (first, second) in changes.items():
            with self.subTest(label):
                self.setUp()
                fake = self.claude()
                with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash, **(first or {}))
                status = self.receipt()['claude_code']
                calls = list(fake.calls)
                if second == 'unreadable': fake.path.write_text('{'); second = {}
                elif second == 'foreign': fake.entry = entry('/foreign/python3', ['mcp']); second = {}
                for _ in range(3):
                    result = self.install(self.packet, **second)
                    self.assertEqual(result['claude_code'], status)
                self.assertEqual(fake.calls, calls)

    # --- rollback: only our exact entry, only before its outcome is durable --------------------

    def test_refusal_right_after_our_add_removes_only_our_exact_entry_and_resume_never_re_adds(self):
        fake = self.claude()
        def fail(stage):
            if stage == 'published:claude_add': raise OSError('simulated disk failure after add-json')
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet, checkpoint=fail)
        self.assertEqual(str(caught.exception), 'installation_refused')
        self.assertEqual(fake.verbs(), ['add-json', 'remove'])
        self.assertIsNone(fake.entry)
        self.assertCompletedWith(self.install(self.packet), 'failed')
        self.assertEqual(fake.verbs(), ['add-json', 'remove'])

    def test_refusal_after_the_outcome_is_recorded_keeps_the_entry_and_resume_completes(self):
        fake = self.claude()
        def fail(stage):
            if stage == 'claude_code': raise OSError('simulated disk failure after registration')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=fail)
        self.assertNotIn('remove', fake.verbs())
        self.assertIsNotNone(fake.entry)
        self.assertFalse((self.runtime / 'current.json').exists())
        self.assertCompletedWith(self.install(self.packet), 'registered')
        self.assertEqual(fake.verbs(), ['add-json'])

    def test_rollback_never_removes_an_entry_it_did_not_add_or_that_is_no_longer_exact(self):
        command, args = self.expected()
        def fail(stage):
            if stage == 'claude_code': raise OSError('simulated disk failure')
        fake = self.claude()
        fake.entry = entry(command, args)  # Pre-existing and exact: reused, not ours.
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=fail)
        self.assertEqual(fake.entry, entry(command, args))
        self.assertEqual(fake.calls, [])
        self.setUp()
        fake = self.claude()
        foreign = entry('/foreign/python3', ['mcp'])
        def replace_then_fail(stage):
            if stage == 'published:claude_add':
                fake.entry = foreign  # Ours was added, then someone replaced it.
                raise OSError('simulated disk failure after add-json')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=replace_then_fail)
        self.assertEqual(fake.entry, foreign)
        self.assertNotIn('remove', fake.verbs())

    def test_cli_failure_during_rollback_cannot_hide_the_original_refusal(self):
        fake = self.claude()
        def fail(stage):
            if stage == 'published:claude_add':
                fake.fail.add('remove')
                raise OSError('simulated disk failure after add-json')
        with self.assertRaises(self.bundle.InstallError) as caught:
            self.install(self.packet, checkpoint=fail)
        self.assertEqual(str(caught.exception), 'installation_refused')
        self.assertIsNotNone(fake.entry)  # Left in place, never forced; the resume confirms it as ours.
        fake.fail.clear()
        self.assertCompletedWith(self.install(self.packet), 'registered')
        self.assertEqual(fake.verbs().count('add-json'), 1)

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

    # --- skill roots ------------------------------------------------------------------------

    def test_skills_root_overlapping_claude_skills_is_refused_before_anything_is_created(self):
        for label in ('equal', 'inside', 'containing'):
            with self.subTest(label):
                self.setUp()
                self.skills = {'equal': self.claude_skills, 'inside': self.claude_skills / 'nested',
                               'containing': self.home / '.claude'}[label]
                fake = self.claude()
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'overlapping_paths')
                self.assertFalse(self.runtime.exists())
                self.assertFalse((self.home / '.claude').exists())
                self.assertFalse((self.codex / 'config.toml').exists())
                self.assertEqual(fake.calls, [])

    def test_claude_skills_are_copied_only_when_claude_code_is_evidently_present(self):
        cases = {'no ~/.claude and no cli': (False, False, 'skipped'), '~/.claude only': (True, False, 'installed'),
                 'cli only': (False, True, 'installed')}
        for label, (directory, cli, status) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.claude()
                if directory: (self.home / '.claude').mkdir(mode=0o755)
                result = self.install(self.packet, **({} if cli else {'claude_candidates': ()}))
                self.assertEqual((result['claude_skills'], self.receipt()['claude_skills']), (status, status))
                objects = set(self.receipt()['publication_objects'])
                if status == 'skipped':
                    self.assertFalse((self.home / '.claude').exists())
                    self.assertEqual(objects, {'release', 'telegram-search', 'telegram-setup'})
                else:
                    self.assertEqual(sorted(p.name for p in self.claude_skills.iterdir()), ['telegram-search', 'telegram-setup'])
                    self.assertIn('claude-telegram-setup', objects)
                self.assertTrue((self.skills / 'telegram-setup' / 'SKILL.md').is_file())  # Always.

    def test_the_claude_skills_decision_is_recorded_once_per_transaction(self):
        self.claude()
        def crash(stage):
            if stage == 'telegram-setup': raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash, claude_candidates=())
        (self.home / '.claude').mkdir(mode=0o755)  # Claude Code arrives before the resume.
        result = self.install(self.packet)
        self.assertEqual(result['claude_skills'], 'skipped')
        self.assertFalse(self.claude_skills.exists())


class ClaudeCliAdapterTests(unittest.TestCase):
    """Fixed locations, no-follow resolution and the file-only state read, inside a disposable directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def make(self, name='claude', mode=0o700, parent_mode=0o700):
        path = self.root / name
        path.parent.mkdir(mode=parent_mode, parents=True, exist_ok=True)
        path.parent.chmod(parent_mode)
        path.write_bytes(b'#!/bin/sh\n')
        path.chmod(mode)
        return path

    def test_the_fixed_candidate_list_is_exactly_the_four_documented_locations_in_order(self):
        self.assertEqual(claude_registration.candidate_paths(Path('/Users/example')),
                         (Path('/Users/example/.local/bin/claude'), Path('/Users/example/.claude/local/claude'),
                          Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude')))

    def test_locate_returns_the_resolved_target_of_the_first_safe_candidate(self):
        first = self.make('a/claude')
        second = self.make('b/versions/2.0.0')
        self.assertEqual(claude_registration.locate((first, second)), first)
        link = self.root / 'link/claude'
        link.parent.mkdir(mode=0o700)
        link.symlink_to('../b/versions/2.0.0')  # Relative, through `..`, like the native installer.
        self.assertEqual(claude_registration.locate((self.root / 'missing', link)), second)
        chained = self.root / 'chain/claude'
        chained.parent.mkdir(mode=0o700)
        (self.root / 'dirlink').symlink_to(self.root / 'b')
        chained.symlink_to(self.root / 'dirlink/versions/../versions/2.0.0')
        self.assertEqual(claude_registration.locate((chained,)), second)

    def test_locate_rejects_missing_non_regular_unexecutable_writable_looping_and_foreign_owned_targets(self):
        (self.root / 'dir').mkdir()
        cases = {'missing': self.root / 'nothing', 'directory': self.root / 'dir',
                 'not executable': self.make('plain/claude', 0o600),
                 'group writable': self.make('gw/claude', 0o770),
                 'world writable': self.make('ww/claude', 0o707),
                 'dangling link': self.root / 'dangling', 'loop': self.root / 'loop-a', 'relative': Path('claude')}
        cases['dangling link'].symlink_to(self.root / 'nowhere')
        (self.root / 'loop-a').symlink_to(self.root / 'loop-b')
        (self.root / 'loop-b').symlink_to(self.root / 'loop-a')
        for label, path in cases.items():
            with self.subTest(label):
                self.assertIsNone(claude_registration.locate((path,)))
        owned = self.make('owned/claude')
        with mock.patch.object(claude_registration.os, 'getuid', return_value=os.getuid() + 1):
            self.assertIsNone(claude_registration.locate((owned,)))  # Neither the caller's nor root's.

    def test_symlinked_candidate_through_a_group_or_world_writable_directory_is_not_located(self):
        for mode in (0o770, 0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                self.setUp()
                target = self.make('shared/claude', parent_mode=mode)  # The target file itself is safe.
                link = self.root / 'bin/claude'
                link.parent.mkdir(mode=0o700)
                link.symlink_to(target)
                self.assertIsNone(claude_registration.locate((link,)))
                (self.root / 'safe').mkdir(mode=0o700)
                (self.root / 'safe/via').symlink_to(self.root / 'shared')  # A directory link into it.
                self.assertIsNone(claude_registration.locate((self.root / 'safe/via/claude',)))
                inside = self.root / 'shared/link'
                inside.symlink_to(self.make('ok/claude'))  # A link that lives in the writable directory.
                self.assertIsNone(claude_registration.locate((inside,)))

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin extended ACLs')
    def test_an_allow_acl_on_a_traversed_directory_is_not_located(self):
        target = self.make('acl/claude')
        if subprocess.run(['/bin/chmod', '+a', 'everyone allow add_file', str(target.parent)], check=False).returncode:
            self.skipTest('cannot set an ACL here')
        self.assertIsNone(claude_registration.locate((target,)))

    def test_locate_never_searches_path(self):
        self.make('onpath/claude')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'onpath')}):
            self.assertIsNone(claude_registration.locate((self.root / 'home/.local/bin/claude',
                                                          self.root / 'home/.claude/local/claude')))

    # --- state: ~/.claude.json only -----------------------------------------------------

    def home_with(self, text=None, mode=0o600):
        home = self.root / ('home-%d' % len(list(self.root.iterdir())))
        home.mkdir(mode=0o700)
        if text is not None:
            (home / '.claude.json').write_text(text)
            (home / '.claude.json').chmod(mode)
        return home

    def test_state_reads_only_the_user_scope_object_of_claude_json(self):
        command, args = '/r e/python/bin/python3', ['-I', '-S', '-B', '/r e/launcher.py', 'mcp']
        def doc(value): return json.dumps({'numStartups': 1, 'mcpServers': {'telegram_search': value}})
        table = {
            'absent': [None, '{}', '{"mcpServers": {}}', json.dumps({'mcpServers': {'other': {}}}),
                       json.dumps({'projects': {'/p': {'mcpServers': {'telegram_search': {'command': command, 'args': args}}}}})],
            'exact': [doc({'type': 'stdio', 'command': command, 'args': args}), doc({'command': command, 'args': args}),
                      doc({'type': 'stdio', 'command': command, 'args': args, 'env': {}})],
            'different': [doc({'type': 'stdio', 'command': command, 'args': ['-I', '-S', '-B', '/r', 'e/launcher.py', 'mcp']}),
                          doc({'type': 'stdio', 'command': command, 'args': ' '.join(args)}),
                          doc({'type': 'stdio', 'command': command, 'args': args, 'env': {'A': 'b'}}),
                          doc({'type': 'stdio', 'command': command, 'args': args, 'url': 'x'}),
                          doc({'type': 'http', 'command': command, 'args': args}),
                          doc({'type': 'stdio', 'command': [command], 'args': args}),
                          doc(None), doc([]), doc('x')],
            'unavailable': ['', '{', '[]', '{"mcpServers": 1}', '{"a": 1, "a": 2}', '{"a": Infinity}', '\xff'],
        }
        for expected, texts in table.items():
            for text in texts:
                with self.subTest(expected=expected, text=(text or '')[:60]):
                    self.assertEqual(claude_registration.state(self.home_with(text), command, args), expected)

    def test_state_refuses_unsafe_files_without_following_links(self):
        command, args = '/c', ['mcp']
        good = json.dumps({'mcpServers': {'telegram_search': {'command': command, 'args': args}}})
        self.assertEqual(claude_registration.state(self.home_with(good), command, args), 'exact')
        self.assertEqual(claude_registration.state(self.home_with(good, 0o644), command, args), 'exact')
        for mode in (0o620, 0o602, 0o000):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(claude_registration.state(self.home_with(good, mode), command, args), 'unavailable')
        linked = self.home_with()
        (self.root / 'real.json').write_text(good); (self.root / 'real.json').chmod(0o600)
        (linked / '.claude.json').symlink_to(self.root / 'real.json')
        self.assertEqual(claude_registration.state(linked, command, args), 'unavailable')
        hard = self.home_with(good)
        os.link(hard / '.claude.json', self.root / 'alias.json')
        self.assertEqual(claude_registration.state(hard, command, args), 'unavailable')
        with mock.patch.object(claude_registration.os, 'getuid', return_value=os.getuid() + 1):
            self.assertEqual(claude_registration.state(self.home_with(good), command, args), 'unavailable')

    def test_state_never_starts_a_process(self):
        home = self.home_with(json.dumps({'mcpServers': {}}))
        with mock.patch('subprocess.run', side_effect=AssertionError('no process')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('no process')):
            self.assertEqual(claude_registration.state(home, '/c', []), 'absent')

    # --- writes ---------------------------------------------------------------------------

    def test_runner_errors_and_non_zero_exits_become_one_fixed_code(self):
        cli = Path('/synthetic/claude')
        failures = [OSError('private'), subprocess.TimeoutExpired('claude', 1), ValueError('private'),
                    subprocess.CompletedProcess(['claude'], 127), subprocess.CompletedProcess(['claude'], 1)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def runner(argv, **kw):
                    if isinstance(failure, BaseException): raise failure
                    return failure
                for call in (lambda: claude_registration.add(runner, cli, Path('/h'), '/c', []),
                             lambda: claude_registration.remove(runner, cli, Path('/h'))):
                    with self.assertRaises(claude_registration.ClaudeCliError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), 'claude_registration_failed')
                    self.assertNotIn('private', str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)

    def test_manual_command_is_a_nonsecret_argv_for_user_scope(self):
        self.assertEqual(claude_registration.manual_command('/r e/python3', ['-I', 'mcp']),
                         ['claude', 'mcp', 'add-json', '--scope', 'user', 'telegram_search',
                          '{"type":"stdio","command":"/r e/python3","args":["-I","mcp"]}'])


if __name__ == '__main__': unittest.main()
