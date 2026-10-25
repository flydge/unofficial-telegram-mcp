"""First-install behavior; every path is a disposable private root."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import shlex
import unittest
from unittest import mock
from bundle_payload import inert_natives, v1_lock
from claude_fake import FakeClaude


class InstallHarness:
    """Disposable user home plus a synthetic admitted release; never a real client, CLI or config."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'user'
        self.home.mkdir(mode=0o700)
        self.runtime = self.home / 'runtime'
        self.codex = self.home / '.codex'
        self.skills = self.home / '.agents' / 'skills'

    @property
    def claude_skills(self):
        return self.home / '.claude' / 'skills'

    def claude(self):
        """The fake `claude` CLI for the current home (rebuilt when a subtest resets the fixture)."""
        if getattr(self, '_claude_base', None) != self.base:
            self._claude_base = self.base
            self._claude = FakeClaude(self.home)
            self.claude_cli = self.base / 'bin' / 'claude'
            self.claude_cli.parent.mkdir(mode=0o700)
            self.claude_cli.write_bytes(b'#!/bin/sh\n# inert placeholder; the fake runner never executes it\n')
            self.claude_cli.chmod(0o700)
        return self._claude

    def module(self):
        spec = importlib.util.find_spec('telegram_search_mcp.bundle_install')
        self.assertIsNotNone(spec, 'missing authenticated first-install vertical')
        return importlib.import_module('telegram_search_mcp.bundle_install')

    def fixture(self):
        module = self.module()
        release = self.base / 'admitted'
        release.mkdir(mode=0o700)
        files = {
            'launcher.py': b'# inert unit fixture\n',
            'python/bin/python3': b'inert-runtime',
            'app/telegram_search_mcp/__init__.py': b'__version__ = "0.58.0.dev0"\n',
            'native/libtdjson.dylib': b'inert-native',
            'skills/telegram-search/SKILL.md': b'search-skill',
            'skills/telegram-setup/SKILL.md': b'setup-skill',
            'tdlib-lock.json': json.dumps(v1_lock(inert_natives())).encode(),  # a v1 manifest pairs with an ad-hoc lock
        }
        manifest = dict(schema_version=1, package_version='0.58.0.dev0', target='macos-arm64',
            minimum_os='26.0', contract_version=1, schema_fingerprint='a' * 64,
            tools=['_manifest', 'search_correspondence'], native_lock_sha256=hashlib.sha256(files['tdlib-lock.json']).hexdigest(),
            skills=[dict(id=n, sha256=hashlib.sha256(files['skills/' + n + '/SKILL.md']).hexdigest())
                    for n in ['telegram-search', 'telegram-setup']],
            source_completeness='incomplete', publication='unreleased')
        files['manifest/release.json'] = json.dumps(manifest).encode()
        records = []
        for name, data in sorted(files.items()):
            path = release / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            executable = name == 'python/bin/python3'
            path.chmod(0o500 if executable else 0o400)
            records.append(dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), executable=executable))
        for parent, dirs, _ in os.walk(release):
            Path(parent).chmod(0o700)
        index = dict(schema_version=1, target='macos-arm64', release_version='0.58.0.dev0', archive_bytes=1,
                     archive_sha256='b' * 64, unpacked_bytes=sum(len(v) for v in files.values()), files=records)
        index_path = self.base / 'index.json'
        index_path.write_bytes(json.dumps(index).encode())
        index_path.chmod(0o400)
        return module, release, index_path, hashlib.sha256(index_path.read_bytes()).hexdigest()

    def install(self, packet, **kwargs):
        module, release, index, pin = packet
        fake = self.claude()
        kwargs.setdefault('claude_candidates', (self.claude_cli,))
        kwargs.setdefault('claude_runner', fake)
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return module.install(release, index, pin, installation_root=self.runtime,
                codex_home=self.codex, skills_root=self.skills, user_home=self.home, **kwargs)


class BundleInstallTests(InstallHarness, unittest.TestCase):

    def test_success_is_activation_required_and_exact_repeat_preserves_config_inode(self):
        packet = self.fixture()
        self.codex.mkdir(mode=0o700)
        config = self.codex / 'config.toml'
        config.write_bytes(b'# preserved neighbor\nmodel = "example"\n')
        config.chmod(0o600)
        result = self.install(packet)
        self.assertEqual(result['status'], 'activation_required')
        self.assertEqual(shlex.split(result['activation_command']),result['activation_arguments'])
        installed = Path(result['release_root'])
        self.assertEqual(result['activation_arguments'], [str(installed/'python/bin/python3'), '-I', '-S', '-B',
                                                          str(installed/'launcher.py'), 'connect'])
        self.assertEqual(result['next_action'], 'run_activation_then_wait')
        self.assertEqual(result['claude_code'], 'registered')
        self.assertTrue(config.read_bytes().startswith(b'# preserved neighbor\nmodel = "example"\n'))
        self.assertTrue((self.runtime / 'current.json').is_file())
        inode = config.stat().st_ino
        self.assertEqual(self.install(packet)['installation'], 'reused')
        self.assertEqual(config.stat().st_ino, inode)

    def test_client_created_0755_roots_are_accepted_and_never_chmodded(self):
        # Codex and Claude Code create ~/.codex, ~/.agents and ~/.claude as 0755.
        packet = self.fixture()
        for root in (self.codex, self.skills, self.claude_skills):
            root.mkdir(parents=True, exist_ok=True)
            for path in (root, root.parent):
                if path != self.home:
                    path.chmod(0o755)
        result = self.install(packet)
        self.assertEqual(result['status'], 'activation_required')
        for root in (self.codex, self.skills, self.claude_skills):
            self.assertEqual(oct(root.stat().st_mode & 0o777), '0o755')
        for name in ('telegram-search', 'telegram-setup'):
            self.assertEqual(oct((self.claude_skills / name).stat().st_mode & 0o777), '0o700')

    def test_group_or_world_writable_client_roots_are_refused(self):
        for mode in (0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                self.setUp()
                packet = self.fixture()
                self.claude_skills.mkdir(parents=True)
                self.claude_skills.chmod(mode)
                with self.assertRaises(packet[0].InstallError):
                    self.install(packet)
                self.assertFalse((self.runtime / 'current.json').exists())

    def test_both_skill_roots_receive_exact_private_copies(self):
        packet = self.fixture()
        result = self.install(packet)
        installed = Path(result['release_root'])
        for root in (self.skills, self.claude_skills):
            self.assertEqual(oct(root.stat().st_mode & 0o777), '0o700')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['telegram-search', 'telegram-setup'])
            for name in ('telegram-search', 'telegram-setup'):
                self.assertEqual(oct((root / name).stat().st_mode & 0o777), '0o700')
                self.assertEqual([p.name for p in (root / name).iterdir()], ['SKILL.md'])
                self.assertEqual((root / name / 'SKILL.md').read_bytes(), (installed / 'skills' / name / 'SKILL.md').read_bytes())
        receipt = json.loads((self.runtime / 'receipt.json').read_text())
        self.assertEqual(receipt['claude_skills_root'], str(self.claude_skills))
        self.assertEqual(set(receipt['publication_objects']), {'release', 'telegram-search', 'telegram-setup',
                                                               'claude-telegram-search', 'claude-telegram-setup'})
        self.assertEqual(receipt['claude_code'], 'registered')

    def test_foreign_config_skill_and_runtime_are_preserved(self):
        for kind in ['config', 'skill', 'claude_skill', 'runtime', 'service']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                self.setUp()
                packet = self.fixture()
                if kind == 'config':
                    target = self.codex / 'config.toml'
                    content = b'[mcp_servers.telegram_search]\ncommand = "/foreign"\n'
                elif kind == 'skill':
                    target = self.skills / 'telegram-search' / 'SKILL.md'
                    content = b'foreign skill'
                elif kind == 'claude_skill':
                    target = self.claude_skills / 'telegram-setup' / 'SKILL.md'
                    content = b'foreign claude skill'
                elif kind == 'service':
                    from telegram_search_mcp.config import _owner_paths
                    target = _owner_paths(self.home)[3]
                    content = b'foreign service'
                else:
                    target = self.runtime / 'foreign.txt'
                    content = b'foreign runtime'
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
                with self.assertRaises(packet[0].InstallError): self.install(packet)
                self.assertEqual(target.read_bytes(), content)
                self.assertFalse((self.runtime / 'current.json').exists())

    def test_interruptions_resume_every_pending_transaction_step(self):
        for stage in ['prepared', 'release', 'registration', 'telegram-search', 'telegram-setup', 'claude-telegram-search',
                      'claude-telegram-setup', 'claude_code', 'claude_desktop', 'receipt']:
            with self.subTest(stage=stage):
                self.setUp()
                packet = self.fixture()
                def interrupt(value):
                    if value == stage: raise RuntimeError('simulated interruption')
                with self.assertRaises(RuntimeError): self.install(packet, checkpoint=interrupt)
                self.assertFalse((self.runtime / 'current.json').exists())
                self.assertEqual(self.install(packet)['status'], 'activation_required')

    def test_tamper_unindexed_link_and_wrong_index_refuse_before_registration(self):
        for kind in ['tamper', 'extra', 'link', 'index']:
            with self.subTest(kind=kind):
                self.setUp()
                packet = self.fixture()
                release = packet[1]
                if kind == 'tamper':
                    (release / 'launcher.py').chmod(0o600)
                    (release / 'launcher.py').write_bytes(b'tampered')
                elif kind == 'extra': (release / 'extra').write_bytes(b'extra')
                elif kind == 'link': (release / 'extra').symlink_to('/etc/passwd')
                else: packet = (*packet[:3], '0' * 64)
                with self.assertRaises(packet[0].InstallError): self.install(packet)
                self.assertFalse((self.codex / 'config.toml').exists())

    def test_concurrent_config_drift_keeps_the_neighbours_and_a_foreign_entry_refuses(self):
        # A neighbour written while the release is copied is merged, every byte kept; a `telegram_search`
        # entry that appears meanwhile is never touched and every resume refuses.
        for drift, merged in ((b'# concurrent edit\n', True),
                              (b'# concurrent edit\n[mcp_servers.telegram_search]\ncommand = "/foreign"\n', False)):
            with self.subTest(merged=merged):
                self.setUp()
                packet = self.fixture()
                def edit(stage):
                    if stage == 'release':
                        (self.codex / 'config.toml').write_bytes(drift)
                        (self.codex / 'config.toml').chmod(0o600)
                if merged:
                    self.assertEqual(self.install(packet, checkpoint=edit)['status'], 'activation_required')
                    data = (self.codex / 'config.toml').read_bytes()
                    self.assertTrue(data.startswith(drift) and b'[mcp_servers.telegram_search]' in data)
                    continue
                with self.assertRaises(packet[0].InstallError): self.install(packet, checkpoint=edit)
                self.assertEqual((self.codex / 'config.toml').read_bytes(), drift)
                with self.assertRaises(packet[0].InstallError): self.install(packet)
                self.assertEqual((self.codex / 'config.toml').read_bytes(), drift)
                self.assertFalse((self.runtime / 'current.json').exists())

    def test_unsafe_root_and_skill_ancestry_are_refused(self):
        packet = self.fixture()
        self.runtime.symlink_to(self.base)
        with self.assertRaises(packet[0].InstallError): self.install(packet)

    def test_unsupported_os_and_arch_refuse_before_any_install_mutation(self):
        module,release,index,pin=self.fixture()
        for system,machine,version in [('Linux','arm64','26.0'),('Darwin','x86_64','26.0'),('Darwin','arm64','25.9')]:
            with self.subTest(system=system,machine=machine,version=version), mock.patch('platform.system',return_value=system), \
                    mock.patch('platform.machine',return_value=machine),mock.patch('platform.mac_ver',return_value=(version,(),'')):
                with self.assertRaises(module.InstallError):
                    module.install(release,index,pin,installation_root=self.runtime,codex_home=self.codex,skills_root=self.skills,user_home=self.home)
                self.assertFalse(self.runtime.exists())

    def test_installer_lease_contention_cannot_publish_any_pointer(self):
        from telegram_search_mcp.session_owner import SessionOwnerLease
        packet=self.fixture();self.runtime.mkdir(mode=0o700)
        with SessionOwnerLease(self.runtime/'.install.lock'):
            with self.assertRaises(packet[0].InstallError):self.install(packet)
        self.assertFalse((self.runtime/'current.json').exists())

    def test_hardlink_or_unsafe_mode_cannot_be_admitted(self):
        for kind in ['hardlink','mode']:
            with self.subTest(kind=kind):
                self.setUp();packet=self.fixture();path=packet[1]/'launcher.py'
                if kind=='hardlink':os.link(path,self.base/'alias')
                else:path.chmod(0o666)
                with self.assertRaises(packet[0].InstallError):self.install(packet)
                self.assertFalse(self.runtime.exists())

    def test_partial_bulk_copy_is_removed_and_exact_retry_completes(self):
        packet=self.fixture();module=packet[0];original=module.shutil.copyfileobj;calls=0
        def interrupted(source,destination,length):
            nonlocal calls
            original(source,destination,length);calls+=1
            if calls==2:raise RuntimeError('simulated bulk interruption')
        with mock.patch.object(module.shutil,'copyfileobj',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.install(packet)
        transaction=self.runtime/'transactions'/('install_'+packet[3][:24])
        self.assertTrue((transaction/'failed-release-0/app').is_dir())  # Unbound: no journal record names it.
        self.assertFalse((self.runtime/'current.json').exists())
        self.assertEqual(self.install(packet)['status'],'activation_required')
        # The retry removed the unbound partial copy before staging its own, which became the release.
        self.assertEqual(sorted(p.name for p in transaction.iterdir() if p.name.startswith('failed-release-')),[])
        self.assertTrue((self.runtime/'releases'/packet[3]/'app').is_dir())

    def test_exact_repeat_reuses_owned_bundled_service_without_service_actions(self):
        import plistlib
        from telegram_search_mcp.config import _owner_paths
        from telegram_search_mcp import launch_agent
        packet=self.fixture();result=self.install(packet);installed=Path(result['release_root'])
        _,_,label,plist=_owner_paths(self.home)
        plist.parent.mkdir(mode=0o700,parents=True)
        payload=launch_agent.launch_agent_payload(python_executable=installed/'python/bin/python3')
        payload.update(Label=label,ProgramArguments=[str(installed/'python/bin/python3'),'-I','-S','-B',str(installed/'launcher.py'),'broker'],EnvironmentVariables={})
        plist.write_bytes(plistlib.dumps(payload));plist.chmod(0o600);before=(plist.stat().st_ino,plist.read_bytes())
        with mock.patch('subprocess.run',side_effect=AssertionError('installer must never run a service command')):
            self.assertEqual(self.install(packet)['installation'],'reused')
        self.assertEqual((plist.stat().st_ino,plist.read_bytes()),before)
        payload['ProgramArguments'][-1]='setup';plist.write_bytes(plistlib.dumps(payload))
        with self.assertRaises(packet[0].InstallError):self.install(packet)
        self.assertEqual(plistlib.loads(plist.read_bytes())['ProgramArguments'][-1],'setup')


class AssemblyIdentityReprTests(unittest.TestCase):
    def test_generated_identity_file_repr_never_carries_its_bytes(self):
        from bundle_payload import keychain_runner, load_assembler
        api_hash = 'fedcba9876543210fedcba9876543210'
        file = load_assembler()._app_identity_file(keychain_runner(2468013, api_hash))
        self.assertIn(api_hash.encode(), file.data)
        for text in (repr(file), str(file)):
            self.assertNotIn(api_hash, text)
            self.assertNotIn('2468013', text)


if __name__ == '__main__': unittest.main()
