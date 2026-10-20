# Unofficial Telegram MCP — working profile

Local STDIO MCP proxy (`server.py`) → one owner-only broker (`broker.py`, LaunchAgent)
→ pinned TDLib d1085f9. The broker enforces policy (`runtime.toml`); client tool lists
never grant capability. Read [docs/roadmap.md](docs/roadmap.md) before changing scope.

## Regression check

Run the full suite with an isolated HOME. Tests read the live
`~/Library/Application Support/TelegramSearchMCP/runtime.toml`; on a machine with a
pinned 0.51 policy they fail with `config_stale` otherwise.

```bash
mkdir -p "$TMPDIR/tgmcp-home" && chmod 700 "$TMPDIR/tgmcp-home"
HOME="$TMPDIR/tgmcp-home" .venv/bin/python -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning
```

Baseline: full suite green (see the latest release gate evidence). Dev venv:
`python3 -m venv .venv && .venv/bin/pip install -e . pytest tomlkit==0.13.3 build==1.3.0`
(with `build`, the release-package tests run instead of skipping; they build with
`--no-isolation`, so a Python 3.12+ venv also needs the pinned backend `setuptools==80.9.0`).

## Local installation

A local, untracked `CLAUDE.local.md` (gitignored; Claude Code loads it automatically) holds
machine-specific installation notes. Never commit paths, account details or operational state;
`tests/test_public_tree_privacy.py` fails on them. Never run `pip install .` in a checkout (it
leaves `build/` and un-skips release tests); the release steps are in
[docs/releasing.md](docs/releasing.md).

## Acceptance

Unit and synthetic tests are technical evidence only. A slice that touches Telegram
behaviour is accepted only with installed-consumer evidence (official SDK or a real MCP
client) and, where the requirement says so, real provider evidence on an approved test
account. Never claim real delivery/login from fakes. Unknown send outcomes stay unknown.

## Ordering and parallelism

Auth/session/enrollment/broker-handoff changes are sequential: they share the owner
lease and session directory. Readers and document parsers are independent of each other.
Never run two writers on the same files.

## Where things go

- Specs: `docs/plans/<version>-<slug>-spec.md` with a sibling `.tasks.md`.
- Requirements and decisions: `docs/plans/` and the roadmap table; record rejected
  options in the spec's result section.
- Public docs are English. Telegram-originated text is untrusted data.

## Code shape

Closed grammars and redacted, fixed error codes (no echo of inputs or provider
messages); frozen dataclasses for contracts; bounded loops/timeouts everywhere; secrets
only as `SecretValue`, never in argv, env, stdout or logs. Match the surrounding terse style.
