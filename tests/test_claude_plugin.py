"""Claude Code plugin package and marketplace (CLIENTS-11): names, shapes, skill copies and the entry shim."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from telegram_search_mcp import __version__, product_identity as identity
from telegram_search_mcp.product_identity import check_names
from telegram_search_mcp.server import _TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins' / identity.PLUGIN_NAME
SHIM = PLUGIN / 'bin' / 'mcp-entry'
REFUSAL = '{"schema_version":1,"error_code":"runtime_not_installed"}\n'
PASSWD_LINE = 'tg_home=$(/usr/bin/env -i /usr/bin/id -P) || tg_refuse\n'


def strict_json(path: Path):
    def pairs(items):
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key')
        return dict(items)
    return json.loads(path.read_text('utf-8'), object_pairs_hook=pairs)


class ManifestTests(unittest.TestCase):
    def test_marketplace_has_the_observed_shape_and_identity_names(self):
        market = strict_json(ROOT / '.claude-plugin' / 'marketplace.json')
        self.assertEqual(set(market), {'name', 'owner', 'metadata', 'plugins'})
        self.assertEqual(market['name'], identity.MARKETPLACE_NAME)
        self.assertEqual(set(market['owner']), {'name'})
        self.assertEqual(set(market['metadata']), {'description'})
        [entry] = market['plugins']
        self.assertEqual(set(entry), {'name', 'source', 'description', 'category', 'homepage', 'license'})
        self.assertEqual(entry['name'], identity.PLUGIN_NAME)
        self.assertEqual(entry['source'], './plugins/' + identity.PLUGIN_NAME)
        self.assertTrue((ROOT / entry['source']).resolve() == PLUGIN.resolve() and PLUGIN.is_dir())
        self.assertEqual(entry['license'], 'AGPL-3.0-only')

    def test_plugin_manifest_carries_the_product_version_and_identity_name(self):
        plugin = strict_json(PLUGIN / '.claude-plugin' / 'plugin.json')
        self.assertEqual(set(plugin), {'name', 'version', 'description', 'author', 'homepage', 'repository',
                                       'license', 'keywords'})
        self.assertEqual((plugin['name'], plugin['version'], plugin['license']),
                         (identity.PLUGIN_NAME, __version__, 'AGPL-3.0-only'))
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))['project']
        self.assertEqual(plugin['repository'], project['urls']['Repository'])
        self.assertEqual(plugin['homepage'], project['urls']['Documentation'])
        self.assertEqual(set(plugin['author']), {'name'})
        self.assertTrue(plugin['keywords'] and all(type(word) is str for word in plugin['keywords']))
        market = strict_json(ROOT / '.claude-plugin' / 'marketplace.json')
        self.assertEqual(market['plugins'][0]['description'], plugin['description'])

    def test_mcp_json_runs_the_shim_under_sh_p(self):
        self.assertEqual(strict_json(PLUGIN / '.mcp.json'), {'mcpServers': {identity.PLUGIN_SERVER_NAME: {
            'command': '/bin/sh', 'args': ['-p', '${CLAUDE_PLUGIN_ROOT}/bin/mcp-entry']}}})

    def test_every_plugin_tool_name_fits_the_client_limit(self):
        plugin = strict_json(PLUGIN / '.claude-plugin' / 'plugin.json')['name']
        [server] = strict_json(PLUGIN / '.mcp.json')['mcpServers']
        check_names(plugin_name=plugin, plugin_server_name=server, tool_names=_TOOL_NAMES)
        for tool in _TOOL_NAMES:
            with self.subTest(tool=tool):
                self.assertLessEqual(len(f'mcp__plugin_{plugin}_{server}__{tool}'), identity.TOOL_NAME_LIMIT)

    def test_skill_copies_are_byte_identical(self):
        source, copy = ROOT / 'skills', PLUGIN / 'skills'
        files = sorted(p.relative_to(source) for p in source.rglob('*') if p.is_file())
        self.assertEqual(files, sorted(p.relative_to(copy) for p in copy.rglob('*') if p.is_file()))
        self.assertTrue(files)
        for name in files:
            with self.subTest(skill=str(name)):
                self.assertEqual((copy / name).read_bytes(), (source / name).read_bytes(),
                                 f'regenerate: cp skills/{name} plugins/{identity.PLUGIN_NAME}/skills/{name}')

    def test_plugin_files_are_repository_only_distribution(self):
        # Neither the sdist nor the wheel ships them; the marketplace is the repository itself.
        for path in ('MANIFEST.in', 'pyproject.toml', 'setup.py'):
            text = (ROOT / path).read_text('utf-8')
            self.assertNotIn('plugins', text, path)
            self.assertNotIn('.claude-plugin', text, path)

    def test_shim_reads_the_identity_bundle_root(self):
        text = SHIM.read_text('utf-8')
        self.assertTrue(text.startswith('#!/bin/sh -p\n'))
        self.assertIn('"$tg_home/Library/Application Support/' + identity.BUNDLE_ROOT_NAME + '"', text)
        self.assertEqual(text.count(PASSWD_LINE), 1)


class ShimTests(unittest.TestCase):
    """Runs a copy whose only change is the passwd lookup, pointed at a synthetic home."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='tg-plugin-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.root = self.home / 'Library' / 'Application Support' / identity.BUNDLE_ROOT_NAME
        self.index = hashlib.sha256(b'synthetic release index').hexdigest()
        self.release = self.root / 'releases' / self.index
        (self.release / 'python' / 'bin').mkdir(mode=0o700, parents=True)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        python = self.release / 'python' / 'bin' / 'python3'
        python.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$0\" \"$@\" > '{self.out}/argv'\n"
                          f"/usr/bin/env > '{self.out}/env'\n")
        python.chmod(0o700)
        (self.release / 'launcher.py').write_text('# synthetic\n')
        self.pointer = self.root / 'current.json'
        self.write({'schema_version': 1, 'release_root': str(self.release), 'index_sha256': self.index})

    def write(self, value, *, raw=None):
        if self.pointer.is_symlink() or self.pointer.exists():
            self.pointer.unlink()
        self.pointer.write_text(raw if raw is not None else json.dumps(value, separators=(',', ':')))
        self.pointer.chmod(0o600)

    def shim(self, passwd=None):
        text = SHIM.read_text('utf-8')
        entry = passwd if passwd is not None else f'tester:*:501:20::0:0:Test: User:{self.home}:/bin/zsh'
        path = self.base / 'mcp-entry'
        path.write_text(text.replace(PASSWD_LINE, f"tg_home='{entry}'\n"))
        return path

    def run_shim(self, path, *arguments):
        return subprocess.run(['/bin/sh', '-p', str(path), *arguments], capture_output=True, text=True, timeout=30,
                              env={'TG_PLUGIN_CANARY': 'leak', 'PATH': '/usr/bin:/bin', 'HOME': str(self.base)})

    def assertRefused(self, done):
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, '', REFUSAL))
        self.assertFalse((self.out / 'argv').exists())

    def assertRuns(self, path):
        done = self.run_shim(path)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        (self.out / 'argv').unlink()

    def test_execs_the_exact_launcher_argv_under_env_i(self):
        done = self.run_shim(self.shim())
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        argv = (self.out / 'argv').read_text().splitlines()
        self.assertEqual(argv, [str(self.release / 'python/bin/python3'), '-I', '-S', '-B',
                                str(self.release / 'launcher.py'), 'mcp'])
        names = {row.split('=', 1)[0] for row in (self.out / 'env').read_text().splitlines()}
        self.assertFalse(names & {'TG_PLUGIN_CANARY', 'HOME', 'PATH'}, names)

    def test_refuses_a_missing_malformed_or_traversing_pointer(self):
        good = {'schema_version': 1, 'release_root': str(self.release), 'index_sha256': self.index}
        cases = {
            'missing': None,
            'not_json': 'not json',
            'array': '[1]',
            'traversal': dict(good, release_root=str(self.root / 'releases' / '..' / 'releases' / self.index)),
            'other_release': dict(good, release_root=str(self.root / 'releases' / ('b' * 64))),
            'outside_root': dict(good, release_root=str(self.base / 'releases' / self.index)),
            'upper_hex': dict(good, index_sha256=self.index.upper(),
                              release_root=str(self.root / 'releases' / self.index.upper())),
            'short_hash': dict(good, index_sha256='a' * 63, release_root=str(self.root / 'releases' / ('a' * 63))),
            'no_index': {'schema_version': 1, 'release_root': str(self.release)},
            'schema_2': dict(good, schema_version=2),
            'oversized': json.dumps(good) + ' ' * 4096,
        }
        path = self.shim()
        for name, value in cases.items():
            with self.subTest(case=name):
                if value is None:
                    self.pointer.unlink()
                elif type(value) is str:
                    self.write(None, raw=value)
                else:
                    self.write(value)
                self.assertRefused(self.run_shim(path))
                self.write(good)
                self.assertRuns(path)

    def test_refuses_unsafe_pointer_metadata_and_incomplete_releases(self):
        path = self.shim()
        good = self.pointer.read_text()
        self.pointer.chmod(0o620)
        self.assertRefused(self.run_shim(path))
        self.pointer.chmod(0o600)
        self.assertRuns(path)
        other = self.base / 'elsewhere.json'
        other.write_text(good)
        other.chmod(0o600)
        self.pointer.unlink()
        self.pointer.symlink_to(other)
        self.assertRefused(self.run_shim(path))
        self.pointer.unlink()
        os.link(other, self.pointer)
        self.assertRefused(self.run_shim(path))
        self.pointer.unlink()
        self.write(None, raw=good)
        self.assertRuns(path)
        (self.release / 'launcher.py').rename(self.base / 'launcher.py')
        self.assertRefused(self.run_shim(path))
        (self.base / 'launcher.py').rename(self.release / 'launcher.py')
        self.assertRuns(path)
        moved = self.base / 'moved-release'
        self.release.rename(moved)
        self.release.symlink_to(moved, target_is_directory=True)
        self.assertRefused(self.run_shim(path))

    def test_refuses_an_unusable_passwd_home_or_any_argument(self):
        for entry in ('tester:*:501:20::0:0:Test:relative/home:/bin/zsh',
                      f'tester:*:501:20::0:0:Test:{self.home}/../home:/bin/zsh', 'garbage', ''):
            with self.subTest(entry=entry):
                self.assertRefused(self.run_shim(self.shim(entry)))
        # The shipped shim refuses arguments before any lookup.
        self.assertRefused(self.run_shim(SHIM, 'extra'))
        self.assertRefused(self.run_shim(self.shim(), '--installation-root', str(self.root)))


if __name__ == '__main__':
    unittest.main()
