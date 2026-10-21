"""Claude Desktop registration inside the install transaction (CLIENTS-9), over disposable homes only.

`<home>/Library/Application Support/Claude` is always inside a temporary directory: no test here reads
or writes the real ~/Library, starts Claude Desktop or uses a real client configuration.
"""
import contextlib
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_bundle_install import InstallHarness
from telegram_search_mcp import claude_desktop_registration as desktop, codex_registration

SECRET = 'SYNTHETIC-NEIGHBOUR-TOKEN-7f3a9c'
# Electron's layout, a neighbour server holding a secret in env, and number lexemes that round-trip.
NEIGHBOUR_TEXT = '''{
  "globalShortcut": "Alt+Space",
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "server-filesystem", "/Users/example/Desktop"],
      "env": {"API_TOKEN": "%s"}
    },
    "weather": {"command": "/opt/weather/bin/weather", "args": [],
                "env": {"RATE": 1.5, "LIMIT": 12345678901234567890, "OFFSET": -3, "BIG": 1e+21, "ON": true, "NOTE": null}}
  },
  "preferences": {"menuBarEnabled": false, "zoom": 0.25, "lang": "espa\\u00f1ol ✓", "list": [1, [2, {"a": []}], {}]}
}
''' % SECRET
UNSUPPORTED_LEXEMES = ('1.10', '1E5', '-0', '1e400', '0.10000000000000001', '1.0e2', '-0.0e0')
DESKTOP_CHECKPOINTS = ('recorded:claude_desktop_add', 'recorded:claude_desktop_write', 'published:claude_desktop_add',
                       'claude_desktop')


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def without_ours(document, *, had_servers=True):
    value = json.loads(json.dumps(document))
    value.get('mcpServers', {}).pop('telegram_search', None)
    if not had_servers and value.get('mcpServers') == {}:
        del value['mcpServers']
    return value


class DesktopHarness(InstallHarness):
    def setUp(self):
        super().setUp()
        self.packet = self.fixture()
        self.bundle = self.packet[0]

    @property
    def desktop_dir(self):
        return self.home / 'Library' / 'Application Support' / 'Claude'

    @property
    def config(self):
        return self.desktop_dir / 'claude_desktop_config.json'

    def present(self, mode=0o700):
        self.desktop_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.desktop_dir.mkdir(mode=mode)
        self.desktop_dir.chmod(mode)

    def write_config(self, text, mode=0o644):
        self.config.write_bytes(text.encode() if type(text) is str else text)
        self.config.chmod(mode)
        return self.config.read_bytes()

    def expected(self):
        release = self.runtime / 'releases' / self.packet[3]
        return str(release / 'python/bin/python3'), ['-I', '-S', '-B', str(release / 'launcher.py'), 'mcp']

    def ours(self):
        command, args = self.expected()
        return {'command': command, 'args': args}

    def tx(self):
        return self.runtime / 'transactions' / ('install_' + self.packet[3][:24])

    def journal(self):
        return json.loads((self.tx() / 'journal.json').read_text())

    def receipt(self):
        return json.loads((self.runtime / 'receipt.json').read_text())

    def document(self):
        return json.loads(self.config.read_text())

    def assertNothingCreated(self):
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.codex / 'config.toml').exists())
        self.assertFalse(self.skills.exists())
        self.assertFalse((self.home / '.claude').exists())

    def assertCompletedWith(self, result, status):
        self.assertEqual(result['status'], 'activation_required')
        self.assertTrue((self.runtime / 'current.json').is_file())
        self.assertEqual((result['claude_desktop'], self.receipt()['claude_desktop']), (status, status))
        if status == 'registered':
            self.assertEqual(result['claude_desktop_next_action'], 'restart_claude_desktop')
        else:
            self.assertNotIn('claude_desktop_next_action', result)
        if status in ('failed', 'unavailable'):
            self.assertEqual(result['claude_desktop_manual_entry'], {'mcpServers': {'telegram_search': self.ours()}})
        else:
            self.assertNotIn('claude_desktop_manual_entry', result)
        self.assertEqual(result['next_action'], 'run_activation_then_wait')  # Activation still comes first.

    @contextlib.contextmanager
    def replacements(self):
        """Counts replacements of the Desktop config (any writer through codex_registration)."""
        counted = []
        original = os.replace
        def replace(source, destination, **kwargs):
            original(source, destination, **kwargs)
            if destination == desktop.CONFIG_NAME:
                counted.append(destination)
        with mock.patch.object(codex_registration.os, 'replace', side_effect=replace):
            yield counted


class ClaudeDesktopInstallTests(DesktopHarness, unittest.TestCase):

    # --- outcomes --------------------------------------------------------------------------

    def test_absent_config_is_created_with_only_our_entry_and_confirmed(self):
        self.present()
        result = self.install(self.packet)
        self.assertCompletedWith(result, 'registered')
        self.assertEqual(self.document(), {'mcpServers': {'telegram_search': self.ours()}})
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), '0o600')
        publications = self.journal()['publications']
        [add] = publications['claude_desktop_add']
        command, args = self.expected()
        owned = codex_registration.Receipt.from_bytes(json.dumps(add['ownership']).encode())
        self.assertEqual((owned.client, owned.scope, owned.pre_subtree, owned.pre_subtree_hash),
                         ('claude-desktop', 'user', None, None))
        self.assertEqual(owned.post_subtree_hash, hashlib.sha256(desktop.subtree(command, args)).hexdigest())
        self.assertEqual((add['existed'], add['candidate_sha256']), (False, hashlib.sha256(self.config.read_bytes()).hexdigest()))
        self.assertEqual(add['source_sha256'], hashlib.sha256(b'').hexdigest())
        [write] = publications['claude_desktop_write']
        from telegram_search_mcp.bundle_paths import object_binding
        self.assertEqual(write['object'], object_binding(self.config))
        self.assertEqual(publications['claude_desktop'], [{'status': 'registered'}])

    def test_neighbours_and_their_secrets_are_preserved_and_never_logged_or_copied(self):
        self.present(mode=0o755)
        before = self.write_config(NEIGHBOUR_TEXT)
        original = json.loads(before)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = self.install(self.packet)
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        self.assertCompletedWith(result, 'registered')
        after = self.document()
        self.assertEqual(after['mcpServers']['telegram_search'], self.ours())
        self.assertEqual(canonical(without_ours(after)), canonical(original))  # Values, order and lexemes.
        self.assertEqual(list(after), list(original))
        self.assertEqual(list(after['mcpServers']), ['filesystem', 'weather', 'telegram_search'])
        text = self.config.read_text()
        for lexeme in ('12345678901234567890', '1e+21', '1.5', '-3', '0.25'):
            self.assertIn(lexeme, text)
        self.assertTrue(text.startswith('{\n  "globalShortcut": "Alt+Space",\n') and text.endswith('}\n'))
        self.assertNotIn(SECRET, stream.getvalue() + json.dumps(result))
        copies = [p for p in self.base.rglob('*') if p.is_file() and SECRET.encode() in p.read_bytes()]
        self.assertEqual(copies, [self.config])

    def test_exact_existing_entry_is_reused_without_any_write(self):
        for label in ('plain', 'empty env'):
            with self.subTest(label):
                self.setUp()
                command, args = self.expected()
                value = {'command': command, 'args': args, **({'env': {}} if label == 'empty env' else {})}
                self.present()
                before = self.write_config(json.dumps({'mcpServers': {'telegram_search': value, 'other': {'command': '/o'}}}))
                inode = self.config.stat().st_ino
                with self.replacements() as replaced:
                    result = self.install(self.packet)
                self.assertCompletedWith(result, 'reused')
                self.assertEqual((self.config.read_bytes(), self.config.stat().st_ino, replaced), (before, inode, []))
                self.assertNotIn('claude_desktop_add', self.journal()['publications'])

    def test_exact_entry_beside_unusual_number_lexemes_is_still_reused(self):
        self.present()
        command, args = self.expected()
        before = self.write_config('{"zoom": 1.10, "mcpServers": {"telegram_search": %s}}'
                                   % json.dumps({'command': command, 'args': args}))
        self.assertCompletedWith(self.install(self.packet), 'reused')
        self.assertEqual(self.config.read_bytes(), before)

    def test_without_the_claude_directory_it_is_skipped_and_nothing_is_created(self):
        result = self.install(self.packet)
        self.assertCompletedWith(result, 'skipped')
        self.assertFalse((self.home / 'Library').exists())
        self.assertNotIn('claude_desktop_add', self.journal()['publications'])

    # --- refusals before anything exists ----------------------------------------------------

    def different_entries(self):
        command, args = self.expected()
        return {
            'other command': {'command': '/foreign/python3', 'args': args},
            'other args': {'command': command, 'args': args + ['--extra']},
            'args joined': {'command': command, 'args': ' '.join(args)},
            'environment': {'command': command, 'args': args, 'env': {'EXAMPLE': 'value'}},
            'environment not an object': {'command': command, 'args': args, 'env': []},
            'type key': {'type': 'stdio', 'command': command, 'args': args},
            'url key': {'command': command, 'args': args, 'url': 'https://example.invalid/mcp'},
            'no args': {'command': command},
            'not an object': 'telegram_search',
            'null': None,
        }

    def test_a_different_entry_refuses_the_whole_install_before_creation_and_is_never_modified(self):
        for label in list(self.different_entries()):
            with self.subTest(label):
                self.setUp()
                self.present()
                fake = self.claude()
                document = {'mcpServers': {'telegram_search': self.different_entries()[label],
                                           'neighbour': {'command': '/n', 'env': {'TOKEN': SECRET}}}}
                before = self.write_config(json.dumps(document, indent=2))
                inode = self.config.stat().st_ino
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'claude_desktop_registration_conflict')
                self.assertEqual((self.config.read_bytes(), self.config.stat().st_ino), (before, inode))
                self.assertNothingCreated()
                self.assertEqual(fake.calls, [])
                for text in ('/foreign', 'example.invalid', 'EXAMPLE', SECRET):
                    self.assertNotIn(text, repr(caught.exception.args) + str(caught.exception))

    def test_a_neighbour_a_write_could_not_keep_exactly_refuses_before_creation(self):
        cases = {lexeme: '{"mcpServers": {"x": {"command": "/x", "env": {"N": %s}}}}' % lexeme for lexeme in UNSUPPORTED_LEXEMES}
        cases['lone surrogate'] = '{"name": "\\ud800"}'
        for label, text in cases.items():
            with self.subTest(label):
                self.setUp()
                self.present()
                before = self.write_config(text)
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'claude_desktop_config_unsupported')
                self.assertEqual(self.config.read_bytes(), before)
                self.assertNothingCreated()

    # --- unavailable never blocks -------------------------------------------------------------

    def test_unreadable_unsafe_or_invalid_config_completes_as_unavailable_untouched(self):
        valid = json.dumps({'mcpServers': {}})
        def symlink(path):
            (self.base / 'elsewhere.json').write_text(valid); path.symlink_to(self.base / 'elsewhere.json')
        def hardlink(path):
            path.write_text(valid); path.chmod(0o644); os.link(path, self.base / 'alias.json')
        cases = {'not json': lambda p: p.write_text('{"mcpServers": {'),
                 'empty': lambda p: p.write_text(''),
                 'byte order mark': lambda p: p.write_bytes(b'\xef\xbb\xbf' + valid.encode()),
                 'not utf-8': lambda p: p.write_bytes(b'{"a": "\xff"}'),
                 'duplicate keys': lambda p: p.write_text('{"mcpServers": {}, "mcpServers": {}}'),
                 'duplicate server': lambda p: p.write_text('{"mcpServers": {"telegram_search": 1, "telegram_search": 2}}'),
                 'constant': lambda p: p.write_text('{"mcpServers": {}, "x": NaN}'),
                 'top-level array': lambda p: p.write_text('[]'),
                 'servers not an object': lambda p: p.write_text('{"mcpServers": []}'),
                 'deeply nested': lambda p: p.write_text('{"a": ' + '[' * 100000 + ']' * 100000 + '}'),
                 'nested past the bound': lambda p: p.write_text('{"a": ' + '[' * 64 + ']' * 64 + '}'),
                 'symlink': symlink, 'hard link': hardlink, 'directory': lambda p: p.mkdir(mode=0o700)}
        modes = {'group writable': 0o664, 'world writable': 0o646, 'executable': 0o744, 'unreadable': 0o000,
                 'set-id': 0o4644}
        cases.update({label: (lambda mode: lambda p: (p.write_text(valid), p.chmod(mode)))(mode) for label, mode in modes.items()})
        for label, arrange in cases.items():
            with self.subTest(label):
                self.setUp()
                self.present()
                path = self.config
                arrange(path)
                if label not in modes and label not in ('symlink', 'directory', 'hard link'): path.chmod(0o644)
                def snapshot():
                    return os.readlink(path) if path.is_symlink() else (path.lstat().st_mode, path.lstat().st_ino,
                                                                         None if path.is_dir() else None)
                before = snapshot()
                content = None if path.is_symlink() or path.is_dir() or label == 'unreadable' else path.read_bytes()
                self.assertCompletedWith(self.install(self.packet), 'unavailable')
                self.assertEqual(snapshot(), before)
                if content is not None: self.assertEqual(path.read_bytes(), content)
                self.assertNotIn('claude_desktop_add', self.journal()['publications'])

    def test_oversized_or_unsafe_directory_is_unavailable(self):
        self.present()
        self.write_config(json.dumps({'mcpServers': {}, 'pad': 'x' * 64}))
        with mock.patch.object(desktop, 'MAX_CONFIG_BYTES', 32):
            self.assertCompletedWith(self.install(self.packet), 'unavailable')
        for label in ('group writable directory', 'symlinked directory'):
            with self.subTest(label):
                self.setUp()
                if label == 'symlinked directory':
                    real = self.base / 'real-claude'
                    real.mkdir(mode=0o700)
                    self.desktop_dir.parent.mkdir(mode=0o700, parents=True)
                    self.desktop_dir.symlink_to(real, target_is_directory=True)
                else:
                    self.present(mode=0o775)
                self.assertCompletedWith(self.install(self.packet), 'unavailable')
                self.assertEqual([p.name for p in (self.base / 'real-claude').iterdir()] if label == 'symlinked directory' else
                                 [p.name for p in self.desktop_dir.iterdir()], [])

    def test_owner_or_modes_electron_writes_are_accepted_and_kept(self):
        for mode in (0o600, 0o604, 0o640, 0o644):
            with self.subTest(mode=oct(mode)):
                self.setUp()
                self.present()
                self.write_config('{}', mode=mode)
                self.assertCompletedWith(self.install(self.packet), 'registered')
                self.assertEqual(self.document(), {'mcpServers': {'telegram_search': self.ours()}})
                self.assertEqual(oct(self.config.stat().st_mode & 0o777), oct(mode))  # Electron's 0644 survives.

    # --- changes during the transaction ----------------------------------------------------

    def test_a_different_entry_appearing_mid_transaction_is_failed_and_never_touched(self):
        self.present()
        foreign = json.dumps({'mcpServers': {'telegram_search': {'command': '/foreign'}}})
        def appear(stage):
            if stage == 'claude_code': self.write_config(foreign)
        with self.replacements() as replaced:
            result = self.install(self.packet, checkpoint=appear)
        self.assertCompletedWith(result, 'failed')
        self.assertEqual((self.config.read_text(), replaced), (foreign, []))

    def test_an_unsupported_neighbour_appearing_mid_transaction_is_unavailable_and_never_touched(self):
        self.present()
        text = '{"zoom": 1.10}'
        def appear(stage):
            if stage == 'claude_code': self.write_config(text)
        self.assertCompletedWith(self.install(self.packet, checkpoint=appear), 'unavailable')
        self.assertEqual(self.config.read_text(), text)

    def test_a_concurrent_rewrite_during_our_write_wins_and_the_outcome_is_failed(self):
        self.present()
        self.write_config(NEIGHBOUR_TEXT)
        rewritten = NEIGHBOUR_TEXT.replace('"Alt+Space"', '"Ctrl+Space"')
        def rewrite(stage):
            if stage == 'recorded:claude_desktop_write': self.write_config(rewritten)
        result = self.install(self.packet, checkpoint=rewrite)
        self.assertCompletedWith(result, 'failed')
        self.assertEqual(self.config.read_text(), rewritten)
        self.assertEqual(sorted(p.name for p in self.desktop_dir.iterdir()), [desktop.CONFIG_NAME])  # No temporary left.

    # --- crash and resume ------------------------------------------------------------------

    def all_checkpoints(self):
        seen = []
        self.present()
        self.write_config(NEIGHBOUR_TEXT)
        self.install(self.packet, checkpoint=seen.append)
        return list(dict.fromkeys(seen))

    def test_crash_and_resume_at_every_checkpoint_never_double_writes(self):
        stages = self.all_checkpoints()
        self.assertTrue(set(DESKTOP_CHECKPOINTS) <= set(stages), stages)
        self.assertLess(stages.index('claude_code'), stages.index('recorded:claude_desktop_add'))
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                self.present()
                original = json.loads(self.write_config(NEIGHBOUR_TEXT))
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.replacements() as replaced:
                    with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
                    result = self.install(self.packet)
                    again = self.install(self.packet)
                # The write-ahead window before our replace never writes again on resume.
                status = 'failed' if stage in ('recorded:claude_desktop_add', 'recorded:claude_desktop_write') else 'registered'
                self.assertCompletedWith(result, status)
                self.assertEqual((again['installation'], again['claude_desktop']), ('reused', status))
                self.assertEqual(len(replaced), 1 if status == 'registered' else 0)
                after = self.document()
                self.assertEqual(canonical(without_ours(after)), canonical(original))
                self.assertEqual('telegram_search' in after['mcpServers'], status == 'registered')
                self.assertEqual(sorted(p.name for p in self.desktop_dir.iterdir()), [desktop.CONFIG_NAME])

    def test_resume_after_the_outcome_is_recorded_never_reads_the_desktop_config_again(self):
        for stage in ('claude_desktop', 'created:packet:receipt', 'recorded:receipt', 'receipt'):
            with self.subTest(stage=stage):
                self.setUp()
                self.present()
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError): self.install(self.packet, checkpoint=crash)
                self.write_config('{')  # The file breaks between crash and resume.
                with mock.patch.object(desktop, 'observe', side_effect=AssertionError('resume must not read')):
                    for _ in range(2):
                        self.assertCompletedWith(self.install(self.packet), 'registered')

    def test_the_receipt_is_published_only_after_the_desktop_outcome_is_recorded(self):
        self.present()
        order = []
        def watch(stage):
            order.append(stage)
            if stage == 'receipt':
                self.assertEqual(self.journal()['publications']['claude_desktop'], [{'status': 'registered'}])
        self.install(self.packet, checkpoint=watch)
        self.assertLess(order.index('claude_code'), order.index('claude_desktop'))
        self.assertLess(order.index('claude_desktop'), order.index('receipt'))

    # --- rollback: only our exact write, only before its outcome is durable --------------------

    def test_a_refusal_right_after_our_write_restores_the_preimage_and_resume_never_rewrites(self):
        for existed in (True, False):
            with self.subTest(existed=existed):
                self.setUp()
                self.present()
                before = self.write_config(NEIGHBOUR_TEXT) if existed else None
                def fail(stage):
                    if stage == 'published:claude_desktop_add': raise OSError('simulated disk failure')
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet, checkpoint=fail)
                self.assertEqual(str(caught.exception), 'installation_refused')
                if existed:
                    self.assertEqual(self.config.read_bytes(), before)
                    self.assertEqual(oct(self.config.stat().st_mode & 0o777), '0o644')  # Restore keeps the mode too.
                else: self.assertFalse(self.config.exists())
                with self.replacements() as replaced:
                    self.assertCompletedWith(self.install(self.packet), 'failed')
                self.assertEqual(replaced, [])
                if existed: self.assertEqual(self.config.read_bytes(), before)
                else: self.assertFalse(self.config.exists())

    def test_rollback_never_touches_a_file_changed_since_or_an_entry_it_did_not_write(self):
        self.present()
        self.write_config(NEIGHBOUR_TEXT)
        def change_then_fail(stage):
            if stage == 'published:claude_desktop_add':
                document = self.document()
                document['addedBySomeoneElse'] = True
                self.write_config(json.dumps(document, indent=2))
                raise OSError('simulated disk failure')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=change_then_fail)
        self.assertEqual(self.document()['mcpServers']['telegram_search'], self.ours())
        self.assertTrue(self.document()['addedBySomeoneElse'])
        # A pre-existing exact entry is reused, never ours to remove.
        self.setUp()
        self.present()
        before = self.write_config(json.dumps({'mcpServers': {'telegram_search': self.ours()}}))
        def fail(stage):
            if stage == 'claude_desktop': raise OSError('simulated disk failure')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=fail)
        self.assertEqual(self.config.read_bytes(), before)

    def test_a_refusal_after_the_outcome_is_recorded_keeps_the_entry_and_resume_completes(self):
        self.present()
        def fail(stage):
            if stage == 'claude_desktop': raise OSError('simulated disk failure after registration')
        with self.assertRaises(self.bundle.InstallError): self.install(self.packet, checkpoint=fail)
        self.assertEqual(self.document()['mcpServers']['telegram_search'], self.ours())
        self.assertFalse((self.runtime / 'current.json').exists())
        with self.replacements() as replaced:
            self.assertCompletedWith(self.install(self.packet), 'registered')
        self.assertEqual(replaced, [])

    def test_repeat_install_reuses_the_recorded_outcome_without_reading_the_config(self):
        self.present()
        self.assertCompletedWith(self.install(self.packet), 'registered')
        self.write_config(json.dumps({'mcpServers': {'telegram_search': {'command': '/foreign'}}}))
        with mock.patch.object(desktop, 'observe', side_effect=AssertionError('repeat must not read')):
            again = self.install(self.packet)
        self.assertEqual((again['installation'], again['claude_desktop']), ('reused', 'registered'))

    def killed_at_our_temporary(self):
        """A real kill (no exception, no `finally`): a child process exits at the journaled temporary."""
        self.claude()
        pid = os.fork()
        if pid == 0:
            try:
                def kill(stage):
                    if stage == 'recorded:claude_desktop_write': os._exit(9)
                self.install(self.packet, checkpoint=kill)
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 9)
        [temporary] = [p for p in self.desktop_dir.iterdir() if p.name != desktop.CONFIG_NAME]
        return temporary

    def test_a_kill_after_our_temporary_is_journaled_leaves_it_and_resume_unlinks_it(self):
        self.present()
        before = self.write_config(NEIGHBOUR_TEXT)
        temporary = self.killed_at_our_temporary()
        self.assertEqual(temporary.name, self.journal()['publications']['claude_desktop_write'][0]['temporary'])
        self.assertEqual(self.config.read_bytes(), before)
        with self.replacements() as replaced:
            result = self.install(self.packet)
        self.assertCompletedWith(result, 'failed')  # Never written again on resume.
        self.assertEqual((replaced, self.config.read_bytes()), ([], before))
        self.assertEqual(sorted(p.name for p in self.desktop_dir.iterdir()), [desktop.CONFIG_NAME])

    def test_a_temporary_that_is_no_longer_our_exact_inode_is_never_unlinked(self):
        self.present()
        self.write_config(NEIGHBOUR_TEXT)
        temporary = self.killed_at_our_temporary()
        temporary.unlink()
        temporary.write_bytes(b'someone else wrote this name')
        temporary.chmod(0o600)
        self.assertCompletedWith(self.install(self.packet), 'failed')
        self.assertEqual(temporary.read_bytes(), b'someone else wrote this name')

    # --- receipt v2 and read-only drift --------------------------------------------------------

    def test_the_receipt_carries_the_desktop_ownership_binding(self):
        self.present()
        self.install(self.packet)
        receipt = self.receipt()
        [add] = self.journal()['publications']['claude_desktop_add']
        self.assertEqual((receipt['schema_version'], receipt['transaction'], receipt['previous'], receipt['adopted']),
                         (2, 'install_' + self.packet[3][:24], None, {}))
        self.assertEqual(receipt['claude_desktop_ownership'], add['ownership'])
        # Not ours (reused or skipped): no ownership.
        for arrange in ('reused', 'skipped'):
            with self.subTest(arrange):
                self.setUp()
                if arrange == 'reused':
                    self.present()
                    self.write_config(json.dumps({'mcpServers': {'telegram_search': self.ours()}}))
                self.install(self.packet)
                self.assertIsNone(self.receipt()['claude_desktop_ownership'])

    def test_v1_receipts_stay_admitted_and_v2_shapes_are_closed(self):
        from telegram_search_mcp import bundle_runtime
        self.present()
        selection = self.selected()
        path = self.runtime / 'receipt.json'
        original = json.loads(path.read_text())
        v1 = {k: v for k, v in original.items() if k not in ('transaction', 'claude_desktop_ownership', 'previous', 'adopted')}
        path.write_text(json.dumps({**v1, 'schema_version': 1}))
        selection.check()
        cases = {'v1 with a v2 key': {**v1, 'schema_version': 1, 'previous': None},
                 'v2 missing a key': {**v1, 'schema_version': 2},
                 'schema 3': {**original, 'schema_version': 3},
                 'transaction grammar': {**original, 'transaction': 'change_0'},
                 'other install transaction': {**original, 'transaction': 'install_' + 'f' * 24},
                 'ownership client': {**original, 'claude_desktop_ownership': {
                     **original['claude_desktop_ownership'], 'client': 'codex'}},
                 'adopted client': {**original, 'adopted': {'other': {'sha256': 'a' * 64, 'bytes': 1, 'transaction': 'change_1'}}},
                 'previous root': {**original, 'previous': {'index_sha256': 'c' * 64, 'release_root': '/elsewhere',
                                                            'package_version': '0.52.9', 'transaction': 'change_1'}}}
        for label, change in cases.items():
            with self.subTest(label):
                path.write_text(json.dumps(change))
                with self.assertRaises(bundle_runtime.BundleError): selection.check()

    def test_status_reports_desktop_drift_read_only(self):
        self.present()
        self.install(self.packet)
        status = lambda: self.bundle.client_status(self.runtime, self.home)
        with mock.patch.object(codex_registration, '_atomic_write', side_effect=AssertionError('status never writes')):
            self.assertEqual(status(), {'claude_desktop': 'ok'})
            # Claude Desktop rewrote its config from memory: our entry is gone, a neighbour arrived.
            self.write_config(json.dumps({'mcpServers': {'other': {'command': '/o'}}}), mode=0o644)
            before = (self.config.read_bytes(), self.config.stat().st_ino)
            self.assertEqual(status(), {'claude_desktop': 'drift',
                                        'claude_desktop_manual_entry': {'mcpServers': {'telegram_search': self.ours()}}})
            self.assertEqual((self.config.read_bytes(), self.config.stat().st_ino), before)
            self.write_config(json.dumps({'mcpServers': {'telegram_search': {'command': '/foreign'}}}), mode=0o644)
            self.assertEqual(status()['claude_desktop'], 'drift')
            self.write_config('{', mode=0o644)
            self.assertEqual(status(), {'claude_desktop': 'unavailable'})
        self.setUp()
        self.install(self.packet)  # Skipped at install time.
        self.assertEqual(status(), {'claude_desktop': 'skipped'})
        self.present()
        self.assertEqual(status(), {'claude_desktop': 'not_registered'})
        (self.runtime / 'current.json').write_text('{}')
        self.assertEqual(status(), {'claude_desktop': 'unavailable'})

    def test_the_launcher_status_action_adds_the_drift_report(self):
        import importlib.util
        from telegram_search_mcp import setup_cli
        self.present()
        result = self.install(self.packet)
        spec = importlib.util.spec_from_file_location('desktop_status_launcher',
                                                      Path(__file__).parents[1] / 'packaging/launcher.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        release = Path(result['release_root'])
        def fake_status(args):
            print(json.dumps({'state': 'new', 'broker': 'not_running'}))
            return 0
        out = io.StringIO()
        with mock.patch.object(setup_cli, 'main', side_effect=fake_status), \
                mock.patch.object(Path, 'home', return_value=self.home), contextlib.redirect_stdout(out):
            self.assertEqual(launcher.status(release), 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {'state': 'new', 'broker': 'not_running', 'clients': {'claude_desktop': 'ok'}})

    # --- the launch never depends on Claude Desktop ------------------------------------------

    def selected(self):
        from telegram_search_mcp import bundle_runtime
        result = self.install(self.packet)
        selection = bundle_runtime.Selection(Path(result['release_root']), self.packet[3])
        patcher = mock.patch.object(bundle_runtime, '_SELECTION', selection)
        patcher.start()
        self.addCleanup(patcher.stop)
        return selection

    def test_the_receipt_status_is_shape_checked_and_the_config_is_never_read_at_launch(self):
        from telegram_search_mcp import bundle_runtime
        self.present()
        selection = self.selected()
        path = self.runtime / 'receipt.json'
        original = json.loads(path.read_text())
        self.assertEqual(original['claude_desktop'], 'registered')
        self.config.unlink()  # The user removes the config: a launch must not notice.
        with mock.patch.object(desktop, 'observe', side_effect=AssertionError('no read')), \
                mock.patch.object(desktop, 'read', side_effect=AssertionError('no read')):
            for value in bundle_runtime.CLAUDE_DESKTOP_STATUSES:
                path.write_text(json.dumps({**original, 'claude_desktop': value}))
                selection.check()
            for value in ('', 'not_found', 'plugin', None, True, ['registered']):
                with self.subTest(value=value):
                    path.write_text(json.dumps({**original, 'claude_desktop': value}))
                    with self.assertRaises(bundle_runtime.BundleError): selection.check()
            path.write_text(json.dumps({k: v for k, v in original.items() if k != 'claude_desktop'}))
            with self.assertRaises(bundle_runtime.BundleError): selection.check()

    def test_the_claude_directory_overlapping_the_runtime_or_skill_roots_is_refused(self):
        for label in ('runtime inside', 'skills inside', 'skills containing'):
            with self.subTest(label):
                self.setUp()
                if label == 'runtime inside': self.runtime = self.desktop_dir / 'runtime'
                elif label == 'skills inside': self.skills = self.desktop_dir / 'skills'
                else: self.skills = self.home / 'Library'
                with self.assertRaises(self.bundle.InstallError) as caught:
                    self.install(self.packet)
                self.assertEqual(str(caught.exception), 'overlapping_paths')


class DesktopAdapterTests(unittest.TestCase):
    """The module alone, inside one disposable home per case."""
    COMMAND = '/r e/python/bin/python3'
    ARGS = ['-I', '-S', '-B', '/r e/launcher.py', 'mcp']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def home_with(self, text=None, mode=0o644, present=True):
        home = self.root / ('home-%d' % len(list(self.root.iterdir())))
        home.mkdir(mode=0o700)
        if present:
            desktop.directory(home).mkdir(mode=0o700, parents=True)
        if text is not None:
            desktop.config_path(home).write_bytes(text.encode() if type(text) is str else text)
            desktop.config_path(home).chmod(mode)
        return home

    def state(self, home):
        return desktop.state(home, self.COMMAND, self.ARGS)

    def test_the_state_table(self):
        ours = {'command': self.COMMAND, 'args': self.ARGS}
        def doc(value): return json.dumps({'mcpServers': {'telegram_search': value}})
        table = {
            'absent': [None, '{}', '{"mcpServers": {}}', '{"mcpServers": {"other": {}}}', '{"zoom": 1.5, "n": -2}'],
            'exact': [doc(ours), doc({**ours, 'env': {}}), '{"zoom": 1.10, "mcpServers": {"telegram_search": %s}}' % json.dumps(ours)],
            'different': [doc({**ours, 'env': {'A': 'b'}}), doc({**ours, 'type': 'stdio'}), doc({'command': self.COMMAND}),
                          doc({'command': self.COMMAND, 'args': ' '.join(self.ARGS)}), doc(None), doc([]), doc(1)],
            'unavailable': ['', '{', '[]', '{"mcpServers": 1}', '{"a": 1, "a": 2}', '{"a": Infinity}', b'\xff'],
            'unsupported': ['{"zoom": %s}' % lexeme for lexeme in UNSUPPORTED_LEXEMES],
        }
        for expected, texts in table.items():
            for text in texts:
                with self.subTest(expected=expected, text=str(text)[:60]):
                    self.assertEqual(self.state(self.home_with(text)), expected)
        self.assertEqual(self.state(self.home_with(present=False)), 'skipped')
        self.assertEqual(set(desktop.STATES), set(table) | {'skipped'})

    def test_modes_owner_and_links(self):
        good = '{}'
        for mode in (0o600, 0o604, 0o640, 0o644):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(self.state(self.home_with(good, mode)), 'absent')
        for mode in (0o664, 0o646, 0o606, 0o700, 0o744, 0o400, 0o000, 0o2644):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(self.state(self.home_with(good, mode)), 'unavailable')
        with mock.patch.object(codex_registration.os, 'getuid', return_value=os.getuid() + 1):
            self.assertEqual(self.state(self.home_with(good)), 'unavailable')
        linked = self.home_with()
        (self.root / 'real.json').write_text(good)
        desktop.config_path(linked).symlink_to(self.root / 'real.json')
        self.assertEqual(self.state(linked), 'unavailable')

    def test_a_planned_change_is_our_entry_plus_the_exact_neighbours_in_claude_desktop_layout(self):
        home = self.home_with('{"b": 1, "a": {"y": 2.5, "x": []}}')
        state, change = desktop.observe(home, self.COMMAND, self.ARGS)
        self.assertEqual(state, 'absent')
        self.assertTrue(change.existed)
        self.assertEqual(change.candidate.decode(), json.dumps(
            {'b': 1, 'a': {'y': 2.5, 'x': []}, 'mcpServers': {'telegram_search': {'command': self.COMMAND, 'args': self.ARGS}}},
            indent=2, ensure_ascii=False) + '\n')
        self.assertNotIn(b'{"b"', repr(change).encode())
        empty = self.home_with()
        state, change = desktop.observe(empty, self.COMMAND, self.ARGS)
        self.assertEqual((state, change.existed, change.source), ('absent', False, b''))

    def test_write_is_compare_before_replace_and_restore_only_undoes_our_exact_object(self):
        home = self.home_with('{"keep": true}')
        state, change = desktop.observe(home, self.COMMAND, self.ARGS)
        seen = []
        desktop.write(home, change, seen.append)
        path = desktop.config_path(home)
        self.assertEqual(path.read_bytes(), change.candidate)
        binding = {'kind': 'file', **{k: seen[0][k] for k in ('dev', 'ino', 'uid', 'mode', 'bytes')}, 'nlink': 1}
        self.assertTrue(desktop.restore(home, change, binding))
        self.assertEqual(path.read_bytes(), b'{"keep": true}')
        self.assertFalse(desktop.restore(home, change, binding))  # Already undone.
        # Stale source bytes: nothing is written.
        path.chmod(0o600)
        path.write_bytes(b'{"keep": false}')
        with self.assertRaises(codex_registration.ConfigConflict):
            desktop.write(home, change, seen.append)
        self.assertEqual(path.read_bytes(), b'{"keep": false}')
        # Ours, then replaced by an identical copy (another inode): not ours to remove.
        fresh = self.home_with()
        state, change = desktop.observe(fresh, self.COMMAND, self.ARGS)
        records = []
        desktop.write(fresh, change, records.append)
        binding = {'kind': 'file', **{k: records[0][k] for k in ('dev', 'ino', 'uid', 'mode', 'bytes')}, 'nlink': 1}
        target = desktop.config_path(fresh)
        copy = target.with_name('copy')
        copy.write_bytes(target.read_bytes())
        copy.chmod(0o600)
        os.replace(copy, target)
        self.assertFalse(desktop.restore(fresh, change, binding))
        self.assertTrue(target.exists())

    def test_manual_entry_is_nonsecret_json(self):
        self.assertEqual(desktop.manual_entry(self.COMMAND, self.ARGS),
                         {'mcpServers': {'telegram_search': {'command': self.COMMAND, 'args': self.ARGS}}})
        self.assertEqual(desktop.subtree(self.COMMAND, self.ARGS),
                         json.dumps(desktop.manual_entry(self.COMMAND, self.ARGS), sort_keys=True, separators=(',', ':')).encode())


class ReceiptAndReadModeTests(unittest.TestCase):
    """The generalized ownership receipt and the parameterized read mode in codex_registration."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_codex_reads_still_require_exactly_0600(self):
        path = self.root / 'config.toml'
        path.write_bytes(b'model = "example"\n')
        for mode in (0o644, 0o640, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with self.assertRaises(codex_registration.ConfigConflict):
                    codex_registration.read_config(path)
        path.chmod(0o600)
        self.assertEqual(codex_registration.read_config(path), b'model = "example"\n')
        fd = codex_registration._open_parent(path)
        try:
            for mode in (0o666, 0o777, 0o664):
                with self.subTest(max_mode=oct(mode)), self.assertRaises(codex_registration.ConfigConflict):
                    codex_registration._read_at(fd, path.name, 1024, mode)
        finally:
            os.close(fd)

    def test_a_claude_desktop_receipt_has_no_preimage_and_never_authorizes_codex_changes(self):
        owned = desktop.receipt('install_test', 'first_install', '/r/python/bin/python3',
                                ['-I', '-S', '-B', '/r/launcher.py', 'mcp'])
        self.assertEqual(codex_registration.Receipt.from_bytes(owned.to_bytes()), owned)
        self.assertEqual(json.loads(owned.to_bytes())['client'], 'claude-desktop')
        preimage = b'[mcp_servers]\ntelegram_search = {command = "/x"}\n'
        for kwargs in ({'client': 'claude-desktop', 'pre_subtree': preimage,
                        'pre_subtree_hash': hashlib.sha256(preimage).hexdigest()},
                       {'client': 'claude-code'}, {'client': 'other'}, {'client': None}):
            with self.subTest(kwargs=str(kwargs)[:40]):
                values = dict(installation_id='install_test', transaction_id='first', pre_subtree_hash=None,
                              post_subtree_hash='a' * 64, pre_subtree=None)
                values.update(kwargs)
                with self.assertRaises(codex_registration.ConfigConflict):
                    codex_registration.Receipt(**values)
        desired = codex_registration.Registration('/owned/python/bin/python3', ('-I', '-S', '-B', '/owned/launcher.py', 'mcp'),
                                                  {}, ('_manifest',))
        source = codex_registration.merge_registration(b'', desired).candidate
        forged = codex_registration.Receipt('install_test', 'first', None,
            hashlib.sha256(codex_registration._subtree(codex_registration._owned(codex_registration._parse(source)))).hexdigest(),
            None, client='claude-desktop')
        with self.assertRaises(codex_registration.ConfigConflict) as caught:
            codex_registration.merge_registration(source, desired, forged)
        self.assertEqual(caught.exception.code, 'ownership_conflict')


if __name__ == '__main__': unittest.main()
