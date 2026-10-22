"""Validate the exact portable native pin before any TDLib client can exist."""
from __future__ import annotations

import ctypes
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from .bundle_paths import RuntimePathError, validate_owned_path, open_owned_file, open_owned_directory, file_metadata_identity

COMMIT = 'd1085f9cebc5a62379991ae1652673954f229c1f'
SCHEMA_SHA256 = '326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87'
REQUIRED_SYMBOLS = ('td_json_client_create','td_json_client_send','td_json_client_receive','td_json_client_execute','td_json_client_destroy')
NAMES = ('libtdjson.dylib','libssl.3.dylib','libcrypto.3.dylib')
SYSTEM_LIBRARIES = frozenset(('/usr/lib/libz.1.dylib','/usr/lib/libc++.1.dylib','/usr/lib/libSystem.B.dylib'))
MAX_MANIFEST_BYTES = 65536
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$', max_length=64)]
TeamId = Annotated[str, Field(pattern=r'^[A-Z0-9]{10}$', max_length=10)]
Text = Annotated[str, Field(max_length=256)]


@dataclass(frozen=True)
class ApprovedPin:
    """One reviewed native pin. A pin change is one reviewed row here, never a free lock value.

    `unsigned` holds the ad-hoc relocated artifacts exactly as the v1 lock pins them; a
    Developer ID re-sign must name them as its inputs. `team_ids` lists the Developer ID teams
    approved to re-sign this row (empty: no re-signed lock is admitted).
    """
    source_commit: str
    tdlib_version: str
    schema_sha256: str
    minimum_os: str
    abi: str
    tdlib_license_source: str
    openssl_version: str
    build_method: str
    unsigned: tuple[tuple[str, int, str], ...]
    team_ids: frozenset[str] = frozenset()

    def key(self) -> tuple:
        return (self.source_commit, self.tdlib_version, self.schema_sha256, self.minimum_os, self.abi,
                self.tdlib_license_source, self.openssl_version, self.build_method)


APPROVED_PINS = (
    ApprovedPin(COMMIT, '1.8.67', SCHEMA_SHA256, '26.0', 'legacy-td_json_client',
                f'https://github.com/tdlib/td/blob/{COMMIT}/LICENSE_1_0.txt', '3.6.4', 'homebrew-receipt-relocated',
                (('libtdjson.dylib', 17638336, '65ef0643b294659eeb3a151f4b496db1808d2535f646a3494a08fde4340ea12c'),
                 ('libssl.3.dylib', 901744, '7e89651576fc4dfe9461f65de448fc99990dc5c32c06c413d86bdbbfc11e9774'),
                 ('libcrypto.3.dylib', 4862416, 'f9563883fdbb4f2b367c708a6abd786119c935d3f9cd01fa8e11c34666775d1b')),
                # The publisher's Developer ID team (owner-provisioned 21.10.2026; 0.53 spec, decisions).
                team_ids=frozenset(('256X5MUA6S',))),
)


class NativeRuntimeError(ValueError):
    """Native integrity/ABI failure with no raw loader, path or provider output."""


class UnsupportedNativePlatform(NativeRuntimeError):
    """No approved portable artifact for this operating-system target."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)

    @model_validator(mode='before')
    @classmethod
    def integer_version(cls, value):
        if isinstance(value, dict) and 'manifest_version' in value and type(value['manifest_version']) is not int:
            raise ValueError('version must be an integer')
        return value



class NativeArtifact(_Closed):
    name: Literal['libtdjson.dylib','libssl.3.dylib','libcrypto.3.dylib']
    bytes: Annotated[int, Field(ge=32,le=32*1024*1024)]
    sha256: Hash
    dependencies: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=16)]


class BuildProvenance(_Closed):
    method: Literal['homebrew-receipt-relocated']
    reproducibility: Literal['unverified']
    compiler: Literal['clang']
    build_os: Annotated[str,Field(max_length=64)]
    xcode: Annotated[str,Field(max_length=64)]
    clt: Annotated[str,Field(max_length=64)]
    used_options: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=32)]
    known_formula_flags: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=32)]
    implicit_flags: Literal['homebrew-std_cmake_args-not-captured']
    openssl_version: Literal['3.6.4']


class NativeNotices(_Closed):
    status: Literal['pending-public-distribution-review']
    tdlib_license: Literal['BSL-1.0']
    tdlib_source: Literal['https://github.com/tdlib/td']
    tdlib_license_source: Literal['https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/LICENSE_1_0.txt']
    openssl_license: Literal['Apache-2.0']
    source_rebuild: Literal['pending']
    publisher_signature: Literal['unavailable']


class NativeManifest(_Closed):
    manifest_version: Literal[1]
    target: Literal['macos-arm64']
    platform: Literal['Darwin']
    architecture: Literal['arm64']
    minimum_os: Literal['26.0']
    source_commit: Literal['d1085f9cebc5a62379991ae1652673954f229c1f']
    tdlib_version: Literal['1.8.67']
    schema_sha256: Literal['326b65b41442901ad6bf0ca2f7c356ae54365d6c343956a62e06a8b3cb305e87']
    abi: Literal['legacy-td_json_client']
    required_symbols: Annotated[list[Annotated[str,Field(max_length=64)]],Field(min_length=5,max_length=5)]
    signature: Literal['adhoc-local-integrity-only']
    artifacts: Annotated[list[NativeArtifact],Field(min_length=3,max_length=3)]
    build: BuildProvenance
    notices: NativeNotices

    @model_validator(mode='after')
    def exact_boundary(self) -> 'NativeManifest':
        _exact_boundary(self)
        return self


def _exact_boundary(manifest) -> None:
    if tuple(manifest.required_symbols) != REQUIRED_SYMBOLS or tuple(a.name for a in manifest.artifacts) != NAMES:
        raise ValueError('native pin set is invalid')
    allowed = SYSTEM_LIBRARIES | {f'@loader_path/{name}' for name in NAMES}
    for artifact in manifest.artifacts:
        if len(set(artifact.dependencies)) != len(artifact.dependencies) or any(dep not in allowed for dep in artifact.dependencies):
            raise ValueError('native dependency boundary is invalid')


class NativeArtifactV2(_Closed):
    name: Literal['libtdjson.dylib','libssl.3.dylib','libcrypto.3.dylib']
    bytes: Annotated[int, Field(ge=32,le=32*1024*1024)]
    sha256: Hash
    unsigned_bytes: Annotated[int, Field(ge=32,le=32*1024*1024)]
    unsigned_sha256: Hash
    dependencies: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=16)]


class BuildProvenanceV2(_Closed):
    method: Annotated[str,Field(max_length=64)]
    reproducibility: Literal['unverified']
    compiler: Literal['clang']
    build_os: Annotated[str,Field(max_length=64)]
    xcode: Annotated[str,Field(max_length=64)]
    clt: Annotated[str,Field(max_length=64)]
    used_options: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=32)]
    known_formula_flags: Annotated[list[Annotated[str,Field(max_length=128)]],Field(max_length=32)]
    implicit_flags: Literal['homebrew-std_cmake_args-not-captured']
    openssl_version: Annotated[str,Field(max_length=32)]


class NativeNoticesV2(_Closed):
    status: Literal['pending-public-distribution-review']
    tdlib_license: Literal['BSL-1.0']
    tdlib_source: Literal['https://github.com/tdlib/td']
    tdlib_license_source: Text
    openssl_license: Literal['Apache-2.0']
    source_rebuild: Literal['pending']
    publisher_signature: Literal['unavailable','developer-id']


class NativeManifestV2(_Closed):
    """Data-driven lock: every pin value must equal exactly one APPROVED_PINS row."""
    manifest_version: Literal[2]
    target: Literal['macos-arm64']
    platform: Literal['Darwin']
    architecture: Literal['arm64']
    minimum_os: Annotated[str,Field(pattern=r'^[0-9]{2}\.[0-9]{1,2}$',max_length=5)]
    source_commit: Annotated[str,Field(pattern=r'^[0-9a-f]{40}$',max_length=40)]
    tdlib_version: Annotated[str,Field(pattern=r'^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$',max_length=12)]
    schema_sha256: Hash
    abi: Literal['legacy-td_json_client']
    required_symbols: Annotated[list[Annotated[str,Field(max_length=64)]],Field(min_length=5,max_length=5)]
    signature: Literal['adhoc','developer-id']
    team_id: TeamId | None
    artifacts: Annotated[list[NativeArtifactV2],Field(min_length=3,max_length=3)]
    build: BuildProvenanceV2
    notices: NativeNoticesV2

    @model_validator(mode='after')
    def approved_row(self) -> 'NativeManifestV2':
        _exact_boundary(self)
        row = approved_pin(self)
        if tuple((a.name,a.unsigned_bytes,a.unsigned_sha256) for a in self.artifacts) != row.unsigned:
            raise ValueError('native unsigned inputs differ from the approved pin')
        if self.signature == 'adhoc':
            if (self.team_id is not None or self.notices.publisher_signature != 'unavailable'
                    or any((a.bytes,a.sha256) != (a.unsigned_bytes,a.unsigned_sha256) for a in self.artifacts)):
                raise ValueError('ad-hoc native pin is invalid')
        elif (self.team_id not in row.team_ids or self.notices.publisher_signature != 'developer-id'
                or any(a.sha256 == a.unsigned_sha256 for a in self.artifacts)):
            raise ValueError('Developer ID native pin is not approved')
        return self


def approved_pin(manifest) -> ApprovedPin:
    """The one reviewed row a lock names; any unlisted field value refuses."""
    key = (manifest.source_commit, manifest.tdlib_version, manifest.schema_sha256, manifest.minimum_os, manifest.abi,
           manifest.notices.tdlib_license_source, manifest.build.openssl_version, manifest.build.method)
    rows = [row for row in APPROVED_PINS if row.key() == key]
    if len(rows) != 1:
        raise ValueError('native pin is not an approved row')
    return rows[0]


LOCK_MODELS = {1: NativeManifest, 2: NativeManifestV2}


def parse_native_manifest(data: bytes) -> NativeManifest | NativeManifestV2:
    if type(data) is not bytes or len(data) > MAX_MANIFEST_BYTES:
        raise NativeRuntimeError('native manifest must be bounded JSON')
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError('duplicate native metadata key')
            result[key] = value
        return result
    try:
        value = json.loads(data,object_pairs_hook=unique)
        version = value.get('manifest_version') if type(value) is dict else None
        if type(version) is not int or version not in LOCK_MODELS:
            raise ValueError('native manifest version is not admitted')
        return LOCK_MODELS[version].model_validate(value)
    except (ValueError,TypeError,UnicodeError,RecursionError,ValidationError):
        raise NativeRuntimeError('native manifest does not match the closed approved pin') from None


def lock_signature(manifest) -> tuple[str, str | None]:
    """(kind, team_id) of a parsed lock: v1 is always ad hoc."""
    if type(manifest) is NativeManifest:
        return 'adhoc', None
    return manifest.signature, manifest.team_id


@dataclass(frozen=True)
class NativeRuntime:
    library_path: Path
    source_commit: str
    version: str
    probed: bool


def _read_at(stream, offset: int, size: int, length: int) -> bytes:
    if offset < 0 or size < 0 or size > 2*1024*1024 or offset+size > length:
        raise NativeRuntimeError('native Mach-O metadata is outside its bounded file')
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise NativeRuntimeError('native Mach-O metadata is truncated')
    return data


def _macho_metadata(stream, length: int) -> tuple[list[str], set[str], str]:
    header = struct.unpack('<8I',_read_at(stream,0,32,length))
    magic,cpu,subtype,filetype,ncmds,command_bytes,*_ = header
    if magic != 0xfeedfacf or cpu != 0x100000c or subtype != 0 or filetype != 6 or ncmds > 256:
        raise NativeRuntimeError('native file is not the approved thin arm64 dylib')
    commands = _read_at(stream,32,command_bytes,length)
    dependencies, symbols, minimum = [], set(), None
    symtab = None
    offset=0
    for _ in range(ncmds):
        if offset+8 > len(commands):
            raise NativeRuntimeError('native load-command table is truncated')
        command,size = struct.unpack_from('<2I',commands,offset)
        if size < 8 or size % 8 or offset+size > len(commands):
            raise NativeRuntimeError('native load-command size is invalid')
        value = commands[offset:offset+size]
        if command in {0xc,0x20,0x80000018,0x8000001f,0x80000023}:
            if size < 24:
                raise NativeRuntimeError('native dylib command is invalid')
            name_offset = struct.unpack_from('<I',value,8)[0]
            if not 24 <= name_offset < size or b'\0' not in value[name_offset:]:
                raise NativeRuntimeError('native dylib name is invalid')
            try:
                dependencies.append(value[name_offset:].split(b'\0',1)[0].decode('ascii'))
            except UnicodeError:
                raise NativeRuntimeError('native dependency encoding is invalid') from None
        elif command in {0x8000001c,0x27,0xe}:  # RPATH, loader environment or alternate dynamic linker.
            raise NativeRuntimeError('native runtime must not contain search-path commands')
        elif command == 0x32:
            if size < 24 or minimum is not None:
                raise NativeRuntimeError('native platform command is invalid')
            os_platform,version = struct.unpack_from('<2I',value,8)
            if os_platform != 1:
                raise NativeRuntimeError('native platform is not macOS')
            minimum = f'{version >> 16}.{(version >> 8)&255}' + (f'.{version&255}' if version&255 else '')
        elif command == 2:
            if size != 24 or symtab is not None:
                raise NativeRuntimeError('native symbol command is invalid')
            symtab = struct.unpack_from('<4I',value,8)
        offset += size
    if offset != len(commands) or minimum is None or symtab is None:
        raise NativeRuntimeError('native required metadata is missing')
    symoff,count,stroff,strsize = symtab
    entries = _read_at(stream,symoff,count*16,length)
    strings = _read_at(stream,stroff,strsize,length)
    for index in range(count):
        string_index,kind,section,_,_ = struct.unpack_from('<IBBHQ',entries,index*16)
        if kind & 1 and kind & 0x0e == 0x0e and section and string_index < len(strings):
            name = strings[string_index:].split(b'\0',1)[0]
            if name.startswith(b'_td_json_client_'):
                try:
                    symbols.add(name[1:].decode('ascii'))
                except UnicodeError:
                    raise NativeRuntimeError('native symbol encoding is invalid') from None
    return dependencies,symbols,minimum


def designated_requirement(team_id: str) -> str:
    """Developer ID Application leaf under Apple's Developer ID CA, bound to one Team ID."""
    if type(team_id) is not str or len(team_id) != 10 or not all(c in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in team_id):
        raise NativeRuntimeError('native Team ID is invalid')
    return ('=anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
            ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
            f' and certificate leaf[subject.OU] = "{team_id}"')


def _verify_signature(path: Path, manifest=None) -> None:
    kind, team = lock_signature(manifest) if manifest is not None else ('adhoc', None)
    command = ['/usr/bin/codesign','--verify','--strict']
    if kind == 'developer-id':
        command += ['-R',designated_requirement(team)]
    try:
        result = subprocess.run([*command,str(path)],capture_output=True,check=False,timeout=10)
        if result.returncode:
            raise NativeRuntimeError('native integrity signature or designated requirement is invalid')
    except (OSError,subprocess.TimeoutExpired):
        raise NativeRuntimeError('native integrity signature verification is unavailable') from None


def _bounded_digest(stream, expected_bytes: int) -> str:
    """Hash exactly the authenticated size; one extra byte rejects any growth."""
    stream.seek(0)
    digest = hashlib.sha256()
    remaining = expected_bytes
    while remaining:
        chunk = stream.read(min(remaining, 65536))
        if not chunk:
            raise NativeRuntimeError('native artifact is truncated')
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise NativeRuntimeError('native artifact grew beyond its pin')
    return digest.hexdigest()


def _artifact_identity(metadata: os.stat_result, artifact: NativeArtifact) -> tuple:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size != artifact.bytes):
        raise NativeRuntimeError('native artifact metadata does not match its pin')
    return file_metadata_identity(metadata)


def probe_identity(library, version: str, commit: str, *, assert_binding=lambda: None) -> None:
    """Synchronous getOption version/commit_hash on a loaded legacy tdjson, before any client exists."""
    try:
        for symbol in REQUIRED_SYMBOLS:
            getattr(library,symbol)
        execute = library.td_json_client_execute
        execute.argtypes = [ctypes.c_void_p,ctypes.c_char_p]
        execute.restype = ctypes.c_char_p
        for option,expected in (('version',version),('commit_hash',commit)):
            assert_binding()
            response = execute(None,json.dumps({'@type':'getOption','name':option},separators=(',',':')).encode())
            if response is None or len(response) > 4096:
                raise NativeRuntimeError('native synchronous version probe failed')
            result = json.loads(response)
            if type(result) is not dict or set(result) != {'@type','value'} or result['@type'] != 'optionValueString' or result['value'] != expected:
                raise NativeRuntimeError('native synchronous version or source pin does not match')
    except NativeRuntimeError:
        raise
    except (OSError,AttributeError,ValueError,TypeError,UnicodeError):
        raise NativeRuntimeError('native ABI or synchronous identity probe failed') from None


def _probe(path: Path, manifest, *, assert_binding) -> None:
    try:
        # Descriptor/hash checks run before dlopen, then again before any execute.
        # Darwin's pathname dlopen is not atomic against a malicious same-UID writer.
        assert_binding()
        library = ctypes.CDLL(str(path))
        assert_binding()
    except NativeRuntimeError:
        raise
    except (OSError,AttributeError,ValueError,TypeError,UnicodeError):
        raise NativeRuntimeError('native ABI or synchronous identity probe failed') from None
    probe_identity(library,manifest.tdlib_version,manifest.source_commit,assert_binding=assert_binding)


def validate_native(manifest: NativeManifest | NativeManifestV2, target: str, *, directory: Path,
                    probe: bool = True) -> NativeRuntime:
    """Verify one serialized held release binding before/after its path-based probe.

    B2 supplies a receipt-validated immutable release directory. File descriptors
    and cooperative shared locks remain held throughout. Rechecks reject observed
    substitutions before loading/execution; they do not provide atomic dlopen or
    protection against a malicious process that already controls this OS UID.
    A Developer ID lock also requires each artifact's designated requirement (Team ID).
    """
    if type(manifest) not in LOCK_MODELS.values():
        raise NativeRuntimeError('native manifest is invalid')
    try:
        manifest = type(manifest).model_validate(manifest.model_dump())
    except (AttributeError,ValueError):
        raise NativeRuntimeError('native manifest is invalid') from None
    try:
        os_version=tuple(int(v) for v in platform.mac_ver()[0].split('.')[:2])
    except ValueError:
        os_version=()
    if target != manifest.target or platform.system() != 'Darwin' or platform.machine() != 'arm64' or os_version < (26,0):
        raise UnsupportedNativePlatform('portable native runtime requires macOS 26.0 or newer on arm64')
    directory = Path(directory)
    bindings = []
    try:
        with ExitStack() as stack:
            directory_fd = stack.enter_context(open_owned_directory(directory))
            directory_identity = file_metadata_identity(os.fstat(directory_fd))
            # Keep every exact artifact open/locked before inspecting or probing.
            for artifact in manifest.artifacts:
                path = directory / artifact.name
                stream = stack.enter_context(open_owned_file(path))
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                identity = _artifact_identity(os.fstat(stream.fileno()), artifact)
                bindings.append((path,artifact,stream,identity))
            for path,artifact,stream,identity in bindings:
                if _bounded_digest(stream,artifact.bytes) != artifact.sha256:
                    raise NativeRuntimeError('native artifact hash does not match its pin')
                dependencies,symbols,minimum = _macho_metadata(stream,artifact.bytes)
                if dependencies != artifact.dependencies or minimum != manifest.minimum_os:
                    raise NativeRuntimeError('native loader dependencies or minimum OS do not match')
                if artifact.name == 'libtdjson.dylib' and not set(REQUIRED_SYMBOLS) <= symbols:
                    raise NativeRuntimeError('native legacy ABI symbols are incomplete')
                if _artifact_identity(os.fstat(stream.fileno()),artifact) != identity:
                    raise NativeRuntimeError('native artifact changed during validation')
                _verify_signature(path,manifest)

            def assert_binding() -> None:
                # Check pathname ancestry/binding and held objects with complete
                # security metadata; then cap/hash the still-held verified object.
                with open_owned_directory(directory) as current_directory_fd:
                    if (file_metadata_identity(os.fstat(directory_fd)) != directory_identity
                            or file_metadata_identity(os.fstat(current_directory_fd)) != directory_identity):
                        raise NativeRuntimeError('native release directory binding changed')
                for path,artifact,stream,identity in bindings:
                    if _artifact_identity(os.fstat(stream.fileno()),artifact) != identity:
                        raise NativeRuntimeError('native artifact security metadata changed')
                    with open_owned_file(path) as current:
                        if _artifact_identity(os.fstat(current.fileno()),artifact) != identity:
                            raise NativeRuntimeError('native artifact pathname binding changed')
                    if (_bounded_digest(stream,artifact.bytes) != artifact.sha256
                            or _artifact_identity(os.fstat(stream.fileno()),artifact) != identity):
                        raise NativeRuntimeError('native artifact content changed')

            assert_binding()  # Always, including metadata-only probe=False.
            if probe:
                _probe(directory/'libtdjson.dylib',manifest,assert_binding=assert_binding)
            assert_binding()
    except (RuntimePathError,OSError,struct.error):
        raise NativeRuntimeError('native artifact is unsafe or unavailable') from None
    return NativeRuntime(directory/'libtdjson.dylib',manifest.source_commit,manifest.tdlib_version,probe)
