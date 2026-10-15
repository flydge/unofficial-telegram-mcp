"""Dev-only harness for the loopback connection screen (not a test: no real run, no Telegram).

Serves the screen with a scripted fake run and prints ONLY its URL on stdout; open it yourself.
It runs until Ctrl-C.

    .venv/bin/python tests/connect_screen_demo.py                  # scripted run, about a minute
    .venv/bin/python tests/connect_screen_demo.py --phase waiting_for_scan   # hold one phase

Scripted run: starting -> QR (replaced after ~15 s) -> simulated scan -> 2FA form (password
"demo" is accepted, anything else is refused with an inline error) -> authorizing -> ready.
--phase holds: PHASES plus password_error, password_pending and closed (server stops answering).
"""
import argparse
import base64
import os
import sys
import threading
import time

from telegram_search_mcp.connect_flow import PHASES, _CODES
from telegram_search_mcp.connect_server import ConnectServer, LoopbackConnectUI
from telegram_search_mcp.credentials import SecretValue

LIFETIME = 600
HINT = 'demo'
PASSWORD = b'demo'
EXTRA_PHASES = ('password_error', 'password_pending', 'closed')
_HOLD_CODES = {'expired': 'expired', 'cancelled': 'cancelled'}


class _Done(Exception):
    pass


def synthetic_link():
    return SecretValue(('tg://login?token=' + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode()).encode())


class Script:
    def __init__(self, ui, expires_at):
        self.ui, self.expires_at = ui, expires_at

    def nap(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(0.1)

    def check(self):
        if self.ui.cancelled():
            self.ui.notify('cancelled', 'cancelled')
            raise _Done
        if time.time() >= self.expires_at:
            self.ui.notify('expired', 'expired')
            raise _Done

    def password(self, code=None):
        self.ui.notify('waiting_for_password', code)
        while True:
            self.check()
            secret = self.ui.request_password(HINT, 1.0)
            if secret is not None:
                return secret

    def run(self):
        ui = self.ui
        try:
            ui.notify('starting', None)
            self.nap(2)
            ui.publish_qr(synthetic_link())
            ui.notify('waiting_for_scan', None)
            self.nap(15)
            ui.publish_qr(synthetic_link())  # Telegram replaces the link about every 30 s; here sooner.
            self.nap(15)  # The simulated phone scans, and the account has two-step verification.
            code = None
            while True:
                secret = self.password(code)
                ui.notify('authorizing', None)
                self.nap(1.5)
                if secret.reveal() == PASSWORD:
                    break
                code = 'invalid_password'
            ui.notify('handing_off', None)
            self.nap(1.5)
            ui.notify('ready', None)
        except _Done:
            pass

    def hold(self, phase, code, server):
        ui = self.ui
        try:
            if phase == 'closed':
                ui.notify('starting', None)
                self.nap(4)
                server.close()
                return
            if phase == 'waiting_for_scan':
                ui.publish_qr(synthetic_link())
            shown, shown_code = ('waiting_for_password', code) if phase.startswith('password_') else (phase, code)
            if phase == 'password_error':
                shown_code = 'invalid_password'
            ui.notify(shown, shown_code)
            if shown != 'waiting_for_password':
                while True:
                    self.check_cancel_only()
                    time.sleep(0.2)
            while True:
                self.check_cancel_only()
                if ui.request_password(HINT, 1.0) is not None:
                    if phase == 'password_pending':
                        threading.Event().wait()  # Keep the "checking" state on screen.
                    ui.notify(shown, shown_code)  # Dropped; show the form again.
        except _Done:
            pass

    def check_cancel_only(self):
        if self.ui.cancelled():
            self.ui.notify('cancelled', 'cancelled')
            raise _Done


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--phase', choices=tuple(PHASES) + EXTRA_PHASES, metavar='PHASE',
                        help='hold one phase instead of the scripted run: ' + ', '.join(tuple(PHASES) + EXTRA_PHASES))
    parser.add_argument('--code', choices=sorted(_CODES), default='auth_request_failed', metavar='CODE',
                        help='fixed flow code shown with --phase failed (default: %(default)s)')
    args = parser.parse_args(argv)
    expires_at = time.time() + LIFETIME
    ui = LoopbackConnectUI()
    server = ConnectServer(ui, expires_at)
    url = server.start()
    print(url, flush=True)
    script = Script(ui, expires_at)
    if args.phase is None:
        target = script.run
    else:
        code = _HOLD_CODES.get(args.phase, args.code if args.phase == 'failed' else None)
        target = lambda: script.hold(args.phase, code, server)
    threading.Thread(target=target, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
