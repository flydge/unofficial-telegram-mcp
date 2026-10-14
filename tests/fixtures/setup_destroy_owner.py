"""Synthetic owning process for teardown-lifetime tests; no native boundaries."""
import gc
import json
from pathlib import Path
import sys
import weakref
from telegram_search_mcp.credentials import SecretValue
from telegram_search_mcp.enrollment import SetupPaths
from telegram_search_mcp.setup_wizard import run_setup


class Terminal:
    def require_secure(self): pass
    def read_secret(self, kind, *, timeout):
        return SecretValue({'api_id': b'123', 'api_hash': b'a' * 32}[kind])


class Store:
    def __init__(self): self.values = {}
    def get(self, account): return self.values.get(account)
    def put(self, account, value):
        assert account not in self.values
        self.values[account] = value


class Raw:
    def __init__(self, mode, provider_closed):
        self.queue, self.alive, self.destroy_calls = [], True, 0
        self.mode, self.provider_closed = mode, provider_closed
    def send(self, payload):
        if payload['@type'] == 'getAuthorizationState':
            self.queue.append({'@type': 'authorizationStateReady', '@extra': payload['@extra']})
        elif self.provider_closed:
            self.queue.extend([{'@type': 'ok', '@extra': payload['@extra']},
                {'@type': 'updateAuthorizationState', 'authorization_state': {'@type': 'authorizationStateClosed'}}])
        else:
            self.queue.append({'@type': 'error', '@extra': payload['@extra'], 'code': 500})
    def receive(self, timeout): return self.queue.pop(0) if self.queue else None
    def close(self):
        self.destroy_calls += 1
        raise {'exception': RuntimeError, 'interrupt': KeyboardInterrupt,
               'system_exit': SystemExit}[self.mode]('SYNTHETIC_DESTROY_SENTINEL')


def main():
    root = Path(sys.argv[1])
    assert root.is_absolute() and root.parent.name.startswith('b1a-destroy-')
    paths, terminal, store = SetupPaths(root), Terminal(), Store()
    raw = Raw(sys.argv[2], sys.argv[3] == 'closed')
    factories, handoffs = [], []
    def factory():
        factories.append(True)
        return raw
    def handoff():
        handoffs.append(True)
        return 'ready'
    result = run_setup(paths, terminal, store, factory, handoff=handoff)
    # A caller discarding normal local result/transport refs cannot release the
    # lease; another setup in this process cannot replay native initialization.
    raw_reference = weakref.ref(raw)
    raw = None
    gc.collect()
    again = run_setup(paths, terminal, store, factory, handoff=handoff, resume=True)
    owner = raw_reference()
    print(json.dumps({'first': result.to_dict(), 'again': again.to_dict(),
        'native_alive': owner is not None and owner.alive, 'destroy_calls': owner.destroy_calls if owner else None,
        'provider_constructions': len(factories), 'handoff_calls': len(handoffs)}), flush=True)
    assert sys.stdin.readline() == 'exit\n'  # Test control only; no secret input.


if __name__ == '__main__':
    main()
