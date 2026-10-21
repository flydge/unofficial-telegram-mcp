"""Structural STDIO recorder: which registration a third-party MCP client launched, and how it went.

A client registers `python -m telegram_search_mcp.stdio_recorder --record <new file> -- <server argv>`
as the server command. Every byte is relayed unchanged in both directions. The record is JSON
Lines: the protocol revision, client and server name/version, the tools/list census and
fingerprint, and per call the tool name, isError and content-block types and byte sizes. Arguments,
result text and Telegram content are never recorded; a name or version outside its closed grammar
becomes "redacted". Exits with the server's status; refusals are fixed codes on stderr.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from . import __version__

SCHEMA = 'tgmcp-stdio-record/v1'
MAX_FRAME = 32 * 1024 * 1024    # a larger line is relayed but not parsed
MAX_EVENTS = 4096               # later events are only counted
MAX_PENDING = 1024              # correlated client requests awaiting a response
MAX_BATCH = 64                  # messages parsed per JSON-RPC batch
MAX_BLOCKS = 64                 # content blocks described per result
MAX_ARGV = 64
CHUNK = 64 * 1024
EXIT_GRACE_SECONDS = 30
KILL_GRACE_SECONDS = 5
REDACTED = 'redacted'
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._+/@()-]{0,63}\Z')
_REVISION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,31}\Z')
_TOOL = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
BLOCK_TYPES = frozenset({'text', 'image', 'audio', 'resource_link', 'resource'})
# The 2026-07-28 revision replaces initialize with server/discover and stamps every request's _meta.
PROTOCOL_META = 'io.modelcontextprotocol/protocolVersion'
CLIENT_META = 'io.modelcontextprotocol/clientInfo'
SERVER_META = 'io.modelcontextprotocol/serverInfo'
CORRELATED = frozenset({'initialize', 'server/discover', 'tools/list', 'tools/call'})
METHODS = CORRELATED | frozenset({
    'notifications/initialized', 'ping', 'tools/list', 'tools/call', 'resources/list',
    'resources/templates/list', 'resources/read', 'resources/subscribe', 'resources/unsubscribe',
    'prompts/list', 'prompts/get', 'completion/complete', 'logging/setLevel', 'roots/list',
    'sampling/createMessage', 'elicitation/create', 'notifications/cancelled', 'notifications/progress',
    'notifications/message', 'notifications/roots/list_changed', 'notifications/tools/list_changed',
    'notifications/resources/list_changed', 'notifications/resources/updated',
    'notifications/prompts/list_changed'})


class RecorderRefusal(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _label(value: object, grammar: re.Pattern[str] = _LABEL) -> str | None:
    if value is None:
        return None
    return value if type(value) is str and grammar.match(value) else REDACTED


def _object(value: object) -> dict:
    return value if type(value) is dict else {}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8', 'surrogatepass')


def _key(message_id: object) -> tuple[str, object] | None:
    if type(message_id) is int or (type(message_id) is str and len(message_id) <= 256):
        return type(message_id).__name__, message_id
    return None


class Recorder:
    """Turns whole JSON-RPC lines into structural events; thread-safe, never raises on input."""

    def __init__(self, emit: Callable[[dict[str, object]], None], *, clock: Callable[[], float] = time.monotonic):
        self._emit, self._clock, self._start = emit, clock, clock()
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, object], tuple[str, str | None]] = {}
        self._events = self._suppressed = self._unparsed = self._oversized = self._untracked = 0
        self._frames = {'client': 0, 'server': 0}
        self._methods: dict[str, dict[str, int]] = {'client': {}, 'server': {}}
        self._stamp: tuple[str | None, ...] | None = None
        self._closed = False
        self.event('start', schema=SCHEMA, recorder_version=__version__)

    def event(self, name: str, *, final: bool = False, **fields: object) -> None:
        if self._events >= MAX_EVENTS and not final:
            self._suppressed += 1
            return
        self._events += 1
        self._emit({'event': name, 't_ms': int((self._clock() - self._start) * 1000), **fields})

    def oversized(self, side: str) -> None:
        with self._lock:
            if not self._closed:
                self._frames[side] += 1
                self._oversized += 1

    def frame(self, side: str, line: bytes) -> None:
        if not line.strip():
            return
        with self._lock:
            if self._closed:  # nothing is emitted after `end`, so the record descriptor can close
                return
            self._frames[side] += 1
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                self._unparsed += 1
                return
            for message in (value[:MAX_BATCH] if type(value) is list else [value]):
                if type(message) is not dict:
                    self._unparsed += 1
                elif side == 'client':
                    self._client(message)
                else:
                    self._server(message)

    def _count(self, side: str, method: object, *, response: bool = False) -> str:
        name = 'response' if response else method if type(method) is str and method in METHODS else 'other'
        self._methods[side][name] = self._methods[side].get(name, 0) + 1
        return name

    def _client(self, message: dict[str, object]) -> None:
        if 'method' not in message:
            self._count('client', None, response=True)  # answers a server request; never correlated
            return
        method = self._count('client', message['method'])
        params = _object(message.get('params'))
        meta = _object(params.get('_meta'))
        if PROTOCOL_META in meta or CLIENT_META in meta:
            info = _object(meta.get(CLIENT_META))
            stamp = (_label(meta.get(PROTOCOL_META), _REVISION), _label(info.get('name')), _label(info.get('version')))
            if stamp != self._stamp:
                self._stamp = stamp
                self.event('stamp', protocol=stamp[0], client_name=stamp[1], client_version=stamp[2])
        tool = None
        if method == 'initialize':
            info = _object(params.get('clientInfo'))
            self.event('initialize', requested_protocol=_label(params.get('protocolVersion'), _REVISION),
                       client_name=_label(info.get('name')), client_version=_label(info.get('version')))
        elif method == 'tools/call':
            tool = _label(params.get('name'), _TOOL)
        if 'id' in message and method in CORRELATED:
            key = _key(message['id'])
            if key is None or (key not in self._pending and len(self._pending) >= MAX_PENDING):
                self._untracked += 1
            else:
                self._pending[key] = (method, tool)

    def _server(self, message: dict[str, object]) -> None:
        if 'method' in message:
            self._count('server', message['method'])
            return
        key = _key(message.get('id'))
        method, tool = self._pending.pop(key, (None, None)) if key is not None else (None, None)
        result = message.get('result')
        if method is None:
            return
        if type(result) is not dict:
            error = message.get('error')
            code = error.get('code') if type(error) is dict else None
            self.event('rpc_error', method=method, tool=tool, code=code if type(code) is int else REDACTED)
        elif method == 'initialize':
            info = _object(result.get('serverInfo'))
            self.event('initialized', negotiated_protocol=_label(result.get('protocolVersion'), _REVISION),
                       server_name=_label(info.get('name')), server_version=_label(info.get('version')))
        elif method == 'server/discover':
            versions = result.get('supportedVersions')
            info = _object(_object(result.get('_meta')).get(SERVER_META))
            self.event('discovered', supported_protocols=[_label(v, _REVISION) for v in versions[:16]]
                       if type(versions) is list else None,
                       server_name=_label(info.get('name')), server_version=_label(info.get('version')))
        elif method == 'tools/list':
            tools = result.get('tools')
            if type(tools) is not list or not all(type(t) is dict and type(t.get('name')) is str for t in tools):
                self.event('tools_list', census=None, tools_sha256=None, next_page=False)
                return
            ordered = sorted(tools, key=lambda t: t['name'])
            digest = hashlib.sha256(_canonical(ordered)).hexdigest()
            self.event('tools_list', census=len(tools), tools_sha256=digest,
                       next_page=result.get('nextCursor') is not None)
        else:
            content = result.get('content') if type(result.get('content')) is list else []
            blocks = [{'type': block.get('type') if type(block) is dict and block.get('type') in BLOCK_TYPES
                       else 'other', 'bytes': len(_canonical(block))} for block in content[:MAX_BLOCKS]]
            self.event('tool_call', tool=tool, is_error=result.get('isError') is True, blocks=blocks,
                       blocks_total=len(content), structured=result.get('structuredContent') is not None)

    def finish(self, exit_status: int | None) -> None:
        with self._lock:
            if self._closed:
                return
            for method, tool in self._pending.values():
                if method == 'tools/call':
                    self.event('tool_call', tool=tool, outcome='unanswered')
            self._pending.clear()
            self._closed = True
            self.event('end', final=True, exit_status=exit_status, frames=dict(self._frames),
                       unparsed=self._unparsed, oversized=self._oversized, untracked=self._untracked,
                       methods={side: dict(sorted(counts.items())) for side, counts in self._methods.items()},
                       suppressed_events=self._suppressed)


class LineSplitter:
    """Yields whole newline-terminated lines up to MAX_FRAME bytes; reports longer ones once each."""

    def __init__(self, limit: int = MAX_FRAME):
        self._limit, self._buffer, self._skipping = limit, bytearray(), False

    def feed(self, chunk: bytes) -> Iterator[bytes | None]:
        start = 0
        while True:
            end = chunk.find(b'\n', start)
            if end < 0:
                break
            if self._skipping:
                self._skipping = False
            elif len(self._buffer) + end - start > self._limit:
                yield None
            else:
                self._buffer += chunk[start:end]
                yield bytes(self._buffer)
            self._buffer.clear()
            start = end + 1
        if not self._skipping:
            self._buffer += chunk[start:]
            if len(self._buffer) > self._limit:
                self._buffer.clear()
                self._skipping = True
                yield None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def pump(source: int, sink: int, side: str, recorder: Recorder) -> None:
    """Relay until EOF or a closed sink; parsing never alters or delays the forwarded bytes."""
    splitter = LineSplitter()
    forwarding = True
    while True:
        try:
            chunk = os.read(source, CHUNK)
        except OSError:
            return
        if not chunk:
            return
        if forwarding:
            try:
                _write_all(sink, chunk)
            except OSError:
                forwarding = False
        try:
            for line in splitter.feed(chunk):
                if line is None:
                    recorder.oversized(side)
                else:
                    recorder.frame(side, line)
        except Exception:  # pragma: no cover - recording must never stop the relay
            pass
        if not forwarding:
            return


def open_record(path: str) -> int:
    """A new owner-only file directly in an existing owner-only directory; never follows links."""
    if (type(path) is not str or not path.startswith('/') or len(path) > 1024 or '\0' in path
            or any(part in ('', '.', '..') for part in path[1:].split('/'))):
        raise RecorderRefusal('record_unavailable')
    parent, name = path.rsplit('/', 1)
    directory = None
    try:
        directory = os.open(parent or '/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RecorderRefusal('record_unavailable')
        return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND | os.O_CLOEXEC,
                       0o600, dir_fd=directory)
    except OSError:
        raise RecorderRefusal('record_unavailable') from None
    finally:
        if directory is not None:
            os.close(directory)


def parse_arguments(argv: list[str]) -> tuple[str, list[str]]:
    """Closed grammar: --record <absolute new file> -- <absolute server command> [arguments]."""
    if len(argv) < 4 or argv[0] != '--record' or argv[2] != '--' or len(argv) - 3 > MAX_ARGV:
        raise RecorderRefusal('recorder_usage')
    command = argv[3:]
    if not command[0].startswith('/') or any(type(a) is not str or '\0' in a for a in command):
        raise RecorderRefusal('recorder_usage')
    return argv[1], command


def run(argv: list[str], *, stdin: int = 0, stdout: int = 1) -> int:
    record_path, command = parse_arguments(argv)
    record = open_record(record_path)
    write_lock = threading.Lock()

    def emit(event: dict[str, object]) -> None:
        line = (json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
        with write_lock:
            try:
                _write_all(record, line)
            except OSError:
                pass

    recorder = Recorder(emit)
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
    except OSError:
        recorder.finish(None)
        os.close(record)
        raise RecorderRefusal('server_unavailable') from None

    def stop(_signal: int, _frame: object) -> None:
        if child.poll() is None:
            child.terminate()

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop)

    def client_side() -> None:
        pump(stdin, child.stdin.fileno(), 'client', recorder)
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.terminate()

    threading.Thread(target=client_side, daemon=True).start()
    pump(child.stdout.fileno(), stdout, 'server', recorder)
    try:
        status = child.wait(timeout=EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            status = child.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            status = child.wait(timeout=KILL_GRACE_SECONDS)
    recorder.finish(status)
    os.close(record)
    return status if 0 <= status <= 255 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except RecorderRefusal as refusal:
        print(json.dumps({'status': 'refused', 'code': refusal.code}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
