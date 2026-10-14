import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from telegram_search_mcp.release_archive import load_archive_index, extract_verified_archive, ReleaseArchiveError


class BundleArchiveTests(unittest.TestCase):
    def fixture(self, root, records=None, trailing=b''):
        members = records or [('safe/value.txt', b'checked', tarfile.REGTYPE)]
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w',format=tarfile.USTAR_FORMAT) as tar:
            for name,data,kind in members:
                item=tarfile.TarInfo(name); item.size=len(data); item.type=kind; item.linkname='outside'
                tar.addfile(item,io.BytesIO(data))
        archive=root/'archive.tar.gz'; archive.write_bytes(gzip.compress(buffer.getvalue()+trailing,mtime=0)); archive.chmod(0o400)
        body=dict(schema_version=1,target='macos-arm64',release_version='0.52.0.dev0',archive_bytes=archive.stat().st_size,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),unpacked_bytes=7,
            files=[dict(path='safe/value.txt',bytes=7,sha256=hashlib.sha256(b'checked').hexdigest(),executable=False)])
        return archive,load_archive_index(json.dumps(body).encode())

    def test_current_dev_archive_extracts_only_authenticated_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); archive,index=self.fixture(root)
            extract_verified_archive(archive,index,root/'new')
            self.assertEqual((root/'new/safe/value.txt').read_bytes(),b'checked')
            self.assertEqual((root/'new/safe/value.txt').stat().st_mode&0o777,0o400)

    def test_duplicate_traversal_link_illegal_type_tail_and_corruption_are_retained(self):
        cases=[ [('safe/value.txt',b'checked',tarfile.REGTYPE)]*2,
            [('../outside',b'checked',tarfile.REGTYPE)],
            [('safe/value.txt',b'checked',tarfile.SYMTYPE)],
            [('safe/value.txt',b'checked',tarfile.FIFOTYPE)],
            [('safe/value.txt',b'changed',tarfile.REGTYPE)]]
        for records in cases:
            with self.subTest(records=records),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve(); archive,index=self.fixture(root,records)
                with self.assertRaises(ReleaseArchiveError): extract_verified_archive(archive,index,root/'new')
                self.assertTrue(archive.exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); archive,index=self.fixture(root,trailing=b'nonzero')
            with self.assertRaises(ReleaseArchiveError): extract_verified_archive(archive,index,root/'new')

    def test_assembler_authenticates_product_and_current_contract(self):
        path=Path(__file__).parents[1]/'packaging/assemble_bundle.py'
        self.assertTrue(path.is_file(),'missing current bundle producer')
        spec=importlib.util.spec_from_file_location('assembly',path)
        module=importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name]=module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); payload=root/'payload';payload.mkdir(mode=0o700)
            (payload/'unindexed-link').symlink_to('/etc/passwd')
            with self.assertRaises(module.ReleasePackageError): module.assemble_bundle(payload,root/'output')
            self.assertFalse((root/'output').exists())
