"""Memory-only, one-use delegation for NEW bytes; never adopt foreign artifacts.

Lock order: shared upload registry -> artifact authority -> broker metadata state.
Copy/fsync/hash are outside those locks. Final pure checks, grant and response
publication share the fence; losing a response never permits another mutation.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import secrets
import time

from .artifact_authority import ArtifactScope, ArtifactUnavailable
from .artifact_store import ArtifactStoreError
from .local_upload import UploadError, _LIMITS
from .local_upload_models import (LocalUploadIssued, LocalUploadUnavailable,
    LocalUploadPending, LocalUploadCancelled, LocalUploadConsumed,
    AuthorizedUploadReady, AuthorizedUploadAccepted, AuthorizedUploadComplete)

TICKET_SECONDS = 900
MAX_DELIVERY_RECEIPTS = 32


@dataclass
class _Ticket:
    result_ref: str
    authority_digest: str
    owner: object
    manifest: object
    epoch: object
    expires_wall: float
    expires_monotonic: float
    state: str = 'issued'
    supplier: object | None = None
    upload_id: str | None = None
    completed: object | None = None
    receipt: object | None = None


class LocalUploadAuthorityRegistry:
    def __init__(self, uploads, authority, *, admission, clock=time.monotonic):
        self.uploads, self.authority, self.admission, self.clock = uploads, authority, admission, clock
        self._tickets = {}
        self._tokens = {}
        self._streams = {}
        self._retired = set()
        uploads._reservation_prune = self.prune

    @contextmanager
    def _fence(self):
        checked_policy = self.admission()
        with self.uploads._lock, self.authority.lock:
            checked_policy()
            self._purge()
            self.uploads._prune()
        self.cleanup()
        with self.uploads._lock, self.authority.lock:
            checked_policy()
            self._purge()
            self.uploads._prune()
            yield

    def _owner_live(self, owner):
        self.authority.live(ArtifactScope(self.authority, self.uploads._store, owner))

    def _check(self, ticket):
        self._owner_live(ticket.owner)
        if self.clock() >= ticket.expires_monotonic or self.uploads._clock() >= ticket.expires_wall:
            raise UploadError('local upload is unavailable')
        if ticket.state != 'consumed':
            if ticket.epoch is not self.authority._account_epoch:
                raise UploadError('local upload is unavailable')
            if ticket.supplier is not None:
                self._owner_live(ticket.supplier)

    def _revoke(self, ticket):
        ticket.state = 'failed'
        self._tickets.pop(ticket.result_ref, None)
        self._tokens.pop(ticket.authority_digest, None)
        entry = self.uploads._entries.get(ticket.upload_id)
        if entry is None:
            self.uploads._reservations.discard(ticket.result_ref)
        else:
            self.uploads._retire_fact(ticket.upload_id)
        if ticket.upload_id is not None:
            self._streams.pop(ticket.upload_id, None)
            self._retired.add(ticket.upload_id)

    def _purge(self):
        for ticket in list(self._tickets.values()):
            try:
                self._check(ticket)
            except (UploadError, ArtifactUnavailable):
                self._revoke(ticket)

    def prune(self):
        # Invoked from legacy admission under the shared upload registry lock.
        with self.authority.lock:
            self._purge()

    def cleanup(self):
        # Pure retirement facts were collected under the fence. Filesystem cleanup
        # is outside the authority lock; finishing paths remain tracked until copy ends.
        with self.uploads._lock:
            retired, self._retired = self._retired, set()
        for upload_id in retired:
            self.uploads._drop_external(upload_id)
        self.uploads._cleanup_retired()

    def release(self, client_id):
        with self.uploads._lock, self.authority.lock:
            self.authority.release(client_id)
            for ticket in list(self._tickets.values()):
                if ticket.owner.client_id == client_id or (
                        ticket.state != 'consumed' and ticket.supplier is not None
                        and ticket.supplier.client_id == client_id):
                    self._revoke(ticket)
        self.cleanup()

    def shutdown(self, stop=None):
        with self.uploads._lock, self.authority.lock:
            if stop is not None:
                stop()
            for ticket in list(self._tickets.values()):
                self._revoke(ticket)
        self.cleanup()

    @staticmethod
    def _publish(response, publish):
        value = response.model_dump(mode='json')
        if publish is not None:
            publish(value)
        return value

    def issue(self, scope, manifest, publish):
        with self._fence():
            self.authority.live(scope)
            occupied = len(self.uploads._reservations) + sum(not e.external for e in self.uploads._entries.values())
            if occupied >= self.uploads._capacity:
                return self._publish(LocalUploadUnavailable(), publish)
            secret = 'upload_authority_' + secrets.token_hex(32)
            token = 'upload_result_' + secrets.token_hex(32)
            digest = hashlib.sha256(secret.encode('ascii')).hexdigest()
            if token in self._tickets or digest in self._tokens:
                return self._publish(LocalUploadUnavailable(), publish)
            wall = self.uploads._clock() + TICKET_SECONDS
            ticket = _Ticket(token, digest, scope.owner, manifest, self.authority._account_epoch,
                wall, self.clock() + TICKET_SECONDS)
            self._tickets[token] = ticket
            self._tokens[digest] = token
            self.uploads._reservations.add(token)
            return self._publish(LocalUploadIssued(upload_authority=secret, result_ref=token,
                expires_at=datetime.fromtimestamp(wall, timezone.utc), max_size_bytes=_LIMITS[manifest.kind]), publish)

    def redeem(self, scope, envelope, publish):
        with self._fence():
            self.authority.live(scope)
            digest = hashlib.sha256(envelope.upload_authority.encode('ascii')).hexdigest()
            token = self._tokens.pop(digest, None)
            ticket = self._tickets.get(token)
            if ticket is None or ticket.state != 'issued':
                return self._publish(LocalUploadUnavailable(), publish)
            # Spend the claim before comparing. Even a mismatched first claim
            # cannot be retried; the original owner cancels/issues a new ticket.
            if envelope.manifest != ticket.manifest:
                self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
            self._check(ticket)
            ticket.state, ticket.supplier = 'uploading', scope.owner
            try:
                ticket.upload_id, entry, pin = self.uploads._claim_begin(**ticket.manifest.model_dump(),
                    _reservation=token, _expires_at=ticket.expires_wall)
            except (UploadError, OSError, ValueError):
                self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
        committed = False
        try:
            try:
                self.uploads._setup(entry)
            except (UploadError, OSError, ValueError):
                return self._publish(LocalUploadUnavailable(), publish)
            with self._fence():
                if self._tickets.get(token) is not ticket or ticket.state != 'uploading':
                    return self._publish(LocalUploadUnavailable(), publish)
                self._check(ticket)
                self.uploads._check_pin(ticket.upload_id, entry, pin)
                entry.busy = None
                self._streams[ticket.upload_id] = token
                committed = True
                return self._publish(AuthorizedUploadReady(upload_id=ticket.upload_id, result_ref=token,
                    expires_at=datetime.fromtimestamp(ticket.expires_wall, timezone.utc)), publish)
        finally:
            if not committed:
                with self.uploads._lock, self.authority.lock:
                    self._revoke(ticket)
            self.uploads._release_pin(ticket.upload_id, entry, pin, retire=not committed)

    def _supplier(self, scope, upload_id):
        ticket = self._tickets.get(self._streams.get(upload_id))
        if ticket is None or ticket.state != 'uploading' or ticket.supplier != scope.owner:
            raise UploadError('local upload is unavailable')
        self._check(ticket)
        return ticket

    def append(self, scope, request, publish):
        try:
            data = self.uploads._decode_chunk(index=request.index, content_base64=request.content_base64,
                                             sha256=request.sha256)
        except UploadError:
            with self._fence():
                self._revoke(self._supplier(scope, request.upload_id))
                return self._publish(LocalUploadUnavailable(), publish)
        with self._fence():
            ticket = self._supplier(scope, request.upload_id)
            try:
                entry, pin = self.uploads._claim_append(request.upload_id, index=request.index,
                                                      size=len(data), external=True)
            except UploadError:
                self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
        committed = False
        try:
            try:
                self.uploads._append_io(entry, data)
            except (UploadError, OSError, ValueError):
                return self._publish(LocalUploadUnavailable(), publish)
            with self._fence():
                if self._supplier(scope, request.upload_id) is not ticket:
                    raise UploadError('local upload is unavailable')
                index, size = self.uploads._accept_append(request.upload_id, entry, pin, size=len(data))
                committed = True
                return self._publish(AuthorizedUploadAccepted(upload_id=request.upload_id,
                    next_index=index, received_bytes=size), publish)
        finally:
            if not committed:
                with self.uploads._lock, self.authority.lock:
                    self._revoke(ticket)
            self.uploads._release_pin(request.upload_id, entry, pin, retire=not committed)

    def finish(self, scope, request, publish):
        with self._fence():
            ticket = self._supplier(scope, request.upload_id)
            try:
                entry = self.uploads._mark_external_finishing(request.upload_id)
            except UploadError:
                self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
            ticket.state = 'finishing'
            pin = entry.busy
        committed = False
        try:
            try:
                completed = self.uploads._copy_external(entry)
            except (UploadError, ArtifactStoreError, OSError, ValueError):
                with self.uploads._lock, self.authority.lock:
                    self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
            with self._fence():
                if self._tickets.get(ticket.result_ref) is not ticket or ticket.state != 'finishing':
                    return self._publish(LocalUploadUnavailable(), publish)
                self._check(ticket)
                self.uploads._check_pin(request.upload_id, entry, pin)
                self.uploads._detach_cleaned(request.upload_id, entry, pin, release_capacity=False)
                ticket.completed, ticket.state = completed, 'complete'
                self._streams.pop(request.upload_id, None)
                committed = True
                return self._publish(AuthorizedUploadComplete(result_ref=ticket.result_ref), publish)
        finally:
            if not committed:
                with self.uploads._lock, self.authority.lock:
                    self._revoke(ticket)
            self.uploads._release_pin(request.upload_id, entry, pin, retire=True,
                                      release_capacity=not committed)

    def _original(self, scope, result_ref):
        ticket = self._tickets.get(result_ref)
        if ticket is None or ticket.owner != scope.owner:
            raise UploadError('local upload is unavailable')
        self._check(ticket)
        return ticket

    def cancel(self, scope, request, publish):
        with self._fence():
            ticket = self._original(scope, request.result_ref)
            if ticket.state == 'consumed':
                return self._publish(LocalUploadUnavailable(), publish)
            self._revoke(ticket)
            return self._publish(LocalUploadCancelled(), publish)

    def consume(self, scope, request, publish, record_metadata):
        with self._fence():
            ticket = self._original(scope, request.result_ref)
            if ticket.state == 'consumed':
                if not self.authority.authorize_local(scope, ticket.receipt.artifact_id):
                    return self._publish(LocalUploadUnavailable(), publish)
                return self._publish(ticket.receipt, publish)
            if ticket.state != 'complete':
                return self._publish(LocalUploadPending(), publish)
            expected = ticket.completed.artifact
        # Byte integrity lookup hashes the stored file; do not hold pure locks.
        actual = self.uploads._store._lookup_bytes(expected.artifact_id)
        with self._fence():
            ticket = self._original(scope, request.result_ref)
            if ticket.state == 'consumed':
                if not self.authority.authorize_local(scope, ticket.receipt.artifact_id):
                    return self._publish(LocalUploadUnavailable(), publish)
                return self._publish(ticket.receipt, publish)
            if (ticket.state != 'complete' or actual != expected
                    or actual is None or actual.sha256 != ticket.manifest.sha256
                    or actual.size_bytes != ticket.manifest.size_bytes or actual.expires_at <= self.uploads._clock()):
                self._revoke(ticket)
                return self._publish(LocalUploadUnavailable(), publish)
            # Never transfer the observed ticket epoch/account into an offline grant.
            local = ArtifactScope(self.authority, self.uploads._store, ticket.owner)
            receipt = LocalUploadConsumed(artifact_id=actual.artifact_id, artifact_path=str(actual.path),
                sha256=actual.sha256, size_bytes=actual.size_bytes, file_name=ticket.manifest.file_name,
                expires_at=datetime.fromtimestamp(actual.expires_at, timezone.utc))
            self.authority.grant(local, actual)
            record_metadata(ticket.completed)
            ticket.receipt, ticket.state = receipt, 'consumed'
            self.uploads._reservations.discard(ticket.result_ref)
            delivered = [t for t in self._tickets.values() if t.state == 'consumed']
            for old in delivered[:-MAX_DELIVERY_RECEIPTS]:
                self._revoke(old)
            return self._publish(receipt, publish)
