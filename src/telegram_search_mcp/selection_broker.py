"""Retained-source admission surrounding legacy operations and B2A publication."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from .artifact_authority import ArtifactUnavailable
from .artifact_store import ArtifactStoreError
from .contract import CompatibilityError, fingerprint
from .message_reader import _message
from .retained_selection_models import RetainSelectionRequest, RetainedSelection, RetainedFile
from .schemas import SelectedMessageAnchor, ReadMessagesResponse
from .selection_projection import SelectionUnavailable, message_projection
from .selection_refs import SelectionRefusal
from .sanitize import sanitize_telegram_text
from .tdjson import TDLibError
from .verified_targets import _identity, TargetGuardFailure
from .retained_selections import SelectionCapacity


_FILE_SELECTION=ContextVar('retained_file_operation',default=None)
_MESSAGE_SELECTION=ContextVar('retained_message_operation',default=None)
_INSERT_FENCE=ContextVar('retained_selection_insert_fence',default=None)
_CANDIDATE_DRAFTS=ContextVar('retained_candidate_drafts',default=None)


def record_selection_draft(draft_id):
    candidates=_CANDIDATE_DRAFTS.get()
    if candidates is not None:candidates.append(draft_id)


def selected_admission_fence():
    fence=_INSERT_FENCE.get()
    return fence() if fence is not None else nullcontext()


def selected_file_context():
    return _FILE_SELECTION.get()


def selected_request_facts(request):
    value=request.model_dump(mode='json',exclude={'cursor'})
    binding=selected_file_context()
    if binding is not None:
        value['retained_source']={'selection_id':binding.selection_id,'expires_at':binding.expires_at.isoformat(),
            'artifact_id':binding.artifact_id,'sha256':binding.facts[0],'bytes':binding.facts[1],
            'provenance_sha256':fingerprint({'kind':binding.facts[3].kind,'anchors':binding.facts[3].anchors,
                'facts':binding.facts[3].facts,'account_id':binding.facts[3].account_id})}
    return value



@dataclass(frozen=True)
class FileProvenance:
    kind: str
    anchors: tuple=()
    facts: tuple=()
    account_id: int | None=None
    account_epoch: object | None=None


def selected_models():
    from .schemas import (AttachmentRequest, MessageContextRequest, ReadReplyChainRequest,
        ReadMessagesRequest, ReadAttachmentRequest, AnalyzeMediaRequest, PrepareArtifactSendRequest)
    from .attachment_page_models import ReadAttachmentPageRequest
    from .spreadsheet_models import ReadSpreadsheetRequest
    from .presentation_models import ReadPresentationRequest
    from .reply_drafts import PrepareReplyTextSendRequest
    from .reply_artifact_drafts import PrepareReplyArtifactSendRequest
    return {'read_messages':ReadMessagesRequest,'get_attachment':AttachmentRequest,
        'get_message_context':MessageContextRequest,'read_reply_chain':ReadReplyChainRequest,
        'read_attachment':ReadAttachmentRequest,'analyze_media':AnalyzeMediaRequest,
        'read_attachment_page':ReadAttachmentPageRequest,'read_spreadsheet':ReadSpreadsheetRequest,
        'read_presentation':ReadPresentationRequest,'prepare_artifact_send':PrepareArtifactSendRequest,
        'prepare_reply_text_send':PrepareReplyTextSendRequest,'prepare_reply_artifact_send':PrepareReplyArtifactSendRequest}


class SelectionBrokerMixin:
    def _selection_message_client(self):
        value=_MESSAGE_SELECTION.get()
        return _ExactOperationClient(self,value[0],value[1]) if value is not None and value[1] else self._shared_client()

    def _download_capture(self,scope,chat,raw):
        if scope is None:return None
        try:
            anchor=(raw.get('chat_id'),raw.get('id'))
            checksum,_=message_projection(raw,anchor)
            identity,_=_identity(chat,anchor[0])
            self._selection_account(scope)
            return FileProvenance('telegram',(anchor,),((identity,checksum),),scope.account_id,scope.account_epoch)
        except (SelectionUnavailable,TargetGuardFailure,ValueError,TypeError):
            # Legacy downloads remain available without supported stable retention facts.
            return None

    def _download_validate(self,scope,provenance):
        scope.account_id=provenance.account_id;scope.account_epoch=provenance.account_epoch
        facts,_=self._capture_sources(scope,provenance.anchors,time.monotonic()+30)
        if facts!=provenance.facts:raise ArtifactUnavailable('artifact is unavailable')

    def _download_record(self,scope,provenance,artifact):
        self._download_validate(scope,provenance)
        with self._artifact_authority.publication(scope):
            with self._state_lock:self._artifact_provenance[artifact.artifact_id]=provenance

    def _selection_account(self,scope):
        self._artifact_authority.live(scope)
        account=self._artifact_account()
        if account is None:raise SelectionUnavailable()
        self._artifact_authority.observe(scope,account)
        return account

    def _capture_sources(self,scope,anchors,deadline):
        if time.monotonic()>=deadline:raise SelectionUnavailable()
        client=self._shared_client();raws=[];facts=[];text_count=0
        with client.request_budget(min(deadline,time.monotonic()+30)):
            self._selection_account(scope)
            for anchor in anchors:
                self._selection_account(scope)
                chat=client.resolve_target(anchor[0]);self._selection_account(scope)
                identity,_=_identity(chat,anchor[0])
                raw=client.get_message(*anchor);self._selection_account(scope)
                checksum,count=message_projection(raw,anchor);text_count+=count
                if text_count>100000:raise SelectionUnavailable()
                facts.append((identity,checksum));raws.append(raw)
            self._selection_account(scope)
        if time.monotonic()>=deadline:raise SelectionUnavailable()
        return tuple(facts),raws

    def _selection_check(self,scope,binding,*,target_locked=False):
        self._artifact_authority.live(scope)
        self._selections.check(binding)
        if binding.schema_fingerprint!=self._contract['schema_fingerprint']:raise SelectionUnavailable()
        if binding.target_dependency is not None:
            reader,target=binding.target_dependency
            if target_locked:reader._lifecycle_locked(binding.target_handle,target)
            else:reader._lifecycle(binding.target_handle,target)
        if binding.account_epoch is not None:
            # Pure epoch checks happen before provider/metadata work and under fences.
            if (binding.account_epoch is not self._artifact_authority._account_epoch or
                    binding.account_id!=self._artifact_authority._account_id):raise SelectionUnavailable()

    @contextmanager
    def _selection_fence(self,scope,bindings):
        # Pure revocation facts only: never observe provider state under these locks.
        with self._artifact_authority.lock, self._selections.lock, ExitStack() as locks:
            self._artifact_authority.live(scope)
            seen=set()
            for binding in bindings:
                if binding.target_dependency is not None:
                    reader=binding.target_dependency[0]
                    if id(reader) not in seen:locks.enter_context(reader._lock);seen.add(id(reader))
            for binding in bindings:self._selection_check(scope,binding,target_locked=True)
            yield

    @contextmanager
    def _selection_publication(self,scope,bindings):
        self._artifact_authority.check_account(scope)
        with self._selection_fence(scope,bindings):yield

    def _selection_target(self,service,binding):
        if binding.target_handle:
            reader=service._verified_target_reader
            target=reader._entries.get(binding.target_handle)
            if target is None:raise SelectionUnavailable()
            reader._lifecycle(binding.target_handle,target)
            reader._hydrate(target.chat_id,binding.target_handle,target)
            return target

    def _file_snapshot(self,scope,artifact_id):
        # The lifetime-bound grant precedes metadata and hashing cached bytes.
        if not self._artifact_authority.authorize(scope,artifact_id):raise SelectionUnavailable()
        metadata=self._attachment_metadata(artifact_id)
        with self._state_lock:provenance=self._artifact_provenance.get(artifact_id)
        if metadata is None or provenance is None:raise SelectionUnavailable()
        artifact=self._artifact_store.lookup(artifact_id)
        if artifact is None:raise SelectionUnavailable()
        anchor,name,mime,kind=metadata
        facts=(artifact.sha256,artifact.size_bytes,fingerprint([anchor.model_dump(mode='json') if anchor else None,
            name,mime,kind]),provenance)
        return facts,artifact,metadata,provenance

    def _revalidate_file(self,scope,binding,deadline):
        self._selection_check(scope,binding)
        facts,artifact,metadata,provenance=self._file_snapshot(scope,binding.artifact_id)
        if facts!=binding.facts:raise SelectionUnavailable()
        if provenance.account_id is not None:
            scope.account_id=provenance.account_id;scope.account_epoch=provenance.account_epoch
            captured,_=self._capture_sources(scope,provenance.anchors,deadline)
            if captured!=provenance.facts:raise SelectionUnavailable()
        self._selection_check(scope,binding)
        return artifact,metadata,provenance

    def _retain_selection(self,request,scope,deadline,service):
        source=RetainSelectionRequest.model_validate(request['payload']).source
        source_key=('capture',fingerprint(source.model_dump(mode='json')))
        existing=self._selections.find_source(scope.owner,source_key)
        if existing is not None:
            try:self._selection_check(scope,existing)
            except (SelectionUnavailable,ArtifactUnavailable,TargetGuardFailure):
                self._selections.invalidate(existing.selection_id);existing=None
        with self._selections.reserve(scope.owner,existing.selection_id if existing else None) as started:
            binding=None
            try:
                cap=None;target_handle=None;target_dependency=None
                if source.kind=='file':
                    facts,artifact,metadata,provenance=self._file_snapshot(scope,source.artifact_id)
                    if provenance.account_id is not None:
                        scope.account_id=provenance.account_id;scope.account_epoch=provenance.account_epoch
                        captured,_=self._capture_sources(scope,provenance.anchors,deadline)
                        if captured!=provenance.facts:raise SelectionUnavailable()
                    cap=datetime.fromtimestamp(artifact.expires_at,tz=timezone.utc)
                    anchor,name,mime,kind=metadata
                    evidence=RetainedFile(artifact_id=artifact.artifact_id,sha256=artifact.sha256,size_bytes=artifact.size_bytes,
                        file_name=sanitize_telegram_text(name,max_length=255) or None,
                        mime_type=sanitize_telegram_text(mime,max_length=127) or None,media_type=kind,
                        anchor=SelectedMessageAnchor.model_validate(anchor.model_dump()) if anchor else None,provenance=provenance.kind)
                    kwargs=dict(kind='file',source_key=source_key,facts=facts,artifact_id=source.artifact_id)
                    content=dict(file=evidence)
                else:
                    if source.kind=='target_messages':
                        if 'verified_targets' not in self._policy.enabled_capabilities:raise CompatibilityError('capability_disabled')
                        reader=service._verified_target_reader
                        target=reader._reserve(source.target_handle)
                        try:
                            reader._hydrate(target.chat_id,source.target_handle,target)
                            anchors=tuple((target.chat_id,i) for i in source.message_ids)
                            cap=target.expires_at;target_handle=source.target_handle
                            target_dependency=(reader,target)
                            facts,raws=self._capture_sources(scope,anchors,deadline)
                            reader._lifecycle(source.target_handle,target)
                        finally:reader._release(issuing=False)
                    else:
                        anchors=tuple((a.chat_id,a.message_id) for a in source.anchors)
                        facts,raws=self._capture_sources(scope,anchors,deadline)
                    rows=[];budget=100000
                    for anchor,raw in zip(anchors,raws):
                        row=_message(self._shared_client(),raw,SelectedMessageAnchor(chat_id=anchor[0],message_id=anchor[1]),budget)
                        if row.message is None:raise SelectionUnavailable()
                        if row.message.text:budget-=len(row.message.text.value)
                        rows.append(row)
                    self._selection_account(scope)
                    current,_=self._capture_sources(scope,anchors,deadline)
                    if current!=facts:raise SelectionUnavailable()
                    content=dict(messages=ReadMessagesResponse(status='complete' if all(r.coverage_complete for r in rows) else 'partial',
                        coverage_complete=all(r.coverage_complete for r in rows),results=rows))
                    kwargs=dict(kind='messages',source_key=source_key,facts=facts,anchors=anchors,
                        target_handle=target_handle,target_dependency=target_dependency)
                with self._artifact_authority.publication(scope), self._selections.lock, ExitStack() as locks:
                    if target_dependency is not None:
                        reader,target=target_dependency
                        locks.enter_context(reader._lock)
                        reader._lifecycle_locked(target_handle,target)
                    binding=self._selections.issue(scope.owner,started,expires_at=cap,account_id=scope.account_id,
                        account_epoch=scope.account_epoch,schema_fingerprint=self._contract['schema_fingerprint'],**kwargs)
                    candidate=RetainedSelection(selection_id=binding.selection_id,kind=binding.kind,
                        expires_at=binding.expires_at,**content)
                    self._selection_check(scope,binding,target_locked=True)
                    return candidate.model_dump(mode='json'),binding
            except (SelectionUnavailable,ArtifactUnavailable,ArtifactStoreError,TDLibError,TargetGuardFailure,
                    TimeoutError,OSError,ValueError,TypeError,AttributeError) as error:
                if not isinstance(error,SelectionCapacity):
                    for prior in (existing,binding):
                        if prior is not None:self._selections.invalidate(prior.selection_id)
                raise

    def _dispatch_selection(self,request,*,publish=None):
        operation=request['operation'];payload=request['payload'];bindings=[];candidate=None;draft_candidates=[]
        if 'retained_selections' not in self._policy.enabled_capabilities:raise CompatibilityError('capability_disabled')
        capture=RetainSelectionRequest.model_validate(payload) if operation=='retain_selection' else None
        if capture is None:selected_models()[operation].model_validate(payload)
        message_operation=operation in {'get_attachment','get_message_context','read_messages','read_reply_chain','retain_selection'}
        deadline=min(request['deadline'],time.monotonic()+(30 if message_operation else 300))
        with self._artifact_authority.scope(request['client_id'],self._artifact_store) as scope:
            try:
                admitted={}
                if capture is None:
                    # Pure owner/kind admission precedes even provider construction.
                    for key in ('selection_id','reply_selection_id'):
                        token=payload.get(key)
                        if token is None:continue
                        binding=self._selections.get(token,scope.owner)
                        self._selection_check(scope,binding)
                        expected='messages' if key=='reply_selection_id' or operation in {
                            'read_messages','get_attachment','get_message_context','read_reply_chain'} else 'file'
                        if binding.kind!=expected or (expected=='messages' and operation!='read_messages' and len(binding.anchors)!=1):raise SelectionUnavailable()
                        admitted[key]=binding
                needs_service=(capture is not None and capture.source.kind=='target_messages') or any(b.target_handle for b in admitted.values())
                with self._service_lease(request['client_id'],artifact_only=not needs_service) as service, ExitStack() as stack:
                    if operation=='retain_selection':
                        candidate,binding=self._retain_selection(request,scope,deadline,service)
                        bindings.append(binding)
                    else:
                        resolved=dict(payload)
                        for key in ('selection_id','reply_selection_id'):
                            token=payload.get(key)
                            if token is None:continue
                            binding=admitted[key]
                            self._selection_check(scope,binding)
                            expected='messages' if key=='reply_selection_id' or operation in {
                                'read_messages','get_attachment','get_message_context','read_reply_chain'} else 'file'
                            if binding.kind!=expected or (expected=='messages' and operation!='read_messages' and len(binding.anchors)!=1):raise SelectionUnavailable()
                            stack.enter_context(self._selections.reserve(scope.owner,token))
                            bindings.append(binding);resolved.pop(key,None)
                            scope.account_id=binding.account_id;scope.account_epoch=binding.account_epoch
                            if expected=='file':
                                self._revalidate_file(scope,binding,deadline);resolved['artifact_id']=binding.artifact_id
                            else:
                                self._selection_target(service,binding)
                                facts,_=self._capture_sources(scope,binding.anchors,deadline)
                                if facts!=binding.facts:raise SelectionUnavailable()
                                rows=[{'chat_id':a[0],'message_id':a[1]} for a in binding.anchors]
                                resolved['reply_to' if key=='reply_selection_id' else 'anchors' if operation=='read_messages' else 'anchor']=rows if operation=='read_messages' else rows[0]
                        file_token=_FILE_SELECTION.set(next((b for b in bindings if b.kind=='file'),None))
                        message_token=_MESSAGE_SELECTION.set((scope,tuple(b for b in bindings if b.kind=='messages')))
                        insert_token=_INSERT_FENCE.set(lambda:self._selection_fence(scope,bindings))
                        candidates_token=_CANDIDATE_DRAFTS.set(draft_candidates)
                        try:
                            candidate=self._dispatch({**request,'payload':resolved})
                        finally:
                            _FILE_SELECTION.reset(file_token)
                            _MESSAGE_SELECTION.reset(message_token)
                            _INSERT_FENCE.reset(insert_token)
                            _CANDIDATE_DRAFTS.reset(candidates_token)
                        if draft_candidates and candidate.get('status')!='prepared':
                            for draft_id in draft_candidates:self._drafts.discard_unpublished(draft_id)
                            draft_candidates.clear()
                        for binding in bindings:
                            self._selection_check(scope,binding)
                            if binding.kind=='file':self._revalidate_file(scope,binding,deadline)
                            else:
                                self._selection_target(service,binding)
                                facts,_=self._capture_sources(scope,binding.anchors,deadline)
                                if facts!=binding.facts:raise SelectionUnavailable()
                        self._selection_derived(candidate,bindings)
                    with self._selection_publication(scope,bindings):
                        if publish:publish(candidate)
                        return candidate
            except (SelectionUnavailable,ArtifactUnavailable,ArtifactStoreError,TDLibError,TargetGuardFailure,
                    TimeoutError,OSError,ValueError,TypeError,AttributeError) as error:
                if not isinstance(error,SelectionCapacity):
                    for binding in bindings:self._selections.invalidate(binding.selection_id)
                if candidate is not None:self._discard_selection_candidate(candidate)
                for draft_id in draft_candidates:self._drafts.discard_unpublished(draft_id)
                result=SelectionRefusal().model_dump(mode='json')
                if publish:publish(result)
                return result

    def _discard_selection_candidate(self,candidate):
        # Remove unpublished drafts atomically, without provider/account observation.
        draft=candidate.get('draft_id') or (candidate.get('reply') or {}).get('draft',{}).get('draft_id')
        if draft:self._drafts.discard_unpublished(draft)

    def _selection_derived(self,candidate,bindings):
        parents=[binding for binding in bindings if binding.kind=='file']
        if not parents:return
        self._record_derived(candidate,parents[0].artifact_id)

    def _record_derived(self,candidate,parent_id):
        with self._state_lock:parent=self._artifact_provenance.get(parent_id)
        if parent is None:return
        for row in [*candidate.get('images',[]),*candidate.get('frames',[])]:
            token=row.get('artifact_id')
            if token and token!=parent_id:
                self._record_derived_file(token,parent_id,
                    'preview.png' if row.get('mime_type')=='image/png' else 'preview.jpg',row.get('mime_type'),'photo')

    def _record_derived_file(self,token,parent_id,name,mime,kind):
        with self._state_lock:
            parent=self._artifact_provenance.get(parent_id)
            metadata=self._artifact_metadata.get(parent_id)
            if parent is None or metadata is None:return
            self._artifact_provenance[token]=FileProvenance('derived',parent.anchors,parent.facts,parent.account_id,parent.account_epoch)
            self._artifact_metadata[token]=(metadata[0],name,mime,kind)


class _ExactOperationClient:
    """Validate the actual returned raw source, including transient drift that reverts."""
    def __init__(self,broker,scope,bindings):
        self.broker,self.scope,self.bindings=broker,scope,bindings
        self.client=broker._shared_client()

    def read_reply_source(self,chat_id,message_id,**kwargs):
        from .reply_drafts import source_from_message
        raw=self.__getattr__('get_message')(chat_id,message_id)
        expected=source_from_message(raw,chat_id,message_id,
            expected_forum_topic_id=kwargs.get('expected_forum_topic_id'))
        self.broker._selection_account(self.scope)
        actual=self.client.read_reply_source(chat_id,message_id,**kwargs)
        self.broker._selection_account(self.scope)
        if actual.source_sha256!=expected.source_sha256:raise SelectionUnavailable()
        return actual

    def __getattr__(self,name):
        method=getattr(self.client,name)
        if name=='request_budget':return method
        def guarded(*args,**kwargs):
            for binding in self.bindings:self.broker._selection_check(self.scope,binding)
            self.broker._selection_account(self.scope)
            result=method(*args,**kwargs)
            self.broker._selection_account(self.scope)
            for binding in self.bindings:self.broker._selection_check(self.scope,binding)
            if name=='get_message':
                anchor=tuple(args[:2])
                for binding in self.bindings:
                    if anchor in binding.anchors:
                        checksum,_=message_projection(result,anchor)
                        if checksum!=binding.facts[binding.anchors.index(anchor)][1]:raise SelectionUnavailable()
            if name=='resolve_target':
                for binding in self.bindings:
                    for anchor,fact in zip(binding.anchors,binding.facts):
                        if args[0]==anchor[0] and _identity(result,anchor[0])[0]!=fact[0]:raise SelectionUnavailable()
            return result
        return guarded
