"""Receipt-bound local runtime context; no environment or MCP path selector."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys

from .bundle_paths import (open_owned_file, open_owned_directory, file_metadata_identity,
                           require_object_binding, RuntimePathError)
from .release_archive import load_archive_index, MAX_INDEX_BYTES


class BundleError(ValueError):
    """Redacted admission or installed binding failure."""


def require_platform():
    try:
        major = int(platform.mac_ver()[0].split('.')[0])
    except ValueError:
        major = 0
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or major < 26:
        raise BundleError('unsupported_platform')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path, maximum=65536, mode=None):
    try:
        with open_owned_file(path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > maximum or mode is not None and stat.S_IMODE(before.st_mode) != mode:
                raise BundleError('unsafe_metadata')
            data = stream.read(maximum + 1)
            if len(data) != before.st_size or file_metadata_identity(before) != file_metadata_identity(os.fstat(stream.fileno())):
                raise BundleError('concurrent_change')
            return data
    except (OSError, RuntimePathError):
        raise BundleError('unsafe_metadata') from None


def unique_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('duplicate')
            out[key] = value
        return out
    try:
        value = json.loads(data, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict: raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BundleError('invalid_metadata') from None


def validate_tree(root, index_path, index_sha256, *, installed=False):
    """Authenticate exact immutable files, including extra-entry and mode checks."""
    try:
        root = Path(root)
        with open_owned_directory(root, private=True): pass
        content = read(Path(index_path), MAX_INDEX_BYTES)
        if digest(content) != index_sha256: raise BundleError('index_mismatch')
        index = load_archive_index(content)
        names = {f.path for f in index.files}
        directories = {str(p) for name in names for p in Path(name).parents if str(p) != '.'}
        extras = {'bundle-index.json'} if installed else set()
        actual = set()
        for parent, dirs, files in os.walk(root, followlinks=False):
            parent = Path(parent)
            with open_owned_directory(parent, private=True): pass
            for name in dirs:
                relative = str((parent / name).relative_to(root))
                if relative not in directories: raise BundleError('unexpected_entry')
                with open_owned_directory(parent / name, private=True): pass
            for name in files: actual.add(str((parent / name).relative_to(root)))
        if actual != names | extras: raise BundleError('unexpected_entry')
        for record in index.files:
            with open_owned_file(root / record.path) as stream:
                before = os.fstat(stream.fileno())
                if (before.st_size != record.bytes or stat.S_IMODE(before.st_mode) != (0o500 if record.executable else 0o400)):
                    raise BundleError('file_mismatch')
                h = hashlib.sha256()
                left = record.bytes
                while left:
                    chunk = stream.read(min(left, 1024 * 1024))
                    if not chunk: raise BundleError('file_mismatch')
                    h.update(chunk); left -= len(chunk)
                if stream.read(1) or h.hexdigest() != record.sha256 or file_metadata_identity(before) != file_metadata_identity(os.fstat(stream.fileno())):
                    raise BundleError('file_mismatch')
        manifest = unique_json(read(root / 'manifest/release.json'))
        keys = {'schema_version','package_version','target','minimum_os','contract_version','schema_fingerprint',
                'tools','native_lock_sha256','skills','source_completeness','publication'}
        from . import __version__
        if (set(manifest) != keys or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
                or manifest['package_version'] != __version__ or index.release_version != __version__
                or manifest['target'] != index.target or manifest['minimum_os'] != '26.0'
                or type(manifest['contract_version']) is not int or manifest['contract_version'] != 1
                or manifest['publication'] != 'unreleased' or manifest['source_completeness'] != 'incomplete'
                or not _hash(manifest['schema_fingerprint']) or not _hash(manifest['native_lock_sha256'])
                or type(manifest['tools']) is not list or not 1 <= len(manifest['tools']) <= 128
                or len(set(manifest['tools'])) != len(manifest['tools'])
                or any(type(t) is not str or not t.replace('_','').isalnum() or not t.isascii() for t in manifest['tools'])):
            raise BundleError('manifest_mismatch')
        if digest(read(root / 'tdlib-lock.json')) != manifest['native_lock_sha256']:
            raise BundleError('native_lock_mismatch')
        skills = manifest['skills']
        if (type(skills) is not list or len(skills) != 2
                or {s.get('id') for s in skills if isinstance(s, dict)} != {'telegram-search','telegram-setup'}):
            raise BundleError('skill_mismatch')
        for skill in skills:
            if set(skill) != {'id','sha256'} or digest(read(root / 'skills' / skill['id'] / 'SKILL.md')) != skill['sha256']:
                raise BundleError('skill_mismatch')
        return index, manifest
    except BundleError: raise
    except (OSError, ValueError, TypeError, KeyError, RuntimePathError):
        raise BundleError('bundle_admission_failed') from None


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


CLAUDE_STATUSES = ('registered', 'reused', 'failed', 'unavailable', 'not_found')
_BINDING_KEYS = {'directory': {'kind', 'dev', 'ino', 'uid', 'mode'},
                 'file': {'kind', 'dev', 'ino', 'uid', 'mode', 'bytes', 'nlink'}}


def _binding(value, kind):
    return (type(value) is dict and value.get('kind') == kind and set(value) == _BINDING_KEYS[kind]
            and all(type(value[k]) is int and value[k] >= 0 for k in _BINDING_KEYS[kind] - {'kind'}))


def _skill_binding(value):
    """Shape of a recorded skill publication: directory plus its SKILL.md, no staging path."""
    return (type(value) is dict and set(value) == {'object', 'child'}
            and _binding(value['object'], 'directory') and _binding(value['child'], 'file'))


@dataclass(frozen=True)
class Selection:
    root: Path
    index_sha256: str

    def check(self):
        installation = self.root.parent.parent
        receipt = unique_json(read(installation / 'receipt.json', mode=0o600))
        pointer = unique_json(read(installation / 'current.json', mode=0o600))
        if (receipt.get('release_root') != str(self.root) or receipt.get('index_sha256') != self.index_sha256
                or pointer != {'schema_version':1,'release_root':str(self.root),'index_sha256':self.index_sha256}
                or self.root != installation / 'releases' / self.index_sha256):
            raise BundleError('receipt_mismatch')
        index,manifest=validate_tree(self.root, self.root / 'bundle-index.json', self.index_sha256, installed=True)
        keys={'schema_version','index_sha256','release_root','installation_root','codex_home','skills_root',
              'claude_skills_root','user_home','package_version','registration_sha256','skills','publication_objects',
              'claude_code','claude_skills','status'}
        if (set(receipt)!=keys or type(receipt['schema_version']) is not int or receipt['schema_version']!=1
                or receipt['installation_root']!=str(installation) or receipt['package_version']!=manifest['package_version']
                or receipt['status']!='activation_required' or receipt['skills']!=manifest['skills']
                or not _hash(receipt['registration_sha256'])
                # Client registrations (Codex config.toml, Claude Code's entry) and the skill copies in
                # `~/.agents/skills` and `~/.claude/skills` belong to external clients the user may
                # reconfigure or wipe. A launch, which runs every time, checks only the records' shape and
                # never reads, drives or depends on them; the installer verifies them at install and repeat time.
                or type(receipt['claude_code']) is not str or receipt['claude_code'] not in CLAUDE_STATUSES
                or type(receipt['claude_skills']) is not str or receipt['claude_skills'] not in ('installed','skipped')):
            raise BundleError('receipt_mismatch')
        try:
            objects=receipt['publication_objects']
            skill_names={'telegram-search','telegram-setup'}
            if receipt['claude_skills']=='installed':skill_names|={'claude-telegram-search','claude-telegram-setup'}
            if (type(objects) is not dict or set(objects)!={'release'}|skill_names
                    or not all(_skill_binding(objects[name]) for name in skill_names)):
                raise BundleError('publication_mismatch')
            require_object_binding(self.root,objects['release']['object'])
            paths={k:Path(receipt[k]) for k in ('codex_home','skills_root','claude_skills_root','user_home')}
            if any(not p.is_absolute() or '..' in p.parts for p in paths.values()):raise BundleError('receipt_mismatch')
            # Installation stores only nonsecret local registration authority.
            # B1A session/Keychain/service identity is still derived from the OS
            # user's home; no receipt field changes that session selection.
            owned=read(installation/'transactions'/('install_'+self.index_sha256[:24])/'registration.json',mode=0o600)
            if digest(owned)!=receipt['registration_sha256']:raise BundleError('registration_mismatch')
            # Bind receipt's home to the transaction's immutable declaration,
            # without using it as an environment or MCP-driven home override.
            journal=unique_json(read(installation/'transactions'/('install_'+self.index_sha256[:24])/'journal.json',mode=0o600))
            if any(journal.get(k)!=receipt[k] for k in ('installation_root','codex_home','skills_root','claude_skills_root','user_home','index_sha256','release_root','package_version')):
                raise BundleError('receipt_mismatch')
        except BundleError:raise
        except (OSError,ValueError,TypeError,AttributeError,RuntimePathError):raise BundleError('installed_binding_mismatch') from None
        return index,manifest

    def arguments(self, action):
        self.check()
        return [str(self.root / 'python/bin/python3'), '-I', '-S', '-B', str(self.root / 'launcher.py'), action]


_SELECTION = None


def activate(root, index_sha256):
    """Called in-process only after the standalone launcher admits its closed tree."""
    global _SELECTION
    selection = Selection(Path(root), index_sha256)
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode
            or Path(sys.executable).absolute() != selection.root / 'python/bin/python3'
            or Path(__file__).absolute() != selection.root / 'app/telegram_search_mcp/bundle_runtime.py'):
        raise BundleError('launcher_context_required')
    selection.check()
    if unique_json(read(selection.root.parent.parent/'receipt.json',mode=0o600))['user_home']!=str(Path.home()):
        raise BundleError('os_user_home_mismatch')
    _SELECTION = selection


def selected_native():
    if _SELECTION is None: return None
    _SELECTION.check()
    from .native_runtime import parse_native_manifest, validate_native
    lock = parse_native_manifest(read(_SELECTION.root / 'tdlib-lock.json'))
    return validate_native(lock, 'macos-arm64', directory=_SELECTION.root / 'native').library_path


def broker_arguments():
    return None if _SELECTION is None else _SELECTION.arguments('broker')


def runtime_pin():
    if _SELECTION is None: return 'Homebrew TDLib HEAD-d1085f9 legacy tdjson ABI'
    _SELECTION.check()
    return 'receipt-bound bundled TDLib d1085f9 legacy tdjson ABI; macos-arm6426+'
