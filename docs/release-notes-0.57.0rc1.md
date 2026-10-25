# 0.57.0rc1 — release candidate, 2026-10-25

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate adds own-message actions to 0.56.0rc1
([spec](plans/0.57-own-message-actions-spec.md), [task record](plans/0.57-own-message-actions-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner row (real caption, styled, pin, unpin and delete actions approved in the dialog) waits for the owner
session.

## Available behavior

- **Caption edit.** `prepare_caption_edit` and `apply_prepared_caption_edit` replace or remove the caption of one
  own media message (photo, video, animation, document, audio, voice note), optionally styled, with one closed
  `editMessageCaption` attempt. Edge whitespace and input that NFKC would change are refused, never rewritten.
- **Styled text edit.** `prepare_text_edit` takes optional `outgoing_entities` behind `edit_own_styled_text` (a
  listed schema change); plain edits are unchanged.
- **Pin and unpin.** `prepare_pin_change` and `apply_prepared_pin_change` pin or unpin one message silently and
  for everyone; the dialog says a visible service message is posted, and the final state is observed or read back
  (`already_pinned`, `not_pinned`, `failed`).
- **Delete one own message.** `prepare_message_delete` and `apply_prepared_message_delete` delete exactly one of
  the owner's own messages for everyone, irreversibly, with one closed `deleteMessages` attempt right after a
  read under the same provider lock. The dialog has a `Delete` button and states that the deletion is
  irreversible, removes the message for every participant and unpins it. Saved Messages, deletion for yourself
  only, other people's, scheduled and sending messages are refused; bulk and history deletes stay unreachable.
- **Capabilities.** `edit_own_caption`, `edit_own_styled_text`, `pin_messages` and `delete_own_message` are each
  default off and require `send` and `read`. The two edits join the `archive_edit` profile; `pin` and `delete`
  are their own profiles, so enabling edits never grants a pin or a deletion.
- **Exact verification.** A styled edit or formatted send reads `applied`/`sent` only when the result carries
  exactly the reviewed styles, accepting TDLib's split form of a span and only the entities Telegram detects
  itself (links, mentions, hashtags, media timestamps on their whole token). Plain replies and topic sends now
  confirm when the text contains such entities.
- **One dialog per draft.** A second concurrent apply of the same draft answers `dialog_unavailable` instead of
  showing a second dialog, for every send and apply kind.

Package version 0.57.0rc1, public contract version 1, 64 tools. **Tool schemas changed** (each change is listed
in `tests/fixtures/schema_changes/`): six new tools, `prepare_text_edit`'s styles, and the new capabilities in
the `missing_capabilities` enums. Clients must refresh `tools/list`, and a `runtime.toml` that pins
`expected_schema_fingerprint` is `config_stale` until re-pinned. Deploy matching proxy, broker, policy and skills
together.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt
  (every synthetic cell through the official SDK against the installed wheel) on the same commit.
- Transport tests of every closed shape and of the exact allowlist, change and refusal sets against the pinned
  TDLib; fake providers modelled on the pinned TDLib source for local-first deletion, pin races and split style
  spans; dialog goldens for every action with and without an alias registry; the new draft ids in the 0.55
  isolation matrix.
- An adversarial review in three lenses, a re-review with a fresh delete-safety critic and a fix-only check, each
  finding checked by two verifiers; every confirmed finding fixed, recorded in the task record.

## Owner row

| Row | Status |
| --- | --- |
| Real caption, styled text, pin, unpin and delete actions on a test account, each approved in the dialog | Owner session |

## Known limits

- A deletion reads `applied` when TDLib answers ok after its locked local read; that read is TDLib's local copy,
  so a deletion for yourself made on another device that this session has not yet received can still read
  `applied` after a server-side no-op.
- An unknown deletion may still complete later, even after a restart (TDLib logs it); no read confirms it.
- After an edit that adds a media timestamp, the stored message carries that entity, so these tools refuse to
  edit it again (`hidden_source_content`).
- A caption with a hidden link, custom emoji or mention by name is refused as hidden content by the caption
  and styled edits until the 0.58 families land.
- Everything listed under Known limits in the [0.56.0rc1 notes](release-notes-0.56.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
