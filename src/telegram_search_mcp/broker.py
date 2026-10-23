"""Single-owner Unix-socket broker for the local TDLib session."""

from __future__ import annotations

import ctypes
import base64
import binascii
import errno
import os
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .artifact_store import ArtifactStore, ArtifactStoreError
from .artifact_authority import ArtifactAuthority, ArtifactReaderClient, ArtifactUnavailable, active_scope
from .config import ConfigurationError
from .approval_prompt import (DETAILS as APPROVAL_DETAILS, NOT_GIVEN, UNAVAILABLE as DIALOG_UNAVAILABLE, ApprovalDecision,
                              approval_outcome, decide_approved_send)
from .attachment_service import get_attachment, get_message_context
from .message_reader import read_messages
from .reply_reader import read_reply_chain
from .broker_protocol import (
    FRAME_READ_TIMEOUT_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    BrokerProtocolError,
    receive_request,
    send_frame,
)
from .config import DEFAULT_ACCOUNT_PATHS, AccountPaths, ensure_private_directory, RuntimePolicy, load_runtime_policy
from . import account_registry, broker_readiness
from .contract import (CapabilityDisabled, CompatibilityError, contract_descriptor, missing_capabilities,
                       require_compatible, require_current, OPERATION_CAPABILITIES, required_capabilities)
from .document_reader import read_document
from .media_analyzer import analyze_media
from .local_upload import LocalUploadRegistry, UploadError
from .local_upload_authority import LocalUploadAuthorityRegistry
from .local_upload_models import UPLOAD_MODELS, LocalUploadUnavailable
from .outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry
from .outgoing_media import validate_photo
from .outgoing_voice import prepare_voice_note, VoiceError
from .outgoing_stage import retire_staged_document, stage_approved_document
from .sanitize import sanitize_telegram_text
from .schemas import (
    AnalyzeMediaRequest, AnalyzeMediaResponse, AttachmentImage, AttachmentRequest,
    CreateLocalArtifactRequest, CreateLocalArtifactResponse, DiscoverTargetsRequest,
    MediaFrameResult, MediaSegment, MessageContextRequest, ReadAttachmentRequest, ReadMessagesRequest, ReadHistoryRequest, ReadReplyChainRequest, ListTopicsRequest, ReadTopicHistoryRequest, ListChatsRequest,
    PrepareArtifactSendRequest, PrepareArtifactSendResponse, ReadAttachmentResponse,
    PrepareTextSendRequest, PrepareTextSendResponse, SendPreparedTextRequest, SendPreparedTextResponse,
    BeginLocalUploadRequest, BeginLocalUploadResponse, AppendLocalUploadRequest, AppendLocalUploadResponse,
    FinishLocalUploadRequest, FinishLocalUploadResponse,
    VerifyTargetRequest, ReadTargetMessagesRequest,
    ResolveTargetRequest, SearchRequest, SearchChatsRequest, SearchMessagesRequest, SendPreparedArtifactRequest, SendPreparedArtifactResponse,
)
from .send_status_models import GetSendStatusRequest, GetSendStatusResponse, send_status
from .reply_artifact_drafts import (PrepareReplyArtifactSendRequest, PrepareReplyArtifactSendResponse,
    GetReplyArtifactDraftRequest, GetReplyArtifactDraftResponse, UpdateReplyArtifactDraftRequest,
    RefreshReplyArtifactDraftRequest, ReviseReplyArtifactDraftResponse, ReplyArtifactDraftPreview)
from .reply_drafts import (ReplyDraftPreview, ReplyTextDraftPreview, PrepareReplyTextSendRequest, PrepareReplyTextSendResponse,
    GetReplyDraftRequest, GetReplyDraftResponse, UpdateReplyDraftRequest, RefreshReplyDraftRequest,
    ReviseReplyDraftResponse, PREPARED as REPLY_PREPARED, PENDING as REPLY_PENDING,
    REVISED as REPLY_REVISED, UNAVAILABLE as REPLY_UNAVAILABLE, CAPABILITY_DISABLED as REPLY_CAPABILITY_DISABLED)

from .draft_models import (ListDraftsRequest, ListDraftsResponse, GetDraftRequest, GetDraftResponse,
                           CancelDraftRequest, CancelDraftResponse, DraftPreview, DraftSummary,
                           UpdateDraftRequest, RefreshDraftRequest, ReviseDraftResponse)
from .search_service import ReadOnlyTelegramClient, SearchService
from .tdjson import AuthorizationBlocked, MessageNotFound, MessageSendFailed, MessageSendNotAttempted, MessageSendOutcomeUnknown, SecretChatRejected, TDLibClient, TDLibError
from .forward_sources import ForwardRefusal, chat_identity
from .forward_drafts import (ForwardChat, ForwardDraftPreview, ForwardSourcePreview, ForwardSpec,
    PrepareForwardSendRequest, PrepareForwardSendResponse, SendPreparedForwardRequest, SendPreparedForwardResponse,
    PREPARED as FORWARD_PREPARED, REFUSED as FORWARD_REFUSED, UNAVAILABLE as FORWARD_UNAVAILABLE)
from .video_probe import VideoProbeError, probe_video
from .video_drafts import (MAX_VIDEO_BYTES, VideoDraftPreview, VideoRecipient, VideoRefusal, VideoSpec,
    PrepareVideoSendRequest, PrepareVideoSendResponse, SendPreparedVideoRequest, SendPreparedVideoResponse,
    PREPARED as VIDEO_PREPARED, REFUSED as VIDEO_REFUSED, UNAVAILABLE as VIDEO_UNAVAILABLE)
from .archive_drafts import (ArchiveChat, ArchiveDraftPreview, ArchiveRefusal, ArchiveSpec, PrepareChatArchiveRequest,
    PrepareChatArchiveResponse, ApplyPreparedArchiveRequest, ApplyPreparedArchiveResponse,
    PREPARED as ARCHIVE_PREPARED, ALREADY as ARCHIVE_ALREADY, REFUSED as ARCHIVE_REFUSED,
    UNAVAILABLE as ARCHIVE_UNAVAILABLE)
from .edit_drafts import (EditChat, EditRefusal, TextEditDraftPreview, TextEditSpec, PrepareTextEditRequest,
    PrepareTextEditResponse, ApplyPreparedTextEditRequest, ApplyPreparedTextEditResponse, evidence_fits,
    edge_whitespace, normalized_text, rewritten_text, PREPARED as TEXT_EDIT_PREPARED, REFUSED as TEXT_EDIT_REFUSED,
    REWRITTEN as TEXT_EDIT_REWRITTEN, UNAVAILABLE as TEXT_EDIT_UNAVAILABLE, WHITESPACE as TEXT_EDIT_WHITESPACE)

RECEIPT_AUTHORITY_UNAVAILABLE = 'current receipt authority is unavailable; do not retry automatically'
# The operations that may reach the provider after the owner's approval: counted in flight while they are handled.
SEND_OPERATIONS = frozenset({'send_prepared_text', 'send_prepared_artifact', 'send_prepared_forward',
                             'send_prepared_video', 'apply_prepared_archive', 'apply_prepared_text_edit'})


# Operations exempt from the account pin check (spec 2): lifecycle (`check` runs the pin itself), and local
# bytes that no Telegram read sourced. The upload-authority operations (UPLOAD_MODELS) are routed before this
# check for the same reason: owner-local bytes. Every other operation, any added later included, is checked.
ACCOUNT_CHECK_EXEMPT = frozenset({'health', 'release_client', 'check', 'create_local_artifact',
                                  'begin_local_upload', 'append_local_upload', 'finish_local_upload'})


_UNRECORDED = object()


class BrokerStartupError(RuntimeError):
    """Raised when exclusive, private broker startup cannot be established."""


class AccountBlocked(AuthorizationBlocked):
    """The account pin refused this session: a fixed registry code, never the user id.

    An AuthorizationBlocked, so every path that reports a session that is not ready reports this one blocked.
    """

    def __init__(self, code: str) -> None:
        self.code = code if code in account_registry.CODES else 'account_mismatch'
        super().__init__(self.code)


ClientFactory = Callable[[], ReadOnlyTelegramClient]
PeerUidLoader = Callable[[socket.socket], int]


@dataclass
class _ClientContext:
    service: SearchService | None
    touched_at: float
    active_requests: int = 0
    release_requested: bool = False
    attachment_pages: object | None = None
    spreadsheets: object | None = None
    presentations: object | None = None

    def close(self) -> None:
        if self.service is not None:
            self.service.close()
        if self.attachment_pages is not None:
            self.attachment_pages.close()
        if self.spreadsheets is not None:
            self.spreadsheets.close()
        if self.presentations is not None:
            self.presentations.close()

    def invalidate_active_handles(self) -> None:
        if self.service is not None:
            self.service.close_verified_targets()
        if self.attachment_pages is not None:
            self.attachment_pages.close()
        if self.spreadsheets is not None:
            self.spreadsheets.close()
        if self.presentations is not None:
            self.presentations.close()


def _peer_uid(connection: socket.socket) -> int:
    """Return the Darwin peer UID without widening the Python socket API."""
    libc = ctypes.CDLL(None, use_errno=True)
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    getpeereid.restype = ctypes.c_int
    if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "unable to verify broker peer")
    return int(uid.value)


_LOCAL_MIME_TYPES = {
    ".txt": "text/plain", ".text": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".tsv": "text/tab-separated-values", ".json": "application/json", ".xml": "application/xml",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".py": "application/x-python",
    ".js": "application/javascript", ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain",
    ".html": "text/html", ".css": "text/css", ".sh": "text/plain", ".sql": "text/plain",
    ".log": "text/plain", ".toml": "text/plain",
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
}


def _local_artifact_classification(file_name: str, kind: str = "document") -> tuple[str, str]:
    suffix = Path(file_name).suffix.casefold()
    media_kind = ("voice_note" if kind == "voice_note" else
                  "video" if suffix in {".mp4", ".mov", ".webm"} else
                  "audio" if suffix in {".wav", ".mp3", ".ogg"} else "document")
    return _LOCAL_MIME_TYPES.get(suffix, "application/octet-stream"), media_kind


from .selection_broker import SelectionBrokerMixin, FileProvenance, selected_models
from .retained_selections import SelectionRegistry


class Broker(SelectionBrokerMixin):
    """Own one lazy Telegram client and isolate discovery state per proxy client."""

    def __init__(
        self,
        *,
        socket_path: Path,
        lock_path: Path | None = None,
        client_factory: ClientFactory | None = None,
        verify_peer_uid: bool = True,
        peer_uid_loader: PeerUidLoader = _peer_uid,
        context_clock: Callable[[], float] = time.monotonic,
        context_ttl_seconds: float = 300.0,
        max_pending: int = 64,
        max_clients: int = 64,
        max_workers: int = 8,
        artifact_store: ArtifactStore | None = None,
        download_source_root: Path | None = None,
        approval_prompt: Callable[..., ApprovalDecision | bool] = decide_approved_send,
        policy: RuntimePolicy | None = None,
        account: AccountPaths | None = None,
    ) -> None:
        """`account` None is the default alias: every default below is its 0.54 call."""
        if min(max_pending, max_clients, max_workers) < 1:
            raise ValueError("broker bounds must be positive")
        if context_ttl_seconds <= 0:
            raise ValueError("context_ttl_seconds must be positive")
        self._policy = policy if policy is not None else (
            load_runtime_policy() if account is None else load_runtime_policy(account.policy))
        self._contract = contract_descriptor(self._policy)
        self._generation = "broker_" + secrets.token_hex(16)
        self._socket_path = socket_path
        self._lock_path = lock_path or socket_path.with_name("broker.lock")
        self._client_factory = client_factory if client_factory is not None else (
            TDLibClient.from_defaults if account is None else lambda: TDLibClient.from_defaults(account))
        self._artifact_store = artifact_store if artifact_store is not None else (
            ArtifactStore() if account is None else ArtifactStore(cache_dir=account.artifacts))
        self._artifact_authority = ArtifactAuthority(self._generation, clock=lambda: self._artifact_store.clock(),
                                                     account_lookup=self._artifact_account)
        self._drafts = OutgoingDraftRegistry(self._artifact_store,
                                            max_ttl_seconds=self._policy.max_draft_ttl_seconds)
        self._uploads = (LocalUploadRegistry(self._artifact_store) if account is None
                         else LocalUploadRegistry(self._artifact_store, root=account.uploads))
        self._upload_authorities = LocalUploadAuthorityRegistry(self._uploads, self._artifact_authority,
                                                               admission=self._upload_admission)
        self._approval_prompt = approval_prompt
        # One owner dialog at a time (_owner_dialog): held only while a dialog is open.
        self._dialog_lock = threading.Lock()
        self._draft_recipient_titles: dict[str, str] = {}
        self._selections = SelectionRegistry(ttl_seconds=self._policy.selection_ttl_seconds)
        self._artifact_provenance = {}
        self._artifact_metadata: dict[str, tuple[object, str | None, str | None, str]] = {}
        self._download_source_root = (download_source_root if download_source_root is not None
                                      else (DEFAULT_ACCOUNT_PATHS if account is None else account).files)
        # Staging keeps the module's own root for the default alias (its 0.54 call).
        self._staging = {} if account is None else {"root": account.staging}
        # The account pin (spec 2): the alias and the registry beside its root; inert until a registry exists.
        account_paths = DEFAULT_ACCOUNT_PATHS if account is None else account
        self._alias = account_paths.alias
        self._accounts_root = account_paths.accounts_root
        # (client, authorization epoch, user id) of the last session the pin admitted; never leaves the broker.
        self._account_session: tuple[object, object, int] | None = None
        # The readiness last recorded for status (broker_readiness); _UNRECORDED until the first write succeeds.
        self._readiness_lock = threading.Lock()
        self._readiness: object = _UNRECORDED
        self._verify_peer_uid = verify_peer_uid
        self._peer_uid_loader = peer_uid_loader
        self._max_clients = max_clients
        self._context_clock = context_clock
        self._context_ttl_seconds = float(context_ttl_seconds)
        self._pending_slots = threading.BoundedSemaphore(max_pending)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="telegram-search-broker",
        )
        self._state_lock = threading.RLock()
        self._client: ReadOnlyTelegramClient | None = None
        self._contexts: dict[str, _ClientContext] = {}
        self._listener: socket.socket | None = None
        self._connections: set[socket.socket] = set()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._started_at = time.monotonic()
        self._pending = 0
        self._active_handlers = 0
        self._peak_overlap = 0
        self._completed = 0
        self._errors = 0
        # Pending sends a restart would cut off or lose (health `pending_sends`; the installer's gate reads them).
        self._sends_in_flight = 0
        self._approval_dialogs = 0

    def _prompt_approval(self, **kwargs) -> ApprovalDecision | bool:
        """The owner's approval dialog, counted as open while it runs. Once an alias registry exists the dialog
        names this broker's alias and a masked account (spec 3C); without one its arguments are 0.54's."""
        if account_registry.present(self._accounts_root):
            kwargs = {**kwargs, 'account_alias': self._alias}
        with self._state_lock:
            self._approval_dialogs += 1
        try:
            return self._approval_prompt(**kwargs)
        finally:
            with self._state_lock:
                self._approval_dialogs -= 1

    def _acquire_owner_lock(self) -> int:
        from .session_owner import acquire_owner_lock, OwnerLeaseError
        try:
            return acquire_owner_lock(self._lock_path)
        except OwnerLeaseError as error:
            message = ("broker owner lock is already held" if error.code == "owner_busy"
                       else "broker owner lock is unsafe")
            raise BrokerStartupError(message) from None

    def _prepare_socket_path(self) -> None:
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            self._socket_path.is_symlink()
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise BrokerStartupError("broker socket path is unsafe")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._socket_path))
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise BrokerStartupError("broker socket ownership is ambiguous") from error
        else:
            raise BrokerStartupError("an active broker socket already exists")
        finally:
            probe.close()
        try:
            current = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            self._socket_path.is_symlink()
            or not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.getuid()
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise BrokerStartupError("broker socket changed during validation")
        self._socket_path.unlink()

    def wait_until_ready(self, *, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def _shared_client(self) -> ReadOnlyTelegramClient:
        with self._state_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _expire_contexts(self) -> None:
        with self._state_lock:
            now = self._context_clock()
            expired = [
                client_id
                for client_id, context in self._contexts.items()
                if context.active_requests == 0
                and now - context.touched_at >= self._context_ttl_seconds
            ]
            for client_id in expired:
                self._contexts.pop(client_id).close()

    @contextmanager
    def _service_lease(self, client_id: str, *, artifact_only: bool = False):
        with self._state_lock:
            self._expire_contexts()
            context = self._contexts.get(client_id)
            if context is None:
                if len(self._contexts) >= self._max_clients:
                    raise BrokerProtocolError("client capacity is exhausted")
                context = _ClientContext(
                    service=None,
                    touched_at=self._context_clock(),
                )
                self._contexts[client_id] = context
            if context.service is None and not artifact_only:
                context.service = SearchService(
                        client=self._shared_client(),
                        owns_client=False,
                        client_id=client_id,
                        broker_generation=self._generation,
                    )
            context.touched_at = self._context_clock()
            context.active_requests += 1
        try:
            yield context.service
        finally:
            with self._state_lock:
                current = self._contexts.get(client_id)
                if current is context:
                    context.active_requests -= 1
                    context.touched_at = self._context_clock()
                    if context.active_requests == 0 and context.release_requested:
                        self._contexts.pop(client_id, None)
                        context.close()

    @staticmethod
    def _require_empty(payload: dict[str, Any]) -> None:
        if payload:
            raise BrokerProtocolError("operation payload must be empty")

    def _health(self) -> dict[str, Any]:
        self._expire_contexts()
        unsettled = self._drafts.unsettled_attempts()  # Its own lock, never nested in the state lock.
        with self._state_lock:
            wait_count = getattr(self._client, "serialization_wait_count", 0)
            if type(wait_count) is not int or wait_count < 0:
                wait_count = 0
            return {
                "uptime_seconds": max(0.0, time.monotonic() - self._started_at),
                "queue_depth": self._pending,
                "active_clients": len(self._contexts),
                "completed_requests": self._completed,
                "error_count": self._errors,
                "peak_overlap": self._peak_overlap,
                "serialization_wait_count": wait_count,
                # Counts only: a send or apply request being handled, an approval dialog open, and an attempt whose
                # outcome get_send_status could still settle (claimed and unfinished, or outcome_unknown, within
                # receipt retention). A restart cuts the first two off and loses the third.
                "pending_sends": {
                    "in_flight": self._sends_in_flight,
                    "approval_dialogs": self._approval_dialogs,
                    "unreconciled_receipts": unsettled,
                },
            }

    def _artifact_account(self):
        client = self._shared_client()
        getter = getattr(client, 'get_account_id', None)
        if getter is None:
            if account_registry.present(self._accounts_root):
                self._account_check()  # Refuses: with a registry, an unidentified session is never admitted.
            return None
        try:
            client.ensure_ready()
            account = getter()
        except (AuthorizationBlocked, TDLibError):
            self._artifact_authority.invalidate_account()
            raise
        if type(account) is not int or not 0 < account < 2**53:
            self._artifact_authority.invalidate_account()
            raise ArtifactUnavailable('artifact is unavailable')
        self._account_check(client=client, user_id=account)
        return account

    def _account_check(self, *, client=None, user_id=None) -> None:
        """The account pin (`_pin_session`), and its outcome recorded as this broker's readiness for status (spec
        3C): admitted true, not authorized or refused false, an unverified account unknown."""
        try:
            admitted = self._pin_session(client=client, user_id=user_id)
        except AccountBlocked as blocked:
            self._record_readiness(None if blocked.code == 'account_unverified' else False)
            raise
        if admitted is not None:
            self._record_readiness(admitted)

    def _record_readiness(self, authorized: bool | None) -> None:
        """Record a changed readiness in this broker's run directory, only once a registry exists or for a named
        alias: without one nothing is written (0.54). Best effort; a failed write leaves no record (unknown)."""
        with self._readiness_lock:
            if self._readiness is authorized:
                return
            if self._alias == account_registry.DEFAULT_ALIAS and not account_registry.present(self._accounts_root):
                return
            written = broker_readiness.record(self._socket_path.parent, self._generation, authorized)
            self._readiness = authorized if written else _UNRECORDED

    def _pin_session(self, *, client=None, user_id=None) -> bool | None:
        """The account pin, at authorization and before every Telegram-sourced grant (spec 2).

        Returns None without a registry (the default alias), False for a session that is not authorized, and True
        for an admitted one; a refusal raises AccountBlocked.

        Without a registry it reads nothing but the registry's absence: 0.54. With one, the authorized
        session's user id is pinned for this alias at the first check (the default alias's in
        `default_user_id`); a different user later blocks with `account_mismatch`, and a user pinned under
        another alias, the default included, with `account_duplicate`. A session that is not authorized is
        left to the operation, which reports it blocked and grants nothing. The user id never leaves.
        """
        if not account_registry.present(self._accounts_root):
            self._account_session = None
            if self._alias != account_registry.DEFAULT_ALIAS:  # a named alias exists only in the registry
                self._artifact_authority.invalidate_account()
                raise AccountBlocked('alias_unavailable')
            return None
        if client is None:
            client = self._shared_client()
        epoch = getattr(client, 'send_observation_epoch', None)
        if user_id is None:
            remembered = self._account_session
            if remembered is not None and remembered[0] is client and epoch is not None and remembered[1] is epoch:
                user_id = remembered[2]
            else:
                getter = getattr(client, 'get_account_id', None)
                if getter is None:
                    self._artifact_authority.invalidate_account()
                    raise AccountBlocked('account_mismatch')
                try:
                    client.ensure_ready()
                except AuthorizationBlocked:
                    return False  # not authorized: the operation itself reports it blocked and grants nothing
                except TDLibError:
                    # Any other failure fails closed: the session's account could not be read, so nothing runs.
                    self._artifact_authority.invalidate_account()
                    raise AccountBlocked('account_unverified') from None
                user_id = getter()
        if type(user_id) is not int or not 0 < user_id < 2**53:
            self._artifact_authority.invalidate_account()
            raise AccountBlocked('account_mismatch')
        try:
            account_registry.pin_account(self._accounts_root, self._alias, user_id)
        except account_registry.RegistryError as error:
            self._account_session = None
            self._artifact_authority.invalidate_account()
            raise AccountBlocked(error.code) from None
        self._account_session = (client, epoch, user_id) if epoch is not None else None
        return True

    def _observed_owner(self, client_id) -> DraftOwner | None:
        """This client's DraftOwner under the account the broker last observed, or None; no provider read.

        Content-free routing only (the send budget): every grant re-reads the account.
        """
        account = self._artifact_authority.observed_account()
        try:
            return None if account is None else DraftOwner(client_id=client_id, account_id=account)
        except ValueError:
            return None

    def _attempted_send(self, operation, draft_id, client_id):
        """A consumed claim never reads as expired. After lost authority (release, account
        switch, publication) it reports its local failure or a content-free outcome_unknown;
        provider evidence is replayed only to a later call that still holds the original client
        lifecycle and account epoch. The call lost the owner that held the claim: its client decides."""
        video = operation == 'send_prepared_video'
        outcome = (self._drafts.attempt_outcome(draft_id, client_id=client_id, owner=None,
                   kinds=frozenset({'video'} if video else {'document', 'photo', 'voice_note'}))
                   if client_id is not None else None)
        if outcome is None:
            return None
        if video:
            if outcome == 'local_failed':
                return SendPreparedVideoResponse(status='local_failed', draft_id=draft_id,
                                                 detail='local revalidation failed; no provider send was attempted')
            return SendPreparedVideoResponse(status='outcome_unknown', draft_id=draft_id, detail=RECEIPT_AUTHORITY_UNAVAILABLE)
        if outcome == 'local_failed':
            return SendPreparedArtifactResponse(status='failed', draft_id=draft_id,
                                                detail='local preparation failed; no provider send was attempted')
        return SendPreparedArtifactResponse(status='outcome_unknown', draft_id=draft_id, detail=RECEIPT_AUTHORITY_UNAVAILABLE)

    def _artifact_unavailable(self, operation, payload, client_id=None):
        detail = 'artifact is unavailable or expired'
        if operation == 'read_attachment':
            return ReadAttachmentResponse(status='expired', detail=detail, coverage_complete=False).model_dump(mode='json')
        if operation == 'analyze_media':
            return AnalyzeMediaResponse(status='expired', detail=detail).model_dump(mode='json')
        if operation == 'get_attachment':
            from .schemas import AttachmentResponse
            return AttachmentResponse(status='error', anchor=payload['anchor'], coverage='none', detail='attachment transfer failed safely').model_dump(mode='json')
        if operation == 'read_attachment_page':
            from .attachment_page_models import terminal_attachment_page
            return terminal_attachment_page('invalid_cursor' if payload.get('cursor') else 'expired').model_dump(mode='json')
        if operation == 'read_spreadsheet':
            from .spreadsheet_models import terminal_spreadsheet
            return terminal_spreadsheet('invalid_cursor' if payload.get('cursor') else 'expired').model_dump(mode='json')
        if operation == 'read_presentation':
            from .presentation_models import terminal_presentation
            return terminal_presentation('expired').model_dump(mode='json')
        if operation == 'create_local_artifact':
            return CreateLocalArtifactResponse(status='error', detail='local artifact creation failed safely').model_dump(mode='json')
        if operation == 'begin_local_upload':
            return BeginLocalUploadResponse(status='error', detail='local upload could not be started safely').model_dump(mode='json')
        if operation == 'append_local_upload':
            return AppendLocalUploadResponse(status='error', upload_id=payload['upload_id'], detail='chunk was rejected; no bytes from this chunk were accepted').model_dump(mode='json')
        if operation == 'finish_local_upload':
            return FinishLocalUploadResponse(status='error', detail='local upload could not be completed safely').model_dump(mode='json')
        if operation == 'prepare_artifact_send':
            return PrepareArtifactSendResponse(status='error', detail='draft could not be prepared safely').model_dump(mode='json')
        if operation == 'prepare_reply_artifact_send':
            return PrepareReplyArtifactSendResponse(status='unavailable', detail=REPLY_UNAVAILABLE).model_dump(mode='json')
        terminal = {'get_draft': GetDraftResponse, 'list_drafts': ListDraftsResponse,
            'cancel_draft': CancelDraftResponse, 'update_draft': ReviseDraftResponse,
            'refresh_draft': ReviseDraftResponse, 'get_reply_artifact_draft': GetReplyArtifactDraftResponse,
            'update_reply_artifact_draft': ReviseReplyArtifactDraftResponse,
            'refresh_reply_artifact_draft': ReviseReplyArtifactDraftResponse}
        if operation in terminal:
            return terminal[operation](status='unavailable', detail=REPLY_UNAVAILABLE if 'reply_artifact' in operation else 'draft is unavailable').model_dump(mode='json')
        if operation == 'get_send_status':
            return send_status(payload['draft_id']).model_dump(mode='json')
        if operation in ('send_prepared_artifact', 'send_prepared_video'):
            attempted = self._attempted_send(operation, payload['draft_id'], client_id)
            if attempted is not None:
                return attempted.model_dump(mode='json')
        if operation == 'send_prepared_artifact':
            return SendPreparedArtifactResponse(status='expired', draft_id=payload['draft_id'], detail='draft is unavailable').model_dump(mode='json')
        if operation == 'prepare_video_send':
            return PrepareVideoSendResponse(status='unavailable', detail=VIDEO_UNAVAILABLE).model_dump(mode='json')
        if operation == 'send_prepared_video':
            return SendPreparedVideoResponse(status='expired', draft_id=payload['draft_id'], detail='draft is unavailable').model_dump(mode='json')
        raise ArtifactUnavailable('artifact is unavailable')

    def _dispatch(self, request: dict[str, Any], *, publish=None) -> dict[str, Any]:
        if request['operation'] in UPLOAD_MODELS:
            return self._dispatch_upload_authority(request, publish=publish)
        require_current(self._policy)
        if request.get('broker_generation') != self._generation:
            raise CompatibilityError('generation_stale')
        operation = request['operation']
        capability = OPERATION_CAPABILITIES.get(operation)
        if capability is not None and not set(required_capabilities(capability)) <= set(self._policy.enabled_capabilities):
            raise CompatibilityError('capability_disabled')
        remaining = request['deadline'] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline expired')
        if remaining > MAX_DEADLINE_SECONDS:
            raise BrokerProtocolError('request deadline exceeds the allowed bound')
        def finish(result):
            if publish is not None:
                publish(result)
            return result
        if operation not in ACCOUNT_CHECK_EXEMPT:
            self._account_check()  # Before any Telegram-sourced handle or provider action.
        if operation == 'release_client':
            # Revoke even clients that never allocated a provider/reader context.
            self._require_empty(request['payload'])
            self._selections.release(request['client_id'])
            self._upload_authorities.release(request['client_id'])
            return finish(self._dispatch_owned(request))
        if operation == 'retain_selection':
            return self._dispatch_selection(request, publish=publish)
        if request['payload'].get('selection_id') is not None or request['payload'].get('reply_selection_id') is not None:
            if operation not in selected_models():
                # A selection token on an operation without selection support is a malformed
                # closed request under every policy; it never reaches artifact or provider work.
                raise BrokerProtocolError('operation payload is not allowed')
            return self._dispatch_selection(request, publish=publish)
        owned_operations = {'get_attachment', 'read_attachment', 'analyze_media', 'read_attachment_page',
            'read_spreadsheet', 'read_presentation', 'create_local_artifact', 'begin_local_upload',
            'append_local_upload', 'finish_local_upload', 'prepare_artifact_send', 'prepare_reply_artifact_send',
            'get_reply_artifact_draft', 'update_reply_artifact_draft', 'refresh_reply_artifact_draft',
            'list_drafts', 'get_draft', 'update_draft', 'refresh_draft', 'cancel_draft',
            'send_prepared_artifact', 'get_send_status', 'prepare_video_send', 'send_prepared_video'}
        if operation not in owned_operations:
            return finish(self._dispatch_owned(request))
        with self._artifact_authority.scope(request['client_id'], self._artifact_store) as scope:
            try:
                # Central admission precedes metadata, provider, parser and draft work.
                # Preserve closed request validation even for unavailable IDs.
                admission_models = {'read_attachment': ReadAttachmentRequest, 'analyze_media': AnalyzeMediaRequest,
                    'prepare_artifact_send': PrepareArtifactSendRequest,
                    'prepare_reply_artifact_send': PrepareReplyArtifactSendRequest,
                    'prepare_video_send': PrepareVideoSendRequest}
                if operation == 'read_attachment_page':
                    from .attachment_page_models import ReadAttachmentPageRequest
                    admission_models[operation] = ReadAttachmentPageRequest
                elif operation == 'read_spreadsheet':
                    from .spreadsheet_models import ReadSpreadsheetRequest
                    admission_models[operation] = ReadSpreadsheetRequest
                elif operation == 'read_presentation':
                    from .presentation_models import ReadPresentationRequest
                    admission_models[operation] = ReadPresentationRequest
                model = admission_models.get(operation)
                if model is not None:
                    model.model_validate(request['payload'])
                artifact_id = request['payload'].get('artifact_id') if model is not None else None
                if artifact_id is not None and not self._artifact_authority.authorize(scope, artifact_id):
                    return finish(self._artifact_unavailable(operation, request['payload'], request['client_id']))
                if operation == 'get_attachment':
                    account = self._artifact_account()
                    if account is not None:
                        self._artifact_authority.observe(scope, account)
                result = self._dispatch_owned(request)
                if artifact_id is not None:
                    self._record_derived(result, artifact_id)
                with self._artifact_authority.publication(scope):
                    return finish(result)
            except AuthorizationBlocked:
                if operation == 'get_attachment':
                    from .schemas import AttachmentResponse
                    return finish(AttachmentResponse(status='blocked', anchor=request['payload']['anchor'], coverage='none', detail='TDLib authorization is not ready').model_dump(mode='json'))
                return finish(self._artifact_unavailable(operation, request['payload'], request['client_id']))
            except (ArtifactUnavailable, TDLibError):
                return finish(self._artifact_unavailable(operation, request['payload'], request['client_id']))

    def _owner_dialog(self, **facts) -> ApprovalDecision | bool:
        """The owner's approval dialog, one at a time across every send and apply. While another dialog is open
        this call shows none and answers dialog_unavailable at once: its draft stays pending, and nothing waits
        behind the open dialog, so a click meant for one draft can never approve another."""
        if not self._dialog_lock.acquire(blocking=False):
            return DIALOG_UNAVAILABLE
        try:
            return self._prompt_approval(**facts)  # counted as an open dialog for the installer's gate
        finally:
            self._dialog_lock.release()

    def _upload_admission(self):
        """Observe bounded policy outside locks, return a pure final fact check."""
        policy, generation, contract = self._policy, self._generation, self._contract
        require_current(policy)
        if ('artifacts' not in policy.enabled_capabilities or contract_descriptor(policy) != contract):
            raise UploadError('local upload is unavailable')
        def checked():
            if (self._policy is not policy or self._generation != generation or self._contract is not contract
                    or self._stop.is_set() or self._artifact_authority.generation != generation):
                raise UploadError('local upload is unavailable')
        return checked

    def _record_uploaded_metadata(self, completed):
        mime_type, media_kind = _local_artifact_classification(completed.file_name, completed.kind)
        with self._state_lock:
            self._artifact_provenance[completed.artifact.artifact_id] = FileProvenance('local')
            self._artifact_metadata[completed.artifact.artifact_id] = (
                None, completed.file_name, mime_type, media_kind)

    def _dispatch_upload_authority(self, request, *, publish=None):
        operation = request['operation']
        model = UPLOAD_MODELS[operation][0].model_validate(request['payload'])
        registry = self._upload_authorities
        try:
            if request.get('broker_generation') != self._generation:
                raise UploadError('local upload is unavailable')
            remaining = request['deadline'] - time.monotonic()
            if not 0 < remaining <= MAX_DEADLINE_SECONDS:
                raise UploadError('local upload is unavailable')
            with self._artifact_authority.scope(request['client_id'], self._artifact_store) as scope:
                if operation == 'issue_local_upload_authority':
                    return registry.issue(scope, model.manifest, publish)
                if operation == 'consume_local_upload_result':
                    return registry.consume(scope, model, publish, self._record_uploaded_metadata)
                if operation == 'cancel_local_upload_authority':
                    return registry.cancel(scope, model, publish)
                if operation == 'redeem_local_upload_authority':
                    return registry.redeem(scope, model, publish)
                if operation == 'append_authorized_local_upload':
                    return registry.append(scope, model, publish)
                return registry.finish(scope, model, publish)
        except (UploadError, ArtifactUnavailable, ArtifactStoreError, CompatibilityError):
            result = LocalUploadUnavailable().model_dump(mode='json')
            if publish is not None:
                publish(result)
            return result
        finally:
            registry.cleanup()

    def _dispatch_owned(self, request: dict[str, Any]) -> dict[str, Any]:
        require_current(self._policy)
        if request.get("broker_generation") != self._generation:
            raise CompatibilityError("generation_stale")
        operation = request["operation"]
        capability = OPERATION_CAPABILITIES.get(operation)
        if capability is not None and not set(required_capabilities(capability)) <= set(self._policy.enabled_capabilities):
            raise CompatibilityError("capability_disabled")
        self._expire_contexts()
        payload = request["payload"]
        client_id = request["client_id"]
        remaining = request["deadline"] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline expired")
        if remaining > MAX_DEADLINE_SECONDS:
            raise BrokerProtocolError("request deadline exceeds the allowed bound")
        if operation == "resolve":
            model = ResolveTargetRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.resolve(model).model_dump(mode="json")
        if operation == "discover":
            model = DiscoverTargetsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.discover(model).model_dump(mode="json")
        if operation == "search":
            model = SearchRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_attachment":
            model = AttachmentRequest.model_validate(payload)
            response = get_attachment(
                self._selection_message_client(), self._artifact_store, model,
                source_root=self._download_source_root,
                capture_source=lambda chat,raw:self._download_capture(active_scope(self._artifact_store),chat,raw),
                validate_source=lambda provenance:self._download_validate(active_scope(self._artifact_store),provenance),
                record_source=lambda provenance,artifact:self._download_record(active_scope(self._artifact_store),provenance,artifact),
            )
            if response.status == "complete" and response.artifact_id is not None:
                with self._state_lock:
                    self._artifact_metadata[response.artifact_id] = (
                        response.anchor, response.file_name, response.mime_type,
                        response.media_type or "document",
                    )
            return response.model_dump(mode="json")
        if operation == "read_attachment_page":
            from .attachment_page_models import ReadAttachmentPageRequest
            from .attachment_pages import AttachmentPageReader
            model = ReadAttachmentPageRequest.model_validate(payload)
            with self._service_lease(client_id, artifact_only=True):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.attachment_pages is None:
                        context.attachment_pages = AttachmentPageReader(
                            client=ArtifactReaderClient(self._artifact_store, self._shared_client), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.attachment_pages.close()
                    reader = context.attachment_pages
                return reader.read(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_spreadsheet":
            from .spreadsheet_models import ReadSpreadsheetRequest
            from .spreadsheets import SpreadsheetReader
            model = ReadSpreadsheetRequest.model_validate(payload)
            with self._service_lease(client_id, artifact_only=True):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.spreadsheets is None:
                        context.spreadsheets = SpreadsheetReader(
                            client=ArtifactReaderClient(self._artifact_store, self._shared_client), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.spreadsheets.close()
                    reader = context.spreadsheets
                return reader.read(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_presentation":
            from .presentation_models import ReadPresentationRequest
            from .presentations import PresentationReader
            policy_snapshot = self._policy
            model = ReadPresentationRequest.model_validate(payload)
            with self._service_lease(client_id, artifact_only=True):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.presentations is None:
                        context.presentations = PresentationReader(
                            client=ArtifactReaderClient(self._artifact_store, self._shared_client), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.presentations.close()
                    reader = context.presentations
                response = reader.read(model, deadline=request["deadline"])
                require_current(policy_snapshot)
                require_current(self._policy)
                if self._policy != policy_snapshot or 'presentations' not in self._policy.enabled_capabilities:
                    raise CompatibilityError("config_stale")
                if request["broker_generation"] != self._generation:
                    raise CompatibilityError("generation_stale")
                return response.model_dump(mode="json")
        if operation == "read_attachment":
            model = ReadAttachmentRequest.model_validate(payload)
            return self._read_attachment(model).model_dump(mode="json")
        if operation == "analyze_media":
            model = AnalyzeMediaRequest.model_validate(payload)
            return self._analyze_media(model).model_dump(mode="json")
        if operation == "create_local_artifact":
            model = CreateLocalArtifactRequest.model_validate(payload)
            return self._create_local_artifact(model).model_dump(mode="json")
        if operation == "begin_local_upload":
            return self._begin_local_upload(BeginLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "append_local_upload":
            return self._append_local_upload(AppendLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "finish_local_upload":
            return self._finish_local_upload(FinishLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "prepare_artifact_send":
            model = PrepareArtifactSendRequest.model_validate(payload)
            return self._prepare_artifact_send(model, client_id=client_id).model_dump(mode="json")
        if operation in {"prepare_reply_artifact_send","get_reply_artifact_draft","update_reply_artifact_draft","refresh_reply_artifact_draft"}:
            models={"prepare_reply_artifact_send":PrepareReplyArtifactSendRequest,
                "get_reply_artifact_draft":GetReplyArtifactDraftRequest,
                "update_reply_artifact_draft":UpdateReplyArtifactDraftRequest,
                "refresh_reply_artifact_draft":RefreshReplyArtifactDraftRequest}
            model=models[operation].model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+330)):
                return getattr(self,"_"+operation)(model,client_id=client_id).model_dump(mode="json")
        if operation == "prepare_reply_text_send":
            model=PrepareReplyTextSendRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._prepare_reply_text_send(model,client_id=client_id).model_dump(mode="json")
        if operation == "get_reply_draft":
            model=GetReplyDraftRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._get_reply_draft(model,client_id=client_id).model_dump(mode="json")
        if operation in {"update_reply_draft","refresh_reply_draft"}:
            model=(UpdateReplyDraftRequest if operation=="update_reply_draft" else RefreshReplyDraftRequest).model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._revise_reply_draft(model,client_id=client_id).model_dump(mode="json")
        if operation == "prepare_forward_send":
            model = PrepareForwardSendRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"], time.monotonic() + 30)):
                return self._prepare_forward_send(model, client_id=client_id).model_dump(mode="json")
        if operation == "send_prepared_forward":
            model = SendPreparedForwardRequest.model_validate(payload)
            return self._send_prepared_forward(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "prepare_video_send":
            model = PrepareVideoSendRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"], time.monotonic() + 30)):
                return self._prepare_video_send(model, client_id=client_id).model_dump(mode="json")
        if operation == "send_prepared_video":
            model = SendPreparedVideoRequest.model_validate(payload)
            return self._send_prepared_video(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "prepare_chat_archive":
            model = PrepareChatArchiveRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"], time.monotonic() + 30)):
                return self._prepare_chat_archive(model, client_id=client_id).model_dump(mode="json")
        if operation == "apply_prepared_archive":
            model = ApplyPreparedArchiveRequest.model_validate(payload)
            return self._apply_prepared_archive(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "prepare_text_edit":
            model = PrepareTextEditRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"], time.monotonic() + 30)):
                return self._prepare_text_edit(model, client_id=client_id).model_dump(mode="json")
        if operation == "apply_prepared_text_edit":
            model = ApplyPreparedTextEditRequest.model_validate(payload)
            return self._apply_prepared_text_edit(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "prepare_text_send":
            model = PrepareTextSendRequest.model_validate(payload)
            return self._prepare_text_send(model, client_id=client_id, deadline=request['deadline']).model_dump(mode="json")
        if operation == "send_prepared_artifact":
            model = SendPreparedArtifactRequest.model_validate(payload)
            return self._send_prepared_artifact(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "send_prepared_text":
            model = SendPreparedTextRequest.model_validate(payload)
            return self._send_prepared_text(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_drafts":
            return self._list_drafts(ListDraftsRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "get_send_status":
            return self._get_send_status(GetSendStatusRequest.model_validate(payload), client_id=client_id,
                                         deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_draft":
            return self._get_draft(GetDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "cancel_draft":
            return self._cancel_draft(CancelDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "update_draft":
            return self._revise_draft(UpdateDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "refresh_draft":
            return self._revise_draft(RefreshDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "read_reply_chain":
            model = ReadReplyChainRequest.model_validate(payload)
            return read_reply_chain(self._selection_message_client(), model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "verify_target":
            model = VerifyTargetRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.verify_target(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_target_messages":
            model = ReadTargetMessagesRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_target_messages(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_messages":
            model = ReadMessagesRequest.model_validate(payload)
            return read_messages(self._selection_message_client(), model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "search_chats":
            model = SearchChatsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search_chats(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "search_messages":
            model = SearchMessagesRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search_messages(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_topic_history":
            model = ReadTopicHistoryRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_topic_history(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_chats":
            model = ListChatsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.list_chats(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_topics":
            model = ListTopicsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.list_topics(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_history":
            model = ReadHistoryRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_history(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_message_context":
            model = MessageContextRequest.model_validate(payload)
            return get_message_context(self._selection_message_client(), model).model_dump(mode="json")
        if operation == "check":
            self._require_empty(payload)
            try:
                self._shared_client().ensure_ready()
                self._account_check()  # At authorization: the first authorized check pins the account.
            except AccountBlocked:
                return {"status": "blocked"}  # Recorded by the account check.
            except AuthorizationBlocked:
                self._record_readiness(False)
                return {"status": "blocked"}
            except TDLibError:
                self._record_readiness(None)
                return {"status": "error"}
            return {"status": "ready"}
        if operation == "health":
            self._require_empty(payload)
            return self._health()
        if operation == "release_client":
            self._require_empty(payload)
            with self._state_lock:
                context = self._contexts.get(client_id)
                if context is not None and context.active_requests > 0:
                    context.release_requested = True
                    context.invalidate_active_handles()
                else:
                    context = self._contexts.pop(client_id, None)
                    if context is not None:
                        context.close()
            return {"released": True}
        raise BrokerProtocolError("operation is not allowed")

    def _attachment_metadata(self, artifact_id: str):
        scope = active_scope(self._artifact_store)
        if scope is not None and not scope.authority.authorize(scope, artifact_id):
            return None
        with self._state_lock:
            return self._artifact_metadata.get(artifact_id)

    def _read_attachment(self, request: ReadAttachmentRequest) -> ReadAttachmentResponse:
        metadata = self._attachment_metadata(request.artifact_id)
        if metadata is None:
            return ReadAttachmentResponse(
                status="expired", detail="artifact metadata is unavailable", coverage_complete=False,
            )
        anchor, name, mime_type, media_type = metadata
        if media_type in {"audio", "voice_note", "video", "video_note"}:
            return ReadAttachmentResponse(
                status="unsupported", source_anchor=anchor,
                detail="use analyze_media for audio or video", coverage_complete=False,
            )
        try:
            artifact = self._artifact_store.lookup(request.artifact_id)
            if artifact is None:
                with self._state_lock:
                    self._artifact_metadata.pop(request.artifact_id, None)
                return ReadAttachmentResponse(
                    status="expired", source_anchor=anchor,
                    detail="artifact has expired or changed", coverage_complete=False,
                )
            result = read_document(
                artifact.path, name=name, mime_type=mime_type,
                max_chars=request.max_chars, max_pages=request.max_pages,
            )
            images: list[AttachmentImage] = []
            temporary_pages = [path for path in result.image_paths if path != artifact.path]
            try:
                temporary_bytes = sum(path.stat().st_size for path in temporary_pages)
                if temporary_bytes > 10 * 1024 * 1024:
                    raise ArtifactStoreError("generated page cleanup exceeds the safe bound")
                for index, image_path in enumerate(result.image_paths):
                    if image_path == artifact.path:
                        image_artifact = artifact
                        image_mime = mime_type if mime_type in {"image/jpeg", "image/png", "image/webp"} else "image/png"
                    else:
                        image_artifact = self._artifact_store.store(image_path, kind="image")
                        image_mime = "image/png"
                    images.append(AttachmentImage(
                        artifact_id=image_artifact.artifact_id,
                        artifact_path=str(image_artifact.path),
                        mime_type=image_mime,
                        page_number=index + 1 if result.total_pages is not None else None,
                    ))
            finally:
                for image_path in temporary_pages:
                    image_path.unlink(missing_ok=True)
                if temporary_pages:
                    temporary_pages[0].parent.rmdir()
            return ReadAttachmentResponse(
                status=result.status, source_anchor=anchor, text=result.text,
                processed_bytes=result.processed_bytes, total_bytes=result.total_bytes,
                processed_pages=result.processed_pages, total_pages=result.total_pages,
                images=images, coverage_complete=result.status == "complete",
                detail=result.error or "bounded local document read",
            )
        except (ArtifactStoreError, OSError, ValueError):
            return ReadAttachmentResponse(
                status="error", source_anchor=anchor, detail="artifact read failed safely",
                coverage_complete=False,
            )

    def _analyze_media(self, request: AnalyzeMediaRequest) -> AnalyzeMediaResponse:
        metadata = self._attachment_metadata(request.artifact_id)
        if metadata is None:
            return AnalyzeMediaResponse(status="expired", detail="artifact metadata is unavailable")
        anchor, _, _, kind = metadata
        if kind not in {"audio", "voice_note", "video", "video_note"}:
            return AnalyzeMediaResponse(status="unsupported", source_anchor=anchor, detail="artifact is not audio or video")
        try:
            artifact = self._artifact_store.lookup(request.artifact_id)
            if artifact is None:
                return AnalyzeMediaResponse(status="expired", source_anchor=anchor, detail="artifact has expired")
            result = analyze_media(artifact.path, kind=kind, start_seconds=request.start_seconds)
            try:
                frames: list[MediaFrameResult] = []
                for item in result.frames:
                    stored = self._artifact_store.store(item.path, kind="image")
                    frames.append(MediaFrameResult(
                        time_seconds=item.time_seconds, source=item.source,
                        artifact_id=stored.artifact_id, artifact_path=str(stored.path),
                    ))
                return AnalyzeMediaResponse(
                    status=result.status, source_anchor=anchor,
                    duration_seconds=result.duration_seconds,
                    window_start_seconds=result.window_start_seconds,
                    window_end_seconds=result.window_end_seconds,
                    transcription_status=result.transcription_status,
                    transcribed_start_seconds=result.transcribed_start_seconds,
                    transcribed_end_seconds=result.transcribed_end_seconds,
                    language=result.language,
                    segments=[MediaSegment(**vars(item)) for item in result.segments],
                    frame_status=result.frame_status, frames=frames,
                    coverage_complete=(result.status == "complete"
                                       and result.transcription_status in {"complete", "no_audio"}
                                       and result.frame_status in {"complete", "not_applicable"}
                                       and not any(item.uncertain for item in result.segments)),
                    detail=result.detail or "bounded local media analysis",
                )
            finally:
                result.cleanup()
        except (ArtifactStoreError, OSError, ValueError):
            return AnalyzeMediaResponse(status="error", source_anchor=anchor, detail="media analysis failed safely")

    def _create_local_artifact(self, request: CreateLocalArtifactRequest) -> CreateLocalArtifactResponse:
        suffix = Path(request.file_name).suffix.casefold()
        if suffix not in _LOCAL_MIME_TYPES:
            return CreateLocalArtifactResponse(status="error", detail="file format is unsupported")
        import tempfile
        if request.content is not None:
            if suffix not in {".txt", ".text", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql", ".log", ".toml"}:
                return CreateLocalArtifactResponse(status="error", detail="binary format requires base64 content")
            data = request.content.encode("utf-8")
        else:
            try:
                data = base64.b64decode(request.content_base64 or "", validate=True)
            except binascii.Error:
                return CreateLocalArtifactResponse(status="error", detail="base64 content is invalid")
        if len(data) > 512 * 1024:
            return CreateLocalArtifactResponse(status="error", detail="content exceeds creation limit")
        try:
            with tempfile.TemporaryDirectory(prefix="telegram-artifact-") as directory:
                path = Path(directory) / request.file_name
                path.write_bytes(data)
                mime_type, media_kind = _local_artifact_classification(request.file_name)
                artifact = self._artifact_store.store(path, kind="media" if media_kind != "document" else "document")
            with self._state_lock:
                self._artifact_provenance[artifact.artifact_id] = FileProvenance('local')
                self._artifact_metadata[artifact.artifact_id] = (
                    None, request.file_name, mime_type, media_kind,
                )
            return CreateLocalArtifactResponse(
                status="complete", artifact_id=artifact.artifact_id,
                artifact_path=str(artifact.path), sha256=artifact.sha256,
                size_bytes=artifact.size_bytes, file_name=request.file_name,
                detail="local artifact created in private cache",
            )
        except (ArtifactStoreError, OSError):
            return CreateLocalArtifactResponse(status="error", detail="local artifact creation failed safely")

    def _begin_local_upload(self, request: BeginLocalUploadRequest) -> BeginLocalUploadResponse:
        try:
            upload_id = self._uploads.begin(file_name=request.file_name, kind=request.kind,
                                            size_bytes=request.size_bytes, sha256=request.sha256)
            return BeginLocalUploadResponse(status="ready", upload_id=upload_id,
                                            expires_at=datetime.fromtimestamp(time.time() + 900, tz=timezone.utc),
                                            detail="ordered local upload started")
        except (UploadError, OSError, ValueError):
            return BeginLocalUploadResponse(status="error", detail="local upload could not be started safely")

    def _append_local_upload(self, request: AppendLocalUploadRequest) -> AppendLocalUploadResponse:
        try:
            next_index, received = self._uploads.append(
                request.upload_id, index=request.index,
                content_base64=request.content_base64, sha256=request.sha256,
            )
            return AppendLocalUploadResponse(status="accepted", upload_id=request.upload_id,
                                             next_index=next_index, received_bytes=received,
                                             detail="chunk accepted")
        except (UploadError, OSError, ValueError):
            return AppendLocalUploadResponse(status="error", upload_id=request.upload_id,
                                             detail="chunk was rejected; no bytes from this chunk were accepted")

    def _finish_local_upload(self, request: FinishLocalUploadRequest) -> FinishLocalUploadResponse:
        try:
            completed = self._uploads.finish_with_metadata(request.upload_id)
            artifact = completed.artifact
            mime_type, media_kind = _local_artifact_classification(completed.file_name, completed.kind)
            with self._state_lock:
                self._artifact_provenance[artifact.artifact_id] = FileProvenance('local')
                self._artifact_metadata[artifact.artifact_id] = (
                    None, completed.file_name, mime_type, media_kind,
                )
            return FinishLocalUploadResponse(status="complete", artifact_id=artifact.artifact_id,
                                             artifact_path=str(artifact.path), sha256=artifact.sha256,
                                             size_bytes=artifact.size_bytes, file_name=completed.file_name,
                                             detail="complete local upload stored in private artifact cache")
        except (UploadError, ArtifactStoreError, OSError, ValueError):
            return FinishLocalUploadResponse(status="error", detail="local upload could not be completed safely")

    def _draft_owner(self, client_id: str) -> DraftOwner:
        require_current(self._policy)
        if "send" not in self._policy.enabled_capabilities:
            raise CompatibilityError("capability_disabled")
        try:
            account_id = self._artifact_account()
        except ArtifactUnavailable:
            raise DraftError("account is unavailable") from None
        if account_id is None:
            raise DraftError("account is unavailable")
        owner = DraftOwner(client_id=client_id, account_id=account_id)
        scope = active_scope(self._artifact_store)
        if scope is not None:
            self._artifact_authority.observe(scope, account_id)
        else:
            self._artifact_authority.observe_account(account_id)
        return owner

    @staticmethod
    def _draft_summary(draft, owner: DraftOwner) -> DraftSummary:
        return DraftSummary(
            draft_id=draft.draft_id, account_id=owner.account_id, recipient=draft.recipient,
            recipient_title=draft.recipient_title, kind=draft.kind,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
            approval_required=draft.approval_required,
            topic=draft.topic,
        )

    def _require_topic_policy(self, draft_id: str, owner: DraftOwner, *, stored_source: bool = True) -> None:
        kind = self._drafts.kind(draft_id, owner=owner)
        if kind == 'forward':
            self._require_forward_policy()  # Status and receipts stay bound to forward_send.
        elif kind == 'video':
            self._require_video_policy()  # Status and receipts stay bound to video_send.
        elif kind == 'archive':
            self._require_change_policy('archive_chats')  # Status and receipts stay bound to archive_chats.
        elif kind == 'text_edit':
            self._require_change_policy('edit_own_text')  # Status and receipts stay bound to edit_own_text.
        if not (self._drafts.is_topic(draft_id, owner=owner) or self._drafts.is_formatted(draft_id, owner=owner)):
            return
        self._require_capabilities('send')
        provider = self._shared_client()
        self._require_capabilities(*self._drafts.source_required_capabilities(draft_id, owner=owner,
            provider=provider, provider_epoch=provider.send_observation_epoch, stored_source=stored_source))

    def _require_capabilities(self, *required: str) -> None:
        """Refuse with only the missing names, in requirement order; reply tools return them typed."""
        require_current(self._policy)
        missing = missing_capabilities(self._policy, *required)
        if missing:
            raise CapabilityDisabled(missing)

    def _require_formatting_capability(self):
        self._require_capabilities('send', 'formatted_text_send')

    @staticmethod
    def _reply_options(*, formatted: bool, topic: bool) -> tuple[str, ...]:
        """A text reply's own options in the documented order: outgoing styles, then a selected topic."""
        return (*(('formatted_text_send',) if formatted else ()), *(('topic_text_send',) if topic else ()))

    def _require_reply_draft_options(self, draft) -> None:
        """A stored text reply: the tool, send, then the draft's own options, before any source family."""
        self._require_capabilities('reply_text_send', 'send', *self._reply_options(
            formatted=bool(draft.outgoing_entities), topic=draft.topic is not None))

    @staticmethod
    def _draft_entity_rows(draft):
        return [{'offset': offset, 'length': length, 'type': {'@type': kind}}
                for offset, length, kind in draft.outgoing_entities]

    def _text_revision_guard(self, old, provider, epoch, *, new_formatted=False, source=None):
        if not (old.topic is not None or old.outgoing_entities or new_formatted):
            return None
        required = {'send'}
        if old.topic is not None:
            required.add('topic_text_send')
        if old.outgoing_entities or new_formatted:
            required.add('formatted_text_send')
        # The source the revision installs: a refresh replaces the stored one, which is never shown again.
        installed = old.reply_source if source is None else source
        if installed is not None:
            required.update(('reply_text_send', *installed.required_capabilities))
        def guard():
            # Called under registry lock: captured immutable/local facts only.
            require_current(self._policy)
            if (self._client is not provider or provider.send_observation_epoch is not epoch or
                    not required <= set(self._policy.enabled_capabilities) or
                    (old.topic is not None and (old.topic_provider is not provider or old.topic_provider_epoch is not epoch)) or
                    (old.outgoing_entities and (old.formatting_provider is not provider or old.formatting_provider_epoch is not epoch))):
                raise DraftError('text draft authority changed')
            self._reply_budget_check(provider)
        return guard

    @staticmethod
    def _topic_digest(draft, owner: DraftOwner) -> str:
        from .topic_models import topic_preview_digest
        return topic_preview_digest(draft_id=draft.draft_id, account_id=owner.account_id,
            recipient=draft.recipient, recipient_title=draft.recipient_title,
            expires_at=datetime.fromtimestamp(draft.expires_at, timezone.utc), text=draft.text,
            sha256=draft.sha256, size_bytes=draft.size_bytes, topic=draft.topic,
            outgoing_entities=Broker._draft_entity_rows(draft))

    def _list_drafts(self, request: ListDraftsRequest, *, client_id: str) -> ListDraftsResponse:
        try:
            owner = self._draft_owner(client_id)
            pending, has_more = self._drafts.list_pending(owner=owner, limit=request.limit,
                                                        after_draft_id=request.after_draft_id)
            for draft in pending:
                self._require_topic_policy(draft.draft_id, owner)
            summaries = [self._draft_summary(draft, owner) for draft in pending]
            return ListDraftsResponse(status="listed", drafts=summaries, has_more=has_more,
                                      next_after_draft_id=pending[-1].draft_id if has_more else None,
                                      detail="owned pending drafts; live view, no recipient verification or approval")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return ListDraftsResponse(status="unavailable", detail="drafts are unavailable")

    def _draft_preview(self, draft, owner: DraftOwner) -> DraftPreview:
        if draft.reply_source is not None or draft.kind in ("forward", "video", "archive", "text_edit"):
            raise DraftError("draft is unavailable")
        self._require_topic_policy(draft.draft_id, owner)
        return self._plain_draft_preview(draft,owner)

    def _plain_draft_preview(self, draft, owner: DraftOwner) -> DraftPreview:
        content = dict(sha256=draft.sha256, size_bytes=draft.size_bytes)
        if draft.kind == "text":
            content["text"] = draft.text
            if draft.outgoing_entities:
                content['outgoing_entities'] = self._draft_entity_rows(draft)
            if draft.topic is not None or draft.outgoing_entities:
                content['preview_sha256'] = self._topic_digest(draft, owner)
        else:
            content.update(artifact_id=draft.artifact_id, display_name=draft.display_name,
                           mime_type=draft.mime_type, caption=draft.caption,
                           duration_seconds=draft.duration_seconds, waveform_base64=draft.waveform_base64,
                           source_sha256=draft.source_sha256, source_display_name=draft.source_display_name,
                           converted=draft.converted)
        return DraftPreview(**self._draft_summary(draft, owner).model_dump(), **content)

    def _get_send_status(self, request: GetSendStatusRequest, *, client_id: str,
                         deadline: float | None = None) -> GetSendStatusResponse:
        budget = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
        try:
            require_current(self._policy)
            if "send" not in self._policy.enabled_capabilities:
                raise CompatibilityError("capability_disabled")
            client = self._shared_client()
            with client.request_budget(budget):
                owner = self._draft_owner(client_id)
                self._require_topic_policy(request.draft_id, owner)
                state, receipt, provider, attempted_at, provider_epoch = self._drafts.status_snapshot(request.draft_id, owner=owner)
                result = send_status(request.draft_id)
                if state == "pending":
                    result = send_status(request.draft_id, "local_pending")
                elif provider is client and provider_epoch is client.send_observation_epoch:
                    observation = client.get_send_observation(request.draft_id)
                    if receipt is not None and receipt.evidence == "local_failed":
                        result = send_status(request.draft_id, "local_failed")
                    elif observation is not None:
                        if observation.status in {"sent", "failed"}:
                            receipt = self._drafts.reconcile(request.draft_id, owner=owner, provider=client, provider_epoch=provider_epoch,
                                attempted_at=attempted_at, status=observation.status, message_id=observation.message_id)
                            # A conflicting terminal event never changes the immutable receipt.
                            if receipt.status == observation.status and receipt.message_id == observation.message_id:
                                result = send_status(request.draft_id, receipt.evidence, receipt.message_id)
                        elif observation.status == "pending" and receipt is not None and receipt.status == "outcome_unknown":
                            result = send_status(request.draft_id, "provider_pending", status_override="outcome_unknown")
                        elif state == "claimed":
                            result = send_status(request.draft_id,
                                "provider_pending" if observation.status == "pending" else "local_claimed")
                    elif state == "claimed":
                        result = send_status(request.draft_id, "local_claimed")
                if self._draft_owner(client_id) != owner or time.monotonic() >= budget:
                    return send_status(request.draft_id)
                # Recheck local expiry and provider retention after account/auth reads.
                current_state, current_receipt, current_provider, current_attempt, current_epoch = self._drafts.status_snapshot(
                    request.draft_id, owner=owner)
                if current_provider is not provider or current_attempt != attempted_at or current_epoch is not provider_epoch:
                    return send_status(request.draft_id)
                if result.status == "pending" and (current_state not in {"pending", "claimed"} or current_receipt is not None):
                    if (result.evidence == "provider_pending" and current_receipt is not None
                            and current_receipt.status == "outcome_unknown"):
                        result = send_status(request.draft_id, "provider_pending", status_override="outcome_unknown")
                    else:
                        return send_status(request.draft_id)
                if self._shared_client() is not client or (provider is client and provider_epoch is not client.send_observation_epoch):
                    return send_status(request.draft_id)
                if result.evidence in {"provider_confirmed", "provider_failed", "provider_pending"}:
                    current = client.get_send_observation(request.draft_id)
                    if current is None or current != observation:
                        return send_status(request.draft_id)
                require_current(self._policy)
                self._require_topic_policy(request.draft_id, owner)
                if "send" not in self._policy.enabled_capabilities or time.monotonic() >= budget:
                    return send_status(request.draft_id)
                return result
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
            return send_status(request.draft_id)

    def _get_draft(self, request: GetDraftRequest, *, client_id: str) -> GetDraftResponse:
        try:
            owner = self._draft_owner(client_id)
            draft = self._drafts.peek(request.draft_id, owner=owner)
            preview = self._draft_preview(draft, owner)
            return GetDraftResponse(status="pending", draft=preview,
                                    detail="exact pending draft; no recipient verification or approval")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return GetDraftResponse(status="unavailable", detail="draft is unavailable")

    def _revise_draft(self, request: UpdateDraftRequest | RefreshDraftRequest,
                      *, client_id: str) -> ReviseDraftResponse:
        try:
            from .outgoing_entity_models import caller_outgoing_entities
            if isinstance(request, UpdateDraftRequest) and request.outgoing_entities:
                self._require_formatting_capability()
            owner = self._draft_owner(client_id)
            old = self._drafts.peek(request.draft_id, owner=owner)
            self._draft_preview(old, owner)
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            if old.topic is not None and isinstance(request, RefreshDraftRequest):
                raise DraftError('topic refresh is unavailable; prepare a fresh topic draft')
            chat = provider.resolve_target(old.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(old.recipient)
            if (old.topic is not None or old.outgoing_entities) and title != old.recipient_title:
                raise DraftError('topic recipient changed; prepare a fresh topic draft')
            if self._draft_owner(client_id) != owner:
                raise DraftError("draft is unavailable")
            rows = (caller_outgoing_entities(request.outgoing_entities) if isinstance(request, UpdateDraftRequest)
                    else self._draft_entity_rows(old))
            if rows:
                self._require_formatting_capability()
            self._require_topic_policy(old.draft_id, owner)
            guard = self._text_revision_guard(old, provider, epoch, new_formatted=bool(rows))
            revised = self._drafts.revise(
                request.draft_id, owner=owner, recipient_title=title,
                text=request.text if isinstance(request, UpdateDraftRequest) else old.text if old.outgoing_entities else None,
                caption=request.caption if isinstance(request, UpdateDraftRequest) else None,
                outgoing_entities=rows if (isinstance(request, UpdateDraftRequest) and request.text is not None) or old.outgoing_entities else None,
                formatting_provider=provider if rows else None,
                formatting_provider_epoch=epoch if rows else None,
                local_admission_guard=guard,
            )
            known = self._drafts.known_ids()
            with self._state_lock:
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[revised.draft_id] = title
            return ReviseDraftResponse(
                status="revised", previous_draft_id=request.draft_id,
                draft=self._draft_preview(revised, owner),
                detail="new immutable revision; prior draft invalidated; fresh approval required",
            )
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return ReviseDraftResponse(status="unavailable", detail="draft is unavailable")

    def _cancel_draft(self, request: CancelDraftRequest, *, client_id: str) -> CancelDraftResponse:
        try:
            owner = self._draft_owner(client_id)
            self._require_topic_policy(request.draft_id, owner)
            self._drafts.cancel(request.draft_id, owner=owner)
            return CancelDraftResponse(status="cancelled", draft_id=request.draft_id,
                                       detail="pending draft cancelled; cached artifacts retained")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return CancelDraftResponse(status="unavailable", detail="draft is unavailable")

    def _prepare_artifact_send(self, request: PrepareArtifactSendRequest, *, client_id: str) -> PrepareArtifactSendResponse:
        try:
            owner = self._draft_owner(client_id)
            client = self._shared_client()
            chat = client.resolve_target(request.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(request.recipient)
            if request.kind == "photo":
                artifact = self._artifact_store.lookup(request.artifact_id)
                if artifact is None:
                    raise DraftError("artifact is unavailable")
                validate_photo(artifact.path, size_bytes=artifact.size_bytes,
                               display_name=request.display_name, mime_type=request.mime_type)
            artifact_id = request.artifact_id
            display_name = request.display_name
            mime_type = request.mime_type
            duration_seconds = None
            waveform_base64 = None
            source_sha256 = None
            source_display_name = None
            converted = False
            if request.kind == "voice_note":
                source = self._artifact_store.lookup(request.artifact_id)
                if source is None:
                    raise DraftError("artifact is unavailable")
                voice = prepare_voice_note(source, self._artifact_store,
                                           input_mime=request.mime_type,
                                           input_name=request.display_name)
                artifact_id = voice.artifact.artifact_id
                self._record_derived_file(artifact_id, request.artifact_id, Path(request.display_name).stem + ".ogg", "audio/ogg", "voice_note")
                display_name = Path(request.display_name).stem + ".ogg"
                mime_type = "audio/ogg"
                duration_seconds = voice.duration_seconds
                waveform_base64 = voice.waveform_base64
                source_sha256 = voice.source_sha256
                source_display_name = request.display_name
                converted = voice.converted
            draft = self._drafts.prepare(
                owner=owner, artifact_id=artifact_id, recipient=request.recipient, recipient_title=title,
                display_name=display_name, mime_type=mime_type,
                caption=request.caption, kind=request.kind,
                duration_seconds=duration_seconds, waveform_base64=waveform_base64,
                source_sha256=source_sha256, source_display_name=source_display_name,
                converted=converted,
            )
            known = self._drafts.known_ids()
            with self._state_lock:
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[draft.draft_id] = title
            return PrepareArtifactSendResponse(
                status="prepared", draft_id=draft.draft_id, recipient=draft.recipient,
                recipient_title=title, display_name=draft.display_name,
                mime_type=draft.mime_type, caption=draft.caption,
                kind=draft.kind, duration_seconds=draft.duration_seconds,
                waveform_base64=draft.waveform_base64,
                source_sha256=draft.source_sha256,
                source_display_name=draft.source_display_name,
                converted=draft.converted,
                sha256=draft.sha256, size_bytes=draft.size_bytes,
                expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
                detail="immutable local draft prepared; explicit approval is required before sending",
            )
        except AuthorizationBlocked:
            return PrepareArtifactSendResponse(status="blocked", detail="TDLib authorization is not ready")
        except (DraftError, TDLibError, VoiceError, ValueError, CompatibilityError):
            return PrepareArtifactSendResponse(status="error", detail="draft could not be prepared safely")

    def _reply_artifact_owner(self, client_id: str) -> DraftOwner:
        owner=self._draft_owner(client_id)
        if "reply_artifact_send" not in self._policy.enabled_capabilities:
            raise CapabilityDisabled(("reply_artifact_send",))
        return owner

    def _require_reply_source_policy(self, source):
        self._require_capabilities(*source.required_capabilities)

    @staticmethod
    def _reply_disabled(response, error: CapabilityDisabled):
        return response(status='capability_disabled', missing_capabilities=list(error.missing),
                        detail=REPLY_CAPABILITY_DISABLED)

    def _reply_artifact_preview(self,draft,owner):
        if draft.reply_source is None or draft.kind=='text':raise DraftError(REPLY_UNAVAILABLE)
        self._require_capabilities('reply_artifact_send','send')
        self._require_reply_source_policy(draft.reply_source)
        return ReplyArtifactDraftPreview.create(self._plain_draft_preview(draft,owner),draft.reply_source)

    @staticmethod
    def _reply_budget_check(provider):
        deadline=getattr(provider._request_context,"deadline",None)
        if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('reply request expired')

    def _prepare_reply_artifact_send(self,request: PrepareReplyArtifactSendRequest,*,client_id: str):
        from .outgoing_voice import prepare_reply_voice_note
        from .outgoing_drafts import _safe_display_name
        try:
            owner=self._reply_artifact_owner(client_id);provider=self._shared_client()
            request.caption.encode('utf-8');request.display_name.encode('utf-8')
            if request.reply_to.chat_id!=request.recipient:raise DraftError(REPLY_UNAVAILABLE)
            chat=provider.resolve_target(request.recipient)
            title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(request.recipient)
            source=self._selection_message_client().read_reply_source(request.reply_to.chat_id,request.reply_to.message_id)
            self._require_reply_source_policy(source)
            artifact=self._artifact_store.lookup(request.artifact_id)
            if artifact is None:raise DraftError(REPLY_UNAVAILABLE)
            kwargs=dict(artifact_id=request.artifact_id,display_name=request.display_name,mime_type=request.mime_type)
            if request.kind=='photo':
                validate_photo(artifact.path,size_bytes=artifact.size_bytes,display_name=request.display_name,mime_type=request.mime_type)
            elif request.kind=='voice_note':
                voice=prepare_reply_voice_note(artifact,self._artifact_store,input_mime=request.mime_type,
                    input_name=request.display_name,budget_check=lambda:self._reply_budget_check(provider))
                self._record_derived_file(voice.artifact.artifact_id,request.artifact_id,Path(request.display_name).stem+".ogg","audio/ogg","voice_note")
                kwargs.update(artifact_id=voice.artifact.artifact_id,display_name=_safe_display_name(Path(_safe_display_name(request.display_name)).stem+'.ogg'),mime_type='audio/ogg',
                    duration_seconds=voice.duration_seconds,waveform_base64=voice.waveform_base64,
                    source_sha256=voice.source_sha256,source_display_name=_safe_display_name(request.display_name),converted=voice.converted)
            self._reply_budget_check(provider)
            if self._reply_artifact_owner(client_id)!=owner or self._shared_client() is not provider:raise DraftError(REPLY_UNAVAILABLE)
            self._require_reply_source_policy(source)
            draft=self._drafts.prepare(owner=owner,recipient=request.recipient,recipient_title=title,
                caption=request.caption,kind=request.kind,reply_source=source,
                local_admission_guard=lambda:self._reply_budget_check(provider),**kwargs)
            preview=self._reply_artifact_preview(draft,owner);self._remember_draft_title(draft)
            return PrepareReplyArtifactSendResponse(status='prepared',reply=preview,detail=REPLY_PREPARED)
        except CapabilityDisabled as error:
            return self._reply_disabled(PrepareReplyArtifactSendResponse, error)
        except (DraftError,TDLibError,VoiceError,ValueError,CompatibilityError,AttributeError,TimeoutError,OSError):
            return PrepareReplyArtifactSendResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _get_reply_artifact_draft(self,request: GetReplyArtifactDraftRequest,*,client_id: str):
        try:
            owner=self._reply_artifact_owner(client_id)
            draft=self._drafts.peek(request.draft_id,owner=owner)
            self._reply_budget_check(self._shared_client())
            return GetReplyArtifactDraftResponse(status='pending',reply=self._reply_artifact_preview(draft,owner),detail=REPLY_PENDING)
        except CapabilityDisabled as error:
            return self._reply_disabled(GetReplyArtifactDraftResponse, error)
        except (DraftError,TDLibError,ValueError,CompatibilityError,TimeoutError):
            return GetReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _revise_reply_artifact_draft(self,request,*,client_id: str):
        try:
            owner=self._reply_artifact_owner(client_id);old=self._drafts.peek(request.draft_id,owner=owner)
            if isinstance(request,RefreshReplyArtifactDraftRequest):
                # A refresh names what the newly read source needs; the stored source it replaces is not asked.
                if old.reply_source is None or old.kind=='text':raise DraftError(REPLY_UNAVAILABLE)
                self._require_capabilities('reply_artifact_send','send')
            else:self._reply_artifact_preview(old,owner)
            provider=self._shared_client()
            if isinstance(request,RefreshReplyArtifactDraftRequest):
                chat=provider.resolve_target(old.recipient)
                title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(old.recipient)
                source=provider.read_reply_source(*old.reply_source.anchor)
            else:title=old.recipient_title;source=old.reply_source
            self._reply_budget_check(provider)
            if self._reply_artifact_owner(client_id)!=owner or self._shared_client() is not provider:raise DraftError(REPLY_UNAVAILABLE)
            self._require_reply_source_policy(source)
            revised=self._drafts.revise(request.draft_id,owner=owner,recipient_title=title,reply=True,
                reply_source=source,caption=request.caption if isinstance(request,UpdateReplyArtifactDraftRequest) else None,
                local_admission_guard=lambda:self._reply_budget_check(provider))
            self._remember_draft_title(revised)
            return ReviseReplyArtifactDraftResponse(status='revised',previous_draft_id=request.draft_id,
                reply=self._reply_artifact_preview(revised,owner),detail=REPLY_REVISED)
        except CapabilityDisabled as error:
            return self._reply_disabled(ReviseReplyArtifactDraftResponse, error)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return ReviseReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _update_reply_artifact_draft(self,request,*,client_id: str):
        return self._revise_reply_artifact_draft(request,client_id=client_id)

    def _refresh_reply_artifact_draft(self,request,*,client_id: str):
        return self._revise_reply_artifact_draft(request,client_id=client_id)

    def _reply_owner(self, client_id: str) -> DraftOwner:
        owner=self._draft_owner(client_id)
        if "reply_text_send" not in self._policy.enabled_capabilities:
            raise CapabilityDisabled(("reply_text_send",))
        return owner

    def _reply_preview(self, draft, owner: DraftOwner) -> ReplyDraftPreview:
        if draft.reply_source is None or draft.kind != "text":
            raise DraftError("reply draft is unavailable")
        # The documented order: the tool, send, the draft's options, then the source's families.
        self._require_reply_draft_options(draft)
        self._require_reply_source_policy(draft.reply_source)
        self._require_topic_policy(draft.draft_id, owner)
        content = ReplyTextDraftPreview(**self._draft_summary(draft, owner).model_dump(),
            sha256=draft.sha256, size_bytes=draft.size_bytes, text=draft.text,
            outgoing_entities=self._draft_entity_rows(draft))
        return ReplyDraftPreview.create(content, draft.reply_source)

    def _remember_draft_title(self, draft):
        known=self._drafts.known_ids()
        with self._state_lock:
            for old_id in list(self._draft_recipient_titles):
                if old_id not in known:self._draft_recipient_titles.pop(old_id,None)
            self._draft_recipient_titles[draft.draft_id]=draft.recipient_title

    def _prepare_reply_text_send(self, request: PrepareReplyTextSendRequest, *, client_id: str) -> PrepareReplyTextSendResponse:
        try:
            from .outgoing_entity_models import caller_outgoing_entities
            # The request's own options are checked together, before Telegram is read.
            options = self._reply_options(formatted=bool(request.outgoing_entities), topic=request.topic is not None)
            if options:
                self._require_capabilities('reply_text_send', 'send', *options)
            owner=self._reply_owner(client_id)
            if request.reply_to.chat_id!=request.recipient:
                raise DraftError("reply draft is unavailable")
            provider=self._shared_client()
            epoch = provider.send_observation_epoch if request.topic is not None or request.outgoing_entities else None
            chat=provider.resolve_target(request.recipient)
            title=sanitize_telegram_text(chat.get("title"),max_length=255) or str(request.recipient)
            topic = None
            if request.topic is not None:
                topic = provider.read_topic_snapshot(request.recipient, request.topic.id,
                    expected_account_id=owner.account_id, expected_recipient_title=title)
            source=self._selection_message_client().read_reply_source(request.reply_to.chat_id,request.reply_to.message_id,
                **({'expected_forum_topic_id': topic.forum_topic_id} if topic is not None else {}))
            self._require_reply_source_policy(source)
            if (self._reply_owner(client_id)!=owner or self._shared_client() is not provider or
                    ((topic is not None or request.outgoing_entities) and provider.send_observation_epoch is not epoch)):
                raise DraftError("reply draft is unavailable")
            self._require_reply_source_policy(source)
            if topic is not None:
                self._require_capabilities('reply_text_send', 'send', 'topic_text_send')
            if request.outgoing_entities:
                self._require_formatting_capability()
                self._reply_budget_check(provider)
            draft=self._drafts.prepare_text(owner=owner,recipient=request.recipient,text=request.text,
                recipient_title=title,reply_source=source,
                outgoing_entities=caller_outgoing_entities(request.outgoing_entities),
                formatting_provider=provider if request.outgoing_entities else None,
                formatting_provider_epoch=epoch if request.outgoing_entities else None,
                **({'topic': topic, 'topic_provider': provider, 'topic_provider_epoch': epoch}
                   if topic is not None else {}))
            preview=self._reply_preview(draft,owner)
            self._remember_draft_title(draft)
            return PrepareReplyTextSendResponse(status="prepared",reply=preview,detail=REPLY_PREPARED)
        except CapabilityDisabled as error:
            return self._reply_disabled(PrepareReplyTextSendResponse, error)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return PrepareReplyTextSendResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _get_reply_draft(self, request: GetReplyDraftRequest, *, client_id: str) -> GetReplyDraftResponse:
        try:
            owner=self._reply_owner(client_id)
            draft=self._drafts.peek(request.draft_id,owner=owner)
            return GetReplyDraftResponse(status="pending",reply=self._reply_preview(draft,owner),detail=REPLY_PENDING)
        except CapabilityDisabled as error:
            return self._reply_disabled(GetReplyDraftResponse, error)
        except (DraftError,TDLibError,ValueError,CompatibilityError):
            return GetReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _revise_reply_draft(self, request: UpdateReplyDraftRequest | RefreshReplyDraftRequest, *, client_id: str) -> ReviseReplyDraftResponse:
        try:
            from .outgoing_entity_models import caller_outgoing_entities
            refresh = isinstance(request, RefreshReplyDraftRequest)
            if not refresh and request.outgoing_entities:
                # The request's own option, before the draft is looked up: the same for any draft ID.
                self._require_capabilities('reply_text_send', 'send', 'formatted_text_send')
            owner=self._reply_owner(client_id)
            old=self._drafts.peek(request.draft_id,owner=owner)
            if refresh:
                # A refresh names what the newly read source needs: the draft's options now, the new
                # source's families once it is read; the stored source it replaces is never asked.
                # A topic reply has no refresh, so it is refused before any option is named.
                if old.reply_source is None or old.kind != "text" or old.topic is not None:
                    raise DraftError("reply draft is unavailable")
                self._require_reply_draft_options(old)
                self._require_topic_policy(old.draft_id, owner, stored_source=False)
            else:
                self._reply_preview(old,owner)
            provider=self._shared_client()
            epoch = provider.send_observation_epoch
            if refresh:
                chat=provider.resolve_target(old.recipient)
                title=sanitize_telegram_text(chat.get("title"),max_length=255) or str(old.recipient)
                if old.outgoing_entities and title != old.recipient_title:
                    raise DraftError('formatted reply recipient changed')
                source=provider.read_reply_source(*old.reply_source.anchor)
            else:
                title=old.recipient_title
                source=old.reply_source
            if self._reply_owner(client_id)!=owner or self._shared_client() is not provider:
                raise DraftError("reply draft is unavailable")
            self._require_reply_source_policy(source)
            rows = (caller_outgoing_entities(request.outgoing_entities) if isinstance(request, UpdateReplyDraftRequest)
                    else self._draft_entity_rows(old))
            if rows:
                self._require_formatting_capability()
            self._require_topic_policy(old.draft_id, owner, stored_source=not refresh)
            local_admission_guard = self._text_revision_guard(old, provider, epoch,
                new_formatted=bool(rows), source=source)
            revised=self._drafts.revise(request.draft_id,owner=owner,recipient_title=title,reply=True,
                reply_source=source,text=request.text if isinstance(request,UpdateReplyDraftRequest) else old.text if old.outgoing_entities else None,
                outgoing_entities=rows if isinstance(request, UpdateReplyDraftRequest) or old.outgoing_entities else None,
                formatting_provider=provider if rows else None,
                formatting_provider_epoch=epoch if rows else None,
                local_admission_guard=local_admission_guard)
            self._remember_draft_title(revised)
            return ReviseReplyDraftResponse(status="revised",previous_draft_id=request.draft_id,
                reply=self._reply_preview(revised,owner),detail=REPLY_REVISED)
        except CapabilityDisabled as error:
            return self._reply_disabled(ReviseReplyDraftResponse, error)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return ReviseReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _prepare_text_send(self, request: PrepareTextSendRequest, *, client_id: str,
                           deadline: float | None = None) -> PrepareTextSendResponse:
        if request.topic is not None or request.outgoing_entities:
            try:
                require_current(self._policy)
                required = {'send'} | ({'topic_text_send'} if request.topic is not None else set()) | ({'formatted_text_send'} if request.outgoing_entities else set())
                if not required <= set(self._policy.enabled_capabilities):
                    raise CompatibilityError('capability_disabled')
                with self._shared_client().request_budget(min(deadline if deadline is not None else float('inf'), time.monotonic()+30)):
                    return self._prepare_text_send_impl(request, client_id=client_id)
            except (TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
                return PrepareTextSendResponse(status='error', detail='topic draft could not be prepared safely')
        return self._prepare_text_send_impl(request, client_id=client_id)

    def _prepare_text_send_impl(self, request: PrepareTextSendRequest, *, client_id: str) -> PrepareTextSendResponse:
        try:
            from .outgoing_entity_models import caller_outgoing_entities
            client = self._shared_client()
            client.ensure_ready()
            epoch = client.send_observation_epoch if request.outgoing_entities else None
            owner = self._draft_owner(client_id)
            chat = client.resolve_target(request.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(request.recipient)
            topic = None
            if request.topic is not None:
                if epoch is None:
                    epoch = client.send_observation_epoch
                topic = client.read_topic_snapshot(request.recipient, request.topic.id,
                    expected_account_id=owner.account_id, expected_recipient_title=title)
                if (self._draft_owner(client_id) != owner or self._shared_client() is not client or
                        client.send_observation_epoch is not epoch):
                    raise DraftError('topic authority changed')
            if request.outgoing_entities:
                if (self._draft_owner(client_id) != owner or self._shared_client() is not client or
                        client.send_observation_epoch is not epoch):
                    raise DraftError('formatting authority changed')
                self._require_formatting_capability()
                self._reply_budget_check(client)
            draft = self._drafts.prepare_text(owner=owner, recipient=request.recipient, text=request.text, recipient_title=title,
                topic=topic, topic_provider=client if topic is not None else None,
                topic_provider_epoch=epoch if topic is not None else None,
                outgoing_entities=caller_outgoing_entities(request.outgoing_entities),
                formatting_provider=client if request.outgoing_entities else None,
                formatting_provider_epoch=epoch if request.outgoing_entities else None)
            if topic is not None or request.outgoing_entities:
                self._require_topic_policy(draft.draft_id, owner)
            known = self._drafts.known_ids()
            with self._state_lock:
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[draft.draft_id] = title
            return PrepareTextSendResponse(
                status="prepared", draft_id=draft.draft_id, recipient=draft.recipient,
                recipient_title=title, text=draft.text, sha256=draft.sha256,
                expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
                detail="immutable text draft prepared; explicit approval is required before sending",
                outgoing_entities=caller_outgoing_entities(request.outgoing_entities),
                **({'account_id': owner.account_id, 'topic': topic, 'preview_sha256': self._topic_digest(draft, owner)}
                   if topic is not None or request.outgoing_entities else {}),
            )
        except AuthorizationBlocked:
            return PrepareTextSendResponse(status="blocked", detail="TDLib authorization is not ready")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return PrepareTextSendResponse(status="error", detail="text draft could not be prepared safely")

    def _send_prepared_text(self, request: SendPreparedTextRequest, *, client_id: str, deadline: float | None = None) -> SendPreparedTextResponse:
        result = self._send_prepared_artifact(
            SendPreparedArtifactRequest(draft_id=request.draft_id, approved=request.approved), client_id=client_id, text_only=True, deadline=deadline
        )
        return SendPreparedTextResponse.model_validate(result.model_dump())

    def _send_prepared_artifact(self, request: SendPreparedArtifactRequest, *, client_id: str, text_only: bool = False, deadline: float | None = None) -> SendPreparedArtifactResponse:
        # Budget routing takes the DraftOwner: only this client's draft of the account last observed is routed.
        owner = self._observed_owner(client_id)
        if owner is not None and ((not text_only and self._drafts.is_owned_artifact_reply(request.draft_id, owner=owner))
                or (text_only and (self._drafts.is_owned_topic(request.draft_id, owner=owner) or
                    self._drafts.is_owned_formatted(request.draft_id, owner=owner)))):
            provider=self._shared_client()
            # Capture once: nested provider budgets must not renew time consumed
            # by account reads, owner confirmation or local staging.
            bounded_deadline=min(deadline if deadline is not None else float("inf"),time.monotonic()+330)
            with provider.request_budget(bounded_deadline):
                return self._send_prepared_artifact_impl(request,client_id=client_id,text_only=text_only,deadline=bounded_deadline)
        return self._send_prepared_artifact_impl(request,client_id=client_id,text_only=text_only,deadline=deadline)

    @contextmanager
    def _artifact_send_fence(self, scope, owner, provider, provider_epoch, claim):
        """Observe outside mutexes; serialize only irreversible raw dispatch.

        Registry/state locks are never acquired while this authority fence is
        held. Confirmation waiting happens after it, so release can acknowledge
        an already-issued attempt without cancelling or losing its evidence.
        """
        try:
            if self._draft_owner(owner.client_id) != owner or self._shared_client() is not provider:
                raise DraftError("draft is unavailable")
            require_current(self._policy)
            if "send" not in self._policy.enabled_capabilities:
                raise CompatibilityError("capability_disabled")
            if claim.draft.kind == "video":
                self._require_video_policy()
            if claim.draft.reply_source is not None:
                self._require_reply_source_policy(claim.draft.reply_source)
                if "reply_artifact_send" not in self._policy.enabled_capabilities:
                    raise CompatibilityError("capability_disabled")
            self._reply_budget_check(provider)
        except (ArtifactUnavailable, DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
            raise MessageSendNotAttempted("artifact scope changed before transport") from None
        with self._artifact_authority.lock:
            try:
                self._artifact_authority.live(scope)
                if (self._client is not provider or provider.send_observation_epoch is not provider_epoch or
                        not self._artifact_authority.authorize_local(scope, claim.draft.artifact_id)):
                    raise ArtifactUnavailable("artifact is unavailable")
            except ArtifactUnavailable:
                raise MessageSendNotAttempted("artifact scope changed before transport") from None
            yield

    def _send_prepared_artifact_impl(self, request: SendPreparedArtifactRequest, *, client_id: str, text_only: bool = False, deadline: float | None = None) -> SendPreparedArtifactResponse:
        if request.approved is not True:
            return SendPreparedArtifactResponse(status="declined", draft_id=request.draft_id, detail=NOT_GIVEN)
        try:
            owner = self._draft_owner(client_id)
            kind = self._drafts.kind(request.draft_id, owner=owner)
            if kind in ("forward", "video", "archive", "text_edit") or (kind == "text") != text_only:
                return SendPreparedArtifactResponse(status="error", draft_id=request.draft_id,
                                                    detail="draft kind does not match send tool")
            self._require_topic_policy(request.draft_id, owner)
            # Receipt replay remains bound to the reply capability as well.
            if self._drafts.is_reply(request.draft_id,owner=owner):
                (self._reply_owner if text_only else self._reply_artifact_owner)(client_id)
                provider=self._shared_client()
                capabilities=self._drafts.source_required_capabilities(request.draft_id,owner=owner,
                    provider=provider,provider_epoch=provider.send_observation_epoch)
                if any(capability not in self._policy.enabled_capabilities for capability in capabilities):
                    raise CompatibilityError('capability_disabled')
            previous = self._drafts.receipt(request.draft_id, owner=owner)
            if previous is not None:
                return SendPreparedArtifactResponse(
                    status=previous.status, draft_id=previous.draft_id,
                    message_id=previous.message_id,
                    detail="previous provider attempt receipt; no new send was attempted",
                )
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(request.draft_id)
            if expected_title is None:
                return SendPreparedArtifactResponse(status="expired", draft_id=request.draft_id,
                                                    detail="draft is unavailable after expiry or restart")
            # The recipient is resolved again immediately before the single provider attempt.
            entry = self._drafts.peek(request.draft_id, owner=owner)
            if entry.kind in ("forward", "video", "archive", "text_edit") or (entry.kind == "text") != text_only:
                return SendPreparedArtifactResponse(status="error", draft_id=request.draft_id,
                                                    detail="draft kind does not match send tool")
            chat = self._shared_client().resolve_target(entry.recipient)
            current_title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(entry.recipient)
            if current_title != expected_title:
                if entry.topic is not None or entry.outgoing_entities:
                    raise DraftError("draft is unavailable")
                return SendPreparedArtifactResponse(status="recipient_changed", draft_id=request.draft_id,
                                                    recipient=entry.recipient, detail="recipient title changed; prepare a new draft")
            provider_before_dialog=self._shared_client()
            epoch_before_dialog=provider_before_dialog.send_observation_epoch
            if entry.topic is not None:
                current = provider_before_dialog.read_topic_snapshot(entry.recipient, entry.topic.forum_topic_id,
                    expected_account_id=owner.account_id, expected_recipient_title=expected_title)
                if entry.reply_source is not None:
                    current_source = provider_before_dialog.read_reply_source(*entry.reply_source.anchor,
                        expected_forum_topic_id=entry.topic.forum_topic_id)
                    if current_source != entry.reply_source:
                        raise DraftError('reply evidence changed; prepare a fresh draft')
                self._require_topic_policy(request.draft_id, owner)
                if (current != entry.topic or self._draft_owner(client_id) != owner or
                        self._shared_client() is not provider_before_dialog or
                        provider_before_dialog.send_observation_epoch is not epoch_before_dialog or
                        self._drafts.peek(request.draft_id, owner=owner) != entry):
                    raise DraftError('topic evidence changed; prepare a fresh draft')
            if entry.outgoing_entities:
                if entry.reply_source is not None and entry.topic is None:
                    current_source = provider_before_dialog.read_reply_source(*entry.reply_source.anchor,
                        require_reply_eligibility=True)
                    if current_source != entry.reply_source:
                        raise DraftError('reply evidence changed; prepare a fresh draft')
                if (self._draft_owner(client_id) != owner or self._shared_client() is not provider_before_dialog or
                        provider_before_dialog.send_observation_epoch is not epoch_before_dialog or
                        self._drafts.peek(request.draft_id, owner=owner) != entry):
                    raise DraftError('formatting authority changed')
                self._require_topic_policy(request.draft_id, owner)
                self._reply_budget_check(provider_before_dialog)
            reply_preview=((self._reply_preview if text_only else self._reply_artifact_preview)(entry,owner).model_dump(mode="json") if entry.reply_source is not None else None)
            reply_confirmation={("reply_preview" if text_only else "reply_artifact_preview"):reply_preview} if reply_preview is not None else {}
            if entry.reply_source is None:
                reply_confirmation[('topic_preview' if entry.topic is not None or entry.outgoing_entities
                                    else 'draft_preview')] = self._plain_draft_preview(entry, owner).model_dump(mode='json')
            decision = approval_outcome(self._owner_dialog(
                recipient_title=expected_title, recipient=entry.recipient,
                display_name=entry.display_name, size_bytes=entry.size_bytes,
                sha256=entry.sha256, caption=entry.caption, kind=entry.kind, text=entry.text,
                duration_seconds=entry.duration_seconds, source_sha256=entry.source_sha256,
                source_display_name=entry.source_display_name, **reply_confirmation,
            ))
            if decision != 'approved':  # Fail closed: declined, timeout or dialog_unavailable.
                return SendPreparedArtifactResponse(status=decision, draft_id=request.draft_id,
                                                    recipient=entry.recipient if entry.topic is None and not entry.outgoing_entities else None,
                                                    detail=APPROVAL_DETAILS[decision])
            # The local dialog may remain open while cancellation/account/policy changes.
            if self._draft_owner(client_id) != owner:
                raise DraftError("draft is unavailable")
            provider = self._shared_client()
            self._require_topic_policy(request.draft_id, owner)
            if entry.reply_source is not None:
                (self._reply_owner if text_only else self._reply_artifact_owner)(client_id)
                self._require_reply_source_policy(entry.reply_source)
                if provider is not provider_before_dialog or provider.send_observation_epoch is not epoch_before_dialog:
                    raise DraftError("draft is unavailable")
            if entry.outgoing_entities:
                self._reply_budget_check(provider)
                if provider is not provider_before_dialog or provider.send_observation_epoch is not epoch_before_dialog:
                    raise DraftError('formatting authority changed')
            claim = self._drafts.claim(request.draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            scope = active_scope(self._artifact_store)
            send_fence = (lambda: self._artifact_send_fence(scope, owner, provider, claim.provider_epoch, claim)) if scope is not None else None
            fence_kwargs = {"pre_send_fence": send_fence} if send_fence is not None else {}
            if claim.draft.kind == "text":
                failure_detail = "TDLib rejected the send"
                try:
                    def formatting_guard():
                        require_current(self._policy)
                        return (not claim.draft.outgoing_entities or (
                            {'send', 'formatted_text_send'} <= set(self._policy.enabled_capabilities)
                            and self._shared_client() is provider and provider.send_observation_epoch is epoch_before_dialog
                            and self._drafts.formatting_claim_current(request.draft_id, owner=owner,
                                provider=provider, provider_epoch=epoch_before_dialog)))
                    style_kwargs = {'outgoing_entities': claim.draft.outgoing_entities} if claim.draft.outgoing_entities else {}
                    if claim.draft.reply_source is not None:
                        def reply_guard():
                            require_current(self._policy)
                            return ("send" in self._policy.enabled_capabilities and "reply_text_send" in self._policy.enabled_capabilities
                                    and all(capability in self._policy.enabled_capabilities for capability in claim.draft.reply_source.required_capabilities)
                                    and self._shared_client() is provider and provider.send_observation_epoch is epoch_before_dialog
                                    and formatting_guard()
                                    and (claim.draft.topic is None or (
                                        'topic_text_send' in self._policy.enabled_capabilities and
                                        self._drafts.topic_claim_current(request.draft_id, owner=owner,
                                            provider=provider, provider_epoch=epoch_before_dialog))))
                        reply_sender=getattr(provider,"send_reply_text_message",None)
                        if reply_sender is None:
                            raise MessageSendNotAttempted("reply provider is unavailable")
                        with provider.request_budget(min(deadline if deadline is not None else float("inf"),time.monotonic()+330)):
                            message_id=reply_sender(claim.draft.recipient,claim.draft.text,
                                reply_source=claim.draft.reply_source,expected_account_id=owner.account_id,
                                expected_recipient_title=expected_title,attempt_id=request.draft_id,pre_send_guard=reply_guard,
                                **({'topic': claim.draft.topic} if claim.draft.topic is not None else {}), **style_kwargs)
                    elif claim.draft.topic is not None:
                        def topic_guard():
                            require_current(self._policy)
                            return ({'send', 'topic_text_send'} <= set(self._policy.enabled_capabilities)
                                and formatting_guard()
                                and self._shared_client() is provider
                                and provider.send_observation_epoch is epoch_before_dialog
                                and self._drafts.topic_claim_current(request.draft_id, owner=owner,
                                    provider=provider, provider_epoch=epoch_before_dialog))
                        sender = getattr(provider, 'send_topic_text_message', None)
                        if sender is None:
                            raise MessageSendNotAttempted('topic provider is unavailable')
                        message_id = sender(claim.draft.recipient, claim.draft.text,
                            topic=claim.draft.topic, expected_account_id=owner.account_id,
                            expected_recipient_title=expected_title, attempt_id=request.draft_id,
                            pre_send_guard=topic_guard, **style_kwargs)
                    elif claim.draft.outgoing_entities:
                        message_id = provider.send_text_message(claim.draft.recipient, claim.draft.text,
                            attempt_id=request.draft_id, expected_account_id=owner.account_id,
                            expected_recipient_title=expected_title, pre_send_guard=formatting_guard, **style_kwargs)
                    else:
                        message_id = provider.send_text_message(claim.draft.recipient, claim.draft.text, attempt_id=request.draft_id)
                    receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="sent", message_id=message_id)
                except MessageSendNotAttempted:
                    failure_detail = "local preparation failed; no provider send was attempted"
                    receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="failed", evidence="local_failed")
                except MessageSendFailed as error:
                    failure_detail = error.public_detail()
                    receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="failed")
                except Exception:
                    receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="outcome_unknown")
                if (claim.draft.topic is not None or claim.draft.outgoing_entities) and receipt.evidence != 'local_failed':
                    # Keep observed evidence internally; disclose it only to the
                    # same current account/provider/session and enabled policy.
                    # Receipt retention, not the pending draft TTL, governs this.
                    try:
                        if self._draft_owner(client_id) != owner:
                            raise DraftError('topic receipt owner changed')
                        self._require_topic_policy(request.draft_id, owner)
                    except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
                        return SendPreparedArtifactResponse(status='outcome_unknown', draft_id=request.draft_id,
                            detail='current receipt authority is unavailable; do not retry automatically')
                return SendPreparedArtifactResponse(
                    status=receipt.status, draft_id=receipt.draft_id,
                    recipient=None if (claim.draft.topic is not None or claim.draft.outgoing_entities) and receipt.evidence == 'local_failed' else claim.draft.recipient,
                    message_id=receipt.message_id,
                    detail=("TDLib confirmed delivery" if receipt.status == "sent" else
                            failure_detail if receipt.status == "failed" else
                            "delivery is unconfirmed; do not retry automatically"),
                )
            failure_detail = "TDLib rejected the send"
            try:
                staged_path = stage_approved_document(claim, **self._staging)
            except (OSError,ValueError,ConfigurationError) as error:
                if isinstance(error,ConfigurationError) and claim.draft.reply_source is None:
                    raise  # Preserve the ordinary-send exception boundary.
                self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="failed", evidence="local_failed")
                return SendPreparedArtifactResponse(status="failed", draft_id=request.draft_id,
                                                    recipient=claim.draft.recipient,
                                                    detail="local staged file could not be prepared; no provider send was attempted")
            try:
                # Recheck after potentially long staging, before provider entry.
                if send_fence is not None:
                    with send_fence():
                        pass
                if claim.draft.reply_source is not None:
                    def artifact_reply_guard():
                        require_current(self._policy)
                        return ("send" in self._policy.enabled_capabilities and "reply_artifact_send" in self._policy.enabled_capabilities
                            and all(capability in self._policy.enabled_capabilities for capability in claim.draft.reply_source.required_capabilities)
                            and self._shared_client() is provider and provider.send_observation_epoch is epoch_before_dialog)
                    sender=getattr(provider,"send_reply_artifact_message",None)
                    if not callable(sender):raise MessageSendNotAttempted("reply provider is unavailable")
                    with provider.request_budget(min(deadline if deadline is not None else float("inf"),time.monotonic()+330)):
                        message_id=sender(claim.draft.recipient,staged_path,claim.draft.caption,kind=claim.draft.kind,
                            duration_seconds=claim.draft.duration_seconds,waveform_base64=claim.draft.waveform_base64,
                            reply_source=claim.draft.reply_source,expected_account_id=owner.account_id,
                            expected_recipient_title=expected_title,attempt_id=request.draft_id,pre_send_guard=artifact_reply_guard,**fence_kwargs)
                elif claim.draft.kind == "photo":
                    message_id = provider.send_photo_message(
                        claim.draft.recipient, staged_path, claim.draft.caption, attempt_id=request.draft_id,
                        **fence_kwargs,
                    )
                elif claim.draft.kind == "voice_note":
                    message_id = provider.send_voice_note_message(
                        claim.draft.recipient, staged_path, claim.draft.caption,
                        claim.draft.duration_seconds, claim.draft.waveform_base64, attempt_id=request.draft_id,
                        **fence_kwargs,
                    )
                else:
                    message_id = provider.send_document_message(
                        claim.draft.recipient, staged_path, claim.draft.caption, attempt_id=request.draft_id,
                        **fence_kwargs,
                    )
                receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="sent", message_id=message_id)
            except MessageSendNotAttempted:
                failure_detail = "local preparation failed; no provider send was attempted"
                receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="failed", evidence="local_failed")
            except MessageSendFailed as error:
                failure_detail = error.public_detail()
                receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="failed")
            except Exception:
                receipt = self._drafts.finish(request.draft_id, owner=owner, claim=claim, status="outcome_unknown")
            if receipt.status in {"sent", "failed"}:
                try:
                    retire_staged_document(staged_path, **self._staging)
                except (OSError, ValueError):
                    pass  # The receipt still owns the provider outcome; staging is TTL-bounded.
            return SendPreparedArtifactResponse(
                status=receipt.status, draft_id=receipt.draft_id,
                recipient=claim.draft.recipient, message_id=receipt.message_id,
                detail=("TDLib confirmed delivery" if receipt.status == "sent" else
                        failure_detail if receipt.status == "failed" else
                        "delivery is unconfirmed; do not retry automatically"),
            )
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
            return SendPreparedArtifactResponse(status="expired", draft_id=request.draft_id,
                                                detail="draft is unavailable or no longer valid")

    def _require_forward_policy(self) -> None:
        require_current(self._policy)
        if not {'send', 'forward_send'} <= set(self._policy.enabled_capabilities):
            raise CompatibilityError('capability_disabled')

    def _forward_owner(self, client_id: str) -> DraftOwner:
        owner = self._draft_owner(client_id)
        self._require_forward_policy()
        return owner

    def _forward_preview(self, draft, owner: DraftOwner) -> ForwardDraftPreview:
        spec = draft.forward
        if draft.kind != 'forward' or not isinstance(spec, ForwardSpec):
            raise DraftError(FORWARD_UNAVAILABLE)
        self._require_forward_policy()
        return ForwardDraftPreview.create(draft_id=draft.draft_id, account_id=owner.account_id,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc), mode=spec.mode,
            remove_caption=spec.remove_caption, source=spec.source,
            recipient=ForwardChat(chat_id=draft.recipient, title=draft.recipient_title))

    def _prepare_forward_send(self, request: PrepareForwardSendRequest, *, client_id: str) -> PrepareForwardSendResponse:
        try:
            owner = self._forward_owner(client_id)
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            recipient_identity, recipient_title = chat_identity(provider.resolve_target(request.recipient), request.recipient)
            snapshot = provider.read_forward_source(request.source_chat, request.source_message_id)
            refusal = snapshot.refusal(request.mode)
            if refusal is not None:
                raise ForwardRefusal(refusal)
            if request.remove_caption and snapshot.source.content_type == 'messageText':
                raise ForwardRefusal('invalid_input')  # A text message has no caption to remove.
            spec = ForwardSpec(snapshot=snapshot, source=ForwardSourcePreview.create(snapshot), mode=request.mode,
                               remove_caption=request.remove_caption, recipient_identity=recipient_identity)
            self._reply_budget_check(provider)
            if (self._forward_owner(client_id) != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch):
                raise DraftError(FORWARD_UNAVAILABLE)
            previews = []
            # The preview is built and validated before insertion: a refused preview leaves
            # no orphan draft and supersedes nothing.
            draft = self._drafts.prepare_forward(owner=owner, recipient=request.recipient,
                recipient_title=recipient_title, forward=spec,
                evidence=lambda candidate: previews.append(self._forward_preview(candidate, owner)))
            [preview] = previews
            self._remember_draft_title(draft)
            return PrepareForwardSendResponse(status='prepared', forward=preview, detail=FORWARD_PREPARED)
        except ForwardRefusal as refusal:
            return PrepareForwardSendResponse(status='refused', refusal=refusal.code, detail=FORWARD_REFUSED)
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
            return PrepareForwardSendResponse(status='unavailable', detail=FORWARD_UNAVAILABLE)

    def _send_prepared_forward(self, request: SendPreparedForwardRequest, *, client_id: str,
                               deadline: float | None = None) -> SendPreparedForwardResponse:
        if request.approved is not True:
            return SendPreparedForwardResponse(status='declined', draft_id=request.draft_id, detail=NOT_GIVEN)
        # Capture once: account reads, owner confirmation and transport share one budget.
        bounded = min(deadline if deadline is not None else float('inf'), time.monotonic() + 330)
        with self._shared_client().request_budget(bounded):
            return self._send_prepared_forward_impl(request, client_id=client_id)

    def _send_prepared_forward_impl(self, request: SendPreparedForwardRequest, *, client_id: str) -> SendPreparedForwardResponse:
        draft_id = request.draft_id
        def result(status, detail, **values):
            return SendPreparedForwardResponse(status=status, draft_id=draft_id, detail=detail, **values)
        held = False
        try:
            owner = self._forward_owner(client_id)
            if self._drafts.kind(draft_id, owner=owner) != 'forward':
                return result('error', 'draft kind does not match send tool')
            held = True  # The current owner holds this draft; a lost claim race is routed below.
            previous = self._drafts.receipt(draft_id, owner=owner)
            if previous is not None:
                if previous.evidence == 'local_failed':
                    return result('local_failed', 'previous local failure receipt; no provider attempt was made '
                                  'and no new send was attempted')
                return result(previous.status, 'previous provider attempt receipt; no new send was attempted',
                              message_id=previous.message_id)
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(draft_id)
            if expected_title is None:
                return result('expired', 'draft is unavailable after expiry or restart')
            entry = self._drafts.peek(draft_id, owner=owner)
            spec = entry.forward
            if entry.kind != 'forward' or not isinstance(spec, ForwardSpec):
                return result('error', 'draft kind does not match send tool')
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            identity, title = chat_identity(provider.resolve_target(entry.recipient), entry.recipient)
            if title != expected_title or identity != spec.recipient_identity:
                return result('recipient_changed', 'recipient changed; prepare a new forward draft',
                              recipient=entry.recipient)
            try:
                current = provider.read_forward_source(*spec.snapshot.source.anchor)
            except (ForwardRefusal, MessageNotFound):
                return result('source_changed', 'source message changed; prepare a new forward draft')
            if (current.source != spec.snapshot.source or current.chat_identity != spec.snapshot.chat_identity
                    or current.chat_title != spec.snapshot.chat_title):
                return result('source_changed', 'source message changed; prepare a new forward draft')
            refusal = current.refusal(spec.mode)
            if refusal is not None:
                # An unchanged projection keeps any other prepare-time refusal unreachable here.
                return result(refusal if refusal in {'source_not_forwardable', 'protected_content'} else 'source_changed',
                              'source is no longer eligible; no send was attempted')
            self._reply_budget_check(provider)
            if (self._forward_owner(client_id) != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch or self._drafts.peek(draft_id, owner=owner) != entry):
                raise DraftError(FORWARD_UNAVAILABLE)
            preview = self._forward_preview(entry, owner).model_dump(mode='json')
            decision = approval_outcome(self._owner_dialog(recipient_title=expected_title,
                    recipient=entry.recipient, display_name=entry.display_name, size_bytes=entry.size_bytes,
                    sha256=entry.sha256, caption='', kind='forward', forward_preview=preview))
            if decision != 'approved':  # Fail closed: declined, timeout or dialog_unavailable.
                return result(decision, APPROVAL_DETAILS[decision])
            # The local dialog may remain open while account, policy or provider change.
            if self._forward_owner(client_id) != owner:
                raise DraftError(FORWARD_UNAVAILABLE)
            provider_after = self._shared_client()
            if provider_after is not provider or provider.send_observation_epoch is not epoch:
                raise DraftError(FORWARD_UNAVAILABLE)
            self._reply_budget_check(provider)
            claim = self._drafts.claim(draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            def forward_guard():
                require_current(self._policy)
                return ({'send', 'forward_send'} <= set(self._policy.enabled_capabilities)
                    and self._shared_client() is provider and provider.send_observation_epoch is epoch
                    and self._drafts.forward_claim_current(draft_id, owner=owner, provider=provider,
                                                           provider_epoch=epoch))
            failure_detail = 'TDLib rejected the forward'
            try:
                sender = getattr(provider, 'forward_message', None)
                if not callable(sender):
                    raise MessageSendNotAttempted('forward provider is unavailable')
                message_id = sender(claim.draft.recipient, snapshot=spec.snapshot, mode=spec.mode,
                    remove_caption=spec.remove_caption, expected_account_id=owner.account_id,
                    expected_recipient_title=expected_title, expected_recipient_identity=spec.recipient_identity,
                    attempt_id=draft_id, pre_send_guard=forward_guard)
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='sent', message_id=message_id)
            except MessageSendNotAttempted:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed', evidence='local_failed')
            except MessageSendFailed as error:
                failure_detail = error.public_detail()
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed')
            except Exception:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='outcome_unknown')
            if receipt.evidence == 'local_failed':
                return result('local_failed', 'local revalidation failed; no provider send was attempted')
            try:
                # Disclose observed evidence only to the same current account and policy.
                if self._forward_owner(client_id) != owner:
                    raise DraftError(FORWARD_UNAVAILABLE)
            except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
                return result('outcome_unknown', 'current receipt authority is unavailable; do not retry automatically')
            return result(receipt.status,
                          'TDLib confirmed delivery' if receipt.status == 'sent' else
                          failure_detail if receipt.status == 'failed' else
                          'delivery is unconfirmed; do not retry automatically',
                          recipient=claim.draft.recipient, message_id=receipt.message_id)
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError, AttributeError):
            return self._send_claim_lost(draft_id, client_id, 'forward', result, owner=owner if held else None)

    def _send_claim_lost(self, draft_id: str, client_id: str, kind: str, result, *, owner: DraftOwner | None):
        """A call that lost the claim to a concurrent or repeated call made no attempt, but the
        draft may have been sent: outcome_unknown, never expired (as `_change_attempted`). Only a
        call whose owner (and, for a video, artifact lifecycle) held the draft is routed, with that
        DraftOwner; a later call without it keeps the content-free `expired`, as does a draft never
        claimed."""
        outcome = (self._drafts.attempt_outcome(draft_id, client_id=client_id, owner=owner, kinds=frozenset({kind}))
                   if owner is not None else None)
        if outcome == 'local_failed':
            return result('local_failed', 'local revalidation failed; no provider send was attempted')
        if outcome == 'attempted':
            return result('outcome_unknown', RECEIPT_AUTHORITY_UNAVAILABLE)
        return result('expired', 'draft is unavailable or no longer valid')

    def _require_video_policy(self) -> None:
        require_current(self._policy)
        if not {'send', 'video_send'} <= set(self._policy.enabled_capabilities):
            raise CompatibilityError('capability_disabled')

    def _video_owner(self, client_id: str) -> DraftOwner:
        owner = self._draft_owner(client_id)
        self._require_video_policy()
        return owner

    def _video_preview(self, draft, owner: DraftOwner) -> VideoDraftPreview:
        spec = draft.video
        if draft.kind != 'video' or not isinstance(spec, VideoSpec):
            raise DraftError(VIDEO_UNAVAILABLE)
        self._require_video_policy()
        return VideoDraftPreview.create(draft_id=draft.draft_id, account_id=owner.account_id,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
            recipient=VideoRecipient(chat_id=draft.recipient, title=draft.recipient_title),
            artifact_id=draft.artifact_id, display_name=draft.display_name, size_bytes=draft.size_bytes,
            sha256=draft.sha256, facts=spec.facts, caption=draft.caption)

    def _prepare_video_send(self, request: PrepareVideoSendRequest, *, client_id: str) -> PrepareVideoSendResponse:
        try:
            owner = self._video_owner(client_id)
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            artifact = self._artifact_store.lookup(request.artifact_id)
            if artifact is None:
                raise DraftError(VIDEO_UNAVAILABLE)
            if not 0 < artifact.size_bytes <= MAX_VIDEO_BYTES:
                raise VideoRefusal('video_too_large')
            try:
                facts = probe_video(artifact.path, size_bytes=artifact.size_bytes)
            except VideoProbeError:
                raise VideoRefusal('unsupported_video') from None
            metadata = self._attachment_metadata(request.artifact_id)
            # The file name is display evidence only; the bytes and probed facts are bound by sha256.
            display_name = metadata[1] if metadata is not None and isinstance(metadata[1], str) and metadata[1] else 'video.mp4'
            recipient_identity, recipient_title = chat_identity(provider.resolve_target(request.recipient), request.recipient)
            spec = VideoSpec(facts=facts, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
                             recipient_identity=recipient_identity)
            self._reply_budget_check(provider)
            if (self._video_owner(client_id) != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch):
                raise DraftError(VIDEO_UNAVAILABLE)
            previews = []
            # The preview is built and validated before insertion: a refused preview leaves
            # no orphan draft and supersedes nothing.
            draft = self._drafts.prepare_video(owner=owner, artifact_id=request.artifact_id, recipient=request.recipient,
                recipient_title=recipient_title, display_name=display_name, caption=request.caption, video=spec,
                evidence=lambda candidate: previews.append(self._video_preview(candidate, owner)))
            [preview] = previews
            self._remember_draft_title(draft)
            return PrepareVideoSendResponse(status='prepared', video=preview, detail=VIDEO_PREPARED)
        except VideoRefusal as refusal:
            return PrepareVideoSendResponse(status='refused', refusal=refusal.code, detail=VIDEO_REFUSED)
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError,
                ArtifactStoreError, ArtifactUnavailable):
            return PrepareVideoSendResponse(status='unavailable', detail=VIDEO_UNAVAILABLE)

    def _send_prepared_video(self, request: SendPreparedVideoRequest, *, client_id: str,
                             deadline: float | None = None) -> SendPreparedVideoResponse:
        if request.approved is not True:
            return SendPreparedVideoResponse(status='declined', draft_id=request.draft_id, detail=NOT_GIVEN)
        # Capture once: account reads, owner confirmation, staging and transport share one budget.
        bounded = min(deadline if deadline is not None else float('inf'), time.monotonic() + 330)
        with self._shared_client().request_budget(bounded):
            return self._send_prepared_video_impl(request, client_id=client_id)

    def _send_prepared_video_impl(self, request: SendPreparedVideoRequest, *, client_id: str) -> SendPreparedVideoResponse:
        draft_id = request.draft_id
        def result(status, detail, **values):
            return SendPreparedVideoResponse(status=status, draft_id=draft_id, detail=detail, **values)
        held = False
        try:
            owner = self._video_owner(client_id)
            if self._drafts.kind(draft_id, owner=owner) != 'video':
                return result('error', 'draft kind does not match send tool')
            held = True  # The current owner and artifact lifecycle hold this draft.
            previous = self._drafts.receipt(draft_id, owner=owner)
            if previous is not None:
                if previous.evidence == 'local_failed':
                    return result('local_failed', 'previous local failure receipt; no provider attempt was made '
                                  'and no new send was attempted')
                return result(previous.status, 'previous provider attempt receipt; no new send was attempted',
                              message_id=previous.message_id)
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(draft_id)
            if expected_title is None:
                return result('expired', 'draft is unavailable after expiry or restart')
            # peek re-hashes the artifact against the draft's sha256 and size.
            entry = self._drafts.peek(draft_id, owner=owner)
            spec = entry.video
            if entry.kind != 'video' or not isinstance(spec, VideoSpec):
                return result('error', 'draft kind does not match send tool')
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            identity, title = chat_identity(provider.resolve_target(entry.recipient), entry.recipient)
            if title != expected_title or identity != spec.recipient_identity:
                return result('recipient_changed', 'recipient changed; prepare a new video draft',
                              recipient=entry.recipient)
            self._reply_budget_check(provider)
            if (self._video_owner(client_id) != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch or self._drafts.peek(draft_id, owner=owner) != entry):
                raise DraftError(VIDEO_UNAVAILABLE)
            preview = self._video_preview(entry, owner).model_dump(mode='json')
            decision = approval_outcome(self._owner_dialog(recipient_title=expected_title,
                    recipient=entry.recipient, display_name=entry.display_name, size_bytes=entry.size_bytes,
                    sha256=entry.sha256, caption=entry.caption, kind='video', video_preview=preview))
            if decision != 'approved':  # Fail closed: declined, timeout or dialog_unavailable.
                return result(decision, APPROVAL_DETAILS[decision])
            # The local dialog may remain open while account, policy or provider change.
            if self._video_owner(client_id) != owner:
                raise DraftError(VIDEO_UNAVAILABLE)
            if self._shared_client() is not provider or provider.send_observation_epoch is not epoch:
                raise DraftError(VIDEO_UNAVAILABLE)
            self._reply_budget_check(provider)
            claim = self._drafts.claim(draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            scope = active_scope(self._artifact_store)
            send_fence = (lambda: self._artifact_send_fence(scope, owner, provider, claim.provider_epoch, claim)) if scope is not None else None
            def video_guard():
                require_current(self._policy)
                return ({'send', 'video_send'} <= set(self._policy.enabled_capabilities)
                    and self._shared_client() is provider and provider.send_observation_epoch is epoch
                    and self._drafts.video_claim_current(draft_id, owner=owner, provider=provider,
                                                         provider_epoch=epoch))
            failure_detail = 'TDLib rejected the send'
            try:
                staged_path = stage_approved_document(claim, **self._staging)
            except (OSError, ValueError, ConfigurationError):
                self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed', evidence='local_failed')
                return result('local_failed', 'local staged file could not be prepared; no provider send was attempted')
            try:
                # Recheck after potentially long staging, before provider entry.
                if send_fence is not None:
                    with send_fence():
                        pass
                sender = getattr(provider, 'send_video_message', None)
                if not callable(sender):
                    raise MessageSendNotAttempted('video provider is unavailable')
                facts = spec.facts
                message_id = sender(claim.draft.recipient, staged_path, claim.draft.caption,
                    duration_seconds=facts.duration_seconds, width=facts.width, height=facts.height,
                    supports_streaming=facts.supports_streaming, size_bytes=claim.draft.size_bytes,
                    expected_account_id=owner.account_id, expected_recipient_title=expected_title,
                    expected_recipient_identity=spec.recipient_identity, attempt_id=draft_id,
                    pre_send_guard=video_guard, pre_send_fence=send_fence)
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='sent', message_id=message_id)
            except MessageSendNotAttempted:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed', evidence='local_failed')
            except MessageSendFailed as error:
                failure_detail = error.public_detail()
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed')
            except Exception:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='outcome_unknown')
            if receipt.status in {'sent', 'failed'}:
                try:
                    retire_staged_document(staged_path, **self._staging)
                except (OSError, ValueError):
                    pass  # The receipt still owns the provider outcome; staging is TTL-bounded.
            if receipt.evidence == 'local_failed':
                return result('local_failed', 'local revalidation failed; no provider send was attempted')
            try:
                # Disclose observed evidence only to the same current account, client and policy.
                if self._video_owner(client_id) != owner:
                    raise DraftError(VIDEO_UNAVAILABLE)
            except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError, ArtifactUnavailable):
                return result('outcome_unknown', RECEIPT_AUTHORITY_UNAVAILABLE)
            return result(receipt.status,
                          'TDLib confirmed delivery' if receipt.status == 'sent' else
                          failure_detail if receipt.status == 'failed' else
                          'delivery is unconfirmed; do not retry automatically',
                          recipient=claim.draft.recipient, message_id=receipt.message_id)
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError, AttributeError):
            return self._send_claim_lost(draft_id, client_id, 'video', result, owner=owner if held else None)

    def _require_change_policy(self, capability: str) -> None:
        require_current(self._policy)
        if capability not in ('archive_chats', 'edit_own_text') or not set(required_capabilities(capability)) <= set(
                self._policy.enabled_capabilities):
            raise CompatibilityError('capability_disabled')

    def _change_owner(self, client_id: str, capability: str) -> DraftOwner:
        owner = self._draft_owner(client_id)
        self._require_change_policy(capability)
        return owner

    def _change_attempted(self, draft_id: str, client_id: str, kind: str, result):
        """A consumed claim never reads as expired after a lost authority (attempt_outcome): the call cannot name
        the owner that held the claim, so its client decides."""
        outcome = self._drafts.attempt_outcome(draft_id, client_id=client_id, owner=None, kinds=frozenset({kind}))
        if outcome == 'local_failed':
            return result('local_failed', 'local revalidation failed; no provider change was attempted')
        if outcome == 'attempted':
            return result('outcome_unknown', RECEIPT_AUTHORITY_UNAVAILABLE)
        return result('expired', 'draft is unavailable or no longer valid')

    def _archive_preview(self, draft, owner: DraftOwner) -> ArchiveDraftPreview:
        spec = draft.archive
        if draft.kind != 'archive' or not isinstance(spec, ArchiveSpec):
            raise DraftError(ARCHIVE_UNAVAILABLE)
        self._require_change_policy('archive_chats')
        snapshot = spec.snapshot
        return ArchiveDraftPreview.create(draft_id=draft.draft_id, account_id=owner.account_id,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
            chat=ArchiveChat(chat_id=draft.recipient, title=draft.recipient_title, kind=snapshot.kind),
            current_list=snapshot.current_list, target_list=spec.target_list)

    def _prepare_chat_archive(self, request: PrepareChatArchiveRequest, *, client_id: str) -> PrepareChatArchiveResponse:
        try:
            owner = self._change_owner(client_id, 'archive_chats')
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            try:
                snapshot = provider.read_chat_list_state(request.chat)
            except SecretChatRejected:
                raise ArchiveRefusal('unsupported_chat') from None
            if snapshot.current_list == 'none':
                raise ArchiveRefusal('unsupported_chat')  # Neither Main nor Archive carries the chat.
            def unchanged():
                self._reply_budget_check(provider)
                if (self._change_owner(client_id, 'archive_chats') != owner or self._shared_client() is not provider or
                        provider.send_observation_epoch is not epoch):
                    raise DraftError(ARCHIVE_UNAVAILABLE)
            unchanged()
            if snapshot.current_list == request.target_list:
                return PrepareChatArchiveResponse(status='already_in_target', detail=ARCHIVE_ALREADY)
            if request.target_list not in provider.chat_lists_to_add(request.chat):
                raise ArchiveRefusal('unsupported_chat')  # Telegram does not offer this list, e.g. the service chat.
            unchanged()
            spec = ArchiveSpec(snapshot=snapshot, target_list=request.target_list)
            previews = []
            draft = self._drafts.prepare_change(owner=owner, chat_id=request.chat, chat_title=snapshot.title,
                change=spec, evidence=lambda candidate: previews.append(self._archive_preview(candidate, owner)))
            [preview] = previews
            self._remember_draft_title(draft)
            return PrepareChatArchiveResponse(status='prepared', archive=preview, detail=ARCHIVE_PREPARED)
        except ArchiveRefusal as refusal:
            return PrepareChatArchiveResponse(status='refused', refusal=refusal.code, detail=ARCHIVE_REFUSED)
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
            return PrepareChatArchiveResponse(status='unavailable', detail=ARCHIVE_UNAVAILABLE)

    def _apply_prepared_archive(self, request: ApplyPreparedArchiveRequest, *, client_id: str,
                                deadline: float | None = None) -> ApplyPreparedArchiveResponse:
        if request.approved is not True:
            return ApplyPreparedArchiveResponse(status='declined', draft_id=request.draft_id, detail=NOT_GIVEN)
        # Capture once: account reads, owner confirmation, the attempt and its observation share one budget.
        bounded = min(deadline if deadline is not None else float('inf'), time.monotonic() + 330)
        with self._shared_client().request_budget(bounded):
            return self._apply_prepared_archive_impl(request, client_id=client_id)

    def _apply_prepared_archive_impl(self, request: ApplyPreparedArchiveRequest, *,
                                     client_id: str) -> ApplyPreparedArchiveResponse:
        draft_id = request.draft_id
        def result(status, detail, **values):
            return ApplyPreparedArchiveResponse(status=status, draft_id=draft_id, detail=detail, **values)
        try:
            owner = self._change_owner(client_id, 'archive_chats')
            if self._drafts.kind(draft_id, owner=owner) != 'archive':
                return result('error', 'draft kind does not match apply tool')
            previous = self._drafts.receipt(draft_id, owner=owner)
            if previous is not None:
                return self._archive_receipt(previous, result, replay=True)
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(draft_id)
            if expected_title is None:
                return result('expired', 'draft is unavailable after expiry or restart')
            entry = self._drafts.peek(draft_id, owner=owner)
            spec = entry.archive
            if entry.kind != 'archive' or not isinstance(spec, ArchiveSpec):
                return result('error', 'draft kind does not match apply tool')
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            try:
                current = provider.read_chat_list_state(entry.recipient)
            except (SecretChatRejected, ValueError):
                return result('chat_changed', 'chat changed; prepare a new archive draft', chat_id=entry.recipient)
            reviewed = spec.snapshot
            if (current.identity != reviewed.identity or current.kind != reviewed.kind or
                    current.title != reviewed.title or current.title != expected_title):
                return result('chat_changed', 'chat identity or title changed; prepare a new archive draft',
                              chat_id=entry.recipient)
            if current.current_list == spec.target_list:
                return result('already_in_target', 'chat is already in the target list; no change was attempted',
                              chat_id=entry.recipient)
            if current.current_list != reviewed.current_list:
                return result('chat_changed', 'chat list changed; prepare a new archive draft', chat_id=entry.recipient)
            self._reply_budget_check(provider)
            if (self._change_owner(client_id, 'archive_chats') != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch or self._drafts.peek(draft_id, owner=owner) != entry):
                raise DraftError(ARCHIVE_UNAVAILABLE)
            preview = self._archive_preview(entry, owner).model_dump(mode='json')
            decision = approval_outcome(self._owner_dialog(recipient_title=expected_title,
                    recipient=entry.recipient, display_name=entry.display_name, size_bytes=entry.size_bytes,
                    sha256=entry.sha256, caption='', kind='archive', archive_preview=preview))
            if decision != 'approved':  # Fail closed: declined, timeout or dialog_unavailable.
                return result(decision, APPROVAL_DETAILS[decision])
            # The local dialog may remain open while account, policy or provider change.
            if self._change_owner(client_id, 'archive_chats') != owner:
                raise DraftError(ARCHIVE_UNAVAILABLE)
            if self._shared_client() is not provider or provider.send_observation_epoch is not epoch:
                raise DraftError(ARCHIVE_UNAVAILABLE)
            self._reply_budget_check(provider)
            claim = self._drafts.claim(draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            def archive_guard():
                require_current(self._policy)
                return (set(required_capabilities('archive_chats')) <= set(self._policy.enabled_capabilities)
                    and self._shared_client() is provider and provider.send_observation_epoch is epoch
                    and self._drafts.change_claim_current(draft_id, owner=owner, kind='archive', provider=provider,
                                                          provider_epoch=epoch))
            failure_detail = 'TDLib rejected the change'
            try:
                mover = getattr(provider, 'move_chat_to_list', None)
                if not callable(mover):
                    raise MessageSendNotAttempted('archive provider is unavailable')
                observed = mover(entry.recipient, target_list=spec.target_list, expected=reviewed,
                                 expected_account_id=owner.account_id, pre_send_guard=archive_guard)
                if observed == spec.target_list:
                    receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='applied', observed=observed)
                else:
                    receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='outcome_unknown')
            except MessageSendNotAttempted:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed', evidence='local_failed')
            except MessageSendFailed as error:
                failure_detail = self._change_failure(error)
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed')
            except Exception:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='outcome_unknown')
            if receipt.evidence == 'local_failed':
                return result('local_failed', 'local revalidation failed; no provider change was attempted')
            try:
                # Disclose observed evidence only to the same current account and policy.
                if self._change_owner(client_id, 'archive_chats') != owner:
                    raise DraftError(ARCHIVE_UNAVAILABLE)
            except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
                return result('outcome_unknown', RECEIPT_AUTHORITY_UNAVAILABLE)
            return self._archive_receipt(receipt, result, failure_detail=failure_detail, chat_id=entry.recipient)
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError, AttributeError):
            return self._change_attempted(draft_id, client_id, 'archive', result)

    @staticmethod
    def _change_failure(error: MessageSendFailed) -> str:
        code = error.provider_code
        return 'TDLib rejected the change' if code is None else f'TDLib rejected the change (code {code})'

    @staticmethod
    def _archive_receipt(receipt, result, *, replay: bool = False, failure_detail: str = 'TDLib rejected the change',
                         chat_id: int | None = None):
        if receipt.evidence == 'local_failed':
            return result('local_failed', 'previous local failure receipt; no provider change was attempted')
        if replay:
            values = {'observed_list': receipt.observed} if receipt.status == 'applied' else {}
            return result(receipt.status, 'previous provider attempt receipt; no new change was attempted', **values)
        if receipt.status == 'applied':
            return result('applied', 'TDLib applied the move; list observed right after the call',
                          chat_id=chat_id, observed_list=receipt.observed)
        if receipt.status == 'failed':
            return result('failed', failure_detail, chat_id=chat_id)
        return result('outcome_unknown', 'the target list was not observed; do not retry automatically', chat_id=chat_id)

    def _text_edit_preview(self, draft, owner: DraftOwner) -> TextEditDraftPreview:
        spec = draft.text_edit
        if draft.kind != 'text_edit' or not isinstance(spec, TextEditSpec):
            raise DraftError(TEXT_EDIT_UNAVAILABLE)
        self._require_change_policy('edit_own_text')
        return TextEditDraftPreview.create(draft_id=draft.draft_id, account_id=owner.account_id,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
            chat=EditChat(chat_id=draft.recipient, title=draft.recipient_title, kind=spec.snapshot.chat_kind),
            snapshot=spec.snapshot,
            new_text=spec.new_text)

    def _prepare_text_edit(self, request: PrepareTextEditRequest, *, client_id: str) -> PrepareTextEditResponse:
        try:
            owner = self._change_owner(client_id, 'edit_own_text')
            try:
                new_text = normalized_text(request.new_text)
            except ValueError:
                raise EditRefusal('invalid_input') from None
            # normalized_text already applied Telegram's own rewrite, so the dialog's new text is what Telegram
            # stores; these two checks only guard input that skipped it.
            if edge_whitespace(new_text):
                return PrepareTextEditResponse(status='refused', refusal='invalid_input', detail=TEXT_EDIT_WHITESPACE)
            if rewritten_text(new_text):
                return PrepareTextEditResponse(status='refused', refusal='invalid_input', detail=TEXT_EDIT_REWRITTEN)
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            snapshot = provider.read_own_text_message(request.chat, request.message_id)
            refusal = snapshot.refusal()
            if refusal is not None:
                raise EditRefusal(refusal)
            if not evidence_fits(snapshot):
                raise EditRefusal('unsupported_source_content')
            if new_text == snapshot.raw_text:
                raise EditRefusal('unchanged_text')
            spec = TextEditSpec(snapshot=snapshot, new_text=new_text)
            self._reply_budget_check(provider)
            if (self._change_owner(client_id, 'edit_own_text') != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch):
                raise DraftError(TEXT_EDIT_UNAVAILABLE)
            previews = []
            draft = self._drafts.prepare_change(owner=owner, chat_id=request.chat, chat_title=snapshot.chat_title,
                change=spec, evidence=lambda candidate: previews.append(self._text_edit_preview(candidate, owner)))
            [preview] = previews
            self._remember_draft_title(draft)
            return PrepareTextEditResponse(status='prepared', text_edit=preview, detail=TEXT_EDIT_PREPARED)
        except EditRefusal as refusal:
            return PrepareTextEditResponse(status='refused', refusal=refusal.code, detail=TEXT_EDIT_REFUSED)
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
            return PrepareTextEditResponse(status='unavailable', detail=TEXT_EDIT_UNAVAILABLE)

    def _apply_prepared_text_edit(self, request: ApplyPreparedTextEditRequest, *, client_id: str,
                                  deadline: float | None = None) -> ApplyPreparedTextEditResponse:
        if request.approved is not True:
            return ApplyPreparedTextEditResponse(status='declined', draft_id=request.draft_id, detail=NOT_GIVEN)
        # Capture once: account reads, owner confirmation and transport share one budget.
        bounded = min(deadline if deadline is not None else float('inf'), time.monotonic() + 330)
        with self._shared_client().request_budget(bounded):
            return self._apply_prepared_text_edit_impl(request, client_id=client_id)

    def _apply_prepared_text_edit_impl(self, request: ApplyPreparedTextEditRequest, *,
                                       client_id: str) -> ApplyPreparedTextEditResponse:
        draft_id = request.draft_id
        def result(status, detail, **values):
            return ApplyPreparedTextEditResponse(status=status, draft_id=draft_id, detail=detail, **values)
        try:
            owner = self._change_owner(client_id, 'edit_own_text')
            if self._drafts.kind(draft_id, owner=owner) != 'text_edit':
                return result('error', 'draft kind does not match apply tool')
            previous = self._drafts.receipt(draft_id, owner=owner)
            if previous is not None:
                if previous.evidence == 'local_failed':
                    return result('local_failed', 'previous local failure receipt; no provider change was attempted')
                return result(previous.status, 'previous provider attempt receipt; no new change was attempted',
                              **({'edit_date': previous.observed} if previous.status == 'applied' else {}))
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(draft_id)
            if expected_title is None:
                return result('expired', 'draft is unavailable after expiry or restart')
            entry = self._drafts.peek(draft_id, owner=owner)
            spec = entry.text_edit
            if entry.kind != 'text_edit' or not isinstance(spec, TextEditSpec):
                return result('error', 'draft kind does not match apply tool')
            chat_id, message_id = spec.snapshot.anchor
            changed = dict(chat_id=chat_id, message_id=message_id)
            provider = self._shared_client()
            epoch = provider.send_observation_epoch
            try:
                current = provider.read_own_text_message(chat_id, message_id)
            except (EditRefusal, MessageNotFound, SecretChatRejected, ValueError):
                return result('message_changed', 'message changed; prepare a new text edit draft', **changed)
            if (current.source != spec.snapshot.source or current.chat_identity != spec.snapshot.chat_identity or
                    current.chat_title != spec.snapshot.chat_title or current.chat_title != expected_title):
                return result('message_changed', 'message changed; prepare a new text edit draft', **changed)
            if not current.can_be_edited:
                return result('message_changed', 'message can no longer be edited; no change was attempted', **changed)
            self._reply_budget_check(provider)
            if (self._change_owner(client_id, 'edit_own_text') != owner or self._shared_client() is not provider or
                    provider.send_observation_epoch is not epoch or self._drafts.peek(draft_id, owner=owner) != entry):
                raise DraftError(TEXT_EDIT_UNAVAILABLE)
            preview = self._text_edit_preview(entry, owner).model_dump(mode='json')
            decision = approval_outcome(self._owner_dialog(recipient_title=expected_title,
                    recipient=entry.recipient, display_name=entry.display_name, size_bytes=entry.size_bytes,
                    sha256=entry.sha256, caption='', kind='text_edit', text_edit_preview=preview))
            if decision != 'approved':  # Fail closed: declined, timeout or dialog_unavailable.
                return result(decision, APPROVAL_DETAILS[decision])
            # The local dialog may remain open while account, policy or provider change.
            if self._change_owner(client_id, 'edit_own_text') != owner:
                raise DraftError(TEXT_EDIT_UNAVAILABLE)
            if self._shared_client() is not provider or provider.send_observation_epoch is not epoch:
                raise DraftError(TEXT_EDIT_UNAVAILABLE)
            self._reply_budget_check(provider)
            claim = self._drafts.claim(draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            def edit_guard():
                require_current(self._policy)
                return (set(required_capabilities('edit_own_text')) <= set(self._policy.enabled_capabilities)
                    and self._shared_client() is provider and provider.send_observation_epoch is epoch
                    and self._drafts.change_claim_current(draft_id, owner=owner, kind='text_edit', provider=provider,
                                                          provider_epoch=epoch))
            failure_detail = 'TDLib rejected the change'
            try:
                editor = getattr(provider, 'edit_own_text', None)
                if not callable(editor):
                    raise MessageSendNotAttempted('edit provider is unavailable')
                edit_date = editor(chat_id, message_id, new_text=spec.new_text, expected=spec.snapshot,
                                   expected_account_id=owner.account_id, pre_send_guard=edit_guard)
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='applied', observed=edit_date)
            except MessageSendNotAttempted:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed', evidence='local_failed')
            except MessageSendFailed as error:
                failure_detail = self._change_failure(error)
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='failed')
            except Exception:
                receipt = self._drafts.finish(draft_id, owner=owner, claim=claim, status='outcome_unknown')
            if receipt.evidence == 'local_failed':
                return result('local_failed', 'local revalidation failed; no provider change was attempted')
            try:
                # Disclose observed evidence only to the same current account and policy.
                if self._change_owner(client_id, 'edit_own_text') != owner:
                    raise DraftError(TEXT_EDIT_UNAVAILABLE)
            except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
                return result('outcome_unknown', RECEIPT_AUTHORITY_UNAVAILABLE)
            if receipt.status == 'applied':
                return result('applied', 'TDLib returned the edited message with the new text', edit_date=receipt.observed,
                              **changed)
            if receipt.status == 'failed':
                return result('failed', failure_detail, **changed)
            return result('outcome_unknown', 'the edit result is unconfirmed; do not retry automatically', **changed)
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError, AttributeError):
            return self._change_attempted(draft_id, client_id, 'text_edit', result)

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, CompatibilityError):
            return error.code
        if isinstance(error, TimeoutError):
            return "expired"
        if isinstance(error, (BrokerProtocolError, ValidationError)):
            return "invalid_request"
        return "unavailable"

    def _handle_connection(self, connection: socket.socket) -> None:
        request: dict[str, Any] | None = None
        response_sent = False
        with self._state_lock:
            self._active_handlers += 1
            self._peak_overlap = max(self._peak_overlap, self._active_handlers)
        try:
            with connection:
                connection.settimeout(FRAME_READ_TIMEOUT_SECONDS)
                if (
                    self._verify_peer_uid
                    and self._peer_uid_loader(connection) != os.getuid()
                ):
                    return
                request = receive_request(connection)
                try:
                    require_current(self._policy)
                    if request["operation"] != "handshake":
                        raise CompatibilityError("handshake_required")
                    if request["broker_generation"] is not None:
                        raise CompatibilityError("handshake_invalid")
                    remaining = request["deadline"] - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("request deadline expired")
                    if remaining > MAX_DEADLINE_SECONDS:
                        raise BrokerProtocolError("request deadline exceeds the allowed bound")
                    require_compatible(request["payload"], self._contract)
                    descriptor = {**self._contract, "broker_generation": self._generation}
                    send_frame(connection, {"version": PROTOCOL_VERSION, "request_id": request["request_id"],
                                            "ok": True, "result": descriptor}, max_bytes=MAX_RESPONSE_BYTES)
                    # Exactly one operation may follow on this authenticated connection.
                    hello = request
                    if not connection.recv(1, socket.MSG_PEEK):
                        return  # A diagnostic-only handshake ends with a clean EOF.
                    request = receive_request(connection)
                    if (request["client_id"] != hello["client_id"] or request["deadline"] != hello["deadline"]
                            or request["request_id"] == hello["request_id"]):
                        raise CompatibilityError("handshake_invalid")
                    def publish(result):
                        nonlocal response_sent
                        send_frame(connection, {"version": PROTOCOL_VERSION,
                            "request_id": request["request_id"], "ok": True, "result": result},
                            max_bytes=MAX_RESPONSE_BYTES)
                        response_sent = True
                    sending = request["operation"] in SEND_OPERATIONS
                    if sending:
                        with self._state_lock:
                            self._sends_in_flight += 1
                    try:
                        result = self._dispatch(request, publish=publish)
                    finally:
                        if sending:
                            with self._state_lock:
                                self._sends_in_flight -= 1
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": True,
                        "result": result,
                    }
                    with self._state_lock:
                        self._completed += 1
                except Exception as error:
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": False,
                        "error": self._error_code(error),
                    }
                    with self._state_lock:
                        self._errors += 1
                if not response_sent:
                    send_frame(connection, response, max_bytes=MAX_RESPONSE_BYTES)
        except (OSError, BrokerProtocolError):
            with self._state_lock:
                self._errors += 1
        finally:
            with self._state_lock:
                self._connections.discard(connection)
                self._active_handlers -= 1
                self._pending -= 1
            self._pending_slots.release()

    def _reject_overloaded(self, connection: socket.socket) -> None:
        """Return a correlated terminal response without entering the worker queue."""
        try:
            with connection:
                connection.settimeout(FRAME_READ_TIMEOUT_SECONDS)
                if (
                    self._verify_peer_uid
                    and self._peer_uid_loader(connection) != os.getuid()
                ):
                    return
                request = receive_request(connection)
                send_frame(
                    connection,
                    {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": False,
                        "error": "overloaded",
                    },
                    max_bytes=MAX_RESPONSE_BYTES,
                )
        except (OSError, BrokerProtocolError):
            pass
        finally:
            with self._state_lock:
                self._connections.discard(connection)
                self._errors += 1

    def serve_forever(self) -> None:
        ensure_private_directory(self._socket_path.parent)
        lock_descriptor = self._acquire_owner_lock()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: tuple[int, int] | None = None
        try:
            self._prepare_socket_path()
            listener.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            bound = self._socket_path.lstat()
            bound_identity = (bound.st_dev, bound.st_ino)
            listener.listen()
            listener.settimeout(0.1)
            self._listener = listener
            self._ready.set()
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                with self._state_lock:
                    self._connections.add(connection)
                if self._stop.is_set():
                    connection.close()
                    with self._state_lock:
                        self._connections.discard(connection)
                    break
                if not self._pending_slots.acquire(blocking=False):
                    self._reject_overloaded(connection)
                    continue
                with self._state_lock:
                    self._pending += 1
                self._executor.submit(self._handle_connection, connection)
        finally:
            self._ready.set()
            listener.close()
            self._executor.shutdown(wait=True, cancel_futures=False)
            with self._state_lock:
                contexts = list(self._contexts.values())
                self._contexts.clear()
                client = self._client
                self._client = None
            for service in contexts:
                service.close()
            if client is not None:
                client.close()
            try:
                metadata = self._socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    bound_identity is not None
                    and stat.S_ISSOCK(metadata.st_mode)
                    and not self._socket_path.is_symlink()
                    and metadata.st_uid == os.getuid()
                    and (metadata.st_dev, metadata.st_ino) == bound_identity
                ):
                    self._socket_path.unlink()
            os.close(lock_descriptor)

    def shutdown(self) -> None:
        self._upload_authorities.shutdown(self._stop.set)
        listener = self._listener
        if listener is not None:
            listener.close()
        with self._state_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
