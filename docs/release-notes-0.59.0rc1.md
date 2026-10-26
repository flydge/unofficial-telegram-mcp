# 0.59.0rc1 — final acceptance candidate, 2026-10-26

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This is the final acceptance candidate of the completion plan, on top of 0.58.0rc1
([spec](plans/final-acceptance-spec.md), [task record](plans/final-acceptance-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner session (signed build, clean macOS user, real approval dialogs, a second real account, Claude
Desktop, migration of the owner's installation, Gpub) is prepared in the owner decision sheet and has not run.
The published version is the owner's decision.

## Available behavior

- **Controlled TDLib upgrade.** [`docs/tdlib-upgrade.md`](tdlib-upgrade.md) gives the triggers and the ordered
  steps for moving off the pinned TDLib commit d1085f9: pinning a candidate, a reproducible from-source build
  route (`packaging/build_tdlib_source.py`), the allowlist diff, the media schema pins, re-signing and repinning,
  the suite and the consumer matrix, the owner's real read-only check, and a rollback path that includes saving
  and restoring the TDLib session, since a launcher rollback alone does not undo a database migration. No
  upgrade happens without a trigger; the shipped pin is unchanged.
- **Consumer matrix over every tool.** The installed official SDK runs 55 synthetic cells across all 64 tools
  (every refusal text, cursors, caps, images, recovery, approval denial by dialog timeout, and the installed
  bundle's install, upgrade, rollback and uninstall), plus an MCP Inspector column and real Codex and Claude
  Code agent columns on a synthetic broker, decided on the broker's own transcript. The receipt is bound to the
  commit, the wheel, the schema fingerprint and the agent CLI versions; a deliberate schema change fails it.
- **Fixes.** `retain_selection` accepts TDLib's own entity shape (it refused every message with formatting); a
  styled or topic `prepare_text_send` and a styled `update_draft` without their capability answer
  `capability_disabled` with `missing_capabilities` and `next_action: enable_capability`, like the reply tools;
  a first install over a session an uninstall kept moves its `runtime.toml` version line onto the installed
  release from an equal or older one, so a reinstall at another version no longer meets `config_stale`.
- **Transport scope.** STDIO-only transport, the Unix-socket broker and the loopback-only connect screen stay
  pinned by tests; Streamable HTTP is not applicable without a demonstrated cloud consumer.

Package version 0.59.0rc1, public contract version 1, 64 tools. **Tool schemas changed** (each change is listed in
`tests/fixtures/schema_changes/`): the typed `capability_disabled` results of `prepare_text_send`,
`update_draft` and `refresh_draft`. Clients must refresh `tools/list`, and a `runtime.toml` that pins
`expected_schema_fingerprint` is `config_stale` until re-pinned. Deploy matching proxy, broker, policy and skills
together.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt with
  the synthetic, Inspector, Codex and Claude Code columns on the same commit.
- The TDLib rehearsal: independent from-source rebuilds of d1085f9 at different build paths came out
  byte-identical (natives and the lock-shaped document); the shipped, Homebrew-derived pin is reproduced from
  the installed keg, not by a rebuild (the owner confirms this reading of the criterion).
- A mounted-DMG `Install.command --prepare-only` run of an unsigned build of this line: verified, nothing
  installed.
- The [requirement-by-requirement audit](plans/final-audit.md) of P0–P3 and F1–F19 (10 met, 11 met with an owner
  remainder, 2 partly met: P3 waits for the history decision, the final gate and Gpub; F19 for the final gates),
  reviewed by independent critics in several rounds, each finding checked by two verifiers.

## Owner session

The [owner decision sheet](plans/owner-decision-sheet.md) lists every decision in the order the session needs
them (history and commit identity first, then the published version, legal review and the API identity before
the signed build) and every owner-only row in one ordered check list.

## Known limits

- Every owner remainder in the audit is open: real dialogs, real sends on the changed send paths, the second
  account, the clean macOS user, Claude Desktop, the signed and notarized build with Gatekeeper, and the
  migration of the owner's installation.
- Signed builds write `publication: unreleased` until the source-completion work is done, so the plugin and curl
  installation paths refuse until then (an owner decision).
- Everything listed under Known limits in the [0.58.0rc1 notes](release-notes-0.58.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
