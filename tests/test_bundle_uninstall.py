"""Uninstall removes only owned objects (CLIENTS-13), over disposable homes only.

launchctl, the claude CLI and the pending-send probe are fakes; the session, enrollment and Keychain are
never touched. No test reads or writes the real ~/Library or a real client configuration.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from claude_fake import entry
from test_bundle_upgrade import ChangeHarness, NEIGHBOUR_CODEX, WRITE_AHEAD
from telegram_search_mcp import claude_desktop_registration as desktop


class UninstallTests(ChangeHarness, unittest.TestCase):

    def installed(self, *, upgrade=False):
        packet, result = self.full()
        fake = self.claude()
        document = fake.document()
        document['mcpServers']['other'] = entry('/other/server', ['x'])
        fake.write_document(document)
        if upgrade:
            new = self.build('b', launcher=b'# second release\n')
            result = self.install(new)
        return result

    def everything(self):
        """Hashes of the whole session tree (runtime.toml included): never part of an uninstall."""
        return {str(p.relative_to(self.session_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.session_root.rglob('*')) if p.is_file()}

    def assertRemoved(self, result, *, claude=True, desktop_entry=True):
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.runtime.parent / ('.' + self.runtime.name + '.install-intent.json')).exists())
        self.assertEqual([p.name for p in self.runtime.parent.iterdir() if p.name.startswith('.' + self.runtime.name)], [])
        self.assertFalse(self.plist.exists())
        self.assertIsNone(self.launchctl.loaded)
        self.assertEqual((self.codex / 'config.toml').read_bytes().rstrip(b'\n'), NEIGHBOUR_CODEX.rstrip(b'\n'))
        servers = self.claude().document()['mcpServers']
        self.assertEqual('telegram_search' in servers, not claude)
        self.assertEqual(servers['other'], entry('/other/server', ['x']))
        document = json.loads(self.desktop_config.read_text())
        self.assertEqual('telegram_search' in document['mcpServers'], not desktop_entry)
        self.assertEqual(document['mcpServers']['weather'], {'command': '/opt/w', 'args': []})
        self.assertEqual(oct(self.desktop_config.stat().st_mode & 0o777), '0o644')
        for base in (self.skills, self.claude_skills):
            self.assertEqual(list(base.iterdir()), [])

    def test_uninstall_removes_only_owned_objects_and_never_the_session(self):
        for upgrade in (False, True):
            with self.subTest(upgrade=upgrade):
                self.setUp()
                self.installed(upgrade=upgrade)
                before = self.everything()
                with self.no_other_process():
                    result = self.change('uninstall')
                self.assertEqual((result['status'], result['installation'], result['service'], result['codex'],
                                  result['claude_code'], result['claude_desktop']),
                                 ('uninstalled', 'removed', 'removed', 'removed', 'removed', 'removed'))
                self.assertEqual(set(result['skills'].values()), {'removed'})
                self.assertNotIn('legacy_backups', result)
                self.assertRemoved(result)
                self.assertEqual(self.everything(), before)
                self.assertEqual(self.launchctl.verbs().count('bootout'), 2 if upgrade else 1)
                self.assertEqual(self.claude().verbs()[-1], 'remove')
                self.assertEqual(self.change('uninstall'), {'status': 'uninstalled', 'installation': 'absent'})
                # A fresh first install works afterwards.
                again = self.install(self.build('fresh', launcher=b'# fresh\n'))
                self.assertEqual(again['installation'], 'installed')

    def test_foreign_or_drifted_objects_are_kept(self):
        self.installed()
        self.codex_present(NEIGHBOUR_CODEX + b'\n[mcp_servers.telegram_search]\ncommand = "/user/edited"\n')
        self.claude().entry = entry('/user/own/python3', ['mcp'])
        self.desktop_present('{"mcpServers": {"telegram_search": {"command": "/user/own"}}}')
        payload = plistlib.loads(self.plist.read_bytes())
        payload['ProgramArguments'] = ['/foreign/python', '-m', 'telegram_search_mcp.launch_agent', 'run']
        self.plist.write_bytes(plistlib.dumps(payload))
        self.launchctl.load()
        skill = self.skills / 'telegram-search' / 'SKILL.md'
        skill.chmod(0o600)
        skill.write_bytes(b'the user edited it')
        snapshot = {p: p.read_bytes() for p in (self.codex / 'config.toml', self.desktop_config, self.plist, skill,
                                                self.claude().path)}
        result = self.change('uninstall')
        self.assertEqual((result['service'], result['codex'], result['claude_code'], result['claude_desktop']),
                         ('kept', 'kept', 'kept', 'kept'))
        self.assertEqual(result['skills']['telegram-search'], 'kept')
        self.assertEqual(result['skills']['telegram-setup'], 'removed')
        self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertFalse(self.runtime.exists())

    def test_a_pending_send_blocks_uninstall_before_anything_is_removed(self):
        self.installed()
        self.answers = ['pending']
        with self.assertRaises(self.module().InstallError) as caught:
            self.change('uninstall')
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertTrue(self.plist.exists() and (self.runtime / 'current.json').exists())
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertIn('telegram_search', self.claude().document()['mcpServers'])
        self.assertEqual(self.change('uninstall')['installation'], 'removed')

    def test_every_checkpoint_resumes_without_double_effects(self):
        seen = []
        self.installed()
        self.change('uninstall', checkpoint=seen.append)
        stages = list(dict.fromkeys(seen))
        for wanted in ('stopped:service', 'recorded:codex', 'recorded:claude_remove', 'recorded:claude_desktop_write',
                       'removed:telegram-search', 'retired', 'published:retired'):
            self.assertIn(wanted, stages)
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                self.installed()
                before = self.everything()
                calls = len(self.claude().calls)
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError):
                    self.change('uninstall', checkpoint=crash)
                result = self.change('uninstall')
                window = WRITE_AHEAD.get(stage)
                self.assertRemoved(result, claude=window != 'claude', desktop_entry=window != 'desktop')
                self.assertEqual(self.claude().verbs()[calls:], [] if stage == 'recorded:claude_remove' else ['remove'])
                self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
                self.assertEqual(self.everything(), before)
                self.assertEqual(sorted(p.name for p in desktop.directory(self.home).iterdir()), [desktop.CONFIG_NAME])


class ChangeActionLauncherTests(unittest.TestCase):
    """`launcher.py rollback|uninstall`: the installed release admits its own pinned tree, then hands over."""

    def launch(self, argv, *, error=None, layout='releases', pin='a' * 64):
        from telegram_search_mcp import bundle_install
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve() / 'install' / layout / pin
            release.mkdir(parents=True)
            shutil.copy(Path(__file__).parents[1] / 'packaging/launcher.py', release / 'launcher.py')
            spec = importlib.util.spec_from_file_location('change_action_launcher', release / 'launcher.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            calls = []
            def change(*args, **kwargs):
                calls.append((args, kwargs))
                if error is not None: raise error
                return {'status': 'done'}
            saved = list(sys.path)
            try:
                with mock.patch.object(module, 'admit') as admit, \
                        mock.patch.object(bundle_install, 'rollback', side_effect=change), \
                        mock.patch.object(bundle_install, 'uninstall', side_effect=change), \
                        mock.patch.object(module, '_read', side_effect=AssertionError('the receipt is not the admission')), \
                        contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                    code = module.main(argv)
            finally:
                sys.path[:] = saved
            return code, out.getvalue(), err.getvalue(), calls, admit, release

    def test_each_action_admits_its_own_tree_and_runs_the_transaction(self):
        for action in ('rollback', 'uninstall'):
            with self.subTest(action=action):
                code, out, err, calls, admit, release = self.launch([action])
                self.assertEqual((code, json.loads(out), err), (0, {'status': 'done'}, ''))
                admit.assert_called_once_with(release, release / 'bundle-index.json', 'a' * 64, True)
                self.assertEqual(calls, [((release.parent.parent,), {'user_home': Path.home()})])

    def test_fixed_codes_are_echoed_and_anything_else_is_generic(self):
        from telegram_search_mcp.bundle_install import InstallError
        for error, code in ((InstallError('pending_send_unresolved'), 'pending_send_unresolved'),
                            (InstallError('rollback_refused'), 'rollback_refused'),
                            (InstallError('/Users/secret/path'), 'bundle_admission_or_install_failed'),
                            (RuntimeError('secret'), 'bundle_admission_or_install_failed')):
            with self.subTest(code=code):
                result = self.launch(['uninstall'], error=error)
                self.assertEqual(result[:3], (2, '', json.dumps({'status': 'refused', 'code': code}) + '\n'))

    def test_a_tree_outside_releases_or_extra_words_refuse_before_admission(self):
        generic = json.dumps({'status': 'refused', 'code': 'bundle_admission_or_install_failed'}) + '\n'
        for kwargs in ({'layout': 'elsewhere'}, {'pin': 'not-a-hash'}):
            with self.subTest(kwargs=kwargs):
                code, out, err, calls, admit, _ = self.launch(['rollback'], **kwargs)
                self.assertEqual((code, out, err, calls, admit.called), (2, '', generic, [], False))
        code, out, err, calls, admit, _ = self.launch(['rollback', '--force'])
        self.assertEqual((code, calls, admit.called), (2, [], False))


if __name__ == '__main__': unittest.main()
