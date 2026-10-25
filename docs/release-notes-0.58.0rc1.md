# 0.58.0rc1 — release candidate, 2026-10-25

[Overview](../README.md) · [Installation](install.md) · [Tool reference](reference.md) · [Roadmap](roadmap.md) · [Licensing](licensing.md)

This candidate adds reply-source families and styled outgoing captions to 0.57.0rc1
([spec](plans/0.58-reply-sources-and-captions-spec.md), [task record](plans/0.58-reply-sources-and-captions-spec.tasks.md),
[changelog](../CHANGELOG.md)). It is a local annotated tag with release-gate evidence; nothing is published.
The owner row (real sources with these families and one approved styled-caption send) waits for the owner
session.

## Available behavior

- **Media TextUrl sources.** A reply may use a document, photo or voice note whose caption carries a hidden link
  (projection v24, under the existing `reply_text_url_targets`); the whole record is shown inertly, so no URL
  is silently sanitized.
- **Custom emoji sources.** Text and media sources with custom emoji (v25/v26) under the new default-off
  `reply_custom_emoji_targets`: the id must be a decimal string within 2^63−1 over exactly one emoji; an
  integer id is refused, and a source heavy with custom emoji that exceeds the 4,096-character display is
  refused, disclosed.
- **Voice-note timestamps.** A voice-note caption with media timestamps (v28) under the new default-off
  `reply_media_timestamp_targets`: each timestamp must match its token and lie within the voice note's
  duration; text, document and photo timestamps stay refused.
- **Styled captions.** `prepare_artifact_send`, `prepare_reply_artifact_send` and `prepare_video_send` take
  optional `outgoing_entities` (the five styles) behind the new default-off `formatted_caption_send`; the
  preview binds a digest over the caption and its styles, the transport accepts only the frozen styles, and
  the read-back is exact. Album items stay plain.
- **Captions that confirm.** A plain caption now confirms when it contains entities Telegram detects itself
  (links, mentions, hashtags); before, such a send read `outcome_unknown`.
- **Profiles.** Both new reply capabilities join the `replies` profile; `formatted_caption_send` joins `send`.
- **One ladder.** Every reply-source projection v1–v23 is frozen in a pinned characterization matrix and served
  from one family table; the matrix is byte-identical before and after.

Package version 0.58.0rc1, public contract version 1, 64 tools. **Tool schemas changed** (each change is listed
in `tests/fixtures/schema_changes/`): `outgoing_entities` on three prepare tools and the new capabilities in the
capability enums. Clients must refresh `tools/list`, and a `runtime.toml` that pins
`expected_schema_fingerprint` is `config_stale` until re-pinned. Deploy matching proxy, broker, policy and skills
together.

## Evidence

- The full isolated-HOME suite and the release gate on the tagged commit, and the consumer-matrix receipt
  (every synthetic cell through the official SDK against the installed wheel) on the same commit.
- The characterization matrix (6,098 rows, pinned by sha256) identical before and after the ladder table; a
  generated family-conjunction matrix; dialog goldens per family and for styled captions; transport and
  read-back tests against the pinned TDLib forms.
- An adversarial review in three lenses with two verifiers per finding and a fix-only check; every confirmed
  finding fixed, recorded in the task record.

## Owner row

| Row | Status |
| --- | --- |
| Real sources (a voice note captioned with a timestamp, custom emoji from a Premium sender or a boosted group, a media caption with a hidden link) and one approved styled-caption send | Owner session |

## Known limits

- A custom emoji record uses the ordinary display, not the inert layer of hidden links: it shows the emoji, its
  id and the provider type; no sticker or emoji file is fetched.
- Real custom-emoji evidence needs a Premium sender or a boosted group (an owner decision).
- Album item captions stay plain.
- Everything listed under Known limits in the [0.57.0rc1 notes](release-notes-0.57.0rc1.md) still applies.

## License and public identifiers

Project-owned code is licensed under **AGPL-3.0-only**; see [licensing](licensing.md) and
[third-party notices](third-party-notices.md). The public name remains **Unofficial Telegram MCP**, a
placeholder until the owner decides; compatibility identifiers are unchanged.
