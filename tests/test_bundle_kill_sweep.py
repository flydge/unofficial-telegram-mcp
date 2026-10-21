"""Kill sweep (CLIENTS-13): a hard kill (a forked child, `os._exit`) right after every filesystem create,
rename, unlink, mkdir and rmdir and after every launchctl or claude CLI call of an upgrade, a rollback, an
uninstall, `--adopt-legacy` on upgrade and on first install, and an uninstall that keeps adoption backups; the
parent then resumes the same action.

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

class Scenario:
    def __init__(self, prepare, action, check, write_ahead=True):
        self.prepare, self.action, self.check, self.write_ahead = prepare, action, check, write_ahead


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
        self.assertTrue({'create', 'replace', 'renameatx', 'launchctl'} <= kinds, kinds)
        if scenario.write_ahead:
            self.assertTrue('claude' in kinds and any(window for _, _, window, _ in trace))
        self.assertGreater(total, 50)
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
        legacy = self.file(parent / ('.runtime-intent-' + 'b' * 32 + '.tmp'), b'{"partial"')
        kept = [self.file(self.directory(parent / '.runtime-initial-foreign1') / 'notes.txt'),
                self.directory(parent / '.runtime-initial-shared12', 0o750),
                self.directory(parent / '.runtime-initial-release1' / 'releases' / 'x'),
                self.file(self.directory(parent / '.runtime-initial-hardlnk1') / '.install.lock', b''),
                self.directory(parent / '.runtime-initial-linked12') / '.install.lock']
        os.link(kept[3], self.base / 'second-link')
        kept[4].symlink_to(self.base / 'elsewhere')
        self.assertEqual(self.install(packet)['status'], 'activation_required')
        self.assertFalse(ours.exists() or legacy.exists())
        for path in kept:
            self.assertTrue(os.path.lexists(path), path)
        self.assertEqual(sorted(p.name for p in parent.glob('.runtime-initial-*')),
                         ['.runtime-initial-foreign1', '.runtime-initial-hardlnk1', '.runtime-initial-linked12',
                          '.runtime-initial-release1', '.runtime-initial-shared12'])

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


if __name__ == '__main__':
    unittest.main()
