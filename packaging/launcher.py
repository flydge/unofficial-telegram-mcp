"""Isolated pre-import bundle admission and direct B1A activation dispatch.

The independently authenticated seed admits the first invocation. Installed
invocations recheck the owned receipt/index/tree before adding the closed app
path. Local checks do not protect against a hostile process controlling this UID.
No profile, credential transport, site startup or environment runtime selector.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys


class AdmissionError(ValueError): pass

# Closed grammar. connect-helper is internal: only the transient launchd job runs it.
ACTIONS=('mcp','broker','setup','resume','status','connect','wait','cancel','api-credentials','connect-helper')
# The actions that take a trailing `--account <alias>` for a registered alias (0.55 spec 3B; connect-helper is the
# argv of the alias's own connect job, 3C). The alias is a closed token, never `default` (the default account is
# spelled by omitting the flag), and is checked against the registry before any directory is created. Without the
# flag an action's argv and behaviour are unchanged.
ACCOUNT_ACTIONS=('mcp','broker','setup','resume','connect','wait','cancel','api-credentials','status','connect-helper')
ALIAS_CHARACTERS=frozenset('abcdefghijklmnopqrstuvwxyz0123456789')
# Installation changes run by an installed release: admitted by its own pinned tree, not by the
# receipt, so an interrupted change stays resumable while receipt and pointer disagree.
CHANGES=('rollback','uninstall')
# The account transactions (0.55 spec 3D), `account-add <alias>` and `account-remove <alias>`: the same admission.
ACCOUNT_CHANGES=('account-add','account-remove')
# The installer's closed refusal codes, echoed by a refused bootstrap-install, rollback, uninstall or account
# transaction; anything else is generic.
INSTALL_CODES=frozenset({'claude_registration_conflict','foreign_registration','initialization_conflict',
    'installation_refused','metadata_conflict','overlapping_paths','recovery_limit','registration_conflict',
    'runtime_conflict','service_conflict','skill_conflict','target_conflict','transaction_conflict',
    'claude_desktop_registration_conflict','claude_desktop_config_unsupported','pending_send_unresolved',
    'policy_conflict','upgrade_conflict','transaction_pending','rollback_unavailable','rollback_refused',
    'service_restart_failed','downgrade_refused','uninstall_incomplete',
    'accounts_inconsistent','account_remove_incomplete','accounts_present',
    'release_not_current','login_in_progress','connect_lock_unsafe','alias_broker_unreadable',
    'alias_invalid','alias_exists','alias_unknown','alias_path_too_long','alias_name_too_long','registry_full',
    'registry_invalid','registry_unsafe','registry_busy'}
    |{'foreign_'+n for n in ('release','receipt','pointer','telegram-search','telegram-setup',
                             'claude-telegram-search','claude-telegram-setup')})
# A `transaction_pending` refusal also names the unfinished action to retry, from this closed set only, and for an
# account transaction its alias (`pending_account`, a closed token).
PENDING_ACTIONS=('install','upgrade','rollback','uninstall','account-add','account-remove')
# An `alias_broker_unreadable` refusal also names the alias (`account`, a closed token) and the owner's manual next
# step: the bootout of that alias's exact broker label, a loaded leftover of a release since removed whose live socket
# the current release cannot read (0.55 review fix 055d3).
UNREADABLE_ACTION='boot_out_alias_broker_then_retry'
# `login_in_progress` (a login of that alias still runs) and `connect_lock_unsafe` (its connect lock or run folder is
# not owner-only, which no `wait` or `cancel` clears) also name the alias (`account`, a closed token; 0.55 critic fix
# 055f).
ACCOUNT_NAMED=('login_in_progress','connect_lock_unsafe')


def bootout_command(alias):
    """`launchctl bootout gui/<uid>/<label>` of a named alias's broker label, as an argv list."""
    from telegram_search_mcp.config import alias_jobs
    return ['launchctl','bootout','gui/%d/%s'%(os.getuid(),alias_jobs(alias,Path.home()).broker_label)]
# The owner's capability profiles (telegram_search_mcp.config.CAPABILITY_PROFILES), spelled here for the grammar.
PROFILES=('readers','send','replies','forward','video','archive_edit','albums','pin','delete')


def _acl(fd):
    if sys.platform != 'darwin': return
    api = ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    api.acl_get_fd_np.argtypes=[ctypes.c_int,ctypes.c_int]; api.acl_get_fd_np.restype=ctypes.c_void_p
    api.acl_get_entry.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p)]
    api.acl_get_tag_type.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)]
    api.acl_valid.argtypes=[ctypes.c_void_p]; api.acl_free.argtypes=[ctypes.c_void_p]
    ctypes.set_errno(0); acl=api.acl_get_fd_np(fd,0x100)
    if not acl:
        import errno
        if ctypes.get_errno()!=errno.ENOENT: raise AdmissionError()
        return
    try:
        if api.acl_valid(acl)!=0: raise AdmissionError()
        import errno
        for n in range(129):
            entry=ctypes.c_void_p(); ctypes.set_errno(0)
            result=api.acl_get_entry(acl,0 if n==0 else -1,ctypes.byref(entry))
            if result==-1 and ctypes.get_errno()==errno.EINVAL: return
            tag=ctypes.c_int()
            if result!=0 or n==128 or not entry.value or api.acl_get_tag_type(entry,ctypes.byref(tag))!=0 or tag.value!=2:
                raise AdmissionError()
        raise AdmissionError()
    finally: api.acl_free(acl)


def _metadata(fd, *, regular=False, private=False):
    m=os.fstat(fd); mode=stat.S_IMODE(m.st_mode)
    if m.st_uid not in (0,os.getuid()) or mode&0o022 and not (m.st_uid==0 and stat.S_ISDIR(m.st_mode) and mode==0o1777):
        raise AdmissionError()
    if regular and (not stat.S_ISREG(m.st_mode) or m.st_uid!=os.getuid() or m.st_nlink!=1): raise AdmissionError()
    if not regular and not stat.S_ISDIR(m.st_mode): raise AdmissionError()
    if private and (m.st_uid!=os.getuid() or mode!=0o700): raise AdmissionError()
    _acl(fd)
    return m


def _directory(path, *, private=False):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts: raise AdmissionError()
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        _metadata(fd)
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child; _metadata(fd)
        _metadata(fd,private=private)
        result,fd=fd,-1; return result
    finally:
        if fd>=0: os.close(fd)


def _file(path, maximum, expected_mode=None):
    parent=_directory(Path(path).parent)
    try: fd=os.open(Path(path).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally: os.close(parent)
    try:
        m=_metadata(fd,regular=True)
        if m.st_size>maximum or expected_mode is not None and stat.S_IMODE(m.st_mode)!=expected_mode: raise AdmissionError()
        return os.fdopen(fd,'rb'),m
    except BaseException: os.close(fd); raise


def _read(path, maximum=65536, mode=None):
    stream,m=_file(path,maximum,mode)
    with stream:
        data=stream.read(maximum+1)
        if len(data)!=m.st_size or _identity(m)!=_identity(os.fstat(stream.fileno())): raise AdmissionError()
        return data


def _identity(m):
    return (m.st_dev,m.st_ino,m.st_uid,m.st_mode,m.st_nlink,m.st_size,m.st_mtime_ns,m.st_ctime_ns)


def _json(data):
    def pairs(items):
        out={}
        for k,v in items:
            if k in out: raise AdmissionError()
            out[k]=v
        return out
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(AdmissionError()))


def _hash(value): return type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def admit(root,index_path,pin,installed):
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode
            or Path(sys.executable).absolute()!=root/'python/bin/python3'):
        raise AdmissionError()
    if platform.system()!='Darwin' or platform.machine()!='arm64' or int(platform.mac_ver()[0].split('.')[0])<26:
        raise AdmissionError()
    fd=_directory(root,private=True); os.close(fd)
    data=_read(index_path,2*1024*1024)
    if not _hash(pin) or hashlib.sha256(data).hexdigest()!=pin: raise AdmissionError()
    value=_json(data)
    keys={'schema_version','target','release_version','archive_bytes','archive_sha256','unpacked_bytes','files'}
    if (type(value) is not dict or set(value)!=keys or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['target']!='macos-arm64' or value['release_version']!='0.59.0.dev0'
            or not _hash(value['archive_sha256']) or type(value['archive_bytes']) is not int
            or not 1<=value['archive_bytes']<=1024**3 or type(value['unpacked_bytes']) is not int
            or not 0<=value['unpacked_bytes']<=2*1024**3
            or type(value['files']) is not list or not 1<=len(value['files'])<=10000): raise AdmissionError()
    names=[]; total=0
    for record in value['files']:
        if type(record) is not dict or set(record)!={'path','bytes','sha256','executable'}: raise AdmissionError()
        name=record['path']
        if (type(name) is not str or not name or len(name.encode())>1024 or '\\' in name
                or any(c in ('','.','..') or len(c.encode())>255 for c in name.split('/'))
                or any(ord(c)<32 or ord(c)==127 for c in name)
                or not _hash(record['sha256']) or type(record['bytes']) is not int
                or not 0<=record['bytes']<=256*1024**2 or type(record['executable']) is not bool): raise AdmissionError()
        names.append(name); total+=record['bytes']
        stream,m=_file(root/name,record['bytes'],0o500 if record['executable'] else 0o400)
        with stream:
            if m.st_size!=record['bytes']: raise AdmissionError()
            h=hashlib.sha256(); remaining=m.st_size
            while remaining:
                part=stream.read(min(remaining,1024**2))
                if not part: raise AdmissionError()
                h.update(part); remaining-=len(part)
            if stream.read(1) or h.hexdigest()!=record['sha256'] or _identity(m)!=_identity(os.fstat(stream.fileno())): raise AdmissionError()
    if names!=sorted(names) or len(set(names))!=len(names) or total!=value['unpacked_bytes']: raise AdmissionError()
    directories={str(p) for name in names for p in Path(name).parents if str(p)!='.'}
    found=set()
    for parent,dirs,files in os.walk(root,followlinks=False):
        parent=Path(parent); fd=_directory(parent,private=True); os.close(fd)
        for name in dirs:
            if str((parent/name).relative_to(root)) not in directories: raise AdmissionError()
            fd=_directory(parent/name,private=True); os.close(fd)
        found.update(str((parent/name).relative_to(root)) for name in files)
    if found!=set(names)|({'bundle-index.json'} if installed else set()): raise AdmissionError()


def alias_token(value):
    """A named alias: `[a-z][a-z0-9]{0,11}`, never `default` (spelled by omitting the flag)."""
    return (type(value) is str and 1<=len(value)<=12 and value[0] in ALIAS_CHARACTERS and not value[0].isdigit()
            and set(value)<=ALIAS_CHARACTERS and value!='default')


def account_action(args):
    """(action argv, alias or None) for `<account action> --account <alias>`; anything else is returned unchanged."""
    if len(args)==3 and args[0] in ACCOUNT_ACTIONS and args[1]=='--account' and alias_token(args[2]):
        return args[:1],args[2]
    return args,None


def profile_action(args):
    """(capabilities argv, alias or None) for `capabilities show|enable <profile>|disable <profile>` with an optional
    trailing `--account <alias>` (0.55 spec 3D); None outside that closed grammar."""
    alias=None
    if args[:1]==['capabilities'] and len(args)>=4 and args[-2]=='--account' and alias_token(args[-1]):
        args,alias=args[:-2],args[-1]
    if args[:1]==['capabilities'] and (args[1:]==['show'] or (len(args)==3 and args[1] in ('enable','disable')
                                                             and args[2] in PROFILES)):
        return args,alias
    return None


def dispatch_mcp(alias=None):
    from telegram_search_mcp.server import main as dispatch
    sys.argv = [sys.argv[0]] if alias is None else [sys.argv[0],'--account',alias]
    return dispatch()


def status(root, alias=None):
    """setup_cli's status plus a read-only Claude Desktop drift report under `clients`, and once an alias registry
    exists `accounts`: the default and every alias, each loaded, authorized and registered, never the account
    (0.55 spec 3D). A named alias's status is its own (loaded, authorized, registered); the default's client
    entries are not its, so it has no `clients`."""
    import contextlib, io
    from telegram_search_mcp.setup_cli import main as dispatch
    from telegram_search_mcp.bundle_install import account_registrations, accounts_status, client_status
    captured=io.StringIO()
    with contextlib.redirect_stdout(captured): result=dispatch(['status'] if alias is None else ['status','--account',alias])
    text=captured.getvalue()
    try: value=_json(text)
    except (ValueError,AdmissionError): value=None
    if type(value) is dict:
        if alias is None:
            value['clients']=client_status(root.parent.parent,Path.home())
            accounts=accounts_status(root.parent.parent,Path.home())
            if accounts is not None: value['accounts']=accounts
        else: value['registered']=account_registrations(root.parent.parent,Path.home()).get(alias)
        text=json.dumps(value,sort_keys=True)+'\n'
    sys.stdout.write(text)
    return result


def capabilities(root, args, account=None):
    """Show, enable or disable one capability profile of the installed release's runtime.toml; `account` is a named
    alias's AccountPaths: its own runtime.toml, its broker's pending-send gate and its own label (0.55 spec 3D)."""
    from telegram_search_mcp import bundle_install, capability_profiles as profiles
    home=Path.home()
    try:
        if args==['show']: result=profiles.show(home) if account is None else profiles.show(home,account)
        elif account is not None:
            result=profiles.apply(home,args[0],args[1],
                pending=lambda: bundle_install.account_pending_sends(home,Path(sys.executable),root/'app',account.socket,
                                                                     account.policy),
                restartable=lambda: bundle_install.restart_receipt_broker(root,home,kickstart=False,account=account),
                restart=lambda: bundle_install.restart_receipt_broker(root,home,account=account),account=account)
        else:
            # Every refusal comes before runtime.toml is written, the broker check (restartable) first. A restart
            # that fails after the write is part of the `changed` result (broker restart_failed, restart_command).
            result=profiles.apply(home,args[0],args[1],
                pending=lambda: bundle_install.default_pending_sends(home,Path(sys.executable),root/'app'),
                restartable=lambda: bundle_install.restart_receipt_broker(root,home,kickstart=False),
                restart=lambda: bundle_install.restart_receipt_broker(root,home))
    except profiles.ProfileError as error:
        print(json.dumps({'status':'refused','code':error.code}),file=sys.stderr); return 2
    except bundle_install.InstallError:
        print(json.dumps({'status':'refused','code':'service_conflict'}),file=sys.stderr); return 2
    print(json.dumps(result,sort_keys=True)); return 0


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if args==['--help']:
        print('0.59.0.dev0: mcp | broker | setup | resume | status | connect | wait | cancel | '
              'api-credentials | capabilities show|enable|disable <profile> | rollback | uninstall | '
              'account-add <alias> | account-remove <alias>. setup, resume '
              'and api-credentials require native Apple Terminal; connect, wait and cancel take no credentials. '
              'mcp, broker, setup, resume, status, connect, wait, cancel, api-credentials and capabilities take a '
              'trailing --account <alias> for a registered account alias. account-add registers an alias and its '
              'client entries without logging in; account-remove moves its session aside, never deleting it.')
        return 0
    # Fixed refusal codes only: an installer refusal from its closed set, otherwise generic.
    code=('bundle_admission_or_install_failed' if args and args[0] in ('bootstrap-install',*CHANGES,*ACCOUNT_CHANGES)
          else 'bundle_admission_failed')
    pending=pending_account=account=None
    try:
        root=Path(__file__).absolute().parent
        account_change=len(args)==2 and args[0] in ACCOUNT_CHANGES and alias_token(args[1])
        if args in [[v] for v in CHANGES] or account_change:
            pin=root.name
            if root.parent!=root.parent.parent/'releases' or not _hash(pin): raise AdmissionError()
            admit(root,root/'bundle-index.json',pin,True)
            sys.path[:]=[str(root/'app'),str(root/'installer')]+[p for p in sys.path if Path(p).is_relative_to(root/'python')]
            from telegram_search_mcp import bundle_install
            try:
                if account_change:
                    # The running release names itself: an account action runs only from the current one.
                    result=getattr(bundle_install,args[0].replace('-','_'))(root.parent.parent,alias=args[1],
                                                                             user_home=Path.home(),release_root=root)
                else:result=getattr(bundle_install,args[0])(root.parent.parent,user_home=Path.home())
            except bundle_install.InstallError as error:
                named=error.args[0] if len(error.args)==1 else None
                if type(named) is str and named in INSTALL_CODES: code=named
                pending=getattr(error,'pending_action',None)
                pending_account=getattr(error,'pending_account',None)
                account=getattr(error,'account',None)
                raise
            print(json.dumps(result,sort_keys=True)); return 0
        if args and args[0]=='bootstrap-install':
            options=args[1:]
            # --adopt-legacy is an explicit flag, only ever the last token; never a default.
            adopt=options[-1:]==['--adopt-legacy']
            if adopt: options=options[:-1]
            # Five required options, each once, plus an optional --claude-code user|plugin|skip. Not given, the
            # installer keeps the installed mode on an upgrade and uses user for a first install.
            required={'--installation-root','--codex-home','--skills-root','--index','--trusted-index-sha256'}
            keys=options[::2]
            if (len(options) not in (10,12) or len(set(keys))!=len(keys) or not required<=set(keys)
                    or set(keys)-required-{'--claude-code'}): raise AdmissionError()
            values=dict(zip(keys,options[1::2])); pin=values['--trusted-index-sha256']
            claude_code=values.get('--claude-code')
            if claude_code is not None and claude_code not in ('user','plugin','skip'): raise AdmissionError()
            admit(root,Path(values['--index']),pin,False)
            sys.path[:]=[str(root/'app'),str(root/'installer')]+[p for p in sys.path if Path(p).is_relative_to(root/'python')]
            from telegram_search_mcp import bundle_install
            try:
                result=bundle_install.install(root,Path(values['--index']),pin,installation_root=Path(values['--installation-root']),
                    codex_home=Path(values['--codex-home']),skills_root=Path(values['--skills-root']),user_home=Path.home(),
                    claude_code=claude_code,adopt_legacy=adopt)
            except bundle_install.InstallError as error:
                named=error.args[0] if len(error.args)==1 else None
                if type(named) is str and named in INSTALL_CODES: code=named
                pending=getattr(error,'pending_action',None)
                pending_account=getattr(error,'pending_account',None)
                account=getattr(error,'account',None)
                raise
            print(json.dumps(result,sort_keys=True)); return 0
        # capabilities show | enable <profile> | disable <profile> [--account <alias>]: the owner's profile switch
        # (closed grammar; the profile name is checked again against the closed table).
        profile=profile_action(args)
        profile_change=profile is not None
        if profile_change: args,alias=profile
        else: args,alias=account_action(args)
        if args not in [[v] for v in ACTIONS] and not profile_change: raise AdmissionError()
        receipt=_json(_read(root.parent.parent/'receipt.json',mode=0o600)); pin=receipt.get('index_sha256')
        if receipt.get('release_root')!=str(root) or root!=root.parent.parent/'releases'/str(pin): raise AdmissionError()
        admit(root,root/'bundle-index.json',pin,True)
        # status also reads the Claude Desktop entry (its registration module needs the installer path).
        sys.path[:]=([str(root/'app')]+([str(root/'installer')] if args==['status'] or profile_change else [])
                     +[p for p in sys.path if Path(p).is_relative_to(root/'python')])
        from telegram_search_mcp.bundle_runtime import activate
        activate(root,pin)
        account=None
        if alias is not None:
            # The registry admits the alias before any directory is created; a refusal is its own fixed code.
            code='alias_unavailable'
            from telegram_search_mcp.config import AccountPaths
            account=AccountPaths.for_alias(alias,Path.home())
            code='bundle_admission_failed'
        if args==['status']:
            return status(root) if alias is None else status(root,alias)
        if profile_change:
            return capabilities(root,args[1:]) if account is None else capabilities(root,args[1:],account)
        if args==['mcp']:
            return dispatch_mcp() if alias is None else dispatch_mcp(alias)
        if args==['broker']:
            from telegram_search_mcp.launch_agent import run_broker
            return run_broker() if account is None else run_broker(account)
        from telegram_search_mcp.setup_cli import main as dispatch
        return dispatch(args if alias is None else [*args,'--account',alias])
    except (Exception,KeyboardInterrupt):
        refusal={'status':'refused','code':code}
        if code=='transaction_pending' and type(pending) is str and pending in PENDING_ACTIONS:
            refusal['pending_action']=pending
            if pending in ACCOUNT_CHANGES and alias_token(pending_account): refusal['pending_account']=pending_account
        if code in ACCOUNT_NAMED and alias_token(account):
            refusal['account']=account
        if code=='alias_broker_unreadable' and alias_token(account):
            try:
                refusal.update(account=account,next_action=UNREADABLE_ACTION,bootout_command=bootout_command(account))
            except Exception:
                refusal={'status':'refused','code':code}
        print(json.dumps(refusal),file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
