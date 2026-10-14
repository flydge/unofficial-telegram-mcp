"""Safe TOML merge and compare-before-replace over disposable private files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from telegram_search_mcp import codex_registration as registration


class RegistrationTests(unittest.TestCase):
    def test_isolated_form_accepts_trusted_paths_containing_secret_words(self):
        for name in ['token','phone','secret','qr','credential','api_hash']:
            with self.subTest(name=name):
                root=Path('/synthetic')/name/'runtime/releases'/('a'*64)
                value=registration.Registration(str(root/'python/bin/python3'),('-I','-S','-B',str(root/'launcher.py'),'mcp'),{},('_manifest',))
                self.assertEqual(value.args[-1],'mcp')
        for args in [('-I','-S','-B','/foreign/launcher.py','mcp'),('--api-hash','secret'),('-I','-S','-B','/synthetic/launcher.py','setup')]:
            with self.subTest(args=args),self.assertRaises(registration.ConfigConflict):
                registration.Registration('/synthetic/python/bin/python3',args,{},('_manifest',))
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.path=self.root/'config.toml'
        self.desired=registration.Registration('/owned/python/bin/python3',('-I','-S','-B','/owned/launcher.py','mcp'),{},('_manifest',))

    def test_interleaved_neighbor_comments_and_tables_are_preserved(self):
        source=b'# top\nmodel="example"\n[mcp_servers.other]\ncommand="/other" # retained\n[projects."/example"]\ntrust_level="trusted"\n'
        self.path.write_bytes(source);self.path.chmod(0o600)
        change=registration.merge_registration(source,self.desired)
        receipt=registration.commit_transaction(change,change.expected_source_hash,self.path,installation_id='install_test',transaction_id='first')
        self.assertTrue(self.path.read_bytes().startswith(source))
        self.assertTrue(registration.merge_registration(self.path.read_bytes(),self.desired,receipt).is_noop)

    def test_drift_after_planning_preserves_actual_concurrent_config(self):
        source=b'# original\n';self.path.write_bytes(source);self.path.chmod(0o600)
        change=registration.merge_registration(source,self.desired)
        self.path.write_bytes(b'# newer\n')
        with self.assertRaises(registration.ConfigConflict):
            registration.commit_transaction(change,change.expected_source_hash,self.path,installation_id='install_test',transaction_id='first')
        self.assertEqual(self.path.read_bytes(),b'# newer\n')

    def test_metadata_drift_during_read_is_refused(self):
        self.path.write_bytes(b'# original\n');self.path.chmod(0o600)
        original=os.fstat
        seen=0
        def inspect(fd):
            nonlocal seen
            value=original(fd)
            if value.st_ino==self.path.stat().st_ino:
                seen+=1
                if seen==2:self.path.chmod(0o644);value=original(fd)
            return value
        with mock.patch('os.fstat',side_effect=inspect):
            with self.assertRaises(registration.ConfigConflict):registration.read_config(self.path)

    def test_foreign_matching_name_and_symlink_parent_are_refused(self):
        with self.assertRaises(registration.ConfigConflict):
            registration.merge_registration(b'[mcp_servers.telegram_search]\ncommand="/foreign"\n',self.desired)
        link=self.root/'alias';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(registration.ConfigConflict):registration.read_config(link/'config.toml')
