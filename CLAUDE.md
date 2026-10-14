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

Baseline at ce44a35: 2053 passed, 3 skipped. Dev venv: `python3 -m venv .venv &&
.venv/bin/pip install -e . pytest tomlkit==0.13.3`.

## Live installation on the owner's Mac — do not disturb

A released 0.51.0 broker (venv under `~/.codex/research/telegram-mcp-release051/fix-build-r1/venv`)
serves Codex, Claude Code and Claude Desktop. Never point a client at this checkout's
`.venv`, never run `telegram-search-broker install|uninstall` from it, and never edit the
live `runtime.toml` or TDLib session. Proxy/broker/policy must match or clients refuse.

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
