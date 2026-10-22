# 0.53.0rc1 — release candidate, 2026-10-22

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate adds client coverage and the distribution pipeline to 0.52.0
([spec](plans/0.53-client-distribution-spec.md), [changelog](../CHANGELOG.md)). It is a local annotated tag
with release-gate evidence; nothing is published. The signed and notarized build, the clean-user install,
the owner's migration and publication are owner-session rows, so the final 0.53.0 comes after that session.

## Available behavior

- **Clients.** The installer registers Claude Desktop (one owned `mcpServers.telegram_search` entry), Claude
  Code in user scope or through the `telegram-local` plugin and its repository marketplace (`--claude-code
  user|plugin|skip`), and Codex. Neighbouring entries are kept byte for byte.
- **Policy-filtered tool list.** `tools/list` shows only the tools the loaded policy enables (15 of 55 under
  the default `read` + `artifacts`); `_manifest` reports `available_tools` and `tool_capabilities`, and the
  server sends client-neutral instructions. The schema fingerprint still covers the full registration and is
  unchanged from 0.52.0.
- **Upgrade, rollback, uninstall, adoption.** Re-running a newer kit upgrades in place; `rollback` returns to
  the previous release; `uninstall` removes only owned objects; `--adopt-legacy` takes over a 0.51 or 0.52
  installation. The Telegram session, enrollment and Keychain are never written. Every step is journaled:
  a kill at any point resumes without double effects, a refusal after the broker stop restores the previous
  broker, and a pending send blocks the change before any client is touched.
- **Distribution pipeline.** Reproducible payload builder (two builds are identical), native lock and
  manifest v2 with a reviewed pin table that approves the publisher Team ID `256X5MUA6S`, inside-out signing
  with the hardened runtime, notarization and stapling scripts, `Install.command` (install and QR login in
  one double-click), and one `release-pins.json` schema whose committed copy at the release tag is the trust
  root of the curl and plugin paths.
- **Product identity.** Every user-visible name comes from one module, so a rename is one edit.

Package version 0.53.0rc1, public contract version 1. The tool schemas are unchanged from 0.52.0.

## Evidence

- Full isolated-HOME suite and the release gate on the tagged commit (`evidence.json` re-derives the commit,
  tree, sdist and wheel hashes from `git archive`).
- Installer: a kill sweep over every journaled effect, a seeded random sequence test over upgrades, repeats,
  rollbacks, uninstalls, conflicts, kills, logins and late refusals, and five independent critic rounds whose
  findings are recorded in the [task record](plans/0.53-client-distribution-spec.tasks.md).
- Ad hoc signing dry run and a mounted-DMG run on the build Mac; the pending-send probe against real v0.51.0
  and v0.52.0 brokers.

## Known limits

- **Owner-session rows:** the Developer ID signed and notarized build and its Gatekeeper observation
  (`Install.command` is not code-signed), the clean-user install, the owner's migration (Claude Desktop and
  Codex still point at older installs), the product name, the application identity of the public bundle,
  and publication.
- The plugin's files come from whatever commit the user's marketplace clone holds; the docs say how to
  inspect them against the release tag.
- A parked change (refused after the broker stop) is finished by repeating the command or by `uninstall`;
  `rollback` refuses until then.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
