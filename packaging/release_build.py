"""Release pipeline orchestrator (CLIENTS-14): one command from a clean checkout to every asset.

Development host only (the owner's Mac); stdlib plus this checkout's own package. In order:
prepare_payload -> sign_payload and the entitlement matrix -> repin_native and the v2 manifest ->
assemble_bundle (identity switch: none, or a named Keychain service) -> build_bootstrap_seed.sh (with
--sign-identity and the same Developer ID identity, or ad hoc in a dry run) -> the kit (Install.command
with the pins embedded, bootstrap.sh, the seed, archive, index, release-pins.json, README, licences,
SHA256SUMS) -> the kit signature check -> notarize_release.sh (payload zip, DMG, sign, notarize,
staple) -> assets/ (the DMG, the curl-path files, release-pins.json, release-provenance.json, the
corresponding-source archive and SHA256SUMS over all of them) -> release-build.json, every hash
re-derived from disk; `check` re-derives them and the commit again.

The kit signature check runs before anything is notarized: every Mach-O the kit ships, found by magic
(the seed, and each payload file that release.tar.gz holds, bound to its index sha256), must be
Developer ID signed with the pinned Team ID's designated requirement, a secure timestamp and the
hardened runtime; an ad hoc seed refuses the real build (`seed_unsigned`). In a dry run the same check
requires ad hoc signatures. Natives: an ad hoc build keeps their pinned unsigned bytes (no
hardened-runtime flag), a Developer ID build re-signs them (NATIVE_SIGNING, recorded in provenance).

--dry-run runs every step except notarization and stapling, with the ad hoc identity `-`: the payload
zip is built and the DMG is signed ad hoc and verified; its output is a test artifact. Nothing is
uploaded or published; publication stays `unreleased` and source completeness `incomplete` until the
corresponding-source inventory covers every component. The output is a fresh owner-only directory
outside the checkout and outside /tmp. Refusals are fixed codes; no path, input or tool output is echoed.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = 'tgmcp-release-build/v1'
CODES = frozenset({'usage', 'output_refused', 'checkout_dirty', 'tag_mismatch', 'prepare_failed', 'sign_failed',
                   'entitlements_unproven', 'repin_failed', 'assemble_failed', 'seed_failed', 'kit_failed',
                   'notarize_failed', 'source_archive_failed', 'evidence_invalid', 'evidence_mismatch',
                   'seed_unsigned', 'signature_refused', 'release_build_refused'})
TOOL_ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
GIT_ENV = {**TOOL_ENV, 'HOME': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
TAG = re.compile(r'v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:rc[0-9]{1,4})?')
PROFILE = re.compile(r'[A-Za-z0-9._][A-Za-z0-9._-]{0,63}')
HASH = re.compile(r'[0-9a-f]{64}')
TEAM = re.compile(r'[0-9A-Z]{10}')
PIN_KEYS = frozenset({'schema_version', 'release_version', 'publisher', 'team_id', 'publication', 'seed', 'index',
                      'archive'})
SEED_KINDS = {'adhoc': 'developer-built-ad-hoc-seed', 'developer-id': 'developer-id-seed'}
# How native/ is signed, recorded in release-provenance.json (policy values: sign_payload.NATIVE_POLICY).
NATIVE_SIGNING = {
    'adhoc': {'policy': 'pinned-unsigned-bytes-no-runtime',
              'note': 'native/ keeps its pinned unsigned bytes, so it has no hardened-runtime flag: the ad hoc '
                      'v2 native lock admits only the v1 pins'},
    'developer-id': {'policy': 'developer-id-resigned-runtime',
                     'note': 'native/ is re-signed with Developer ID, the hardened runtime and a secure timestamp; '
                             'the v2 native lock pins the signed bytes and the unsigned v1 pins'}}
CS_RUNTIME = 0x10000
CODESIGN_TIMEOUT = 120
KIT_DATA = ('bootstrap.sh', 'release.tar.gz', 'release-index.json', 'release-pins.json', 'README.txt', 'LICENSE',
            'NOTICE', 'third-party-notices.md')
KIT_EXECUTABLE = ('Install.command', 'bootstrap-verifier')
CURL_PATH = ('bootstrap.sh', 'bootstrap-verifier', 'release.tar.gz', 'release-index.json', 'release-pins.json')
GIT_TIMEOUT, SEED_TIMEOUT, NOTARY_TIMEOUT = 120, 900, 7200
MAX_EVIDENCE = 4 << 20
MAX_OUTPUT = 1 << 20
CHUNK = 1024 * 1024


class BuildError(ValueError):
    """One fixed refusal code from CODES."""

    def __init__(self, code):
        super().__init__(code if code in CODES else 'release_build_refused')


def _need(condition, code):
    if not condition:
        raise BuildError(code)


def _sibling(name):
    spec = importlib.util.spec_from_file_location(f'release_build_{name}', HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False, allow_nan=False) + '\n').encode()


def file_digest(path):
    """(sha256, bytes) of one regular non-symlink file, streamed."""
    with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        _need(stat.S_ISREG(metadata.st_mode), 'evidence_mismatch')
        digest, total = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
            total += len(chunk)
    _need(total == metadata.st_size, 'evidence_mismatch')
    return digest.hexdigest(), total


def _write(path, data, mode=0o444):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(path, mode)


def _copy(source, target, mode=0o444):
    with open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream, \
            open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'kit_failed')
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(target, mode)


def _git(checkout, *args):
    try:
        result = subprocess.run(['/usr/bin/git', '-C', str(checkout), *args], capture_output=True, check=False,
                                timeout=GIT_TIMEOUT, env=GIT_ENV)
    except (OSError, subprocess.SubprocessError):
        raise BuildError('checkout_dirty') from None
    return result


def head_commit(checkout):
    result = _git(checkout, 'rev-parse', '--verify', 'HEAD^{commit}')
    value = result.stdout.decode('ascii', 'replace').strip()
    _need(result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', value) is not None, 'checkout_dirty')
    return value


def _clean(checkout, tag):
    """HEAD with no tracked change; a named tag must point at HEAD."""
    commit = head_commit(checkout)
    status = _git(checkout, 'status', '--porcelain', '--untracked-files=no')
    _need(status.returncode == 0 and not status.stdout.strip(), 'checkout_dirty')
    if tag is not None:
        _need(type(tag) is str and TAG.fullmatch(tag) is not None, 'usage')
        tagged = _git(checkout, 'rev-parse', '--verify', '--quiet', f'refs/tags/{tag}^{{commit}}')
        _need(tagged.returncode == 0 and tagged.stdout.decode('ascii', 'replace').strip() == commit, 'tag_mismatch')
    return commit


def _fresh_output(output, checkout):
    output = Path(output)
    _need(output.is_absolute() and '..' not in output.parts and not output.exists() and not output.is_symlink()
          and output.parent.is_dir() and not output.parent.is_symlink(), 'output_refused')
    _need(not any(output == p or output.is_relative_to(p) for p in (Path('/tmp'), Path('/private/tmp'))),
          'output_refused')
    _need(not output.resolve().is_relative_to(Path(checkout).resolve())
          and not any((p / '.git').exists() for p in output.parents), 'output_refused')
    return output


def slug(display_name):
    value = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
    _need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value) is not None, 'usage')
    return value


# Default steps (tests replace them) --------------------------------------------------------------

def _default_steps(runner=subprocess.run):
    payload, signer, repin, assembler = (_sibling(n) for n in ('prepare_payload', 'sign_payload', 'repin_native',
                                                                'assemble_bundle'))

    def seed(checkout, output, identity, team_id):
        argv = ['/bin/sh', str(Path(checkout) / 'packaging/build_bootstrap_seed.sh'), '--output-directory', str(output)]
        env = dict(TOOL_ENV)
        if identity is not None:  # Developer ID: codesign reads the login Keychain through HOME.
            argv += ['--sign-identity', identity, '--team-id', team_id]
            if os.environ.get('HOME', '').startswith('/'):
                env['HOME'] = os.environ['HOME']
        try:
            result = runner(argv, capture_output=True, check=False, timeout=SEED_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError):
            raise BuildError('seed_failed') from None
        _need(result.returncode == 0, 'seed_failed')

    def notarize(checkout, arguments, real):
        env = dict(TOOL_ENV)
        if real and os.environ.get('HOME', '').startswith('/'):
            env['HOME'] = os.environ['HOME']  # notarytool reads its keychain profile from the login Keychain
        try:
            result = runner(['/bin/sh', '-p', str(Path(checkout) / 'packaging/notarize_release.sh'), *arguments],
                            capture_output=True, check=False, timeout=NOTARY_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError):
            raise BuildError('notarize_failed') from None
        _need(result.returncode == 0, 'notarize_failed')

    return {
        'git_export': payload.git_export,
        'fetch': payload.fetch,
        'load_lock': payload.load_lock,
        'prepare': lambda output, native_directory, cache, checkout, offline: payload.prepare_payload(
            output, native_directory=native_directory, cache=cache, checkout=checkout, offline=offline),
        'sign': lambda payload_dir, output, identity, team_id: signer.sign_payload(
            payload_dir, output, identity=identity, team_id=team_id),
        'matrix': lambda signed, scratch, identity, team_id, version: signer.entitlement_matrix(
            signed, scratch, identity=identity, team_id=team_id, version=version),
        'check_matrix': signer.check_matrix,
        'identity_kind': signer.identity_kind,
        'repin': lambda output, v1, unsigned, signed, team_id: repin.repin_native(
            output, v1_lock=v1, unsigned_directory=unsigned, signed_directory=signed, team_id=team_id),
        'assemble': lambda signed, output, service: assembler.assemble_bundle(signed, output,
                                                                               app_identity_service=service),
        'seed': seed,
        'signatures': lambda kit, signed, index, kind, team_id: kit_signatures(kit, signed, index, kind, team_id,
                                                                               runner=runner),
        'notarize': notarize,
    }


# Kit signatures ----------------------------------------------------------------------------------

def _codesign_fields(text):
    """The first value of each `codesign -d --verbose=4` field this check reads; a closed parse."""
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        if not separator:
            continue
        if key == 'CodeDirectory v' and 'flags' not in fields:
            found = re.search(r' flags=0x([0-9a-f]{1,8})\(', value)
            _need(found is not None, 'signature_refused')
            fields['flags'] = int(found.group(1), 16)
        elif key in ('Identifier', 'TeamIdentifier', 'Signature', 'Timestamp', 'Signed Time', 'Authority'):
            fields.setdefault(key, value)
    return fields


def signature_check(path, kind, team_id, runner=subprocess.run):
    """One Mach-O: `codesign --verify --strict` (with the Team ID's designated requirement for Developer ID),
    then what `codesign -d` shows. Developer ID: the pinned Team ID, a Developer ID Application leaf, a
    secure timestamp (never `Signed Time`) and the hardened runtime. Ad hoc: an ad hoc signature, no team."""
    from telegram_search_mcp.native_runtime import designated_requirement
    requirement = ['-R', designated_requirement(team_id)] if kind == 'developer-id' else []
    for argv in (['/usr/bin/codesign', '--verify', '--strict', *requirement, str(path)],
                 ['/usr/bin/codesign', '-d', '--verbose=4', str(path)]):
        try:
            result = runner(argv, capture_output=True, check=False, timeout=CODESIGN_TIMEOUT, env=dict(TOOL_ENV))
        except (OSError, subprocess.SubprocessError):
            raise BuildError('signature_refused') from None
        _need(result.returncode == 0 and len(result.stdout or b'') <= MAX_OUTPUT
              and len(result.stderr or b'') <= MAX_OUTPUT, 'signature_refused')
    text = result.stderr.decode('utf-8', 'replace') if type(result.stderr) is bytes else (result.stderr or '')
    fields = _codesign_fields(text)
    if kind == 'developer-id':
        leaf = re.fullmatch(r'Developer ID Application: .+ \(([0-9A-Z]{10})\)', fields.get('Authority', ''))
        _need(fields.get('TeamIdentifier') == team_id and leaf is not None and leaf.group(1) == team_id
              and bool(fields.get('Timestamp')) and 'Signed Time' not in fields
              and bool(fields.get('flags', 0) & CS_RUNTIME), 'signature_refused')
    else:
        _need(kind == 'adhoc' and fields.get('Signature') == 'adhoc' and fields.get('TeamIdentifier') == 'not set',
              'signature_refused')


def kit_signatures(kit, signed, index, kind, team_id, runner=subprocess.run):
    """Every Mach-O the kit ships, found by magic: the seed at its top level and the payload files that
    release.tar.gz holds (each bound to its index sha256; the same tree is notarized as the payload zip).
    Each must pass signature_check for `kind`; returns a nonsecret summary."""
    kit, signed = Path(kit), Path(signed)
    signer = _sibling('sign_payload')
    try:
        entries = sorted(kit.iterdir(), key=lambda p: p.name)
        _need(all(p.is_file() and not p.is_symlink() for p in entries), 'signature_refused')
        top = [p.name for p in entries if signer.macho(p) is not None]
        payload = [entry['path'] for entry in signer.inventory(signed, signer._walk(signed))]
    except signer.SigningError:
        raise BuildError('signature_refused') from None
    _need(top == ['bootstrap-verifier'] and payload, 'signature_refused')
    listed = {f['path']: f['sha256'] for f in index['files']}
    for relative in payload:
        _need(listed.get(relative) == file_digest(signed / relative)[0], 'signature_refused')
    for path in [kit / 'bootstrap-verifier', *(signed / relative for relative in payload)]:
        signature_check(path, kind, team_id, runner)
    return {'kind': kind, 'team_id': team_id, 'checked': len(payload) + 1, 'seed': 1, 'payload': len(payload),
            'counted_by': 'magic'}


# Build -------------------------------------------------------------------------------------------

def release_build(output, *, native_directory, cache, checkout=ROOT, offline=False, dry_run=False, identity=None,
                  team_id=None, notary_profile=None, app_identity_service=None, tag=None, steps=None):
    """Build every asset into one fresh owner-only directory; returns the release-build.json value."""
    try:
        return _release_build(Path(output), Path(native_directory), Path(cache), Path(checkout), offline, dry_run,
                              identity, team_id, notary_profile, app_identity_service, tag, steps)
    except BuildError:
        raise
    except Exception as error:  # Step refusals carry their own fixed codes; map them, never echo them.
        raise BuildError(_STEP_CODES.get(type(error).__name__, 'release_build_refused')) from None


_STEP_CODES = {'PayloadError': 'prepare_failed', 'SigningError': 'sign_failed', 'RepinError': 'repin_failed',
               'ReleasePackageError': 'assemble_failed', 'InstallCommandError': 'kit_failed'}


def _release_build(output, native_directory, cache, checkout, offline, dry_run, identity, team_id, notary_profile,
                   service, tag, steps):
    from telegram_search_mcp import product_identity
    steps = steps or _default_steps()
    if dry_run:
        _need(identity is None and team_id is None and notary_profile is None, 'usage')
        identity, kind = '-', 'adhoc'
    else:
        try:
            kind = steps['identity_kind'](identity, team_id)
        except ValueError:
            raise BuildError('usage') from None
        _need(kind == 'developer-id' and type(notary_profile) is str and PROFILE.fullmatch(notary_profile), 'usage')
    _need(service is None or (type(service) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', service)),
          'usage')
    commit = _clean(checkout, tag)
    output = _fresh_output(output, checkout)
    name = slug(product_identity.DISPLAY_NAME)
    commit_found, files = steps['git_export'](checkout)
    _need(commit_found == commit, 'checkout_dirty')
    required = ('packaging/bootstrap.sh', 'packaging/tdlib-lock.json', 'packaging/payload-lock.json', 'LICENSE',
                'NOTICE', 'docs/third-party-notices.md')
    _need(all(n in files for n in required), 'prepare_failed')
    output.mkdir(mode=0o700)
    work, kit, assets = output / 'work', output / 'kit', output / 'assets'
    for directory in (work, kit, assets):
        directory.mkdir(mode=0o700)

    # 1-2. Payload, then the signed copy and the entitlement matrix it must agree with.
    prepared = steps['prepare'](work / 'payload', native_directory, cache, checkout, offline)
    _need(prepared['source_commit'] == commit, 'prepare_failed')
    version = prepared['package_version']
    v1 = files['packaging/tdlib-lock.json'][0]
    _need((work / 'payload/tdlib-lock.json').read_bytes() == v1, 'prepare_failed')
    tdlib_version = json.loads(v1)['tdlib_version']
    signing = steps['sign'](work / 'payload', work / 'signed', identity, team_id)
    matrix = steps['matrix'](work / 'signed', work / 'matrix', identity, team_id, tdlib_version)
    try:
        granted = steps['check_matrix'](matrix, kind)
    except ValueError:
        raise BuildError('entitlements_unproven') from None
    _need(sorted(signing['interpreter_entitlements']) == sorted(granted), 'entitlements_unproven')
    _write(work / 'entitlement-matrix.json', json_bytes(matrix), 0o400)
    shutil.rmtree(work / 'matrix', ignore_errors=True)

    # 3. The v2 native lock from HEAD's v1 pins, and the manifest that names it and its publisher.
    repinned = steps['repin'](work / 'tdlib-lock.v2.json', v1, work / 'payload/native', work / 'signed/native',
                              None if kind == 'adhoc' else team_id)
    lock = (work / 'tdlib-lock.v2.json').read_bytes()
    _need(repinned['lock_sha256'] == sha256(lock), 'repin_failed')
    manifest = _republish(work / 'signed', lock, kind, None if kind == 'adhoc' else team_id)

    # 4. The bundle, with or without an application identity.
    bundle = steps['assemble'](work / 'signed', work / 'bundle', service)
    index_bytes = (work / 'bundle/release-index.json').read_bytes()
    index = json.loads(index_bytes)
    _need(bundle.index_sha256 == sha256(index_bytes) and index['release_version'] == version, 'assemble_failed')
    identity_record = next((f for f in index['files'] if f['path'] == 'app-identity.json'), None)
    _need((identity_record is None) == (service is None), 'assemble_failed')

    # 5. The seed, built from this commit: Developer ID with the payload's identity, ad hoc in a dry run.
    pinned_team = None if kind == 'adhoc' else team_id
    steps['seed'](checkout, work / 'seed', None if kind == 'adhoc' else identity, pinned_team)
    seed_sha, seed_bytes = file_digest(work / 'seed/bootstrap-verifier')
    seed_provenance = json.loads((work / 'seed/provenance.json').read_bytes())
    _need(type(seed_provenance) is dict and seed_provenance.get('source_commit') == commit
          and seed_provenance.get('binary_sha256') == seed_sha and seed_provenance.get('binary_bytes') == seed_bytes,
          'seed_failed')
    _need(seed_provenance.get('kind') == SEED_KINDS[kind]
          and seed_provenance.get('publisher_identity') is (kind == 'developer-id')
          and seed_provenance.get('team_id') == pinned_team, 'seed_failed' if kind == 'adhoc' else 'seed_unsigned')

    # 6. The kit: what the DMG holds.
    install = _sibling('install_command')
    archive_sha, archive_bytes = file_digest(work / 'bundle/release.tar.gz')
    _need(archive_sha == bundle.archive_sha256, 'assemble_failed')
    pins = {'schema_version': 1, 'release_version': version, 'publisher': kind, 'team_id': pinned_team,
            'publication': manifest['publication'],
            'seed': {'file': 'bootstrap-verifier', 'sha256': seed_sha, 'bytes': seed_bytes},
            'index': {'file': 'release-index.json', 'sha256': sha256(index_bytes)},
            'archive': {'file': 'release.tar.gz', 'sha256': archive_sha, 'bytes': archive_bytes}}
    _write(kit / 'Install.command', install.render(seed_sha256=seed_sha, seed_bytes=seed_bytes,
                                                   index_sha256=sha256(index_bytes), team_id=pinned_team,
                                                   published=manifest['publication'] == 'published'), 0o555)
    _write(kit / 'bootstrap.sh', files['packaging/bootstrap.sh'][0])
    _copy(work / 'seed/bootstrap-verifier', kit / 'bootstrap-verifier', 0o555)
    _copy(work / 'bundle/release.tar.gz', kit / 'release.tar.gz')
    _write(kit / 'release-index.json', index_bytes)
    _write(kit / 'release-pins.json', json_bytes(pins))
    _write(kit / 'README.txt', readme(product_identity.DISPLAY_NAME, version, dry_run))
    _write(kit / 'LICENSE', files['LICENSE'][0])
    _write(kit / 'NOTICE', files['NOTICE'][0])
    _write(kit / 'third-party-notices.md', files['docs/third-party-notices.md'][0])
    _write(kit / 'SHA256SUMS', sums(kit))

    # 7. Before anything is notarized: every Mach-O the kit ships carries the expected signature.
    signatures = steps['signatures'](kit, work / 'signed', index, kind, pinned_team)
    _need(type(signatures) is dict and signatures.get('kind') == kind and signatures.get('team_id') == pinned_team,
          'signature_refused')

    # 8. Notarization and stapling (skipped in a dry run) and the DMG, signed ad hoc in a dry run.
    dmg = f'{name}-{version}-macos-arm64.dmg'
    arguments = ['--kit', str(kit), '--dmg', str(assets / dmg), '--volume-name', product_identity.DMG_VOLUME_NAME,
                 '--evidence', str(work / 'notary.json'), '--payload', str(work / 'signed')]
    arguments += ['--dry-run'] if dry_run else ['--identity', identity, '--team-id', team_id,
                                                '--profile', notary_profile]
    os.chmod(kit, 0o755)  # The volume root: readable by whoever mounts the image.
    steps['notarize'](checkout, arguments, not dry_run)
    notary = json.loads((work / 'notary.json').read_bytes())
    _need(type(notary) is dict and notary.get('mode') == ('dry-run' if dry_run else 'notarized')
          and type(notary.get('dmg')) is dict and notary['dmg'].get('contents') == 'matches SHA256SUMS'
          and notary['dmg'].get('sha256') == file_digest(assets / dmg)[0]
          and (notary['dmg'].get('signature') == 'adhoc' if dry_run else notary['dmg'].get('team_id') == team_id),
          'notarize_failed')
    os.chmod(assets / dmg, 0o444)

    # 9. Assets: curl-path copies, the corresponding source, provenance, SHA256SUMS.
    for asset in CURL_PATH:
        _copy(kit / asset, assets / asset, 0o555 if asset in KIT_EXECUTABLE else 0o444)
    source_name = f'corresponding-source-{version}.tar.gz'
    lock_value = steps['load_lock'](files['packaging/payload-lock.json'][0])
    tdlib_source = steps['fetch'](lock_value['tdlib_source'], cache, offline=offline)
    components = (work / 'payload/provenance/components.json').read_bytes()
    inventory = source_archive(assets / source_name, version=version, commit=commit, tag=tag, files=files,
                               tdlib=tdlib_source, lock=lock_value, components=components)
    provenance = {
        'schema_version': 1, 'release_version': version, 'source_commit': commit, 'tag': tag,
        'mode': 'adhoc-dry-run' if dry_run else 'developer-id', 'publication': manifest['publication'],
        'source_completeness': manifest['source_completeness'],
        'payload': {k: prepared[k] for k in ('tree_sha256', 'files', 'macho', 'macho_total', 'manifest_sha256')},
        'signing': signing, 'native_signing': NATIVE_SIGNING[kind], 'entitlement_matrix': matrix,
        'native_lock': {'sha256': sha256(lock), 'publisher': repinned['publisher']},
        'manifest_sha256': sha256((work / 'signed/manifest/release.json').read_bytes()),
        'bundle': {'archive_sha256': archive_sha, 'archive_bytes': archive_bytes, 'index_sha256': sha256(index_bytes),
                   'files': bundle.files, 'provenance_sha256': bundle.provenance_sha256,
                   'app_identity': {'source': 'none' if service is None else 'named-keychain-service',
                                    'sha256': None if identity_record is None else identity_record['sha256']}},
        'seed': {'sha256': seed_sha, 'bytes': seed_bytes, 'provenance': seed_provenance},
        'kit_signatures': signatures,
        'notarization': notary,
        'source_archive': {'file': source_name, 'inventory_sha256': sha256(json_bytes(inventory)),
                           'source_completeness': inventory['source_completeness']}}
    _write(assets / 'release-provenance.json', json_bytes(provenance))
    _write(assets / 'SHA256SUMS', sums(assets))

    # 10. The gate JSON, every hash re-derived from disk.
    evidence = derive_evidence(output, commit=commit, version=version, tag=tag, mode=provenance['mode'])
    _write(output / 'release-build.json', canonical(evidence), 0o400)
    return evidence


def _republish(signed, lock, kind, team_id):
    """Swap the signed tree's native lock for the v2 one and point the manifest at it and its publisher."""
    manifest_path = signed / 'manifest/release.json'
    manifest = json.loads(manifest_path.read_bytes())
    _need(type(manifest) is dict and manifest.get('schema_version') == 2 and 'publisher' in manifest, 'repin_failed')
    manifest.update(native_lock_sha256=sha256(lock), publisher={'kind': kind, 'team_id': team_id})
    for path, data in ((signed / 'tdlib-lock.json', lock), (manifest_path, json_bytes(manifest))):
        os.chmod(path, 0o600)
        with open(os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW), 'wb') as out:
            out.write(data)
        os.chmod(path, 0o400)
    return manifest


def readme(display_name, version, dry_run):
    build = ('Ad hoc test build (dry run): not notarized, so Gatekeeper refuses it on other Macs.\n' if dry_run
             else 'Signed with Developer ID and notarized; the DMG is stapled.\n')
    return (f'{display_name} {version} for macOS 26 or later on Apple silicon\n\n'
            'Double-click Install.command. In Terminal it verifies this kit against the pins embedded in it,\n'
            'installs into your user account and registers the MCP server with the supported AI clients it\n'
            'finds. It then starts the Telegram login in the same window: a QR page opens in your browser;\n'
            'scan it with Telegram on your phone. The final result prints in Terminal.\n\n'
            'Without the DMG: run `/usr/bin/env -i /bin/sh -p bootstrap.sh` with the seed and index pins from\n'
            'release-pins.json, and --seed-team with its team_id when it has one (see the install guide).\n\n'
            'SHA256SUMS lists every file here. Licences: LICENSE, NOTICE, third-party-notices.md.\n'
            + build).encode()


def sums(directory):
    rows = []
    for path in sorted(Path(directory).iterdir(), key=lambda p: p.name):
        if path.name == 'SHA256SUMS':
            continue
        digest, _ = file_digest(path)
        rows.append(f'{digest}  {path.name}\n')
    return ''.join(rows).encode()


def _tar(path, members, mtime):
    """A deterministic tar.gz: sorted members, fixed owner, mtime and modes."""
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT, encoding='utf-8') as tar:
                for name in sorted(members):
                    data, executable = members[name]
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime, info.mode = len(data), mtime, 0o755 if executable else 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    tar.addfile(info, io.BytesIO(data))
        raw.flush()
        os.fsync(raw.fileno())
    os.chmod(path, 0o444)


def source_archive(path, *, version, commit, tag, files, tdlib, lock, components):
    """corresponding-source-<version>.tar.gz: the project at HEAD, the pinned TDLib source archive and
    references for what is not yet carried; returns its INVENTORY.json value."""
    prefix = f'corresponding-source-{version}/'
    python = lock['python']
    tdlib_name = f'tdlib/td-{lock["tdlib_source"]["commit"]}.tar.gz'
    _need(len(tdlib) == lock['tdlib_source']['bytes'] and sha256(tdlib) == lock['tdlib_source']['sha256'],
          'source_archive_failed')
    references = {
        'references/python-build-standalone.json': json_bytes({
            'upstream': 'https://github.com/astral-sh/python-build-standalone', 'tag': python['pbs_tag'],
            'recipe': f'https://github.com/astral-sh/python-build-standalone/tree/{python["pbs_tag"]}',
            'python_version': python['version'],
            'cpython_source': f'https://www.python.org/ftp/python/{python["version"]}/Python-{python["version"]}.tar.xz',
            'runtime_archive': {'url': python['url'], 'bytes': python['bytes'], 'sha256': python['sha256']}}),
        'references/openssl.json': json_bytes({
            'version': lock['native_notices']['openssl_version'], 'upstream': 'https://github.com/openssl/openssl',
            'source': f'https://github.com/openssl/openssl/releases/tag/openssl-{lock["native_notices"]["openssl_version"]}'}),
        'references/components.json': components}
    inventory = {
        'schema_version': 1, 'release_version': version, 'source_commit': commit, 'tag': tag,
        'source_completeness': 'incomplete',
        'included': [{'component': 'project', 'path': 'project/', 'files': len(files),
                      'origin': 'committed blobs of the source commit'},
                     {'component': 'build scripts', 'path': 'project/packaging/',
                      'files': sum(name.startswith('packaging/') for name in files)},
                     {'component': 'tdlib', 'path': tdlib_name, 'commit': lock['tdlib_source']['commit'],
                      'bytes': len(tdlib), 'sha256': sha256(tdlib)}],
        'referenced': sorted(references),
        'missing': ['CPython corresponding source and the python-build-standalone build',
                    'OpenSSL corresponding source archive',
                    'sources of the native wheels: PyMuPDF/MuPDF, Pillow and its libraries, lxml, cryptography with '
                    'its OpenSSL, pydantic_core, rpds']}
    members = {prefix + 'project/' + name: (data, executable) for name, (data, executable) in files.items()}
    members[prefix + tdlib_name] = (tdlib, False)
    members.update({prefix + name: (data, False) for name, data in references.items()})
    members[prefix + 'INVENTORY.json'] = (json_bytes(inventory), False)
    try:
        _tar(path, members, 0)
    except (OSError, ValueError, tarfile.TarError):
        raise BuildError('source_archive_failed') from None
    return inventory


# Evidence ----------------------------------------------------------------------------------------

def _listing(directory):
    names = sorted(p.name for p in Path(directory).iterdir())
    return {name: dict(zip(('sha256', 'bytes'), file_digest(Path(directory) / name))) for name in names}


def derive_evidence(output, *, commit, version, tag, mode):
    output = Path(output)
    assets, kit = _listing(output / 'assets'), _listing(output / 'kit')
    return {'schema': SCHEMA, 'mode': mode, 'source_commit': commit, 'release_version': version, 'tag': tag,
            'assets': assets, 'kit': kit}


def _parse_sums(data):
    rows = {}
    for line in data.decode('utf-8').splitlines():
        digest, _, name = line.partition('  ')
        _need(HASH.fullmatch(digest) is not None and name and name not in rows and '/' not in name, 'evidence_mismatch')
        rows[name] = digest
    return rows


def check(output, checkout=ROOT):
    """Re-derive every recorded hash and the commit from disk; returns a nonsecret summary."""
    try:
        return _check(Path(output), Path(checkout))
    except BuildError:
        raise
    except Exception:  # Malformed evidence, pins, sums or kit text (KeyError, UnicodeError, ...): one fixed code.
        raise BuildError('evidence_invalid') from None


def _pins_team(pins, mode):
    """The kit pins' closed shape for this evidence mode; returns the pinned Team ID (None ad hoc)."""
    _need(type(pins) is dict and set(pins) == PIN_KEYS and pins['schema_version'] == 1
          and pins['publication'] in ('unreleased', 'published')
          and all(type(pins[k]) is dict for k in ('seed', 'index', 'archive')), 'evidence_invalid')
    team = pins['team_id']
    if mode == 'developer-id':
        _need(pins['publisher'] == 'developer-id' and type(team) is str and TEAM.fullmatch(team) is not None,
              'evidence_mismatch')
    else:
        _need(mode == 'adhoc-dry-run' and pins['publisher'] == 'adhoc' and team is None
              and pins['publication'] == 'unreleased', 'evidence_mismatch')
    return team


def _check(output, checkout):
    try:
        raw = (output / 'release-build.json').read_bytes()
        _need(len(raw) <= MAX_EVIDENCE, 'evidence_invalid')
        evidence = json.loads(raw)
    except (OSError, ValueError):
        raise BuildError('evidence_invalid') from None
    _need(type(evidence) is dict and canonical(evidence) == raw and evidence.get('schema') == SCHEMA
          and set(evidence) == {'schema', 'mode', 'source_commit', 'release_version', 'tag', 'assets', 'kit'}
          and evidence['mode'] in ('adhoc-dry-run', 'developer-id')
          and all(type(evidence[k]) is dict for k in ('assets', 'kit')), 'evidence_invalid')
    _need(head_commit(checkout) == evidence['source_commit'], 'evidence_mismatch')
    derived = derive_evidence(output, commit=evidence['source_commit'], version=evidence['release_version'],
                              tag=evidence['tag'], mode=evidence['mode'])
    _need(derived == evidence, 'evidence_mismatch')
    _need(set(evidence['kit']) == {*KIT_DATA, *KIT_EXECUTABLE, 'SHA256SUMS'}
          and set(CURL_PATH) | {'SHA256SUMS', 'release-provenance.json'} < set(evidence['assets']), 'evidence_mismatch')
    for group in ('assets', 'kit'):
        listed = _parse_sums((output / group / 'SHA256SUMS').read_bytes())
        _need(listed == {n: v['sha256'] for n, v in evidence[group].items() if n != 'SHA256SUMS'}, 'evidence_mismatch')
    pins = json.loads((output / 'kit/release-pins.json').read_bytes())
    team = _pins_team(pins, evidence['mode'])
    _need(pins['seed']['sha256'] == evidence['kit']['bootstrap-verifier']['sha256']
          and pins['seed']['bytes'] == evidence['kit']['bootstrap-verifier']['bytes']
          and pins['index']['sha256'] == evidence['kit']['release-index.json']['sha256']
          and pins['archive']['sha256'] == evidence['kit']['release.tar.gz']['sha256'], 'evidence_mismatch')
    command = (output / 'kit/Install.command').read_bytes().decode('ascii')
    _need(f"install_seed_sha256={pins['seed']['sha256']}\n" in command
          and f"install_seed_bytes={pins['seed']['bytes']}\n" in command
          and f"install_index_sha256={pins['index']['sha256']}\n" in command
          and f"install_seed_team='{team or ''}'\n" in command, 'evidence_mismatch')
    for name in CURL_PATH:
        _need(evidence['assets'][name] == evidence['kit'][name], 'evidence_mismatch')
    return {'status': 'verified', 'source_commit': evidence['source_commit'], 'mode': evidence['mode'],
            'assets': len(evidence['assets']), 'kit': len(evidence['kit'])}


def main(argv=None):
    import argparse

    class Parser(argparse.ArgumentParser):
        def error(self, message):  # A usage error never echoes the arguments.
            raise BuildError('usage')

    parser = Parser(description='Build every release asset into a fresh owner-only directory.')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    build = commands.add_parser('build')
    build.add_argument('--output', required=True, help='fresh owner-only directory (outside the checkout and /tmp)')
    build.add_argument('--native-directory', required=True, help='prepare_native.py output directory')
    build.add_argument('--cache', required=True, help='private download cache (mode 0700, never under /tmp)')
    build.add_argument('--offline', action='store_true', help='use only cached inputs')
    build.add_argument('--checkout', default=str(ROOT), help='clean checkout whose HEAD is built')
    build.add_argument('--tag', help='release tag that must point at HEAD')
    build.add_argument('--app-identity-service', help='named Keychain service holding the app identity (never printed)')
    build.add_argument('--dry-run', action='store_true', help='ad hoc identity, no notarization (a test artifact)')
    build.add_argument('--identity', help='Developer ID Application identity (name with Team ID, or SHA-1)')
    build.add_argument('--team-id', help='its Team ID')
    build.add_argument('--notary-profile', help='notarytool keychain profile name')
    verify = commands.add_parser('check')
    verify.add_argument('--output', required=True)
    verify.add_argument('--checkout', default=str(ROOT))
    try:
        args = parser.parse_args(argv)
        if str(Path(args.checkout) / 'src') not in sys.path and 'telegram_search_mcp' not in sys.modules:
            sys.path.insert(0, str(Path(args.checkout) / 'src'))
        if args.command == 'check':
            result = check(Path(args.output), Path(args.checkout))
        else:
            evidence = release_build(Path(args.output), native_directory=Path(args.native_directory),
                                     cache=Path(args.cache), checkout=Path(args.checkout), offline=args.offline,
                                     dry_run=args.dry_run, identity=args.identity, team_id=args.team_id,
                                     notary_profile=args.notary_profile, app_identity_service=args.app_identity_service,
                                     tag=args.tag)
            result = {'status': 'built', 'mode': evidence['mode'], 'source_commit': evidence['source_commit'],
                      'assets': {n: v['sha256'] for n, v in evidence['assets'].items()}}
    except BuildError as error:
        print(json.dumps({'status': 'refused', 'code': str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
