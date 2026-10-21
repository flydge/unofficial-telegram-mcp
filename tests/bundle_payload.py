"""Synthetic prepared payload for the bundle assembler; no real runtime, native library or secret."""
import hashlib
import json
from pathlib import Path


def v1_lock(natives):
    """A v1 lock over synthetic native bytes (no dependencies; the assembler never loads them)."""
    from telegram_search_mcp.native_runtime import COMMIT, NAMES, REQUIRED_SYMBOLS, SCHEMA_SHA256
    return dict(manifest_version=1, target='macos-arm64', platform='Darwin', architecture='arm64', minimum_os='26.0',
        source_commit=COMMIT, tdlib_version='1.8.67', schema_sha256=SCHEMA_SHA256, abi='legacy-td_json_client',
        required_symbols=list(REQUIRED_SYMBOLS), signature='adhoc-local-integrity-only',
        artifacts=[dict(name=n, bytes=len(natives[n]), sha256=hashlib.sha256(natives[n]).hexdigest(), dependencies=[])
                   for n in NAMES],
        build=dict(method='homebrew-receipt-relocated', reproducibility='unverified', compiler='clang',
            build_os='synthetic', xcode='synthetic', clt='synthetic', used_options=[], known_formula_flags=[],
            implicit_flags='homebrew-std_cmake_args-not-captured', openssl_version='3.6.4'),
        notices=dict(status='pending-public-distribution-review', tdlib_license='BSL-1.0',
            tdlib_source='https://github.com/tdlib/td',
            tdlib_license_source='https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/LICENSE_1_0.txt',
            openssl_license='Apache-2.0', source_rebuild='pending', publisher_signature='unavailable'))


def v2_lock(lock, *, signature='adhoc', team_id=None, signed=None, **pins):
    """The v2 form of a v1 lock: unsigned = the v1 pins; `signed` maps name -> shipped bytes."""
    value = json.loads(json.dumps(lock))
    value.update(manifest_version=2, signature=signature, team_id=team_id)
    value['notices']['publisher_signature'] = 'developer-id' if signature == 'developer-id' else 'unavailable'
    for artifact in value['artifacts']:
        artifact['unsigned_bytes'], artifact['unsigned_sha256'] = artifact['bytes'], artifact['sha256']
        if signed and artifact['name'] in signed:
            artifact['bytes'], artifact['sha256'] = len(signed[artifact['name']]), hashlib.sha256(signed[artifact['name']]).hexdigest()
    for key, item in pins.items():
        parent, _, field = key.rpartition('__')
        (value[parent] if parent else value)[field] = item
    return value


def synthetic_pin(natives, *, team_ids=(), **fields):
    """The approved d1085f9 row re-pinned to synthetic unsigned natives (and any field override)."""
    import dataclasses
    from telegram_search_mcp.native_runtime import APPROVED_PINS, NAMES
    return dataclasses.replace(APPROVED_PINS[0], team_ids=frozenset(team_ids), **fields,
        unsigned=tuple((n, len(natives[n]), hashlib.sha256(natives[n]).hexdigest()) for n in NAMES))


def inert_natives(tag=b''):
    from telegram_search_mcp.native_runtime import NAMES
    return {name: (name.encode() + tag + b'-inert-native-bytes-padding-0123456789')[:48].ljust(48, b'.') for name in NAMES}


def make_payload(root, *, manifest_version=1, publication='unreleased', source_completeness='incomplete',
                 publisher=None, natives=None, lock=None, extra_files=None):
    """A synthetic prepared payload that satisfies the assembler's checks (inert bytes, real contract/schema).

    Defaults reproduce the v1 kit; keywords build v2 manifests, other locks (a dict, or exact bytes)
    and extra files.
    """
    import telegram_search_mcp
    from telegram_search_mcp import __version__
    from telegram_search_mcp.config import RuntimePolicy
    from telegram_search_mcp.contract import CONTRACT_VERSION, schema_document, schema_fingerprint
    from telegram_search_mcp.server import build_server
    payload = root / 'payload'
    files = {'python/bin/python3': b'inert-runtime', 'launcher.py': b'# inert\n',
             'installer/tomlkit/__init__.py': b'', 'notices/LICENSE': b'license', 'notices/NOTICE': b'notice',
             'provenance/source-completeness.json': b'{}',
             'skills/telegram-search/SKILL.md': b'search-skill', 'skills/telegram-setup/SKILL.md': b'setup-skill',
             'app/segno/__init__.py': b'# inert stand-in for the pinned QR encoder\n'}
    # The assembler requires the checkout's exact package: every .py file, byte for byte.
    package = Path(telegram_search_mcp.__file__).parent
    files.update({'app/telegram_search_mcp/' + str(path.relative_to(package)): path.read_bytes()
                  for path in sorted(package.rglob('*.py')) if '__pycache__' not in path.relative_to(package).parts})
    natives = natives or inert_natives()
    files.update({'native/' + name: data for name, data in natives.items()})
    lock_bytes = lock if type(lock) is bytes else json.dumps(lock or v1_lock(natives)).encode()
    files['tdlib-lock.json'] = lock_bytes
    manifest = dict(schema_version=manifest_version, package_version=__version__, target='macos-arm64',
        minimum_os='26.0', contract_version=CONTRACT_VERSION, schema_fingerprint=schema_fingerprint(),
        tools=[t['name'] for t in schema_document(build_server(policy=RuntimePolicy()))['tools']],
        native_lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        skills=[dict(id=n, sha256=hashlib.sha256(files['skills/' + n + '/SKILL.md']).hexdigest())
                for n in ('telegram-search', 'telegram-setup')],
        source_completeness=source_completeness, publication=publication)
    if manifest_version == 2:
        manifest['publisher'] = publisher or {'kind': 'adhoc', 'team_id': None}
    files['manifest/release.json'] = json.dumps(manifest).encode()
    files.update(extra_files or {})
    payload.mkdir(mode=0o700)
    for name, data in files.items():
        path = payload / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o700 if name == 'python/bin/python3' else 0o600)
    return payload


def load_assembler(name='assembly_under_test'):
    import importlib.util
    import sys
    path = Path(__file__).parents[1] / 'packaging/assemble_bundle.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def keychain_runner(api_id, api_hash):
    """A fake `security find-generic-password` over synthetic values only."""
    import subprocess
    values = {'api_id': str(api_id), 'api_hash': api_hash}
    return lambda args, **kwargs: subprocess.CompletedProcess(args, 0, values[args[-1]] + '\n', '')
