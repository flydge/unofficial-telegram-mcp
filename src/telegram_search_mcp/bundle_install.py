"""Owned installation transactions: first install, upgrade, rollback and uninstall.

Each is a bounded journaled transaction under the installer lease. Checkpoints observe durable
mutation windows; a rerun resumes from the journal without repeating an effect. Only objects the
receipt or journal binds are ever replaced or removed; the Telegram session, enrollment, managed
key and Keychain are never written.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import ctypes
import shlex
import plistlib
import subprocess
import tempfile
import tomllib
import uuid

from .bundle_paths import (ensure_owned_private_directory, open_owned_directory,
                           open_owned_file, validate_owned_path, object_binding, require_object_binding, RuntimePathError)
from .bundle_runtime import (validate_tree, require_platform, read, unique_json, digest, BundleError,
                             CLAUDE_CODE_MODES, CLAUDE_MODE_STATUSES, CLAUDE_SKILLS_STATUSES, CLAUDE_DESKTOP_STATUSES,
                             RECEIPT_KEYS, ADOPTION_CLIENTS, MAX_BACKUP_BYTES, check_receipt_v2, receipt_transaction,
                             transaction_name, ADOPTED_NAME, MAX_ADOPTED, check_adopted)
from .session_owner import SessionOwnerLease, OwnerLeaseError
from . import claude_desktop_registration, claude_registration
from .codex_registration import (Registration, Receipt, ConfigConflict, read_config,
    merge_registration, rollback_registration, adopt_registration, owned_subtree, discard_temporary,
    commit_transaction, new_temporary, is_temporary, _atomic_write, _open_parent, _read_at, MAX_CONFIG_BYTES)


# The closed set of unfinished actions a `transaction_pending` refusal names: an unfinished first install, or an
# unfinished change.
PENDING_ACTIONS = ('install', 'upgrade', 'rollback', 'uninstall')


class InstallError(ValueError):
    """An integrity/conflict refusal; inputs and neighboring config stay redacted.

    `pending_action` (only with `transaction_pending`) names the unfinished change from PENDING_ACTIONS that must
    be retried first; anything else is dropped.
    """

    def __init__(self, *args, pending_action=None):
        super().__init__(*args)
        self.pending_action = (pending_action if args == ('transaction_pending',) and type(pending_action) is str
                               and pending_action in PENDING_ACTIONS else None)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',',':')) + '\n').encode()


def write(path, data, *, expected=None, existed=None, maximum=65536):
    _atomic_write(path, data, maximum, expected=expected, require_existing=existed)


def create(path, data, *, maximum=65536):
    write(path, data, expected=b'', existed=False, maximum=maximum)


def _config(path):
    validate_owned_path(path, regular=True)
    if not path.parent.exists(): return b'', False
    return read_config(path), path.exists()


def _skill_hash(directory):
    with open_owned_directory(directory, private=True): pass
    if set(p.name for p in directory.iterdir()) != {'SKILL.md'}:
        raise InstallError('skill_conflict')
    return digest(read(directory / 'SKILL.md', mode=0o600))


# macOS SDK sys/stdio.h: RENAME_SWAP 0x00000002, RENAME_EXCL 0x00000004.
RENAME_SWAP, RENAME_EXCL = 2, 4


def _renameatx(source, destination, flags):
    with open_owned_directory(source.parent) as a, open_owned_directory(destination.parent) as b:
        if flags == RENAME_EXCL and (destination.exists() or destination.is_symlink()): raise InstallError('target_conflict')
        # Held parents. RENAME_EXCL: a no-replace rename, refusing even a concurrently created empty
        # foreign directory. RENAME_SWAP: both names exist and exchange atomically.
        if sys.platform == 'darwin':
            api=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
            rename=api.renameatx_np
            rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
            rename.restype=ctypes.c_int
            if rename(a,os.fsencode(source.name),b,os.fsencode(destination.name),flags)!=0:
                raise InstallError('target_conflict')
        elif flags == RENAME_EXCL:
            # Production admission only supports macOS; this fallback is for
            # inert portable tests under the same cooperative lease.
            os.rename(source.name, destination.name, src_dir_fd=a, dst_dir_fd=b)
        else:
            spare='.swap-'+uuid.uuid4().hex
            os.rename(destination.name, spare, src_dir_fd=b, dst_dir_fd=b)
            os.rename(source.name, destination.name, src_dir_fd=a, dst_dir_fd=b)
            os.rename(spare, source.name, src_dir_fd=b, dst_dir_fd=a)
        os.fsync(a); os.fsync(b)


def _rename(source, destination):
    _renameatx(source, destination, RENAME_EXCL)


def _swap(source, destination):
    _renameatx(source, destination, RENAME_SWAP)


def _copy_release(source, destination, index, index_bytes, pin):
    ensure_owned_private_directory(destination)
    for record in index.files:
        target = destination / record.path
        ensure_owned_private_directory(target.parent)
        with open_owned_file(source / record.path) as incoming, os.fdopen(os.open(target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500 if record.executable else 0o400), 'wb') as outgoing:
            # The source tree was authenticated before this streamed bulk copy.
            # Rehash both source and destination before publishing a release.
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush(); os.fsync(outgoing.fileno())
            os.fchmod(outgoing.fileno(), 0o500 if record.executable else 0o400)
    path = destination / 'bundle-index.json'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400), 'wb') as out:
        out.write(index_bytes); out.flush(); os.fsync(out.fileno()); os.fchmod(out.fileno(), 0o400)
    validate_tree(destination, path, pin, installed=True)


def _service_payload(installed, label):
    from .launch_agent import launch_agent_payload
    expected=launch_agent_payload(python_executable=installed/'python/bin/python3')
    expected.update(Label=label,ProgramArguments=[str(installed/'python/bin/python3'),'-I','-S','-B',str(installed/'launcher.py'),'broker'],EnvironmentVariables={})
    return expected


def _plist_bytes(payload):
    return plistlib.dumps(payload, sort_keys=True)


def _check_existing_service(plist, installed, label):
    """Repeat observes only an exact owned disk definition; never launchctl.

    Loaded-service/Ready authority remains the native setup handoff gate. This
    read-only repeat check never adopts, stops or claims readiness for a process.
    """
    if not plist.exists():return
    expected=_service_payload(installed,label)
    try:
        actual=plistlib.loads(read(plist,1024*1024,mode=0o600))
        if actual!=expected:raise InstallError('service_conflict')
    except (ValueError,TypeError,plistlib.InvalidFileException,BundleError):
        raise InstallError('service_conflict') from None


def _fresh_packet(tx, label):
    """A fresh packet; an earlier one is never adopted (opening the journal removed each unbound one of ours)."""
    for attempt in range(8):
        path=tx/(label+'-'+str(attempt))
        if path.exists() or path.is_symlink():continue
        ensure_owned_private_directory(path)
        return path
    raise InstallError('recovery_limit')


def _sync_directories(root):
    for parent,dirs,_ in os.walk(root,followlinks=False):
        with open_owned_directory(Path(parent),private=True) as fd:os.fsync(fd)


class _NotOurs(Exception):
    """An entry of an abandoned tree is not provably ours: nothing of it is removed."""


def _check_owned(fd, allowed, budget, prefix=()):
    """Every entry under the held directory `fd` is an owner-only directory, or an owner-only single-link regular
    file, whose relative path `allowed(parts, is_directory)` admits; at most `budget[0]` entries in all."""
    names=os.listdir(fd)
    budget[0]-=len(names)
    if budget[0]<0:raise _NotOurs()
    for name in names:
        path=prefix+(name,)
        m=os.stat(name,dir_fd=fd,follow_symlinks=False)
        directory=stat.S_ISDIR(m.st_mode)
        if (m.st_uid!=os.getuid() or stat.S_IMODE(m.st_mode)&0o077 or len(path)>32
                or not (directory or stat.S_ISREG(m.st_mode) and m.st_nlink==1) or not allowed(path,directory)):
            raise _NotOurs()
        if directory:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                if (os.fstat(child).st_dev,os.fstat(child).st_ino)!=(m.st_dev,m.st_ino):raise _NotOurs()
                _check_owned(child,allowed,budget,path)
            finally:os.close(child)


def _remove_abandoned(parent, name, allowed, budget=64, *, empty=True):
    """Remove `parent/name`, an abandoned unpublished directory of ours, only when it and everything in it is
    provably ours (owner-only, single-link files, every path one we create; see `_check_owned`); True when
    removed. With `empty` False an empty directory proves nothing and stays. The caller holds the lock that
    excludes its writer, so it never races the creation."""
    with open_owned_directory(parent) as fd:
        m=os.stat(name,dir_fd=fd,follow_symlinks=False)
        if not stat.S_ISDIR(m.st_mode) or m.st_uid!=os.getuid() or stat.S_IMODE(m.st_mode)&0o077:return False
        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            if (os.fstat(child).st_dev,os.fstat(child).st_ino)!=(m.st_dev,m.st_ino):return False
            if not empty and not os.listdir(child):return False
            _check_owned(child,allowed,[budget])
        except _NotOurs:return False
        finally:os.close(child)
        shutil.rmtree(name,dir_fd=fd)
        os.fsync(fd)
        return True


_INITIAL_FILES = frozenset({'config.preimage','config.candidate','registration.json','journal.json',
                            *('adopted-'+c for c in ADOPTION_CLIENTS)})
# The intent is written inside the unpublished staged root; its no-replace rename out is its publication.
_STAGED_INTENT = 'install-intent.json'


def _initial_entry(path, directory):
    """Whether `path` (parts relative to an unpublished staged root) is one `_initialize` creates."""
    if len(path)==1:
        if path[0] in ('transactions','releases'):return directory
        return not directory and (path[0] in ('.install.lock',_STAGED_INTENT) or is_temporary(path[0]))
    if path[0]!='transactions' or not path[1].startswith('install_') or not transaction_name(path[1]):return False
    if len(path)==2:return directory
    return len(path)==3 and not directory and (path[2] in _INITIAL_FILES or is_temporary(path[2]))


@contextlib.contextmanager
def _parent_lock(parent):
    """An exclusive advisory lock on the root's parent directory: first-install staging and its cleanup never
    overlap another installer's. Busy refuses."""
    with open_owned_directory(parent) as fd:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise InstallError('installation_refused') from None
        yield


def _intent_path(root):
    return root.parent/('.'+root.name+'.install-intent.json')


def _staged_names(root):
    """The eight attempt names of a first install's staged root beside `root`, derived from the root's path: an
    empty one a killed attempt left (right after its mkdir) is provably ours by its name, a random name is not."""
    return tuple('.'+root.name+'-initial-'+hashlib.sha256(('%s\0%d'%(root,n)).encode()).hexdigest()[:8] for n in range(8))


def _intent_shape(value, root):
    """Whether `value` is the closed shape `_initialize` writes as the intent for `root`."""
    staged=value.get('staged_root') if type(value) is dict else None
    return (type(value) is dict and set(value)=={'identity','staged_root','object'} and type(value['identity']) is dict
            and value['identity'].get('installation_root')==str(root) and type(staged) is str
            and Path(staged).parent==root.parent
            and re.fullmatch(re.escape('.'+root.name+'-initial-')+r'[a-z0-9_]{8}',Path(staged).name) is not None
            and type(value['object']) is dict and value['object'].get('kind')=='directory')


def _read_intent(root):
    """The install intent beside `root`, or None when there is none; any other shape refuses."""
    path=_intent_path(root)
    if not os.path.lexists(path):return None
    intent=unique_json(read(path,mode=0o600))
    if not _intent_shape(intent,root):raise InstallError('initialization_conflict')
    return intent


def _intent_temporary(root, name):
    """Whether the parent's `name` is an earlier release's intent temporary for `root`: its bytes parse as that intent."""
    try:return _intent_shape(unique_json(read(root.parent/name)),root)
    except (BundleError,ValueError,TypeError,UnicodeError):return False


def _retired_names(root, intent):
    """Names beside `root` of the root an uninstall retired: our closed name holding the exact directory the intent
    binds (a directory's inode, and so its binding, survives the retiring rename). At most one."""
    retired=re.compile(re.escape('.'+root.name+'-removed-')+r'[0-9a-f]{32}\Z')
    with open_owned_directory(root.parent) as fd:names=[n for n in os.listdir(fd) if retired.fullmatch(n)]
    out=[]
    for name in names:
        try:require_object_binding(root.parent/name,intent['object'])
        except RuntimePathError:continue
        out.append(name)
    return out


def _staged_ours(intent):
    """Whether the intent's recorded staged root still is the exact directory the intent binds: a first install not
    yet published. A lookalike under that name is not (the published root holds the bound inode)."""
    if not os.path.lexists(intent['staged_root']):return False
    try:require_object_binding(Path(intent['staged_root']),intent['object'])
    except RuntimePathError:return False
    return True


def _absent_root(root, intent):
    """With `root` absent beside its intent: `install` (the bound staged root exists: a first install not yet
    published), `uninstall` (the retired root exists: an uninstall past its last journaled step, the retiring rename,
    whose sweep did not finish) or None (all gone: the intent is the last leftover of a finished uninstall)."""
    if _staged_ours(intent):return 'install'
    return 'uninstall' if _retired_names(root,intent) else None


def _unlink_intent(root):
    with open_owned_directory(root.parent) as fd:
        os.unlink(_intent_path(root).name,dir_fd=fd);os.fsync(fd)


def _finish_retired(root):
    """Under the parent lock, with `root` absent: finish what an uninstall left beside it, only what is provably ours,
    and return None; or return `install`, removing nothing, for an unfinished first install (the bound staged root).

    The retiring rename is the uninstall's last journaled step, so every client removal is done: the retired root
    (whole, or partly removed by a killed sweep) is removed only under our closed name and only while it is the exact
    directory the install intent binds (its inode keeps the binding through the rename and the removal of its
    entries); a killed first install's unbound staged roots go by `_sweep_initial`'s rules; the intent goes last, so a
    kill anywhere leaves the proof for the retry. Anything else, a lookalike included, stays.
    """
    intent=_read_intent(root)
    if intent is not None and _absent_root(root,intent)=='install':return 'install'
    for name in _retired_names(root,intent) if intent is not None else ():
        with open_owned_directory(root.parent) as fd:shutil.rmtree(name,dir_fd=fd);os.fsync(fd)
    _sweep_initial(root,None)
    if intent is not None:_unlink_intent(root)
    return None


def _stale_intent(root):
    """Before a first install over an absent `root`, under the parent lock: an uninstall interrupted after its
    retiring rename (even partway through the retired root's removal) is finished and its intent removed, like an
    intent whose staged, published and retired roots are all gone; a bound staged root keeps it for the resume."""
    with _parent_lock(root.parent):
        if not os.path.lexists(root):_finish_retired(root)


def _sweep_initial(root, keep):
    """Under the parent lock: remove what killed first-install attempts left beside `root`, only what is provably
    ours. An unpublished staged root other than `keep` goes when every entry in it is one `_initialize` creates;
    an empty one only under one of our derived names. An earlier release's intent temporary goes only when its
    bytes parse as an intent for this root. Lookalikes stay."""
    staged=re.compile(re.escape('.'+root.name+'-initial-')+r'[a-z0-9_]{8}\Z')
    temporary=re.compile(re.escape('.'+root.name+'-intent-')+r'[0-9a-f]{32}\.tmp\Z')
    names=_staged_names(root)
    with open_owned_directory(root.parent) as fd:
        for name in os.listdir(fd):
            if staged.fullmatch(name) and root.parent/name!=keep:
                _remove_abandoned(root.parent,name,_initial_entry,empty=name in names)
            elif temporary.fullmatch(name):
                m=os.stat(name,dir_fd=fd,follow_symlinks=False)
                if (stat.S_ISREG(m.st_mode) and m.st_uid==os.getuid() and m.st_nlink==1
                        and not stat.S_IMODE(m.st_mode)&0o077 and m.st_size<=65536 and _intent_temporary(root,name)):
                    os.unlink(name,dir_fd=fd);os.fsync(fd)


def _install_pending(root):
    """Whether the first install of the published `root` is unfinished: its journal short of `complete`."""
    found=[]
    with open_owned_directory(root/'transactions',private=True):pass
    for count,path in enumerate((root/'transactions').iterdir()):
        if count>4*MAX_CHANGES:raise InstallError('recovery_limit')
        if path.name.startswith('install_') and transaction_name(path.name):found.append(path)
    if len(found)!=1:return False  # Not one first install of ours: the transaction's own checks refuse.
    return unique_json(read(found[0]/'journal.json',mode=0o600)).get('stage')!='complete'


def _initialize(root, identity, change, existed, checkpoint, adopted=None):
    """Publish a complete owned root once; incomplete preparations stay outside.

    The adjacent immutable intent precedes final root mutation and records its
    exact staged inode. A retry removes the unbound staged roots of killed
    attempts (only exactly what this function creates) and makes a fresh one
    instead of repairing foreign or ambiguous metadata; the recorded one is kept.
    With the root absent, `install` finished (and removed) any intent but an
    unfinished first install's before; one that appeared since refuses for the retry.
    Another identity over an unfinished first install refuses
    (`transaction_pending`, naming the action to retry).
    The supported root's parent must already exist safely.
    `adopted` maps a client to its exact `--adopt-legacy` backup bytes, kept in the transaction.
    """
    with open_owned_directory(root.parent):pass
    intent_path=_intent_path(root)
    with _parent_lock(root.parent):
        intent=_read_intent(root)
        if intent is not None:
            staged=Path(intent['staged_root'])
            if not os.path.lexists(root) and not _staged_ours(intent):
                # A concurrent uninstall's leftover since `_stale_intent`: the retry finishes it.
                raise InstallError('installation_refused')
            if intent['identity']!=identity:
                if not os.path.lexists(root) or _install_pending(root):
                    raise InstallError('transaction_pending',pending_action='install')
                raise InstallError('initialization_conflict')
            _sweep_initial(root,None if root.exists() else staged)
            if root.exists():
                require_object_binding(root,intent['object'])
                return
            require_object_binding(staged,intent['object'])
            _rename(staged,root);checkpoint('published:root');return
        if root.exists() or root.is_symlink():raise InstallError('runtime_conflict')
        _sweep_initial(root,None)
        # A name the sweep kept is not ours: the next one is used, within the bounded attempts.
        name=next((n for n in _staged_names(root) if not os.path.lexists(root.parent/n)),None)
        if name is None:raise InstallError('recovery_limit')
        with open_owned_directory(root.parent) as fd:os.mkdir(name,0o700,dir_fd=fd)
        staged=root.parent/name
        checkpoint('created:initial_root')
        transactions=staged/'transactions';ensure_owned_private_directory(transactions)
        ensure_owned_private_directory(staged/'releases')
        tx=transactions/('install_'+identity['index_sha256'][:24]);ensure_owned_private_directory(tx)
        create(staged/'.install.lock',b'')
        create(tx/'config.preimage',change.source,maximum=MAX_CONFIG_BYTES)
        create(tx/'config.candidate',change.candidate,maximum=MAX_CONFIG_BYTES)
        adopted=adopted or {}
        # An adopted foreign Codex entry is backed up beside the transaction, never kept as a receipt preimage.
        pre=(None,None) if 'codex' in adopted else (change.pre_subtree_hash,change.pre_subtree)
        ownership=Receipt('install_'+identity['index_sha256'][:24],'first_install',pre[0],change.post_subtree_hash,pre[1])
        create(tx/'registration.json',ownership.to_bytes())
        for client,data in adopted.items():create(tx/('adopted-'+client),data,maximum=MAX_BACKUP_BYTES)
        records={'adopted':[{c:{'sha256':digest(d),'bytes':len(d)} for c,d in adopted.items()}]} if adopted else {}
        journal={**identity,'stage':'prepared','config_existed':existed,'publications':records}
        create(tx/'journal.json',encoded(journal))
        _sync_directories(staged)
        intent={'identity':identity,'staged_root':str(staged),'object':object_binding(staged,directory=True)}
        # Fully flushed inside the unbound staged root, then published beside the root by a no-replace rename:
        # an interrupted write leaves nothing outside the staged root, which a retry removes.
        create(staged/_STAGED_INTENT,encoded(intent))
        _rename(staged/_STAGED_INTENT,intent_path)
        checkpoint('recorded:root')
        _rename(staged,root)
        checkpoint('published:root')


def _file_object(value):
    return {'kind':'file',**{k:value[k] for k in ('dev','ino','uid','mode','bytes')},'nlink':1}


def _sweep_temporaries(directory):
    """Unlink the temporaries killed writes left in an owner-only transaction directory (held under the lease)."""
    with open_owned_directory(directory,private=True) as fd:
        names=os.listdir(fd)
        if len(names)>4096:raise InstallError('recovery_limit')
        for name in names:
            if not is_temporary(name):continue
            metadata=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_uid==os.getuid():os.unlink(name,dir_fd=fd)
        os.fsync(fd)


# `_stage` packets: `<label>-<attempt>` in the transaction directory, label to journal record name.
_PACKET = re.compile(r'(?:(failed-release)|skill-packet-([a-z][a-z-]{0,63})|metadata-packet-([a-z][a-z_]{0,63}))-[0-7]\Z')


def _packet_entry(kind):
    """What `_stage` creates in a packet: a release tree, a skill directory or one metadata file (plus the
    temporaries a killed `create` leaves)."""
    if kind=='release':return lambda path,directory:True
    if kind=='skill':return lambda path,directory:len(path)==1 and not directory and (path[0]=='SKILL.md' or is_temporary(path[0]))
    return lambda path,directory:len(path)==1 and not directory and (path[0] in ('receipt.json','current.json',ADOPTED_NAME)
                                                                    or is_temporary(path[0]))


class _Journal:
    """One transaction's journal under the installer lease; each update is compare-before-replace.

    Opening it removes what a killed run left in the transaction directory: journal temporaries and every packet
    no journal record binds (a `_stage` killed before its binding). Neither ever counts toward a recovery bound.
    """

    def __init__(self, tx, lease, steps, checkpoint):
        self.tx,self.lease,self.steps,self.checkpoint=tx,lease,steps,checkpoint
        _sweep_temporaries(tx)
        self.value=unique_json(read(tx/'journal.json',mode=0o600))
        if type(self.value.get('publications')) is not dict or self.value.get('stage') not in steps:
            raise InstallError('transaction_conflict')
        self._sweep_packets()

    def _sweep_packets(self):
        with open_owned_directory(self.tx,private=True) as fd:names=os.listdir(fd)
        for name in names:
            match=_PACKET.fullmatch(name)
            if match is None:continue
            record=match.group(2) or match.group(3) or 'release'
            packet=self.tx/name
            if any(type(a.get('staged')) is str and packet in (Path(a['staged']),Path(a['staged']).parent)
                   for a in self.attempts(record)):continue
            kind='release' if match.group(1) else 'skill' if match.group(2) else 'metadata'
            _remove_abandoned(self.tx,name,_packet_entry(kind),1<<20 if kind=='release' else 64)

    def __getitem__(self, key):
        return self.value[key]

    def update(self, stage=None, publication=None, single=None, amend=None):
        self.lease.check();next_value={**self.value}
        if stage is not None:next_value['stage']=stage
        if publication is not None:
            name,proof=publication
            attempts=self.value['publications'].get(name,[])
            if type(attempts) is not list or len(attempts)>=8:raise InstallError('recovery_limit')
            next_value['publications']={**self.value['publications'],name:[*attempts,proof]}
        if amend is not None:
            # The last attempt's journaled temporary gains its flushed inode; the attempt count stays.
            name,proof=amend
            attempts=self.value['publications'].get(name)
            if type(attempts) is not list or not attempts or attempts[-1]!={'temporary':proof['temporary']}:
                raise InstallError('transaction_conflict')
            next_value['publications']={**self.value['publications'],name:[*attempts[:-1],proof]}
        if single is not None:
            # Single records are written once per transaction and never count toward a recovery cap.
            name,value=single
            if name in self.value['publications']:raise InstallError('transaction_conflict')
            next_value['publications']={**self.value['publications'],name:[value]}
        write(self.tx/'journal.json',encoded(next_value),expected=encoded(self.value),existed=True)
        self.value=next_value

    def reached(self, value):
        return self.steps.index(self.value['stage'])>=self.steps.index(value)

    def advance(self, value):
        if not self.reached(value):self.update(value)

    def proof(self, name):
        attempts=self.attempts(name)
        return attempts[-1] if attempts else None

    def attempts(self, name):
        attempts=self.value['publications'].get(name,[])
        if type(attempts) is not list or len(attempts)>8 or any(type(a) is not dict for a in attempts):
            raise InstallError('transaction_conflict')
        return attempts

    def discard(self, name, path, discard=discard_temporary):
        """Unlink every temporary a killed attempt of `name` left beside `path` (see `attempt`)."""
        attempts=self.attempts(name)
        if attempts and os.path.lexists(Path(path).parent):
            for record in attempts:discard(path,record)

    def attempt(self, name, write):
        """One journaled atomic write: its temporary's name is recorded before `write(temporary, admit)`
        creates it, and `admit` adds the flushed inode before the replace. A resume finds either record."""
        temporary=new_temporary()
        self.update(publication=(name,{'temporary':temporary}))
        def admit(value):
            self.update(amend=(name,{'object':_file_object(value),'temporary':temporary}))
            self.checkpoint('recorded:'+name)
        write(temporary,admit)

    def recorded(self, name, publications=None):
        records=(self.value['publications'] if publications is None else publications).get(name)
        if records is None:return None
        if type(records) is not list or len(records)!=1 or type(records[0]) is not dict:raise InstallError('transaction_conflict')
        return records[0]

    def _stage(self, name, target, build, validate, directory, child):
        bound=self.proof(name)
        if bound is None:
            packet=_fresh_packet(self.tx,'failed-release' if name=='release' else 'skill-packet-'+name if child else 'metadata-packet-'+name)
            self.checkpoint('created:packet:'+name)
            staged=packet if directory else packet/target.name
            build(staged);validate(staged);_sync_directories(packet)
            bound={'staged':str(staged),'object':object_binding(staged,directory=directory)}
            if child:bound['child']=object_binding(staged/'SKILL.md')
            self.update(publication=(name,bound));self.checkpoint('recorded:'+name)
        staged=Path(bound['staged'])
        if not staged.is_relative_to(self.tx):raise InstallError('transaction_conflict')
        require_object_binding(staged,bound['object'])
        if child:require_object_binding(staged/'SKILL.md',bound['child'])
        validate(staged)
        return bound,staged

    def publish(self, name, target, build, validate, *, directory=False, child=False):
        """Create an absent target from a bound staged object; an existing target must be that object."""
        bound=self.proof(name)
        if target.exists() or target.is_symlink():
            if bound is None:raise InstallError('foreign_'+name)
            require_object_binding(target,bound['object'])
            if child:require_object_binding(target/'SKILL.md',bound['child'])
            validate(target);return
        bound,staged=self._stage(name,target,build,validate,directory,child)
        _rename(staged,target);self.checkpoint('published:'+name)
        require_object_binding(target,bound['object']);validate(target)

    def replace(self, name, target, build, validate, replaceable, *, directory=False, child=False):
        """Atomically exchange `target` (accepted by `replaceable`) with a bound staged object.

        A missing target is created; the replaced object stays in the transaction packet.
        """
        bound=self.proof(name)
        if bound is not None and (target.exists() or target.is_symlink()):
            try:
                require_object_binding(target,bound['object'])
                if child:require_object_binding(target/'SKILL.md',bound['child'])
                validate(target);return
            except RuntimePathError:pass
        bound,staged=self._stage(name,target,build,validate,directory,child)
        if target.exists() or target.is_symlink():
            replaceable(target);_swap(staged,target)
        else:_rename(staged,target)
        self.checkpoint('published:'+name)
        require_object_binding(target,bound['object'])
        if child:require_object_binding(target/'SKILL.md',bound['child'])
        validate(target)

    def write(self, name, path, data, expected, *, existed, maximum, max_mode=0o600, preserve_mode=False):
        """Journaled compare-before-replace of an external file; a killed attempt's temporary is discarded."""
        self.discard(name,path)
        bound=self.proof(name)
        if bound is not None and 'object' in bound:
            try:
                require_object_binding(path,bound['object']);return
            except RuntimePathError:pass
        current,exists=_read_file(path,maximum,max_mode)
        if exists and current==data:return
        if current!=expected or exists!=existed:raise InstallError('target_conflict')
        self.attempt(name,lambda temporary,admit:_atomic_write(path,data,maximum,expected=expected,require_existing=existed,
                     before_replace=admit,max_mode=max_mode,preserve_mode=preserve_mode,temporary=temporary))
        self.checkpoint('published:'+name)


def _read_file(path, maximum, max_mode=0o600):
    """(bytes, existed) of an owner-only file through held no-follow parents; raises on unsafe metadata."""
    fd=_open_parent(path)
    try:return _read_at(fd,Path(path).name,maximum,max_mode)
    finally:os.close(fd)


def _desktop_discard(j, home):
    """Our journaled Claude Desktop temporaries (a write, a best-effort restore) a killed attempt left."""
    for name in ('claude_desktop_write','claude_desktop_restore'):
        j.discard(name,claude_desktop_registration.config_path(home),
                  lambda _,record:claude_desktop_registration.discard_temporary(home,record))


def _desktop_write(j, home, plan):
    """One journaled compare-before-replace attempt; the confirming read decides its outcome."""
    def write(temporary, admit):
        try:claude_desktop_registration.write(home,plan,admit,temporary)
        except ConfigConflict:pass
    j.attempt('claude_desktop_write',write)


def _desktop_unchanged(j, add, plan):
    """Whether the config is still byte-identical to the write-ahead record's preimage (the same write is safe
    again), within the bounded attempts."""
    return (plan is not None and type(add) is dict and add.get('existed')==plan.existed
            and add.get('source_sha256')==plan.source_sha256 and add.get('candidate_sha256')==plan.candidate_sha256
            and len(j.attempts('claude_desktop_write'))<8)


CLAUDE_SKILLS = CLAUDE_SKILLS_STATUSES
SKILL_NAMES = ('telegram-search', 'telegram-setup', 'claude-telegram-search', 'claude-telegram-setup')
_MANUAL = ('failed', 'unavailable', 'not_found')
_DESKTOP_MANUAL = ('failed', 'unavailable')
_REFUSALS = (BundleError, RuntimePathError, ConfigConflict, OwnerLeaseError, OSError, ValueError, KeyError, TypeError)
_INSTALL_STEPS = ['prepared','release_pending','release','registration_pending','registration','telegram-search_pending','telegram-search',
                  'telegram-setup_pending','telegram-setup','claude-telegram-search_pending','claude-telegram-search',
                  'claude-telegram-setup_pending','claude-telegram-setup','claude_code_pending','claude_code',
                  'claude_desktop_pending','claude_desktop','service_pending','service','policy_pending','policy',
                  'receipt_pending','receipt','complete']
_CHANGE_STEPS = ['prepared','release_pending','release','registration_pending','registration',
                 *[s for n in SKILL_NAMES for s in (n+'_pending',n)],
                 'claude_code_pending','claude_code','claude_desktop_pending','claude_desktop','service_pending','service',
                 'policy_pending','policy','receipt_pending','receipt','pointer_pending','complete','finished']
_UNINSTALL_STEPS = ['prepared','service','registration','claude_code','claude_desktop','skills','retired']
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
MAX_POLICY_BYTES = 16384
MAX_PLIST_BYTES = 1024 * 1024
MAX_CHANGES = 999
# Run under the interpreter of the broker it asks: only that release's own client passes the
# broker's version handshake. Any client context may hold a draft or an unresolved send outcome.
_PENDING_PROBE = ("import sys\nsys.path[:0]=[p for p in sys.argv[1:2] if p]\n"
                  "from telegram_search_mcp.enrollment import SetupPaths\n"
                  "from telegram_search_mcp.setup_cli import _client\n"
                  "c=_client(SetupPaths(),5)\ntry:\n h=c.health()\n"
                  " print('none' if type(h.get('active_clients')) is int and h['active_clients']==0 else 'pending')\n"
                  "finally:\n c.close()\n")


def default_pending_sends(home, python, app):
    """'none', 'pending' or 'unknown' for the running broker; no socket means nothing is held in memory.

    `python` is the broker's own interpreter and `app` its bundled package path (None for a source
    venv). Bounded, no shell, explicit minimal environment; only a fixed token is read back.
    """
    from .config import _owner_paths
    session_root=_owner_paths(Path(home))[0]
    if not os.path.lexists(session_root/'run'/'broker.sock'):return 'none'
    if python is None:return 'unknown'
    try:
        done=subprocess.run([str(python),'-I',*(('-S',) if app else ()),'-B','-c',_PENDING_PROBE,str(app or '')],
                            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False,
                            timeout=20,cwd=str(home),env={'HOME':str(home),'PATH':SYSTEM_PATH,'LC_ALL':'C'})
    except (OSError,ValueError,subprocess.SubprocessError):
        return 'unknown'
    return {b'none\n':'none',b'pending\n':'pending'}.get(done.stdout,'unknown') if done.returncode==0 else 'unknown'


def _launchctl(runner, arguments, timeout=10):
    from .launch_agent import _run_launchctl
    try:return _run_launchctl(arguments,runner=runner,required=False,timeout=timeout)
    except (OSError,ValueError,subprocess.SubprocessError):raise InstallError('service_restart_failed') from None


def _live(runner, label):
    """The loaded registration text of the exact label, None when not loaded."""
    from .launch_agent import LAUNCHCTL
    state=_launchctl(runner,[LAUNCHCTL,'print','gui/%d/%s'%(os.getuid(),label)])
    if state.returncode==113:return None
    if state.returncode!=0 or type(state.stdout) is not str:raise InstallError('service_conflict')
    return state.stdout


def _bootout(runner, label):
    from .launch_agent import LAUNCHCTL, _await_retired, LaunchAgentError
    import time
    service='gui/%d/%s'%(os.getuid(),label)
    if _launchctl(runner,[LAUNCHCTL,'bootout',service]).returncode!=0:raise InstallError('service_restart_failed')
    try:_await_retired(service,runner=runner,sleeper=time.sleep)
    except (LaunchAgentError,OSError,subprocess.SubprocessError):raise InstallError('service_restart_failed') from None


def _bootstrap(runner, plist):
    from .launch_agent import LAUNCHCTL
    if _launchctl(runner,[LAUNCHCTL,'bootstrap','gui/%d'%os.getuid(),str(plist)]).returncode!=0:
        raise InstallError('service_restart_failed')


def _matches_live(text, plist, payload):
    from .launch_agent import _matching_live_registration
    return _matching_live_registration(text,plist,payload)


def _probe_python(payload):
    """(interpreter, bundled app path or None) for a plist's broker; (None, None) when unknown."""
    arguments=payload.get('ProgramArguments') if type(payload) is dict else None
    if type(arguments) is not list or not arguments or type(arguments[0]) is not str:return None,None
    python=Path(arguments[0])
    if not python.is_absolute() or '..' in python.parts:return None,None
    if arguments[1:4]==['-I','-S','-B'] and len(arguments)==6:return python,python.parent.parent.parent/'app'
    return python,None


class _Service:
    """The exact receipt-bound (or adopted) broker LaunchAgent: stop, rewrite, restart.

    `known` maps each acceptable on-disk definition kind to its payload; `adopted` is the sha256 of an
    `--adopt-legacy` plist backup. Only the exact label is ever booted out or bootstrapped.
    """

    def __init__(self, plist, label, target, *, known=(), adopted=None, runner, pending, home):
        self.plist,self.label,self.target=plist,label,target
        self.known=dict(known);self.adopted=adopted;self.runner=runner;self.pending=pending;self.home=home

    def kind(self):
        """('absent'|'to'|<known name>|'adopted', bytes, payload); anything else raises service_conflict."""
        validate_owned_path(self.plist,regular=True)
        if not self.plist.exists():return 'absent',b'',None
        try:
            data=read(self.plist,MAX_PLIST_BYTES,mode=0o600)
            payload=plistlib.loads(data)
        except (ValueError,TypeError,plistlib.InvalidFileException,BundleError):
            payload=None;data=None
        if payload is not None and self.target is not None and payload==self.target:return 'to',data,payload
        for name,value in self.known.items():
            if payload is not None and payload==value:return name,data,payload
        if data is not None and self.adopted is not None and digest(data)==self.adopted:return 'adopted',data,payload
        raise InstallError('service_conflict')

    def check(self):
        """Pre-mutation gate: a known definition, an exactly matching live registration and no pending send."""
        kind,_,payload=self.kind()
        live=_live(self.runner,self.label)
        if live is None:return kind,False
        if kind=='absent' or kind!='adopted' and not _matches_live(live,self.plist,payload):raise InstallError('service_conflict')
        self.gate(payload)
        return kind,True

    def gate(self, payload):
        python,app=_probe_python(payload)
        if self.pending(self.home,python,app)!='none':raise InstallError('pending_send_unresolved')

    def _observed(self, j):
        observed=j.recorded('service')
        if (observed is None or type(observed.get('loaded')) is not bool
                or observed.get('plist') not in ('absent','to','adopted',*self.known)):
            raise InstallError('transaction_conflict')
        return observed

    def _recheck(self):
        """True while the exact registration is still loaded and holds no pending send; False once it is gone."""
        live=_live(self.runner,self.label)
        if live is None:return False
        kind,_,payload=self.kind()
        if kind=='absent' or kind!='adopted' and not _matches_live(live,self.plist,payload):raise InstallError('service_conflict')
        self.gate(payload)
        return True

    def observe(self, j):
        """Before the first client write: record the observation once; while a recorded loaded broker runs,
        every run passes the pending-send gate here, so a late send refuses with no client changed."""
        if j.recorded('service') is None:
            kind,loaded=self.check()
            j.update(single=('service',{'plist':kind,'loaded':loaded}))
            return
        if self._observed(j)['loaded'] and j.recorded('service_stopped') is None:self._recheck()

    def stop(self, j):
        """Boot out only a still-loaded exact registration, gated again right before, then rewrite the plist."""
        observed=self._observed(j)
        if observed['loaded'] and j.recorded('service_stopped') is None:
            if self._recheck():_bootout(self.runner,self.label)
            j.update(single=('service_stopped',{'attempt':1}));j.checkpoint('stopped:service')
        if observed['plist']!='absent':
            j.discard('plist',self.plist)
            kind,data,_=self.kind()
            if kind!='to':j.write('plist',self.plist,_plist_bytes(self.target),data,existed=True,maximum=MAX_PLIST_BYTES)
            if self.kind()[0]!='to':raise InstallError('service_conflict')

    def restart(self, j):
        """After the commit: bootstrap the rewritten definition only when the stopped broker was loaded."""
        observed=j.recorded('service')
        if observed is None or not observed['loaded']:return 'not_loaded'
        if observed['plist']=='absent':return 'stopped'
        if j.recorded('service_restarted') is None:
            live=_live(self.runner,self.label)
            if live is None:_bootstrap(self.runner,self.plist)
            elif not _matches_live(live,self.plist,self.target):raise InstallError('service_conflict')
            j.update(single=('service_restarted',{'attempt':1}));j.checkpoint('restarted')
        return 'restarted'


_PIN = re.compile(rb'expected_package_version = "([0-9A-Za-z.+-]{1,64})"')


def _policy_candidate(data, old, new):
    """runtime.toml with only its expected_package_version line rewritten from `old` (any when None) to `new`.

    None when the file pins no version. Every other byte, capabilities included, stays identical.
    """
    lines=data.split(b'\n')
    hits=[i for i,line in enumerate(lines) if line.lstrip().startswith(b'expected_package_version')]
    if not hits:return None
    match=_PIN.fullmatch(lines[hits[0]]) if len(hits)==1 else None
    if match is None:raise InstallError('policy_conflict')
    value=match.group(1).decode()
    if value==new:return data
    if old is not None and value!=old:raise InstallError('policy_conflict')
    lines[hits[0]]=b'expected_package_version = "'+new.encode()+b'"'
    candidate=b'\n'.join(lines)
    try:
        if tomllib.loads(candidate.decode())!={**tomllib.loads(data.decode()),'expected_package_version':new}:
            raise InstallError('policy_conflict')
    except (tomllib.TOMLDecodeError,UnicodeError):
        raise InstallError('policy_conflict') from None
    return candidate


def _policy_step(j, session_root, old, new):
    policy=session_root/'runtime.toml'
    if not os.path.lexists(session_root):return
    j.discard('policy',policy)
    data,exists=_read_file(policy,MAX_POLICY_BYTES)
    candidate=_policy_candidate(data,old,new) if exists else None
    if candidate is not None and candidate!=data:
        j.write('policy',policy,candidate,data,existed=True,maximum=MAX_POLICY_BYTES)


def _state(session_root):
    """Hashes of the session state an older release must accept: enrollment.json and runtime.toml."""
    out={}
    for name in ('enrollment.json','runtime.toml'):
        if not os.path.lexists(session_root):out[name]=None;continue
        data,exists=_read_file(session_root/name,65536)
        out[name]=digest(data) if exists else None
    return out


def _commands(installed):
    return str(installed/'python/bin/python3'),['-I','-S','-B',str(installed/'launcher.py'),'mcp']


def _registration(installed, manifest):
    command,arguments=_commands(installed)
    return Registration(command,tuple(arguments),{},tuple(manifest['tools']))


def _receipt_shape(receipt, root):
    version=receipt.get('schema_version')
    keys=RECEIPT_KEYS.get(version) if type(version) is int else None
    index=receipt.get('index_sha256')
    if (keys is None or set(receipt)!=keys or receipt['installation_root']!=str(root) or type(index) is not str
            or len(index)!=64 or any(c not in '0123456789abcdef' for c in index)
            or receipt['release_root']!=str(root/'releases'/index) or receipt['status']!='activation_required'
            or receipt['claude_code_mode'] not in CLAUDE_CODE_MODES or receipt['claude_skills'] not in CLAUDE_SKILLS
            or type(receipt['publication_objects']) is not dict):
        raise InstallError('metadata_conflict')
    try:
        if version==2:check_receipt_v2(receipt,root)
    except BundleError:raise InstallError('metadata_conflict') from None
    return receipt


def _pointer(receipt):
    return encoded({'schema_version':1,'release_root':receipt['release_root'],'index_sha256':receipt['index_sha256']})


def _installation(root):
    """(receipt, receipt bytes) of a pointer-consistent receipt-bound installation."""
    data=read(root/'receipt.json',mode=0o600)
    receipt=_receipt_shape(unique_json(data),root)
    if read(root/'current.json',mode=0o600)!=_pointer(receipt):raise InstallError('metadata_conflict')
    return receipt,data


def _codex_ownership(root, receipt):
    data=read(root/'transactions'/receipt_transaction(receipt)/'registration.json',mode=0o600)
    if digest(data)!=receipt['registration_sha256']:raise InstallError('metadata_conflict')
    return Receipt.from_bytes(data)


def _desktop_ownership(root, receipt, command, arguments):
    """The receipt's Claude Desktop ownership when it owns exactly (command, arguments), else None.

    v2 carries it in receipt.json; a v1 receipt's comes from its install journal.
    """
    if receipt['schema_version']==2:value=receipt['claude_desktop_ownership']
    elif receipt['claude_desktop']=='registered':
        journal=unique_json(read(root/'transactions'/receipt_transaction(receipt)/'journal.json',mode=0o600))
        records=journal.get('publications',{}).get('claude_desktop_add')
        value=records[0].get('ownership') if type(records) is list and len(records)==1 and type(records[0]) is dict else None
    else:value=None
    if value is None:return None
    owned=Receipt.from_bytes(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
    expected=hashlib.sha256(claude_desktop_registration.subtree(command,arguments)).hexdigest()
    return value if owned.client=='claude-desktop' and owned.post_subtree_hash==expected else None


def _codex_change(source, registration, ownership):
    """Our owned subtree replaced in place; an entry the user removed is added back like a first install."""
    return merge_registration(source,registration,None if owned_subtree(source) is None else ownership)


def _changes(root):
    """Sorted numbers of the existing change transactions (bounded)."""
    numbers=[]
    with open_owned_directory(root/'transactions',private=True):pass
    for count,path in enumerate((root/'transactions').iterdir()):
        if count>4*MAX_CHANGES:raise InstallError('recovery_limit')
        if path.name.startswith('change_') and transaction_name(path.name):numbers.append(int(path.name[7:]))
    return sorted(numbers)


def _latest_change(root):
    """(name, journal) of the newest change transaction, or (None, None)."""
    numbers=_changes(root)
    if not numbers:return None,None
    name='change_%d'%numbers[-1]
    return name,unique_json(read(root/'transactions'/name/'journal.json',mode=0o600))


_STAGING = re.compile(r'\.change_[1-9][0-9]{0,2}-staging-[a-z0-9_]{8}\Z')
_STAGED_FILES = frozenset({'journal.json','receipt.previous','config.preimage','config.candidate','registration.json',
                           'receipt.target',*('adopted-'+c for c in ADOPTION_CLIENTS)})


def _staged_entry(path, directory):
    return len(path)==1 and not directory and (path[0] in _STAGED_FILES or is_temporary(path[0]))


def _sweep_staging(root):
    """Under the lease no staging directory is being written: each one a killed opening left (it never had
    authority) is removed when exactly ours (`_staged_entry`); one that is not stays and counts toward the bound."""
    transactions=root/'transactions'
    _changes(root)  # Bounded listing.
    for path in list(transactions.iterdir()):
        if _STAGING.fullmatch(path.name):_remove_abandoned(transactions,path.name,_staged_entry)


def _open_change(root, journal):
    """Publish a complete change transaction directory once (no-replace); returns its name."""
    numbers=_changes(root)
    number=(numbers[-1] if numbers else 0)+1
    if number>MAX_CHANGES:raise InstallError('recovery_limit')
    name='change_%d'%number
    transactions=root/'transactions'
    _sweep_staging(root)
    prefix='.'+name+'-staging-'
    if sum(p.name.startswith('.change_') for p in transactions.iterdir())>=8:raise InstallError('recovery_limit')
    staged=Path(tempfile.mkdtemp(prefix=prefix,dir=transactions))
    for file,data in journal(name).items():create(staged/file,data,maximum=MAX_CONFIG_BYTES if file.startswith('config.') else MAX_BACKUP_BYTES)
    _sync_directories(staged)
    _rename(staged,transactions/name)
    return name


def _result_extras(result, claude_status, desktop_status, command, arguments):
    if claude_status in _MANUAL:
        result['claude_registration_command']=claude_registration.manual_command(command,arguments)
    # Claude Desktop reads its config only at launch.
    if desktop_status=='registered':result['claude_desktop_next_action']='restart_claude_desktop'
    if desktop_status in _DESKTOP_MANUAL:
        result['claude_desktop_manual_entry']=claude_desktop_registration.manual_entry(command,arguments)
    return result


_ROOT_NAMES = frozenset({'transactions','releases','.install.lock','receipt.json','current.json',ADOPTED_NAME})


def _adopted_file(root):
    """(bytes or None, backup records) of the installation's adopted.json."""
    path=root/ADOPTED_NAME
    if not os.path.lexists(path):return None,[]
    data=read(path,mode=0o600)
    try:return data,check_adopted(unique_json(data))
    except BundleError:raise InstallError('metadata_conflict') from None


def _record_adopted(j, root, adopted, transaction):
    """Before any client write: this transaction's adoption backups listed in adopted.json, which no receipt
    schema touches. Published from a bound packet like receipt.json (never a temporary in the root)."""
    if not adopted:return
    data,records=_adopted_file(root)
    known={(r['transaction'],r['client']) for r in records}
    new=[{'client':c,'transaction':transaction,'sha256':v['sha256'],'bytes':v['bytes']}
         for c,v in sorted(adopted.items()) if (transaction,c) not in known]
    if not new:return
    if len(records)+len(new)>MAX_ADOPTED:raise InstallError('recovery_limit')
    value=encoded({'schema_version':1,'backups':records+new})
    def replaceable(p):
        if data is None or read(p,mode=0o600)!=data:raise InstallError('metadata_conflict')
    j.replace('adopted_sidecar',root/ADOPTED_NAME,lambda p:create(p,value),_exact(value),replaceable)


def _adopted_records(records, transaction):
    return {c:{'sha256':v['sha256'],'bytes':v['bytes'],'transaction':transaction} for c,v in (records or {}).items()}


def _check_adopted(record):
    if record is None:return {}
    if (type(record) is not dict or set(record)-set(ADOPTION_CLIENTS)
            or any(type(v) is not dict or set(v)!={'sha256','bytes'} for v in record.values())):
        raise InstallError('transaction_conflict')
    return record


def _installed_mode(root):
    """The `--claude-code` mode recorded for `root` (an unfinished change's, else the receipt's, else an unfinished
    first install's intent), None when nothing is installed. A read before the lease that only picks the default
    when no mode is given: every transaction compares it again under its lock."""
    intent=_read_intent(root)
    if intent is None:return None
    mode=intent['identity'].get('claude_code_mode')
    if os.path.lexists(root):
        _,journal=_latest_change(root)
        if journal is not None and journal.get('stage')!='finished' and 'claude_code_mode' in journal:
            mode=journal['claude_code_mode']
        elif os.path.lexists(root/'current.json'):mode=_installation(root)[0]['claude_code_mode']
    elif not _staged_ours(intent):return None  # A retired or stale intent records nothing current.
    return mode if type(mode) is str and mode in CLAUDE_CODE_MODES else None


def install(release, index_path, index_sha256, *, installation_root, codex_home,
            skills_root, user_home, checkpoint=lambda _: None, claude_candidates=None,
            claude_runner=subprocess.run, claude_code=None, adopt_legacy=False,
            launchctl=subprocess.run, pending_sends=None):
    """First install, its recovery, or (over a receipt-bound installation of another index) an upgrade.

    `claude_code` is `user`, `plugin` or `skip`, and is part of the transaction identity. None (the option not
    given) keeps the mode the installation recorded (an upgrade, a repeat or a resumed first install) and is
    `user` for a new first install; only an explicitly different mode refuses (`upgrade_conflict` on an upgrade).
    `user`: Claude Code state is read from `<user_home>/.claude.json`; the `claude` CLI, looked up only
    at `claude_candidates` (default: the fixed user/system list) and driven through `claude_runner`,
    is used only to add the entry. Claude registration never blocks the install except for a
    different existing entry, which refuses before anything is created. Claude skills go to
    `<user_home>/.claude/skills` only when `~/.claude` exists or the CLI was located. `plugin` and
    `skip` never run the CLI or touch `~/.claude/skills`; plugin mode only notes, read-only, an
    existing user-scope entry (`claude_code_user_entry: present`).

    Claude Desktop: when `<user_home>/Library/Application Support/Claude` exists, an absent
    `telegram_search` entry is added once to its `claude_desktop_config.json`; a different entry
    refuses before anything is created. Otherwise it never blocks the install.

    `adopt_legacy`: a foreign `telegram_search` entry (Codex, Claude Code, Claude Desktop) or a foreign
    broker plist is replaced only after its exact backup is recorded in the transaction, adopted.json and
    the receipt; never by default. `launchctl` and `pending_sends` are the injectable service boundary.
    Over an installed root this is an upgrade (`_change`); a lower package version refuses
    (`downgrade_refused`): going back is only `rollback`. Once any change exists, the installed release's own kit
    goes through `_change` too: its repeat result (`upgraded`, or after a rollback back to it `rolled_back`, with
    `installation: reused`), after the same conflict checks. Over an absent root, an uninstall interrupted after its
    retiring rename is finished first (`_stale_intent`).
    """
    try:
        require_platform()
        if (claude_code is not None and (type(claude_code) is not str or claude_code not in CLAUDE_CODE_MODES)
                or type(adopt_legacy) is not bool):raise InstallError('installation_refused')
        probe=default_pending_sends if pending_sends is None else pending_sends
        release,index_path,root,codex,skills,home=map(Path,(release,index_path,installation_root,codex_home,skills_root,user_home))
        if claude_code is None:claude_code=_installed_mode(root) or 'user'
        claude_skills=home/'.claude'/'skills'
        desktop=claude_desktop_registration.directory(home)
        cli=None
        if claude_code=='user':
            cli=claude_registration.locate(claude_registration.candidate_paths(home) if claude_candidates is None else claude_candidates)
        index,manifest=validate_tree(release,index_path,index_sha256)
        index_bytes=read(index_path,2*1024*1024);config=codex/'config.toml'
        from .config import _owner_paths
        session_root,_,agent_label,plist=_owner_paths(home)
        if (any(root==p or root.is_relative_to(p) or p.is_relative_to(root) for p in (release,session_root,codex,skills,claude_skills,desktop))
                or any(a.is_relative_to(b) for a,b in ((skills,claude_skills),(claude_skills,skills),(skills,desktop),
                                                       (desktop,skills),(claude_skills,desktop),(desktop,claude_skills)))):
            raise InstallError('overlapping_paths')
        # Only the runtime root must be owner-only; Codex and Claude create their homes 0755.
        for path in (root,codex,skills,home)+((claude_skills,) if claude_code=='user' else ()):
            validate_owned_path(path,private=path==root)
        validate_owned_path(plist,regular=True)
        intent_path=_intent_path(root)
        if not os.path.lexists(root) and os.path.lexists(intent_path):_stale_intent(root)
        if intent_path.exists() and root.exists() and _changing(root,index_sha256):
            source=dict(release=release,index=index,index_bytes=index_bytes,index_sha256=index_sha256,manifest=manifest)
            return _change('upgrade',root,home,codex=codex,skills=skills,claude_code=claude_code,source=source,
                           adopt=adopt_legacy,checkpoint=checkpoint,cli=cli,claude_runner=claude_runner,
                           launchctl=launchctl,pending_sends=probe)
        installed=root/'releases'/index_sha256
        tx=root/'transactions'/('install_'+index_sha256[:24])
        registration=Registration(str(installed/'python/bin/python3'),('-I','-S','-B',str(installed/'launcher.py'),'mcp'),{},tuple(manifest['tools']))
        command,arguments=registration.command,list(registration.args)
        identity=dict(schema_version=1,index_sha256=index_sha256,release_root=str(installed),installation_root=str(root),
                      codex_home=str(codex),skills_root=str(skills),claude_skills_root=str(claude_skills),user_home=str(home),
                      package_version=manifest['package_version'],claude_code_mode=claude_code)
        def claude_state():return claude_registration.state(home,command,arguments)  # A file read; never the CLI.
        def desktop_state():return claude_desktop_registration.state(home,command,arguments)  # A file read only.
        if not intent_path.exists():
            if root.exists():raise InstallError('runtime_conflict')
            adopted={}
            for skill in manifest['skills']:
                for base in (skills,claude_skills) if claude_code=='user' else (skills,):
                    if (base/skill['id']).exists() or (base/skill['id']).is_symlink():raise InstallError('skill_conflict')
            # A different (foreign or malformed) Claude entry refuses the whole install before anything is
            # created, unless --adopt-legacy backs it up first.
            if claude_code=='user' and claude_state()=='different':
                data=claude_registration.entry_backup(home) if adopt_legacy else None
                if data is None or len(data)>MAX_BACKUP_BYTES:raise InstallError('claude_registration_conflict')
                adopted['claude_code']=data
            # So does a different Claude Desktop entry, or a config whose neighbours a write could not keep exactly.
            observed=desktop_state()
            if observed=='different':
                data=claude_desktop_registration.entry_backup(home) if adopt_legacy else None
                if data is None or len(data)>MAX_BACKUP_BYTES:raise InstallError('claude_desktop_registration_conflict')
                if claude_desktop_registration.observe_change(home,None,command,arguments,adopted=digest(data))[0]!='adopted':
                    raise InstallError('claude_desktop_config_unsupported')
                adopted['claude_desktop']=data
            if observed=='unsupported':raise InstallError('claude_desktop_config_unsupported')
            source,existed=_config(config)
            try:change=merge_registration(source,registration)
            except ConfigConflict as error:
                if error.code!='ownership_conflict':raise
                if not adopt_legacy:raise InstallError('registration_conflict') from None
                adopted['codex']=owned_subtree(source);change=adopt_registration(source,registration)
            if plist.exists():
                # A foreign broker plist (a source-venv broker) blocks the install; adoption stops it first.
                if not adopt_legacy:raise InstallError('service_conflict')
                data=read(plist,MAX_BACKUP_BYTES,mode=0o600)
                service=_Service(plist,agent_label,_service_payload(release,agent_label),adopted=digest(data),
                                 runner=launchctl,pending=probe,home=home)
                service.check()
                adopted['service']=data
        else:source=b'';existed=False;change=None;adopted=None
        _initialize(root,identity,change,existed,checkpoint,adopted)
        with SessionOwnerLease(root/'.install.lock') as lease:
            if set(p.name for p in root.iterdir())-_ROOT_NAMES:
                raise InstallError('runtime_conflict')
            j=_Journal(tx,lease,_INSTALL_STEPS,checkpoint)
            journal=j.value
            if (set(journal)!=set(identity)|{'stage','config_existed','publications'}
                    or any(journal.get(k)!=v for k,v in identity.items())):
                raise InstallError('transaction_conflict')
            adopted=_check_adopted(j.recorded('adopted'))
            for client,record in adopted.items():
                data=read(tx/('adopted-'+client),MAX_BACKUP_BYTES,mode=0o600)
                if digest(data)!=record['sha256'] or len(data)!=record['bytes']:raise InstallError('transaction_conflict')
            before=read(tx/'config.preimage',MAX_CONFIG_BYTES,mode=0o600)
            candidate=read(tx/'config.candidate',MAX_CONFIG_BYTES,mode=0o600)
            ownership=Receipt.from_bytes(read(tx/'registration.json',mode=0o600))
            fresh=(adopt_registration if 'codex' in adopted else merge_registration)(before,registration)
            if candidate!=fresh.candidate or ownership.post_subtree_hash!=fresh.post_subtree_hash:raise InstallError('transaction_conflict')
            if plist.exists() and ('service' not in adopted or j.reached('complete')):
                if not j.reached('complete') or read(root/'current.json',mode=0o600)!=encoded({'schema_version':1,'release_root':str(installed),'index_sha256':index_sha256}):
                    raise InstallError('service_conflict')
                _check_existing_service(plist,installed,agent_label)
            for client_root in (codex,skills):ensure_owned_private_directory(client_root,private_leaf=False)
            update,reached,proof,recorded=j.update,j.reached,j.proof,j.recorded
            if not reached('release_pending'):_record_adopted(j,root,adopted,tx.name)
            checkpoint('prepared')
            if not reached('release_pending'):update('release_pending')
            j.publish('release',installed,lambda p:_copy_release(release,p,index,index_bytes,index_sha256),
                      lambda p:validate_tree(p,p/'bundle-index.json',index_sha256,installed=True),directory=True)
            if not reached('release'):update('release')
            checkpoint('release')
            # Only an adopted foreign broker is ever stopped here; its definition is replaced by ours.
            service=_Service(plist,agent_label,_service_payload(installed,agent_label),adopted=adopted['service']['sha256'],
                             runner=launchctl,pending=probe,home=home) if 'service' in adopted else None
            # Its observation and pending-send gate come before the first client write.
            if service is not None and not reached('service'):service.observe(j)
            if not reached('registration'):
                if not reached('registration_pending'):update('registration_pending')
                # A killed earlier attempt leaves its journaled temporary beside the config.
                j.discard('registration',config)
                current,exists=_config(config);bound=proof('registration')
                owned=False
                if bound is not None and exists and 'object' in bound:
                    try:require_object_binding(config,bound['object']);owned=current==candidate
                    except RuntimePathError:pass
                if not owned:
                    if current!=before or exists!=journal['config_existed']:raise InstallError('foreign_registration')
                    # Each attempt is journaled (and counted) before its temporary exists.
                    j.attempt('registration',lambda temporary,admit:_atomic_write(config,candidate,MAX_CONFIG_BYTES,expected=before,
                              require_existing=journal['config_existed'],before_replace=admit,temporary=temporary))
                    checkpoint('published:registration')
                    require_object_binding(config,proof('registration')['object'])
                update('registration')
            if not merge_registration(read_config(config),registration,ownership).is_noop:raise InstallError('registration_conflict')
            checkpoint('registration')
            # `~/.claude/skills` is touched only when Claude Code is evidently present; decided once per transaction.
            if recorded('claude_skills') is None:
                present=claude_code=='user' and (os.path.lexists(home/'.claude') or cli is not None)
                update(single=('claude_skills',{'status':'installed' if present else 'skipped'}))
            claude_skills_status=recorded('claude_skills').get('status')
            if claude_skills_status not in CLAUDE_SKILLS:raise InstallError('transaction_conflict')
            for prefix,base in (('',skills),('claude-',claude_skills)):
                for skill in manifest['skills']:
                    sid,expected=skill['id'],skill['sha256'];name=prefix+sid;target=base/sid
                    if not reached(name+'_pending'):update(name+'_pending')
                    # An external client directory the user may wipe: published once, never re-verified later.
                    if prefix and (claude_skills_status=='skipped' or reached(name)):
                        if not reached(name):update(name)
                        checkpoint(name);continue
                    if prefix:ensure_owned_private_directory(claude_skills,private_leaf=False)
                    def validate_skill(p):
                        if _skill_hash(p)!=expected:raise InstallError('skill_conflict')
                    j.publish(name,target,lambda p:create(p/'SKILL.md',read(installed/'skills'/sid/'SKILL.md')),validate_skill,directory=True,child=True)
                    if not reached(name):update(name)
                    checkpoint(name)
            def outcome(publications=None,name='claude_code'):
                record=recorded(name,publications)
                return None if record is None else record.get('status')
            added_now=False
            def register_claude():
                """This transaction's Claude Code outcome. Only an absent (or adopted) entry is replaced, at most once."""
                nonlocal added_now
                if claude_code!='user':return 'plugin' if claude_code=='plugin' else 'skipped'
                if recorded('claude_add') is not None:
                    # Attempted before an interruption: confirm from the file, never run the CLI again.
                    return 'registered' if claude_state()=='exact' else 'failed'
                state=claude_state()
                if state=='exact':return 'reused'
                if cli is None:return 'not_found'
                if (state=='different' and 'claude_code' in adopted and recorded('claude_remove') is None
                        and claude_registration.adopted(home,adopted['claude_code']['sha256'])):
                    # The backed-up foreign entry: write-ahead, removed once, then ours is added.
                    update(single=('claude_remove',{'attempt':1}))
                    checkpoint('recorded:claude_remove')
                    try:claude_registration.remove(claude_runner,cli,home)
                    except claude_registration.ClaudeCliError:pass
                    checkpoint('published:claude_remove')
                    state=claude_state()
                if state!='absent':return 'unavailable' if state=='unavailable' else 'failed'
                # Write-ahead: a journaled attempt is what makes the entry ours.
                update(single=('claude_add',{'attempt':1}))
                checkpoint('recorded:claude_add')
                added_now=True
                try:claude_registration.add(claude_runner,cli,home,command,arguments)
                except claude_registration.ClaudeCliError:pass  # The confirming read decides.
                checkpoint('published:claude_add')
                return 'registered' if claude_state()=='exact' else 'failed'
            def rollback_claude():
                """Best effort: only an entry this run just added, before its outcome is durable, and only while exactly ours."""
                if not added_now:return
                try:
                    if outcome(unique_json(read(tx/'journal.json',mode=0o600))['publications']) is None and claude_state()=='exact':
                        claude_registration.remove(claude_runner,cli,home)
                except Exception:pass
            desktop_change=None;desktop_written=False
            def register_desktop():
                """This transaction's Claude Desktop outcome. Only an absent (or adopted) entry is written; after an
                interruption the same write is made again only over a file still byte-identical to its recorded preimage."""
                nonlocal desktop_change,desktop_written
                add=recorded('claude_desktop_add')
                if add is not None:
                    # Attempted before an interruption: drop our journaled temporaries a kill left, then confirm.
                    _desktop_discard(j,home)
                    if desktop_state()=='exact':return 'registered'
                if 'claude_desktop' in adopted:
                    state,change=claude_desktop_registration.observe_change(home,None,command,arguments,
                                                                            adopted=adopted['claude_desktop']['sha256'])
                    state='absent' if state=='adopted' else state
                else:state,change=claude_desktop_registration.observe(home,command,arguments)
                if add is not None:
                    if state!='absent' or not _desktop_unchanged(j,add,change):return 'failed'
                else:
                    if state in ('skipped','unavailable'):return state
                    if state=='exact':return 'reused'
                    if state=='unsupported':return 'unavailable'
                    if state!='absent':return 'failed'  # A different entry appeared mid-transaction: never touched.
                    owned=claude_desktop_registration.receipt('install_'+index_sha256[:24],'first_install',command,arguments)
                    # Write-ahead: a journaled attempt is what makes the entry ours. Hashes only; never config bytes.
                    update(single=('claude_desktop_add',{'attempt':1,'existed':change.existed,'source_sha256':change.source_sha256,
                                                         'candidate_sha256':change.candidate_sha256,
                                                         'ownership':unique_json(owned.to_bytes())}))
                    checkpoint('recorded:claude_desktop_add')
                desktop_change,desktop_written=change,True
                _desktop_write(j,home,change)
                checkpoint('published:claude_desktop_add')
                return 'registered' if desktop_state()=='exact' else 'failed'
            def rollback_desktop():
                """Best effort: only the file this run just wrote, before its outcome is durable, while still exactly ours."""
                if not desktop_written:return
                try:
                    publications=unique_json(read(tx/'journal.json',mode=0o600))['publications']
                    written=(publications.get('claude_desktop_write') or [None])[-1]
                    if outcome(publications,'claude_desktop') is None and type(written) is dict and 'object' in written:
                        temporary=new_temporary()
                        update(publication=('claude_desktop_restore',{'temporary':temporary}))
                        claude_desktop_registration.restore(home,desktop_change,written['object'],temporary)
                except Exception:pass
            try:
                # Once recorded the outcome is final for this transaction: never re-run, re-read or rolled back.
                claude_status=outcome()
                if claude_status is None and not reached('complete'):
                    if not reached('claude_code_pending'):update('claude_code_pending')
                    claude_status=register_claude()
                    update(single=('claude_code',{'status':claude_status}))
                if claude_status not in CLAUDE_MODE_STATUSES[claude_code]:raise InstallError('transaction_conflict')
                if not reached('complete'):
                    if not reached('claude_code'):update('claude_code')
                    checkpoint('claude_code')
                desktop_status=outcome(name='claude_desktop')
                if desktop_status is None and not reached('complete'):
                    if not reached('claude_desktop_pending'):update('claude_desktop_pending')
                    desktop_status=register_desktop()
                    update(single=('claude_desktop',{'status':desktop_status}))
                if desktop_status not in CLAUDE_DESKTOP_STATUSES:raise InstallError('transaction_conflict')
                if not reached('complete'):
                    if not reached('claude_desktop'):update('claude_desktop')
                    checkpoint('claude_desktop')
                if not reached('service'):
                    j.advance('service_pending')
                    if service is not None:service.stop(j)
                    update('service')
                if not reached('policy'):
                    j.advance('policy_pending')
                    # The adopted broker's session pin follows it (any version), only that line.
                    if 'service' in adopted:_policy_step(j,session_root,None,manifest['package_version'])
                    update('policy')
                names=['release','telegram-search','telegram-setup']
                if claude_skills_status=='installed':names+=['claude-telegram-search','claude-telegram-setup']
                objects={name:{k:v for k,v in proof(name).items() if k!='staged'} for name in names}
                add=recorded('claude_desktop_add')
                receipt={**identity,'schema_version':2,'registration_sha256':digest(ownership.to_bytes()),'skills':manifest['skills'],
                         'publication_objects':objects,'claude_code':claude_status,'claude_skills':claude_skills_status,
                         'claude_desktop':desktop_status,'status':'activation_required','transaction':tx.name,
                         'claude_desktop_ownership':add['ownership'] if desktop_status=='registered' and add else None,
                         'previous':None,'adopted':_adopted_records(adopted,tx.name)}
                if not reached('receipt_pending'):update('receipt_pending')
                j.publish('receipt',root/'receipt.json',lambda p:create(p,encoded(receipt)),_exact(encoded(receipt)))
                if not reached('receipt'):update('receipt')
                checkpoint('receipt')
                pointer=encoded({'schema_version':1,'release_root':str(installed),'index_sha256':index_sha256})
                reused=reached('complete')
                j.publish('pointer',root/'current.json',lambda p:create(p,pointer),_exact(pointer))
                if not reached('complete'):update('complete')
                restarted=service.restart(j) if service is not None else None
                activation=[str(installed/'python/bin/python3'),'-I','-S','-B',str(installed/'launcher.py'),'connect']
                result={'status':'activation_required','installation':'reused' if reused else 'installed','release_root':str(installed),
                        'package_version':manifest['package_version'],'next_action':'run_activation_then_wait',
                        'claude_code':claude_status,'claude_skills':claude_skills_status,'claude_desktop':desktop_status,
                        'activation_arguments':activation,'activation_command':shlex.join(activation)}
                if adopted:result['adopted']=sorted(adopted)
                if restarted is not None:result['service']=restarted
                if claude_code=='plugin' and claude_registration.present(home)=='present':
                    # Read-only note: plugin and user scope would duplicate the tool set; never removed here.
                    result['claude_code_user_entry']='present'
                return _result_extras(result,claude_status,desktop_status,command,arguments)
            except _REFUSALS:
                rollback_claude()
                rollback_desktop()
                raise
    except InstallError:raise
    except _REFUSALS:
        raise InstallError('installation_refused') from None


_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})((?:\.(?:0|[1-9][0-9]{0,5})){0,4})(?:(a|b|rc)(0|[1-9][0-9]{0,5}))?'
                      r'(?:\.post(0|[1-9][0-9]{0,5}))?(?:\.dev(0|[1-9][0-9]{0,5}))?')
_INF = float('inf')


def _version_key(value):
    """PEP 440 order of a public release version (N[.N]*[{a|b|rc}N][.postN][.devN]); anything else refuses."""
    match=_VERSION.fullmatch(value) if type(value) is str else None
    if match is None:raise InstallError('downgrade_refused')
    head,rest,letter,number,post,dev=match.groups()
    release=[int(head),*(int(p) for p in rest.split('.')[1:])]
    while len(release)>1 and release[-1]==0:release.pop()
    if letter is not None:pre=(('a','b','rc').index(letter),int(number))
    elif dev is not None and post is None:pre=(-_INF,0)  # X.devN sorts before X's pre-releases.
    else:pre=(_INF,0)
    return (tuple(release),pre,-_INF if post is None else int(post),_INF if dev is None else int(dev))


def _changing(root, index_sha256):
    """Whether a rerun over this root goes through the change path (`_change`) rather than the first install's
    repeat: any change transaction exists (an unfinished one is resumed or named; after a finished one, a rollback
    back to the first release included, receipt.json is no longer the object the install journal bound), or the
    receipt names another release."""
    name,journal=_latest_change(root)
    if journal is not None:return True
    if not (root/'current.json').exists():return False
    receipt,_=_installation(root)
    return receipt['index_sha256']!=index_sha256 or receipt_transaction(receipt)!='install_'+index_sha256[:24]


def rollback(installation_root, *, user_home, checkpoint=lambda _: None, claude_candidates=None,
             claude_runner=subprocess.run, launchctl=subprocess.run, pending_sends=None):
    """Back to the release before the last upgrade, only while no newer session state exists."""
    try:
        require_platform()
        root,home=Path(installation_root),Path(user_home)
        validate_owned_path(root,private=True)
        intent=None if os.path.lexists(root) else _read_intent(root)
        state=None if intent is None else _absent_root(root,intent)
        if state is not None:raise InstallError('transaction_pending',pending_action=state)
        _,journal=_latest_change(root)
        unfinished=journal is not None and journal.get('stage')!='finished'
        if not unfinished and _install_pending(root):raise InstallError('transaction_pending',pending_action='install')
        mode=journal.get('claude_code_mode') if unfinished else _installation(root)[0]['claude_code_mode']
        cli=None
        if mode=='user':
            cli=claude_registration.locate(claude_registration.candidate_paths(home) if claude_candidates is None else claude_candidates)
        return _change('rollback',root,home,checkpoint=checkpoint,cli=cli,claude_runner=claude_runner,launchctl=launchctl,
                       pending_sends=default_pending_sends if pending_sends is None else pending_sends)
    except InstallError:raise
    except _REFUSALS:
        raise InstallError('installation_refused') from None


def _change(kind, root, home, *, codex=None, skills=None, claude_code=None, source=None, adopt=False,
            checkpoint, cli, claude_runner, launchctl, pending_sends):
    """Upgrade (to `source`) or rollback (to the receipt's previous release) of an installed root.

    Every refusal (a downgrade, a foreign or drifted object, a pending send) comes before the transaction
    exists. Order: adoption records (adopted.json), release, the broker observation with its pending-send
    gate, Codex, skills, Claude Code, Claude Desktop, broker stop (gated again) and plist, the runtime.toml
    version line, receipt.json, then current.json (the commit). Only a broker that was loaded is
    bootstrapped again, after the commit, because a release is admitted only once current.json names it.
    """
    from .config import _owner_paths
    session_root,_,label,plist=_owner_paths(home)
    with SessionOwnerLease(root/'.install.lock') as lease:
        if set(p.name for p in root.iterdir())-_ROOT_NAMES:
            raise InstallError('runtime_conflict')
        name,journal=_latest_change(root)
        resuming=journal is not None and journal.get('stage')!='finished'
        if resuming:
            # An unfinished change blocks every other action until that same action is retried.
            if journal.get('kind')!=kind or kind=='upgrade' and journal.get('index_sha256')!=source['index_sha256']:
                raise InstallError('transaction_pending',pending_action=journal.get('kind'))
            _sweep_staging(root)
            tx=root/'transactions'/name
            previous_bytes=read(tx/'receipt.previous',mode=0o600)
            current=_receipt_shape(unique_json(previous_bytes),root)
        else:
            # A first install killed after its pointer but short of `complete` is finished by repeating it.
            if _install_pending(root):raise InstallError('transaction_pending',pending_action='install')
            current,previous_bytes=_installation(root)
        # Before any result, a repeat's included: an explicitly given mode (`install` passes the recorded one when
        # none is given) or a home other than the receipt's refuses.
        if kind=='upgrade' and (str(codex)!=current['codex_home'] or str(skills)!=current['skills_root']
                                or str(home)!=current['user_home'] or claude_code!=current['claude_code_mode']):
            raise InstallError('upgrade_conflict')
        if str(home)!=current['user_home']:raise InstallError('upgrade_conflict')
        if not resuming and kind=='upgrade' and current['index_sha256']==source['index_sha256']:
            # The kit of the installed release run again: no effect. After a finished rollback back to it (the newest
            # change), that rollback's result; otherwise the repeat's.
            if _finished(current,journal,'rollback'):return _rolled_back(current,journal)
            return _reused(root,current,name,journal)
        if not resuming and kind=='rollback' and _finished(current,journal,'rollback'):
            # Repeated after it finished (a retry after a kill past its last journal write): its result, no effect.
            return _rolled_back(current,journal)
        codex,skills=Path(current['codex_home']),Path(current['skills_root'])
        claude_skills,mode=Path(current['claude_skills_root']),current['claude_code_mode']
        from_root,from_index=Path(current['release_root']),current['index_sha256']
        config=codex/'config.toml'
        if kind=='upgrade':
            to_index,to_manifest=source['index_sha256'],source['manifest']
            target=None
        else:
            if resuming:target=_receipt_shape(unique_json(read(tx/'receipt.target',mode=0o600)),root)
            else:
                if current['schema_version']!=2 or current['previous'] is None or not current['transaction'].startswith('change_'):
                    raise InstallError('rollback_unavailable')
                origin=root/'transactions'/current['transaction']
                target=_receipt_shape(unique_json(read(origin/'receipt.previous',mode=0o600)),root)
                if target['index_sha256']!=current['previous']['index_sha256']:raise InstallError('rollback_unavailable')
            to_index=target['index_sha256']
            to_manifest=unique_json(read(root/'releases'/to_index/'manifest/release.json'))
            if to_manifest.get('skills')!=target['skills'] or to_manifest.get('package_version')!=target['package_version']:
                raise InstallError('rollback_refused')
        to_root=root/'releases'/to_index
        from_command,from_arguments=_commands(from_root)
        to_command,to_arguments=_commands(to_root)
        to_registration=_registration(to_root,to_manifest)
        claude_skills_status=current['claude_skills']
        if claude_skills_status not in CLAUDE_SKILLS:raise InstallError('metadata_conflict')
        names=['telegram-search','telegram-setup']+(['claude-telegram-search','claude-telegram-setup'] if claude_skills_status=='installed' else [])
        ownership=_codex_ownership(root,current)
        desktop_owned=_desktop_ownership(root,current,from_command,from_arguments)
        claude_owned=current['claude_code']=='registered'
        payloads={}
        if from_root.exists():payloads['from']=_service_payload(from_root,label)
        if not resuming:
            # Every refusal comes before the transaction exists: a downgrade, a foreign or drifted object, a pending send.
            if kind=='upgrade':
                # Going back is only the rollback action, with its session-state checks.
                if _version_key(to_manifest['package_version'])<_version_key(current['package_version']):
                    raise InstallError('downgrade_refused')
                if to_root.exists() or to_root.is_symlink():
                    validate_tree(to_root,to_root/'bundle-index.json',to_index,installed=True)
            if kind=='rollback':
                state=unique_json(read(root/'transactions'/current['transaction']/'journal.json',mode=0o600)).get('state')
                now=_state(session_root)
                if type(state) is not dict or now['enrollment.json']!=state.get('enrollment.json'):raise InstallError('rollback_refused')
                if now['runtime.toml']!=state.get('runtime.toml'):
                    data,exists=_read_file(session_root/'runtime.toml',MAX_POLICY_BYTES)
                    back=_policy_candidate(data,current['package_version'],target['package_version']) if exists else None
                    if back is None or digest(back)!=state.get('runtime.toml'):raise InstallError('rollback_refused')
                require_object_binding(to_root,target['publication_objects']['release']['object'])
            adopted={}
            source_bytes,existed=_config(config)
            try:change=_codex_change(source_bytes,to_registration,ownership)
            except ConfigConflict as error:
                if error.code!='ownership_conflict' or not adopt or kind!='upgrade':raise InstallError('registration_conflict')
                adopted['codex']=owned_subtree(source_bytes);change=adopt_registration(source_bytes,to_registration)
            if kind=='rollback':
                owned_target=_codex_ownership(root,target)
                if change.post_subtree_hash!=owned_target.post_subtree_hash:raise InstallError('rollback_refused')
            for skill_name in names:
                base,sid=(claude_skills,skill_name[7:]) if skill_name.startswith('claude-') else (skills,skill_name)
                path=base/sid;bound=current['publication_objects'].get(skill_name)
                if path.exists() or path.is_symlink():
                    try:
                        require_object_binding(path,bound['object']);require_object_binding(path/'SKILL.md',bound['child'])
                    except (RuntimePathError,TypeError,KeyError):raise InstallError('skill_conflict') from None
            if mode=='user':
                state=claude_registration.state(home,to_command,to_arguments)
                if state=='different' and claude_registration.state(home,from_command,from_arguments)!='exact':
                    data=claude_registration.entry_backup(home) if adopt and kind=='upgrade' else None
                    if data is None or len(data)>MAX_BACKUP_BYTES:raise InstallError('claude_registration_conflict')
                    adopted['claude_code']=data
            state,_=claude_desktop_registration.observe_change(home,(from_command,from_arguments) if desktop_owned else None,
                                                               to_command,to_arguments)
            if state=='different' and claude_desktop_registration.state(home,from_command,from_arguments)!='exact':
                data=claude_desktop_registration.entry_backup(home) if adopt and kind=='upgrade' else None
                if data is None or len(data)>MAX_BACKUP_BYTES:raise InstallError('claude_desktop_registration_conflict')
                if claude_desktop_registration.observe_change(home,None,to_command,to_arguments,adopted=digest(data))[0]!='adopted':
                    raise InstallError('claude_desktop_config_unsupported')
                adopted['claude_desktop']=data
            if state=='unsupported':raise InstallError('claude_desktop_config_unsupported')
            to_payload=_service_payload(to_root,label) if to_root.exists() else None
            service=_Service(plist,label,to_payload,known=payloads,runner=launchctl,pending=pending_sends,home=home)
            try:service.check()
            except InstallError as error:
                if str(error)!='service_conflict' or not adopt or kind!='upgrade' or not plist.exists():raise
                data=read(plist,MAX_BACKUP_BYTES,mode=0o600)
                service.adopted=digest(data);service.check()
                adopted['service']=data
            if os.path.lexists(session_root):
                data,exists=_read_file(session_root/'runtime.toml',MAX_POLICY_BYTES)
                if exists:_policy_candidate(data,None if 'service' in adopted else current['package_version'],to_manifest['package_version'])
            if len(_adopted_file(root)[1])+len(adopted)>MAX_ADOPTED:raise InstallError('recovery_limit')
            identity=dict(schema_version=1,kind=kind,index_sha256=to_index,release_root=str(to_root),installation_root=str(root),
                          codex_home=str(codex),skills_root=str(skills),claude_skills_root=str(claude_skills),user_home=str(home),
                          package_version=to_manifest['package_version'],claude_code_mode=mode,from_index_sha256=from_index,
                          from_release_root=str(from_root),from_package_version=current['package_version'])
            def files(tx_name):
                new_owner=Receipt(ownership.installation_id,tx_name,None if 'codex' in adopted else change.pre_subtree_hash,
                                  change.post_subtree_hash,None if 'codex' in adopted else change.pre_subtree)
                records={'adopted':[{c:{'sha256':digest(d),'bytes':len(d)} for c,d in adopted.items()}]} if adopted else {}
                value={'journal.json':encoded({**identity,'stage':'prepared','config_existed':existed,'publications':records,
                                               'state':_state(session_root)}),
                       'receipt.previous':previous_bytes,'config.preimage':change.source,'config.candidate':change.candidate,
                       **{'adopted-'+c:d for c,d in adopted.items()}}
                if kind=='upgrade':value['registration.json']=new_owner.to_bytes()
                else:value['receipt.target']=encoded(target)
                return value
            name=_open_change(root,files)
            tx=root/'transactions'/name
        j=_Journal(tx,lease,_CHANGE_STEPS,checkpoint)
        if (j['kind']!=kind or j['index_sha256']!=to_index or j['from_index_sha256']!=from_index
                or j['installation_root']!=str(root) or j['user_home']!=str(home)):
            raise InstallError('transaction_conflict')
        adopted=_check_adopted(j.recorded('adopted'))
        for client,record in adopted.items():
            data=read(tx/('adopted-'+client),MAX_BACKUP_BYTES,mode=0o600)
            if digest(data)!=record['sha256'] or len(data)!=record['bytes']:raise InstallError('transaction_conflict')
        before=read(tx/'config.preimage',MAX_CONFIG_BYTES,mode=0o600)
        candidate=read(tx/'config.candidate',MAX_CONFIG_BYTES,mode=0o600)
        new_ownership=(Receipt.from_bytes(read(tx/'registration.json',mode=0o600)) if kind=='upgrade'
                       else _codex_ownership(root,target))
        fresh=(adopt_registration(before,to_registration) if 'codex' in adopted
               else _codex_change(before,to_registration,ownership))
        if candidate!=fresh.candidate or new_ownership.post_subtree_hash!=fresh.post_subtree_hash:raise InstallError('transaction_conflict')
        if not j.reached('release_pending'):_record_adopted(j,root,adopted,name)
        checkpoint('prepared')
        # Release: the new one is copied once; an existing exact tree (an earlier upgrade) is reused.
        j.advance('release_pending')
        if kind=='upgrade':
            def validate_release(p):validate_tree(p,p/'bundle-index.json',to_index,installed=True)
            if j.proof('release') is None and to_root.exists():
                validate_release(to_root)
                j.update(publication=('release',{'object':object_binding(to_root,directory=True)}))
            j.publish('release',to_root,lambda p:_copy_release(source['release'],p,source['index'],source['index_bytes'],to_index),
                      validate_release,directory=True)
        else:require_object_binding(to_root,target['publication_objects']['release']['object'])
        j.advance('release');checkpoint('release')
        to_payload=_service_payload(to_root,label)
        service=_Service(plist,label,to_payload,known=payloads,adopted=adopted['service']['sha256'] if 'service' in adopted else None,
                         runner=launchctl,pending=pending_sends,home=home)
        # The broker observation and its pending-send gate come before the first client write.
        if not j.reached('service'):service.observe(j)
        # Codex: our owned subtree replaced in place (compare-before-replace over the exact preimage).
        if not j.reached('registration'):
            j.advance('registration_pending')
            ensure_owned_private_directory(codex,private_leaf=False)
            j.write('registration',config,candidate,before,existed=j['config_existed'],maximum=MAX_CONFIG_BYTES)
            j.update('registration')
        if not merge_registration(read_config(config),to_registration,new_ownership).is_noop:raise InstallError('registration_conflict')
        checkpoint('registration')
        objects={}
        to_skills={s['id']:s['sha256'] for s in to_manifest['skills']}
        for skill_name in SKILL_NAMES:
            if skill_name not in names:
                j.advance(skill_name+'_pending');j.advance(skill_name);checkpoint(skill_name);continue
            base,sid=(claude_skills,skill_name[7:]) if skill_name.startswith('claude-') else (skills,skill_name)
            path=base/sid;old=current['publication_objects'].get(skill_name);expected=to_skills[sid]
            j.advance(skill_name+'_pending')
            def validate_skill(p,expected=expected):
                if _skill_hash(p)!=expected:raise InstallError('skill_conflict')
            def replaceable(p,old=old):
                try:require_object_binding(p,old['object']);require_object_binding(p/'SKILL.md',old['child'])
                except (RuntimePathError,TypeError,KeyError):raise InstallError('skill_conflict') from None
            if j.proof(skill_name) is None and path.exists() and type(old) is dict:
                replaceable(path)
                if _skill_hash(path)==expected:
                    # Same bytes: the owned copy stays; its binding carries over.
                    objects[skill_name]={'object':old['object'],'child':old['child']}
                    j.advance(skill_name);checkpoint(skill_name);continue
            if skill_name.startswith('claude-'):ensure_owned_private_directory(claude_skills,private_leaf=False)
            else:ensure_owned_private_directory(skills,private_leaf=False)
            j.replace(skill_name,path,lambda p,sid=sid:create(p/'SKILL.md',read(to_root/'skills'/sid/'SKILL.md')),
                      validate_skill,replaceable,directory=True,child=True)
            bound=j.proof(skill_name)
            objects[skill_name]={'object':bound['object'],'child':bound['child']}
            j.advance(skill_name);checkpoint(skill_name)
        def outcome(name):
            record=j.recorded(name)
            return None if record is None else record.get('status')
        def register_claude():
            """Owned remove then add, each write-ahead and at most once; a foreign entry is never touched."""
            if mode!='user':return 'plugin' if mode=='plugin' else 'skipped'
            def state():return claude_registration.state(home,to_command,to_arguments)
            if j.recorded('claude_add') is not None:return 'registered' if state()=='exact' else 'failed'
            now=state()
            if now=='exact':return 'registered' if j.recorded('claude_remove') else 'reused'
            if now=='unavailable':return 'unavailable'
            if j.recorded('claude_remove') is None and now=='different':
                old=claude_registration.state(home,from_command,from_arguments)
                removable=(old=='exact' and claude_owned or 'claude_code' in adopted
                           and claude_registration.adopted(home,adopted['claude_code']['sha256']))
                if not removable:return 'failed'
                if cli is None:return 'not_found'
                j.update(single=('claude_remove',{'attempt':1}));checkpoint('recorded:claude_remove')
                try:claude_registration.remove(claude_runner,cli,home)
                except claude_registration.ClaudeCliError:pass
                checkpoint('published:claude_remove')
                now=state()
            if now!='absent':return 'failed'
            if cli is None:return 'not_found'
            j.update(single=('claude_add',{'attempt':1}));checkpoint('recorded:claude_add')
            try:claude_registration.add(claude_runner,cli,home,to_command,to_arguments)
            except claude_registration.ClaudeCliError:pass
            checkpoint('published:claude_add')
            return 'registered' if state()=='exact' else 'failed'
        def register_desktop():
            """Owned (or adopted) replace, or an absent entry added, mode kept; after an interruption the same
            write is made again only over a file still byte-identical to its recorded preimage."""
            def state():return claude_desktop_registration.state(home,to_command,to_arguments)
            add=j.recorded('claude_desktop_add')
            if add is not None:
                _desktop_discard(j,home)
                if state()=='exact':return 'registered'
            observed,plan=claude_desktop_registration.observe_change(
                home,(from_command,from_arguments) if desktop_owned else None,to_command,to_arguments,
                adopted=adopted['claude_desktop']['sha256'] if 'claude_desktop' in adopted else None)
            if add is not None:
                if observed not in ('owned','adopted','absent') or not _desktop_unchanged(j,add,plan):return 'failed'
            else:
                if observed in ('skipped','unavailable'):return observed
                if observed=='exact':return 'reused'
                if observed=='unsupported':return 'unavailable'
                if observed not in ('owned','adopted','absent'):return 'failed'
                owned=claude_desktop_registration.receipt(ownership.installation_id,name,to_command,to_arguments)
                j.update(single=('claude_desktop_add',{'attempt':1,'existed':plan.existed,'source_sha256':plan.source_sha256,
                                                       'candidate_sha256':plan.candidate_sha256,
                                                       'ownership':unique_json(owned.to_bytes())}))
                checkpoint('recorded:claude_desktop_add')
            _desktop_write(j,home,plan)
            checkpoint('published:claude_desktop_add')
            return 'registered' if state()=='exact' else 'failed'
        claude_status=outcome('claude_code')
        if claude_status is None:
            j.advance('claude_code_pending');claude_status=register_claude()
            j.update(single=('claude_code',{'status':claude_status}))
        if claude_status not in CLAUDE_MODE_STATUSES[mode]:raise InstallError('transaction_conflict')
        j.advance('claude_code');checkpoint('claude_code')
        desktop_status=outcome('claude_desktop')
        if desktop_status is None:
            j.advance('claude_desktop_pending');desktop_status=register_desktop()
            j.update(single=('claude_desktop',{'status':desktop_status}))
        if desktop_status not in CLAUDE_DESKTOP_STATUSES:raise InstallError('transaction_conflict')
        j.advance('claude_desktop');checkpoint('claude_desktop')
        if not j.reached('service'):
            j.advance('service_pending');service.stop(j);j.update('service')
        checkpoint('service')
        if not j.reached('policy'):
            j.advance('policy_pending')
            _policy_step(j,session_root,None if 'service' in adopted else current['package_version'],to_manifest['package_version'])
            j.update('policy')
        checkpoint('policy')
        if kind=='upgrade':
            add=j.recorded('claude_desktop_add')
            # Ownership follows only an entry this transaction wrote.
            desktop_owner=add['ownership'] if desktop_status=='registered' and add else None
            carried={**(current['adopted'] if current['schema_version']==2 else {}),**_adopted_records(adopted,name)}
            receipt={k:current[k] for k in ('installation_root','codex_home','skills_root','claude_skills_root','user_home',
                                            'claude_code_mode','claude_skills','status')}
            receipt.update(schema_version=2,index_sha256=to_index,release_root=str(to_root),package_version=to_manifest['package_version'],
                           registration_sha256=digest(read(tx/'registration.json',mode=0o600)),skills=to_manifest['skills'],
                           publication_objects={'release':{'object':j.proof('release')['object']},**objects},
                           claude_code=claude_status,claude_desktop=desktop_status,
                           transaction=name,claude_desktop_ownership=desktop_owner,adopted=carried,
                           previous={'index_sha256':from_index,'release_root':str(from_root),
                                     'package_version':current['package_version'],'transaction':receipt_transaction(current)})
        else:
            # The receipt the older release last accepted, with the skill copies it now finds.
            receipt={**target,'publication_objects':{**target['publication_objects'],**objects}}
            if target['schema_version']==2 and current['schema_version']==2:
                receipt['adopted']={**current['adopted'],**target['adopted']}
        data=encoded(receipt)
        j.advance('receipt_pending')
        j.replace('receipt',root/'receipt.json',lambda p:create(p,data),_exact(data),_exact(previous_bytes))
        j.advance('receipt');checkpoint('receipt')
        pointer=encoded({'schema_version':1,'release_root':str(to_root),'index_sha256':to_index})
        j.advance('pointer_pending')
        j.replace('pointer',root/'current.json',lambda p:create(p,pointer),_exact(pointer),_exact(_pointer(current)))
        j.advance('complete');checkpoint('complete')
        restarted=service.restart(j)
        j.advance('finished')
        result={'status':'upgraded' if kind=='upgrade' else 'rolled_back','installation':'upgraded' if kind=='upgrade' else 'rolled_back',
                'release_root':str(to_root),'package_version':to_manifest['package_version'],'previous_release_root':str(from_root),
                'claude_code':claude_status,'claude_skills':claude_skills_status,'claude_desktop':desktop_status,
                'service':restarted,'next_action':'restart_mcp_clients'}
        if adopted:result['adopted']=sorted(adopted)
        return _result_extras(result,claude_status,desktop_status,to_command,to_arguments)


def _exact(expected):
    def validate(p):
        if read(p,mode=0o600)!=expected:raise InstallError('metadata_conflict')
    return validate


def _finished(receipt, journal, kind):
    """Whether the newest change journal is a finished `kind` that left `receipt`'s release installed."""
    return (journal is not None and journal.get('kind')==kind and journal.get('stage')=='finished'
            and journal.get('index_sha256')==receipt['index_sha256'] and journal.get('release_root')==receipt['release_root'])


def _record(journal, name):
    """A finished journal's single record `name`, or None."""
    publications=journal.get('publications')
    records=publications.get(name) if type(publications) is dict else None
    if records is None:return None
    if type(records) is not list or len(records)!=1 or type(records[0]) is not dict:raise InstallError('transaction_conflict')
    return records[0]


def _reused(root, receipt, name=None, journal=None):
    installed=Path(receipt['release_root'])
    result={'status':'upgraded','installation':'reused','release_root':str(installed),'package_version':receipt['package_version'],
            'claude_code':receipt['claude_code'],'claude_skills':receipt['claude_skills'],'claude_desktop':receipt['claude_desktop'],
            'next_action':'restart_mcp_clients'}
    if _finished(receipt,journal,'upgrade') and receipt_transaction(receipt)==name:
        adopted=_check_adopted(_record(journal,'adopted'))
        if adopted:result['adopted']=sorted(adopted)
    return result


def _rolled_back(receipt, journal):
    """The result a finished rollback returned, from its journal and the receipt it restored."""
    claude_status,desktop_status=((_record(journal,n) or {}).get('status') for n in ('claude_code','claude_desktop'))
    previous=journal.get('from_release_root')
    if (claude_status not in CLAUDE_MODE_STATUSES[receipt['claude_code_mode']] or desktop_status not in CLAUDE_DESKTOP_STATUSES
            or type(previous) is not str):
        raise InstallError('transaction_conflict')
    command,arguments=_commands(Path(receipt['release_root']))
    result={'status':'rolled_back','installation':'reused','release_root':receipt['release_root'],
            'package_version':receipt['package_version'],'previous_release_root':previous,'claude_code':claude_status,
            'claude_skills':receipt['claude_skills'],'claude_desktop':desktop_status,'next_action':'restart_mcp_clients'}
    return _result_extras(result,claude_status,desktop_status,command,arguments)


def _unlink_exact(path, expected):
    """Remove our exact owner-only file through held no-follow parents; False when it differs."""
    fd=_open_parent(path)
    try:
        current,exists=_read_at(fd,path.name,MAX_PLIST_BYTES)
        if not exists:return True
        if current!=expected:return False
        os.unlink(path.name,dir_fd=fd);os.fsync(fd);return True
    finally:os.close(fd)


def _sweep(root):
    """Under the parent lock, with `root` absent: finish what an uninstall left beside it (`_finish_retired`). An
    unfinished first install (the staged root the intent binds; a lookalike under its name is not one) removes nothing
    and refuses as `transaction_pending` (`install`)."""
    if not os.path.lexists(root.parent):return
    with _parent_lock(root.parent):
        if os.path.lexists(root):return
        if _finish_retired(root)=='install':raise InstallError('transaction_pending',pending_action='install')


def _retired_origin(path):
    """The installation root an uninstall retired as `path`, or None: a retired root's own launcher passes `path`,
    so a retry from there finishes that uninstall. Only our closed name holding the directory the intent binds."""
    match=re.fullmatch(r'\.(.+)-removed-[0-9a-f]{32}',path.name)
    if match is None:return None
    origin=path.parent/match.group(1)
    intent=None if os.path.lexists(origin) else _read_intent(origin)
    return origin if intent is not None and path.name in _retired_names(origin,intent) else None


def _codex_removal(config, owner):
    """('remove', source, candidate) for our exact Codex subtree, else ('absent'|'kept', source, None)."""
    source,exists=_config(config)
    subtree=owned_subtree(source) if exists else None
    if subtree is None:return 'absent',source,None
    if digest(subtree)!=owner.post_subtree_hash:return 'kept',source,None
    plan=rollback_registration(source,Receipt(owner.installation_id,owner.transaction_id,None,owner.post_subtree_hash,None))
    return 'remove',source,plan.candidate


def _adoption_backups(root, receipt):
    """Every recorded `--adopt-legacy` backup: adopted.json plus a v2 receipt's own records, sorted."""
    _,records=_adopted_file(root)
    out={(r['transaction'],r['client']):r for r in records}
    if receipt['schema_version']==2:
        for client,value in receipt['adopted'].items():
            out.setdefault((value['transaction'],client),{'client':client,**value})
    return [out[k] for k in sorted(out)]


def _backup_path(root, value):
    return root/'transactions'/value['transaction']/('adopted-'+value['client'])


def _uninstall_service(service):
    """(kind, loaded) of the broker; a foreign definition is kept, never stopped."""
    try:return service.check()
    except InstallError as error:
        if str(error)!='service_conflict':raise
        return 'foreign',False


def uninstall(installation_root, *, user_home, checkpoint=lambda _: None, claude_candidates=None,
              claude_runner=subprocess.run, launchctl=subprocess.run, pending_sends=None):
    """Remove only owned objects: the broker plist (exact label booted out first), the Codex, Claude Code and
    Claude Desktop entries the receipt owns, the owned skill copies and the installation root with its
    releases. The Telegram session, enrollment and Keychain are never touched; `--adopt-legacy` backups
    (adopted.json and a v2 receipt's records) are kept beside the removed root.

    Every refusal, the pending-send gate included, comes before the transaction exists. After a write-ahead
    removal attempt of a Claude Code or Claude Desktop entry the receipt and root stay until the entry is
    confirmed gone (or no longer ours): a still exact entry is removed again, otherwise `uninstall_incomplete`.
    An unfinished first install (published or not) refuses as `transaction_pending` (`install`) and is kept; the
    install intent the closing sweep reads is checked before any client removal or the retiring rename, so a
    lookalike at its recorded staged name never stops an uninstall halfway. Repeated with the root absent (or from
    the retired root's own launcher), it finishes the sweep an interruption after that rename left.
    """
    try:
        require_platform()
        root,home=Path(installation_root),Path(user_home)
        probe=default_pending_sends if pending_sends is None else pending_sends
        if not os.path.lexists(root):
            _sweep(root)
            return {'status':'uninstalled','installation':'absent'}
        origin=_retired_origin(root)
        if origin is not None:
            # Repeated by the retired root's own launcher (the only one left): finish that uninstall.
            _sweep(origin)
            return {'status':'uninstalled','installation':'absent'}
        validate_owned_path(root,private=True)
        from .config import _owner_paths
        session_root,_,label,plist=_owner_paths(home)
        with SessionOwnerLease(root/'.install.lock') as lease:
            # Before any client removal or the rename: the intent the closing sweep reads (any other shape refuses
            # here), and an unfinished first install only when its staged path is the exact directory it binds.
            intent=_read_intent(root)
            if intent is not None and _staged_ours(intent):raise InstallError('transaction_pending',pending_action='install')
            name,journal=_latest_change(root)
            if journal is not None and journal.get('stage') not in ('finished',) and journal.get('kind')!='uninstall':
                raise InstallError('transaction_pending',pending_action=journal.get('kind'))
            if (journal is None or journal.get('kind')!='uninstall') and _install_pending(root):
                raise InstallError('transaction_pending',pending_action='install')
            receipt,_=_installation(root)
            if str(home)!=receipt['user_home']:raise InstallError('upgrade_conflict')
            installed=Path(receipt['release_root'])
            command,arguments=_commands(installed)
            config=Path(receipt['codex_home'])/'config.toml'
            owner=_codex_ownership(root,receipt)
            owned=_desktop_ownership(root,receipt,command,arguments)
            backups=_adoption_backups(root,receipt)
            service=_Service(plist,label,_service_payload(installed,label),runner=launchctl,pending=probe,home=home)
            observed=None
            if journal is None or journal.get('kind')!='uninstall':
                # Every refusal before the transaction exists: a refused uninstall leaves nothing that blocks a
                # later upgrade, repair or rollback.
                for value in backups:
                    if digest(read(_backup_path(root,value),MAX_BACKUP_BYTES,mode=0o600))!=value['sha256']:
                        raise InstallError('transaction_conflict')
                _codex_removal(config,owner)
                observed=_uninstall_service(service)
                identity=dict(schema_version=1,kind='uninstall',index_sha256=receipt['index_sha256'],release_root=receipt['release_root'],
                              installation_root=str(root),user_home=str(home))
                name=_open_change(root,lambda _:{'journal.json':encoded({**identity,'stage':'prepared','publications':{}})})
            else:_sweep_staging(root)
            tx=root/'transactions'/name
            j=_Journal(tx,lease,_UNINSTALL_STEPS,checkpoint)
            if j['index_sha256']!=receipt['index_sha256'] or j['installation_root']!=str(root):raise InstallError('transaction_conflict')
            def done(step,status):
                if j.recorded(step) is None:j.update(single=(step,{'status':status}))
                j.advance(step);checkpoint(step)
                return j.recorded(step)['status']
            out={}
            # Broker: only our exact definition; a loaded one is booted out by label after the pending-send gate
            # (just passed above, or again on a resume).
            if j.recorded('service') is None:
                kind,loaded=observed if observed is not None else _uninstall_service(service)
                if kind=='to':
                    # Write-ahead, so a resume after the unlink reports our definition removed, not absent.
                    if j.recorded('service_owned') is None:j.update(single=('service_owned',{'plist':'to'}))
                    if loaded and _live(launchctl,label) is not None:_bootout(launchctl,label)
                    checkpoint('stopped:service')
                    status='removed' if _unlink_exact(plist,service.kind()[1]) else 'kept'
                elif kind=='absent' and j.recorded('service_owned') is not None:status='removed'
                else:status='absent' if kind=='absent' else 'kept'
            else:status=None
            out['service']=done('service',status)
            # Codex: our subtree removed only while it is exactly what our ownership receipt records.
            if j.recorded('registration') is None:
                j.discard('codex',config)
                status,source,candidate=_codex_removal(config,owner)
                if status=='remove':
                    j.write('codex',config,candidate,source,existed=True,maximum=MAX_CONFIG_BYTES)
                    status='removed'
                elif status=='absent' and any('object' in a for a in j.attempts('codex')):
                    status='removed'  # Our journaled replacement landed before an interruption.
            else:status=None
            out['codex']=done('registration',status)
            # Claude Code: our owned user-scope entry, write-ahead; while it stays exactly ours it is removed again.
            if j.recorded('claude_code') is None:
                status='skipped'
                if receipt['claude_code_mode']=='user':
                    attempts=j.attempts('claude_remove')
                    state=claude_registration.state(home,command,arguments)
                    if not attempts and state=='absent':status='absent'
                    elif not attempts and state=='unavailable':status='unavailable'
                    elif not attempts and (state!='exact' or receipt['claude_code']!='registered'):status='kept'
                    else:
                        cli=None
                        if state=='exact':
                            cli=claude_registration.locate(claude_registration.candidate_paths(home) if claude_candidates is None else claude_candidates)
                        if state=='exact' and cli is not None and len(attempts)<8:
                            j.update(publication=('claude_remove',{'attempt':len(attempts)+1}));checkpoint('recorded:claude_remove')
                            try:claude_registration.remove(claude_runner,cli,home)
                            except claude_registration.ClaudeCliError:pass
                            checkpoint('published:claude_remove')
                            state=claude_registration.state(home,command,arguments)
                        if not attempts and cli is None:status='not_found'
                        elif state=='absent':status='removed'
                        elif state=='different':status='kept'  # No longer ours.
                        else:raise InstallError('uninstall_incomplete')
            else:status=None
            out['claude_code']=done('claude_code',status)
            # Claude Desktop: our owned entry removed, neighbours and the file's mode kept; compare-before-replace,
            # so a still exact entry is removed again after an interruption.
            if j.recorded('claude_desktop') is None:
                add=j.recorded('claude_desktop_add')
                if add is not None:_desktop_discard(j,home)
                state,plan=claude_desktop_registration.observe_removal(home,command,arguments)
                status=None
                if add is None:
                    if state in ('skipped','unavailable','absent'):status=state
                    elif state!='exact' or owned is None:status='kept'
                    else:
                        j.update(single=('claude_desktop_add',{'attempt':1,'existed':plan.existed,'source_sha256':plan.source_sha256,
                                                               'candidate_sha256':plan.candidate_sha256}))
                        checkpoint('recorded:claude_desktop_add')
                if status is None:
                    if state=='exact' and len(j.attempts('claude_desktop_write'))<8:
                        _desktop_write(j,home,plan)
                        checkpoint('published:claude_desktop_add')
                        state,_=claude_desktop_registration.observe_removal(home,command,arguments)
                    if state in ('absent','skipped'):status='removed'
                    elif state=='different':status='kept'  # No longer ours.
                    else:raise InstallError('uninstall_incomplete')
            else:status=None
            out['claude_desktop']=done('claude_desktop',status)
            # Skill copies: only the exact directories the receipt binds; moved into the retiring root.
            if j.recorded('skills') is None:
                statuses={}
                for skill_name,bound in sorted(receipt['publication_objects'].items()):
                    if skill_name=='release':continue
                    base,sid=((Path(receipt['claude_skills_root']),skill_name[7:]) if skill_name.startswith('claude-')
                              else (Path(receipt['skills_root']),skill_name))
                    path,retired=base/sid,tx/('removed-'+skill_name)
                    if retired.exists():statuses[skill_name]='removed';continue
                    if not path.exists() and not path.is_symlink():statuses[skill_name]='absent';continue
                    try:
                        require_object_binding(path,bound['object']);require_object_binding(path/'SKILL.md',bound['child'])
                    except (RuntimePathError,TypeError,KeyError):statuses[skill_name]='kept';continue
                    _rename(path,retired);checkpoint('removed:'+skill_name)
                    statuses[skill_name]='removed'
                j.update(single=('skills',{'status':statuses}))
            j.advance('skills');checkpoint('skills')
            out['skills']=j.recorded('skills')['status']
            # Adoption backups outlive the root; the session and Keychain are not part of it.
            kept_root=None
            if backups:
                record=j.recorded('backups')
                if record is None:
                    j.update(single=('backups',{'path':str(root.parent/('.'+root.name+'-adopted-'+uuid.uuid4().hex[:24]))}))
                    record=j.recorded('backups')
                kept_root=Path(record['path'])
                if kept_root.parent!=root.parent or not kept_root.name.startswith('.'+root.name+'-adopted-'):raise InstallError('transaction_conflict')
                ensure_owned_private_directory(kept_root)
                for value in backups:
                    origin,kept=_backup_path(root,value),kept_root/(value['transaction']+'-'+value['client'])
                    if not kept.exists():
                        if digest(read(origin,MAX_BACKUP_BYTES,mode=0o600))!=value['sha256']:raise InstallError('transaction_conflict')
                        _rename(origin,kept)
            j.advance('retired');checkpoint('retired')
            retired=root.parent/('.'+root.name+'-removed-'+uuid.uuid4().hex)
            _rename(root,retired);checkpoint('published:retired')
        _sweep(root)
        result={'status':'uninstalled','installation':'removed','next_action':'restart_mcp_clients',**out}
        if kept_root is not None:result['legacy_backups']=str(kept_root)
        return result
    except InstallError:raise
    except _REFUSALS:
        raise InstallError('installation_refused') from None


def client_status(installation_root, user_home):
    """Read-only drift of the owned Claude Desktop entry: never writes, runs a CLI or holds a lease.

    `ok` (exactly ours), `drift` (recorded registered/reused but now absent or different: Claude Desktop
    may rewrite its config from memory while running), `not_registered`, `skipped` or `unavailable`.
    """
    try:
        root,home=Path(installation_root),Path(user_home)
        receipt,_=_installation(root)
        command,arguments=_commands(Path(receipt['release_root']))
        observed=claude_desktop_registration.state(home,command,arguments)
    except (InstallError,*_REFUSALS):
        return {'claude_desktop':'unavailable'}
    if observed=='exact':status='ok'
    elif observed in ('skipped','unavailable'):status=observed
    elif receipt['claude_desktop'] in ('registered','reused'):status='drift'
    else:status='not_registered'
    result={'claude_desktop':status}
    if status=='drift':result['claude_desktop_manual_entry']=claude_desktop_registration.manual_entry(command,arguments)
    return result
