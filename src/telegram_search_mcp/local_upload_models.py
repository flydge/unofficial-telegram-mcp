"""Closed public owner receipts and private supplier transport contracts."""
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator, field_validator
from .schemas import ArtifactId, AppendLocalUploadRequest, FinishLocalUploadRequest
from .local_upload import _LIMITS, _NAME, _SHA256, MAX_CHUNK_BYTES


class _Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)

    @field_validator('expires_at', check_fields=False)
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timezone required')
        return value


class LocalUploadManifest(_Closed):
    file_name: Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    kind: Literal['document', 'photo', 'voice_note']
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=64*1024*1024)]
    sha256: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]

    @model_validator(mode='after')
    def exact(self):
        if (_NAME.fullmatch(self.file_name) is None or _SHA256.fullmatch(self.sha256) is None
                or self.size_bytes > _LIMITS[self.kind]):
            raise ValueError('invalid local upload manifest')
        return self


UploadAuthority = Annotated[str, Field(strict=True, pattern=r'^upload_authority_[0-9a-f]{64}$', max_length=81)]
UploadResultRef = Annotated[str, Field(strict=True, pattern=r'^upload_result_[0-9a-f]{64}$', max_length=78)]
UploadSession = Annotated[str, Field(strict=True, pattern=r'^upload_[0-9a-f]{32}$', max_length=39)]


class IssueLocalUploadAuthorityRequest(_Closed):
    manifest: LocalUploadManifest


class LocalUploadResultRequest(_Closed):
    result_ref: UploadResultRef


class LocalUploadUnavailable(_Closed):
    status: Literal['unavailable'] = 'unavailable'
    detail: Literal['local upload is unavailable'] = 'local upload is unavailable'


class LocalUploadPending(_Closed):
    status: Literal['pending'] = 'pending'
    detail: Literal['local upload is pending'] = 'local upload is pending'


class LocalUploadCancelled(_Closed):
    status: Literal['cancelled'] = 'cancelled'
    detail: Literal['local upload cancelled'] = 'local upload cancelled'


class LocalUploadIssued(_Closed):
    status: Literal['issued'] = 'issued'
    upload_authority: UploadAuthority
    result_ref: UploadResultRef
    expires_at: datetime
    max_chunk_bytes: Literal[524288] = MAX_CHUNK_BYTES
    max_size_bytes: Annotated[int, Field(strict=True, ge=1, le=64*1024*1024)]
    detail: Literal['supply bytes with the trusted helper; original owner must consume'] = 'supply bytes with the trusted helper; original owner must consume'


class LocalUploadConsumed(_Closed):
    status: Literal['complete'] = 'complete'
    artifact_id: ArtifactId
    artifact_path: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    sha256: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=64*1024*1024)]
    file_name: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    expires_at: datetime
    detail: Literal['complete local upload stored in private artifact cache'] = 'complete local upload stored in private artifact cache'


IssueLocalUploadAuthorityResponse = LocalUploadIssued | LocalUploadUnavailable
ConsumeLocalUploadResultResponse = LocalUploadConsumed | LocalUploadPending | LocalUploadUnavailable
CancelLocalUploadAuthorityResponse = LocalUploadCancelled | LocalUploadUnavailable


class UploadAuthorityEnvelope(_Closed):
    upload_authority: UploadAuthority
    manifest: LocalUploadManifest


class AuthorizedUploadReady(_Closed):
    status: Literal['ready'] = 'ready'
    upload_id: UploadSession
    result_ref: UploadResultRef
    expires_at: datetime
    max_chunk_bytes: Literal[524288] = MAX_CHUNK_BYTES
    detail: Literal['ordered authorized local upload started'] = 'ordered authorized local upload started'


class AuthorizedUploadAccepted(_Closed):
    status: Literal['accepted'] = 'accepted'
    upload_id: UploadSession
    next_index: Annotated[int, Field(strict=True, ge=1, le=1025)]
    received_bytes: Annotated[int, Field(strict=True, ge=1, le=64*1024*1024)]
    detail: Literal['chunk accepted'] = 'chunk accepted'


class AuthorizedUploadComplete(_Closed):
    status: Literal['upload_complete'] = 'upload_complete'
    result_ref: UploadResultRef
    detail: Literal['local upload supplied; original owner must consume'] = 'local upload supplied; original owner must consume'


RedeemLocalUploadAuthorityResponse = AuthorizedUploadReady | LocalUploadUnavailable
AppendAuthorizedLocalUploadResponse = AuthorizedUploadAccepted | LocalUploadUnavailable
FinishAuthorizedLocalUploadResponse = AuthorizedUploadComplete | LocalUploadUnavailable

PUBLIC_MODELS = {
    'issue_local_upload_authority': (IssueLocalUploadAuthorityRequest, IssueLocalUploadAuthorityResponse),
    'consume_local_upload_result': (LocalUploadResultRequest, ConsumeLocalUploadResultResponse),
    'cancel_local_upload_authority': (LocalUploadResultRequest, CancelLocalUploadAuthorityResponse),
}
HELPER_MODELS = {
    'redeem_local_upload_authority': (UploadAuthorityEnvelope, RedeemLocalUploadAuthorityResponse),
    'append_authorized_local_upload': (AppendLocalUploadRequest, AppendAuthorizedLocalUploadResponse),
    'finish_authorized_local_upload': (FinishLocalUploadRequest, FinishAuthorizedLocalUploadResponse),
}
UPLOAD_MODELS = PUBLIC_MODELS | HELPER_MODELS


def upload_response(operation, value):
    return TypeAdapter(UPLOAD_MODELS[operation][1]).validate_python(value)
