"""The pending-send probe the installer runs under a broker's own interpreter before it stops that broker.

It must work against every broker the installer adopts or upgrades: a 0.51 or 0.52 source-venv broker (adopted with
--adopt-legacy) and a bundled one. So it may use only what those releases share: `config.BROKER_SOCKET_PATH`,
`config.load_runtime_policy`, `broker_client.BrokerClient` with its health() and close(). Anything it cannot answer is
`unknown`, which refuses (fail closed). Disposable homes only; the broker is a real one on a temporary socket.

From 0.53.0rc2 a broker's health also counts `pending_sends` (sends in flight, approval dialogs open, outcomes not yet
settled): any of them, or any client context, answers `pending`. A 0.51 or 0.52 broker counts only contexts, so there
any context answers `unknown` (it may hold a send the probe cannot see) and none answers `none`.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import sysconfig
import tempfile
import threading
import unittest

import telegram_search_mcp
from telegram_search_mcp import bundle_install

SOCKET = Path('Library/Application Support/TelegramSearchMCP/run/broker.sock')

# The surface 0.51.0 and 0.52.0 both ship (git show v0.51.0 / v0.52.0: config.py, broker_client.py), stdlib only. A
# probe that imports or calls anything else fails here: the stub has no `enrollment`, no `setup_cli`.
STUB_CONFIG = '''from pathlib import Path
APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "TelegramSearchMCP"
BROKER_RUNTIME_DIRECTORY = APP_SUPPORT_ROOT / "run"
BROKER_SOCKET_PATH = BROKER_RUNTIME_DIRECTORY / "broker.sock"
RUNTIME_POLICY_PATH = APP_SUPPORT_ROOT / "runtime.toml"
def load_runtime_policy(path=RUNTIME_POLICY_PATH):
    return {"policy": str(path)}
'''
STUB_CLIENT = '''import json
from pathlib import Path
class BrokerUnavailable(RuntimeError):
    pass
class BrokerClient:
    def __init__(self, *, socket_path, connector=None, request_timeout=570.0, client_id=None, restart_callback=None,
                 retry_backoff_seconds=0.25, sleeper=None, policy=None):
        self.log = Path(socket_path).with_name("calls.json")
        self.answer = json.loads(Path(socket_path).with_name("answer.json").read_text())
        self.calls = [{"socket_path": str(socket_path), "request_timeout": request_timeout, "policy": policy,
                       "retry_backoff_seconds": retry_backoff_seconds, "restart": restart_callback is not None}]
    def health(self):
        self.calls.append("health")
        if self.answer is None:
            raise BrokerUnavailable("unavailable")
        return dict(uptime_seconds=1.0, queue_depth=0, active_clients=self.answer, completed_requests=0,
                    error_count=0, peak_overlap=0, serialization_wait_count=0)
    def close(self):
        self.calls.append("close")
        self.log.write_text(json.dumps(self.calls))
'''


class StubSurfaceTests(unittest.TestCase):
    """The probe against a package that has only the 0.51/0.52 surface, run as a bundled app path (-I -S -B)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home, self.app = base / 'home', base / 'app'
        package = self.app / 'telegram_search_mcp'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('__version__ = "0.51.0"\n')
        (package / 'config.py').write_text(STUB_CONFIG)
        (package / 'broker_client.py').write_text(STUB_CLIENT)
        self.run_dir = self.home / SOCKET.parent
        self.run_dir.mkdir(parents=True)
        (self.run_dir / 'broker.sock').write_bytes(b'')  # Only its presence is checked before the probe runs.

    def answer(self, value):
        (self.run_dir / 'answer.json').write_text(json.dumps(value))
        return bundle_install.default_pending_sends(self.home, Path(sys.executable), self.app)

    def test_none_pending_and_unknown_from_the_shared_surface_only(self):
        self.assertEqual(self.answer(0), 'none')
        calls = json.loads((self.run_dir / 'calls.json').read_text())
        self.assertEqual(calls, [{'socket_path': str(self.home / SOCKET), 'request_timeout': 5,
                                  'policy': {'policy': str(self.home / SOCKET.parent.parent / 'runtime.toml')},
                                  'retry_backoff_seconds': 0, 'restart': True}, 'health', 'close'])
        self.assertEqual(self.answer(2), 'unknown')  # A context, and no pending-send count to say what it holds.
        self.assertEqual(self.answer(None), 'unknown')  # The client could not ask: fail closed.
        self.assertEqual(self.answer(True), 'unknown')  # Not an exact zero count: never 'none'.

    def test_no_socket_is_none_and_no_interpreter_is_unknown(self):
        (self.run_dir / 'broker.sock').unlink()
        self.assertEqual(self.answer(5), 'none')
        (self.run_dir / 'broker.sock').write_bytes(b'')
        self.assertEqual(bundle_install.default_pending_sends(self.home, None, None), 'unknown')
        self.assertEqual(bundle_install.default_pending_sends(self.home, self.home / 'missing-python', self.app), 'unknown')


def _bundled_app(directory):
    """A bundled release's `app` directory: the package under test and its dependencies, nothing from site."""
    directory.mkdir()
    os.symlink(Path(telegram_search_mcp.__file__).parent, directory / 'telegram_search_mcp')
    for site in dict.fromkeys((sysconfig.get_paths()['purelib'], sysconfig.get_paths()['platlib'])):
        for entry in Path(site).iterdir():
            if (entry.name.startswith(('telegram_search_mcp', '__editable__', '_distutils_hack')) or entry.suffix == '.pth'
                    or os.path.lexists(directory / entry.name)):
                continue
            os.symlink(entry, directory / entry.name)


class RealBrokerTests(unittest.TestCase):
    """This release's broker on a temporary socket in a disposable home; the probe runs as for a bundled broker."""

    def setUp(self):
        # An AF_UNIX path is at most 104 bytes: a short home under /private/tmp, as the other socket tests use.
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='tgmcp-probe-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home = base / 'h'
        self.root = self.home / SOCKET.parent.parent
        self.root.mkdir(parents=True, mode=0o700)
        for path in (self.home, self.home / 'Library', self.home / 'Library/Application Support', self.root):
            path.chmod(0o700)
        policy = self.root / 'runtime.toml'
        policy.write_text('config_version = 1\nenabled_capabilities = ["read", "artifacts", "send"]\n')
        policy.chmod(0o600)
        self.app = base / 'app'
        _bundled_app(self.app)

    def broker(self, *, client_factory=None, approval_prompt=None, **policy_overrides):
        from telegram_search_mcp.artifact_store import ArtifactStore
        from telegram_search_mcp.broker import Broker
        from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
        from test_broker import SerializedFakeTDLib
        policy = load_runtime_policy(self.root / 'runtime.toml')
        if policy_overrides:
            policy = RuntimePolicy(**{**policy.__dict__, **policy_overrides})
        broker = Broker(socket_path=self.home / SOCKET, client_factory=client_factory or SerializedFakeTDLib,
                        verify_peer_uid=False, policy=policy,
                        artifact_store=ArtifactStore(cache_dir=self.root / 'artifacts'),
                        download_source_root=self.root / 'tdlib' / 'files',
                        **({} if approval_prompt is None else {'approval_prompt': approval_prompt}))
        thread = threading.Thread(target=broker.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(broker.shutdown)
        self.assertTrue(broker.wait_until_ready(timeout=5))
        return broker

    def probe(self):
        return bundle_install.default_pending_sends(self.home, Path(sys.executable), self.app)

    def test_an_idle_broker_is_none_and_a_client_context_is_pending(self):
        broker = self.broker()
        self.assertEqual(self.probe(), 'none')
        self.assertEqual(self.probe(), 'none')  # Asking leaves no client context behind.
        with broker._service_lease('client_another_mcp_session'):
            self.assertEqual(self.probe(), 'pending')
        self.assertEqual(self.probe(), 'pending')  # An idle context may still hold a draft or an unresolved send.

    def test_a_send_in_flight_an_open_dialog_and_an_unsettled_outcome_are_pending(self):
        """0.53.0rc1 critic (safety, high): the probe counted client contexts only, and prepare, send and
        get_send_status never create one, so it answered `none` with an approval dialog open, a send in flight or
        an outcome_unknown receipt held; a restart then lost the receipt. Driven through a real BrokerClient over
        the socket, as an MCP proxy sends; no client context exists at any step."""
        from telegram_search_mcp.broker_client import BrokerClient
        from telegram_search_mcp.config import load_runtime_policy
        from telegram_search_mcp.schemas import PrepareTextSendRequest, SendPreparedTextRequest
        from test_outgoing_text import TextSender

        class Sender(TextSender):
            def __init__(self):
                super().__init__()
                self.entered, self.release, self.fail = threading.Event(), threading.Event(), False

            def send_text_message(self, chat_id, text, *, attempt_id=None):
                self.entered.set()
                self.release.wait(10)
                if self.fail:
                    raise RuntimeError('connection lost after the request')
                return super().send_text_message(chat_id, text, attempt_id=attempt_id)

            def close(self):
                pass

        sender, dialog, answer = Sender(), threading.Event(), threading.Event()

        def prompt(**_):
            dialog.set()
            answer.wait(10)
            return True
        broker = self.broker(client_factory=lambda: sender, approval_prompt=prompt)
        client = BrokerClient(socket_path=self.home / SOCKET, policy=load_runtime_policy(self.root / 'runtime.toml'),
                              request_timeout=60, restart_callback=lambda: None, retry_backoff_seconds=0)
        self.addCleanup(client.close)
        self.addCleanup(answer.set)
        self.addCleanup(sender.release.set)
        counts = lambda: broker._health()['pending_sends']
        idle = {'in_flight': 0, 'approval_dialogs': 0, 'unreconciled_receipts': 0}

        def send(draft_id, out):
            thread = threading.Thread(target=lambda: out.append(client.send_prepared_text(
                SendPreparedTextRequest(draft_id=draft_id, approved=True))))
            thread.start()
            return thread
        prepared = client.prepare_text_send(PrepareTextSendRequest(recipient=123, text='Hello'))
        self.assertEqual((self.probe(), counts()), ('none', idle))  # A prepared draft is not a send.
        out = []
        thread = send(prepared.draft_id, out)
        self.assertTrue(dialog.wait(10))
        self.assertEqual(self.probe(), 'pending')
        self.assertEqual(counts(), {'in_flight': 1, 'approval_dialogs': 1, 'unreconciled_receipts': 0})
        answer.set()
        self.assertTrue(sender.entered.wait(10))
        self.assertEqual(self.probe(), 'pending')  # Approved and claimed: the provider call is in flight.
        self.assertEqual(counts(), {'in_flight': 1, 'approval_dialogs': 0, 'unreconciled_receipts': 1})
        sender.release.set()
        thread.join(10)
        self.assertEqual(out[0].status, 'sent')
        self.assertEqual((self.probe(), counts()), ('none', idle))  # Settled.
        self.assertEqual(broker._health()['active_clients'], 0)
        sender.entered.clear()
        sender.fail = True
        prepared = client.prepare_text_send(PrepareTextSendRequest(recipient=123, text='Again'))
        out = []
        send(prepared.draft_id, out).join(10)
        self.assertEqual(out[0].status, 'outcome_unknown')
        self.assertEqual(self.probe(), 'pending')  # get_send_status could still settle it: a restart would lose it.
        self.assertEqual(counts(), {'in_flight': 0, 'approval_dialogs': 0, 'unreconciled_receipts': 1})
        clock = broker._drafts._attempt_clock
        broker._drafts._attempt_clock = lambda: clock() + 900  # Past receipt retention: nothing left to settle.
        self.assertEqual((self.probe(), counts()), ('none', idle))

    def test_a_broker_that_cannot_answer_is_unknown(self):
        self.broker(enabled_capabilities=('read',))  # Another policy: the handshake refuses.
        self.assertEqual(self.probe(), 'unknown')

    def test_a_stale_socket_is_unknown(self):
        (self.home / SOCKET).parent.mkdir(mode=0o700)
        import socket
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.home / SOCKET))
        stale.close()  # Bound, never listening: connect is refused.
        self.assertEqual(self.probe(), 'unknown')


class LeftoverAliasProbeTests(unittest.TestCase):
    """Review fix (055d3, R4): a loaded alias broker whose definition names a release since removed made every gate
    answer `unknown` for good (its interpreter is gone, the probe could not even start), so uninstall, account-add,
    account-remove and upgrade refused `pending_send_unresolved` forever. With `current`, the real
    `account_pending_sends` probes that broker's live socket under the current release: no listener (a stale socket
    file) is `none`, a broker that answers is counted as usual, and one that cannot be read is `unreadable`."""

    setUp = RealBrokerTests.setUp
    broker = RealBrokerTests.broker

    def gone(self):
        python = self.home / 'gone-release' / 'python/bin/python3'
        return python, python.parent.parent.parent / 'app'

    def probe(self, current=True):
        python, app = self.gone()
        return bundle_install.account_pending_sends(self.home, python, app, self.home / SOCKET,
                                                    self.root / 'runtime.toml',
                                                    current=(Path(sys.executable), self.app) if current else None)

    def test_no_socket_and_a_stale_socket_file_hold_nothing(self):
        self.assertEqual(self.probe(), 'none')
        (self.home / SOCKET).parent.mkdir(mode=0o700)
        import socket
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.home / SOCKET))
        stale.close()  # Bound, never listening: connect is refused.
        self.assertTrue((self.home / SOCKET).exists())
        self.assertEqual(self.probe(), 'none')
        self.assertEqual(self.probe(current=False), 'unknown')  # Without the current release: as before.

    def test_a_live_broker_that_answers_is_gated_normally(self):
        broker = self.broker()
        self.assertEqual(self.probe(), 'none')
        with broker._service_lease('client_another_mcp_session'):
            self.assertEqual(self.probe(), 'pending')

    def test_a_live_listener_that_cannot_be_read_is_unreadable(self):
        self.broker(enabled_capabilities=('read',))  # Another policy: the current release's handshake refuses.
        self.assertEqual(self.probe(), 'unreadable')

    def test_a_listener_that_is_no_broker_is_unreadable(self):
        import socket
        (self.home / SOCKET).parent.mkdir(mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.home / SOCKET))
        listener.listen(4)
        self.addCleanup(listener.close)
        stop = threading.Event()

        def drop():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    listener.accept()[0].close()
                except OSError:
                    continue
        thread = threading.Thread(target=drop)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        self.assertEqual(self.probe(), 'unreadable')


if __name__ == '__main__':
    unittest.main()
