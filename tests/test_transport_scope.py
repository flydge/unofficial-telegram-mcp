"""F19: Streamable HTTP is not applicable. The MCP server speaks STDIO only, the broker listens only on its
owner-only Unix socket, and the one HTTP listener in the tree is the loopback connect screen.

A future HTTP transport must be a deliberate, reviewed change to these pins, never a side effect.
"""
from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

from telegram_search_mcp import server

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted([*(ROOT / 'src/telegram_search_mcp').rglob('*.py'), *(ROOT / 'packaging').glob('*.py'),
                  ROOT / 'plugins/telegram-local/bin/mcp-entry'])
# The SDK's network transports and the stdlib listeners; any reference outside the allowlist fails.
NETWORK_NAMES = frozenset({'streamable_http_app', 'sse_app', 'run_streamable_http_async', 'run_sse_async',
                           'StreamableHTTPSessionManager', 'SseServerTransport', 'uvicorn', 'starlette',
                           'http.server', 'socketserver', 'AF_INET', 'AF_INET6'})
ALLOWED = {'src/telegram_search_mcp/connect_server.py': {'http.server', 'socketserver'}}


def referenced_names(path):
    """Dotted import targets and attribute/name identifiers in one Python source."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Constant) and type(node.value) is str and node.value in {'sse', 'streamable-http'}:
            found.add(node.value)
    return found


class TransportScopeTests(unittest.TestCase):
    def test_no_network_transport_or_listener_outside_the_loopback_connect_screen(self):
        self.assertGreater(len(SOURCES), 50)
        for path in SOURCES:
            relative = path.relative_to(ROOT).as_posix()
            if path.name == 'mcp-entry':
                text = path.read_text(encoding='utf-8')
                self.assertNotRegex(text, r'(?i)\b(http|sse|port)\b=', relative)
                continue
            with self.subTest(source=relative):
                names = referenced_names(path)
                hits = {name for name in names if name in NETWORK_NAMES or name in {'sse', 'streamable-http'}}
                self.assertEqual(hits - ALLOWED.get(relative, set()), set())

    def test_connect_screen_binds_loopback_only(self):
        source = (ROOT / 'src/telegram_search_mcp/connect_server.py').read_text(encoding='utf-8')
        servers = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Name) and node.func.id in {'_Server', 'ThreadingHTTPServer'}]
        self.assertTrue(servers)
        for call in servers:
            address = call.args[0]
            self.assertIsInstance(address, ast.Tuple)
            self.assertEqual(ast.literal_eval(address.elts[0]), '127.0.0.1')

    def test_server_main_runs_stdio_only_and_refuses_transport_options(self):
        with mock.patch.object(server, 'build_server') as build, mock.patch.object(sys, 'argv', ['telegram-search-mcp']):
            self.assertEqual(server.main(), 0)
        build.return_value.run.assert_called_once_with(transport='stdio')
        for extra in (['--transport', 'streamable-http'], ['--transport=sse'], ['--port', '8000'], ['--host', '0.0.0.0'],
                      ['--http']):
            with self.subTest(argv=extra), mock.patch.object(server, 'build_server') as build, \
                    mock.patch.object(sys, 'argv', ['telegram-search-mcp', *extra]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                server.main()
            self.assertEqual(caught.exception.code, 2)
            build.assert_not_called()

    def test_broker_listens_on_a_unix_socket(self):
        source = (ROOT / 'src/telegram_search_mcp/broker.py').read_text(encoding='utf-8')
        unix = source.count('socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)')
        self.assertGreater(unix, 0)
        self.assertEqual(source.count('socket.socket('), unix)


if __name__ == '__main__':
    unittest.main()
