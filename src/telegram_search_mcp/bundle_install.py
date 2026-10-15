"""First install only: bounded owned transaction, no auth or service effects."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import ctypes
import shlex
import plistlib
import subprocess
import tempfile
import uuid

from .bundle_paths import (ensure_owned_private_directory, open_owned_directory,
                           open_owned_file, validate_owned_path, object_binding, require_object_binding, RuntimePathError)
from .bundle_runtime import (validate_tree, require_platform, read, unique_json,
                             digest, BundleError)
from .session_owner import SessionOwnerLease, OwnerLeaseError
from . import claude_registration
from .codex_registration import (Registration, Receipt, ConfigConflict, read_config,
    merge_registration, commit_transaction, _atomic_write, MAX_CONFIG_BYTES)


class InstallError(ValueError):
    """An integrity/conflict refusal; inputs and neighboring config stay redacted."""


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


def _rename(source, destination):
    with open_owned_directory(source.parent) as a, open_owned_directory(destination.parent) as b:
        if destination.exists() or destination.is_symlink(): raise InstallError('target_conflict')
        # Held parents plus a no-replace directory rename; existing nonempty
        # targets always refuse. The installer lease serializes our own writers.
        if sys.platform == 'darwin':
            # macOS SDK sys/stdio.h: RENAME_EXCL is 0x00000004. Atomically
            # refuse even a concurrently created empty foreign directory.
            api=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
            rename=api.renameatx_np
            rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
            rename.restype=ctypes.c_int
            if rename(a,os.fsencode(source.name),b,os.fsencode(destination.name),4)!=0:
                raise InstallError('target_conflict')
        else:
            # Production admission only supports macOS; this fallback is for
            # inert portable tests under the same cooperative lease.
            os.rename(source.name, destination.name, src_dir_fd=a, dst_dir_fd=b)
        os.fsync(a); os.fsync(b)


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


def _check_existing_service(plist, installed, label):
    """Repeat observes only an exact owned disk definition; never launchctl.

    Loaded-service/Ready authority remains the native setup handoff gate. This
    read-only repeat check never adopts, stops or claims readiness for a process.
    """
    if not plist.exists():return
    from .launch_agent import launch_agent_payload
    expected=launch_agent_payload(python_executable=installed/'python/bin/python3')
    expected.update(Label=label,ProgramArguments=[str(installed/'python/bin/python3'),'-I','-S','-B',str(installed/'launcher.py'),'broker'],EnvironmentVariables={})
    try:
        actual=plistlib.loads(read(plist,1024*1024,mode=0o600))
        if actual!=expected:raise InstallError('service_conflict')
    except (ValueError,TypeError,plistlib.InvalidFileException,BundleError):
        raise InstallError('service_conflict') from None


def _fresh_packet(tx, label):
    """Never adopt/delete an earlier packet without publication authority."""
    for attempt in range(8):
        path=tx/(label+'-'+str(attempt))
        if path.exists() or path.is_symlink():continue
        ensure_owned_private_directory(path)
        return path
    raise InstallError('recovery_limit')


def _sync_directories(root):
    for parent,dirs,_ in os.walk(root,followlinks=False):
        with open_owned_directory(Path(parent),private=True) as fd:os.fsync(fd)


def _initialize(root, identity, change, existed, checkpoint):
    """Publish a complete owned root once; incomplete preparations stay outside.

    The adjacent immutable intent precedes final root mutation and records its
    exact staged inode. Unbound early staging files are retained, never adopted.
    A normal retry makes a fresh bounded packet instead of repairing foreign or
    ambiguous metadata. The supported root's parent must already exist safely.
    """
    with open_owned_directory(root.parent):pass
    intent_path=root.parent/('.'+root.name+'.install-intent.json')
    if intent_path.exists() or intent_path.is_symlink():
        intent=unique_json(read(intent_path,mode=0o600))
        if (set(intent)!={'identity','staged_root','object'} or intent['identity']!=identity):
            raise InstallError('initialization_conflict')
        staged=Path(intent['staged_root'])
        if staged.parent!=root.parent or not staged.name.startswith('.'+root.name+'-initial-'):
            raise InstallError('initialization_conflict')
        if root.exists():
            require_object_binding(root,intent['object'])
            return
        require_object_binding(staged,intent['object'])
        _rename(staged,root);checkpoint('published:root');return
    if root.exists() or root.is_symlink():raise InstallError('runtime_conflict')
    prefix='.'+root.name+'-initial-'
    if sum(p.name.startswith(prefix) for p in root.parent.iterdir())>=8:raise InstallError('recovery_limit')
    staged=Path(tempfile.mkdtemp(prefix=prefix,dir=root.parent))
    checkpoint('created:initial_root')
    transactions=staged/'transactions';ensure_owned_private_directory(transactions)
    ensure_owned_private_directory(staged/'releases')
    tx=transactions/('install_'+identity['index_sha256'][:24]);ensure_owned_private_directory(tx)
    create(staged/'.install.lock',b'')
    create(tx/'config.preimage',change.source,maximum=MAX_CONFIG_BYTES)
    create(tx/'config.candidate',change.candidate,maximum=MAX_CONFIG_BYTES)
    ownership=Receipt('install_'+identity['index_sha256'][:24],'first_install',change.pre_subtree_hash,
                      change.post_subtree_hash,change.pre_subtree)
    create(tx/'registration.json',ownership.to_bytes())
    journal={**identity,'stage':'prepared','config_existed':existed,'publications':{}}
    create(tx/'journal.json',encoded(journal))
    _sync_directories(staged)
    intent={'identity':identity,'staged_root':str(staged),'object':object_binding(staged,directory=True)}
    # Fully flush an unbound fresh file before no-replace intent publication.
    # Interrupted temporary writes have no authority over the final target.
    temporary=root.parent/('.'+root.name+'-intent-'+uuid.uuid4().hex+'.tmp')
    create(temporary,encoded(intent))
    _rename(temporary,intent_path)
    checkpoint('recorded:root')
    _rename(staged,root)
    checkpoint('published:root')


CLAUDE_STATUSES = ('registered', 'reused', 'not_found')
_REFUSALS = (BundleError, RuntimePathError, ConfigConflict, OwnerLeaseError, OSError, ValueError, KeyError, TypeError)


def install(release, index_path, index_sha256, *, installation_root, codex_home,
            skills_root, user_home, checkpoint=lambda _: None, claude_candidates=None,
            claude_runner=subprocess.run):
    """First-install/recovery only; checkpoints observe durable mutation windows.

    Claude Code skills live in `<user_home>/.claude/skills`. The `claude` CLI is looked up only
    at `claude_candidates` (default: the fixed user/system list) and driven through `claude_runner`.
    """
    try:
        require_platform()
        release,index_path,root,codex,skills,home=map(Path,(release,index_path,installation_root,codex_home,skills_root,user_home))
        claude_skills=home/'.claude'/'skills'
        cli=claude_registration.locate(claude_registration.candidate_paths(home) if claude_candidates is None else claude_candidates)
        index,manifest=validate_tree(release,index_path,index_sha256)
        index_bytes=read(index_path,2*1024*1024);config=codex/'config.toml'
        from .config import _owner_paths
        session_root,_,agent_label,plist=_owner_paths(home)
        if any(root==p or root.is_relative_to(p) or p.is_relative_to(root) for p in (release,session_root,codex,skills,claude_skills)):
            raise InstallError('overlapping_paths')
        # Only the runtime root must be owner-only; Codex and Claude create their homes 0755.
        for path in (root,codex,skills,claude_skills,home):validate_owned_path(path,private=path==root)
        validate_owned_path(plist,regular=True)
        if plist.exists() and not (root/'current.json').exists():raise InstallError('service_conflict')
        installed=root/'releases'/index_sha256
        tx=root/'transactions'/('install_'+index_sha256[:24])
        registration=Registration(str(installed/'python/bin/python3'),('-I','-S','-B',str(installed/'launcher.py'),'mcp'),{},tuple(manifest['tools']))
        identity=dict(schema_version=1,index_sha256=index_sha256,release_root=str(installed),installation_root=str(root),
                      codex_home=str(codex),skills_root=str(skills),claude_skills_root=str(claude_skills),user_home=str(home),
                      package_version=manifest['package_version'])
        def claude(action,*extra):
            # The one place the CLI is driven; a CLI failure is a fixed redacted refusal.
            try:return getattr(claude_registration,action)(claude_runner,cli,home,*extra)
            except claude_registration.ClaudeCliError:raise InstallError('claude_registration_failed') from None
        def claude_state():return claude('state',registration.command,registration.args)
        intent_path=root.parent/('.'+root.name+'.install-intent.json')
        if not intent_path.exists():
            if root.exists():raise InstallError('runtime_conflict')
            for skill in manifest['skills']:
                for base in (skills,claude_skills):
                    if (base/skill['id']).exists() or (base/skill['id']).is_symlink():raise InstallError('skill_conflict')
            # A foreign Claude entry refuses the whole install before anything is created.
            if cli is not None and claude_state()=='different':raise InstallError('claude_registration_conflict')
            source,existed=_config(config);change=merge_registration(source,registration)
        else:source=b'';existed=False;change=None
        _initialize(root,identity,change,existed,checkpoint)
        with SessionOwnerLease(root/'.install.lock') as lease:
            if set(p.name for p in root.iterdir())-{'transactions','releases','.install.lock','receipt.json','current.json'}:
                raise InstallError('runtime_conflict')
            journal=unique_json(read(tx/'journal.json',mode=0o600))
            if (set(journal)!=set(identity)|{'stage','config_existed','publications'}
                    or any(journal.get(k)!=v for k,v in identity.items()) or type(journal['publications']) is not dict):
                raise InstallError('transaction_conflict')
            before=read(tx/'config.preimage',MAX_CONFIG_BYTES,mode=0o600)
            candidate=read(tx/'config.candidate',MAX_CONFIG_BYTES,mode=0o600)
            ownership=Receipt.from_bytes(read(tx/'registration.json',mode=0o600))
            fresh=merge_registration(before,registration)
            if candidate!=fresh.candidate or ownership.post_subtree_hash!=fresh.post_subtree_hash:raise InstallError('transaction_conflict')
            steps=['prepared','release_pending','release','registration_pending','registration','telegram-search_pending','telegram-search',
                   'telegram-setup_pending','telegram-setup','claude-telegram-search_pending','claude-telegram-search',
                   'claude-telegram-setup_pending','claude-telegram-setup','claude_code_pending','claude_code',
                   'receipt_pending','receipt','complete']
            if journal['stage'] not in steps:raise InstallError('transaction_conflict')
            if plist.exists():
                if journal['stage']!='complete' or read(root/'current.json',mode=0o600)!=encoded({'schema_version':1,'release_root':str(installed),'index_sha256':index_sha256}):
                    raise InstallError('service_conflict')
                _check_existing_service(plist,installed,agent_label)
            for client_root in (codex,skills,claude_skills):ensure_owned_private_directory(client_root,private_leaf=False)
            def update(stage=None, publication=None):
                nonlocal journal
                lease.check();next_value={**journal}
                if stage is not None:next_value['stage']=stage
                if publication is not None:
                    name,proof=publication
                    attempts=journal['publications'].get(name,[])
                    if type(attempts) is not list or len(attempts)>=8:raise InstallError('recovery_limit')
                    next_value['publications']={**journal['publications'],name:[*attempts,proof]}
                write(tx/'journal.json',encoded(next_value),expected=encoded(journal),existed=True)
                journal=next_value
            def reached(value):return steps.index(journal['stage'])>=steps.index(value)
            def proof(name):
                attempts=journal['publications'].get(name,[])
                if type(attempts) is not list or len(attempts)>8:raise InstallError('transaction_conflict')
                return attempts[-1] if attempts else None
            def publish(name,target,build,validate,*,directory=False,child=False):
                bound=proof(name)
                if target.exists() or target.is_symlink():
                    if bound is None:raise InstallError('foreign_'+name)
                    require_object_binding(target,bound['object'])
                    if child:require_object_binding(target/'SKILL.md',bound['child'])
                    validate(target);return
                if bound is None:
                    packet=_fresh_packet(tx,'failed-release' if name=='release' else 'skill-packet-'+name if child else 'metadata-packet-'+name)
                    checkpoint('created:packet:'+name)
                    staged=packet if directory else packet/target.name
                    build(staged);validate(staged);_sync_directories(packet)
                    bound={'staged':str(staged),'object':object_binding(staged,directory=directory)}
                    if child:bound['child']=object_binding(staged/'SKILL.md')
                    update(publication=(name,bound));checkpoint('recorded:'+name)
                staged=Path(bound['staged'])
                if not staged.is_relative_to(tx):raise InstallError('transaction_conflict')
                require_object_binding(staged,bound['object'])
                if child:require_object_binding(staged/'SKILL.md',bound['child'])
                validate(staged);_rename(staged,target);checkpoint('published:'+name)
                require_object_binding(target,bound['object']);validate(target)
            checkpoint('prepared')
            if not reached('release_pending'):update('release_pending')
            publish('release',installed,lambda p:_copy_release(release,p,index,index_bytes,index_sha256),
                    lambda p:validate_tree(p,p/'bundle-index.json',index_sha256,installed=True),directory=True)
            if not reached('release'):update('release')
            checkpoint('release')
            if not reached('registration'):
                if not reached('registration_pending'):update('registration_pending')
                current,exists=_config(config);bound=proof('registration')
                owned=False
                if bound is not None and exists:
                    try:require_object_binding(config,bound['object']);owned=current==candidate
                    except RuntimePathError:pass
                if not owned:
                    if current!=before or exists!=journal['config_existed']:raise InstallError('foreign_registration')
                    # Bound retries even when a process dies during temporary
                    # creation before its flushed inode can be recorded.
                    update(publication=('registration_attempts',{'attempt':len(journal['publications'].get('registration_attempts',[]))+1}))
                    def admit_config(value):
                        obj={'kind':'file',**{k:value[k] for k in ('dev','ino','uid','mode','bytes')},'nlink':1}
                        update(publication=('registration',{'object':obj,'temporary':value['temporary']}))
                        checkpoint('recorded:registration')
                    _atomic_write(config,candidate,MAX_CONFIG_BYTES,expected=before,require_existing=journal['config_existed'],before_replace=admit_config)
                    checkpoint('published:registration')
                    require_object_binding(config,proof('registration')['object'])
                update('registration')
            if not merge_registration(read_config(config),registration,ownership).is_noop:raise InstallError('registration_conflict')
            checkpoint('registration')
            for prefix,base in (('',skills),('claude-',claude_skills)):
                for skill in manifest['skills']:
                    sid,expected=skill['id'],skill['sha256'];name=prefix+sid;target=base/sid
                    if not reached(name+'_pending'):update(name+'_pending')
                    def validate_skill(p):
                        if _skill_hash(p)!=expected:raise InstallError('skill_conflict')
                    publish(name,target,lambda p:create(p/'SKILL.md',read(installed/'skills'/sid/'SKILL.md')),validate_skill,directory=True,child=True)
                    if not reached(name):update(name)
                    checkpoint(name)
            def attempted():return bool(journal['publications'].get('claude_add'))
            def recorded_claude():
                records=journal['publications'].get('claude_code',[])
                return records[-1]['status'] if records and type(records[-1]) is dict else None
            def register_claude():
                """Claude Code outcome for this transaction; only an absent entry is ever added."""
                if cli is None:return 'not_found'
                state=claude_state()
                if state=='different':raise InstallError('claude_registration_conflict')
                if state=='absent':
                    # Write-ahead: a journaled attempt is what makes the entry ours to roll back.
                    update(publication=('claude_add',{'attempt':len(journal['publications'].get('claude_add',[]))+1}))
                    checkpoint('recorded:claude_add')
                    claude('add',registration.command,registration.args)
                    checkpoint('published:claude_add')
                    confirmed=claude_state()
                    if confirmed!='exact':raise InstallError('claude_registration_conflict' if confirmed=='different' else 'claude_registration_failed')
                return 'registered' if attempted() else 'reused'
            def rollback_claude():
                """Best effort, never overwriting: remove the entry only if this transaction added it and it is still exactly ours."""
                if cli is None or not attempted() or journal['stage']=='complete':return
                try:
                    if claude_state()=='exact':claude('remove')
                except Exception:pass
            try:
                if reached('complete'):
                    claude_status=recorded_claude()  # Repeat install: the CLI is not consulted again.
                    if claude_status not in CLAUDE_STATUSES:raise InstallError('transaction_conflict')
                else:
                    if not reached('claude_code_pending'):update('claude_code_pending')
                    claude_status=register_claude()
                    if recorded_claude()!=claude_status:update(publication=('claude_code',{'status':claude_status}))
                    if not reached('claude_code'):update('claude_code')
                    checkpoint('claude_code')
                objects={name:{k:v for k,v in proof(name).items() if k!='staged'}
                         for name in ('release','telegram-search','telegram-setup','claude-telegram-search','claude-telegram-setup')}
                receipt={**identity,'registration_sha256':digest(ownership.to_bytes()),'skills':manifest['skills'],
                         'publication_objects':objects,'claude_code':claude_status,'status':'activation_required'}
                if not reached('receipt_pending'):update('receipt_pending')
                def exact(expected):
                    def validate(p):
                        if read(p,mode=0o600)!=expected:raise InstallError('metadata_conflict')
                    return validate
                publish('receipt',root/'receipt.json',lambda p:create(p,encoded(receipt)),exact(encoded(receipt)))
                if not reached('receipt'):update('receipt')
                checkpoint('receipt')
                pointer=encoded({'schema_version':1,'release_root':str(installed),'index_sha256':index_sha256})
                reused=reached('complete')
                publish('pointer',root/'current.json',lambda p:create(p,pointer),exact(pointer))
                if not reached('complete'):update('complete')
                activation=[str(installed/'python/bin/python3'),'-I','-S','-B',str(installed/'launcher.py'),'connect']
                return {'status':'activation_required','installation':'reused' if reused else 'installed','release_root':str(installed),
                        'package_version':manifest['package_version'],'next_action':'run_activation_then_wait','claude_code':claude_status,
                        'activation_arguments':activation,'activation_command':shlex.join(activation)}
            except _REFUSALS:
                rollback_claude()
                raise
    except InstallError:raise
    except _REFUSALS:
        raise InstallError('installation_refused') from None
