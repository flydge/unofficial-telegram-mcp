"""Memory-only fixed-lifetime selections; provider work belongs outside the lock."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
from .config import SELECTION_TTL_DEFAULT, SELECTION_TTL_MAXIMUM, SELECTION_TTL_MINIMUM
from .selection_projection import SelectionUnavailable


class SelectionCapacity(SelectionUnavailable):
    """Transient capacity refusal does not revoke existing selections."""


@dataclass(frozen=True)
class SelectionBinding:
    selection_id: str
    owner: object
    kind: str
    source_key: object
    facts: tuple
    anchors: tuple
    expires_monotonic: float
    expires_at: datetime
    account_id: int | None = None
    account_epoch: object | None = None
    artifact_id: str | None = None
    target_handle: str | None = None
    target_dependency: tuple | None = None
    schema_fingerprint: str = ''


class SelectionRegistry:
    def __init__(self, *, ttl_seconds=SELECTION_TTL_DEFAULT, clock=time.monotonic, wall_clock=lambda:datetime.now(timezone.utc)):
        # Trusted policy value; fixed from capture start and never renewed.
        if type(ttl_seconds) is not int or not SELECTION_TTL_MINIMUM<=ttl_seconds<=SELECTION_TTL_MAXIMUM:
            raise ValueError('selection TTL is outside the policy bounds')
        self.ttl_seconds=ttl_seconds
        self.clock,self.wall_clock=clock,wall_clock
        self.lock=threading.RLock()
        self._entries={};self._active={};self._pending={}

    def _purge(self):
        now,wall=self.clock(),self.wall_clock()
        self._entries={k:v for k,v in self._entries.items() if now<v.expires_monotonic and wall<v.expires_at}

    @contextmanager
    def reserve(self, owner, token=None):
        with self.lock:
            self._purge()
            if token is not None:self.get(token,owner)
            count=sum(v.owner==owner for v in self._entries.values())
            if self._active.get(owner,0)>=4 or (token is None and count+self._pending.get(owner,0)>=16):raise SelectionCapacity()
            self._active[owner]=self._active.get(owner,0)+1
            if token is None:self._pending[owner]=self._pending.get(owner,0)+1
            started=(self.clock(),self.wall_clock(),token is None)
        try:yield started
        finally:
            with self.lock:
                self._active[owner]-=1
                if token is None:self._pending[owner]-=1

    def issue(self, owner, started, *, kind, source_key, facts, anchors=(), expires_at=None, **kwargs):
        with self.lock:
            self._purge()
            for existing in self._entries.values():
                if (existing.owner==owner and existing.kind==kind and existing.source_key==source_key and
                        existing.facts==facts and all(getattr(existing,k)==v for k,v in kwargs.items())):return existing
            # An observed replacement permanently retires the prior source token.
            # Unchanged recapture returned above, so capacity alone never revokes it.
            self._entries={k:v for k,v in self._entries.items()
                if not (v.owner==owner and v.source_key==source_key)}
            count=sum(value.owner==owner for value in self._entries.values())
            if count>=16 or (not started[2] and count+self._pending.get(owner,0)>=16):raise SelectionCapacity()
            wall=min(started[1]+timedelta(seconds=self.ttl_seconds),expires_at or datetime.max.replace(tzinfo=timezone.utc))
            monotonic=started[0]+min(float(self.ttl_seconds),(wall-started[1]).total_seconds())
            if self.clock()>=monotonic or self.wall_clock()>=wall:raise SelectionUnavailable()
            token='selection_'+secrets.token_hex(32)
            if token in self._entries:raise SelectionUnavailable()
            binding=SelectionBinding(token,owner,kind,source_key,facts,anchors,monotonic,wall,**kwargs)
            self._entries[token]=binding
            return binding

    def find_source(self,owner,source_key):
        with self.lock:
            self._purge()
            return next((value for value in self._entries.values() if value.owner==owner and value.source_key==source_key),None)

    def get(self, token, owner):
        with self.lock:
            self._purge()
            binding=self._entries.get(token)
            if binding is None or binding.owner!=owner:raise SelectionUnavailable()
            return binding

    def check(self, binding):
        with self.lock:
            if self.get(binding.selection_id,binding.owner) is not binding:raise SelectionUnavailable()

    def invalidate(self, token):
        with self.lock:self._entries.pop(token,None)

    def release(self, client_id):
        with self.lock:
            self._entries={k:v for k,v in self._entries.items() if v.owner.client_id!=client_id}
