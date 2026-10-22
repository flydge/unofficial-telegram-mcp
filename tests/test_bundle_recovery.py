"""Review regressions over retained, explicit private OS-test-temp fixtures."""
import os
from pathlib import Path
import shutil
import tempfile
from unittest import mock
from test_bundle_install import BundleInstallTests


class RecoveryTests(BundleInstallTests):
    def setUp(self):
        self.base=Path(tempfile.mkdtemp(prefix='telegram-b1b-recovery-',dir=tempfile.gettempdir())).resolve()
        self.home=self.base/'user';self.home.mkdir(mode=0o700)
        self.runtime=self.home/'runtime';self.codex=self.home/'.codex';self.skills=self.home/'.agents/skills'

    def test_retry_after_each_initialization_and_partial_skill_mutation(self):
        for kind in ['transactions','releases','tx','config.preimage','config.candidate','registration.json','journal.json','skill_dir','skill_file']:
            with self.subTest(kind=kind):
                self.setUp();packet=self.fixture();module=packet[0];fired=False
                original_directory=module.ensure_owned_private_directory;original_create=module.create
                def directory(path,**kwargs):
                    nonlocal fired
                    value=original_directory(path,**kwargs);path=Path(path)
                    wanted=(path.name==kind or kind=='tx' and path.name.startswith('install_')
                            or kind=='skill_dir' and path.name.startswith('skill-packet-telegram-search-'))
                    if wanted and not fired:fired=True;raise RuntimeError('interrupted after mkdir')
                    return value
                def create(path,data,**kwargs):
                    nonlocal fired
                    path=Path(path)
                    if not fired and (path.name==kind or kind=='skill_file' and path.name=='SKILL.md'):
                        # Simulate a process dying before/after an actual write.
                        if kind=='skill_file':path.write_bytes(data[:3]);path.chmod(0o600)
                        fired=True;raise RuntimeError('interrupted metadata/file publication')
                    return original_create(path,data,**kwargs)
                with mock.patch.object(module,'ensure_owned_private_directory',side_effect=directory),mock.patch.object(module,'create',side_effect=create):
                    with self.assertRaises(RuntimeError):self.install(packet)
                self.assertTrue(fired)
                self.assertFalse((self.runtime/'current.json').exists())
                self.assertEqual(self.install(packet)['status'],'activation_required')

    def test_retry_before_and_after_initial_control_file_publication(self):
        for name in ['.install.lock','config.preimage','config.candidate','registration.json','journal.json','SKILL.md','receipt.json','current.json']:
            for phase in ['before','after']:
                with self.subTest(name=name,phase=phase):
                    self.setUp();packet=self.fixture();module=packet[0];original=module.create;fired=False
                    def create(path,data,**kwargs):
                        nonlocal fired
                        selected=Path(path).name==name and not fired
                        if selected and phase=='before':fired=True;raise RuntimeError('before control file publication')
                        result=original(path,data,**kwargs)
                        if selected:fired=True;raise RuntimeError('after control file publication')
                        return result
                    with mock.patch.object(module,'create',side_effect=create):
                        with self.assertRaises(RuntimeError):self.install(packet)
                    self.assertTrue(fired)
                    self.assertFalse((self.runtime/'current.json').exists())
                    self.assertEqual(self.install(packet)['status'],'activation_required')

    def test_atomic_temporary_creation_interruption_is_retryable_and_retained(self):
        for role in ['initial','journal','config','skill']:
            with self.subTest(role=role):
                self.setUp();packet=self.fixture();module=packet[0];original=module.os.open;fired=False
                def open_file(path,flags,*args,**kwargs):
                    nonlocal fired
                    descriptor=original(path,flags,*args,**kwargs)
                    if flags&os.O_CREAT and str(path).startswith('.telegram-search-') and not fired:
                        parent=Path(os.readlink('/dev/fd/'+str(kwargs['dir_fd']))) if os.sys.platform=='darwin' else None
                        # /dev/fd readlink is unavailable on some hosts. Use the
                        # already recorded logical checkpoints to select role.
                        selected=(role=='initial' and not self.runtime.exists()
                                  or role=='config' and parent==self.codex
                                  or role=='journal' and parent is not None and parent.name.startswith('install_') and self.runtime.exists()
                                  or role=='skill' and parent is not None and parent.name.startswith('skill-packet-telegram-search-'))
                        if selected:
                            os.close(descriptor);fired=True;raise RuntimeError('after temporary inode creation')
                    return descriptor
                # Darwin fcntl F_GETPATH returns the descriptor's actual path.
                import fcntl
                original_readlink=os.readlink
                def path_of(fd):
                    if os.sys.platform=='darwin':return Path(fcntl.fcntl(fd,50,b'\0'*1024).split(b'\0',1)[0].decode())
                    return Path(original_readlink('/proc/self/fd/'+str(fd)))
                def readlink(path):
                    return str(path_of(int(path.rsplit('/',1)[1])))
                with mock.patch.object(module.os,'open',side_effect=open_file),mock.patch.object(module.os,'readlink',side_effect=readlink):
                    with self.assertRaises(RuntimeError):self.install(packet)
                self.assertTrue(fired)
                self.assertEqual(self.install(packet)['status'],'activation_required')

    def test_matching_or_mismatched_foreign_creation_after_durable_intent_refuses(self):
        for kind in ['release','registration','telegram-search','telegram-setup','claude-telegram-search','claude-telegram-setup','receipt','pointer']:
            for matching in [True,False]:
                with self.subTest(kind=kind,matching=matching):
                    self.setUp();packet=self.fixture();module=packet[0];foreign=None
                    def actor(event):
                        nonlocal foreign
                        if event!='recorded:'+kind:return
                        tx=self.runtime/'transactions'/('install_'+packet[3][:24])
                        journal=__import__('json').loads((tx/'journal.json').read_text())
                        bound=journal['publications'][kind][-1]
                        if kind=='registration':
                            target=self.codex/'config.toml';target.write_bytes((tx/'config.candidate').read_bytes() if matching else b'# foreign\n');target.chmod(0o600)
                        else:
                            target=(self.runtime/'releases'/packet[3] if kind=='release' else self.skills/kind if kind.startswith('telegram-')
                                    else self.claude_skills/kind[len('claude-'):] if kind.startswith('claude-')
                                    else self.runtime/('current.json' if kind=='pointer' else 'receipt.json'))
                            source=Path(bound['staged'])
                            if source.is_dir():shutil.copytree(source,target)
                            else:shutil.copyfile(source,target);target.chmod(0o600)
                            if not matching:
                                changed=target/'launcher.py' if kind=='release' else target/'SKILL.md' if kind.startswith(('telegram-','claude-')) else target
                                changed.chmod(0o600);changed.write_bytes(b'foreign')
                        foreign=(target,target.stat().st_ino)
                    if kind=='registration' and not matching:
                        # Neighbours only, no entry: the write is planned again over them in the same run, every byte kept.
                        self.assertEqual(self.install(packet,checkpoint=actor)['status'],'activation_required')
                        self.assertTrue(foreign[0].read_bytes().startswith(b'# foreign\n'));continue
                    with self.assertRaises(module.InstallError):self.install(packet,checkpoint=actor)
                    self.assertIsNotNone(foreign);self.assertEqual(foreign[0].stat().st_ino,foreign[1])
                    with self.assertRaises(module.InstallError):self.install(packet)
                    self.assertEqual(foreign[0].stat().st_ino,foreign[1])

    def test_matching_foreign_targets_appearing_during_install_are_never_adopted(self):
        for kind in ['release','config','skill','claude_skill']:
            with self.subTest(kind=kind):
                self.setUp();packet=self.fixture();module,source,index,pin=packet;foreign=None
                def actor(stage):
                    nonlocal foreign
                    if kind=='release' and stage=='prepared':
                        target=self.runtime/'releases'/pin
                        shutil.copytree(source,target);shutil.copyfile(index,target/'bundle-index.json');(target/'bundle-index.json').chmod(0o400)
                        foreign=(target,target.stat().st_ino)
                    if kind=='config' and stage=='release':
                        tx=self.runtime/'transactions'/('install_'+pin[:24]);target=self.codex/'config.toml'
                        target.write_bytes((tx/'config.candidate').read_bytes());target.chmod(0o600);foreign=(target,target.stat().st_ino)
                    if kind in ('skill','claude_skill') and stage=='registration':
                        target=(self.skills if kind=='skill' else self.claude_skills)/'telegram-search';target.mkdir(mode=0o700,parents=True)
                        (target/'SKILL.md').write_bytes((source/'skills/telegram-search/SKILL.md').read_bytes());(target/'SKILL.md').chmod(0o600)
                        foreign=(target,target.stat().st_ino)
                with self.assertRaises(module.InstallError):self.install(packet,checkpoint=actor)
                self.assertIsNotNone(foreign)
                self.assertEqual(foreign[0].stat().st_ino,foreign[1])
                self.assertFalse((self.runtime/'current.json').exists())

    def test_crash_immediately_after_own_publication_can_recover(self):
        for kind in ['root','release','config','telegram-search','telegram-setup','claude-telegram-search','claude-telegram-setup']:
            with self.subTest(kind=kind):
                self.setUp();packet=self.fixture();module=packet[0];original_rename=module._rename;fired=False
                def renamed(source,destination):
                    nonlocal fired
                    original_rename(source,destination)
                    name=Path(destination).name
                    if not fired and (kind=='root' and Path(destination)==self.runtime or kind=='release' and name==packet[3] or name==kind
                                      or kind.startswith('claude-') and Path(destination)==self.claude_skills/kind[len('claude-'):]):
                        fired=True;raise RuntimeError('crash after directory rename')
                from telegram_search_mcp import codex_registration
                original_replace=codex_registration.os.replace
                def replaced(source,destination,**kwargs):
                    nonlocal fired
                    original_replace(source,destination,**kwargs)
                    if kind=='config' and destination=='config.toml' and not fired:fired=True;raise RuntimeError('crash after config replace')
                with mock.patch.object(module,'_rename',side_effect=renamed),mock.patch.object(codex_registration.os,'replace',side_effect=replaced):
                    with self.assertRaises(RuntimeError):self.install(packet)
                self.assertTrue(fired)
                self.assertEqual(self.install(packet)['status'],'activation_required')
