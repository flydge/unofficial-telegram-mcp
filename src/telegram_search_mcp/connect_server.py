"""Loopback connection screen: the ConnectUI that run_connect drives, served on 127.0.0.1 only.

Trust model: the page is served from an ephemeral loopback port and authorized by a random token
that lives only in the URL fragment (never sent to the server or a referrer); the page returns it
as a bearer token. Host and Origin are pinned to the exact loopback origin, so DNS rebinding and
cross-site posts fail before the token is even looked at. The QR link is rendered to an SVG data
URI at publish time and the link itself is not kept; the 2FA password is carried as a SecretValue
from the HTTP body to the run thread and is never stored elsewhere, echoed or logged. Responses
for refusals have empty bodies; nothing is written to stdout, stderr or any log.
"""
from __future__ import annotations
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import secrets
import socket
import socketserver
import threading
import time
import unicodedata
import segno
from .connect_flow import PHASES, STATUS_CODES, ConnectUI, valid_retry_after
from .connect_page import assets as page_assets
from .credentials import SecretValue

MAX_BODY = 4096  # Bytes of a POST body.
MAX_LINK = 256  # Bytes of a login link; the provider's links are far shorter.
MAX_HINT = 128  # Characters of the password hint shown on the page.
SOCKET_TIMEOUT = 10.0  # Per blocking read; a silent client is dropped after this.
REQUEST_DEADLINE = 15.0  # Absolute per connection; a client trickling bytes is cut off here.
MAX_CONNECTIONS = 16  # Concurrent; the page needs one or two. Extra connections are closed unanswered.
_DRAIN = 65536  # Unread request bytes consumed before refusing, so the client sees the status.
_SECURITY_HEADERS = (
    ('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    ('Cache-Control', 'no-store'),
    ('Referrer-Policy', 'no-referrer'),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
    ('Cross-Origin-Opener-Policy', 'same-origin'),
)
_ROUTES = {'/': 'GET', '/app.js': 'GET', '/app.css': 'GET', '/api/state': 'GET',
           '/api/password': 'POST', '/api/cancel': 'POST'}


def _render_qr(link):
    """SVG data URI of the link; the only text ever derived from it. Errors carry no link text."""
    try:
        if type(link) is not SecretValue:
            raise TypeError
        raw = link.reveal()
        if not 0 < len(raw) <= MAX_LINK:
            raise ValueError
        # Plain QR (no Micro symbols), error correction M, quiet zone and colours fixed for scanners.
        return segno.make(raw.decode('ascii'), error='m', micro=False).svg_data_uri(
            scale=1, border=4, dark='#000', light='#fff')
    except Exception:
        pass
    raise ValueError('invalid_link')  # Outside the handler: no chained exception can carry the link.


def _clean_hint(hint):
    """Plain text of bounded length: controls and format characters become spaces."""
    if type(hint) is not str:
        return ''
    text = ''.join(' ' if unicodedata.category(char).startswith('C') else char for char in hint)
    return ' '.join(text.split())[:MAX_HINT].strip()


class LoopbackConnectUI(ConnectUI):
    """Thread-safe state shared by the run thread (the ConnectUI calls) and the HTTP threads."""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._phase, self._code, self._retry_after = 'starting', None, None
        self._qr = self._hint = self._slot = None
        self._wanted = self._pending = self._cancelled = self._closed = False

    # ConnectUI ---------------------------------------------------------------------------

    def publish_qr(self, link: SecretValue) -> None:
        uri = _render_qr(link)  # Outside the lock; the link is not retained beyond this call.
        with self._cond:
            if not self._closed:
                self._qr = uri

    def request_password(self, hint: str, timeout: float) -> SecretValue | None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('invalid_timeout')
        shown, end = _clean_hint(hint), time.monotonic() + timeout
        with self._cond:
            if self._cancelled or self._closed:
                return None
            # A request stays open across the run's polling slices, so a post that lands between two
            # calls is kept for the next one; it ends on delivery, cancel, close or a phase change.
            self._wanted, self._hint = True, shown
            while self._wanted and self._slot is None and not (self._cancelled or self._closed):
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            secret, self._slot = self._slot, None
            if secret is None or self._cancelled or self._closed:
                return None
            self._wanted, self._hint = False, None
            return secret

    def cancelled(self) -> bool:
        with self._cond:
            return self._cancelled or self._closed  # A closed screen can no longer finish the run.

    def notify(self, phase: str, code: str | None, retry_after: int | None = None) -> None:
        if type(phase) is not str or phase not in PHASES or (code is not None and (
                type(code) is not str or code not in STATUS_CODES)) or not valid_retry_after(code, retry_after):
            raise ValueError('invalid_status')
        with self._cond:
            self._phase, self._code, self._retry_after = phase, code, retry_after
            if phase != 'waiting_for_password':
                self._wanted, self._hint, self._slot = False, None, None
            if phase not in ('starting', 'waiting_for_scan'):
                self._qr = None  # The link was consumed or abandoned; drop it with the QR.
            if phase != 'authorizing':
                self._pending = False
            self._cond.notify_all()

    # Page side ---------------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._cond:
            phase, code = self._phase, self._code
            state = {'phase': phase, 'code': code}
            if phase == 'waiting_for_scan' and self._qr is not None:
                state['qr'] = self._qr
            if phase == 'waiting_for_password' and self._hint:
                state['password_hint'] = self._hint
            state['password_error'] = phase == 'waiting_for_password' and code is not None
            state['password_pending'] = self._pending
            if phase == 'failed' and self._retry_after is not None:  # Only flood_wait carries one.
                state['retry_after'] = self._retry_after
            return state

    def accept_password(self, value: bytes) -> bool:
        """False when no password is being requested or one is already pending."""
        with self._cond:
            if self._closed or self._cancelled or not self._wanted or self._slot is not None:
                return False
            self._slot, self._pending = SecretValue(value), True
            self._cond.notify_all()
            return True

    def request_cancel(self) -> None:
        with self._cond:
            self._cancelled = True
            self._wanted, self._pending = False, False
            self._hint = self._slot = None
            self._cond.notify_all()

    def close(self) -> None:
        """Drop the QR, hint and any undelivered password; blocked callers return None."""
        with self._cond:
            self._closed = True
            self._qr = self._hint = self._slot = None
            self._wanted = self._pending = False
            self._cond.notify_all()


def _is_json_type(value):
    media, _, parameters = value.partition(';')
    return media.strip().lower() == 'application/json' and ''.join(parameters.split()).lower() in ('', 'charset=utf-8')


class _Handler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT
    _body_read = False
    _watchdog = None

    def setup(self):
        super().setup()
        # The socket timeout bounds each read only; this bounds the whole connection.
        connection = self.connection
        def cut():
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._watchdog = threading.Timer(REQUEST_DEADLINE, cut)
        self._watchdog.daemon = True
        self._watchdog.start()

    def finish(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # Protocol-level failures (bad request line, oversized headers, unknown method): empty body.
        self._send(405 if code == 501 else code)

    def _send(self, status, body=b'', content_type=None, extra=()):
        headers = [*_SECURITY_HEADERS, ('Content-Length', str(len(body))), ('Connection', 'close')]
        if content_type:
            headers.append(('Content-Type', content_type))
        headers.extend(extra)
        lines = ['HTTP/1.1 %d %s' % (status, HTTPStatus(status).phrase)] + ['%s: %s' % row for row in headers]
        self.close_connection = True
        self.wfile.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body)

    def _json(self, value):
        self._send(200, json.dumps(value, separators=(',', ':')).encode('ascii'), 'application/json')

    def _dispatch(self):
        try:
            self._route()
            self._drain_unread()
        except OSError:
            self.close_connection = True  # The peer went away or timed out; nothing to answer.
        except Exception:
            try:
                self._send(500)
            except Exception:
                self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    def _route(self):
        screen = self.server.screen
        if self.headers.get_all('Host') != [screen.host]:
            return self._send(421)  # DNS rebinding guard, before anything else is looked at.
        path = self.path.partition('?')[0]
        allowed = _ROUTES.get(path)
        if allowed is None:
            return self._send(404)
        if self.command != allowed:
            return self._send(405, extra=[('Allow', allowed)])
        if allowed == 'GET' and path in screen.assets:
            content_type, body = screen.assets[path]
            return self._send(200, body, content_type)
        if allowed == 'POST' and self.headers.get_all('Origin') != [screen.origin]:
            return self._send(403)
        if not screen.authorized(self.headers.get_all('Authorization')):
            return self._send(401)
        if path == '/api/state':
            return self._json(screen.state())
        body = self._read_body()
        if body is None:
            return
        try:
            data = json.loads(body.decode('utf-8')) if body else None
        except (ValueError, RecursionError):
            return self._send(400)
        if path == '/api/cancel':
            if data is not None and type(data) is not dict:
                return self._send(400)
            screen.ui.request_cancel()
            return self._json({'ok': True})
        try:
            if type(data) is not dict or set(data) != {'password'} or type(data['password']) is not str:
                return self._send(400)
            try:
                raw = data['password'].encode('utf-8')
            except UnicodeEncodeError:  # A lone surrogate escape in the JSON string.
                return self._send(400)
            if not 0 < len(raw) <= MAX_BODY:
                return self._send(400)
            accepted = screen.ui.accept_password(raw)
        finally:
            data = raw = body = None
        if not accepted:
            return self._send(409)
        self._json({'ok': True})

    def _read_body(self):
        """Bytes of a bounded JSON POST body, or None after the refusal has been sent."""
        types = self.headers.get_all('Content-Type')
        if types is None or len(types) != 1 or not _is_json_type(types[0]):
            self._send(415)
            return None
        lengths = self.headers.get_all('Content-Length')
        if self.headers.get('Transfer-Encoding') is not None or lengths is None or len(lengths) != 1:
            self._send(411)
            return None
        text = lengths[0].strip()
        if not (text.isascii() and text.isdigit() and len(text) <= 10):
            self._send(400)
            return None
        size = int(text)
        if size > MAX_BODY:
            self._send(413)
            return None
        body = self.rfile.read(size)
        if len(body) != size:
            self._send(400)
            return None
        self._body_read = True
        return body

    def _drain_unread(self):
        """After the answer is out: consume a declared body we refused, so closing the socket cannot
        reset the connection before the client has read the status."""
        if self._body_read:
            return
        text = (self.headers.get('Content-Length') or '').strip()
        remaining = min(int(text), _DRAIN) if text.isascii() and text.isdigit() and len(text) <= 10 else 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                break
            remaining -= len(chunk)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    # Pinned: SO_REUSEPORT would let another local socket share the port and receive the token.
    allow_reuse_address = False
    allow_reuse_port = False

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # Over the cap: close unanswered, no thread.
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # Skips HTTPServer's reverse-DNS lookup of the host.
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    def handle_error(self, request, client_address):
        pass  # The default prints a traceback to stderr.


class ConnectServer:
    """Owns the listening socket and the token; `start()` returns the one URL that carries it."""

    def __init__(self, ui: LoopbackConnectUI, expires_at: float, *, clock=time.time, alias=None):
        """`alias` names the registered account alias whose page this serves; None serves the default's."""
        if (not isinstance(ui, LoopbackConnectUI) or type(expires_at) not in (int, float)
                or not math.isfinite(expires_at) or not callable(clock)):
            raise ValueError('invalid_input')
        self.assets = page_assets(alias)  # Refuses anything but None or a named alias's closed token.
        self.ui, self._expires_at, self._clock = ui, expires_at, clock
        self._token = secrets.token_urlsafe(32).encode('ascii')
        self._lock = threading.Lock()
        self._httpd = self._thread = None
        self._closed = False
        self.host = self.origin = ''

    def __repr__(self):
        return 'ConnectServer(<redacted>)'

    def start(self) -> str:
        with self._lock:
            if self._closed or self._httpd is not None:
                raise RuntimeError('invalid_state')
            httpd = _Server(('127.0.0.1', 0), _Handler)
            httpd.screen = self
            self.host = '127.0.0.1:%d' % httpd.server_address[1]
            self.origin = 'http://' + self.host
            thread = threading.Thread(target=self._serve, args=(httpd,), name='connect-screen', daemon=True)
            self._httpd, self._thread = httpd, thread
            thread.start()
            return '%s/#t=%s' % (self.origin, self._token.decode('ascii'))

    @staticmethod
    def _serve(httpd):
        try:
            httpd.serve_forever(poll_interval=0.1)
        except Exception:
            pass

    def authorized(self, values) -> bool:
        if values is None or len(values) != 1 or not values[0].startswith('Bearer '):
            return False
        return hmac.compare_digest(values[0][7:].encode('latin-1', 'replace'), self._token)

    def state(self) -> dict:
        state = self.ui.snapshot()
        left = max(0, math.ceil(self._expires_at - self._clock()))
        ordered = {'phase': state.pop('phase'), 'code': state.pop('code'), 'expires_in': left}
        ordered.update(state)
        return ordered

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            httpd, thread = self._httpd, self._thread
        self.ui.close()
        if httpd is not None:
            httpd.shutdown()
            thread.join(5)
            httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
