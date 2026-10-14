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


class BundleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'user'
        self.home.mkdir(mode=0o700)
        self.runtime = self.home / 'runtime'
        self.codex = self.home / '.codex'
        self.skills = self.home / '.agents' / 'skills'

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
            'app/telegram_search_mcp/__init__.py': b'__version__ = "0.52.0.dev0"\n',
            'native/libtdjson.dylib': b'inert-native',
            'skills/telegram-search/SKILL.md': b'search-skill',
            'skills/telegram-setup/SKILL.md': b'setup-skill',
            'tdlib-lock.json': b'{}',
        }
        manifest = dict(schema_version=1, package_version='0.52.0.dev0', target='macos-arm64',
            minimum_os='26.0', contract_version=1, schema_fingerprint='a' * 64,
            tools=['_manifest', 'search_correspondence'], native_lock_sha256=hashlib.sha256(b'{}').hexdigest(),
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
        index = dict(schema_version=1, target='macos-arm64', release_version='0.52.0.dev0', archive_bytes=1,
                     archive_sha256='b' * 64, unpacked_bytes=sum(len(v) for v in files.values()), files=records)
        index_path = self.base / 'index.json'
        index_path.write_bytes(json.dumps(index).encode())
        index_path.chmod(0o400)
        return module, release, index_path, hashlib.sha256(index_path.read_bytes()).hexdigest()

    def install(self, packet, **kwargs):
        module, release, index, pin = packet
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return module.install(release, index, pin, installation_root=self.runtime,
                codex_home=self.codex, skills_root=self.skills, user_home=self.home, **kwargs)

    def test_success_is_activation_required_and_exact_repeat_preserves_config_inode(self):
        packet = self.fixture()
        self.codex.mkdir(mode=0o700)
        config = self.codex / 'config.toml'
        config.write_bytes(b'# preserved neighbor\nmodel = "example"\n')
        config.chmod(0o600)
        result = self.install(packet)
        self.assertEqual(result['status'], 'activation_required')
        self.assertEqual(shlex.split(result['activation_command']),result['activation_arguments'])
        self.assertTrue(config.read_bytes().startswith(b'# preserved neighbor\nmodel = "example"\n'))
        self.assertTrue((self.runtime / 'current.json').is_file())
        inode = config.stat().st_ino
        self.assertEqual(self.install(packet)['installation'], 'reused')
        self.assertEqual(config.stat().st_ino, inode)

    def test_foreign_config_skill_and_runtime_are_preserved(self):
        for kind in ['config', 'skill', 'runtime', 'service']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                self.setUp()
                packet = self.fixture()
                if kind == 'config':
                    target = self.codex / 'config.toml'
                    content = b'[mcp_servers.telegram_search]\ncommand = "/foreign"\n'
                elif kind == 'skill':
                    target = self.skills / 'telegram-search' / 'SKILL.md'
                    content = b'foreign skill'
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
        for stage in ['prepared', 'release', 'registration', 'telegram-search', 'telegram-setup', 'receipt']:
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

    def test_concurrent_config_drift_is_retained_and_resume_refuses(self):
        packet = self.fixture()
        def drift(stage):
            if stage == 'release':
                (self.codex / 'config.toml').write_bytes(b'# concurrent edit\n')
                (self.codex / 'config.toml').chmod(0o600)
        with self.assertRaises(packet[0].InstallError): self.install(packet, checkpoint=drift)
        self.assertEqual((self.codex / 'config.toml').read_bytes(), b'# concurrent edit\n')
        with self.assertRaises(packet[0].InstallError): self.install(packet)
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

    def test_partial_bulk_copy_is_quarantined_and_exact_retry_completes(self):
        packet=self.fixture();module=packet[0];original=module.shutil.copyfileobj;calls=0
        def interrupted(source,destination,length):
            nonlocal calls
            original(source,destination,length);calls+=1
            if calls==2:raise RuntimeError('simulated bulk interruption')
        with mock.patch.object(module.shutil,'copyfileobj',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.install(packet)
        self.assertFalse((self.runtime/'current.json').exists())
        self.assertEqual(self.install(packet)['status'],'activation_required')
        transaction=self.runtime/'transactions'/('install_'+packet[3][:24])
        self.assertTrue((transaction/'failed-release-0').is_dir())
        self.assertTrue((transaction/'failed-release-0/app').is_dir())

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


if __name__ == '__main__': unittest.main()
