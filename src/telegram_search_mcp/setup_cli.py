"""Installed local auth CLI with a closed, credential-free argument grammar."""
from __future__ import annotations
import json
import os
import sys
import time
from .broker_client import BrokerClient, BrokerCompatibilityError, BrokerUnavailable
from .config import load_runtime_policy
from .credentials import MacOSSecretStore
from .enrollment import SetupPaths
from .launch_agent import CONNECT_JOB_LABEL, start_enrolled_broker, validate_enrolled_broker, LaunchAgentError
from .secure_terminal import NativeTerminal
from .session_owner import SessionOwnerLease, OwnerLeaseError
from .setup_wizard import run_setup, setup_status
from .tdjson import LegacyTDJson

_HELP = '''Usage: telegram-search-setup {connect|wait|cancel|setup|resume|status|api-credentials}

Unreleased technical support for a new default session on Apple ARM64 macOS.
connect opens the local QR screen on this Mac and returns at once; wait reports
its next phase change (up to 540 s per call; repeat until ready, failed, expired
or cancelled); cancel stops a screen opened by connect. All three are safe from a
captured agent shell and take no credentials.
setup/resume/api-credentials require a foreground, uncaptured native Apple Terminal.
API credentials, phone, Telegram code and optional 2FA use hidden prompts.
Never pass secrets as arguments, environment variables or piped input.
api-credentials stores your own Telegram API ID and hash (add-only) before the
first connect; without it an installed bundle supplies its own application identity.
status reads enrollment metadata and an existing broker handshake only;
it does not initialize TDLib, read Keychain, start services or prove Ready.
connect-helper is internal: connect starts it as a transient launchd job.
Pinned TDLib and an installed matching Python package remain prerequisites.
'''
COMMANDS = ('setup', 'resume', 'status', 'connect', 'wait', 'cancel', 'api-credentials', 'connect-helper')


def _client(paths, timeout):
    return BrokerClient(socket_path=paths.lock.parent / 'broker.sock',
        policy=load_runtime_policy(paths.policy), request_timeout=timeout,
        restart_callback=lambda: None, retry_backoff_seconds=0)


def existing_broker_status(paths):
    if not (paths.lock.parent / 'broker.sock').exists():
        return 'not_running'
    client = None
    try:
        client = _client(paths, 1)
        client.handshake()  # No provider/check request and no service restart.
        return 'compatible_running'
    except BrokerCompatibilityError:
        return 'incompatible_or_unavailable'
    except Exception:
        return 'unavailable'
    finally:
        if client is not None: client.close()


def handoff_broker(*, paths=None, timeout=30, sleeper=time.sleep):
    """Successful launch is not Ready. Never stop a competing owner."""
    paths = SetupPaths() if paths is None else paths
    client = None
    busy = False
    try:
        try:
            with SessionOwnerLease(paths.lock):
                pass
        except OwnerLeaseError as error:
            if error.code != 'owner_busy': return error.code
            busy = True
        if busy:
            try:
                validate_enrolled_broker()  # Read-only; preserve the competing owner.
            except LaunchAgentError:
                return 'broker_incompatible'
        else:
            start_enrolled_broker()
        deadline = time.monotonic() + timeout
        for _ in range(1000):
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            if client is not None: client.close()
            client = _client(paths, min(remaining, 5))
            try:
                client.handshake()
                if client.check() == 'ready':
                    return 'ready'
            except BrokerCompatibilityError as error:
                # Missing/unstarted sockets are retriable readiness observations;
                # a real contract/config mismatch refuses immediately.
                if error.code not in {'handshake_unavailable_or_legacy_broker'}:
                    return 'broker_incompatible'
            except BrokerUnavailable:
                pass
            if busy: return 'owner_busy'
            sleeper(min(.1, max(0, deadline - time.monotonic())))
        return 'owner_busy' if busy else 'broker_readiness_timeout'
    except Exception:
        return 'broker_start_failed'
    finally:
        if client is not None: client.close()


def _connect_command(command, paths):
    from . import connect_cli  # Loaded only here: the unchanged commands never import the screen stack.
    def helper(**options):
        return connect_cli.connect_helper(paths, store=MacOSSecretStore(), provider_factory=LegacyTDJson,
                                          handoff=handoff_broker, **options)
    if command == 'connect-helper':  # Internal: only our launchd job runs it, and it prints nothing.
        if os.environ.get('XPC_SERVICE_NAME') != CONNECT_JOB_LABEL:
            # Not launched as our transient job: touch nothing (no lock, status or plist).
            print(json.dumps({'status': 'refused', 'code': 'connect_helper_requires_launchd',
                              'next_action': 'run_connect'}, sort_keys=True))
            return 2
        try:
            return helper(own_plist=connect_cli.job_plist_path(paths), handle_sigterm=True).exit_code
        except (Exception, KeyboardInterrupt):
            return 2
    if command == 'connect':
        # Inline in native Terminal: connect itself holds connect.lock for the whole run.
        value, code = connect_cli.connect(paths, client_factory=_client, terminal=NativeTerminal(),
                                          inline=lambda: helper(lock_held=True))
    elif command == 'wait':
        value, code = connect_cli.wait(paths)
    elif command == 'cancel':
        value, code = connect_cli.cancel(paths)
    else:
        value, code = connect_cli.api_credentials(paths, terminal=NativeTerminal(),
                                                  store_factory=MacOSSecretStore)
    print(json.dumps(value, sort_keys=True))
    return code


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # argparse's default diagnostics echo rejected inputs. Keep a closed grammar
    # and redacted diagnostics, including for secret-bearing accidental flags.
    if args in ([], ['--help'], ['-h']):
        print(_HELP, end='')
        return 0
    if args not in [[command] for command in COMMANDS]:
        print(json.dumps({'status': 'refused', 'code': 'invalid_arguments',
                          'detail': 'Use connect, wait, cancel, setup, resume, status, api-credentials or --help; '
                                    'never pass secrets.'}))
        return 2
    paths = SetupPaths()
    if args == ['status']:
        status = setup_status(paths)
        status['broker'] = existing_broker_status(paths)
        print(json.dumps(status, sort_keys=True))
        return 0 if status['state'] != 'inconsistent' else 2
    if args[0] in ('connect', 'wait', 'cancel', 'api-credentials', 'connect-helper'):
        return _connect_command(args[0], paths)
    terminal = NativeTerminal()
    try:
        terminal.require_secure()
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'status': 'pending', 'code': 'secure_terminal_required',
                          'next_action': 'run_in_foreground_native_terminal'}))
        return 2
    result = run_setup(paths, terminal, MacOSSecretStore(), LegacyTDJson,
                       handoff=lambda: handoff_broker(paths=paths), resume=args == ['resume'])
    print(json.dumps(result.to_dict(), sort_keys=True))
    return result.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
