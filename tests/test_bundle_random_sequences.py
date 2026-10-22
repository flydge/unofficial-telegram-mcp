"""Seeded random sequences of installer actions (0.53 critic rounds 4 and 5), over disposable homes only.

Each sequence starts from an installed release with every client present, a session and an exact loaded broker,
and runs at most `STEPS` steps drawn with a fixed seed from:

- an upgrade to kit B or C, the repeat of the last action, a rollback, an uninstall;
- a conflict injected now, or armed to fire at a checkpoint of the next run: our Codex table changed, our search
  skill copy edited, our Claude Desktop or Claude Code entry changed, or a neighbour added to every client;
- every conflict cleared (a neighbour stays: it is the user's);
- a kill: the run's forked child hard-exits at a checkpoint or right after its Nth durable effect;
- a login: launchd loads the plist on disk when nothing is loaded (after a kill past the plist rewrite, the
  target release's);
- a burst of 2-12 late refusals: our Codex table changed at a random checkpoint after the broker stop, then put
  back, run after run.

Every action runs in a forked child, so a kill leaves exactly what a crash would; launchd, the claude CLI and the
pending-send probe are file-backed fakes. The seeds, the number of sequences and the steps are fixed.

Oracle after every action:

- No crash: anything other than an `InstallError` is a problem.
- A finished install, upgrade or rollback: `current.json`, the `runtime.toml` pin, the plist and the loaded
  broker name one release, every client entry and skill copy is exactly that release's (K1-K3 aside), and no
  `.telegram-search-*.tmp` is left anywhere.
- A refusal other than `transaction_pending`, an installation present: the same one-release state for the
  release `current.json` names (a late refusal's restore leaves the old release serving), no temporary.
- A finished uninstall: no root, plist, loaded broker or temporary; no entry or skill copy of ours (K4, K5);
  every neighbour kept.

Liveness at the end: every conflict cleared, then, as the seed decides, the unfinished action is repeated (at most
four times) and must finish in the one-release state with every client entry exactly that release's; and
uninstall, repeating a named unfinished action first, must finish within six tries and leave nothing.

Accepted outcomes, each documented in install.md and never a problem:

- K1. Claude Code `failed` (a kill between the journaled `claude_remove|claude_add` record and the CLI call is
  never retried) or `unavailable`: the entry is not compared.
- K2. Claude Desktop `failed` (an entry changed meanwhile, or its bounded attempts) or `unavailable`: not compared.
- K3. An entry or skill copy whose conflict is active stays the user's edit: compared with that edit.
- K4. Uninstall `kept`: an entry or skill copy whose conflict was active during an uninstall run that decided
  anything stays, even when the edit is reverted before a resume (each client is decided once, journaled).
- K5. An uninstall resumed after its retiring rename reports `installation: absent` without client statuses; K4
  applies by the conflicts active during its runs.
- K6. No broker after a first install over an absent root (an uninstall killed after its rename, then a kit): the
  service is not compared.
- K7. A Claude Code or Claude Desktop entry the user changed while a run wrote it (that run's result `failed`, its
  conflict active) is not owned, even once the change is reverted: uninstall keeps it (`kept`) until a later
  change registers that client again.
"""
import collections
import json
import os
import plistlib
import random
import unittest

from claude_fake import entry
from test_bundle_kill_sweep import Effects
from test_bundle_late_refusal import ParkedHarness, OURS, CHANGED, WEATHER
from test_bundle_upgrade import NEIGHBOUR_CODEX

SEEDS = range(96)
WORKERS = max(2, min(4, (os.cpu_count() or 2) // 2))  # As the kill sweep: more only contend for the same directories.
STEPS = 9
BURST = (2, 12)
KILL_EFFECTS = 140
# Every checkpoint an upgrade, rollback or uninstall passes (a kill or an armed conflict picks one).
CHECKPOINTS = ['prepared', 'created:packet:release', 'recorded:release', 'published:release', 'release',
               'stopped:service', 'recorded:plist', 'published:plist', 'service', 'recorded:registration',
               'published:registration', 'registration', 'telegram-search', 'telegram-setup', 'claude-telegram-search',
               'claude-telegram-setup', 'recorded:claude_remove', 'published:claude_remove', 'recorded:claude_add',
               'published:claude_add', 'claude_code', 'recorded:claude_desktop_add', 'recorded:claude_desktop_write',
               'published:claude_desktop_add', 'claude_desktop', 'recorded:policy', 'published:policy', 'policy',
               'created:packet:receipt', 'recorded:receipt', 'published:receipt', 'receipt', 'created:packet:pointer',
               'recorded:pointer', 'published:pointer', 'complete', 'restarted', 'recorded:plist_restore',
               'recorded:service_restore', 'restored:service', 'published:retired', 'removed:telegram-search',
               'removed:telegram-setup', 'removed:claude-telegram-search', 'removed:claude-telegram-setup',
               'recorded:codex', 'published:codex', 'retired', 'skills', 'published:plist_restore',
               'recorded:policy_restore', 'published:policy_restore',
               *(s + n for n in ('telegram-search', 'telegram-setup', 'claude-telegram-search', 'claude-telegram-setup')
                 for s in ('created:packet:', 'recorded:', 'published:')),
               'created:initial_root', 'recorded:root', 'published:root']
POST_STOP = CHECKPOINTS[CHECKPOINTS.index('stopped:service'):CHECKPOINTS.index('published:pointer')]
# Where our Codex table changed refuses the run late: from the stop to the check right after the Codex write.
LATE = CHECKPOINTS[CHECKPOINTS.index('stopped:service'):CHECKPOINTS.index('registration')]
CALENDAR = {'command': '/opt/calendar', 'args': ['--port', '7']}
KINDS = ('codex', 'codex', 'skill', 'desktop', 'claude', 'neighbour')
# The uninstall result key a conflict kind makes `kept` (K4); a neighbour never does.
KEPT = {'codex': 'codex', 'skill': 'telegram-search', 'desktop': 'claude_desktop', 'claude': 'claude_code'}
ACTIONS = ('upgrade', 'repeat', 'rollback', 'uninstall', 'inject', 'clear', 'kill', 'burst', 'login')
WEIGHTS = (4, 3, 2, 1, 3, 2, 4, 1, 2)


class RandomSequenceTests(ParkedHarness, unittest.TestCase):

    # --- one action in a forked child --------------------------------------------------------------------

    def run_child(self, name, kill_at=None, kill_effect=None, inject=None):
        """['ok', result] | ['refused', code, pending_action] | ['crash', text] | ['killed']."""
        if name[0] == 'upgrade' and not (self.runtime / 'current.json').exists():
            self.serviced = False  # K6: a first install over an absent root starts no broker.
        self.during = self.active | ({inject.kind} if inject is not None else set())
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            try:
                def checkpoint(value):
                    self.seen.add(value)
                    if inject is not None:
                        inject(value)
                    if value == kill_at:
                        os._exit(9)

                def hook(count, kind, where):
                    if count == kill_effect:
                        os._exit(9)
                effects = Effects(self.base, hook)
                kwargs = dict(checkpoint=checkpoint, launchctl=effects.runner(self.launchctl, 'launchctl'),
                              claude_runner=effects.runner(self.claude(), 'claude'))
                try:
                    with effects.patched():
                        value = ['ok', self.act(name, kwargs)]
                except self.module().InstallError as error:
                    value = ['refused', str(error), error.pending_action]
                value.append(sorted(self.seen))
            except BaseException as error:
                value = ['crash', repr(error), []]
            try:
                os.write(write, json.dumps(value).encode())
            finally:
                os._exit(0)
        os.close(write)
        data = b''
        while True:
            chunk = os.read(read, 65536)
            if not chunk:
                break
            data += chunk
        os.close(read)
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) == 9:
            outcome = ['killed']
        else:
            outcome = json.loads(data)
            self.seen.update(outcome.pop())
        if name[0] == 'uninstall' and not (outcome[0] == 'refused' and outcome[1] == 'transaction_pending'):
            self.decided |= self.during  # K4: this run may have decided a client with these conflicts active.
        self.trace[outcome[0] if outcome[0] != 'refused' else 'refused:' + outcome[1]] += 1
        return outcome

    def act(self, name, kwargs):
        if name[0] == 'upgrade':
            return self.install(self.packets[name[1]], **kwargs)
        return self.change(name[0], **kwargs)

    # --- conflicts ---------------------------------------------------------------------------------------

    def apply(self, kind):
        self.active.add(kind)
        if kind == 'codex':
            data = self.config.read_bytes()
            if OURS in data:
                self.config.write_bytes(data.replace(OURS, CHANGED))
        elif kind == 'skill':
            path = self.skills / 'telegram-search' / 'SKILL.md'
            if path.exists() and path.read_bytes() != b'mine':
                self.note(skill=[path.read_bytes().hex(), path.stat().st_mode & 0o777])
                path.chmod(0o600)
                path.write_bytes(b'mine')
        elif kind == 'desktop':
            document = json.loads(self.desktop_config.read_text())
            value = document['mcpServers'].get('telegram_search')
            if value is not None and '--x' not in value['args']:
                value['args'] = value['args'] + ['--x']
                self.desktop_config.write_text(json.dumps(document, indent=2) + '\n')
        elif kind == 'claude':
            document = self.claude().document() or {}
            value = document.get('mcpServers', {}).get('telegram_search')
            if value is not None and '--x' not in value['args']:
                value['args'] = value['args'] + ['--x']
                self.claude().write_document(document)
        else:
            if WEATHER not in self.config.read_bytes():
                self.config.write_bytes(self.config.read_bytes() + WEATHER)
            document = json.loads(self.desktop_config.read_text())
            if 'calendar' not in document['mcpServers']:
                document['mcpServers']['calendar'] = CALENDAR
                self.desktop_config.write_text(json.dumps(document, indent=2) + '\n')
            document = self.claude().document() or {}
            if 'calendar' not in document.get('mcpServers', {}):
                document.setdefault('mcpServers', {})['calendar'] = entry(CALENDAR['command'], CALENDAR['args'])
                self.claude().write_document(document)
            self.note(neighboured=True)

    def notes(self):
        """What a conflict applied in a child records for the parent: the skill copy's bytes and mode before its
        edit, and whether neighbours were added."""
        path = self.base / 'conflicts.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def note(self, **values):
        (self.base / 'conflicts.json').write_text(json.dumps({**self.notes(), **values}))

    def armed(self, kind, at):
        """A checkpoint hook that applies `kind` when the child reaches `at` (the parent counts it as active)."""
        def inject(value):
            if value == at:
                self.apply(kind)
        inject.kind = kind
        return inject

    def clear(self):
        """Every conflict reverted; neighbours stay."""
        self.active = set()
        self.config.write_bytes(self.config.read_bytes().replace(CHANGED, OURS))
        path = self.skills / 'telegram-search' / 'SKILL.md'
        saved = self.notes().get('skill')
        if path.exists() and path.read_bytes() == b'mine' and saved is not None:
            path.write_bytes(bytes.fromhex(saved[0]))  # In place: the copy is again exactly the one we wrote.
            path.chmod(saved[1])
        self.note(skill=None)
        document = json.loads(self.desktop_config.read_text())
        value = document['mcpServers'].get('telegram_search')
        if value is not None and '--x' in value['args']:
            value['args'] = [a for a in value['args'] if a != '--x']
            self.desktop_config.write_text(json.dumps(document, indent=2) + '\n')
        document = self.claude().document() or {}
        value = document.get('mcpServers', {}).get('telegram_search')
        if value is not None and '--x' in value['args']:
            value['args'] = [a for a in value['args'] if a != '--x']
            self.claude().write_document(document)

    # --- the oracle --------------------------------------------------------------------------------------

    def served(self):
        """current.json, the runtime.toml pin, the plist and the loaded broker name one release (K6 aside)."""
        if not (self.runtime / 'current.json').exists():
            return ['no current.json']
        target = self.pointer()['release_root']
        launcher = target + '/launcher.py'
        version = json.loads(open(target + '/manifest/release.json').read())['package_version']
        out = []
        plist = plistlib.loads(self.plist.read_bytes())['ProgramArguments'] if self.plist.exists() else None
        loaded = self.launchctl.loaded
        if not self.serviced:
            # K6: such a first install writes no plist and starts no broker (its `connect` would); none appears.
            return ['a plist or broker after a first install over an absent root'] if plist or loaded else []
        if plist is None or plist[4] != launcher:
            out.append('plist does not name the pointer release')
        if loaded is None or loaded[4] != launcher:
            out.append('the loaded broker is not the pointer release' if loaded else 'no broker loaded')
        if ('expected_package_version = "%s"' % version).encode() not in (self.session_root / 'runtime.toml').read_bytes():
            out.append('the pin does not name the pointer release')
        return out

    def clients(self, result, *, exact=False):
        """Every client entry and skill copy is the pointer release's: K1 and K2 by the result's statuses, K3 by
        the active conflicts (none when `exact`)."""
        out = []
        target = self.pointer()['release_root']
        command, args = self.commands(target)
        active = set() if exact else self.active
        edited = args + ['--x']
        codex = self.config.read_bytes()
        if command.encode() not in codex or (CHANGED in codex and 'codex' not in active):
            out.append('codex entry is not the pointer release')
        if result.get('claude_code') not in ('failed', 'unavailable'):
            value = self.claude().entry
            if value != entry(command, args) and not ('claude' in active and value == entry(command, edited)):
                out.append('claude code entry is not the pointer release (%s)' % result.get('claude_code'))
        if result.get('claude_desktop') not in ('failed', 'unavailable'):
            value = json.loads(self.desktop_config.read_text())['mcpServers'].get('telegram_search')
            if value != {'command': command, 'args': args} and not (
                    'desktop' in active and value == {'command': command, 'args': edited}):
                out.append('claude desktop entry is not the pointer release (%s)' % result.get('claude_desktop'))
        for name in ('telegram-search', 'telegram-setup'):
            path = self.skills / name / 'SKILL.md'
            want = open(target + '/skills/' + name + '/SKILL.md', 'rb').read()
            have = path.read_bytes() if path.exists() else None
            if have != want and not ('skill' in active and name == 'telegram-search' and have == b'mine'):
                out.append('skill %s is not the pointer release' % name)
        return out

    def gone(self, result):
        """Nothing of ours is left after an uninstall; a `kept` part only by K4 and K5; neighbours stay."""
        kept = {k for k in ('codex', 'claude_code', 'claude_desktop') if result.get(k) == 'kept'}
        kept |= {k for k, v in result.get('skills', {}).items() if v == 'kept'}
        decided = {KEPT[k] for k in self.decided if k in KEPT} | self.lost
        out = ['%s kept without its conflict' % k for k in sorted(kept - decided)]
        allowed = kept | (decided if result.get('installation') == 'absent' else set())
        if self.runtime.exists() or self.plist.exists() or self.launchctl.loaded is not None:
            out.append('root, plist or broker left')
        codex = self.config.read_bytes()
        if b'telegram_search' in codex and 'codex' not in allowed:
            out.append('codex entry left')
        if not codex.startswith(NEIGHBOUR_CODEX):
            out.append('codex neighbours lost')
        if 'telegram_search' in (self.claude().document() or {}).get('mcpServers', {}) and 'claude_code' not in allowed:
            out.append('claude code entry left')
        servers = json.loads(self.desktop_config.read_text())['mcpServers']
        if 'telegram_search' in servers and 'claude_desktop' not in allowed:
            out.append('claude desktop entry left')
        if servers.get('weather') != {'command': '/opt/w', 'args': []}:
            out.append('claude desktop neighbour lost')
        if self.notes().get('neighboured') and (WEATHER not in codex or servers.get('calendar') != CALENDAR
                                 or 'calendar' not in (self.claude().document() or {}).get('mcpServers', {})):
            out.append('an added neighbour lost')
        for base in (self.skills, self.claude_skills):
            for name in ('telegram-search', 'telegram-setup'):
                if (base / name).exists() and name not in allowed and 'claude-' + name not in allowed:
                    out.append('skill copy left: %s' % (base / name).relative_to(self.base))
        return out

    def newest(self, number=False):
        """The newest change journal (with its number), or None."""
        transactions = self.runtime / 'transactions'
        changes = sorted((int(p.name[7:]), p) for p in transactions.iterdir()
                         if p.name.startswith('change_')) if transactions.exists() else []
        if not changes:
            return (None, None) if number else None
        journal = json.loads((changes[-1][1] / 'journal.json').read_text())
        return (changes[-1][0], journal) if number else journal

    def owners(self):
        """K7, from each client outcome a change records (finished, or parked by a late refusal), attributed to the
        run that recorded it: `registered` makes the client owned again, `failed` with its conflict active not."""
        number, journal = self.newest(number=True)
        if journal is None or journal.get('kind') == 'uninstall':
            return
        for key, kind in (('claude_code', 'claude'), ('claude_desktop', 'desktop')):
            status = (journal['publications'].get(key) or [{}])[0].get('status')
            if status is None or (number, key) in self.recorded:
                continue
            self.recorded.add((number, key))
            if status == 'registered':
                self.lost.discard(key)
            elif status == 'failed' and kind in self.during:
                self.lost.add(key)

    def pending(self):
        """The unfinished action the newest change journal names, or None."""
        journal = self.newest()
        if journal is None or journal.get('stage') == 'finished':
            return None
        if journal['kind'] == 'upgrade':
            return ('upgrade', next(k for k, p in self.packets.items() if p[3] == journal['index_sha256']))
        return (journal['kind'],)

    def unfinished(self):
        """The run that finishes the unfinished action: a change from its journal, or a first install (its staged
        root, or its published root short of `complete`) from its intent; None when nothing is unfinished."""
        change = self.pending()
        if change is not None:
            return change
        intent = self.home / '.runtime.install-intent.json'
        if not intent.exists():
            return None
        module = self.module()
        if (module._install_pending(self.runtime) if self.runtime.exists()
                else module._staged_ours(json.loads(intent.read_text()))):
            return self.named('install')
        return None

    def other(self, rng):
        """A change that stops the broker: an upgrade to the kit not installed, or a rollback."""
        if rng.random() < 0.3 or not (self.runtime / 'current.json').exists():
            return ('rollback',)
        installed = self.pointer()['index_sha256']
        return ('upgrade', 'C' if self.packets['B'][3] == installed else 'B')

    def named(self, action):
        """The run that repeats the unfinished `action` a `transaction_pending` refusal names."""
        if action == 'install':
            intent = json.loads((self.home / '.runtime.install-intent.json').read_text())
            return ('upgrade', next(k for k, p in self.packets.items() if p[3] == intent['identity']['index_sha256']))
        return self.pending() if action == 'upgrade' else (action,)

    def check(self, name, outcome):
        """Problems with the state right after `outcome` of `name`."""
        if outcome[0] == 'crash':
            return ['crash: ' + outcome[1]]
        if name[0] != 'uninstall' and self.runtime.exists():
            self.owners()  # After a kill too: the outcome may be recorded already.
        if outcome[0] == 'killed':
            return []
        out = []
        pending = outcome[0] == 'refused' and outcome[1] == 'transaction_pending'
        if self.temporaries() and not pending:
            out.append('temporaries left: %s' % self.temporaries())
        if outcome[0] == 'ok':
            if name[0] == 'uninstall':
                out += self.gone(outcome[1])
            else:
                out += self.served() + self.clients(outcome[1])
                self.last = outcome[1]
        elif not pending and name[0] != 'uninstall' and (self.runtime / 'current.json').exists():
            out += self.served()
        return out

    # --- one sequence ------------------------------------------------------------------------------------

    def sequence(self, seed):
        """(problems, log) of one seeded sequence."""
        rng = random.Random(seed)
        self.setUp()
        self.serviced = True
        self.active, self.decided, self.during, self.lost, self.recorded, self.last = set(), set(), set(), set(), set(), {}
        self.full()
        self.claude()
        self.packets = {'B': self.build('b', launcher=b'# second release\n', search=b'search-skill-2'),
                        'C': self.build('c', launcher=b'# third release\n', search=b'search-skill-3')}
        problems, log, last, armed = [], [], None, None

        def run(name, label='', **kwargs):
            outcome = self.run_child(name, **kwargs)
            log.append('%s%s -> %s' % (name, label, outcome[:3] if outcome[0] != 'ok' else ['ok', outcome[1].get('status')]))
            problems.extend('%s: %s' % (log[-1], p) for p in self.check(name, outcome))
            return outcome

        def login():
            if self.plist.exists() and self.launchctl.loaded is None:
                self.launchctl.load()
                self.trace['login'] += 1
                if self.runtime.exists() and self.pending():
                    self.trace['login during a change'] += 1
                log.append('login (loads %s)' % self.launchctl.loaded[4].rsplit('/', 2)[-2][:12])

        for _ in range(STEPS):
            choice = rng.choices(ACTIONS, WEIGHTS)[0]
            if choice == 'login':
                login()
                continue
            if choice == 'inject':
                kind = rng.choice(KINDS)
                if rng.random() < 0.5:
                    self.apply(kind)
                    self.trace['inject now'] += 1
                    log.append('inject now: ' + kind)
                else:
                    armed = (kind, rng.choice(CHECKPOINTS if rng.random() < 0.3 else POST_STOP))
                    self.active.add(kind)  # It may fire in the child: counted as active from now on.
                    self.trace['inject armed'] += 1
                    log.append('arm %s at %s' % armed)
                continue
            if choice == 'clear':
                self.clear()
                self.trace['clear'] += 1
                log.append('clear')
                continue
            if choice in ('repeat', 'burst') and last is None:
                choice = 'upgrade'
            if choice == 'upgrade':
                name = ('upgrade', rng.choice('BC'))
            elif choice in ('rollback', 'uninstall'):
                name = (choice,)
            elif choice == 'kill':
                name = rng.choice([('upgrade', rng.choice('BC')), last or ('rollback',), ('rollback',), ('uninstall',),
                                   last or ('uninstall',)])
            elif choice == 'burst':
                name = (self.runtime.exists() and self.pending()) or self.other(rng)
            else:
                name = last
            self.trace[choice] += 1
            if choice == 'burst':
                for _ in range(rng.randint(*BURST)):
                    # The unfinished change again, else the next one: a finished change's repeat stops nothing.
                    name = (self.runtime.exists() and self.pending()) or self.other(rng)
                    self.active.add('codex')
                    at = rng.choice(LATE if rng.random() < 0.7 else POST_STOP)
                    outcome = run(name, ' burst, codex at %s' % at, inject=self.armed('codex', at))
                    self.clear()
                    if outcome[0] == 'ok' and name[0] == 'uninstall':
                        return problems, log
                last = name
                continue
            inject = self.armed(*armed) if armed is not None else None
            if armed is not None:
                self.active.add(armed[0])  # Armed before a clear, it still fires in this run.
            armed_at, armed = armed, None
            kill_at = kill_effect = None
            if choice == 'kill':
                if rng.random() < 0.5:
                    kill_at = rng.choice(CHECKPOINTS)
                    self.trace['kill at a checkpoint'] += 1
                else:
                    kill_effect = rng.randint(1, KILL_EFFECTS)
                    self.trace['kill after an effect'] += 1
            label = (' kill at %s' % kill_at if kill_at else ' kill after effect %d' % kill_effect if kill_effect else '')
            label += ' + armed %s at %s' % armed_at if armed_at else ''
            outcome = run(name, label, kill_at=kill_at, kill_effect=kill_effect, inject=inject)
            last = name
            if outcome[0] == 'killed' and rng.random() < 0.5:
                login()  # A crash is often a reboot: launchd then loads the plist on disk.
            if outcome[0] == 'ok' and name[0] == 'uninstall':
                return problems, log
        # Liveness: every conflict cleared; the repeat (as the seed decides) and uninstall must finish.
        self.clear()
        log.append('-- conflicts cleared')
        if rng.random() < 0.5 and self.runtime.exists():
            for _ in range(4):
                name = self.unfinished()
                if name is None:
                    break
                outcome = run(name)
                if outcome[0] == 'refused':
                    problems.append('the repeat refused with every conflict cleared: %s' % outcome[1])
            else:
                problems.append('the repeat never finished')
            if (self.runtime / 'current.json').exists() and self.unfinished() is None:
                problems.extend('after the repeat: %s' % p for p in self.served() + self.clients(self.last, exact=True))
        for _ in range(6):
            outcome = run(('uninstall',))
            if outcome[0] == 'ok':
                return problems, log
            if outcome[0] == 'refused' and outcome[1] == 'transaction_pending':
                again = run(self.named(outcome[2]))
                if again[0] == 'refused':
                    problems.append('the named repeat refused with every conflict cleared: %s' % again[1])
                    return problems, log
            else:
                problems.append('uninstall refused with every conflict cleared: %s' % outcome[1:])
                return problems, log
        problems.append('uninstall never finished')
        return problems, log

    def share(self, seeds):
        """One worker's sequences: {'found': [...], 'trace': {...}, 'seen': [...]}."""
        self.trace, self.seen, found = collections.Counter(), set(), []
        for seed in seeds:
            try:
                problems, log = self.sequence(seed)
            finally:
                self.temp.cleanup()
            if problems:
                found.append('seed %d:\n  %s\n log:\n  %s' % (seed, '\n  '.join(problems), '\n  '.join(log)))
        return {'found': found, 'trace': dict(self.trace), 'seen': sorted(self.seen)}

    def test_seeded_sequences_end_in_one_release_or_nothing(self):
        """The fixed seeds split across forked workers (each sequence has its own disposable base)."""
        workers = []
        for index in range(WORKERS):
            read, write = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read)
                try:
                    value = self.share(SEEDS[index::WORKERS])
                except BaseException as error:
                    value = {'found': ['worker %d raised %r' % (index, error)], 'trace': {}, 'seen': []}
                try:
                    with os.fdopen(write, 'wb') as stream:
                        stream.write(json.dumps(value).encode())
                finally:
                    os._exit(0)
            os.close(write)
            workers.append((pid, read))
        found, trace, seen = [], collections.Counter(), set()
        for pid, read in workers:
            with os.fdopen(read, 'rb') as stream:
                data = stream.read()
            _, status = os.waitpid(pid, 0)
            if os.waitstatus_to_exitcode(status) != 0 or not data:
                found.append('a worker failed')
                continue
            value = json.loads(data)
            found += value['found']
            trace.update(value['trace'])
            seen |= set(value['seen'])
        self.assertEqual(found, [], '\n\n'.join(found))
        # Not vacuous: every action ran, kills of both kinds, late refusals, a login during an unfinished change.
        for what in ('upgrade', 'repeat', 'rollback', 'uninstall', 'inject now', 'inject armed', 'clear',
                     'kill at a checkpoint', 'kill after an effect', 'burst', 'login', 'login during a change',
                     'killed', 'refused:target_conflict', 'refused:transaction_pending', 'ok'):
            self.assertGreater(trace[what], 0, (what, dict(trace)))
        # Kills and armed conflicts can reach every window: each checkpoint any run passed is listed.
        self.assertEqual(sorted(seen - set(CHECKPOINTS)), [])


if __name__ == '__main__':
    unittest.main()
