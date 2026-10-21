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
        from . import __version__
        check_manifest(manifest, __version__)
        if index.release_version != __version__ or manifest['target'] != index.target:
            raise BundleError('manifest_mismatch')
        lock = read(root / 'tdlib-lock.json')
        if digest(lock) != manifest['native_lock_sha256']:
            raise BundleError('native_lock_mismatch')
        check_publisher_lock(manifest, lock)
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


MANIFEST_V1_KEYS = frozenset({'schema_version','package_version','target','minimum_os','contract_version',
                              'schema_fingerprint','tools','native_lock_sha256','skills','source_completeness',
                              'publication'})
MANIFEST_KEYS = {1: MANIFEST_V1_KEYS, 2: MANIFEST_V1_KEYS | {'publisher'}}
PUBLICATIONS = ('unreleased', 'published')
SOURCE_COMPLETENESS = ('incomplete', 'complete')
PUBLISHER_KINDS = ('adhoc', 'developer-id')


def _team_id(value):
    return type(value) is str and len(value) == 10 and all(c in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in value)


def check_manifest(manifest, version):
    """Closed release manifest grammar.

    v1: unsigned and unreleased only. v2 adds `publisher {kind, team_id}`; `published` requires
    `source_completeness: complete` and a Developer ID publisher. Anything else refuses.
    """
    keys = MANIFEST_KEYS.get(manifest.get('schema_version')) if type(manifest.get('schema_version')) is int else None
    if (keys is None or set(manifest) != keys or manifest['package_version'] != version
            or manifest['target'] != 'macos-arm64' or manifest['minimum_os'] != '26.0'
            or type(manifest['contract_version']) is not int or manifest['contract_version'] != 1
            or not _hash(manifest['schema_fingerprint']) or not _hash(manifest['native_lock_sha256'])
            or type(manifest['tools']) is not list or not 1 <= len(manifest['tools']) <= 128
            or len(set(manifest['tools'])) != len(manifest['tools'])
            or any(type(t) is not str or not t.replace('_','').isalnum() or not t.isascii() for t in manifest['tools'])
            or type(manifest['publication']) is not str or manifest['publication'] not in PUBLICATIONS
            or type(manifest['source_completeness']) is not str
            or manifest['source_completeness'] not in SOURCE_COMPLETENESS):
        raise BundleError('manifest_mismatch')
    if manifest['schema_version'] == 1:
        if manifest['publication'] != 'unreleased' or manifest['source_completeness'] != 'incomplete':
            raise BundleError('manifest_mismatch')
        return
    publisher = manifest['publisher']
    if (type(publisher) is not dict or set(publisher) != {'kind','team_id'}
            or type(publisher['kind']) is not str or publisher['kind'] not in PUBLISHER_KINDS
            or (publisher['team_id'] is not None if publisher['kind'] == 'adhoc' else not _team_id(publisher['team_id']))):
        raise BundleError('manifest_mismatch')
    if manifest['publication'] == 'published' and (manifest['source_completeness'] != 'complete'
                                                   or publisher['kind'] != 'developer-id'):
        raise BundleError('manifest_mismatch')


def check_publisher_lock(manifest, lock_data):
    """A checked manifest's publisher names exactly the native lock's signature kind and Team ID.

    A v1 manifest has no publisher and pairs only with an ad-hoc (or v1) lock.
    """
    from .native_runtime import NativeRuntimeError, lock_signature, parse_native_manifest
    try:
        signature = lock_signature(parse_native_manifest(lock_data))
    except NativeRuntimeError:
        raise BundleError('native_lock_mismatch') from None
    publisher = manifest['publisher'] if manifest['schema_version'] == 2 else {'kind': 'adhoc', 'team_id': None}
    if signature != (publisher['kind'], publisher['team_id']):
        raise BundleError('publisher_mismatch')


# Claude Code: `--claude-code user` ends in one of the first five; `plugin` and `skip` record themselves.
CLAUDE_CODE_MODES = ('user', 'plugin', 'skip')
CLAUDE_USER_STATUSES = ('registered', 'reused', 'failed', 'unavailable', 'not_found')
CLAUDE_STATUSES = CLAUDE_USER_STATUSES + ('plugin', 'skipped')
CLAUDE_MODE_STATUSES = {'user': CLAUDE_USER_STATUSES, 'plugin': ('plugin',), 'skip': ('skipped',)}
CLAUDE_SKILLS_STATUSES = ('installed', 'skipped')
CLAUDE_DESKTOP_STATUSES = ('registered', 'reused', 'failed', 'unavailable', 'skipped')
_BINDING_KEYS = {'directory': {'kind', 'dev', 'ino', 'uid', 'mode'},
                 'file': {'kind', 'dev', 'ino', 'uid', 'mode', 'bytes', 'nlink'}}


def _binding(value, kind):
    return (type(value) is dict and value.get('kind') == kind and set(value) == _BINDING_KEYS[kind]
            and all(type(value[k]) is int and value[k] >= 0 for k in _BINDING_KEYS[kind] - {'kind'}))


def _skill_binding(value):
    """Shape of a recorded skill publication: directory plus its SKILL.md, no staging path."""
    return (type(value) is dict and set(value) == {'object', 'child'}
            and _binding(value['object'], 'directory') and _binding(value['child'], 'file'))


# Receipt v1 (0.53 wave 2) stays admitted. v2 names its transaction, carries the Claude Desktop ownership,
# the previous release (rollback) and `--adopt-legacy` backup records. Launch checks only their shape.
RECEIPT_V1_KEYS = frozenset({'schema_version','index_sha256','release_root','installation_root','codex_home',
                             'skills_root','claude_skills_root','user_home','package_version','claude_code_mode',
                             'registration_sha256','skills','publication_objects','claude_code','claude_skills',
                             'claude_desktop','status'})
RECEIPT_KEYS = {1: RECEIPT_V1_KEYS, 2: RECEIPT_V1_KEYS | {'transaction','claude_desktop_ownership','previous','adopted'}}
ADOPTION_CLIENTS = ('codex', 'claude_code', 'claude_desktop', 'service')
MAX_BACKUP_BYTES = 1024 * 1024
_RECEIPT_ID = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')


def transaction_name(value):
    """Closed transaction directory grammar: `install_<24 hex>` or `change_<1..999>`."""
    if type(value) is not str: return False
    if value.startswith('install_'): return len(value) == 32 and all(c in '0123456789abcdef' for c in value[8:])
    number = value[7:] if value.startswith('change_') else ''
    return number.isascii() and number.isdigit() and 1 <= len(number) <= 3 and number[0] != '0'


def receipt_transaction(receipt):
    """The transaction directory a shape-checked receipt is bound to."""
    return receipt['transaction'] if receipt['schema_version'] == 2 else 'install_' + receipt['index_sha256'][:24]


def _identifier(value):
    return (type(value) is str and 1 <= len(value) <= 64 and value[0].isalpha() and value.isascii()
            and set(value) <= _RECEIPT_ID)


def desktop_ownership(value):
    """Shape of a Claude Desktop ownership receipt (codex_registration.Receipt, client claude-desktop)."""
    return (type(value) is dict and set(value) == {'schema_version','client','scope','installation_id','transaction_id',
                                                   'pre_subtree_hash','post_subtree_hash','pre_subtree'}
            and type(value['schema_version']) is int and value['schema_version'] == 1
            and value['client'] == 'claude-desktop' and value['scope'] == 'user'
            and _identifier(value['installation_id']) and _identifier(value['transaction_id'])
            and value['pre_subtree_hash'] is None and value['pre_subtree'] is None and _hash(value['post_subtree_hash']))


def check_receipt_v2(receipt, installation):
    """The v2 keys' closed shapes; raises BundleError('receipt_mismatch')."""
    transaction, previous, adopted = receipt['transaction'], receipt['previous'], receipt['adopted']
    ownership = receipt['claude_desktop_ownership']
    if (not transaction_name(transaction)
            or transaction.startswith('install_') and transaction != 'install_' + str(receipt['index_sha256'])[:24]
            or ownership is not None and not desktop_ownership(ownership)
            or previous is not None and (type(previous) is not dict
                                         or set(previous) != {'index_sha256','release_root','package_version','transaction'}
                                         or not _hash(previous['index_sha256']) or previous['index_sha256'] == receipt['index_sha256']
                                         or previous['release_root'] != str(installation / 'releases' / previous['index_sha256'])
                                         or type(previous['package_version']) is not str
                                         or not 1 <= len(previous['package_version']) <= 64
                                         or not transaction_name(previous['transaction']))
            or type(adopted) is not dict or set(adopted) - set(ADOPTION_CLIENTS)
            or any(type(v) is not dict or set(v) != {'sha256','bytes','transaction'} or not _hash(v['sha256'])
                   or type(v['bytes']) is not int or not 0 < v['bytes'] <= MAX_BACKUP_BYTES
                   or not transaction_name(v['transaction']) for v in adopted.values())):
        raise BundleError('receipt_mismatch')


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
        version=receipt.get('schema_version')
        keys=RECEIPT_KEYS.get(version) if type(version) is int else None
        if (keys is None or set(receipt)!=keys
                or receipt['installation_root']!=str(installation) or receipt['package_version']!=manifest['package_version']
                or receipt['status']!='activation_required' or receipt['skills']!=manifest['skills']
                or not _hash(receipt['registration_sha256'])
                # Client registrations (Codex config.toml, Claude Code's entry) and the skill copies in
                # `~/.agents/skills` and `~/.claude/skills` belong to external clients the user may
                # reconfigure or wipe. A launch, which runs every time, checks only the records' shape and
                # never reads, drives or depends on them; the installer verifies them at install and repeat time.
                or type(receipt['claude_code_mode']) is not str or receipt['claude_code_mode'] not in CLAUDE_CODE_MODES
                or type(receipt['claude_code']) is not str
                or receipt['claude_code'] not in CLAUDE_MODE_STATUSES[receipt['claude_code_mode']]
                or type(receipt['claude_skills']) is not str or receipt['claude_skills'] not in CLAUDE_SKILLS_STATUSES
                or receipt['claude_code_mode']!='user' and receipt['claude_skills']!='skipped'
                # Claude Desktop's config, like the others, is never read at launch: the status only.
                or type(receipt['claude_desktop']) is not str or receipt['claude_desktop'] not in CLAUDE_DESKTOP_STATUSES):
            raise BundleError('receipt_mismatch')
        if version==2:check_receipt_v2(receipt,installation)
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
            transaction=installation/'transactions'/receipt_transaction(receipt)
            owned=read(transaction/'registration.json',mode=0o600)
            if digest(owned)!=receipt['registration_sha256']:raise BundleError('registration_mismatch')
            # Bind receipt's home to the transaction's immutable declaration,
            # without using it as an environment or MCP-driven home override.
            journal=unique_json(read(transaction/'journal.json',mode=0o600))
            if any(journal.get(k)!=receipt[k] for k in ('installation_root','codex_home','skills_root','claude_skills_root','user_home','index_sha256','release_root','package_version','claude_code_mode')):
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
