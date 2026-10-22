"""Independent proxy issuance memory and strict selected response validation."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time
from pydantic import TypeAdapter
from .contract import CompatibilityError, fingerprint, require_current
from .retained_selection_models import RetainSelectionRequest, RetainSelectionResponse, RetainedSelection
from .selection_refs import SelectionRefusal

_WIRE=ContextVar('retained_selection_wire',default=None)


class ProxySelectionRefusal(RuntimeError):
    pass


def selection_wire(operation,payload):
    value=_WIRE.get()
    return value[1] if value is not None and value[0]==operation else payload


def strict_selection_response(operation,result):
    value=_WIRE.get()
    if value is None or value[0]!=operation:return result
    if result.get('status')=='selection_unavailable':
        TypeAdapter(SelectionRefusal).validate_json(json.dumps(result,allow_nan=False),strict=True)
        raise ProxySelectionRefusal()
    TypeAdapter(value[2]).validate_json(json.dumps(result,allow_nan=False),strict=True)
    return result


@dataclass(frozen=True)
class _Issuance:
    response: RetainedSelection
    generation: str
    expires_monotonic: float
    source_key: str


def public_message_facts(row):
    value=row.message.model_dump(mode='json') if row.message is not None else None
    if value is not None:
        for key in ('display_name','display_name_sanitized','display_name_truncated'):
            value['sender'].pop(key,None)
        # A best-effort provider link may appear or vanish between reads.
        value['source'].pop('telegram_url',None)
    return row.anchor.model_dump(mode='json'),value


class SelectionClientMixin:
    def _init_selections(self):
        self._selection_lock=threading.RLock();self._selection_states={};self._selection_cursors={}
        self._selection_active=0;self._selection_pending=0;self._selection_generation=None

    def _require_selections(self):
        require_current(self._policy)
        if 'retained_selections' not in self._policy.enabled_capabilities:raise CompatibilityError('capability_disabled')

    def _selection_local(self,token):
        with self._selection_lock:
            state=self._selection_states.get(token)
            if (self._closed or state is None or state.generation!=self._selection_generation or
                    time.monotonic()>=state.expires_monotonic or datetime.now(timezone.utc)>=state.response.expires_at):
                self._selection_states.pop(token,None)
                raise ProxySelectionRefusal()
            return state

    def _selection_handshake(self,generation):
        with self._selection_lock:
            if self._selection_generation!=generation:
                self._selection_states.clear();self._selection_cursors.clear();self._selection_generation=generation
            wire=_WIRE.get()
            if wire:
                for key in ('selection_id','reply_selection_id'):
                    if wire[1].get(key):self._selection_local(wire[1][key])

    def retain_selection(self,request):
        self._require_selections()
        request=RetainSelectionRequest.model_validate(request.model_dump())
        source_key=fingerprint(request.source.model_dump(mode='json'))
        started=time.monotonic();wall=datetime.now(timezone.utc)
        with self._selection_lock:
            if self._closed or self._selection_active>=4: return SelectionRefusal()
            self._selection_states={k:v for k,v in self._selection_states.items()
                if started<v.expires_monotonic and wall<v.response.expires_at}
            recapture=any(v.source_key==source_key for v in self._selection_states.values())
            if len(self._selection_states)+self._selection_pending>=16 and not recapture:return SelectionRefusal()
            self._selection_active+=1;self._selection_pending+=1
        try:
            source=request.source;target=None
            if source.kind=='target_messages':
                self._require_verified_targets()
                with self._target_lock:target=self._target_check(source.target_handle)
            raw=self._request('retain_selection',request.model_dump(mode='json'))
            result=TypeAdapter(RetainSelectionResponse).validate_json(json.dumps(raw,allow_nan=False),strict=True)
            if isinstance(result,SelectionRefusal):return result
            # The proxy loaded the same trusted policy; the broker cannot widen the lifetime.
            ttl=self._policy.selection_ttl_seconds
            remaining=(result.expires_at-datetime.now(timezone.utc)).total_seconds()
            if not 0<remaining<=ttl:raise ProxySelectionRefusal()
            if source.kind=='file':
                if result.kind!='file' or result.file.artifact_id!=source.artifact_id:raise ProxySelectionRefusal()
                _,_,checksum,size=source.artifact_id.split('_')
                if result.file.sha256!=checksum or result.file.size_bytes!=int(size):raise ProxySelectionRefusal()
            else:
                anchors=([(a.chat_id,a.message_id) for a in source.anchors] if source.kind=='messages' else
                         [(target.chat_id,mid) for mid in source.message_ids])
                if result.kind!='messages' or [(r.anchor.chat_id,r.anchor.message_id) for r in result.messages.results]!=anchors:raise ProxySelectionRefusal()
                if target is not None and result.expires_at>target.expires_at:raise ProxySelectionRefusal()
            with self._selection_lock:
                if self._closed or self._selection_generation is None:raise ProxySelectionRefusal()
                prior=self._selection_states.get(result.selection_id)
                if prior is not None:
                    if (prior.source_key!=source_key or prior.response.kind!=result.kind or
                            prior.response.expires_at!=result.expires_at or prior.response.file!=result.file or
                            (result.messages is not None and [public_message_facts(r) for r in result.messages.results]!=
                             [public_message_facts(r) for r in prior.response.messages.results])):raise ProxySelectionRefusal()
                else:
                    # A verified replacement cannot leave stale same-source issuance
                    # consuming local capacity or being reused after source reversion.
                    self._selection_states={k:v for k,v in self._selection_states.items() if v.source_key!=source_key}
                    prior=_Issuance(result.model_copy(deep=True),self._selection_generation,min(started+ttl,time.monotonic()+remaining),source_key)
                    self._selection_states[result.selection_id]=prior
                self._selection_local(result.selection_id)
            return result
        except (ProxySelectionRefusal,ValueError,TypeError,OSError,RuntimeError):
            return SelectionRefusal()
        finally:
            with self._selection_lock:self._selection_active-=1;self._selection_pending-=1

    def _selected_call(self,operation,request,model,legacy):
        self._require_selections()
        states=[];raw=request.model_dump(mode='json');resolved=dict(raw)
        with self._selection_lock:
            if self._selection_active>=4:return SelectionRefusal()
            self._selection_active+=1
        try:
            for key in ('selection_id','reply_selection_id'):
                token=raw.get(key)
                if token is None:continue
                state=self._selection_local(token);states.append(state)
                expected='messages' if key=='reply_selection_id' or operation in {
                    'read_messages','get_attachment','get_message_context','read_reply_chain'} else 'file'
                result=state.response
                if result.kind!=expected:raise ProxySelectionRefusal()
                resolved.pop(key,None)
                if expected=='file':resolved['artifact_id']=result.file.artifact_id
                else:
                    anchors=[row.anchor.model_dump(mode='json') for row in result.messages.results]
                    if operation!='read_messages' and len(anchors)!=1:raise ProxySelectionRefusal()
                    resolved['reply_to' if key=='reply_selection_id' else 'anchors' if operation=='read_messages' else 'anchor']=anchors if operation=='read_messages' else anchors[0]
            digest=fingerprint({k:v for k,v in raw.items() if k!='cursor'})
            cursor=raw.get('cursor')
            if cursor is not None:
                with self._selection_lock:
                    if self._selection_cursors.pop(cursor,None)!=(operation,digest):raise ProxySelectionRefusal()
            wire=_WIRE.set((operation,raw,model|SelectionRefusal))
            try:response=legacy(type(request).model_validate(resolved))
            finally:_WIRE.reset(wire)
            if operation=='read_messages' and any(row.message is None for row in response.results):
                raise ProxySelectionRefusal()
            if operation=='read_messages':
                expected=next(state.response.messages for state in states if state.response.kind=='messages')
                if [public_message_facts(row) for row in response.results]!=[public_message_facts(row) for row in expected.results]:raise ProxySelectionRefusal()
            if operation in {'get_attachment','get_message_context'}:
                expected=next(state.response.messages.results[0].anchor for state in states if state.response.kind=='messages')
                if response.anchor!=expected:raise ProxySelectionRefusal()
            if operation=='prepare_artifact_send' and response.status=='prepared':
                from .outgoing_drafts import _safe_display_name, _safe_caption
                file=next(state.response.file for state in states if state.response.kind=='file')
                if (response.recipient!=request.recipient or response.kind!=request.kind or
                        response.caption!=_safe_caption(request.caption)):raise ProxySelectionRefusal()
                if request.kind=='voice_note':
                    if response.source_sha256!=file.sha256 or response.source_display_name!=_safe_display_name(request.display_name):raise ProxySelectionRefusal()
                elif (response.sha256!=file.sha256 or response.size_bytes!=file.size_bytes or
                        response.display_name!=_safe_display_name(request.display_name) or response.mime_type!=request.mime_type):raise ProxySelectionRefusal()
            for state in states:self._selection_local(state.response.selection_id)
            scope=getattr(response,'scope',None)
            if scope is not None:
                file=next(state.response for state in states if state.response.kind=='file')
                if (scope.selection_id!=file.selection_id or scope.artifact_id!=file.file.artifact_id or
                        scope.artifact_sha256!=file.file.sha256 or scope.artifact_bytes!=file.file.size_bytes or
                        (hasattr(scope,'expires_at') and scope.expires_at>file.expires_at)):raise ProxySelectionRefusal()
            next_cursor=getattr(response,'next_cursor',None)
            if next_cursor is not None:
                with self._selection_lock:self._selection_cursors[next_cursor]=(operation,digest)
            return response
        except (ProxySelectionRefusal,ValueError,TypeError,OSError,RuntimeError):
            return SelectionRefusal()
        finally:
            with self._selection_lock:self._selection_active-=1


def selected_method(operation,response_model):
    """Keep every legacy comparison, with independently validated selected wire data."""
    def decorate(fn):
        def wrapper(self,request):
            if getattr(request,'selection_id',None) is not None or getattr(request,'reply_selection_id',None) is not None:
                return self._selected_call(operation,request,response_model,lambda raw:fn(self,raw))
            return fn(self,request)
        wrapper.__name__=fn.__name__
        return wrapper
    return decorate
