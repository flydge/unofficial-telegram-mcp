"""Memory-only authority captured by trusted broker dispatch, never tool payloads.

A broker generation is a fixed provider-session namespace, not a numeric account.
Raw byte-store use outside broker dispatch remains an internal API; persisted bytes
alone never establish a grant in a broker scope.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import threading
from typing import Callable


class ArtifactUnavailable(RuntimeError):
    """Content-free lifecycle/ownership refusal."""


@dataclass(frozen=True)
class ArtifactOwner:
    client_id: str
    generation: str
    lifecycle: object


@dataclass
class ArtifactScope:
    authority: ArtifactAuthority
    store: object
    owner: ArtifactOwner
    account_id: int | None = None
    account_epoch: object | None = None


@dataclass(frozen=True)
class _Grant:
    owner: ArtifactOwner
    expires_at: float
    account_id: int | None
    account_epoch: object | None


_ACTIVE: ContextVar[ArtifactScope | None] = ContextVar('broker_artifact_scope', default=None)


def active_scope(store=None):
    scope = _ACTIVE.get()
    return scope if scope is not None and (store is None or scope.store is store) else None


class ArtifactAuthority:
    def __init__(self, generation: str, *, clock: Callable[[], float], account_lookup: Callable[[], int | None]):
        self.generation = generation
        self.clock = clock
        self.account_lookup = account_lookup
        self.lock = threading.RLock()
        self._clients: dict[str, object] = {}
        self._grants: dict[str, _Grant] = {}
        self._account_id: int | None = None
        self._account_epoch = object()

    @contextmanager
    def scope(self, client_id, store):
        with self.lock:
            lifecycle = self._clients.setdefault(client_id, object())
            scope = ArtifactScope(self, store, ArtifactOwner(client_id, self.generation, lifecycle))
        token = _ACTIVE.set(scope)
        try:
            yield scope
        finally:
            _ACTIVE.reset(token)

    def live(self, scope):
        with self.lock:
            if (scope.authority is not self or scope.owner.generation != self.generation or
                    self._clients.get(scope.owner.client_id) is not scope.owner.lifecycle or
                    (scope.account_epoch is not None and scope.account_epoch is not self._account_epoch)):
                raise ArtifactUnavailable('artifact is unavailable')

    def release(self, client_id):
        with self.lock:
            self._clients.pop(client_id, None)
            self._grants = {key: value for key, value in self._grants.items() if value.owner.client_id != client_id}

    def invalidate_account(self):
        with self.lock:
            self._account_epoch = object()
            self._account_id = None
            self._grants = {key: value for key, value in self._grants.items() if value.account_id is None}

    def observed_account(self):
        """The account last observed at an account boundary, or None after an invalidation. No provider read:
        for content-free routing only, never a grant."""
        with self.lock:
            return self._account_id

    def observe_account(self, account):
        if type(account) is not int or not 0 < account < 2**53:
            self.invalidate_account()
            raise ArtifactUnavailable('artifact is unavailable')
        with self.lock:
            if self._account_id is not None and account != self._account_id:
                self._account_epoch = object()
                self._grants = {key: value for key, value in self._grants.items() if value.account_id is None}
            self._account_id = account

    def observe(self, scope, account):
        if type(account) is not int or not 0 < account < 2**53:
            self.invalidate_account()
            raise ArtifactUnavailable('artifact is unavailable')
        with self.lock:
            self.live(scope)
            self.observe_account(account)
            if scope.account_id is not None and (scope.account_id != account or scope.account_epoch is not self._account_epoch):
                raise ArtifactUnavailable('artifact is unavailable')
            scope.account_id = account
            scope.account_epoch = self._account_epoch
        return account

    def check_account(self, scope):
        self.live(scope)
        if scope.account_id is not None:
            account = self.account_lookup()
            if account is None:
                raise ArtifactUnavailable('artifact is unavailable')
            self.observe(scope, account)

    def grant(self, scope, artifact):
        self.check_account(scope)
        with self.lock:
            self.live(scope)
            self._grants = {key: value for key, value in self._grants.items() if value.expires_at > self.clock()}
            self._grants[artifact.artifact_id] = _Grant(scope.owner, artifact.expires_at, scope.account_id, scope.account_epoch)

    def authorize_local(self, scope, artifact_id):
        """Pure grant/lifecycle facts; safe under a registry critical section.

        Provider observations belong at dispatch/account/publication boundaries,
        outside registry, broker-state and authority locks.
        """
        with self.lock:
            self.live(scope)
            grant = self._grants.get(artifact_id)
            if (grant is None or grant.owner != scope.owner or grant.expires_at <= self.clock() or
                    (grant.account_epoch is not None and grant.account_epoch is not self._account_epoch)):
                return False
            if grant.account_id is not None:
                if scope.account_id is not None and scope.account_id != grant.account_id:
                    return False
                scope.account_id = grant.account_id
                scope.account_epoch = grant.account_epoch
            return True

    def authorize(self, scope, artifact_id):
        if not self.authorize_local(scope, artifact_id):
            return False
        self.check_account(scope)
        return self.authorize_local(scope, artifact_id)

    @contextmanager
    def publication(self, scope):
        self.check_account(scope)
        with self.lock:
            self.live(scope)
            yield


class ArtifactReaderClient:
    """Lazy reader budget/account adapter: offline artifacts never start TDLib."""
    def __init__(self, store, provider):
        self.store = store
        self.provider = provider

    def request_budget(self, deadline):
        scope = active_scope(self.store)
        if scope is None or scope.account_id is None:
            return nullcontext()
        return self.provider().request_budget(deadline)

    def artifact_account(self, expected=None):
        scope = active_scope(self.store)
        if scope is None:
            raise ArtifactUnavailable('artifact is unavailable')
        scope.authority.check_account(scope)
        if expected is not None and expected != scope.account_id:
            raise ArtifactUnavailable('artifact is unavailable')
        return scope.account_id
