"""Produce a local candidate bundle from prevalidated prepared inputs.

This development-side helper never executes supplied Python/app/native content.
The caller must separately validate the frozen source/schema bindings and must
explicitly authenticate the external index before installing. Deterministic
packaging does not establish native source-build reproducibility or publication
rights. Failed private output directories are retained for diagnosis/quarantine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
from types import SimpleNamespace

from telegram_search_mcp.app_identity import IDENTITY_FILE, parse_app_identity
from telegram_search_mcp.bundle_runtime import unique_json, digest, BundleError, check_manifest, check_publisher_lock
from telegram_search_mcp.keychain import read_api_credentials
from telegram_search_mcp.native_runtime import parse_native_manifest, NativeRuntimeError
from telegram_search_mcp.release_archive import (
    MAX_ARCHIVE_BYTES, MAX_FILES, MAX_INDEX_BYTES, MAX_MEMBER_BYTES,
    MAX_UNPACKED_BYTES, ReleaseArchiveError, load_archive_index,
)

LARGE_OPERATION_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
NATIVE_NAMES = {'libtdjson.dylib', 'libssl.3.dylib', 'libcrypto.3.dylib'}
SKILL_IDS = {'telegram-search', 'telegram-setup'}
TDLIB_SOURCE = 'source/tdlib/'
TDLIB_SCHEMA = TDLIB_SOURCE + 'td/generate/scheme/td_api.tl'


class ReleasePackageError(ValueError):
    """Bounded, redacted input, identity, or producer integrity failure."""


@dataclass(frozen=True)
class AssemblyResult:
    archive: Path
    index: Path
    provenance: Path
    audit: Path
    archive_sha256: str
    index_sha256: str
    provenance_sha256: str
    files: int
    unpacked_bytes: int
    archive_bytes: int


@dataclass(frozen=True)
class _File:
    name: str
    source: Path
    supplied: Path
    root: Path | None
    metadata: os.stat_result
    sha256: str
    executable: bool
    # Generated in memory (never read from the payload); source is a label only. Never in repr.
    data: bytes | None = field(default=None, repr=False)

    def record(self):
        return {'path': self.name, 'bytes': self.metadata.st_size,
                'sha256': self.sha256, 'executable': self.executable}


def _safe_path(path: Path, directory=False):
    """No parent traversal, symlink ancestors, or unsafe ownership/modes."""
    # Preserve the supplied spelling for ancestor checks. Resolving first could
    # erase a forbidden symlink ancestor or hide input/output overlap.
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError
    for parent in reversed(path.parents):
        m = parent.lstat()
        if (not stat.S_ISDIR(m.st_mode) or m.st_uid not in (0, os.getuid()) or
                m.st_mode & 0o022 and not (m.st_uid == 0 and m.st_mode & stat.S_ISVTX)):
            raise ValueError
    m = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(m.st_mode) or m.st_uid != os.getuid() or m.st_mode & 0o022:
        raise ValueError
    return m


def _snapshot(m):
    return (m.st_dev, m.st_ino, m.st_size, m.st_mtime_ns, m.st_ctime_ns, m.st_mode, m.st_uid)


def _open_source(path, maximum=MAX_MEMBER_BYTES):
    _safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        m = os.fstat(fd)
        if (not stat.S_ISREG(m.st_mode) or m.st_uid != os.getuid() or m.st_mode & 0o022 or
                not 0 <= m.st_size <= maximum):
            raise ValueError
        return os.fdopen(fd, 'rb'), m
    except BaseException:
        os.close(fd)
        raise


def _digest_input(path, maximum=None):
    stream, before = _open_source(path, MAX_MEMBER_BYTES if maximum is None else maximum)
    with stream:
        h = hashlib.sha256()
        left = before.st_size
        while left:
            chunk = stream.read(min(left, CHUNK_BYTES))
            if not chunk:
                raise ValueError
            h.update(chunk)
            left -= len(chunk)
        if stream.read(1) or _snapshot(before) != _snapshot(os.fstat(stream.fileno())):
            raise ValueError
        if _snapshot(before) != _snapshot(path.lstat()):
            raise ValueError
    return before, h.hexdigest()


def _bounded_input(path, limit=65536):
    m = _safe_path(path)
    if m.st_size > limit:
        raise ValueError
    stream, before = _open_source(path)
    with stream:
        content = stream.read(limit + 1)
        if (len(content) != before.st_size or len(content) > limit or
                _snapshot(before) != _snapshot(os.fstat(stream.fileno())) or
                _snapshot(before) != _snapshot(path.lstat())):
            raise ValueError
    return content


def _resolve_python(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError
    return resolved


def _scan(root: Path, prefix: str, python_links=False):
    _safe_path(root, directory=True)
    count = total = 0
    def visit(supplied, name, ancestors):
        nonlocal count, total
        m = supplied.lstat()
        if m.st_uid != os.getuid():
            raise ValueError
        if stat.S_ISLNK(m.st_mode) and not python_links:
            raise ValueError
        actual = _resolve_python(supplied, root) if python_links else supplied
        m = _safe_path(actual, directory=actual.is_dir())
        if stat.S_ISDIR(m.st_mode):
            if actual in ancestors:
                raise ValueError
            for child in sorted(supplied.iterdir(), key=lambda p: p.name):
                if child.is_symlink() and not python_links:
                    raise ValueError
                if child.name == '__pycache__' or child.name.endswith(('.pyc', '.pyo')):
                    continue
                if child.name == 'pyvenv.cfg':
                    raise ValueError
                yield from visit(child, name + '/' + child.name, ancestors | {actual})
        elif stat.S_ISREG(m.st_mode):
            count += 1
            total += m.st_size
            if count > MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise ValueError
            before, digest = _digest_input(actual)
            yield _File(name, actual, supplied, root if python_links else None,
                        before, digest, bool(before.st_mode & 0o111))
        else:
            raise ValueError
    return list(visit(root, prefix, frozenset()))


def _single(source, name):
    m, digest = _digest_input(source)
    return _File(name, source, source, None, m, digest, bool(m.st_mode & 0o111))


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def _app_identity_file(runner):
    """The owner's Keychain pair as an in-memory, indexed `app-identity.json` (mode 0400).

    Only behind the explicit assembly flag. Neither value is printed, logged or written
    anywhere but the archive; failures carry a fixed code and no chained exception.
    """
    failed = False
    try:
        credentials = read_api_credentials(runner=runner)
        data = _json_bytes({'version': 1, 'api_id': credentials.api_id, 'api_hash': credentials.api_hash})
        parse_app_identity(data)
    except Exception:  # Keychain, parser and encoder errors alike: never forward their text.
        failed = True
    if failed:
        raise ReleasePackageError('app_identity_unavailable')
    return _File(IDENTITY_FILE, Path('<keychain>'), Path('<keychain>'), None, SimpleNamespace(st_size=len(data)),
                 digest(data), False, data)


def _header(file):
    # The index parser supplies the consumer's path/count/size bounds as well.
    info = tarfile.TarInfo(file.name)
    info.size = file.metadata.st_size
    info.mode = 0o500 if file.executable else 0o400
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ''
    info.type = tarfile.REGTYPE
    info.tobuf(format=tarfile.USTAR_FORMAT, encoding='utf-8', errors='strict')
    return info


def _index(files, manifest, archive_bytes, archive_sha256):
    return {'schema_version': 1, 'target': 'macos-arm64', 'release_version': manifest['package_version'],
            'archive_bytes': archive_bytes, 'archive_sha256': archive_sha256,
            'unpacked_bytes': sum(f.metadata.st_size for f in files), 'files': [f.record() for f in files]}


def _write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
        os.fchmod(out.fileno(), 0o400)


class _VerifiedReader:
    def __init__(self, file, stream):
        self.file, self.stream = file, stream
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size):
        # tarfile requests bounded copy buffers; never read the whole member.
        if size < 0 or size > CHUNK_BYTES:
            raise ValueError
        content = self.stream.read(size)
        self.digest.update(content)
        self.count += len(content)
        return content

    def finish(self):
        f = self.file
        if (self.count != f.metadata.st_size or self.stream.read(1) or
                self.digest.hexdigest() != f.sha256 or
                _snapshot(os.fstat(self.stream.fileno())) != _snapshot(f.metadata) or
                _snapshot(f.source.lstat()) != _snapshot(f.metadata)):
            raise ValueError
        _safe_path(f.source)
        if f.root is not None and _resolve_python(f.supplied, f.root) != f.source:
            raise ValueError


def assemble_bundle(payload, output_directory, *, app_identity_from_keychain=False,
                    keychain_runner=subprocess.run):
    """Stream one prepared current bundle; only audited internal Python links.

    Preparation is a separate bounded builder operation. This producer verifies
    actual current product/contract/skills/native bytes, never executes payload
    binaries and emits its metadata audit before creating the large archive.
    `app_identity_from_keychain` is the only way an `app-identity.json` can enter a
    bundle: a payload that already carries one is refused either way.
    """
    identity = _app_identity_file(keychain_runner) if app_identity_from_keychain else None
    try:
        payload, output = Path(payload), Path(output_directory)
        _safe_path(payload,directory=True); _safe_path(output.parent,directory=True)
        if output.exists() or output.is_symlink() or output.is_relative_to(payload) or payload.is_relative_to(output): raise ValueError
        if any((p/'.git').exists() for p in output.parents): raise ValueError
        files=[]
        for child in sorted(payload.iterdir()):
            if child.is_dir(): files += _scan(child,child.name,python_links=child.name=='python')
            else: files.append(_single(child,child.name))
        if any(f.name==IDENTITY_FILE for f in files): raise ValueError
        if identity is not None: files.append(identity)
        files.sort(key=lambda f:f.name)
        manifest_data=_bounded_input(payload/'manifest/release.json')
        manifest=unique_json(manifest_data)
        from telegram_search_mcp import __version__
        from telegram_search_mcp.contract import CONTRACT_VERSION, schema_document, schema_fingerprint
        from telegram_search_mcp.server import build_server
        from telegram_search_mcp.config import RuntimePolicy
        actual_tools=[t['name'] for t in schema_document(build_server(policy=RuntimePolicy()))['tools']]
        check_manifest(manifest,__version__)  # v1, or v2 with publication/completeness/publisher
        if (manifest['contract_version']!=CONTRACT_VERSION
                or manifest['schema_fingerprint']!=schema_fingerprint() or manifest['tools']!=actual_tools):
            raise ValueError
        # The bundled package is exactly this checkout's: same .py set, same bytes; and the QR encoder ships.
        package=Path(__import__('telegram_search_mcp').__file__).parent;prefix='app/telegram_search_mcp/'
        expected={str(p.relative_to(package)):digest(p.read_bytes()) for p in sorted(package.rglob('*.py'))
                  if '__pycache__' not in p.relative_to(package).parts}
        bundled={f.name[len(prefix):]:f.sha256 for f in files if f.name.startswith(prefix)}
        if bundled!=expected or not any(f.name=='app/segno/__init__.py' for f in files):
            raise ReleasePackageError('payload_app_mismatch')
        lock_data=_bounded_input(payload/'tdlib-lock.json'); lock=parse_native_manifest(lock_data)
        if digest(lock_data)!=manifest['native_lock_sha256']:raise ValueError
        check_publisher_lock(manifest,lock_data)  # v1 pairs only with an ad-hoc lock
        inventory={f.name:f for f in files}
        # Shipped TDLib source must be the pinned schema; a complete-source claim requires it.
        schema=inventory.get(TDLIB_SCHEMA)
        if schema is None and (manifest['source_completeness']=='complete' or any(n.startswith(TDLIB_SOURCE) for n in inventory)):
            raise ReleasePackageError('native_schema_mismatch')
        if schema is not None and schema.sha256!=lock.schema_sha256:raise ReleasePackageError('native_schema_mismatch')
        required={'python/bin/python3','launcher.py','manifest/release.json','tdlib-lock.json',
            'installer/tomlkit/__init__.py','notices/LICENSE','notices/NOTICE','provenance/source-completeness.json'}
        if not required<=set(inventory) or not inventory['python/bin/python3'].executable: raise ValueError
        for artifact in lock.artifacts:
            file=inventory['native/'+artifact.name]
            if file.sha256!=artifact.sha256 or file.metadata.st_size!=artifact.bytes:raise ValueError
        if {p.name for p in (payload/'native').iterdir()}!=NATIVE_NAMES:raise ValueError
        if {s['id'] for s in manifest['skills']}!=SKILL_IDS:raise ValueError
        for skill in manifest['skills']:
            if inventory['skills/'+skill['id']+'/SKILL.md'].sha256!=skill['sha256']:raise ValueError
        preindex=_json_bytes(_index(files,manifest,1,'0'*64));load_archive_index(preindex)
        for file in files:_header(file)
        unpacked=sum(f.metadata.st_size for f in files)
        tar_bound=sum(512+((f.metadata.st_size+511)//512)*512 for f in files)+10240
        archive_bound=tar_bound+tar_bound//1000+65536
        if archive_bound>MAX_ARCHIVE_BYTES:raise ValueError
        audit=_json_bytes({'schema_version':1,'unpacked_bytes':unpacked,'archive_upper_bound_bytes':archive_bound,
            'large_operation':unpacked+archive_bound>LARGE_OPERATION_BYTES,
            'files':[{'source':str(f.source),'destination':str(output/f.name),**f.record()} for f in files]})
        if len(audit)>LARGE_OPERATION_BYTES:raise ValueError
        output.mkdir(mode=0o700);os.chmod(output,0o700)
        _write_private(output/'preflight-audit.json',audit)
        archive=output/'release.tar.gz'
        with os.fdopen(os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as raw:
            with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=6) as compressed:
                with tarfile.open(fileobj=compressed,mode='w|',format=tarfile.USTAR_FORMAT,encoding='utf-8',errors='strict') as tar:
                    for file in files:
                        if file.data is not None:tar.addfile(_header(file),io.BytesIO(file.data));continue
                        if file.root is not None and _resolve_python(file.supplied,file.root)!=file.source:raise ValueError
                        stream,before=_open_source(file.source)
                        with stream:
                            if _snapshot(before)!=_snapshot(file.metadata):raise ValueError
                            reader=_VerifiedReader(file,stream);tar.addfile(_header(file),reader);reader.finish()
            raw.flush();os.fsync(raw.fileno());os.fchmod(raw.fileno(),0o400)
        am,ah=_digest_input(archive,MAX_ARCHIVE_BYTES)
        for file in files:
            if file.data is None and _snapshot(_safe_path(file.source))!=_snapshot(file.metadata):raise ValueError
        final=_json_bytes(_index(files,manifest,am.st_size,ah));load_archive_index(final)
        index=output/'release-index.json';_write_private(index,final)
        publisher=manifest.get('publisher') or {'kind':'adhoc','team_id':None}
        provenance=_json_bytes({'schema_version':1,'package_version':__version__,'publication':manifest['publication'],
            'publisher_identity':publisher['kind']=='developer-id','team_id':publisher['team_id'],
            'source_completeness':manifest['source_completeness'],'archive_sha256':ah,
            'index_sha256':digest(final),'manifest_sha256':digest(manifest_data),'native_lock_sha256':digest(lock_data),
            'files':len(files),'unpacked_bytes':unpacked})
        _write_private(output/'provenance.json',provenance)
        return AssemblyResult(archive,index,output/'provenance.json',output/'preflight-audit.json',ah,
            digest(final),digest(provenance),len(files),unpacked,am.st_size)
    except ReleasePackageError:raise  # Already a fixed, redacted code.
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RuntimeError,tarfile.TarError,BundleError,NativeRuntimeError,ReleaseArchiveError):
        raise ReleasePackageError('bundle_producer_integrity_refused') from None


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--payload',required=True);parser.add_argument('--output-directory',required=True)
    parser.add_argument('--app-identity-from-keychain',action='store_true',
        help='bundle this Mac\'s Keychain api_id/api_hash as app-identity.json (never printed)')
    args=parser.parse_args()
    result=assemble_bundle(args.payload,args.output_directory,app_identity_from_keychain=args.app_identity_from_keychain)
    print(_json_bytes({'archive_sha256':result.archive_sha256,'index_sha256':result.index_sha256,
        'files':result.files,'unpacked_bytes':result.unpacked_bytes,'archive_bytes':result.archive_bytes}).decode(),end='')
