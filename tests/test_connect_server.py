"""Loopback connection screen: raw HTTP guards, UI semantics, and leakage; every link and password is synthetic."""
import base64
import http.client
import json
import logging
import re
import socket
import threading
import time
import unittest
from unittest import mock
from urllib.parse import unquote
import xml.etree.ElementTree as ElementTree

from test_connect_flow import (ConnectHarness, captured_process_output, other_device, password_state, PHONE, READY,
                               LINK as FLOW_LINK, NEXT_LINK as FLOW_NEXT_LINK, PASSWORD as FLOW_PASSWORD)

from telegram_search_mcp import connect_page, connect_server
from telegram_search_mcp.connect_flow import PHASES, ConnectUI
from telegram_search_mcp.connect_server import (MAX_BODY, ConnectServer, LoopbackConnectUI, SOCKET_TIMEOUT)
from telegram_search_mcp.credentials import SecretValue

LINK = 'tg://login?token=' + 'Z' * 40 + 'LEAKCANARYLINK'
NEXT_LINK = 'tg://login?token=' + 'Y' * 43
PASSWORD = 'LEAKCANARYPASSWORD-9d1 wörd ✓'
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def secret(text):
    return SecretValue(text.encode('utf-8'))


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)


class ScreenCase(unittest.TestCase):
    """A started server per test; every response is checked for the fixed headers and no CORS."""

    expires_in = 600

    def setUp(self):
        self.ui = LoopbackConnectUI()
        self.server = ConnectServer(self.ui, time.time() + self.expires_in)
        self.url = self.server.start()
        self.addCleanup(self.server.close)
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/#t=([A-Za-z0-9_-]{43})', self.url)
        self.assertIsNotNone(match)
        self.port, self.token = int(match.group(1)), match.group(2)
        self.host, self.origin = '127.0.0.1:%d' % self.port, 'http://127.0.0.1:%d' % self.port
        self.log = []

    def call(self, method, path, body=None, *, token=True, host=True, origin=None, ctype=None, headers=(),
             length=None, port=None):
        """One raw request. host/token True = correct, None = absent, str = that value."""
        if body is not None and ctype is None:
            ctype = 'application/json'
        if method == 'POST' and origin is None:
            origin = True
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                connection.putheader('Host', self.host if host is True else host)
            if token is not None:
                connection.putheader('Authorization', 'Bearer ' + (self.token if token is True else token))
            if origin not in (None, False):
                connection.putheader('Origin', self.origin if origin is True else origin)
            if ctype not in (None, False):
                connection.putheader('Content-Type', ctype)
            if length is not False and (body is not None or length is not None):
                connection.putheader('Content-Length', str(len(body) if length is None else length))
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            answer = connection.getresponse()
            response = Response(answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read())
        finally:
            connection.close()
        self.assert_headers(response)
        self.log.append(response)
        return response

    def assert_headers(self, response):
        headers = response.headers
        self.assertEqual(headers.get('content-security-policy'), CSP)
        self.assertEqual(headers.get('cache-control'), 'no-store')
        self.assertEqual(headers.get('referrer-policy'), 'no-referrer')
        self.assertEqual(headers.get('x-content-type-options'), 'nosniff')
        self.assertEqual(headers.get('x-frame-options'), 'DENY')
        self.assertEqual([name for name in headers if name.startswith('access-control-')], [])

    def refused(self, response, status):
        self.assertEqual(response.status, status)
        self.assertEqual(response.body, b'')

    def state(self):
        response = self.call('GET', '/api/state')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['content-type'], 'application/json')
        return response.json()

    def wait_state(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.01)
        self.fail('state never matched; last: %r' % {k: v for k, v in state.items() if k != 'qr'})

    def post_password(self, text, **kwargs):
        return self.call('POST', '/api/password', json.dumps({'password': text}).encode('utf-8'), **kwargs)

    def request_in_thread(self, hint='cat', timeout=5):
        box = []
        thread = threading.Thread(target=lambda: box.append(self.ui.request_password(hint, timeout)), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread, box


class RequestGuardTests(ScreenCase):
    def test_url_shape_bind_and_fresh_token_per_server(self):
        self.assertEqual(self.server._httpd.server_address[0], '127.0.0.1')
        other = ConnectServer(LoopbackConnectUI(), time.time() + 60)
        self.addCleanup(other.close)
        other_url = other.start()
        self.assertNotEqual(other_url.split('#t=')[1], self.token)
        self.assertNotIn(self.token, repr(self.server))
        self.assertNotIn(self.token, repr(self.ui))

    def test_wrong_or_missing_host_is_refused_on_every_route(self):
        hosts = [None, 'localhost:%d' % self.port, '127.0.0.1', '127.0.0.1:1', 'evil.example:%d' % self.port,
                 '127.0.0.1:%d.evil.example' % self.port, '127.0.0.1:%d ' % self.port, '[::1]:%d' % self.port]
        routes = [('GET', '/'), ('GET', '/app.js'), ('GET', '/app.css'), ('GET', '/api/state'),
                  ('GET', '/nowhere'), ('POST', '/api/cancel'), ('POST', '/api/password')]
        for host in hosts:
            for method, path in routes:
                with self.subTest(host=host, route=path):
                    body = b'{}' if method == 'POST' else None
                    self.refused(self.call(method, path, body, host=host), 421)
        self.assertFalse(self.ui.cancelled())

    def test_duplicate_host_header_is_refused(self):
        self.refused(self.call('GET', '/', headers=[('Host', self.host)]), 421)

    def test_post_origin_must_match_exactly(self):
        origins = [False, 'http://localhost:%d' % self.port, 'http://127.0.0.1', 'http://127.0.0.1:1',
                   'https://127.0.0.1:%d' % self.port, 'null', self.origin + '/', 'http://evil.example']
        for origin in origins:
            for path, body in [('/api/cancel', b'{}'), ('/api/password', b'{"password":"x"}')]:
                with self.subTest(origin=origin, path=path):
                    self.refused(self.call('POST', path, body, origin=origin), 403)
        self.assertFalse(self.ui.cancelled())

    def test_token_is_required_for_api_but_not_for_assets(self):
        bad = [None, '', 'x', self.token + 'x', self.token[:-1], self.token.swapcase(), 'Bearer ' + self.token]
        for token in bad:
            for method, path, body in [('GET', '/api/state', None), ('POST', '/api/cancel', b'{}'),
                                       ('POST', '/api/password', b'{"password":"x"}')]:
                with self.subTest(token=token, route=path):
                    self.refused(self.call(method, path, body, token=token), 401)
        self.assertFalse(self.ui.cancelled())
        self.assertEqual(self.state()['phase'], 'starting')

    def test_authorization_scheme_must_be_bearer(self):
        for value in ['Basic ' + base64.b64encode(self.token.encode()).decode(), self.token, 'Token ' + self.token]:
            with self.subTest(value=value):
                self.refused(self.call('GET', '/api/state', token=None, headers=[('Authorization', value)]), 401)
        self.refused(self.call('GET', '/api/state', headers=[('Authorization', 'Bearer ' + self.token)]), 401)

    def test_content_type_must_be_json(self):
        for ctype in [False, 'text/plain', 'application/x-www-form-urlencoded', 'application/jsonx',
                      'application/json; charset=latin-1', 'multipart/form-data; boundary=x', '']:
            with self.subTest(ctype=ctype):
                self.refused(self.call('POST', '/api/password', b'{"password":"x"}', ctype=ctype or False), 415)
        for ctype in ['application/json', 'application/json; charset=utf-8', 'Application/JSON;charset=UTF-8']:
            with self.subTest(ctype=ctype):  # Passes validation; nothing is waiting, so the conflict answers.
                self.refused(self.post_password('x', ctype=ctype), 409)

    def test_body_size_and_length_rules(self):
        exact = json.dumps({'password': 'a' * (MAX_BODY - 15)}, separators=(',', ':')).encode()
        self.assertEqual(len(exact), MAX_BODY)
        self.refused(self.call('POST', '/api/password', exact), 409)
        for size in (MAX_BODY + 1, 5000, 60000):
            with self.subTest(size=size):
                self.refused(self.call('POST', '/api/password', b'{"password":"' + b'a' * size + b'"}'), 413)
        self.refused(self.call('POST', '/api/password', None, ctype='application/json'), 411)
        self.refused(self.call('POST', '/api/password', None, ctype='application/json',
                               headers=[('Transfer-Encoding', 'chunked')]), 411)
        self.refused(self.call('POST', '/api/password', None, length=None, headers=[
            ('Content-Length', '2'), ('Content-Length', '2')], ctype='application/json'), 411)
        for bad in ('abc', '-1', '1e3', '\u00b2', '1' * 11):
            with self.subTest(length=bad):
                self.refused(self.call('POST', '/api/password', None, ctype='application/json',
                                       headers=[('Content-Length', bad)]), 400)
        self.assertFalse(self.ui.cancelled())

    def test_malformed_json_bodies_are_refused_without_echo(self):
        bodies = [b'', b'not json', b'[]', b'"password"', b'{}', b'{"password":1}', b'{"password":null}',
                  b'{"password":""}', b'{"password":"x","extra":1}', b'{"Password":"x"}', b'{"password":"\\ud800"}',
                  b'{"password":"\xff\xfe"}', b'[' * 4000]
        for body in bodies:
            with self.subTest(body=body[:30]):
                self.refused(self.call('POST', '/api/password', body, ctype='application/json'), 400)

    def test_unknown_paths_are_404(self):
        paths = ['/x', '/index.html', '/api', '/api/', '/api/state/', '/favicon.ico', '/../etc/passwd', '/app.js/',
                 '/API/state', '/api/state%2F', '/%61pp.js', '/api/qr', 'http://127.0.0.1/', '*']
        for path in paths:
            for token in (True, None):
                with self.subTest(path=path, token=token):
                    self.refused(self.call('GET', path, token=token), 404)
                    self.refused(self.call('POST', path, b'{}', token=token), 404)

    def test_wrong_method_is_405_and_never_grants_cors(self):
        wrong = {'/': ['POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'FOO'],
                 '/app.js': ['POST', 'PUT'], '/app.css': ['POST', 'DELETE'],
                 '/api/state': ['POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'],
                 '/api/password': ['GET', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'],
                 '/api/cancel': ['GET', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']}
        for path, methods in wrong.items():
            for method in methods:
                with self.subTest(path=path, method=method):
                    response = self.call(method, path, headers=[('Access-Control-Request-Method', 'POST'),
                                                               ('Origin', 'http://evil.example')])
                    self.refused(response, 405)
        self.assertFalse(self.ui.cancelled())

    def test_every_answer_carries_the_fixed_headers(self):
        # call() asserts them on each response; this pins the status classes it covers.
        statuses = {self.call('GET', '/').status, self.call('GET', '/api/state').status,
                    self.call('POST', '/api/cancel', b'{}').status, self.call('GET', '/nope').status,
                    self.call('GET', '/api/state', token=None).status, self.call('GET', '/', host=None).status,
                    self.call('POST', '/api/cancel', b'{}', origin='null').status, self.call('PUT', '/').status}
        self.assertEqual(statuses, {200, 404, 401, 421, 403, 405})

    def test_handler_failure_answers_500_with_no_body_and_prints_nothing(self):
        def boom():
            raise RuntimeError('state failed for ' + LINK)
        excepted = []
        with mock.patch.object(self.ui, 'snapshot', boom), mock.patch.object(threading, 'excepthook', excepted.append):
            with captured_process_output() as output:
                response = self.call('GET', '/api/state')
        self.refused(response, 500)
        self.assertEqual(excepted, [])
        self.assertEqual(list(output), [b'', b'', b'', b''])

    def test_protocol_errors_get_empty_bodies_and_no_output(self):
        def raw(payload):
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as stream:
                stream.sendall(payload)
                stream.shutdown(socket.SHUT_WR)
                data = b''
                try:
                    while True:
                        chunk = stream.recv(4096)
                        if not chunk:
                            return data
                        data += chunk
                except ConnectionResetError:  # Unread junk at close can reset the connection; fine.
                    return data
        probes = [b'\x00\xff garbage\r\n\r\n', b'GET /' + b'a' * 70000 + b' HTTP/1.1\r\n\r\n',
                  b'GET / HTTP/1.1\r\n' + b'X-A: b\r\n' * 150 + b'\r\n']
        with captured_process_output() as output:
            answers = [raw(probe) for probe in probes]
        self.assertEqual(list(output), [b'', b'', b'', b''])
        self.assertTrue(any(answers))
        for answer in filter(None, answers):
            head, _, body = answer.partition(b'\r\n\r\n')
            self.assertRegex(head, rb'^HTTP/1\.1 4\d\d ')
            self.assertEqual(body, b'')
            self.assertIn(CSP.encode(), head)
            self.assertIn(b'no-store', head)

    def test_reset_connections_are_silent(self):
        # A peer reset surfaces from readline outside the handler's own try; the default hook would print it.
        import struct
        with captured_process_output() as output:
            for _ in range(5):
                stream = socket.create_connection(('127.0.0.1', self.port), timeout=5)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                stream.sendall(b'GET /api/st')
                stream.close()  # RST, not FIN
            time.sleep(0.3)
            self.assertEqual(self.state()['phase'], 'starting')  # Still serving.
        self.assertEqual(list(output), [b'', b'', b'', b''])

    def test_stalled_client_is_dropped_by_the_socket_timeout(self):
        self.assertEqual(SOCKET_TIMEOUT, 10.0)
        self.assertEqual(connect_server._Handler.timeout, SOCKET_TIMEOUT)
        with mock.patch.object(connect_server._Handler, 'timeout', 0.3):
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as stream:
                stream.sendall(b'GET / HT')
                started = time.monotonic()
                data = stream.recv(4096)  # b'' once the server hangs up; never blocks for the default.
        self.assertEqual(data, b'')
        self.assertLess(time.monotonic() - started, 3)

    def test_post_to_cancel_accepts_empty_and_object_bodies_only(self):
        self.refused(self.call('POST', '/api/cancel', b'[]'), 400)
        self.refused(self.call('POST', '/api/cancel', b'nope'), 400)
        self.assertFalse(self.ui.cancelled())
        self.assertEqual(self.call('POST', '/api/cancel', b'{}').json(), {'ok': True})
        self.assertTrue(self.ui.cancelled())

    def test_empty_cancel_body_is_accepted(self):
        response = self.call('POST', '/api/cancel', b'', ctype='application/json')
        self.assertEqual(response.status, 200)
        self.assertTrue(self.ui.cancelled())


class StaticAssetTests(ScreenCase):
    def assets(self):
        return {path: self.call('GET', path, token=None) for path in ('/', '/app.js', '/app.css')}

    def test_assets_need_no_token_and_carry_no_secret(self):
        assets = self.assets()
        types = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8',
                 '/app.css': 'text/css; charset=utf-8'}
        self.ui.notify('waiting_for_scan', None)
        self.ui.publish_qr(secret(LINK))
        for path, response in self.assets().items():
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['content-type'], types[path])
            self.assertEqual(response.body, assets[path].body)  # Nothing per run, nothing state-dependent.
            for canary in (self.token, LINK, 'LEAKCANARY'):
                self.assertNotIn(canary.encode(), response.body)
        other = ConnectServer(LoopbackConnectUI(), time.time() + 60)
        self.addCleanup(other.close)
        other.start()
        again = http.client.HTTPConnection('127.0.0.1', int(other.host.split(':')[1]), timeout=5)
        again.request('GET', '/app.js', headers={'Host': other.host})
        self.assertEqual(again.getresponse().read(), assets['/app.js'].body)
        again.close()

    def test_markup_satisfies_the_content_security_policy(self):
        page = {path: response.body.decode('utf-8') for path, response in self.assets().items()}
        html = page['/']
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)')
        self.assertNotRegex(html, r'\son[a-z]+\s*=')  # No inline handlers.
        self.assertNotRegex(html, r'\sstyle\s*=')
        self.assertNotIn('<style', html)
        self.assertIn('<script src="/app.js" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/app.css">', html)
        for path, text in page.items():
            self.assertNotRegex(text, r'https?://', path)  # No external fonts or assets.
            self.assertNotRegex(text, r'@import|url\(\s*[\'"]?(?!data:)', path)
        self.assertNotIn('innerHTML', page['/app.js'])  # Hint and codes render as text.
        self.assertNotIn('eval(', page['/app.js'])
        self.assertNotIn('document.write', page['/app.js'])

    def test_page_content_matches_the_product_contract(self):
        html = self.assets()['/'].body.decode('utf-8')
        js = self.assets()['/app.js'].body.decode('utf-8')
        self.assertIn('<title>Unofficial Telegram MCP</title>', html)
        self.assertIn('Uses the Telegram API. Not an official Telegram app.', html)
        self.assertIn('type="password"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertRegex(html, r'<label for="pw-input"')
        self.assertNotRegex(html, r'(?i)logo|\.svg|\.png')
        self.assertIn('Использует Telegram API. Не является официальным приложением Telegram.', js)
        self.assertRegex(js, r"navigator\.language[^;]*indexOf\('ru'\) === 0")
        self.assertIn('history.replaceState', js)
        self.assertIn("'Bearer ' + token", js)
        self.assertIn('/api/state', js)
        self.assertIn('Connected. You can close this tab and return to your assistant.', js)
        for step in ('Open Telegram on your phone', 'Settings → Devices → Link Desktop Device',
                     'Scan this code'):
            self.assertIn(step, html)

    def test_both_languages_define_every_string(self):
        self.assertEqual(set(connect_page.STRINGS['en']), set(connect_page.STRINGS['ru']))
        for language, strings in connect_page.STRINGS.items():
            for key, value in strings.items():
                self.assertTrue(value.strip(), (language, key))
        for key in re.findall(r'data-i18n="(\w+)"', connect_page.INDEX_HTML):
            self.assertIn(key, connect_page.STRINGS['en'])
        for key in re.findall(r'\bT\.(\w+)', connect_page.APP_JS):
            self.assertIn(key, connect_page.STRINGS['ru'], key)
        keys = set()
        for arguments in re.findall(r"\b(?:end|wait)\(([^)]*)\)", connect_page.APP_JS):
            keys.update(re.findall(r"'(\w+)'", arguments))
        self.assertTrue({'readyTitle', 'closedText', 'finishing'} <= keys)
        for key in keys - {'ok', 'bad', 'idle'}:
            self.assertIn(key, connect_page.STRINGS['en'], key)


class StateTests(ScreenCase):
    def test_initial_state(self):
        state = self.state()
        self.assertEqual(list(state), ['phase', 'code', 'expires_in', 'password_error', 'password_pending'])
        self.assertEqual((state['phase'], state['code']), ('starting', None))
        self.assertIs(type(state['expires_in']), int)
        self.assertIn(state['expires_in'], range(597, 601))
        self.assertEqual((state['password_error'], state['password_pending']), (False, False))

    def test_expires_in_is_derived_from_the_given_expiry(self):
        now = [1000.0]
        server = ConnectServer(LoopbackConnectUI(), 1060.2, clock=lambda: now[0])
        self.addCleanup(server.close)
        server.start()
        for clock, expected in [(1000.0, 61), (1059.5, 1), (1060.2, 0), (1090.0, 0)]:
            now[0] = clock
            self.assertEqual(server.state()['expires_in'], expected)
        for bad in (None, 'soon', float('nan'), float('inf'), True):
            with self.subTest(expires_at=bad), self.assertRaises(ValueError):
                ConnectServer(LoopbackConnectUI(), bad)
        with self.assertRaises(ValueError):
            ConnectServer(object(), 1.0)

    def test_phases_and_codes_follow_notify(self):
        for phase in PHASES:
            self.ui.notify(phase, None)
            self.assertEqual(self.state()['phase'], phase)
        self.ui.notify('failed', 'password_submission_uncertain')
        state = self.state()
        self.assertEqual((state['phase'], state['code']), ('failed', 'password_submission_uncertain'))
        self.ui.notify('expired', 'expired')
        self.assertEqual(self.state()['code'], 'expired')

    def test_notify_rejects_anything_outside_the_fixed_grammar(self):
        self.ui.notify('waiting_for_scan', None)
        for phase, code in [('nope', None), (None, None), ('failed', 'free text ' + LINK), ('failed', 5),
                            ('ready', ['x']), (['failed'], None)]:
            with self.subTest(phase=phase, code=code):
                with self.assertRaises(ValueError) as caught:
                    self.ui.notify(phase, code)
                self.assertEqual(str(caught.exception), 'invalid_status')
        state = self.state()
        self.assertEqual((state['phase'], state['code']), ('waiting_for_scan', None))

    def test_qr_appears_only_while_waiting_for_scan(self):
        self.ui.publish_qr(secret(LINK))  # run_connect publishes before it reports the phase
        self.assertNotIn('qr', self.state())
        self.ui.notify('waiting_for_scan', None)
        uri = self.state()['qr']
        self.assertTrue(uri.startswith('data:image/svg+xml;charset=utf-8,'))
        svg = ElementTree.fromstring(unquote(uri.partition(',')[2]))
        self.assertTrue(svg.tag.endswith('svg'))
        for phase in ('waiting_for_password', 'authorizing', 'handing_off', 'ready'):
            self.ui.notify(phase, None)
            self.assertNotIn('qr', self.state())
        self.ui.notify('waiting_for_scan', None)  # Dropped for good, not merely hidden.
        self.assertNotIn('qr', self.state())

    def test_qr_encodes_the_link_with_error_correction_m_and_a_quiet_zone(self):
        import segno
        self.ui.notify('waiting_for_scan', None)
        self.ui.publish_qr(secret(LINK))
        uri = self.state()['qr']
        expected = segno.make(LINK, error='m', micro=False)
        svg = unquote(uri.partition(',')[2])
        side = int(re.search(r"width='(\d+)'", svg).group(1))
        self.assertEqual(side, expected.symbol_size(scale=1, border=4)[0])
        self.assertIn("fill='#fff'", svg)
        self.assertEqual(uri, expected.svg_data_uri(scale=1, border=4, dark='#000', light='#fff'))

    def test_publish_replaces_the_qr_and_drops_the_old_one(self):
        self.ui.notify('waiting_for_scan', None)
        self.ui.publish_qr(secret(LINK))
        first = self.state()['qr']
        self.ui.publish_qr(secret(NEXT_LINK))
        second = self.state()['qr']
        self.assertNotEqual(first, second)
        for _ in range(3):
            self.assertEqual(self.state()['qr'], second)
        self.assertEqual(self.ui.snapshot()['qr'], second)

    def test_publish_rejects_bad_links_without_echo_and_keeps_the_current_qr(self):
        self.ui.notify('waiting_for_scan', None)
        self.ui.publish_qr(secret(LINK))
        current = self.state()['qr']
        for bad in [LINK, b'tg://login?token=' + b'A' * 300 + b'LEAKCANARY', SecretValue('tg://é'.encode()),
                    None, 12]:
            with self.subTest(bad=type(bad)):
                with self.assertRaises(ValueError) as caught:
                    self.ui.publish_qr(bad if not isinstance(bad, bytes) else SecretValue(bad))
                self.assertEqual(str(caught.exception), 'invalid_link')
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.state()['qr'], current)

    def test_password_hint_is_text_and_only_while_waiting_for_password(self):
        self.ui.notify('waiting_for_password', None)
        thread, box = self.request_in_thread('<b>my\nhi‮nt</b> &amp; ' + 'x' * 200, timeout=0.3)
        state = self.wait_state(lambda s: 'password_hint' in s)
        self.assertTrue(state['password_hint'].startswith('<b>my hi nt</b> &amp; x'))
        self.assertEqual(len(state['password_hint']), 128)
        self.assertNotRegex(state['password_hint'], r'[\x00-\x1f‮]')
        thread.join(5)
        self.ui.notify('authorizing', None)
        self.assertNotIn('password_hint', self.state())

    def test_state_is_consistent_under_concurrent_polling(self):
        errors = []
        stop = threading.Event()
        def poll():
            while not stop.is_set():
                try:
                    state = self.state()
                    if 'qr' in state and state['phase'] != 'waiting_for_scan':
                        errors.append(state['phase'])
                except Exception as error:  # pragma: no cover - failure path
                    errors.append(repr(error))
        pollers = [threading.Thread(target=poll, daemon=True) for _ in range(4)]
        for poller in pollers:
            poller.start()
        for index in range(25):
            self.ui.notify('waiting_for_scan', None)
            self.ui.publish_qr(secret(LINK if index % 2 else NEXT_LINK))
            self.ui.notify('waiting_for_password', None)
        stop.set()
        for poller in pollers:
            poller.join(5)
        self.assertEqual(errors, [])


class PasswordTests(ScreenCase):
    def test_posted_password_reaches_the_run_thread_as_a_secret_value(self):
        self.ui.notify('waiting_for_password', None)
        thread, box = self.request_in_thread('hint')
        self.wait_state(lambda s: s.get('password_hint') == 'hint')
        response = self.post_password(PASSWORD)
        self.assertEqual((response.status, response.json()), (200, {'ok': True}))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIs(type(box[0]), SecretValue)
        self.assertEqual(box[0].reveal(), PASSWORD.encode('utf-8'))
        self.assertNotIn('LEAKCANARY', repr(box[0]))
        self.assertEqual(response.body, b'{"ok":true}')

    def test_request_times_out_to_none(self):
        started = time.monotonic()
        self.assertIsNone(self.ui.request_password('hint', 0.15))
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(self.ui.request_password('hint', 0))

    def test_invalid_timeouts_are_rejected(self):
        for bad in (-1, float('nan'), float('inf'), '1', None, True):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                self.ui.request_password('hint', bad)

    def test_cancel_returns_none_promptly_and_stays_cancelled(self):
        self.ui.notify('waiting_for_password', None)
        thread, box = self.request_in_thread(timeout=30)
        self.wait_state(lambda s: 'password_hint' in s)
        self.assertFalse(self.ui.cancelled())
        started = time.monotonic()
        self.assertEqual(self.call('POST', '/api/cancel', b'{}').status, 200)
        thread.join(5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(box, [None])
        self.assertTrue(self.ui.cancelled())
        self.assertIsNone(self.ui.request_password('hint', 5))
        self.refused(self.post_password('late'), 409)
        self.assertEqual(self.call('POST', '/api/cancel', b'{}').status, 200)  # Idempotent.

    def test_close_and_phase_change_release_a_blocked_request(self):
        for release in ('close', 'phase'):
            with self.subTest(release=release):
                ui = LoopbackConnectUI()
                box = []
                thread = threading.Thread(target=lambda: box.append(ui.request_password('h', 30)), daemon=True)
                thread.start()
                end = time.monotonic() + 5
                while not ui._wanted and time.monotonic() < end:  # The request is open and blocked.
                    time.sleep(0.005)
                started = time.monotonic()
                ui.close() if release == 'close' else ui.notify('failed', 'timeout')
                thread.join(5)
                self.assertEqual(box, [None])
                self.assertLess(time.monotonic() - started, 3)

    def test_password_without_a_request_is_a_conflict(self):
        self.refused(self.post_password('early'), 409)
        self.ui.notify('waiting_for_password', None)  # The phase alone is not a request.
        self.refused(self.post_password('early'), 409)
        self.assertFalse(self.state()['password_pending'])

    def test_second_password_while_one_is_pending_is_a_conflict(self):
        self.ui.notify('waiting_for_password', None)
        thread, box = self.request_in_thread(timeout=5)
        self.wait_state(lambda s: 'password_hint' in s)
        self.assertEqual(self.post_password('first').status, 200)
        self.refused(self.post_password('second'), 409)
        thread.join(5)
        self.refused(self.post_password('third'), 409)  # Delivered; nothing is waiting any more.
        self.assertEqual(box[0].reveal(), b'first')

    def test_posted_between_slices_is_kept_for_the_next_request(self):
        self.ui.notify('waiting_for_password', None)
        self.assertIsNone(self.ui.request_password('hint', 0.05))  # A slice ends with no password.
        self.assertEqual(self.post_password('gap').status, 200)
        self.assertEqual(self.ui.request_password('hint', 0.05).reveal(), b'gap')
        self.refused(self.post_password('again'), 409)

    def test_error_and_pending_flags_follow_the_flow(self):
        notice = self.ui.notify
        notice('waiting_for_password', None)
        thread, box = self.request_in_thread()
        state = self.wait_state(lambda s: 'password_hint' in s)
        self.assertEqual((state['password_error'], state['password_pending']), (False, False))
        self.assertEqual(self.post_password('one').status, 200)
        state = self.state()
        self.assertEqual((state['phase'], state['password_pending']), ('waiting_for_password', True))
        thread.join(5)
        notice('authorizing', None)
        state = self.state()
        self.assertEqual((state['phase'], state['password_pending'], state['password_error']),
                         ('authorizing', True, False))
        self.assertNotIn('password_hint', state)
        notice('waiting_for_password', 'invalid_password')
        thread, box = self.request_in_thread()
        state = self.wait_state(lambda s: 'password_hint' in s)
        self.assertEqual((state['code'], state['password_error'], state['password_pending']),
                         ('invalid_password', True, False))
        self.assertEqual(self.post_password('two').status, 200)
        self.assertTrue(self.state()['password_pending'])
        thread.join(5)
        self.assertEqual(box[0].reveal(), b'two')
        notice('authorizing', None)
        notice('handing_off', None)
        state = self.state()
        self.assertEqual((state['password_pending'], state['password_error']), (False, False))
        notice('ready', None)
        self.assertEqual(self.state()['phase'], 'ready')

    def test_a_password_left_undelivered_is_dropped_when_the_phase_moves_on(self):
        self.ui.notify('waiting_for_password', None)
        self.assertIsNone(self.ui.request_password('hint', 0.05))
        self.assertEqual(self.post_password('orphan').status, 200)
        self.ui.notify('failed', 'timeout')
        self.assertIsNone(self.ui.request_password('hint', 0.05))
        self.assertFalse(self.state()['password_pending'])

    def test_implements_the_connect_ui_port(self):
        self.assertIsInstance(self.ui, ConnectUI)
        self.assertFalse(self.ui.cancelled())


class CloseTests(unittest.TestCase):
    def test_close_stops_the_server_and_the_port_refuses_connections(self):
        ui = LoopbackConnectUI()
        server = ConnectServer(ui, time.time() + 60)
        url = server.start()
        port = int(re.search(r':(\d+)/', url).group(1))
        with socket.create_connection(('127.0.0.1', port), timeout=5):
            pass
        ui.notify('waiting_for_scan', None)
        ui.publish_qr(secret(LINK))
        self.assertIn('qr', ui.snapshot())
        server.close()
        self.assertFalse(server._thread.is_alive())
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', port), timeout=5)
        self.assertNotIn('qr', ui.snapshot())  # The link is dropped with the server.
        ui.publish_qr(secret(NEXT_LINK))
        self.assertNotIn('qr', ui.snapshot())
        self.assertIsNone(ui.request_password('hint', 0.1))
        server.close()  # Idempotent.
        with self.assertRaises(RuntimeError):
            server.start()

    def test_close_before_start_and_context_manager(self):
        server = ConnectServer(LoopbackConnectUI(), time.time() + 60)
        server.close()
        with self.assertRaises(RuntimeError):
            server.start()
        with ConnectServer(LoopbackConnectUI(), time.time() + 60) as managed:
            managed.start()
            with self.assertRaises(RuntimeError):
                managed.start()
        self.assertFalse(managed._thread.is_alive())

    def test_close_returns_even_with_a_stalled_connection_open(self):
        server = ConnectServer(LoopbackConnectUI(), time.time() + 60)
        url = server.start()
        stalled = socket.create_connection(('127.0.0.1', int(re.search(r':(\d+)/', url).group(1))), timeout=5)
        try:
            stalled.sendall(b'GET / HT')
            started = time.monotonic()
            server.close()
            self.assertLess(time.monotonic() - started, 5)
        finally:
            stalled.close()


class LeakageTests(ConnectHarness):
    """Criterion 5, screen part: the real flow drives the real server; a browser stand-in polls and posts."""

    def browse(self, server_url, outcome):
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/#t=(\S+)', server_url)
        port, token, seen = int(match.group(1)), match.group(2), outcome
        host = '127.0.0.1:%d' % port

        def call(method, path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                headers = {'Host': host, 'Authorization': 'Bearer ' + token}
                if method == 'POST':
                    headers.update({'Origin': 'http://' + host, 'Content-Type': 'application/json'})
                connection.request(method, path, body=body, headers=headers)
                answer = connection.getresponse()
                data = answer.read()
                seen['responses'].append((path, answer.status, dict(answer.getheaders()), data))
                return answer.status, data
            finally:
                connection.close()

        end, posted, states = time.monotonic() + 30, 0, []
        while time.monotonic() < end and not seen['finished'].is_set():
            status, data = call('GET', '/api/state')
            state = json.loads(data)
            states.append(state)
            if state['phase'] == 'waiting_for_password' and 'password_hint' in state and not state['password_pending']:
                if posted < len(seen['passwords']):
                    posted += 1
                    seen['posted'] = call('POST', '/api/password',
                                          json.dumps({'password': seen['passwords'][posted - 1]}).encode())[0]
            if state['phase'] in ('ready', 'failed', 'expired', 'cancelled'):
                break
            time.sleep(0.01)
        states.append(json.loads(call('GET', '/api/state')[1]))  # The run is over; the end state stays up.
        seen['states'] = states

    def run_screen(self, flow, *, passwords, outcomes, scan_after=40):
        ui = LoopbackConnectUI()
        server = ConnectServer(ui, time.time() + 600)
        url = server.start()
        outcome = {'responses': [], 'passwords': passwords, 'finished': threading.Event()}
        box = []
        def run():
            try:
                box.append(self.connect(flow, ui, raw_options={'outcomes': outcomes, 'scan_after': scan_after})[0])
            finally:
                outcome['finished'].set()
        excepted = []
        records, logger = [], logging.getLogger()
        class Keep(logging.Handler):
            def emit(self, record):
                records.append(self.format(record).encode('utf-8', 'replace'))
        handler, level = Keep(level=logging.DEBUG), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(threading, 'excepthook', excepted.append):
                with captured_process_output() as output:
                    runner = threading.Thread(target=run, daemon=True)
                    runner.start()
                    self.browse(url, outcome)
                    runner.join(30)
                    # What a developer or an attacker could still ask for after the run: bad and odd requests.
                    self.probe(url)
                    server.close()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
        self.assertFalse(runner.is_alive())
        return box[0], outcome, list(output), records, excepted

    def probe(self, url):
        """Odd requests with the password in the body, after the run: refused, and nothing echoed."""
        match = re.fullmatch(r'http://127\.0\.0\.1:(\d+)/#t=(\S+)', url)
        port, token = int(match.group(1)), match.group(2)
        host, origin = '127.0.0.1:%d' % port, 'http://127.0.0.1:%d' % port
        good = {'Origin': origin, 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        valid = json.dumps({'password': FLOW_PASSWORD}).encode()
        for headers, body in [
                (dict(good, **{'Content-Type': 'text/plain'}), FLOW_PASSWORD.encode()),
                (dict(good, Origin='http://evil.example'), valid),
                (good, b'{"password": "' + FLOW_PASSWORD.encode()),
                (good, valid),
                (dict(good, Authorization='Bearer wrong'), valid)]:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.request('POST', '/api/password', body=body, headers=dict(headers, Host=host))
            answer = connection.getresponse()
            self.probes.append(('/api/password', answer.status, dict(answer.getheaders()), answer.read()))
            connection.close()

    def assert_clean(self, outcome, output, records, canaries):
        surfaces = list(output) + records
        for path, status, headers, body in outcome['responses'] + self.probes:
            surfaces.append(b'%s %d %s' % (path.encode(), status, json.dumps(headers).encode()))
            if path == '/api/state' and status == 200:
                state = json.loads(body)
                uri = state.pop('qr', None)
                if uri is not None:
                    # The one place the link may be rendered: as a QR image, never as text.
                    for canary in canaries:
                        for form in (uri.encode(), unquote(uri).encode(), base64.b64encode(canary)):
                            self.assertNotEqual(form, canary)
                        self.assertNotIn(canary, uri.encode())
                        self.assertNotIn(canary, unquote(uri).encode())
                        self.assertNotIn(base64.b64encode(canary), uri.encode())
                surfaces.append(json.dumps(state).encode())
            else:
                surfaces.append(body)
        for surface in surfaces:
            for canary in canaries:
                self.assertNotIn(canary, surface)
        self.assertEqual(list(output), [b'', b'', b'', b''])  # Nothing at all on stdout or stderr.
        self.assertEqual([row[1] for row in self.probes], [415, 403, 400, 409, 401])
        self.assertTrue(all(row[3] == b'' for row in self.probes))

    def test_known_link_and_password_never_leave_the_screen_boundary(self):
        self.probes = []
        flow = [PHONE, other_device(), other_device(FLOW_NEXT_LINK), password_state('hint'), READY]
        result, outcome, output, records, excepted = self.run_screen(
            flow, passwords=['wrong ' + FLOW_PASSWORD, FLOW_PASSWORD], outcomes=['invalid', 'ok'])
        self.assertEqual((result.phase, result.code), ('ready', None))
        self.assertEqual(excepted, [])
        states = outcome['states']
        # The browser stand-in really saw both links as QRs, the form, the error, and the end.
        qrs = []
        for state in states:
            if 'qr' in state and (not qrs or qrs[-1] != state['qr']):
                qrs.append(state['qr'])
        self.assertGreaterEqual(len(qrs), 2, 'both links were never shown')
        self.assertTrue(any(s['phase'] == 'waiting_for_password' and s['password_error'] for s in states))
        self.assertEqual(states[-1]['phase'], 'ready')
        self.assertEqual(outcome['posted'], 200)
        self.assert_clean(outcome, output, records, [
            FLOW_LINK.encode(), FLOW_NEXT_LINK.encode(), FLOW_PASSWORD.encode(), b'LEAKCANARY'])
        # Only the provider boundary received the password.
        self.assertIn(FLOW_PASSWORD, [row.get('password') for row in self.raws[-1].sent])

    def test_failed_run_shows_its_fixed_code_and_no_password(self):
        self.probes = []
        flow = [PHONE, other_device(), password_state('hint'), READY]
        wrong = ['bad%dpw' % index for index in range(5)]
        result, outcome, output, records, excepted = self.run_screen(
            flow, passwords=wrong, outcomes=['invalid'] * 5, scan_after=2)
        self.assertEqual((result.phase, result.code), ('failed', 'invalid_password'))
        self.assertEqual(excepted, [])
        last = outcome['states'][-1]
        self.assertEqual((last['phase'], last['code']), ('failed', 'invalid_password'))
        self.assert_clean(outcome, output, records, [FLOW_LINK.encode(), FLOW_PASSWORD.encode()]
                          + [word.encode() for word in wrong])

    def test_exceptions_from_the_screen_never_carry_link_or_password_text(self):
        ui = LoopbackConnectUI()
        for call in (lambda: ui.publish_qr(secret(FLOW_LINK * 20)), lambda: ui.publish_qr(FLOW_LINK),
                     lambda: ui.request_password(FLOW_PASSWORD, FLOW_PASSWORD), lambda: ui.notify(FLOW_LINK, None),
                     lambda: ui.notify('failed', FLOW_PASSWORD), lambda: SecretValue(FLOW_PASSWORD)):
            with self.assertRaises(Exception) as caught:
                call()
            error = caught.exception
            while error is not None:
                for text in (str(error), repr(error), repr(error.args)):
                    self.assertNotIn('LEAKCANARY', text)
                error = error.__cause__ or error.__context__


if __name__ == '__main__':
    unittest.main()
