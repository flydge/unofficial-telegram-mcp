"""Upgrade and rollback of a receipt-bound installation (CLIENTS-13), over disposable homes only.

Every release is a synthetic admitted tree; launchctl, the claude CLI and the pending-send probe are fakes.
No test reads or writes the real ~/Library, a real client configuration, the Keychain or a launchd job.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import unittest
from unittest import mock

from bundle_payload import inert_natives, v1_lock
from test_bundle_install import InstallHarness
from telegram_search_mcp import claude_desktop_registration as desktop, codex_registration

NEIGHBOUR_DESKTOP = '{\n  "globalShortcut": "Alt+Space",\n  "mcpServers": {\n    "weather": {"command": "/opt/w", "args": []}\n  }\n}\n'
NEIGHBOUR_CODEX = b'# preserved neighbor\nmodel = "example"\n'
OLD_VERSION = '0.52.9'
V2_KEYS = ('transaction', 'claude_desktop_ownership', 'previous', 'adopted')
# A crash in a Claude Code write-ahead window before the CLI effect: never retried, the outcome is `failed`.
# Claude Desktop writes are compare-before-replace: an unchanged file gets the same write again on resume.
WRITE_AHEAD = {'recorded:claude_remove': 'claude', 'recorded:claude_add': 'claude'}


class FakeLaunchctl:
    """`launchctl print|bootout|bootstrap` over one label; the loaded job is the plist read at bootstrap.

    `others` maps further labels (a named alias's broker and connect jobs, 0.55 spec 3D) to their plists: a call
    naming one of them acts on that job alone; every other label is the default's, as before. Such a job prints no
    state line (running, as launchd may show a job) until `idle` marks it `state = not running` (a helper that
    exited, its job still registered); loading or booting it out clears that. Like launchd, its state outlives a
    killed (forked) installer process: it is kept in a file.
    """

    def __init__(self, plist, state, others=None):
        self.plist, self.state, self.fail = plist, state, set()
        self.others = {label: Path(path) for label, path in (others or {}).items()}
        self._write({'loaded': None, 'calls': [], **({'jobs': dict.fromkeys(self.others)} if self.others else {})})

    def _value(self):
        return json.loads(self.state.read_text())

    def _write(self, value):
        self.state.write_text(json.dumps(value))

    def _save(self, loaded, calls):
        self._write({**self._value(), 'loaded': loaded, 'calls': calls})

    @property
    def loaded(self):
        return self._value()['loaded']

    @loaded.setter
    def loaded(self, value):
        self._save(value, self.calls)

    @property
    def calls(self):
        return self._value()['calls']

    def job(self, label):
        return self._value()['jobs'][label]

    def set_job(self, label, value):
        state = self._value()
        idle = [name for name in state.get('idle', []) if name != label]
        self._write({**{k: v for k, v in state.items() if k != 'idle'}, 'jobs': {**state['jobs'], label: value},
                     **({'idle': idle} if idle else {})})

    def idle(self, label):
        """The loaded job `label` explicitly idle: its process exited, launchd keeps it registered."""
        state = self._value()
        assert state['jobs'][label] is not None, label
        self._write({**state, 'idle': sorted({*state.get('idle', []), label})})

    def load(self, label=None):
        if label is None:
            self.loaded = plistlib.loads(self.plist.read_bytes())['ProgramArguments']
        else:
            self.set_job(label, plistlib.loads(self.others[label].read_bytes())['ProgramArguments'])

    def verbs(self):
        return [call[0] for call in self.calls]

    def __call__(self, argv, **kwargs):
        assert argv[0] == '/bin/launchctl' and kwargs.get('check') is False and kwargs.get('timeout'), (argv, kwargs)
        verb = argv[1]
        self._save(self.loaded, self.calls + [argv[1:]])
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 5, '', '')
        label = argv[2].rpartition('/')[2] if verb in ('print', 'bootout') else None
        if label in self.others:
            loaded, plist = self.job(label), self.others[label]
        elif self.others and label is not None and label != self.plist.stem:
            return subprocess.CompletedProcess(argv, 113 if verb == 'print' else 3, '', '')  # Another alias's label.
        else:
            loaded, plist, label = self.loaded, self.plist, None
        if verb == 'print':
            if loaded is None:
                return subprocess.CompletedProcess(argv, 113, '', '')
            idle = ['\tstate = not running'] if label is not None and label in self._value().get('idle', []) else []
            text = '\n'.join(['%s = {' % argv[2], '\tpath = %s' % plist, *idle, '\tprogram = %s' % loaded[0],
                              '\targuments = {', *('\t\t' + a for a in loaded), '\t}', '}'])
            return subprocess.CompletedProcess(argv, 0, text, '')
        if verb == 'bootout':
            if loaded is None:
                return subprocess.CompletedProcess(argv, 3, '', '')
            if label is None:
                self.loaded = None
            else:
                self.set_job(label, None)
            return subprocess.CompletedProcess(argv, 0, '', '')
        named = next((other for other, path in self.others.items() if argv[3:4] == [str(path)]), None)
        assert verb == 'bootstrap' and (argv[3] == str(self.plist) or named), argv
        self.load(named)
        return subprocess.CompletedProcess(argv, 0, '', '')


class ChangeHarness(InstallHarness):
    """Two (or more) synthetic releases, a synthetic session and fake service/client boundaries."""

    def setUp(self):
        super().setUp()
        from telegram_search_mcp.config import _owner_paths
        self.session_root, _, self.label, self.plist = _owner_paths(self.home)
        self.launchctl = FakeLaunchctl(self.plist, self.base / 'launchd-state.json')
        self.answers = []
        self.probes = []
        self.module()

    def probe(self, home, python, app):
        self.probes.append((python, app))
        return self.answers.pop(0) if self.answers else 'none'

    def build(self, name, *, launcher=b'# inert unit fixture\n', search=b'search-skill', version='0.57.0rc1'):
        module = self.module()
        release = self.base / name
        release.mkdir(mode=0o700)
        files = {'launcher.py': launcher, 'python/bin/python3': b'inert-runtime',
                 'app/telegram_search_mcp/__init__.py': b'__version__ = "0.57.0rc1"\n',
                 'native/libtdjson.dylib': b'inert-native', 'skills/telegram-search/SKILL.md': search,
                 'skills/telegram-setup/SKILL.md': b'setup-skill',
                 'tdlib-lock.json': json.dumps(v1_lock(inert_natives())).encode()}
        manifest = dict(schema_version=1, package_version=version, target='macos-arm64', minimum_os='26.0',
                        contract_version=1, schema_fingerprint='a' * 64, tools=['_manifest', 'search_correspondence'],
                        native_lock_sha256=hashlib.sha256(files['tdlib-lock.json']).hexdigest(),
                        skills=[dict(id=n, sha256=hashlib.sha256(files['skills/' + n + '/SKILL.md']).hexdigest())
                                for n in ('telegram-search', 'telegram-setup')],
                        source_completeness='incomplete', publication='unreleased')
        files['manifest/release.json'] = json.dumps(manifest).encode()
        records = []
        for path_name, data in sorted(files.items()):
            path = release / path_name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            executable = path_name == 'python/bin/python3'
            path.chmod(0o500 if executable else 0o400)
            records.append(dict(path=path_name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), executable=executable))
        for parent, _, _ in os.walk(release):
            Path(parent).chmod(0o700)
        index = dict(schema_version=1, target='macos-arm64', release_version=version, archive_bytes=1,
                     archive_sha256='b' * 64, unpacked_bytes=sum(len(v) for v in files.values()), files=records)
        index_path = self.base / (name + '-index.json')
        index_path.write_bytes(json.dumps(index).encode())
        index_path.chmod(0o400)
        return module, release, index_path, hashlib.sha256(index_path.read_bytes()).hexdigest()

    def old(self):
        """The installed release, built and installed as an older package version."""
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            packet = self.build('a', version=OLD_VERSION)
            return packet, self.install(packet)

    def install(self, packet, **kwargs):
        kwargs.setdefault('launchctl', self.launchctl)
        kwargs.setdefault('pending_sends', self.probe)
        return super().install(packet, **kwargs)

    def change(self, action, **kwargs):
        module = self.module()
        self.claude()
        kwargs.setdefault('claude_candidates', (self.claude_cli,))
        kwargs.setdefault('claude_runner', self.claude())
        kwargs.setdefault('launchctl', self.launchctl)
        kwargs.setdefault('pending_sends', self.probe)
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return getattr(module, action)(self.runtime, user_home=self.home, **kwargs)

    # --- synthetic client and session state ------------------------------------------------

    @property
    def desktop_config(self):
        return desktop.config_path(self.home)

    def desktop_present(self, text=NEIGHBOUR_DESKTOP, mode=0o644):
        desktop.directory(self.home).mkdir(mode=0o700, parents=True, exist_ok=True)
        if text is not None:
            self.desktop_config.write_text(text)
            self.desktop_config.chmod(mode)

    def codex_present(self, data=NEIGHBOUR_CODEX):
        self.codex.mkdir(mode=0o700, exist_ok=True)
        (self.codex / 'config.toml').write_bytes(data)
        (self.codex / 'config.toml').chmod(0o600)

    def session(self, pin=OLD_VERSION, capabilities='["read", "artifacts", "send"]'):
        self.session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        files = {'enrollment.json': b'{"synthetic": "enrollment"}\n',
                 'runtime.toml': ('config_version = 1\nexpected_package_version = "%s"\nexpected_contract_version = 1\n'
                                  'enabled_capabilities = %s\n' % (pin, capabilities)).encode(),
                 'tdlib/td.binlog': b'synthetic-session-bytes'}
        for name, data in files.items():
            path = self.session_root / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)

    def session_hashes(self):
        return {str(p.relative_to(self.session_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.session_root.rglob('*')) if p.is_file() and p.name != 'runtime.toml'}

    def service(self, release_root, *, loaded=True):
        payload = self.module()._service_payload(Path(release_root), self.label)
        self.plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps(payload))
        self.plist.chmod(0o600)
        if loaded:
            self.launchctl.load()
        return payload

    def receipt(self):
        return json.loads((self.runtime / 'receipt.json').read_text())

    def pointer(self):
        return json.loads((self.runtime / 'current.json').read_text())

    def as_v1(self):
        """Rewrite the current receipt as the v1 form 0.53 wave 2 published (no v2 keys)."""
        value = {k: v for k, v in self.receipt().items() if k not in V2_KEYS}
        value['schema_version'] = 1
        path = self.runtime / 'receipt.json'
        path.write_bytes((json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode())
        path.chmod(0o600)
        return value

    def selection_check(self, version=None):
        from telegram_search_mcp import bundle_runtime
        pointer = self.pointer()
        selection = bundle_runtime.Selection(Path(pointer['release_root']), pointer['index_sha256'])
        with mock.patch('telegram_search_mcp.__version__', version) if version else contextlib.nullcontext():
            selection.check()

    def full(self, *, loaded=True):
        """Old release installed with every client present, a session and an exact loaded broker."""
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755, exist_ok=True)
        self.session()
        packet, result = self.old()
        self.service(result['release_root'], loaded=loaded)
        return packet, result

    @contextlib.contextmanager
    def no_other_process(self):
        """No subprocess outside the injected fakes: the Keychain and launchd are never reached."""
        with mock.patch('subprocess.run', side_effect=AssertionError('unexpected process')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
            yield

    def commands(self, release_root):
        release_root = Path(release_root)
        return str(release_root / 'python/bin/python3'), ['-I', '-S', '-B', str(release_root / 'launcher.py'), 'mcp']

    def assertUpgradedTo(self, release_root, old_root, *, claude=True, desktop=True):
        command, args = self.commands(release_root)
        self.assertEqual(self.pointer()['release_root'], str(release_root))
        if claude:
            self.assertEqual(self.claude().entry, {'type': 'stdio', 'command': command, 'args': args})
        if desktop:
            self.assertEqual(json.loads(self.desktop_config.read_text())['mcpServers']['telegram_search'],
                             {'command': command, 'args': args})
        self.assertEqual(oct(self.desktop_config.stat().st_mode & 0o777), '0o644')
        config = (self.codex / 'config.toml').read_bytes()
        self.assertTrue(config.startswith(NEIGHBOUR_CODEX))
        self.assertIn(command.encode(), config)
        self.assertNotIn(str(old_root).encode(), config)
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist['ProgramArguments'][4], str(Path(release_root) / 'launcher.py'))
        self.assertEqual(json.loads(self.desktop_config.read_text())['mcpServers']['weather'], {'command': '/opt/w', 'args': []})


class UpgradeTests(ChangeHarness, unittest.TestCase):

    def test_upgrade_from_a_v1_receipt_rewrites_only_owned_state(self):
        old_packet, old = self.full()
        self.as_v1()
        before = self.session_hashes()
        policy = (self.session_root / 'runtime.toml').read_bytes()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        fake = self.claude()
        with self.no_other_process():
            result = self.install(new)
        self.assertEqual((result['status'], result['installation'], result['service']), ('upgraded', 'upgraded', 'restarted'))
        self.assertEqual((result['claude_code'], result['claude_desktop'], result['claude_skills']),
                         ('registered', 'registered', 'installed'))
        self.assertEqual(result['claude_desktop_next_action'], 'restart_claude_desktop')
        new_root = self.runtime / 'releases' / new[3]
        self.assertUpgradedTo(new_root, old['release_root'])
        self.assertTrue(Path(old['release_root']).is_dir())  # Kept for rollback.
        self.assertEqual(fake.verbs(), ['add-json', 'remove', 'add-json'])
        # Gated before the transaction, before the first client write and right before the stop.
        self.assertEqual(self.launchctl.verbs(), ['print', 'print', 'print', 'bootout', 'print', 'print', 'bootstrap'])
        self.assertEqual(self.launchctl.loaded[4], str(new_root / 'launcher.py'))
        self.assertEqual([p[0] for p in self.probes], [Path(old['release_root']) / 'python/bin/python3'] * 3)
        self.assertEqual(self.session_hashes(), before)
        self.assertEqual((self.session_root / 'runtime.toml').read_bytes(),
                         policy.replace(OLD_VERSION.encode(), b'0.57.0rc1'))
        for name in ('telegram-search',):
            for base in (self.skills, self.claude_skills):
                self.assertEqual((base / name / 'SKILL.md').read_bytes(), b'search-skill-2')
        receipt = self.receipt()
        self.assertEqual(receipt['schema_version'], 2)
        self.assertEqual(receipt['previous'], {'index_sha256': old_packet[3], 'release_root': old['release_root'],
                                               'package_version': OLD_VERSION, 'transaction': 'install_' + old_packet[3][:24]})
        self.assertEqual(receipt['transaction'], 'change_1')
        self.assertEqual(receipt['claude_desktop_ownership']['transaction_id'], 'change_1')
        self.assertEqual(receipt['adopted'], {})
        self.selection_check()
        again = self.install(new)
        self.assertEqual((again['status'], again['installation']), ('upgraded', 'reused'))
        self.assertEqual(fake.verbs(), ['add-json', 'remove', 'add-json'])

    def test_every_checkpoint_resumes_without_double_effects(self):
        seen = []
        self.full()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        self.install(new, checkpoint=seen.append)
        stages = list(dict.fromkeys(seen))
        for wanted in ('release', 'registration', 'recorded:claude_remove', 'recorded:claude_desktop_write',
                       'stopped:service', 'recorded:plist', 'recorded:policy', 'recorded:receipt', 'published:pointer',
                       'complete', 'restarted'):
            self.assertIn(wanted, stages)
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                _, old = self.full()
                before = self.session_hashes()
                new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                replaced = []
                original = os.replace
                def replace(source, destination, **kwargs):
                    original(source, destination, **kwargs)
                    if type(destination) is str: replaced.append(destination)  # Our dirfd-relative writes only.
                with mock.patch.object(codex_registration.os, 'replace', side_effect=replace):
                    with self.assertRaises(RuntimeError):
                        self.install(new, checkpoint=crash)
                    result = self.install(new)
                self.assertEqual(result['status'], 'upgraded')
                window = WRITE_AHEAD.get(stage)
                self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'],
                                      claude=window != 'claude', desktop=window != 'desktop')
                if window == 'claude':
                    self.assertEqual(result['claude_code'], 'failed')
                    self.assertIn('claude_registration_command', result)
                if window == 'desktop':
                    self.assertEqual(result['claude_desktop'], 'failed')
                    self.assertIn('claude_desktop_manual_entry', result)
                self.assertEqual(self.claude().verbs(), {'recorded:claude_remove': ['add-json'],
                                                         'recorded:claude_add': ['add-json', 'remove']}.get(
                                                             stage, ['add-json', 'remove', 'add-json']))
                self.assertEqual(self.launchctl.verbs().count('bootout'), 1)
                self.assertEqual(self.launchctl.verbs().count('bootstrap'), 1)
                targets = {desktop.CONFIG_NAME, 'config.toml', self.plist.name, 'runtime.toml'} - (
                    {desktop.CONFIG_NAME} if window == 'desktop' else set())
                self.assertEqual(sorted(n for n in replaced if n in targets | {desktop.CONFIG_NAME}), sorted(targets))
                self.assertEqual(self.session_hashes(), before)
                self.assertEqual(sorted(p.name for p in desktop.directory(self.home).iterdir()), [desktop.CONFIG_NAME])
                self.selection_check()
                self.assertEqual(self.install(new)['installation'], 'reused')

    def test_a_foreign_object_refuses_before_any_change(self):
        cases = ('codex', 'claude_code', 'claude_desktop', 'plist', 'skill', 'policy', 'live')
        for kind in cases:
            with self.subTest(kind=kind):
                self.setUp()
                _, old = self.full()
                if kind == 'codex':
                    self.codex_present(NEIGHBOUR_CODEX + b'\n[mcp_servers.telegram_search]\ncommand = "/foreign"\n')
                elif kind == 'claude_code':
                    from claude_fake import entry
                    self.claude().entry = entry('/foreign/python3', ['mcp'])
                elif kind == 'claude_desktop':
                    self.desktop_present('{"mcpServers": {"telegram_search": {"command": "/foreign"}}}')
                elif kind == 'plist':
                    payload = plistlib.loads(self.plist.read_bytes())
                    payload['ProgramArguments'] = ['/foreign/python', '-m', 'telegram_search_mcp.launch_agent', 'run']
                    self.plist.write_bytes(plistlib.dumps(payload))
                elif kind == 'skill':
                    (self.skills / 'telegram-search' / 'SKILL.md').chmod(0o600)
                    target = self.skills / 'telegram-search'
                    os.rename(target, self.base / 'moved')
                    target.mkdir(mode=0o700)
                    (target / 'SKILL.md').write_bytes(b'the user own copy')
                elif kind == 'policy':
                    self.session(pin='9.9.9')
                else:
                    self.launchctl.loaded = ['/foreign/python', 'x']
                snapshot = {p: p.read_bytes() for p in (self.codex / 'config.toml', self.desktop_config, self.plist,
                                                         self.runtime / 'receipt.json', self.session_root / 'runtime.toml')}
                new = self.build('b', launcher=b'# second release\n')
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(new)
                expected = {'codex': 'registration_conflict', 'claude_code': 'claude_registration_conflict',
                            'claude_desktop': 'claude_desktop_registration_conflict', 'plist': 'service_conflict',
                            'skill': 'skill_conflict', 'policy': 'policy_conflict', 'live': 'service_conflict'}[kind]
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
                self.assertFalse((self.runtime / 'releases' / new[3]).exists())
                self.assertFalse((self.runtime / 'transactions' / 'change_1').exists())
                self.assertNotIn('bootout', self.launchctl.verbs())
                self.assertEqual(self.pointer()['release_root'], old['release_root'])

    def test_entries_the_user_removed_are_added_back_like_a_first_install(self):
        _, old = self.full()
        self.codex_present()  # The user dropped our Codex subtree; the neighbours stay.
        self.claude().entry = None
        self.desktop_present()
        new = self.build('b', launcher=b'# second release\n')
        result = self.install(new)
        self.assertEqual((result['claude_code'], result['claude_desktop']), ('registered', 'registered'))
        self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])
        self.assertEqual(self.claude().verbs(), ['add-json', 'add-json'])
        self.selection_check()

    def test_a_pending_send_blocks_the_broker_restart(self):
        for answer in ('pending', 'unknown'):
            with self.subTest(answer=answer):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n')
                self.answers = [answer]
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(new)
                self.assertEqual(str(caught.exception), 'pending_send_unresolved')
                self.assertNotIn('bootout', self.launchctl.verbs())
                self.assertFalse((self.runtime / 'transactions' / 'change_1').exists())
                self.assertEqual(self.pointer()['release_root'], old['release_root'])
        # A send that starts after the pre-check still blocks the stop; the transaction resumes later.
        self.setUp()
        _, old = self.full()
        new = self.build('b', launcher=b'# second release\n')
        self.answers = ['none', 'pending']
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(new)
        self.assertEqual(str(caught.exception), 'pending_send_unresolved')
        self.assertNotIn('bootout', self.launchctl.verbs())
        self.assertEqual(self.pointer()['release_root'], old['release_root'])
        self.assertEqual(self.install(new)['status'], 'upgraded')
        self.assertEqual(self.launchctl.verbs().count('bootout'), 1)

    def test_an_unloaded_broker_is_rewritten_but_never_started_and_an_absent_plist_stays_absent(self):
        _, old = self.full(loaded=False)
        new = self.build('b', launcher=b'# second release\n')
        result = self.install(new)
        self.assertEqual(result['service'], 'not_loaded')
        self.assertEqual(plistlib.loads(self.plist.read_bytes())['ProgramArguments'][4],
                         str(self.runtime / 'releases' / new[3] / 'launcher.py'))
        self.assertNotIn('bootstrap', self.launchctl.verbs())
        self.assertEqual(self.probes, [])
        self.setUp()
        self.codex_present()
        self.session()
        self.old()
        result = self.install(self.build('b', launcher=b'# second release\n'))
        self.assertEqual(result['service'], 'not_loaded')
        self.assertFalse(self.plist.exists())

    def test_policy_capabilities_stay_byte_identical_and_only_the_pin_line_changes(self):
        self.full()
        capabilities = '["artifacts", "read", "send", "forward_send"]'
        self.session(capabilities=capabilities)
        before = (self.session_root / 'runtime.toml').read_bytes()
        self.install(self.build('b', launcher=b'# second release\n'))
        after = (self.session_root / 'runtime.toml').read_bytes()
        changed = [(a, b) for a, b in zip(before.split(b'\n'), after.split(b'\n')) if a != b]
        self.assertEqual(changed, [(b'expected_package_version = "%s"' % OLD_VERSION.encode(),
                                    b'expected_package_version = "0.57.0rc1"')])
        self.assertIn(capabilities.encode(), after)
        self.assertEqual(oct((self.session_root / 'runtime.toml').stat().st_mode & 0o777), '0o600')

    def test_plugin_mode_upgrade_never_touches_claude_code(self):
        self.codex_present()
        fake = self.claude()
        from claude_fake import entry
        fake.entry = entry('/user/own/python3', ['mcp'])
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            old = self.build('a', version=OLD_VERSION)
            self.install(old, claude_code='plugin')
        before = fake.path.read_bytes()
        result = self.install(self.build('b', launcher=b'# second release\n'), claude_code='plugin')
        self.assertEqual((result['claude_code'], result['claude_skills']), ('plugin', 'skipped'))
        self.assertEqual((fake.calls, fake.path.read_bytes()), ([], before))
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(self.build('c', launcher=b'# third release\n'), claude_code='user')
        self.assertEqual(str(caught.exception), 'upgrade_conflict')

    def test_a_kit_run_without_a_mode_keeps_the_installed_one(self):
        """No --claude-code (Install.command, or bootstrap.sh without it): an upgrade, its resume and a repeat keep
        the installed mode; only an explicitly different mode refuses."""
        self.codex_present()
        fake = self.claude()
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            self.install(self.build('a', version=OLD_VERSION), claude_code='plugin')
        b = self.build('b', launcher=b'# second release\n')
        result = self.install(b)
        self.assertEqual((result['status'], result['installation'], result['claude_code'], result['claude_skills']),
                         ('upgraded', 'upgraded', 'plugin', 'skipped'))
        self.assertEqual((self.receipt()['claude_code_mode'], fake.calls), ('plugin', []))
        self.assertEqual(self.install(b, claude_code=None)['installation'], 'reused')
        c = self.build('c', launcher=b'# third release\n')
        for other in ('user', 'skip'):
            with self.assertRaises(self.module().InstallError) as caught:
                self.install(c, claude_code=other)
            self.assertEqual(str(caught.exception), 'upgrade_conflict')
        def crash(value):
            if value == 'registration': raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            self.install(c, checkpoint=crash, claude_code='plugin')
        result = self.install(c)  # The resume keeps the unfinished upgrade's mode.
        self.assertEqual((result['status'], result['claude_code']), ('upgraded', 'plugin'))
        self.assertEqual((self.receipt()['claude_code_mode'], fake.calls), ('plugin', []))
        self.selection_check()

    def test_an_explicit_other_mode_refuses_even_a_repeat_of_the_installed_release(self):
        """The mode, Codex home and skills root are compared before a repeat's result: a repeat with an explicitly
        different mode refuses like a new upgrade would, and changes nothing."""
        self.codex_present()
        self.desktop_present()
        (self.home / '.claude').mkdir(mode=0o755)
        self.session()
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            self.install(self.build('a', version=OLD_VERSION), claude_code='user')
        b = self.build('b', launcher=b'# second release\n')
        result = self.install(b)
        self.assertEqual((result['status'], result['installation'], self.receipt()['claude_code_mode']),
                         ('upgraded', 'upgraded', 'user'))
        receipt, transactions = self.receipt(), sorted(os.listdir(self.runtime / 'transactions'))
        c = self.build('c', launcher=b'# third release\n')
        for kit, mode, codex in ((b, 'plugin', self.codex), (b, 'skip', self.codex), (b, None, self.home / '.codex-other'),
                                 (c, 'plugin', self.codex)):
            with self.subTest(kit=kit[1].name, mode=mode, codex=codex.name), mock.patch.object(self, 'codex', codex):
                with self.assertRaises(self.module().InstallError) as caught:
                    self.install(kit, claude_code=mode)
                self.assertEqual(str(caught.exception), 'upgrade_conflict')
                self.assertEqual((self.receipt(), sorted(os.listdir(self.runtime / 'transactions'))), (receipt, transactions))
        for mode in (None, 'user'):
            result = self.install(b, claude_code=mode)
            self.assertEqual((result['status'], result['installation'], result['claude_code']), ('upgraded', 'reused', 'registered'))

    def test_a_first_install_repeat_with_an_explicit_other_mode_refuses(self):
        self.codex_present()
        (self.home / '.claude').mkdir(mode=0o755)
        a = self.build('a')
        self.install(a, claude_code='skip')
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(a, claude_code='user')
        self.assertEqual((str(caught.exception), caught.exception.pending_action), ('initialization_conflict', None))
        result = self.install(a, claude_code=None)
        self.assertEqual((result['status'], result['installation'], result['claude_code']), ('activation_required', 'reused', 'skipped'))

    def test_an_interrupted_upgrade_refuses_a_different_target_until_resumed(self):
        self.full()
        b = self.build('b', launcher=b'# second release\n')
        def crash(value):
            if value == 'registration': raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            self.install(b, checkpoint=crash)
        with self.assertRaises(self.module().InstallError) as caught:
            self.install(self.build('c', launcher=b'# third release\n'))
        self.assertEqual((str(caught.exception), caught.exception.pending_action), ('transaction_pending', 'upgrade'))
        self.assertEqual(self.install(b)['status'], 'upgraded')
        c = self.build('c2', launcher=b'# third release again\n')
        self.assertEqual(self.install(c)['status'], 'upgraded')
        self.assertEqual(self.receipt()['transaction'], 'change_2')
        self.selection_check()


class KilledWriteTests(ChangeHarness, unittest.TestCase):
    """A real kill between our journaled temporary and its replace: the resume unlinks exactly that inode."""

    def test_each_journaled_external_write_survives_a_kill(self):
        directories = {'recorded:registration': lambda: self.codex, 'recorded:plist': lambda: self.plist.parent,
                       'recorded:policy': lambda: self.session_root,
                       'recorded:claude_desktop_write': lambda: desktop.directory(self.home)}
        for stage, directory in directories.items():
            with self.subTest(stage=stage):
                self.setUp()
                _, old = self.full()
                new = self.build('b', launcher=b'# second release\n')
                self.claude()
                pid = os.fork()
                if pid == 0:
                    try:
                        def kill(value):
                            if value == stage: os._exit(9)
                        self.install(new, checkpoint=kill)
                    finally:
                        os._exit(3)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 9)
                left = [p for p in directory().iterdir() if p.name.startswith('.telegram-search-')]
                self.assertEqual(len(left), 1)
                result = self.install(new)
                self.assertEqual(result['status'], 'upgraded')
                self.assertEqual([p for p in directory().iterdir() if p.name.startswith('.telegram-search-')], [])
                self.assertUpgradedTo(self.runtime / 'releases' / new[3], old['release_root'])
                self.assertEqual(result['claude_desktop'], 'registered')
                self.selection_check()


class RollbackTests(ChangeHarness, unittest.TestCase):

    def upgraded(self, *, v1=False):
        packet, old = self.full()
        if v1:self.as_v1()
        original = self.receipt()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')
        self.install(new)
        return packet, old, new, original

    def test_rollback_restores_the_previous_release_and_receipt(self):
        for v1 in (False, True):
            with self.subTest(v1=v1):
                self.setUp()
                packet, old, new, original = self.upgraded(v1=v1)
                before = self.session_hashes()
                policy = (self.session_root / 'runtime.toml').read_bytes()
                with self.no_other_process():
                    result = self.change('rollback')
                self.assertEqual((result['status'], result['service']), ('rolled_back', 'restarted'))
                self.assertUpgradedTo(Path(old['release_root']), self.runtime / 'releases' / new[3])
                receipt = self.receipt()
                rewritten = ('publication_objects', 'claude_desktop_ownership')
                self.assertEqual({k: v for k, v in receipt.items() if k not in rewritten},
                                 {k: v for k, v in original.items() if k not in rewritten})
                self.assertEqual(receipt['publication_objects']['release'], original['publication_objects']['release'])
                if not v1:  # It owns the entry it wrote: the same entry as before, recorded by the rollback.
                    owned, installed = receipt['claude_desktop_ownership'], original['claude_desktop_ownership']
                    self.assertEqual((owned['transaction_id'], owned['post_subtree_hash']),
                                     ('change_2', installed['post_subtree_hash']))
                self.assertEqual((self.skills / 'telegram-search' / 'SKILL.md').read_bytes(), b'search-skill')
                self.assertEqual((self.session_root / 'runtime.toml').read_bytes(),
                                 policy.replace(b'0.57.0rc1', OLD_VERSION.encode()))
                self.assertEqual(self.session_hashes(), before)
                self.selection_check(OLD_VERSION)
                self.assertTrue((self.runtime / 'releases' / new[3]).is_dir())
                # Forward again: the kept release is verified and reused.
                self.assertEqual(self.install(new)['status'], 'upgraded')
                self.selection_check()

    def switch(self, *steps):
        """The owner's profile switch (capability_profiles.apply) with a stopped broker, step by step."""
        from telegram_search_mcp import capability_profiles
        for action, profile in steps:
            result = capability_profiles.apply(self.home, action, profile, pending=lambda: 'none',
                                               restartable=lambda: 'not_running', restart=lambda: 'not_running')
            self.assertEqual(result['status'], 'changed')

    def test_a_profile_switch_never_blocks_a_later_rollback(self):
        """Only the enabled_capabilities line differs from the state the upgrade recorded: the rollback goes back
        and keeps the owner's switch; enabling and disabling again (sorted bytes) is no different."""
        for steps in ((('enable', 'video'),), (('enable', 'send'), ('disable', 'send')),
                      (('enable', 'replies'), ('enable', 'readers'), ('disable', 'send'))):
            with self.subTest(steps=steps):
                self.setUp()
                _, old, new, _ = self.upgraded()
                self.switch(*steps)
                path = self.session_root / 'runtime.toml'
                switched = path.read_bytes()
                result = self.change('rollback')
                self.assertEqual((result['status'], result['release_root']), ('rolled_back', old['release_root']))
                self.assertEqual(path.read_bytes(), switched.replace(b'0.57.0rc1', OLD_VERSION.encode()))
                # Forward again over the switched policy: the kept release is verified and reused.
                self.assertEqual(self.install(new)['status'], 'upgraded')
                self.assertEqual(path.read_bytes(), switched)

    def test_rollback_refuses_newer_state_or_a_first_install(self):
        self.full()
        with self.assertRaises(self.module().InstallError) as caught:
            self.change('rollback')
        self.assertEqual(str(caught.exception), 'rollback_unavailable')
        for kind in ('enrollment', 'other_line', 'switch_and_other_line', 'unrecorded'):
            with self.subTest(kind=kind):
                self.setUp()
                self.upgraded()
                path = self.session_root / 'runtime.toml'
                if kind == 'enrollment':
                    (self.session_root / 'enrollment.json').write_bytes(b'{"written": "by the newer release"}\n')
                elif kind == 'other_line':  # A key the older release may not accept.
                    path.write_bytes(path.read_bytes() + b'selection_ttl_seconds = 1200\n')
                elif kind == 'switch_and_other_line':
                    self.switch(('enable', 'video'))
                    path.write_bytes(path.read_bytes() + b'selection_ttl_seconds = 1200\n')
                else:  # A transaction recorded without the capability-line digest keeps the whole-file rule.
                    journal = self.runtime / 'transactions' / self.receipt()['transaction'] / 'journal.json'
                    value = json.loads(journal.read_bytes())
                    value['state'] = {k: v for k, v in value['state'].items() if k in ('enrollment.json', 'runtime.toml')}
                    journal.write_bytes(self.module().encoded(value))
                    self.switch(('enable', 'video'))
                snapshot = self.receipt()
                with self.assertRaises(self.module().InstallError) as caught:
                    self.change('rollback')
                self.assertEqual(str(caught.exception), 'rollback_refused')
                self.assertEqual(self.receipt(), snapshot)

    def test_a_rollback_repeated_after_it_finished_returns_its_result_without_another_step(self):
        for chained in (False, True):
            with self.subTest(chained=chained):
                self.setUp()
                _, old, new, _ = self.upgraded()
                target = old['release_root']
                if chained:  # A -> B -> C: the rollback goes back to B, and a repeat never steps on to A.
                    self.install(self.build('c', launcher=b'# third release\n', search=b'search-skill-3'))
                    target = str(self.runtime / 'releases' / new[3])
                first = self.change('rollback')
                self.assertEqual((first['status'], first['release_root']), ('rolled_back', target))
                transactions = sorted(p.name for p in (self.runtime / 'transactions').iterdir())
                calls, verbs, receipt = len(self.claude().calls), self.launchctl.verbs(), self.receipt()
                with self.no_other_process():
                    again = self.change('rollback')
                expected = {k: v for k, v in first.items() if k != 'service'}
                self.assertEqual(again, {**expected, 'installation': 'reused'})
                self.assertEqual(sorted(p.name for p in (self.runtime / 'transactions').iterdir()), transactions)
                self.assertEqual((len(self.claude().calls), self.launchctl.verbs(), self.receipt()), (calls, verbs, receipt))
                self.assertEqual(self.pointer()['release_root'], target)

    def test_the_kit_a_rollback_went_back_to_run_again_returns_that_rollback_without_another_step(self):
        """A -> B -> rollback, then A's kit again (Install.command, the plugin): the rollback's result with
        `installation: reused`, never `installation_refused` and never a step; an explicit other mode refuses; B's
        kit upgrades again."""
        packet, old, new, _ = self.upgraded()
        first = self.change('rollback')
        transactions = sorted(p.name for p in (self.runtime / 'transactions').iterdir())
        calls, verbs, receipt = len(self.claude().calls), self.launchctl.verbs(), self.receipt()
        expected = {**{k: v for k, v in first.items() if k != 'service'}, 'installation': 'reused'}
        with mock.patch('telegram_search_mcp.__version__', OLD_VERSION):
            with self.no_other_process():
                for mode in (None, 'user'):
                    self.assertEqual(self.install(packet, claude_code=mode), expected)
            with self.assertRaises(self.module().InstallError) as caught:
                self.install(packet, claude_code='plugin')
            self.assertEqual(str(caught.exception), 'upgrade_conflict')
        self.assertEqual((expected['status'], expected['release_root']), ('rolled_back', old['release_root']))
        self.assertEqual(sorted(p.name for p in (self.runtime / 'transactions').iterdir()), transactions)
        self.assertEqual((len(self.claude().calls), self.launchctl.verbs(), self.receipt()), (calls, verbs, receipt))
        result = self.install(new)
        self.assertEqual((result['status'], result['installation']), ('upgraded', 'upgraded'))
        self.assertEqual(self.pointer()['release_root'], str(self.runtime / 'releases' / new[3]))
        self.selection_check()

    def test_every_rollback_checkpoint_resumes_without_double_effects(self):
        seen = []
        self.upgraded()
        self.change('rollback', checkpoint=seen.append)
        stages = list(dict.fromkeys(seen))
        self.assertIn('published:pointer', stages)
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                _, old, new, _ = self.upgraded()
                fake = self.claude()
                calls = len(fake.calls)
                boots = self.launchctl.verbs().count('bootout')
                def crash(value):
                    if value == stage: raise RuntimeError('simulated crash')
                with self.assertRaises(RuntimeError):
                    self.change('rollback', checkpoint=crash)
                result = self.change('rollback')
                self.assertEqual(result['status'], 'rolled_back')
                window = WRITE_AHEAD.get(stage)
                self.assertUpgradedTo(Path(old['release_root']), self.runtime / 'releases' / new[3],
                                      claude=window != 'claude', desktop=window != 'desktop')
                if window:
                    self.assertEqual(result['claude_code' if window == 'claude' else 'claude_desktop'], 'failed')
                self.assertEqual(fake.verbs()[calls:], {'recorded:claude_remove': [], 'recorded:claude_add': ['remove']}.get(
                    stage, ['remove', 'add-json']))
                self.assertEqual(self.launchctl.verbs().count('bootout') - boots, 1)
                self.selection_check(OLD_VERSION)


if __name__ == '__main__': unittest.main()
