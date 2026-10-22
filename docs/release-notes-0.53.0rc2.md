# 0.53.0rc2 — release candidate, 2026-10-22

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate replaces [0.53.0rc1](release-notes-0.53.0rc1.md), whose own kit could not be installed by
any path: the native seed refused the version `0.53.0rc1`. It carries the fixes of the independent
re-review of rc1 ([task record](plans/0.53-client-distribution-spec.tasks.md), "Release candidate
0.53.0rc2"). Nothing is published. The signed and notarized build, the clean-user install, the owner's
migration and publication are owner-session rows, so the final 0.53.0 comes after that session.

## Available behavior

Everything listed for [0.53.0rc1](release-notes-0.53.0rc1.md#available-behavior), with these corrections:

- **One version grammar.** Every site that admits a version (the native seed, the plugin's `bin/install`,
  `release-pins.json`, the archive index, the installer's order, the launcher generator and the release
  gate) holds one rule: `X.Y.Z`, optionally `aN`, `bN` or `rcN`, optionally `.devN`, without leading zeros.
  Release tags are `v<version>` for a release or an `rcN` candidate. `release_build.py` now runs the kit's
  own seed on the kit's own index before anything is signed for notarization (`kit_failed` otherwise).
- **No dead ends in the installer.** A skill copy touched after the change's `receipt.json` is written stays
  as the user left it and never stops the repeat (`uninstall` reports an edited one `kept`), for a first
  install as well as an upgrade or rollback; after an upgrade's or rollback's commit a changed Codex table
  no longer stops it either. A broker plist removed by hand while a change is unfinished is written anew by
  the repeat, or restored from the transaction's copy by a refused repeat's restore. The previous plist
  bootstrapped again between the stop and the rewrite (a concurrent `connect`) is stopped like any other.
  Repeated refusals in the instant of the Codex write no longer use up its attempts.
- **Uninstall over a parked change** removes a Claude Code or Claude Desktop entry the change wrote before
  an interruption cut off its outcome record, while it is exactly what was written.
- **Pending-send gate.** The broker's health now counts sends in flight, approval dialogs open and send
  outcomes not yet settled; the installer refuses `pending_send_unresolved` while any of them, or any client
  context, exists. Against a 0.51 or 0.52 broker (no such count) a connected context is an unknown answer.

Package version 0.53.0rc2, public contract version 1. The tool schemas are unchanged from 0.52.0.

## Evidence

- Unit, synthetic and real-broker tests, among them a test that compiles the shipping seed with the release
  build's flags and runs it against every entry of the version table, and the pending-send probe against a
  real broker on a temporary socket with a send in flight, a dialog open and an `outcome_unknown` held. The
  full isolated-HOME suite must show 0 failed and 0 skipped; the release gate re-runs it on the tagged commit.
- The release gate with its bundle stage runs on the tagged commit and records its run in `evidence.json` and
  the tag annotation; until that commit is tagged and gated, this candidate has no gate evidence of its own.
  rc1's gate evidence (3116 passed, 0 skipped, bundle stage `adhoc-dry-run`) is for rc1 only, and that gate
  never ran the seed on the kit.
- Independent reviews: round 1, and re-reviews of rounds 1+2, of round 3, of round 4 and of rc1 (which
  covered round 5); every one found fix-needed, and its findings were fixed in the next round (task record,
  Critic). Rounds 2 to 5 and this round are fix rounds by the author. This round also fixes the pending-send
  finding of the 0.54 safety review, which applies to this installer too.
- Not re-reviewed: this round's fixes. No mounted-DMG or `Install.command --prepare-only` run exists for rc1
  or rc2; the one recorded ran on 0.53.0.dev0 and predates later installer changes.

## Known limits

- **Critic gate open:** spec criterion 14 is not met until an independent review of this round's changes
  finds no unresolved high issue; releasing.md pre-cut condition 2 is disclosed here instead.
- **Owner-session rows:** the Developer ID signed and notarized build and its Gatekeeper observation
  (`Install.command` is not code-signed), the clean-user install, the owner's migration (Claude Desktop and
  Codex still point at older installs), the product name, the application identity of the public bundle,
  and publication.
- The plugin's files come from whatever commit the user's marketplace clone holds; the docs say how to
  inspect them against the release tag.
- A parked change (refused after the broker stop) is finished by repeating the command or by `uninstall`;
  `rollback` refuses until then. Each skill copy is written at most eight times within one upgrade: removing
  or replacing the same copy before each of eight repeats of one unfinished upgrade leaves `uninstall` as
  the only way to end it (`recovery_limit`).
- Open lows from earlier reviews: after a `_manifest` schema mismatch `tools/list` still lists every enabled
  tool; the server instructions' recovery line names the telegram-setup skill (absent in Claude Desktop)
  and has no `config_stale` step; a v1 native lock is admitted outside the approved-pin table;
  `repin_native.py` does not check that the signed natives re-sign its unsigned inputs; git replace refs
  could change exported release files.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
