"""0.55 phase C review fix: `status --account` is read-only; the broker records its readiness (spec 3C).

Once an alias registry exists, a broker records the outcome of its own account check in `run/readiness.json`:
its generation and `authorized` (ready true; not authorized or refused by the pin false), written on a change,
atomically and owner-only, and removed when the outcome is unknown. `status --account` reads it beside the handshake
it already makes and never sends `check`, so it never starts TDLib or writes the first pin. A record counts only for
the running broker's generation. Without a registry nothing is written (0.54). Fake TDLib clients only.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from test_account_pin import USER, OTHER, Account, Fixture

from telegram_search_mcp import account_registry, broker_readiness
from telegram_search_mcp.config import AccountPaths
from telegram_search_mcp.tdjson import TDLibError


def recorded(account):
    path = account.run / broker_readiness.NAME
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
    return json.loads(path.read_bytes())


class BrokerRecordTests(Fixture):
    def account(self, alias):
        return AccountPaths.for_alias(alias, self.home)

    def broker(self, client, alias='default'):
        # A serving broker has created its owner-only run directory (it binds its socket there).
        self.account(alias).run.mkdir(parents=True, mode=0o700, exist_ok=True)
        return super().broker(client, alias)

    def test_without_a_registry_nothing_is_recorded(self):
        broker = self.broker(Account(USER))
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertFalse((self.account('default').run / broker_readiness.NAME).exists())

    def test_the_check_records_ready_and_blocked_for_this_generation_only(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        work = self.account('work')
        client = Account(USER)
        broker = self.broker(client, 'work')
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertEqual(recorded(work), {'version': 1, 'generation': broker._generation, 'authorized': True})
        self.assertIs(broker_readiness.read(work.run, broker._generation), True)
        self.assertIsNone(broker_readiness.read(work.run, 'broker_' + '0' * 32))  # An earlier broker's record.
        client.ready = False
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})
        self.assertIs(broker_readiness.read(work.run, broker._generation), False)
        client.ready = True  # Any later operation that passes the account check records ready again.
        self.assertNotEqual(self.resolve(broker)['status'], 'error')
        self.assertIs(broker_readiness.read(work.run, broker._generation), True)
        self.assertNotIn(str(USER), (work.run / broker_readiness.NAME).read_text())

    def test_an_account_refused_by_the_pin_is_recorded_blocked(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        account_registry.pin_account(self.root, 'work', OTHER)
        work = self.account('work')
        broker = self.broker(Account(USER), 'work')
        self.assertEqual(self.request(broker, 'check'), {'status': 'blocked'})
        self.assertEqual(recorded(work), {'version': 1, 'generation': broker._generation, 'authorized': False})
        self.blocked('account_mismatch', lambda: self.resolve(broker))
        self.assertIs(broker_readiness.read(work.run, broker._generation), False)

    def test_an_unknown_outcome_removes_the_record(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        work = self.account('work')
        client = Account(USER)
        broker = self.broker(client, 'work')
        self.request(broker, 'check')
        self.assertTrue((work.run / broker_readiness.NAME).exists())

        def failing():
            raise TDLibError('SYNTHETIC')
        client.ensure_ready = failing
        self.assertEqual(self.request(broker, 'check'), {'status': 'error'})
        self.assertFalse((work.run / broker_readiness.NAME).exists())

    def test_the_default_with_a_registry_records_too(self):
        account_registry.add_alias('work', self.home, longest_tool='t')
        broker = self.broker(Account(USER))
        self.assertEqual(self.request(broker, 'check'), {'status': 'ready'})
        self.assertIs(broker_readiness.read(self.account('default').run, broker._generation), True)


class ReaderTests(unittest.TestCase):
    GENERATION = 'broker_' + 'a' * 32

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='r')
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name).resolve() / 'run'
        self.run_dir.mkdir(mode=0o700)
        self.path = self.run_dir / broker_readiness.NAME

    def write(self, data, mode=0o600):
        self.path.write_bytes(data)
        os.chmod(self.path, mode)

    def test_a_record_round_trips_and_unknown_removes_it(self):
        for value in (True, False):
            broker_readiness.record(self.run_dir, self.GENERATION, value)
            self.assertIs(broker_readiness.read(self.run_dir, self.GENERATION), value)
        broker_readiness.record(self.run_dir, self.GENERATION, None)
        self.assertFalse(self.path.exists())
        self.assertIsNone(broker_readiness.read(self.run_dir, self.GENERATION))
        self.assertEqual(sorted(os.listdir(self.run_dir)), [])  # No temporary file is left behind.

    def test_anything_but_the_closed_record_reads_unknown(self):
        good = {'version': 1, 'generation': self.GENERATION, 'authorized': True}
        for data in (b'', b'{', b'[]', json.dumps({**good, 'extra': 1}).encode(),
                     json.dumps({**good, 'authorized': 1}).encode(), json.dumps({**good, 'version': 2}).encode(),
                     json.dumps({**good, 'generation': 7}).encode(), b' ' * 300 + json.dumps(good).encode()):
            with self.subTest(data=data[:40]):
                self.write(data)
                self.assertIsNone(broker_readiness.read(self.run_dir, self.GENERATION))
        self.write(json.dumps(good).encode(), mode=0o644)
        self.assertIsNone(broker_readiness.read(self.run_dir, self.GENERATION))
        self.path.unlink()
        target = self.run_dir / 'elsewhere'
        target.write_bytes(json.dumps(good).encode())
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        self.assertIsNone(broker_readiness.read(self.run_dir, self.GENERATION))
        self.assertIsNone(broker_readiness.read(self.run_dir / 'missing', self.GENERATION))
        self.assertIsNone(broker_readiness.read(self.run_dir, None))


if __name__ == '__main__':
    unittest.main()
