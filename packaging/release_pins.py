"""The one release-pins.json schema (CLIENTS-12, CLIENTS-14).

Development host only; stdlib only. release_build writes the kit's release-pins.json with `encode`, and
that file is copied byte for byte into plugins/<plugin>/release-pins.json in the release commit, where
the plugin's bin/install reads it; Install.command's embedded pins are rendered from the same bytes.
`parse` makes exactly the checks bin/install makes before it downloads anything: the closed key set in
canonical bytes (sorted keys, two-space indent, one newline), schema 1, the publication vocabulary of the
release manifest, the version grammar, an empty or ten-character Team ID, lowercase sha256 values and
decimal sizes, and for published pins a Team ID and sizes within each asset's bound. Refusals are one
fixed code.
"""
from __future__ import annotations

import json
import re

# (pin name, release asset, size bound when published), in bin/install's download order.
ASSETS = (('bootstrap', 'bootstrap.sh', 262144), ('seed', 'bootstrap-verifier', 2097152),
          ('index', 'release-index.json', 2097152), ('archive', 'release.tar.gz', 1073741824))
# The release manifest's vocabulary (bundle_runtime.PUBLICATIONS); bin/install accepts exactly these.
PUBLICATIONS = ('unreleased', 'published')
KEYS = frozenset({'schema_version', 'version', 'team_id', 'publication'}
                 | {f'{name}_{field}' for name, _, _ in ASSETS for field in ('sha256', 'bytes')})
MAX_BYTES = 4096
HASH = re.compile(r'[0-9a-f]{64}')
TEAM = re.compile(r'[0-9A-Z]{10}')
# The one release version grammar (docs/releasing.md, Versions): X.Y.Z, optionally aN, bN or rcN, optionally .devN.
VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})'
                     r'(?:(?:a|b|rc)(0|[1-9][0-9]{0,5}))?(?:\.dev(0|[1-9][0-9]{0,5}))?')


class PinsError(ValueError):
    """The one fixed refusal: the pins do not have the schema."""

    def __init__(self):
        super().__init__('pins_invalid')


def _need(condition):
    if not condition:
        raise PinsError()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def _number(value):
    return type(value) is int and 0 <= value and len(str(value)) <= 12


def parse(data):
    """The pins value of release-pins.json bytes, under bin/install's checks."""
    _need(type(data) is bytes and len(data) <= MAX_BYTES)
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError):
        raise PinsError() from None
    _need(type(value) is dict and set(value) == KEYS and canonical(value) == data)
    version, team = value['version'], value['team_id']
    _need(type(value['schema_version']) is int and value['schema_version'] == 1
          and value['publication'] in PUBLICATIONS
          and type(version) is str and VERSION.fullmatch(version) is not None
          and type(team) is str and (team == '' or TEAM.fullmatch(team) is not None))
    for name, _, bound in ASSETS:
        digest, size = value[f'{name}_sha256'], value[f'{name}_bytes']
        _need(type(digest) is str and HASH.fullmatch(digest) is not None and _number(size))
        if value['publication'] == 'published':
            _need(1 <= size <= bound)
    if value['publication'] == 'published':
        _need(team != '')
    return value


def encode(*, version, publication, team_id, assets):
    """Canonical release-pins.json bytes; `assets` maps each pin name to its (sha256, bytes)."""
    _need(type(assets) is dict and set(assets) == {name for name, _, _ in ASSETS})
    value = {'schema_version': 1, 'version': version, 'publication': publication, 'team_id': team_id}
    for name, (digest, size) in assets.items():
        value.update({f'{name}_sha256': digest, f'{name}_bytes': size})
    data = canonical(value)
    parse(data)
    return data
