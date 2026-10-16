# Changelog

## 0.52.0.dev0 — unreleased

- Add F14 exact forward/copy of one message (development; [spec](docs/plans/0.53-forward-copy-spec.md)):
  tools `prepare_forward_send` and `send_prepared_forward` (49 tools with this slice) and the default-off
  `forward_send` capability, which also requires `send`. `mode="forward"` shows the original
  sender; `mode="copy"` sends a new message without attribution (`remove_caption` only for
  copied media). Preparation returns a complete preview with source and preview hashes, and
  reports secret chats as unavailable and refuses hidden formatting and link targets, reply
  markup, auto-deleting, protected or non-forwardable sources and, in forward mode, forwarded sources. Sending
  revalidates recipient and source, asks the local owner to confirm the same facts and makes
  one closed-shape `forwardMessages` attempt with no retry; `get_send_status` reports the
  outcome. Re-preparing supersedes the earlier draft; the generic draft tools do not expose
  forward drafts. Verified with a synthetic provider only; real approved delivery is pending
  the owner. See [reference](docs/reference.md#exact-forwardcopy-f14) and
  [security](docs/security.md#exact-forwardcopy-boundary-f14-candidate).

- Harden F14 after the independent re-review: the forward dialog marks more invisible and
  look-alike characters as `⟦U+XXXX⟧`, encloses the text, entity spans and link-preview URLs in
  `« »` under one stated rule, link-preview option and link-card URLs must be a Url entity or a
  whole token of the visible text, and names that sanitizing would alter are refused.

- Add F15a native video send (development; [spec](docs/plans/0.53-native-video-spec.md)):
  tools `prepare_video_send` and `send_prepared_video` (51 tools) and the default-off
  `video_send` capability, which also requires `send`. One owned MP4/MOV artifact of at most
  64 MiB is read by a bounded stdlib ISO-BMFF probe (duration, dimensions, H.264 or HEVC codec,
  audio and streaming layout, shown as declared by the file, not decoded); fragmented,
  unknown-codec, multi-track and otherwise unusual files refuse as `unsupported_video` and
  larger ones as `video_too_large`. Sending revalidates recipient and artifact bytes, asks the
  local owner to confirm the same facts in a dedicated dialog and makes one closed-shape
  `sendMessage(inputMessageVideo)` attempt with a 120-second confirmation wait and no retry;
  `get_send_status` reports the outcome by correlation. Re-preparing for the same artifact and
  recipient supersedes the earlier draft; the generic draft tools do not expose video drafts.
  Verified with generated MP4/MOV boxes and a synthetic provider only; a real approved send is
  pending the owner. See [reference](docs/reference.md#native-video-send-f15a) and
  [security](docs/security.md#native-video-boundary-f15a-candidate).

- Fix a misleading `expired` for sends that were already attempted: when artifact authority is
  lost after the claim (client release during the confirmation wait, an account switch), the
  attempting `send_prepared_artifact` (documents, photos, voice notes and artifact replies) or
  `send_prepared_video` call now returns `outcome_unknown` ("current receipt authority is
  unavailable; do not retry automatically"), or its local failure, instead of `expired`, which
  invited a duplicate send. Later calls without the original authority still read `expired`.
  Display names are now NFKC-normalised before directory parts are removed, so a document named
  `a／b.txt` becomes `b.txt` instead of failing at staging, and a `selection_id` on an operation
  without selection support is refused as `invalid_request` instead of failing internally when
  `retained_selections` was enabled.

- Add F17a archive/unarchive of one chat and plain-text edit of one own text message (development;
  [spec](docs/plans/0.54-archive-and-edit-spec.md)): tools `prepare_chat_archive`,
  `apply_prepared_archive`, `prepare_text_edit` and `apply_prepared_text_edit` (55 tools) and the
  default-off, independent capabilities `archive_chats` and `edit_own_text`, each of which also requires
  `send`. An archive preview shows the chat, its current and target list and Telegram's return-to-Main
  behaviour; an edit preview shows the chat and its kind, the complete current and new text, the
  current formatting that a plain-text edit removes and the edit date. Applying revalidates the chat or
  message and its edit permission, asks the local owner to confirm the same facts in a dedicated dialog
  and makes one closed-shape `addChatToList` or `editMessageText` attempt with no retry. A move is
  `applied` only after the target list is observed, an edit only when the returned message carries
  exactly the new text and a non-zero edit date; anything else is `outcome_unknown`, and only a 4xx
  provider error is `failed`. A new text with leading or trailing whitespace, or with a tab, U+2028 to
  U+202E, U+030A, U+0333 or U+033F (which Telegram trims or rewrites), is refused instead of silently
  changed; sponsored or announcement list positions are not list membership. The `_manifest`
  `forbidden` list now names exactly these two mutations instead of all edits. `get_send_status` reads
  coarsely and never `sent` for these drafts, the generic draft tools do not expose them, and enabling
  either capability also lets its prepare tool reveal that chat's or message's content. The earlier 51
  tool schemas are unchanged. Verified with a synthetic provider only; a real archive/unarchive and a
  real edit are pending the owner. See [reference](docs/reference.md#archiveunarchive-and-own-text-edit-f17a)
  and [security](docs/security.md#archive-and-own-text-edit-boundary-f17a-candidate).

- Add QR-first Telegram login to the private candidate: `connect` opens a loopback
  QR screen on the Mac, `wait` reports progress, `cancel` closes the screen, 2FA is typed only on
  that page, `api-credentials` stores the owner's own API pair (native Terminal), and a
  bundle assembled with the application identity needs no API ID/hash entry. The installer now
  registers the MCP server in both Claude Code (user scope, through the `claude` CLI at
  fixed locations, foreign entries refused, `claude_code: not_found` when absent) and Codex,
  copies the paired skills to `~/.agents/skills` (and to `~/.claude/skills` when Claude Code is
  present), and returns an activation command ending in `connect`. The bundle assembler injects `app-identity.json`
  only with the explicit `--app-identity-from-keychain` flag. The Terminal `setup` flow
  remains as the fallback; the setup skill is rewritten for the connect/wait flow.
  Verified with synthetic fakes only; real clean-user acceptance (phase F) is pending.

- Record the approved [QR-first onboarding requirement](docs/plans/0.52-qr-onboarding.md)
  as the next priority and a mandatory 0.52.0 release gate. Application-supplied
  API credentials replace mandatory end-user API application creation/entry in
  the target flow. This entry records the decision; the implementation is the entry
  above.

- Add authenticated offline macOS ARM64 first-install bundle admission,
  recoverable owned runtime/registration/paired-skills installation and an
  isolated receipt-bound launcher integrated with the preserved default session.
- Private installation ends at `activation_required`. Real-account acceptance of native
  onboarding, fresh Codex search, public distribution and source completeness remain open.

- Preserve client-owned artifact grants across the supported local lifecycle and
  add retained selections for bounded conversational message reuse.
- Add issuer-owned local upload authorities, stdin-only CLI handoff and original
  client result retrieval/cancellation, including idempotent receipt recovery after
  a lost response without copying bytes or renewing storage.
- Verify the bounded B2C slice with the full guarded suite, package/install checks,
  independent review and installed official SDK auto/legacy scenarios using
  synthetic data. No real Telegram send or full F18 acceptance is claimed.
- Development was paused after B2C at the owner's request and later resumed (QR onboarding,
  F14, F15a above); the unfinished scope and evidence limits are in
  [the roadmap](docs/roadmap.md). No new release is tagged.

## 0.51.0 — 2026-10-13

- Export the current 0.51.0 implementation onto the public contributor lineage, preserving Unofficial Telegram MCP branding and practical installation instructions.
- Document the existing opt-in topic text/reply and five-style outgoing text behavior, conservative reply-source evidence subsets, and release preparation diagnostics.
- Correct stale tool counts and the 0.51.0 policy example; include current reference, security and release-preflight documents in package data.
- Handle corrupt PNG checksums as reader errors and correlate formatted Saved Messages receipts with the revalidated account's own topic, without changing the outgoing destination or retry policy.
- Record the completed bounded live SDK/Inspector/native acceptance and exact corrected self-send receipt, while preserving the historical unknown outcome and remaining whole-roadmap limits.
- License project-owned code under AGPL-3.0-only, retain upstream notices, and document PyMuPDF as the current reason and a possible future PDF/license change.
- Publish the original development scope and current completed/remaining roadmap as linked, version-controlled documentation. See [release notes](docs/release-notes-0.51.0.md).

## 0.35.0 — published baseline

The previously published `main` at `796f95d22ae7016ae8c7a313e96e15233aba7531` contains the Unofficial Telegram MCP rebrand. This changelog does not reconstruct intermediate private development commits or assert that they were publicly released.
