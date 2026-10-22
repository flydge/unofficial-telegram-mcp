"""Kill sweep (CLIENTS-13): a hard kill (a forked child, `os._exit`) right after every filesystem create,
rename, unlink, mkdir and rmdir and after every launchctl or claude CLI call of an upgrade, a rollback, an
uninstall, `--adopt-legacy` on upgrade and on first install, an uninstall that keeps adoption backups, an
upgrade whose Codex write is planned again over a neighbour added while the release is copied, and a late
refusal whose journaled restore runs after the broker stop: our Codex entry (or the adopted one) changed while
the release is copied, for an upgrade and an adopting first install, and `receipt.json` changed after the
`runtime.toml` version line was rewritten, which the restore puts back first, or our Codex table changed right
after its write. After a late refusal (0.53 critic round 4) also the successful repeat of an upgrade, a rollback
and an adopting first install, the repeat after nine refusals, and an uninstall over a parked upgrade or
rollback. The parent then resumes the same action. A late refusal's resume refuses with the same code and leaves
the same restored state; where a scenario names a `final` check, the resumed state also passes it (one release
named by `current.json`, the `runtime.toml` pin, the plist and the loaded broker; or nothing left).

The resumed final state must equal the uninterrupted run's: a normalised tree of the whole synthetic base, every
publication binding resolved to the path that now holds that inode. Nothing may be left anywhere (no staging
directory, unbound packet, `.telegram-search-*.tmp` or `.runtime-*` leftover), a resume never refuses, and a
kill never costs more than one attempt of a recovery bound. The single accepted difference is the documented
Claude Code write-ahead window: a kill between the journaled `claude_remove|claude_add` record and the CLI call
is never retried and ends `failed` with the manual command. A retry after the last journal write reports the
finished action (`installation` reused, or absent after an uninstall) without repeating it.

The kill points are deterministic (the uninterrupted run is traced once per scenario) and are split across
forked workers to bound the runtime. Disposable homes and fakes only; nothing real is read or written.
"""
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from test_adopt_legacy import AdoptionHarness
from test_bundle_upgrade import NEIGHBOUR_CODEX
from telegram_search_mcp import bundle_install

FOREIGN_CODEX = NEIGHBOUR_CODEX + b'\n[mcp_servers.telegram_search]\ncommand = "/legacy/051"\n'
WORKERS = max(2, min(4, (os.cpu_count() or 2) // 2))  # More workers only contend for the same ancestor directories.
# What a killed run may leave but its resume must remove. `.runtime-adopted-*` (kept backups) is not one of them.
ORPHAN = re.compile(r'\A(?:\.telegram-search-|\.change_[0-9]+-staging-|\.swap-|\.claude\.json\.tmp'
                    r'|\..*-(?:initial|removed)-|\..*-intent-.*\.tmp\Z)')
PACKET = re.compile(r'(?:failed-release|skill-packet-[a-z][a-z-]*|metadata-packet-[a-z][a-z_]*)-[0-9]+\Z')
RANDOM = [(re.compile(pattern), replacement) for pattern, replacement in (
    (r'-adopted-[0-9a-f]{24}', '-adopted-*'), (r'-initial-[a-z0-9_]{8}', '-initial-*'),
    (r'-removed-[0-9a-f]{32}', '-removed-*'), (r'-staging-[a-z0-9_]{8}', '-staging-*'),
    (r'\.telegram-search-[0-9a-f]{32}\.tmp', '.telegram-search-*.tmp'))]
WRITE_AHEAD = {'claude_remove': 'remove', 'claude_add': 'add-json'}
CLAUDE_DOCUMENT = 'user/.claude.json'


class Effects:
    """Counts every durable effect of the code under test and calls `hook(count, kind, where)` right after it.

    Effects: a file creation (`os.open` with O_CREAT), `os.replace`, `os.rename`, `os.unlink`, `os.mkdir`,
    `os.rmdir`, the installer's `_renameatx` and every launchctl or claude CLI call. A fake's own bookkeeping
    inside a CLI call is part of that call, not an effect of its own.
    """

    def __init__(self, base, hook):
        self.base, self.hook, self.count, self.inside = str(base), hook, 0, False

    def tick(self, kind, where):
        if not self.inside:
            self.count += 1
            self.hook(self.count, kind, where)

    def where(self, path, dir_fd=None):
        path = os.fsdecode(path)
        if dir_fd is not None:
            if hasattr(fcntl, 'F_GETPATH'):
                directory = fcntl.fcntl(dir_fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0].decode()
            else:
                directory = os.readlink('/proc/self/fd/%d' % dir_fd)
            path = os.path.join(directory, path)
        return os.path.relpath(path, self.base) if path.startswith(self.base + '/') else path

    @contextlib.contextmanager
    def patched(self):
        real = {name: getattr(os, name) for name in ('open', 'replace', 'rename', 'unlink', 'mkdir', 'rmdir')}
        renameatx = bundle_install._renameatx

        def opened(path, flags, *args, **kwargs):
            descriptor = real['open'](path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                self.tick('create', self.where(path, kwargs.get('dir_fd')))
            return descriptor

        def effect(name, position, directory):
            def call(*args, **kwargs):
                value = real[name](*args, **kwargs)
                self.tick(name, self.where(args[position], kwargs.get(directory)))
                return value
            return call

        def renamed(source, destination, flags):
            renameatx(source, destination, flags)
            self.tick('renameatx', self.where(destination))

        with mock.patch.object(os, 'open', opened), mock.patch.object(os, 'replace', effect('replace', 1, 'dst_dir_fd')), \
                mock.patch.object(os, 'rename', effect('rename', 1, 'dst_dir_fd')), \
                mock.patch.object(os, 'unlink', effect('unlink', 0, 'dir_fd')), \
                mock.patch.object(os, 'mkdir', effect('mkdir', 0, 'dir_fd')), \
                mock.patch.object(os, 'rmdir', effect('rmdir', 0, 'dir_fd')), \
                mock.patch.object(bundle_install, '_renameatx', renamed):
            yield

    def runner(self, real, kind):
        def call(argv, **kwargs):
            self.inside = True
            try:
                value = real(argv, **kwargs)
            finally:
                self.inside = False
            self.tick(kind, argv[1] if kind == 'launchctl' else argv[2])
            return value
        return call


# --- normalised state ---------------------------------------------------------------------------------------

def text(value, prefix):
    value = value.replace(prefix, '<base>')
    for pattern, replacement in RANDOM:
        value = pattern.sub(replacement, value)
    return value


def normal(value, prefix, inodes):
    """JSON with the synthetic base and random names replaced and each binding's inode resolved to its path."""
    if type(value) is dict:
        if 'dev' in value and 'ino' in value:
            value = {**{k: v for k, v in value.items() if k not in ('dev', 'ino')},
                     'at': inodes.get((value['dev'], value['ino']), 'missing')}
        return {k: normal(v, prefix, inodes) for k, v in value.items()}
    if type(value) is list:
        return [normal(v, prefix, inodes) for v in value]
    return text(value, prefix) if type(value) is str else value


def content(path, prefix, inodes):
    data = path.read_bytes()
    if path.name == 'launchd-state.json':
        return normal(json.loads(data)['loaded'], prefix, inodes)  # The call log differs by design; the job does not.
    if path.name == 'journal.json':
        # Attempt counts and temporary names differ after a kill; the identity, stage and record names do not.
        value = json.loads(data)
        return {**normal({k: v for k, v in value.items() if k != 'publications'}, prefix, inodes),
                'publications': sorted(value['publications'])}
    if path.name.endswith('.json') or path.name in ('receipt.previous', 'receipt.target'):
        try:
            return normal(json.loads(data), prefix, inodes)
        except ValueError:
            pass
    return hashlib.sha256(data.replace(prefix.encode(), b'<base>')).hexdigest()


def walk(base):
    for directory, dirs, files in os.walk(base):
        dirs.sort()
        for name in dirs + sorted(files):
            yield Path(directory) / name


def snapshot(base):
    prefix, found, inodes = str(base), {}, {}
    for path in walk(base):
        metadata = path.lstat()
        relative = text(str(path.relative_to(base)), prefix)
        inodes[(metadata.st_dev, metadata.st_ino)] = relative
        found[relative] = (path, metadata)
    state = {}
    for relative, (path, metadata) in found.items():
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            state[relative] = ['link', text(os.readlink(path), prefix)]
        elif stat.S_ISDIR(metadata.st_mode):
            state[relative] = ['dir', mode]
        else:
            state[relative] = ['file', mode, content(path, prefix, inodes)]
    return state


def attempts(base):
    """{journal: {record: attempt count}} of every journal under the base."""
    out = {}
    for path in walk(base):
        if path.name == 'journal.json':
            value = json.loads(path.read_bytes())
            out[text(str(path.relative_to(base)), str(base))] = {k: len(v) for k, v in value['publications'].items()}
    return out


def orphans(base):
    """Every leftover a resume must remove: temporaries, staging and staged roots, retired roots, unbound packets."""
    left = [str(path.relative_to(base)) for path in walk(base) if ORPHAN.search(path.name)]
    for journal in Path(base).glob('**/transactions/*/journal.json'):
        staged = [Path(a['staged']) for records in json.loads(journal.read_bytes())['publications'].values()
                  for a in records if type(a) is dict and type(a.get('staged')) is str]
        for packet in journal.parent.iterdir():
            if PACKET.fullmatch(packet.name) and not any(packet in (s, s.parent) for s in staged):
                left.append(str(packet.relative_to(base)))
    return sorted(left)


def differences(expected, actual, limit=4):
    keys = sorted(set(expected) | set(actual))
    changed = [k for k in keys if expected.get(k, '<absent>') != actual.get(k, '<absent>')]
    return ['%s: %s -> %s' % (k, str(expected.get(k, '<absent>'))[:160], str(actual.get(k, '<absent>'))[:160])
            for k in changed[:limit]]


# --- scenarios ---------------------------------------------------------------------------------------------

EFFECTS = (frozenset({'create', 'replace', 'renameatx', 'launchctl'}), 50)
# A repeat that only finishes a restore publishes no packet (its release is already copied) and has fewer effects.
RESTORE_EFFECTS = (frozenset({'create', 'replace', 'launchctl'}), 10)


class Scenario:
    """`final(h)`, when given, lists what is wrong with the state after the uninterrupted run and after every
    resume (beyond the comparison with the uninterrupted run's state). `effects`: the effect kinds the
    uninterrupted run must include and the number of effects it must exceed."""

    def __init__(self, prepare, action, check, write_ahead=True, final=None, effects=EFFECTS):
        self.prepare, self.action, self.check, self.write_ahead, self.final = prepare, action, check, write_ahead, final
        self.effects = effects


def _upgrade_prepare(h):
    h.full()
    h.new = h.build('b', launcher=b'# second release\n', search=b'search-skill-2')
    h.claude()


def _upgrade_check(h, result):
    new_root = h.runtime / 'releases' / h.new[3]
    h.assertEqual((result['status'], result['claude_code'], result['claude_desktop'], result['service']),
                  ('upgraded', 'registered', 'registered', 'restarted'))
    h.assertEqual(h.pointer()['release_root'], str(new_root))
    h.assertEqual(h.launchctl.loaded[4], str(new_root / 'launcher.py'))


def _rollback_prepare(h):
    _, h.previous = h.full()
    h.install(h.build('b', launcher=b'# second release\n', search=b'search-skill-2'))
    h.claude()


def _rollback_check(h, result):
    h.assertEqual((result['status'], result['claude_code'], result['service']), ('rolled_back', 'registered', 'restarted'))
    h.assertEqual(h.pointer()['release_root'], h.previous['release_root'])
    h.assertEqual(h.launchctl.loaded[4], str(Path(h.previous['release_root']) / 'launcher.py'))


def _uninstall_prepare(h):
    h.full()
    h.claude()


def _uninstall_check(h, result):
    h.assertEqual(result['status'], 'uninstalled')
    h.assertFalse(h.runtime.exists() or h.plist.exists())
    h.assertNotIn('telegram_search', h.claude().document().get('mcpServers', {}))
    h.assertNotIn('telegram_search', json.loads(h.desktop_config.read_text())['mcpServers'])
    h.assertIsNone(h.launchctl.loaded)


def _adopt_upgrade_prepare(h):
    h.full()
    h.as_v1()
    h.codex_present(FOREIGN_CODEX)
    h.new = h.build('b', launcher=b'# second release\n')
    h.claude()


def _adopt_upgrade_check(h, result):
    h.assertEqual((result['status'], result.get('adopted')), ('upgraded', ['codex']))
    side = json.loads((h.runtime / 'adopted.json').read_text())
    h.assertEqual([(b['client'], b['transaction']) for b in side['backups']], [('codex', 'change_1')])
    h.change('rollback')
    removed = h.change('uninstall')
    h.assertTrue((Path(removed['legacy_backups']) / 'change_1-codex').is_file())


def _adopt_first_prepare(h):
    h.backups = h.legacy()
    h.packet = h.build('a')


def _adopt_first_check(h, result):
    h.assertEqual((result['status'], result.get('adopted'), result.get('service')),
                  ('activation_required', sorted(h.backups), 'restarted'))
    side = json.loads((h.runtime / 'adopted.json').read_text())
    h.assertEqual(sorted(b['client'] for b in side['backups']), sorted(h.backups))


def _kept_prepare(h):
    h.backups = h.legacy()
    h.install(h.build('a'), adopt_legacy=True)
    h.claude()


def _kept_check(h, result):
    h.assertFalse(h.runtime.exists())
    found = sorted(p.name.split('-', 1)[1] for d in h.home.glob('.runtime-adopted-*') for p in d.iterdir())
    h.assertEqual(found, sorted(h.backups))


def _at_copy(h, edit):
    """A checkpoint that edits the Codex config by hand once the release is copied; `edit` returns the bytes
    unchanged once its edit is there, so a resume finds it done."""
    config = h.codex / 'config.toml'

    def checkpoint(stage):
        if stage == 'release':
            data = config.read_bytes()
            if edit(data) != data:
                config.write_bytes(edit(data))
    return checkpoint


def _refusal(h, action):
    """The action's result, or its refusal with the Claude Code outcome its open change journal recorded (a
    refusal prints no client statuses; a kill in a write-ahead window still ends `failed` there)."""
    try:
        return action()
    except bundle_install.InstallError as error:
        result = {'refused': str(error), 'pending_action': error.pending_action}
        changes = sorted(h.runtime.glob('transactions/change_*/journal.json'), key=lambda p: int(p.parent.name[7:]))
        if changes:
            outcome = json.loads(changes[-1].read_bytes())['publications'].get('claude_code')
            if outcome:
                result['claude_code'] = outcome[0]['status']
                if result['claude_code'] == 'failed':
                    result['claude_registration_command'] = 'recorded'
        return result


OURS, CHANGED = b'tool_timeout_sec = 600', b'tool_timeout_sec = 601'
LEGACY, LEGACY_CHANGED = b'"telegram_search_mcp.server"]', b'"telegram_search_mcp.server", "--changed"]'
WEATHER = b'\n[mcp_servers.weather]\ncommand = "/opt/w"\n'


def _merged_check(h, result):
    _upgrade_check(h, result)
    data = (h.codex / 'config.toml').read_bytes()
    h.assertTrue(data.startswith(NEIGHBOUR_CODEX) and data.endswith(WEATHER))


def _late_upgrade_prepare(h):
    _upgrade_prepare(h)
    h.served = (h.plist.read_bytes(), h.launchctl.loaded)
    h.target = Path(h.pointer()['release_root'])  # A late refusal leaves the installed release serving.


def _late_upgrade_check(h, result):
    h.assertEqual(result, {'refused': 'target_conflict', 'pending_action': None})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)  # The old release keeps serving.
    h.assertEqual(h.launchctl.verbs()[-2:], ['print', 'bootstrap'])
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(CHANGED, OURS))
    _upgrade_check(h, h.install(h.new))


def _late_policy_prepare(h):
    _late_upgrade_prepare(h)
    h.receipt_bytes = (h.runtime / 'receipt.json').read_bytes()
    h.pin = (h.session_root / 'runtime.toml').read_bytes()


def _receipt_changed(h):
    """At the policy checkpoint, once: receipt.json changed by hand, so the receipt step refuses after the pin."""
    receipt = h.runtime / 'receipt.json'

    def checkpoint(stage):
        if stage == 'policy' and receipt.read_bytes() == h.receipt_bytes:
            receipt.write_bytes(h.receipt_bytes + b' ')
    return checkpoint


def _late_policy_check(h, result):
    h.assertEqual(result, {'refused': 'metadata_conflict', 'pending_action': None, 'claude_code': 'registered'})
    h.assertEqual(((h.session_root / 'runtime.toml').read_bytes(), h.plist.read_bytes(), h.launchctl.loaded),
                  (h.pin,) + h.served)
    (h.runtime / 'receipt.json').write_bytes(h.receipt_bytes)
    _upgrade_check(h, h.install(h.new))


def _late_adopt_prepare(h):
    _adopt_first_prepare(h)
    h.served = (h.plist.read_bytes(), h.launchctl.loaded)


def _late_adopt_check(h, result):
    h.assertEqual(result, {'refused': 'foreign_registration', 'pending_action': None})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)
    h.assertEqual(h.plist.read_bytes(), h.backups['service'])
    h.assertFalse((h.runtime / 'current.json').exists())
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(LEGACY_CHANGED, LEGACY))
    _adopt_first_check(h, h.install(h.packet, adopt_legacy=True))


def _one_release(h):
    """current.json, the runtime.toml pin, the plist and the loaded broker all name `h.target` (0.53 critic round
    4: a kill in a repeat's stop left the old broker on the new pin)."""
    launcher = str(h.target / 'launcher.py')
    version = json.loads((h.target / 'manifest/release.json').read_text())['package_version']
    loaded = h.launchctl.loaded
    named = {'current.json': json.loads((h.runtime / 'current.json').read_text())['release_root'] == str(h.target),
             'pin': ('expected_package_version = "%s"' % version).encode() in (h.session_root / 'runtime.toml').read_bytes(),
             'plist': h.plist.exists() and plistlib.loads(h.plist.read_bytes())['ProgramArguments'][4] == launcher,
             'loaded broker': loaded is not None and loaded[4] == launcher}
    return ['%s does not name %s' % (k, h.target.name) for k, ok in named.items() if not ok]


def _nothing_left(h):
    """After an uninstall: no root, no plist, no loaded broker and no telegram_search entry in any client."""
    left = {'root': h.runtime.exists(), 'plist': h.plist.exists(), 'loaded broker': h.launchctl.loaded is not None,
            'codex entry': b'telegram_search' in (h.codex / 'config.toml').read_bytes(),
            'claude code entry': 'telegram_search' in h.claude().document().get('mcpServers', {}),
            'claude desktop entry': 'telegram_search' in json.loads(h.desktop_config.read_text())['mcpServers'],
            'codex neighbours': not (h.codex / 'config.toml').read_bytes().startswith(NEIGHBOUR_CODEX)}
    return ['%s left' % k for k, bad in left.items() if bad]


def _late_codex(h, action, ours, changed, code):
    """A late refusal from our Codex table changed during the copy, then the table put back: the change is parked."""
    h.assertEqual(_refusal(h, lambda: action(checkpoint=_at_copy(h, lambda d: d.replace(ours, changed))))['refused'], code)
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(changed, ours))


def _repeat_upgrade_prepare(h):
    _upgrade_prepare(h)
    h.target = h.runtime / 'releases' / h.new[3]
    _late_codex(h, lambda **kwargs: h.install(h.new, **kwargs), OURS, CHANGED, 'target_conflict')


def _repeat_rollback_prepare(h):
    _rollback_prepare(h)
    h.target = Path(h.previous['release_root'])
    _late_codex(h, lambda **kwargs: h.change('rollback', **kwargs), OURS, CHANGED, 'target_conflict')


def _repeat_adopt_prepare(h):
    _adopt_first_prepare(h)
    h.target = h.runtime / 'releases' / h.packet[3]
    _late_codex(h, lambda **kwargs: h.install(h.packet, adopt_legacy=True, **kwargs), LEGACY, LEGACY_CHANGED,
                'foreign_registration')


def _skill_edited(h):
    """At the copy, once: our search skill copy edited by hand, so the change refuses after its Codex write."""
    skill = h.skills / 'telegram-search' / 'SKILL.md'

    def checkpoint(stage):
        if stage == 'release' and skill.read_bytes() != b'mine':
            skill.chmod(0o600)
            skill.write_bytes(b'mine')
    return checkpoint


def _many_refusals_prepare(h):
    """Nine `skill_conflict` refusals after the Codex write (more than the eight cycles 0.53 round 3 allowed),
    then the edited copy removed: the repeat publishes its own."""
    _upgrade_prepare(h)
    h.target = h.runtime / 'releases' / h.new[3]
    for _ in range(9):
        h.assertEqual(_refusal(h, lambda: h.install(h.new, checkpoint=_skill_edited(h)))['refused'], 'skill_conflict')
    skill = h.skills / 'telegram-search'
    (skill / 'SKILL.md').unlink()
    skill.rmdir()


def _parked_after_receipt(h, action):
    """Parked after every client names the target: receipt.json changed at the policy step, then put back."""
    receipt = h.runtime / 'receipt.json'
    saved = receipt.read_bytes()

    def checkpoint(stage):
        if stage == 'policy' and receipt.read_bytes() == saved:
            receipt.write_bytes(saved + b' ')
    h.assertEqual(_refusal(h, lambda: action(checkpoint=checkpoint))['refused'], 'metadata_conflict')
    receipt.write_bytes(saved)


def _uninstall_parked_upgrade_prepare(h):
    _upgrade_prepare(h)
    _parked_after_receipt(h, lambda **kwargs: h.install(h.new, **kwargs))


def _uninstall_parked_rollback_prepare(h):
    _rollback_prepare(h)
    _parked_after_receipt(h, lambda **kwargs: h.change('rollback', **kwargs))


def _uninstall_parked_check(h, result):
    h.assertEqual((result['status'], result['service'], result['codex'], result['claude_code'], result['claude_desktop'],
                   sorted(set(result['skills'].values()))), ('uninstalled', 'removed', 'removed', 'removed', 'removed', ['removed']))
    h.assertEqual(_nothing_left(h), [])


def _after_codex_write(h):
    """Our Codex table changed right after this transaction wrote it (`published:registration`); a resume killed
    between the write and that checkpoint makes the same edit once our written table is found in place."""
    config, ours = h.codex / 'config.toml', str(h.runtime / 'releases' / h.new[3]).encode()

    def checkpoint(stage):
        data = config.read_bytes()
        if stage in ('service', 'published:registration') and ours in data and OURS in data:
            config.write_bytes(data.replace(OURS, CHANGED))
    return checkpoint


def _codex_write_refusal_check(h, result):
    h.assertEqual(result, {'refused': 'target_conflict', 'pending_action': None})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(CHANGED, OURS))
    h.target = h.runtime / 'releases' / h.new[3]
    _upgrade_check(h, h.install(h.new))
    h.assertEqual(_one_release(h), [])


def _die(h, action, at=None, effect=None):
    """Run `action(checkpoint)` in a forked child that hard-exits at checkpoint `at`, or right after the first
    durable effect `effect(kind, where)` selects; launchd's fake state outlives it."""
    pid = os.fork()
    if pid == 0:
        try:
            def checkpoint(value):
                if value == at:
                    os._exit(9)

            def hook(count, kind, where):
                if effect is not None and effect(kind, where):
                    os._exit(9)
            with Effects(h.base, hook).patched():
                action(checkpoint)
        finally:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    h.assertEqual(os.waitstatus_to_exitcode(status), 9)


def _at_stage(h, stage, ours, changed):
    """A checkpoint that changes our Codex table (or the adopted one) at `stage`, once it is not already changed."""
    config = h.codex / 'config.toml'

    def checkpoint(value):
        if value == stage and ours in config.read_bytes():
            config.write_bytes(config.read_bytes().replace(ours, changed))
    return checkpoint


def _user_skill(h):
    """At the Codex step's end: a `telegram-search` skill directory of the user's where ours goes (completed by a
    resume when a kill came right after its mkdir, which the sweep counts as an effect)."""
    skill = h.skills / 'telegram-search'

    def checkpoint(value):
        if value == 'registration' and not (skill / 'SKILL.md').exists():
            skill.mkdir(mode=0o700, exist_ok=True)
            (skill / 'SKILL.md').write_bytes(b'mine')
    return checkpoint


def _login(h, release_root):
    """launchd at login loads the plist on disk: after a kill past the plist rewrite, the target release's."""
    h.assertIsNone(h.launchctl.loaded)
    h.launchctl.load()
    h.assertEqual(h.launchctl.loaded[4], str(Path(release_root) / 'launcher.py'))


def _login_upgrade_prepare(h):
    """0.53 critic round 4: an upgrade killed after its plist rewrite, then a login starts the new broker."""
    _late_upgrade_prepare(h)
    _die(h, lambda checkpoint: h.install(h.new, checkpoint=checkpoint), at='published:plist')
    _login(h, h.runtime / 'releases' / h.new[3])


def _login_rollback_prepare(h):
    _rollback_prepare(h)
    h.served = (h.plist.read_bytes(), h.launchctl.loaded)
    h.target = Path(h.pointer()['release_root'])
    _die(h, lambda checkpoint: h.change('rollback', checkpoint=checkpoint), at='published:plist')
    _login(h, h.previous['release_root'])


def _login_restore_check(h, result, finish):
    """The late refusal booted the target's broker out and put the previous plist and broker back; once our
    table is put back the repeat (`finish`) ends on one release."""
    h.assertEqual(result, {'refused': 'target_conflict', 'pending_action': None})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)
    h.assertEqual(h.launchctl.verbs()[-2:], ['print', 'bootstrap'])
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(CHANGED, OURS))
    finish()


def _login_upgrade_check(h, result):
    _login_restore_check(h, result, lambda: _upgrade_check(h, h.install(h.new)))


def _login_rollback_check(h, result):
    _login_restore_check(h, result, lambda: _rollback_check(h, h.change('rollback')))


def _login_adopt_prepare(h):
    _late_adopt_prepare(h)
    h.target = None  # No installed release yet: the legacy broker keeps serving.
    _die(h, lambda checkpoint: h.install(h.packet, adopt_legacy=True, checkpoint=checkpoint), at='published:plist')
    _login(h, h.runtime / 'releases' / h.packet[3])


def _login_adopt_check(h, result):
    h.assertEqual(result, {'refused': 'foreign_telegram-search', 'pending_action': None})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)  # The legacy plist and broker, not the new.
    h.assertEqual(h.plist.read_bytes(), h.backups['service'])
    h.assertFalse((h.runtime / 'current.json').exists())
    skill = h.skills / 'telegram-search'
    (skill / 'SKILL.md').unlink()
    skill.rmdir()
    _adopt_first_check(h, h.install(h.packet, adopt_legacy=True))
    h.target = h.runtime / 'releases' / h.packet[3]
    h.assertEqual(_one_release(h), [])


def _legacy_served(h):
    """The legacy plist and broker exactly as before the adopting first install (no release named yet)."""
    plist, loaded = h.served
    return [] if (h.plist.read_bytes(), h.launchctl.loaded) == (plist, loaded) else ['the legacy service is not back']


def _desktop_temporary_prepare(h):
    """0.53 critic round 4: an upgrade killed right after our Claude Desktop temporary is created."""
    _late_upgrade_prepare(h)
    _die(h, lambda checkpoint: h.install(h.new, checkpoint=checkpoint),
         effect=lambda kind, where: kind == 'create' and '/Claude/.telegram-search-' in '/' + where)
    h.assertEqual(len(list(h.desktop_config.parent.glob('.telegram-search-*'))), 1)


def _desktop_temporary_check(h, result):
    """The repeat refused at the Codex step, before the Desktop step: its restore removed the temporary (the sweep's
    leftover check) and put the old service back; uninstall then goes over the parked change."""
    h.assertEqual(result, {'refused': 'target_conflict', 'pending_action': None, 'claude_code': 'registered'})
    h.assertEqual((h.plist.read_bytes(), h.launchctl.loaded), h.served)
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(CHANGED, OURS))
    _uninstall_parked_check(h, h.change('uninstall'))


def _parked_desktop_temporary_prepare(h):
    """Then that repeat refused with a restore that leaves the temporary, as the round-4 build's did: the change
    is parked with it, and only the uninstall can remove it."""
    _desktop_temporary_prepare(h)
    with mock.patch.object(bundle_install, '_client_temporaries'):
        h.assertEqual(_refusal(h, lambda: h.install(h.new, checkpoint=_at_stage(h, 'service', OURS, CHANGED)))['refused'],
                      'target_conflict')
    h.assertEqual(len(list(h.desktop_config.parent.glob('.telegram-search-*'))), 1)
    config = h.codex / 'config.toml'
    config.write_bytes(config.read_bytes().replace(CHANGED, OURS))


def _not_running_prepare(h):
    """0.53 critic round 5: an upgrade that found no broker running, parked by a late refusal; then a login loads
    the plist on disk, the installed release's."""
    h.full(loaded=False)
    h.new = h.build('b', launcher=b'# second release\n', search=b'search-skill-2')
    h.claude()
    h.target = h.runtime / 'releases' / h.new[3]
    _late_codex(h, lambda **kwargs: h.install(h.new, **kwargs), OURS, CHANGED, 'target_conflict')
    _login(h, h.pointer()['release_root'])


def _not_running_check(h, result):
    """The repeat stopped the old broker the login started and, as none ran when the change began, started none."""
    h.assertEqual((result['status'], result['service']), ('upgraded', 'not_loaded'))
    h.launchctl.load()  # The next login.
    h.assertEqual(_one_release(h), [])


def _stopped_on_target(h):
    """current.json, the pin and the plist name `h.target`, and no broker runs (none did when the change began)."""
    out = [p for p in _one_release(h) if not p.startswith('loaded broker')]
    return out + (['a broker is loaded'] if h.launchctl.loaded is not None else [])


SCENARIOS = {
    'upgrade': Scenario(_upgrade_prepare, lambda h, lc, cl: h.install(h.new, launchctl=lc, claude_runner=cl), _upgrade_check),
    'rollback': Scenario(_rollback_prepare, lambda h, lc, cl: h.change('rollback', launchctl=lc, claude_runner=cl),
                         _rollback_check),
    'uninstall': Scenario(_uninstall_prepare, lambda h, lc, cl: h.change('uninstall', launchctl=lc, claude_runner=cl),
                          _uninstall_check, write_ahead=False),
    'adopt_upgrade': Scenario(_adopt_upgrade_prepare,
                              lambda h, lc, cl: h.install(h.new, adopt_legacy=True, launchctl=lc, claude_runner=cl),
                              _adopt_upgrade_check),
    'adopt_first_install': Scenario(_adopt_first_prepare,
                                    lambda h, lc, cl: h.install(h.packet, adopt_legacy=True, launchctl=lc, claude_runner=cl),
                                    _adopt_first_check),
    'uninstall_keeps_backups': Scenario(_kept_prepare,
                                        lambda h, lc, cl: h.change('uninstall', launchctl=lc, claude_runner=cl),
                                        _kept_check, write_ahead=False),
    'merged_upgrade': Scenario(_upgrade_prepare, lambda h, lc, cl: h.install(
        h.new, launchctl=lc, claude_runner=cl, checkpoint=_at_copy(h, lambda d: d if WEATHER in d else d + WEATHER)),
        _merged_check),
    'late_refusal_upgrade': Scenario(_late_upgrade_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.new, launchctl=lc, claude_runner=cl, checkpoint=_at_copy(h, lambda d: d.replace(OURS, CHANGED)))),
        _late_upgrade_check, write_ahead=False, final=_one_release),
    'late_refusal_after_policy': Scenario(_late_policy_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.new, launchctl=lc, claude_runner=cl, checkpoint=_receipt_changed(h))), _late_policy_check, final=_one_release),
    'late_refusal_adopt_first_install': Scenario(_late_adopt_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.packet, adopt_legacy=True, launchctl=lc, claude_runner=cl,
        checkpoint=_at_copy(h, lambda d: d.replace(LEGACY, LEGACY_CHANGED)))), _late_adopt_check, write_ahead=False),
    # 0.53 critic round 4: the successful repeat after a late refusal, killed anywhere (its broker stop included).
    'repeat_after_late_refusal_upgrade': Scenario(_repeat_upgrade_prepare, lambda h, lc, cl: h.install(
        h.new, launchctl=lc, claude_runner=cl), _upgrade_check, final=_one_release),
    'repeat_after_late_refusal_rollback': Scenario(_repeat_rollback_prepare, lambda h, lc, cl: h.change(
        'rollback', launchctl=lc, claude_runner=cl), _rollback_check, final=_one_release),
    'repeat_after_late_refusal_adopt_first_install': Scenario(_repeat_adopt_prepare, lambda h, lc, cl: h.install(
        h.packet, adopt_legacy=True, launchctl=lc, claude_runner=cl), _adopt_first_check, final=_one_release),
    'repeat_after_many_late_refusals': Scenario(_many_refusals_prepare, lambda h, lc, cl: h.install(
        h.new, launchctl=lc, claude_runner=cl), _upgrade_check, final=_one_release),
    'uninstall_over_a_parked_upgrade': Scenario(_uninstall_parked_upgrade_prepare, lambda h, lc, cl: h.change(
        'uninstall', launchctl=lc, claude_runner=cl), _uninstall_parked_check, write_ahead=False, final=_nothing_left),
    'uninstall_over_a_parked_rollback': Scenario(_uninstall_parked_rollback_prepare, lambda h, lc, cl: h.change(
        'uninstall', launchctl=lc, claude_runner=cl), _uninstall_parked_check, write_ahead=False, final=_nothing_left),
    'late_refusal_after_the_codex_write': Scenario(_late_upgrade_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.new, launchctl=lc, claude_runner=cl, checkpoint=_after_codex_write(h))), _codex_write_refusal_check,
        write_ahead=False, final=_one_release),
    # 0.53 critic round 5: a late refusal after a kill past the plist rewrite and a login (the target's broker
    # loaded); a repeat after a login in a change that found no broker running; client temporaries a kill left,
    # removed by a late refusal's restore and by an uninstall over a change parked with one.
    'restore_after_a_login_upgrade': Scenario(_login_upgrade_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.new, launchctl=lc, claude_runner=cl, checkpoint=_at_stage(h, 'service', OURS, CHANGED))), _login_upgrade_check,
        write_ahead=False, final=_one_release, effects=RESTORE_EFFECTS),
    'restore_after_a_login_rollback': Scenario(_login_rollback_prepare, lambda h, lc, cl: _refusal(h, lambda: h.change(
        'rollback', launchctl=lc, claude_runner=cl, checkpoint=_at_stage(h, 'service', OURS, CHANGED))),
        _login_rollback_check, write_ahead=False, final=_one_release, effects=RESTORE_EFFECTS),
    'restore_after_a_login_adopt_first_install': Scenario(_login_adopt_prepare, lambda h, lc, cl: _refusal(h, lambda: h.install(
        h.packet, adopt_legacy=True, launchctl=lc, claude_runner=cl, checkpoint=_user_skill(h))), _login_adopt_check,
        write_ahead=False, final=_legacy_served, effects=RESTORE_EFFECTS),
    'repeat_stops_a_broker_a_login_started_in_a_change_that_found_none': Scenario(
        _not_running_prepare, lambda h, lc, cl: h.install(h.new, launchctl=lc, claude_runner=cl), _not_running_check,
        final=_stopped_on_target),
    'late_refusal_after_a_kill_in_the_desktop_write': Scenario(_desktop_temporary_prepare, lambda h, lc, cl: _refusal(
        h, lambda: h.install(h.new, launchctl=lc, claude_runner=cl, checkpoint=_at_stage(h, 'service', OURS, CHANGED))),
        _desktop_temporary_check, write_ahead=False, final=_one_release, effects=RESTORE_EFFECTS),
    'uninstall_over_a_change_parked_with_a_desktop_temporary': Scenario(
        _parked_desktop_temporary_prepare, lambda h, lc, cl: h.change('uninstall', launchctl=lc, claude_runner=cl),
        _uninstall_parked_check, write_ahead=False, final=_nothing_left),
}


class Reference:
    """The uninterrupted run: its effect trace, normalised result, final state and attempt counts."""

    def __init__(self, trace, result, state, budget):
        self.trace, self.result, self.state, self.budget = trace, result, state, budget


class FixedDirectory:
    """A TemporaryDirectory stand-in at one fixed path. Every run of a worker then sees byte-identical paths,
    so digests over them (ownership receipts, registration hashes) compare equal with its own reference."""

    def __init__(self, path):
        self.name = str(path)
        self.cleanup()
        path.mkdir(mode=0o700)

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)


class KillSweepTests(AdoptionHarness, unittest.TestCase):
    fixed = None

    def setUp(self):
        self._claude_base = None  # A fixed base reuses its path: the fake CLI is rebuilt in each fresh home.
        if self.fixed is None:
            return super().setUp()
        with mock.patch('tempfile.TemporaryDirectory', lambda: FixedDirectory(self.fixed)):
            super().setUp()

    @contextlib.contextmanager
    def fixed_base(self):
        parent = Path(tempfile.mkdtemp(prefix='tgmcp-kill-sweep-')).resolve()
        self.fixed = parent / 'base'
        try:
            yield
        finally:
            self.fixed = None
            shutil.rmtree(parent, ignore_errors=True)

    def write_ahead_records(self):
        counts = dict.fromkeys(WRITE_AHEAD, 0)
        for journal in (self.runtime / 'transactions').glob('*/journal.json') if self.runtime.is_dir() else ():
            publications = json.loads(journal.read_bytes())['publications']
            for name in counts:
                counts[name] += len(publications.get(name, []))
        return counts

    def claude_document(self):
        document = self.claude().document()
        return None if document is None else normal(document, str(self.base), {})

    def run_action(self, scenario, hook):
        effects = Effects(self.base, hook)
        with effects.patched():
            return scenario.action(self, effects.runner(self.launchctl, 'launchctl'), effects.runner(self.claude(), 'claude'))

    def reference(self, scenario):
        """Trace the uninterrupted run; each effect notes whether it falls in a Claude Code write-ahead window."""
        self.setUp()
        scenario.prepare(self)
        trace, calls, baseline = [], dict.fromkeys(WRITE_AHEAD.values(), 0), self.write_ahead_records()

        def hook(count, kind, where):
            if kind == 'claude':
                calls[where] = calls.get(where, 0) + 1
            window = False
            if scenario.write_ahead:
                records = self.write_ahead_records()
                window = any(records[name] - baseline[name] > calls[verb] for name, verb in WRITE_AHEAD.items())
            trace.append((kind, where, window, self.claude_document() if window else None))

        result = self.run_action(scenario, hook)
        reference = Reference(trace, normal(result, str(self.base), {}), snapshot(self.base), attempts(self.base))
        self.assertEqual(orphans(self.base), [])
        if scenario.final is not None:
            self.assertEqual(scenario.final(self), [])
        scenario.check(self, result)
        return reference

    def killed(self, scenario, predicate):
        """Run the action in a forked child that exits right after the first effect `predicate` selects."""
        pid = os.fork()
        if pid == 0:
            code = 3
            try:
                def hook(count, kind, where):
                    if predicate(count, kind, where):
                        os._exit(9)
                self.run_action(scenario, hook)
            except BaseException:
                code = 4
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def problems(self, reference, result, window=None):
        """How the resumed state and result differ from the uninterrupted run's."""
        prefix, out = str(self.base), []
        left = orphans(self.base)
        if left:
            out.append('left %s' % left[:4])
        expected, actual = copy.deepcopy(reference.state), snapshot(self.base)
        expected_result, actual_result = dict(reference.result), normal(result, prefix, {})
        if window is not None:
            # Never retried after its write-ahead record: `failed` with the manual command, the entry as the kill left it.
            if actual_result.get('claude_code') != 'failed' or 'claude_registration_command' not in actual_result:
                out.append('write-ahead window not reported failed: %s' % actual_result.get('claude_code'))
            for value in (expected_result, actual_result):
                value.pop('claude_code', None)
                value.pop('claude_registration_command', None)
            for state in (expected, actual):
                for name, entry in state.items():
                    if name.endswith('/receipt.json') and entry[0] == 'file' and type(entry[2]) is dict:
                        entry[2].pop('claude_code', None)
                    if name.endswith('/journal.json'):  # A `claude_add` after a killed remove is never attempted.
                        entry[2]['publications'] = [n for n in entry[2]['publications'] if n not in WRITE_AHEAD]
            if window == '<absent>':
                expected.pop(CLAUDE_DOCUMENT, None)
            else:
                expected[CLAUDE_DOCUMENT] = expected[CLAUDE_DOCUMENT][:2] + [window]
        out.extend('state %s' % d for d in differences(expected, actual))
        finished = ('reused', 'absent')
        if actual_result.get('installation') in finished and expected_result.get('installation') not in finished:
            # Killed after its last journal write: the retry reports the finished action without repeating it.
            wrong = {k: v for k, v in actual_result.items() if k != 'installation' and expected_result.get(k) != v}
            if actual_result.get('status') != expected_result.get('status') or wrong:
                out.append('finished result %s' % sorted(wrong))
        elif actual_result != expected_result:
            out.extend('result %s' % d for d in differences(expected_result, actual_result))
        # One kill costs at most one attempt of any bounded record.
        for journal, counts in attempts(self.base).items():
            for name, count in counts.items():
                if count > reference.budget.get(journal, {}).get(name, 0) + 1:
                    out.append('%s %s used %d attempts' % (journal, name, count))
        return out

    def kill_points(self, scenario, trace, reference, points):
        """Kill after each effect `points` names, resume, and compare with `reference` (same fixed base)."""
        found = []
        for n in points:
            self.setUp()
            try:
                scenario.prepare(self)
                kind, where, window, document = trace[n - 1]
                at = '%d (after %s %s)' % (n, kind, where)
                code = self.killed(scenario, lambda count, *_: count == n)
                if code != 9:
                    found.append('%s: the killed run exited %d' % (at, code))
                    continue
                try:
                    result = scenario.action(self, self.launchctl, self.claude())
                except Exception as error:
                    found.append('%s: the resume refused %r' % (at, error))
                    continue
                window = ('<absent>' if document is None else document) if window else None
                found.extend('%s: %s' % (at, p) for p in self.problems(reference, result, window))
                if scenario.final is not None:
                    found.extend('%s: %s' % (at, p) for p in scenario.final(self))
            except Exception as error:
                found.append('%d: the sweep raised %r' % (n, error))
            finally:
                self.temp.cleanup()
        return found

    def sweep(self, name):
        """Trace the uninterrupted run once, then let forked workers kill after every effect of it in turn."""
        scenario = SCENARIOS[name]
        trace = self.reference(scenario).trace
        total = len(trace)
        kinds = {kind for kind, *_ in trace}
        required, minimum = scenario.effects
        self.assertTrue(required <= kinds, kinds)
        if scenario.write_ahead:
            self.assertTrue('claude' in kinds and any(window for _, _, window, _ in trace))
        self.assertGreater(total, minimum)
        workers = []
        for index in range(WORKERS):
            read, write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read)
                try:
                    with self.fixed_base():
                        reference = self.reference(scenario)  # This worker's own uninterrupted run at its fixed base.
                        if [(k, x) for k, _, x, _ in reference.trace] != [(k, x) for k, _, x, _ in trace]:
                            found = ['0: the uninterrupted run is not deterministic']
                        else:
                            found = self.kill_points(scenario, trace, reference, range(index + 1, total + 1, WORKERS))
                except BaseException as error:
                    found = ['0: worker raised %r' % error]
                try:
                    with os.fdopen(write, 'wb') as stream:
                        stream.write(json.dumps(found).encode())
                finally:
                    os._exit(0)
            os.close(write)
            workers.append((pid, read))
        found = []
        for pid, read in workers:
            with os.fdopen(read, 'rb') as stream:
                data = stream.read()
            _, status = os.waitpid(pid, 0)
            if os.waitstatus_to_exitcode(status) != 0 or not data:
                found.append('a worker failed')
                continue
            found.extend(json.loads(data))
        self.assertEqual(sorted(found, key=lambda p: int(p.split(' ', 1)[0].split(':')[0])), [], '%s: %d kill points' % (name, total))

    def test_upgrade(self):
        self.sweep('upgrade')

    def test_rollback(self):
        self.sweep('rollback')

    def test_uninstall(self):
        self.sweep('uninstall')

    def test_adopt_on_upgrade(self):
        self.sweep('adopt_upgrade')

    def test_adopt_on_first_install(self):
        self.sweep('adopt_first_install')

    def test_uninstall_keeps_adoption_backups(self):
        self.sweep('uninstall_keeps_backups')

    def test_upgrade_merging_a_codex_neighbour_added_during_the_copy(self):
        self.sweep('merged_upgrade')

    def test_late_refusal_restores_the_service_of_an_upgrade(self):
        self.sweep('late_refusal_upgrade')

    def test_late_refusal_after_the_version_line_restores_it_and_the_service(self):
        self.sweep('late_refusal_after_policy')

    def test_late_refusal_restores_the_adopted_broker_of_a_first_install(self):
        self.sweep('late_refusal_adopt_first_install')

    def test_repeat_after_a_late_refusal_of_an_upgrade_ends_on_one_release(self):
        self.sweep('repeat_after_late_refusal_upgrade')

    def test_repeat_after_a_late_refusal_of_a_rollback_ends_on_one_release(self):
        self.sweep('repeat_after_late_refusal_rollback')

    def test_repeat_after_a_late_refusal_of_an_adopting_first_install_ends_on_one_release(self):
        self.sweep('repeat_after_late_refusal_adopt_first_install')

    def test_repeat_after_more_late_refusals_than_any_bound_finishes(self):
        self.sweep('repeat_after_many_late_refusals')

    def test_uninstall_over_a_parked_upgrade_removes_what_it_wrote(self):
        self.sweep('uninstall_over_a_parked_upgrade')

    def test_uninstall_over_a_parked_rollback_removes_what_it_wrote(self):
        self.sweep('uninstall_over_a_parked_rollback')

    def test_late_refusal_after_the_codex_write_refuses_with_its_documented_code(self):
        self.sweep('late_refusal_after_the_codex_write')

    def test_restore_after_a_login_boots_the_new_broker_out_for_an_upgrade(self):
        self.sweep('restore_after_a_login_upgrade')

    def test_restore_after_a_login_boots_the_new_broker_out_for_a_rollback(self):
        self.sweep('restore_after_a_login_rollback')

    def test_restore_after_a_login_puts_the_legacy_broker_back_for_an_adopting_first_install(self):
        self.sweep('restore_after_a_login_adopt_first_install')

    def test_repeat_stops_an_old_broker_a_login_started_in_a_change_that_found_none_running(self):
        self.sweep('repeat_stops_a_broker_a_login_started_in_a_change_that_found_none')

    def test_late_refusal_after_a_kill_in_the_desktop_write_removes_its_temporary(self):
        self.sweep('late_refusal_after_a_kill_in_the_desktop_write')

    def test_uninstall_over_a_change_parked_with_a_desktop_temporary_removes_it(self):
        self.sweep('uninstall_over_a_change_parked_with_a_desktop_temporary')

    def test_repeated_kills_while_staging_never_exhaust_a_recovery_bound(self):
        """More kills than any bound (8) at the same unbound preparation, then one resume: nothing left, nothing refused."""
        cases = [('upgrade', 'create', '/.change_1-staging-'),                 # an opening change transaction
                 ('upgrade', 'create', '/skill-packet-telegram-search-'),      # a skill packet
                 ('rollback', 'create', '/metadata-packet-receipt-'),          # a receipt packet
                 ('adopt_first_install', 'mkdir', '/.runtime-initial-'),       # a first-install staged root
                 ('adopt_first_install', 'replace', '/install-intent.json'),   # its intent, before publication
                 ('adopt_first_install', 'create', '/metadata-packet-adopted_sidecar-')]  # the adopted.json packet
        references = {}
        with self.fixed_base():
            for name, effect, fragment in cases:
                with self.subTest(scenario=name, effect=effect, at=fragment):
                    scenario = SCENARIOS[name]
                    if name not in references:
                        references[name] = self.reference(scenario)
                    self.setUp()
                    scenario.prepare(self)
                    for _ in range(9):
                        code = self.killed(scenario, lambda count, kind, where: kind == effect and fragment in '/' + where)
                        self.assertEqual(code, 9)
                    result = scenario.action(self, self.launchctl, self.claude())
                    self.assertEqual(self.problems(references[name], result), [])


class LeftoverOwnershipTests(AdoptionHarness, unittest.TestCase):
    """A resume removes only the leftovers it can prove its own; anything else stays, untouched and never adopted."""

    def directory(self, path, mode=0o700):
        for component in [p for p in reversed(path.parents) if p.is_relative_to(self.base)] + [path]:
            if not component.exists():
                component.mkdir()
                component.chmod(mode)
        return path

    def file(self, path, data=b'x', mode=0o600):
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_first_install_removes_only_staged_roots_and_intent_temporaries_provably_its_own(self):
        packet, parent = self.build('a'), self.runtime.parent
        ours = self.directory(parent / '.runtime-initial-abcdefgh')
        tx = self.directory(ours / 'transactions' / ('install_' + '0' * 24))
        self.file(tx / 'journal.json', b'{}')
        self.file(tx / ('.telegram-search-' + 'a' * 32 + '.tmp'))
        self.directory(ours / 'releases')
        self.file(ours / 'install-intent.json', b'{}')
        # An empty staged root is ours only under one of the installer's derived attempt names.
        empty_ours = self.directory(parent / bundle_install._staged_names(self.runtime)[5])
        # An earlier release's intent temporary is ours only when its bytes parse as an intent for this root.
        intent = {'identity': {'installation_root': str(self.runtime)}, 'staged_root': str(ours), 'object': {'kind': 'directory'}}
        legacy = self.file(parent / ('.runtime-intent-' + 'c' * 32 + '.tmp'), json.dumps(intent).encode())
        kept = [self.file(self.directory(parent / '.runtime-initial-foreign1') / 'notes.txt'),
                self.directory(parent / '.runtime-initial-shared12', 0o750),
                self.directory(parent / '.runtime-initial-release1' / 'releases' / 'x'),
                self.file(self.directory(parent / '.runtime-initial-hardlnk1') / '.install.lock', b''),
                self.directory(parent / '.runtime-initial-linked12') / '.install.lock',
                self.directory(parent / '.runtime-initial-emptydir'),
                self.file(parent / ('.runtime-intent-' + 'b' * 32 + '.tmp'), b'{"partial"'),
                self.file(parent / ('.runtime-intent-' + 'd' * 32 + '.tmp'),
                          json.dumps({**intent, 'identity': {'installation_root': '/elsewhere'}}).encode()),
                self.file(parent / ('.runtime-intent-' + 'e' * 32 + '.tmp'), json.dumps({**intent, 'extra': 1}).encode())]
        os.link(kept[3], self.base / 'second-link')
        kept[4].symlink_to(self.base / 'elsewhere')
        self.assertEqual(self.install(packet)['status'], 'activation_required')
        self.assertFalse(ours.exists() or empty_ours.exists() or legacy.exists())
        for path in kept:
            self.assertTrue(os.path.lexists(path), path)
        self.assertEqual(sorted(p.name for p in parent.glob('.runtime-initial-*')),
                         ['.runtime-initial-emptydir', '.runtime-initial-foreign1', '.runtime-initial-hardlnk1',
                          '.runtime-initial-linked12', '.runtime-initial-release1', '.runtime-initial-shared12'])
        self.assertEqual(sorted(p.name for p in parent.glob('.runtime-intent-*')),
                         ['.runtime-intent-' + c * 32 + '.tmp' for c in 'bde'])

    def test_a_resumed_change_removes_only_staging_and_unbound_packets_provably_its_own(self):
        self.full()
        new = self.build('b', launcher=b'# second release\n', search=b'search-skill-2')

        def crash(value):
            if value == 'prepared':
                raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            self.install(new, checkpoint=crash)
        transactions = self.runtime / 'transactions'
        tx = transactions / 'change_1'
        ours = [self.directory(tx / 'skill-packet-telegram-search-5'), self.directory(tx / 'metadata-packet-receipt-6'),
                self.directory(tx / 'failed-release-7' / 'app'), self.directory(transactions / '.change_2-staging-abcdefgh')]
        self.file(ours[0] / 'SKILL.md')
        self.file(ours[1] / ('.telegram-search-' + 'c' * 32 + '.tmp'))
        self.file(ours[2] / 'launcher.py', b'partial', 0o400)
        self.file(ours[3] / 'journal.json', b'{}')
        kept = [self.directory(tx / 'skill-packet-telegram-setup-0') / 'SKILL.md',
                self.file(self.directory(tx / 'metadata-packet-pointer-0') / 'current.json', b'{}', 0o644),
                self.file(self.directory(tx / 'metadata-packet-adopted_sidecar-0') / 'adopted.json', b'{}'),
                self.file(self.directory(transactions / '.change_2-staging-zzzzzzzz') / 'notes.txt')]
        kept[0].symlink_to(self.base / 'elsewhere')
        os.link(kept[2], self.base / 'second-link')
        self.assertEqual(self.install(new)['status'], 'upgraded')
        for path in ours:
            self.assertFalse(path.exists(), path)
        for path in kept:
            self.assertTrue(os.path.lexists(path), path)
        # A packet that is not provably ours is never reused: the pointer was staged in the next free one.
        pointer = json.loads((tx / 'journal.json').read_text())['publications']['pointer'][-1]
        self.assertEqual(Path(pointer['staged']).parent.name, 'metadata-packet-pointer-1')


class UnfinishedActionTests(AdoptionHarness, unittest.TestCase):
    """Uninstall's sweep removes only the retired root it can prove (the directory the install intent binds); an
    uninstall interrupted after its retiring rename (its last journaled step) is finished by the next install or
    uninstall, and an unfinished first install is named by the next action, never half-removed. Real kills only."""

    ABSENT = {'status': 'uninstalled', 'installation': 'absent'}

    def beside(self):
        """Every name beside the root that starts like the installer's (intent, staged, retired or kept root)."""
        return sorted(p.name for p in self.home.iterdir() if p.name.startswith('.' + self.runtime.name))

    def lookalikes(self):
        """Directories beside the root under the installer's prefixes that are not its own."""
        made = []
        for name in ('.runtime-removed-notmine', '.runtime-removed-' + 'a' * 32, '.runtime-initial-emptydir'):
            directory = self.home / name
            directory.mkdir(mode=0o700)
            if 'emptydir' not in name:
                (directory / 'notes.txt').write_bytes(b'not the installer\'s')
            made.append(directory)
        return made

    def assertKept(self, lookalikes):
        self.assertEqual([p for p in lookalikes if not p.is_dir()], [])
        for directory in lookalikes:
            self.assertEqual(sorted(p.name for p in directory.iterdir()), [] if 'emptydir' in directory.name else ['notes.txt'])

    def perform(self, action, effects):
        """`action(launchctl, claude_runner)` with both fakes counted as single effects, like the sweep's."""
        with effects.patched():
            return action(effects.runner(self.launchctl, 'launchctl'), effects.runner(self.claude(), 'claude'))

    def killed(self, action, predicate):
        """Run `action` in a forked child that exits right after the first effect `predicate` selects."""
        pid = os.fork()
        if pid == 0:
            code = 3
            try:
                def hook(count, kind, where):
                    if predicate(count, kind, where):
                        os._exit(9)
                self.perform(action, Effects(self.base, hook))
            except BaseException:
                code = 4
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def assertPending(self, action, pending):
        with self.assertRaises(bundle_install.InstallError) as caught:
            action()
        self.assertEqual((str(caught.exception), caught.exception.pending_action), ('transaction_pending', pending))

    def test_uninstall_removes_only_the_retired_root_the_intent_binds(self):
        lookalikes = self.lookalikes()
        self.full()
        self.assertEqual(self.change('uninstall')['installation'], 'removed')
        self.assertEqual(self.beside(), sorted(p.name for p in lookalikes))
        self.assertKept(lookalikes)
        self.assertEqual(self.change('uninstall'), self.ABSENT)  # Nothing installed: the lookalikes still stay.
        self.assertKept(lookalikes)

    RETIRING = staticmethod(lambda count, kind, where: kind == 'renameatx'
                            and re.search(r'/\.runtime-removed-[0-9a-f]{32}\Z', where))
    # The first entry the closing sweep removes from the retired root.
    SWEEPING = staticmethod(lambda count, kind, where: kind == 'unlink' and re.search(r'/\.runtime-removed-[0-9a-f]{32}/', where))

    def uninstall_at(self, path):
        """`uninstall` as the launcher inside `path` runs it: that release passes its own installation root."""
        with mock.patch('telegram_search_mcp.bundle_install.require_platform'):
            return bundle_install.uninstall(path, user_home=self.home, launchctl=self.launchctl, pending_sends=self.probe,
                                            claude_candidates=(self.claude_cli,), claude_runner=self.claude())

    def retired(self, lookalikes=()):
        """The retired root a killed uninstall left (exactly one, beside the intent)."""
        left = [n for n in self.beside() if n not in {p.name for p in lookalikes}]
        self.assertEqual(sorted(re.sub('[0-9a-f]{32}', '*', n) for n in left), ['.runtime-removed-*', '.runtime.install-intent.json'])
        self.assertFalse(self.runtime.exists())
        return self.home / next(n for n in left if '-removed-' in n)

    def test_a_kill_after_the_retiring_rename_is_finished_by_the_retired_roots_launcher(self):
        lookalikes = self.lookalikes()
        self.full()
        self.assertEqual(self.killed(lambda lc, cl: self.change('uninstall', launchctl=lc, claude_runner=cl), self.RETIRING), 9)
        retired = self.retired(lookalikes)
        self.assertPending(lambda: self.change('rollback'), 'uninstall')
        # The only launcher left is the retired root's own, which passes that root's path.
        self.assertEqual(self.uninstall_at(retired), self.ABSENT)
        self.assertEqual(self.beside(), sorted(p.name for p in lookalikes))
        self.assertKept(lookalikes)
        self.assertEqual(self.change('uninstall'), self.ABSENT)
        self.assertEqual(self.install(self.build('c', launcher=b'# third release\n'))['installation'], 'installed')

    def test_a_kill_after_the_retiring_rename_or_inside_its_sweep_is_finished_by_install_or_uninstall(self):
        """The rename is the uninstall's last journaled step: a kill after it, or partway through the retired root's
        removal (whose own launcher may then no longer start), is finished by the next install (this kit or another)
        or uninstall, never refused as `transaction_pending`. Lookalikes stay."""
        entries = {}
        def release_file(count, kind, where):  # The first file removed from the retired release tree.
            return kind == 'unlink' and re.search(r'/\.runtime-removed-[0-9a-f]{32}/releases/', where)
        for point, predicate in (('rename', self.RETIRING), ('sweep', self.SWEEPING), ('release file', release_file)):
            for follow in ('same kit', 'other kit', 'uninstall'):
                with self.subTest(point=point, follow=follow):
                    self.setUp()
                    lookalikes = self.lookalikes()
                    packet, _ = self.full()
                    self.assertEqual(self.killed(lambda lc, cl: self.change('uninstall', launchctl=lc, claude_runner=cl),
                                                 predicate), 9)
                    retired = self.retired(lookalikes)
                    entries[point] = sum(1 for _ in retired.rglob('*'))
                    if point == 'sweep':
                        self.assertEqual(entries['sweep'], entries['rename'] - 1)  # Partly removed: one entry gone.
                    if point == 'release file':
                        # A file its own launcher's admission requires is gone: that launcher no longer starts.
                        release = retired / 'releases' / packet[3]
                        files = [f['path'] for f in json.loads(packet[2].read_text())['files']] + ['bundle-index.json']
                        self.assertNotEqual([f for f in files if not (release / f).exists()], [])
                    if follow == 'uninstall':
                        self.assertEqual(self.change('uninstall'), self.ABSENT)
                        self.assertEqual(self.beside(), sorted(p.name for p in lookalikes))
                    else:
                        with mock.patch('telegram_search_mcp.__version__', '0.52.9') if follow == 'same kit' else contextlib.nullcontext():
                            kit = packet if follow == 'same kit' else self.build('b', launcher=b'# second release\n')
                            result = self.install(kit)
                        self.assertEqual((result['status'], result['installation']), ('activation_required', 'installed'))
                        self.assertEqual(self.pointer()['index_sha256'], kit[3])
                        self.assertEqual(self.beside(), sorted([p.name for p in lookalikes] + ['.runtime.install-intent.json']))
                        self.assertEqual(self.change('uninstall')['installation'], 'removed')
                        self.assertEqual(self.beside(), sorted(p.name for p in lookalikes))
                    self.assertKept(lookalikes)

    def test_lookalikes_and_a_copy_of_the_retired_root_survive_every_finishing_action(self):
        """Lookalikes under every installer prefix, and an exact copy of the retired root under another retired name
        (the same bytes, another inode), survive an install, a killed uninstall and its finish by the copy's launcher
        (refused: not the root the intent binds) and by the next install."""
        h, made = self.home, {}

        def directory(name, files=()):
            made[name] = h / name
            made[name].mkdir(mode=0o700)
            for file in files:
                (made[name] / file).parent.mkdir(parents=True, exist_ok=True)
                (made[name] / file).write_bytes(b'x')

        directory('.runtime-removed-' + '1' * 32, ['notes.txt'])
        directory('.runtime-removed-' + '2' * 32)
        directory('.runtime-removed-' + '3' * 32, ['releases/x/launcher.py', 'receipt.json'])
        made['file'] = h / ('.runtime-removed-' + '4' * 32)
        made['file'].write_bytes(b'file')
        made['link'] = h / ('.runtime-removed-' + '5' * 32)
        os.symlink(self.base, made['link'])
        directory('.runtime-initial-randomxx', ['notes.txt'])
        directory(bundle_install._staged_names(self.runtime)[0], ['foreign.bin'])  # Not empty: not provably ours.
        made['temporary'] = h / ('.runtime-intent-' + 'f' * 32 + '.tmp')
        made['temporary'].write_bytes(b'garbage')
        made['bak'] = h / '.runtime.install-intent.json.bak'
        made['bak'].write_bytes(b'{}')
        directory('.runtime-adopted-' + 'a' * 24, ['x'])

        def snapshot():
            return {k: os.readlink(p) if p.is_symlink() else sorted(str(q.relative_to(p)) for q in p.rglob('*'))
                    if p.is_dir() else p.read_bytes() for k, p in made.items()}

        before = snapshot()
        self.full()
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.killed(lambda lc, cl: self.change('uninstall', launchctl=lc, claude_runner=cl), self.RETIRING), 9)
        retired = self.retired(made.values())
        made['copy'] = h / ('.runtime-removed-' + '6' * 32)
        shutil.copytree(retired, made['copy'], symlinks=True)
        before = snapshot()
        self.assertPending(lambda: self.change('rollback'), 'uninstall')
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.uninstall_at(made['copy'])
        self.assertEqual(str(caught.exception), 'metadata_conflict')
        self.assertEqual(snapshot(), before)
        self.assertTrue(retired.is_dir())
        result = self.install(self.build('b', launcher=b'# second release\n'))
        self.assertEqual((result['status'], result['installation']), ('activation_required', 'installed'))
        self.assertFalse(os.path.lexists(retired))
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.change('uninstall')['installation'], 'removed')
        self.assertEqual(self.beside(), sorted(p.name for p in made.values()))
        self.assertEqual(snapshot(), before)

    def test_lookalikes_under_the_retired_name_stay_through_an_uninstall(self):
        lookalikes = {'file': lambda p: p.write_bytes(b'file'), 'symlink': lambda p: os.symlink(self.base, p),
                      'directory 0755': lambda p: p.mkdir(mode=0o755), 'empty directory 0700': lambda p: p.mkdir(mode=0o700)}
        for number, (kind, make) in enumerate(lookalikes.items(), start=4):
            with self.subTest(kind=kind):
                self.setUp()
                lookalike = self.home / ('.runtime-removed-' + str(number) * 32)
                make(lookalike)
                self.full()
                self.assertEqual(self.change('uninstall')['installation'], 'removed')
                self.assertEqual(self.beside(), [lookalike.name])
                self.assertEqual(self.change('uninstall'), self.ABSENT)
                self.assertEqual(self.beside(), [lookalike.name])

    def test_a_lookalike_at_the_recorded_staged_name_never_stops_an_uninstall_halfway(self):
        """After the root's publication its recorded staged name is free: whatever appears there is not the directory
        the intent binds, so it names no unfinished first install. Uninstall completes and the lookalike stays."""
        lookalikes = {'directory with data': lambda p: (p.mkdir(mode=0o700), (p / 'mine.txt').write_bytes(b'user data')),
                      'directory 0755': lambda p: p.mkdir(mode=0o755), 'file': lambda p: p.write_bytes(b'file'),
                      'symlink': lambda p: os.symlink(self.base, p)}
        for kind, make in lookalikes.items():
            with self.subTest(kind=kind):
                self.setUp()
                self.full()
                staged = Path(json.loads((self.home / '.runtime.install-intent.json').read_text())['staged_root'])
                self.assertEqual(staged.name, bundle_install._staged_names(self.runtime)[0])
                make(staged)
                kept = lambda: (os.path.islink(staged), staged.is_dir(), sorted(os.listdir(staged)) if staged.is_dir() and not staged.is_symlink() else None)
                before = kept()
                result = self.change('uninstall')
                self.assertEqual((result['installation'], result['codex'], result['claude_code'], result['claude_desktop']),
                                 ('removed', 'removed', 'removed', 'removed'))
                self.assertEqual(self.beside(), [staged.name])
                self.assertEqual(self.change('uninstall'), self.ABSENT)
                result = self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual((result['status'], result['installation']), ('activation_required', 'installed'))
                self.assertEqual(self.change('uninstall')['installation'], 'removed')
                self.assertEqual((self.beside(), kept()), ([staged.name], before))

    def test_unreadable_lookalikes_never_stop_an_uninstall_or_the_next_install(self):
        """A lookalike the installer cannot read (mode 000 or 300) is not provably ours: it stays, and neither the
        uninstall nor the next install stops on it."""
        def staged(home):
            return Path(json.loads((home / '.runtime.install-intent.json').read_text())['staged_root'])
        plants = {
            'initial 000': lambda h: (h / '.runtime-initial-zzzzzzzz', 0o000, None),
            'initial 300': lambda h: (h / '.runtime-initial-zzzzzzzz', 0o300, None),
            'initial/transactions 000': lambda h: (h / '.runtime-initial-zzzzzzzz', 0o000, 'transactions'),
            'recorded staged 000': lambda h: (staged(h), 0o000, None),
            'removed 000': lambda h: (h / ('.runtime-removed-' + 'b' * 32), 0o000, None)}
        for kind, plant in plants.items():
            with self.subTest(kind=kind):
                self.setUp()
                self.full()
                path, mode, inner = plant(self.home)
                path.mkdir(mode=0o700)
                locked = path / inner if inner else path
                if inner:
                    locked.mkdir(mode=0o700)
                locked.chmod(mode)
                self.addCleanup(os.chmod, locked, 0o700)
                result = self.change('uninstall')
                self.assertEqual(result['installation'], 'removed')
                self.assertFalse(self.runtime.exists())
                self.assertEqual(self.beside(), [path.name])
                result = self.install(self.build('b', launcher=b'# second release\n'))
                self.assertEqual((result['status'], result['installation']), ('activation_required', 'installed'))
                self.assertEqual(self.change('uninstall')['installation'], 'removed')
                self.assertEqual(self.beside(), [path.name])

    def test_an_intent_the_closing_sweep_could_not_read_refuses_the_uninstall_before_any_removal(self):
        self.full()
        intent = self.home / '.runtime.install-intent.json'
        intent.write_bytes(intent.read_bytes().replace(b'"staged_root"', b'"staged_rooted"'))
        receipt, config, desktop = self.receipt(), (self.codex / 'config.toml').read_bytes(), self.desktop_config.read_bytes()
        transactions = sorted(os.listdir(self.runtime / 'transactions'))
        with self.assertRaises(bundle_install.InstallError) as caught:
            self.change('uninstall')
        self.assertEqual(str(caught.exception), 'initialization_conflict')
        self.assertEqual((self.receipt(), (self.codex / 'config.toml').read_bytes(), self.desktop_config.read_bytes()),
                         (receipt, config, desktop))
        self.assertEqual(sorted(os.listdir(self.runtime / 'transactions')), transactions)
        self.assertIsNotNone(self.launchctl.loaded)

    def test_an_intent_left_after_the_retired_root_is_gone_is_removed_by_the_next_install(self):
        """Killed after the retired root's removal, before the intent's: no launcher is left, so the next install
        (or an uninstall) removes the intent, which binds nothing any more, and a first install follows."""
        lookalikes = self.lookalikes()
        _, old = self.full()
        code = self.killed(lambda lc, cl: self.change('uninstall', launchctl=lc, claude_runner=cl),
                           lambda count, kind, where: kind == 'rmdir' and re.search(r'/\.runtime-removed-[0-9a-f]{32}\Z', where))
        self.assertEqual(code, 9)
        self.assertEqual([n for n in self.beside() if n not in {p.name for p in lookalikes}], ['.runtime.install-intent.json'])
        result = self.install(self.build('b', launcher=b'# second release\n'), claude_code=None)
        self.assertEqual((result['installation'], result['claude_code']), ('installed', 'registered'))
        self.assertEqual(json.loads((self.home / '.runtime.install-intent.json').read_text())['identity']['index_sha256'],
                         self.pointer()['index_sha256'])
        self.assertNotEqual(self.pointer()['release_root'], old['release_root'])
        self.assertKept(lookalikes)

    def test_a_killed_first_install_is_named_by_every_other_action_or_its_unbound_staging_removed(self):
        """A kill after every effect up to the root's publication, then at a stride. Once the intent is recorded,
        uninstall, rollback and another kit refuse as an unfinished `install` and remove nothing (intent, staged root
        or root stay); before it, uninstall removes the killed attempt's unbound staged root. The install then
        completes and uninstalls without a leftover."""
        def prepare():
            self.codex_present()
            self.desktop_present()
            (self.home / '.claude').mkdir(mode=0o755)
            self.claude()  # Built in this process, so a killed child's resume finds the same fake.
            self.packet = self.build('a')
        def first(lc, cl):
            return self.install(self.packet, launchctl=lc, claude_runner=cl)
        trace = []
        prepare()
        self.perform(first, Effects(self.base, lambda count, kind, where: trace.append((kind, where))))
        published = trace.index(('renameatx', 'user/runtime')) + 1
        points = sorted({*range(1, published + 1), *range(published + 1, len(trace) + 1, 4), len(trace)})
        outcomes = set()
        for n in points:
            with self.subTest(kill=n, after=trace[n - 1]):
                self.setUp()
                prepare()
                self.assertEqual(self.killed(first, lambda count, *_: count == n), 9)
                before, root = self.beside(), self.runtime.exists()
                if root and not bundle_install._install_pending(self.runtime):
                    outcomes.add('complete')  # Killed after its last journal write: an ordinary uninstall follows.
                elif (self.home / '.runtime.install-intent.json').exists():
                    self.assertPending(lambda: self.change('uninstall'), 'install')
                    self.assertPending(lambda: self.change('rollback'), 'install')
                    self.assertPending(lambda: self.install(self.build('b', launcher=b'# second release\n')), 'install')
                    self.assertEqual((self.beside(), self.runtime.exists()), (before, root))
                    outcomes.add('install pending')
                else:
                    self.assertEqual(self.change('uninstall'), self.ABSENT)
                    self.assertEqual(self.beside(), [])
                    outcomes.add('staging removed')
                self.assertEqual(self.install(self.packet)['status'], 'activation_required')
                self.assertEqual(self.change('uninstall')['installation'], 'removed')
                self.assertEqual(self.beside(), [])
        self.assertEqual(outcomes, {'complete', 'install pending', 'staging removed'})

    def test_a_first_install_killed_beside_lookalikes_never_removes_one(self):
        """Lookalikes under the retired, staged and intent-temporary prefixes (one not empty under a derived attempt
        name) beside a first install killed after each effect up to just past its publication, and after its last:
        the uninstall that follows refuses as an unfinished `install` or completes, and neither it nor the finished
        install and uninstall after it removes a lookalike or leaves anything else."""
        def prepare():
            self.codex_present()
            self.desktop_present()
            (self.home / '.claude').mkdir(mode=0o755)
            self.claude()
            self.packet = self.build('a')
            names = ['.runtime-intent-' + 'e' * 32 + '.tmp']
            (self.home / names[0]).write_bytes(b'{}')
            for name, files in (('.runtime-removed-' + '1' * 32, ['n.txt']), ('.runtime-removed-' + '2' * 32, []),
                                ('.runtime-initial-zzzzzzzz', ['n.txt']), ('.runtime-initial-yyyyyyyy', []),
                                (bundle_install._staged_names(self.runtime)[7], ['n.txt'])):
                (self.home / name).mkdir(mode=0o700)
                for file in files:
                    (self.home / name / file).write_bytes(b'x')
                names.append(name)
            return sorted(names)

        def first(lc, cl):
            return self.install(self.packet, launchctl=lc, claude_runner=cl)
        trace = []
        prepare()
        self.perform(first, Effects(self.base, lambda count, kind, where: trace.append((kind, where))))
        published = trace.index(('renameatx', 'user/runtime')) + 1
        outcomes = set()
        for n in sorted({*range(1, published + 2), len(trace)}):
            with self.subTest(kill=n, after=trace[n - 1]):
                self.setUp()
                lookalikes = prepare()
                self.assertEqual(self.killed(first, lambda count, *_: count == n), 9)
                try:
                    outcome = self.change('uninstall')['installation']
                except bundle_install.InstallError as error:
                    outcome = (str(error), error.pending_action)
                    self.assertEqual(outcome, ('transaction_pending', 'install'))
                outcomes.add(str(outcome))
                self.assertEqual([name for name in lookalikes if not os.path.lexists(self.home / name)], [])
                if outcome in ('absent', 'removed'):
                    self.assertEqual(self.beside(), lookalikes)
                self.assertEqual(self.install(self.packet)['status'], 'activation_required')
                self.assertEqual(self.change('uninstall')['installation'], 'removed')
                self.assertEqual(self.beside(), lookalikes)
        self.assertEqual(outcomes, {'absent', 'removed', str(('transaction_pending', 'install'))})


if __name__ == '__main__':
    unittest.main()
