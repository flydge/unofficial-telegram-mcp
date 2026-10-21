"""Structural STDIO recorder (F18-4): frames carrying canaries never reach the record."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import Client, StdioServerParameters

from telegram_search_mcp import __version__, stdio_recorder
from telegram_search_mcp.server import _TOOL_NAMES, available_tools
from telegram_search_mcp.stdio_recorder import LineSplitter, Recorder, RecorderRefusal, open_record, parse_arguments

CANARY = 'RECORDER_CANARY_7f3a'
ROOT = Path(__file__).resolve().parents[1]
CONNECT = ('read', 'artifacts')


def line(value) -> bytes:
    return json.dumps(value).encode()


def call(message_id, name, arguments):
    return {'jsonrpc': '2.0', 'id': message_id, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}


def result(message_id, value):
    return {'jsonrpc': '2.0', 'id': message_id, 'result': value}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.recorder = Recorder(self.events.append, clock=lambda: 0.0)

    def feed(self, side, *messages):
        for message in messages:
            self.recorder.frame(side, message if type(message) is bytes else line(message))

    def named(self, name):
        return [{k: v for k, v in e.items() if k not in ('event', 't_ms')} for e in self.events if e['event'] == name]

    def test_session_records_structure_only(self):
        tools = [{'name': 'b_tool', 'description': CANARY, 'inputSchema': {'type': 'object'}},
                 {'name': 'a_tool', 'description': 'x', 'inputSchema': {'type': 'object', 'title': CANARY}}]
        self.feed('client', {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'claude-code', 'version': '2.1.304'},
            'capabilities': {'roots': {CANARY: True}}}})
        self.feed('server', result(0, {'protocolVersion': '2025-06-18', 'instructions': CANARY,
                                       'serverInfo': {'name': 'telegram-search-mcp', 'version': __version__}}))
        self.feed('client', {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                  {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.feed('server', result(1, {'tools': tools}))
        self.feed('client', call(2, '_manifest', {'check_broker': True, 'note': CANARY}))
        self.feed('server', result(2, {'content': [{'type': 'text', 'text': CANARY * 3}],
                                       'structuredContent': {'detail': CANARY}, 'isError': False}))
        self.feed('client', call('req-3', 'prepare_text_send', {'recipient': 7, 'text': CANARY}))
        # A server request reusing id 2 and the client's answer to it are never correlated.
        self.feed('server', {'jsonrpc': '2.0', 'id': 2, 'method': 'sampling/createMessage', 'params': {'m': CANARY}})
        self.feed('client', result(2, {'content': {'type': 'text', 'text': CANARY}}))
        self.feed('server', result('req-3', {'content': [{'type': 'text', 'text': 'refused ' + CANARY},
                                                         {'type': CANARY, 'data': 'x'}], 'isError': True}))
        self.feed('client', call(4, 'read_history', {'target': CANARY}))
        self.feed('server', {'jsonrpc': '2.0', 'id': 4, 'error': {'code': -32602, 'message': CANARY}})
        self.feed('client', [call(5, 'bad name ' + CANARY, {}), {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                                                  'params': {'reason': CANARY}}])
        self.feed('client', b'not json ' + CANARY.encode(), b'   ')
        self.recorder.finish(0)

        self.assertNotIn(CANARY, json.dumps(self.events))
        self.assertEqual([e['event'] for e in self.events],
                         ['start', 'initialize', 'initialized', 'tools_list', 'tool_call', 'tool_call', 'rpc_error',
                          'tool_call', 'end'])
        self.assertEqual(self.named('start'), [{'schema': 'tgmcp-stdio-record/v1', 'recorder_version': __version__}])
        self.assertEqual(self.named('initialize'), [{'requested_protocol': '2025-06-18', 'client_name': 'claude-code',
                                                     'client_version': '2.1.304'}])
        self.assertEqual(self.named('initialized'), [{'negotiated_protocol': '2025-06-18',
                                                      'server_name': 'telegram-search-mcp', 'server_version': __version__}])
        listing = self.named('tools_list')[0]
        self.assertEqual((listing['census'], listing['next_page']), (2, False))
        block = json.dumps({'type': 'text', 'text': CANARY * 3}, separators=(',', ':')).encode()
        calls = self.named('tool_call')
        self.assertEqual(calls[0], {'tool': '_manifest', 'is_error': False, 'structured': True, 'blocks_total': 1,
                                    'blocks': [{'type': 'text', 'bytes': len(block)}]})
        self.assertEqual((calls[1]['tool'], calls[1]['is_error'], [b['type'] for b in calls[1]['blocks']]),
                         ('prepare_text_send', True, ['text', 'other']))
        self.assertEqual(self.named('rpc_error'), [{'method': 'tools/call', 'tool': 'read_history', 'code': -32602}])
        self.assertEqual(calls[2], {'tool': 'other', 'outcome': 'unanswered'})
        end = self.named('end')[0]
        self.assertEqual(end['exit_status'], 0)
        self.assertEqual(end['frames'], {'client': 9, 'server': 6})
        self.assertEqual(end['unparsed'], 1)
        self.assertEqual(end['methods']['client'], {'initialize': 1, 'notifications/cancelled': 1,
                                                    'notifications/initialized': 1, 'response': 1, 'tools/call': 4,
                                                    'tools/list': 1})
        self.assertEqual(end['methods']['server'], {'sampling/createMessage': 1})

    def test_only_registered_tool_names_are_recorded(self):
        # A grammar-valid but unregistered name could carry user text; only registered names pass.
        names = [CANARY, CANARY.lower() + '_tool', 'read_history', 'Read_History', '_manifest', None, 7, {'n': CANARY}]
        for index, name in enumerate(names):
            self.feed('client', {'jsonrpc': '2.0', 'id': index, 'method': 'tools/call', 'params': {'name': name}})
            self.feed('server', result(index, {'content': [], 'isError': True}))
        self.feed('client', call(99, CANARY, {}))
        self.feed('server', {'jsonrpc': '2.0', 'id': 99, 'error': {'code': -32602, 'message': 'x'}})
        self.feed('client', call(100, CANARY + '_unanswered', {}))
        self.recorder.finish(0)
        self.assertNotIn(CANARY, json.dumps(self.events))
        self.assertNotIn(CANARY.lower(), json.dumps(self.events))
        self.assertEqual([e['tool'] for e in self.named('tool_call')],
                         ['other', 'other', 'read_history', 'other', '_manifest', 'other', 'other', 'other', 'other'])
        self.assertEqual(self.named('rpc_error'), [{'method': 'tools/call', 'tool': 'other', 'code': -32602}])
        self.assertTrue(stdio_recorder.TOOL_NAMES and stdio_recorder.TOOL_NAMES == set(_TOOL_NAMES))

    def test_tools_fingerprint_ignores_order_and_follows_content(self):
        tools = [{'name': 'b', 'inputSchema': {'type': 'object'}}, {'name': 'a', 'inputSchema': {'type': 'object'}}]
        changed = [tools[0], {'name': 'a', 'inputSchema': {'type': 'object', 'additionalProperties': False}}]
        for index, (listed, cursor) in enumerate(((tools, None), (tools[::-1], 'next'), (changed, None))):
            self.feed('client', {'id': index, 'method': 'tools/list'})
            self.feed('server', result(index, {'tools': listed, 'nextCursor': cursor}))
        first, second, third = self.named('tools_list')
        self.assertEqual((first['tools_sha256'], second['next_page']), (second['tools_sha256'], True))
        self.assertNotEqual(first['tools_sha256'], third['tools_sha256'])
        self.assertEqual([first['census'], first['next_page']], [2, False])

    def test_names_outside_the_grammar_are_redacted(self):
        for name in ('', 'a\nb', 'x' * 65, '-lead', 'name"quote', 7, {'n': 1}):
            events = []
            recorder = Recorder(events.append)
            recorder.frame('client', line({'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-06-18 ' + CANARY, 'clientInfo': {'name': name, 'version': '1.0'}}}))
            with self.subTest(name=name):
                self.assertEqual(events[1]['client_name'], 'redacted')
                self.assertEqual(events[1]['requested_protocol'], 'redacted')

    def test_modern_discover_and_request_stamps(self):
        meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                'io.modelcontextprotocol/clientInfo': {'name': 'codex-mcp-client', 'version': '0.9.1'},
                'io.modelcontextprotocol/clientCapabilities': {CANARY: 1}}
        self.feed('client', {'id': 1, 'method': 'server/discover', 'params': {'_meta': meta}})
        self.feed('server', result(1, {'supportedVersions': ['2026-07-28', CANARY + ' x'], 'capabilities': {},
                                       '_meta': {'io.modelcontextprotocol/serverInfo': {'name': 's', 'version': '1'}}}))
        self.feed('client', {'id': 2, 'method': 'tools/list', 'params': {'_meta': meta}},
                  call(3, 'resolve_target', {'target': CANARY}) | {'params': {'name': 'resolve_target', '_meta': meta}})
        self.recorder.finish(None)
        self.assertNotIn(CANARY, json.dumps(self.events))
        self.assertEqual(self.named('stamp'), [{'protocol': '2026-07-28', 'client_name': 'codex-mcp-client',
                                                'client_version': '0.9.1'}])
        self.assertEqual(self.named('discovered'), [{'supported_protocols': ['2026-07-28', 'redacted'],
                                                     'server_name': 's', 'server_version': '1'}])

    def test_events_are_bounded_and_end_is_always_written(self):
        for index in range(stdio_recorder.MAX_EVENTS + 10):
            self.feed('client', call(index, 'x', {}))
            self.feed('server', result(index, {'content': []}))
        self.recorder.finish(1)
        self.assertEqual(self.events[-1]['event'], 'end')
        self.assertEqual(len(self.events), stdio_recorder.MAX_EVENTS + 1)
        self.assertEqual(self.events[-1]['suppressed_events'], 11)
        self.recorder.frame('client', line(call(1, 'late', {})))
        self.assertEqual(self.events[-1]['event'], 'end')

    def test_pending_requests_are_bounded(self):
        for index in range(stdio_recorder.MAX_PENDING + 5):
            self.feed('client', call(index, 'x', {}))
        self.feed('client', call({'object': 'id'}, 'x', {}))
        self.recorder.finish(0)
        self.assertEqual(self.named('end')[0]['untracked'], 6)
        self.assertEqual(len(self.named('tool_call')), stdio_recorder.MAX_PENDING)


class SplitterTests(unittest.TestCase):
    def test_frames_split_across_chunks(self):
        splitter = LineSplitter()
        self.assertEqual(list(splitter.feed(b'{"a":1}\n{"b"')), [b'{"a":1}'])
        self.assertEqual(list(splitter.feed(b':2}\n\n')), [b'{"b":2}', b''])

    def test_oversized_lines_are_reported_once_and_skipped(self):
        splitter = LineSplitter(limit=8)
        self.assertEqual(list(splitter.feed(b'0123456789\nabc\n')), [None, b'abc'])
        self.assertEqual(list(splitter.feed(b'0123456789')), [None])
        self.assertEqual(list(splitter.feed(b'more')), [])
        self.assertEqual(list(splitter.feed(b'xx\nok\n')), [b'ok'])
        self.assertEqual(list(splitter.feed(b'01234')), [])
        self.assertEqual(list(splitter.feed(b'5678\n')), [None])


class ArgumentAndRecordTests(unittest.TestCase):
    def test_closed_argument_grammar(self):
        self.assertEqual(parse_arguments(['--record', '/r', '--', '/bin/cat', '-u']), ('/r', ['/bin/cat', '-u']))
        for argv in ([], ['--record', '/r', '--'], ['--record', '/r', '/bin/cat'], ['--out', '/r', '--', '/bin/cat'],
                     ['--record', '/r', '--', 'cat'], ['--record', '/r', '--', '/bin/cat', 'a\0b'],
                     ['--record', '/r', '--', '/bin/cat'] + ['x'] * stdio_recorder.MAX_ARGV):
            with self.subTest(argv=argv), self.assertRaises(RecorderRefusal) as caught:
                parse_arguments(argv)
            self.assertEqual(caught.exception.code, 'recorder_usage')

    def test_record_is_a_new_owner_only_file_in_an_owner_only_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            private = Path(parent) / 'private'
            private.mkdir(mode=0o700)
            fd = open_record(str(private / 'session.jsonl'))
            os.close(fd)
            self.assertEqual(stat.S_IMODE((private / 'session.jsonl').stat().st_mode), 0o600)
            shared = Path(parent) / 'shared'
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            link = Path(parent) / 'link'
            link.symlink_to(private, target_is_directory=True)
            for path in (str(private / 'session.jsonl'), str(shared / 'x.jsonl'), str(link / 'y.jsonl'),
                         'relative.jsonl', str(private) + '/../private/z.jsonl', str(private) + '/', '/',
                         str(private / 'missing' / 'x.jsonl')):
                with self.subTest(path=path), self.assertRaises(RecorderRefusal) as caught:
                    open_record(path)
                self.assertEqual(caught.exception.code, 'record_unavailable')


SERVER = r'''
import sys
from telegram_search_mcp.config import RuntimePolicy
from telegram_search_mcp.schemas import TargetResolutionResponse
from telegram_search_mcp.server import build_server

DETAIL = 'complete ' + sys.argv[1]


class Service:
    def close(self):
        pass

    def resolve(self, request):
        part = {'status': 'complete', 'detail': DETAIL}
        return TargetResolutionResponse.model_validate({'status': 'not_found', 'coverage': {
            'complete': True, 'saved_messages': part, 'exact_username': part, 'search_chats': part,
            'search_chats_on_server': part, 'recent_main': part, 'hydration': part, 'detail': DETAIL}})


build_server(service_factory=Service, policy=RuntimePolicy(enabled_capabilities=('read', 'artifacts'))).run('stdio')
'''


class RecorderProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_client_through_recorder_to_the_real_server(self):
        with tempfile.TemporaryDirectory() as parent:
            record = Path(parent) / 'session.jsonl'
            os.chmod(parent, 0o700)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=['-m', 'telegram_search_mcp.stdio_recorder', '--record', str(record), '--',
                      sys.executable, '-c', SERVER, CANARY],
                env={'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}, cwd=parent)
            async with Client(parameters) as client:
                listed = [tool.name for tool in (await client.list_tools()).tools]
                manifest = await client.call_tool('_manifest', {})
                resolved = await client.call_tool('resolve_target', {'target': '@Recorder_canary_7f3a'})
                refused = await client.call_tool('prepare_text_send', {'recipient': 7, 'text': CANARY})
            deadline = time.monotonic() + 10
            while b'"event":"end"' not in record.read_bytes() and time.monotonic() < deadline:
                time.sleep(0.05)
            text = record.read_text('ascii')
            mode = stat.S_IMODE(record.stat().st_mode)
        # The relay is transparent: the client saw the real server's answers.
        self.assertEqual(listed, available_tools(CONNECT))
        self.assertEqual(manifest.structured_content['available_tools'], available_tools(CONNECT))
        self.assertIn(CANARY, json.dumps(resolved.structured_content))
        self.assertTrue(refused.is_error)
        # The record holds only structure.
        self.assertEqual(mode, 0o600)
        self.assertNotIn(CANARY, text)
        self.assertNotIn('canary', text.casefold())
        events = [json.loads(row) for row in text.splitlines()]
        self.assertEqual((events[0]['event'], events[-1]['event']), ('start', 'end'))
        names = [e['event'] for e in events]
        revisions = [e.get('negotiated_protocol') for e in events if e['event'] == 'initialized'] + [
            e.get('protocol') for e in events if e['event'] == 'stamp']
        self.assertTrue(revisions and all(r and r != 'redacted' for r in revisions), names)
        listings = [e for e in events if e['event'] == 'tools_list']
        self.assertEqual([e['census'] for e in listings], [len(available_tools(CONNECT))])
        calls = [(e['tool'], e['is_error']) for e in events if e['event'] == 'tool_call']
        self.assertEqual(calls, [('_manifest', False), ('resolve_target', False), ('prepare_text_send', True)])
        self.assertTrue(all(block['type'] == 'text' and block['bytes'] > 0
                            for e in events if e['event'] == 'tool_call' for block in e['blocks']))

    def test_refusals_are_fixed_codes(self):
        environment = {'PYTHONPATH': str(ROOT / 'src'), 'PATH': '/usr/bin:/bin'}
        for argv, code in ((['--record', 'relative', '--', '/bin/cat'], 'record_unavailable'),
                           (['--record', '/x', '--', 'cat'], 'recorder_usage')):
            with self.subTest(argv=argv):
                done = subprocess.run([sys.executable, '-m', 'telegram_search_mcp.stdio_recorder', *argv],
                                      env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual((done.returncode, done.stdout), (2, ''))
                self.assertEqual(json.loads(done.stderr), {'status': 'refused', 'code': code})


if __name__ == '__main__':
    unittest.main()
