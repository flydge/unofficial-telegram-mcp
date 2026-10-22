"""The conftest guard: no test may leave the process umask changed for the tests after it."""
from __future__ import annotations

import os

from conftest import SESSION_UMASK, umask_restored


def test_the_guard_runs_around_every_test(request):
    assert '_umask_unchanged' in request.fixturenames


def test_an_unchanged_umask_reports_nothing():
    assert umask_restored(SESSION_UMASK) is None
    assert umask_restored(SESSION_UMASK) is None


def test_a_leaked_umask_is_reported_and_the_session_value_restored():
    leaked = 0o027 if SESSION_UMASK != 0o027 else 0o077
    os.umask(leaked)
    try:
        assert umask_restored(SESSION_UMASK) == leaked
        assert umask_restored(SESSION_UMASK) is None
    finally:
        os.umask(SESSION_UMASK)
