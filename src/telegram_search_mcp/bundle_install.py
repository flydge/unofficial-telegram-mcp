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
                           open_owned_file, validate_owned_path, object_binding, require_object_binding, RuntimePathError,
                           ChangedRuntimePath)
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
        try:child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        except OSError:return False  # unreadable (mode 000/300): not provably ours, so it stays
        try:
            if (os.fstat(child).st_dev,os.fstat(child).st_ino)!=(m.st_dev,m.st_ino):return False
            if not empty and not os.listdir(child):return False
            _check_owned(child,allowed,[budget])
        except (_NotOurs,OSError):return False
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
    binds (a directory's inode, and so its binding, survives the retiring rename). At most one. A check that changed
    under it answers nothing and refuses: taking the retired root for a lookalike would drop the intent, its proof."""
    retired=re.compile(re.escape('.'+root.name+'-removed-')+r'[0-9a-f]{32}\Z')
    with open_owned_directory(root.parent) as fd:names=[n for n in os.listdir(fd) if retired.fullmatch(n)]
    out=[]
    for name in names:
        try:require_object_binding(root.parent/name,intent['object'])
        except ChangedRuntimePath:raise
        except RuntimePathError:continue
        out.append(name)
    return out


def _staged_ours(intent):
    """Whether the intent's recorded staged root still is the exact directory the intent binds: a first install not
    yet published. A lookalike under that name is not (the published root holds the bound inode)."""
    if not os.path.lexists(intent['staged_root']):return False
    try:require_object_binding(Path(intent['staged_root']),intent['object'])
    except ChangedRuntimePath:raise
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


def _install_transactions(root):
    """The first-install transaction directories of the published `root` (a bounded listing)."""
    found=[]
    with open_owned_directory(root/'transactions',private=True):pass
    for count,path in enumerate((root/'transactions').iterdir()):
        if count>4*MAX_CHANGES:raise InstallError('recovery_limit')
        if path.name.startswith('install_') and transaction_name(path.name):found.append(path)
    return found


def _install_pending(root):
    """Whether the first install of the published `root` is unfinished: its journal short of `complete`."""
    found=_install_transactions(root)
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


def _discard_journaled(publications, name, path, discard=discard_temporary):
    """Unlink every temporary the journaled attempts of `name` (in a journal's `publications`) left beside `path`:
    only a name recorded before its creation, and once its inode was journaled only that inode
    (`discard_temporary`). Anything else beside the file is never touched."""
    attempts=publications.get(name,[])
    if type(attempts) is not list or len(attempts)>8 or any(type(a) is not dict for a in attempts):
        raise InstallError('transaction_conflict')
    if attempts and os.path.lexists(Path(path).parent):
        for record in attempts:discard(path,record)


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

    def update(self, stage=None, publication=None, single=None, amend=None, put=None, clear=()):
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
        if put is not None:
            # One fixed record replaced in place, with the per-cycle records `clear` names dropped in the same
            # write: a record kept per cycle, so the journal stays the same size however many cycles there are.
            name,value=put
            next_value['publications']={**{k:v for k,v in next_value['publications'].items() if k not in clear},name:[value]}
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
        _discard_journaled(self.value['publications'],name,path,discard)

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

    def _holds(self, bound, child):
        """Whether the bound packet still holds its staged object, unpublished. Once published (renamed or swapped
        into place) it does not, so a target removed or changed since is staged afresh."""
        staged=bound.get('staged')
        if staged is None:return False  # A reused existing object (an earlier upgrade's release tree) has no packet.
        if type(staged) is not str or not Path(staged).is_relative_to(self.tx):raise InstallError('transaction_conflict')
        try:
            require_object_binding(Path(staged),bound['object'])
            if child:require_object_binding(Path(staged)/'SKILL.md',bound['child'])
        except ChangedRuntimePath:raise
        except (RuntimePathError,KeyError):return False
        return True

    def _stage(self, name, target, build, validate, directory, child):
        """The bound staged object of `name`: the packet an earlier run left while it still holds that object and
        `validate` accepts it, else a fresh packet (the bound one was published since, or holds what an earlier
        cycle built from other data, such as a receipt naming a skill copy written again since)."""
        bound=self.proof(name)
        if bound is not None and self._holds(bound,child):
            staged=Path(bound['staged'])
            try:validate(staged);return bound,staged
            except (InstallError,BundleError):bound=None
        else:bound=None
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

    def _published(self, name, target, validate, child):
        """Whether `target` is the exact object an attempt of `name` staged (and so this transaction published),
        holding what `validate` accepts: its own copy, even when a later attempt staged another since (the user
        edited it in place, then put it back exactly). An edit in place that kept the binding is not."""
        for attempt in self.attempts(name):
            try:
                require_object_binding(target,attempt['object'])
                if child:require_object_binding(target/'SKILL.md',attempt['child'])
            except ChangedRuntimePath:raise
            except (RuntimePathError,KeyError):continue
            try:validate(target)
            except ChangedRuntimePath:raise
            except (InstallError,BundleError,RuntimePathError):return False
            return True
        return False

    def replace(self, name, target, build, validate, replaceable, *, directory=False, child=False):
        """Atomically exchange `target` (accepted by `replaceable`, or an object an earlier attempt of `name`
        published) with a bound staged object.

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
            if not self._published(name,target,validate,child):replaceable(target)
            _swap(staged,target)
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


# Every journaled write beside a file outside the installation root: (record name, file).
_CLIENT_WRITES = (('registration', 'codex'), ('codex', 'codex'), ('claude_desktop_write', 'desktop'),
                  ('claude_desktop_restore', 'desktop'), ('plist', 'plist'), ('plist_restore', 'plist'),
                  ('policy', 'policy'), ('policy_restore', 'policy'))


def _client_temporaries(publications, home, config, plist, session_root):
    """Every temporary a transaction's journaled writes left beside the files it touches outside the root (the
    Codex config, the Claude Desktop config, the broker plist and runtime.toml), each provably ours
    (`_discard_journaled`). Run by a late refusal's restore and by every finishing action (install, upgrade and
    rollback before the commit; uninstall for its own journal and a parked change's): a step skipped since (a
    repeat refused before it, its outcome recorded, a parked change an uninstall goes over) would otherwise leave
    a copy of a client file there for good."""
    paths={'codex':config,'desktop':claude_desktop_registration.config_path(home),'plist':plist,
           'policy':session_root/'runtime.toml'}
    def desktop(_, record):claude_desktop_registration.discard_temporary(home,record)
    for name,key in _CLIENT_WRITES:
        _discard_journaled(publications,name,paths[key],desktop if key=='desktop' else discard_temporary)


def _desktop_write(j, home, plan):
    """One journaled compare-before-replace attempt; the confirming read decides its outcome."""
    def write(temporary, admit):
        try:claude_desktop_registration.write(home,plan,admit,temporary)
        except ConfigConflict:pass
    j.attempt('claude_desktop_write',write)


def _desktop_again(j, plan):
    """After an interruption, whether our write may be made again: a plan over the current bytes (our entry still
    absent, owned or adopted; every neighbour as the file holds it now), within the bounded attempts."""
    return plan is not None and len(j.attempts('claude_desktop_write'))<8


CLAUDE_SKILLS = CLAUDE_SKILLS_STATUSES
SKILL_NAMES = ('telegram-search', 'telegram-setup', 'claude-telegram-search', 'claude-telegram-setup')
_MANUAL = ('failed', 'unavailable', 'not_found')
_DESKTOP_MANUAL = ('failed', 'unavailable')
_REFUSALS = (BundleError, RuntimePathError, ConfigConflict, OwnerLeaseError, OSError, ValueError, KeyError, TypeError)
# The broker stop (`service`) directly follows its last pending-send gate and precedes the first client write.
_INSTALL_STEPS = ['prepared','release_pending','release','service_pending','service','registration_pending','registration',
                  'telegram-search_pending','telegram-search','telegram-setup_pending','telegram-setup',
                  'claude-telegram-search_pending','claude-telegram-search','claude-telegram-setup_pending',
                  'claude-telegram-setup','claude_code_pending','claude_code','claude_desktop_pending','claude_desktop',
                  'policy_pending','policy','receipt_pending','receipt','complete']
_CHANGE_STEPS = ['prepared','release_pending','release','service_pending','service','registration_pending','registration',
                 *[s for n in SKILL_NAMES for s in (n+'_pending',n)],
                 'claude_code_pending','claude_code','claude_desktop_pending','claude_desktop',
                 'policy_pending','policy','receipt_pending','receipt','pointer_pending','complete','finished']
_UNINSTALL_STEPS = ['prepared','service','registration','claude_code','claude_desktop','skills','retired']
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
MAX_POLICY_BYTES = 16384
MAX_PLIST_BYTES = 1024 * 1024
MAX_CHANGES = 999
# Run under the interpreter of the broker it asks: only that release's own client passes the
# broker's version handshake. Any client context may hold a draft or an unresolved send outcome.
# It uses only what every broker this installer adopts or upgrades ships, a 0.51 or 0.52 source
# venv included: config.BROKER_SOCKET_PATH and load_runtime_policy, broker_client.BrokerClient,
# its health() and close().
_PENDING_PROBE = ("import sys\nsys.path[:0]=[p for p in sys.argv[1:2] if p]\n"
                  "from telegram_search_mcp.config import BROKER_SOCKET_PATH,load_runtime_policy\n"
                  "from telegram_search_mcp.broker_client import BrokerClient\n"
                  "c=BrokerClient(socket_path=BROKER_SOCKET_PATH,policy=load_runtime_policy(),request_timeout=5,\n"
                  " restart_callback=lambda:None,retry_backoff_seconds=0)\ntry:\n h=c.health()\n"
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


_CODE = re.compile(r'[a-z][a-z_-]{0,63}\Z')


def _refusal(error):
    """The fixed code a late refusal is recorded and raised again with: an installer code, else the generic one."""
    code=error.args[0] if type(error) is InstallError and len(error.args)==1 else None
    return code if type(code) is str and _CODE.fullmatch(code) and code!='transaction_pending' else 'installation_refused'


def _uncommitted(path, previous):
    """Whether the commit point has not passed: `current.json` still holds `previous` (None: still absent)."""
    if previous is None:return not os.path.lexists(path)
    try:return read(path,mode=0o600)==previous
    except (BundleError,OSError,ValueError):return False


# The records of one stop cycle's writes (the plist and the runtime.toml version line, and their restores): a
# finished restore drops them with its closing record, so every cycle starts them afresh.
_CYCLE_WRITES = ('plist', 'plist_restore', 'policy', 'policy_restore')
_CYCLE_STATES = ('stopped', 'restoring', 'restored')


class _Service:
    """The exact receipt-bound (or adopted) broker LaunchAgent: stop, rewrite, restart, and restore.

    `known` maps each acceptable on-disk definition kind to its payload; `adopted` is the sha256 of an
    `--adopt-legacy` plist backup. Only the exact label is ever booted out or bootstrapped.

    Stops and restores come in cycles, journaled in one fixed record, `service_cycle`, replaced in place:
    `stopped` once a cycle's stop is done, `restoring` (write-ahead, with the refusal code) and `restored`
    around a restore. A restore brings the previous definition back byte for byte (`service.previous`, kept
    before the first rewrite) and bootstraps the previous broker when it was loaded; the next run starts a new
    cycle. Every repeat is a run the user starts, so cycles are not limited, and the journal does not grow
    with them: a finished restore drops its cycle's write records (`_CYCLE_WRITES`).
    """

    def __init__(self, plist, label, target, *, known=(), adopted=None, runner, pending, home):
        self.plist,self.label,self.target=plist,label,target
        self.known=dict(known);self.adopted=adopted;self.runner=runner;self.pending=pending;self.home=home
        self.stopping=False  # This run issued a bootout its cycle may not have recorded yet.

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

    def cycle(self, j):
        """(number, state, code) of the journaled stop cycle: (0, None, None) before the first stop; the code only
        while `restoring`."""
        record=j.recorded('service_cycle')
        if record is None:return 0,None,None
        number,state,code=record.get('cycle'),record.get('state'),record.get('code')
        if (type(number) is not int or number<1 or state not in _CYCLE_STATES
                or set(record)!=({'cycle','state','code'} if state=='restoring' else {'cycle','state'})
                or state=='restoring' and (type(code) is not str or not _CODE.fullmatch(code))):
            raise InstallError('transaction_conflict')
        return number,state,code

    def stopped(self, j):
        """Whether this transaction's stop is in effect: done in the current cycle, and no restore begun since."""
        return self.cycle(j)[1]=='stopped'

    def observe(self, j):
        """Before the first client write: record the observation once; while a recorded loaded broker runs,
        every run passes the pending-send gate here, so a late send refuses with no client changed."""
        if j.recorded('service') is None:
            kind,loaded=self.check()
            j.update(single=('service',{'plist':kind,'loaded':loaded}))
            return
        if self._observed(j)['loaded'] and not self.stopped(j):self._recheck()

    def stop(self, j):
        """Idempotent by state and run by every run until the commit, ahead of the client steps, so a refusal in a
        new cycle's stop leaves every client entry unchanged: the service becomes the target's whatever stage the
        journal records. A new cycle boots out a still-loaded exact registration, gated again right before, and
        records its stop. While a cycle's stop is in effect, a previous definition loaded again (a login loads the
        plist on disk) is gated and booted out again. Both by state, not by the observation: a broker a login
        started in a change that found none running is stopped too (and, as none ran before, `restart` starts
        none), so the previous broker never runs on the target's plist and pin. Then the plist is rewritten, its
        exact previous bytes kept first, until it is the target's."""
        observed=self._observed(j)
        number,state,_=self.cycle(j)
        if state=='restoring':raise InstallError('transaction_conflict')  # `restore` finishes it first.
        if state!='stopped':
            if self._recheck():
                self.stopping=True;_bootout(self.runner,self.label)
            j.update(put=('service_cycle',{'cycle':number+1,'state':'stopped'}));j.checkpoint('stopped:service')
        elif self.kind()[0]!='to' and self._recheck():
            self.stopping=True;_bootout(self.runner,self.label)
        if observed['plist']!='absent':
            j.discard('plist',self.plist)
            kind,data,_=self.kind()
            if kind!='to':
                self._keep(j,data)
                j.write('plist',self.plist,_plist_bytes(self.target),data,existed=True,maximum=MAX_PLIST_BYTES)
            if self.kind()[0]!='to':raise InstallError('service_conflict')

    def _keep(self, j, data):
        """Once, before the first rewrite: the definition's exact bytes kept in the transaction directory, their
        hash journaled first, so a restore writes them back byte for byte."""
        if j.recorded('service_previous') is None:j.update(single=('service_previous',{'sha256':digest(data),'bytes':len(data)}))
        if not os.path.lexists(j.tx/'service.previous'):
            if digest(data)!=j.recorded('service_previous')['sha256']:raise InstallError('service_conflict')
            create(j.tx/'service.previous',data,maximum=MAX_PLIST_BYTES)
        self._kept(j)

    def _kept(self, j):
        record=j.recorded('service_previous')
        data=read(j.tx/'service.previous',MAX_PLIST_BYTES,mode=0o600) if record is not None else None
        if data is None or digest(data)!=record.get('sha256') or len(data)!=record.get('bytes'):
            raise InstallError('transaction_conflict')
        return data

    def unfinished(self, j):
        """The refusal code of a restore a kill interrupted (its write-ahead `restoring` record), else None. The run
        finishes that restore and raises the same code."""
        _,state,code=self.cycle(j)
        return code if state=='restoring' else None

    def restore(self, j, code, undo):
        """Before the refusal `code`, raised after the stop and short of the commit: the target's broker booted
        out if one is loaded, `undo()` (the temporaries killed client writes left removed, `_client_temporaries`,
        and the runtime.toml version line back), then the previous definition back, byte for byte, and the
        previous broker bootstrapped again when it was loaded before the change, so the old release keeps
        serving and a parked change leaves no copy of a client file beside it.

        Idempotent by state, like the stop: every effect is decided from the files and launchd, whatever an
        earlier run left. A login since the plist rewrite loads the target's plist, so the target's broker may
        be loaded: it is booted out first (no pending-send gate: short of the commit its release is not admitted,
        `current.json` not naming it, so it serves no session). The previous broker loaded again (a login after
        the definition is back) stays. A loaded broker that is neither stays as well and refuses
        `service_conflict`, as does a definition changed since the stop, which is never overwritten. A cycle
        whose stop changed nothing restores nothing. The closing record drops the cycle's write records, whose
        temporaries the restore has discarded."""
        number,state,_=self.cycle(j)
        if state!='restoring':
            if state!='stopped':
                if not self.stopping:return
                number+=1  # This run's bootout came before its cycle's record.
            j.update(put=('service_cycle',{'cycle':number,'state':'restoring','code':code}))
            j.checkpoint('recorded:service_restore')
        observed=self._observed(j)
        live=_live(self.runner,self.label)
        if live is not None and observed['plist']!='to' and _matches_live(live,self.plist,self.target):
            _bootout(self.runner,self.label);live=None
        undo()
        if observed['plist']!='absent':
            j.discard('plist',self.plist);j.discard('plist_restore',self.plist)
            kind,data,_=self.kind()
            if kind=='to' and observed['plist']!='to':
                j.write('plist_restore',self.plist,self._kept(j),data,existed=True,maximum=MAX_PLIST_BYTES)
        if live is None:
            if observed['loaded']:_bootstrap(self.runner,self.plist)
        else:
            kind,_,payload=self.kind()
            if kind=='absent' or kind!='adopted' and not _matches_live(live,self.plist,payload):
                raise InstallError('service_conflict')
        j.update(put=('service_cycle',{'cycle':number,'state':'restored'}),clear=_CYCLE_WRITES)
        j.checkpoint('restored:service')

    def restart(self, j):
        """After the commit: bootstrap the rewritten definition only when the stopped broker was loaded, and only
        while the plist is the target's (the stop made it so before the commit); any other definition refuses."""
        observed=j.recorded('service')
        if observed is None or not observed['loaded']:return 'not_loaded'
        if observed['plist']=='absent':return 'stopped'
        if j.recorded('service_restarted') is None:
            if self.kind()[0]!='to':raise InstallError('service_conflict')
            live=_live(self.runner,self.label)
            if live is None:_bootstrap(self.runner,self.plist)
            elif not _matches_live(live,self.plist,self.target):raise InstallError('service_conflict')
            j.update(single=('service_restarted',{'attempt':1}));j.checkpoint('restarted')
        return 'restarted'


_PIN = re.compile(rb'expected_package_version = "([0-9A-Za-z.+-]{1,64})"')
_VALUE = re.compile(r'[0-9A-Za-z.+-]{1,64}\Z')


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
        # Write-ahead, once: the value a late refusal's restore puts back (`_policy_back`).
        if j.recorded('policy_previous') is None:
            j.update(single=('policy_previous',{'version':next(_PIN.fullmatch(line).group(1).decode()
                                                               for line in data.split(b'\n') if _PIN.fullmatch(line))}))
        j.write('policy',policy,candidate,data,existed=True,maximum=MAX_POLICY_BYTES)


def _policy_back(j, session_root, new):
    """Part of a late refusal's restore, before the broker starts again: the version line this transaction
    rewrote to `new` goes back to the value recorded before its first rewrite, so the restored broker accepts
    its pin (journaled `policy_restore`). A line that no longer names `new` stays as it is."""
    record=j.recorded('policy_previous')
    if record is None or not os.path.lexists(session_root):return
    value=record.get('version')
    if type(value) is not str or not _VALUE.fullmatch(value):raise InstallError('transaction_conflict')
    policy=session_root/'runtime.toml'
    j.discard('policy',policy);j.discard('policy_restore',policy)
    data,exists=_read_file(policy,MAX_POLICY_BYTES)
    try:back=_policy_candidate(data,new,value) if exists else None
    except InstallError:return
    if back is not None and back!=data:
        j.write('policy_restore',policy,back,data,existed=True,maximum=MAX_POLICY_BYTES)


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


def _owned_desktop(value, command, arguments):
    """`value` when it is a Claude Desktop ownership record of exactly (command, arguments), else None."""
    if value is None:return None
    owned=Receipt.from_bytes(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
    expected=hashlib.sha256(claude_desktop_registration.subtree(command,arguments)).hexdigest()
    return value if owned.client=='claude-desktop' and owned.post_subtree_hash==expected else None


def _client_owners(root, below=None, parked=None):
    """The Claude Code and Claude Desktop entries the installation owns: {client: (command, arguments, record)}.

    For each client, the entry written by the newest finished transaction whose step for that client ended
    `registered`: its release's command, and for Claude Desktop its ownership record. An upgrade or rollback whose
    step ended otherwise (a CLI failure, no CLI, an unreadable config, a kill in a write-ahead window, `reused`)
    replaced nothing of ours, so the earlier entry stays ours while it is still exactly that entry. Read newest
    first from the journals: the finished changes numbered below `below` (the one being run does not count yet),
    then the first install. `parked` names an unfinished change an uninstall goes over (`_parked`): its recorded
    outcomes count like a finished one's. A client missing from the result owns nothing. The same for v1 and v2
    receipts.
    """
    paths=[root/'transactions'/('change_%d'%n)/'journal.json' for n in reversed(_changes(root)) if below is None or n<below]
    found=_install_transactions(root)
    if len(found)==1:paths.append(found[0]/'journal.json')
    owners,decided={},set()
    for path in paths:
        if len(decided)==2:break
        journal=unique_json(read(path,mode=0o600))
        if path.parent.name.startswith('change_') and (journal.get('kind') not in ('upgrade','rollback')
                                                      or journal.get('stage')!='finished' and path.parent.name!=parked):
            raise InstallError('transaction_conflict')
        if type(journal.get('release_root')) is not str:raise InstallError('transaction_conflict')
        command,arguments=_commands(Path(journal['release_root']))
        for client in ('claude_code','claude_desktop'):
            if client in decided or (_record(journal,client) or {}).get('status')!='registered':continue
            decided.add(client)
            if client=='claude_code':owners[client]=(command,arguments,None);continue
            value=_owned_desktop((_record(journal,'claude_desktop_add') or {}).get('ownership'),command,arguments)
            if value is not None:owners[client]=(command,arguments,value)
    return owners


def _codex_change(source, registration, ownership):
    """Our owned subtree replaced in place; an entry the user removed is added back like a first install."""
    return merge_registration(source,registration,None if owned_subtree(source) is None else ownership)


def _codex_write(j, config, before, existed, candidate, post_hash, replan, code):
    """The journaled Codex write (`registration`) over the file as it is now, run by every run until the commit.

    Over the exact preimage the prepared candidate is written. When the file changed since, only the
    `telegram_search` entry decides: once the journal records the write done (stage `registration`), our
    written entry (`post_hash`) is done, whatever the neighbours; still the preimage's (ours, or the adopted
    foreign one) or removed, the same edit is planned again over the current bytes (`replan`, which must yield
    `post_hash`), keeping every neighbour byte the user wrote meanwhile; any other entry refuses as `code`,
    untouched, even one equal to ours before the write is recorded done (only the object this transaction
    wrote is ever taken as its own). A file changed between the write's record and its replace is planned
    again the same way, a bounded number of times.
    """
    j.discard('registration',config)
    bound=j.proof('registration')
    if bound is not None and 'object' in bound:
        try:require_object_binding(config,bound['object']);return
        except RuntimePathError:pass
    landed=j.reached('registration')
    try:reference=owned_subtree(before)
    except ConfigConflict:raise InstallError('transaction_conflict') from None
    for _ in range(3):
        current,exists=_read_file(config,MAX_CONFIG_BYTES)
        if (current,exists)!=(before,existed):
            try:
                mine=owned_subtree(current)
                if landed and mine is not None and digest(mine)==post_hash:return
                if mine is not None and mine!=reference:raise InstallError(code)
                change=replan(current,mine is not None)
            except ConfigConflict:raise InstallError(code) from None
            if change.post_subtree_hash!=post_hash:raise InstallError(code)
            before,existed,candidate=current,exists,change.candidate
        try:
            j.write('registration',config,candidate,before,existed=existed,maximum=MAX_CONFIG_BYTES)
            return
        except ConfigConflict as error:  # Changed between the write's record and its replace.
            if error.code!='concurrent_change':raise
        except InstallError as error:  # Changed between this read and the write's own.
            if str(error)!='target_conflict':raise
    raise InstallError(code)


def _codex_check(config, registration, ownership, code):
    """After the Codex step: our entry is exactly the one this transaction wrote (a no-op merge), else `code`."""
    try:
        if merge_registration(read_config(config),registration,ownership).is_noop:return
    except ConfigConflict:pass
    raise InstallError(code)


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


def _plugin_note(result, mode, home):
    """Read-only, in every plugin-mode result: a user-scope entry beside the plugin would duplicate the tool
    set (`claude_code_user_entry: present`); it is never removed here."""
    if mode=='plugin' and claude_registration.present(home)=='present':result['claude_code_user_entry']='present'
    return result


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
    the receipt; never by default. A refusal after the adopted broker's stop and before `current.json` exists
    first restores its exact definition and, when it was loaded, the broker (`_Service.restore`). A Codex
    config edited while the install runs is merged (`_codex_write`). `launchctl` and `pending_sends` are the
    injectable service boundary.
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
            # A restore a kill interrupted is finished first, and its refusal raised again (`_Service.restore`).
            unfinished=service.unfinished(j) if service is not None else None
            def undo():
                _client_temporaries(j.value['publications'],home,config,plist,session_root)
                _policy_back(j,session_root,manifest['package_version'])
            if unfinished is not None:
                service.restore(j,unfinished,undo);raise InstallError(unfinished)
            try:
                # Its observation, its pending-send gate and its stop all come before the first client write: a send
                # that starts before the stop refuses with every client entry still as it was. Every run until the
                # commit makes the service the target's, whatever the stage (`_Service.stop`); after a restore the
                # next run stops it again.
                if service is not None and not reached('complete'):
                    service.observe(j)
                    j.advance('service_pending');service.stop(j)
                j.advance('service_pending');j.advance('service')
                if not reached('complete'):
                    j.advance('registration_pending')
                    # Each attempt is journaled (and counted) before its temporary exists; the write goes over the
                    # current bytes (`_codex_write`), a killed attempt's temporary discarded first. Until the commit
                    # every run writes our entry again if it was removed since.
                    def replan(source, present):
                        return (adopt_registration if 'codex' in adopted and present else merge_registration)(source,registration)
                    _codex_write(j,config,before,journal['config_existed'],candidate,ownership.post_subtree_hash,replan,
                                 'foreign_registration')
                    j.advance('registration')
                # Our entry changed after the write: inside the transaction the same code as before it.
                _codex_check(config,registration,ownership,'registration_conflict' if reached('complete') else 'foreign_registration')
                checkpoint('registration')
                # `~/.claude/skills` is touched only when Claude Code is evidently present; decided once per transaction.
                if recorded('claude_skills') is None:
                    present=claude_code=='user' and (os.path.lexists(home/'.claude') or cli is not None)
                    update(single=('claude_skills',{'status':'installed' if present else 'skipped'}))
                claude_skills_status=recorded('claude_skills').get('status')
                if claude_skills_status not in CLAUDE_SKILLS:raise InstallError('transaction_conflict')
                # Once receipt.json is the receipt this install published, the skill copies are done: it records them,
                # and a copy the user edited, replaced or removed since stays as it is and never blocks the repeat.
                published=_published_receipt(j,root)
                for prefix,base in (('',skills),('claude-',claude_skills)):
                    for skill in manifest['skills']:
                        sid,expected=skill['id'],skill['sha256'];name=prefix+sid;target=base/sid
                        if not reached(name+'_pending'):update(name+'_pending')
                        # An external client directory the user may wipe: published once, never re-verified later.
                        if published is not None or prefix and (claude_skills_status=='skipped' or reached(name)):
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
                    interruption it is written again, planned over the current bytes, while it is still absent (or adopted)."""
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
                        if state!='absent' or not _desktop_again(j,change):return 'failed'
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
                    if not reached('complete'):
                        j.advance('policy_pending')
                        # The adopted broker's session pin follows it (any version), only that line; checked by
                        # every run before the commit, since a late refusal's restore puts the old line back.
                        if 'service' in adopted:_policy_step(j,session_root,None,manifest['package_version'])
                        j.advance('policy')
                        _client_temporaries(j.value['publications'],home,config,plist,session_root)
                    names=['release','telegram-search','telegram-setup']
                    if claude_skills_status=='installed':names+=['claude-telegram-search','claude-telegram-setup']
                    objects=({name:published['publication_objects'][name] for name in names} if published is not None
                             else {name:{k:v for k,v in proof(name).items() if k!='staged'} for name in names})
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
                    _plugin_note(result,claude_code,home)
                    return _result_extras(result,claude_status,desktop_status,command,arguments)
                except _REFUSALS:
                    rollback_claude()
                    rollback_desktop()
                    raise
            except _REFUSALS as error:
                # Short of the commit the adopted broker keeps serving: its definition and broker come back first.
                if service is not None and _uncommitted(root/'current.json',None):service.restore(j,_refusal(error),undo)
                raise
    except InstallError:raise
    except _REFUSALS:
        raise InstallError('installation_refused') from None


# The one release version grammar (docs/releasing.md, Versions), as release_archive and the native seed admit it.
_VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})'
                      r'(?:(a|b|rc)(0|[1-9][0-9]{0,5}))?(?:\.dev(0|[1-9][0-9]{0,5}))?')
_INF = float('inf')


def _version_key(value):
    """PEP 440 order of a release version (X.Y.Z[{a|b|rc}N][.devN]); anything else refuses."""
    match=_VERSION.fullmatch(value) if type(value) is str else None
    if match is None:raise InstallError('downgrade_refused')
    major,minor,micro,letter,number,dev=match.groups()
    if letter is not None:pre=(('a','b','rc').index(letter),int(number))
    elif dev is not None:pre=(-_INF,0)  # X.devN sorts before X's pre-releases.
    else:pre=(_INF,0)
    return ((int(major),int(minor),int(micro)),pre,_INF if dev is None else int(dev))


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

    Every refusal of a downgrade or a foreign or drifted object comes before the transaction exists; so does
    the first pending-send gate. Order: adoption records (adopted.json), release, the broker observation with
    its pending-send gate, the broker stop (gated again right before it) and plist, then Codex, skills, Claude
    Code, Claude Desktop, the runtime.toml version line, receipt.json, then current.json (the commit). A
    pending send found inside the transaction refuses before any client write, leaving the copied release
    and the open transaction for the repeat. Only a broker that was loaded is bootstrapped again, after the
    commit, because a release is admitted only once current.json names it.

    A client file edited while the transaction runs is merged: only a change of our own entry (or the adopted
    one) refuses (`_codex_write`; Claude Code and Claude Desktop are planned over the current file). Any refusal
    after the stop and short of the commit first restores the previous definition and broker
    (`_Service.restore`), so the old release keeps serving; the copied release, the open transaction and the
    client entries already written stay. The change is then parked (`_parked`): the same command stops the
    broker again and resumes, as often as it is repeated, and `uninstall` goes over it, removing what it wrote;
    another kit or a rollback names it (`transaction_pending`).
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
        # The entries this installation owns now: the from-release's, or one an earlier step left in place.
        owners=_client_owners(root,int(name[7:]) if resuming else None)
        claude_owned,desktop_owned=owners.get('claude_code'),owners.get('claude_desktop')
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
                if (state=='different' and claude_registration.state(home,from_command,from_arguments)!='exact'
                        and (claude_owned is None or claude_registration.state(home,*claude_owned[:2])!='exact')):
                    data=claude_registration.entry_backup(home) if adopt and kind=='upgrade' else None
                    if data is None or len(data)>MAX_BACKUP_BYTES:raise InstallError('claude_registration_conflict')
                    adopted['claude_code']=data
            state,_=claude_desktop_registration.observe_change(home,desktop_owned[:2] if desktop_owned else None,
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
        # A restore a kill interrupted is finished first, and its refusal raised again (`_Service.restore`).
        unfinished=service.unfinished(j)
        def undo():
            _client_temporaries(j.value['publications'],home,config,plist,session_root)
            _policy_back(j,session_root,to_manifest['package_version'])
        if unfinished is not None:
            service.restore(j,unfinished,undo);raise InstallError(unfinished)
        # The broker observation, its pending-send gate and the stop of a loaded broker (gated again right before it)
        # all come before the first client write: no session can start between the last gate and the stop, and a
        # send that starts before it refuses with every client entry, the plist and the session unchanged. Every run
        # until the commit makes the service the target's, whatever the stage (`_Service.stop`); after a restore
        # the next run stops it again.
        try:
            if not j.reached('complete'):
                service.observe(j)
                j.advance('service_pending');service.stop(j);j.advance('service')
            checkpoint('service')
            # Codex: our owned subtree replaced in place, over the current bytes (`_codex_write`); until the commit
            # every run writes it again if it was removed since and checks it. After the commit it is done: a table
            # the user changed or removed since stays as it is (`kept` by uninstall while changed), and the repeat
            # finishes.
            if not j.reached('complete'):
                j.advance('registration_pending')
                ensure_owned_private_directory(codex,private_leaf=False)
                def replan(source, present):
                    if 'codex' in adopted and present:return adopt_registration(source,to_registration)
                    return _codex_change(source,to_registration,ownership)
                _codex_write(j,config,before,j['config_existed'],candidate,new_ownership.post_subtree_hash,replan,'target_conflict')
                j.advance('registration')
                _codex_check(config,to_registration,new_ownership,'target_conflict')
            checkpoint('registration')
            # Once receipt.json is the receipt this change published, the skill copies are done: it records them, and
            # they are not written or checked again. One the user edited, replaced or removed since stays as it is
            # (`kept` by uninstall while edited); the repeat finishes, and the next upgrade decides it as usual.
            published=_published_receipt(j,root)
            objects={}
            to_skills={s['id']:s['sha256'] for s in to_manifest['skills']}
            for skill_name in SKILL_NAMES:
                if skill_name not in names:
                    j.advance(skill_name+'_pending');j.advance(skill_name);checkpoint(skill_name);continue
                if published is not None:
                    # The binding the published receipt records: the copy this change wrote, owned while it is that copy.
                    bound=published['publication_objects'].get(skill_name)
                    if type(bound) is not dict or type(bound.get('object')) is not dict or type(bound.get('child')) is not dict:
                        raise InstallError('transaction_conflict')
                    objects[skill_name]={'object':bound['object'],'child':bound['child']}
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
                    removable=(claude_owned is not None and claude_registration.state(home,*claude_owned[:2])=='exact'
                               or 'claude_code' in adopted and claude_registration.adopted(home,adopted['claude_code']['sha256']))
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
                """Owned (or adopted) replace, or an absent entry added, mode kept; after an interruption it is made
                again, planned over the current bytes, while the entry is still owned, adopted or absent."""
                def state():return claude_desktop_registration.state(home,to_command,to_arguments)
                add=j.recorded('claude_desktop_add')
                if add is not None:
                    _desktop_discard(j,home)
                    if state()=='exact':return 'registered'
                observed,plan=claude_desktop_registration.observe_change(
                    home,desktop_owned[:2] if desktop_owned else None,to_command,to_arguments,
                    adopted=adopted['claude_desktop']['sha256'] if 'claude_desktop' in adopted else None)
                if add is not None:
                    if observed not in ('owned','adopted','absent') or not _desktop_again(j,plan):return 'failed'
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
            # Checked by every run before the commit: a late refusal's restore puts the old line back.
            if not j.reached('complete'):
                j.advance('policy_pending')
                _policy_step(j,session_root,None if 'service' in adopted else current['package_version'],to_manifest['package_version'])
                j.advance('policy')
                _client_temporaries(j.value['publications'],home,config,plist,session_root)
            checkpoint('policy')
            # Ownership follows the entry: the one this transaction wrote, else the one it left in place (`_client_owners`).
            add=j.recorded('claude_desktop_add')
            desktop_owner=add['ownership'] if desktop_status=='registered' and add else desktop_owned[2] if desktop_owned else None
            if kind=='upgrade':
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
                # The receipt the older release last accepted (its schema), with the skill copies it now finds and this
                # rollback's own client outcomes: it owns what it wrote, so uninstall and the next upgrade see it.
                receipt={**target,'publication_objects':{**target['publication_objects'],**objects},
                         'claude_code':claude_status,'claude_desktop':desktop_status}
                if target['schema_version']==2:
                    receipt['claude_desktop_ownership']=desktop_owner
                    if current['schema_version']==2:receipt['adopted']={**current['adopted'],**target['adopted']}
            data=encoded(receipt)
            j.advance('receipt_pending')
            j.replace('receipt',root/'receipt.json',lambda p:create(p,data),_exact(data),_exact(previous_bytes))
            j.advance('receipt');checkpoint('receipt')
            pointer=encoded({'schema_version':1,'release_root':str(to_root),'index_sha256':to_index})
            j.advance('pointer_pending')
            j.replace('pointer',root/'current.json',lambda p:create(p,pointer),_exact(pointer),_exact(_pointer(current)))
        except _REFUSALS as error:
            # Short of the commit the old release keeps serving: its definition and broker come back first.
            if _uncommitted(root/'current.json',_pointer(current)):service.restore(j,_refusal(error),undo)
            raise
        j.advance('complete');checkpoint('complete')
        restarted=service.restart(j)
        j.advance('finished')
        result={'status':'upgraded' if kind=='upgrade' else 'rolled_back','installation':'upgraded' if kind=='upgrade' else 'rolled_back',
                'release_root':str(to_root),'package_version':to_manifest['package_version'],'previous_release_root':str(from_root),
                'claude_code':claude_status,'claude_skills':claude_skills_status,'claude_desktop':desktop_status,
                'service':restarted,'next_action':'restart_mcp_clients'}
        if adopted:result['adopted']=sorted(adopted)
        _plugin_note(result,mode,home)
        return _result_extras(result,claude_status,desktop_status,to_command,to_arguments)


def _published_receipt(j, root):
    """The receipt value an attempt of this change's `receipt` step published, while receipt.json is still that
    exact object; else None (not published yet, or changed by hand since, which the receipt step refuses)."""
    path=root/'receipt.json'
    for attempt in j.attempts('receipt'):
        if type(attempt.get('object')) is dict and _bound(path,attempt['object']):
            value=unique_json(read(path,mode=0o600))
            if type(value.get('publication_objects')) is not dict:raise InstallError('transaction_conflict')
            return value
    return None


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
    return _plugin_note(result,receipt['claude_code_mode'],Path(receipt['user_home']))


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
    _plugin_note(result,receipt['claude_code_mode'],Path(receipt['user_home']))
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


def _codex_removal(config, owners):
    """('remove', source, candidate) for our exact Codex subtree (any of `owners`), else ('absent'|'kept', source, None)."""
    source,exists=_config(config)
    subtree=owned_subtree(source) if exists else None
    if subtree is None:return 'absent',source,None
    owner=next((o for o in owners if digest(subtree)==o.post_subtree_hash),None)
    if owner is None:return 'kept',source,None
    plan=rollback_registration(source,Receipt(owner.installation_id,owner.transaction_id,None,owner.post_subtree_hash,None))
    return 'remove',source,plan.candidate


def _adoption_backups(root, receipt, parked=None):
    """Every recorded `--adopt-legacy` backup: adopted.json plus a v2 receipt's own records, and those a parked
    change (`(name, journal)`) recorded, sorted."""
    _,records=_adopted_file(root)
    out={(r['transaction'],r['client']):r for r in records}
    if receipt['schema_version']==2:
        for client,value in receipt['adopted'].items():
            out.setdefault((value['transaction'],client),{'client':client,**value})
    if parked is not None:
        for client,value in _check_adopted(_record(parked[1],'adopted')).items():
            out.setdefault((parked[0],client),{'client':client,'transaction':parked[0],**value})
    return [out[k] for k in sorted(out)]


def _parked(journal):
    """Whether an unfinished upgrade or rollback is parked: short of its commit with its service as it was before
    it, either never stopped (refused or interrupted before the stop, so no client was written) or put back by a
    late refusal's finished restore. The client entries and skill copies it wrote stay, owned through its journal:
    its repeat resumes it, and an uninstall goes over it."""
    stage=journal.get('stage')
    if journal.get('kind') not in ('upgrade','rollback') or stage not in _CHANGE_STEPS or stage in ('complete','finished'):
        return False
    cycle=_record(journal,'service_cycle')
    if cycle is None:return _CHANGE_STEPS.index(stage)<_CHANGE_STEPS.index('service')
    return cycle.get('state')=='restored'


def _bound(path, binding):
    """Whether `path` is the exact object `binding` records."""
    try:require_object_binding(path,binding)
    except ChangedRuntimePath:raise
    except RuntimePathError:return False
    return True


def _parked_receipt(root, name, journal):
    """The installation a parked change leaves serving: its `receipt.previous`, while current.json is still that
    receipt's pointer and receipt.json that receipt or the one the change published at its receipt step."""
    previous=read(root/'transactions'/name/'receipt.previous',mode=0o600)
    receipt=_receipt_shape(unique_json(previous),root)
    if read(root/'current.json',mode=0o600)!=_pointer(receipt):raise InstallError('metadata_conflict')
    if read(root/'receipt.json',mode=0o600)!=previous:
        attempts=journal['publications'].get('receipt',[])
        if type(attempts) is not list or not any(type(a) is dict and type(a.get('object')) is dict
                                                 and _bound(root/'receipt.json',a['object']) for a in attempts):
            raise InstallError('metadata_conflict')
    return receipt


def _parked_objects(root, name, journal, receipt):
    """(Codex owners, {skill: [bindings]}) a parked change adds to what `receipt` owns: its Codex ownership once one
    of its writes was journaled with its flushed object, and every skill copy it staged (only the object it
    published can still be in place)."""
    publications=journal['publications']
    if type(publications) is not dict:raise InstallError('transaction_conflict')
    codex=[]
    if any(type(a) is dict and 'object' in a for a in publications.get('registration',[])):
        tx=root/'transactions'/name
        codex.append(Receipt.from_bytes(read(tx/'registration.json',mode=0o600)) if journal['kind']=='upgrade'
                     else _codex_ownership(root,_receipt_shape(unique_json(read(tx/'receipt.target',mode=0o600)),root)))
    skills={}
    for skill_name in receipt['publication_objects']:
        attempts=publications.get(skill_name,[]) if skill_name!='release' else []
        if type(attempts) is not list:raise InstallError('transaction_conflict')
        skills[skill_name]=[a for a in attempts if type(a) is dict and 'object' in a and 'child' in a]
    return codex,skills


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
    """Remove only owned objects: the broker plist (exact label booted out first), the Codex entry the receipt owns,
    the Claude Code and Claude Desktop entries the installation owns (`_client_owners`), the owned skill copies and
    the installation root with its releases. The Telegram session, enrollment and Keychain are never touched;
    `--adopt-legacy` backups (adopted.json and a v2 receipt's records) are kept beside the removed root.

    Every refusal, the pending-send gate included, comes before the transaction exists. After a write-ahead
    removal attempt of a Claude Code or Claude Desktop entry the receipt and root stay until the entry is
    confirmed gone (or no longer ours): a still exact entry is removed again, otherwise `uninstall_incomplete`.
    Over a parked upgrade or rollback (`_parked`) it removes the installation its `receipt.previous` names plus
    what the change's journal binds (its Codex entry once written, its skill copies, its Claude Code and Claude
    Desktop outcomes, its adoption backups, which are kept); any other unfinished change is named for its repeat.
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
            # A parked change (`_parked`): an uninstall goes over it, owning what it wrote through its journal; any
            # other unfinished change is named for its repeat. Resumed, the uninstall finds it just below itself.
            parked=None
            if journal is not None and journal.get('kind')=='uninstall':
                below=[n for n in _changes(root) if n<int(name[7:])]
                if below:
                    previous=unique_json(read(root/'transactions'/('change_%d'%below[-1])/'journal.json',mode=0o600))
                    if previous.get('stage')!='finished':
                        if not _parked(previous):raise InstallError('transaction_conflict')
                        parked=('change_%d'%below[-1],previous)
            elif journal is not None and journal.get('stage')!='finished':
                if not _parked(journal):raise InstallError('transaction_pending',pending_action=journal.get('kind'))
                parked=(name,journal)
            if (journal is None or journal.get('kind')!='uninstall') and _install_pending(root):
                raise InstallError('transaction_pending',pending_action='install')
            receipt=_parked_receipt(root,*parked) if parked else _installation(root)[0]
            if str(home)!=receipt['user_home']:raise InstallError('upgrade_conflict')
            installed=Path(receipt['release_root'])
            command,arguments=_commands(installed)
            config=Path(receipt['codex_home'])/'config.toml'
            codex_owners,parked_skills=_parked_objects(root,*parked,receipt) if parked else ([],{})
            codex_owners=[_codex_ownership(root,receipt),*codex_owners]
            owners=_client_owners(root,int(name[7:]) if journal is not None and journal.get('kind')=='uninstall' else None,
                                  parked[0] if parked else None)
            backups=_adoption_backups(root,receipt,parked)
            service=_Service(plist,label,_service_payload(installed,label),runner=launchctl,pending=probe,home=home)
            observed=None
            if journal is None or journal.get('kind')!='uninstall':
                # Every refusal before the transaction exists: a refused uninstall leaves nothing that blocks a
                # later upgrade, repair or rollback.
                for value in backups:
                    if digest(read(_backup_path(root,value),MAX_BACKUP_BYTES,mode=0o600))!=value['sha256']:
                        raise InstallError('transaction_conflict')
                _codex_removal(config,codex_owners)
                observed=_uninstall_service(service)
                identity=dict(schema_version=1,kind='uninstall',index_sha256=receipt['index_sha256'],release_root=receipt['release_root'],
                              installation_root=str(root),user_home=str(home))
                name=_open_change(root,lambda _:{'journal.json':encoded({**identity,'stage':'prepared','publications':{}})})
            else:_sweep_staging(root)
            tx=root/'transactions'/name
            j=_Journal(tx,lease,_UNINSTALL_STEPS,checkpoint)
            if j['index_sha256']!=receipt['index_sha256'] or j['installation_root']!=str(root):raise InstallError('transaction_conflict')
            # A parked change's temporaries beside the client files (a kill in its Claude Desktop write, then a
            # repeat refused before that step): its journal goes with the root, so they are removed first.
            if parked:_client_temporaries(parked[1]['publications'],home,config,plist,session_root)
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
                status,source,candidate=_codex_removal(config,codex_owners)
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
                    owned=owners.get('claude_code')
                    cc=owned[:2] if owned else (command,arguments)
                    state=claude_registration.state(home,*cc)
                    if not attempts and state=='absent':status='absent'
                    elif not attempts and state=='unavailable':status='unavailable'
                    elif not attempts and (state!='exact' or owned is None):status='kept'
                    else:
                        cli=None
                        if state=='exact':
                            cli=claude_registration.locate(claude_registration.candidate_paths(home) if claude_candidates is None else claude_candidates)
                        if state=='exact' and cli is not None and len(attempts)<8:
                            j.update(publication=('claude_remove',{'attempt':len(attempts)+1}));checkpoint('recorded:claude_remove')
                            try:claude_registration.remove(claude_runner,cli,home)
                            except claude_registration.ClaudeCliError:pass
                            checkpoint('published:claude_remove')
                            state=claude_registration.state(home,*cc)
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
                owned=owners.get('claude_desktop')
                desktop_entry=owned[:2] if owned else (command,arguments)
                state,plan=claude_desktop_registration.observe_removal(home,*desktop_entry)
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
                        state,_=claude_desktop_registration.observe_removal(home,*desktop_entry)
                    if state in ('absent','skipped'):status='removed'
                    elif state=='different':status='kept'  # No longer ours.
                    else:raise InstallError('uninstall_incomplete')
            else:status=None
            out['claude_desktop']=done('claude_desktop',status)
            # Skill copies: only the exact directories the receipt (or a parked change's journal) binds; moved into the
            # retiring root.
            if j.recorded('skills') is None:
                statuses={}
                for skill_name,bound in sorted(receipt['publication_objects'].items()):
                    if skill_name=='release':continue
                    base,sid=((Path(receipt['claude_skills_root']),skill_name[7:]) if skill_name.startswith('claude-')
                              else (Path(receipt['skills_root']),skill_name))
                    path,retired=base/sid,tx/('removed-'+skill_name)
                    if retired.exists():statuses[skill_name]='removed';continue
                    if not path.exists() and not path.is_symlink():statuses[skill_name]='absent';continue
                    def ours(binding):
                        try:require_object_binding(path,binding['object']);require_object_binding(path/'SKILL.md',binding['child'])
                        except (RuntimePathError,TypeError,KeyError):return False
                        return True
                    if not any(ours(b) for b in [bound,*parked_skills.get(skill_name,())]):statuses[skill_name]='kept';continue
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
            _client_temporaries(j.value['publications'],home,config,plist,session_root)
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
