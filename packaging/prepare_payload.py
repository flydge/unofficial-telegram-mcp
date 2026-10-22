"""Build the whole prepared payload for assemble_bundle.py from pinned inputs.

Development host only. Inputs: `git archive HEAD` of this checkout, whose packaging/payload-lock.json
(PBS CPython archive, the TDLib source archive and every wheel, each by URL, bytes and sha256, plus the
OpenSSL notice hashes) is the only lock read (never the working tree's), and a prepare_native.py
output directory whose dylibs must equal HEAD's packaging/tdlib-lock.json, whose notices must equal
the payload lock's hashes and whose build provenance must agree with that native lock.
Downloads go only to a private content-addressed cache and are used only after their pinned
hash matches; with --offline nothing is fetched. Nothing here executes downloaded code: wheels
are unpacked against their RECORD by this script, and the manifest is computed by importing the
exported HEAD package with the build interpreter. The standalone Python is pruned to what the
product uses (no Tcl/Tk, _tkinter, idle, turtledemo, ensurepip, pip, include/, share/ or
duplicate executables; only python/bin/python3), and the Mach-O inventory, counted by magic,
is recorded in provenance. The same inputs give the same tree. Stdlib only; refusals are
fixed codes without paths or provider text.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
LOCK = 'packaging/payload-lock.json'  # read from the HEAD export only
CODES = frozenset({'output_refused', 'cache_refused', 'lock_invalid', 'input_unavailable', 'input_mismatch',
                   'archive_refused', 'wheel_refused', 'source_refused', 'native_mismatch', 'manifest_failed',
                   'prune_refused', 'payload_refused'})
HOSTS = frozenset({'files.pythonhosted.org', 'github.com', 'codeload.github.com'})
SKILLS = ('telegram-search', 'telegram-setup')
NATIVE_NAMES = ('libtdjson.dylib', 'libssl.3.dylib', 'libcrypto.3.dylib')
OPENSSL_NOTICES = ('AUTHORS.md', 'LICENSE.txt', 'README.md')
PROVENANCE = ('provenance/tdlib-build.json', 'provenance/openssl-build.json')
REDACTED_PATH = 'private-machine-path-redacted; evidence hash retained'  # prepare_native's receipt redaction
MAX_LOCK = 1 << 20
MAX_NOTICE = 1 << 20
SCHEMA = 'td/generate/scheme/td_api.tl'
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_MEMBER = 256 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_UNPACKED = 1024 * 1024 * 1024
MAX_SOURCE = 128 * 1024 * 1024
FETCH_TIMEOUT = 120
TOOL_TIMEOUT = 300
CHUNK = 1024 * 1024
TOOL_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
# The launcher's two version literals: the admitted index release_version and its --help line.
LAUNCHER_SITES = (re.compile(r"value\['release_version'\]!='([^'\n]*)'"), re.compile(r"print\('([^'\n]*): mcp \|"))
VERSION = re.compile(r'^[0-9A-Za-z.+-]{1,64}$')
# This package's own version: the one release version grammar (docs/releasing.md, Versions).
PACKAGE_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})'
                             r'(?:(?:a|b|rc)(0|[1-9][0-9]{0,5}))?(?:\.dev(0|[1-9][0-9]{0,5}))?')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
HASH = re.compile(r'^[0-9a-f]{64}$')
LICENSE_NAMES = re.compile(r'^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)', re.I)
# Standalone Python: kept bin entry, the closed lib/ result and what the product never uses.
PYTHON_BIN = 'bin/python3.14'
PYTHON_LIB = frozenset({'libpython3.14.dylib', 'python3.14'})
PRUNED_LIB = frozenset({'itcl4.3.8', 'tcl9', 'tcl9.0', 'tk9.0', 'thread3.0.6', 'libtcl9.0.dylib',
                        'libtcl9tk9.0.dylib', 'pkgconfig'})
PRUNED_STDLIB = ('tkinter/', 'idlelib/', 'turtledemo/', 'ensurepip/', 'site-packages/pip/', 'site-packages/pip-',
                 'lib-dynload/_tkinter.')
MACHO = {b'\xcf\xfa\xed\xfe': 'mach-o-64', b'\xce\xfa\xed\xfe': 'mach-o-32', b'\xca\xfe\xba\xbe': 'universal',
         b'\xca\xfe\xba\xbf': 'universal-64'}
MANIFEST_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import telegram_search_mcp
from telegram_search_mcp import __version__
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.contract import CONTRACT_VERSION, schema_document, schema_fingerprint
from telegram_search_mcp.server import build_server
if telegram_search_mcp.__file__ != sys.argv[1] + '/telegram_search_mcp/__init__.py':
    raise SystemExit(3)
print(json.dumps({'package_version': __version__, 'contract_version': CONTRACT_VERSION,
                  'schema_fingerprint': schema_fingerprint(),
                  'tools': [t['name'] for t in schema_document(build_server(policy=RuntimePolicy()))['tools']]}))
'''


class PayloadError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'payload_refused')


def _need(condition, code):
    if not condition:
        raise PayloadError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError
            out[key] = value
        return out
    return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


# Lock -------------------------------------------------------------------------------------------

def _pin(value, keys):
    _need(type(value) is dict and set(value) == keys, 'lock_invalid')
    url = urllib.parse.urlsplit(value['url']) if type(value['url']) is str else None
    _need(url is not None and url.scheme == 'https' and url.hostname in HOSTS and not url.query and not url.fragment
          and len(value['url']) <= 512, 'lock_invalid')
    _need(type(value['sha256']) is str and HASH.match(value['sha256']) is not None, 'lock_invalid')
    _need(type(value['bytes']) is int and 1 <= value['bytes'] <= MAX_DOWNLOAD, 'lock_invalid')
    return value


def load_lock(data):
    """Closed payload lock: python, tdlib_source, OpenSSL notice hashes, app wheels, installer wheels."""
    try:
        lock = _json(data)
    except (ValueError, UnicodeError, RecursionError):
        raise PayloadError('lock_invalid') from None
    _need(type(lock) is dict and set(lock) == {'schema_version', 'target', 'python', 'tdlib_source', 'native_notices',
                                               'app', 'installer'}
          and lock['schema_version'] == 1 and type(lock['schema_version']) is int and lock['target'] == 'macos-arm64',
          'lock_invalid')
    notices = lock['native_notices']
    _need(type(notices) is dict and set(notices) == {'openssl_version', 'files'} and type(notices['openssl_version']) is str
          and VERSION.match(notices['openssl_version']) is not None and type(notices['files']) is dict
          and tuple(sorted(notices['files'])) == OPENSSL_NOTICES, 'lock_invalid')
    for pin in notices['files'].values():
        _need(type(pin) is dict and set(pin) == {'bytes', 'sha256'} and type(pin['bytes']) is int
              and 1 <= pin['bytes'] <= MAX_NOTICE and type(pin['sha256']) is str and HASH.match(pin['sha256']), 'lock_invalid')
    python = _pin(lock['python'], {'version', 'pbs_tag', 'url', 'bytes', 'sha256'})
    _need(all(type(python[k]) is str and VERSION.match(python[k]) for k in ('version', 'pbs_tag')), 'lock_invalid')
    source = _pin(lock['tdlib_source'], {'commit', 'url', 'bytes', 'sha256', 'files', 'unpacked_bytes'})
    _need(type(source['commit']) is str and re.match(r'^[0-9a-f]{40}$', source['commit'])
          and type(source['files']) is int and 1 <= source['files'] <= MAX_MEMBERS
          and type(source['unpacked_bytes']) is int and 1 <= source['unpacked_bytes'] <= MAX_SOURCE, 'lock_invalid')
    seen = set()
    for group in ('app', 'installer'):
        _need(type(lock[group]) is list and 1 <= len(lock[group]) <= 128, 'lock_invalid')
        for wheel in lock[group]:
            _pin(wheel, {'project', 'version', 'filename', 'url', 'bytes', 'sha256'})
            _need(type(wheel['project']) is str and NAME.match(wheel['project']) and type(wheel['version']) is str
                  and VERSION.match(wheel['version']) and type(wheel['filename']) is str
                  and wheel['filename'].endswith('.whl') and '/' not in wheel['filename']
                  and wheel['url'].endswith('/' + wheel['filename'])
                  and wheel['filename'].startswith(normalize(wheel['project']) + '-' + wheel['version'] + '-'), 'lock_invalid')
            _need(normalize(wheel['project']) not in seen, 'lock_invalid')
            seen.add(normalize(wheel['project']))
    return lock


def normalize(name):
    return re.sub(r'[-_.]+', '_', name).lower()


# Cache and downloads ----------------------------------------------------------------------------

def _private_directory(path, code):
    path = Path(path)
    _need(path.is_absolute() and '..' not in path.parts, code)
    _need(not any(path == p or path.is_relative_to(p) for p in (Path('/tmp'), Path('/private/tmp'))), code)
    try:
        if not path.exists():
            path.mkdir(mode=0o700, parents=False)
        m = path.lstat()
    except OSError:
        raise PayloadError(code) from None
    _need(stat.S_ISDIR(m.st_mode) and m.st_uid == os.getuid() and stat.S_IMODE(m.st_mode) == 0o700, code)
    return path


def fetch(pin, cache, *, offline=False, opener=urllib.request.urlopen):
    """The cached bytes of one pin; downloaded at most once and only kept when the hash matches."""
    downloads = _private_directory(Path(cache) / 'downloads', 'cache_refused')
    target = downloads / pin['sha256']
    if not target.exists():
        _need(not offline, 'input_unavailable')
        partial = downloads / (pin['sha256'] + '.partial')
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            raise PayloadError('cache_refused') from None
        digest, total = hashlib.sha256(), 0
        try:
            with opener(pin['url'], timeout=FETCH_TIMEOUT) as response, \
                    os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
                _need(urllib.parse.urlsplit(response.geturl()).scheme == 'https', 'input_unavailable')
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    _need(total <= pin['bytes'], 'input_mismatch')
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except PayloadError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, http.client.HTTPException):
            partial.unlink(missing_ok=True)
            raise PayloadError('input_unavailable') from None
        if total != pin['bytes'] or digest.hexdigest() != pin['sha256']:
            partial.unlink(missing_ok=True)
            raise PayloadError('input_mismatch')
        try:
            os.chmod(partial, 0o400)
            os.rename(partial, target)
        except OSError:
            raise PayloadError('cache_refused') from None
    data = _read(target, pin['bytes'], 'input_mismatch')
    _need(len(data) == pin['bytes'] and sha256(data) == pin['sha256'], 'input_mismatch')
    return data


def _read(path, limit, code):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise PayloadError(code) from None
    with os.fdopen(fd, 'rb') as stream:
        m = os.fstat(stream.fileno())
        _need(stat.S_ISREG(m.st_mode) and m.st_size <= limit, code)
        data = stream.read(limit + 1)
    _need(len(data) == m.st_size, code)
    return data


# Writing ----------------------------------------------------------------------------------------

class Tree:
    """The payload under construction: exact relative paths, no overwrite, executable bit only."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = {}

    def write(self, relative, data, executable=False, code='payload_refused'):
        _need(_safe_relative(relative), code)
        _need(relative not in self.files and len(self.files) < MAX_MEMBERS, code)
        path = self.root / relative
        try:  # A file and a directory of one name, or any filesystem failure, is this input's refusal.
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
                out.write(data)
        except OSError:
            raise PayloadError(code) from None
        self.files[relative] = executable

    def rename(self, old, new):
        _need(old in self.files and new not in self.files, 'prune_refused')
        try:
            os.rename(self.root / old, self.root / new)
        except OSError:
            raise PayloadError('prune_refused') from None
        self.files[new] = self.files.pop(old)

    def seal(self):
        """Normalize modes (dirs 0700, files 0400/0500) and return the tree digest."""
        rows = []
        for relative in sorted(self.files):
            path = self.root / relative
            os.chmod(path, 0o500 if self.files[relative] else 0o400)
            rows.append(f'{relative}\0{"x" if self.files[relative] else "-"}\0{sha256(path.read_bytes())}\n')
        for base, dirs, files in os.walk(self.root):
            os.chmod(base, 0o700)
            _need(all((Path(base) / f).relative_to(self.root).as_posix() in self.files for f in files), 'payload_refused')
        return sha256(''.join(rows).encode())


def _safe_relative(name):
    path = PurePosixPath(name)
    return (type(name) is str and 0 < len(name.encode()) <= 1024 and not path.is_absolute() and '\\' not in name
            and all(part not in ('', '.', '..') and len(part.encode()) <= 255 for part in name.split('/'))
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


# Inputs -----------------------------------------------------------------------------------------

def _members(tar):
    count = 0
    for member in tar:
        count += 1
        _need(count <= MAX_MEMBERS, 'archive_refused')
        yield member


def python_tree(archive, tree):
    """Extract PBS `python/`, pruned; returns the pruned-entry summary."""
    pruned, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            for member in _members(tar):
                name = member.name.rstrip('/')
                _need(name == 'python' or name.startswith('python/'), 'archive_refused')
                relative = name[len('python/'):]
                if not relative:
                    continue
                _need(_safe_relative(relative), 'archive_refused')
                reason = _pruned(relative)
                if reason:
                    entry = pruned.setdefault(reason, {'files': 0, 'bytes': 0})
                    entry['files'] += not member.isdir()
                    entry['bytes'] += member.size if member.isfile() else 0
                    continue
                if member.isdir():
                    continue
                _need(member.isfile() and 0 <= member.size <= MAX_MEMBER, 'archive_refused')
                total += member.size
                _need(total <= MAX_UNPACKED, 'archive_refused')
                data = tar.extractfile(member).read(member.size + 1)
                _need(len(data) == member.size, 'archive_refused')
                tree.write('python/' + relative, data, bool(member.mode & 0o111), 'archive_refused')
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise PayloadError('archive_refused') from None
    tree.rename('python/' + PYTHON_BIN, 'python/bin/python3')
    top = {n.split('/')[1] for n in tree.files if n.startswith('python/')}
    _need(top == {'bin', 'lib'}, 'prune_refused')
    _need({n for n in tree.files if n.startswith('python/bin/')} == {'python/bin/python3'}, 'prune_refused')
    _need({n.split('/')[2] for n in tree.files if n.startswith('python/lib/')} == PYTHON_LIB, 'prune_refused')
    _need(tree.files['python/bin/python3'], 'prune_refused')
    return dict(sorted(pruned.items()))


def _pruned(relative):
    parts = relative.split('/')
    if '__pycache__' in parts or relative.endswith(('.pyc', '.pyo')):
        return 'bytecode caches'
    if parts[0] in ('include', 'share'):
        return parts[0] + '/'
    if parts[0] == 'bin':
        return None if relative in ('bin', PYTHON_BIN) else 'bin/ (duplicate executables, pip, idle, pydoc, config)'
    if parts[0] == 'lib' and len(parts) > 1 and parts[1] in PRUNED_LIB:
        return 'lib/' + parts[1] + ('/' if len(parts) > 2 else '')
    if parts[:2] == ['lib', 'python3.14'] and len(parts) > 2:
        stdlib = '/'.join(parts[2:])
        for prefix in PRUNED_STDLIB:
            if stdlib.startswith(prefix) or stdlib + '/' == prefix:
                return 'lib/python3.14/' + prefix
    return None


def record_hash(data):
    return 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')


def install_wheel(data, pin, tree, destination):
    """Unpack one wheel into `destination/` after checking every member against its RECORD."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            _need(1 <= len(infos) <= MAX_MEMBERS and len({i.filename for i in infos}) == len(infos), 'wheel_refused')
            _need(sum(i.file_size for i in infos) <= MAX_UNPACKED, 'wheel_refused')
            prefix = normalize(pin['project']) + '-' + pin['version']
            info_dirs = {i.filename.split('/')[0] for i in infos if i.filename.split('/')[0].endswith('.dist-info')}
            _need(len(info_dirs) == 1, 'wheel_refused')
            dist_info = info_dirs.pop()
            _need(normalize(dist_info[:-len('.dist-info')]) == normalize(prefix), 'wheel_refused')
            data_dir = dist_info[:-len('.dist-info')] + '.data'
            record_name = dist_info + '/RECORD'
            rows = {}
            for row in csv.reader(io.StringIO(archive.read(record_name).decode('utf-8'))):
                _need(len(row) == 3 and row[0] not in rows, 'wheel_refused')
                rows[row[0]] = (row[1], row[2])
            dropped, licenses = [], []
            for info in infos:
                name = info.filename
                if name.endswith('/'):
                    continue
                mode = info.external_attr >> 16
                _need(not stat.S_ISLNK(mode) and _safe_relative(name) and info.file_size <= MAX_MEMBER, 'wheel_refused')
                content = archive.read(info)
                _need(len(content) == info.file_size, 'wheel_refused')
                if name != record_name:
                    _need(rows.get(name) == (record_hash(content), str(len(content))), 'wheel_refused')
                relative = name
                if name.startswith(data_dir + '/'):
                    scheme, _, rest = name[len(data_dir) + 1:].partition('/')
                    _need(scheme in ('purelib', 'platlib', 'data', 'scripts', 'headers') and rest, 'wheel_refused')
                    if scheme not in ('purelib', 'platlib'):
                        dropped.append(name)  # Like `pip install --target`: only the import tree ships.
                        continue
                    relative = rest
                tree.write(destination + '/' + relative, content, bool(mode & 0o111), 'wheel_refused')
                inner = name[len(dist_info) + 1:] if name.startswith(dist_info + '/') else None
                if inner and (inner.startswith('licenses/') or '/' not in inner and LICENSE_NAMES.match(inner)):
                    licenses.append((dist_info + '/' + inner, content))
            _need(set(rows) - {record_name} <= {i.filename for i in infos}, 'wheel_refused')
    except (zipfile.BadZipFile, KeyError, UnicodeError, csv.Error, OSError, EOFError, RuntimeError,
            NotImplementedError, zlib.error):  # Encrypted members, unknown compression, corrupt deflate.
        raise PayloadError('wheel_refused') from None
    return dist_info, licenses, sorted(dropped)


def tdlib_source(archive, pin, tree):
    """The pinned TDLib export under source/tdlib (regular files only); returns td_api.tl's sha256."""
    prefix = 'td-' + pin['commit'] + '/'
    count = total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            for member in _members(tar):
                name = member.name
                if member.isdir() and (name.rstrip('/') + '/').startswith(prefix):
                    continue
                _need(member.isfile() and name.startswith(prefix) and _safe_relative(name[len(prefix):])
                      and member.size <= MAX_MEMBER, 'source_refused')
                data = tar.extractfile(member).read(member.size + 1)
                _need(len(data) == member.size, 'source_refused')
                count += 1
                total += member.size
                tree.write('source/tdlib/' + name[len(prefix):], data, False, 'source_refused')
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise PayloadError('source_refused') from None
    _need(count == pin['files'] and total == pin['unpacked_bytes'] and 'source/tdlib/' + SCHEMA in tree.files,
          'source_refused')
    return sha256(_read(tree.root / 'source/tdlib' / SCHEMA, MAX_MEMBER, 'source_refused'))


def git_export(root):
    """HEAD's committed blobs as {path: (bytes, executable)} plus the commit; links and submodules refuse.

    Read with ls-tree and cat-file --batch, the raw objects, so local config (autocrlf, smudge filters)
    and attributes cannot change the bytes the way `git archive` would."""
    def git(*args, data=None):
        return subprocess.run(['/usr/bin/git', '-C', str(root), *args], input=data, capture_output=True, check=True,
                              timeout=TOOL_TIMEOUT, env=TOOL_ENV).stdout
    try:
        commit = git('rev-parse', 'HEAD').decode('ascii').strip()
        _need(re.match(r'^[0-9a-f]{40}$', commit) is not None, 'source_refused')
        listing = git('ls-tree', '-r', '-z', '--full-tree', commit)
        _need(len(listing) <= MAX_SOURCE, 'source_refused')
        entries = []
        for row in filter(None, listing.split(b'\0')):
            meta, _, name = row.partition(b'\t')
            mode, kind, sha = meta.decode('ascii').split(' ')
            path = name.decode('utf-8')
            _need(kind == 'blob' and mode in ('100644', '100755') and re.match(r'^[0-9a-f]{40}$', sha) is not None
                  and _safe_relative(path), 'source_refused')
            entries.append((path, sha, mode == '100755'))
        out = git('cat-file', '--batch', data=''.join(sha + '\n' for _, sha, _ in entries).encode('ascii'))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise PayloadError('source_refused') from None
    _need(len(out) <= MAX_SOURCE, 'source_refused')
    files, offset = {}, 0
    for path, sha, executable in entries:
        end = out.find(b'\n', offset)
        _need(end > 0, 'source_refused')
        header = out[offset:end].split(b' ')
        _need(len(header) == 3 and header[0] == sha.encode() and header[1] == b'blob' and header[2].isdigit(),
              'source_refused')
        size = int(header[2])
        data = out[end + 1:end + 1 + size]
        _need(len(data) == size and out[end + 1 + size:end + 2 + size] == b'\n' and path not in files, 'source_refused')
        files[path] = (data, executable)
        offset = end + 2 + size
    _need(offset == len(out), 'source_refused')
    return commit, files


def manifest_facts(export_src, python=sys.executable):
    """Version, contract, fingerprint and tools of the exported HEAD package (build interpreter)."""
    try:
        result = subprocess.run([python, '-I', '-B', '-c', MANIFEST_PROBE, str(export_src)], capture_output=True,
                                check=True, timeout=TOOL_TIMEOUT)
        facts = _json(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        raise PayloadError('manifest_failed') from None
    _need(type(facts) is dict and set(facts) == {'package_version', 'contract_version', 'schema_fingerprint', 'tools'}
          and type(facts['package_version']) is str and PACKAGE_VERSION.fullmatch(facts['package_version']),
          'manifest_failed')
    return facts


def generate_launcher(template, version):
    """The launcher with its admitted version regenerated from __version__ at both closed sites."""
    _need(type(version) is str and PACKAGE_VERSION.fullmatch(version) is not None, 'manifest_failed')
    try:
        text = template.decode('utf-8')
    except UnicodeError:
        raise PayloadError('manifest_failed') from None
    found = [site.findall(text) for site in LAUNCHER_SITES]
    _need(all(len(f) == 1 for f in found) and found[0] == found[1], 'manifest_failed')
    for site in LAUNCHER_SITES:
        text = site.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + version + m.group(0)[m.end(1) - m.start(0):], text)
    return text.encode()


def _field(value):
    """One METADATA header value: a bounded printable string (no line break can add a header)."""
    return type(value) is str and 0 < len(value) <= 1024 and value.isprintable()


def project_distribution(files, version, tree):
    """app/telegram_search_mcp from HEAD plus a generated dist-info with a full RECORD."""
    package = {name[len('src/'):]: data for name, (data, _) in files.items() if name.startswith('src/telegram_search_mcp/')}
    _need(package and all(n.endswith('.py') for n in package), 'source_refused')
    try:
        project = tomllib.loads(files['pyproject.toml'][0].decode('utf-8'))['project']
        _need(type(project) is dict and all(_field(project.get(k)) for k in ('name', 'description', 'license',
                                                                             'requires-python'))
              and all(type(project.get(k)) is list and all(_field(v) for v in project[k])
                      for k in ('license-files', 'dependencies'))
              and all(name in files for name in project['license-files']), 'source_refused')
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        raise PayloadError('source_refused') from None
    dist = f'telegram_search_mcp-{version}.dist-info'
    metadata = ['Metadata-Version: 2.4', 'Name: ' + project['name'], 'Version: ' + version,
                'Summary: ' + project['description'], 'License-Expression: ' + project['license'],
                *('License-File: ' + name for name in project['license-files']),
                'Requires-Python: ' + project['requires-python'], *('Requires-Dist: ' + d for d in project['dependencies'])]
    generated = {dist + '/METADATA': ('\n'.join(metadata) + '\n').encode(),
                 dist + '/top_level.txt': b'telegram_search_mcp\n',
                 **{dist + '/licenses/' + name: files[name][0] for name in project['license-files']}}
    content = {**package, **generated}
    record = io.StringIO()
    writer = csv.writer(record, lineterminator='\n')
    for name in sorted(content):
        writer.writerow([name, record_hash(content[name]), str(len(content[name]))])
    writer.writerow([dist + '/RECORD', '', ''])
    content[dist + '/RECORD'] = record.getvalue().encode()
    for name in sorted(content):
        tree.write('app/' + name, content[name], False, 'source_refused')
    return dist


def native_tree(native_directory, lock_data, notices, tree):
    """Copy prepare_native's dylibs only after they equal HEAD's native lock, its OpenSSL notices only
    after they equal the payload lock's hashes, and its build provenance only after it agrees with
    that native lock."""
    try:
        lock = _json(lock_data)
        artifacts = {a['name']: (a['bytes'], a['sha256']) for a in lock['artifacts']}
        openssl_version = lock['build']['openssl_version']
    except (ValueError, UnicodeError, KeyError, TypeError, RecursionError):
        raise PayloadError('native_mismatch') from None
    _need(tuple(artifacts) == NATIVE_NAMES and type(openssl_version) is str
          and openssl_version == notices['openssl_version'], 'native_mismatch')
    native_directory = Path(native_directory)
    try:
        names = sorted(p.name for p in (native_directory / 'native').iterdir())
    except OSError:
        raise PayloadError('native_mismatch') from None
    _need(names == sorted(NATIVE_NAMES), 'native_mismatch')
    for name in NATIVE_NAMES:
        data = _read(native_directory / 'native' / name, 32 * 1024 * 1024, 'native_mismatch')
        _need((len(data), sha256(data)) == artifacts[name], 'native_mismatch')
        tree.write('native/' + name, data, True, 'native_mismatch')
    for relative, check in zip(PROVENANCE, (_tdlib_receipt, _openssl_receipt)):
        data = _read(native_directory / relative, 1 << 20, 'native_mismatch')
        check(_provenance_record(data), lock)
        tree.write(relative, data, False, 'native_mismatch')
    openssl = 'openssl-' + openssl_version
    for name in OPENSSL_NOTICES:
        data = _read(native_directory / 'notices' / openssl / name, MAX_NOTICE, 'native_mismatch')
        _need({'bytes': len(data), 'sha256': sha256(data)} == notices['files'][name], 'native_mismatch')
        tree.write(f'notices/{openssl}/{name}', data, False, 'native_mismatch')
    return lock


def _provenance_record(data):
    """prepare_native's `{input_sha256, record}` provenance, byte for byte as it writes it, with the
    receipt's private path redacted; returns the receipt record."""
    try:
        value = _json(data)
        canonical = (json.dumps(value, sort_keys=True) + '\n').encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PayloadError('native_mismatch') from None
    record = value.get('record') if type(value) is dict else None
    _need(type(value) is dict and data == canonical and set(value) == {'input_sha256', 'record'}
          and type(value['input_sha256']) is str and HASH.match(value['input_sha256']) is not None
          and type(record) is dict and type(record.get('source')) is dict and type(record.get('built_on')) is dict
          and record.get('arch') == 'arm64' and record['source'].get('path', REDACTED_PATH) == REDACTED_PATH,
          'native_mismatch')
    return record


def _tdlib_receipt(record, lock):
    """The TDLib receipt that the native lock's source pin and build fields were taken from."""
    try:
        build, built_on, commit = lock['build'], record['built_on'], lock['source_commit']
        versions = record['source'].get('versions')
        openssl = [d.get('version') for d in record.get('runtime_dependencies') or ()
                   if type(d) is dict and d.get('full_name') == 'openssl@3']
        _need(type(commit) is str and record['source'].get('scm_revision') == commit and type(versions) is dict
              and versions.get('head') == 'HEAD-' + commit[:7] and record.get('compiler') == build['compiler']
              and built_on.get('os_version') == build['build_os'] and built_on.get('xcode') == build['xcode']
              and built_on.get('clt') == build['clt'] and record.get('used_options') == build['used_options']
              and openssl == [build['openssl_version']], 'native_mismatch')
    except (KeyError, TypeError, AttributeError):
        raise PayloadError('native_mismatch') from None


def _openssl_receipt(record, lock):
    """The OpenSSL receipt of the version that the native lock names."""
    try:
        versions = record['source'].get('versions')
        _need(type(versions) is dict and versions.get('stable') == lock['build']['openssl_version'], 'native_mismatch')
    except (KeyError, TypeError, AttributeError):
        raise PayloadError('native_mismatch') from None


def macho_inventory(tree):
    """Every Mach-O in the payload, counted by magic rather than file name."""
    files, counts = [], {}
    for relative in sorted(tree.files):
        data = (tree.root / relative).read_bytes()
        kind = MACHO.get(data[:4])
        if kind is None or kind.startswith('universal') and not (len(data) >= 8 and 0 < int.from_bytes(data[4:8], 'big') <= 8):
            continue
        counts[kind] = counts.get(kind, 0) + 1
        files.append({'path': relative, 'kind': kind, 'bytes': len(data), 'sha256': sha256(data),
                      'executable': tree.files[relative]})
    return {'schema_version': 1, 'counted_by': 'magic', 'total': len(files), 'counts': dict(sorted(counts.items())),
            'files': files}


# Build ------------------------------------------------------------------------------------------

def prepare_payload(output, *, native_directory, cache, checkout=ROOT, offline=False,
                    opener=urllib.request.urlopen, python=sys.executable):
    """Build one fresh payload directory; returns a nonsecret summary with the tree digest."""
    try:
        return _prepare_payload(Path(output), native_directory, cache, checkout, offline, opener, python)
    except PayloadError:
        raise
    except Exception:  # The last boundary: never a traceback, an escaping path or provider text.
        raise PayloadError('payload_refused') from None


def _prepare_payload(output, native_directory, cache, checkout, offline, opener, python):
    _need(output.is_absolute() and '..' not in output.parts and not output.exists() and not output.is_symlink()
          and output.parent.is_dir() and not output.is_relative_to(Path(checkout).resolve())
          and not any((p / '.git').exists() for p in output.parents), 'output_refused')
    _private_directory(cache, 'cache_refused')
    commit, files = git_export(checkout)
    required = [LOCK, 'packaging/launcher.py', 'packaging/tdlib-lock.json', 'pyproject.toml', 'LICENSE', 'NOTICE',
                *(f'skills/{s}/SKILL.md' for s in SKILLS), 'src/telegram_search_mcp/__init__.py']
    _need(all(name in files for name in required), 'source_refused')
    lock_data = files[LOCK][0]  # HEAD's committed lock; an uncommitted working-tree edit never counts.
    _need(len(lock_data) <= MAX_LOCK, 'lock_invalid')
    lock = load_lock(lock_data)
    inputs = {'python': fetch(lock['python'], cache, offline=offline, opener=opener),
              'tdlib_source': fetch(lock['tdlib_source'], cache, offline=offline, opener=opener),
              **{w['sha256']: fetch(w, cache, offline=offline, opener=opener) for w in lock['app'] + lock['installer']}}
    try:
        output.mkdir(mode=0o700)
    except OSError:
        raise PayloadError('output_refused') from None
    tree = Tree(output)
    for name, (data, executable) in sorted(files.items()):
        tree.write('source/project/' + name, data, False, 'source_refused')
    facts = manifest_facts(output / 'source/project/src', python)
    version = facts['package_version']
    pruned = python_tree(inputs['python'], tree)
    components = []
    for group in ('app', 'installer'):
        for pin in lock[group]:
            dist_info, licenses, dropped = install_wheel(inputs[pin['sha256']], pin, tree, group)
            for name, content in licenses:
                tree.write(f'notices/{group}/{name}', content, False, 'wheel_refused')
            components.append({'project': pin['project'], 'version': pin['version'], 'filename': pin['filename'],
                               'sha256': pin['sha256'], 'destination': group, 'dist_info': dist_info,
                               'notices': sorted(f'notices/{group}/{n}' for n, _ in licenses),
                               'dropped_wheel_data': dropped})
    project_dist = project_distribution(files, version, tree)
    _need(('app/segno/__init__.py' in tree.files and 'installer/tomlkit/__init__.py' in tree.files), 'wheel_refused')
    native_lock = files['packaging/tdlib-lock.json'][0]
    lock_value = native_tree(native_directory, native_lock, lock['native_notices'], tree)
    schema = tdlib_source(inputs['tdlib_source'], lock['tdlib_source'], tree)
    _need(lock['tdlib_source']['commit'] == lock_value.get('source_commit') and schema == lock_value.get('schema_sha256'),
          'source_refused')
    tree.write('tdlib-lock.json', native_lock)
    tree.write('launcher.py', generate_launcher(files['packaging/launcher.py'][0], version))
    for skill in SKILLS:
        tree.write(f'skills/{skill}/SKILL.md', files[f'skills/{skill}/SKILL.md'][0])
    tree.write('notices/LICENSE', files['LICENSE'][0])
    tree.write('notices/NOTICE', files['NOTICE'][0])
    tdlib_license = 'source/tdlib/LICENSE_1_0.txt'
    _need(tdlib_license in tree.files, 'source_refused')
    tree.write('notices/tdlib/LICENSE_1_0.txt', _read(output / tdlib_license, MAX_NOTICE, 'source_refused'))
    python_license = 'python/lib/python3.14/LICENSE.txt'
    _need(python_license in tree.files, 'prune_refused')
    tree.write('notices/python/LICENSE.txt', _read(output / python_license, MAX_NOTICE, 'prune_refused'))
    tree.write('provenance/python-runtime.json', json_bytes({
        'upstream': 'astral-sh/python-build-standalone', 'target': 'aarch64-apple-darwin',
        'flavor': 'install_only_stripped', 'python_version': lock['python']['version'], 'tag': lock['python']['pbs_tag'],
        'url': lock['python']['url'], 'bytes': lock['python']['bytes'], 'sha256': lock['python']['sha256'],
        'kept_executable': 'python/bin/python3 (upstream bin/python3.14)', 'pruned': pruned}))
    tree.write('provenance/components.json', json_bytes({
        'schema_version': 1, 'source_commit': commit, 'project_dist_info': project_dist,
        'payload_lock_sha256': sha256(lock_data),
        'tdlib_source': lock['tdlib_source'], 'native_lock_sha256': sha256(native_lock), 'wheels': components}))
    tree.write('provenance/source-completeness.json', json_bytes({
        'status': 'incomplete', 'publication': 'unreleased',
        'available': ['source/project (git archive HEAD)', 'source/tdlib (pinned TDLib export)',
                      'app and installer Python sources', 'notices/'],
        'unestablished': ['CPython/PBS corresponding source and build', 'OpenSSL corresponding source',
                          'native wheel build sources', 'reproducible native build', 'publisher identity']}))
    tree.write('provenance/macho-inventory.json', json_bytes(macho_inventory(tree)))
    skills = [{'id': s, 'sha256': sha256(files[f'skills/{s}/SKILL.md'][0])} for s in SKILLS]
    manifest = {'schema_version': 2, 'package_version': version, 'target': 'macos-arm64', 'minimum_os': '26.0',
                'contract_version': facts['contract_version'], 'schema_fingerprint': facts['schema_fingerprint'],
                'tools': facts['tools'], 'native_lock_sha256': sha256(native_lock), 'skills': skills,
                'source_completeness': 'incomplete', 'publication': 'unreleased',
                'publisher': {'kind': 'adhoc', 'team_id': None}}
    tree.write('manifest/release.json', json_bytes(manifest))
    digest = tree.seal()
    inventory = _json((output / 'provenance/macho-inventory.json').read_bytes())
    return {'tree_sha256': digest, 'files': len(tree.files),
            'bytes': sum((output / n).stat().st_size for n in tree.files), 'package_version': version,
            'source_commit': commit, 'macho': inventory['counts'], 'macho_total': inventory['total'],
            'manifest_sha256': sha256((output / 'manifest/release.json').read_bytes())}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Build the prepared payload from pinned inputs.')
    parser.add_argument('--output', required=True, help='fresh payload directory (outside the checkout)')
    parser.add_argument('--native-directory', required=True, help='prepare_native.py output directory')
    parser.add_argument('--cache', required=True, help='private download cache (mode 0700, never under /tmp)')
    parser.add_argument('--offline', action='store_true', help='use only cached inputs')
    args = parser.parse_args()
    try:
        result = prepare_payload(Path(args.output), native_directory=Path(args.native_directory), cache=Path(args.cache),
                                 offline=args.offline)
    except PayloadError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True))
