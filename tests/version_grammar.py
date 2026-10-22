"""The one release version grammar (docs/releasing.md, Versions), as one shared table.

A version is X.Y.Z, optionally followed by aN, bN or rcN, optionally followed by .devN. Every number is 0 or
starts with a nonzero digit, and has at most six digits. A release tag is v<version> for a release or an rcN
candidate only (no aN, bN or .devN). Every site that admits a version (the native seed, bin/install,
release-pins.json, the archive index, the installer's order, the launcher generator, the gate's project version
and the gate and build tags) is held to this table.
"""

GOOD = ('0.0.0', '0.53.0', '10.0.12', '999999.999999.999999', '0.53.0a1', '0.53.0b2', '0.53.0rc1', '0.53.0rc10',
        '0.53.0a0', '0.53.0rc0', '0.53.0.dev0', '0.53.0.dev12', '0.53.0rc1.dev2', '1.2.3b0.dev0',
        '999999.999999.999999rc999999.dev999999')

BAD = ('', '0', '0.53', '1.0', '0.53.0.0', '0.53.0rc', '0.53.0RC1', '0.53.0-rc1', '0.53.0c1', '0.53.0pre1',
       '0.53.0rc01', '0.53.0a01', '00.53.0', '0.053.0', '0.53.00', '0.53.0.post1', '0.53.0rc1.post1', '0.53.0.dev',
       '0.53.0.dev01', '0.53.0dev1', '0.53.0.devrc', '0.53.0vvv', '0rc1', '0.dev', '0.53.0a1b1', '0.53.0rc1rc1',
       '0.53.0.dev1.dev1', '0.53.0.dev1rc1', ' 0.53.0', '0.53.0 ', '0.53.0\n', 'v0.53.0', '0.53.0+local',
       '0.53..0', '0.53.0.', '.0.53.0', '1234567.0.0', '0.1234567.0', '0.53.0rc1234567', '0.53.0.dev1234567',
       '\u0660.53.0', '0.53.0/x', '../x')


def tag_admitted(version):
    """Whether v<version> is a release tag: a release or rcN version of the table (no aN, bN or .devN)."""
    return version in GOOD and '.dev' not in version and 'a' not in version and 'b' not in version
