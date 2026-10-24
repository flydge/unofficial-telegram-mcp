# 0.56.0rc1 — release candidate, 2026-10-24

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate adds ordered albums to 0.55.0rc1
([spec](plans/0.56-albums-spec.md), [task record](plans/0.56-albums-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner row (one real album sent, read back and approved in the dialog) waits for the owner session.

## Available behavior

- **Albums.** `prepare_album_send`, `send_prepared_album` and `get_album_send_status` (58 tools) send 2 to 10
  of the owner's own artifacts as one ordered album: photos and videos, or documents only, each with an
  optional plain caption and an optional `display_name`. Audio and voice notes are not album items.
- **Capability.** The default-off `album_send` requires `send`, and a video item also requires `video_send`,
  so an album never bypasses that capability. The owner switches it with the `albums` profile
  (`launcher.py capabilities enable albums`).
- **One dialog, one attempt.** The owner sees one dialog listing every item (position, kind, delivered name,
  size and caption), the total and the recipient, bound to one ordered digest; the approval sends exactly
  one `sendMessageAlbum`, never retried. The dialog goes through the one owner-dialog gate, which also holds
  across every account's broker once an alias registry exists.
- **Per-item outcomes.** Each item ends `sent` (with its message id), `failed` or `outcome_unknown` on exact
  provider evidence only; the album is `sent`, `failed`, `partial` or `outcome_unknown`, and nothing is ever
  resent. `get_album_send_status` reconciles unconfirmed items later; a repair is a new draft or a single
  send.
- **Account-bound.** An album draft is a handle of its account: another alias answers it exactly like an
  unknown draft.

Package version 0.56.0rc1, public contract version 1. **Tool schemas changed** (each change is listed in
`tests/fixtures/schema_changes/`): three new tools, and the reply tools' `missing_capabilities` enum gains
`album_send`. Clients must refresh `tools/list`, and a `runtime.toml` that pins `expected_schema_fingerprint`
is `config_stale` until re-pinned. Deploy matching proxy, broker, policy and skills together.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt
  (every synthetic cell through the official SDK against the installed wheel) on the same commit.
- Transport tests of the closed request shape against the pinned TDLib header; observation tests for
  update-before-result order, conflicts on any mismatch, per-slot terminal updates bound by chat and
  temporary id, and late updates after expiry; staging tests showing single sends unaffected; dialog goldens
  for 2, 5 and 10 items; the album handle in the 0.55 isolation matrix.
- An adversarial review in three dimensions and a re-review of its fixes with two fresh-lens critics, each
  finding checked by two verifiers; every confirmed finding fixed, recorded in the task record.

## Owner row

| Row | Status |
| --- | --- |
| One real album sent and read back; the dialog approved by the owner; how Telegram clients show several captions | Owner session |

## Known limits

- Large albums can outlast the 330-second send budget, so `outcome_unknown` is common for them; the response
  says so and `get_album_send_status` reconciles later. Pending items survive a restart in TDLib's message
  database and can finish after `outcome_unknown` was reported.
- Captions are plain text; formatted album captions come with 0.58 at the earliest.
- Everything listed under Known limits in the [0.55.0rc1 notes](release-notes-0.55.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
