# 0.55.0rc1 — release candidate, draft

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

**Draft.** The evidence lines below are filled after the release gate; until then this file describes the
candidate's code, not a tagged release.

This candidate adds multiple accounts to 0.54.0rc1
([spec](plans/0.55-multiple-accounts-spec.md), [task record](plans/0.55-multiple-accounts-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner rows (two really authorized accounts, one approved action each) wait for the owner session.

## Available behavior

- **A second account.** The owner runs the current release's `launcher.py account-add <alias>`, then
  `connect --account <alias>` and scans the QR with that account's phone; the page names the alias. An
  alias is `[a-z][a-z0-9]{0,11}`, never `default`, at most eight per installation, and must fit the macOS
  socket-path and the clients' tool-name limits (`alias_path_too_long`, `alias_name_too_long`). See
  [Accounts](install.md#accounts-055-candidate).
- **Everything separate.** Each alias has its own session root
  (`TelegramSearchMCP-accounts/<alias>`), broker LaunchAgent and connect job, socket, `runtime.toml`,
  caches and one TDLib, and its own client entries under the server name `telegram_search_<alias>`: Codex
  with `required = false`, Claude Code in user scope (also in plugin mode; the plugin stays default-only),
  Claude Desktop beside the default's. The alias is fixed in each entry's argument list; tools, schemas and
  the schema fingerprint are the default's.
- **Handles stay with their account.** Every draft, send status, artifact (also through the transfer
  helper's `download`), upload, selection, verified target and cursor of one account is refused by another
  exactly like an unknown handle.
- **The account pin.** Each account's broker pins the Telegram user it first finds authorized, and every
  login applies the same rule before the handoff: `account_mismatch` for another user, `account_duplicate`
  for a user already serving another alias or the default account. The user id is never printed; the
  approval dialog names the alias and the last two digits of the account.
- **Status and capabilities per account.** `status` reports `loaded`, `authorized` and `registered` for
  the default and every alias, never the account; `capabilities … --account <alias>` switches that alias's
  own profiles.
- **Removal moves aside.** `account-remove <alias>` stops the alias's jobs, removes its own client entries
  and its pin, and moves its session to `removed-<alias>-<32 hex>`; nothing is deleted or logged out.
  Rename is a remove plus an add.
- **Upgrade, rollback and uninstall over every account.** One transaction moves the default and every
  alias, with a pending-send gate per broker and a refusal while an alias login runs
  (`login_in_progress`). A rollback below 0.55 refuses while an alias exists (`accounts_present`).
  Uninstall keeps alias sessions and the registry; a later install repairs each alias with its
  `account-add`.
- **The default account unchanged.** Without an alias every path, launchd label and plist, client entry,
  receipt, `runtime.toml` and the schema fingerprint equal 0.54.0rc1's.

Package version 0.55.0rc1, public contract version 1. Tool schemas and the schema fingerprint are
unchanged from 0.54.0rc1. New installer refusal codes are listed in the
[reference](reference.md#installer-refusal-codes). Deploy matching proxy, broker, policy and skills
together.

## Evidence

- The full isolated-HOME suite on the tagged commit: `<filled after the gate>`.
- The release gate and the consumer-matrix receipt on the same commit: `<filled after the gate>`.
- Default-account goldens computed by running v0.54.0rc1; the isolation matrix on two real brokers over
  fake TDLib sessions; the kill sweep over `account-add`, `account-remove` and the upgrade, rollback and
  uninstall over aliases: `<counts filled after the gate>`.
- An independent critic of the slice and its fix rounds, recorded in the task record:
  `<filled after the critic>`.

## Owner rows

| Row | Status |
| --- | --- |
| Add a second account by QR; two really authorized accounts | Owner session |
| One approved action on each account; the dialog names the right account | Owner session |

## Known limits

- Owner-session rows above; every evidence item is unit and synthetic.
- Aliases separate sessions, not users: every alias runs as the same macOS user
  ([security](security.md#accounts-boundary-055-candidate)).
- Each alias runs its own broker and TDLib: memory, TDLib's own session files and up to 1 GiB of artifact
  cache per account.
- Client allowlists, permission rules and hooks keyed on `mcp__telegram_search__*` do not cover alias
  servers (`mcp__telegram_search_<alias>__*`).
- An uninstall with an alias left keeps the registry and the default's pin: installing a release below 0.55
  over it, re-logging the default into another account and then 0.55 again meets `account_mismatch` for the
  default.
- An alias added after an upgrade blocks that upgrade's rollback until it is removed.
- An alias the registry lists without client entries (after an uninstall) gets none until its
  `account-add`.
- An upgrade or rollback waits for a running alias login (`login_in_progress`); the account actions run only
  from the current release (`release_not_current`).
- With the default broker not loaded at `account-add` (`default_check: not_loaded`), the default account is
  pinned only at its broker's next check; an alias logged into the default's own account first blocks the
  default with `account_duplicate` until that alias is removed.
- Everything listed under Known limits in the [0.54.0rc1 notes](release-notes-0.54.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
